import math
from dataclasses import asdict, dataclass


def finite_number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"Invalid {name}")
    return float(value)


@dataclass(frozen=True)
class Quote:
    price: float
    observed_at: float

    def validate(self, now, max_age):
        finite_number(self.price, "price", positive=True)
        finite_number(self.observed_at, "quote timestamp", positive=True)
        if not 0 <= now - self.observed_at <= max_age:
            raise ValueError("Stale or future quote")


@dataclass(frozen=True)
class Snapshot:
    candle_id: int
    observed_at: float
    current_price: float
    ema_20: float
    ema_50: float
    ema_200: float
    rsi_14: float
    atr_14: float
    volume_ratio: float

    def validate(self):
        if isinstance(self.candle_id, bool) or not isinstance(self.candle_id, int) or self.candle_id <= 0:
            raise ValueError("Invalid closed-candle timestamp")
        for name in ("observed_at", "current_price", "ema_20", "ema_50", "ema_200", "atr_14"):
            finite_number(getattr(self, name), name, positive=True)
        finite_number(self.rsi_14, "RSI")
        finite_number(self.volume_ratio, "volume")
        if not 0 <= self.rsi_14 <= 100 or self.volume_ratio < 0:
            raise ValueError("Invalid indicator range")

    def as_dict(self):
        return asdict(self)


REGIMES = ("TRENDING_BULL", "TRENDING_BEAR", "CHOPPY_RANGE", "VOLATILITY_EXPANSION")
ACTIONS = ("BUY", "SELL", "HOLD", "CLOSE")


@dataclass(frozen=True)
class Decision:
    action: str
    confidence: float
    scale_in_signal: bool
    market_regime: str
    thesis: str = ""
    copilot_commentary: str = ""

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Decision must be an object")
        if set(value) != set(DECISION_SCHEMA["properties"]):
            raise ValueError("Decision fields must match the schema")
        if value.get("action") not in ACTIONS or value.get("market_regime") not in REGIMES:
            raise ValueError("Invalid action or regime")
        errors = []
        try:
            confidence = finite_number(value.get("confidence"), "confidence")
            if not 0 <= confidence <= 1:
                errors.append(f"confidence must be between 0 and 1; got {confidence!r}")
        except ValueError as exc:
            errors.append(str(exc))
        if type(value.get("scale_in_signal")) is not bool:
            errors.append(
                "scale_in_signal must be a JSON boolean true/false; got "
                f"{repr(value.get('scale_in_signal'))[:100]} ({type(value.get('scale_in_signal')).__name__})"
            )
        if errors:
            raise ValueError("; ".join(errors))
        for key in ("thesis", "copilot_commentary"):
            if not isinstance(value.get(key, ""), str) or len(value.get(key, "")) > 4000:
                raise ValueError(f"Invalid {key}")
        return cls(
            value["action"],
            confidence,
            value["scale_in_signal"],
            value["market_regime"],
            value.get("thesis", ""),
            value.get("copilot_commentary", ""),
        )


DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "market_regime": {"type": "string", "enum": list(REGIMES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "scale_in_signal": {"type": "boolean"},
        "thesis": {"type": "string", "maxLength": 4000},
        "copilot_commentary": {"type": "string", "maxLength": 4000},
    },
    "required": ["action", "market_regime", "confidence", "scale_in_signal", "thesis", "copilot_commentary"],
}
