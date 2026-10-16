"""Double-click Windows entry points; bootstrap only a project-local environment."""

import argparse
import subprocess
import sys
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def prepare(mode):
    if sys.version_info < (3, 10):
        raise RuntimeError("Απαιτείται Python 3.10 ή νεότερη.")
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        print("Πρώτη εκκίνηση: προετοιμασία Python...", flush=True)
        run(sys.executable, "-m", "venv", ROOT / ".venv")
    if Path(sys.executable).resolve() != python.resolve():
        run(python, __file__, mode)
        return False
    # Check installed requirements rather than assuming an existing venv is ready.
    check = subprocess.run(
        [str(python), "-c", "import ccxt, pandas, requests"],
        cwd=ROOT,
        capture_output=True,
    )
    if check.returncode:
        print("Εγκατάσταση απαραίτητων βιβλιοθηκών. Χρειάζεται internet...", flush=True)
        run(python, "-m", "pip", "install", "-r", ROOT / "requirements.txt")
    return True


def research(*args):
    run(sys.executable, "-m", "quantbot.research", *args)


def output_dir(label):
    return ROOT / "research-runs" / (f"{label}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}")


def open_report(path):
    print(f"\nΗ αναφορά αποθηκεύτηκε εδώ:\n{path}", flush=True)
    webbrowser.open(path.resolve().as_uri())


def demo():
    output = output_dir("demo")
    research("demo", "--output", output, "--hours", "24")
    open_report(output / "comparison" / "report.html")


def dates():
    today = datetime.now(timezone.utc).date()
    default_start = today - timedelta(days=7)
    print("Ημερομηνίες UTC. Η ημερομηνία λήξης δεν περιλαμβάνεται.")
    start = input(f"Από [Enter για {default_start}]: ").strip() or str(default_start)
    end = input(f"Έως [Enter για {today}]: ").strip() or str(today)
    start = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if not start < end or end.date() > today:
        raise ValueError("Η αρχή πρέπει να είναι πριν από τη λήξη και η λήξη μέχρι σήμερα.")
    return start, end


def backtest():
    print("\n1. Επίδειξη με τεχνητές τιμές (χωρίς Ollama ή λήψη τιμών)")
    print("2. Πραγματικό ιστορικό: σύγκριση χωρίς AI")
    print("3. Ίδια σύγκριση και με το υπάρχον Qwen2.5 14B")
    print("0. Έξοδος")
    choice = input("Επιλογή: ").strip()
    if choice == "0":
        return
    if choice == "1":
        demo()
        return
    if choice not in ("2", "3"):
        raise ValueError("Επιλέξτε 0, 1, 2 ή 3.")
    if choice == "3":
        print("Το Ollama πρέπει να λειτουργεί με εγκατεστημένο qwen2.5:14b.")
        print("Το πείραμα με AI μπορεί να διαρκέσει αρκετά. Δεν κατεβάζει νέο μοντέλο.")
    print("Προτείνεται αρχικά 7 ημέρες. Δεν αρκούν για συμπέρασμα κερδοφορίας.")
    start, end = dates()
    dataset = ROOT / "research-datasets" / f"{start:%Y%m%d}-{end:%Y%m%d}"
    hourly, minute = dataset / "hourly", dataset / "minute"
    for interval, since, target in (
        ("1h", start - timedelta(hours=600), hourly),
        ("1m", start, minute),
    ):
        if target.exists():
            print(f"Έλεγχος αποθηκευμένων δεδομένων: {target}", flush=True)
            research("validate", target)
        else:
            print(f"Λήψη δημόσιων τιμών {interval} από Binance...", flush=True)
            research(
                "fetch",
                "--since",
                since.isoformat(),
                "--until",
                end.isoformat(),
                "--interval",
                interval,
                "--output",
                target,
            )
    output = output_dir("qwen" if choice == "3" else "baselines")
    args = ["compare", "--data", hourly, "--execution-data", minute, "--output", output]
    if choice == "3":
        args += ["--model", "qwen2.5:14b", "--cache", ROOT / "research-cache"]
    print("Εκτέλεση ιστορικής δοκιμής. Κρατήστε το παράθυρο ανοιχτό...", flush=True)
    research(*args)
    open_report(output / "report.html")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("ui", "dashboard", "backtest", "demo", "check"))
    args = parser.parse_args()
    try:
        if args.mode == "ui":
            from windows_gui import main as gui_main

            gui_main()
            return 0
        if not prepare(args.mode):
            return 0
        if args.mode == "dashboard":
            print("Εκκίνηση paper dashboard. Για διακοπή πατήστε Ctrl+C.", flush=True)
            run(sys.executable, ROOT / "quant_bot.py")
        elif args.mode == "backtest":
            backtest()
        elif args.mode == "demo":
            demo()
        else:
            print("Η Python και οι απαραίτητες βιβλιοθήκες είναι έτοιμες.")
        return 0
    except KeyboardInterrupt:
        print("\nΔιακοπή από τον χρήστη.")
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"\nΗ εκτέλεση σταμάτησε: {exc}")
        print("Δείτε το μήνυμα σφάλματος παραπάνω. Δεν διαγράφηκαν παλιά αποτελέσματα.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
