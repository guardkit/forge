---
id: TASK-REV-F09A
title: "Diagnose & fix FEAT-FORGE-009 autobuild bootstrap failure"
status: review_complete
created: 2026-04-30T00:00:00Z
updated: 2026-04-30T00:00:00Z
priority: high
task_type: review
review_mode: decision
review_depth: standard
tags: [review, autobuild, bootstrap, nats-core, pep-668, uv-sources, FEAT-FORGE-009]
complexity: 6
feature_id: FEAT-FORGE-009
related_tasks:
  - TASK-FIX-F0E6   # sibling-source override that introduced [tool.uv.sources]
  - TASK-FIX-F0E6b  # follow-up to republish a corrected nats-core wheel
context_files:
  - docs/history/autobuild-FEAT-FORGE-009-failure-run-1-history.md
  - pyproject.toml
  - .guardkit/features/FEAT-FORGE-009.yaml
  - tasks/backlog/TASK-FIX-F0E6b-republish-nats-core-wheel.md
review_results:
  mode: decision
  depth: standard
  decision: implement-layered (G + A + D)
  recommended_layer1: forge-side preflight.sh + add click/rich deps + runbook entry
  recommended_layer2: cross-repo guardkit task to honour [tool.uv.sources] when uv on PATH
  recommended_layer3: ride existing TASK-FIX-F0E6b (nats-core wheel republish)
  rejected_options: [C-warn-mode, E-git-url, F-private-index, H-vendored-wheel]
  report_path: .claude/reviews/TASK-REV-F09A-review-report.md
  implementation_tasks:
    - TASK-FIX-F09A1  # Layer 1: forge-side ship-now (priority high, run today)
    - TASK-FIX-F09A2  # Layer 2: guardkit-side durable (priority medium, cross-repo)
    - TASK-FIX-F0E6b  # Layer 3: existing — long-term root cause (not duplicated here)
  completed_at: 2026-04-30T00:00:00Z
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Diagnose & fix FEAT-FORGE-009 autobuild bootstrap failure

## Description

Decision-mode review to analyse, diagnose, and select a fix for the
GuardKit autobuild orchestration failure observed on
**FEAT-FORGE-009 (Forge Production Image)** — Run 1, 2026-04-30.

### Symptom (verbatim from the failure transcript)

`guardkit autobuild feature FEAT-FORGE-009 --verbose` failed during
**Phase 1 (Setup) → environment bootstrap**, before any task execution.
The orchestrator's bootstrap step ran `pip install -e .` inside the
freshly created worktree at
`.guardkit/worktrees/FEAT-FORGE-009/`, fell back to a venv after a PEP
668 externally-managed-environment error, and then hard-failed with:

```
ERROR: Ignored the following versions that require a different python
       version: 0.1.0 Requires-Python >=3.13;
                0.2.0 Requires-Python >=3.13
ERROR: Could not find a version that satisfies the requirement
       nats-core<0.4,>=0.3.0 (from forge) (from versions: 0.0.0)
ERROR: No matching distribution found for nats-core<0.4,>=0.3.0

guardkit.orchestrator.feature_orchestrator.FeatureOrchestrationError:
  Bootstrap hard-fail: 0/1 install(s) succeeded for essential stack(s):
  python.
  Manifest requires-python: >=3.11
```

Full transcript: `docs/history/autobuild-FEAT-FORGE-009-failure-run-1-history.md`.

### Initial root-cause hypothesis (to be confirmed by the review)

Three intersecting facts produce the failure:

1. **`forge/pyproject.toml`** declares `nats-core>=0.3.0,<0.4` as a hard
   dependency and `requires-python = ">=3.11"`.
2. **PyPI** does not publish a satisfying `nats-core` artefact: only
   `0.0.0` is available under the requested constraint, and the newer
   `0.1.0` / `0.2.0` releases require Python `>=3.13` — see
   `TASK-FIX-F0E6b-republish-nats-core-wheel.md` for the upstream wheel
   bug.
3. **`forge/pyproject.toml`** works around (2) with
   `[tool.uv.sources] nats-core = { path = "../nats-core", editable = true }`
   — but this directive is read by **`uv` only**. GuardKit's
   `environment_bootstrap` runs **`pip install -e .`**, which silently
   ignores `[tool.uv.sources]` and falls back to PyPI, where the
   dependency cannot resolve.

So although the developer-machine workflow (`uv sync` / `uv pip install`)
works fine, the autobuild's `pip`-based bootstrap is **structurally
incapable** of resolving the dependency graph for any branch that ships
a sibling-source override. This affects every GuardKit autobuild run on
this repo until either (a) the bootstrap learns to honour
`[tool.uv.sources]`, or (b) the dependency is satisfiable from the
public index, or (c) the bootstrap is configured to skip / warn.

### Why this needs review (not direct implementation)

Multiple credible fixes exist with different blast radii and ownership
boundaries (forge repo, GuardKit repo, nats-core repo). Picking one
without analysis risks doing the work in the wrong repo or leaving the
hole open for the next feature.

## Goals

1. **Confirm the root cause** with empirical evidence (re-run the
   bootstrap manually with `uv` vs `pip`; inspect the resolver output;
   verify `[tool.uv.sources]` is the load-bearing piece).
2. **Enumerate the candidate fixes** and evaluate each on
   correctness / blast radius / time-to-unblock / ownership.
3. **Recommend a fix** and produce an implementation task (or tasks)
   to land it.
4. **Unblock FEAT-FORGE-009 autobuild Run 2** so the 8-task / 4-wave
   feature can proceed.

## Candidate Fixes to Evaluate

The reviewer should weigh at least these options and may surface others:

| Option | Repo | Description | Effort | Risk |
|--------|------|-------------|--------|------|
| **A. Switch GuardKit bootstrap to `uv`** | guardkit | Detect `[tool.uv.sources]` (or a `uv.lock`) and use `uv pip install -e .` instead of `pip install -e .`. Honours sibling-source overrides. | M | Low–Med |
| **B. Pre-bootstrap the venv before invoking guardkit** | forge | Run `uv sync` (or equivalent) into `.guardkit/worktrees/<FEAT>/.guardkit/venv` *before* `guardkit autobuild`, then have GuardKit detect and reuse it. | S–M | Low |
| **C. Configure `bootstrap_failure_mode: warn`** | forge | Add `.guardkit/config.yaml` setting (or pass `--bootstrap-failure-mode warn`) so the orchestrator continues despite install failure. **Workaround only — leaves the install broken inside the worktree.** | XS | High (masks real failures) |
| **D. Republish a working `nats-core` wheel to PyPI** | nats-core | Already tracked as `TASK-FIX-F0E6b`. Removes the need for the sibling-source override entirely. | M (cross-repo) | Low (long-term right answer) |
| **E. Vendored / git-URL dependency** | forge | Replace `nats-core>=0.3.0,<0.4` with `nats-core @ git+ssh://…@<sha>` (works with plain `pip`). | S | Med (lock fidelity) |
| **F. Per-worktree `pip.conf` with extra-index-url to a private wheel server** | infra | Publish the corrected wheel internally so plain `pip` resolves it. | M | Med |

## Acceptance Criteria

- [ ] **Failure reproduced locally** with both `pip install -e .` (fails)
      and `uv pip install -e .` (succeeds) inside a fresh
      `.guardkit/worktrees/FEAT-FORGE-009/` checkout, and the diff in
      behaviour traced to `[tool.uv.sources]` honouring.
- [ ] **Root-cause statement confirmed or revised** in writing, with
      pointers to the exact `pyproject.toml` lines and the GuardKit
      `environment_bootstrap` code path (`guardkit/orchestrator/environment_bootstrap.py`)
      that picks `pip` over `uv`.
- [ ] **Each candidate fix scored** on correctness, blast radius
      (forge / guardkit / nats-core), time-to-unblock, and durability.
      Surface any options not on the table above.
- [ ] **Decision recorded** with explicit justification — which option
      lands now, which (if any) follow up, which are rejected and why.
- [ ] **Implementation task(s) created** for the chosen fix. If the fix
      lives in another repo (e.g. guardkit, nats-core), open a
      cross-repo task / issue with a clear hand-off.
- [ ] **FEAT-FORGE-009 autobuild Run 2 verified green** (or, if the
      chosen fix is multi-step, the next blocking gate is documented).
- [ ] **Runbook / history updated**: append a "Run 1 failure → fix →
      Run 2 outcome" section to
      `docs/history/RUNBOOK-FEAT-FORGE-008-validation-run-1.md` (or a
      new FEAT-FORGE-009 runbook), referencing this task by ID.

## Out of Scope

- Implementing the underlying `nats-core` wheel republish — that work is
  already scoped in `TASK-FIX-F0E6b`. This review may **recommend** that
  it be expedited but should not duplicate it.
- Re-architecting GuardKit's environment bootstrap beyond the minimum
  needed to honour `[tool.uv.sources]` (or to detect a pre-built venv).

## Test Requirements

- [ ] Reproduction script (`pip` vs `uv` install) is captured in the
      review report so the failure mode is replayable.
- [ ] Post-fix: a clean
      `guardkit autobuild feature FEAT-FORGE-009 --verbose --fresh`
      run completes Phase 1 (Setup) without bootstrap hard-fail.
- [ ] Post-fix: the `forge` console-script entry point resolves inside
      the bootstrapped venv (`.guardkit/venv/bin/forge --help` works).

## Implementation Notes

- The failing transcript is the canonical artefact —
  `docs/history/autobuild-FEAT-FORGE-009-failure-run-1-history.md`.
- The `TASK-FIX-F0E6b` task body has the full chain of how the
  `[tool.uv.sources]` workaround came to exist (TASK-FIX-F0E6 option (b)
  for the demo unblock, with option (d) deferred). Read it before
  recommending option D above — there is a deliberate reason the
  sibling-source override exists, and removing it has cross-team
  coordination implications.
- The orchestrator emits a hint:
  *"set `bootstrap_failure_mode: warn` in `.guardkit/config.yaml`"* —
  this is a band-aid, not a fix. Reviewer should explicitly justify if
  recommending it as the chosen path.
- The worktree at `.guardkit/worktrees/FEAT-FORGE-009/` already exists
  from the failed run (per `git status`); clean it up or reuse it as
  the reproduction harness.

## Test Execution Log

[Automatically populated by /task-review and downstream /task-work]
