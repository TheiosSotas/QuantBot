import queue
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from conftest import NOW, decision, quote, seed, snapshot

from quantbot.ai import Job, ModelWorker, Result
from quantbot.app import Runner


class FakeWorker:
    def __init__(self):
        self.results = queue.Queue()
        self.jobs = []

    def ready(self):
        return not self.jobs

    def submit(self, job):
        self.jobs.append(job)
        return True


def make_runner(config, engine, worker=None, price=80, data=None, clock=lambda: NOW):
    market = SimpleNamespace(
        fetch_quote=lambda: quote(price), fetch_snapshot=lambda _: data or snapshot(price)
    )
    return Runner(config, engine, market, worker or FakeWorker(), clock=clock)


class FakeClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_model_failure_cannot_skip_breached_stop(config, engine):
    seed(engine)
    worker = FakeWorker()
    worker.results.put(Result(Job("DECISION", snapshot(), {}, NOW, 0), error="Timeout"))
    make_runner(config, engine, worker).step()
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["portfolio"]["cash_usdt"] == 9980


def test_failed_schema_repair_is_audited_and_does_not_skip_existing_stop(config, engine):
    seed(engine)
    worker = FakeWorker()
    worker.results.put(
        Result(
            Job("DECISION", snapshot(), {}, NOW, 0),
            error="ModelDecisionError: confidence must be between 0 and 1",
        )
    )
    make_runner(config, engine, worker).step()
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["portfolio"]["cash_usdt"] == 9980
    assert engine.store.decision_history()[-1]["reason_code"] == "INVALID_MODEL_OUTPUT"


def test_bad_indicators_cannot_skip_breached_stop(config, engine):
    seed(engine)
    make_runner(config, engine, data=snapshot(80, atr=0)).step()
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["system"]["status"] == "DATA_ERROR"


def test_blocked_real_model_worker_does_not_block_protection(config, engine):
    entered, release = threading.Event(), threading.Event()

    def blocked(*_):
        entered.set()
        release.wait(3)
        return decision("HOLD")

    worker = ModelWorker(SimpleNamespace(consult=blocked))
    worker.start()
    worker.submit(Job("DECISION", snapshot(), {}, NOW, 0))
    try:
        assert entered.wait(1)
        seed(engine)
        started = time.monotonic()
        make_runner(config, engine, worker).step()
        assert time.monotonic() - started < 1
        assert engine.state["portfolio"]["side"] == "FLAT"
    finally:
        worker.close()
        release.set()
        worker.thread.join(2)


def test_successful_old_model_result_cannot_reopen_closed_position(config, engine):
    seed(engine)
    worker = FakeWorker()
    worker.results.put(Result(Job("DECISION", snapshot(), {}, NOW, 0), value=decision()))
    make_runner(config, engine, worker).step()
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["system"]["model_status"] in ("POSITION_CHANGED", "PENDING")


def test_model_is_requested_once_per_closed_candle(config, engine):
    worker = FakeWorker()
    runner = make_runner(config, engine, worker, price=100)
    runner.step()
    worker.jobs.clear()
    runner.step()
    assert worker.jobs == []


def test_model_submission_uses_fresh_price_even_with_cached_indicators(config, engine):
    worker = FakeWorker()
    make_runner(config, engine, worker, price=110, data=snapshot(100)).step()
    assert worker.jobs[0].snapshot.current_price == 110


def test_transient_model_failure_retries_once_then_applies_result(config, engine):
    clock, worker = FakeClock(), FakeWorker()
    price = {"value": 100}
    market = SimpleNamespace(
        fetch_quote=lambda: quote(price["value"], now=clock.value),
        fetch_snapshot=lambda _: snapshot(price["value"]),
    )
    runner = Runner(config, engine, market, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    assert first.deadline_monotonic is not None
    worker.jobs.clear()
    worker.results.put(Result(first, error="ConnectionError: offline", retryable=True))
    runner.step()
    assert worker.jobs == []
    assert engine.store.decision_history()[-1]["reason_code"] == "MODEL_ERROR"

    price["value"] = 105
    with engine.lock:
        engine.state["portfolio"]["cash_usdt"] += 1
    clock.advance(5)
    runner.step()
    second = worker.jobs[-1]
    assert second.snapshot.candle_id == first.snapshot.candle_id
    assert second.snapshot.current_price == 105
    assert second.requested_at == first.requested_at
    assert second.revision == first.revision
    assert second.deadline_monotonic == first.deadline_monotonic
    assert second.portfolio["cash_usdt"] == first.portfolio["cash_usdt"] + 1
    assert second.portfolio == engine.snapshot()["portfolio"]

    worker.jobs.clear()
    worker.results.put(Result(second, value=decision()))
    runner.step()
    assert engine.state["portfolio"]["side"] == "LONG"
    assert runner.retry_state is None
    runner.step()
    assert len(worker.jobs) == 0


def test_second_transient_failure_is_audited_and_never_submitted_a_third_time(config, engine):
    clock, worker = FakeClock(), FakeWorker()
    runner = make_runner(config, engine, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(first, error="Timeout: first failure", retryable=True))
    runner.step()
    assert len(engine.store.decision_history()) == 1

    clock.advance(5)
    runner.step()
    second = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(second, error="Timeout: second failure", retryable=True))
    runner.step()
    clock.advance(10)
    runner.step()

    assert worker.jobs == []
    assert runner.retry_state is None
    assert [row["reason_code"] for row in engine.store.decision_history()] == ["MODEL_ERROR", "MODEL_ERROR"]


def test_permanent_model_failure_is_not_retried(config, engine):
    clock, worker = FakeClock(), FakeWorker()
    runner = make_runner(config, engine, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(first, error="ValueError: invalid schema"))
    runner.step()
    clock.advance(10)
    runner.step()
    assert worker.jobs == []
    assert runner.retry_state is None


def test_retry_is_not_submitted_after_original_decision_deadline(config, engine):
    config = replace(config, max_decision_age=6)
    clock, worker = FakeClock(), FakeWorker()
    runner = make_runner(config, engine, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(first, error="Timeout: offline", retryable=True))
    runner.step()
    assert runner.retry_state["retry_at"] == NOW + 5
    clock.advance(6)
    runner.step()
    assert worker.jobs == []
    assert runner.retry_state is None


def test_retry_is_discarded_when_position_revision_changes(config, engine):
    clock, worker = FakeClock(), FakeWorker()
    runner = make_runner(config, engine, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(first, error="Timeout: offline", retryable=True))
    runner.step()
    with engine.lock:
        engine.state["position_revision"] += 1
    clock.advance(5)
    runner.step()
    assert worker.jobs == []
    assert runner.retry_state is None
    assert engine.store.decision_history()[-1]["reason_code"] == "POSITION_CHANGED"
    assert engine.snapshot()["system"]["model_status"] == "CANCELLED"
    assert engine.snapshot()["system"].get("model_error") is None


def test_new_candle_starts_a_fresh_submission_budget(config, engine):
    clock, worker = FakeClock(), FakeWorker()
    current = {"snapshot": snapshot()}
    market = SimpleNamespace(fetch_quote=lambda: quote(100), fetch_snapshot=lambda _: current["snapshot"])
    runner = Runner(config, engine, market, worker, clock=clock)
    runner.step()
    first = worker.jobs[-1]
    worker.jobs.clear()
    worker.results.put(Result(first, error="Timeout: offline", retryable=True))
    runner.step()
    current["snapshot"] = replace(snapshot(), candle_id=snapshot().candle_id + 3_600_000)
    clock.advance(1)
    runner.step()
    next_job = worker.jobs[-1]
    assert next_job.snapshot.candle_id == current["snapshot"].candle_id
    assert next_job.requested_at == clock.value
    assert runner.retry_state["submissions"] == 1
