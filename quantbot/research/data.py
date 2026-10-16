"""Validated, immutable historical candle datasets for reproducible research."""

import csv
import hashlib
import io
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from ..indicators import calculate_indicators
from ..models import Snapshot

CSV_HEADER = ("timestamp", "open", "high", "low", "close", "volume")
DEFAULT_INTERVAL_MS = 3_600_000
DEFAULT_SYMBOL = "BTC/USDT:USDT"
DATASET_SCHEMA_VERSION = 1
MAX_TIMESTAMP_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z


@dataclass(frozen=True)
class Bar:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _finite_real(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Invalid {name}")
    if positive and result <= 0:
        raise ValueError(f"Invalid {name}")
    if nonnegative and result < 0:
        raise ValueError(f"Invalid {name}")
    return result


def _interval(value):
    if type(value) is not int or value <= 0:
        raise ValueError("interval_ms must be a positive integer")
    return value


def _freeze_json(value):
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Dataset source keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Dataset source must contain only finite JSON values")


def _thaw_json(value):
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_rows(rows, interval_ms):
    interval_ms = _interval(interval_ms)
    normalized = []
    previous = None
    for index, row in enumerate(rows):
        if isinstance(row, Bar):
            values = (row.timestamp, row.open, row.high, row.low, row.close, row.volume)
        elif isinstance(row, (list, tuple)) and len(row) == 6:
            values = row
        else:
            raise ValueError(f"Invalid candle row at index {index}")
        timestamp = values[0]
        if (
            type(timestamp) is not int
            or timestamp < 0
            or timestamp > MAX_TIMESTAMP_MS
            or timestamp % interval_ms
        ):
            raise ValueError(f"Invalid or misaligned candle timestamp at index {index}")
        if previous is not None and timestamp != previous + interval_ms:
            raise ValueError("Candle timestamps must be strictly ascending and contiguous")
        open_price, high, low, close = (
            _finite_real(value, f"OHLC value at index {index}", positive=True) for value in values[1:5]
        )
        volume = _finite_real(values[5], f"volume at index {index}", nonnegative=True)
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise ValueError(f"Inconsistent OHLC range at index {index}")
        normalized.append(Bar(timestamp, open_price, high, low, close, volume))
        previous = timestamp
    if not normalized:
        raise ValueError("Dataset must contain at least one candle")
    return tuple(normalized)


def _identity_payload(rows, interval_ms, symbol, market_type):
    return {
        "interval_ms": interval_ms,
        "market_type": market_type,
        "rows": [[row.timestamp, row.open, row.high, row.low, row.close, row.volume] for row in rows],
        "symbol": symbol,
    }


def _canonical_sha256(rows, interval_ms, symbol, market_type):
    canonical = json.dumps(
        _identity_payload(rows, interval_ms, symbol, market_type),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class Dataset:
    rows: tuple[Bar, ...]
    interval_ms: int
    symbol: str
    market_type: str
    sha256: str
    source: dict

    def __post_init__(self):
        interval_ms = _interval(self.interval_ms)
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a nonempty string")
        if not isinstance(self.market_type, str) or not self.market_type.strip():
            raise ValueError("market_type must be a nonempty string")
        rows = _normalize_rows(self.rows, interval_ms)
        expected = _canonical_sha256(rows, interval_ms, self.symbol, self.market_type)
        if self.sha256 != expected:
            raise ValueError("Dataset canonical SHA-256 does not match its candles")
        if not isinstance(self.source, dict):
            raise ValueError("source must be an object")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "source", _freeze_json(self.source))

    def manifest(self):
        """Return a detached JSON-safe provenance manifest."""
        return {
            "schema_version": DATASET_SCHEMA_VERSION,
            "symbol": self.symbol,
            "market_type": self.market_type,
            "interval_ms": self.interval_ms,
            "row_count": len(self.rows),
            "first_timestamp": self.rows[0].timestamp,
            "last_timestamp": self.rows[-1].timestamp,
            "sha256": self.sha256,
            "source": _thaw_json(self.source),
        }

    def to_frame(self):
        return pd.DataFrame(
            ((row.timestamp, row.open, row.high, row.low, row.close, row.volume) for row in self.rows),
            columns=CSV_HEADER,
        )


def dataset_from_rows(
    rows,
    *,
    interval_ms=DEFAULT_INTERVAL_MS,
    symbol=DEFAULT_SYMBOL,
    market_type="USDT-M",
    source=None,
):
    interval_ms = _interval(interval_ms)
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a nonempty string")
    if not isinstance(market_type, str) or not market_type.strip():
        raise ValueError("market_type must be a nonempty string")
    rows = _normalize_rows(rows, interval_ms)
    source = {} if source is None else source
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    # Validate JSON types and finite numbers before storing detached provenance.
    json.dumps(source, allow_nan=False)
    return Dataset(
        rows=rows,
        interval_ms=interval_ms,
        symbol=symbol,
        market_type=market_type,
        sha256=_canonical_sha256(rows, interval_ms, symbol, market_type),
        source=source,
    )


def _read_csv_bytes(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Dataset CSV must be UTF-8") from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("Dataset CSV is empty") from exc
    if tuple(header) != CSV_HEADER:
        raise ValueError("Dataset CSV header must be timestamp,open,high,low,close,volume")
    rows = []
    for line_number, fields in enumerate(reader, start=2):
        if len(fields) != len(CSV_HEADER):
            raise ValueError(f"Malformed CSV row at line {line_number}")
        if re.fullmatch(r"[0-9]+", fields[0]) is None:
            raise ValueError(f"Timestamp must be integer milliseconds at line {line_number}")
        try:
            numeric = [float(field) for field in fields[1:]]
        except ValueError as exc:
            raise ValueError(f"Invalid numeric candle at line {line_number}") from exc
        rows.append((int(fields[0]), *numeric))
    if not rows:
        raise ValueError("Dataset CSV contains no candles")
    return rows


def load_csv(path, *, interval_ms=DEFAULT_INTERVAL_MS, symbol=DEFAULT_SYMBOL, market_type="USDT-M"):
    path = Path(path)
    content = path.read_bytes()
    source = {
        "kind": "csv",
        "path": str(path.resolve()),
        "raw_sha256": hashlib.sha256(content).hexdigest(),
    }
    rows = _read_csv_bytes(content)
    return dataset_from_rows(
        rows,
        interval_ms=interval_ms,
        symbol=symbol,
        market_type=market_type,
        source=source,
    )


def _csv_bytes(dataset):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in dataset.rows:
        writer.writerow(
            (
                row.timestamp,
                *(format(value, ".17g") for value in (row.open, row.high, row.low, row.close, row.volume)),
            )
        )
    return stream.getvalue().encode("utf-8")


def write_dataset(dataset, directory):
    if not isinstance(dataset, Dataset):
        raise ValueError("dataset must be a validated Dataset")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        candle_bytes = _csv_bytes(dataset)
        data_path = directory / "candles.csv"
        with data_path.open("xb") as stream:
            stream.write(candle_bytes)
        manifest = dataset.manifest()
        manifest.update(
            data_file="candles.csv",
            data_sha256=hashlib.sha256(candle_bytes).hexdigest(),
        )
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
        with (directory / "manifest.json").open("xb") as stream:
            stream.write(manifest_bytes)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


def load_dataset(directory):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid dataset manifest") from exc
    required = {
        "schema_version",
        "symbol",
        "market_type",
        "interval_ms",
        "row_count",
        "first_timestamp",
        "last_timestamp",
        "sha256",
        "source",
        "data_file",
        "data_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("Dataset manifest has an invalid schema")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != DATASET_SCHEMA_VERSION
        or manifest["data_file"] != "candles.csv"
    ):
        raise ValueError("Unsupported dataset manifest")
    content = (directory / "candles.csv").read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["data_sha256"]:
        raise ValueError("Dataset CSV file hash does not match manifest")
    rows = _read_csv_bytes(content)
    dataset = dataset_from_rows(
        rows,
        interval_ms=manifest["interval_ms"],
        symbol=manifest["symbol"],
        market_type=manifest["market_type"],
        source=manifest["source"],
    )
    if dataset.sha256 != manifest["sha256"]:
        raise ValueError("Dataset canonical hash does not match manifest")
    if (
        type(manifest["row_count"]) is not int
        or type(manifest["first_timestamp"]) is not int
        or type(manifest["last_timestamp"]) is not int
        or len(dataset.rows) != manifest["row_count"]
        or dataset.rows[0].timestamp != manifest["first_timestamp"]
        or dataset.rows[-1].timestamp != manifest["last_timestamp"]
    ):
        raise ValueError("Dataset range or row count does not match manifest")
    return dataset


def _index(value, rows_length):
    if type(value) is not int or not 0 <= value < rows_length:
        raise ValueError("index is outside the dataset")


def validate_execution_data(hourly, execution, start_index, end_index):
    """Validate and return lower-timeframe bars covering hourly[start_index:end_index].

    The execution dataset may begin at the evaluation window, independent of any
    warmup history available in the hourly dataset.
    """
    if not isinstance(hourly, Dataset) or not isinstance(execution, Dataset):
        raise ValueError("hourly and execution must be validated Datasets")
    if (
        type(start_index) is not int
        or type(end_index) is not int
        or not 0 <= start_index < end_index <= len(hourly.rows)
    ):
        raise ValueError("Require 0 <= start_index < end_index <= hourly dataset length")
    if hourly.symbol != execution.symbol or hourly.market_type != execution.market_type:
        raise ValueError("Hourly and execution datasets must use the same symbol and market")
    if execution.interval_ms > hourly.interval_ms or hourly.interval_ms % execution.interval_ms:
        raise ValueError("Execution interval must divide the hourly interval")

    bars_per_hour = hourly.interval_ms // execution.interval_ms
    by_timestamp = {bar.timestamp: bar for bar in execution.rows}
    selected = []
    for parent in hourly.rows[start_index:end_index]:
        children = []
        for offset in range(bars_per_hour):
            timestamp = parent.timestamp + offset * execution.interval_ms
            child = by_timestamp.get(timestamp)
            if child is None:
                raise ValueError(f"Execution data does not fully cover hourly candle {parent.timestamp}")
            children.append(child)
        aggregate = (
            children[0].open,
            max(bar.high for bar in children),
            min(bar.low for bar in children),
            children[-1].close,
            sum(bar.volume for bar in children),
        )
        expected = (parent.open, parent.high, parent.low, parent.close, parent.volume)
        if not all(
            math.isclose(actual, wanted, rel_tol=1e-9, abs_tol=1e-8)
            for actual, wanted in zip(aggregate, expected)
        ):
            raise ValueError(f"Execution bars do not aggregate to hourly candle {parent.timestamp}")
        selected.extend(children)
    return tuple(selected)


def history_until(dataset, index, limit=None):
    if not isinstance(dataset, Dataset):
        raise ValueError("dataset must be a validated Dataset")
    _index(index, len(dataset.rows))
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    start = 0 if limit is None else max(0, index + 1 - limit)
    return dataset.rows[start : index + 1]


def make_snapshot(dataset, index, config, now, price):
    if not isinstance(dataset, Dataset):
        raise ValueError("dataset must be a validated Dataset")
    _index(index, len(dataset.rows))
    if config.symbol != dataset.symbol:
        raise ValueError("Dataset symbol does not match config")
    if dataset.market_type != "USDT-M":
        raise ValueError("Historical snapshots require USDT-M market data")
    if config.timeframe != "1h" or dataset.interval_ms != DEFAULT_INTERVAL_MS:
        raise ValueError("Historical snapshots require a matching 1h dataset and config")
    if type(config.candle_limit) is not int or type(config.minimum_candles) is not int:
        raise ValueError("Invalid candle warmup configuration")
    rows = history_until(dataset, index, config.candle_limit)
    if len(rows) < config.minimum_candles:
        raise ValueError("Insufficient historical candles for indicator warmup")
    now = _finite_real(now, "observed_at", positive=True)
    price = _finite_real(price, "price", positive=True)
    bar = dataset.rows[index]
    if bar.timestamp + dataset.interval_ms > now * 1000:
        raise ValueError("Historical candle has not closed by the requested snapshot time")
    frame = pd.DataFrame(
        ((row.timestamp, row.open, row.high, row.low, row.close, row.volume) for row in rows),
        columns=CSV_HEADER,
    )
    computed = calculate_indicators(frame).iloc[-1]
    atr = float(computed.atr_14)
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError("Historical snapshot has no positive ATR")
    snapshot = Snapshot(
        candle_id=int(computed.timestamp),
        observed_at=now,
        current_price=price,
        ema_20=float(computed.ema_20),
        ema_50=float(computed.ema_50),
        ema_200=float(computed.ema_200),
        rsi_14=float(computed.rsi_14),
        atr_14=atr,
        volume_ratio=float(computed.volume_ratio),
    )
    snapshot.validate()
    return snapshot
