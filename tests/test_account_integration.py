"""Account-level protection, audit, and history regressions."""

import queue
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import CANDLE, NOW, decision, quote, seed, snapshot

from quantbot.ai import Job, Result
from quantbot.app import Runner
from quantbot.execution import PaperEngine
from quantbot.storage import Store


def apply_at(engine, value, *, at, candle=CANDLE, requested_at=None, price=100, side="LONG"):
    data = replace(snapshot(price, side=side), candle_id=candle, observed_at=at)
    return engine.apply_decision(
        value,
        data,
        quote(price, at),
        now=at,
        requested_at=at if requested_at is None else requested_at,
        revision=engine.snapshot()["position_revision"],
        latest_candle=candle,
    )


def test_daily_loss_latch_blocks_entries_and_resets_on_utc_day(engine):
    engine.config = replace(engine.config, daily_loss_limit=0.0005)
    seed(engine, stop_loss=80.0)
    engine.on_quote(quote(100, NOW), NOW)

    # Mark-to-market loss exceeds five basis points without touching the position stop.
    engine.on_quote(quote(90, NOW + 60), NOW + 60)
    assert engine.snapshot()["protections"]["daily_loss_triggered_at"] == NOW + 60

    assert apply_at(engine, decision("CLOSE"), at=NOW + 61) == "CLOSED"
    assert apply_at(engine, decision(), at=NOW + 62, candle=CANDLE + 1) == "DAILY_LOSS_LIMIT"

    next_day = (
        datetime.fromtimestamp(NOW, timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
        + 86400
    )
    assert apply_at(engine, decision(), at=next_day, candle=CANDLE + 2) == "OPENED"
    protections = engine.snapshot()["protections"]
    assert protections["daily_loss_triggered_at"] is None
    assert protections["session_date"] == datetime.fromtimestamp(next_day, timezone.utc).date().isoformat()


def test_drawdown_latch_survives_restart_and_recovery_but_allows_close(config):
    config = replace(config, max_account_drawdown=0.0005)
    store = Store(config)
    engine = PaperEngine(config, store)
    try:
        seed(engine, stop_loss=80.0)
        engine.on_quote(quote(100, NOW), NOW)
        engine.on_quote(quote(90, NOW + 60), NOW + 60)
        assert engine.snapshot()["protections"]["max_drawdown_triggered_at"] == NOW + 60
        store.close()

        store = Store(config)
        engine = PaperEngine(config, store)
        engine.on_quote(quote(100, NOW + 120), NOW + 120)
        assert apply_at(engine, decision("CLOSE"), at=NOW + 121) == "CLOSED"
        assert apply_at(engine, decision(), at=NOW + 122, candle=CANDLE + 1) == "MAX_DRAWDOWN"
        assert engine.snapshot()["protections"]["max_drawdown_triggered_at"] is not None
    finally:
        store.close()


def test_equity_history_samples_marks_and_skips_quotes_inside_interval(engine):
    engine.config = replace(engine.config, equity_sample_seconds=60)
    seed(engine, stop_loss=80.0)

    engine.on_quote(quote(100, NOW), NOW)
    engine.on_quote(quote(101, NOW + 30), NOW + 30)
    engine.on_quote(quote(102, NOW + 61), NOW + 61)

    rows = engine.store.equity_history()
    assert len(rows) == 2
    assert [row["observed_at"] for row in rows] == [NOW, NOW + 61]
    assert rows[-1]["unrealized_pnl"] == pytest.approx(2.0)
    assert rows[-1]["equity"] == pytest.approx(rows[-1]["cash"] + 2.0)

    before = len(rows)
    with pytest.raises(ValueError):
        engine.on_quote(quote(102, NOW + 1), NOW + 62)
    assert len(engine.store.equity_history()) == before

    # A close is a risk-relevant account event and must be sampled inside the interval.
    assert apply_at(engine, decision("CLOSE"), at=NOW + 62, candle=CANDLE + 1) == "CLOSED"
    rows = engine.store.equity_history()
    assert len(rows) == before + 1
    assert rows[-1]["observed_at"] == NOW + 62
    assert rows[-1]["side"] == "FLAT"
    assert rows[-1]["exposure"] == 0


@pytest.mark.parametrize(
    "action,side,regime",
    [("BUY", "LONG", "TRENDING_BULL"), ("SELL", "SHORT", "TRENDING_BEAR")],
)
def test_accepted_long_and_short_fills_are_audited_and_force_equity_samples(engine, action, side, regime):
    engine.on_quote(quote(100, NOW), NOW)
    value = decision(action, regime=regime)
    assert apply_at(engine, value, at=NOW + 1, candle=CANDLE + 1, side=side) == "OPENED"

    assert engine.snapshot()["portfolio"]["side"] == side
    audit = engine.store.decision_history()[-1]
    assert (audit["outcome"], audit["reason_code"], audit["status"]) == ("OPENED", "ENTRY", "ACCEPTED")
    sample = engine.store.equity_history()[-1]
    assert sample["observed_at"] == NOW + 1
    assert sample["side"] == side
    assert sample["exposure"] > 0


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_cooldown_blocks_scale_and_entry_but_not_model_or_stop_exits(engine, side):
    engine.config = replace(engine.config, cooldown_seconds=300)
    seed(engine, side, stop_loss=90.0 if side == "LONG" else 110.0)
    state = engine.snapshot()
    state["protections"]["initialized"] = True
    state["protections"].update(
        current_equity=10000.0,
        peak_equity=10000.0,
        daily_start_equity=10000.0,
        session_date=datetime.fromtimestamp(NOW, timezone.utc).date().isoformat(),
        last_update_at=NOW,
        cooldown_until=NOW + 300,
    )
    engine._commit(state, [])

    profitable = 105 if side == "LONG" else 95
    add_signal = decision("HOLD", scale=True, regime="TRENDING_BULL" if side == "LONG" else "TRENDING_BEAR")
    before_units = engine.snapshot()["portfolio"]["position_units"]
    assert apply_at(engine, add_signal, at=NOW + 1, candle=CANDLE, price=profitable, side=side) == "COOLDOWN"
    assert engine.snapshot()["portfolio"]["position_units"] == before_units

    # A risk-reducing model close remains available during the entry cooldown.
    assert (
        apply_at(engine, decision("CLOSE"), at=NOW + 2, candle=CANDLE + 1, price=100, side=side) == "CLOSED"
    )
    assert apply_at(engine, decision(), at=NOW + 3, candle=CANDLE + 2) == "COOLDOWN"

    # A fresh account-level cooldown also cannot defer a stop exit.
    seed(engine, side, stop_loss=90.0 if side == "LONG" else 110.0)
    engine.on_quote(quote(100, NOW + 4), NOW + 4)
    stop_price = 90 if side == "LONG" else 110
    assert engine.on_quote(quote(stop_price, NOW + 5), NOW + 5) is not None
    assert engine.snapshot()["portfolio"]["side"] == "FLAT"


def test_decision_audit_failure_rolls_back_fill_and_forced_sample(engine):
    engine.on_quote(quote(100, NOW), NOW)
    before = engine.snapshot()
    before_equity = engine.store.equity_history()
    before_audit = engine.store.decision_history()
    engine.store.db.execute(
        "CREATE TRIGGER broken_audit BEFORE INSERT ON decision_audit "
        "BEGIN SELECT RAISE(ABORT, 'audit write failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="audit write failure"):
        apply_at(engine, decision(), at=NOW + 1, candle=CANDLE + 1)

    assert engine.snapshot()["portfolio"] == before["portfolio"]
    assert engine.snapshot()["last_decision_candle"] == before["last_decision_candle"]
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='ENTRY'").fetchone()[0] == 0
    assert engine.store.equity_history() == before_equity
    assert engine.store.decision_history() == before_audit
    assert engine.snapshot()["system"]["storage_error"]

    engine.store.db.execute("DROP TRIGGER broken_audit")
    engine.on_quote(quote(100, NOW + 2), NOW + 2)
    assert engine.snapshot()["system"]["storage_error"] is None
    assert apply_at(engine, decision(), at=NOW + 3, candle=CANDLE + 1) == "OPENED"
    assert engine.store.decision_history()[-1]["outcome"] == "OPENED"
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='ENTRY'").fetchone()[0] == 1


class _Worker:
    def __init__(self, result):
        self.results = queue.Queue()
        self.results.put(result)
        self.jobs = []

    def ready(self):
        return not self.jobs

    def submit(self, job):
        self.jobs.append(job)
        return True


def _runner(config, engine, worker, *, data=None):
    market = SimpleNamespace(
        fetch_quote=lambda: quote(100, NOW),
        fetch_snapshot=lambda _quote: data or snapshot(100),
    )
    return Runner(config, engine, market, worker, clock=lambda: NOW)


def test_runner_audits_worker_failures_and_invalidated_decisions(config, engine):
    timeout_job = Job("DECISION", snapshot(), {}, NOW, 0)
    _runner(config, engine, _Worker(Result(timeout_job, error="Timeout"))).step()
    assert engine.store.decision_history()[-1]["reason_code"] == "MODEL_ERROR"
    assert engine.store.decision_history()[-1]["status"] == "REJECTED"

    invalid_config = replace(config, data_dir=config.data_dir / "invalid")
    invalid_engine_store = Store(invalid_config)
    try:
        invalid_engine = PaperEngine(invalid_engine_store.config, invalid_engine_store)
        stale_job = Job("DECISION", snapshot(), {}, NOW, 0)
        _runner(
            invalid_engine.config,
            invalid_engine,
            _Worker(Result(stale_job, value=decision())),
            data=snapshot(atr=0),
        ).step()
        rows = invalid_engine.store.decision_history()
        assert rows[-1]["reason_code"] == "INVALID_MARKET_DATA"
        assert rows[-1]["status"] == "REJECTED"
        assert invalid_engine.snapshot()["last_decision_candle"] is None
    finally:
        invalid_engine_store.close()


def test_runner_audits_pending_decisions_when_quote_acquisition_fails(config, engine):
    job = Job("DECISION", snapshot(), {}, NOW, 0)
    worker = _Worker(Result(job, value=decision()))
    market = SimpleNamespace(
        fetch_quote=lambda: (_ for _ in ()).throw(OSError("quote endpoint unavailable")),
        fetch_snapshot=lambda _quote: snapshot(),
    )

    Runner(config, engine, market, worker, clock=lambda: NOW).step()

    audit = engine.store.decision_history()[-1]
    assert audit["reason_code"] == "INVALID_MARKET_DATA"
    assert audit["status"] == "REJECTED"
    assert audit["quote_at"] is None
    assert audit["execution_price"] is None
    assert engine.snapshot()["last_decision_candle"] is None


def test_clock_rollback_does_not_skip_a_preexisting_stop(engine):
    seed(engine)
    engine.on_quote(quote(100, NOW), NOW)

    assert engine.on_quote(quote(90, NOW - 1), NOW - 1) is not None
    assert engine.snapshot()["portfolio"]["side"] == "FLAT"
    assert engine.snapshot()["protections"]["last_update_at"] == NOW

    assert apply_at(engine, decision(), at=NOW - 1, candle=CANDLE + 1) == "PROTECTION_STATE_INVALID"


def test_rejected_decisions_are_audited_without_consuming_the_candle(engine):
    before = engine.snapshot()
    assert (
        apply_at(engine, decision(), at=NOW, requested_at=NOW - engine.config.max_decision_age - 1)
        == "STALE_DECISION"
    )

    after = engine.snapshot()
    assert after["last_decision_candle"] == before["last_decision_candle"]
    assert after["portfolio"] == before["portfolio"]
    rows = engine.store.decision_history()
    assert rows[-1]["outcome"] == "STALE_DECISION"
    assert rows[-1]["reason_code"] == "AI_DECISION_EXPIRED"
    assert rows[-1]["status"] == "REJECTED"


def test_invalid_model_output_is_audited_but_raises_without_account_mutation(engine):
    before = engine.snapshot()
    malformed = decision()
    malformed["confidence"] = float("nan")

    with pytest.raises(ValueError):
        apply_at(engine, malformed, at=NOW)

    after = engine.snapshot()
    assert after["last_decision_candle"] == before["last_decision_candle"]
    assert after["portfolio"] == before["portfolio"]
    rows = engine.store.decision_history()
    assert rows[-1]["outcome"] == "INVALID_MODEL_OUTPUT"
    assert rows[-1]["reason_code"] == "INVALID_MODEL_OUTPUT"
    assert rows[-1]["status"] == "REJECTED"


def test_equity_history_and_close_fill_roll_back_together_on_storage_failure(engine):
    seed(engine, stop_loss=90.0)
    engine.on_quote(quote(100, NOW), NOW)
    before_portfolio = engine.snapshot()["portfolio"]
    before_history = engine.store.equity_history()
    engine.store.db.execute(
        "CREATE TRIGGER broken_equity BEFORE INSERT ON equity_history "
        "BEGIN SELECT RAISE(ABORT, 'equity history failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        engine.on_quote(quote(90, NOW + 60), NOW + 60)

    assert engine.snapshot()["portfolio"] == before_portfolio
    assert engine.store.load()["portfolio"] == before_portfolio
    assert engine.store.equity_history() == before_history
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='CLOSE'").fetchone()[0] == 0
    assert engine.snapshot()["system"]["storage_error"]

    engine.store.db.execute("DROP TRIGGER broken_equity")
    engine.on_quote(quote(90, NOW + 60), NOW + 60)
    assert engine.snapshot()["portfolio"]["side"] == "FLAT"
    assert len(engine.store.equity_history()) == len(before_history) + 1
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='CLOSE'").fetchone()[0] == 1
