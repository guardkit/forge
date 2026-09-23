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
from typing import Callable, Sequence

from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.launch_environment import build_launch_env
from forge.memory.redaction import scrub_process_output
from forge.persistence.repositories.runbook_models import Step, StepStatus

#: A callable with the :func:`_run_script_step` keyword signature. The deploy
#: stage injects a sidecar-backed implementation here (S1) to route
#: docker-touching scripts over loopback HTTP without a second executor; the
#: default is the in-process subprocess core.
ScriptRunner = Callable[..., tuple[int, str]]

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
    extra_env: dict[str, str] | None = None,
    memory_project: str | None = None,
    launch_settings: Sequence[str] | None = None,
    declared_at: str | None = None,  # noqa: ARG001 — the far side's door reads it
    deploy: dict[str, object] | None = None,
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
        script: Script to execute, resolved relative to ``cwd`` and must be
            executable. A bare filename (no directory component, e.g.
            ``deploy.sh``) is resolved relative to ``cwd`` — it is **not**
            searched on PATH. Names that already carry a directory component
            (``./deploy.sh``, ``bin/deploy.sh``, ``/abs/deploy.sh``) are used
            as-is (TASK-FMDR-007).
        env_file: Optional path to an environment file. Passed via ENV_FILE
            environment variable. No existence check is performed (ASSUM-013).
        timeout: Maximum execution time in seconds. Defaults to 600 (10 min).
        output_cap: Maximum output size in bytes. Defaults to 1 MiB.
        extra_env: Optional additional environment variables merged into the
            subprocess environment AFTER ENV_FILE (so entries here win on a
            key collision). Handlers use this to thread step-level signals
            (e.g. the O-32 revert contract) to the vetted script.
        memory_project: The memory name recorded for the build this script is
            running for, handed over on purpose. ``None`` = nothing was
            recorded, and then no name is set at all, which is memory off
            rather than somebody else's project name.
        launch_settings: The setting NAMES the project itself declared its own
            programs need beyond the factory's own list, read at the commit the
            work started from. Names only; a value never comes out of a
            project's settings file.

    Returns:
        A tuple of (exit_code, output):
        - exit_code: 0 on success, 124 on timeout, or the script's exit code.
        - output: Combined stdout+stderr, truncated if needed, with credentials
          scrubbed. Empty string if no output.

    Raises:
        Never raises. All errors (missing script, permission denied, etc.) are
        returned as non-zero exit codes with descriptive output.
    """
    # AN OWNED DEPLOY IS NOT RUN HERE. A step that carries the ownership of a
    # deployment target has to go through the EXECUTOR — one deploy command
    # per target, held for the whole life of the command's process group,
    # findable and stoppable by a takeover. This core can do none of that: it
    # starts a subprocess and waits. Running an owned deploy here would be
    # exactly the ungated deploy the design's fourth revision H removes, so it
    # is refused in plain words rather than run.
    if deploy:
        return (
            1,
            "this deploy owns a deployment target, and it was sent to the "
            "plain subprocess runner, which cannot hold one command per "
            "target and cannot stop one that is already running. Nothing was "
            "deployed. An owned deploy runs through the executor in the "
            "helper service where the project lives.",
        )

    # THE ENVIRONMENT DOOR (23 September 2026; the one-true-copy design pass,
    # item 1, fourth revision H, "the environment door"). This used to be
    # ``os.environ.copy()`` — whatever the process that got here happened to
    # be holding, handed whole to a project's own program. It was the ONE
    # command the sandbox's helper service did not filter, and its own
    # docstring said so where a reader met it.
    #
    # Now the child's environment is BUILT: the factory's own named list, plus
    # the memory name recorded for this build and the setting NAMES the
    # project itself declared, both passed in by the caller that read them off
    # the ledger. What is not named is not passed — not a credential the
    # operator's shell was carrying, not an agent socket, not the
    # coordinator's ledger.
    env = build_launch_env(
        memory_project=memory_project, declared=launch_settings or ()
    )
    if env_file is not None:
        env["ENV_FILE"] = env_file
    if extra_env:
        env.update(extra_env)

    # Resolve a bare script name (no directory component) relative to cwd.
    # subprocess resolves an executable containing a path separator relative
    # to cwd, but searches PATH for a bare name — so "deploy.sh" would not be
    # found in cwd. Prepending "./" honors the "script is relative to cwd"
    # contract while leaving paths that already carry a directory component
    # (e.g. "./deploy.sh", "bin/deploy.sh", "/abs/deploy.sh") untouched
    # (TASK-FMDR-007).
    program = script if os.path.dirname(script) else os.path.join(os.curdir, script)

    try:
        # Run the subprocess with combined output capture
        result = subprocess.run(
            [program],
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


def deploy_compose(step: Step, *, runner: ScriptRunner = _run_script_step) -> StepOutcome:
    """Execute a deploy_compose step (TASK-SSH-003).

    Extracts cwd, script, env_file (and optional timeout/output_cap overrides)
    from step.params, delegates to the shared core (``runner``, defaulting to
    :func:`_run_script_step`), and maps the exit status to a verdict:
    0 → passed, non-zero → failed.

    The ``runner`` seam (default = the in-process subprocess core) lets the
    deploy stage route this step's execution through the forge-deploy-sidecar
    when ``deploy.execution_surface == "sidecar"`` (S1, C4 residue #24) WITHOUT
    duplicating the O-32 revert-env threading below. The default keeps the local
    path byte-identical to before the seam existed.

    Vetted-script revert contract (O-32, C4-prep): revert runbooks
    (:func:`forge.deploy.runbook_builder.build_revert_runbook`) carry
    ``revert: True`` and ``rollback_image_ref: <tag>`` in step.params. This
    handler threads them to the script as environment variables, following the
    unprefixed ENV_FILE naming precedent:

    - ``REVERT=1`` — set only when ``params["revert"]`` is truthy.
    - ``ROLLBACK_IMAGE_REF=<tag>`` — set only when ``params["rollback_image_ref"]``
      is a non-empty string. A non-string or empty value is never silently
      stringified — it is omitted, and the vetted script's own
      revert-without-ref refusal fails loud.

    Without this threading a revert step would re-run the deploy script in
    NORMAL mode, re-deploying the very build it was meant to roll back.

    Args:
        step: The Step instance containing params and metadata.

    Returns:
        StepOutcome with status (passed/failed) and a JSON-serializable result
        dict containing exit_code and scrubbed captured_output.

    Raises:
        Never raises. All errors are returned as failed outcomes.
    """
    # Extract required params
    cwd = step.params["cwd"]
    script = step.params["script"]
    env_file = step.params.get("env_file")

    # Extract optional overrides (use defaults if not provided)
    timeout = step.params.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    output_cap = step.params.get("output_cap", DEFAULT_OUTPUT_CAP_BYTES)

    # Step-level env overlay (candidate-then-promote sequencing, S2F): a
    # ``extra_env`` param dict is threaded verbatim to the vetted script. It
    # carries the candidate/promote mode flags + the candidate.env addressing
    # overlay ({CANDIDATE:"1", CANDIDATE_PORT:...} / {PROMOTE:"1"} /
    # {CANDIDATE_DOWN:"1", ...}). Absent (every legacy/normal-mode step) ⇒ no
    # entries added ⇒ byte-identical to before the seam existed.
    extra_env: dict[str, str] = {}
    params_extra_env = step.params.get("extra_env")
    if isinstance(params_extra_env, dict):
        for k, v in params_extra_env.items():
            if isinstance(k, str) and isinstance(v, str):
                extra_env[k] = v

    # O-32 revert contract: thread the revert signal to the vetted script.
    revert = bool(step.params.get("revert"))
    rollback_image_ref = step.params.get("rollback_image_ref")
    if revert:
        extra_env["REVERT"] = "1"
    if isinstance(rollback_image_ref, str) and rollback_image_ref:
        extra_env["ROLLBACK_IMAGE_REF"] = rollback_image_ref

    # WHAT THE PROJECT DECLARED, AND WHICH MEMORY THIS WORK BELONGS TO. Both
    # ride the step's own params, put there by whatever built the runbook from
    # the ledger. Absent ⇒ the factory's own list and memory explicitly off,
    # which is the honest state and never a guessed name.
    # WHO OWNS THE TARGET THIS STEP CHANGES. Present only on a leg that
    # changes the live thing; its presence sends the step through the
    # executor, and a runner that has none refuses rather than deploying
    # ungated.
    ownership = step.params.get("deploy")
    memory_project = step.params.get("memory_project")
    declared = step.params.get("launch_settings")
    launch_settings = (
        tuple(str(name) for name in declared if isinstance(name, str))
        if isinstance(declared, (list, tuple))
        else ()
    )

    # WHERE THOSE NAMES WERE SAID: the recorded commit this work starts from.
    # Only the far side's environment door reads a project's declarations, so
    # only it uses this — and it is sent ONLY when there is one, so a runner
    # written before it existed is called exactly as it always was.
    said_at = step.params.get("declared_at")
    where_declared = (
        str(said_at).strip()
        if isinstance(said_at, str) and said_at.strip()
        else None
    )

    # Delegate to the runner (default = in-process subprocess core)
    exit_code, captured_output = runner(
        cwd=cwd,
        script=script,
        env_file=env_file,
        timeout=timeout,
        output_cap=output_cap,
        extra_env=extra_env or None,
        memory_project=(
            str(memory_project).strip()
            if isinstance(memory_project, str) and memory_project.strip()
            else None
        ),
        launch_settings=launch_settings,
        **({"declared_at": where_declared} if where_declared else {}),
        deploy=dict(ownership) if isinstance(ownership, dict) else None,
    )

    # Map exit status to verdict
    status = StepStatus.passed if exit_code == 0 else StepStatus.failed

    # Build result dict (JSON-serializable, executor persists it verbatim)
    result = {
        "exit_code": exit_code,
        "captured_output": captured_output,
    }

    return StepOutcome(status=status, result=result)


def run_smoke_tests(step: Step) -> StepOutcome:
    """Execute a run_smoke_tests step (TASK-SSH-004).

    Extracts cwd, script, env_file (and optional timeout/output_cap overrides)
    from step.params, delegates to the shared core (_run_script_step), and maps
    the exit status to a verdict: 0 → passed, non-zero → failed.

    For smoke tests, the script's exit status IS the verdict.

    Args:
        step: The Step instance containing params and metadata.

    Returns:
        StepOutcome with status (passed/failed) and a JSON-serializable result
        dict containing exit_code and scrubbed captured_output.

    Raises:
        Never raises. All errors are returned as failed outcomes.
    """
    # Extract required params
    cwd = step.params["cwd"]
    script = step.params["script"]
    env_file = step.params.get("env_file")

    # Extract optional overrides (use defaults if not provided)
    timeout = step.params.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    output_cap = step.params.get("output_cap", DEFAULT_OUTPUT_CAP_BYTES)

    # Delegate to shared core
    exit_code, captured_output = _run_script_step(
        cwd=cwd,
        script=script,
        env_file=env_file,
        timeout=timeout,
        output_cap=output_cap,
    )

    # Map exit status to verdict: 0 → passed, non-zero → failed
    status = StepStatus.passed if exit_code == 0 else StepStatus.failed

    # Build result dict (JSON-serializable, executor persists it verbatim)
    result = {
        "exit_code": exit_code,
        "captured_output": captured_output,
    }

    return StepOutcome(status=status, result=result)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def register_shell_handlers(registry: StepTypeRegistry) -> None:
    """Register shell-step handlers in the StepTypeRegistry (TASK-SSH-005).

    Wires deploy_compose and run_smoke_tests handlers into the provided registry
    under their step-type keys, making them discoverable by the executor.

    Args:
        registry: The StepTypeRegistry instance to register handlers in.

    Per FEAT-SSH planning, the step-type keys match handler names:
    - "deploy_compose" → deploy_compose
    - "run_smoke_tests" → run_smoke_tests
    """
    registry.register("deploy_compose", deploy_compose)
    registry.register("run_smoke_tests", run_smoke_tests)


__all__ = [
    "_run_script_step",
    "ScriptRunner",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_OUTPUT_CAP_BYTES",
    "deploy_compose",
    "run_smoke_tests",
    "register_shell_handlers",
]
