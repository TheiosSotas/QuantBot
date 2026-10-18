"""Explicit local inference check; synthetic input can never reach an account."""

import time
from urllib.parse import urlsplit, urlunsplit

from .ai import ModelClient
from .models import Snapshot


def check_model(config, *, client=None, stage=None, cancelled=None):
    client = client or ModelClient(config)
    stage = stage or (lambda message: None)
    cancelled = cancelled or (lambda: False)

    def check_cancel():
        if cancelled():
            raise RuntimeError("Ακυρώθηκε ο έλεγχος AI.")

    check_cancel()
    stage("Φόρτωση μοντέλου AI — δεν γίνεται συναλλαγή")
    warmup = client.warmup()
    check_cancel()
    stage("Δοκιμή έγκυρης απόφασης με τεχνητά δεδομένα")
    now = time.time()
    candle = (int(now) // 3600 - 1) * 3_600_000
    sample = Snapshot(candle, now, 100, 98, 95, 90, 50, 5, 1)
    started = time.perf_counter()
    _decision, trace = client.consult_with_trace(
        sample,
        {
            "side": "FLAT",
            "cash_usdt": 10000,
            "total_equity": 10000,
            "position_units": 0,
            "entry_price": 0,
            "stop_loss": 0,
            "take_profit": 0,
            "tranches": 0,
            "unrealized_pnl": 0,
            "breakeven_active": False,
        },
    )
    elapsed = time.perf_counter() - started
    check_cancel()
    result = {
        "model": config.model,
        "ok": True,
        "warmup_seconds": warmup["seconds"],
        "decision_seconds": elapsed,
        "repaired": trace["recovered"],
        "memory": None,
        "paper_budget_ok": elapsed + config.poll_seconds < config.max_decision_age,
        "note": "Έλεγχος λειτουργίας με τεχνητά δεδομένα. Δεν αξιολογεί κερδοφορία.",
    }
    try:
        parts = urlsplit(config.ollama_url)
        url = urlunsplit((parts.scheme, parts.netloc, "/api/ps", "", ""))
        response = client.session.get(url, timeout=(2, 3))
        try:
            response.raise_for_status()
            for item in response.json().get("models", []):
                if item.get("name", item.get("model")) == config.model:
                    result["memory"] = {
                        key: item.get(key) if type(item.get(key)) is int and item[key] >= 0 else None
                        for key in ("size", "size_vram", "context_length")
                    }
                    break
        finally:
            response.close()
    except Exception:
        result["memory"] = None  # Optional telemetry must not invalidate successful inference.
    return result
