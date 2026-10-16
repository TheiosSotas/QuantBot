"""Preserve the original SMA RSI/ATR conventions; handle flat/zero-volume series explicitly."""

import pandas as pd


def calculate_indicators(frame):
    frame = frame.copy()
    close = frame["close"]
    for span in (20, 50, 200):
        frame[f"ema_{span}"] = close.ewm(span=span, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    frame["rsi_14"] = rsi.mask((loss == 0) & (gain == 0), 50.0)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - close.shift()).abs(),
            (frame["low"] - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr_14"] = true_range.rolling(14).mean()
    mean_volume = frame["volume"].rolling(20).mean()
    frame["volume_ratio"] = (frame["volume"] / mean_volume.replace(0, float("nan"))).fillna(0)
    return frame
