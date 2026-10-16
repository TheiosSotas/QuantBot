# Validation — 2026-10-16

## Κέντρο ελέγχου Windows / μεγάλα backtests

- Πλήρες suite κατά την ολοκλήρωση του compact engine: **245 passed σε 467.07 s**, περιλαμβανομένου του συνεχούς συνθετικού ετήσιου replay.
- Τελικό suite μετά τις διορθώσεις CLI/UI/reporting: **248 passed, 1 deselected σε 66.85 s**. Μόνο το αμετάβλητο αργό ετήσιο test εξαιρέθηκε, επειδή είχε ήδη περάσει στο πλήρες suite. Συνολικά καλύπτονται 249 διαφορετικά tests.
- Ruff check και format check: πέρασαν. `pip check`: κανένα ασύμβατο requirement.
- Πραγματική εκκίνηση Windows μέσω `START-QUANTBOT.vbs`: πέρασε με το υπάρχον Python/venv. Το παράθυρο προετοιμασίας εκκίνησε το loopback control centre χωρίς terminal input.
- Browser: κανονική φόρτωση, πλήρες έτος 2025 → `2025-01-01` έως `2026-01-01`, demo → ολοκληρωμένη HTML αναφορά και JSON μέσω HTTP, επιτυχής ακύρωση χωρίς ορφανό research process, αυτόματη αλλαγή σε 1m όταν επιλέγεται μοντέλο, μηδενικά JavaScript errors στην τελική σελίδα.
- Συνθετικό 24h compact CLI με 5m execution και ρητό μηδενικό simulated compute latency: cash/rules/numeric ολοκληρώθηκαν. Μηδενικά `AI_DECISION_EXPIRED`, 4/3 εικονικά κλεισίματα για rules/numeric αντίστοιχα. Αποτελεί έλεγχο λογισμικού, όχι πραγματικό trading αποτέλεσμα.
- Συνθετικό συνεχές έτος: μία ανοικτή LONG θέση διατηρήθηκε χωρίς μηνιαίο reset· **35.041** πλήρη equity samples. Όλα τα streaming metrics συμφώνησαν με ανεξάρτητο επανυπολογισμό από το εξαγόμενο CSV. Οι δοκιμές καλύπτουν επίσης κέρδη/ζημιές, αρνητικό equity, round-trip κλείσιμο και διατήρηση πλήρους audit.
- Δεν έγινε πραγματικό ετήσιο market backtest, λήψη νέων model weights, inference ή benchmark της RTX 5070 Ti του φίλου. Η πραγματική υπηρεσία Binance/Ollama και οι χρόνοι του δικού του PC χρειάζονται έλεγχο εκεί.

Review αποφάσεων/διορθώσεων: `DESKTOP-REVIEW.md`. Απλές οδηγίες: `QUICKSTART.md`.

## Historical replay / AI comparison package

Base: `efab424`. Root reviewed three Luna subagents' data, metrics/fold, advisor/cache and replay-test changes, corrected findings and implemented orchestration/reports/CLI. New commits use Greek subjects and explanatory bodies.

| Check | Result |
|---|---|
| Full `python -m pytest -q` | **221 passed**, 59.43 seconds (120 previous + 101 new) |
| Ruff check / format check | Passed; no formatting changes required |
| `pip check` | No broken requirements |
| Production launcher `--help` | Passed; paper launch options preserved |
| Research `--help` and `compare --help` | Passed; fetch/freeze/validate/replay/compare/walk-forward/demo exposed |
| Offline 24h synthetic comparison | Completed for cash/rules/numeric, independent accounts, detailed files and HTML report |
| Offline chronological walk-forward example | Three outer folds, final `[620:624]` holdout not evaluated |
| Two-model loopback HTTP protocol and cache-only repetition | Passed; mock `/api/tags` and `/api/generate`, independent model labels, zero subsequent network calls |

Regression coverage includes dataset corruption and mutable-source isolation, incorrect timestamp units, missing/duplicate bars, incomplete/future features, parent/child OHLCV mismatch, chronological folds/embargo/tie-breaking/holdout exclusion, negative equity and first-sample loss, mark-to-market metrics, conservative/optimistic bracket resolution, breakeven path sensitivity, gap-stop fills, actual delayed next-open application, default publication delay, pending results invalidated by an intervening stop, cache-only misses/corruption, digest/source/prompt/portfolio cache invalidation, model digest mutation, optional Chronos local-only loading, no-decision error reporting, full CLI baseline replay and immutable output files.

The offline examples use **synthetic data**, not downloaded Binance history. Actual Ollama inference, real Chronos weights, GPU memory/latency and profitability were **not** measured. Chronos tests use an injected fake pipeline; the HTTP model test uses an explicitly fake loopback server. No market request, model download or real order was part of these checks. The opt-in fetch implementation is tested with paginated fake exchange responses. Forward/live shadow monitoring, Optuna and supervised retraining are outside this package.

See `REPLAY.md` for timing/intrabar assumptions and `RESEARCH.md` for commands, cache/provenance semantics and statistical limits. Prior validation sections below are historical.

## Account history / audit / protections package

Base for this package: `aa92a87`. Three `gpt-6-luna` subagents implemented the protection module, storage migration, and integration regressions. The root agent reviewed those changes, requested corrections, integrated execution/Runner/UI/CLI, and approved the revised result.

| Check | Current result |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **120 passed**, 61.60 seconds (59 previous + 61 new tests) |
| Ruff check | Passed |
| Ruff format check | 30 Python files formatted |
| pip check | No broken requirements |
| Launcher `--help` | Shows all four new account/history options |
| Invalid CLI control values | Exit with usage error before creating an account; covered in tests |
| Dashboard extracted JavaScript / `node --check` | Passed |
| Browser / temporary synthetic account | Equity polyline, 8 samples, accepted ENTRY and MODEL_CLOSE audits, disabled limits and READY account gate displayed |
| SQLite HTTP history access | Threaded loopback HTTP regression passes; fresh read-only connections |

New regressions cover unrealized daily/global losses, same-day recovery latches, UTC rollover, restart persistence, disabled-by-default controls, no reset on disabling a triggered limit, negative-equity drawdown above 100%, corrupted state, backward OS clock with a breached stop, cooldown for entry/add with exits allowed, forced and interval samples, stale/invalid decisions, quote/indicator/model failures, old snapshot migration, failed initialization releasing account lock, and state/fill/history/audit rollback/recovery under SQLite failures.

The browser preview used a temporary directory and synthetic prices/model decisions, not the user's active account. The existing external TradingView frame reported `cannot_get_metainfo`; that third-party chart was not validated. Local account-history/control/audit panels worked and had no new script error. Screenshot evidence is outside the repository in the account-package delivery directory. No Binance or Ollama request was used for this validation. No live order, hardware performance, historical profitability, or new model was tested.

The changes below remain historical validation of the previous package, not the current test count.

## Previous modularization package

Base source: `b28831a548f91e041c8c10d481073d7a8903c5eb`.

Environment: Windows, Python 3.10.0, isolated `.venv`; exact installed packages are in `requirements-lock.txt`.

| Check | Result |
|---|---|
| `python -m pytest -q` | **59 passed**, 14.52 seconds |
| `python -m ruff check --config pyproject.toml .` | Passed |
| `python -m ruff format --config pyproject.toml --check .` | 22 Python files already formatted |
| `python -m pip check` | No broken requirements |
| `python quant_bot.py --help` | Passed; compatible launcher and CLI options |
| Dashboard script extracted and checked with `node --check` | Passed |
| Default CCXT client construction | Initialized Binance with `BTC/USDT:USDT`; no API request made |

The tests use deterministic fake market/model responses and temporary account directories. The dashboard test starts a temporary loopback-only HTTP server and checks the paper-mode page and state endpoint. No Binance/Ollama request or real trade is part of this validation.

Coverage includes long and short protection, scale/breakeven interaction, existing exits taking precedence, coded entry/volume filters, stale decisions, changed positions, once-per-candle evaluation, nonfinite data and arithmetic overflow, notional and aggregate stop-risk limits, closed-candle warmup/cache, invalid OHLCV, atomic database rollback, CSV export failure, restart recovery, single-instance locking, storage recovery, strict model schema and a blocked background inference worker.

The tests establish these software invariants. Historical profitability and live provider integration were not measured. Fees and funding charges remain intentionally excluded from paper PnL.
