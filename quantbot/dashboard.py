import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .protections import entry_block_reason


def make_server(engine, port=0):
    html = (Path(__file__).parent / "web" / "dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body, content_type = html, "text/html; charset=utf-8"
            elif self.path == "/api/state":
                state = engine.snapshot()
                last = state["system"].get("last_quote_at")
                if last is not None and time.time() - last > engine.config.max_quote_age:
                    state["system"]["status"] = "STALE_DATA"
                blocked = entry_block_reason(state, engine.config, time.time())
                if last is None:
                    blocked = "WAITING_FOR_PRICE"
                elif not 0 <= time.time() - last <= engine.config.max_quote_age:
                    blocked = "STALE_PRICE"
                if state["system"].get("storage_error"):
                    blocked = "STORAGE_BLOCKED"
                state["system"]["entry_block_reason"] = blocked
                state["account_limits"] = {
                    "daily_loss_limit": engine.config.daily_loss_limit,
                    "max_account_drawdown": engine.config.max_account_drawdown,
                    "cooldown_seconds": engine.config.cooldown_seconds,
                    "equity_sample_seconds": engine.config.equity_sample_seconds,
                }
                body = json.dumps(state, allow_nan=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path == "/api/history":
                try:
                    history = {
                        "equity": engine.store.equity_history(200),
                        "decisions": engine.store.decision_history(20),
                    }
                    body = json.dumps(history, allow_nan=False).encode("utf-8")
                except Exception:
                    self.send_error(503, "Account history temporarily unavailable")
                    return
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
