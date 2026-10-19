import csv
import json

import pytest

from quantbot.research.advisors import OllamaAdvisor
from quantbot.research.cli import main
from quantbot.research.data import dataset_from_rows, write_dataset

INTERVAL = 3_600_000
START = 1_700_000_000_000 // INTERVAL * INTERVAL


def _dataset(count=602):
    rows = []
    for index in range(count):
        close = 100 + index * 0.02
        rows.append((START + index * INTERVAL, close, close + 1, close - 1, close, 10 + index % 3))
    return dataset_from_rows(rows)


def _write_data_pair(directory):
    dataset = _dataset()
    candles = directory / "hourly"
    execution = directory / "execution"
    write_dataset(dataset, candles)
    write_dataset(dataset, execution)
    return candles, execution


def test_cli_freeze_validate_help_and_refuse_overwrite(tmp_path, capsys):
    source = tmp_path / "market.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        writer.writerow((START, 100, 102, 99, 101, 5))
        writer.writerow((START + INTERVAL, 101, 103, 100, 102, 6))

    frozen = tmp_path / "frozen"
    assert main(["freeze", "--csv", str(source), "--interval", "1h", "--output", str(frozen)]) == 0
    frozen_summary = json.loads(capsys.readouterr().out)
    assert frozen_summary["row_count"] == 2
    assert (frozen / "candles.csv").exists()
    assert main(["validate", str(frozen)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["sha256"] == frozen_summary["sha256"]

    with pytest.raises(SystemExit) as error:
        main(["freeze", "--csv", str(source), "--interval", "1h", "--output", str(frozen)])
    assert error.value.code == 2
    assert "already exists" in capsys.readouterr().err

    with pytest.raises(SystemExit) as help_result:
        main(["--help"])
    assert help_result.value.code == 0
    assert "offline research" in capsys.readouterr().out


def test_cli_replay_rejects_invalid_end_exclusive_bounds_before_writing(tmp_path):
    data, execution = _write_data_pair(tmp_path)
    output = tmp_path / "invalid-run"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "replay",
                "--data",
                str(data),
                "--execution-data",
                str(execution),
                "--output",
                str(output),
                "--start-index",
                "600",
                "--end-index",
                "600",
                "--publication-delay-seconds",
                "0",
            ]
        )
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("advisor", ["cash", "rules", "numeric"])
def test_cli_replay_runs_each_baseline_and_exports_a_complete_run(tmp_path, advisor):
    data, execution = _write_data_pair(tmp_path)
    output = tmp_path / "run"
    assert (
        main(
            [
                "replay",
                "--data",
                str(data),
                "--execution-data",
                str(execution),
                "--output",
                str(output),
                "--advisor",
                advisor,
                "--decision-interval",
                "1h",  # This fixture intentionally has hourly-only execution data.
                "--publication-delay-seconds",
                "0",
                "--fixed-latency-seconds",
                "0",
            ]
        )
        == 0
    )
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert run["advisor"]["provider"] == advisor
    assert run["metrics"]["model_errors"] == 0
    assert (output / "artifacts.json").exists()


def test_replay_only_ollama_needs_digest_and_cache_miss_writes_failed_report(tmp_path, monkeypatch):
    data, execution = _write_data_pair(tmp_path)
    cache = tmp_path / "advisor-cache"

    def forbidden_resolve(self):
        raise AssertionError("replay-only must not query or pull a model")

    monkeypatch.setattr(OllamaAdvisor, "resolve_model_digest", forbidden_resolve)
    base = [
        "compare",
        "--decision-interval",
        "1h",
        "--data",
        str(data),
        "--execution-data",
        str(execution),
        "--output",
        str(tmp_path / "comparison-missing-digest"),
        "--baselines",
        "cash",
        "--model",
        "qwen-local",
        "--cache",
        str(cache),
        "--cache-mode",
        "replay-only",
        "--publication-delay-seconds",
        "0",
        "--fixed-latency-seconds",
        "0",
    ]
    with pytest.raises(SystemExit) as missing:
        main(base)
    assert missing.value.code == 2
    assert not (tmp_path / "comparison-missing-digest").exists()

    output = tmp_path / "comparison-cache-miss"
    command = list(base)
    command[command.index(str(tmp_path / "comparison-missing-digest"))] = str(output)
    command.extend(["--model-digest", "qwen-local=sha256:installed-version"])
    with pytest.raises(SystemExit) as missing_cache:
        main(command)
    assert missing_cache.value.code == 2
    failed = json.loads((output / "FAILED.json").read_text(encoding="utf-8"))
    assert failed["complete"] is False
    assert failed["error"].startswith("CacheMissError:")
    assert not (output / "comparison.json").exists()
    assert not list(tmp_path.rglob("quantbot.sqlite3"))


def test_coarse_execution_requires_explicit_latency_assumption(tmp_path, capsys):
    data, execution = _write_data_pair(tmp_path)
    output = tmp_path / "coarse"
    with pytest.raises(SystemExit) as result:
        main(
            [
                "compare",
                "--data",
                str(data),
                "--execution-data",
                str(execution),
                "--output",
                str(output),
                "--publication-delay-seconds",
                "0",
            ]
        )
    assert result.value.code == 2
    assert "decision freshness limit" in capsys.readouterr().err
    assert not output.exists()


def test_offline_demo_writes_metrics_csv_provenance_and_refuses_overwrite(tmp_path, capsys):
    output = tmp_path / "demo"
    assert main(["demo", "--output", str(output), "--hours", "1"]) == 0
    report_path = output / "comparison"
    report = json.loads((report_path / "comparison.json").read_text(encoding="utf-8"))
    assert report["dataset"]["source"]["type"] == "SYNTHETIC"
    assert set(report["candidates"]) == {"cash", "rules", "numeric"}
    assert "report.html" in capsys.readouterr().out
    assert "Σύγκριση πειραμάτων" in (report_path / "report.html").read_text(encoding="utf-8")
    for candidate in report["candidates"]:
        run_dir = report_path / candidate
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        artifacts = json.loads((run_dir / "artifacts.json").read_text(encoding="utf-8"))
        assert run["dataset"]["sha256"] == report["dataset"]["sha256"]
        assert "total_return" in metrics
        assert "equity.csv" in artifacts
        assert (run_dir / "equity.csv").exists()
    assert not list(output.rglob("quantbot.sqlite3"))

    with pytest.raises(SystemExit) as error:
        main(["demo", "--output", str(output), "--hours", "1"])
    assert error.value.code == 2
