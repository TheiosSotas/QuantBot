"""Causal OHLC replay through PaperEngine, with explicit intrabar assumptions."""

import bisect
import copy
import math
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from statistics import mean, stdev
from urllib.parse import urlsplit, urlunsplit

from ..execution import PaperEngine
from ..models import Decision, Quote, finite_number
from .data import history_until, make_snapshot, validate_execution_data
from .memory import CompactMemoryStore, MemoryStore
from .metrics import _drawdown, _number, _utc_date, summarize


@dataclass
class ReplayClock:
    value: float = 0.0

    def __call__(self):
        return self.value


def _tick(engine, clock, price, at):
    clock.value = at
    engine.on_quote(Quote(float(price), at), at)


def _segment(engine, clock, price, at):
    """Touch existing thresholds along an assumed continuous segment.

    Gaps at bar opens are handled separately at the observed opening price.
    This is a declared fill model, not knowledge of the actual tick sequence.
    """
    old_price, old_at = engine.state["market"]["price"], clock.value
    p = engine.state["portfolio"]
    levels = []
    if p["side"] != "FLAT" and price != old_price:
        for level in (p["stop_loss"], p["take_profit"]):
            if min(old_price, price) <= level <= max(old_price, price) and level != old_price:
                levels.append(level)
        levels = sorted(set(levels), reverse=price < old_price)
    for level in levels:
        touch_at = old_at + (at - old_at) * abs((level - old_price) / (price - old_price))
        _tick(engine, clock, level, touch_at)
    _tick(engine, clock, price, at)


def _path(engine, clock, bar, interval_ms, order):
    begin, duration = bar.timestamp / 1000, interval_ms / 1000
    middle = (bar.high, bar.low) if order == "OHLC" else (bar.low, bar.high)
    for price, at in zip(
        middle + (bar.close,),
        (begin + duration / 3, begin + 2 * duration / 3, begin + duration - min(0.001, duration / 1000)),
    ):
        _segment(engine, clock, price, at)


def _resolve_bar(engine, clock, bar, interval_ms, policy):
    """Choose a declared local bound between OHLC and OLHC assumptions.

    Both branches call the real PaperEngine. Local end-equity selection is
    not a global worst/best-case proof over every possible intrabar path.
    """
    p = engine.state["portfolio"]
    bracket_ambiguous = p["side"] != "FLAT" and (
        bar.low <= p["stop_loss"] <= bar.high and bar.low <= p["take_profit"] <= bar.high
    )
    if p["side"] == "FLAT":
        _path(engine, clock, bar, interval_ms, "OHLC")
        equity = engine.state["portfolio"]["total_equity"]
        engine._commit(
            copy.deepcopy(engine.state),
            [
                (
                    "REPLAY_PATH",
                    {
                        "bar_timestamp": bar.timestamp,
                        "order": "OHLC",
                        "policy": policy,
                        "bracket_ambiguous": False,
                        "path_sensitive": False,
                        "branch_end_equities": {"OHLC": equity},
                    },
                )
            ],
        )
        return False, False
    orders = ("OHLC", "OLHC") if p["side"] != "FLAT" else ("OHLC",)
    branches = []
    for order in orders:
        branch_store = engine.store.fork()
        branch_clock = ReplayClock(clock.value)
        branch = PaperEngine(engine.config, branch_store, clock=branch_clock)
        _path(branch, branch_clock, bar, interval_ms, order)
        rows = branch_store.equity_history()
        worst_dd = max((row["drawdown"] for row in rows), default=0)
        branches.append(
            (branch.state["portfolio"]["total_equity"], worst_dd, order, branch_store, branch_clock.value)
        )
    if policy == "conservative":
        chosen = min(branches, key=lambda item: (item[0], -item[1], item[2]))
    else:
        chosen = max(branches, key=lambda item: (item[0], -item[1], item[2]))
    clock.value = chosen[4]
    sensitive = len(branches) == 2 and branches[0][3].state["portfolio"] != branches[1][3].state["portfolio"]
    events = chosen[3].events + [
        [
            "REPLAY_PATH",
            {
                "bar_timestamp": bar.timestamp,
                "order": chosen[2],
                "policy": policy,
                "bracket_ambiguous": bracket_ambiguous,
                "path_sensitive": sensitive,
                "branch_end_equities": {item[2]: item[0] for item in branches},
            },
        ]
    ]
    engine._commit(copy.deepcopy(chosen[3].state), events)
    return bracket_ambiguous, sensitive


class _CurveSampler:
    """Keep a bounded, time-ordered curve for charts, always preserving endpoints."""

    def __init__(self, limit=2000):
        self.limit = limit
        self.stride = 1
        self.seen = 0
        self.rows = []
        self.last = None

    def add(self, row):
        self.seen += 1
        self.last = row
        if len(self.rows) >= self.limit:
            self.rows = self.rows[::2]
            self.stride *= 2
        if (self.seen - 1) % self.stride == 0 and len(self.rows) < self.limit:
            self.rows.append(row)

    def result(self):
        if self.last is not None and (not self.rows or self.rows[-1] != self.last):
            if len(self.rows) >= self.limit:
                self.rows[-1] = self.last
            else:
                self.rows.append(self.last)
        return self.rows


def _summarize_compact(store, starting_cash):
    """Compute exact account metrics in one bounded-memory pass over disk events."""
    curve = _CurveSampler()
    last_by_day = {}
    daily_equity_count = 0
    previous_time = None
    previous_sample = None
    peak = starting_cash
    max_drawdown = 0.0
    exposure_duration = weighted_exposure = in_market_duration = 0.0
    ending_equity = starting_cash
    unrealized_pnl = None
    trade_count = wins = losses = losing_streak = max_losing_streak = 0
    gross_profit = gross_loss = realized_pnl = 0.0
    rejected_decisions = 0

    for kind, payload in store.iter_events():
        if kind == "EQUITY":
            observed_at = _number(payload["observed_at"], "observed_at")
            equity = _number(payload["equity"], "equity")
            exposure = _number(payload["exposure"], "exposure")
            _number(payload["peak_equity"], "peak_equity")
            stored_drawdown = _number(payload["drawdown"], "drawdown")
            if observed_at < 0 or exposure < 0 or stored_drawdown < 0:
                raise ValueError("Invalid equity sample metrics")
            if previous_time is not None and observed_at < previous_time:
                raise ValueError("Equity rows must be chronological")
            if previous_sample is not None:
                duration = observed_at - previous_time
                exposure_duration += duration
                weighted_exposure += duration * previous_sample["exposure"]
                if previous_sample["exposure"] > 0:
                    in_market_duration += duration
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, _drawdown(peak, equity))
            row = dict(payload)
            curve.add(row)
            day = _utc_date(observed_at)
            last_by_day[day] = equity
            daily_equity_count += 1
            ending_equity = equity
            unrealized_pnl = (
                _number(payload["unrealized_pnl"], "unrealized_pnl") if "unrealized_pnl" in payload else None
            )
            previous_sample = {"exposure": exposure}
            previous_time = observed_at
        elif kind == "CLOSE":
            pnl = _number(payload["pnl"], "trade pnl")
            trade_count += 1
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
                gross_profit += pnl
                losing_streak = 0
            elif pnl < 0:
                losses += 1
                gross_loss -= pnl
                losing_streak += 1
                max_losing_streak = max(max_losing_streak, losing_streak)
            else:
                losing_streak = 0
        elif kind == "DECISION_AUDIT" and payload.get("status") == "REJECTED":
            rejected_decisions += 1

    if not all(math.isfinite(value) for value in (gross_profit, gross_loss, realized_pnl)):
        raise ValueError("Trade aggregate overflow")
    total_return = ending_equity / starting_cash - 1.0
    if not math.isfinite(total_return):
        raise ValueError("Total return overflow")
    daily_returns = []
    sharpe_daily = None
    if last_by_day:
        first_day, last_day = min(last_by_day), max(last_by_day)
        previous_equity = starting_cash
        current_day = first_day
        last_observed = starting_cash
        while current_day <= last_day:
            if current_day in last_by_day:
                last_observed = last_by_day[current_day]
            daily_return = last_observed / previous_equity - 1 if previous_equity > 0 else None
            if daily_return is not None and not math.isfinite(daily_return):
                raise ValueError("Daily return overflow")
            daily_returns.append({"date": current_day.isoformat(), "return": daily_return})
            previous_equity = last_observed
            current_day += timedelta(days=1)
        values = [row["return"] for row in daily_returns if row["return"] is not None]
        if len(values) >= 2:
            deviation = stdev(values)
            if deviation > 0:
                sharpe_daily = mean(values) / deviation * math.sqrt(365)
                if not math.isfinite(sharpe_daily):
                    raise ValueError("Daily Sharpe overflow")

    time_weighted_exposure_pct = time_in_market_pct = None
    if exposure_duration > 0:
        time_weighted_exposure_pct = 100 * weighted_exposure / exposure_duration
        time_in_market_pct = 100 * in_market_duration / exposure_duration
        if not math.isfinite(time_weighted_exposure_pct) or not math.isfinite(time_in_market_pct):
            raise ValueError("Time-weighted exposure overflow")
    no_loss = gross_loss == 0
    profit_factor = None if no_loss else gross_profit / gross_loss
    expectancy = None if not trade_count else realized_pnl / trade_count
    if profit_factor is not None and not math.isfinite(profit_factor):
        raise ValueError("Profit factor overflow")
    if expectancy is not None and not math.isfinite(expectancy):
        raise ValueError("Expectancy overflow")
    metrics = {
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
        "periods_per_year": 365,
        "fees_included": False,
        "funding_included": False,
        "return_basis": "marked_to_market_equity",
        "exposure_weighting": "left_continuous_until_next_sample",
    }
    return metrics, curve.result(), rejected_decisions, daily_equity_count


def run_replay(
    dataset,
    advisor,
    *,
    config,
    execution_data=None,
    start_index=None,
    end_index=None,
    publication_delay_seconds=2.0,
    fixed_latency_seconds=None,
    ambiguity_policy="conservative",
    compact=False,
    compact_dir=None,
    progress_callback=None,
):
    """Run an isolated account; decisions fill only at observable bar opens.

    Indices address evaluation bars, end-exclusive. Earlier rows are available
    only for closed-candle warmup. Inference completion never fills backwards
    at the source candle close or at an assumed intrabar extremum.
    """
    from .advisors import Advice, DecisionContext

    start = config.minimum_candles if start_index is None else start_index
    end = len(dataset.rows) if end_index is None else end_index
    if (
        type(start) is not int
        or type(end) is not int
        or not config.minimum_candles <= start < end <= len(dataset.rows)
    ):
        raise ValueError("Replay requires warmup followed by a nonempty end-exclusive evaluation window")
    delay = finite_number(publication_delay_seconds, "publication delay")
    if delay < 0:
        raise ValueError("Publication delay must be nonnegative")
    if fixed_latency_seconds is not None and finite_number(fixed_latency_seconds, "fixed latency") < 0:
        raise ValueError("Fixed latency must be nonnegative")
    if ambiguity_policy not in ("conservative", "optimistic"):
        raise ValueError("Unknown intrabar ambiguity policy")
    if type(compact) is not bool:
        raise ValueError("compact must be a boolean")
    execution = execution_data or dataset
    if (
        dataset.interval_ms != 3_600_000
        or config.timeframe != "1h"
        or dataset.symbol != config.symbol
        or dataset.market_type != "USDT-M"
    ):
        raise ValueError("Replay requires the configured symbol's 1h USDT-M dataset")
    bars = validate_execution_data(dataset, execution, start, end)
    identity = advisor.identity()
    provider = identity
    while provider.get("provider") == "cached-advisor":
        provider = provider["advisor"]
    model_label = provider.get("model", provider.get("model_id", provider.get("provider", "research")))
    # Sample every simulated observation while leaving all position/risk rules intact.
    replay_config = replace(config, equity_sample_seconds=0.000001, model=model_label)
    if compact:
        if compact_dir is None:
            compact_dir = Path(tempfile.mkdtemp(prefix="quantbot-replay-compact-"))
        else:
            compact_dir = Path(compact_dir)
            compact_dir.mkdir(parents=True, exist_ok=False)
        store = CompactMemoryStore(replay_config, compact_dir / "events.jsonl")
    else:
        store = MemoryStore(replay_config)
    clock = ReplayClock()
    engine = PaperEngine(replay_config, store, clock=clock)
    timestamps = [bar.timestamp for bar in dataset.rows]
    last_requested, pending = None, None
    advice_rows = []
    advice_errors = 0
    latencies = []
    ambiguous_count = sensitive_count = 0
    errors = 0

    total_bars = len(bars)
    progress_stride = max(1, total_bars // 100)
    if progress_callback is not None:
        progress_callback({"phase": "replay", "completed": 0, "total": total_bars, "percent": 0.0})
    for bar_number, bar in enumerate(bars, start=1):
        now = bar.timestamp / 1000
        _tick(engine, clock, bar.open, now)
        quote = Quote(bar.open, now)
        index = bisect.bisect_right(timestamps, int((now - delay) * 1000) - dataset.interval_ms) - 1
        latest = dataset.rows[index].timestamp if index >= 0 else None
        if pending is not None and pending["ready_at"] <= now:
            context, advice = pending["context"], pending["advice"]
            if advice.error or advice.decision is None:
                engine.audit_rejection(
                    "MODEL_ERROR",
                    now=now,
                    requested_at=context.requested_at,
                    snapshot=context.snapshot,
                    quote=quote,
                    revision=context.revision,
                    latest_candle=latest,
                    detail=advice.error or "No model decision",
                )
                errors += 1
            else:
                try:
                    engine.apply_decision(
                        advice.decision,
                        context.snapshot,
                        quote,
                        now=now,
                        requested_at=context.requested_at,
                        revision=context.revision,
                        latest_candle=latest,
                    )
                except ValueError:
                    errors += 1  # Engine persists the precise invalid-input rejection.
            pending = None
        if pending is None and index >= config.minimum_candles - 1 and latest != last_requested:
            context = DecisionContext(
                snapshot=make_snapshot(dataset, index, config, now, bar.open),
                portfolio=copy.deepcopy(engine.state["portfolio"]),
                history=history_until(dataset, index, config.candle_limit),
                requested_at=now,
                revision=engine.state["position_revision"],
            )
            try:
                advice = advisor.advise(context)
            except (LookupError, ValueError):
                # Missing/corrupt replay evidence must fail explicitly, not be
                # disguised as an unsuccessful trading strategy.
                raise
            except Exception as exc:
                advice = Advice(None, 0.0, error=f"{type(exc).__name__}: {exc}")
            if not isinstance(advice, Advice):
                raise ValueError("Advisor must return an Advice object")
            if advice.decision is None and not advice.error:
                advice = Advice(
                    None,
                    advice.latency_seconds,
                    error="Advisor returned no decision",
                    metadata=advice.as_dict()["metadata"],
                )
            elif advice.decision is not None:
                try:
                    Decision.parse(asdict(advice.decision))
                except ValueError as exc:
                    advice = Advice(
                        None,
                        advice.latency_seconds,
                        error=f"INVALID_MODEL_OUTPUT: {exc}",
                        metadata=advice.as_dict()["metadata"],
                    )
            latency = finite_number(advice.latency_seconds, "measured model latency")
            if latency < 0:
                raise ValueError("Model latency must be nonnegative")
            simulated_latency = latency if fixed_latency_seconds is None else fixed_latency_seconds
            pending = {"context": context, "advice": advice, "ready_at": now + simulated_latency}
            latencies.append(latency)
            advice_errors += bool(advice.error)
            advice_row = {
                "requested_at": now,
                "ready_at": pending["ready_at"],
                "candle_id": latest,
                "position_revision": context.revision,
                "latency_seconds": latency,
                "simulated_latency_seconds": simulated_latency,
                "error": advice.error,
                "metadata": advice.as_dict()["metadata"],
            }
            if compact:
                store.append_event("ADVICE", advice_row)
            else:
                advice_rows.append(advice_row)
            last_requested = latest
            if simulated_latency == 0:
                if advice.error or advice.decision is None:
                    engine.audit_rejection(
                        "MODEL_ERROR",
                        now=now,
                        requested_at=now,
                        snapshot=context.snapshot,
                        quote=quote,
                        revision=context.revision,
                        latest_candle=latest,
                        detail=advice.error or "No model decision",
                    )
                    errors += 1
                else:
                    try:
                        engine.apply_decision(
                            advice.decision,
                            context.snapshot,
                            quote,
                            now=now,
                            requested_at=now,
                            revision=context.revision,
                            latest_candle=latest,
                        )
                    except ValueError:
                        errors += 1
                pending = None
        ambiguous, sensitive = _resolve_bar(engine, clock, bar, execution.interval_ms, ambiguity_policy)
        ambiguous_count += ambiguous
        sensitive_count += sensitive
        if progress_callback is not None and (bar_number % progress_stride == 0 or bar_number == total_bars):
            progress_callback(
                {
                    "phase": "replay",
                    "completed": bar_number,
                    "total": total_bars,
                    "percent": 100.0 * bar_number / total_bars,
                    "timestamp": bar.timestamp,
                }
            )
    if pending is not None:
        context = pending["context"]
        engine.audit_rejection(
            "SIMULATION_ENDED",
            now=clock.value,
            requested_at=context.requested_at,
            snapshot=context.snapshot,
            revision=context.revision,
            detail="No execution open after inference completion inside evaluation window",
        )
    if compact:
        store.close()
        metrics, curve, rejected_decisions, equity_count = _summarize_compact(store, config.starting_cash)
        trades, audits, events = [], [], []
    else:
        curve, trades, audits = store.equity_history(), store.trades(), store.decision_history()
        metrics = summarize(curve, trades, starting_cash=config.starting_cash)
        rejected_decisions = sum(row["status"] == "REJECTED" for row in audits)
        equity_count = len(curve)
        events = store.events
    latencies.sort()
    metrics.update(
        {
            "model_errors": advice_errors if compact else sum(bool(row["error"]) for row in advice_rows),
            "processed_decision_failures": errors,
            "advice_count": len(latencies),
            "invalid_or_expired_decisions": rejected_decisions,
            "equity_samples": equity_count,
            "latency_p50_seconds": latencies[len(latencies) // 2] if latencies else None,
            "latency_p95_seconds": latencies[min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1)]
            if latencies
            else None,
            "bracket_ambiguous_bars": ambiguous_count,
            "path_sensitive_bars": sensitive_count,
        }
    )
    public_config = {
        key: str(value) if key == "data_dir" else value for key, value in asdict(replay_config).items()
    }
    endpoint = urlsplit(public_config["ollama_url"])
    host = endpoint.hostname or ""
    if endpoint.port:
        host += f":{endpoint.port}"
    public_config["ollama_url"] = urlunsplit((endpoint.scheme, host, endpoint.path, "", ""))
    return {
        "schema_version": 1,
        "advisor": identity,
        "dataset": dataset.manifest(),
        "execution_dataset": execution.manifest(),
        "start_index": start,
        "end_index": end,
        "config": public_config,
        "assumptions": {
            "fill": "next observable execution-bar open at/after inference completion",
            "publication_delay_seconds": delay,
            "fixed_latency_seconds": fixed_latency_seconds,
            "ambiguity_policy": ambiguity_policy,
            "intrabar": "local OHLC/OLHC branch bound with threshold touches; times are assumed, not ticks",
            "global_worst_case_proven": False,
            "fees_included": False,
            "funding_included": False,
            "end_position": "left open and marked to market",
            "account_storage": "isolated memory state with disk event journal"
            if compact
            else "isolated memory",
        },
        "metrics": metrics,
        "equity": curve,
        "trades": trades,
        "decisions": audits,
        "advice": advice_rows,
        "events": events,
        "final_state": engine.snapshot(),
        "completed": True,
        "compact_artifacts": (
            {"directory": str(compact_dir), "events": str(compact_dir / "events.jsonl")} if compact else None
        ),
    }
