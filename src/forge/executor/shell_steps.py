"""Subprocess-runner core for shell-step handlers (TASK-SSH-002).

This module provides the shared subprocess execution machinery for shell-step
handlers (deploy_compose, run_smoke_tests). It implements timeout + size-cap
hardening and applies credential scrubbing at the capture boundary.

The core function :func:`_run_script_step` is private; shell-step handlers
(TASK-SSH-003/004) provide the public interface with verdict semantics layered
on top of this mechanics-only foundation.
"""

from __future__ import annotations

import os
import subprocess

from forge.memory.redaction import scrub_process_output

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default timeout in seconds for script execution (10 minutes).
DEFAULT_TIMEOUT_SECONDS = 600

#: Default output capture limit in bytes (1 MiB).
DEFAULT_OUTPUT_CAP_BYTES = 1_048_576

#: Exit code returned when a script times out (GNU timeout convention).
_TIMEOUT_EXIT_CODE = 124

#: Truncation marker appended when output exceeds the cap.
_TRUNCATION_MARKER = "\n... [OUTPUT TRUNCATED] ...\n"


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def _run_script_step(
    *,
    cwd: str,
    script: str,
    env_file: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    output_cap: int = DEFAULT_OUTPUT_CAP_BYTES,
) -> tuple[int, str]:
    """Run a shell script with timeout, size-cap, and credential scrubbing.

    This is the shared core for all shell-step handlers. It runs the named
    script as a subprocess, captures combined stdout+stderr, enforces timeout
    and output-size limits, and scrubs credentials at the capture boundary.

    The env_file path (if provided) is exposed to the script via the ENV_FILE
    environment variable. The function **never** reads the env file contents —
    the script is responsible for sourcing or reading it if needed.

    Args:
        cwd: Working directory for the subprocess. Must exist.
        script: Path to the script to execute. Must be executable.
        env_file: Optional path to an environment file. Passed via ENV_FILE
            environment variable. No existence check is performed (ASSUM-013).
        timeout: Maximum execution time in seconds. Defaults to 600 (10 min).
        output_cap: Maximum output size in bytes. Defaults to 1 MiB.

    Returns:
        A tuple of (exit_code, output):
        - exit_code: 0 on success, 124 on timeout, or the script's exit code.
        - output: Combined stdout+stderr, truncated if needed, with credentials
          scrubbed. Empty string if no output.

    Raises:
        Never raises. All errors (missing script, permission denied, etc.) are
        returned as non-zero exit codes with descriptive output.
    """
    # Build environment: inherit parent env, add ENV_FILE if provided
    env = os.environ.copy()
    if env_file is not None:
        env["ENV_FILE"] = env_file

    try:
        # Run the subprocess with combined output capture
        result = subprocess.run(
            [script],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=False,  # Capture as bytes for accurate size limiting
            timeout=timeout,
        )
        exit_code = result.returncode
        raw_output = result.stdout + result.stderr

    except subprocess.TimeoutExpired as e:
        # Script exceeded timeout — kill it and capture partial output
        exit_code = _TIMEOUT_EXIT_CODE
        # TimeoutExpired.stdout/stderr may be None if process was killed early
        stdout_bytes = e.stdout if e.stdout else b""
        stderr_bytes = e.stderr if e.stderr else b""
        raw_output = stdout_bytes + stderr_bytes

    except FileNotFoundError:
        # Script doesn't exist or is not executable
        return (127, "")  # 127 = command not found (shell convention)

    except PermissionError:
        # Script exists but is not executable
        return (126, "")  # 126 = command not executable (shell convention)

    except Exception as exc:
        # Any other error — return generic failure with error message
        error_msg = f"Unexpected error running script: {type(exc).__name__}: {exc}"
        return (1, error_msg)

    # Truncate to output_cap bytes BEFORE decoding and scrubbing
    if len(raw_output) > output_cap:
        raw_output = raw_output[:output_cap]
        truncated = True
    else:
        truncated = False

    # Decode bytes to string (replace invalid UTF-8 sequences)
    try:
        output = raw_output.decode("utf-8", errors="replace")
    except Exception:
        # Fallback: if decode somehow fails, return empty output
        output = ""

    # Append truncation marker if we truncated
    if truncated:
        output += _TRUNCATION_MARKER

    # Scrub credentials at the capture boundary (exactly once)
    output = scrub_process_output(output)

    return (exit_code, output)


__all__ = ["_run_script_step", "DEFAULT_TIMEOUT_SECONDS", "DEFAULT_OUTPUT_CAP_BYTES"]
