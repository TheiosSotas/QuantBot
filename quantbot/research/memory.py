"""Transactional in-memory storage adapter for the unchanged PaperEngine."""

import copy
import json

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
