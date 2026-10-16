"""Deterministic and model-backed advice providers for historical replay.

Advisors only produce recommendations. They do not mutate the live account or
place orders; the execution layer remains responsible for validating decisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from ..ai import ModelClient
from ..models import Decision, Snapshot
from ..risk import eligible
from .data import DEFAULT_INTERVAL_MS, Bar


def _freeze(value):
    """Deep-copy JSON-shaped context into immutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({copy.deepcopy(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _json_copy(value, name):
    try:
        return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc


def _sha256(value):
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DecisionContext:
    """One detached, immutable view of the evidence available to an advisor."""

    snapshot: Snapshot
    portfolio: dict
    history: tuple[Bar, ...]
    requested_at: float
    revision: int

    def __post_init__(self):
        if not isinstance(self.snapshot, Snapshot):
            raise TypeError("snapshot must be a Snapshot")
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.validate()
        if not isinstance(self.portfolio, Mapping):
            raise TypeError("portfolio must be a dictionary")
        portfolio = _json_copy(_thaw(self.portfolio), "portfolio")
        if isinstance(self.requested_at, bool) or not isinstance(self.requested_at, (int, float)):
            raise ValueError("requested_at must be finite and positive")
        requested_at = float(self.requested_at)
        if not math.isfinite(requested_at) or requested_at <= 0:
            raise ValueError("requested_at must be finite and positive")
        history = tuple(copy.deepcopy(tuple(self.history)))
        if any(not isinstance(bar, Bar) for bar in history):
            raise TypeError("history must contain Bar values")
        for bar in history:
            if type(bar.timestamp) is not int or bar.timestamp < 0 or bar.timestamp % DEFAULT_INTERVAL_MS:
                raise ValueError("history contains an invalid or misaligned candle timestamp")
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
                raise ValueError("history contains a nonnumeric candle value")
            if any(not math.isfinite(float(value)) for value in values):
                raise ValueError("history contains a nonfinite candle value")
            if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
                raise ValueError("history contains an invalid price or volume")
            if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
                raise ValueError("history contains an inconsistent OHLC range")
        if any(left.timestamp >= right.timestamp for left, right in zip(history, history[1:])):
            raise ValueError("history bars must be strictly chronological")
        # Bar timestamps are open times. A 1h candle is usable only after its
        # close, and no candle later than the decision snapshot may be exposed.
        latest_closed_open = min(
            snapshot.candle_id,
            math.floor(requested_at * 1000) - DEFAULT_INTERVAL_MS,
        )
        if any(bar.timestamp > latest_closed_open for bar in history):
            raise ValueError("history contains a candle that was not closed at decision time")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "portfolio", _freeze(portfolio))
        object.__setattr__(self, "history", history)
        object.__setattr__(self, "requested_at", requested_at)

    def as_dict(self):
        return {
            "snapshot": self.snapshot.as_dict(),
            "portfolio": _thaw(self.portfolio),
            "history": [asdict(bar) for bar in self.history],
            "requested_at": self.requested_at,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Advice:
    """Structured provider output with measured latency and visible failures."""

    decision: Decision | dict | None
    latency_seconds: float
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        latency = self.latency_seconds
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            raise ValueError("latency_seconds must be finite and nonnegative")
        latency = float(latency)
        if not math.isfinite(latency) or latency < 0:
            raise ValueError("latency_seconds must be finite and nonnegative")
        if not isinstance(self.error, str):
            raise ValueError("error must be a string")
        if self.decision is not None and not isinstance(self.decision, (Decision, dict)):
            raise TypeError("decision must be a Decision, dictionary, or None")
        if isinstance(self.decision, dict):
            object.__setattr__(self, "decision", Decision.parse(copy.deepcopy(self.decision)))
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dictionary")
        metadata = _json_copy(_thaw(self.metadata), "metadata")
        object.__setattr__(self, "latency_seconds", latency)
        object.__setattr__(self, "metadata", _freeze(metadata))

    def as_dict(self):
        return {
            "decision": asdict(self.decision) if isinstance(self.decision, Decision) else None,
            "latency_seconds": self.latency_seconds,
            "error": self.error,
            "metadata": _thaw(self.metadata),
        }


class Advisor:
    """Structural base class for advice providers."""

    def advise(self, context: DecisionContext) -> Advice:
        raise NotImplementedError

    def identity(self) -> dict:
        raise NotImplementedError


class RulesAdvisor(Advisor):
    """Transparent deterministic benchmark using QuantBot's execution gates."""

    def advise(self, context: DecisionContext) -> Advice:
        started = time.perf_counter()
        snapshot = context.snapshot
        price = snapshot.current_price
        ema_trend = (
            "TRENDING_BULL"
            if snapshot.ema_50 > snapshot.ema_200
            else "TRENDING_BEAR"
            if snapshot.ema_50 < snapshot.ema_200
            else "CHOPPY_RANGE"
        )
        side = context.portfolio.get("side", "FLAT")
        action, regime, scale, flags = "HOLD", ema_trend, False, {}

        long_ok = eligible("LONG", snapshot, price, "TRENDING_BULL")
        short_ok = eligible("SHORT", snapshot, price, "TRENDING_BEAR")
        flags.update(long_entry=long_ok, short_entry=short_ok)

        if side == "FLAT":
            if long_ok:
                action, regime = "BUY", "TRENDING_BULL"
            elif short_ok:
                action, regime = "SELL", "TRENDING_BEAR"
        elif side in ("LONG", "SHORT"):
            opposite = short_ok if side == "LONG" else long_ok
            if opposite:
                action = "CLOSE"
                regime = "TRENDING_BEAR" if side == "LONG" else "TRENDING_BULL"
            else:
                entry = context.portfolio.get("entry_price")
                units = context.portfolio.get("position_units")
                profitable = (
                    isinstance(entry, (int, float))
                    and not isinstance(entry, bool)
                    and isinstance(units, (int, float))
                    and not isinstance(units, bool)
                    and ((price - entry) * units > 0 if side == "LONG" else (entry - price) * units > 0)
                )
                side_eligible = long_ok if side == "LONG" else short_ok
                scale = bool(profitable and snapshot.volume_ratio > 1.25 and side_eligible)
                flags.update(position_profitable=bool(profitable), scale_eligible=side_eligible)
        else:
            raise ValueError(f"Unsupported portfolio side: {side}")

        detail = "; ".join(f"{key}={str(value).lower()}" for key, value in flags.items())
        decision = Decision(
            action=action,
            confidence=1.0,
            scale_in_signal=scale,
            market_regime=regime,
            thesis=f"Deterministic EMA/RSI/volume rules; {detail}.",
            copilot_commentary="Rules benchmark; confidence is a fixed rule marker, not a probability.",
        )
        return Advice(
            decision,
            time.perf_counter() - started,
            metadata={"provider": "rules", "rule_flags": flags, "confidence_is_probability": False},
        )

    def identity(self):
        return {"provider": "rules", "version": 1, "risk_gates": "quantbot.risk.eligible"}


class CashAdvisor(Advisor):
    """A no-risk baseline that always recommends HOLD."""

    def advise(self, context: DecisionContext) -> Advice:
        started = time.perf_counter()
        return Advice(
            Decision(
                action="HOLD",
                confidence=0.0,
                scale_in_signal=False,
                market_regime="CHOPPY_RANGE",
                thesis="Cash baseline: make no entry or scaling recommendation.",
                copilot_commentary="Deterministic no-trade benchmark.",
            ),
            time.perf_counter() - started,
            metadata={"provider": "cash", "confidence_is_probability": False},
        )

    def identity(self):
        return {"provider": "cash", "version": 1}


class NumericAdvisor(Advisor):
    """Fixed lookback momentum filter layered on the transparent rules benchmark."""

    def __init__(self, lookback=24, rules=None):
        if type(lookback) is not int or lookback < 1:
            raise ValueError("lookback must be a positive integer")
        self.lookback = lookback
        self.rules = rules or RulesAdvisor()

    def advise(self, context: DecisionContext) -> Advice:
        started = time.perf_counter()
        base = self.rules.advise(context)
        if base.error or base.decision is None:
            return Advice(
                None,
                time.perf_counter() - started,
                error=base.error or "rules benchmark returned no decision",
                metadata={
                    **_thaw(base.metadata),
                    "provider": "numeric",
                    "base_provider": self.rules.identity(),
                },
            )
        decision = base.decision
        if decision.action == "CLOSE":
            return Advice(
                decision,
                time.perf_counter() - started,
                metadata={
                    **_thaw(base.metadata),
                    "provider": "numeric",
                    "momentum_filter": "bypassed_for_close",
                },
            )

        bars = [bar for bar in context.history if bar.timestamp < context.snapshot.candle_id]
        required = self.lookback + 1
        metadata = {
            **_thaw(base.metadata),
            "provider": "numeric",
            "lookback": self.lookback,
            "momentum_fixed_not_fitted": True,
            "history_bars_available": len(bars),
        }
        if len(bars) < required:
            held = replace(
                decision,
                action="HOLD",
                scale_in_signal=False,
                thesis=f"Momentum filter requires {required} prior bars; received {len(bars)}.",
            )
            metadata.update(momentum_filter="insufficient_history", momentum=None)
            return Advice(held, time.perf_counter() - started, metadata=metadata)

        start_close, end_close = bars[-required].close, bars[-1].close
        momentum = end_close / start_close - 1.0
        metadata.update(
            momentum=momentum,
            momentum_start_timestamp=bars[-required].timestamp,
            momentum_end_timestamp=bars[-1].timestamp,
        )
        agrees = (decision.action == "BUY" and momentum > 0) or (decision.action == "SELL" and momentum < 0)
        if decision.action in ("BUY", "SELL") and not agrees:
            held = replace(
                decision,
                action="HOLD",
                scale_in_signal=False,
                thesis=f"Momentum filter blocked {decision.action}; fixed {self.lookback}-bar return={momentum:.6g}.",
            )
            metadata["momentum_filter"] = "blocked_entry"
            decision = held
        elif decision.scale_in_signal and not (
            (decision.market_regime == "TRENDING_BULL" and momentum > 0)
            or (decision.market_regime == "TRENDING_BEAR" and momentum < 0)
        ):
            decision = replace(
                decision,
                scale_in_signal=False,
                thesis=f"Momentum filter blocked scaling; fixed {self.lookback}-bar return={momentum:.6g}.",
            )
            metadata["momentum_filter"] = "blocked_scale"
        else:
            metadata["momentum_filter"] = "passed"
        return Advice(decision, time.perf_counter() - started, metadata=metadata)

    def identity(self):
        return {
            "provider": "numeric",
            "version": 1,
            "lookback": self.lookback,
            "fixed_not_fitted": True,
            "base_advisor": self.rules.identity(),
        }


class OllamaAdvisor(Advisor):
    """Adapter for the existing local Ollama model request format."""

    def __init__(self, config, model=None, client=None, model_digest=None):
        self.config = replace(config, model=model) if model is not None else config
        self.client = client or ModelClient(self.config)
        client_config = getattr(self.client, "config", self.config)
        if model is not None and client is not None and getattr(client_config, "model", model) != model:
            raise ValueError("model override must match the injected client's configured model")
        self.config = client_config
        self.model = model or getattr(client_config, "model", self.config.model)
        self.endpoint = getattr(client_config, "ollama_url", config.ollama_url)
        self.symbol = getattr(client_config, "symbol", config.symbol)
        self.timeframe = getattr(client_config, "timeframe", config.timeframe)
        self._model_digest = model_digest
        self._digest_source = "explicit" if model_digest else "unresolved"
        self._model_details = {}

    @staticmethod
    def _safe_endpoint(url):
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))

    def _template_identity(self):
        # Build through the same request factory the actual consult call uses.
        sample = Snapshot(1, 1, 1, 1, 1, 1, 50, 1, 1)
        request = self.client.build_request(sample, {})
        template = {key: value for key, value in request.items() if key != "prompt"}
        return _sha256(template), request

    def identity(self):
        template_hash, request = self._template_identity()
        return {
            "provider": "ollama",
            "model": request.get("model", self.model),
            "model_digest": self._model_digest,
            "model_digest_source": self._digest_source,
            "model_details": copy.deepcopy(self._model_details),
            "endpoint": self._safe_endpoint(self.endpoint),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "request_template_hash": template_hash,
            "options": copy.deepcopy(request.get("options", {})),
            "runtime": {"python": platform.python_version(), "requests": _package_version("requests")},
        }

    def _installed_model_identity(self):
        """Read the selected installed tag without pulling or changing identity."""
        session = getattr(self.client, "session", None)
        if session is None:
            raise RuntimeError("Ollama client has no HTTP session for /api/tags")
        parts = urlsplit(self.endpoint)
        tags_url = urlunsplit((parts.scheme, parts.netloc, "/api/tags", "", ""))
        timeout = min(getattr(self.config, "model_timeout", 15), 15)
        response = session.get(tags_url, timeout=(5, timeout))
        response.raise_for_status()
        data = response.json()
        models = data.get("models") if isinstance(data, dict) else None
        for item in models or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            digest = item.get("digest")
            if name == self.model and isinstance(digest, str) and digest:
                safe = {}
                for key in ("size", "modified_at"):
                    value = item.get(key)
                    if isinstance(value, (str, int)) and not isinstance(value, bool):
                        safe[key] = value
                details = item.get("details")
                if isinstance(details, dict):
                    safe_details = {}
                    for key in ("format", "family", "families", "parameter_size", "quantization_level"):
                        value = details.get(key)
                        if isinstance(value, (str, int, float, bool)) or (
                            isinstance(value, list) and all(isinstance(part, str) for part in value)
                        ):
                            safe_details[key] = value
                    if safe_details:
                        safe["details"] = safe_details
                return digest, safe
        raise LookupError(f"Model {self.model!r} is not installed in the configured Ollama server")

    def resolve_model_digest(self):
        """Read installed model tags; this never pulls or downloads a model."""
        self._model_digest, self._model_details = self._installed_model_identity()
        self._digest_source = "ollama_api_tags"
        return self._model_digest

    def advise(self, context: DecisionContext) -> Advice:
        started = time.perf_counter()
        request = None
        metadata = {
            "provider": "ollama",
            "model": self.model,
            "model_digest": self._model_digest,
            "model_details": copy.deepcopy(self._model_details),
        }
        try:
            if self._digest_source == "ollama_api_tags":
                installed_digest, _installed_details = self._installed_model_identity()
                if installed_digest != self._model_digest:
                    raise RuntimeError(
                        "Installed Ollama model digest changed after resolution; re-resolve identity before advising"
                    )
            request = self.client.build_request(context.snapshot, _thaw(context.portfolio))
            metadata.update(
                prompt_hash=_sha256(request.get("prompt")),
                request_hash=_sha256(request),
                request_template_hash=_sha256(
                    {key: value for key, value in request.items() if key != "prompt"}
                ),
                options=copy.deepcopy(request.get("options", {})),
                model=request.get("model", self.model),
                model_digest_source=self._digest_source,
            )
            decision = self.client.consult(context.snapshot, _thaw(context.portfolio))
            if isinstance(decision, dict):
                decision = Decision.parse(decision)
            if not isinstance(decision, Decision):
                raise TypeError("Ollama client returned no valid Decision")
            return Advice(decision, time.perf_counter() - started, metadata=metadata)
        except Exception as exc:
            metadata["request_available"] = request is not None
            return Advice(
                None, time.perf_counter() - started, error=f"{type(exc).__name__}: {exc}", metadata=metadata
            )


class ChronosAdvisor(Advisor):
    """Chronos-2 forecast used only as a directional filter on RulesAdvisor entries."""

    def __init__(
        self,
        pipeline=None,
        *,
        model_id="amazon/chronos-2",
        revision=None,
        device="cpu",
        prediction_length=1,
        rules=None,
        allow_download=False,
    ):
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id must be a nonempty string")
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ValueError("revision must be None or a nonempty pinned revision")
        if device not in ("cpu", "cuda"):
            raise ValueError("device must be 'cpu' or explicit 'cuda'")
        if type(prediction_length) is not int or prediction_length < 1:
            raise ValueError("prediction_length must be a positive integer")
        if type(allow_download) is not bool:
            raise ValueError("allow_download must be a boolean")
        self.pipeline = pipeline
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self.prediction_length = prediction_length
        self.rules = rules or RulesAdvisor()
        self.allow_download = allow_download

    def _load_pipeline(self):
        if self.pipeline is None:
            if not self._revision_is_pinned():
                raise ValueError("Loading Chronos requires a pinned 40-character model revision")
            try:
                from chronos import Chronos2Pipeline
            except ImportError as exc:
                raise RuntimeError(
                    "Chronos-2 is optional; install chronos-forecasting to select this advisor"
                ) from exc
            kwargs = {
                "device_map": self.device,
                "revision": self.revision,
                "local_files_only": not self.allow_download,
            }
            self.pipeline = Chronos2Pipeline.from_pretrained(self.model_id, **kwargs)
        return self.pipeline

    def _revision_is_pinned(self):
        return isinstance(self.revision, str) and re.fullmatch(r"[0-9a-fA-F]{40}", self.revision) is not None

    def identity(self):
        return {
            "provider": "chronos",
            "model_id": self.model_id,
            "revision": self.revision,
            "revision_pinned": self._revision_is_pinned(),
            "device": self.device,
            "prediction_length": self.prediction_length,
            "max_context_bars": 2048,
            "quantile_levels": [0.1, 0.5, 0.9],
            "allow_download": self.allow_download,
            "runtime": {
                "chronos_forecasting": _package_version("chronos-forecasting"),
                "torch": _package_version("torch"),
                "transformers": _package_version("transformers"),
                "pandas": _package_version("pandas"),
            },
            "base_advisor": self.rules.identity(),
        }

    def advise(self, context: DecisionContext) -> Advice:
        started = time.perf_counter()
        base = self.rules.advise(context)
        metadata = {
            **_thaw(base.metadata),
            "provider": "chronos",
            "model_id": self.model_id,
            "revision": self.revision,
            "revision_pinned": self._revision_is_pinned(),
            "device": self.device,
            "prediction_length": self.prediction_length,
        }
        if base.error or base.decision is None:
            return Advice(
                None,
                time.perf_counter() - started,
                error=base.error or "rules returned no decision",
                metadata=metadata,
            )
        if base.decision.action not in ("BUY", "SELL") and not base.decision.scale_in_signal:
            metadata["forecast_status"] = "not_needed_for_non_entry"
            return Advice(base.decision, time.perf_counter() - started, metadata=metadata)
        bars = [bar for bar in context.history if bar.timestamp <= context.snapshot.candle_id][-2048:]
        if len(bars) < 2:
            return Advice(
                None,
                time.perf_counter() - started,
                error="Chronos requires at least two causal bars",
                metadata=metadata,
            )
        try:
            import pandas as pd

            frame = pd.DataFrame(
                {
                    "item_id": ["market"] * len(bars),
                    "timestamp": pd.to_datetime([bar.timestamp for bar in bars], unit="ms", utc=True),
                    "target": [bar.close for bar in bars],
                }
            )
            pipeline = self._load_pipeline()
            forecast = pipeline.predict_df(
                frame,
                prediction_length=self.prediction_length,
                quantile_levels=[0.1, 0.5, 0.9],
                id_column="item_id",
                timestamp_column="timestamp",
                target="target",
                freq="h",
            )
            if forecast is None or len(forecast) < 1:
                raise ValueError("Chronos returned an empty forecast")
            final = forecast.iloc[-1]
            median = float(final["0.5"] if "0.5" in final else final["predictions"])
            low = float(final["0.1"])
            high = float(final["0.9"])
            if (
                not all(math.isfinite(value) for value in (median, low, high))
                or low > median
                or median > high
            ):
                raise ValueError("Chronos returned invalid forecast quantiles")
            last_close = bars[-1].close
            direction = "UP" if median > last_close else "DOWN" if median < last_close else "FLAT"
            decision = base.decision
            held_side = context.portfolio.get("side")
            expected_side = (
                decision.action
                if decision.action in ("BUY", "SELL")
                else ("BUY" if held_side == "LONG" else "SELL" if held_side == "SHORT" else None)
            )
            agrees = (expected_side == "BUY" and median > last_close) or (
                expected_side == "SELL" and median < last_close
            )
            if decision.action in ("BUY", "SELL") and not agrees:
                decision = replace(
                    decision,
                    action="HOLD",
                    scale_in_signal=False,
                    thesis=f"Chronos directional filter blocked entry; median={median:.8g}, last={last_close:.8g}.",
                )
            elif decision.scale_in_signal and not agrees:
                decision = replace(
                    decision,
                    scale_in_signal=False,
                    thesis=f"Chronos directional filter blocked scaling; median={median:.8g}, last={last_close:.8g}.",
                )
            metadata.update(
                forecast_status="ok",
                last_close=last_close,
                forecast_median=median,
                forecast_q10=low,
                forecast_q90=high,
                forecast_interval_width=high - low,
                forecast_direction=direction,
                forecast_agrees_with_rules=agrees,
            )
            return Advice(decision, time.perf_counter() - started, metadata=metadata)
        except Exception as exc:
            return Advice(
                None,
                time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                metadata={**metadata, "forecast_status": "error"},
            )


def _package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None
