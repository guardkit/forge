---
id: TASK-SSH-002
title: Subprocess-runner core (timeout + size-cap + scrub at boundary)
status: in_review
priority: high
task_type: feature
parent_review: TASK-REV-SSH1
parent_feature: FEAT-SSH
feature_slug: shell-script-step-handlers
wave: 2
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies:
- TASK-SSH-001
tags:
- forge
- runbook
- shell-step
- subprocess
consumer_context:
- task: TASK-SSH-001
  consumes: SCRUB_MARKERS
  framework: src/forge/memory/redaction.scrub_process_output (pure str->str)
  driver: re
  format_note: 'Captured output MUST pass through scrub_process_output exactly once,
    at the capture boundary, before it is returned or stored. Markers: ***REDACTED-DSN***
    / ***REDACTED-PASSWORD***.'
autobuild_state:
  current_turn: 1
  max_turns: 5
  worktree_path: /Users/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-SSH
  base_branch: main
  started_at: '2026-06-22T15:31:23.541937'
  last_updated: '2026-06-22T15:40:44.879743'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-06-22T15:31:23.541937'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-SSH-002 — Subprocess-runner core

## Context

Both shell-step handlers (`deploy_compose`, `run_smoke_tests`) share identical
mechanics: run a named script in a working directory with an env-file path
available, capture combined stdout+stderr, and produce an `(exit_code, output)`
pair. Implementing this once removes the duplicate credential-leak surface (the
security focus from review). The two thin handlers (TASK-SSH-003/004) only differ
in verdict semantics, which they layer on top of this core.

This core also carries the **timeout + size-cap hardening** elected during
planning (extends ASSUM-008/009 — these were low-confidence "deferred" in the
spec and are now in-scope). ASSUM-013 (env-file pre-validation) remains
**deferred**: a missing env file is *not* checked here; it surfaces as the
script's own non-zero exit.

## Scope

New module `src/forge/executor/shell_steps.py` with a private core:

```python
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_OUTPUT_CAP_BYTES = 1_048_576  # 1 MiB

def _run_script_step(
    *, cwd: str, script: str, env_file: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    output_cap: int = DEFAULT_OUTPUT_CAP_BYTES,
) -> tuple[int, str]: ...
```

Behaviour:
1. Run `script` as a subprocess with `cwd` as its working directory.
2. Make `env_file` available to the script **by path only** — pass it via an
   environment variable (e.g. `ENV_FILE=<path>`); never read its contents.
3. Capture stdout and stderr into one combined buffer.
4. On `subprocess.TimeoutExpired`: kill the process, capture whatever partial
   output exists, and return a non-zero exit code (the timeout sentinel).
5. Truncate combined output to `output_cap` bytes **before** scrubbing, appending
   a truncation marker when truncated.
6. Pass the (possibly truncated) output through `scrub_process_output` **exactly
   once** before returning.

## Acceptance Criteria

- [ ] `_run_script_step` runs the named script with `cwd` as its current working
      directory (covers `.feature`: "runs with that working directory as its
      current directory").
- [ ] `env_file` is exposed to the subprocess **by path only**; the function
      never opens or reads the env file (covers "environment file referenced
      only by its path").
- [ ] Combined stdout **and** stderr are captured into a single output string
      (covers "secret printed to the error stream is captured and scrubbed").
- [ ] Captured output is passed through `scrub_process_output` exactly once at
      the capture boundary; a postgres DSN or password in either stream does not
      appear in the returned output (covers the scrub-on-store scenarios).
- [ ] A script that exceeds `timeout` is killed and the call returns a non-zero
      exit code with the partial output captured — it does **not** hang
      (hardens ASSUM-008).
- [ ] Output longer than `output_cap` is truncated to the cap before scrubbing,
      with a visible truncation marker (hardens ASSUM-009).
- [ ] A script producing no output returns `(0, "")` for the clean-exit path
      (covers "produces no output still records a result").
- [ ] A missing / non-executable script does **not** raise out of the function —
      it is contained as a non-zero exit code (covers "script that cannot be run
      … executor should not crash").
- [ ] The function performs no env-file existence check (ASSUM-013 stays
      deferred).
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Coach Validation

```bash
pytest tests/forge/executor/test_shell_steps_core.py -v
ruff check src/forge/executor/shell_steps.py
ruff format --check src/forge/executor/shell_steps.py
```

## Seam Tests

The following seam test validates the credential-scrub contract (`SCRUB_MARKERS`)
with producer TASK-SSH-001. Implement it to verify the boundary before
integration — it is the security-critical seam.

```python
"""Seam test: verify SCRUB_MARKERS contract from TASK-SSH-001."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("SCRUB_MARKERS")
def test_scrub_markers_applied_at_capture_boundary(tmp_path):
    """Captured output is scrubbed exactly once before return.

    Contract: output containing a postgres DSN must come back with the DSN
    replaced by ***REDACTED-DSN***; scrubbing is idempotent.
    Producer: TASK-SSH-001 (scrub_process_output).
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


@pytest.mark.seam
@pytest.mark.integration_contract("SCRUB_MARKERS")
def test_subprocess_timeout_does_not_hang(tmp_path):
    """A hung script is killed at the timeout and returns non-zero."""
    from forge.executor.shell_steps import _run_script_step

    script = tmp_path / "hang.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    script.chmod(0o755)

    exit_code, _ = _run_script_step(
        cwd=str(tmp_path), script=str(script), env_file=None, timeout=1.0
    )
    assert exit_code != 0
```

## Implementation Notes

- Use `subprocess.run(..., capture_output=True, text=True, timeout=...)` and
  combine `stdout + stderr`, or `Popen` + `communicate(timeout=...)` if finer
  control over the partial-output-on-timeout path is needed.
- The timeout sentinel exit code should be a stable non-zero value (e.g. `124`,
  the GNU `timeout` convention) so handlers map it to `failed` deterministically.
- Truncate on a byte boundary, then `.decode(errors="replace")` if working in
  bytes, to avoid splitting a multibyte sequence before scrubbing.
