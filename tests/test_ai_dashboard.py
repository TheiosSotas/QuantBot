import json
import threading
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from conftest import decision, snapshot

from quantbot.ai import ModelClient
from quantbot.dashboard import make_server


def test_model_uses_schema_and_validates_response(config):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"response": json.dumps(decision())}
        )

    model = ModelClient(config, SimpleNamespace(post=post))
    assert model.consult(snapshot(), {}).action == "BUY"
    assert calls[0]["json"]["format"]["properties"]["scale_in_signal"] == {
        "type": "boolean", "enum": [False]
    }
    assert "MANDATORY HARD RULE" not in calls[0]["json"]["system"]


@pytest.mark.parametrize("raw", ["[]", '{"action":"BUY"}', "not JSON"])
def test_invalid_model_response_is_rejected(config, raw):
    session = SimpleNamespace(
        post=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"response": raw})
    )
    with pytest.raises(ValueError):
        ModelClient(config, session).consult(snapshot(), {})


def test_unknown_schema_fields_are_not_silently_accepted(config):
    data = decision()
    data["override_stop"] = True
    session = SimpleNamespace(
        post=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"response": json.dumps(data)}
        )
    )
    with pytest.raises(ValueError, match="schema"):
        ModelClient(config, session).consult(snapshot(), {})


def test_dashboard_serves_escaped_paper_ui_and_health(engine):
    server = make_server(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(base) as response:
            html = response.read().decode()
        assert "PAPER TRADING" in html
        assert "RISK-FREE" not in html
        assert "${esc(item.message)}" in html
        with urlopen(base + "/api/state") as response:
            assert response.headers.get("Access-Control-Allow-Origin") is None
            state = json.load(response)
        assert state["system"]["mode"] == "PAPER"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
