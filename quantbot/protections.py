"""Persistent account-level loss limits and entry cooldowns.

The module deliberately does not pick risk limits. A limit is active only when
the corresponding Config value is set. Once a configured limit latches, the
latch stays authoritative across restarts and later config changes.
"""

import math
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .models import finite_number

_FIELDS = {
    "initialized",
    "current_equity",
    "peak_equity",
    "daily_start_equity",
    "session_date",
    "current_drawdown",
    "daily_loss",
    "daily_loss_triggered_at",
    "max_drawdown_triggered_at",
    "cooldown_until",
    "last_update_at",
}


def initial_protections():
    """Return an uninitialized protection record, awaiting a live equity sample."""
    return {
        "initialized": False,
        "current_equity": None,
        "peak_equity": None,
        "daily_start_equity": None,
        "session_date": None,
        "current_drawdown": 0.0,
        "daily_loss": 0.0,
        "daily_loss_triggered_at": None,
        "max_drawdown_triggered_at": None,
        "cooldown_until": None,
        "last_update_at": None,
    }


def migrate_protections(state):
    """Add protections only to a legacy snapshot with no protection block."""
    if "protections" not in state:
        state["protections"] = initial_protections()
        return True
    return False


def _optional_time(value, name):
    if value is None:
        return None
    result = finite_number(value, name)
    if result < 0:
        raise ValueError(f"Invalid {name}")
    return result


def validate_protections(value):
    """Validate the persisted protection state, rejecting partial/corrupt data."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Invalid saved protection state")
    if type(value["initialized"]) is not bool:
        raise ValueError("Invalid saved protection initialization")
    if value["initialized"]:
        for name in ("current_equity", "peak_equity", "daily_start_equity"):
            finite_number(value[name], name)
        if value["peak_equity"] < value["current_equity"]:
            raise ValueError("Saved protection peak is below current equity")
        if not isinstance(value["session_date"], str):
            raise ValueError("Invalid saved protection session date")
        try:
            parsed = datetime.strptime(value["session_date"], "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != value["session_date"]:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Invalid saved protection session date") from exc
    elif any(
        value[key] is not None
        for key in ("current_equity", "peak_equity", "daily_start_equity", "session_date")
    ):
        raise ValueError("Uninitialized protection state contains equity baselines")
    for name in ("current_drawdown", "daily_loss"):
        number = finite_number(value[name], name)
        if number < 0:
            raise ValueError(f"Invalid saved {name}")
    for name in ("daily_loss_triggered_at", "max_drawdown_triggered_at", "cooldown_until", "last_update_at"):
        _optional_time(value[name], name)
    if value["initialized"] != (value["last_update_at"] is not None):
        raise ValueError("Invalid saved protection timestamp")
    if value["initialized"]:
        last_update = value["last_update_at"]
        _, update_date = _now_value(last_update)
        if value["session_date"] != update_date:
            raise ValueError("Saved protection session date does not match its last update")
        for name in ("daily_loss_triggered_at", "max_drawdown_triggered_at"):
            triggered_at = value[name]
            if triggered_at is not None and triggered_at > last_update:
                raise ValueError(f"Saved {name} is later than the last update")
    if not value["initialized"] and (
        value["daily_loss_triggered_at"] is not None or value["max_drawdown_triggered_at"] is not None
    ):
        raise ValueError("Uninitialized protection state contains a latch")
    return value


def _now_value(now):
    result = finite_number(now, "protection timestamp")
    if result < 0:
        raise ValueError("Invalid protection timestamp")
    try:
        date = datetime.fromtimestamp(result, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Invalid protection timestamp") from exc
    return result, date


def _fractional_loss(reference, equity):
    if reference <= 0:
        return 0.0
    if equity >= reference:
        return 0.0
    # Decimal avoids overflowing the subtraction when a finite positive peak
    # is compared with a large negative equity. The resulting metric remains
    # a float, so reject it if the actual ratio cannot be represented finitely.
    with localcontext() as context:
        context.prec = 40
        loss = (Decimal(str(reference)) - Decimal(str(equity))) / Decimal(str(reference))
    try:
        result = float(loss)
    except OverflowError as exc:
        raise ValueError("Protection loss ratio overflow") from exc
    if not math.isfinite(result):
        raise ValueError("Protection loss ratio overflow")
    return result


def update_protections(state, config, now):
    """Update persistent account metrics from current portfolio equity.

    `now` is the timestamp of the fresh quote that produced the equity value.
    Backward clocks and invalid account data are rejected so callers can fail
    closed instead of silently resetting a daily baseline.
    """
    value = state.get("protections")
    validate_protections(value)
    now, utc_date = _now_value(now)
    previous_update = value["last_update_at"]
    if previous_update is not None and now < previous_update:
        raise ValueError("Protection clock moved backward")
    equity = finite_number(state["portfolio"]["total_equity"], "total_equity")
    before = value.copy()

    if not value["initialized"]:
        value.update(
            initialized=True,
            current_equity=equity,
            peak_equity=equity,
            daily_start_equity=equity,
            session_date=utc_date,
            current_drawdown=0.0,
            daily_loss=0.0,
            last_update_at=now,
        )
    else:
        if utc_date > value["session_date"]:
            # A new UTC session starts at its first observed equity. The global
            # peak and drawdown latch deliberately survive this rollover.
            value["session_date"] = utc_date
            value["daily_start_equity"] = equity
            value["daily_loss"] = 0.0
            value["daily_loss_triggered_at"] = None
        value["current_equity"] = equity
        value["peak_equity"] = max(value["peak_equity"], equity)
        value["current_drawdown"] = _fractional_loss(value["peak_equity"], equity)
        value["daily_loss"] = _fractional_loss(value["daily_start_equity"], equity)
        value["last_update_at"] = now

    # Evaluate limits on the initialization sample too, including the zero or
    # negative equity case where fractions cannot safely be computed.
    if value["initialized"]:
        if (
            config.daily_loss_limit is not None
            and value["daily_loss_triggered_at"] is None
            and value["daily_loss"] >= config.daily_loss_limit
        ):
            value["daily_loss_triggered_at"] = now
        if (
            config.max_account_drawdown is not None
            and value["max_drawdown_triggered_at"] is None
            and value["current_drawdown"] >= config.max_account_drawdown
        ):
            value["max_drawdown_triggered_at"] = now
    validate_protections(value)
    return value != before


def entry_block_reason(state, config, now):
    """Return the persistent reason code preventing a new account entry."""
    try:
        value = state.get("protections")
        validate_protections(value)
        now, _ = _now_value(now)
        last_update = value["last_update_at"]
        if last_update is not None and now < last_update:
            return "PROTECTION_STATE_INVALID"
    except (KeyError, TypeError, ValueError):
        return "PROTECTION_STATE_INVALID"
    if not value["initialized"]:
        return "PROTECTION_STATE_INVALID"
    if value["current_equity"] <= 0:
        return "INVALID_EQUITY"
    if value["max_drawdown_triggered_at"] is not None:
        return "MAX_DRAWDOWN"
    if value["daily_loss_triggered_at"] is not None:
        return "DAILY_LOSS_LIMIT"
    if value["cooldown_until"] is not None and now < value["cooldown_until"]:
        return "COOLDOWN"
    return None


def register_close(state, config, now):
    """Start or extend the entry cooldown after every position close."""
    value = state.get("protections")
    validate_protections(value)
    now, _ = _now_value(now)
    last_update = value["last_update_at"]
    if last_update is not None and now < last_update:
        raise ValueError("Protection clock moved backward")
    value["cooldown_until"] = now + config.cooldown_seconds
    if value["last_update_at"] is None or now > value["last_update_at"]:
        value["last_update_at"] = now
    validate_protections(value)
