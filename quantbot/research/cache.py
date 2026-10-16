"""Immutable, checksummed JSON caching for offline advisor replays."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import tempfile
from pathlib import Path

from ..models import Decision
from .advisors import Advice, DecisionContext

CACHE_SCHEMA_VERSION = 1
_IMPLEMENTATION_FILES = (
    "ai.py",
    "models.py",
    "risk.py",
    "research/advisors.py",
    "research/cache.py",
    "research/data.py",
)


class CacheMissError(LookupError):
    """Raised when replay-only mode has no entry for the requested context."""


class CacheCorruptionError(ValueError):
    """Raised when a cache record fails key, schema, or checksum validation."""


def _canonical(value):
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("cache identity and records must contain finite JSON values") from exc


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _implementation_fingerprint(advisor):
    """Hash behavior-defining source, independent of checkout and line endings."""
    package = Path(__file__).resolve().parents[1]
    hasher = hashlib.sha256()
    for relative in _IMPLEMENTATION_FILES:
        path = package / relative
        content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        hasher.update(relative.replace("\\", "/").encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    advisor_class = type(advisor)
    try:
        advisor_source = inspect.getsource(advisor_class)
    except (OSError, TypeError):
        advisor_source = f"{advisor_class.__module__}.{advisor_class.__qualname__}"
    advisor_source = advisor_source.replace("\r\n", "\n").replace("\r", "\n")
    hasher.update(f"{advisor_class.__module__}.{advisor_class.__qualname__}".encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(advisor_source.encode("utf-8"))
    return hasher.hexdigest()


def _cache_identity(identity):
    """Keep evidence provenance out of keys when it does not change behavior.

    Ollama's digest identifies the installed weights. Whether that digest came
    from `/api/tags` at request time or was supplied explicitly for offline
    replay, and the optional tag metadata returned alongside it, do not change
    the model or its request. Normalizing those fields lets replay-only use the
    original cache entry while retaining their provenance in run reports.
    """
    if isinstance(identity, dict):
        if identity.get("provider") == "ollama":
            return {
                key: _cache_identity(value)
                for key, value in identity.items()
                if key not in ("model_digest_source", "model_details")
            }
        if identity.get("provider") == "chronos":
            return {key: _cache_identity(value) for key, value in identity.items() if key != "allow_download"}
        return {key: _cache_identity(value) for key, value in identity.items()}
    if isinstance(identity, list):
        return [_cache_identity(value) for value in identity]
    return identity


class CachedAdvisor:
    """Cache one advisor response per complete context and provider identity.

    Records are written to a temporary file and hard-linked into place, so a
    reader sees either no record or one complete immutable record. Existing
    records are never replaced. ``implementation_hash`` is normally computed
    from the behavior-defining source modules; tests may inject a valid SHA-256
    value to verify invalidation when an implementation changes.
    """

    def __init__(
        self,
        advisor,
        cache_dir,
        *,
        namespace=None,
        dataset_hash=None,
        cache_mode="readwrite",
        implementation_hash=None,
    ):
        if cache_mode not in ("readwrite", "replay-only"):
            raise ValueError("cache_mode must be 'readwrite' or 'replay-only'")
        if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
            raise ValueError("namespace must be None or a nonempty string")
        if dataset_hash is not None and (not isinstance(dataset_hash, str) or not dataset_hash.strip()):
            raise ValueError("dataset_hash must be None or a nonempty string")
        if implementation_hash is None:
            implementation_hash = _implementation_fingerprint(advisor)
        if (
            not isinstance(implementation_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", implementation_hash) is None
        ):
            raise ValueError("implementation_hash must be a lowercase SHA-256 hex digest")
        self.advisor = advisor
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.dataset_hash = dataset_hash
        self.cache_mode = cache_mode
        self.implementation_hash = implementation_hash

    def identity(self):
        return {
            "provider": "cached-advisor",
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "implementation_fingerprint": self.implementation_hash,
            "namespace": self.namespace,
            "dataset_hash": self.dataset_hash,
            "advisor": self.advisor.identity(),
        }

    def request_fingerprint(self, context: DecisionContext):
        if not isinstance(context, DecisionContext):
            raise TypeError("context must be a DecisionContext")
        key_input = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "implementation_fingerprint": self.implementation_hash,
            "advisor_identity": _cache_identity(self.advisor.identity()),
            "namespace": self.namespace,
            "dataset_hash": self.dataset_hash,
            "context": context.as_dict(),
        }
        return _digest(key_input)

    def advise(self, context: DecisionContext) -> Advice:
        key = self.request_fingerprint(context)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            return self._read(path, key)
        if self.cache_mode == "replay-only":
            raise CacheMissError(
                f"No cached advice for context {key}; replay-only mode did not query the advisor"
            )

        advice = self.advisor.advise(context)
        if not isinstance(advice, Advice):
            raise TypeError("advisor.advise(context) must return an Advice")
        stored = Advice(
            advice.decision,
            advice.latency_seconds,
            error=advice.error,
            metadata={**dict(advice.metadata), "cache_hit": False, "cache_key": key},
        )
        record = self._record(key, stored)
        try:
            self._write_once(path, record)
        except FileExistsError:
            # A concurrent worker won the immutable first-write race. Return
            # that canonical result so all callers observe the same replay.
            return self._read(path, key)
        return stored

    def _record(self, key, advice):
        payload = advice.as_dict()
        body = {"schema_version": CACHE_SCHEMA_VERSION, "key": key, "advice": payload}
        return {**body, "content_sha256": _digest(body)}

    def _write_once(self, path, record):
        encoded = _canonical(record)
        descriptor, temporary = tempfile.mkstemp(prefix=".advisor-cache-", suffix=".tmp", dir=self.cache_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link creation is atomic and fails if the immutable target exists.
            os.link(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _read(self, path, expected_key):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError(f"Cannot read advisor cache record {path.name}: {exc}") from exc
        if not isinstance(record, dict) or set(record) != {
            "schema_version",
            "key",
            "advice",
            "content_sha256",
        }:
            raise CacheCorruptionError(f"Invalid advisor cache envelope: {path.name}")
        body = {key: record[key] for key in ("schema_version", "key", "advice")}
        if record["schema_version"] != CACHE_SCHEMA_VERSION or record["key"] != expected_key:
            raise CacheCorruptionError(f"Advisor cache key or schema mismatch: {path.name}")
        try:
            checksum_matches = record["content_sha256"] == _digest(body)
        except ValueError as exc:
            raise CacheCorruptionError(f"Invalid non-JSON advisor cache content: {path.name}") from exc
        if path.stem != expected_key or not checksum_matches:
            raise CacheCorruptionError(f"Advisor cache checksum mismatch: {path.name}")
        payload = record["advice"]
        if not isinstance(payload, dict) or set(payload) != {
            "decision",
            "latency_seconds",
            "error",
            "metadata",
        }:
            raise CacheCorruptionError(f"Invalid cached advice payload: {path.name}")
        try:
            decision = payload["decision"]
            if decision is not None:
                decision = Decision.parse(decision)
            metadata = payload["metadata"]
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            metadata = {**metadata, "cache_hit": True, "cache_key": expected_key}
            return Advice(
                decision,
                payload["latency_seconds"],
                error=payload["error"],
                metadata=metadata,
            )
        except (TypeError, ValueError) as exc:
            raise CacheCorruptionError(f"Invalid cached advice content: {path.name}: {exc}") from exc
