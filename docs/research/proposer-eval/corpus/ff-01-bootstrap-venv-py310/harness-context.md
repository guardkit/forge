# INPUT — harness-context.md  ·  FF-01 (bootstrap-venv-py310)

> **AUTOGEN TARGET — not yet populated.** `../capture-prefix-harness.sh` will **overwrite** this file with the exact *pre-fix* source from `base_commit`. It must contain **only** the buggy pre-fix harness code — **no gold**. The fix lives in `GOLD.md` / `gold-fix.patch`, never here.
>
> **Capture target:** `EnvironmentBootstrapper._ensure_worktree_venv` (+ `DetectedManifest.get_requires_python`) in `guardkit/orchestrator/environment_bootstrap.py`, at the parent of the **TASK-AB-BOOTPY01** fix (search token `_uv_python_request`). After the script writes the whole pre-fix file, trim to that function plus enough surrounding context to stand alone.
>
> **Observable behaviour at base (what the candidate diagnoses):** on a `requires-python >= 3.12` project the worktree venv comes up on CPython 3.10.x and bootstrap hard-fails on the version floor. (Full stall trace: `trace.log`.)
