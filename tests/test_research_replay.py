"""Causal replay, execution-boundary, and offline-error regressions."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from quantbot.config import Config
from quantbot.execution import PaperEngine
from quantbot.models import Decision, Quote
from quantbot.research.advisors import Advice
from quantbot.research.cache import CacheCorruptionError, CachedAdvisor, CacheMissError
from quantbot.research.data import Bar, dataset_from_rows
from quantbot.research.memory import MemoryStore
from quantbot.research.replay import ReplayClock, _resolve_bar, _tick, run_replay
from quantbot.research.synthetic import demo_datasets


class _FixedAdvisor:
    def __init__(self, *, latency=0.0, error="", action="BUY", record_contexts=False):
        self.latency = latency
        self.error = error
        self.action = action
        self.record_contexts = record_contexts
        self.contexts = []

    def identity(self):
        return {"provider": "test-fixed", "action": self.action, "latency": self.latency, "error": self.error}

    def advise(self, context):
        if self.record_contexts:
            self.contexts.append(context)
        decision = (
            None
            if self.error
            else Decision(
                self.action,
                1.0 if self.action == "BUY" else 0.0,
                False,
                "TRENDING_BULL" if self.action == "BUY" else "CHOPPY_RANGE",
                "test fixture",
                "",
            )
        )
        return Advice(decision, self.latency, error=self.error)


def _rebuild(dataset, rows):
    manifest = dataset.manifest()
    return dataset_from_rows(
        rows,
        interval_ms=dataset.interval_ms,
        symbol=dataset.symbol,
        market_type=dataset.market_type,
        source=manifest["source"],
    )


def _make_data(*, hours=4):
    return demo_datasets(warmup=606, hours=hours)


def _flat_first_hour(hourly, minute, hour_index=606):
    """Keep the first opened position safely alive through its request hour."""
    minutes = list(minute.rows)
    child_start = (hour_index - 606) * 60
    price = hourly.rows[hour_index].open
    for offset in range(60):
        old = minutes[child_start + offset]
        minutes[child_start + offset] = Bar(
            old.timestamp, price, price + 0.01, price - 0.01, price, old.volume
        )
    parent_rows = list(hourly.rows)
    children = minutes[child_start : child_start + 60]
    parent = parent_rows[hour_index]
    parent_rows[hour_index] = Bar(
        parent.timestamp,
        children[0].open,
        max(item.high for item in children),
        min(item.low for item in children),
        children[-1].close,
        sum(item.volume for item in children),
    )
    return _rebuild(hourly, parent_rows), _rebuild(minute, minutes)


def _force_gap_on_second_minute(hourly, minute, hour_index=607, gap_price=80.0):
    minutes = list(minute.rows)
    child_start = (hour_index - 606) * 60
    old = minutes[child_start + 1]
    minutes[child_start + 1] = Bar(
        old.timestamp, gap_price, gap_price + 0.1, gap_price - 0.1, gap_price, old.volume
    )
    for offset in range(2, 60):
        prior = minutes[child_start + offset]
        minutes[child_start + offset] = Bar(
            prior.timestamp, gap_price, gap_price + 0.1, gap_price - 0.1, gap_price, prior.volume
        )
    parent_rows = list(hourly.rows)
    children = minutes[child_start : child_start + 60]
    parent = parent_rows[hour_index]
    parent_rows[hour_index] = Bar(
        parent.timestamp,
        children[0].open,
        max(item.high for item in children),
        min(item.low for item in children),
        children[-1].close,
        sum(item.volume for item in children),
    )
    return _rebuild(hourly, parent_rows), _rebuild(minute, minutes)


def _seeded_engine(config, timestamp):
    store = MemoryStore(config)
    state = store.load()
    state["portfolio"].update(
        side="LONG",
        position_units=1.0,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        tranches=1,
    )
    store.save(state)
    clock = ReplayClock(timestamp)
    engine = PaperEngine(config, store, clock=clock)
    _tick(engine, clock, 100.0, timestamp)
    return engine, store, clock


def test_ambiguous_bracket_paths_use_real_engine_and_close_once_at_selected_level(tmp_path):
    hourly, _ = _make_data(hours=2)
    at = hourly.rows[606].timestamp / 1000
    bar = Bar(hourly.rows[606].timestamp, 100, 125, 85, 100, 1)
    outcomes = {}
    for policy in ("conservative", "optimistic"):
        config = Config(data_dir=tmp_path / policy)
        engine, store, clock = _seeded_engine(config, at)
        ambiguous, sensitive = _resolve_bar(engine, clock, bar, 60_000, policy)
        outcomes[policy] = (engine, store, ambiguous, sensitive)

    conservative, conservative_store, ambiguous, sensitive = outcomes["conservative"]
    optimistic, optimistic_store, _, _ = outcomes["optimistic"]
    assert ambiguous is True
    assert sensitive is True
    assert conservative.snapshot()["portfolio"]["cash_usdt"] == pytest.approx(9_990)
    assert optimistic.snapshot()["portfolio"]["cash_usdt"] == pytest.approx(10_020)
    assert [event[1]["exit"] for event in conservative_store.events if event[0] == "CLOSE"] == [90]
    assert [event[1]["exit"] for event in optimistic_store.events if event[0] == "CLOSE"] == [120]
    for store in (conservative_store, optimistic_store):
        assert sum(kind == "CLOSE" for kind, _ in store.events) == 1


def test_gap_open_stop_fills_at_observed_open_and_trade_time_uses_replay_clock(tmp_path):
    hourly, _ = _make_data(hours=2)
    at = hourly.rows[606].timestamp / 1000
    config = Config(data_dir=tmp_path)
    engine, store, clock = _seeded_engine(config, at)

    closed = engine.on_quote(Quote(80, at + 60), at + 60)
    assert closed["exit"] == 80
    assert closed["type"] == "STOP_LOSS"
    assert closed["time"] == datetime.fromtimestamp(clock.value, timezone.utc).isoformat(timespec="seconds")
    assert not (tmp_path / "quantbot.sqlite3").exists()
    assert not (tmp_path / ".quantbot.lock").exists()
    assert len(store.trades()) == 1


@pytest.mark.parametrize(
    "policy,expected_side,expected_cash",
    [("conservative", "LONG", 10_000), ("optimistic", "LONG", 10_000)],
)
def test_halfway_then_retrace_does_not_move_stop_in_either_path(
    tmp_path, policy, expected_side, expected_cash
):
    hourly, _ = _make_data(hours=2)
    at = hourly.rows[606].timestamp / 1000
    config = Config(data_dir=tmp_path / policy)
    engine, store, clock = _seeded_engine(config, at)
    bar = Bar(hourly.rows[606].timestamp, 100, 115, 95, 105, 1)

    ambiguous, sensitive = _resolve_bar(engine, clock, bar, 60_000, policy)
    assert ambiguous is False
    assert sensitive is False
    assert engine.snapshot()["portfolio"]["side"] == expected_side
    assert engine.snapshot()["portfolio"]["cash_usdt"] == pytest.approx(expected_cash)
    assert engine.snapshot()["portfolio"]["stop_loss"] == 90
    assert not any(kind in ("CLOSE", "STOP_UPDATE") for kind, _ in store.events)


def test_replay_advisors_receive_only_closed_causal_history_and_future_edits_are_prefix_invariant(tmp_path):
    hourly, minute = _make_data(hours=4)
    config = Config(data_dir=tmp_path / "first")
    first_advisor = _FixedAdvisor(action="HOLD", record_contexts=True)
    first = run_replay(
        hourly,
        first_advisor,
        config=config,
        execution_data=minute,
        start_index=606,
        end_index=608,
        fixed_latency_seconds=0,
    )
    for context in first_advisor.contexts:
        assert context.snapshot.candle_id + hourly.interval_ms <= context.requested_at * 1000
        assert all(bar.timestamp <= context.snapshot.candle_id for bar in context.history)

    changed_rows = list(hourly.rows)
    future = changed_rows[608]
    changed_rows[608] = Bar(
        future.timestamp,
        future.open,
        future.high + 10_000,
        future.low,
        future.close,
        future.volume,
    )
    changed_hourly = _rebuild(hourly, changed_rows)
    second_advisor = _FixedAdvisor(action="HOLD", record_contexts=True)
    second = run_replay(
        changed_hourly,
        second_advisor,
        config=replace(config, data_dir=tmp_path / "second"),
        execution_data=minute,
        start_index=606,
        end_index=608,
        fixed_latency_seconds=0,
    )
    assert changed_hourly.sha256 != hourly.sha256
    assert [context.as_dict() for context in first_advisor.contexts] == [
        context.as_dict() for context in second_advisor.contexts
    ]
    for key in ("metrics", "equity", "trades", "decisions", "events"):
        assert first[key] == second[key]


def test_delayed_advice_fills_only_at_the_next_minute_open(tmp_path):
    hourly, minute = _make_data(hours=2)
    config = Config(data_dir=tmp_path)
    advisor = _FixedAdvisor(latency=60)
    result = run_replay(
        hourly,
        advisor,
        config=config,
        execution_data=minute,
        start_index=606,
        end_index=607,
        publication_delay_seconds=0,
    )
    entry = next(payload for kind, payload in result["events"] if kind == "ENTRY")
    expected = minute.rows[1]
    assert entry["time"] == datetime.fromtimestamp(expected.timestamp / 1000, timezone.utc).isoformat(
        timespec="seconds"
    )
    assert entry["price"] == expected.open
    assert entry["price"] != hourly.rows[606].close
    assert result["advice"][0]["ready_at"] == expected.timestamp / 1000


def test_default_publication_delay_waits_for_next_observable_open(tmp_path):
    hourly, minute = _make_data(hours=2)
    result = run_replay(
        hourly,
        _FixedAdvisor(latency=60),
        config=Config(data_dir=tmp_path),
        execution_data=minute,
        start_index=606,
        end_index=607,
    )
    entry = next(payload for kind, payload in result["events"] if kind == "ENTRY")
    # With the default 2-second candle publication delay, the 04:00 request
    # cannot use a fresh published candle and is rejected as stale. The next
    # request is at 04:01 and its 60-second result fills at the 04:02 open.
    first_request_open = minute.rows[1]
    completion_open = minute.rows[2]
    advice = result["advice"][-1]
    assert advice["requested_at"] == first_request_open.timestamp / 1000
    assert entry["time"] == datetime.fromtimestamp(completion_open.timestamp / 1000, timezone.utc).isoformat(
        timespec="seconds"
    )
    assert advice["ready_at"] == completion_open.timestamp / 1000


def test_decision_completing_after_staleness_window_is_rejected_without_entry(tmp_path):
    hourly, minute = _make_data(hours=2)
    result = run_replay(
        hourly,
        _FixedAdvisor(),
        config=Config(data_dir=tmp_path),
        execution_data=minute,
        start_index=606,
        end_index=607,
        fixed_latency_seconds=180,
    )
    assert not any(kind == "ENTRY" for kind, _ in result["events"])
    assert any(row["reason_code"] == "AI_DECISION_EXPIRED" for row in result["decisions"])


def test_stop_close_invalidates_pending_decision_revision_before_it_can_reopen(tmp_path):
    hourly, minute = _make_data(hours=2)
    hourly, minute = _flat_first_hour(hourly, minute)
    hourly, minute = _force_gap_on_second_minute(hourly, minute)

    class FirstImmediateThenDelayed(_FixedAdvisor):
        def __init__(self):
            super().__init__(latency=0)
            self.calls = 0

        def advise(self, context):
            self.calls += 1
            self.contexts.append(context)
            latency = 0 if self.calls == 1 else 60
            return Advice(
                Decision("BUY", 1.0, False, "TRENDING_BULL", "test", ""),
                latency,
            )

    advisor = FirstImmediateThenDelayed()
    result = run_replay(
        hourly,
        advisor,
        config=Config(data_dir=tmp_path),
        execution_data=minute,
        start_index=606,
        end_index=608,
        publication_delay_seconds=0,
    )
    assert sum(kind == "ENTRY" for kind, _ in result["events"]) == 1
    closes = [payload for kind, payload in result["events"] if kind == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["exit"] == 80
    assert any(row["reason_code"] == "POSITION_CHANGED" for row in result["decisions"])
    assert result["final_state"]["portfolio"]["side"] == "FLAT"


def test_final_pending_known_advisor_error_is_counted_even_before_ready_time(tmp_path):
    hourly, minute = _make_data(hours=1)
    result = run_replay(
        hourly,
        _FixedAdvisor(latency=0, error="RuntimeError: cache provider failed"),
        config=Config(data_dir=tmp_path),
        execution_data=minute,
        start_index=606,
        end_index=607,
        fixed_latency_seconds=3_600,
    )
    assert result["metrics"]["model_errors"] == 1
    assert result["metrics"]["processed_decision_failures"] == 0
    assert result["advice"][0]["error"] == "RuntimeError: cache provider failed"
    assert result["decisions"][-1]["reason_code"] == "SIMULATION_ENDED"


def test_replay_only_cache_miss_and_corruption_propagate_instead_of_being_swallowed(tmp_path):
    hourly, minute = _make_data(hours=1)
    config = Config(data_dir=tmp_path / "account")
    cache_dir = tmp_path / "cache"
    advisor = _FixedAdvisor()
    replay_only = CachedAdvisor(
        advisor,
        cache_dir,
        dataset_hash=hourly.sha256,
        cache_mode="replay-only",
    )
    kwargs = dict(
        config=config,
        execution_data=minute,
        start_index=606,
        end_index=607,
        fixed_latency_seconds=0,
    )
    with pytest.raises(CacheMissError):
        run_replay(hourly, replay_only, **kwargs)

    writer = CachedAdvisor(advisor, cache_dir, dataset_hash=hourly.sha256, cache_mode="readwrite")
    run_replay(hourly, writer, **kwargs)
    cached_path = next(cache_dir.glob("*.json"))
    record = json.loads(cached_path.read_text(encoding="utf-8"))
    record["content_sha256"] = "corrupted"
    cached_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(CacheCorruptionError):
        run_replay(hourly, replay_only, **kwargs)


def test_fixed_zero_latency_replay_is_byte_stable_and_never_opens_sqlite(tmp_path):
    hourly, minute = _make_data(hours=2)
    config = Config(data_dir=tmp_path)

    def replay():
        return run_replay(
            hourly,
            _FixedAdvisor(action="HOLD"),
            config=config,
            execution_data=minute,
            start_index=606,
            end_index=608,
            fixed_latency_seconds=0,
        )

    first, second = replay(), replay()
    for key in ("metrics", "equity", "trades", "decisions", "advice", "events", "final_state"):
        assert first[key] == second[key]
        json.dumps(first[key], allow_nan=False)
    assert not (tmp_path / "quantbot.sqlite3").exists()
    assert not (tmp_path / ".quantbot.lock").exists()
    assert list(tmp_path.iterdir()) == []
