# Validation — 2026-10-18

## Προφόρτωση / διάγνωση PAPER Ollama ReadTimeout — 2026-10-18

- Τελική regression suite: **332 passed, 1 deselected σε 67.69 s**. Εξαιρέθηκε το αμετάβλητο ετήσιο stress test. Ruff check/format πέρασαν για 73 Python αρχεία, και `git diff --check` πέρασε.
- Η φωτογραφία εξετάστηκε σε σωστό προσανατολισμό: `ReadTimeout: HTTPConnectionPool(host='localhost', port=11434)` με περίπου 31,59 s read timeout. Δεν αποδεικνύει ότι το πρώτο αίτημα είχε budget 31 s ούτε ότι η αιτία ήταν GPU, RAM ή cold load. Το ONLINE δείχνει ανανέωση τιμών ενώ το AI δεν έχει ολοκληρώσει έγκυρη απόφαση.
- Επιβεβαιώθηκε από τις επίσημες οδηγίες Ollama το default 5-minute unload και η υποστήριξη preload μέσω empty generate request / keep_alive. Η νέα PAPER εκκίνηση προφορτώνει σε background worker και ζητά keep_alive 7200 s. Οι κανονικές αποφάσεις διατηρούν το ίδιο deadline 120 s. Το research config παραμένει σε keep_alive 300 s.
- Τα tests καλύπτουν load request χωρίς decision prompt, κλειστό response, απόρριψη incomplete/error loading, κατανάλωση WARMUP σε κανονικό loop και αποτυχίες quote/indicators χωρίς μετατροπή σε θέση/lesson, και stop που κλείνει κανονικά όσο το warmup είναι μπλοκαρισμένο.
- Ελέγχθηκε η μικρή dequeue/busy race με unfinished task tracking. Η δοκιμή startup/handoff επεκτάθηκε ώστε να επιβεβαιώνει WARMUP μετά το worker.start και το PAPER keep_alive 7200.
- Πραγματικό loopback HTTP για diagnostic: φόρτωση → strict schema decision με τεχνητά δεδομένα → `/api/ps` telemetry, χωρίς λήψη ή άνοιγμα λογαριασμού. Tests async UI job, αμοιβαίου αποκλεισμού με PAPER και ακύρωσης πριν από inference πέρασαν.
- Luna read-only review εντόπισε και διορθώθηκε το retry-status cancellation path: κλείσιμο θέσης κατά το backoff δεν αφήνει RETRY_WAIT στο dashboard. Νέα υποβολή καθαρίζει παλιό error· expiry/cancellation έχουν ρητές καταστάσεις.
- Browser έλεγχος σε ξεχωριστό προσωρινό control root: click «Έλεγχος AI», επιτυχής εμφάνιση χρόνων και memory/VRAM από ελεγχόμενο stub. Οι αριθμοί προεπισκόπησης **δεν είναι πραγματικές μετρήσεις Qwen ή του PC του φίλου**. Screenshot: `E:\temp\quantbot-ai-check-preview.png`. Ο προσωρινός server και η προσωρινή καρτέλα έκλεισαν.
- Δεν εκκινήθηκε πραγματικός PAPER λογαριασμός, δεν έγινε λήψη μοντέλου ή πραγματική Qwen inference. Η συγκεκριμένη αιτία στο PC του φίλου παραμένει ανεπιβεβαίωτη μέχρι να εκτελέσει τον νέο έλεγχο. Το keep_alive μπορεί να παρακαμφθεί από eviction/memory pressure του Ollama και το socket timeout δεν αποτελεί hard wall-clock cancellation.

## Επαναλήψεις PAPER και παραλλαγές στρατηγικής — 2026-10-17

- Τελική regression suite: **315 passed, 1 deselected σε 80.90 s**. Εξαιρέθηκε το αμετάβλητο αργό ετήσιο stress test. Ruff check/format: πέρασαν για 71 Python αρχεία. `git diff --check`: πέρασε.
- Luna implementation/review με τελική ανασκόπηση του κύριου agent: structured retryability για connection/timeout/429/5xx, έως δύο υποβολές, backoff 5 s, ίδιο candle/revision και αρχικό wall/monotonic deadline. Ο κύριος agent εντόπισε ότι το transport budget χρειαζόταν επίσης μεταφορά μέχρι τον worker και ζήτησε/έλεγξε τη διόρθωση πριν από το release.
- Tests καλύπτουν ανάκτηση με τρέχουσα τιμή/portfolio, αποτυχία δεύτερης προσπάθειας χωρίς τρίτη, HTTP 400/401 και invalid schema χωρίς retry, ακύρωση μετά από deadline/αλλαγή θέσης/κεριού, υπόλοιπο χρόνου στο πραγματικό ModelClient με ελεγχόμενο session και μηδενικά POST από ληγμένο queued job.
- Strategy controls: no-confidence εξακολουθεί να περνά hard entry/risk filters, no-scale διατηρεί υπάρχον stop, trend gate δεν εμποδίζει CLOSE, LONG/SHORT protective stops λειτουργούν με όλα τα πειραματικά flags. Μη έγκυρα booleans/thresholds/max_tranches απορρίπτονται.
- Matrix tests: ένας παράγοντας ανά παραλλαγή, ξεχωριστά effective configs, ίδια προεπιλογή PAPER, απόρριψη ασύγκριτων candidate κεφαλαίων. CLI και model/replay/cache tests πέρασαν.
- Πραγματική compact CLI εκτέλεση σε **6 ώρες συνθετικών δεδομένων 1m**, με 8 υποψηφίους rules/numeric × 4 παραλλαγές, ολοκλήρωσε exports/αναφορά. Πρόκειται για δοκιμή λειτουργίας, όχι αξιολόγηση στρατηγικής ή πραγματικής αγοράς. Αναφορά: `E:\temp\quantbot-strategy-review-dfs611yt\comparison\report.html`.
- Browser σε απομονωμένο προσωρινό control server: το νέο checkbox υποβάλλει `strategy_experiments: true` χωρίς overflow φόρμας. Η ενέργεια υποβολής παρεμποδίστηκε μόνο στη δοκιμή browser, ώστε να μην γίνει δικτυακή λήψη/πραγματικό backtest. Ξεχωριστά unit tests επαλήθευσαν το backend CLI flag. Η πραγματική αποθηκευμένη συνθετική αναφορά εμφανίζει 8 σειρές με διακριτές ελληνικές ονομασίες, τελικά ποσά και διευκρινίσεις execution timing. Ο προσωρινός server τερματίστηκε από το δικό του shutdown action.
- Δεν εκκινήθηκε PAPER λογαριασμός, δεν άλλαξε το μοντέλο, δεν έγινε νέα πραγματική Qwen inference ή ετήσιο market backtest. Η τελική δοκιμή πραγματικών μοντέλων/δεδομένων παραμένει ξεχωριστή εργασία.

## Προληπτικό AI review με Luna και κύριο agent — 2026-10-17

- Τελική regression suite: **283 passed, 1 deselected σε 72.86 s**. Εξαιρέθηκε μόνο το αμετάβλητο ετήσιο stress test. Ruff check και format check: πέρασαν για 69 Python αρχεία. `git diff --check`: πέρασε.
- Δύο Luna subagents: read-only review AI/worker και research/cache. Ο κύριος agent αποδέχθηκε και έλεγξε το timeout cache-key fix της Luna, με regression test που αποδεικνύει νέα επιτυχή κλήση μετά από αποθηκευμένη αποτυχία διαφορετικού timeout.
- Νέα tests: υπερβολικό βάθος JSON, διπλά action fields, NaN, τεράστιος αριθμός και κείμενο πέρα από το όριο μπαίνουν στη μία correction. Μη ολοκληρωμένη generation/τερματισμός μήκους/error δεν γίνονται δεκτά ακόμη και με parseable εσωτερικό JSON.
- Ελεγχόμενος slow-body iterator περνά το elapsed deadline: απορρίπτεται με transport timeout, χωρίς δεύτερη generation, και το response κλείνει. Απόκριση που επιστρέφει μετά το deadline απορρίπτεται ακόμη και αν περιέχει έγκυρη απόφαση.
- Body άνω των 256 KiB διακόπτεται, με δύο μόνο bounded attempts και κλειστά responses. HTTP 503: μία μόνο κλήση, close και diagnostics, χωρίς schema retry. Ο δεύτερος έλεγχος της Luna εντόπισε αυτό το close path πριν το commit.
- Τα πραγματικά loopback HTTP/replay/cache tests εξακολουθούν να περνούν. Δεν έγινε πραγματική Qwen inference ή εγκατάσταση μοντέλου. Τα αποτελέσματα αποδεικνύουν συμπεριφορά κώδικα υπό ελεγχόμενες απαντήσεις, όχι μηδενική πιθανότητα αποτυχίας μοντέλου ή trading performance.
- Το Requests socket timeout παραμένει όριο αδράνειας, όχι εξαναγκασμένη ακύρωση transport σε ακριβές wall-clock deadline. Η σταδιακή ανάγνωση προσθέτει ελέγχους χρόνου ανάμεσα στα chunks· μπλοκαρισμένο read/headers εξακολουθεί να εξαρτάται από το socket timeout. Βλ. `AI-RELIABILITY-REVIEW.md`.

## Ανάκτηση άκυρης μορφής απόφασης AI — 2026-10-17

- Regression suite: **270 passed, 1 deselected σε 66.14 s**. Εξαιρέθηκε το αργό ετήσιο stress test. Ruff check/format: πέρασαν.
- Αναπαραγωγή του ορατού failure class με `confidence: 90` και `scale_in_signal: "false"`. Η αρχική έξοδος απορρίπτεται αυστηρά, ζητείται μία διόρθωση και γίνεται δεκτή μόνο νέα έγκυρη έξοδος. Το αρχικό context/portfolio παραμένει ίδιο.
- Καλύπτονται δύο μόνιμα άκυρες απαντήσεις χωρίς fallback, άκυρο JSON, πρώτο έγκυρο output χωρίς retry, κοινό χρονικό περιθώριο χωρίς νέο πλήρες timeout, αποτυχία σύνδεσης χωρίς schema retry και αλλαγή model digest πριν από τη διόρθωση.
- Πραγματικός worker PAPER με ελεγχόμενες HTTP απαντήσεις επιστρέφει διορθωμένο Decision. Αποτυχημένη διόρθωση καταγράφεται ως INVALID_MODEL_OUTPUT και υπάρχον breached stop κλείνει κανονικά.
- Loopback HTTP + κοινός replay engine + compact export + immutable cache: δύο POST (άκυρο/έγκυρο), model_errors=0, model_output_repairs=1, πλήρης διατήρηση trace και measured latency. Offline cache replay παρήγαγε τα ίδια metrics χωρίς GET ή POST. Το digest ελέγχθηκε πριν και από τις δύο προσπάθειες.
- Ελέγχθηκε τοπικά ότι δεν λειτουργεί Ollama server και δεν υπάρχουν εγκατεστημένα Qwen weights σε αυτόν τον host (υπάρχει μόνο διαφορετικό cloud tag). Δεν έγινε πραγματική Qwen inference, cloud κλήση ή λήψη βαρών. Η επιβεβαίωση του πραγματικού μοντέλου/έκδοσης Ollama του φίλου πρέπει να γίνει στο δικό του PC.
- Η φωτογραφία δείχνει PAPER εκκίνηση (`python main.py` / PAPER trading dashboard), όχι ερευνητικό runner. Η διόρθωση μπήκε στο κοινό ModelClient που χρησιμοποιούν και οι δύο λειτουργίες.

## Παρουσίαση τελικού κεφαλαίου ανά στρατηγική — 2026-10-17

- Regression suite: **259 passed, 1 deselected σε 64.26 s**. Εξαιρέθηκε το αργό ετήσιο stress test· δεν άλλαξε η στρατηγική ή ο υπολογισμός metrics.
- Νέοι έλεγχοι: ακριβές τελικό ποσό αντί αναγωγής από στρογγυλοποιημένη απόδοση, θετικό/αρνητικό συνολικό PnL χωριστά από realized/unrealized, εξαίρεση cash, model errors και escaping, BTC hold reference, επίπεδη καμπύλη, ασφαλείς διαδρομές αρχείων.
- Επαλήθευση ανανέωσης παλιάς αναφοράς μέσω πραγματικού loopback HTTP: εμφανίζονται τα αποθηκευμένα τελικά metrics με τη νέα μορφή, χωρίς replay/AI ή άνοιγμα της ενεργής βάσης. SHA-256 όλων των αρχικών αρχείων παραμένουν ίδια. Επαναλαμβανόμενη προβολή χρησιμοποιεί cache που ελέγχει timestamps/μεγέθη των αρχείων πηγής.
- Οπτικός έλεγχος στον browser με ολοκληρωμένο συνθετικό 5m πείραμα: πίνακας τελικών ποσών πρώτος, σωστά πρόσημα/χρώματα, δύο στρατηγικές αντί cash, άξονες χρόνου/USDT, υπόμνημα και χωριστό BTC hold.
- Ruff check/format: πέρασαν. Δεν χρησιμοποιήθηκαν δεδομένα ή ακριβή αποτελέσματα του φίλου· εδώ δεν υπάρχει ο δικός του φάκελος πειράματος.

## Αφαίρεση αυτόματου stop στην είσοδο στο 50% του στόχου

- Τελικό regression suite: **251 passed, 1 deselected σε 63.36 s**. Εξαιρέθηκε μόνο το αργό ετήσιο stress test· δεν επαναλήφθηκε για αυτή τη στοχευμένη αλλαγή κανόνα.
- Καλύπτονται LONG/SHORT που περνούν το μισό του στόχου, επιστρέφουν πέρα από την είσοδο και παραμένουν ανοιχτά μέχρι τον αρχικό στόχο. Δεν παράγεται αυτόματο `STOP_UPDATE`.
- Τα δύο intrabar σενάρια replay δεν μεταφέρουν stop στο μισό. Υπάρχον αποθηκευμένο stop δεν χαλαρώνει· οι έλεγχοι restart/λογιστικής κλεισίματος χρησιμοποιούν το πραγματικό stop.
- Αφαιρέθηκαν η ένδειξη και ο υπολογισμός του 50% από το dashboard. Ruff check/format: πέρασαν.

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
