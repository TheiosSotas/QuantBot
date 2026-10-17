import queue
import threading
import time
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


def make_runner(config, engine, worker=None, price=80, data=None):
    market = SimpleNamespace(
        fetch_quote=lambda: quote(price), fetch_snapshot=lambda _: data or snapshot(price)
    )
    return Runner(config, engine, market, worker or FakeWorker(), clock=lambda: NOW)


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
