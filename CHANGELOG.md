# Changelog

## Σαφή τελικά ποσά στις αναφορές backtest — 2026-10-17

- Πίνακας αποτελεσμάτων πριν από το διάγραμμα: αρχικό και τελικό κεφάλαιο, μεταβολή σε USDT/ποσοστό, μέγιστη πτώση και κλεισμένες συναλλαγές ανά στρατηγική. Τα ποσά διαβάζονται από τα metrics, χωρίς ανακατασκευή από στρογγυλοποιημένη απόδοση.
- Αφαιρέθηκε η γραμμή cash από το HTML/διάγραμμα και από τους υποψηφίους των νέων GUI backtests. Το εσωτερικό benchmark παραμένει διαθέσιμο στις ερευνητικές εντολές/παλιότερα JSON.
- Το διάγραμμα έχει άξονα αξίας και χρόνου με υπόμνημα, αντί για ασαφές ελάχιστο/μέγιστο ως επικεφαλίδα. Η υπάρχουσα αναφορά αγοράς/διακράτησης BTC εμφανίζεται χωριστά.
- Οι παλιές ολοκληρωμένες δοκιμές εμφανίζονται με τη νέα μορφή από την εφαρμογή, διαβάζοντας τα αποθηκευμένα metrics και equity CSV. Δεν εκτελείται ξανά στρατηγική/AI και δεν τροποποιούνται τα αρχικά artifacts. Απαιτείται να διατηρηθεί ολόκληρος ο φάκελος του πειράματος.

## Αφαίρεση αυτόματου breakeven στο μισό του στόχου — 2026-10-16

- Η επίτευξη του 50% της διαδρομής προς το take-profit δεν μετακινεί πλέον το stop στην είσοδο, ούτε σε LONG ούτε σε SHORT.
- Η αλλαγή ισχύει στον κοινό engine PAPER/backtest. Αφαιρέθηκαν το ενδιάμεσο replay threshold και η αντίστοιχη ένδειξη dashboard.
- Ήδη αποθηκευμένα stops διατηρούνται χωρίς χαλάρωση. Η προϋπάρχουσα σύσφιξη stop κατά την προσθήκη θέσης παραμένει ξεχωριστός κανόνας.
- Προστέθηκαν έλεγχοι ότι η θέση παραμένει ανοιχτή σε επιστροφή της τιμής μετά το μισό του στόχου και μπορεί να φτάσει στο αρχικό take-profit.

## Γραφικό κέντρο ελέγχου και μεγάλα backtests — 2026-10-16

- Μία εκκίνηση Windows με διπλό κλικ, παράθυρο αυτόματης προετοιμασίας και ελληνικό περιβάλλον στον browser. Κουμπιά για PAPER/Ollama, επιλογή ή ρητή λήψη μοντέλου, demo, ημερομηνίες backtest, πρόοδο, ακύρωση και αποθηκευμένες αναφορές.
- Εύρος μέχρι 366 ολοκληρωμένες ημέρες, presets ημερολογιακού έτους/365 ημερών, 5m execution προεπιλογή και προαιρετικό 1m. Ίδια κοινή μηχανή, συνεχές κεφάλαιο/θέσεις σε όλη την περίοδο, πλήρης equity/audit εξαγωγή στον δίσκο με compact mode και μικρότερο διάγραμμα στη μνήμη.
- Όλες οι ενέργειες ελέγχου περιορίζονται στο loopback με Host/Origin/token checks. Ο controller δεν ανοίγει την ενεργή βάση. Διακοπή PAPER με ασφαλές σήμα/heartbeat και αποθήκευση, χωρίς εξαναγκασμένο kill λογαριασμού. Ακύρωση τερματίζει μόνο το δέντρο της δικής του research διαδικασίας.
- Τα νέα συνολικά όρια παραμένουν αρχικά ανενεργά. Δεν αλλάζει αυτόματα το μοντέλο της στρατηγικής και δεν προστίθενται πραγματικές εντολές, προμήθειες ή funding.

## Ιστορικές δοκιμές και σύγκριση μοντέλων — 2026-10-16

- Προστέθηκε αιτιακό replay του ίδιου PaperEngine με απομονωμένη μνήμη, ιστορικό ρολόι, έλεγχο warmup και ρητό χρόνο δημοσίευσης/inference/εκτέλεσης.
- Τα ιστορικά datasets έχουν αμετάβλητα candles, CSV/manifest hashes, ελέγχους πληρότητας και συμφωνία ωριαίων/λεπτών OHLCV. Η ρητή λήψη CCXT ελέγχει gaps και πρόοδο σελίδων.
- Προστέθηκαν σύγκριση ανεξάρτητων λογαριασμών, cash/rules/momentum baselines, adapters εγκατεστημένου Ollama και προαιρετικού pinned Chronos χωρίς αυτόματο download.
- Η cache διατηρεί αρχικές καθυστερήσεις, ελέγχει prompt/model/context/source fingerprint και επιτρέπει replay-only χωρίς νέα inference. Αλλαγή εγκατεστημένου model digest απορρίπτεται.
- Προστέθηκαν chronological validation/test folds με ανέγγιχτο holdout, πλήρη mark-to-market metrics, manifests πειραμάτων, JSON/CSV και ελληνική HTML αναφορά με εμφανή σήμανση συνθετικών δεδομένων.
- Η ενεργή paper εφαρμογή παραμένει με Qwen2.5:14b και τους ίδιους κανόνες. Δεν προστέθηκαν fees/funding, Optuna, MLflow, πραγματικές εντολές ή αυτόματη προαγωγή μοντέλου.

## Account history, decision audit and persistent protections — 2026-10-15

- Persisted sampled mark-to-market equity history, including unrealized PnL, account peak, drawdown and exposure; forced samples on fills and protection transitions.
- Added configurable UTC daily-loss and peak-drawdown latches plus post-close cooldown. New limits default to disabled; existing position limits remain active. Latches survive restart/config changes; daily reset uses first fresh quote of a later UTC day, global latch does not auto-reset.
- New-risk gates do not suppress stops/targets or eligible model closes. A backward OS clock blocks entries while preserving existing exits.
- Added accepted/rejected/no-action decision audit, including stale/schema/model/data failures, stored atomically with state/fill/history events.
- Additive SQLite schema-1 migration preserves legacy positions and events; thread-safe read-only history APIs feed a local equity chart, account controls and decision table.
- Paper-only mode, Qwen2.5:14b, existing indicator conventions and exclusion of fees/funding remain unchanged. No new framework, model or historical replay is introduced.

## Previous modular paper-trading corrections

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
