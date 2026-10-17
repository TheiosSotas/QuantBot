"""Loopback-only desktop control centre. Research never opens the paper database."""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from .config import Config

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = "http://127.0.0.1:11434"


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def hidden_process(args, **kwargs):
    return subprocess.Popen(
        [str(arg) for arg in args],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **kwargs,
    )


def parse_settings(payload):
    """Validate public form values before spawning any worker."""

    def number(name, default, optional=False):
        value = payload.get(name, default)
        if optional and value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError(f"Μη έγκυρη τιμή: {name}")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"Μη έγκυρη τιμή: {name}")
        return value

    config = Config(
        starting_cash=number("starting_cash", 10000),
        daily_loss_limit=number("daily_loss_limit", None, True),
        max_account_drawdown=number("max_account_drawdown", None, True),
        cooldown_seconds=number("cooldown_seconds", 0),
    )
    args = []
    for name in ("daily_loss_limit", "max_account_drawdown", "cooldown_seconds"):
        value = getattr(config, name)
        if value is not None:
            args += ["--" + name.replace("_", "-"), str(value)]
    return config, args


def parse_range(payload):
    try:
        start = datetime.strptime(payload["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(payload["end"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Επιλέξτε έγκυρες ημερομηνίες αρχής και λήξης.") from exc
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if not start < end <= today or end - start > timedelta(days=366):
        raise ValueError("Επιλέξτε από 1 έως 366 ολοκληρωμένες ημέρες, μέχρι σήμερα UTC.")
    interval = payload.get("interval", "5m")
    if interval not in ("1m", "5m"):
        raise ValueError("Η ανάλυση πρέπει να είναι 1m ή 5m.")
    return start, end, interval


class Control:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.runtime = self.root / ".quantbot-desktop"
        self.runtime.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.workspace = hashlib.sha256(str(self.root).encode()).hexdigest()
        self.paper = None
        self.paper_info = None
        self.paper_stop = None
        self.paper_heartbeat = None
        self.paper_error = None
        self.paper_log = None
        self.job_process = None
        self.cancel = threading.Event()
        self.logs = deque(maxlen=160)
        self.job = {"running": False, "id": None, "stage": "Έτοιμο", "error": None, "report_url": None}
        self.closed = False
        self.server = None
        self.report_cache = {}

    def report_html(self, directory):
        from .research.presentation import saved_comparison_html

        directory = Path(directory).resolve()
        summary = directory / "comparison.json"
        candidates = json.loads(summary.read_text(encoding="utf-8"))["candidates"]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in candidates):
            raise ValueError("Invalid saved candidate name")
        files = [summary] + [
            directory / name / file
            for name in candidates
            for file in ("run.json", "metrics.json", "equity.csv")
        ]
        if any(not file.resolve().is_relative_to(directory) for file in files):
            raise ValueError("Invalid saved report path")
        fingerprint = tuple((file.stat().st_mtime_ns, file.stat().st_size) for file in files)
        key = (str(directory), fingerprint)
        with self.lock:
            if key in self.report_cache:
                return self.report_cache[key]
        body = saved_comparison_html(directory).encode("utf-8")
        with self.lock:
            if len(self.report_cache) >= 8:
                self.report_cache.pop(next(iter(self.report_cache)))
            self.report_cache[key] = body
        return body

    def heartbeat(self):
        while not self.closed:
            with self.lock:
                if self.paper_heartbeat and self.paper is not None and self.paper.poll() is None:
                    try:
                        self.paper_heartbeat.touch()
                    except OSError:
                        pass  # A missing/stale heartbeat makes the child stop itself safely.
            time.sleep(2)

    def stop_orphans(self):
        """Called only after owning the controller OS lock; never kill a PID."""
        for ready in self.runtime.glob("paper-*.json"):
            (ready.with_suffix(".stop")).touch()

    def ollama(self):
        try:
            response = requests.get(OLLAMA + "/api/tags", timeout=2)
            response.raise_for_status()
            models = response.json()["models"]
            return {"ok": True, "error": None}, [
                {"name": item["name"], "size": item.get("size", 0)}
                for item in models
                if isinstance(item.get("name"), str)
            ]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Το Ollama δεν απαντά. Πατήστε Εκκίνηση Ollama."}, []

    def results(self):
        folder = self.root / "research-runs"
        paths = list(folder.glob("*/report.html")) + list(folder.glob("*/comparison/report.html"))
        rows = []
        for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            relative = path.relative_to(folder)
            if re.fullmatch(r"[a-zA-Z0-9_-]+", relative.parts[0]):
                label = relative.parts[0]
                try:
                    details = json.loads(
                        (folder / relative.parts[0] / "desktop-label.json").read_text(encoding="utf-8")
                    )
                    label = str(details["label"])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                rows.append(
                    {
                        "id": relative.parts[0],
                        "label": label,
                        "url": "/reports/" + relative.parts[0],
                    }
                )
        return rows

    def status(self):
        ollama, models = self.ollama()
        with self.lock:
            running = self.paper is not None and self.paper.poll() is None
            url = None
            if running and self.paper_info and self.paper_info.exists():
                try:
                    info = json.loads(self.paper_info.read_text(encoding="utf-8"))
                    port = int(info["port"])
                    if 0 < port <= 65535 and info["pid"] == self.paper.pid:
                        url = f"http://127.0.0.1:{port}"
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if self.paper is not None and not running and self.paper.returncode:
                self.paper_error = (
                    "Το paper bot σταμάτησε με σφάλμα. Δείτε το αρχείο καταγραφής ή κλείστε άλλο ενεργό bot."
                )
            today = datetime.now(timezone.utc).date()
            try:
                log_path = self.runtime / "paper.log"
                with log_path.open("rb") as stream:
                    stream.seek(max(0, log_path.stat().st_size - 12000))
                    paper_logs = stream.read().decode("utf-8", errors="replace").splitlines()[-30:]
            except OSError:
                paper_logs = []
            return {
                "paper": {"running": running, "url": url, "error": self.paper_error, "logs": paper_logs},
                "job": {**copy.deepcopy(self.job), "logs": list(self.logs)},
                "models": models,
                "ollama": ollama,
                "results": self.results(),
                "defaults": {
                    "year": today.year - 1,
                    "start": str(today - timedelta(days=7)),
                    "end": str(today),
                },
            }

    def paper_start(self, payload):
        _, controls = parse_settings(payload)
        model = payload.get("model") or "qwen2.5:14b"
        _, models = self.ollama()
        if model not in [item["name"] for item in models]:
            raise ValueError("Ανοίξτε το Ollama και επιλέξτε ένα εγκατεστημένο μοντέλο.")
        with self.lock:
            if self.paper is not None and self.paper.poll() is None:
                raise ValueError("Το paper bot λειτουργεί ήδη.")
            ident = uuid.uuid4().hex
            self.paper_stop = self.runtime / f"paper-{ident}.stop"
            self.paper_info = self.runtime / f"paper-{ident}.json"
            self.paper_heartbeat = self.runtime / f"paper-{ident}.heartbeat"
            self.paper_heartbeat.touch()
            log_path = self.runtime / "paper.log"
            self.paper_error = None
            with log_path.open("w", encoding="utf-8") as stream:
                self.paper = hidden_process(
                    [
                        sys.executable,
                        "-u",
                        self.root / "quant_bot.py",
                        "--no-browser",
                        "--control-stop-file",
                        self.paper_stop,
                        "--control-ready-file",
                        self.paper_info,
                        "--control-heartbeat-file",
                        self.paper_heartbeat,
                        "--model",
                        model,
                        *controls,
                    ],
                    cwd=self.root,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )

    def paper_stop_request(self):
        with self.lock:
            if self.paper is not None and self.paper.poll() is None:
                self.paper_stop.touch()

    def log(self, message):
        with self.lock:
            self.logs.append(str(message)[-1500:])

    def stage(self, message):
        with self.lock:
            self.job["stage"] = message
        self.log(message)

    def command(self, *args):
        if self.cancel.is_set():
            raise RuntimeError("Ακυρώθηκε από τον χρήστη.")
        with self.lock:
            if self.cancel.is_set():
                raise RuntimeError("Ακυρώθηκε από τον χρήστη.")
            process = hidden_process(
                [sys.executable, "-u", "-m", "quantbot.research", *map(str, args)],
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONUTF8": "1"},
            )
            self.job_process = process
        with (self.runtime / f"{self.job['id']}.log").open("a", encoding="utf-8") as stream:
            for line in process.stdout:
                stream.write(line)
                stream.flush()
                try:
                    progress = json.loads(line)
                    if isinstance(progress, dict) and progress.get("type") == "quantbot.progress":
                        label = "Λήψη τιμών" if progress.get("phase") == "fetch" else "Backtest"
                        candidate = progress.get("candidate", progress.get("advisor", ""))
                        self.stage(
                            f"{label} {candidate}: {progress['completed']:,}/{progress['total']:,} ({progress['percent']:.0f}%)"
                        )
                    else:
                        self.log(line.rstrip())
                except (ValueError, KeyError, TypeError):
                    self.log(line.rstrip())
        code = process.wait()
        with self.lock:
            self.job_process = None
        if self.cancel.is_set():
            raise RuntimeError("Ακυρώθηκε από τον χρήστη. Τα ολοκληρωμένα δεδομένα διατηρήθηκαν.")
        if code:
            raise RuntimeError("Η εργασία απέτυχε. Το ακριβές μήνυμα βρίσκεται στην καταγραφή παρακάτω.")

    def research_job(self, payload, ident):
        output = self.root / "research-runs" / ident
        if payload["action"] == "demo":
            self.stage("Επίδειξη με τεχνητές τιμές")
            self.command("demo", "--hours", "24", "--output", output)
        else:
            start, end, interval = parse_range(payload)
            config, controls = parse_settings(payload)
            model = payload.get("model", "")
            experiments = payload.get("strategy_experiments", False)
            if type(experiments) is not bool:
                raise ValueError("Η σύγκριση παραλλαγών πρέπει να είναι ενεργή ή ανενεργή.")
            if model:
                if interval != "1m":
                    raise ValueError(
                        "Για AI επιλέξτε ανάλυση 1 λεπτού ώστε οι αποφάσεις να ελέγχονται πριν λήξουν."
                    )
                _, models = self.ollama()
                if model not in [item["name"] for item in models]:
                    raise ValueError("Το επιλεγμένο μοντέλο δεν είναι διαθέσιμο στο Ollama.")
            dataset = self.root / "research-datasets" / f"{start:%Y%m%d}-{end:%Y%m%d}-{interval}"
            hourly, execution = dataset / "hourly", dataset / "execution"
            for resolution, since, target in (
                ("1h", start - timedelta(hours=600), hourly),
                (interval, start, execution),
            ):
                if target.exists():
                    self.stage(f"Έλεγχος αποθηκευμένων τιμών {resolution}")
                    self.command("validate", target)
                else:
                    self.stage(f"Λήψη τιμών {resolution}: {since:%Y-%m-%d} έως {end:%Y-%m-%d}")
                    self.command(
                        "fetch",
                        "--since",
                        since.isoformat(),
                        "--until",
                        end.isoformat(),
                        "--interval",
                        resolution,
                        "--output",
                        target,
                    )
            self.stage("Συνεχές backtest — οι θέσεις διατηρούνται σε όλη την περίοδο")
            args = [
                "compare",
                "--data",
                hourly,
                "--execution-data",
                execution,
                "--output",
                output,
                "--starting-cash",
                str(config.starting_cash),
                "--baselines",
                "rules,numeric",
                "--compact",
                *controls,
            ]
            if model:
                args += ["--model", model, "--cache", self.root / "research-cache"]
            if experiments:
                args += ["--strategy-experiments"]
            if interval == "5m":
                # A nonzero latency would wait 5m, exceeding the live 120s limit.
                # This explicit zero-compute-latency assumption is for rules only.
                args += ["--fixed-latency-seconds", "0"]
            self.command(*args)
        label = "Επίδειξη — τεχνητές τιμές"
        if payload["action"] != "demo":
            label = f"{payload['start']} → {payload['end']} · {payload.get('model') or 'χωρίς AI'} · {payload.get('interval', '5m')}"
            if payload.get("strategy_experiments", False):
                label += " · 4 παραλλαγές στρατηγικής"
        atomic_json(output / "desktop-label.json", {"label": label, "settings": payload})
        with self.lock:
            self.job["report_url"] = "/reports/" + ident
        comparison = output / "comparison.json"
        if payload["action"] == "demo":
            comparison = output / "comparison" / "comparison.json"
        summary = json.loads(comparison.read_text(encoding="utf-8"))
        errors = sum(row["metrics"].get("model_errors", 0) for row in summary["candidates"].values())
        if errors:
            with self.lock:
                self.job["error"] = (
                    f"Η αναφορά περιέχει {errors} σφάλματα AI. Δεν είναι καθαρή αξιολόγηση του μοντέλου."
                )
            self.stage("Ολοκληρώθηκε με σφάλματα AI — ελέγξτε την αναφορά")
        else:
            self.stage("Ολοκληρώθηκε — ανοίξτε την αναφορά")

    def pull_model(self, name):
        self.stage(f"Λήψη μοντέλου {name} — δεν αλλάζει αυτόματα το paper bot")
        with requests.post(
            OLLAMA + "/api/pull", json={"model": name, "stream": True}, stream=True, timeout=(5, 30)
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if self.cancel.is_set():
                    raise RuntimeError("Ακυρώθηκε η αναμονή λήψης. Ελέγξτε την κατάσταση στο Ollama.")
                if line:
                    data = json.loads(line)
                    if data.get("error"):
                        raise ValueError(data["error"])
                    text = data.get("status", "Λήψη")
                    if data.get("total"):
                        text += f" — {100 * data.get('completed', 0) / data['total']:.0f}% του αρχείου"
                    self.stage(text)

    def start_job(self, payload):
        if payload["action"] == "backtest":
            _, _, interval = parse_range(payload)
            parse_settings(payload)
            if type(payload.get("strategy_experiments", False)) is not bool:
                raise ValueError("Η σύγκριση παραλλαγών πρέπει να είναι ενεργή ή ανενεργή.")
            model = payload.get("model", "")
            if not isinstance(model, str) or len(model) > 200:
                raise ValueError("Μη έγκυρο μοντέλο.")
            if model and interval != "1m":
                raise ValueError("Η δοκιμή με AI απαιτεί ανάλυση 1 λεπτού. Αλλάξτε την επιλογή ανάλυσης.")
        if payload["action"] == "model_pull":
            name = payload.get("name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,150}", name):
                raise ValueError("Γράψτε το ακριβές όνομα Ollama του μοντέλου.")
        with self.lock:
            if self.closed or self.job["running"]:
                raise ValueError("Περιμένετε να ολοκληρωθεί ή ακυρώστε την τρέχουσα εργασία.")
            ident = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            self.cancel.clear()
            self.logs.clear()
            self.job = {
                "running": True,
                "id": ident,
                "stage": "Προετοιμασία",
                "error": None,
                "report_url": None,
            }

        def work():
            try:
                if payload["action"] == "model_pull":
                    self.pull_model(payload["name"])
                    self.stage("Το μοντέλο είναι διαθέσιμο για επιλογή")
                else:
                    self.research_job(payload, ident)
            except Exception as exc:
                with self.lock:
                    self.job["error"] = str(exc)
                    self.job["stage"] = "Ακυρώθηκε" if self.cancel.is_set() else "Αποτυχία"
                incomplete = self.root / "research-runs" / ident
                if incomplete.is_dir() and not (incomplete / "report.html").exists():
                    atomic_json(
                        incomplete / "FAILED.json",
                        {"complete": False, "cancelled": self.cancel.is_set(), "error": str(exc)},
                    )
                self.log(str(exc))
            finally:
                with self.lock:
                    self.job["running"] = False

        threading.Thread(target=work, daemon=True, name="quantbot-desktop-job").start()

    def cancel_job(self):
        with self.lock:
            self.cancel.set()
            if self.job_process is not None and self.job_process.poll() is None:
                if os.name == "nt":
                    # Windows venv executables can be redirectors with a child
                    # interpreter. Cancel only this owned research process tree.
                    subprocess.run(
                        ["taskkill", "/PID", str(self.job_process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    self.job_process.terminate()

    def action(self, payload):
        action = payload.get("action")
        if action in ("demo", "backtest", "model_pull"):
            self.start_job(payload)
        elif action == "paper_start":
            self.paper_start(payload)
        elif action == "paper_stop":
            self.paper_stop_request()
        elif action == "cancel":
            self.cancel_job()
        elif action == "ollama_start":
            if self.ollama()[0]["ok"]:
                return
            executable = shutil.which("ollama")
            if not executable:
                candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
                if candidate.is_file():
                    executable = str(candidate)
            if not executable:
                raise ValueError("Δεν βρέθηκε Ollama. Εγκαταστήστε το από ollama.com και ξαναδοκιμάστε.")
            with (self.runtime / "ollama.log").open("a", encoding="utf-8") as stream:
                hidden_process([executable, "serve"], cwd=self.root, stdout=stream, stderr=stream)
        elif action == "shutdown":
            threading.Thread(target=self.close, daemon=True).start()
        else:
            raise ValueError("Άγνωστη ενέργεια.")

    def stop_children(self):
        self.closed = True
        self.cancel_job()
        self.paper_stop_request()
        if self.paper is not None and self.paper.poll() is None:
            try:
                self.paper.wait(timeout=45)
            except subprocess.TimeoutExpired:
                # Do not force-kill a paper account while it may be committing.
                self.log("Το paper bot ακόμη τερματίζει. Περιμένετε πριν ανοίξετε νέα εφαρμογή.")

    def close(self):
        self.stop_children()
        if self.server:
            self.server.shutdown()


def make_server(control, port=0):
    html = (Path(__file__).parent / "web" / "control.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, kind="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def local(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_address[1]}"

        def do_GET(self):
            if not self.local():
                self.send(403, b"{}")
                return
            if self.path == "/":
                self.send(200, html.replace("__TOKEN__", control.token).encode(), "text/html; charset=utf-8")
            elif self.path == "/favicon.ico":
                self.send(204, b"", "image/x-icon")
            elif self.path == "/api/health":
                self.send(200, json.dumps({"app": "QuantBot", "workspace": control.workspace}).encode())
            elif self.path == "/api/status":
                self.send(200, json.dumps(control.status(), ensure_ascii=False, allow_nan=False).encode())
            elif re.fullmatch(r"/reports/[a-zA-Z0-9_-]+(?:/comparison.json|/)?", self.path):
                ident = self.path.split("/")[2]
                folder = control.root / "research-runs" / ident
                path = folder / "report.html"
                if not path.exists():
                    path = folder / "comparison" / "report.html"
                if self.path.endswith("comparison.json"):
                    path = path.with_name("comparison.json")
                if path.is_file() and path.resolve().is_relative_to(
                    (control.root / "research-runs").resolve()
                ):
                    if self.path == f"/reports/{ident}":
                        self.send_response(302)
                        self.send_header("Location", self.path + "/")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        kind = (
                            "application/json; charset=utf-8"
                            if path.suffix == ".json"
                            else "text/html; charset=utf-8"
                        )
                        if path.suffix == ".html" and path.with_name("comparison.json").is_file():
                            try:
                                body = control.report_html(path.parent)
                            except (OSError, ValueError, KeyError, TypeError) as exc:
                                self.send(
                                    503,
                                    json.dumps(
                                        {
                                            "error": f"Δεν ήταν δυνατή η ανάγνωση των αποθηκευμένων αποτελεσμάτων: {exc}"
                                        },
                                        ensure_ascii=False,
                                    ).encode(),
                                )
                                return
                        else:
                            body = path.read_bytes()
                        self.send(200, body, kind)
                else:
                    self.send(404, b"{}")
            else:
                self.send(404, b"{}")

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_address[1]}"
            if (
                not self.local()
                or self.headers.get("Origin") != origin
                or not secrets.compare_digest(self.headers.get("X-QuantBot-Token", ""), control.token)
            ):
                # Drain a small declared body before closing. Otherwise Windows
                # can reset the socket and hide the 403 behind a connection error.
                # Never parse or execute unauthenticated data; bound size and wait.
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 16_384:
                        self.connection.settimeout(2)
                        self.rfile.read(length)
                except (OSError, ValueError):
                    pass
                self.send(403, b"{}")
                return
            if self.path != "/api/action":
                self.send(404, b"{}")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Μη έγκυρο αίτημα.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Μη έγκυρο αίτημα.")
                control.action(payload)
                self.send(200, b'{"ok":true}')
            except (ValueError, TypeError, OSError, KeyError) as exc:
                self.send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    control.server = server
    return server


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    control = Control()
    # OS lock is released even after a crash; the record alone is never a lock.
    lock = (control.runtime / "desktop.lock").open("a+b")
    try:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.stop_orphans()
        threading.Thread(target=control.heartbeat, daemon=True, name="quantbot-controller-heartbeat").start()
        server = make_server(control)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        if args.ready_file:
            atomic_json(args.ready_file, {"url": url, "workspace": control.workspace})
        print(url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        finally:
            control.stop_children()
            server.server_close()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
