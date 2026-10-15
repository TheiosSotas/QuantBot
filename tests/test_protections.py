from dataclasses import replace

import pytest

from quantbot.config import Config
from quantbot.protections import (
    entry_block_reason,
    initial_protections,
    migrate_protections,
    register_close,
    update_protections,
    validate_protections,
)

BASE = 1_700_000_000.0


def account(equity=10_000.0):
    return {"portfolio": {"total_equity": equity}, "protections": initial_protections()}


def test_defaults_are_disabled_and_protection_waits_for_first_quote():
    config = Config()
    state = account()
    assert config.daily_loss_limit is None
    assert config.max_account_drawdown is None
    assert not state["protections"]["initialized"]
    assert entry_block_reason(state, config, BASE) == "PROTECTION_STATE_INVALID"
    assert update_protections(state, config, BASE)
    assert state["protections"]["peak_equity"] == 10_000
    assert entry_block_reason(state, config, BASE) is None


def test_daily_loss_latches_through_intraday_recovery_and_resets_on_utc_day():
    config = replace(Config(), daily_loss_limit=0.10)
    state = account()
    update_protections(state, config, BASE)
    state["portfolio"]["total_equity"] = 8_900
    assert update_protections(state, config, BASE + 60)
    assert entry_block_reason(state, config, BASE + 60) == "DAILY_LOSS_LIMIT"
    triggered_at = state["protections"]["daily_loss_triggered_at"]
    state["portfolio"]["total_equity"] = 9_900
    update_protections(state, config, BASE + 120)
    assert state["protections"]["daily_loss_triggered_at"] == triggered_at
    assert entry_block_reason(state, config, BASE + 120) == "DAILY_LOSS_LIMIT"

    next_day = BASE + 24 * 60 * 60
    state["portfolio"]["total_equity"] = 9_900
    update_protections(state, config, next_day)
    assert state["protections"]["daily_loss_triggered_at"] is None
    assert state["protections"]["daily_start_equity"] == 9_900
    assert entry_block_reason(state, config, next_day) is None


def test_global_drawdown_uses_unrealized_equity_and_latch_survives_day_change():
    config = replace(Config(), max_account_drawdown=0.20)
    state = account()
    update_protections(state, config, BASE)
    state["portfolio"]["total_equity"] = 7_900
    update_protections(state, config, BASE + 60)
    assert state["protections"]["current_drawdown"] == pytest.approx(0.21)
    assert entry_block_reason(state, config, BASE + 60) == "MAX_DRAWDOWN"

    # Even a healthier new session and a later config with the limit disabled
    # cannot clear a previously triggered account-level latch.
    next_day = BASE + 24 * 60 * 60
    state["portfolio"]["total_equity"] = 9_000
    disabled = replace(config, max_account_drawdown=None)
    update_protections(state, disabled, next_day)
    assert state["protections"]["max_drawdown_triggered_at"] == BASE + 60
    assert entry_block_reason(state, disabled, next_day) == "MAX_DRAWDOWN"


def test_first_nonpositive_equity_is_tracked_and_blocks_entries():
    state = account(0)
    config = Config()
    update_protections(state, config, BASE)
    assert state["protections"]["initialized"]
    assert state["protections"]["peak_equity"] == 0
    assert entry_block_reason(state, config, BASE) == "INVALID_EQUITY"


def test_negative_equity_records_loss_greater_than_one_and_trips_limits():
    config = replace(Config(), daily_loss_limit=0.90, max_account_drawdown=0.95)
    state = account()
    update_protections(state, config, BASE)
    state["portfolio"]["total_equity"] = -1_000
    update_protections(state, config, BASE + 60)
    assert state["protections"]["current_drawdown"] == pytest.approx(1.1)
    assert state["protections"]["daily_loss"] == pytest.approx(1.1)
    assert state["protections"]["daily_loss_triggered_at"] == BASE + 60
    assert state["protections"]["max_drawdown_triggered_at"] == BASE + 60
    assert entry_block_reason(state, config, BASE + 60) == "INVALID_EQUITY"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(peak_equity=value["current_equity"] - 1),
        lambda value: value.update(session_date="2023-11-13"),
        lambda value: value.update(daily_loss_triggered_at=value["last_update_at"] + 1),
    ],
)
def test_persisted_protection_state_rejects_fabricated_invariants(mutation):
    state = account()
    update_protections(state, Config(), BASE)
    value = state["protections"].copy()
    mutation(value)
    with pytest.raises(ValueError):
        validate_protections(value)


def test_close_registers_cooldown_and_repeated_close_extends_it():
    config = replace(Config(), cooldown_seconds=30)
    state = account()
    update_protections(state, config, BASE)
    register_close(state, config, BASE + 10)
    assert entry_block_reason(state, config, BASE + 39) == "COOLDOWN"
    assert entry_block_reason(state, config, BASE + 40) is None
    register_close(state, config, BASE + 35)
    assert entry_block_reason(state, config, BASE + 64) == "COOLDOWN"
    assert entry_block_reason(state, config, BASE + 65) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_loss_limit": 0},
        {"daily_loss_limit": 1},
        {"daily_loss_limit": True},
        {"daily_loss_limit": float("nan")},
        {"max_account_drawdown": -0.1},
        {"max_account_drawdown": float("inf")},
        {"cooldown_seconds": -1},
        {"cooldown_seconds": True},
        {"cooldown_seconds": float("nan")},
        {"equity_sample_seconds": 0},
        {"equity_sample_seconds": float("inf")},
    ],
)
def test_protection_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


def test_boolean_or_backward_time_is_rejected_and_entry_fails_closed():
    state = account()
    config = Config()
    update_protections(state, config, BASE)
    with pytest.raises(ValueError):
        update_protections(state, config, True)
    with pytest.raises(ValueError, match="backward"):
        update_protections(state, config, BASE - 1)
    assert entry_block_reason(state, config, BASE - 1) == "PROTECTION_STATE_INVALID"


def test_migration_adds_only_a_missing_whole_protection_block():
    legacy = {"portfolio": {"total_equity": 1}}
    assert migrate_protections(legacy)
    assert legacy["protections"] == initial_protections()
    partial = {"protections": {"initialized": False}}
    assert not migrate_protections(partial)
    with pytest.raises(ValueError):
        validate_protections(partial["protections"])


def test_protection_state_rejects_boolean_metrics():
    value = initial_protections()
    value["daily_loss"] = True
    with pytest.raises(ValueError):
        validate_protections(value)
