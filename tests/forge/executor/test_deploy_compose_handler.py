"""Tests for deploy_compose handler (TASK-SSH-003).

Validates the deploy_compose handler's conformance to the StepHandler protocol,
exit-status verdict mapping, result structure, and credential scrubbing.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.executor.shell_steps import deploy_compose
from forge.persistence.repositories.runbook_models import Step, StepStatus


class TestDeployComposeHandler:
    """Test suite for deploy_compose handler (AC-001 through AC-006)."""

    def test_handler_satisfies_protocol(self) -> None:
        """AC-001: deploy_compose satisfies StepHandler Protocol."""
        # Verify callable signature: (step: Step) -> StepOutcome
        # This is validated structurally by the type system, but we verify it's
        # actually callable with a Step instance.
        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": "/tmp",
                "script": "/bin/true",
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        # Should not raise TypeError
        outcome = deploy_compose(step)
        assert outcome is not None
        assert hasattr(outcome, "status")
        assert hasattr(outcome, "result")

    def test_successful_deploy_yields_passed_status(self, tmp_path: Path) -> None:
        """AC-002: Deploy script exiting 0 yields StepOutcome(status=passed)."""
        # Create a simple script that exits 0
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\necho 'Deploying...'\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.passed
        assert outcome.result is not None
        assert outcome.result["exit_code"] == 0
        assert "Deploying..." in outcome.result["captured_output"]

    def test_failed_deploy_yields_failed_status(self, tmp_path: Path) -> None:
        """AC-003: Deploy script exiting non-zero yields StepOutcome(status=failed)."""
        # Create a script that exits with error
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\necho 'Deploy failed!'\nexit 1\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.failed
        assert outcome.result is not None
        assert outcome.result["exit_code"] == 1
        assert "Deploy failed!" in outcome.result["captured_output"]

    def test_result_is_json_serializable(self, tmp_path: Path) -> None:
        """AC-004: Result dict is JSON-serializable with exit_code and captured_output."""
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\necho 'Output'\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        # Should be JSON-serializable without error
        result_json = json.dumps(outcome.result)
        assert result_json is not None

        # Should contain required fields
        assert "exit_code" in outcome.result
        assert "captured_output" in outcome.result
        assert isinstance(outcome.result["exit_code"], int)
        assert isinstance(outcome.result["captured_output"], str)

    def test_dsn_scrubbed_even_on_failure(self, tmp_path: Path) -> None:
        """AC-005: Postgres DSN is scrubbed even when script exits non-zero."""
        # Create a script that outputs a DSN and then fails
        dsn = "postgresql://user:secret@localhost:5432/db"
        script = tmp_path / "deploy.sh"
        script.write_text(f"#!/bin/bash\necho 'Connecting to {dsn}'\nexit 1\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.failed
        # DSN should be scrubbed in the output
        assert dsn not in outcome.result["captured_output"]
        assert "secret" not in outcome.result["captured_output"]

    def test_rerun_reinvokes_script(self, tmp_path: Path) -> None:
        """AC-006: Re-running the step re-invokes the script with no idempotency guard."""
        # Create a script that writes to a counter file each time it runs
        counter_file = tmp_path / "counter.txt"
        script = tmp_path / "deploy.sh"
        script.write_text(
            f"""#!/bin/bash
echo "Run $(cat {counter_file} 2>/dev/null || echo 0)"
echo "$(($(cat {counter_file} 2>/dev/null || echo 0) + 1))" > {counter_file}
exit 0
"""
        )
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        # First run
        outcome1 = deploy_compose(step)
        assert outcome1.status == StepStatus.passed
        assert "Run 0" in outcome1.result["captured_output"]

        # Second run (same step instance)
        outcome2 = deploy_compose(step)
        assert outcome2.status == StepStatus.passed
        assert "Run 1" in outcome2.result["captured_output"]

        # Third run to be extra sure
        outcome3 = deploy_compose(step)
        assert outcome3.status == StepStatus.passed
        assert "Run 2" in outcome3.result["captured_output"]

    def test_script_runs_in_specified_cwd(self, tmp_path: Path) -> None:
        """Verify script runs in the directory specified by cwd param."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\npwd\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(workdir),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.passed
        assert str(workdir) in outcome.result["captured_output"]

    def test_bare_script_with_relative_cwd_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """TASK-FMDR-007: deploy_compose runs a bare script name with a relative cwd.

        Reproduces the real-NAS step-0 failure: the shipped exemplar runbook uses
        script="deploy.sh" (filename only) + a relative cwd ("fleet-memory/deploy/nas").
        Before the fix this returned exit 127 (command not found); it must now resolve
        the script relative to cwd and run it.
        """
        deploy_dir = tmp_path / "fleet-memory" / "deploy" / "nas"
        deploy_dir.mkdir(parents=True)
        script = deploy_dir / "deploy.sh"
        script.write_text("#!/bin/bash\necho 'Deploying...'\nexit 0\n")
        script.chmod(0o755)
        monkeypatch.chdir(tmp_path)  # make the relative cwd resolvable

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": "fleet-memory/deploy/nas",  # relative cwd
                "script": "deploy.sh",  # bare filename (no path separator)
                "env_file": ".env.deploy",
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.passed
        assert outcome.result["exit_code"] == 0
        assert outcome.result["exit_code"] != 127
        assert "Deploying..." in outcome.result["captured_output"]

    def test_env_file_passed_to_script(self, tmp_path: Path) -> None:
        """Verify env_file param is passed via ENV_FILE environment variable."""
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=value\n")

        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\necho \"ENV_FILE=$ENV_FILE\"\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": str(env_file),
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.passed
        assert str(env_file) in outcome.result["captured_output"]

    def test_timeout_override_honored(self, tmp_path: Path) -> None:
        """Verify custom timeout param is passed through to _run_script_step."""
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\nsleep 2\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
                "timeout": 1,  # 1 second timeout - should timeout
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        # Should fail with timeout exit code (124)
        assert outcome.status == StepStatus.failed
        assert outcome.result["exit_code"] == 124

    def test_output_cap_override_honored(self, tmp_path: Path) -> None:
        """Verify custom output_cap param is passed through to _run_script_step."""
        # Generate output larger than cap
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/bash\nfor i in {1..1000}; do echo 'Line $i with lots of padding to make it bigger'; done\nexit 0\n")
        script.chmod(0o755)

        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
                "output_cap": 100,  # Very small cap
            },
            status=StepStatus.pending,
            sequence_index=0,
        )

        outcome = deploy_compose(step)

        assert outcome.status == StepStatus.passed
        # Output should be truncated
        assert "TRUNCATED" in outcome.result["captured_output"]
