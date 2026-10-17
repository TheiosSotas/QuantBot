from dataclasses import asdict

from quantbot.config import Config
from quantbot.research.advisors import CashAdvisor
from quantbot.research.cli import _strategy_experiment_matrix
from quantbot.research.comparison import compare
from quantbot.research.synthetic import demo_datasets


def test_strategy_experiment_matrix_changes_one_control_at_a_time():
    base = Config()
    advisors, configs = _strategy_experiment_matrix({"cash": CashAdvisor()}, base)

    assert set(advisors) == {
        "cash__control",
        "cash__no_scale",
        "cash__no_confidence",
        "cash__trend_filter",
    }
    assert set(configs) == set(advisors)
    baseline = asdict(base)
    for name, candidate in configs.items():
        changed = {key for key, value in asdict(candidate).items() if value != baseline[key]}
        expected = {
            "cash__control": set(),
            "cash__no_scale": {"scale_enabled"},
            "cash__no_confidence": {"confidence_gate_enabled"},
            "cash__trend_filter": {"min_trend_strength"},
        }
        assert changed == expected[name]
    assert configs["cash__no_scale"].scale_enabled is False
    assert configs["cash__no_confidence"].confidence_gate_enabled is False
    assert configs["cash__trend_filter"].min_trend_strength == 0.25


def test_compare_records_each_strategy_config_and_keeps_defaults(tmp_path):
    hourly, minute = demo_datasets(hours=1)
    config = Config()
    advisors, candidate_configs = _strategy_experiment_matrix({"cash": CashAdvisor()}, config)

    report = compare(
        hourly,
        advisors,
        config=config,
        candidate_configs=candidate_configs,
        execution_data=minute,
        output=tmp_path / "comparison",
        start_index=600,
        end_index=601,
        publication_delay_seconds=0,
        fixed_latency_seconds=0,
    )

    assert set(report["candidates"]) == set(candidate_configs)
    assert report["candidates"]["cash__control"]["config"]["scale_enabled"] is True
    assert report["candidates"]["cash__no_scale"]["config"]["scale_enabled"] is False
    assert report["candidates"]["cash__no_confidence"]["config"]["confidence_gate_enabled"] is False
    assert report["candidates"]["cash__trend_filter"]["config"]["min_trend_strength"] == 0.25
    assert config.scale_enabled is True
    assert config.confidence_gate_enabled is True
    assert config.min_trend_strength == 0


def test_compare_rejects_candidate_configs_that_change_unrelated_settings():
    hourly, _ = demo_datasets(hours=1)
    config = Config()

    try:
        compare(
            hourly,
            {"cash": CashAdvisor()},
            config=config,
            candidate_configs={"cash": Config(starting_cash=12_000)},
        )
    except ValueError as exc:
        assert "non-strategy settings" in str(exc)
    else:
        raise AssertionError("comparison accepted a candidate with different starting cash")
