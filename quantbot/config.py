from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    # 1h remains available for reproducing historical runs. The UI/CLI use 1m/5m.
    decision_interval_seconds: int = 3600
    candle_limit: int = 1000
    minimum_candles: int = 600
    poll_seconds: float = 5.0
    model_timeout: float = 120.0
    model_keep_alive_seconds: float = 300.0
    model_warmup_timeout: float = 300.0
    max_decision_age: float = 120.0
    max_quote_age: float = 15.0
    risk_per_tranche: float = 0.01
    max_position_risk: float = 0.03
    max_notional_equity: float = 1.0
    reward_risk: float = 2.0
    max_tranches: int = 3
    scale_enabled: bool = True
    confidence_gate_enabled: bool = True
    min_trend_strength: float = 0.0
    unit_step: float = 0.0001
    starting_cash: float = 10000.0
    daily_loss_limit: float | None = None
    max_account_drawdown: float | None = None
    cooldown_seconds: float = 0.0
    equity_sample_seconds: float = 60.0
    ollama_url: str = "http://localhost:11434/api/generate"
    model: str = "qwen2.5:14b"
    data_dir: Path = Path(__file__).resolve().parent.parent

    def __post_init__(self):
        import math

        if type(self.decision_interval_seconds) is not int or self.decision_interval_seconds not in (
            60,
            300,
            3600,
        ):
            raise ValueError("decision_interval_seconds must be 60, 300 or 3600")
        for name in ("scale_enabled", "confidence_gate_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if (
            isinstance(self.min_trend_strength, bool)
            or not isinstance(self.min_trend_strength, (int, float))
            or not math.isfinite(self.min_trend_strength)
            or self.min_trend_strength < 0
        ):
            raise ValueError("min_trend_strength must be finite and nonnegative")
        for name in (
            "poll_seconds",
            "model_timeout",
            "model_keep_alive_seconds",
            "model_warmup_timeout",
            "max_decision_age",
            "max_quote_age",
            "risk_per_tranche",
            "max_position_risk",
            "max_notional_equity",
            "reward_risk",
            "unit_step",
            "starting_cash",
            "equity_sample_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        for name in ("daily_loss_limit", "max_account_drawdown"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value < 1
            ):
                raise ValueError(f"{name} must be None or a finite fraction strictly between 0 and 1")
        if (
            isinstance(self.cooldown_seconds, bool)
            or not isinstance(self.cooldown_seconds, (int, float))
            or not math.isfinite(self.cooldown_seconds)
            or self.cooldown_seconds < 0
        ):
            raise ValueError("cooldown_seconds must be finite and nonnegative")
        if self.risk_per_tranche > self.max_position_risk or self.max_position_risk > 1:
            raise ValueError("Invalid position risk limits")
        if not 200 <= self.minimum_candles <= self.candle_limit <= 1000:
            raise ValueError("Invalid candle warmup")
        if type(self.max_tranches) is not int or self.max_tranches < 1:
            raise ValueError("max_tranches must be positive")

    def decision_key(self, requested_at, candle_id):
        if self.decision_interval_seconds == 3600:
            return candle_id  # Legacy once-per-closed-candle replay.
        return int(requested_at // self.decision_interval_seconds) * self.decision_interval_seconds

    def decision_budget(self, requested_at):
        return min(self.max_decision_age, self.decision_interval_seconds)
