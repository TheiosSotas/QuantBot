# Changelog

## Προφόρτωση μοντέλου και διάγνωση PAPER ReadTimeout — 2026-10-18

- Ασύγχρονο WARMUP πριν από την πρώτη trading απόφαση, με χωριστό budget φόρτωσης. Το κύριο loop εξακολουθεί να ενημερώνει τιμές/stop. Η κατανάλωση αποτελέσματος WARMUP καλύπτει και διαδρομές αποτυχίας quote/indicators χωρίς να το μετατρέπει σε θέση ή post-mortem.
- Ρητό Ollama keep_alive: δύο ώρες για PAPER/διαγνωστικό έλεγχο, πέντε λεπτά για το default research config. Το keep_alive ανανεώνεται και στις κανονικές αποφάσεις/post-mortems. Δεν αλλάζει το trading decision deadline των 120 s.
- Νέο ασύγχρονο κουμπί «Έλεγχος AI»: φόρτωση, πραγματικό schema validation με τεχνητό context, measured latency και προαιρετικό `/api/ps` memory/VRAM snapshot. Καμία πρόσβαση σε λογαριασμό/θέση, καμία λήψη βαρών. Αμοιβαίος αποκλεισμός με PAPER start/active PAPER.
- Καταστάσεις WARMING_UP / RETRY_WAIT / EXPIRED / CANCELLED, σαφέστερα ελληνικά μηνύματα και καθαρισμός παλιού model_error στην επόμενη υποβολή. Το ONLINE περιγράφει ρητά τη ροή τιμών, όχι επιτυχή inference.
- Ο worker παραμένει μη διαθέσιμος στο μικρό διάστημα dequeue/busy μέσω unfinished task tracking, ώστε η προφόρτωση να μην καταναλώνει κατά λάθος το deadline μιας ήδη queued απόφασης.
- Δεν έχει τεκμηριωθεί η αιτία στο πραγματικό PC μόνο από τη φωτογραφία: πρόκειται για localhost Ollama ReadTimeout, όχι απόδειξη κακής μορφής JSON ή βλάβης στρατηγικής. Η πιθανή ψυχρή φόρτωση χρειάζεται επιβεβαίωση με τον νέο έλεγχο.
## Σύνδεσμος live dashboard στα Windows — 2026-10-17

- Διόρθωση του κρυμμένου κουμπιού «Άνοιγμα live dashboard» όταν το PAPER εκτελείται από Windows virtual environment. Ο launcher της Python μπορεί να έχει διαφορετικό PID από την πραγματική διεργασία PAPER. Η διεύθυνση διαβάζεται από το μοναδικό αρχείο ετοιμότητας της συγκεκριμένης εκκίνησης, όσο ο launcher παραμένει ενεργός.

## Ανάκτηση προσωρινών αποτυχιών και ελεγχόμενες παραλλαγές στρατηγικής — 2026-10-17

- Το PAPER κάνει έως μία επιπλέον υποβολή μετά από προσωρινό connection/timeout ή HTTP 429/5xx, με παύση 5 δευτερολέπτων. Διατηρεί το αρχικό deadline σε wall/monotonic χρόνο, την έκδοση θέσης και το ίδιο κερί. Ελέγχει ξανά την τρέχουσα τιμή/portfolio. Δεν επαναλαμβάνει μόνιμα άκυρο schema ή HTTP 400/401.
- Το υπόλοιπο monotonic budget μεταφέρεται μέχρι τον model worker και το ModelClient· ληγμένο queued job δεν στέλνει αίτημα. Κάθε αποτυχία καταγράφεται. Αλλαγή κεριού/θέσης ακυρώνει την προγραμματισμένη επανάληψη και δεν παρακάμπτεται η διαχείριση stops.
- Προαιρετικό checkbox «Σύγκριση 4 παραλλαγών στρατηγικής» και CLI `compare --strategy-experiments`. Κάθε υποψήφιος έχει αρχική εκδοχή και τρεις παραλλαγές ενός παράγοντα: χωρίς προσθήκες, χωρίς confidence gate ή με ελάχιστη απόσταση EMA50/EMA200 0,25 ATR. Το threshold είναι πειραματικό, όχι επιλεγμένο ως βέλτιστο.
- Ξεχωριστοί λογαριασμοί, ελληνικές ονομασίες, τελικό ποσό ανά εκδοχή και πλήρες effective config σε JSON/run artifacts. Απορρίπτονται candidate configs που αλλάζουν κεφάλαιο ή άλλες μη στρατηγικές παραμέτρους σύγκρισης.
- Τα πειραματικά gates δεν παρακάμπτουν close/reversal, stops ή τους υπόλοιπους κανόνες ρίσκου. Οι προεπιλογές PAPER παραμένουν ίδιες, με τα account limits αρχικά ανενεργά όπως ζητήθηκε.
- Το interface/report εξηγεί τη διαφορά execution candles από παρακολούθηση PAPER ανά περίπου 5 δευτερόλεπτα και ότι το numeric benchmark είναι απλό momentum. Η νέα operational επανάληψη του PAPER δεν προσομοιώνεται ως ξεχωριστό δικτυακό outage στο historical runner.

## Προληπτικός έλεγχος αξιοπιστίας AI με δύο Luna reviewers — 2026-10-17

- Η ταυτότητα Ollama/cache περιλαμβάνει πλέον το χρονικό όριο inference. Μια αποθηκευμένη αποτυχία μικρού timeout δεν επαναχρησιμοποιείται ως αποτέλεσμα άλλου timeout.
- Αυστηρή ανάγνωση JSON: διπλά κλειδιά, NaN/Infinity, υπερβολικό βάθος και μη αναπαραστάσιμοι αριθμοί δεν παρακάμπτουν τη μία διορθωτική προσπάθεια. Το schema δηλώνει επίσης το ήδη υπάρχον όριο 4.000 χαρακτήρων στα δύο κείμενα.
- Ελέγχονται τα ρητά πεδία μη ολοκληρωμένης/κομμένης generation του Ollama. Ένα parseable JSON δεν αρκεί όταν ο server δηλώνει αποτυχία ή μη κανονικό τερματισμό.
- Η HTTP απάντηση διαβάζεται σταδιακά με όριο 256 KiB και ελέγχους elapsed time. Εκπρόθεσμη απάντηση δεν επιστρέφεται ως έγκυρη απόφαση. Τα response objects κλείνουν και σε σφάλματα HTTP.
- Το socket timeout εξακολουθεί να είναι όριο αδράνειας ανά blocking read, όχι εγγύηση εξαναγκασμένου τερματισμού ακριβώς στο deadline. Ο κύριος κύκλος PAPER παραμένει ανεξάρτητος από τον worker.
- Καμία αυτόματη αλλαγή μοντέλου, trading κανόνων ή ορίων ρίσκου.

## Ανθεκτική ανάγνωση αποφάσεων AI σε PAPER και backtest — 2026-10-17

- Το JSON schema δίνεται και μέσα στο prompt, με ρητά παραδείγματα τύπων confidence/scale_in_signal και temperature 0, σύμφωνα με τις [οδηγίες structured outputs του Ollama](https://docs.ollama.com/capabilities/structured-outputs).
- Μία μόνο διορθωτική προσπάθεια για άκυρο JSON, πεδία, τύπους ή όρια. Χρησιμοποιεί τις ίδιες παρατηρήσεις και portfolio, με κοινό χρονικό περιθώριο. Δεν μετατρέπονται αυτόματα ποσοστά/strings σε έγκυρη απόφαση και δεν παράγεται τεχνητό HOLD ως fallback.
- Συγκεκριμένα μηνύματα validation για confidence και boolean. Μετά από αποτυχία και της διόρθωσης η απόφαση καταγράφεται ως INVALID_MODEL_OUTPUT στο PAPER, ενώ οι υπάρχουσες θέσεις συνεχίζουν να προστατεύονται.
- Το research κρατά τις δύο προσπάθειες, hashes, αρχική έξοδο και ακριβές corrective request. Ο χρόνος και των δύο περιλαμβάνεται στη latency. Το digest εγκατεστημένου μοντέλου επανελέγχεται πριν από κάθε generation. Η cache-only επανάληψη δεν κάνει νέα inference.
- Μετρήσεις model_output_repairs / invalid_model_output_attempts και πεδίο «Διορθωμένες απαντήσεις AI» στις λεπτομέρειες αναφοράς. Τα μη ανακτήσιμα σφάλματα εξακολουθούν να εμφανίζονται ως σφάλματα.
- Μικρή διόρθωση loopback HTTP στα Windows: απορριφθέν μικρό POST body αποστραγγίζεται με όριο μεγέθους/χρόνου πριν το 403, χωρίς parsing ή εκτέλεση, ώστε να μην κρύβεται η απόρριψη πίσω από socket reset.

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
