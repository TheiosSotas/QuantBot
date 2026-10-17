"""Readable reports from completed results; presentation never reruns a strategy."""

import csv
import html
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Report contains a non-finite number")
    return result


def _amount(value, signed=False):
    value = _number(value)
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _money(value, signed=False):
    return "$" + _amount(value, signed)


def _percent(value):
    return "—" if value is None else _amount(100 * value) + "%"


def _provider(identity):
    while identity.get("provider") == "cached-advisor":
        identity = identity["advisor"]
    return identity


def comparison_html(results, *, reference=None, saved=False):
    first_result = next(iter(results.values()))
    shown = {
        name: result
        for name, result in results.items()
        if _provider(result["advisor"]).get("provider") != "cash" and name != "cash"
    }
    colors = ("#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#22d3ee")
    origin = first_result["dataset"]["first_timestamp"]
    begin = datetime.fromtimestamp((origin + first_result["start_index"] * 3_600_000) / 1000, timezone.utc)
    finish = datetime.fromtimestamp((origin + first_result["end_index"] * 3_600_000) / 1000, timezone.utc)
    period = f"{begin:%d/%m/%Y %H:%M} → {finish:%d/%m/%Y %H:%M} UTC"
    names = {"rules": "Σταθεροί κανόνες", "numeric": "Κανόνες + αριθμητικό φίλτρο"}
    table, details, legend, series = [], [], [], []
    lows, highs, times = [], [], []
    synthetic = any(r["dataset"].get("source", {}).get("type") == "SYNTHETIC" for r in results.values())
    note = (
        "<p class='warning'>ΣΥΝΘΕΤΙΚΑ ΔΕΔΟΜΕΝΑ — επίδειξη λογισμικού, όχι αποτέλεσμα αγοράς.</p>"
        if synthetic
        else ""
    )
    if saved:
        note += "<p class='muted'>Προβολή αποθηκευμένου backtest. Δεν έγινε νέα εκτέλεση ή αλλαγή αποτελεσμάτων.</p>"
    for index, (name, result) in enumerate(shown.items()):
        metrics = result["metrics"]
        provider = _provider(result["advisor"])
        label = html.escape(provider.get("model") or provider.get("model_id") or names.get(name, name))
        color = colors[index % len(colors)]
        initial, final = metrics["starting_cash"], metrics["ending_equity"]
        pnl = final - initial
        tone = "gain" if pnl >= 0 else "loss"
        table.append(
            f'<tr><th scope="row"><i style="background:{color}"></i>{label}</th>'
            f'<td>{_money(initial)}</td><td class="final">{_money(final)}</td>'
            f'<td class="{tone}">{_money(pnl, True)}</td><td class="{tone}">{_percent(metrics["total_return"])}</td>'
            f"<td>{_percent(metrics['max_drawdown'])}</td><td>{int(metrics['trade_count'])}</td></tr>"
        )
        errors = int(metrics.get("model_errors", 0))
        details.append(
            f'<tr><th scope="row">{label}</th><td>{_percent(metrics.get("win_rate"))}</td>'
            f"<td>{'—' if metrics.get('profit_factor') is None else _amount(metrics['profit_factor'])}</td>"
            f"<td>{_money(metrics.get('realized_pnl', 0))}</td>"
            f"<td>{'—' if metrics.get('unrealized_pnl') is None else _money(metrics['unrealized_pnl'])}</td>"
            f"<td>{errors}</td><td>{'—' if metrics.get('model_output_repairs') is None else int(metrics['model_output_repairs'])}</td><td>{int(metrics.get('invalid_or_expired_decisions', 0))}</td></tr>"
        )
        if errors:
            note += f"<p class='warning'>{label}: {errors} σφάλματα AI. Το αποτέλεσμα χρειάζεται έλεγχο πριν χρησιμοποιηθεί για σύγκριση μοντέλων.</p>"
        rows = result["equity"]
        if rows:
            bounds = result.get("plot_bounds", {})
            lows.append(bounds.get("low", min(row["equity"] for row in rows)))
            highs.append(bounds.get("high", max(row["equity"] for row in rows)))
            times.extend((rows[0]["observed_at"], rows[-1]["observed_at"]))
        series.append((label, color, rows))
        legend.append(f'<span><i style="background:{color}"></i>{label}</span>')
    chart = "<p>Δεν υπάρχουν δεδομένα γραφήματος.</p>"
    if times:
        low, high = min(lows), max(highs)
        pad = (high - low) * 0.06 if high > low else max(abs(high) * 0.02, 1)
        low, high = low - pad, high + pad
        first, last = min(times), max(times)
        grid, lines = [], []
        for tick in range(5):
            y = 25 + tick * 65
            value = high - tick / 4 * (high - low)
            grid.append(
                f'<line x1="100" x2="980" y1="{y}" y2="{y}" stroke="#29364b"/><text x="88" y="{y + 4}" text-anchor="end">{_amount(value)}</text>'
            )
        for tick in range(5):
            x = 100 + tick * 220
            at = first + (last - first) * tick / 4
            date_format = "%d/%m %H:%M" if last - first < 3 * 86400 else "%d/%m/%Y"
            label = datetime.fromtimestamp(at, timezone.utc).strftime(date_format)
            anchor = "start" if tick == 0 else "end" if tick == 4 else "middle"
            grid.append(f'<text x="{x}" y="315" text-anchor="{anchor}">{label}</text>')
        for label, color, rows in series:
            if not rows:
                continue
            step = max(1, math.ceil(len(rows) / 1000))
            sampled = rows[::step]
            if sampled[-1] is not rows[-1]:
                sampled += [rows[-1]]
            points = " ".join(
                f"{100 + (row['observed_at'] - first) / max(last - first, 0.001) * 880:.2f},{25 + (high - row['equity']) / (high - low) * 260:.2f}"
                for row in sampled
            )
            lines.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"><title>{label}</title></polyline>'
            )
        chart = f'<svg viewBox="0 0 1000 340" role="img" aria-label="Εξέλιξη κεφαλαίου στρατηγικών, αξία σε USDT και ημερομηνίες UTC"><text x="100" y="13">Κεφάλαιο (USDT)</text>{"".join(grid)}{"".join(lines)}</svg>'
    hold = ""
    if reference:
        metrics = reference["metrics"]
        hold = f'<section><h2>Σύγκριση: αγορά BTC και διακράτηση</h2><p>Αρχικό {_money(metrics["starting_cash"])} → τελικό <strong>{_money(metrics["ending_equity"])}</strong> · απόδοση <strong>{_percent(metrics["total_return"])}</strong> · μέγιστη πτώση {_percent(metrics["max_drawdown"])}</p><p class="muted">Στην ίδια περίοδο, με έκθεση 1×, χωρίς stop. Έχει διαφορετική έκθεση σε ρίσκο από τις στρατηγικές.</p></section>'
    return f"""<!doctype html><html lang="el"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>QuantBot — Σύγκριση πειραμάτων</title>
<style>body{{background:#0b1020;color:#e2e8f0;font:15px system-ui;max-width:1240px;margin:auto;padding:24px}}h1{{font-size:26px}}h2{{font-size:19px}}section{{background:#121a2e;border:1px solid #29364b;border-radius:14px;padding:22px;margin:20px 0}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}td,th{{padding:14px 10px;text-align:right;border-bottom:1px solid #29364b;white-space:nowrap}}th:first-child{{text-align:left;white-space:normal;min-width:170px}}thead th{{font-size:12px;color:#aebdd1;white-space:normal}}.final{{font-size:19px;font-weight:800;color:#f8fafc}}.gain{{color:#65e0b3}}.loss{{color:#ff9d97}}.muted{{color:#aebdd1;font-size:13px;line-height:1.7}}.warning{{padding:12px;background:#443621;color:#ffda96;border-radius:8px}}i{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px}}.legend{{display:flex;gap:20px;flex-wrap:wrap;font-size:13px}}svg{{width:100%;height:auto;min-height:180px}}svg text{{fill:#aebdd1;font-size:12px}}summary{{cursor:pointer;font-weight:700}}a{{color:#84baff}}@media(max-width:600px){{body{{padding:12px}}section{{padding:14px}}}}</style></head>
<body><h1>Αποτελέσματα backtest</h1><p>{period}</p><p class="muted">Η λήξη δεν περιλαμβάνεται. Προσομοιωμένα αποτελέσματα πριν από προμήθειες και funding.</p>{note}
<section><h2>Τελικό αποτέλεσμα ανά στρατηγική</h2><div class="scroll"><table id="strategy-results"><thead><tr><th>Στρατηγική</th><th>Αρχικό κεφάλαιο</th><th>Τελικό κεφάλαιο</th><th>Κέρδος / ζημιά</th><th>Απόδοση</th><th>Μέγιστη πτώση</th><th>Κλεισμένες συναλλαγές</th></tr></thead><tbody>{"".join(table)}</tbody></table></div><p class="muted">Το τελικό κεφάλαιο περιλαμβάνει και το κέρδος ή τη ζημιά θέσεων που παραμένουν ανοιχτές στο τέλος. Η μέγιστη πτώση μετρά την υποχώρηση από προηγούμενη κορυφή, όχι το τελικό αποτέλεσμα.</p></section>
<section><h2>Εξέλιξη κεφαλαίου</h2><div class="legend">{"".join(legend)}</div>{chart}<p class="muted">Οριζόντια: ημερομηνίες UTC. Κάθετα: αξία λογαριασμού σε USDT. Το διάγραμμα αραιώνει σημεία για ταχύτητα· τα αποτελέσματα και η μέγιστη πτώση προκύπτουν από το πλήρες ιστορικό.</p></section>{hold}
<section><details><summary>Αναλυτικά στοιχεία και έλεγχος αποφάσεων</summary><div class="scroll"><table><thead><tr><th>Στρατηγική</th><th>Κερδοφόρες συναλλαγές</th><th>Profit factor</th><th>Πραγματοποιημένο PnL</th><th>Ανοιχτό PnL</th><th>Σφάλματα AI</th><th>Διορθωμένες απαντήσεις AI</th><th>Απορρίψεις αποφάσεων</th></tr></thead><tbody>{"".join(details)}</tbody></table></div><p class="muted">Διορθωμένη απάντηση: το AI έδωσε αρχικά λάθος μορφή και επέστρεψε έγκυρη απάντηση στη μία διορθωτική προσπάθεια. Μετράται η συνολική καθυστέρηση. Οι απορρίψεις περιλαμβάνουν φίλτρα στρατηγικής, όρια ρίσκου και ληγμένες αποφάσεις. Δεν είναι όλες σφάλματα. Κάθε στρατηγική έχει ανεξάρτητο εικονικό λογαριασμό.</p><p class="muted">Οι παραδοχές χρόνου/εκτέλεσης βρίσκονται στο αποθηκευμένο run.json. Οι ενδοκερικές διαδρομές είναι προσομοίωση, όχι πραγματικά ticks. Το backtest δεν αποδεικνύει μελλοντική κερδοφορία.</p><a href="comparison.json">Λήψη αναλυτικών αποτελεσμάτων JSON</a></details></section></body></html>"""


def saved_comparison_html(directory):
    """Read existing evidence without writing files or invoking models/replay."""
    directory = Path(directory).resolve()
    summary = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
    results = {}
    for name in summary["candidates"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Invalid saved candidate directory")
        paths = [directory / name / filename for filename in ("run.json", "metrics.json", "equity.csv")]
        if any(not path.resolve().is_relative_to(directory) for path in paths):
            raise ValueError("Saved report path escapes its directory")
        run = json.loads(paths[0].read_text(encoding="utf-8"))
        if _provider(run["advisor"]).get("provider") == "cash" or name == "cash":
            continue
        metrics = json.loads(paths[1].read_text(encoding="utf-8"))
        rows, stride, seen, last = [], 1, 0, None
        low, high = math.inf, -math.inf
        with paths[2].open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                last = {key: _number(row[key]) for key in ("observed_at", "equity")}
                low, high = min(low, last["equity"]), max(high, last["equity"])
                if len(rows) >= 2000:
                    rows, stride = rows[::2], stride * 2
                if seen % stride == 0:
                    rows.append(last)
                seen += 1
        if last is not None and (not rows or rows[-1] != last):
            rows.append(last)
        results[name] = {**run, "metrics": metrics, "equity": rows}
        if rows:
            results[name]["plot_bounds"] = {"low": low, "high": high}
    if not results:
        raise ValueError("The saved comparison has no trading strategies")
    return comparison_html(results, reference=summary.get("buy_and_hold_reference"), saved=True)
