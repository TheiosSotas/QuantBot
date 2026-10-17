"""Schema repair is bounded, preserves context and never fabricates a trade."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from conftest import decision, snapshot

from quantbot.ai import Job, ModelClient, ModelDecisionError, ModelWorker
from quantbot.models import Decision


class Session:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"response": output})


def test_photo_failure_is_repaired_without_coercing_the_original_values(config):
    invalid = decision(confidence=85, scale="false")
    valid = decision(confidence=0.85, scale=False)
    with pytest.raises(ValueError) as strict:
        Decision.parse(invalid)
    assert "confidence" in str(strict.value) and "scale_in_signal" in str(strict.value)
    session = Session([json.dumps(invalid), json.dumps(valid)])
    client = ModelClient(config, session)
    portfolio = {"side": "FLAT", "cash_usdt": 10000}
    actual, trace = client.consult_with_trace(snapshot(), portfolio)
    assert actual.confidence == 0.85 and actual.scale_in_signal is False
    assert len(session.requests) == 2
    original = json.loads(session.requests[0]["json"]["prompt"])
    correction = json.loads(session.requests[1]["json"]["prompt"])
    assert correction.pop("format_correction")["validation_error"] == str(strict.value)
    assert correction == original
    assert portfolio == {"side": "FLAT", "cash_usdt": 10000}
    assert trace["recovered"] is True
    assert [row["validation"] for row in trace["attempts"]] == ["invalid", "valid"]
    assert json.loads(trace["attempts"][0]["response_text"])["confidence"] == 85
    assert trace["attempts"][1]["repair_request"] == session.requests[1]["json"]
    assert session.requests[1]["timeout"][1] <= session.requests[0]["timeout"][1]


@pytest.mark.parametrize("raw", ["not JSON", "[]", json.dumps(decision(confidence="75%", scale=1))])
def test_permanently_invalid_model_has_only_two_attempts_and_no_fallback(config, raw):
    session = Session([raw, raw])
    with pytest.raises(ModelDecisionError) as error:
        ModelClient(config, session).consult(snapshot(), {})
    assert len(session.requests) == 2
    assert error.value.diagnostics["recovered"] is False
    assert len(error.value.diagnostics["attempts"]) == 2


def test_first_valid_output_is_not_retried(config):
    session = Session([json.dumps(decision())])
    actual, trace = ModelClient(config, session).consult_with_trace(snapshot(), {})
    assert actual.action == "BUY"
    assert len(session.requests) == 1 and trace["recovered"] is False
    request = session.requests[0]["json"]
    assert json.loads(request["prompt"])["output_schema"] == request["format"]
    assert request["options"]["temperature"] == 0


def test_retry_does_not_receive_a_new_full_timeout(config):
    config = replace(config, model_timeout=10)
    session = Session(["invalid"])
    # Deadline=10, first response consumes 11 seconds: no second request.
    with patch("quantbot.ai.time.perf_counter", side_effect=[0, 0, 0, 0, 11, 11]):
        with pytest.raises(ModelDecisionError):
            ModelClient(config, session).consult(snapshot(), {})
    assert len(session.requests) == 1


def test_unavailable_ollama_does_not_trigger_schema_retry(config):
    session = Session([requests.ConnectionError("Ollama unavailable")])
    with pytest.raises(requests.ConnectionError) as error:
        ModelClient(config, session).consult(snapshot(), {})
    assert len(session.requests) == 1
    assert error.value.diagnostics["attempts"][0]["validation"] == "transport_error"


def test_model_identity_is_checked_again_before_correction(config):
    session = Session(["invalid"])
    calls = []

    def verify():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("Model digest changed")

    with pytest.raises(RuntimeError, match="digest changed") as error:
        ModelClient(config, session).consult_with_trace(snapshot(), {}, before_attempt=verify)
    assert len(session.requests) == 1
    assert error.value.diagnostics["attempts"][0]["validation"] == "invalid"


def test_paper_worker_returns_repaired_decision_without_crashing(config):
    session = Session([json.dumps(decision(confidence=90, scale="false")), json.dumps(decision())])
    worker = ModelWorker(ModelClient(config, session))
    worker.start()
    try:
        data = snapshot()
        assert worker.submit(Job("DECISION", data, {"side": "FLAT"}, data.observed_at, 0))
        result = worker.results.get(timeout=3)
        assert not result.error
        assert result.value.confidence == 0.9 and result.value.scale_in_signal is False
        assert len(session.requests) == 2
    finally:
        worker.close()
        worker.thread.join(timeout=2)
