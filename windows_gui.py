"""Visible first-run setup followed by the browser control centre (no terminal input)."""

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def existing_url(record):
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        url = data["url"]
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port:
            return None
        expected = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()
        with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
            health = json.load(response)
        if health == {"app": "QuantBot", "workspace": expected}:
            return url
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("QuantBot — Προετοιμασία")
    window.geometry("660x400")
    window.minsize(560, 300)
    ttk.Label(window, text="QuantBot", font=("Segoe UI", 22, "bold")).pack(pady=(18, 4))
    label = ttk.Label(window, text="Προετοιμασία εφαρμογής. Δεν χρειάζεται να γράψετε εντολές.")
    label.pack(pady=5)
    progress = ttk.Progressbar(window, mode="indeterminate")
    progress.pack(fill="x", padx=20, pady=10)
    progress.start()
    logs = tk.Text(window, height=10, wrap="word", font=("Consolas", 9))
    logs.pack(fill="both", expand=True, padx=20, pady=10)
    messages = queue.Queue()
    runtime = ROOT / ".quantbot-desktop"

    def command(args):
        process = subprocess.Popen(
            [str(arg) for arg in args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        for line in process.stdout:
            messages.put(("log", line))
        if process.wait():
            raise RuntimeError("Η προετοιμασία απέτυχε. Δείτε το μήνυμα παραπάνω.")

    def setup():
        try:
            runtime.mkdir(exist_ok=True)
            record = runtime / "desktop.json"
            url = existing_url(record)
            if url:
                messages.put(("ready", url))
                return
            if sys.version_info < (3, 10):
                raise RuntimeError("Χρειάζεται Python 3.10 ή νεότερη.")
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            if not python.exists():
                messages.put(("log", "Δημιουργία τοπικού περιβάλλοντος Python...\n"))
                command([sys.executable, "-m", "venv", ROOT / ".venv"])
            check = subprocess.run(
                [str(python), "-c", "import ccxt, pandas, requests"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if check.returncode:
                messages.put(("log", "Εγκατάσταση βιβλιοθηκών — χρειάζεται internet...\n"))
                command([python, "-m", "pip", "install", "-r", ROOT / "requirements.txt"])
            messages.put(("log", "Εκκίνηση κέντρου ελέγχου...\n"))
            with (runtime / "desktop.log").open("w", encoding="utf-8") as stream:
                process = subprocess.Popen(
                    [
                        str(python),
                        "-u",
                        "-m",
                        "quantbot.control",
                        "--no-browser",
                        "--ready-file",
                        str(record),
                    ],
                    cwd=ROOT,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                url = existing_url(record)
                if url:
                    messages.put(("ready", url))
                    return
                if process.poll() is not None:
                    raise RuntimeError((runtime / "desktop.log").read_text(encoding="utf-8")[-6000:])
                time.sleep(0.25)
            raise RuntimeError("Το κέντρο ελέγχου δεν απάντησε. Ξανανοίξτε την εφαρμογή.")
        except Exception:
            messages.put(("error", traceback.format_exc()))

    def update():
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "ready":
                    progress.stop()
                    label.configure(text="Έτοιμο! Το κέντρο ελέγχου ανοίγει στον browser.")
                    webbrowser.open(value)
                    ttk.Button(window, text="Άνοιγμα εφαρμογής", command=lambda: webbrowser.open(value)).pack(
                        pady=5
                    )
                    window.after(3000, window.destroy)
                    return
                if kind == "error":
                    progress.stop()
                    label.configure(text="Η εκκίνηση δεν ολοκληρώθηκε. Κρατήστε το μήνυμα παρακάτω.")
                logs.insert("end", value + ("\n" if kind == "error" else ""))
                logs.see("end")
        except queue.Empty:
            pass
        window.after(100, update)

    threading.Thread(target=setup, daemon=True).start()
    window.after(100, update)
    window.mainloop()


if __name__ == "__main__":
    main()
