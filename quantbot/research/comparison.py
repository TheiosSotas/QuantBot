"""Independent accounts and shared evaluation conditions for every challenger."""

from pathlib import Path

from .metrics import summarize
from .replay import run_replay
from .reports import comparison_html, export_run, provenance, write_json


def hold_reference(dataset, execution_data, start, end, starting_cash):
    """1x buy/hold reference, without strategy stops; not risk-matched."""
    from .data import validate_execution_data

    bars = validate_execution_data(dataset, execution_data or dataset, start, end)
    units = starting_cash / bars[0].open
    peak = starting_cash
    rows = [
        {
            "observed_at": bars[0].timestamp / 1000,
            "equity": starting_cash,
            "exposure": 1.0,
            "peak_equity": starting_cash,
            "drawdown": 0.0,
        }
    ]
    for bar in bars:
        equity = units * bar.close
        peak = max(peak, equity)
        rows.append(
            {
                "observed_at": (bar.timestamp + (execution_data or dataset).interval_ms) / 1000,
                "equity": equity,
                "exposure": 1.0,
                "peak_equity": peak,
                "drawdown": max(0, (peak - equity) / peak),
            }
        )
    return {
        "metrics": summarize(rows, [], starting_cash=starting_cash),
        "assumptions": "1x buy/hold at first execution open; marked at execution closes; no stops; not risk-matched",
    }


def compare(dataset, advisors, *, config, execution_data=None, output=None, **options):
    if not isinstance(advisors, dict) or not advisors:
        raise ValueError("Provide a nonempty mapping of named advisors")
    for name in advisors:
        if (
            not isinstance(name, str)
            or not name
            or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in name
            )
        ):
            raise ValueError("Candidate names must use only ASCII letters, numbers, '_' and '-'")
    target = Path(output) if output is not None else None
    compact = options.pop("compact", False)
    progress_callback = options.pop("progress_callback", None)
    if compact and target is None:
        raise ValueError("Compact comparisons require an output directory for their audit journal")
    if target is not None:
        target.mkdir(parents=True, exist_ok=False)
    results = {}
    try:
        for name, advisor in advisors.items():
            run_options = dict(options)
            run_options["compact"] = compact
            if compact:
                run_options["compact_dir"] = target / ".compact" / name
            if progress_callback is not None:
                run_options["progress_callback"] = lambda event, candidate=name: progress_callback(
                    {"advisor": candidate, **event}
                )
            result = run_replay(dataset, advisor, config=config, execution_data=execution_data, **run_options)
            results[name] = result
            if target is not None:
                export_run(result, target / name)
        first = next(iter(results.values()))
        report = {
            "schema_version": 1,
            "provenance": provenance(),
            "dataset": dataset.manifest(),
            "evaluation": {
                "start_index": first["start_index"],
                "end_index": first["end_index"],
                "assumptions": first["assumptions"],
            },
            "candidates": {
                name: {"identity": result["advisor"], "metrics": result["metrics"]}
                for name, result in results.items()
            },
            "buy_and_hold_reference": hold_reference(
                dataset, execution_data, first["start_index"], first["end_index"], config.starting_cash
            ),
            "automatic_promotion": False,
            "historical_pretraining_contamination_excluded": False,
        }
        if target is not None:
            write_json(target / "comparison.json", report)
            with (target / "report.html").open("x", encoding="utf-8") as stream:
                stream.write(comparison_html(results, reference=report["buy_and_hold_reference"]))
            compact_root = target / ".compact"
            if compact_root.exists() and not any(compact_root.iterdir()):
                compact_root.rmdir()
        return report
    except Exception as exc:
        if target is not None:
            write_json(
                target / "FAILED.json",
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "completed_candidates": list(results),
                    "complete": False,
                    "audit_spools": [str(path) for path in (target / ".compact").glob("*")]
                    if (target / ".compact").exists()
                    else [],
                },
            )
        raise
