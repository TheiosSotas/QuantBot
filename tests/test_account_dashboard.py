"""Read-only dashboard and malformed-input evidence at public boundaries."""

import json
import subprocess
import sys
import threading
from urllib.request import urlopen

import pytest
from conftest import NOW, apply, decision

from quantbot.dashboard import make_server


def test_history_api_reads_committed_rows_from_http_thread(engine):
    assert apply(engine) == "OPENED"
    server = make_server(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(base + "/api/history") as response:
            assert response.headers["Cache-Control"] == "no-store"
            history = json.load(response)
        assert history["equity"][-1]["side"] == "LONG"
        assert history["decisions"][-1]["reason_code"] == "ENTRY"
        assert history["decisions"][-1]["status"] == "ACCEPTED"
        with urlopen(base + "/api/state") as response:
            state = json.load(response)
        assert state["account_limits"]["daily_loss_limit"] is None
        assert state["account_limits"]["max_account_drawdown"] is None
        assert state["account_limits"]["cooldown_seconds"] == 0
        assert state["system"]["entry_block_reason"] == "STALE_PRICE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("bad_action", [{"override": "BUY"}, ["BUY"], None])
def test_malformed_action_still_gets_a_safe_rejection_record(engine, bad_action):
    data = decision()
    data["action"] = bad_action
    with pytest.raises(ValueError):
        apply(engine, data)
    row = engine.store.decision_history()[-1]
    assert row["reason_code"] == "INVALID_MODEL_OUTPUT"
    assert row["action"] is None
    assert row["observed_at"] == NOW
    assert engine.state["portfolio"]["side"] == "FLAT"


@pytest.mark.parametrize(
    "option,value",
    [
        ("--daily-loss-limit", "nan"),
        ("--max-account-drawdown", "1"),
        ("--cooldown-seconds", "-1"),
        ("--equity-sample-seconds", "0"),
    ],
)
def test_cli_rejects_invalid_controls_before_creating_account(tmp_path, option, value):
    account = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, "quant_bot.py", "--no-browser", "--data-dir", str(account), option, value],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert not account.exists()
