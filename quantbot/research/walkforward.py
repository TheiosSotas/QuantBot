"""Chronological walk-forward folds with an untouched final holdout."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    train_start: int
    train_end: int
    validation_start: int
    validation_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class WalkForwardPlan:
    folds: tuple[Fold, ...]
    holdout_start: int
    holdout_end: int
    gap: int


def _integer(value, name, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be a {qualifier} integer")


def make_folds(
    n_rows,
    *,
    train_size,
    validation_size,
    test_size,
    gap=0,
    holdout_size=0,
    expanding=False,
):
    """Build full-size test folds before a final holdout range.

    Each fold has an embargo of `gap` rows on both the train/validation and
    validation/test boundaries. Rolling folds keep a fixed training window;
    expanding folds start training at row zero and grow it over time. Test
    folds advance by `test_size`; any short remainder before the final holdout
    is left unused rather than producing a partial fold.
    """
    _integer(n_rows, "n_rows", positive=True)
    _integer(train_size, "train_size", positive=True)
    _integer(validation_size, "validation_size", positive=True)
    _integer(test_size, "test_size", positive=True)
    _integer(gap, "gap")
    _integer(holdout_size, "holdout_size")
    if type(expanding) is not bool:
        raise ValueError("expanding must be a boolean")
    if holdout_size >= n_rows:
        raise ValueError("holdout_size must be smaller than n_rows")

    holdout_start, holdout_end = n_rows - holdout_size, n_rows
    first_test_start = train_size + gap + validation_size + gap
    folds = []
    test_start = first_test_start
    while test_start + test_size <= holdout_start:
        validation_end = test_start - gap
        validation_start = validation_end - validation_size
        train_end = validation_start - gap
        train_start = 0 if expanding else train_end - train_size
        if train_start < 0:
            raise ValueError("Insufficient rows for requested walk-forward windows")
        folds.append(
            Fold(
                train_start=train_start,
                train_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
                test_start=test_start,
                test_end=test_start + test_size,
            )
        )
        test_start += test_size
    if not folds:
        raise ValueError("Insufficient rows for one complete walk-forward fold")
    return WalkForwardPlan(tuple(folds), holdout_start, holdout_end, gap)


def _candidate_list(candidates):
    if isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a nonempty iterable of candidate values")
    try:
        values = list(candidates)
    except TypeError as exc:
        raise ValueError("candidates must be a nonempty iterable of candidate values") from exc
    if not values or any(value is None for value in values):
        raise ValueError("candidates must be nonempty and cannot contain None")
    for index, candidate in enumerate(values):
        for previous in values[:index]:
            if candidate is previous:
                raise ValueError("Duplicate walk-forward candidate")
            try:
                equal = candidate == previous
            except Exception as exc:
                raise ValueError("Candidate equality must be well-defined") from exc
            if type(equal) is not bool:
                raise ValueError("Candidate equality must return a boolean")
            if equal:
                raise ValueError("Duplicate walk-forward candidate")
    return values


def _score(result):
    value = result.get("score") if isinstance(result, dict) else result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Evaluator result must be a finite scalar or mapping with a finite score")
    try:
        score = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Evaluator score must be finite") from exc
    if not math.isfinite(score):
        raise ValueError("Evaluator score must be finite")
    return score


def _validate_plan(plan):
    if not isinstance(plan, WalkForwardPlan) or not plan.folds:
        raise ValueError("plan must contain at least one walk-forward fold")
    _integer(plan.holdout_start, "holdout_start")
    _integer(plan.holdout_end, "holdout_end")
    _integer(plan.gap, "gap")
    if plan.holdout_end < plan.holdout_start:
        raise ValueError("Invalid holdout range")
    previous_test_end = None
    for fold in plan.folds:
        if not isinstance(fold, Fold):
            raise ValueError("plan contains an invalid fold")
        for name in (
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        ):
            _integer(getattr(fold, name), name)
        if not (
            fold.train_start
            < fold.train_end
            <= fold.validation_start
            < fold.validation_end
            <= fold.test_start
            < fold.test_end
            <= plan.holdout_start
        ):
            raise ValueError("Fold ranges must be ordered, nonempty, and end before the holdout")
        if (
            fold.validation_start - fold.train_end < plan.gap
            or fold.test_start - fold.validation_end < plan.gap
        ):
            raise ValueError("Fold does not respect the configured embargo gap")
        if previous_test_end is not None and fold.test_start < previous_test_end:
            raise ValueError("Outer test folds must not overlap")
        previous_test_end = fold.test_end


def run_walk_forward(plan, evaluate, candidates):
    """Select on each validation window, then evaluate only that winner on test.

    `evaluate(candidate, start, end)` may return a finite score or a mapping
    containing a finite `score`. A malformed result or evaluator exception
    fails the study explicitly; failed candidates are never assigned a default
    score. Candidate input order breaks score ties. The final holdout is never
    evaluated by this function.
    """
    _validate_plan(plan)
    if not callable(evaluate):
        raise ValueError("evaluate must be callable")
    values = _candidate_list(candidates)
    results = []
    for fold in plan.folds:
        validation_results = []
        for candidate in values:
            try:
                result = evaluate(candidate, fold.validation_start, fold.validation_end)
            except Exception as exc:
                raise RuntimeError(
                    f"Validation evaluation failed for candidate {candidate!r} "
                    f"on [{fold.validation_start}, {fold.validation_end})"
                ) from exc
            validation_results.append((candidate, _score(result), result))
        # max() keeps the first candidate when scores tie.
        selected, selected_score, _ = max(validation_results, key=lambda item: item[1])
        try:
            test_result = evaluate(selected, fold.test_start, fold.test_end)
        except Exception as exc:
            raise RuntimeError(
                f"Test evaluation failed for selected candidate {selected!r} "
                f"on [{fold.test_start}, {fold.test_end})"
            ) from exc
        test_score = _score(test_result)
        results.append(
            {
                "fold": fold,
                "selected": selected,
                "validation_score": selected_score,
                "validation_results": [
                    {"candidate": candidate, "score": score, "result": result}
                    for candidate, score, result in validation_results
                ],
                "test_score": test_score,
                "test_result": test_result,
            }
        )
    return results
