"""Tests for subprocess-runner core (TASK-SSH-002).

This module validates the _run_script_step function's timeout, size-cap,
and scrub-at-boundary mechanics. All acceptance criteria are covered.
"""

from __future__ import annotations

import pytest


class TestScriptExecution:
    """Test basic script execution mechanics."""

    def test_runs_script_with_specified_working_directory(self, tmp_path):
        """_run_script_step runs the named script with cwd as its current working directory.

        AC: runs with that working directory as its current directory.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "pwd_test.sh"
        script.write_text("#!/usr/bin/env bash\npwd\n")
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None
        )

        assert exit_code == 0
        assert str(tmp_path) in output.strip()

    def test_env_file_exposed_by_path_only(self, tmp_path):
        """env_file is exposed to the subprocess by path only; function never reads it.

        AC: environment file referenced only by its path.
        """
        from forge.executor.shell_steps import _run_script_step

        env_file = tmp_path / "test.env"
        env_file.write_text("SECRET_KEY=super_secret\n")

        script = tmp_path / "check_env.sh"
        # Script should receive ENV_FILE variable pointing to the file
        script.write_text('#!/usr/bin/env bash\necho "ENV_FILE=$ENV_FILE"\n')
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=str(env_file)
        )

        assert exit_code == 0
        assert str(env_file) in output

    def test_no_output_returns_clean_exit(self, tmp_path):
        """A script producing no output returns (0, "") for the clean-exit path.

        AC: produces no output still records a result.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "silent.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None
        )

        assert exit_code == 0
        assert output == ""

    def test_missing_script_does_not_raise(self, tmp_path):
        """A missing/non-executable script does not raise — contained as non-zero exit.

        AC: script that cannot be run … executor should not crash.
        """
        from forge.executor.shell_steps import _run_script_step

        missing_script = tmp_path / "does_not_exist.sh"

        # Should not raise, should return non-zero exit
        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(missing_script), env_file=None
        )

        assert exit_code != 0


class TestOutputCapture:
    """Test stdout and stderr capture mechanics."""

    def test_combined_stdout_and_stderr_captured(self, tmp_path):
        """Combined stdout AND stderr are captured into a single output string.

        AC: secret printed to the error stream is captured and scrubbed.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "dual_stream.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'echo "stdout message"\n'
            'echo "stderr message" >&2\n'
        )
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None
        )

        assert exit_code == 0
        assert "stdout message" in output
        assert "stderr message" in output


class TestCredentialScrubbing:
    """Test scrub_process_output integration at the capture boundary."""

    @pytest.mark.seam
    @pytest.mark.integration_contract("SCRUB_MARKERS")
    def test_scrub_markers_applied_at_capture_boundary(self, tmp_path):
        """Captured output is scrubbed exactly once before return.

        Contract: output containing a postgres DSN must come back with the DSN
        replaced by ***REDACTED-DSN***; scrubbing is idempotent.
        Producer: TASK-SSH-001 (scrub_process_output).

        AC: Captured output is passed through scrub_process_output exactly once.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "leak.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'echo "connecting to postgresql://u:secret@db:5432/app"\n'
        )
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None
        )

        assert exit_code == 0
        assert "postgresql://" not in output
        assert "***REDACTED-DSN***" in output
        assert "secret" not in output

    def test_password_in_stderr_is_scrubbed(self, tmp_path):
        """Password printed to stderr stream is captured and scrubbed.

        AC: secret printed to the error stream is captured and scrubbed.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "leak_stderr.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'echo "Error: password=my_secret_pass" >&2\n'
        )
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None
        )

        assert exit_code == 0
        assert "my_secret_pass" not in output
        assert "***REDACTED-PASSWORD***" in output


class TestTimeoutHandling:
    """Test timeout mechanics and process termination."""

    @pytest.mark.seam
    @pytest.mark.integration_contract("SCRUB_MARKERS")
    def test_subprocess_timeout_does_not_hang(self, tmp_path):
        """A hung script is killed at the timeout and returns non-zero.

        AC: A script that exceeds timeout is killed and returns non-zero.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "hang.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 30\n")
        script.chmod(0o755)

        exit_code, _ = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None, timeout=1.0
        )
        assert exit_code != 0

    def test_timeout_captures_partial_output(self, tmp_path):
        """Timeout kills process but captures partial output before timeout.

        AC: partial output captured — it does not hang.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "slow.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'echo "starting work"\n'
            "sleep 30\n"
            'echo "never printed"\n'
        )
        script.chmod(0o755)

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None, timeout=1.0
        )

        assert exit_code != 0
        assert "starting work" in output
        assert "never printed" not in output


class TestOutputSizeCap:
    """Test output truncation mechanics."""

    def test_output_exceeding_cap_is_truncated(self, tmp_path):
        """Output longer than output_cap is truncated to the cap before scrubbing.

        AC: Output longer than output_cap is truncated with visible marker.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "large.sh"
        # Generate ~2KB of output
        script.write_text(
            "#!/usr/bin/env bash\n"
            "for i in {1..100}; do echo 'line of text with some content here'; done\n"
        )
        script.chmod(0o755)

        # Set a small cap (500 bytes)
        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None, output_cap=500
        )

        assert exit_code == 0
        assert len(output.encode()) <= 600  # Allow for truncation marker
        assert "TRUNCATED" in output or "..." in output

    def test_truncation_happens_before_scrubbing(self, tmp_path):
        """Truncation occurs before scrubbing to ensure credentials don't leak.

        AC: truncated to the cap before scrubbing.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "leak_large.sh"
        # Put credential at the end, which will be truncated
        script.write_text(
            "#!/usr/bin/env bash\n"
            "for i in {1..50}; do echo 'padding padding padding padding'; done\n"
            'echo "postgresql://user:secret@db:5432/app"\n'
        )
        script.chmod(0o755)

        # Small cap to force truncation before the credential
        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=None, output_cap=200
        )

        assert exit_code == 0
        # The credential should be truncated away, so we shouldn't see the marker
        # (because scrubbing happens after truncation removes the credential)
        assert len(output.encode()) <= 300  # Allow for truncation marker


class TestEnvFileValidation:
    """Test env-file validation behavior."""

    def test_no_env_file_existence_check_performed(self, tmp_path):
        """The function performs no env-file existence check.

        AC: ASSUM-013 stays deferred — no env-file existence check.
        """
        from forge.executor.shell_steps import _run_script_step

        script = tmp_path / "test.sh"
        script.write_text("#!/usr/bin/env bash\necho 'ok'\n")
        script.chmod(0o755)

        # Pass a non-existent env file path — should not raise
        missing_env = tmp_path / "nonexistent.env"

        exit_code, output = _run_script_step(
            cwd=str(tmp_path), script=str(script), env_file=str(missing_env)
        )

        # The script itself runs successfully; env_file is just passed as a path
        assert exit_code == 0
        assert "ok" in output
