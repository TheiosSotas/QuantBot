import json
from dataclasses import replace

import pytest
from conftest import CANDLE, NOW, snapshot

from quantbot.models import Decision
from quantbot.research.advisors import Advice, DecisionContext
from quantbot.research.cache import CacheCorruptionError, CachedAdvisor, CacheMissError
from quantbot.research.data import Bar


def make_context(*, data=None, portfolio=None, history=None, requested_at=NOW, revision=1):
    return DecisionContext(
        data or snapshot(),
        portfolio or {"side": "FLAT", "cash_usdt": 10_000.0},
        tuple(history if history is not None else [Bar(CANDLE, 99, 102, 98, 100, 5)]),
        requested_at,
        revision,
    )


class _Advisor:
    def __init__(self, *, version=1, error=""):
        self.version = version
        self.error = error
        self.calls = 0

    def identity(self):
        return {"provider": "fake", "version": self.version, "options": {"temperature": 0.1}}

    def advise(self, context):
        self.calls += 1
        return Advice(
            None if self.error else Decision("HOLD", 0.0, False, "CHOPPY_RANGE", "fake"),
            2.5,
            error=self.error,
            metadata={"call": self.calls, "snapshot_price": context.snapshot.current_price},
        )


def test_identical_context_uses_one_immutable_record_and_keeps_original_latency(tmp_path):
    base = _Advisor()
    cached = CachedAdvisor(base, tmp_path, namespace="benchmark", dataset_hash="dataset-1")
    ctx = make_context()

    first = cached.advise(ctx)
    files = list(tmp_path.glob("*.json"))
    before = files[0].read_bytes()
    second = cached.advise(ctx)

    assert base.calls == 1
    assert first.latency_seconds == second.latency_seconds == 2.5
    assert first.decision == second.decision
    assert first.metadata["cache_hit"] is False
    assert second.metadata["cache_hit"] is True
    assert files[0].read_bytes() == before


def test_cache_key_includes_every_context_component_and_provider_identity(tmp_path):
    base = _Advisor()
    cached = CachedAdvisor(base, tmp_path, namespace="walk-forward", dataset_hash="dataset-a")
    original = make_context()
    changed = [
        make_context(data=replace(original.snapshot, current_price=101.0)),
        make_context(portfolio={"side": "FLAT", "cash_usdt": 9_999.0}),
        make_context(history=[Bar(CANDLE, 99, 102, 98, 101, 5)]),
        make_context(requested_at=NOW + 1),
        make_context(revision=2),
    ]
    contexts = [original, *changed]
    for ctx in contexts:
        cached.advise(ctx)
    assert base.calls == len(contexts)
    assert len(list(tmp_path.glob("*.json"))) == len(contexts)

    other_provider = _Advisor(version=2)
    CachedAdvisor(other_provider, tmp_path, namespace="walk-forward", dataset_hash="dataset-a").advise(
        original
    )
    other_dataset = _Advisor()
    CachedAdvisor(other_dataset, tmp_path, namespace="walk-forward", dataset_hash="dataset-b").advise(
        original
    )
    assert other_provider.calls == other_dataset.calls == 1
    assert len(list(tmp_path.glob("*.json"))) == len(contexts) + 2


def test_replay_only_returns_cached_errors_and_never_queries_on_miss(tmp_path):
    failing = _Advisor(error="RuntimeError: local provider failed")
    writer = CachedAdvisor(failing, tmp_path, dataset_hash="d")
    ctx = make_context()
    first = writer.advise(ctx)
    replay = CachedAdvisor(_Advisor(), tmp_path, dataset_hash="d", cache_mode="replay-only")
    restored = replay.advise(ctx)
    assert first.error == restored.error == "RuntimeError: local provider failed"
    assert restored.decision is None
    assert restored.latency_seconds == 2.5
    assert restored.metadata["cache_hit"] is True

    missing_advisor = _Advisor()
    replay_missing = CachedAdvisor(missing_advisor, tmp_path, dataset_hash="d", cache_mode="replay-only")
    with pytest.raises(CacheMissError, match="did not query"):
        replay_missing.advise(make_context(revision=99))
    assert missing_advisor.calls == 0


def test_cache_detects_changed_content_checksum_and_wrong_key(tmp_path):
    cached = CachedAdvisor(_Advisor(), tmp_path)
    ctx = make_context()
    cached.advise(ctx)
    path = next(tmp_path.glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["advice"]["latency_seconds"] = 999
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="checksum"):
        CachedAdvisor(_Advisor(), tmp_path, cache_mode="replay-only").advise(ctx)


def test_cache_reports_nonfinite_json_payload_as_corruption(tmp_path):
    cached = CachedAdvisor(_Advisor(), tmp_path)
    ctx = make_context()
    cached.advise(ctx)
    path = next(tmp_path.glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["advice"]["metadata"]["tampered"] = float("nan")
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="non-JSON"):
        CachedAdvisor(_Advisor(), tmp_path, cache_mode="replay-only").advise(ctx)


def test_cache_rejects_invalid_mode_and_non_json_metadata(tmp_path):
    with pytest.raises(ValueError, match="cache_mode"):
        CachedAdvisor(_Advisor(), tmp_path, cache_mode="refresh-and-delete")

    class BadMetadata(_Advisor):
        def advise(self, context):
            return Advice(Decision("HOLD", 0, False, "CHOPPY_RANGE"), 0.1, metadata={"bad": float("nan")})

    with pytest.raises(ValueError, match="finite JSON"):
        CachedAdvisor(BadMetadata(), tmp_path).advise(make_context())


def test_request_fingerprint_is_sha256_and_changes_with_model_options(tmp_path):
    ctx = make_context()
    first = CachedAdvisor(_Advisor(version=1), tmp_path, dataset_hash="d")
    second = CachedAdvisor(_Advisor(version=1), tmp_path, dataset_hash="d2")
    key = first.request_fingerprint(ctx)
    assert len(key) == 64
    assert key != second.request_fingerprint(ctx)


def test_cache_fingerprint_changes_when_implementation_hash_changes(tmp_path):
    ctx = make_context()
    first = CachedAdvisor(_Advisor(), tmp_path, implementation_hash="1" * 64)
    changed = CachedAdvisor(_Advisor(), tmp_path, implementation_hash="2" * 64)

    assert first.identity()["implementation_fingerprint"] == "1" * 64
    assert first.request_fingerprint(ctx) != changed.request_fingerprint(ctx)
