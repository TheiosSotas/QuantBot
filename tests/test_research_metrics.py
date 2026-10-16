import json

import pytest

from quantbot.research.metrics import summarize


def row(at, equity, exposure=0.0, *, peak=None, drawdown=0.0, unrealized=None):
    result = {
        "observed_at": at,
        "equity": equity,
        "exposure": exposure,
        "peak_equity": max(equity, peak if peak is not None else equity),
        "drawdown": drawdown,
    }
    if unrealized is not None:
        result["unrealized_pnl"] = unrealized
    return result


def test_metrics_keep_starting_cash_as_initial_performance_baseline_and_are_json_safe():
    summary = summarize(
        [row(1_700_000_000, 10_100, 0.25, peak=10_100), row(1_700_000_060, 10_200, 0.5, peak=10_200)],
        [{"pnl": 200}, {"pnl": -50}, {"pnl": 0}],
        starting_cash=10_000,
    )
    assert summary["total_return"] == pytest.approx(0.02)
    assert summary["starting_cash"] == 10_000
    assert summary["ending_equity"] == 10_200
    assert summary["max_drawdown"] == 0
    assert summary["trade_count"] == 3
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == pytest.approx(1 / 3)
    assert summary["realized_pnl"] == 150
    assert summary["profit_factor"] == 4
    assert summary["no_loss"] is False
    assert summary["expectancy"] == 50
    assert summary["time_weighted_exposure_pct"] == pytest.approx(25)
    assert summary["max_losing_streak"] == 1
    assert summary["fees_included"] is False
    assert summary["funding_included"] is False
    assert summary["return_basis"] == "marked_to_market_equity"
    json.dumps(summary, allow_nan=False)


def test_max_drawdown_uses_starting_capital_and_exceeds_one_for_negative_equity():
    summary = summarize(
        [row(1_700_000_000, -1_000, peak=10_000, drawdown=1.1)],
        [],
        starting_cash=10_000,
    )
    assert summary["ending_equity"] == -1_000
    assert summary["total_return"] == pytest.approx(-1.1)
    assert summary["max_drawdown"] == pytest.approx(1.1)
    assert summary["profit_factor"] is None
    assert summary["no_loss"] is True
    assert summary["win_rate"] is None
    assert summary["expectancy"] is None


def test_daily_returns_use_utc_end_of_day_marks_and_daily_annualization():
    day0 = 1_700_000_000  # 2023-11-14 UTC
    day1 = day0 + 24 * 60 * 60
    summary = summarize(
        [
            row(day0, 10_100),
            row(day0 + 60, 10_200),
            row(day1 + 60, 10_300),
            row(day1 + 120, 10_400),
        ],
        [],
        starting_cash=10_000,
        periods_per_year=365,
    )
    assert summary["daily_returns"][0]["return"] == pytest.approx(0.02)
    assert summary["daily_returns"][1]["return"] == pytest.approx(10_400 / 10_200 - 1)
    assert summary["sharpe_daily"] is not None
    assert summary["periods_per_year"] == 365


def test_daily_sharpe_needs_two_defined_daily_returns_and_unrealized_is_optional():
    one_day = summarize([row(1_700_000_000, 10_100, unrealized=100)], [], starting_cash=10_000)
    assert one_day["sharpe_daily"] is None
    assert one_day["unrealized_pnl"] == 100
    assert one_day["time_weighted_exposure_pct"] is None

    with_zero_baseline = summarize(
        [row(1_700_000_000, 0), row(1_700_086_400, 0), row(1_700_172_800, -100)],
        [],
        starting_cash=10_000,
    )
    assert with_zero_baseline["daily_returns"] == [
        {"date": "2023-11-14", "return": -1.0},
        {"date": "2023-11-15", "return": None},
        {"date": "2023-11-16", "return": None},
    ]
    assert with_zero_baseline["sharpe_daily"] is None
    json.dumps(with_zero_baseline, allow_nan=False)


def test_time_weighted_exposure_is_left_continuous_and_max_loss_streak_uses_trade_order():
    summary = summarize(
        [row(0, 100, 0.2), row(10, 100, 0.8), row(30, 100, 0.0)],
        [{"pnl": -1}, {"pnl": -2}, {"pnl": 3}, {"pnl": -4}],
        starting_cash=100,
    )
    # 0.2 for 10 seconds and 0.8 for 20 seconds; the final sample has no following interval.
    assert summary["time_weighted_exposure_pct"] == pytest.approx(60)
    assert summary["time_in_market_pct"] == 100
    assert summary["max_losing_streak"] == 2


def test_time_in_market_is_independent_of_notional_exposure():
    summary = summarize(
        [row(0, 100, 0.0), row(10, 100, 0.25), row(30, 100, 0.0)],
        [],
        starting_cash=100,
    )
    assert summary["time_weighted_exposure_pct"] == pytest.approx(16.6666667)
    assert summary["time_in_market_pct"] == pytest.approx(66.6666667)


@pytest.mark.parametrize(
    "rows,trades,starting_cash",
    [
        ([row(2, 100), row(1, 100)], [], 100),
        ([row(1, float("nan"))], [], 100),
        ([row(1, 100, exposure=True)], [], 100),
        ([{"observed_at": 1, "equity": 100}], [], 100),
        ([], [{"pnl": float("inf")}], 100),
        ([], [], 0),
        ([], [], True),
    ],
)
def test_invalid_metric_inputs_are_rejected(rows, trades, starting_cash):
    with pytest.raises(ValueError):
        summarize(rows, trades, starting_cash=starting_cash)
