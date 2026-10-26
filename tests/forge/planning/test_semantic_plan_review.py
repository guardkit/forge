"""Forge reads specialist semantic decisions and exact plan identity; the
traceability score and the Coach's verdict are advice (step 2)."""

from __future__ import annotations

import json

import pytest

from forge.planning.driver import PlanningRunDriver


@pytest.fixture()
def files() -> dict[str, str]:
    return {
        ".guardkit/features/FEAT-1234.yaml": "id: FEAT-1234\n",
        "tasks/backlog/example/TASK-1234-001.md": "# task\n",
    }


def _receipt(
    files: dict[str, str],
    **overrides: object,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": 1,
        "decision": "approved",
        "criterion": "request_traceability",
        "criterion_score": 1.0,
        "coach_verdict": "GOOD",
        "artifact_identity": PlanningRunDriver._plan_artifact_identity(files),
        "reviewed_after_rewrite": False,
    }
    receipt.update(overrides)
    return receipt


def _role_output(files: dict[str, str], receipt: object) -> dict[str, object]:
    return {
        **files,
        "semantic_review.json": (
            json.dumps(receipt) if not isinstance(receipt, str) else receipt
        ),
    }


def test_exact_approved_receipt_passes(files: dict[str, str]) -> None:
    review, error = PlanningRunDriver._semantic_review_of(
        _role_output(files, _receipt(files)), files
    )
    assert error is None
    assert review is not None and review["decision"] == "approved"


@pytest.mark.parametrize(
    ("role_output", "expected"),
    [
        ({}, "missing"),
        ({"semantic_review.json": "not-json"}, "not parseable"),
        (
            {"semantic_review.json": json.dumps({"decision": "approved"})},
            "wrong fields",
        ),
    ],
)
def test_missing_or_malformed_review_fails_closed(
    files: dict[str, str],
    role_output: dict[str, object],
    expected: str,
) -> None:
    review, error = PlanningRunDriver._semantic_review_of(role_output, files)
    assert review is None
    assert error is not None and expected in error


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"decision": "rejected"}, "not approved"),
        ({"criterion": "wave_sanity"}, "wrong criterion"),
        ({"criterion_score": float("nan")}, "not a number from 0 to 1"),
        ({"criterion_score": float("inf")}, "not a number from 0 to 1"),
        ({"criterion_score": 1.5}, "not a number from 0 to 1"),
        ({"coach_verdict": 3}, "not text"),
        ({"reviewed_after_rewrite": "yes"}, "not boolean"),
    ],
)
def test_non_approving_semantic_decisions_fail_closed(
    files: dict[str, str],
    override: dict[str, object],
    expected: str,
) -> None:
    review, error = PlanningRunDriver._semantic_review_of(
        _role_output(files, _receipt(files, **override)), files
    )
    assert review is None
    assert error is not None and expected in error


@pytest.mark.parametrize(
    "override",
    [{"criterion_score": 0.0}, {"criterion_score": 0.4}, {"coach_verdict": "REVISE"}],
)
def test_a_low_score_or_verdict_is_advice_not_a_refusal(
    files: dict[str, str],
    override: dict[str, object],
) -> None:
    """Step 2 (26 October 2026): the traceability score and the Coach's
    verdict are read and kept for the build card; they never refuse."""
    review, error = PlanningRunDriver._semantic_review_of(
        _role_output(files, _receipt(files, **override)), files
    )
    assert error is None
    assert review is not None
    for key, value in override.items():
        assert review[key] == value


def test_changed_artifact_loses_old_approval(files: dict[str, str]) -> None:
    receipt = _receipt(files)
    changed = dict(files)
    changed["tasks/backlog/example/TASK-1234-001.md"] = "# rewritten\n"

    review, error = PlanningRunDriver._semantic_review_of(
        _role_output(changed, receipt), changed
    )
    assert review is None
    assert error is not None and "digest does not match" in error


def test_review_receipt_is_not_committed_as_a_plan_file(
    files: dict[str, str],
) -> None:
    role_output = _role_output(files, _receipt(files))
    projected = PlanningRunDriver._plan_tree_files(role_output)
    assert projected == files


def test_dispatcher_turns_final_review_revision_into_exact_review_argument() -> None:
    from forge.pipeline.dispatchers.specialist import (
        FINAL_PLAN_REVIEW_FEEDBACK,
        build_specialist_command,
    )
    from forge.pipeline.stage_taxonomy import StageClass

    revision = {"tasks/backlog/example/TASK-1234-001.md": "# normalized\n"}
    _command, args = build_specialist_command(
        StageClass.FEATURE_PLAN,
        request_text=None,
        context_entries=[],
        extra_command_args={
            "feature_id": "FEAT-1234",
            "revision_of": revision,
            "validate_feedback": FINAL_PLAN_REVIEW_FEEDBACK,
        },
    )

    assert args["semantic_review_artifacts"] == revision


def test_huge_integer_semantic_score_fails_closed_without_float_conversion(
    files: dict[str, str],
) -> None:
    receipt = _receipt(files, criterion_score=10**10000)
    role_output = {**files, "semantic_review.json": receipt}

    review, error = PlanningRunDriver._semantic_review_of(role_output, files)

    assert review is None
    assert error is not None and "not a number from 0 to 1" in error
