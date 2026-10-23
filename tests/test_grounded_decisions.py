"""Regression coverage for false EMA claims and unexplained flat-account HOLDs."""

import json
from dataclasses import replace

import pytest
from conftest import apply, decision, snapshot

from quantbot.ai import ModelClient, ModelDecisionError
from quantbot.grounding import entry_facts, ground_decision
from quantbot.models import Decision


@pytest.mark.parametrize("side,action,regime", [("LONG", "BUY", "TRENDING_BULL"), ("SHORT", "SELL", "TRENDING_BEAR")])
def test_initial_entry_uses_verified_facts_without_a_volume_gate(config, side, action, regime):
    data = snapshot(side=side, volume_ratio=0)
    raw = Decision.parse({**decision(action, regime=regime), "reason_code": "ENTRY_SIGNAL"})
    actual, facts = ground_decision(raw, data, {"side": "FLAT"}, config)
    assert facts["entry_candidate"] == side
    assert facts["volume_gate_for_initial_entry"] is False
    assert actual.action == action
    assert actual.reason_code == "ENTRY_SIGNAL"


def test_bearish_alignment_is_not_a_choppy_range_veto(config):
    raw = Decision.parse({**decision("HOLD", regime="CHOPPY_RANGE"), "reason_code": "MODEL_ABSTAIN"})
    with pytest.raises(ValueError, match="contradicts verified EMA structure BEARISH"):
        ground_decision(raw, snapshot(side="SHORT"), {"side": "FLAT"}, config)


def test_numeric_setup_is_not_forced_into_a_trade_and_abstention_is_visible(config, engine):
    data = snapshot(volume_ratio=0.1)
    raw = Decision.parse({**decision("HOLD"), "reason_code": "MODEL_ABSTAIN"})
    actual, _ = ground_decision(raw, data, {"side": "FLAT"}, config)
    assert actual.action == "HOLD"
    assert "despite a numeric LONG setup" in actual.copilot_commentary
    assert apply(engine, actual, data) == "HOLD"
    audit = engine.store.decision_history()[-1]
    assert audit["reason_code"] == "MODEL_ABSTAIN"
    assert audit["strategy_facts"]["numeric_long_setup"] is True
    assert audit["status"] == "NO_ACTION"


def test_untrusted_prose_cannot_reach_the_verified_ui_explanation(config):
    raw = Decision.parse({
        **decision("HOLD"), "reason_code": "MODEL_ABSTAIN",
        "thesis": "Price is below EMA50 and volume forbids entry.",
        "copilot_commentary": "The account is flat so it cannot open a position.",
    })
    actual, _ = ground_decision(raw, snapshot(), {"side": "FLAT"}, config)
    assert "Price > EMA50" in actual.thesis
    assert "EMA50 > EMA200" in actual.thesis
    assert actual.copilot_commentary != raw.copilot_commentary
    assert "not a volume rejection" in actual.copilot_commentary


def test_reason_cannot_falsely_claim_no_setup(config):
    raw = Decision.parse({**decision("HOLD"), "reason_code": "NO_ENTRY_SETUP"})
    with pytest.raises(ValueError, match="reason_code NO_ENTRY_SETUP contradicts"):
        ground_decision(raw, snapshot(), {"side": "FLAT"}, config)


def test_rsi_block_is_distinct_from_mixed_ema_structure(config):
    data = snapshot(rsi_14=70)
    facts = entry_facts(data, config)
    assert facts["ema_structure"] == "BULLISH"
    assert facts["entry_candidate"] == "NONE"
    actual, _ = ground_decision(
        Decision.parse({**decision("HOLD"), "reason_code": "NO_ENTRY_SETUP"}), data, {}, config
    )
    assert actual.action == "HOLD"


def test_request_explains_flat_actions_and_removes_confidence_example_anchoring(config):
    request = ModelClient(config).build_request(snapshot(side="SHORT"), {"side": "FLAT"})
    payload = json.loads(request["prompt"])
    assert payload["verified_strategy_facts"]["initial_entry_actions"] == ["HOLD", "SELL"]
    assert "SELL opens the first SHORT" in request["system"]
    assert "use 0.75" not in request["system"]
    assert "reason_code" in request["format"]["required"]


def test_old_six_field_records_remain_readable():
    actual = Decision.parse(decision())
    assert actual.reason_code == "LEGACY_UNSPECIFIED"


def test_scaling_cannot_be_claimed_for_a_flat_account(config):
    raw = Decision.parse({**decision(scale=True), "reason_code": "SCALE_SIGNAL"})
    with pytest.raises(ValueError, match="scaling rules"):
        ground_decision(raw, snapshot(), {"side": "FLAT"}, config)


def test_one_shared_budget_repair_can_fix_semantic_regime_error(config):
    from test_ai_repair import Session

    bad = {**decision("HOLD", regime="CHOPPY_RANGE"), "reason_code": "MODEL_ABSTAIN"}
    good = {**decision("SELL", regime="TRENDING_BEAR"), "reason_code": "ENTRY_SIGNAL"}
    session = Session([json.dumps(bad), json.dumps(good)])
    result, trace = ModelClient(config, session).consult_with_trace(snapshot(side="SHORT"), {"side": "FLAT"})
    assert result.action == "SELL"
    assert trace["recovered"] is True
    assert "verified EMA structure" in trace["attempts"][0]["error"]
    assert trace["attempts"][1]["grounding"]["facts"]["numeric_short_setup"] is True
    assert json.loads(trace["attempts"][1]["response_text"]) == good


def test_repeated_semantic_failure_never_invents_an_entry(config):
    from test_ai_repair import Session

    bad = {**decision("HOLD", regime="CHOPPY_RANGE"), "reason_code": "MODEL_ABSTAIN"}
    with pytest.raises(ModelDecisionError):
        ModelClient(config, Session([json.dumps(bad), json.dumps(bad)])).consult(
            snapshot(side="SHORT"), {"side": "FLAT"}
        )


def test_disabled_confidence_gate_does_not_claim_a_low_confidence_rejection(config):
    raw = Decision.parse({**decision("HOLD", confidence=0.1), "reason_code": "LOW_CONFIDENCE"})
    with pytest.raises(ValueError, match="reason_code LOW_CONFIDENCE contradicts"):
        ground_decision(raw, snapshot(), {}, replace(config, confidence_gate_enabled=False))


def test_wrong_model_narrative_cannot_suppress_a_close_proposal(config):
    raw = Decision.parse({
        **decision("CLOSE", scale=True, regime="CHOPPY_RANGE"), "reason_code": "SCALE_SIGNAL",
    })
    actual, _ = ground_decision(raw, snapshot(), {"side": "LONG", "entry_price": 90}, config)
    assert actual.action == "CLOSE"
    assert actual.reason_code == "EXIT_SIGNAL"
    assert actual.scale_in_signal is False
    assert actual.market_regime == "TRENDING_BULL"


def test_legacy_same_direction_without_scale_keeps_the_existing_position(config):
    actual, _ = ground_decision(Decision.parse(decision("BUY")), snapshot(), {"side": "LONG"}, config)
    assert actual.action == "BUY"
    assert actual.scale_in_signal is False
    assert actual.reason_code == "POSITION_HOLD"


@pytest.mark.parametrize("entry,tranches", [(110, 1), (90, 3)])
def test_request_cannot_generate_an_impossible_scale_signal(config, entry, tranches):
    request = ModelClient(config).build_request(
        snapshot(), {"side": "LONG", "entry_price": entry, "tranches": tranches}
    )
    facts = json.loads(request["prompt"])["verified_strategy_facts"]
    assert facts["scaling_filters_pass"] is False
    assert "SCALE_SIGNAL" not in request["format"]["properties"]["reason_code"]["enum"]
    assert request["format"]["properties"]["scale_in_signal"]["enum"] == [False]
    assert "BUY" not in facts["allowed_actions"]


def test_profitable_position_addition_remains_available_when_its_filters_pass(config):
    request = ModelClient(config).build_request(
        snapshot(), {"side": "LONG", "entry_price": 90, "tranches": 1}
    )
    facts = json.loads(request["prompt"])["verified_strategy_facts"]
    assert facts["scaling_filters_pass"] is True
    assert "SCALE_SIGNAL" in facts["allowed_reason_codes"]
    assert request["format"]["properties"]["scale_in_signal"]["enum"] == [False, True]


def test_unknown_historical_volatility_cannot_be_invented_from_one_snapshot(config):
    raw = Decision.parse({**decision("HOLD", regime="VOLATILITY_EXPANSION"), "reason_code": "MODEL_ABSTAIN"})
    with pytest.raises(ValueError, match="contradicts verified EMA structure"):
        ground_decision(raw, snapshot(), {}, config)
