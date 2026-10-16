import csv
import sqlite3

import pytest
from conftest import NOW, apply, quote, seed

from quantbot.execution import PaperEngine
from quantbot.storage import CSV_FIELDS, Store


def test_open_position_and_protection_survive_restart(config):
    first = Store(config)
    engine = PaperEngine(config, first)
    apply(engine)
    engine.on_quote(quote(108), NOW)
    before = engine.snapshot()
    first.close()
    second = Store(config)
    try:
        restarted = PaperEngine(config, second)
        assert restarted.state["portfolio"] == before["portfolio"]
        assert restarted.state["last_decision_candle"] == before["last_decision_candle"]
        restarted.on_quote(quote(99), NOW)
        assert restarted.state["portfolio"]["side"] == "FLAT"
    finally:
        second.close()


def test_database_failure_rolls_back_cash_position_and_event(engine):
    seed(engine)
    before = engine.snapshot()
    engine.store.db.execute(
        "CREATE TRIGGER broken_disk BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        engine.on_quote(quote(80), NOW)
    assert engine.state["portfolio"] == before["portfolio"]
    assert engine.store.load()["portfolio"] == before["portfolio"]
    assert engine.state["trades"] == []
    engine.store.db.execute("DROP TRIGGER broken_disk")
    engine.on_quote(quote(80), NOW)
    assert engine.state["portfolio"]["cash_usdt"] == 9980
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='CLOSE'").fetchone()[0] == 1


def test_failed_csv_export_does_not_leave_position_open_or_repeat_pnl(engine, monkeypatch):
    seed(engine)

    def fail():
        raise OSError("CSV is locked")

    monkeypatch.setattr(engine.store, "export_csv", fail)
    engine.on_quote(quote(80), NOW)
    engine.on_quote(quote(80), NOW)
    assert engine.state["portfolio"]["cash_usdt"] == 9980
    assert engine.store.load()["portfolio"]["side"] == "FLAT"
    assert len(engine.state["trades"]) == 1
    assert "CSV" in engine.state["system"]["csv_warning"]


def test_legacy_csv_import_is_once_and_keeps_original(config):
    target = config.data_dir / "trades.csv"
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_FIELDS)
        writer.writerow(["old", "LONG", "STOP_LOSS", 100, 90, 1, -10, -10, "LOSS"])
    original = target.read_bytes()
    store = Store(config)
    try:
        assert store.load()["portfolio"]["cash_usdt"] == 9990
        assert store.load()["portfolio"]["cash_usdt"] == 9990
        assert target.read_bytes() == original
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    finally:
        store.close()


def test_corrupt_saved_state_never_silently_resets(config):
    store = Store(config)
    store.load()
    store.db.execute("UPDATE snapshot SET payload='{}'")
    store.db.commit()
    with pytest.raises((ValueError, KeyError)):
        store.load()
    store.close()


def test_two_bot_processes_cannot_share_the_same_account(config):
    first = Store(config)
    try:
        with pytest.raises(RuntimeError, match="Another QuantBot"):
            Store(config)
    finally:
        first.close()
    second = Store(config)
    second.close()


def test_storage_recovers_without_replaying_a_failed_flat_decision(engine):
    engine.store.db.execute(
        "CREATE TRIGGER failed_update BEFORE UPDATE ON snapshot BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        apply(engine)
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["system"]["storage_error"]
    engine.store.db.execute("DROP TRIGGER failed_update")
    engine.on_quote(quote(100), NOW)
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["system"]["storage_error"] is None
    assert engine.store.load()["portfolio"]["side"] == "FLAT"


def test_saved_account_cannot_silently_switch_instruments(engine, config):
    from dataclasses import replace

    engine.store.config = replace(config, symbol="ETH/USDT:USDT")
    with pytest.raises(ValueError, match="different symbol"):
        engine.store.load()


def test_lessons_are_persisted_but_do_not_mutate_strategy(engine):
    before = engine.snapshot()["portfolio"]
    engine.add_lesson(
        {
            "date": "test",
            "root_cause": "hypothesis",
            "preventative_rule": "unverified",
            "pnl": -1,
            "side": "LONG",
        }
    )
    assert engine.store.load()["lessons"][0]["preventative_rule"] == "unverified"
    assert engine.state["portfolio"] == before
