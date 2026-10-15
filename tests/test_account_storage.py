import json
import sqlite3

import pytest

from quantbot.state import initial_state
from quantbot.storage import SCHEMA_VERSION, Store


def _equity_event(**overrides):
    event = {
        "observed_at": 1_700_000_001.0,
        "quote_at": 1_700_000_000.5,
        "price": 101.25,
        "cash": 1_000.0,
        "unrealized_pnl": 1.25,
        "equity": 1_001.25,
        "peak_equity": 1_001.25,
        "drawdown": 0.0,
        "exposure": 0.1,
        "side": "LONG",
    }
    event.update(overrides)
    return event


def _audit_event(**overrides):
    event = {
        "observed_at": 1_700_000_001.0,
        "requested_at": 1_700_000_000.0,
        "candle_id": 1_699_995_600_000,
        "action": "BUY",
        "status": "ACCEPTED",
        "reason_code": "OK",
        "outcome": "OPENED",
        "model": "local-model",
        "snapshot": {"price": 101.25},
        "decision": {"action": "BUY", "confidence": 0.9},
        "quote_at": 1_700_000_000.5,
        "execution_price": 101.25,
        "position_revision": 0,
        "latest_candle": 1_699_995_600_000,
    }
    event.update(overrides)
    return event


def test_legacy_open_position_migrates_without_synthetic_history(config):
    store = Store(config)
    state = initial_state(config)
    state["portfolio"].update(
        side="LONG", position_units=2.0, entry_price=100.0, stop_loss=90.0, take_profit=120.0, tranches=1
    )
    state.pop("protections")
    store.save(state, [("DECISION", {"legacy": True})])
    legacy_payload = store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()[0]
    store.db.execute("UPDATE snapshot SET payload=?", (legacy_payload,))
    store.db.execute("PRAGMA user_version=0")
    store.db.commit()
    store.close()

    migrated_store = Store(config)
    try:
        migrated = migrated_store.load()
        assert migrated["portfolio"]["side"] == "LONG"
        assert migrated["portfolio"]["position_units"] == 2.0
        assert migrated["protections"]["initialized"] is False
        assert migrated_store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated_store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert migrated_store.equity_history() == []
        persisted = json.loads(
            migrated_store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()[0]
        )
        assert persisted["protections"] == migrated["protections"]
    finally:
        migrated_store.close()


def test_equity_and_decision_histories_survive_restart_and_are_chronological(config):
    store = Store(config)
    state = initial_state(config)
    store.save(
        state,
        [
            ("EQUITY", _equity_event(observed_at=1_700_000_002.0)),
            ("DECISION_AUDIT", _audit_event(observed_at=1_700_000_002.0)),
            ("EQUITY", _equity_event(observed_at=1_700_000_003.0, price=102.0)),
            ("DECISION_AUDIT", _audit_event(observed_at=1_700_000_003.0, status="REJECTED", outcome="HOLD")),
        ],
    )
    store.close()

    reopened = Store(config)
    try:
        all_equity = reopened.equity_history()
        equity = reopened.equity_history(limit=1)
        audit = reopened.decision_history(limit=2)
        assert [row["observed_at"] for row in equity] == [1_700_000_003.0]
        assert equity[0]["price"] == 102.0
        assert [row["status"] for row in audit] == ["ACCEPTED", "REJECTED"]
        assert all(row["event_id"] for row in equity + audit)
        assert {row["event_id"] for row in all_equity} == {
            row[0] for row in reopened.db.execute("SELECT id FROM events WHERE kind='EQUITY'")
        }
        assert reopened.db.execute("SELECT count(*) FROM events WHERE kind='EQUITY'").fetchone()[0] == 2
        assert {row["event_id"] for row in audit} == {
            row[0] for row in reopened.db.execute("SELECT id FROM events WHERE kind='DECISION_AUDIT'")
        }
        reopened.save(reopened.load())
        assert reopened.db.execute("SELECT count(*) FROM equity_history").fetchone()[0] == 2
    finally:
        reopened.close()


def test_equity_projection_failure_rolls_back_snapshot_and_event(config):
    store = Store(config)
    state = initial_state(config)
    store.save(state)
    before = store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()[0]
    store.db.execute(
        "CREATE TRIGGER fail_equity BEFORE INSERT ON equity_history "
        "BEGIN SELECT RAISE(ABORT, 'projection failure'); END"
    )
    changed = initial_state(config)
    changed["portfolio"]["cash_usdt"] = 900.0
    changed["portfolio"]["total_equity"] = 900.0
    with pytest.raises(sqlite3.IntegrityError, match="projection failure"):
        store.save(changed, [("EQUITY", _equity_event())])
    assert store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()[0] == before
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='EQUITY'").fetchone()[0] == 0
    assert store.equity_history() == []
    store.close()


@pytest.mark.parametrize(
    "event",
    [
        _equity_event(equity=float("nan")),
        _equity_event(side="UNKNOWN"),
        _equity_event(price=0),
        _equity_event(observed_at=-1),
        _equity_event(quote_at=1_700_000_002.0),
        _equity_event(drawdown=-0.1),
        _equity_event(exposure=-0.1),
        _equity_event(peak_equity=999.0),
        _equity_event(cash=999.0),
        _equity_event(notional=float("inf")),
    ],
)
def test_invalid_equity_event_rolls_back_snapshot(config, event):
    store = Store(config)
    before = store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
    with pytest.raises((ValueError, TypeError)):
        store.save(initial_state(config), [("EQUITY", event)])
    assert store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone() == before
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    store.close()


def test_history_limit_rejects_bool_and_unbounded_values(config):
    store = Store(config)
    try:
        with pytest.raises(ValueError):
            store.equity_history(True)
        with pytest.raises(ValueError):
            store.decision_history(10_001)
    finally:
        store.close()


def test_invalid_decision_audit_event_rolls_back_snapshot_and_event(config):
    store = Store(config)
    before = store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
    malformed = _audit_event()
    del malformed["reason_code"]
    with pytest.raises(ValueError, match="Missing decision audit field"):
        store.save(initial_state(config), [("DECISION_AUDIT", malformed)])
    assert store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone() == before
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert store.decision_history() == []
    store.close()


def test_failed_store_initialization_releases_account_lock(config):
    database = config.data_dir / "quantbot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(ValueError, match="newer than supported"):
        Store(config)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=0")
    store = Store(config)
    store.close()
