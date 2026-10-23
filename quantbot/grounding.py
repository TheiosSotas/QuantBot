"""Deterministic observations and reason validation; never invent a model trade."""

from dataclasses import replace

from .risk import eligible

STRUCTURE_REGIMES = {"BULLISH": "TRENDING_BULL", "BEARISH": "TRENDING_BEAR", "MIXED": "CHOPPY_RANGE"}


def entry_facts(snapshot, config):
    snapshot.validate()
    price = snapshot.current_price
    bullish = price > snapshot.ema_50 > snapshot.ema_200
    bearish = price < snapshot.ema_50 < snapshot.ema_200
    long_ok = eligible("LONG", snapshot, price, "TRENDING_BULL", config)
    short_ok = eligible("SHORT", snapshot, price, "TRENDING_BEAR", config)
    structure = "BULLISH" if bullish else "BEARISH" if bearish else "MIXED"
    return {
        "price_above_ema50": price > snapshot.ema_50,
        "price_below_ema50": price < snapshot.ema_50,
        "ema50_above_ema200": snapshot.ema_50 > snapshot.ema_200,
        "ema50_below_ema200": snapshot.ema_50 < snapshot.ema_200,
        "long_rsi_pass": snapshot.rsi_14 < 68,
        "short_rsi_pass": snapshot.rsi_14 > 32,
        "ema_structure": structure,
        "verified_regime": STRUCTURE_REGIMES[structure],
        "numeric_long_setup": long_ok,
        "numeric_short_setup": short_ok,
        "entry_candidate": "LONG" if long_ok else "SHORT" if short_ok else "NONE",
        "volume_gate_for_initial_entry": False,
        "volume_gate_for_scaling_pass": snapshot.volume_ratio > 1.25,
        "entry_confidence_minimum": 0.75 if config.confidence_gate_enabled else None,
        "scale_confidence_minimum": 0.8 if config.confidence_gate_enabled else None,
        "range_evidence": "MIXED_EMA_ALIGNMENT" if not bullish and not bearish else "NONE",
        "range_evidence_scope": "EMA alignment only; no historical range detection",
    }


def proposal_context(snapshot, portfolio, config):
    facts = entry_facts(snapshot, config)
    side = portfolio.get("side", "FLAT")
    if side not in ("FLAT", "LONG", "SHORT"):
        raise ValueError("Invalid portfolio side")
    facts["position_side"] = side
    facts["initial_entry_actions"] = (
        ["HOLD"] + (["BUY"] if facts["numeric_long_setup"] else [])
        + (["SELL"] if facts["numeric_short_setup"] else [])
        if side == "FLAT" else []
    )
    same_action = "BUY" if side == "LONG" else "SELL"
    opposite_action = "SELL" if side == "LONG" else "BUY"
    sign = 1 if side == "LONG" else -1
    profitable = side != "FLAT" and (
        snapshot.current_price - portfolio.get("entry_price", snapshot.current_price)
    ) * sign > 0
    facts["position_profitable"] = profitable
    facts["tranches"] = portfolio.get("tranches", 0)
    facts["max_tranches"] = config.max_tranches
    facts["scaling_enabled"] = config.scale_enabled
    facts["scaling_filters_pass"] = bool(
        side != "FLAT" and config.scale_enabled and profitable
        and snapshot.volume_ratio > 1.25
        and facts["tranches"] < config.max_tranches
        and facts["entry_candidate"] == side
    )
    facts["allowed_actions"] = facts["initial_entry_actions"] if side == "FLAT" else (
        ["HOLD", "CLOSE", opposite_action]
        + ([same_action] if facts["scaling_filters_pass"] else [])
    )
    facts["allowed_reason_codes"] = (
        (["ENTRY_SIGNAL", "MODEL_ABSTAIN"] if facts["entry_candidate"] != "NONE" else ["NO_ENTRY_SETUP"])
        if side == "FLAT" else ["POSITION_HOLD", "EXIT_SIGNAL"]
        + (["SCALE_SIGNAL"] if facts["scaling_filters_pass"] else [])
    )
    if config.confidence_gate_enabled:
        facts["allowed_reason_codes"].append("LOW_CONFIDENCE")
    facts["execution_rechecks_risk_and_limits"] = True
    return facts


def ground_decision(decision, snapshot, portfolio, config):
    """Validate the proposal against supplied facts and render verified UI prose.

    HOLD remains a model choice. A qualified setup is not forced into a trade.
    CHOPPY_RANGE is restricted to the explicitly disclosed mixed-EMA proxy;
    aligned setups can still be declined with MODEL_ABSTAIN.
    """
    facts = proposal_context(snapshot, portfolio, config)
    side, action = facts["position_side"], decision.action
    candidate = facts["entry_candidate"]
    structure = facts["ema_structure"]
    opposite = "SELL" if side == "LONG" else "BUY"
    if side != "FLAT" and action in ("CLOSE", opposite):
        # Preserve a risk-reducing proposal. A wrong narrative/regime or a
        # contradictory scale flag must not turn an existing close into HOLD.
        decision = replace(
            decision, reason_code="EXIT_SIGNAL", scale_in_signal=False,
            market_regime=facts["verified_regime"],
        )
    if decision.market_regime != facts["verified_regime"]:
        raise ValueError(
            f"market_regime contradicts verified EMA structure {structure}; "
            f"use {facts['verified_regime']}. "
            "HOLD with MODEL_ABSTAIN is available without inventing a range."
        )
    if side == "FLAT" and action not in facts["initial_entry_actions"]:
        raise ValueError(f"Action {action} contradicts numeric setup {candidate} for a FLAT account")
    if decision.scale_in_signal:
        sign = 1 if side == "LONG" else -1
        same = "BUY" if side == "LONG" else "SELL"
        if not (
            side != "FLAT" and config.scale_enabled and action in (same, "HOLD")
            and (snapshot.current_price - portfolio.get("entry_price", snapshot.current_price)) * sign > 0
            and snapshot.volume_ratio > 1.25
            and portfolio.get("tranches", 0) < config.max_tranches
            and eligible(side, snapshot, snapshot.current_price, decision.market_regime, config)
        ):
            raise ValueError("scale_in_signal contradicts the verified position/scaling rules")

    reason = decision.reason_code
    # Backward-compatible records may lack a reason. Derive only an action
    # description, never a made-up market cause or a replacement trade.
    if reason == "LEGACY_UNSPECIFIED":
        if decision.scale_in_signal:
            reason = "SCALE_SIGNAL"
        elif side == "FLAT":
            reason = "ENTRY_SIGNAL" if action != "HOLD" else (
                "MODEL_ABSTAIN" if candidate != "NONE" else "NO_ENTRY_SETUP"
            )
        else:
            opposite = "SELL" if side == "LONG" else "BUY"
            reason = "EXIT_SIGNAL" if action in ("CLOSE", opposite) else "POSITION_HOLD"

    valid_reasons = {
        "ENTRY_SIGNAL": side == "FLAT" and action in ("BUY", "SELL"),
        "NO_ENTRY_SETUP": side == "FLAT" and action == "HOLD" and candidate == "NONE",
        "MODEL_ABSTAIN": side == "FLAT" and action == "HOLD" and candidate != "NONE",
        "LOW_CONFIDENCE": action == "HOLD" and config.confidence_gate_enabled and decision.confidence < 0.75,
        "POSITION_HOLD": side != "FLAT" and action in (
            "HOLD", "BUY" if side == "LONG" else "SELL"
        ) and not decision.scale_in_signal,
        "SCALE_SIGNAL": decision.scale_in_signal,
        "EXIT_SIGNAL": side != "FLAT" and action in ("CLOSE", opposite) and not decision.scale_in_signal,
    }
    if not valid_reasons.get(reason):
        raise ValueError(f"reason_code {reason} contradicts action {action}, position {side}, setup {candidate}")

    price_comparison = ">" if facts["price_above_ema50"] else "<" if facts["price_below_ema50"] else "="
    ema_comparison = ">" if facts["ema50_above_ema200"] else "<" if facts["ema50_below_ema200"] else "="
    thesis = (
        f"Price {price_comparison} EMA50; EMA50 {ema_comparison} EMA200; "
        f"RSI={snapshot.rsi_14:.2f}; numeric entry={candidate}."
    )
    commentary = {
        "ENTRY_SIGNAL": f"Model proposes {action}; execution still checks fresh price, risk and account limits.",
        "NO_ENTRY_SETUP": "No initial numeric setup passes the EMA, RSI and configured trend-strength rules.",
        "MODEL_ABSTAIN": f"Model abstained despite a numeric {candidate} setup; this is not a volume rejection.",
        "LOW_CONFIDENCE": "Model confidence is below the configured entry/exit confidence gate.",
        "POSITION_HOLD": f"Model proposes holding the existing {side} position; protection remains independent.",
        "SCALE_SIGNAL": "Model proposes a profitable-position addition; execution rechecks all scaling limits.",
        "EXIT_SIGNAL": "Model proposes closing the held position; execution applies its normal exit rules.",
    }[reason]
    return replace(decision, reason_code=reason, thesis=thesis, copilot_commentary=commentary), facts
