import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests

from quantbot.control import Control, make_server, parse_range, parse_settings


@pytest.fixture
def server(tmp_path):
    control = Control(tmp_path)
    control.ollama = lambda: ({"ok": True, "error": None}, [{"name": "qwen2.5:14b", "size": 1}])
    server = make_server(control)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield control, url
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def headers(control, url):
    return {"Origin": url, "X-QuantBot-Token": control.token}


def test_page_and_status(server):
    control, url = server
    page = requests.get(url, timeout=3)
    assert page.status_code == 200
    assert "__TOKEN__" not in page.text
    assert control.token in page.text
    status = requests.get(url + "/api/status", timeout=3).json()
    assert status["paper"]["running"] is False
    assert status["job"]["running"] is False
    assert status["models"][0]["name"] == "qwen2.5:14b"
    assert not (control.root / "quantbot.sqlite3").exists()


@pytest.mark.parametrize("worker_pid", [1234, 5678])
def test_paper_dashboard_url_accepts_windows_python_redirector(server, worker_pid):
    control, url = server
    control.paper = Mock(pid=1234)
    control.paper.poll.return_value = None
    control.paper_info = control.runtime / "paper-current-launch.json"
    control.paper_info.write_text(
        json.dumps({"pid": worker_pid, "port": 43210, "url": "http://127.0.0.1:43210"}),
        encoding="utf-8",
    )

    paper = requests.get(url + "/api/status", timeout=3).json()["paper"]
    assert paper["running"] is True
    assert paper["url"] == "http://127.0.0.1:43210"

    control.paper.poll.return_value = 0
    paper = requests.get(url + "/api/status", timeout=3).json()["paper"]
    assert paper["running"] is False
    assert paper["url"] is None


@pytest.mark.parametrize("port", [0, 65536, "invalid"])
def test_paper_dashboard_handoff_rejects_invalid_ports(server, port):
    control, url = server
    control.paper = Mock(pid=1234)
    control.paper.poll.return_value = None
    control.paper_info = control.runtime / "paper-current-launch.json"
    control.paper_info.write_text(json.dumps({"pid": 5678, "port": port}), encoding="utf-8")
    assert requests.get(url + "/api/status", timeout=3).json()["paper"]["url"] is None


def test_cross_origin_and_missing_token_cannot_run_actions(server):
    control, url = server
    with patch.object(control, "action") as action:
        for request_headers in (
            {},
            {"Origin": "https://example.com", "X-QuantBot-Token": control.token},
            {"Origin": url, "X-QuantBot-Token": "wrong"},
        ):
            response = requests.post(
                url + "/api/action", json={"action": "demo"}, headers=request_headers, timeout=3
            )
            assert response.status_code == 403
        action.assert_not_called()


def test_dns_rebinding_host_rejected(server):
    _, url = server
    assert requests.get(url, headers={"Host": "evil.example"}, timeout=3).status_code == 403


def test_authorized_action_and_rejected_unknown_action(server):
    control, url = server
    with patch.object(control, "paper_stop_request") as stop:
        response = requests.post(
            url + "/api/action", json={"action": "paper_stop"}, headers=headers(control, url), timeout=3
        )
        assert response.status_code == 200
        stop.assert_called_once()
    response = requests.post(
        url + "/api/action", json={"action": "arbitrary_command"}, headers=headers(control, url), timeout=3
    )
    assert response.status_code == 400


def test_reports_only_from_run_directory(server):
    control, url = server
    report = control.root / "research-runs" / "example" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("<h1>Report</h1>", encoding="utf-8")
    assert requests.get(url + "/reports/example", timeout=3).text == "<h1>Report</h1>"
    assert requests.get(url + "/reports/%2e%2e%2fREADME.md", timeout=3).status_code == 404
    assert requests.get(url + "/api/status", timeout=3).json()["results"][0]["url"] == "/reports/example"


def test_full_calendar_leap_year_and_future_rejection():
    start, end, interval = parse_range({"start": "2024-01-01", "end": "2025-01-01"})
    assert (end - start).days == 366
    assert interval == "5m"
    tomorrow = str(datetime.now(timezone.utc).date() + timedelta(days=1))
    with pytest.raises(ValueError):
        parse_range({"start": "2024-01-01", "end": tomorrow})
    with pytest.raises(ValueError):
        parse_range({"start": "2023-01-01", "end": "2025-01-01"})


def test_controls_disabled_unless_explicit_and_reject_invalid_values():
    config, _ = parse_settings({})
    assert config.daily_loss_limit is None
    assert config.max_account_drawdown is None
    assert config.cooldown_seconds == 0
    for payload in ({"daily_loss_limit": 5}, {"starting_cash": float("nan")}, {"cooldown_seconds": -1}):
        with pytest.raises(ValueError):
            parse_settings(payload)


def test_paper_stop_signals_owned_process_without_termination(tmp_path):
    control = Control(tmp_path)
    control.paper = Mock()
    control.paper.poll.return_value = None
    control.paper_stop = tmp_path / "stop"
    control.paper_stop_request()
    assert control.paper_stop.exists()
    control.paper.terminate.assert_not_called()


def test_invalid_range_does_not_launch_job(tmp_path):
    control = Control(tmp_path)
    with pytest.raises(ValueError):
        control.start_job({"action": "backtest", "start": "bad", "end": "2025-01-01"})
    assert not control.job["running"]


def test_failed_job_has_error_and_no_report(tmp_path):
    control = Control(tmp_path)
    with patch.object(control, "research_job", side_effect=ValueError("missing data")):
        control.start_job({"action": "demo"})
        for thread in threading.enumerate():
            if thread.name == "quantbot-desktop-job":
                thread.join(timeout=3)
    assert control.job["running"] is False
    assert control.job["error"] == "missing data"
    assert control.job["report_url"] is None


def test_existing_gui_instance_checks_workspace_identity(tmp_path):
    import windows_gui

    record = tmp_path / "desktop.json"
    record.write_text(json.dumps({"url": "https://evil.example"}), encoding="utf-8")
    with patch("urllib.request.urlopen") as opening:
        assert windows_gui.existing_url(record) is None
        opening.assert_not_called()


@pytest.mark.parametrize("experiments", [False, True])
def test_gui_backtest_uses_compact_common_cli_and_preserves_active_account(tmp_path, experiments):
    control = Control(tmp_path)
    commands = []
    payload = {
        "action": "backtest",
        "start": "2025-01-01",
        "end": "2026-01-01",
        "interval": "5m",
        "model": "",
        "strategy_experiments": experiments,
    }

    def command(*args):
        commands.append([str(arg) for arg in args])
        if args[0] == "compare":
            output = next(iter(args[args.index("--output") + 1 :]))
            output.mkdir(parents=True)
            (output / "comparison.json").write_text(
                json.dumps({"candidates": {"rules": {"metrics": {"model_errors": 0}}}})
            )
            (output / "report.html").write_text("report")

    control.command = command
    control.research_job(payload, "annual-test")
    assert [args[0] for args in commands] == ["fetch", "fetch", "compare"]
    assert "5m" in commands[1]
    assert "--compact" in commands[2]
    assert commands[2][-2:] == ["--fixed-latency-seconds", "0"]
    assert "--model" not in commands[2]
    assert ("--strategy-experiments" in commands[2]) is experiments
    assert control.job["report_url"] == "/reports/annual-test"
    assert not (tmp_path / "quantbot.sqlite3").exists()


def test_invalid_experiment_flag_does_not_launch_job(tmp_path):
    control = Control(tmp_path)
    with pytest.raises(ValueError, match="παραλλαγών"):
        control.start_job(
            {
                "action": "backtest",
                "start": "2025-01-01",
                "end": "2025-01-02",
                "strategy_experiments": "false",
            }
        )
    assert control.job["running"] is False


def test_orphan_recovery_signals_only_private_paper_records(tmp_path):
    control = Control(tmp_path)
    ready = control.runtime / "paper-test.json"
    ready.write_text('{"pid":123}')
    control.stop_orphans()
    assert ready.with_suffix(".stop").exists()
    assert not (tmp_path / "quantbot.sqlite3").exists()


def test_coarse_ai_run_is_rejected_before_launching_any_job(tmp_path):
    control = Control(tmp_path)
    with pytest.raises(ValueError, match="AI"):
        control.start_job(
            {
                "action": "backtest",
                "start": "2025-01-01",
                "end": "2026-01-01",
                "interval": "5m",
                "model": "qwen2.5:14b",
            }
        )
    assert not control.job["running"]
