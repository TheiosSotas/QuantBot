"""Research commands are opt-in and never start the production Runner."""

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from .advisors import CashAdvisor, ChronosAdvisor, NumericAdvisor, OllamaAdvisor, RulesAdvisor
from .cache import CachedAdvisor
from .comparison import compare
from .data import load_csv, load_dataset, write_dataset
from .fetch import fetch_candles
from .replay import run_replay
from .reports import export_run, provenance, write_json
from .synthetic import demo_datasets
from .walkforward import make_folds, run_walk_forward


def _utc(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return int(instant.timestamp() * 1000)
    except (ValueError, OverflowError, OSError) as exc:
        raise argparse.ArgumentTypeError("Use an ISO UTC date/time, e.g. 2024-01-01T00:00:00Z") from exc


def _controls(parser):
    parser.add_argument("--daily-loss-limit", type=float)
    parser.add_argument("--max-account-drawdown", type=float)
    parser.add_argument("--cooldown-seconds", type=float, default=0)
    parser.add_argument("--starting-cash", type=float, default=10000)


def _config(args):
    return Config(
        starting_cash=args.starting_cash,
        daily_loss_limit=args.daily_loss_limit,
        max_account_drawdown=args.max_account_drawdown,
        cooldown_seconds=args.cooldown_seconds,
        ollama_url=args.ollama_url,
        model_timeout=args.model_timeout,
    )


def _run_options(args):
    return {
        "start_index": args.start_index,
        "end_index": args.end_index,
        "publication_delay_seconds": args.publication_delay_seconds,
        "fixed_latency_seconds": args.fixed_latency_seconds,
        "ambiguity_policy": args.ambiguity_policy,
        "compact": args.compact,
        "progress_callback": _progress if args.compact else None,
    }


def _progress(event):
    print(json.dumps({"type": "quantbot.progress", **event}, ensure_ascii=False), file=sys.stderr, flush=True)


def _candidates(args, config, dataset):
    advisors = {}
    baselines = args.baselines.split(",")
    if len(set(baselines)) != len(baselines) or any(
        name not in ("cash", "rules", "numeric") for name in baselines
    ):
        raise ValueError("Choose unique baselines from cash,rules,numeric")
    for name in baselines:
        advisors[name] = {"cash": CashAdvisor, "rules": RulesAdvisor, "numeric": NumericAdvisor}[name]()
    digests = {}
    for entry in args.model_digest:
        name, separator, digest = entry.partition("=")
        if not separator or not name or not digest:
            raise ValueError("Model digest must have form MODEL=DIGEST")
        digests[name] = digest
    if any(name not in args.model for name in digests):
        raise ValueError("Every --model-digest must match a selected --model")
    for index, model in enumerate(args.model):
        if model in args.model[:index]:
            raise ValueError("Duplicate model name")
        advisor = OllamaAdvisor(config, model=model, model_digest=digests.get(model))
        if args.cache_mode == "replay-only" and model not in digests:
            raise ValueError(
                "Cache-only Ollama replay requires --model-digest MODEL=DIGEST from the original run"
            )
        if args.cache_mode != "replay-only":
            resolved = advisor.resolve_model_digest()  # Installed models only; no pull.
            if model in digests and resolved != digests[model]:
                raise ValueError(f"Installed digest does not match the requested version for {model}")
        advisors[f"ollama_{index + 1}"] = advisor
    if args.chronos_revision is not None:
        if re.fullmatch(r"[0-9a-fA-F]{40}", args.chronos_revision) is None:
            raise ValueError("Chronos revision must be an immutable 40-hex commit")
        advisors["chronos"] = ChronosAdvisor(
            revision=args.chronos_revision, device=args.device, allow_download=args.allow_model_download
        )
    if args.cache is not None:
        advisors = {
            name: CachedAdvisor(
                advisor,
                args.cache,
                dataset_hash=dataset.sha256,
                namespace="quantbot-research-v1",
                cache_mode=args.cache_mode,
            )
            for name, advisor in advisors.items()
        }
    elif args.model or args.chronos_revision is not None or args.cache_mode == "replay-only":
        raise ValueError("Model experiments require an explicit --cache directory")
    return advisors


def _experiment_arguments(parser):
    parser.add_argument("--data", type=Path, required=True, help="Frozen 1h dataset directory")
    parser.add_argument("--execution-data", type=Path, help="Validated 1m/5m dataset directory")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--publication-delay-seconds", type=float, default=2)
    parser.add_argument(
        "--fixed-latency-seconds",
        type=float,
        help="Explicit simulated latency override; measured latency still recorded",
    )
    parser.add_argument("--ambiguity-policy", choices=("conservative", "optimistic"), default="conservative")
    parser.add_argument(
        "--compact", action="store_true", help="Stream full audit to disk and bound replay RAM"
    )
    parser.add_argument("--baselines", default="cash,rules,numeric")
    parser.add_argument(
        "--model", action="append", default=[], help="Installed Ollama model; repeat to compare"
    )
    parser.add_argument("--model-digest", action="append", default=[])
    parser.add_argument("--ollama-url", default=Config().ollama_url)
    parser.add_argument("--model-timeout", type=float, default=Config().model_timeout)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--cache-mode", choices=("readwrite", "replay-only"), default="readwrite")
    parser.add_argument("--chronos-revision", help="Optional pinned Chronos-2 weights revision")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument(
        "--allow-model-download", action="store_true", help="Explicitly allow Chronos weight download"
    )
    _controls(parser)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="QuantBot offline research; no live orders or active-account writes"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Validate and freeze a canonical OHLCV CSV")
    freeze.add_argument("--csv", type=Path, required=True)
    freeze.add_argument("--interval", choices=("1h", "1m", "5m"), required=True)
    freeze.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="Verify manifest and candle hashes")
    validate.add_argument("data", type=Path)
    fetch = commands.add_parser("fetch", help="Explicitly fetch fully closed public Binance futures candles")
    fetch.add_argument("--since", type=_utc, required=True)
    fetch.add_argument("--until", type=_utc, required=True)
    fetch.add_argument("--interval", choices=("1h", "1m", "5m"), required=True)
    fetch.add_argument("--output", type=Path, required=True)
    demo = commands.add_parser("demo", help="Entirely offline synthetic-data comparison")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--hours", type=int, default=12)
    replay = commands.add_parser("replay", help="One baseline replay with the existing PaperEngine")
    _experiment_arguments(replay)
    replay.add_argument("--advisor", choices=("cash", "rules", "numeric"), default="rules")
    comparison = commands.add_parser("compare", help="Independent candidate accounts on identical data")
    _experiment_arguments(comparison)
    walk = commands.add_parser("walk-forward", help="Select only on validation, evaluate winner on next test")
    _experiment_arguments(walk)
    walk.add_argument("--train-size", type=int, required=True)
    walk.add_argument("--validation-size", type=int, required=True)
    walk.add_argument("--test-size", type=int, required=True)
    walk.add_argument("--gap", type=int, default=1)
    walk.add_argument("--holdout-size", type=int, default=0)
    walk.add_argument("--expanding", action="store_true")
    args = parser.parse_args(argv)
    output_created = False
    try:
        if args.command == "freeze":
            ms = {"1h": 3_600_000, "1m": 60_000, "5m": 300_000}[args.interval]
            data = load_csv(args.csv, interval_ms=ms)
            write_dataset(data, args.output)
            print(json.dumps(data.manifest(), ensure_ascii=False))
        elif args.command == "validate":
            print(json.dumps(load_dataset(args.data).manifest(), ensure_ascii=False))
        elif args.command == "fetch":
            if args.output.exists():
                raise FileExistsError("Output already exists")
            ms = {"1h": 3_600_000, "1m": 60_000, "5m": 300_000}[args.interval]
            data = fetch_candles(
                start_ms=args.since,
                end_ms=args.until,
                interval_ms=ms,
                progress=lambda completed, total: _progress(
                    {
                        "phase": "fetch",
                        "completed": completed,
                        "total": total,
                        "percent": 100.0 * completed / total if total else 100.0,
                    }
                ),
            )
            write_dataset(data, args.output)
            print(str(args.output.resolve()))
        elif args.command == "demo":
            hourly, minute = demo_datasets(hours=args.hours)
            args.output.mkdir(parents=True, exist_ok=False)
            write_dataset(hourly, args.output / "hourly")
            write_dataset(minute, args.output / "minute")
            compare(
                hourly,
                {"cash": CashAdvisor(), "rules": RulesAdvisor(), "numeric": NumericAdvisor()},
                config=Config(),
                execution_data=minute,
                output=args.output / "comparison",
                fixed_latency_seconds=0,
            )
            print(str((args.output / "comparison" / "report.html").resolve()))
        else:
            if args.output.exists():
                raise FileExistsError("Output already exists; choose a new directory")
            hourly = load_dataset(args.data)
            minute = load_dataset(args.execution_data) if args.execution_data else None
            config = _config(args)
            if (
                minute or hourly
            ).interval_ms / 1000 > config.max_decision_age and args.fixed_latency_seconds is None:
                raise ValueError(
                    "Execution bars exceed the decision freshness limit. Use 1m execution data; "
                    "for deterministic screening only, explicitly choose --fixed-latency-seconds 0."
                )
            if minute is None and (args.model or args.chronos_revision or args.publication_delay_seconds > 0):
                raise ValueError(
                    "Use --execution-data with 1m/5m candles for delayed/model decisions; coarse replay requires explicit --publication-delay-seconds 0"
                )
            options = _run_options(args)
            if args.command == "replay":
                if args.model or args.chronos_revision:
                    raise ValueError("Use compare for model experiments")
                args.baselines = args.advisor
                advisor = _candidates(args, config, hourly)[args.advisor]
                result = run_replay(
                    hourly,
                    advisor,
                    config=config,
                    execution_data=minute,
                    compact_dir=(args.output / ".compact") if args.compact else None,
                    **{
                        **options,
                        "progress_callback": (lambda event: _progress({"advisor": args.advisor, **event}))
                        if args.compact
                        else None,
                    },
                )
                export_run(result, args.output)
                print(json.dumps(result["metrics"], ensure_ascii=False))
            elif args.command == "compare":
                report = compare(
                    hourly,
                    _candidates(args, config, hourly),
                    config=config,
                    execution_data=minute,
                    output=args.output,
                    **options,
                )
                print(
                    json.dumps(
                        {name: row["metrics"] for name, row in report["candidates"].items()},
                        ensure_ascii=False,
                    )
                )
            else:
                if args.train_size < config.minimum_candles:
                    raise ValueError("Training prefix must include the configured indicator warmup")
                if args.start_index is not None or args.end_index is not None:
                    raise ValueError(
                        "Walk-forward uses explicit fold windows, not replay start/end overrides"
                    )
                plan = make_folds(
                    len(hourly.rows),
                    train_size=args.train_size,
                    validation_size=args.validation_size,
                    test_size=args.test_size,
                    gap=args.gap,
                    holdout_size=args.holdout_size,
                    expanding=args.expanding,
                )
                advisors = _candidates(args, config, hourly)
                args.output.mkdir(parents=True, exist_ok=False)
                output_created = True
                evaluations = []

                def evaluate(name, start, end):
                    result = run_replay(
                        hourly,
                        advisors[name],
                        config=config,
                        execution_data=minute,
                        compact_dir=(args.output / ".compact" / f"eval_{len(evaluations):04d}_{name}")
                        if args.compact
                        else None,
                        **{
                            **options,
                            "start_index": start,
                            "end_index": end,
                            "progress_callback": (
                                lambda event: _progress({"advisor": name, "fold": len(evaluations), **event})
                            )
                            if args.compact
                            else None,
                        },
                    )
                    export_run(result, args.output / f"eval_{len(evaluations):04d}_{name}")
                    if result["metrics"]["model_errors"]:
                        raise ValueError(
                            "Failed model evaluation cannot be selected as a zero-return strategy"
                        )
                    metrics = result["metrics"]
                    score = metrics["total_return"] - metrics["max_drawdown"]
                    evaluations.append({"candidate": name, "start": start, "end": end})
                    return {"score": score, "metrics": metrics}

                results = run_walk_forward(plan, evaluate, list(advisors))
                compact_root = args.output / ".compact"
                if compact_root.exists() and not any(compact_root.iterdir()):
                    compact_root.rmdir()
                write_json(
                    args.output / "walk-forward.json",
                    {
                        "plan": asdict(plan),
                        "results": results,
                        "provenance": provenance(),
                        "score": "gross total_return minus max_drawdown",
                        "holdout_evaluated": False,
                        "fitted_models": False,
                        "note": "Fixed advisors selected on validation; train ranges supply chronological warmup, not supervised fitting",
                    },
                )
                print(str((args.output / "walk-forward.json").resolve()))
    except (ValueError, TypeError, OSError, RuntimeError, LookupError) as exc:
        compact_root = getattr(args, "output", Path()) / ".compact"
        should_record_incomplete = (args.command == "walk-forward" and output_created) or (
            args.command == "replay" and compact_root.exists()
        )
        incomplete_path = getattr(args, "output", Path()) / "FAILED.json"
        if should_record_incomplete and not incomplete_path.exists():
            write_json(
                incomplete_path,
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "complete": False,
                    "audit_spools": [str(path) for path in compact_root.glob("*")]
                    if compact_root.exists()
                    else [],
                },
            )
        parser.exit(2, f"Research error: {exc}\n")
    return 0
