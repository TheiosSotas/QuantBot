import csv
import hashlib
import json
import threading
from copy import deepcopy

import pytest
import requests

from quantbot.research.presentation import comparison_html, saved_comparison_html


def result(provider="rules", *, ending=9781.234, total_return=-0.02188):
    return {
        "dataset": {"first_timestamp": 1704067200000, "source": {"type": "historical"}},
        "start_index": 600,
        "end_index": 602,
        "advisor": {"provider": provider},
        "equity": [
            {"observed_at": 1706227200, "equity": 10000},
            {"observed_at": 1706234400, "equity": ending},
        ],
        "metrics": {
            "starting_cash": 10000,
            "ending_equity": ending,
            "total_return": total_return,
            "max_drawdown": 0.23817,
            "trade_count": 528,
            "model_errors": 0,
            "invalid_or_expired_decisions": 284,
            "win_rate": 0.4,
            "profit_factor": 0.99,
            "realized_pnl": -300,
            "unrealized_pnl": 81.234,
        },
    }


def test_final_amount_is_read_from_saved_equity_not_rounded_percentage():
    page = comparison_html({"rules": result(), "cash": result("cash", ending=10000, total_return=0)})
    assert "$9.781,23" in page
    assert "$-218,77" in page
    assert "$10.000,00" in page
    assert "Τελικό κεφάλαιο" in page
    assert "cash" not in page
    assert " — $" not in page
    assert "Κεφάλαιο (USDT)" in page
    assert "25/01/2024" not in page  # Warmup is not shown as evaluation start.
    assert "26/01/2024" in page


def test_positive_negative_and_open_position_totals_are_distinct():
    page = comparison_html(
        {"rules": result(), "numeric": result("numeric", ending=10451, total_return=0.0451)}
    )
    assert "$10.451,00" in page and "$+451,00" in page
    assert "$-300,00" in page  # Realized PnL is separate from the full account change.
    assert "$-218,77" in page and "$81,23" in page
    assert "ανοιχτές" in page and "23,82%" in page


def test_model_errors_and_untrusted_labels_remain_visible_and_escaped():
    item = result()
    item["advisor"] = {
        "provider": "cached-advisor",
        "advisor": {"provider": "ollama", "model": "<script>bad</script>"},
    }
    item["metrics"]["model_errors"] = 3
    page = comparison_html({"ollama_1": item})
    assert "<script>bad</script>" not in page
    assert "&lt;script&gt;bad&lt;/script&gt;" in page
    assert "3 σφάλματα AI" in page


def test_buy_and_hold_reference_is_separate_from_the_cash_baseline():
    page = comparison_html({"rules": result()}, reference={"metrics": result(ending=12000)["metrics"]})
    assert "αγορά BTC και διακράτηση" in page
    assert "$12.000,00" in page
    assert "χωρίς stop" in page


def write_saved(folder, *, name="rules", item=None):
    item = deepcopy(item or result())
    (folder / name).mkdir(parents=True)
    (folder / "comparison.json").write_text(
        json.dumps({"candidates": {name: {"identity": item["advisor"], "metrics": item["metrics"]}}}),
        encoding="utf-8",
    )
    (folder / name / "run.json").write_text(
        json.dumps({k: v for k, v in item.items() if k not in ("equity", "metrics")}), encoding="utf-8"
    )
    (folder / name / "metrics.json").write_text(json.dumps(item["metrics"]), encoding="utf-8")
    with (folder / name / "equity.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("observed_at", "equity"))
        writer.writeheader()
        writer.writerows(item["equity"])


def test_old_report_is_redrawn_without_replay_or_modifying_any_evidence(tmp_path):
    write_saved(tmp_path)
    (tmp_path / "report.html").write_text("OLD REPORT", encoding="utf-8")
    hashes = {
        p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    page = saved_comparison_html(tmp_path)
    assert "$9.781,23" in page
    assert "Δεν έγινε νέα εκτέλεση" in page
    assert hashes == {
        p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }


def test_saved_report_rejects_escaping_candidate_path(tmp_path):
    (tmp_path / "comparison.json").write_text(json.dumps({"candidates": {"../private": {}}}))
    with pytest.raises(ValueError, match="candidate"):
        saved_comparison_html(tmp_path)


def test_control_serves_updated_saved_report_and_caches_without_replay(tmp_path):
    from quantbot.control import Control, make_server

    control = Control(tmp_path)
    folder = tmp_path / "research-runs" / "existing"
    write_saved(folder)
    (folder / "report.html").write_text("OLD REPORT", encoding="utf-8")
    server = make_server(control)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/reports/existing/"
        response = requests.get(url, timeout=5)
        assert response.status_code == 200 and "$9.781,23" in response.text
        assert "OLD REPORT" not in response.text
        assert len(control.report_cache) == 1
        assert requests.get(url, timeout=5).content == response.content
        assert (folder / "report.html").read_text() == "OLD REPORT"
        assert not (tmp_path / "quantbot.sqlite3").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_flat_curve_draws_finite_axes():
    item = result(ending=10000, total_return=0)
    page = comparison_html({"rules": item})
    assert "nan" not in page and "inf" not in page
    assert "Εξέλιξη κεφαλαίου" in page
