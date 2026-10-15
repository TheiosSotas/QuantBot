from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    candle_limit: int = 1000
    minimum_candles: int = 600
    poll_seconds: float = 5.0
    model_timeout: float = 120.0
    max_decision_age: float = 120.0
    max_quote_age: float = 15.0
    risk_per_tranche: float = 0.01
    max_position_risk: float = 0.03
    max_notional_equity: float = 1.0
    reward_risk: float = 2.0
    max_tranches: int = 3
    unit_step: float = 0.0001
    starting_cash: float = 10000.0
    ollama_url: str = "http://localhost:11434/api/generate"
    model: str = "qwen2.5:14b"
    data_dir: Path = Path(__file__).resolve().parent.parent

    def __post_init__(self):
        import math

        for name in (
            "poll_seconds",
            "model_timeout",
            "max_decision_age",
            "max_quote_age",
            "risk_per_tranche",
            "max_position_risk",
            "max_notional_equity",
            "reward_risk",
            "unit_step",
            "starting_cash",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.risk_per_tranche > self.max_position_risk or self.max_position_risk > 1:
            raise ValueError("Invalid position risk limits")
        if not 200 <= self.minimum_candles <= self.candle_limit <= 1000:
            raise ValueError("Invalid candle warmup")
        if self.max_tranches < 1:
            raise ValueError("max_tranches must be positive")
