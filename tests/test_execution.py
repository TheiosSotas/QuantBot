import math
from dataclasses import replace

import pytest
from conftest import CANDLE, NOW, apply, decision, quote, seed, snapshot

from quantbot.models import Decision
from quantbot.risk import size_addition


@pytest.mark.parametrize("side,price", [("LONG", 111), ("SHORT", 89)])
def test_scale_cannot_undo_same_tick_breakeven(engine, side, price):
    seed(engine, side)
    result = apply(engine, decision("HOLD", scale=True), snapshot(price, 20, side), price)
    p = engine.snapshot()["portfolio"]
    assert result == "SCALED"
    assert p["stop_loss"] == 100
    assert not p["breakeven_active"]  # An enlarged position has a new average entry.
    sign = 1 if side == "LONG" else -1
    engine.on_quote(quote(p["stop_loss"] - sign), NOW)
    assert engine.state["portfolio"]["side"] == "FLAT"


@pytest.mark.parametrize("side,price", [("LONG", 121), ("SHORT", 79)])
def test_reached_target_closes_before_scale(engine, side, price):
    seed(engine, side)
    assert apply(engine, decision("HOLD", scale=True), snapshot(price, side=side), price) == "PROTECTIVE_EXIT"
    assert engine.state["portfolio"]["side"] == "FLAT"
    assert engine.state["trades"][0]["type"] == "TAKE_PROFIT"
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='SCALE'").fetchone()[0] == 0


def test_close_instruction_wins_over_scale(engine):
    seed(engine)
    assert apply(engine, decision("CLOSE", scale=True), snapshot(105), 105) == "CLOSED"
    assert engine.state["trades"][0]["units"] == 1
    assert engine.store.db.execute("SELECT count(*) FROM events WHERE kind='SCALE'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "data,regime",
    [(snapshot(80), "TRENDING_BULL"), (snapshot(rsi_14=70), "TRENDING_BULL"), (snapshot(), "CHOPPY_RANGE")],
)
def test_entry_filters_are_mandatory(engine, data, regime):
    assert apply(engine, decision(regime=regime), data, data.current_price) == "HOLD"
    assert engine.state["portfolio"]["side"] == "FLAT"


def test_short_filter_and_pnl(engine):
    assert apply(engine, decision("SELL", regime="TRENDING_BEAR"), snapshot(100, side="SHORT")) == "OPENED"
    units = engine.state["portfolio"]["position_units"]
    engine.on_quote(quote(110), NOW)
    assert engine.state["trades"][0]["pnl"] == pytest.approx(-10 * units)


def test_scaling_requires_volume(engine):
    seed(engine)
    assert apply(engine, decision("HOLD", scale=True), snapshot(105, volume_ratio=0.2), 105) == "HOLD"
    assert engine.state["portfolio"]["tranches"] == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("scale_in_signal", "false"),
        ("confidence", "75%"),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", True),
        ("confidence", 90),
        ("action", "YOLO"),
        ("thesis", {}),
    ],
)
def test_malformed_model_values_are_rejected(key, value):
    data = decision()
    data[key] = value
    with pytest.raises(ValueError):
        Decision.parse(data)


def test_zero_atr_rejected_and_invalid_decision_cannot_open(engine):
    with pytest.raises(ValueError):
        apply(engine, decision("HOLD"), snapshot(atr=0))
    assert engine.state["portfolio"]["side"] == "FLAT"


def test_small_atr_cannot_create_unbounded_notional(engine):
    assert apply(engine, data=snapshot(60000, atr=10), price=60000) == "OPENED"
    p = engine.state["portfolio"]
    assert p["position_units"] * 60000 <= p["total_equity"] * engine.config.max_notional_equity


def test_floor_size_respects_aggregate_stop_budget(config):
    p = {"side": "LONG", "total_equity": 1000, "position_units": 2, "entry_price": 100}
    config = replace(config, max_position_risk=0.01, risk_per_tranche=0.01)
    units = size_addition(config, p, 105, 96)
    assert (100 - 96) * 2 + (105 - 96) * units <= 10


def test_only_one_evaluation_per_closed_candle_even_after_restart(engine):
    assert apply(engine) == "OPENED"
    assert apply(engine, decision("HOLD", scale=True), snapshot(105), 105) == "ALREADY_EVALUATED"
    assert engine.store.load()["last_decision_candle"] == CANDLE


def test_stale_decision_and_changed_position_are_rejected(engine):
    assert apply(engine, requested_at=NOW - 121) == "STALE_DECISION"
    assert apply(engine, latest_candle=CANDLE + 3600000) == "STALE_CANDLE"
    assert apply(engine, revision=99) == "POSITION_CHANGED"
    assert engine.state["portfolio"]["side"] == "FLAT"


def test_stale_or_nonfinite_quotes_are_rejected(engine):
    for q in (quote(float("nan")), quote(100, NOW - 16), quote(-10), quote(100, NOW + 1)):
        with pytest.raises(ValueError):
            engine.on_quote(q, NOW)


def test_overflowing_quote_cannot_poison_account_state(engine):
    seed(engine, position_units=100.0)
    before = engine.snapshot()["portfolio"]
    with pytest.raises(ValueError):
        engine.on_quote(quote(1e308), NOW)
    assert engine.state["portfolio"] == before


def test_cash_and_position_reconcile_after_multiple_fills(engine):
    seed(engine)
    apply(engine, decision("HOLD", scale=True), snapshot(111, atr=20), 111)
    p = engine.snapshot()["portfolio"]
    expected = 10000 + (99 - p["entry_price"]) * p["position_units"]
    engine.on_quote(quote(99), NOW)
    assert engine.state["portfolio"]["cash_usdt"] == pytest.approx(expected)
    assert math.isfinite(engine.state["portfolio"]["cash_usdt"])
