"""Model readiness is separate from trading and from a candle decision deadline."""

import json
import queue
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from conftest import NOW, decision, quote, seed, snapshot

from quantbot.ai import Job, ModelClient, ModelWorker, Result
from quantbot.app import Runner
from quantbot.control import Control
from quantbot.diagnostics import check_model


def test_warmup_loads_without_requesting_a_decision_and_closes_response(config):
    calls, closed = [], []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"done": True, "done_reason": "load"},
            close=lambda: closed.append(True),
        )

    config = replace(config, model_keep_alive_seconds=7200)
    client = ModelClient(config, SimpleNamespace(post=post))
    assert client.warmup()["seconds"] >= 0
    assert calls[0]["json"] == {"model": config.model, "stream": False, "keep_alive": 7200}
    assert calls[0]["timeout"][1] == config.model_warmup_timeout
    assert client.build_request(snapshot(), {})["keep_alive"] == 7200
    assert closed == [True]


@pytest.mark.parametrize("payload", [{"done": False}, {"done": True, "error": "no memory"}])
def test_incomplete_warmup_does_not_claim_success(config, payload):
    closed = []
    response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: payload, close=lambda: closed.append(True)
    )
    with pytest.raises(ValueError, match="loading"):
        ModelClient(config, SimpleNamespace(post=lambda *a, **k: response)).warmup()
    assert closed == [True]


def test_blocked_warmup_does_not_block_existing_stop(config, engine):
    entered, release = threading.Event(), threading.Event()

    def warmup():
        entered.set()
        release.wait(3)
        return {"seconds": 1}

    worker = ModelWorker(SimpleNamespace(warmup=warmup))
    worker.start()
    try:
        worker.submit(Job("WARMUP", None, {}, NOW, 0))
        assert entered.wait(1) and not worker.ready()
        seed(engine)
        market = SimpleNamespace(fetch_quote=lambda: quote(80), fetch_snapshot=lambda _: snapshot(80))
        Runner(config, engine, market, worker, clock=lambda: NOW).step()
        assert engine.snapshot()["portfolio"]["side"] == "FLAT"
        assert engine.snapshot()["trades"][0]["type"] == "STOP_LOSS"
    finally:
        release.set()
        worker.close()
        worker.thread.join(2)


def test_dequeued_warmup_remains_busy_before_busy_flag_is_set():
    worker = ModelWorker(SimpleNamespace())
    assert worker.submit(Job("WARMUP", None, {}, NOW, 0))
    worker.jobs.get_nowait()
    assert not worker.busy.is_set()
    assert not worker.ready()
    worker.jobs.task_done()
    assert worker.ready()


@pytest.mark.parametrize("bad_data", ["quote", "indicators", None])
def test_warmup_result_is_never_treated_as_a_trade_or_lesson(config, engine, bad_data):
    results = queue.Queue()
    results.put(Result(Job("WARMUP", None, {}, NOW, 0), value={"seconds": 1}))
    worker = SimpleNamespace(results=results, ready=lambda: False)

    def failure():
        raise ValueError("data unavailable")

    market = SimpleNamespace(
        fetch_quote=failure if bad_data == "quote" else lambda: quote(),
        fetch_snapshot=lambda _: failure() if bad_data == "indicators" else snapshot(),
    )
    Runner(config, engine, market, worker, clock=lambda: NOW).step()
    state = engine.snapshot()
    assert state["portfolio"]["side"] == "FLAT"
    assert state["lessons"] == [] and state["trades"] == []
    assert state["system"]["model_status"] == "READY"


def test_diagnostic_real_http_load_schema_and_gpu_telemetry_are_isolated(config):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(payload)
            self.send(
                {"done": True, "done_reason": "stop", "response": json.dumps(decision())}
                if "prompt" in payload
                else {"done": True, "done_reason": "load"}
            )

        def do_GET(self):
            assert self.path == "/api/ps"
            self.send(
                {"models": [{"name": config.model, "size": 100, "size_vram": 80, "context_length": 4096}]}
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        selected = replace(config, ollama_url=f"http://127.0.0.1:{server.server_address[1]}/api/generate")
        result = check_model(selected)
        assert result["ok"] is True and result["paper_budget_ok"] is True
        assert result["memory"]["size_vram"] == 80
        assert len(seen) == 2 and "prompt" not in seen[0] and "format" in seen[1]
        assert not (config.data_dir / "quantbot.sqlite3").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_control_diagnostic_is_async_and_prevents_concurrent_paper_start(tmp_path):
    control = Control(tmp_path)
    control.ollama = lambda: ({"ok": True}, [{"name": "qwen2.5:14b"}])
    entered, release = threading.Event(), threading.Event()

    def check(*args, **kwargs):
        entered.set()
        release.wait(3)
        return {"ok": True, "model": "qwen2.5:14b", "decision_seconds": 1, "warmup_seconds": 2}

    with patch("quantbot.diagnostics.check_model", side_effect=check):
        try:
            control.action({"action": "model_check", "model": "qwen2.5:14b"})
            assert entered.wait(1) and control.job["running"] is True
            with pytest.raises(ValueError, match="έλεγχος"):
                control.paper_start({})
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread.name == "quantbot-desktop-job":
                    thread.join(2)
    assert control.model_check_result["ok"] is True and not control.job["running"]
    assert not (tmp_path / "quantbot.sqlite3").exists()


def test_model_check_rejects_running_paper_and_unknown_model(tmp_path):
    control = Control(tmp_path)
    control.ollama = lambda: ({"ok": True}, [{"name": "qwen2.5:14b"}])
    with pytest.raises(ValueError, match="εγκατεστημένο"):
        control.start_job({"action": "model_check", "model": "unknown"})
    control.paper = Mock()
    control.paper.poll.return_value = None
    with pytest.raises(ValueError, match="πριν"):
        control.start_job({"action": "model_check", "model": "qwen2.5:14b"})
    assert not control.job["running"]


def test_check_cancelled_after_loading_never_generates_decision(config):
    cancelled = [False]

    def load():
        cancelled[0] = True
        return {"seconds": 1}

    client = SimpleNamespace(warmup=load, consult_with_trace=Mock())
    with pytest.raises(RuntimeError, match="Ακυρώθηκε"):
        check_model(config, client=client, cancelled=lambda: cancelled[0])
    client.consult_with_trace.assert_not_called()
