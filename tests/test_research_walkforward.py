import pytest

from quantbot.research.walkforward import Fold, WalkForwardPlan, make_folds, run_walk_forward


def test_rolling_folds_are_chronological_embargoed_and_leave_final_holdout_untouched():
    plan = make_folds(
        100,
        train_size=10,
        validation_size=5,
        test_size=10,
        gap=2,
        holdout_size=20,
    )
    assert plan.holdout_start == 80
    assert plan.holdout_end == 100
    assert plan.gap == 2
    assert plan.folds[0] == Fold(0, 10, 12, 17, 19, 29)
    assert plan.folds[1].test_start == plan.folds[0].test_end
    for fold in plan.folds:
        assert fold.train_start < fold.train_end <= fold.validation_start < fold.validation_end
        assert fold.validation_end <= fold.test_start < fold.test_end <= plan.holdout_start
        assert fold.validation_start - fold.train_end >= plan.gap
        assert fold.test_start - fold.validation_end >= plan.gap
    assert all(earlier.test_end <= later.test_start for earlier, later in zip(plan.folds, plan.folds[1:]))


def test_expanding_training_windows_grow_without_using_final_holdout():
    plan = make_folds(
        100,
        train_size=10,
        validation_size=5,
        test_size=10,
        gap=1,
        holdout_size=10,
        expanding=True,
    )
    assert plan.folds[0].train_start == 0
    assert plan.folds[0].train_end == 10
    assert plan.folds[1].train_start == 0
    assert plan.folds[1].train_end > plan.folds[0].train_end
    assert all(fold.test_end <= plan.holdout_start for fold in plan.folds)


def test_walk_forward_selects_using_all_validation_candidates_then_tests_only_winner():
    plan = make_folds(
        60,
        train_size=8,
        validation_size=4,
        test_size=6,
        gap=1,
        holdout_size=8,
    )
    calls = []

    def evaluate(candidate, start, end):
        calls.append((candidate, start, end))
        if start >= plan.holdout_start:
            raise AssertionError("final holdout must not be evaluated")
        score = {"slow": 1.0, "fast": 2.0}[candidate]
        return {"score": score if end != start + 6 else -score, "ending_equity": 100 + score}

    results = run_walk_forward(plan, evaluate, ["slow", "fast"])
    for fold, result in zip(plan.folds, results):
        validation_calls = [
            call for call in calls if call[1:3] == (fold.validation_start, fold.validation_end)
        ]
        test_calls = [call for call in calls if call[1:3] == (fold.test_start, fold.test_end)]
        assert [item[0] for item in validation_calls] == ["slow", "fast"]
        assert test_calls == [("fast", fold.test_start, fold.test_end)]
        assert result["selected"] == "fast"
        assert result["validation_score"] == 2.0
        assert result["test_result"]["score"] == -2.0
    assert all(plan.holdout_end <= start or end <= plan.holdout_start for _, start, end in calls)


def test_equal_validation_scores_choose_first_candidate_deterministically():
    plan = make_folds(30, train_size=5, validation_size=3, test_size=4)
    result = run_walk_forward(plan, lambda _candidate, _start, _end: 1.0, ["first", "second"])
    assert all(item["selected"] == "first" for item in result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_rows": True, "train_size": 1, "validation_size": 1, "test_size": 1},
        {"n_rows": 20, "train_size": 0, "validation_size": 1, "test_size": 1},
        {"n_rows": 20, "train_size": 1, "validation_size": 1, "test_size": 1, "gap": -1},
        {"n_rows": 20, "train_size": 1, "validation_size": 1, "test_size": 1, "holdout_size": 20},
        {"n_rows": 10, "train_size": 5, "validation_size": 3, "test_size": 3},
    ],
)
def test_invalid_or_insufficient_fold_requests_are_rejected(kwargs):
    with pytest.raises(ValueError):
        make_folds(**kwargs)


@pytest.mark.parametrize(
    "candidates,evaluator",
    [
        ([], lambda *_: 1),
        (["same", "same"], lambda *_: 1),
        (["candidate"], lambda *_: float("nan")),
        (["candidate"], lambda *_: {"not_score": 1}),
        (["candidate"], lambda *_: None),
    ],
)
def test_walk_forward_rejects_empty_duplicate_or_malformed_candidates_and_scores(candidates, evaluator):
    plan = make_folds(30, train_size=5, validation_size=3, test_size=4)
    with pytest.raises((ValueError, RuntimeError)):
        run_walk_forward(plan, evaluator, candidates)


def test_walk_forward_propagates_failed_candidate_as_study_error_not_zero_score():
    plan = make_folds(30, train_size=5, validation_size=3, test_size=4)

    def evaluate(candidate, _start, _end):
        if candidate == "broken":
            raise OSError("candidate model failed")
        return 100.0

    with pytest.raises(RuntimeError, match="Validation evaluation failed"):
        run_walk_forward(plan, evaluate, ["healthy", "broken"])


def test_walk_forward_rejects_overlapping_test_folds_in_manual_plan():
    plan = WalkForwardPlan(
        (
            Fold(0, 5, 5, 8, 8, 12),
            Fold(0, 5, 5, 8, 10, 14),
        ),
        holdout_start=20,
        holdout_end=20,
        gap=0,
    )
    with pytest.raises(ValueError, match="must not overlap"):
        run_walk_forward(plan, lambda *_: 1.0, ["candidate"])
