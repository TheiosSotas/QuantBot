"""SQLite is authoritative; trades.csv is a disposable export, never a commit prerequisite."""

import csv
import json
import math
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

from .state import initial_state, validate_state

CSV_FIELDS = ["Timestamp", "Side", "Type", "Entry", "Exit", "Units", "PnL_USDT", "Return_Pct", "Result"]
TRADE_KEYS = ["time", "side", "type", "entry", "exit", "units", "pnl", "return_pct", "result"]
SCHEMA_VERSION = 1
MAX_HISTORY_LIMIT = 10_000


class Store:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.root / ".quantbot.lock").open("a+b")
        if self._lock_file.seek(0, os.SEEK_END) == 0:
            self._lock_file.write(b"0")
            self._lock_file.flush()
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise RuntimeError("Another QuantBot owns this data directory") from exc
        self.db_path = self.root / "quantbot.sqlite3"
        self.db = None
        try:
            self.db = sqlite3.connect(self.db_path)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.commit()
            self._migrate_schema()
        except Exception:
            try:
                if self.db is not None:
                    self.db.close()
            finally:
                self._lock_file.close()
            raise

    def _migrate_schema(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError(f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS equity_history "
                "(event_id TEXT PRIMARY KEY REFERENCES events(id), observed_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS decision_audit "
                "(event_id TEXT PRIMARY KEY REFERENCES events(id), observed_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS equity_history_observed_at ON equity_history(observed_at)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS decision_audit_observed_at ON decision_audit(observed_at)"
            )
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def load(self):
        row = self.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
        if row:
            state = json.loads(row[0])
            from .protections import migrate_protections

            migrated = migrate_protections(state)
            validate_state(state)
            json.dumps(state, allow_nan=False)
            if (
                state["system"]["symbol"] != self.config.symbol
                or state["system"]["timeframe"] != self.config.timeframe
            ):
                raise ValueError("Saved account belongs to a different symbol/timeframe")
            if migrated:
                # Persist only the schema upgrade; startup-only health fields are not historical state.
                self.save(state)
            # Never restore an optimistic health status from a prior process.
            state["system"].update(
                status="STARTING",
                model_status="IDLE",
                error=None,
                last_quote_at=None,
                last_protection_at=None,
                storage_error=None,
            )
            return state
        state = initial_state(self.config)
        events = []
        legacy = self.root / "trades.csv"
        if legacy.exists():
            with legacy.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not set(CSV_FIELDS).issubset(reader.fieldnames or []):
                    raise ValueError("Legacy CSV has missing columns; refusing to reset the account")
                for row in reader:
                    trade = dict(zip(TRADE_KEYS, (row[k] for k in CSV_FIELDS)))
                    for key in ("entry", "exit", "units", "pnl", "return_pct"):
                        trade[key] = float(trade[key])
                        if not math.isfinite(trade[key]):
                            raise ValueError("Nonfinite legacy trade")
                    state["trades"].insert(0, trade)
                    state["portfolio"]["cash_usdt"] += trade["pnl"]
                    events.append(("LEGACY_CLOSE", trade))
        memory = self.root / "quant_memory.json"
        if memory.exists():
            lessons = json.loads(memory.read_text(encoding="utf-8"))
            if not isinstance(lessons, list) or not all(isinstance(x, dict) for x in lessons):
                raise ValueError("Invalid legacy lessons")
            state["lessons"] = lessons[:25]
        state["portfolio"]["total_equity"] = state["portfolio"]["cash_usdt"]
        state["trades"] = state["trades"][:200]
        self.save(state, events)
        return state

    def save(self, state, events=()):
        validate_state(state)
        payload = json.dumps(state, allow_nan=False)
        with self.db:
            self.db.execute(
                "INSERT INTO snapshot VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (payload,),
            )
            for kind, event in events:
                if not isinstance(kind, str) or not kind:
                    raise ValueError("Invalid event kind")
                event_payload = json.dumps(event, allow_nan=False)
                event_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO events VALUES (?, ?, ?)",
                    (event_id, kind, event_payload),
                )
                if kind == "EQUITY":
                    observed_at = _validate_equity_event(event)
                    self.db.execute(
                        "INSERT INTO equity_history VALUES (?, ?, ?)",
                        (event_id, observed_at, event_payload),
                    )
                elif kind == "DECISION_AUDIT":
                    observed_at = _validate_decision_audit_event(event)
                    self.db.execute(
                        "INSERT INTO decision_audit VALUES (?, ?, ?)",
                        (event_id, observed_at, event_payload),
                    )

    def equity_history(self, limit=200):
        return self._history("equity_history", limit)

    def decision_history(self, limit=100):
        return self._history("decision_audit", limit)

    def _history(self, table, limit):
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"History limit must be an integer from 1 to {MAX_HISTORY_LIMIT}")
        if table not in ("equity_history", "decision_audit"):
            raise ValueError("Unsupported history table")
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = connection.execute(
                f"SELECT event_id, payload FROM {table} ORDER BY observed_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        rows.reverse()
        result = []
        for event_id, payload in rows:
            decoded = json.loads(payload)
            decoded["event_id"] = event_id
            result.append(decoded)
        return result

    def export_csv(self):
        rows = self.db.execute(
            "SELECT payload FROM events WHERE kind IN ('CLOSE','LEGACY_CLOSE') ORDER BY rowid"
        ).fetchall()
        descriptor, temporary = tempfile.mkstemp(prefix=".trades-", suffix=".csv", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(CSV_FIELDS)
                for row in rows:
                    trade = json.loads(row[0])
                    writer.writerow([trade[k] for k in TRADE_KEYS])
            os.replace(temporary, self.root / "trades.csv")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self):
        try:
            self.db.close()
        finally:
            self._lock_file.close()  # OS releases the process lock, including after a crash.


def _finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {field}")


def _required_string(payload, field):
    if not isinstance(payload.get(field), str) or not payload[field]:
        raise ValueError(f"Invalid {field}")


def _optional_number(payload, field):
    value = payload.get(field)
    if value is not None:
        _finite_number(value, field)


def _optional_int(payload, field):
    value = payload.get(field)
    if value is not None and type(value) is not int:
        raise ValueError(f"Invalid {field}")


def _validate_equity_event(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid equity event")
    for key in (
        "observed_at",
        "quote_at",
        "price",
        "cash",
        "unrealized_pnl",
        "equity",
        "peak_equity",
        "drawdown",
        "exposure",
    ):
        if key not in event:
            raise ValueError(f"Missing equity field: {key}")
        _finite_number(event[key], key)
    if "notional" in event:
        _finite_number(event["notional"], "notional")
    if event["price"] <= 0:
        raise ValueError("Invalid equity price")
    if event["observed_at"] < 0 or event["quote_at"] < 0 or event["quote_at"] > event["observed_at"]:
        raise ValueError("Invalid equity timestamps")
    if event["drawdown"] < 0 or event["exposure"] < 0:
        raise ValueError("Invalid equity risk metrics")
    if event["peak_equity"] < event["equity"]:
        raise ValueError("Peak equity is below current equity")
    if not math.isclose(event["cash"] + event["unrealized_pnl"], event["equity"], rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("Equity does not match cash plus unrealized PnL")
    if event["side"] not in ("FLAT", "LONG", "SHORT"):
        raise ValueError("Invalid equity side")
    return event["observed_at"]


def _validate_decision_audit_event(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid decision audit event")
    required = (
        "observed_at",
        "requested_at",
        "candle_id",
        "action",
        "status",
        "reason_code",
        "outcome",
        "model",
        "snapshot",
        "decision",
        "quote_at",
        "execution_price",
        "position_revision",
        "latest_candle",
    )
    missing = [key for key in required if key not in event]
    if missing:
        raise ValueError(f"Missing decision audit field: {missing[0]}")
    _finite_number(event["observed_at"], "observed_at")
    for key in ("requested_at", "quote_at", "execution_price"):
        _optional_number(event, key)
    for key in ("candle_id", "position_revision", "latest_candle"):
        _optional_int(event, key)
    for key in ("action",):
        if event[key] is not None and not isinstance(event[key], str):
            raise ValueError(f"Invalid {key}")
    if event["status"] not in ("ACCEPTED", "REJECTED", "NO_ACTION"):
        raise ValueError("Invalid decision audit status")
    for key in ("reason_code", "outcome", "model"):
        _required_string(event, key)
    for key in ("snapshot", "decision"):
        if event[key] is not None and not isinstance(event[key], dict):
            raise ValueError(f"Invalid {key}")
    if "detail" in event and event["detail"] is not None and not isinstance(event["detail"], str):
        raise ValueError("Invalid detail")
    return event["observed_at"]
