"""Bounded JSON-safe decision evidence, separate from trading authority."""

import math
from dataclasses import asdict, is_dataclass


def clean(value, depth=0):
    if depth > 4:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
    if isinstance(value, str):
        return value[:4000]
    if is_dataclass(value) and not isinstance(value, type):
        return clean(asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(key)[:100]: clean(item, depth + 1) for key, item in list(value.items())[:32]}
    return None


def number(value):
    return clean(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else None


def integer(value):
    return value if type(value) is int and number(value) is not None else None


def decision_event(
    config,
    *,
    now,
    outcome,
    reason_code,
    status,
    requested_at=None,
    snapshot=None,
    quote=None,
    revision=None,
    latest_candle=None,
    decision=None,
    detail="",
):
    action = (
        getattr(decision, "action", None)
        if is_dataclass(decision)
        else (decision.get("action") if isinstance(decision, dict) else None)
    )
    return {
        "observed_at": number(now),
        "requested_at": number(requested_at),
        "candle_id": integer(getattr(snapshot, "candle_id", None)),
        "decision_interval_seconds": config.decision_interval_seconds,
        "action": clean(action) if isinstance(action, str) else None,
        "status": status,
        "reason_code": reason_code,
        "outcome": outcome,
        "model": config.model,
        "snapshot": clean(snapshot),
        "decision": clean(decision),
        "quote_at": number(getattr(quote, "observed_at", None)),
        "execution_price": number(getattr(quote, "price", None)),
        "position_revision": integer(revision),
        "latest_candle": integer(latest_candle),
        "detail": str(detail)[:500],
    }
