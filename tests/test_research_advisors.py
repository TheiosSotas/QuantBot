import json
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import CANDLE, NOW, decision, snapshot

from quantbot.ai import ModelClient
from quantbot.models import Decision
from quantbot.research.advisors import (
    Advice,
    CashAdvisor,
    ChronosAdvisor,
    DecisionContext,
    NumericAdvisor,
    OllamaAdvisor,
    RulesAdvisor,
)
from quantbot.research.cache import CachedAdvisor
from quantbot.research.data import Bar


def context(data=None, portfolio=None, bars=None, *, requested_at=NOW, revision=3):
    data = data or snapshot()
    if bars is None:
        bars = [Bar(CANDLE, 99, 102, 98, 100, 1)]
    return DecisionContext(data, portfolio or {"side": "FLAT"}, tuple(bars), requested_at, revision)


def prior_bars(closes, *, last_timestamp=CANDLE):
    first = last_timestamp - (len(closes) - 1) * 3_600_000
    return tuple(Bar(first + i * 3_600_000, close, close, close, close, 1) for i, close in enumerate(closes))


def test_decision_context_detaches_and_deeply_freezes_inputs():
    portfolio = {"side": "LONG", "nested": {"weights": [1, 2]}}
    bars = [Bar(CANDLE, 99, 102, 98, 100, 1)]
    source_snapshot = snapshot()
    ctx = DecisionContext(source_snapshot, portfolio, bars, NOW, 3)

    portfolio["nested"]["weights"][0] = 99
    bars.append(Bar(CANDLE + 3_600_000, 100, 101, 99, 100, 1))
    assert ctx.portfolio["nested"]["weights"] == (1, 2)
    assert len(ctx.history) == 1
    with pytest.raises(TypeError):
        ctx.portfolio["side"] = "FLAT"
    with pytest.raises(TypeError):
        ctx.portfolio["nested"]["weights"] = ()


@pytest.mark.parametrize(
    "bars,requested_at",
    [
        ([Bar(CANDLE + 3_600_000, 1, 1, 1, 1, 1)], NOW),
        ([Bar(CANDLE, 1, 1, 1, 1, 1)], (CANDLE + 3_600_000 - 1) / 1000),
    ],
)
def test_context_rejects_future_or_not_yet_closed_bars(bars, requested_at):
    with pytest.raises(ValueError, match="not closed"):
        context(bars=bars, requested_at=requested_at)


def test_context_rejects_nonchronological_and_invalid_requested_time():
    with pytest.raises(ValueError, match="chronological"):
        context(bars=[Bar(CANDLE, 1, 1, 1, 1, 1), Bar(CANDLE, 1, 1, 1, 1, 1)])
    with pytest.raises(ValueError, match="requested_at"):
        context(requested_at=float("nan"))


def test_rules_advisor_uses_execution_gates_and_never_flips():
    rules = RulesAdvisor()
    assert rules.advise(context()).decision.action == "BUY"
    assert rules.advise(context(data=snapshot(80))).decision.action == "HOLD"
    assert rules.advise(context(data=snapshot(side="SHORT"))).decision.action == "SELL"

    held = context(portfolio={"side": "LONG", "entry_price": 90, "position_units": 1})
    advice = rules.advise(held)
    assert advice.decision.action == "HOLD"
    assert advice.decision.scale_in_signal is True

    reversing = context(
        data=snapshot(100, side="SHORT"),
        portfolio={"side": "LONG", "entry_price": 110, "position_units": 1},
    )
    reverse_advice = rules.advise(reversing)
    assert reverse_advice.decision.action == "CLOSE"
    assert reverse_advice.decision.scale_in_signal is False


def test_rules_scale_requires_profit_volume_and_held_direction_filter():
    rules = RulesAdvisor()
    base = context(portfolio={"side": "LONG", "entry_price": 90, "position_units": 1})
    assert rules.advise(base).decision.scale_in_signal
    low_volume = replace(base.snapshot, volume_ratio=1.25)
    assert not rules.advise(replace(base, snapshot=low_volume)).decision.scale_in_signal
    losing = context(portfolio={"side": "LONG", "entry_price": 110, "position_units": 1})
    assert not rules.advise(losing).decision.scale_in_signal


def test_cash_advisor_is_a_no_entry_no_scale_hold():
    advice = CashAdvisor().advise(context())
    assert advice.decision.action == "HOLD"
    assert advice.decision.scale_in_signal is False
    assert advice.metadata["provider"] == "cash"


def test_numeric_advisor_uses_only_prior_bars_and_blocks_opposed_momentum():
    ctx = context(bars=prior_bars([120, 110, 107, 104, 101]))
    advice = NumericAdvisor(lookback=3).advise(ctx)
    assert advice.decision.action == "HOLD"
    assert advice.metadata["momentum_filter"] == "blocked_entry"
    assert advice.metadata["momentum"] < 0
    assert advice.metadata["momentum_fixed_not_fitted"] is True


def test_numeric_advisor_requires_history_but_never_blocks_model_close():
    no_history = context(bars=[])
    advice = NumericAdvisor(lookback=4).advise(no_history)
    assert advice.decision.action == "HOLD"
    assert advice.metadata["momentum_filter"] == "insufficient_history"

    close_context = context(
        data=snapshot(side="SHORT"),
        portfolio={"side": "LONG", "entry_price": 110, "position_units": 1},
        bars=[],
    )
    close = NumericAdvisor().advise(close_context)
    assert close.decision.action == "CLOSE"
    assert close.metadata["momentum_filter"] == "bypassed_for_close"


def test_model_client_build_request_is_the_payload_consult_posts(config):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"response": json.dumps(decision())}
        )

    client = ModelClient(config, SimpleNamespace(post=post))
    expected = client.build_request(snapshot(), {"side": "FLAT"})
    client.consult(snapshot(), {"side": "FLAT"})
    assert calls == [expected]


class _FakeModelClient:
    def __init__(self, answer=None, error=None, model="local-test:1"):
        self.answer = answer or Decision.parse(decision())
        self.error = error
        self.config = SimpleNamespace(model=model, symbol="BTC/USDT:USDT", timeframe="1h")
        self.requests = []
        self.session = SimpleNamespace()

    def build_request(self, data, portfolio):
        request = {
            "model": self.config.model,
            "stream": False,
            "format": {"type": "object"},
            "system": "test-system",
            "prompt": json.dumps({"snapshot": data.as_dict(), "portfolio": portfolio}, sort_keys=True),
            "options": {"temperature": 0.1},
        }
        self.requests.append(request)
        return request

    def consult(self, data, portfolio):
        if self.error:
            raise self.error
        return self.answer


def test_ollama_adapter_hashes_actual_request_and_records_identity(config):
    client = _FakeModelClient()
    advisor = OllamaAdvisor(config, client=client, model_digest="sha256:installed")
    first = advisor.advise(context())
    second = advisor.advise(context(portfolio={"side": "FLAT", "cash_usdt": 9_000}))

    assert first.decision.action == "BUY"
    assert first.latency_seconds >= 0
    assert first.metadata["prompt_hash"] != second.metadata["prompt_hash"]
    assert first.metadata["model_digest"] == "sha256:installed"
    identity = advisor.identity()
    assert identity["model_digest"] == "sha256:installed"
    assert identity["request_template_hash"]
    assert "username" not in identity["endpoint"]
    assert identity["symbol"] == config.symbol


def test_ollama_identity_changes_with_prompt_template_symbol_and_options(config):
    first = OllamaAdvisor(config)
    changed = OllamaAdvisor(replace(config, symbol="ETH/USDT:USDT"))
    assert first.identity() != changed.identity()


def test_ollama_adapter_returns_visible_error_without_fallback(config):
    advisor = OllamaAdvisor(config, client=_FakeModelClient(error=TimeoutError("local model timeout")))
    result = advisor.advise(context())
    assert result.decision is None
    assert "local model timeout" in result.error


def test_ollama_resolves_installed_digest_without_pulling(config):
    client = _FakeModelClient()
    calls = []
    client.session.get = lambda url, **kwargs: (
        calls.append((url, kwargs))
        or SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": "local-test:1", "digest": "abc123"}]},
        )
    )
    advisor = OllamaAdvisor(config, client=client)
    assert advisor.resolve_model_digest() == "abc123"
    assert calls[0][0].endswith("/api/tags")
    assert advisor.identity()["model_digest_source"] == "ollama_api_tags"


def test_ollama_rechecks_resolved_digest_before_fresh_generation(config):
    client = _FakeModelClient()
    digests = iter(("abc123", "changed456"))
    client.session.get = lambda url, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"models": [{"name": "local-test:1", "digest": next(digests)}]},
    )
    advisor = OllamaAdvisor(config, client=client)
    assert advisor.resolve_model_digest() == "abc123"
    resolved_identity = advisor.identity()
    identity_request_count = len(client.requests)

    result = advisor.advise(context())

    assert result.decision is None
    assert "digest changed" in result.error
    assert len(client.requests) == identity_request_count
    assert advisor.identity() == resolved_identity


def test_ollama_installed_digest_cache_replays_with_explicit_offline_identity(config, tmp_path):
    tagged_client = _FakeModelClient()
    tagged_client.session.get = lambda url, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"models": [{"name": "local-test:1", "digest": "sha256:same"}]},
    )
    tagged = OllamaAdvisor(config, client=tagged_client)
    tagged.resolve_model_digest()
    context_value = context()
    writer = CachedAdvisor(tagged, tmp_path, dataset_hash="same-data")
    written = writer.advise(context_value)

    replay_client = _FakeModelClient()
    consults = []
    replay_client.consult = lambda *args: consults.append(args) or replay_client.answer
    replay = CachedAdvisor(
        OllamaAdvisor(config, client=replay_client, model_digest="sha256:same"),
        tmp_path,
        dataset_hash="same-data",
        cache_mode="replay-only",
    )
    restored = replay.advise(context_value)

    assert restored.decision == written.decision
    assert restored.latency_seconds == written.latency_seconds
    assert restored.metadata["cache_hit"] is True
    assert consults == []


def test_ollama_cache_separates_timeout_dependent_failure_from_longer_retry(config, tmp_path):
    context_value = context()
    short_client = _FakeModelClient(error=TimeoutError("model request timed out"))
    short_client.config.model_timeout = 1
    short = CachedAdvisor(
        OllamaAdvisor(config, client=short_client, model_digest="sha256:stable"),
        tmp_path,
        dataset_hash="same-data",
    )

    failed = short.advise(context_value)
    assert "timed out" in failed.error

    long_client = _FakeModelClient()
    long_client.config.model_timeout = 120.0
    long = CachedAdvisor(
        OllamaAdvisor(config, client=long_client, model_digest="sha256:stable"),
        tmp_path,
        dataset_hash="same-data",
    )
    recovered = long.advise(context_value)

    assert recovered.error == ""
    assert recovered.decision.action == "BUY"
    assert recovered.metadata["cache_hit"] is False
    assert long_client.requests


class _FakeChronos:
    def __init__(self, median):
        self.median = median
        self.frames = []
        self.options = []

    def predict_df(self, frame, **kwargs):
        self.frames.append(frame.copy())
        self.options.append(kwargs)
        return pd.DataFrame({"0.1": [self.median - 1], "0.5": [self.median], "0.9": [self.median + 1]})


def test_chronos_predict_df_is_causal_and_forecast_filters_rules_only():
    bars = prior_bars([90, 95, 100])
    forecast = _FakeChronos(99)
    advisor = ChronosAdvisor(forecast, revision="a" * 40)
    result = advisor.advise(context(bars=bars))

    assert result.decision.action == "HOLD"
    assert result.metadata["forecast_status"] == "ok"
    assert result.metadata["forecast_median"] == 99
    assert result.metadata["forecast_q10"] == 98
    assert list(forecast.frames[0].columns) == ["item_id", "timestamp", "target"]
    assert forecast.options[0]["prediction_length"] == 1
    assert forecast.options[0]["quantile_levels"] == [0.1, 0.5, 0.9]
    assert advisor.identity()["revision_pinned"] is True


def test_chronos_filters_scaling_and_marks_unpinned_revisions_untrusted():
    bars = prior_bars([90, 95, 100])
    held = context(
        portfolio={"side": "LONG", "entry_price": 90, "position_units": 1},
        bars=bars,
    )
    blocked = ChronosAdvisor(_FakeChronos(99)).advise(held)
    allowed = ChronosAdvisor(_FakeChronos(101)).advise(held)
    assert blocked.decision.action == "HOLD"
    assert blocked.decision.scale_in_signal is False
    assert allowed.decision.action == "HOLD"
    assert allowed.decision.scale_in_signal is True

    unpinned = ChronosAdvisor(_FakeChronos(101), revision="main")
    assert unpinned.identity()["revision_pinned"] is False
    assert unpinned.identity()["allow_download"] is False


def test_chronos_default_model_load_requires_a_pinned_revision_and_is_visible():
    result = ChronosAdvisor().advise(context(bars=prior_bars([90, 95])))
    assert result.decision is None
    assert "pinned 40-character model revision" in result.error


def test_advice_metadata_is_detached_and_deeply_read_only():
    source = {"flags": {"entry": True}, "values": [1, 2]}
    advice = Advice(Decision.parse(decision()), 0.01, metadata=source)
    source["flags"]["entry"] = False
    assert advice.metadata["flags"]["entry"] is True
    with pytest.raises(TypeError):
        advice.metadata["flags"]["entry"] = False


def test_chronos_errors_are_visible_and_not_replaced_with_rules_output():
    advisor = ChronosAdvisor(_FakeChronos(100))
    bad = context(bars=[Bar(CANDLE, 100, 100, 100, 100, 1)])
    result = advisor.advise(bad)
    assert result.decision is None
    assert "at least two causal bars" in result.error

    failing = ChronosAdvisor(
        SimpleNamespace(
            predict_df=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forecast failed"))
        )
    )
    bars = prior_bars([90, 95])
    failure = failing.advise(context(bars=bars))
    assert failure.decision is None
    assert "forecast failed" in failure.error
