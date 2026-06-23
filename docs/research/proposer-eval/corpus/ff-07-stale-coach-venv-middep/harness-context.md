# INPUT — harness-context.md  ·  FF-07 (stale-coach-venv-middep)

> **AUTOGEN TARGET — not yet populated.** `../capture-prefix-harness.sh` will **overwrite** this file with the exact *pre-fix* source from `base_commit`. **Only** buggy pre-fix code here — **no gold** (that lives in `GOLD.md` / `gold-fix.patch`).
>
> **Capture target:** the pre-fix venv lifecycle in `guardkit/orchestrator/environment_bootstrap.py` + the `feature_orchestrator` turn loop, at the parent of the **TASK-AB-COACHVENV01** fix (search token `changed_dependency_manifests`). At base, the helpers `changed_dependency_manifests` / `refresh_environment_for_changes` do **not** exist and the orchestrator has no per-turn manifest-change refresh before the Coach test. Trim the written pre-fix source to that turn-loop region.
>
> **Observable behaviour at base (what the candidate diagnoses):** a task that adds a runtime dep mid-wave stalls — the Coach's independent `pytest` runs against the stale venv → `ModuleNotFoundError` → every AC rejected → `context_pollution_stall_no_checkpoint`. (Full stall trace: `trace.log`.)
