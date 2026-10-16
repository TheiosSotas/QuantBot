import hashlib
import json
import math

import pytest

from quantbot.indicators import calculate_indicators
from quantbot.research.data import (
    Bar,
    dataset_from_rows,
    history_until,
    load_csv,
    load_dataset,
    make_snapshot,
    validate_execution_data,
    write_dataset,
)

INTERVAL = 3_600_000
START = 1_700_000_000_000 // INTERVAL * INTERVAL


def candles(count=630):
    rows = []
    for index in range(count):
        close = 100 + index * 0.03 + math.sin(index / 7) * 2
        open_price = close - math.sin(index / 3) * 0.7
        rows.append(
            (
                START + index * INTERVAL,
                open_price,
                max(open_price, close) + 1.2,
                min(open_price, close) - 1.2,
                close,
                50 + index % 23,
            )
        )
    return rows


def test_factory_normalizes_bars_and_source_provenance_is_detached():
    source = {"provider": "unit-test", "labels": ["one", {"verified": True}]}
    original = dataset_from_rows(candles(2), source=source)
    source["labels"][1]["verified"] = False
    assert original.source["labels"][1]["verified"] is True
    with pytest.raises(TypeError):
        original.source["provider"] = "changed"
    manifest = original.manifest()
    manifest["source"]["labels"][1]["verified"] = False
    assert original.manifest()["source"]["labels"][1]["verified"] is True

    rows = candles(2)
    bars = [Bar(*row) for row in rows]
    assert dataset_from_rows(bars).rows == dataset_from_rows(rows).rows


@pytest.mark.parametrize(
    "rows,interval",
    [
        ([(START, 1, 2, 0.5, 1, 1), (START + INTERVAL * 2, 1, 2, 0.5, 1, 1)], INTERVAL),
        ([(START, 1, 2, 0.5, 1, 1), (START, 1, 2, 0.5, 1, 1)], INTERVAL),
        ([(START + 1, 1, 2, 0.5, 1, 1)], INTERVAL),
        ([(START, True, 2, 0.5, 1, 1)], INTERVAL),
        ([(START, 1, float("inf"), 0.5, 1, 1)], INTERVAL),
        ([(START, 1, 2, 0.5, 1, -1)], INTERVAL),
        ([(START, 3, 2, 0.5, 1, 1)], INTERVAL),
        ([(START, 1, 2, 0.5, 1, 1)], True),
    ],
)
def test_factory_rejects_malformed_rows_and_intervals(rows, interval):
    with pytest.raises(ValueError):
        dataset_from_rows(rows, interval_ms=interval)


def test_csv_loader_requires_explicit_integer_millisecond_timestamp(tmp_path):
    source = tmp_path / "market.csv"
    source.write_text(
        "timestamp,open,high,low,close,volume\n"
        f"{START},100,102,99,101,5\n"
        f"{START + INTERVAL},101,103,100,102,6\n",
        encoding="utf-8",
    )
    content = source.read_bytes()
    dataset = load_csv(source)
    assert dataset.source["raw_sha256"] == hashlib.sha256(content).hexdigest()
    assert dataset.source["kind"] == "csv"

    source.write_text("timestamp,open,high,low,close,volume\n1700000000,100,102,99,101,5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="misaligned"):
        load_csv(source)

    source.write_text("time,open,high,low,close,volume\n1,100,102,99,101,5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        load_csv(source)

    source.write_text(
        "timestamp,open,high,low,close,volume\n1700000000000000,100,102,99,101,5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timestamp"):
        load_csv(source, interval_ms=1000)


def test_dataset_directory_round_trips_canonically_and_refuses_overwrite(tmp_path):
    dataset = dataset_from_rows(candles(5), source={"origin": "fixture"})
    directory = tmp_path / "bundle"
    write_dataset(dataset, directory)
    loaded = load_dataset(directory)
    assert loaded.sha256 == dataset.sha256
    assert loaded.rows == dataset.rows
    assert loaded.manifest()["source"] == {"origin": "fixture"}
    with pytest.raises(FileExistsError):
        write_dataset(dataset, directory)

    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["symbol"] = "ETH/USDT:USDT"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical hash"):
        load_dataset(directory)


def test_dataset_directory_detects_raw_candle_file_edits(tmp_path):
    directory = tmp_path / "tamper"
    write_dataset(dataset_from_rows(candles(3)), directory)
    candle_path = directory / "candles.csv"
    candle_path.write_bytes(candle_path.read_bytes().replace(b"timestamp", b"tamestamp", 1))
    with pytest.raises(ValueError, match="file hash"):
        load_dataset(directory)


def test_history_until_is_bounded_and_never_includes_future_rows():
    dataset = dataset_from_rows(candles(10))
    assert history_until(dataset, 4) == dataset.rows[:5]
    assert history_until(dataset, 4, 2) == dataset.rows[3:5]
    with pytest.raises(ValueError):
        history_until(dataset, True)
    with pytest.raises(ValueError):
        history_until(dataset, 10)


def test_snapshot_matches_live_indicator_rules_and_is_prefix_invariant(config):
    source_rows = candles()
    dataset = dataset_from_rows(source_rows)
    index = 619
    price, now = 125.5, 1_800_000_000.0
    snapshot = make_snapshot(dataset, index, config, now, price)

    prefix = dataset.to_frame().iloc[: index + 1].tail(config.candle_limit)
    expected = calculate_indicators(prefix).iloc[-1]
    assert snapshot.candle_id == source_rows[index][0]
    assert snapshot.observed_at == now
    assert snapshot.current_price == price
    assert snapshot.ema_20 == pytest.approx(expected.ema_20)
    assert snapshot.ema_50 == pytest.approx(expected.ema_50)
    assert snapshot.ema_200 == pytest.approx(expected.ema_200)
    assert snapshot.rsi_14 == pytest.approx(expected.rsi_14)
    assert snapshot.atr_14 == pytest.approx(expected.atr_14)
    assert snapshot.volume_ratio == pytest.approx(expected.volume_ratio)

    source_rows[index + 1] = (source_rows[index + 1][0], 900, 901, 899, 900, 10_000)
    future_changed = dataset_from_rows(source_rows)
    assert make_snapshot(future_changed, index, config, now, price) == snapshot


def test_snapshot_rejects_incompatible_symbol_timeframe_warmup_or_zero_atr(config):
    with pytest.raises(ValueError, match="Insufficient"):
        make_snapshot(dataset_from_rows(candles(599)), 598, config, 1_800_000_000, 100)
    with pytest.raises(ValueError, match="symbol"):
        make_snapshot(dataset_from_rows(candles(), symbol="ETH/USDT:USDT"), 620, config, 1_800_000_000, 100)
    minute_rows = [(START + index * 60_000, *row[1:]) for index, row in enumerate(candles())]
    with pytest.raises(ValueError, match="1h"):
        make_snapshot(dataset_from_rows(minute_rows, interval_ms=60_000), 620, config, 1_800_000_000, 100)
    flat = [(START + index * INTERVAL, 100, 100, 100, 100, 1) for index in range(620)]
    with pytest.raises(ValueError, match="ATR"):
        make_snapshot(dataset_from_rows(flat), 619, config, 1_800_000_000, 100)


def test_snapshot_rejects_future_source_bar_and_non_usdm_market(config):
    rows = candles()
    dataset = dataset_from_rows(rows)
    with pytest.raises(ValueError, match="has not closed"):
        make_snapshot(dataset, 619, config, (rows[619][0] + INTERVAL // 2) / 1000, 120)
    spot = dataset_from_rows(rows, market_type="spot")
    with pytest.raises(ValueError, match="USDT-M"):
        make_snapshot(spot, 619, config, 1_800_000_000, 120)


def _hour_and_quarters(count=3):
    hours = []
    quarters = []
    for hour_index in range(count):
        timestamp = START + hour_index * INTERVAL
        child_rows = []
        for part in range(4):
            open_price = 100 + hour_index + part * 0.1
            close = open_price + (0.2 if part % 2 == 0 else -0.1)
            child_rows.append(
                (
                    timestamp + part * (INTERVAL // 4),
                    open_price,
                    max(open_price, close) + 0.3,
                    min(open_price, close) - 0.3,
                    close,
                    10 + part,
                )
            )
        quarters.extend(child_rows)
        hours.append(
            (
                timestamp,
                child_rows[0][1],
                max(row[2] for row in child_rows),
                min(row[3] for row in child_rows),
                child_rows[-1][4],
                sum(row[5] for row in child_rows),
            )
        )
    return dataset_from_rows(hours), dataset_from_rows(quarters, interval_ms=INTERVAL // 4)


def test_execution_validation_covers_only_selected_hourly_evaluation_window():
    hourly, all_quarters = _hour_and_quarters()
    evaluation_quarters = dataset_from_rows(all_quarters.rows[4:], interval_ms=INTERVAL // 4)
    selected = validate_execution_data(hourly, evaluation_quarters, 1, 3)
    assert selected == all_quarters.rows[4:]


def test_execution_validation_rejects_missing_coverage_identity_and_aggregate_mismatch():
    hourly, quarters = _hour_and_quarters()
    with pytest.raises(ValueError, match="fully cover"):
        missing_first = dataset_from_rows(quarters.rows[1:], interval_ms=INTERVAL // 4)
        validate_execution_data(hourly, missing_first, 0, 2)
    with pytest.raises(ValueError, match="same symbol"):
        other_market = dataset_from_rows(quarters.rows, interval_ms=INTERVAL // 4, market_type="spot")
        validate_execution_data(hourly, other_market, 0, 1)
    with pytest.raises(ValueError, match="divide"):
        indivisible_rows = [
            (START // 70_000 * 70_000 + index * 70_000, 100, 101, 99, 100, 1) for index in range(3)
        ]
        indivisible = dataset_from_rows(indivisible_rows, interval_ms=70_000)
        validate_execution_data(hourly, indivisible, 0, 1)

    bad_hourly_rows = list(hourly.rows)
    row = bad_hourly_rows[1]
    bad_hourly_rows[1] = Bar(row.timestamp, row.open, row.high, row.low, row.close, row.volume + 1)
    bad_hourly = dataset_from_rows(bad_hourly_rows)
    with pytest.raises(ValueError, match="aggregate"):
        validate_execution_data(bad_hourly, quarters, 1, 2)
