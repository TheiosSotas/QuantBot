"""Transactional in-memory storage adapter for the unchanged PaperEngine."""

import copy
import json
import time
from pathlib import Path

from ..state import initial_state, validate_state


class MemoryStore:
    def __init__(self, config, state=None):
        self.config = config
        self.state = copy.deepcopy(state) if state is not None else initial_state(config)
        self.events = []

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state, events=()):
        validate_state(state)
        # Validate everything before publishing either state or events.
        candidate = json.loads(json.dumps(state, allow_nan=False))
        additions = json.loads(json.dumps(list(events), allow_nan=False))
        self.state = candidate
        self.events.extend(additions)

    def export_csv(self):
        pass  # Research exports are written once, without production account paths.

    def equity_history(self):
        return [copy.deepcopy(payload) for kind, payload in self.events if kind == "EQUITY"]

    def decision_history(self):
        return [copy.deepcopy(payload) for kind, payload in self.events if kind == "DECISION_AUDIT"]

    def trades(self):
        return [copy.deepcopy(payload) for kind, payload in self.events if kind == "CLOSE"]

    def fork(self):
        return MemoryStore(self.config, self.state)


class CompactMemoryStore(MemoryStore):
    """File-backed event journal for long research replays.

    Only account state remains resident. Branch stores created by ``fork`` are
    short-lived in-memory stores containing events for the current execution
    bar only; the chosen branch's new events are then appended here.
    """

    def __init__(self, config, event_path, state=None):
        super().__init__(config, state)
        self.event_path = Path(event_path)
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.event_path.open("x", encoding="utf-8", buffering=1024 * 1024)
        self._last_flush = time.monotonic()
        self.event_count = 0

    def save(self, state, events=()):
        validate_state(state)
        candidate = json.loads(json.dumps(state, allow_nan=False))
        additions = json.loads(json.dumps(list(events), allow_nan=False))
        self._write_events(additions)
        self.state = candidate

    def append_event(self, kind, payload):
        event = json.loads(json.dumps([kind, payload], allow_nan=False))
        self._write_events([event])

    def _write_events(self, additions):
        for event in additions:
            self._stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
            self._stream.write("\n")
        self.event_count += len(additions)
        if time.monotonic() - self._last_flush >= 1.0:
            self.flush()

    def flush(self):
        if not self._stream.closed:
            self._stream.flush()
            self._last_flush = time.monotonic()

    def close(self):
        if not self._stream.closed:
            self._stream.flush()
            self._stream.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def iter_events(self):
        self.flush()
        with self.event_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)

    def fork(self):
        return MemoryStore(self.config, self.state)
