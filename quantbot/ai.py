"""A bounded background worker; model output is untrusted input, never execution authority."""

import json
import queue
import threading
from dataclasses import dataclass

import requests

from .models import DECISION_SCHEMA, Decision
from .state import timestamp


class ModelClient:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()

    def consult(self, snapshot, portfolio):
        response = self.session.post(
            self.config.ollama_url,
            json={
                "model": self.config.model,
                "stream": False,
                "format": DECISION_SCHEMA,
                "system": (
                    "Analyze a SIMULATED BTC perpetual position using only the supplied observations. "
                    "LONG requires price > EMA50 > EMA200 and RSI < 68. "
                    "SHORT requires price < EMA50 < EMA200 and RSI > 32. Avoid choppy ranges. "
                    "Scale only into a profitable held direction with volume_ratio > 1.25. "
                    "Never request scaling when closing or reversing. Output the requested JSON schema. "
                    "Confidence is a subjective score, not an estimated win probability."
                ),
                "prompt": json.dumps(
                    {
                        "symbol": self.config.symbol,
                        "timeframe": self.config.timeframe,
                        "closed_candle_metrics": snapshot.as_dict(),
                        "portfolio": portfolio,
                    },
                    allow_nan=False,
                ),
                "options": {"temperature": 0.1},
            },
            timeout=(5, self.config.model_timeout),
        )
        response.raise_for_status()
        return Decision.parse(json.loads(response.json()["response"]))

    def post_mortem(self, trade, snapshot):
        schema = {
            "type": "object",
            "properties": {"root_cause": {"type": "string"}, "preventative_rule": {"type": "string"}},
            "required": ["root_cause", "preventative_rule"],
        }
        response = self.session.post(
            self.config.ollama_url,
            json={
                "model": self.config.model,
                "stream": False,
                "format": schema,
                "prompt": json.dumps(
                    {
                        "instruction": "Suggest a research hypothesis about this paper loss. "
                        "The limited observations cannot establish causation or eliminate future losses. "
                        "Suggestions will not change trading rules automatically.",
                        "trade": trade,
                        "exit_metrics": snapshot.as_dict() if snapshot else None,
                    }
                ),
                "options": {"temperature": 0.2},
            },
            timeout=(5, 60),
        )
        response.raise_for_status()
        data = json.loads(response.json()["response"])
        if not isinstance(data, dict) or any(
            not isinstance(data.get(k), str) or len(data[k]) > 4000
            for k in ("root_cause", "preventative_rule")
        ):
            raise ValueError("Invalid post-mortem")
        return dict(
            date=timestamp(),
            side=trade["side"],
            pnl=trade["pnl"],
            root_cause=data["root_cause"],
            preventative_rule=data["preventative_rule"],
            status="RESEARCH_ONLY",
        )


@dataclass(frozen=True)
class Job:
    kind: str
    snapshot: object
    portfolio: dict
    requested_at: float
    revision: int
    trade: object = None


@dataclass(frozen=True)
class Result:
    job: Job
    value: object = None
    error: str = ""


class ModelWorker:
    def __init__(self, client):
        self.client = client
        self.jobs = queue.Queue(maxsize=2)
        self.results = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = threading.Event()
        self.thread = threading.Thread(target=self._run, name="quantbot-model", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, job):
        try:
            self.jobs.put_nowait(job)
            return True
        except queue.Full:
            return False

    def ready(self):
        return not self.busy.is_set() and self.jobs.empty()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            self.busy.set()
            try:
                value = (
                    self.client.consult(job.snapshot, job.portfolio)
                    if job.kind == "DECISION"
                    else self.client.post_mortem(job.trade, job.snapshot)
                )
                self.results.put(Result(job, value=value))
            except Exception as exc:
                self.results.put(Result(job, error=f"{type(exc).__name__}: {exc}"))
            finally:
                self.busy.clear()
                self.jobs.task_done()

    def close(self):
        self.stop_event.set()


def drain_results(worker):
    while True:
        try:
            yield worker.results.get_nowait()
        except queue.Empty:
            return
