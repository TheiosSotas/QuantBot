import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
                body = json.dumps(state, allow_nan=False).encode("utf-8")
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
