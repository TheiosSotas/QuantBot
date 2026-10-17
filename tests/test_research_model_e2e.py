"""Real loopback HTTP protocol + immutable cache, with explicitly fake AI."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from quantbot.config import Config
from quantbot.research.advisors import OllamaAdvisor
from quantbot.research.cache import CachedAdvisor
from quantbot.research.comparison import compare
from quantbot.research.synthetic import demo_datasets


def test_empty_advice_cannot_be_reported_as_a_successful_model():
    from quantbot.research.advisors import Advice
    from quantbot.research.replay import run_replay

    class EmptyAdvisor:
        def identity(self):
            return {"provider": "empty-test"}

        def advise(self, _context):
            return Advice(None, 10000)

    hourly, minute = demo_datasets(hours=1)
    result = run_replay(hourly, EmptyAdvisor(), config=Config(), execution_data=minute)
    assert result["metrics"]["model_errors"] >= 1
    assert "no decision" in result["advice"][0]["error"]


def test_malformed_http_ai_is_repaired_and_cached_with_complete_trace(tmp_path):
    calls = {"get": 0, "post": 0}
    digest = "c" * 64

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls["get"] += 1
            self.send_json({"models": [{"name": "repair-test", "digest": digest}]})

        def do_POST(self):
            calls["post"] += 1
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            corrected = "format_correction" in json.loads(payload["prompt"])
            output = {
                "action": "BUY",
                "confidence": 0.9 if corrected else 90,
                "scale_in_signal": False if corrected else "false",
                "market_regime": "TRENDING_BULL",
                "thesis": "Protocol fixture",
                "copilot_commentary": "",
            }
            self.send_json({"response": json.dumps(output)})

        def send_json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = replace(Config(), ollama_url=f"http://127.0.0.1:{server.server_address[1]}/api/generate")
        hourly, minute = demo_datasets(hours=1)

        def advisor(offline=False):
            model = OllamaAdvisor(config, model="repair-test", model_digest=digest if offline else None)
            if not offline:
                model.resolve_model_digest()
            return CachedAdvisor(
                model,
                tmp_path / "cache",
                dataset_hash=hourly.sha256,
                cache_mode="replay-only" if offline else "readwrite",
            )

        options = dict(config=config, execution_data=minute, compact=True)
        first = compare(hourly, {"qwen": advisor()}, output=tmp_path / "first", **options)
        before = calls.copy()
        second = compare(hourly, {"qwen": advisor(True)}, output=tmp_path / "second", **options)
        metrics = first["candidates"]["qwen"]["metrics"]
        assert metrics["model_errors"] == 0
        assert metrics["model_output_repairs"] == metrics["invalid_model_output_attempts"] == 1
        assert metrics == second["candidates"]["qwen"]["metrics"]
        assert calls == before and calls["post"] == 2
        assert calls["get"] == 3  # Resolution, initial request, corrective request.
        advice = [
            json.loads(line)
            for line in (tmp_path / "first" / "qwen" / "advice.jsonl").read_text().splitlines()
        ]
        trace = advice[0]["metadata"]["output_validation"]
        assert trace["recovered"] is True and len(trace["attempts"]) == 2
        assert trace["attempts"][1]["request_hash"] != trace["attempts"][0]["request_hash"]
        assert not (tmp_path / "quantbot.sqlite3").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_two_http_model_accounts_and_cache_only_reproduction(tmp_path):
    calls = {"get": 0, "post": 0}
    digests = {"mock-buyer": "a" * 64, "mock-observer": "b" * 64}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls["get"] += 1
            self._send({"models": [{"name": name, "digest": digest} for name, digest in digests.items()]})

        def do_POST(self):
            calls["post"] += 1
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/generate"
            prompt = json.loads(request["prompt"])
            assert prompt["closed_candle_metrics"]["candle_id"] > 0
            output = {
                "action": "BUY" if request["model"] == "mock-buyer" else "HOLD",
                "confidence": 0.9,
                "scale_in_signal": False,
                "market_regime": "TRENDING_BULL",
                "thesis": "Mock HTTP fixture, not an AI forecast",
                "copilot_commentary": "",
            }
            self._send({"response": json.dumps(output)})

        def _send(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = replace(Config(), ollama_url=f"http://127.0.0.1:{server.server_address[1]}/api/generate")
        hourly, minute = demo_datasets(warmup=606, hours=2)

        def advisors(replay_only=False):
            result = {}
            for name, digest in digests.items():
                advisor = OllamaAdvisor(config, model=name, model_digest=digest if replay_only else None)
                if not replay_only:
                    advisor.resolve_model_digest()
                result[name] = CachedAdvisor(
                    advisor,
                    tmp_path / "cache",
                    dataset_hash=hourly.sha256,
                    cache_mode="replay-only" if replay_only else "readwrite",
                )
            return result

        options = dict(
            config=config,
            execution_data=minute,
            start_index=606,
            end_index=608,
            fixed_latency_seconds=0,
            publication_delay_seconds=0,
        )
        first = compare(hourly, advisors(), output=tmp_path / "first", **options)
        before = calls.copy()
        second = compare(hourly, advisors(True), output=tmp_path / "second", **options)
        assert calls == before  # No model or tag network calls in cache-only mode.
        assert calls["post"] == 4
        assert first["candidates"]["mock-buyer"]["metrics"]["model_errors"] == 0
        for name in digests:
            assert first["candidates"][name]["metrics"] == second["candidates"][name]["metrics"]
            state = json.loads((tmp_path / "first" / name / "final-state.json").read_text())
            assert state["system"]["mode"] == "PAPER"
            audits = [
                json.loads(line)
                for line in (tmp_path / "first" / name / "decisions.jsonl").read_text().splitlines()
            ]
            assert all(row["model"] == name for row in audits)
        assert not (tmp_path / "quantbot.sqlite3").exists()
        html = (tmp_path / "first" / "report.html").read_text(encoding="utf-8")
        assert "mock-buyer" in html and "mock-observer" in html
        assert "polyline" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
