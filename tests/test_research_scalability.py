"""Long-horizon, compact-replay and progress regressions."""

import csv
import json
import math

from quantbot.config import Config
from quantbot.models import Decision
from quantbot.research.advisors import Advice
from quantbot.research.data import Bar, dataset_from_rows
from quantbot.research.memory import CompactMemoryStore
from quantbot.research.metrics import summarize
from quantbot.research.replay import _summarize_compact, run_replay
from quantbot.research.reports import export_run
from quantbot.research.synthetic import demo_datasets


class _HoldAdvisor:
    def identity(self):
        return {"provider": "test-hold"}

    def advise(self, context):
        return Advice(Decision("HOLD", 0, False, "CHOPPY_RANGE", "scalability fixture"), 0)


class _BuyAdvisor:
    def identity(self):
        return {"provider": "test-buy"}

    def advise(self, context):
        return Advice(Decision("BUY", 1, False, "TRENDING_BULL", "continuous position fixture"), 0)


class _RoundTripAdvisor(_BuyAdvisor):
    def advise(self, context):
        action = "BUY" if context.portfolio["side"] == "FLAT" else "CLOSE"
        return Advice(Decision(action, 1, False, "TRENDING_BULL", "round trip fixture"), 0)


def test_compact_metrics_match_with_negative_equity_wins_losses_and_rejections(tmp_path):
    store = CompactMemoryStore(Config(), tmp_path / "events.jsonl")
    equities = []
    for index, value in enumerate((10000, 11000, 9000, -500, 11000)):
        row = {
            "observed_at": 1700000000 + index * 86400,
            "equity": value,
            "peak_equity": 11000,
            "drawdown": 0,
            "exposure": index / 2,
            "unrealized_pnl": value - 10000,
        }
        equities.append(row)
        store.append_event("EQUITY", row)
    trades = [{"pnl": value} for value in (300, -200, -100, 0, 400)]
    for trade in trades:
        store.append_event("CLOSE", trade)
    store.append_event("DECISION_AUDIT", {"status": "REJECTED"})
    store.close()
    metrics, curve, rejects, count = _summarize_compact(store, 10000)
    assert metrics == summarize(equities, trades, starting_cash=10000)
    assert metrics["max_drawdown"] > 1
    assert rejects == 1 and count == len(equities)
    assert curve[0] == equities[0] and curve[-1] == equities[-1]


def test_compact_keeps_real_engine_round_trip_and_audit(tmp_path):
    hourly, _ = demo_datasets(warmup=606, hours=3)
    options = dict(config=Config(), start_index=606, publication_delay_seconds=0, fixed_latency_seconds=0)
    full = run_replay(hourly, _RoundTripAdvisor(), **options)
    compact = run_replay(hourly, _RoundTripAdvisor(), compact=True, compact_dir=tmp_path / "spool", **options)
    assert full["metrics"]["trade_count"] >= 1
    assert compact["metrics"] == full["metrics"]
    assert compact["final_state"] == full["final_state"]
    output = export_run(compact, tmp_path / "output")
    assert json.loads((output / "trades.json").read_text(encoding="utf-8")) == full["trades"]
    audits = [
        json.loads(line) for line in (output / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audits == full["decisions"]


def _year_dataset():
    warmup, _ = demo_datasets(warmup=606, hours=1)
    rows = list(warmup.rows)
    year_hours = 365 * 24
    anchor = rows[-1].close
    for offset in range(1, year_hours):
        previous = rows[-1].close
        close = anchor + 0.5 * math.sin(2 * math.pi * offset / year_hours)
        rows.append(
            Bar(
                rows[-1].timestamp + 3_600_000,
                previous,
                max(previous, close) + 0.01,
                min(previous, close) - 0.01,
                close,
                1.0,
            )
        )
    return dataset_from_rows(rows, source={"type": "SYNTHETIC", "purpose": "continuous annual replay test"})


def _read_equity(path):
    with path.open("r", newline="", encoding="utf-8") as stream:
        fields = ("observed_at", "equity", "exposure", "peak_equity", "drawdown", "unrealized_pnl")
        return [{key: float(row[key]) for key in fields} for row in csv.DictReader(stream)]


def test_compact_metrics_and_export_match_the_full_curve_on_a_short_run(tmp_path):
    hourly, _ = demo_datasets(warmup=606, hours=3)
    config = Config(data_dir=tmp_path / "paper")
    baseline = run_replay(
        hourly,
        _HoldAdvisor(),
        config=config,
        start_index=606,
        end_index=len(hourly.rows),
        fixed_latency_seconds=0,
        publication_delay_seconds=0,
    )
    compact = run_replay(
        hourly,
        _HoldAdvisor(),
        config=config,
        start_index=606,
        end_index=len(hourly.rows),
        fixed_latency_seconds=0,
        publication_delay_seconds=0,
        compact=True,
        compact_dir=tmp_path / "compact-spool",
    )

    assert compact["metrics"] == baseline["metrics"]
    assert compact["final_state"] == baseline["final_state"]
    assert compact["events"] == compact["decisions"] == compact["advice"] == []
    assert compact["compact_artifacts"]["events"]

    output = export_run(compact, tmp_path / "compact-output")
    assert not (tmp_path / "compact-spool").exists()
    assert len(_read_equity(output / "equity.csv")) == compact["metrics"]["equity_samples"]
    assert len((output / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert len((output / "advice.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert json.loads((output / "trades.json").read_text(encoding="utf-8")) == []


def test_compact_annual_run_keeps_one_position_and_full_drawdown_history(tmp_path):
    dataset = _year_dataset()
    progress = []
    compact_dir = tmp_path / "annual-spool"
    result = run_replay(
        dataset,
        _BuyAdvisor(),
        config=Config(data_dir=tmp_path / "paper"),
        start_index=606,
        end_index=len(dataset.rows),
        fixed_latency_seconds=0,
        publication_delay_seconds=0,
        compact=True,
        compact_dir=compact_dir,
        progress_callback=progress.append,
    )

    assert result["end_index"] - result["start_index"] == 365 * 24
    assert result["final_state"]["portfolio"]["side"] == "LONG"
    assert result["metrics"]["trade_count"] == 0
    assert result["metrics"]["equity_samples"] > 365 * 24
    assert result["metrics"]["max_drawdown"] > 0
    assert result["metrics"]["ending_equity"] < result["final_state"]["portfolio"]["total_equity"] + 1e-8
    assert progress[0]["percent"] == 0
    assert progress[-1]["percent"] == 100
    assert all(left["completed"] <= right["completed"] for left, right in zip(progress, progress[1:]))

    output = export_run(result, tmp_path / "annual-output")
    full_curve = _read_equity(output / "equity.csv")
    exact = summarize(full_curve, [], starting_cash=Config().starting_cash)
    for key, value in exact.items():
        assert result["metrics"][key] == value
    assert len(full_curve) == result["metrics"]["equity_samples"]
    assert (
        len((output / "decisions.jsonl").read_text(encoding="utf-8").splitlines())
        == result["metrics"]["advice_count"]
    )
