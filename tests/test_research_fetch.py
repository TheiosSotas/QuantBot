import pytest

from quantbot.research.fetch import fetch_candles

INTERVAL = 3_600_000
START = 1_700_000_000_000 // INTERVAL * INTERVAL


def _candle(timestamp):
    return [timestamp, 100.0, 102.0, 99.0, 101.0, 5.0]


class _PagedExchange:
    id = "fake-binance"

    def __init__(self, end_ms, *, gap_at=None):
        self.end_ms = end_ms
        self.gap_at = gap_at
        self.calls = []

    def fetch_ohlcv(self, symbol, *, timeframe, since, limit):
        self.calls.append((symbol, timeframe, since, limit))
        rows = [_candle(timestamp) for timestamp in range(since, self.end_ms, INTERVAL)][:limit]
        if self.gap_at is not None:
            rows = [row for row in rows if row[0] != self.gap_at]
        return rows


def test_fetch_pages_more_than_one_exchange_limit_and_records_source_provenance():
    end = START + 1_005 * INTERVAL
    exchange = _PagedExchange(end)
    dataset = fetch_candles(
        start_ms=START,
        end_ms=end,
        interval_ms=INTERVAL,
        exchange=exchange,
        now_ms=end + INTERVAL,
    )
    assert len(dataset.rows) == 1_005
    assert [call[3] for call in exchange.calls] == [1_000, 5]
    assert [call[2] for call in exchange.calls] == [START, START + 1_000 * INTERVAL]
    assert dataset.source["type"] == "public_ccxt_rest"
    assert dataset.source["exchange"] == "fake-binance"
    assert dataset.source["start_ms"] == START
    assert dataset.source["end_ms"] == end


def test_fetch_rejects_gap_even_when_page_starts_at_requested_cursor():
    end = START + 3 * INTERVAL
    exchange = _PagedExchange(end, gap_at=START + INTERVAL)
    with pytest.raises(ValueError):
        fetch_candles(
            start_ms=START,
            end_ms=end,
            interval_ms=INTERVAL,
            exchange=exchange,
            now_ms=end + INTERVAL,
        )
    assert len(exchange.calls) == 1


def test_fetch_rejects_a_candle_that_has_not_fully_closed_without_requesting_it():
    end = START + 3 * INTERVAL

    class UnexpectedRequest:
        def fetch_ohlcv(self, *args, **kwargs):
            pytest.fail("An incomplete current candle must be rejected before API access")

    with pytest.raises(ValueError, match="not fully closed"):
        fetch_candles(
            start_ms=START,
            end_ms=end,
            interval_ms=INTERVAL,
            exchange=UnexpectedRequest(),
            now_ms=end - 1,
        )


def test_fetch_rejects_a_nonprogressing_page_instead_of_repeating_forever():
    class BackwardCursor:
        calls = 0

        def fetch_ohlcv(self, symbol, *, timeframe, since, limit):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("fetch cursor did not advance after the first response")
            return [_candle(since), _candle(since - INTERVAL)]

    exchange = BackwardCursor()
    with pytest.raises(ValueError):
        fetch_candles(
            start_ms=START,
            end_ms=START + 2 * INTERVAL,
            interval_ms=INTERVAL,
            exchange=exchange,
            now_ms=START + 3 * INTERVAL,
        )
    assert exchange.calls == 1
