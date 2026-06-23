"""Tests for fleet-memory runbook exemplar (TASK-FMDR-001).

Each test class mirrors one acceptance criterion so the mapping between the
criterion and its verifier stays explicit (AAA pattern, AC traceability).

Written test-first (TDD) to validate the hand-authored runbook JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.cli.runbook import _parse_runbook_file
from forge.persistence.repositories.runbook_models import Runbook, StepStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runbook_path() -> Path:
    """Path to the fleet-memory runbook exemplar."""
    return Path("forge/runbooks/RUNBOOK-fleet-memory-nas.json")


# ---------------------------------------------------------------------------
# AC-001: File exists and parses cleanly
# ---------------------------------------------------------------------------


class TestRunbookFileExistsAndParses:
    """AC-001: forge/runbooks/RUNBOOK-fleet-memory-nas.json exists and parses
    cleanly through _parse_runbook_file into a Runbook.
    """

    def test_file_exists(self, runbook_path: Path) -> None:
        """The runbook file exists at the expected location."""
        assert runbook_path.exists(), f"Runbook file not found: {runbook_path}"

    def test_file_is_valid_json(self, runbook_path: Path) -> None:
        """The runbook file contains valid JSON."""
        content = runbook_path.read_text(encoding="utf-8")
        data = json.loads(content)
        assert isinstance(data, dict), "Runbook JSON must be a dictionary"

    def test_parses_into_runbook(self, runbook_path: Path) -> None:
        """The runbook file parses cleanly into a Runbook object."""
        runbook = _parse_runbook_file(runbook_path)
        assert isinstance(runbook, Runbook), "Parsed object must be a Runbook"
        assert runbook.runbook_id == "fleet-memory-nas-deploy"


# ---------------------------------------------------------------------------
# AC-002: Two steps in order, no inline shell, no approval gates
# ---------------------------------------------------------------------------


class TestRunbookStepsStructure:
    """AC-002: The runbook contains exactly two steps in order: a deploy_compose
    step then a run_smoke_tests step; both reference the deploy directory and
    .env.deploy; neither carries any inline shell; no step requires an approval gate.
    """

    def test_has_exactly_two_steps(self, runbook_path: Path) -> None:
        """The runbook contains exactly two steps."""
        runbook = _parse_runbook_file(runbook_path)
        assert len(runbook.steps) == 2, f"Expected 2 steps, got {len(runbook.steps)}"

    def test_first_step_is_deploy_compose(self, runbook_path: Path) -> None:
        """The first step is a deploy_compose step."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[0]
        assert step.step_type == "deploy_compose"
        assert step.sequence_index == 0

    def test_second_step_is_run_smoke_tests(self, runbook_path: Path) -> None:
        """The second step is a run_smoke_tests step."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[1]
        assert step.step_type == "run_smoke_tests"
        assert step.sequence_index == 1

    def test_both_steps_reference_deploy_directory(self, runbook_path: Path) -> None:
        """Both steps reference the fleet-memory/deploy/nas directory."""
        runbook = _parse_runbook_file(runbook_path)
        for step in runbook.steps:
            assert "cwd" in step.params
            assert step.params["cwd"] == "fleet-memory/deploy/nas"

    def test_both_steps_reference_env_deploy(self, runbook_path: Path) -> None:
        """Both steps reference .env.deploy."""
        runbook = _parse_runbook_file(runbook_path)
        for step in runbook.steps:
            assert "env_file" in step.params
            assert step.params["env_file"] == ".env.deploy"

    def test_no_inline_shell(self, runbook_path: Path) -> None:
        """Neither step carries any inline shell commands."""
        runbook = _parse_runbook_file(runbook_path)
        for step in runbook.steps:
            # Inline shell would be a "command" param, not a "script" param
            assert "command" not in step.params, \
                f"Step {step.step_type} has inline 'command' param"

    def test_no_approval_gates(self, runbook_path: Path) -> None:
        """No step requires an approval gate."""
        runbook = _parse_runbook_file(runbook_path)
        for step in runbook.steps:
            assert step.status != StepStatus.awaiting_approval, \
                f"Step {step.step_type} has awaiting_approval status"


# ---------------------------------------------------------------------------
# AC-003: Freshly-parsed runbook initial state
# ---------------------------------------------------------------------------


class TestRunbookInitialState:
    """AC-003: A freshly-parsed runbook has current_step_index == 0 and its
    first step is the deploy step.
    """

    def test_current_step_index_is_zero(self, runbook_path: Path) -> None:
        """A freshly-parsed runbook has current_step_index == 0."""
        runbook = _parse_runbook_file(runbook_path)
        assert runbook.current_step_index == 0

    def test_first_step_is_deploy(self, runbook_path: Path) -> None:
        """The first step (at index 0) is the deploy_compose step."""
        runbook = _parse_runbook_file(runbook_path)
        first_step = runbook.steps[runbook.current_step_index]
        assert first_step.step_type == "deploy_compose"


# ---------------------------------------------------------------------------
# AC-004: Round-trip serialization is lossless
# ---------------------------------------------------------------------------


class TestRunbookRoundTrip:
    """AC-004: A unit test loads the saved JSON, re-serialises it, and asserts
    the round-trip is lossless — the loaded Runbook equals the authored one
    and its typed steps are unchanged.
    """

    def test_round_trip_preserves_structure(self, runbook_path: Path) -> None:
        """Round-trip serialization preserves the runbook structure."""
        # Load the original JSON
        original_data = json.loads(runbook_path.read_text(encoding="utf-8"))

        # Parse into Runbook
        runbook = _parse_runbook_file(runbook_path)

        # Re-serialize to dict
        reserialized_data = {
            "runbook_id": runbook.runbook_id,
            "target": runbook.target,
            "current_step_index": runbook.current_step_index,
            "status": runbook.status.value,
            "created_at": runbook.created_at.isoformat(),
            "steps": [
                {
                    "step_type": step.step_type,
                    "params": dict(step.params),
                    "status": step.status.value,
                    "sequence_index": step.sequence_index,
                }
                for step in runbook.steps
            ],
        }

        # Compare key fields (order-independent for params)
        assert reserialized_data["runbook_id"] == original_data["runbook_id"]
        assert reserialized_data["target"] == original_data["target"]
        assert reserialized_data["current_step_index"] == original_data["current_step_index"]
        assert reserialized_data["status"] == original_data["status"]
        assert len(reserialized_data["steps"]) == len(original_data["steps"])

        # Compare steps
        for res_step, orig_step in zip(reserialized_data["steps"], original_data["steps"]):
            assert res_step["step_type"] == orig_step["step_type"]
            assert res_step["status"] == orig_step["status"]
            assert res_step["sequence_index"] == orig_step["sequence_index"]
            assert res_step["params"] == orig_step["params"]


# ---------------------------------------------------------------------------
# AC-005: Self-contained and reusable as a template
# ---------------------------------------------------------------------------


class TestRunbookSelfContained:
    """AC-005: A test asserts the saved record is self-contained (no external
    $refs, no inline shell) and reusable as a template — i.e. the only edit
    needed for a different target is the cwd/env_file.
    """

    def test_no_external_refs(self, runbook_path: Path) -> None:
        """The runbook contains no external JSON Schema $ref references."""
        content = runbook_path.read_text(encoding="utf-8")
        assert "$ref" not in content, "Runbook must not contain $ref references"

    def test_no_inline_shell_in_raw_json(self, runbook_path: Path) -> None:
        """The raw JSON does not contain inline shell commands."""
        data = json.loads(runbook_path.read_text(encoding="utf-8"))
        for step in data["steps"]:
            params = step.get("params", {})
            assert "command" not in params, \
                "Step params must not contain 'command' (inline shell)"

    def test_template_reusability(self, runbook_path: Path) -> None:
        """The runbook is reusable as a template with minimal edits."""
        runbook = _parse_runbook_file(runbook_path)

        # All environment-specific config should be in cwd/env_file
        for step in runbook.steps:
            assert "cwd" in step.params, "Step must have 'cwd' param"
            assert "env_file" in step.params, "Step must have 'env_file' param"
            assert "script" in step.params, "Step must have 'script' param"

            # Script names should be generic (not environment-specific paths)
            script = step.params["script"]
            assert "/" not in script, "Script should be filename only, not a path"


# ---------------------------------------------------------------------------
# AC-006: Step params match integration contract
# ---------------------------------------------------------------------------


class TestStepParamsContract:
    """AC-006: The step params keys exactly match what deploy_compose/
    run_smoke_tests read (cwd, script, env_file) — see §4 Integration Contract.
    """

    def test_deploy_compose_params_match_contract(self, runbook_path: Path) -> None:
        """The deploy_compose step params match the integration contract."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[0]  # deploy_compose is first

        # Exact keys required by the contract
        required_keys = {"cwd", "script", "env_file"}
        actual_keys = set(step.params.keys())

        assert actual_keys == required_keys, \
            f"deploy_compose params mismatch. Expected: {required_keys}, Got: {actual_keys}"

    def test_run_smoke_tests_params_match_contract(self, runbook_path: Path) -> None:
        """The run_smoke_tests step params match the integration contract."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[1]  # run_smoke_tests is second

        # Exact keys required by the contract
        required_keys = {"cwd", "script", "env_file"}
        actual_keys = set(step.params.keys())

        assert actual_keys == required_keys, \
            f"run_smoke_tests params mismatch. Expected: {required_keys}, Got: {actual_keys}"

    def test_deploy_compose_param_values(self, runbook_path: Path) -> None:
        """The deploy_compose step param values are correct."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[0]

        assert step.params["cwd"] == "fleet-memory/deploy/nas"
        assert step.params["script"] == "deploy.sh"
        assert step.params["env_file"] == ".env.deploy"

    def test_run_smoke_tests_param_values(self, runbook_path: Path) -> None:
        """The run_smoke_tests step param values are correct."""
        runbook = _parse_runbook_file(runbook_path)
        step = runbook.steps[1]

        assert step.params["cwd"] == "fleet-memory/deploy/nas"
        assert step.params["script"] == "smoke.sh"
        assert step.params["env_file"] == ".env.deploy"
