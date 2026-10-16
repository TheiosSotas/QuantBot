"""Local launcher handoff and graceful stop behavior."""

import json
import os
import threading
import time

from quantbot import app


def test_missing_stale_or_future_heartbeat_requests_shutdown(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    assert not app._heartbeat_is_fresh(heartbeat)
    heartbeat.touch()
    now = time.time()
    assert app._heartbeat_is_fresh(heartbeat, now=now)
    assert not app._heartbeat_is_fresh(heartbeat, now=now + 31)
    assert not app._heartbeat_is_fresh(heartbeat, now=now - 6)
    stop = threading.Event()
    watcher = threading.Thread(
        target=app._watch_control_files, args=(None, heartbeat, stop, lambda: now + 31)
    )
    watcher.start()
    assert stop.wait(2)
    watcher.join(timeout=2)


def test_missing_heartbeat_exits_before_opening_paper_account(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "Store", lambda *_args: opened.append(True))
    assert app.main(["--no-browser", "--control-heartbeat-file", str(tmp_path / "missing")]) == 0
    assert opened == []


def test_control_stop_file_requests_normal_shutdown_and_ready_handoff_is_atomic(tmp_path, monkeypatch):
    stop_file = tmp_path / "private-control" / "stop"
    ready_file = tmp_path / "private-control" / "ready.json"
    observed = {}

    class Store:
        def __init__(self, config):
            observed["store_config"] = config

        def close(self):
            observed["store_closed"] = True

    class Engine:
        def __init__(self, config, store):
            self.config, self.store = config, store

        def health(self, **values):
            observed.setdefault("health", []).append(values)

    class Worker:
        def __init__(self, client):
            pass

        def start(self):
            observed["worker_started"] = True

        def close(self):
            observed["worker_closed"] = True

    class Server:
        server_address = ("127.0.0.1", 43210)

        def __init__(self):
            self.stopped = threading.Event()

        def serve_forever(self):
            self.stopped.wait()

        def shutdown(self):
            self.stopped.set()

        def server_close(self):
            observed["server_closed"] = True

    class Runner:
        def __init__(self, config, engine, market, worker):
            observed["model"] = config.model

        def run(self, stop):
            payload = json.loads(ready_file.read_text(encoding="utf-8"))
            observed["ready"] = payload
            assert payload == {
                "pid": os.getpid(),
                "port": 43210,
                "url": "http://127.0.0.1:43210",
            }
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text("stop", encoding="utf-8")
            assert stop.wait(2)

    monkeypatch.setattr(app, "Store", Store)
    monkeypatch.setattr(app, "PaperEngine", Engine)
    monkeypatch.setattr(app, "ModelWorker", Worker)
    monkeypatch.setattr(app, "make_server", lambda *_args: Server())
    monkeypatch.setattr(app, "MarketData", lambda _config: object())
    monkeypatch.setattr(app, "Runner", Runner)

    assert (
        app.main(
            [
                "--data-dir",
                str(tmp_path / "account"),
                "--no-browser",
                "--model",
                "installed-model:tag",
                "--control-stop-file",
                str(stop_file),
                "--control-ready-file",
                str(ready_file),
            ]
        )
        is None
    )

    assert observed["model"] == "installed-model:tag"
    assert observed["worker_started"] is True
    assert observed["worker_closed"] is True
    assert observed["server_closed"] is True
    assert observed["store_closed"] is True
    assert observed["health"][-1]["status"] == "OFFLINE"
    assert not ready_file.exists()
    assert not stop_file.exists()
    assert list(ready_file.parent.iterdir()) == []


def test_preexisting_stop_marker_exits_before_opening_account(tmp_path, monkeypatch):
    marker = tmp_path / "stop"
    marker.write_text("stop", encoding="utf-8")
    opened = []
    monkeypatch.setattr(app, "Store", lambda *_args: opened.append(True))

    assert (
        app.main(
            [
                "--data-dir",
                str(tmp_path / "account"),
                "--no-browser",
                "--control-stop-file",
                str(marker),
            ]
        )
        == 0
    )
    assert opened == []
    assert not marker.exists()


def test_server_startup_failure_still_closes_the_store_and_worker(tmp_path, monkeypatch):
    observed = []

    class Store:
        def __init__(self, config):
            pass

        def close(self):
            observed.append("store")

    class Engine:
        def __init__(self, config, store):
            pass

        def health(self, **values):
            observed.append(values["status"] if "status" in values else "health")

    class Worker:
        def __init__(self, client):
            pass

        def close(self):
            observed.append("worker")

    monkeypatch.setattr(app, "Store", Store)
    monkeypatch.setattr(app, "PaperEngine", Engine)
    monkeypatch.setattr(app, "ModelWorker", Worker)
    monkeypatch.setattr(app, "make_server", lambda *_args: (_ for _ in ()).throw(OSError("port unavailable")))

    try:
        app.main(["--data-dir", str(tmp_path / "account"), "--no-browser"])
    except OSError as exc:
        assert str(exc) == "port unavailable"
    else:
        raise AssertionError("server startup error should propagate")
    assert observed == ["OFFLINE", "worker", "store"]
