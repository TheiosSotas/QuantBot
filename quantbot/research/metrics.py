"""Account-level performance metrics for historical research replays.

Returns are measured from the supplied starting capital and include marked-to-
market equity samples. Fees and funding are excluded unless already reflected
in the input equity/trade records. Exposure is left-continuous: each sample's
exposure applies from its timestamp up to (but not including) the next sample.
"""

import math
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Invalid {name}")
    return result


def _records(values, name):
    if values is None:
        raise ValueError(f"{name} must be an iterable of records")
    try:
        return list(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be an iterable of records") from exc


def _utc_date(timestamp):
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Invalid equity timestamp") from exc


def _drawdown(peak, equity):
    if peak <= 0 or equity >= peak:
        return 0.0
    # Decimal avoids overflow in (peak - equity) for finite positive/negative
    # float inputs. Raise if the mathematical ratio is too large for JSON float.
    from decimal import Decimal, localcontext

    with localcontext() as context:
        context.prec = 40
        ratio = (Decimal(str(peak)) - Decimal(str(equity))) / Decimal(str(peak))
    try:
        result = float(ratio)
    except OverflowError as exc:
        raise ValueError("Drawdown ratio overflow") from exc
    if not math.isfinite(result):
        raise ValueError("Drawdown ratio overflow")
    return result


def _daily_returns(samples, starting_cash):
    if not samples:
        return []
    last_by_day = {}
    for sample in samples:
        last_by_day[sample["date"]] = sample["equity"]
    first_day, last_day = min(last_by_day), max(last_by_day)
    result = []
    previous = starting_cash
    current_day = first_day
    last_observed = starting_cash
    while current_day <= last_day:
        if current_day in last_by_day:
            last_observed = last_by_day[current_day]
        daily_return = None
        if previous > 0:
            daily_return = last_observed / previous - 1.0
            if not math.isfinite(daily_return):
                raise ValueError("Daily return overflow")
        result.append({"date": current_day.isoformat(), "return": daily_return})
        previous = last_observed
        current_day += timedelta(days=1)
    return result


def summarize(equity_rows, trades, *, starting_cash, periods_per_year=365):
    """Summarize marked-to-market account performance as JSON-safe values.

    Equity rows and trades are consumed in chronological order. Every equity
    sample is compared with the supplied starting capital/path peak; the first
    sample is never treated as a new performance baseline. Losing streaks use
    the supplied trade order. A sample's exposure applies until the next sample
    (left-continuous weighting); with fewer than two timestamps, average
    exposure is unavailable.
    """
    starting_cash = _number(starting_cash, "starting_cash")
    if starting_cash <= 0:
        raise ValueError("starting_cash must be positive")
    periods_per_year = _number(periods_per_year, "periods_per_year")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")

    raw_rows = _records(equity_rows, "equity_rows")
    raw_trades = _records(trades, "trades")
    samples = []
    previous_time = None
    for row in raw_rows:
        if not isinstance(row, dict):
            raise ValueError("Each equity row must be an object")
        for key in ("observed_at", "equity", "exposure", "peak_equity", "drawdown"):
            if key not in row:
                raise ValueError(f"Missing equity field: {key}")
        observed_at = _number(row["observed_at"], "observed_at")
        equity = _number(row["equity"], "equity")
        exposure = _number(row["exposure"], "exposure")
        _number(row["peak_equity"], "peak_equity")
        stored_drawdown = _number(row["drawdown"], "drawdown")
        if observed_at < 0 or exposure < 0 or stored_drawdown < 0:
            raise ValueError("Invalid equity sample metrics")
        if previous_time is not None and observed_at < previous_time:
            raise ValueError("Equity rows must be chronological")
        previous_time = observed_at
        samples.append(
            {
                "observed_at": observed_at,
                "equity": equity,
                "exposure": exposure,
                "date": _utc_date(observed_at),
                "unrealized_pnl": (
                    _number(row["unrealized_pnl"], "unrealized_pnl") if "unrealized_pnl" in row else None
                ),
            }
        )

    trade_pnls = []
    for trade in raw_trades:
        if not isinstance(trade, dict) or "pnl" not in trade:
            raise ValueError("Each trade must be an object with pnl")
        trade_pnls.append(_number(trade["pnl"], "trade pnl"))

    peak = starting_cash
    max_drawdown = 0.0
    for sample in samples:
        peak = max(peak, sample["equity"])
        max_drawdown = max(max_drawdown, _drawdown(peak, sample["equity"]))
    ending_equity = samples[-1]["equity"] if samples else starting_cash
    total_return = ending_equity / starting_cash - 1.0
    if not math.isfinite(total_return):
        raise ValueError("Total return overflow")

    wins = sum(pnl > 0 for pnl in trade_pnls)
    losses = sum(pnl < 0 for pnl in trade_pnls)
    gross_profit = sum(pnl for pnl in trade_pnls if pnl > 0)
    gross_loss = -sum(pnl for pnl in trade_pnls if pnl < 0)
    realized_pnl = sum(trade_pnls)
    if not all(math.isfinite(value) for value in (gross_profit, gross_loss, realized_pnl)):
        raise ValueError("Trade aggregate overflow")
    trade_count = len(trade_pnls)
    no_loss = gross_loss == 0
    profit_factor = None if no_loss else gross_profit / gross_loss
    expectancy = None if trade_count == 0 else realized_pnl / trade_count
    if profit_factor is not None and not math.isfinite(profit_factor):
        raise ValueError("Profit factor overflow")
    if expectancy is not None and not math.isfinite(expectancy):
        raise ValueError("Expectancy overflow")

    losing_streak = max_losing_streak = 0
    for pnl in trade_pnls:
        if pnl < 0:
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)
        else:
            losing_streak = 0

    exposure_duration = 0.0
    weighted_exposure = 0.0
    in_market_duration = 0.0
    for current, following in zip(samples, samples[1:]):
        duration = following["observed_at"] - current["observed_at"]
        exposure_duration += duration
        weighted_exposure += duration * current["exposure"]
        if current["exposure"] > 0:
            in_market_duration += duration
    time_weighted_exposure_pct = None
    time_in_market_pct = None
    if exposure_duration > 0:
        time_weighted_exposure_pct = 100.0 * weighted_exposure / exposure_duration
        time_in_market_pct = 100.0 * in_market_duration / exposure_duration
        if not math.isfinite(time_weighted_exposure_pct):
            raise ValueError("Time-weighted exposure overflow")
        if not math.isfinite(time_in_market_pct):
            raise ValueError("Time-in-market overflow")

    daily_returns = _daily_returns(samples, starting_cash)
    sharpe_daily = None
    defined_returns = [item["return"] for item in daily_returns if item["return"] is not None]
    if len(defined_returns) >= 2:
        returns = defined_returns
        deviation = stdev(returns)
        if deviation > 0:
            sharpe_daily = mean(returns) / deviation * math.sqrt(periods_per_year)
            if not math.isfinite(sharpe_daily):
                raise ValueError("Daily Sharpe overflow")

    unrealized_pnl = samples[-1]["unrealized_pnl"] if samples else None
    return {
        "starting_cash": starting_cash,
        "ending_equity": ending_equity,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "trade_count": trade_count,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trade_count if trade_count else None,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "profit_factor": profit_factor,
        "no_loss": no_loss,
        "expectancy": expectancy,
        "time_weighted_exposure_pct": time_weighted_exposure_pct,
        "time_in_market_pct": time_in_market_pct,
        "max_losing_streak": max_losing_streak,
        "daily_returns": daily_returns,
        "sharpe_daily": sharpe_daily,
        "periods_per_year": periods_per_year,
        "fees_included": False,
        "funding_included": False,
        "return_basis": "marked_to_market_equity",
        "exposure_weighting": "left_continuous_until_next_sample",
    }
