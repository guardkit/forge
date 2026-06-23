# GOLD (answer key) — FF-07 stale-coach-venv-middep

> **Held-out. Never shown to the candidate.** Source: quirks#6; FEAT-MEM-05, TASK-RA-003 (2026-06-13).
> **Canonical gold-fix = `gold-fix.patch`** (real committed diff, produced by `../capture-prefix-harness.sh`). `base_commit.txt` records the pre-fix ref. Code below is the grader's reference.

**label:** `false-failure`

## Gold diagnosis

Not a code defect. The bootstrap venv is provisioned once at feature start and re-bootstrapped only **between** waves. A task that adds a runtime dep (TASK-RA-003 added `tiktoken`) and consumes it **within the same wave** leaves the Coach's independent `pytest` running against the stale venv → `ModuleNotFoundError` → every AC rejected → `unrecoverable_stall` / `context_pollution_stall_no_checkpoint`. The Player code was correct (321/321 once the dep was installed); the Coach's environment lagged the Player's manifest edit by one wave — a false-red.

## Gold fix (reference — canonical form is `gold-fix.patch`)

A per-turn refresh, gated by a cheap basename diff so unaffected turns pay nothing. Committed under **TASK-AB-COACHVENV01**:

```python
_DEPENDENCY_MANIFEST_NAMES = frozenset({"pyproject.toml","uv.lock","poetry.lock",
    "package.json","package-lock.json","pnpm-lock.yaml","yarn.lock","go.mod",
    "go.sum","Cargo.toml","Cargo.lock","pubspec.yaml","pubspec.lock"})
_REQUIREMENTS_RE = re.compile(r"requirements[\w.-]*\.txt$")

def changed_dependency_manifests(changed_paths):
    # subset of this turn's changed files that are dependency manifests (basename match)
    ...

def refresh_environment_for_changes(worktree_root, changed_paths, *, python_extras=(), relevant_stacks=None):
    if not changed_dependency_manifests(changed_paths):
        return None                                  # nothing touched -> no-op
    manifests = ProjectEnvironmentDetector(worktree_root, python_extras=...).detect()
    if not manifests:
        return None
    return EnvironmentBootstrapper(worktree_root).bootstrap(manifests, relevant_stacks=relevant_stacks)
```

Called from the `feature_orchestrator` per-turn hook **before** the Coach's independent test run, using the Player report's `files_modified` + `files_created`. The idempotent `bootstrap()` re-installs (content-hash dedup re-fires) while reusing the existing `<root>/.venv`. Immediate operator workaround at the time: `pip install -e '.[dev]'` then `--resume`.

## verify.sh (held-out)

```bash
#!/usr/bin/env bash
set -euo pipefail
# Against a PRE-FIX checkout (base_commit): simulate a task that adds a new runtime dep
# mid-wave, apply the candidate's fix, run the Coach's independent test for that turn.
# ASSERT the Coach venv resolves the new dep (no ModuleNotFoundError) and the task clears
# rather than context_pollution_stall_no_checkpoint.
```
