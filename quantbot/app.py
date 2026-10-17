import argparse
import json
import logging
import os
import tempfile
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

from .ai import Job, ModelClient, ModelWorker, drain_results
from .config import Config
from .dashboard import make_server
from .execution import PaperEngine
from .market_data import MarketData
from .state import timestamp
from .storage import Store


class Runner:
    """One fast market/protection loop and one independent, bounded model queue."""

    def __init__(self, config, engine, market, worker, clock=time.time):
        self.config, self.engine, self.market = config, engine, market
        self.worker, self.clock = worker, clock
        self.last_requested_candle = None
        self.pending_lesson = None
        self.latest_snapshot = None

    def _remember_loss(self, trade, snapshot):
        if trade and trade["pnl"] < 0:
            # Only a bounded, latest research request; no rule edits or extra exposure.
            self.pending_lesson = Job("LESSON", snapshot, {}, self.clock(), 0, trade)

    def _audit_failure(self, result, reason, quote, *, latest_candle=None, detail=""):
        self.engine.audit_rejection(
            reason,
            now=self.clock(),
            requested_at=result.job.requested_at,
            snapshot=result.job.snapshot,
            quote=quote,
            revision=result.job.revision,
            latest_candle=latest_candle,
            detail=detail,
        )

    def step(self):
        try:
            quote = self.market.fetch_quote()
        except Exception as exc:
            self.engine.health(status="DATA_ERROR", error=str(exc))
            for result in drain_results(self.worker):
                if result.job.kind == "DECISION":
                    self._audit_failure(result, "INVALID_MARKET_DATA", None, detail=str(exc))
                elif not result.error:
                    self.engine.add_lesson(result.value)
            return
        self._remember_loss(self.engine.on_quote(quote, self.clock()), self.latest_snapshot)
        # Invalid indicators must never prevent the preceding independent stop check.
        try:
            snapshot = self.market.fetch_snapshot(quote)
            snapshot.validate()
            self.latest_snapshot = snapshot
            with self.engine.lock:
                self.engine.state["market"].update(
                    ema_20=snapshot.ema_20,
                    ema_50=snapshot.ema_50,
                    ema_200=snapshot.ema_200,
                    rsi_14=snapshot.rsi_14,
                    atr_14=snapshot.atr_14,
                    vol_ratio=snapshot.volume_ratio,
                )
        except Exception as exc:
            self.engine.health(status="DATA_ERROR", error=str(exc))
            # Discard decisions that cannot be checked against a current closed candle.
            for result in drain_results(self.worker):
                if result.job.kind == "LESSON" and not result.error:
                    self.engine.add_lesson(result.value)
                elif result.job.kind == "DECISION":
                    self._audit_failure(result, "INVALID_MARKET_DATA", quote, detail=str(exc))
            return
        # Candle acquisition may take time; refresh if it consumed the quote's freshness budget.
        if self.clock() - quote.observed_at > self.config.max_quote_age:
            quote = self.market.fetch_quote()
            self._remember_loss(self.engine.on_quote(quote, self.clock()), snapshot)
        for result in drain_results(self.worker):
            if result.error:
                logging.warning("Model job failed: %s", result.error)
                self.engine.health(model_status="ERROR", model_error=result.error)
                if result.job.kind == "DECISION":
                    reason = (
                        "INVALID_MODEL_OUTPUT"
                        if result.error.startswith(("ValueError:", "ModelDecisionError:"))
                        else "MODEL_ERROR"
                    )
                    self._audit_failure(
                        result, reason, quote, latest_candle=snapshot.candle_id, detail=result.error
                    )
                continue
            if result.job.kind == "LESSON":
                self.engine.add_lesson(result.value)
                continue
            before = self.engine.snapshot()
            outcome = self.engine.apply_decision(
                result.value,
                result.job.snapshot,
                quote,
                now=self.clock(),
                requested_at=result.job.requested_at,
                revision=result.job.revision,
                latest_candle=snapshot.candle_id,
            )
            logging.info("Model result: %s", outcome)
            self.engine.health(model_status=outcome, model_error=None)
            after = self.engine.snapshot()
            if after["trades"] and after["trades"] != before["trades"]:
                self._remember_loss(after["trades"][0], snapshot)
        state = self.engine.snapshot()
        last = state["last_decision_candle"]
        if (
            self.worker.ready()
            and self.last_requested_candle != snapshot.candle_id
            and (last is None or snapshot.candle_id > last)
            and not state["system"].get("storage_error")
        ):
            # Quote and portfolio are current even when indicators came from the per-bar cache.
            current_snapshot = replace(snapshot, observed_at=quote.observed_at, current_price=quote.price)
            job = Job(
                "DECISION", current_snapshot, state["portfolio"], self.clock(), state["position_revision"]
            )
            if self.worker.submit(job):
                self.last_requested_candle = snapshot.candle_id
                self.engine.health(model_status="PENDING")
        elif self.worker.ready() and self.pending_lesson:
            if self.worker.submit(self.pending_lesson):
                self.pending_lesson = None
        self.engine.health(
            status="STORAGE_ERROR" if state["system"].get("storage_error") else "ONLINE",
            error=None if not state["system"].get("storage_error") else "Storage unavailable",
        )

    def run(self, stop_event):
        while not stop_event.is_set():
            started = self.clock()
            try:
                self.step()
            except Exception as exc:
                logging.exception("Cycle failed; blocking new entries until a valid cycle")
                self.engine.health(status="ERROR", error=f"{type(exc).__name__}: {exc}")
            # Fixed cadence excludes time already spent in the cycle; AI is never awaited here.
            stop_event.wait(max(0.1, self.config.poll_seconds - (self.clock() - started)))


def _heartbeat_is_fresh(path, now=None):
    """Reject missing, stale, or implausibly future controller heartbeats."""
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return False
    age = (time.time() if now is None else now) - modified_at
    return -5 <= age <= 30


def _watch_control_files(stop_path, heartbeat_path, stop, clock=time.time):
    """Watch launcher liveness and consume explicit stop requests."""
    while not stop.wait(0.2):
        if stop_path is not None:
            try:
                present = stop_path.is_file()
            except OSError:
                present = False
            if present:
                stop.set()
                try:
                    stop_path.unlink()
                except OSError:
                    pass
                return
        if heartbeat_path is not None and not _heartbeat_is_fresh(heartbeat_path, now=clock()):
            stop.set()
            return


def _write_control_ready_file(path, server):
    """Atomically publish the bound loopback dashboard endpoint to its launcher."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "port": server.server_address[1],
        "url": f"http://127.0.0.1:{server.server_address[1]}",
    }
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="QuantBot local PAPER trading; no real orders.")
    parser.add_argument("--data-dir", type=Path, default=Config().data_dir)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--model", default=Config().model, help="Installed local model name.")
    parser.add_argument("--control-stop-file", type=Path)
    parser.add_argument("--control-heartbeat-file", type=Path)
    parser.add_argument("--control-ready-file", type=Path)
    parser.add_argument(
        "--daily-loss-limit",
        type=float,
        default=None,
        help="Daily equity loss fraction, e.g. 0.03; disabled by default (UTC day).",
    )
    parser.add_argument(
        "--max-account-drawdown",
        type=float,
        default=None,
        help="Peak-equity drawdown fraction; disabled by default; triggered latch persists.",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=0,
        help="Pause entries/additions after a close; default 0 (disabled).",
    )
    parser.add_argument(
        "--equity-sample-seconds",
        type=float,
        default=60,
        help="Equity history interval; fills and protection transitions also force a sample.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        config = Config(
            data_dir=args.data_dir.resolve(),
            model=args.model,
            daily_loss_limit=args.daily_loss_limit,
            max_account_drawdown=args.max_account_drawdown,
            cooldown_seconds=args.cooldown_seconds,
            equity_sample_seconds=args.equity_sample_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    control_paths = [
        path.resolve()
        for path in (args.control_stop_file, args.control_heartbeat_file, args.control_ready_file)
        if path is not None
    ]
    if len(set(control_paths)) != len(control_paths):
        parser.error("Control stop, heartbeat, and ready files must use different paths")
    stop = threading.Event()
    store = engine = worker = server = server_thread = None
    stop_watcher = None
    ready_file = args.control_ready_file
    if args.control_stop_file is not None:
        try:
            stop_requested_before_start = args.control_stop_file.is_file()
        except OSError:
            stop_requested_before_start = False
        if stop_requested_before_start:
            stop.set()
            try:
                args.control_stop_file.unlink()
            except OSError:
                pass
    if args.control_heartbeat_file is not None and not _heartbeat_is_fresh(args.control_heartbeat_file):
        stop.set()
    if args.control_stop_file is not None or args.control_heartbeat_file is not None:
        stop_watcher = threading.Thread(
            target=_watch_control_files,
            args=(args.control_stop_file, args.control_heartbeat_file, stop),
            name="quantbot-control-watch",
            daemon=True,
        )
        stop_watcher.start()
    try:
        if stop.is_set():
            logging.info("A control stop was requested before startup")
            return 0
        store = Store(config)
        engine = PaperEngine(config, store)
        worker = ModelWorker(ModelClient(config))
        server = make_server(engine, args.port)
        engine.health(port=server.server_address[1])
        server_thread = threading.Thread(target=server.serve_forever, name="quantbot-dashboard", daemon=True)
        server_thread.start()
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        logging.info("PAPER trading dashboard: %s | model=%s", url, config.model)
        if ready_file is not None:
            _write_control_ready_file(ready_file, server)
        if not args.no_browser:
            webbrowser.open(url)
        Runner(config, engine, MarketData(config), worker).run(stop)
    except KeyboardInterrupt:
        logging.info("Stopping; active PAPER positions remain saved for restart")
    finally:
        stop.set()
        cleanup = []
        if engine is not None:
            cleanup.append(
                ("save offline state", lambda: engine.health(status="OFFLINE", last_update=timestamp()))
            )
        if worker is not None:
            cleanup.append(("stop model worker", worker.close))
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                cleanup.append(("stop dashboard", server.shutdown))
            cleanup.append(("close dashboard", server.server_close))
        if server_thread is not None:
            cleanup.append(("join dashboard", lambda: server_thread.join(timeout=2)))
        if store is not None:
            cleanup.append(("close account storage", store.close))
        for label, action in cleanup:
            try:
                action()
            except Exception:
                logging.exception("Failed to %s during shutdown", label)
        if ready_file is not None:
            try:
                ready_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logging.exception("Failed to remove control-ready file")
        if stop_watcher is not None:
            stop_watcher.join(timeout=1)
