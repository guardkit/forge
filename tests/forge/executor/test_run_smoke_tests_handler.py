"""Tests for run_smoke_tests handler (TASK-SSH-004).

This module validates the run_smoke_tests step handler, which executes smoke
test scripts and maps their exit status to verdict outcomes.
"""

from __future__ import annotations

import pytest

from forge.executor.registry import StepStatus
from forge.persistence.repositories.runbook_models import Step


class TestHandlerProtocol:
    """Test that run_smoke_tests satisfies the StepHandler Protocol."""

    def test_run_smoke_tests_satisfies_step_handler_protocol(self, tmp_path):
        """run_smoke_tests satisfies the StepHandler Protocol structurally.

        AC-001: satisfies the StepHandler Protocol structurally.
        """
        from forge.executor.shell_steps import run_smoke_tests
        from forge.executor.registry import StepHandler

        # Verify it's callable with the right signature
        script = tmp_path / "test.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = run_smoke_tests(step)

        # Should return StepOutcome with terminal status
        assert hasattr(outcome, "status")
        assert hasattr(outcome, "result")
        assert outcome.status in {
            StepStatus.passed,
            StepStatus.failed,
            StepStatus.awaiting_approval,
        }


class TestExitStatusVerdictMapping:
    """Test exit status → verdict mapping."""

    def test_exit_zero_yields_passed_verdict(self, tmp_path):
        """A smoke script exiting 0 yields StepOutcome(status=passed, …).

        AC-002: exit 0 yields status=passed.
        """
        from forge.executor.shell_steps import run_smoke_tests

        script = tmp_path / "success.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = run_smoke_tests(step)

        assert outcome.status == StepStatus.passed
        assert outcome.result is not None
        assert outcome.result["exit_code"] == 0

    def test_exit_nonzero_yields_failed_verdict(self, tmp_path):
        """A smoke script exiting non-zero yields StepOutcome(status=failed, …).

        AC-003: exit non-zero yields status=failed.
        """
        from forge.executor.shell_steps import run_smoke_tests

        script = tmp_path / "fail.sh"
        script.write_text("#!/usr/bin/env bash\nexit 1\n")
        script.chmod(0o755)

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = run_smoke_tests(step)

        assert outcome.status == StepStatus.failed
        assert outcome.result is not None
        assert outcome.result["exit_code"] == 1

    def test_verdict_follows_exit_status_across_boundary_cases(self, tmp_path):
        """Verdict follows exit status: 0 → passed, 1 → failed, 137 → failed.

        AC-004: Verdict follows exit status (0, 1, 137).
        """
        from forge.executor.shell_steps import run_smoke_tests

        # Test exit code 0 → passed
        script_0 = tmp_path / "exit0.sh"
        script_0.write_text("#!/usr/bin/env bash\nexit 0\n")
        script_0.chmod(0o755)

        step_0 = Step(
            step_type="run_smoke_tests",
            params={"cwd": str(tmp_path), "script": str(script_0), "env_file": None},
            status=StepStatus.running,
            sequence_index=0,
        )
        outcome_0 = run_smoke_tests(step_0)
        assert outcome_0.status == StepStatus.passed
        assert outcome_0.result["exit_code"] == 0

        # Test exit code 1 → failed
        script_1 = tmp_path / "exit1.sh"
        script_1.write_text("#!/usr/bin/env bash\nexit 1\n")
        script_1.chmod(0o755)

        step_1 = Step(
            step_type="run_smoke_tests",
            params={"cwd": str(tmp_path), "script": str(script_1), "env_file": None},
            status=StepStatus.running,
            sequence_index=0,
        )
        outcome_1 = run_smoke_tests(step_1)
        assert outcome_1.status == StepStatus.failed
        assert outcome_1.result["exit_code"] == 1

        # Test exit code 137 (SIGKILL) → failed
        script_137 = tmp_path / "exit137.sh"
        script_137.write_text("#!/usr/bin/env bash\nexit 137\n")
        script_137.chmod(0o755)

        step_137 = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script_137),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )
        outcome_137 = run_smoke_tests(step_137)
        assert outcome_137.status == StepStatus.failed
        assert outcome_137.result["exit_code"] == 137


class TestPasswordScrubbing:
    """Test password scrubbing in output."""

    def test_password_in_output_is_scrubbed(self, tmp_path):
        """A password in script output does not appear in result dict.

        AC-005: Password is scrubbed before publishing.
        """
        from forge.executor.shell_steps import run_smoke_tests

        script = tmp_path / "leak.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'echo "Running tests..."\n'
            'echo "password=super_secret_123"\n'
            "exit 0\n"
        )
        script.chmod(0o755)

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = run_smoke_tests(step)

        assert outcome.status == StepStatus.passed
        assert "super_secret_123" not in outcome.result["captured_output"]
        assert "***REDACTED-PASSWORD***" in outcome.result["captured_output"]


class TestEnvFileHandling:
    """Test env_file handling behavior."""

    def test_missing_env_file_does_not_prevent_running(self, tmp_path):
        """Missing env_file does not prevent script execution.

        AC-006: Missing env_file does not prevent running (ASSUM-013).
        """
        from forge.executor.shell_steps import run_smoke_tests

        script = tmp_path / "test.sh"
        script.write_text("#!/usr/bin/env bash\necho 'ok'\nexit 0\n")
        script.chmod(0o755)

        missing_env = tmp_path / "nonexistent.env"

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": str(missing_env),
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = run_smoke_tests(step)

        # Should succeed — the env_file is just passed as a path
        assert outcome.status == StepStatus.passed
        assert outcome.result["exit_code"] == 0


class TestWiringReadiness:
    """Test that run_smoke_tests is ready for registry wiring."""

    def test_handler_can_be_resolved_through_registry(self, tmp_path):
        """run_smoke_tests can be registered and resolved through StepTypeRegistry.

        This test demonstrates wiring readiness — the handler can be registered
        in a registry and invoked through the registry.resolve() path. Actual
        registration is TASK-SSH-005.
        """
        from forge.executor.shell_steps import run_smoke_tests
        from forge.executor.registry import StepTypeRegistry

        # Create a registry and register the handler
        registry = StepTypeRegistry()
        registry.register("run_smoke_tests", run_smoke_tests)

        # Verify it can be resolved
        resolved_handler = registry.resolve("run_smoke_tests")
        assert resolved_handler is not None
        assert resolved_handler is run_smoke_tests

        # Verify the resolved handler works
        script = tmp_path / "test.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        outcome = resolved_handler(step)
        assert outcome.status == StepStatus.passed
