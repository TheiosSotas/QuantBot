from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import windows_launcher as launcher


def test_dates_reject_reversed_range():
    with patch("builtins.input", side_effect=["2024-02-05", "2024-01-26"]):
        with pytest.raises(ValueError):
            launcher.dates()


def test_dates_default_to_seven_completed_utc_days():
    with patch("builtins.input", side_effect=["", ""]):
        start, end = launcher.dates()
    assert (end - start).days == 7
    assert end.date() == datetime.now(timezone.utc).date()
    assert start.hour == end.hour == 0


def test_failed_research_never_opens_success_report(tmp_path):
    with (
        patch.object(launcher, "ROOT", tmp_path),
        patch.object(launcher, "research", side_effect=RuntimeError("Dataset invalid")),
        patch.object(launcher, "open_report") as browser,
        pytest.raises(RuntimeError, match="Dataset invalid"),
    ):
        launcher.demo()
    browser.assert_not_called()


def test_output_directories_do_not_collide():
    assert launcher.output_dir("demo") != launcher.output_dir("demo")
