# Validation — 2026-10-15

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
