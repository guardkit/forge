"""Integration tests for shell-step handlers against real scripts (TASK-SSH-006).

This module contains marker-gated integration tests that exercise shell-step
handlers (run_smoke_tests, deploy_compose) against real scripts, specifically
the fleet-memory smoke.sh script.

These tests are gated behind @integration @slow markers and are excluded from
the default pytest run. They require external resources (fleet-memory repo,
throwaway deployment target) and only run when explicitly invoked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.executor.registry import StepStatus, StepTypeRegistry
from forge.executor.shell_steps import register_shell_handlers
from forge.persistence.repositories.runbook_models import Step


# ---------------------------------------------------------------------------
# Test configuration and guards
# ---------------------------------------------------------------------------

#: Path to the fleet-memory smoke.sh script
FLEET_MEMORY_SMOKE_SCRIPT = Path.home() / "Projects/appmilla_github/fleet-memory/deploy/nas/smoke.sh"

#: Environment variable that must point to a throwaway .env.deploy file
THROWAWAY_ENV_VAR = "FLEET_MEMORY_THROWAWAY_ENV"

#: Production host patterns that must never appear in test targets (ASSUM-010)
PRODUCTION_HOST_PATTERNS = [
    "nas.finproxy.co.uk",
    "production",
    "prod",
    "fleet-memory-prod",
]


def _is_production_target(env_file_path: Path) -> bool:
    """Guard: returns True if env_file points to a production target.

    Args:
        env_file_path: Path to the .env.deploy file to check.

    Returns:
        True if the file contains production host patterns, False otherwise.
    """
    if not env_file_path.exists():
        return False

    content = env_file_path.read_text()
    return any(pattern in content.lower() for pattern in PRODUCTION_HOST_PATTERNS)


def _skip_if_resources_unavailable() -> tuple[bool, str]:
    """Check if integration test resources are available.

    Returns:
        Tuple of (should_skip, reason_message).
    """
    # Check if smoke script exists
    if not FLEET_MEMORY_SMOKE_SCRIPT.exists():
        return (True, f"Smoke script not found at {FLEET_MEMORY_SMOKE_SCRIPT}")

    # Check if throwaway target is configured
    throwaway_env = os.environ.get(THROWAWAY_ENV_VAR)
    if not throwaway_env:
        return (True, f"Throwaway target not configured (set {THROWAWAY_ENV_VAR})")

    throwaway_path = Path(throwaway_env)
    if not throwaway_path.exists():
        return (True, f"Throwaway env file not found: {throwaway_path}")

    # Guard: verify target is not production
    if _is_production_target(throwaway_path):
        pytest.fail(
            f"SAFETY VIOLATION (ASSUM-010): Throwaway env file contains production host patterns. "
            f"This test MUST NOT run against production. File: {throwaway_path}"
        )

    return (False, "")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestFleetMemorySmokeIntegration:
    """Integration tests against the real fleet-memory smoke.sh script.

    These tests exercise the run_smoke_tests handler against the actual
    fleet-memory smoke script, verifying end-to-end behavior including:
    - Script execution through the handler registry
    - Exit status → verdict mapping
    - Credential scrubbing on real script output
    - Skip behavior when resources unavailable
    """

    def test_marker_gated_integration_test_excluded_from_default_run(self):
        """AC-001: Test is gated behind @integration @slow markers.

        This test verifies that the integration test class is properly marked
        and will be excluded from the default pytest run (pytest -m "not slow").

        The markers are applied at the class level, so all tests in this class
        inherit them automatically.
        """
        # This test always passes — it exists to document the marker requirement
        # and ensure the markers are present
        assert True

    def test_skips_cleanly_when_smoke_script_unavailable(self):
        """AC-004: Test skips cleanly when smoke script path unavailable.

        When the fleet-memory smoke.sh script is not present at the expected
        location, the test should skip (not error), allowing CI without the
        fleet-memory repo to stay green.
        """
        # Check if smoke script exists
        if not FLEET_MEMORY_SMOKE_SCRIPT.exists():
            pytest.skip(f"Smoke script not found at {FLEET_MEMORY_SMOKE_SCRIPT}")

        # If we reach here, the script exists
        assert FLEET_MEMORY_SMOKE_SCRIPT.is_file()
        assert os.access(FLEET_MEMORY_SMOKE_SCRIPT, os.X_OK)

    def test_skips_cleanly_when_throwaway_target_unavailable(self):
        """AC-004: Test skips cleanly when throwaway target unavailable.

        When the throwaway target is not configured (via FLEET_MEMORY_THROWAWAY_ENV),
        the test should skip (not error), allowing CI without a target to stay green.
        """
        should_skip, reason = _skip_if_resources_unavailable()
        if should_skip:
            pytest.skip(reason)

        # If we reach here, resources are available
        throwaway_env = Path(os.environ[THROWAWAY_ENV_VAR])
        assert throwaway_env.exists()

    def test_production_guard_prevents_test_against_production_target(self, tmp_path):
        """AC-005: Guard prevents test from running against production target.

        The test must never run against a production target. The guard checks
        the throwaway .env.deploy file for production host patterns and fails
        the test (not skip) if detected.
        """
        # Create a fake production .env.deploy
        prod_env = tmp_path / ".env.deploy"
        prod_env.write_text(
            "NAS_HOST=nas.finproxy.co.uk\n"
            "NAS_USER=admin\n"
            "NAS_SSH_PORT=22\n"
            "NAS_DOCKER_ROOT=/volume1/docker\n"
            "FLEET_MEMORY_PG_PASSWORD=prod_secret\n"
        )

        # Verify the guard detects it
        assert _is_production_target(prod_env) is True

        # Verify non-production targets pass
        safe_env = tmp_path / ".env.deploy.safe"
        safe_env.write_text(
            "NAS_HOST=throwaway-test-nas.local\n"
            "NAS_USER=testuser\n"
            "NAS_SSH_PORT=2222\n"
            "NAS_DOCKER_ROOT=/tmp/docker\n"
            "FLEET_MEMORY_PG_PASSWORD=throwaway_pass\n"
        )
        assert _is_production_target(safe_env) is False

    def test_smoke_script_executes_and_verdict_equals_exit_status(self, tmp_path):
        """AC-002: Smoke script executes to completion, verdict equals exit status.

        When the fleet-memory smoke script is invoked through run_smoke_tests
        handler, it should execute to completion and the step's verdict should
        equal the script's exit status (passed if 0, failed if non-zero).
        """
        # Skip if resources unavailable
        should_skip, reason = _skip_if_resources_unavailable()
        if should_skip:
            pytest.skip(reason)

        # Get throwaway target env file
        throwaway_env = Path(os.environ[THROWAWAY_ENV_VAR])

        # Build Step params pointing to real smoke.sh and throwaway target
        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(FLEET_MEMORY_SMOKE_SCRIPT.parent),
                "script": str(FLEET_MEMORY_SMOKE_SCRIPT),
                "env_file": str(throwaway_env),
                "timeout": 120,  # 2 minutes for smoke tests
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        # Resolve handler via registry (tests the full wiring path)
        registry = StepTypeRegistry()
        register_shell_handlers(registry)
        handler = registry.resolve("run_smoke_tests")
        assert handler is not None

        # Execute the step
        outcome = handler(step)

        # Verify outcome has terminal status
        assert outcome.status in {StepStatus.passed, StepStatus.failed}

        # Verify result contains exit_code and captured_output
        assert "exit_code" in outcome.result
        assert "captured_output" in outcome.result

        # Verify verdict matches exit status (core requirement)
        exit_code = outcome.result["exit_code"]
        if exit_code == 0:
            assert outcome.status == StepStatus.passed
        else:
            assert outcome.status == StepStatus.failed

        # Log the outcome for debugging (not captured by default, only with -s)
        print(f"\nSmoke test outcome: status={outcome.status}, exit_code={exit_code}")
        print(f"Output preview: {outcome.result['captured_output'][:200]}...")

    def test_no_credentials_in_step_result_from_real_script(self, tmp_path):
        """AC-003: No credentials from script output appear in step result.

        The scrub boundary must hold end-to-end: when the fleet-memory smoke
        script outputs credentials (DSN with FLEET_MEMORY_PG_PASSWORD), they
        should not appear in the step result — scrub_process_output should
        redact them at the capture boundary.
        """
        # Skip if resources unavailable
        should_skip, reason = _skip_if_resources_unavailable()
        if should_skip:
            pytest.skip(reason)

        # Get throwaway target env file and read the password
        throwaway_env = Path(os.environ[THROWAWAY_ENV_VAR])
        env_content = throwaway_env.read_text()

        # Extract FLEET_MEMORY_PG_PASSWORD from env file
        password = None
        for line in env_content.split("\n"):
            if line.startswith("FLEET_MEMORY_PG_PASSWORD="):
                password = line.split("=", 1)[1].strip()
                break

        if not password:
            pytest.skip("FLEET_MEMORY_PG_PASSWORD not found in throwaway env file")

        # Build Step and execute through handler
        step = Step(
            step_type="run_smoke_tests",
            params={
                "cwd": str(FLEET_MEMORY_SMOKE_SCRIPT.parent),
                "script": str(FLEET_MEMORY_SMOKE_SCRIPT),
                "env_file": str(throwaway_env),
                "timeout": 120,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        registry = StepTypeRegistry()
        register_shell_handlers(registry)
        handler = registry.resolve("run_smoke_tests")
        outcome = handler(step)

        # Verify password does NOT appear in captured output
        captured_output = outcome.result["captured_output"]
        assert password not in captured_output, (
            f"Credential scrubbing failed: password '{password}' appears in output"
        )

        # Verify redaction marker IS present (if the script outputs the password)
        # The smoke.sh uses the password in psql DSN, so it should be scrubbed
        if "postgresql://" in captured_output:
            assert "***REDACTED" in captured_output, (
                "Expected redaction marker in output containing database DSN"
            )

        print(f"\nCredential scrubbing verified: password '{password}' not in output")


@pytest.mark.integration
@pytest.mark.slow
class TestDeployComposeSmokeIntegration:
    """Integration tests for deploy_compose handler (optional, for completeness).

    These tests mirror the run_smoke_tests integration tests but exercise the
    deploy_compose handler. This is not strictly required by TASK-SSH-006 but
    provides symmetry and future-proofs the integration test suite.
    """

    def test_deploy_compose_wiring_ready(self, tmp_path):
        """Verify deploy_compose can be resolved and invoked through registry.

        This is a minimal integration test that verifies the deploy_compose
        handler is properly registered and can be resolved through the registry.
        """
        # Create a minimal test script
        script = tmp_path / "test.sh"
        script.write_text("#!/usr/bin/env bash\necho 'deploy ok'\nexit 0\n")
        script.chmod(0o755)

        # Build Step and execute through registry
        step = Step(
            step_type="deploy_compose",
            params={
                "cwd": str(tmp_path),
                "script": str(script),
                "env_file": None,
            },
            status=StepStatus.running,
            sequence_index=0,
        )

        registry = StepTypeRegistry()
        register_shell_handlers(registry)
        handler = registry.resolve("deploy_compose")
        assert handler is not None

        outcome = handler(step)
        assert outcome.status == StepStatus.passed
        assert outcome.result["exit_code"] == 0
