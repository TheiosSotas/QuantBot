"""Real HTTP + model worker + SQLite at minute/five-minute cadence.

The server emits deterministic fixtures, never real model predictions.
"""

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from conftest import NOW, decision, quote, seed, snapshot

from quantbot.ai import ModelClient, ModelWorker
from quantbot.app import Runner
from quantbot.config import Config
from quantbot.dashboard import make_server
from quantbot.execution import PaperEngine
from quantbot.research.data import write_dataset
from quantbot.research.synthetic import demo_datasets
from quantbot.storage import Store


@contextmanager
def model_server(respond):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(request)
            status, body = respond(request)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            data = json.dumps({"models": [{"name": "fixture-local", "digest": "a" * 64}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/generate", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def wait_worker(worker):
    deadline = time.monotonic() + 3
    while not worker.ready() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert worker.ready(), "HTTP fixture/model worker did not finish"


@pytest.mark.parametrize("seconds", [60, 300])
@pytest.mark.parametrize("model", ["fixture-local", "fixture:cloud"])
def test_http_invalid_repair_buy_hold_sell_and_dashboard(tmp_path, seconds, model):
    outputs = iter(
        [
            "",
            "",  # Both attempts fail: must reject rather than invent HOLD.
            json.dumps(decision("BUY", confidence=85)),
            json.dumps(decision("BUY")),
            json.dumps(decision("HOLD")),
            json.dumps(decision("SELL")),
            json.dumps(decision("BUY")),
            json.dumps(decision("CLOSE")),
        ]
    )

    def respond(_request):
        return 200, {"done": True, "done_reason": "stop", "response": next(outputs)}

    with model_server(respond) as (endpoint, seen):
        config = Config(
            data_dir=tmp_path, model=model, ollama_url=endpoint, decision_interval_seconds=seconds
        )
        store = Store(config)
        clock = {"now": (int(NOW // seconds) + 1) * seconds + 2, "price": 100}
        engine = PaperEngine(config, store, clock=lambda: clock["now"])
        worker = ModelWorker(ModelClient(config))
        market = SimpleNamespace(
            fetch_quote=lambda: quote(clock["price"], now=clock["now"]),
            fetch_snapshot=lambda _: snapshot(),
        )
        runner = Runner(config, engine, market, worker, clock=lambda: clock["now"])
        worker.start()
        try:
            for index, expected_side in enumerate(("FLAT", "LONG", "LONG", "FLAT", "LONG", "FLAT")):
                if index:
                    clock["now"] += seconds
                    clock["price"] += 1
                runner.step()
                wait_worker(worker)
                runner.step()
                assert engine.state["portfolio"]["side"] == expected_side
                before = len(seen)
                runner.step()
                assert len(seen) == before  # No second evaluation in this interval.

            audits = store.decision_history()
            assert len(audits) == 6
            assert audits[0]["action"] is None and audits[0]["status"] == "REJECTED"
            assert audits[0]["reason_code"] == "INVALID_MODEL_OUTPUT"
            assert "Empty model response" in audits[0]["detail"]
            assert [row["action"] for row in audits[1:]] == ["BUY", "HOLD", "SELL", "BUY", "CLOSE"]
            assert len({row["candle_id"] for row in audits}) == 1
            assert len(engine.state["trades"]) == 2
            assert engine.state["portfolio"]["cash_usdt"] > config.starting_cash
            assert len(seen) == 8
            assert all(("format" not in payload) == model.endswith(":cloud") for payload in seen)
            assert all(
                json.loads(payload["prompt"])["decision_interval_seconds"] == seconds for payload in seen
            )
            assert "format_correction" in json.loads(seen[1]["prompt"])
            assert "format_correction" in json.loads(seen[3]["prompt"])

            dashboard = make_server(engine)
            thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{dashboard.server_address[1]}/api/history") as response:
                    public = json.load(response)
                assert len(public["decisions"]) == 6
                assert public["decisions"][0]["detail"] == audits[0]["detail"]
            finally:
                dashboard.shutdown()
                dashboard.server_close()
                thread.join(2)
        finally:
            worker.close()
            worker.thread.join(2)
            store.close()


@pytest.mark.parametrize("seconds", [60, 300])
def test_http_billing_failure_recovers_next_interval_without_fabricated_trade(tmp_path, seconds):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return 402, {"error": "Payment Required (test fixture)"}
        return 200, {"response": json.dumps(decision("BUY")), "done": True}

    with model_server(respond) as (endpoint, _seen):
        config = Config(
            data_dir=tmp_path, model="fixture:cloud", ollama_url=endpoint, decision_interval_seconds=seconds
        )
        store = Store(config)
        worker = ModelWorker(ModelClient(config))
        now = [(int(NOW // seconds) + 1) * seconds + 2]
        engine = PaperEngine(config, store, clock=lambda: now[0])
        market = SimpleNamespace(fetch_quote=lambda: quote(now=now[0]), fetch_snapshot=lambda _: snapshot())
        runner = Runner(config, engine, market, worker, clock=lambda: now[0])
        worker.start()
        try:
            runner.step()
            wait_worker(worker)
            runner.step()
            assert len(calls) == 1  # 402 is not a transport retry.
            assert engine.state["portfolio"]["side"] == "FLAT"
            assert "402" in store.decision_history()[-1]["detail"]
            now[0] += seconds
            runner.step()
            wait_worker(worker)
            runner.step()
            assert len(calls) == 2
            assert engine.state["portfolio"]["side"] == "LONG"
        finally:
            worker.close()
            worker.thread.join(2)
            store.close()


@pytest.mark.parametrize("seconds", [60, 300])
def test_real_http_block_cannot_delay_stop_or_reopen_closed_position(tmp_path, seconds):
    entered, release = threading.Event(), threading.Event()

    def respond(request):
        if "exit_metrics" in json.loads(request["prompt"]):
            return 200, {"response": json.dumps({"root_cause": "fixture", "preventative_rule": "fixture"})}
        entered.set()
        release.wait(3)
        return 200, {"done": True, "response": json.dumps(decision("BUY"))}

    with model_server(respond) as (endpoint, _seen):
        config = Config(data_dir=tmp_path, ollama_url=endpoint, decision_interval_seconds=seconds)
        store = Store(config)
        worker = ModelWorker(ModelClient(config))
        engine = PaperEngine(config, store, clock=lambda: NOW)
        seed(engine)
        price = [100]
        market = SimpleNamespace(fetch_quote=lambda: quote(price[0]), fetch_snapshot=lambda _: snapshot())
        runner = Runner(config, engine, market, worker, clock=lambda: NOW)
        worker.start()
        try:
            runner.step()
            assert entered.wait(2)
            price[0] = 80
            started = time.monotonic()
            runner.step()
            assert time.monotonic() - started < 0.5
            assert engine.state["portfolio"]["side"] == "FLAT"
            assert engine.state["trades"][0]["type"] == "STOP_LOSS"
            release.set()
            wait_worker(worker)
            runner.step()
            assert engine.state["portfolio"]["side"] == "FLAT"
            assert any(row["reason_code"] == "POSITION_CHANGED" for row in store.decision_history())
        finally:
            release.set()
            worker.close()
            worker.thread.join(2)
            store.close()


@pytest.mark.parametrize("interval,expected", [("1m", 60), ("5m", 12)])
def test_cli_backtest_real_http_compact_export_and_cache_only_replay(tmp_path, interval, expected):
    hourly, minute = demo_datasets(warmup=606, hours=1)
    write_dataset(hourly, tmp_path / "hourly")
    write_dataset(minute, tmp_path / "minute")
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            output = json.dumps(decision("HOLD", confidence=85))
        elif len(calls) in (3, 4):
            output = ""
        else:
            output = json.dumps(decision("HOLD"))
        return 200, {"done": True, "response": output}

    with model_server(respond) as (endpoint, _seen):
        common = [
            sys.executable,
            "-m",
            "quantbot.research",
            "compare",
            "--data",
            str(tmp_path / "hourly"),
            "--execution-data",
            str(tmp_path / "minute"),
            "--start-index",
            "606",
            "--decision-interval",
            interval,
            "--baselines",
            "rules,numeric",
            "--model",
            "fixture-local",
            "--ollama-url",
            endpoint,
            "--cache",
            str(tmp_path / "cache"),
            "--compact",
        ]
        first = subprocess.run(
            common + ["--output", str(tmp_path / "first")], capture_output=True, text=True, timeout=45
        )
        assert first.returncode == 0, first.stderr
        count = len(calls)
        cached = subprocess.run(
            common
            + [
                "--output",
                str(tmp_path / "cached"),
                "--cache-mode",
                "replay-only",
                "--model-digest",
                "fixture-local=" + "a" * 64,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert cached.returncode == 0, cached.stderr
        assert len(calls) == count

    first_report = json.loads((tmp_path / "first" / "comparison.json").read_text(encoding="utf-8"))
    cached_report = json.loads((tmp_path / "cached" / "comparison.json").read_text(encoding="utf-8"))
    assert set(first_report["candidates"]) == {"rules", "numeric", "ollama_1"}
    for name, candidate in first_report["candidates"].items():
        metrics = candidate["metrics"]
        assert metrics["advice_count"] == expected
        assert metrics == cached_report["candidates"][name]["metrics"]
        folder = tmp_path / "first" / name
        assert (folder / "equity.csv").stat().st_size > 0
        assert (folder / "decisions.jsonl").stat().st_size > 0
    ai = first_report["candidates"]["ollama_1"]["metrics"]
    assert ai["model_output_repairs"] == 1
    assert ai["model_errors"] == 1
    assert ai["invalid_model_output_attempts"] == 3
    html = (tmp_path / "first" / "report.html").read_text(encoding="utf-8")
    assert "σφάλματα AI" in html and "Νέα απόφαση κάθε" in html
    assert not (tmp_path / "quantbot.sqlite3").exists()


@pytest.mark.parametrize("seconds", [60, 300])
@pytest.mark.parametrize("status", [429, 500])
def test_http_transient_failure_retries_same_decision_once(tmp_path, seconds, status):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return status, {"error": "Temporary test failure"}
        return 200, {"done": True, "response": json.dumps(decision("BUY"))}

    with model_server(respond) as (endpoint, _seen):
        config = Config(data_dir=tmp_path, ollama_url=endpoint, decision_interval_seconds=seconds)
        store = Store(config)
        worker = ModelWorker(ModelClient(config))
        now = [(int(NOW // seconds) + 1) * seconds + 2]
        engine = PaperEngine(config, store, clock=lambda: now[0])
        market = SimpleNamespace(fetch_quote=lambda: quote(now=now[0]), fetch_snapshot=lambda _: snapshot())
        runner = Runner(config, engine, market, worker, clock=lambda: now[0])
        worker.start()
        try:
            runner.step()
            original_deadline = runner.retry_state["deadline_monotonic"]
            wait_worker(worker)
            runner.step()
            assert engine.state["system"]["model_status"] == "RETRY_WAIT"
            assert engine.state["portfolio"]["side"] == "FLAT"
            original_request = store.decision_history()[-1]["requested_at"]
            now[0] += 5
            runner.step()
            assert runner.retry_state["deadline_monotonic"] == original_deadline
            wait_worker(worker)
            runner.step()
            assert engine.state["portfolio"]["side"] == "LONG"
            assert runner.retry_state is None
            assert store.decision_history()[-1]["requested_at"] == original_request
            runner.step()
            assert len(calls) == 2
            assert len(store.decision_history()) == 2
        finally:
            worker.close()
            worker.thread.join(2)
            store.close()
