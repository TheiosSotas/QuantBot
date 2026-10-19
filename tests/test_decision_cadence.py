"""Minute/five-minute evaluations must not be gated by the indicator candle."""

import json
import queue
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import NOW, decision, quote, seed, snapshot

from quantbot.ai import ModelClient, ModelDecisionError, Result
from quantbot.app import Runner
from quantbot.config import Config
from quantbot.control import Control
from quantbot.execution import PaperEngine
from quantbot.research.advisors import Advice
from quantbot.research.replay import run_replay
from quantbot.research.synthetic import demo_datasets
from quantbot.storage import Store


class Worker:
    def __init__(self):
        self.jobs = []
        self.results = queue.Queue()

    def ready(self):
        return not self.jobs

    def submit(self, job):
        self.jobs.append(job)
        return True


@pytest.mark.parametrize("seconds", [60, 300])
def test_paper_rechecks_same_hour_with_fresh_price_and_survives_restart(tmp_path, seconds):
    config = Config(data_dir=tmp_path, decision_interval_seconds=seconds)
    clock = {"now": NOW, "price": 100}
    market = SimpleNamespace(
        fetch_quote=lambda: quote(clock["price"], now=clock["now"]),
        fetch_snapshot=lambda _: snapshot(),  # Same cached hourly metrics throughout.
    )
    worker = Worker()
    store = Store(config)
    try:
        engine = PaperEngine(config, store, clock=lambda: clock["now"])
        runner = Runner(config, engine, market, worker, clock=lambda: clock["now"])
        runner.step()
        first = worker.jobs.pop()
        worker.results.put(Result(first, value=decision("HOLD")))
        runner.step()
        runner.step()
        assert worker.jobs == []
        persisted_equity = engine.state["portfolio"]["total_equity"]
    finally:
        store.close()

    store = Store(config)
    try:
        engine = PaperEngine(config, store, clock=lambda: clock["now"])
        runner = Runner(config, engine, market, worker, clock=lambda: clock["now"])
        runner.step()
        assert worker.jobs == []  # Restart must not replay a completed interval.
        clock.update(now=(int(NOW // seconds) + 1) * seconds + 1, price=105)
        runner.step()
        second = worker.jobs.pop()
        assert second.snapshot.candle_id == first.snapshot.candle_id
        assert second.snapshot.current_price == 105
        worker.results.put(Result(second, value=decision("BUY")))
        runner.step()
        assert engine.state["portfolio"]["side"] == "LONG"
        assert len(store.decision_history()) == 2
        assert engine.state["portfolio"]["total_equity"] == persisted_equity
        assert store.decision_history()[-1]["decision_interval_seconds"] == seconds
    finally:
        store.close()


@pytest.mark.parametrize("seconds", [60, 300])
def test_invalid_output_does_not_wait_for_next_hour(tmp_path, seconds):
    config = Config(data_dir=tmp_path, decision_interval_seconds=seconds)
    now = [NOW]
    worker = Worker()
    market = SimpleNamespace(fetch_quote=lambda: quote(now=now[0]), fetch_snapshot=lambda _: snapshot())
    store = Store(config)
    try:
        engine = PaperEngine(config, store)
        runner = Runner(config, engine, market, worker, clock=lambda: now[0])
        runner.step()
        first = worker.jobs.pop()
        worker.results.put(Result(first, error="ModelDecisionError: Empty model response"))
        runner.step()
        assert worker.jobs == []
        assert "Empty model response" in store.decision_history()[-1]["detail"]
        now[0] = (int(NOW // seconds) + 1) * seconds + 1
        runner.step()
        assert len(worker.jobs) == 1
        assert worker.jobs[0].snapshot.candle_id == first.snapshot.candle_id
        assert engine.state["portfolio"]["side"] == "FLAT"
    finally:
        store.close()


class Recorder:
    def __init__(self, latency=0):
        self.contexts = []
        self.latency = latency

    def identity(self):
        return {"provider": "cadence-test"}

    def advise(self, context):
        from quantbot.models import Decision

        self.contexts.append(context)
        return Advice(Decision.parse(decision("HOLD")), self.latency)


@pytest.mark.parametrize("seconds,expected", [(60, 120), (300, 24)])
@pytest.mark.parametrize("latency", [0, 0.5])
def test_replay_decision_counts_and_causality(tmp_path, seconds, expected, latency):
    hourly, minute = demo_datasets(warmup=606, hours=2)
    advisor = Recorder(latency)
    result = run_replay(
        hourly,
        advisor,
        execution_data=minute,
        config=Config(data_dir=tmp_path, decision_interval_seconds=seconds),
        start_index=606,
    )
    assert len(advisor.contexts) == expected
    assert result["metrics"]["advice_count"] == expected
    assert result["assumptions"]["decision_interval_seconds"] == seconds
    assert len({item.snapshot.candle_id for item in advisor.contexts}) < expected
    assert len({item.snapshot.current_price for item in advisor.contexts}) > 2
    for before, after in zip(advisor.contexts, advisor.contexts[1:]):
        assert after.requested_at - before.requested_at == seconds
    for item in advisor.contexts:
        assert item.snapshot.candle_id + hourly.interval_ms <= item.requested_at * 1000
        assert all(bar.timestamp <= item.snapshot.candle_id for bar in item.history)
    # A nonzero model delay must still allow decisions at the following 1m open.
    accepted = [row for row in result["decisions"] if row["outcome"] == "HOLD"]
    # Each hourly metric refresh cancels one in-flight old-candle result;
    # a 1m run also ends with its last result still pending.
    expected_cancellations = (2 + (seconds == 60)) if latency else 0
    assert len(accepted) == expected - expected_cancellations
    assert all(row["reason_code"] != "DUPLICATE_CANDLE" for row in result["decisions"])


def test_coarse_execution_cannot_pretend_to_evaluate_every_minute():
    hourly, _ = demo_datasets(hours=1)
    with pytest.raises(ValueError, match="fine as the decision interval"):
        run_replay(hourly, Recorder(), config=Config(decision_interval_seconds=60))


@pytest.mark.parametrize("seconds", [60, 300])
def test_failed_fast_decision_does_not_disable_stop(tmp_path, seconds):
    config = Config(data_dir=tmp_path, decision_interval_seconds=seconds)
    store = Store(config)
    try:
        engine = PaperEngine(config, store)
        seed(engine)
        worker = Worker()
        from quantbot.ai import Job

        worker.results.put(
            Result(Job("DECISION", snapshot(), {}, NOW, 0), error="ModelDecisionError: Empty model response")
        )
        market = SimpleNamespace(fetch_quote=lambda: quote(80), fetch_snapshot=lambda _: snapshot(80))
        Runner(config, engine, market, worker, clock=lambda: NOW).step()
        assert engine.state["portfolio"]["side"] == "FLAT"
        assert engine.state["trades"][0]["type"] == "STOP_LOSS"
        assert store.decision_history()[-1]["reason_code"] == "INVALID_MODEL_OUTPUT"
    finally:
        store.close()


def test_short_cadence_rejects_late_decision_without_opening_position(tmp_path):
    config = Config(data_dir=tmp_path, decision_interval_seconds=60)
    store = Store(config)
    try:
        engine = PaperEngine(config, store)
        outcome = engine.apply_decision(
            decision("BUY"),
            snapshot(),
            quote(now=NOW + 61),
            now=NOW + 61,
            requested_at=NOW,
            revision=0,
            latest_candle=snapshot().candle_id,
        )
        assert outcome == "STALE_DECISION"
        assert engine.state["portfolio"]["side"] == "FLAT"
    finally:
        store.close()


def test_five_minute_ai_uses_minute_execution_and_five_minute_decisions(tmp_path):
    control = Control(tmp_path)
    commands = []
    control.ollama = lambda: ({}, [{"name": "test-local"}])

    def command(*args):
        commands.append(list(map(str, args)))
        if args[0] == "compare":
            output = args[args.index("--output") + 1]
            output.mkdir(parents=True)
            (output / "comparison.json").write_text(json.dumps({"candidates": {}}))

    control.command = command
    control.research_job(
        {
            "action": "backtest",
            "start": "2025-01-01",
            "end": "2025-01-02",
            "interval": "5m",
            "model": "test-local",
        },
        "test",
    )
    assert "1m" in commands[1]
    assert commands[2][commands[2].index("--decision-interval") + 1] == "5m"
    assert "--fixed-latency-seconds" not in commands[2]
    assert not (tmp_path / "quantbot.sqlite3").exists()


def test_cloud_omits_unsupported_format_but_still_validates(config):
    calls = []
    raw = "```json\n" + json.dumps(decision("HOLD")) + "\n```"

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"response": raw})

    client = ModelClient(replace(config, model="gemma4:cloud"), SimpleNamespace(post=post))
    assert client.consult(snapshot(), {}).action == "HOLD"
    assert "format" not in calls[0]
    assert "output_schema" in json.loads(calls[0]["prompt"])
    raw = '{"action":"BUY"}'
    with pytest.raises(ModelDecisionError, match="missing="):
        client.consult(snapshot(), {})
    assert len(calls) == 3  # One valid call, then invalid + bounded repair.


def test_empty_model_output_names_failure_and_preserves_generation_evidence(config):
    session = SimpleNamespace(
        post=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"response": "", "thinking": "internal reasoning", "done": True},
        )
    )
    with pytest.raises(ModelDecisionError, match="thinking was returned without a final answer") as failure:
        ModelClient(config, session).consult(snapshot(), {})
    assert len(failure.value.diagnostics["attempts"]) == 2
    assert failure.value.diagnostics["attempts"][0]["generation"]["has_thinking"] is True
    assert "internal reasoning" not in json.dumps(failure.value.diagnostics)
