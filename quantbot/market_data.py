import math
import time

import pandas as pd

from .indicators import calculate_indicators
from .models import Quote, Snapshot


class MarketData:
    def __init__(self, config, exchange=None, clock=time.time):
        self.config, self.clock = config, clock
        if exchange is None:
            import ccxt

            exchange = ccxt.binance(
                {"enableRateLimit": True, "timeout": 10000, "options": {"defaultType": "future"}}
            )
        self.exchange = exchange
        self.interval_ms = int(exchange.parse_timeframe(config.timeframe) * 1000)
        self.cached = None

    def fetch_quote(self):
        requested_at = self.clock()
        ticker = self.exchange.fetch_ticker(self.config.symbol)
        now = self.clock()
        stamp = ticker.get("timestamp")
        if stamp is not None and (
            isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
        ):
            raise ValueError("Invalid exchange quote timestamp")
        observed = min(stamp / 1000, requested_at) if stamp is not None else requested_at
        # Reject materially future exchange timestamps instead of normalizing them away.
        if stamp is not None and stamp / 1000 > now + 2:
            raise ValueError("Future exchange quote timestamp")
        quote = Quote(ticker.get("last"), observed)
        quote.validate(now, self.config.max_quote_age)
        return quote

    def fetch_snapshot(self, quote):
        now = self.clock()
        # Give the exchange two seconds to publish the fully closed bar.
        expected = ((int(now * 1000) - 2000) // self.interval_ms - 1) * self.interval_ms
        if self.cached and self.cached.candle_id == expected:
            return self.cached
        rows = self.exchange.fetch_ohlcv(
            self.config.symbol, timeframe=self.config.timeframe, limit=self.config.candle_limit
        )
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if frame.empty or frame.isna().any().any():
            raise ValueError("Missing OHLCV data")
        for column in frame:
            if (
                not frame[column]
                .map(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
                .all()
            ):
                raise ValueError("Nonfinite OHLCV data")
        if not frame["timestamp"].is_monotonic_increasing or frame["timestamp"].duplicated().any():
            raise ValueError("Unordered or duplicate candles")
        if not (frame["timestamp"] % self.interval_ms).eq(0).all():
            raise ValueError("Misaligned candle timestamps")
        frame = frame[frame.timestamp <= expected].copy()
        if len(frame) < self.config.minimum_candles or frame.iloc[-1].timestamp != expected:
            raise ValueError("Insufficient or stale closed candles")
        if not frame.timestamp.diff().dropna().eq(self.interval_ms).all():
            raise ValueError("Missing candle intervals")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume < 0).any():
            raise ValueError("Invalid candle prices or volume")
        if (
            (frame.high < frame[["open", "close", "low"]].max(axis=1))
            | (frame.low > frame[["open", "close", "high"]].min(axis=1))
        ).any():
            raise ValueError("Inconsistent OHLC range")
        row = calculate_indicators(frame).iloc[-1]
        snapshot = Snapshot(
            int(row.timestamp),
            quote.observed_at,
            quote.price,
            float(row.ema_20),
            float(row.ema_50),
            float(row.ema_200),
            float(row.rsi_14),
            float(row.atr_14),
            float(row.volume_ratio),
        )
        snapshot.validate()
        self.cached = snapshot
        return snapshot
