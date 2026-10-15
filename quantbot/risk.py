import math


def eligible(side, snapshot, price, regime):
    """Enforce the existing prompt's entry filters at the execution boundary."""
    if regime == "CHOPPY_RANGE":
        return False
    if side == "LONG":
        return price > snapshot.ema_50 > snapshot.ema_200 and snapshot.rsi_14 < 68
    return price < snapshot.ema_50 < snapshot.ema_200 and snapshot.rsi_14 > 32


def size_addition(config, portfolio, price, stop):
    equity = portfolio["total_equity"]
    distance = abs(price - stop)
    if equity <= 0 or distance <= 0 or stop <= 0:
        return 0.0
    units = portfolio["position_units"]
    sign = 1 if portfolio["side"] != "SHORT" else -1
    old_risk = max(0.0, (portfolio["entry_price"] - stop) * units * sign)
    risk_budget = min(
        equity * config.risk_per_tranche, max(0.0, equity * config.max_position_risk - old_risk)
    )
    notional_room = max(0.0, equity * config.max_notional_equity - units * price)
    raw = min(risk_budget / distance, notional_room / price)
    # Floor rather than round up through a budget or quantity ceiling.
    return round(math.floor(raw / config.unit_step) * config.unit_step, 10)
