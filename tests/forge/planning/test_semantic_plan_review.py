"""The plan writer's review receipt is not committed as a plan file."""

from __future__ import annotations

import json

from tests.forge.planning._plan_identity import plan_artifact_identity
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
        "artifact_identity": plan_artifact_identity(files),
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



def test_review_receipt_is_not_committed_as_a_plan_file(
    files: dict[str, str],
) -> None:
    role_output = _role_output(files, _receipt(files))
    projected = PlanningRunDriver._plan_tree_files(role_output)
    assert projected == files
