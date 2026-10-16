from types import SimpleNamespace

import pandas as pd
import pytest

from quantbot.indicators import calculate_indicators
from quantbot.market_data import MarketData
from quantbot.models import Quote

NOW = 1700002805.0
INTERVAL = 3600000
EXPECTED = (int(NOW * 1000) // INTERVAL - 1) * INTERVAL


def candles():
    return [[EXPECTED - (998 - i) * INTERVAL, 100, 102, 98, 100, 10] for i in range(1000)]


def market(config, rows=None, ticker=None):
    values = candles() if rows is None else rows
    exchange = SimpleNamespace(
        parse_timeframe=lambda _: 3600,
        fetch_ohlcv=lambda *a, **k: values,
        fetch_ticker=lambda _: ticker or {"last": 100, "timestamp": NOW * 1000},
    )
    return MarketData(config, exchange, clock=lambda: NOW)


def test_current_candle_is_excluded_and_flat_rsi_is_neutral(config):
    rows = candles()
    rows[-1] = [EXPECTED + INTERVAL, 100, 20000, 1, 19000, 100000]
    data = market(config, rows).fetch_snapshot(Quote(101, NOW))
    assert data.candle_id == EXPECTED
    assert data.ema_20 == pytest.approx(100)
    assert data.rsi_14 == 50
    assert data.current_price == 101
    assert data.volume_ratio == 1


def test_indicator_fetch_is_cached_once_per_closed_candle(config):
    source = market(config)
    first = source.fetch_snapshot(Quote(100, NOW))
    source.exchange.fetch_ohlcv = lambda *a, **k: pytest.fail("Unnecessary candle download")
    assert source.fetch_snapshot(Quote(101, NOW)) is first


@pytest.mark.parametrize("kind", ["short", "missing", "duplicate", "nonfinite", "bad_range", "stale"])
def test_invalid_or_stale_candles_are_rejected(config, kind):
    rows = candles()
    if kind == "short":
        rows = rows[-20:]
    elif kind == "missing":
        rows.pop(100)
    elif kind == "duplicate":
        rows[100] = rows[99]
    elif kind == "nonfinite":
        rows[100][4] = float("inf")
    elif kind == "bad_range":
        rows[100][2] = 90
    else:
        rows = rows[:-2]
    with pytest.raises(ValueError):
        market(config, rows).fetch_snapshot(Quote(100, NOW))


@pytest.mark.parametrize("stamp", [(NOW - 20) * 1000, (NOW + 30) * 1000, float("nan")])
def test_exchange_quote_timestamp_is_checked(config, stamp):
    with pytest.raises(ValueError):
        market(config, ticker={"last": 100, "timestamp": stamp}).fetch_quote()


def test_warmup_is_longer_than_ema_span(config):
    assert config.candle_limit >= 1000
    frame = pd.DataFrame(
        {
            "close": list(range(1, 101)),
            "high": list(range(2, 102)),
            "low": list(range(0, 100)),
            "volume": [0] * 100,
        }
    )
    computed = calculate_indicators(frame)
    assert computed.rsi_14.iloc[-1] == 100
    assert computed.volume_ratio.iloc[-1] == 0
