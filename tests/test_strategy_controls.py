"""Research controls change one rule while preserving execution safeguards."""

from dataclasses import replace

import pytest
from conftest import apply, decision, seed, snapshot

from quantbot.config import Config


def test_no_confidence_variant_is_explicit_and_still_enforces_entry_filters(engine, config):
    assert apply(engine, decision(confidence=0.1)) == "HOLD"
    engine.config = replace(config, confidence_gate_enabled=False)
    data = snapshot(candle=snapshot().candle_id + 3_600_000)
    assert apply(engine, decision(confidence=0.1), data=data) == "OPENED"
    state = engine.snapshot()["portfolio"]
    assert state["position_units"] * 100 <= state["total_equity"] * config.max_notional_equity
    assert state["stop_loss"] == 92.5


def test_no_confidence_does_not_bypass_strategy_conditions(engine, config):
    engine.config = replace(config, confidence_gate_enabled=False)
    assert apply(engine, decision(confidence=0.1), data=snapshot(ema_50=110)) == "HOLD"
    assert engine.snapshot()["portfolio"]["side"] == "FLAT"


def test_no_scale_preserves_original_stop_and_allows_low_confidence_close_when_configured(engine, config):
    seed(engine)
    engine.config = replace(config, scale_enabled=False)
    assert apply(engine, decision(action="HOLD", scale=True), price=110) == "HOLD"
    state = engine.snapshot()
    assert state["portfolio"]["tranches"] == 1 and state["portfolio"]["stop_loss"] == 90
    assert state["system"]["last_decision_audit"]["reason_code"] == "SCALE_DISABLED"
    engine.config = replace(config, scale_enabled=False, confidence_gate_enabled=False)
    data = snapshot(candle=snapshot().candle_id + 3_600_000)
    assert apply(engine, decision(action="CLOSE", confidence=0.1), data=data) == "CLOSED"


def test_trend_filter_blocks_weak_entry_but_never_existing_position_exit(engine, config):
    engine.config = replace(config, min_trend_strength=2)
    assert apply(engine) == "HOLD"  # EMA separation / ATR = 1.
    seed(engine)
    data = snapshot(candle=snapshot().candle_id + 3_600_000)
    assert apply(engine, decision(action="CLOSE"), data=data) == "CLOSED"


@pytest.mark.parametrize("side,price", [("LONG", 89), ("SHORT", 111)])
def test_experimental_controls_do_not_prevent_protective_stop(engine, config, side, price):
    seed(engine, side)
    engine.config = replace(
        config, scale_enabled=False, confidence_gate_enabled=False, min_trend_strength=100
    )
    data = snapshot(price=price, side=side)
    assert apply(engine, decision(action="HOLD", confidence=0), data=data, price=price) == "PROTECTIVE_EXIT"
    assert engine.snapshot()["portfolio"]["side"] == "FLAT"
    assert engine.snapshot()["trades"][0]["type"] == "STOP_LOSS"


@pytest.mark.parametrize(
    "settings",
    [
        {"scale_enabled": "false"},
        {"confidence_gate_enabled": 0},
        {"min_trend_strength": -1},
        {"min_trend_strength": float("nan")},
        {"min_trend_strength": True},
        {"max_tranches": True},
        {"max_tranches": 1.5},
    ],
)
def test_invalid_strategy_controls_are_rejected(settings):
    with pytest.raises(ValueError):
        Config(**settings)
