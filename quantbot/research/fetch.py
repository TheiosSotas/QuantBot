"""Explicit public-API acquisition; never called by replay or the paper process."""

import time

from .data import dataset_from_rows


def fetch_candles(*, start_ms, end_ms, interval_ms, symbol="BTC/USDT:USDT", exchange=None, now_ms=None):
    if any(type(value) is not int for value in (start_ms, end_ms, interval_ms)):
        raise ValueError("Fetch bounds and interval must be integer milliseconds")
    if interval_ms not in (60_000, 300_000, 3_600_000) or not 0 < start_ms < end_ms:
        raise ValueError("Use a positive UTC range and a 1m, 5m or 1h interval")
    if start_ms % interval_ms or end_ms % interval_ms:
        raise ValueError("Fetch bounds must align with the chosen interval")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if end_ms > now_ms - now_ms % interval_ms:
        raise ValueError("Fetch range contains a candle which is not fully closed")
    if exchange is None:
        import ccxt

        exchange = ccxt.binance(
            {"enableRateLimit": True, "timeout": 10000, "options": {"defaultType": "future"}}
        )
    timeframe = {60_000: "1m", 300_000: "5m", 3_600_000: "1h"}[interval_ms]
    cursor, rows = start_ms, []
    while cursor < end_ms:
        page = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=cursor, limit=min(1000, (end_ms - cursor) // interval_ms)
        )
        if not page:
            raise ValueError(f"Historical API returned no candles at {cursor}")
        if any(not isinstance(row, (list, tuple)) or len(row) != 6 for row in page):
            raise ValueError("Historical API returned an invalid OHLCV page")
        if any(type(row[0]) is not int for row in page) or page[0][0] != cursor:
            raise ValueError("Historical API skipped or repeated the requested first candle")
        if any(row[0] != cursor + index * interval_ms for index, row in enumerate(page)):
            raise ValueError("Historical API page is unordered, duplicated or has missing candle intervals")
        accepted = [row for row in page if row[0] < end_ms]
        if not accepted:
            raise ValueError("Historical API made no forward progress")
        rows.extend(accepted)
        cursor = accepted[-1][0] + interval_ms
    return dataset_from_rows(
        rows,
        interval_ms=interval_ms,
        symbol=symbol,
        source={
            "type": "public_ccxt_rest",
            "exchange": getattr(exchange, "id", "binance"),
            "timeframe": timeframe,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "downloaded_at_ms": now_ms,
            "dataset_terms": "Check source terms for the intended use; this manifest grants no data license",
        },
    )
