"""Simulated fills only. Protection never calls or waits for an AI model."""

import copy
import logging
import threading
from dataclasses import asdict

from .models import Decision, finite_number
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
            if events or self.state["system"].get("storage_error"):
                self._commit(candidate, events)
            else:
                self.state = candidate  # Tick-level marking needs no disk transaction.
            return closed

    def apply_decision(self, raw_decision, snapshot, quote, *, now, requested_at, revision, latest_candle):
        decision = Decision.parse(
            asdict(raw_decision) if isinstance(raw_decision, Decision) else raw_decision
        )
        snapshot.validate()
        quote.validate(now, self.config.max_quote_age)
        if not 0 <= now - requested_at <= self.config.max_decision_age:
            return "STALE_DECISION"
        if snapshot.candle_id != latest_candle:
            return "STALE_CANDLE"
        # Also protect direct callers: a delayed decision cannot skip a newly breached exit.
        if self.on_quote(quote, now):
            return "PROTECTIVE_EXIT"
        with self.lock:
            if self.state["system"].get("storage_error"):
                return "STORAGE_BLOCKED"
            if revision != self.state["position_revision"]:
                return "POSITION_CHANGED"
            last = self.state["last_decision_candle"]
            if last is not None and snapshot.candle_id <= last:
                return "ALREADY_EVALUATED"
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
            opposite = "SELL" if p["side"] == "LONG" else "BUY"
            if p["side"] != "FLAT" and decision.action in ("CLOSE", opposite) and decision.confidence >= 0.75:
                self._close(candidate, price, "MODEL_REVERSAL", events)
                outcome = "CLOSED"
            elif p["side"] == "FLAT" and decision.action in ("BUY", "SELL") and decision.confidence >= 0.75:
                side = "LONG" if decision.action == "BUY" else "SHORT"
                if eligible(side, snapshot, price, decision.market_regime):
                    sign = 1 if side == "LONG" else -1
                    stop = price - sign * 1.5 * snapshot.atr_14
                    target = price + sign * 1.5 * snapshot.atr_14 * self.config.reward_risk
                    units = size_addition(self.config, p, price, stop)
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
                if can_scale:
                    # Use the current stop, including any breakeven update made on this quote.
                    stop = (
                        max(p["stop_loss"], price - 1.2 * snapshot.atr_14)
                        if sign == 1
                        else min(p["stop_loss"], price + 1.2 * snapshot.atr_14)
                    )
                    extra = size_addition(self.config, p, price, stop)
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
            self._mark(candidate, price)
            self._commit(candidate, events)
            return outcome

    def add_lesson(self, lesson):
        with self.lock:
            candidate = copy.deepcopy(self.state)
            candidate["lessons"].insert(0, lesson)
            del candidate["lessons"][25:]
            thought(
                candidate, "Post-mortem saved as a research suggestion; trading rules unchanged.", "LEARNING"
            )
            self._commit(candidate, [("LESSON", lesson)])
