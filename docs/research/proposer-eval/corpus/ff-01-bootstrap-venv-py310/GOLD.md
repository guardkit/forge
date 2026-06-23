# GOLD (answer key) — FF-01 bootstrap-venv-py310

> **Held-out. Never shown to the candidate.** Source: quirks#1; FEAT-CA81 (2026-06-12).
> **Canonical gold-fix = `gold-fix.patch`** (the real committed diff, produced by `../capture-prefix-harness.sh` from `base_commit`..fix-commit). `base_commit.txt` records the pre-fix ref. The code below is the grader's reference for that diff.

**label:** `false-failure`

## Gold diagnosis

Not a code/project defect — a harness bootstrap defect. `uv venv --seed` with no `--python` flag prefers a uv-managed interpreter, and the only uv-managed Python on this machine is CPython 3.10.19. Projects requiring `>= 3.12` therefore hard-fail bootstrap on a version floor that has nothing to do with their code.

## Gold fix (reference — canonical form is `gold-fix.patch`)

Pin the interpreter at venv creation: derive it from the manifest's `requires-python` and pass it to `uv venv`. Committed under **TASK-AB-BOOTPY01** ("FEAT-MEM-01 Error 1: Python 3.10 bootstrap trap"):

```python
_PY_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

def _uv_python_request(requires_python):      # ">=3.12,<4.0" -> "3.12"  (lower bound)
    # min() over every major.minor token in the specifier; None if unparseable
    ...

# inside _ensure_worktree_venv, when uv is on PATH:
cmd = ["uv", "venv", "--seed"]
py_request = _uv_python_request(requires_python)     # from manifest.get_requires_python()
if py_request:
    cmd += ["--python", py_request]
cmd.append(str(venv_dir))
```

`requires_python` threads from `DetectedManifest.get_requires_python()` (PEP 621 `[project].requires-python`, Poetry `[tool.poetry].python` fallback). A pre-pip `check_requires_python_precheck()` also surfaces a clean mismatch before pip runs. The immediate operator workaround at the time was pre-creating `.venv` with `uv venv --seed --python 3.14`, or `uv python install`-ing a `>=3.12` managed version.

## verify.sh (held-out)

```bash
#!/usr/bin/env bash
set -euo pipefail
# On a fixture project with requires-python >= 3.12, run bootstrap WITH the candidate's fix
# against a PRE-FIX checkout (base_commit). ASSERT the created venv's python is >= 3.12
# and bootstrap install succeeds.
# <run guardkit bootstrap on the 3.12+ fixture in the base_commit worktree>
# "$WT/.venv/bin/python" --version   # assert >= 3.12
```
