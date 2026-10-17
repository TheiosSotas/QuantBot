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


@pytest.mark.parametrize("raw", ["invalid", json.dumps(decision())])
def test_retry_does_not_receive_a_new_full_timeout(config, raw):
    config = replace(config, model_timeout=10)
    session = Session([raw])
    clock = [0]
    original_post = session.post

    def late_post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        clock[0] = 11
        return response

    session.post = late_post
    # A read timeout is not a total-duration limit: reject even a returned response.
    with patch("quantbot.ai.time.perf_counter", side_effect=lambda: clock[0]):
        with pytest.raises(requests.Timeout, match="shared decision time budget"):
            ModelClient(config, session).consult(snapshot(), {})
    assert len(session.requests) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "[" * 1500 + "]" * 1500,
        json.dumps(decision()).replace('"confidence": 0.9', '"confidence": NaN'),
        json.dumps(decision()).replace('"action": "BUY"', '"action": "SELL", "action": "BUY"'),
        json.dumps(decision()).replace('"confidence": 0.9', '"confidence": ' + "9" * 400),
        json.dumps({**decision(), "thesis": "a" * 4001}),
    ],
)
def test_malformed_or_ambiguous_json_receives_one_correction(config, raw):
    session = Session([raw, json.dumps(decision())])
    result, trace = ModelClient(config, session).consult_with_trace(snapshot(), {})
    assert result.action == "BUY" and trace["recovered"] is True
    assert [row["validation"] for row in trace["attempts"]] == ["invalid", "valid"]
    assert len(session.requests) == 2


@pytest.mark.parametrize("fields", [{"done": False}, {"done_reason": "length"}, {"error": "failed"}])
def test_incomplete_generation_is_rejected_even_when_inner_json_is_valid(config, fields):
    responses = iter([{"response": json.dumps(decision()), **fields}, {"response": json.dumps(decision())}])
    session = SimpleNamespace(
        post=lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: next(responses)
        )
    )
    result, trace = ModelClient(config, session).consult_with_trace(snapshot(), {})
    assert result.action == "BUY" and trace["recovered"] is True
    assert trace["attempts"][0]["validation"] == "invalid"


def test_slow_body_is_stopped_by_elapsed_budget_and_response_is_closed(config):
    clock, closed, reads = [0], [], []

    def trickle(chunk_size):
        assert chunk_size == 1
        for part in b'{"response": "':
            clock[0] += 4
            reads.append(part)
            yield bytes([part])

    response = SimpleNamespace(
        raise_for_status=lambda: None, iter_content=trickle, close=lambda: closed.append(True)
    )
    session = SimpleNamespace(post=lambda *args, **kwargs: response)
    with patch("quantbot.ai.time.perf_counter", side_effect=lambda: clock[0]):
        with pytest.raises(requests.Timeout) as failure:
            ModelClient(replace(config, model_timeout=10), session).consult(snapshot(), {})
    assert len(reads) == 3 and closed == [True]
    assert len(failure.value.diagnostics["attempts"]) == 1
    assert failure.value.diagnostics["attempts"][0]["validation"] == "transport_error"


def test_oversized_body_is_bounded_and_both_attempts_are_closed(config):
    from quantbot.ai import MAX_RESPONSE_BYTES

    closed, reads = [], []

    def body(chunk_size):
        for _ in range(MAX_RESPONSE_BYTES + 20):
            reads.append(True)
            yield b" "

    session = SimpleNamespace(
        post=lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, iter_content=body, close=lambda: closed.append(True)
        )
    )
    with pytest.raises(ModelDecisionError) as error:
        ModelClient(config, session).consult(snapshot(), {})
    assert len(reads) == 2 * (MAX_RESPONSE_BYTES + 1) and len(closed) == 2
    assert all("body size" in row["error"] for row in error.value.diagnostics["attempts"])


def test_unavailable_ollama_does_not_trigger_schema_retry(config):
    session = Session([requests.ConnectionError("Ollama unavailable")])
    with pytest.raises(requests.ConnectionError) as error:
        ModelClient(config, session).consult(snapshot(), {})
    assert len(session.requests) == 1
    assert error.value.diagnostics["attempts"][0]["validation"] == "transport_error"


def test_http_error_closes_streamed_response_without_schema_retry(config):
    closed, calls = [], []

    def reject():
        raise requests.HTTPError("503 Service Unavailable")

    response = SimpleNamespace(raise_for_status=reject, close=lambda: closed.append(True))

    def post(*args, **kwargs):
        calls.append(kwargs)
        return response

    with pytest.raises(requests.HTTPError) as failure:
        ModelClient(config, SimpleNamespace(post=post)).consult(snapshot(), {})
    assert len(calls) == 1 and closed == [True]
    assert failure.value.diagnostics["attempts"][0]["validation"] == "transport_error"


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
