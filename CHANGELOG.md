# Modular paper-trading corrections

Base: `b28831a548f91e041c8c10d481073d7a8903c5eb`.

## Scope

Correctness and modularization only. Qwen remains `qwen2.5:14b`; the EMA/RSI entry thresholds, initial 1.5 ATR stop, 2:1 target, 75%/80% model-score thresholds and three total tranches remain. No fees, funding charges, spread/slippage engine, real orders, Qwen upgrade, framework migration or third-party trading modules were added. PnL remains gross paper PnL.

## Corrections

- The market/protection loop never waits for a model response. Model inference and research post-mortems use a bounded background queue. Pending/failed model jobs cannot skip stop/target checks or PnL marking.
- Existing stops and targets close positions before model exits, entries or additions. A cached old stop cannot overwrite a newer stop. Breakeven status is recalculated against the enlarged position's actual average entry.
- The existing EMA ordering, RSI limits, choppy-regime exclusion and scaling-volume threshold are enforced in code. No new indicator or numerical definition of sideways markets was invented; the range label remains an AI assessment.
- JSON responses are validated locally against required fields, enums, real booleans and finite values. Invalid model output is rejected, not guessed or converted from ambiguous strings.
- Stale quotes, stale model decisions, decisions for an older candle, and decisions for a changed position are rejected. One successfully evaluated decision is persisted per closed candle, including HOLD/rejected entry eligibility. A failed inference is retried on the next candle; restarts can retry an unfinished evaluation.
- Signals use fully closed, timestamp-checked candles. Indicators are cached per candle; fresh quotes still drive protection and simulated execution. EMA warmup increases from 250 fetched candles to 1,000, with at least 600 valid closed candles required. SMA-based RSI and ATR conventions are preserved; a flat RSI becomes 50 and zero volume becomes 0.
- Invalid/nonfinite data, duplicate or missing candles, zero ATR, impossible OHLC ranges and invalid timestamps block new entries. They do not prevent a protective exit when a fresh valid quote is available.
- Position size is floored to the configured step, with explicit aggregate stop-risk and notional/equity ceilings. Defaults are **1% per tranche, 3% aggregate planned stop risk and 1x notional/equity**. The first two reflect the existing 1%-per-tranche/three-tranche configuration; the new 1x ceiling is an explicit conservative simulation default, not a claim about optimal leverage. Gaps may exceed planned stop risk.
- SQLite commits position state, cash, decision records and fill events atomically. Failed commits leave the old in-memory state intact. CSV is a derived export, so a locked CSV cannot realize PnL twice or leave a closed position open.
- Entries, additions, closes, protection changes and research suggestions are recorded. Open positions and the last evaluated candle survive restart. An OS-level lock prevents two application instances from using the same account directory.
- Legacy CSV/lesson data are imported once when creating the SQLite account. Corrupt state refuses startup instead of silently restoring 10,000 USDT. A different symbol/timeframe requires a different account directory.
- Automatic post-mortems are kept as stored research suggestions, with no automatic mandatory-rule injection into trading prompts. Losses alone are not proof that a new rule is valid.
- The dashboard explicitly says PAPER, reports health/data/protection timestamps and AI errors, escapes narrative text, distinguishes underlying price return from account return, and removes the RISK-FREE claim. It stays loopback-only and no longer permits wildcard cross-origin state access.

## Remaining boundaries

- This is still a local simulator. It does not submit real orders or place exchange-held stops.
- Protection requires fresh market data and samples last trades; a touch/recovery between observations remains invisible. A network outage can prevent pricing. The poll interval is a target cadence, not a fill guarantee.
- Only the historical records actually present in a legacy CSV can be imported. Positions lost by the old program before this migration cannot be reconstructed from completed trades alone. Prior recorded PnL is preserved, not retrospectively certified.
- Historical AI responses, order books, fees and intrabar fill sequencing have not been backtested here. The regression tests establish the corrected software behavior, not profitability.
- Post-mortem output uses the same model worker, after decision work; it is advisory and may be deferred. One latest pending loss hypothesis is retained in the queue to bound work.
- No automated drawdown-pause strategy, extra analyst roles, new predictive model or open-source framework adoption is included; those remain separate decisions.
