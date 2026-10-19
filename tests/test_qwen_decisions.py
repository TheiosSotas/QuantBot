"""Qwen token-limit regression from the 2026-10-19 PAPER screenshot."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from conftest import decision, snapshot
from test_cadence_http_integration import model_server

from quantbot.ai import (
    DECISION_MAX_TOKENS,
    QWEN_DECISION_CONTEXT,
    ModelClient,
    ModelDecisionError,
)


@pytest.mark.parametrize("model", ["qwen3.5:9b", "qwen3:8b", "local/qwen3.5:9b"])
def test_qwen_decision_disables_thinking_at_top_level_and_overrides_defaults(config, model):
    request = ModelClient(replace(config, model=model)).build_request(snapshot(), {"side": "FLAT"})
    assert request["think"] is False
    assert "think" not in request["options"]
    assert request["options"]["num_predict"] == DECISION_MAX_TOKENS
    assert request["options"]["num_ctx"] == QWEN_DECISION_CONTEXT
    assert request["format"] == json.loads(request["prompt"])["output_schema"]
    assert "under 160 characters" in request["system"]


@pytest.mark.parametrize("model", ["qwen2.5:14b", "gpt-oss:20b", "glm-5.2:cloud"])
def test_other_model_thinking_controls_are_not_guessed(config, model):
    request = ModelClient(replace(config, model=model)).build_request(snapshot(), {})
    assert "think" not in request
    assert "num_ctx" not in request["options"]
    assert "num_predict" not in request["options"]


def test_qwen_warmup_preloads_the_same_context_without_requesting_a_trade(config):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"done": True})

    client = ModelClient(replace(config, model="qwen3.5:9b"), SimpleNamespace(post=post))
    client.warmup()
    assert "prompt" not in calls[0] and "format" not in calls[0]
    assert calls[0]["options"]["num_ctx"] == client.build_request(snapshot(), {})["options"]["num_ctx"]


def test_http_old_request_truncates_but_explicit_qwen_request_finishes(config):
    def respond(request):
        if request.get("think", True) or request.get("options", {}).get("num_predict", 8) < 256:
            return 200, {
                "done": True,
                "done_reason": "length",
                "response": "",
                "thinking": "fixture reasoning, not a real model",
                "eval_count": 8,
            }
        return 200, {"done": True, "done_reason": "stop", "response": json.dumps(decision("HOLD"))}

    with model_server(respond) as (endpoint, calls):
        config = replace(config, model="qwen3.5:9b", ollama_url=endpoint, decision_interval_seconds=60)
        client = ModelClient(config)
        old = copy.deepcopy(client.build_request(snapshot(), {}))
        old.pop("think")
        old["options"].pop("num_predict")
        old["options"].pop("num_ctx")
        with requests.post(endpoint, json=old, timeout=2) as response:
            assert response.json()["done_reason"] == "length"
        actual, trace = client.consult_with_trace(snapshot(), {})
        assert actual.action == "HOLD"
        assert trace["recovered"] is False and len(trace["attempts"]) == 1
        assert len(calls) == 2


def test_qwen_correction_retains_profile_and_shared_remaining_budget(config):
    now = [0.0]
    calls = []

    def post(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            now[0] = 32
            envelope = {
                "done": True,
                "done_reason": "length",
                "response": '{"action":',
                "eval_count": 512,
                "thinking": "fixture reasoning",
            }
        else:
            now[0] = 35
            envelope = {"done": True, "done_reason": "stop", "response": json.dumps(decision("HOLD"))}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: envelope)

    config = replace(config, model="qwen3.5:9b", decision_interval_seconds=60)
    with patch("quantbot.ai.time.perf_counter", side_effect=lambda: now[0]):
        actual, trace = ModelClient(config, SimpleNamespace(post=post)).consult_with_trace(snapshot(), {})
    assert actual.action == "HOLD" and trace["recovered"] is True
    assert calls[0]["timeout"][1] == 60
    assert calls[1]["timeout"][1] == 28  # Screenshot's ~28s is remaining time, not a 28s default.
    for call in calls:
        assert call["json"]["think"] is False
        assert call["json"]["options"]["num_predict"] == 512
    evidence = trace["attempts"][0]
    assert evidence["generation"]["eval_count"] == 512
    assert evidence["generation"]["done_reason"] == "length"
    assert "truncated decision rejected" in evidence["error"]
    assert "fixture reasoning" not in json.dumps(trace)


def test_length_followed_by_read_timeout_preserves_evidence_and_does_not_return_trade(config):
    now = [0.0]
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise requests.ReadTimeout("Read timed out (fixture)")
        now[0] = 32
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "done": True,
                "done_reason": "length",
                "response": json.dumps(decision("BUY")),
                "eval_count": 512,
            },
        )

    config = replace(config, model="qwen3.5:9b", decision_interval_seconds=60)
    with patch("quantbot.ai.time.perf_counter", side_effect=lambda: now[0]):
        with pytest.raises(requests.ReadTimeout) as failure:
            ModelClient(config, SimpleNamespace(post=post)).consult(snapshot(), {})
    assert calls[1]["timeout"][1] == 28
    assert [row["validation"] for row in failure.value.diagnostics["attempts"]] == [
        "invalid",
        "transport_error",
    ]
    assert failure.value.diagnostics["recovered"] is False


def test_repeated_length_returns_actionable_error_never_accepts_inner_buy(config):
    session = SimpleNamespace(
        post=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "done": True,
                "done_reason": "length",
                "response": json.dumps(decision("BUY")),
                "eval_count": 512,
            },
        )
    )
    with pytest.raises(ModelDecisionError, match="token limit reached") as failure:
        ModelClient(replace(config, model="qwen3.5:9b"), session).consult(snapshot(), {})
    assert len(failure.value.diagnostics["attempts"]) == 2
    assert all(row["generation"]["eval_count"] == 512 for row in failure.value.diagnostics["attempts"])
