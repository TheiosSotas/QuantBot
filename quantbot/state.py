from datetime import datetime, timezone

from .models import finite_number
from .protections import initial_protections, validate_protections


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_state(config):
    return {
        "system": {
            "status": "STARTING",
            "mode": "PAPER",
            "market_type": "BINANCE USDT-M FUTURES / PAPER",
            "symbol": config.symbol,
            "timeframe": config.timeframe,
            "last_update": "Starting up...",
            "last_quote_at": None,
            "last_protection_at": None,
            "last_model_at": None,
            "model_status": "IDLE",
            "storage_error": None,
            "error": None,
            "port": 0,
        },
        "market": {
            "price": 0.0,
            "ema_20": 0.0,
            "ema_50": 0.0,
            "ema_200": 0.0,
            "rsi_14": 50.0,
            "atr_14": 0.0,
            "vol_ratio": 1.0,
        },
        "model": {
            "regime": "CALIBRATING",
            "action": "HOLD",
            "confidence": 0.0,
            "thesis": "Waiting for a closed candle and local model.",
        },
        "portfolio": {
            "cash_usdt": config.starting_cash,
            "side": "FLAT",
            "position_units": 0.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "tranches": 0,
            "breakeven_active": False,
            "unrealized_pnl": 0.0,
            "total_equity": config.starting_cash,
        },
        "protections": initial_protections(),
        "position_revision": 0,
        "last_decision_candle": None,
        "trades": [],
        "copilot_feed": [],
        "lessons": [],
    }


def thought(state, message, category="ANALYSIS"):
    state["copilot_feed"].insert(0, {"time": timestamp(), "category": category, "message": message})
    del state["copilot_feed"][45:]


def validate_state(state):
    if "protections" in state:
        validate_protections(state["protections"])
    p = state["portfolio"]
    for key in (
        "cash_usdt",
        "position_units",
        "entry_price",
        "stop_loss",
        "take_profit",
        "unrealized_pnl",
        "total_equity",
    ):
        finite_number(p[key], key)
    if p["side"] not in ("FLAT", "LONG", "SHORT") or p["position_units"] < 0:
        raise ValueError("Invalid saved position")
    if p["side"] == "FLAT":
        if any(p[k] != 0 for k in ("position_units", "entry_price", "stop_loss", "take_profit", "tranches")):
            raise ValueError("Flat saved position contains exposure")
    elif min(p[k] for k in ("position_units", "entry_price", "stop_loss", "take_profit", "tranches")) <= 0:
        raise ValueError("Incomplete saved position")
    elif (p["take_profit"] - p["stop_loss"]) * (1 if p["side"] == "LONG" else -1) <= 0:
        raise ValueError("Saved stop/target ordering is invalid")
    if type(p["tranches"]) is not int or type(p["breakeven_active"]) is not bool:
        raise ValueError("Invalid saved protection state")
    if not isinstance(state["position_revision"], int) or state["position_revision"] < 0:
        raise ValueError("Invalid saved revision")
    if state["last_decision_candle"] is not None and type(state["last_decision_candle"]) is not int:
        raise ValueError("Invalid saved candle")
    if not all(isinstance(state[key], list) for key in ("trades", "lessons", "copilot_feed")):
        raise ValueError("Invalid saved history")
