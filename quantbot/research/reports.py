"""Portable local experiment evidence, without tracking-server dependencies."""

import csv
import hashlib
import json
import platform
import shutil
import subprocess
from dataclasses import asdict, is_dataclass
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


def comparison_html(results, *, reference=None):
    from .presentation import comparison_html as render

    return render(results, reference=reference)
