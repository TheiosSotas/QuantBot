"""Simulated fills only. Protection never calls or waits for an AI model."""

import copy
import logging
import threading
from dataclasses import asdict

from .audit import decision_event
from .models import Decision, finite_number
from .protections import entry_block_reason, register_close, update_protections
from .risk import eligible, size_addition
from .state import thought, timestamp


class PaperEngine:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.state = store.load()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def health(self, **values):
        with self.lock:
            self.state["system"].update(values)

    def _commit(self, candidate, events):
        candidate["system"]["storage_error"] = None
        try:
            self.store.save(candidate, events)
        except Exception as exc:
            self.health(
                status="STORAGE_ERROR", storage_error=str(exc), error="State commit failed; entries blocked"
            )
            raise
        candidate["system"]["storage_error"] = None
        self.state = candidate
        if any(kind == "CLOSE" for kind, _ in events):
            try:
                self.store.export_csv()
                self.state["system"].pop("csv_warning", None)
            except Exception as exc:
                # The authoritative commit already succeeded. Never repeat a fill for a CSV retry.
                logging.warning("CSV export failed; SQLite state is intact: %s", exc)
                self.state["system"]["csv_warning"] = str(exc)

    def _equity_event(self, state, quote, now, events, *, force=False):
        last = state["system"].get("last_equity_sample_at")
        if not force and last is not None and now - last < self.config.equity_sample_seconds:
            return
        p, protection = state["portfolio"], state["protections"]
        notional = p["position_units"] * quote.price
        exposure = notional / p["total_equity"] if p["total_equity"] > 0 else 0.0
        finite_number(exposure, "exposure")
        events.append(
            (
                "EQUITY",
                {
                    "observed_at": now,
                    "quote_at": quote.observed_at,
                    "price": quote.price,
                    "cash": p["cash_usdt"],
                    "unrealized_pnl": p["unrealized_pnl"],
                    "equity": p["total_equity"],
                    "peak_equity": protection["peak_equity"],
                    "drawdown": protection["current_drawdown"],
                    "exposure": exposure,
                    "side": p["side"],
                    "notional": notional,
                },
            )
        )
        state["system"]["last_equity_sample_at"] = now

    def _update_account(self, state, now):
        # A backward OS clock blocks new risk but must never suppress a stop.
        previous = state["protections"]["last_update_at"]
        protection_time = max(now, previous) if previous is not None else now
        update_protections(state, self.config, protection_time)
        state["system"]["clock_rollback"] = protection_time > now
        return protection_time

    @staticmethod
    def _protection_events(previous, candidate, now, events):
        keys = ("session_date", "daily_loss_triggered_at", "max_drawdown_triggered_at", "cooldown_until")
        current = candidate["protections"]
        if any(previous.get(key) != current.get(key) for key in keys):
            events.append(("PROTECTION", {"observed_at": now, "protections": copy.deepcopy(current)}))

    def _record_outcome(
        self, outcome, reason_code, context, *, candidate=None, events=None, status="REJECTED", detail=""
    ):
        with self.lock:
            candidate = copy.deepcopy(self.state) if candidate is None else candidate
            events = [] if events is None else events
            audit = decision_event(
                self.config, outcome=outcome, reason_code=reason_code, status=status, detail=detail, **context
            )
            events.append(("DECISION_AUDIT", audit))
            candidate["system"]["last_decision_audit"] = {
                key: audit[key] for key in ("observed_at", "action", "status", "reason_code", "outcome")
            }
            self._commit(candidate, events)
            return outcome

    def audit_rejection(
        self,
        reason_code,
        *,
        now,
        requested_at=None,
        snapshot=None,
        quote=None,
        revision=None,
        latest_candle=None,
        detail="",
    ):
        return self._record_outcome(
            reason_code,
            reason_code,
            dict(
                now=now,
                requested_at=requested_at,
                snapshot=snapshot,
                quote=quote,
                revision=revision,
                latest_candle=latest_candle,
            ),
            detail=detail,
        )

    @staticmethod
    def _mark(state, price):
        p = state["portfolio"]
        sign = 1 if p["side"] == "LONG" else -1
        p["unrealized_pnl"] = (
            (price - p["entry_price"]) * p["position_units"] * sign if p["side"] != "FLAT" else 0.0
        )
        p["total_equity"] = p["cash_usdt"] + p["unrealized_pnl"]
        finite_number(p["unrealized_pnl"], "unrealized PnL")
        finite_number(p["total_equity"], "equity")

    @staticmethod
    def _close(state, price, reason, events):
        p = state["portfolio"]
        sign = 1 if p["side"] == "LONG" else -1
        pnl = (price - p["entry_price"]) * p["position_units"] * sign
        trade = {
            "time": timestamp(),
            "side": p["side"],
            "type": reason,
            "entry": p["entry_price"],
            "exit": price,
            "units": p["position_units"],
            "pnl": pnl,
            "return_pct": (price / p["entry_price"] - 1) * 100 * sign,
            "result": "WIN" if pnl > 1e-8 else "LOSS" if pnl < -1e-8 else "BREAKEVEN",
        }
        p["cash_usdt"] += pnl
        p.update(
            side="FLAT",
            position_units=0.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            tranches=0,
            breakeven_active=False,
            unrealized_pnl=0.0,
            total_equity=p["cash_usdt"],
        )
        state["position_revision"] += 1
        state["trades"].insert(0, trade)
        del state["trades"][200:]
        events.append(("CLOSE", trade))
        thought(state, f"Closed {trade['side']} via {reason}; gross PnL {pnl:+.2f} USDT", "TRADE_EXIT")
        return trade

    def on_quote(self, quote, now):
        quote.validate(now, self.config.max_quote_age)
        with self.lock:
            candidate = copy.deepcopy(self.state)
            self._mark(candidate, quote.price)
            protection_time = self._update_account(candidate, now)
            candidate["market"]["price"] = quote.price
            candidate["system"].update(
                last_quote_at=quote.observed_at, last_protection_at=now, last_update=timestamp()
            )
            p = candidate["portfolio"]
            events = []
            closed = None
            if p["side"] != "FLAT":
                sign = 1 if p["side"] == "LONG" else -1
                # Evaluate existing exits before moving any thresholds or adding exposure.
                if (quote.price - p["stop_loss"]) * sign <= 0:
                    reason = "BREAKEVEN_EXIT" if abs(quote.price - p["entry_price"]) < 1e-8 else "STOP_LOSS"
                    closed = self._close(candidate, quote.price, reason, events)
                elif (quote.price - p["take_profit"]) * sign >= 0:
                    closed = self._close(candidate, quote.price, "TAKE_PROFIT", events)
                else:
                    threshold = (p["entry_price"] + p["take_profit"]) / 2
                    if (quote.price - threshold) * sign >= 0:
                        old_stop = p["stop_loss"]
                        new_stop = (
                            max(old_stop, p["entry_price"]) if sign == 1 else min(old_stop, p["entry_price"])
                        )
                        if new_stop != old_stop:
                            p["stop_loss"] = new_stop
                            events.append(("STOP_UPDATE", {"time": timestamp(), "stop": new_stop}))
                            thought(
                                candidate,
                                "Stop moved to current entry; fills and losses are not guaranteed.",
                                "GUARDRAIL",
                            )
                    p["breakeven_active"] = (p["stop_loss"] - p["entry_price"]) * sign >= -1e-8
            if closed:
                register_close(candidate, self.config, protection_time)
            self._update_account(candidate, now)
            candidate["system"]["entry_block_reason"] = entry_block_reason(candidate, self.config, now)
            self._protection_events(self.state["protections"], candidate, now, events)
            self._equity_event(candidate, quote, now, events, force=bool(events))
            # One small transaction per valid quote preserves peak/day baselines
            # across crashes; full history rows are sampled separately.
            self._commit(candidate, events)
            return closed

    def apply_decision(self, raw_decision, snapshot, quote, *, now, requested_at, revision, latest_candle):
        context = dict(
            now=now,
            requested_at=requested_at,
            snapshot=snapshot,
            quote=quote,
            revision=revision,
            latest_candle=latest_candle,
            decision=raw_decision,
        )
        try:
            quote.validate(now, self.config.max_quote_age)
        except ValueError as exc:
            self._record_outcome("INVALID_QUOTE", "STALE_PRICE", context, detail=exc)
            raise
        # A model failure/rejection must not prevent an already-breached exit.
        closed = self.on_quote(quote, now)
        try:
            decision = Decision.parse(
                asdict(raw_decision) if isinstance(raw_decision, Decision) else raw_decision
            )
        except ValueError as exc:
            self._record_outcome("INVALID_MODEL_OUTPUT", "INVALID_MODEL_OUTPUT", context, detail=exc)
            raise
        context["decision"] = decision
        try:
            snapshot.validate()
            finite_number(requested_at, "decision request timestamp", positive=True)
        except ValueError as exc:
            self._record_outcome("INVALID_MARKET_DATA", "INVALID_MARKET_DATA", context, detail=exc)
            raise
        if not 0 <= now - requested_at <= self.config.max_decision_age:
            return self._record_outcome("STALE_DECISION", "AI_DECISION_EXPIRED", context)
        if snapshot.candle_id != latest_candle:
            return self._record_outcome("STALE_CANDLE", "STALE_CANDLE", context)
        if closed:
            return self._record_outcome("PROTECTIVE_EXIT", "PROTECTIVE_EXIT", context)
        with self.lock:
            if self.state["system"].get("storage_error"):
                return self._record_outcome("STORAGE_BLOCKED", "STORAGE_BLOCKED", context)
            if revision != self.state["position_revision"]:
                return self._record_outcome("POSITION_CHANGED", "POSITION_CHANGED", context)
            last = self.state["last_decision_candle"]
            if last is not None and snapshot.candle_id <= last:
                return self._record_outcome("ALREADY_EVALUATED", "DUPLICATE_CANDLE", context)
            candidate = copy.deepcopy(self.state)
            candidate["last_decision_candle"] = snapshot.candle_id
            candidate["model"].update(
                regime=decision.market_regime,
                action=decision.action,
                confidence=decision.confidence,
                thesis=decision.thesis,
            )
            candidate["system"]["last_model_at"] = now
            if decision.copilot_commentary or decision.thesis:
                thought(candidate, decision.copilot_commentary or decision.thesis)
            p = candidate["portfolio"]
            price = quote.price
            events = [
                (
                    "DECISION",
                    {
                        "time": timestamp(),
                        "model": self.config.model,
                        "snapshot": snapshot.as_dict(),
                        "decision": asdict(decision),
                        "execution_price": price,
                        "requested_at": requested_at,
                        "position_revision": revision,
                    },
                )
            ]
            outcome = "HOLD"
            reason = "NO_ACTION"
            status = "NO_ACTION"
            opposite = "SELL" if p["side"] == "LONG" else "BUY"
            if p["side"] != "FLAT" and decision.action in ("CLOSE", opposite) and decision.confidence >= 0.75:
                self._close(candidate, price, "MODEL_REVERSAL", events)
                register_close(candidate, self.config, candidate["protections"]["last_update_at"])
                outcome = "CLOSED"
                reason, status = "MODEL_CLOSE", "ACCEPTED"
            elif p["side"] == "FLAT" and decision.action in ("BUY", "SELL") and decision.confidence >= 0.75:
                side = "LONG" if decision.action == "BUY" else "SHORT"
                block = entry_block_reason(candidate, self.config, now)
                reason, status = "STRATEGY_FILTER", "REJECTED"
                if block:
                    outcome, reason = block, block
                elif eligible(side, snapshot, price, decision.market_regime):
                    sign = 1 if side == "LONG" else -1
                    stop = price - sign * 1.5 * snapshot.atr_14
                    target = price + sign * 1.5 * snapshot.atr_14 * self.config.reward_risk
                    units = size_addition(self.config, p, price, stop)
                    reason = "RISK_BUDGET" if units <= 0 else "INVALID_TARGET"
                    if units > 0 and target > 0:
                        p.update(
                            side=side,
                            position_units=units,
                            entry_price=price,
                            stop_loss=stop,
                            take_profit=target,
                            tranches=1,
                            breakeven_active=False,
                        )
                        candidate["position_revision"] += 1
                        events.append(
                            (
                                "ENTRY",
                                {
                                    "time": timestamp(),
                                    "side": side,
                                    "units": units,
                                    "price": price,
                                    "stop": stop,
                                    "target": target,
                                    "candle_id": snapshot.candle_id,
                                },
                            )
                        )
                        thought(candidate, f"Paper {side}: {units} BTC at {price:.2f}", f"{side}_ENTRY")
                        outcome = "OPENED"
                        reason, status = "ENTRY", "ACCEPTED"
            elif p["side"] != "FLAT":
                sign = 1 if p["side"] == "LONG" else -1
                same_action = "BUY" if sign == 1 else "SELL"
                can_scale = (
                    decision.scale_in_signal
                    and decision.action in (same_action, "HOLD")
                    and decision.confidence >= 0.8
                    and p["tranches"] < self.config.max_tranches
                    and (price - p["entry_price"]) * sign > 0
                    and snapshot.volume_ratio > 1.25
                    and eligible(p["side"], snapshot, price, decision.market_regime)
                )
                block = entry_block_reason(candidate, self.config, now)
                if decision.scale_in_signal:
                    reason, status = "SCALE_FILTER", "REJECTED"
                    if p["tranches"] >= self.config.max_tranches:
                        reason = "MAX_TRANCHES"
                if decision.scale_in_signal and block:
                    outcome, reason, status = block, block, "REJECTED"
                elif can_scale:
                    # Use the current stop, including any breakeven update made on this quote.
                    stop = (
                        max(p["stop_loss"], price - 1.2 * snapshot.atr_14)
                        if sign == 1
                        else min(p["stop_loss"], price + 1.2 * snapshot.atr_14)
                    )
                    extra = size_addition(self.config, p, price, stop)
                    reason = "RISK_BUDGET" if extra <= 0 else "INVALID_TARGET"
                    if extra > 0:
                        total = p["position_units"] + extra
                        blended = (p["entry_price"] * p["position_units"] + price * extra) / total
                        target = blended + sign * abs(blended - stop) * self.config.reward_risk
                        # Do not create an immediately hit target or an invalid stop.
                        if target > 0 and (target - price) * sign > 0 and (price - stop) * sign > 0:
                            p.update(
                                position_units=total,
                                entry_price=blended,
                                stop_loss=stop,
                                take_profit=target,
                                tranches=p["tranches"] + 1,
                                breakeven_active=(stop - blended) * sign >= -1e-8,
                            )
                            candidate["position_revision"] += 1
                            events.append(
                                (
                                    "SCALE",
                                    {
                                        "time": timestamp(),
                                        "side": p["side"],
                                        "units": extra,
                                        "price": price,
                                        "stop": stop,
                                        "target": target,
                                        "candle_id": snapshot.candle_id,
                                    },
                                )
                            )
                            thought(
                                candidate, f"Added {extra} BTC; blended entry {blended:.2f}", "PYRAMID_SCALE"
                            )
                            outcome = "SCALED"
                            reason, status = "SCALE", "ACCEPTED"
                elif decision.action in ("CLOSE", opposite):
                    reason, status = "LOW_CONFIDENCE", "REJECTED"
            elif decision.action in ("BUY", "SELL"):
                reason, status = "LOW_CONFIDENCE", "REJECTED"
            elif decision.action == "CLOSE":
                reason, status = "NO_POSITION", "REJECTED"
            self._mark(candidate, price)
            self._update_account(candidate, now)
            candidate["system"]["entry_block_reason"] = entry_block_reason(candidate, self.config, now)
            self._protection_events(self.state["protections"], candidate, now, events)
            self._equity_event(candidate, quote, now, events, force=outcome in ("OPENED", "SCALED", "CLOSED"))
            return self._record_outcome(
                outcome, reason, context, candidate=candidate, events=events, status=status
            )

    def add_lesson(self, lesson):
        with self.lock:
            candidate = copy.deepcopy(self.state)
            candidate["lessons"].insert(0, lesson)
            del candidate["lessons"][25:]
            thought(
                candidate, "Post-mortem saved as a research suggestion; trading rules unchanged.", "LEARNING"
            )
            self._commit(candidate, [("LESSON", lesson)])
