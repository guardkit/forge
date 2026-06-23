# INPUT — harness-context.md  ·  FS-01

> Candidate input. The harness machinery relevant to this stall. **Capture-pending:** paste the actual source from `guardkit` (paths below) before the run — `run_proposer.py`'s author will be in that tree anyway.

The proposer needs the three harness mechanisms that jointly let a partial-pass green through:

1. **Per-task test scope** — how the Player/Coach decide *which* tests run for a task. The failure: only the new module's tests ran, not the full suite, so a change to a shared file (`app.py` lifespan) that breaks a *pre-existing* test (`test_app_lifespan`) was never executed.
   - Capture from `guardkit`: the Coach test-execution path (the `autobuild.coach.test_execution: subprocess` runner) and how it selects the pytest target/scope per task.

2. **Smoke-gate scheduling** — when the feature-level smoke gate runs relative to waves. The failure: the smoke gate ran after wave 3, so wave 4's edit was never smoke-covered.
   - Capture from `guardkit`: the orchestrator's smoke-gate trigger (`guardkit.orchestrator.*` — the wave/gate scheduling).

3. **Coach approval criteria** — what the Coach treats as sufficient evidence to approve.
   - Capture from `guardkit`: the Coach approval/scoring logic.

> Identifiers to grep in `~/Projects/appmilla_github/guardkit`: the smoke-gate scheduler in `guardkit.orchestrator.feature_orchestrator`, the Coach test runner, and the per-task verification scope. Paste the relevant functions here.
