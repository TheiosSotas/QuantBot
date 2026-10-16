"""Portable local experiment evidence, without tracking-server dependencies."""

import csv
import hashlib
import html
import json
import platform
import shutil
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2, default=_json_default)
        stream.write("\n")


def provenance():
    package = Path(__file__).resolve().parents[1]
    source = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        source.update(path.relative_to(package).as_posix().encode())
        source.update(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(package.parent), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(package.parent), "status", "--porcelain"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    packages = {}
    for name in ("pandas", "requests", "ccxt"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "git_commit": commit,
        "working_tree_dirty": dirty,
        "source_sha256": source.hexdigest(),
        "python": platform.python_version(),
        "packages": packages,
    }


def export_run(result, output):
    output = Path(output)
    compact = result.get("compact_artifacts")
    compact_directory = Path(compact["directory"]) if compact else None
    if output.exists():
        allowed = output / ".compact"
        if (
            compact_directory is None
            or not compact_directory.is_relative_to(allowed)
            or any(child != allowed for child in output.iterdir())
        ):
            raise FileExistsError(f"Output directory already contains unrelated files: {output}")
    else:
        output.mkdir(parents=True, exist_ok=False)
    small = {
        key: value
        for key, value in result.items()
        if key
        not in (
            "equity",
            "trades",
            "decisions",
            "advice",
            "events",
            "final_state",
            "compact_artifacts",
        )
    }
    write_json(output / "run.json", {**small, "provenance": provenance()})
    write_json(output / "metrics.json", result["metrics"])
    write_json(output / "final-state.json", result["final_state"])
    if compact:
        _export_compact_events(compact_directory, output)
    else:
        with (output / "equity.csv").open("x", newline="", encoding="utf-8") as stream:
            fields = list(result["equity"][0]) if result["equity"] else []
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["equity"])
        for field in ("events", "decisions", "advice"):
            with (output / f"{field}.jsonl").open("x", encoding="utf-8") as stream:
                for row in result[field]:
                    stream.write(
                        json.dumps(row, ensure_ascii=False, allow_nan=False, default=_json_default) + "\n"
                    )
        with (output / "trades.json").open("x", encoding="utf-8") as stream:
            json.dump(result["trades"], stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
    hashes = {path.name: _file_sha256(path) for path in output.iterdir() if path.is_file()}
    write_json(output / "artifacts.json", hashes)
    if compact_directory is not None:
        shutil.rmtree(compact_directory)
    return output


def _file_sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _export_compact_events(compact_directory, output):
    event_source = Path(compact_directory) / "events.jsonl"
    shutil.copyfile(event_source, output / "events.jsonl")
    decision_stream = (output / "decisions.jsonl").open("x", encoding="utf-8")
    advice_stream = (output / "advice.jsonl").open("x", encoding="utf-8")
    trades_stream = (output / "trades.json").open("x", encoding="utf-8")
    equity_stream = (output / "equity.csv").open("x", newline="", encoding="utf-8")
    trade_first = True
    equity_writer = None
    try:
        trades_stream.write("[\n")
        with event_source.open("r", encoding="utf-8") as source:
            for line in source:
                kind, payload = json.loads(line)
                if kind == "DECISION_AUDIT":
                    decision_stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
                elif kind == "ADVICE":
                    advice_stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
                elif kind == "CLOSE":
                    if not trade_first:
                        trades_stream.write(",\n")
                    json.dump(payload, trades_stream, ensure_ascii=False, allow_nan=False, indent=2)
                    trade_first = False
                elif kind == "EQUITY":
                    if equity_writer is None:
                        equity_writer = csv.DictWriter(equity_stream, fieldnames=list(payload))
                        equity_writer.writeheader()
                    equity_writer.writerow(payload)
        trades_stream.write("\n]\n")
    finally:
        decision_stream.close()
        advice_stream.close()
        trades_stream.close()
        equity_stream.close()


def comparison_html(results):
    names, curves = list(results), [r["equity"] for r in results.values()]
    all_rows = [row for rows in curves for row in rows]
    low = min((row["equity"] for row in all_rows), default=0)
    high = max((row["equity"] for row in all_rows), default=1)
    first = min((row["observed_at"] for row in all_rows), default=0)
    last = max((row["observed_at"] for row in all_rows), default=1)
    colors = ("#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#fb7185")
    first_result = next(iter(results.values()))
    origin = first_result["dataset"]["first_timestamp"]
    begin = datetime.fromtimestamp((origin + first_result["start_index"] * 3_600_000) / 1000, timezone.utc)
    finish = datetime.fromtimestamp((origin + first_result["end_index"] * 3_600_000) / 1000, timezone.utc)
    period = f"{begin:%Y-%m-%d %H:%M} έως {finish:%Y-%m-%d %H:%M} UTC (η λήξη δεν περιλαμβάνεται)"
    synthetic = any(
        result["dataset"].get("source", {}).get("type") == "SYNTHETIC" for result in results.values()
    )
    data_note = (
        "<p><strong>ΣΥΝΘΕΤΙΚΑ ΔΕΔΟΜΕΝΑ — έλεγχος λογισμικού, χωρίς πραγματικές τιμές αγοράς.</strong></p>"
        if synthetic
        else ""
    )
    lines, table = [], []
    for index, (name, rows) in enumerate(zip(names, curves)):
        color = colors[index % len(colors)]
        step = max(1, len(rows) // 500)
        sampled = rows[::step]
        if rows and sampled[-1] is not rows[-1]:
            sampled += [rows[-1]]
        points = " ".join(
            f"{20 + (row['observed_at'] - first) / max(last - first, 0.001) * 960:.2f},"
            f"{280 - (row['equity'] - low) / max(high - low, 0.001) * 260:.2f}"
            for row in sampled
        )
        lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>')
        metrics = results[name]["metrics"]
        identity = results[name]["advisor"]
        while identity.get("provider") == "cached-advisor":
            identity = identity["advisor"]
        model = identity.get("model", identity.get("model_id"))
        display_name = f"{name} · {model}" if model else name
        state = "Σφάλματα AI" if metrics["model_errors"] else "Ολοκληρώθηκε"
        table.append(
            f'<tr><td style="color:{color}">{html.escape(display_name)}</td><td>{metrics["total_return"] * 100:.3f}%</td>'
            f"<td>{metrics['max_drawdown'] * 100:.3f}%</td><td>{metrics['trade_count']}</td>"
            f"<td>{metrics['model_errors']}</td><td>{metrics['invalid_or_expired_decisions']}</td><td>{state}</td></tr>"
        )
    return f"""<!doctype html><html lang="el"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuantBot — Σύγκριση πειραμάτων</title><style>body{{background:#0b1020;color:#e2e8f0;font:16px system-ui;max-width:1100px;margin:auto;padding:24px}}section{{background:#121a2e;border:1px solid #26324b;border-radius:12px;padding:20px;margin:20px 0;overflow:auto}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #26324b}}svg{{width:100%;height:300px}}a{{color:#22d3ee}}.note{{color:#b5c0d5}}</style>
<h1>QuantBot — Σύγκριση πειραμάτων</h1>{data_note}<p>{period}</p><p class="note">Μικτή προσομοιωμένη απόδοση πριν από προμήθειες/funding. Κάθε υποψήφιος έχει ανεξάρτητο λογαριασμό. Δεν πρόκειται για πρόβλεψη πραγματικής κερδοφορίας.</p>
<section><h2>Συνολική αξία λογαριασμού</h2><p>${low:,.2f} — ${high:,.2f}. Περιλαμβάνει ανοιχτό PnL. Το γράφημα αραιώνει δείγματα· τα metrics χρησιμοποιούν ολόκληρη την καμπύλη.</p><svg viewBox="0 0 1000 300" role="img" aria-label="Καμπύλες κεφαλαίου">{"".join(lines)}</svg></section>
<section><table><thead><tr><th>Υποψήφιος</th><th>Απόδοση</th><th>Μέγιστη πτώση</th><th>Κλεισίματα</th><th>Σφάλματα AI</th><th>Απορρίψεις αποφάσεων</th><th>Κατάσταση</th></tr></thead><tbody>{"".join(table)}</tbody></table><p class="note">cash: χωρίς συναλλαγές · rules: σταθεροί κανόνες · numeric: κανόνες με αριθμητικό φίλτρο. Οι απορρίψεις περιλαμβάνουν περιορισμούς ρίσκου, ληγμένες αποφάσεις και άλλα φίλτρα· δεν αποτελούν όλες σφάλμα.</p></section>
<p class="note">Οι παραδοχές χρόνου, εκτέλεσης και OHLC διαδρομών βρίσκονται στο run.json κάθε υποψηφίου. Η συντηρητική διαδρομή είναι τοπική εκτίμηση μεταξύ δύο σεναρίων, όχι εγγυημένο καθολικό χειρότερο αποτέλεσμα.</p><p><a href="comparison.json">Αναλυτική σύγκριση JSON</a></p></html>"""
