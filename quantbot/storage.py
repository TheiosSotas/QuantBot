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
        self.db = sqlite3.connect(self.root / "quantbot.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
        if row:
            state = json.loads(row[0])
            validate_state(state)
            json.dumps(state, allow_nan=False)
            if (
                state["system"]["symbol"] != self.config.symbol
                or state["system"]["timeframe"] != self.config.timeframe
            ):
                raise ValueError("Saved account belongs to a different symbol/timeframe")
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
                self.db.execute(
                    "INSERT INTO events VALUES (?, ?, ?)",
                    (uuid.uuid4().hex, kind, json.dumps(event, allow_nan=False)),
                )

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
