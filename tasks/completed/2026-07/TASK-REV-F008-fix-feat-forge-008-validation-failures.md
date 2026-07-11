---
id: TASK-REV-F008
title: "Fix FEAT-FORGE-008 validation failures and address runbook findings"
task_type: review
status: review_complete
priority: high
created: 2026-04-29T18:00:00Z
updated: 2026-04-29T20:00:00Z
complexity: 6
tags: [review, validation, regression, mode-a, mode-c, runbook, feat-forge-008]
related_features: [FEAT-FORGE-007, FEAT-FORGE-008]
related_runbooks:
  - docs/runbooks/RUNBOOK-FEAT-FORGE-008-validation.md
  - docs/runbooks/RESULTS-FEAT-FORGE-008-validation.md
related_commits:
  - 2f13eac  # FEAT-FORGE-008 merge (Mode B + Mode C autobuild)
  - 22c0b1f  # autobuild metadata for FEAT-FORGE-008
  - 51ae6a6  # FEAT-FORGE-008 worktree cleanup
review_results:
  mode: architectural
  depth: standard
  focus: all
  tradeoff_priority: quality_correctness
  score: 72
  findings_count: 7
  recommendations_count: 8
  decision: implement
  report_path: docs/reviews/REVIEW-F008-validation-triage.md
  fan_out_folder: tasks/backlog/feat-f8-validation-fixes/
  child_tasks:
    - TASK-F8-003  # wave 1, blocker — supervisor _dispatch routing — ✅ 68adba9
    - TASK-F8-004  # wave 1 — recovery AC-008 single-ownership      — ✅ c9503f1
    - TASK-F8-005  # wave 1 — stale idempotency-test assertion      — ✅ 8c06012
    - TASK-F8-002  # wave 2 — Mode C task_id (cross-repo nats-core) — ✅ ae20423 (nats-core 0.3.0) + forge CLI/pin
    - TASK-F8-001  # wave 2 — forge.build modules (TASK-IC-009/010) — ✅ d0c2f81
    - TASK-F8-006  # wave 2 — runbook gap-fold (LES1 §8)            — ✅ 35952fd
    - TASK-F8-007a # wave 3 — NATS provisioning handoff (delegated) — ✅ 2026-04-30 (FCH-001 filed in nats-infrastructure)
    - TASK-F8-007b # wave 3 — Dockerfile scoping (sibling feature)  — ✅ 2026-04-30 (scoping doc + FEAT-FORGE-009 stub)
  re_run_gate: "After Wave 1 lands, re-run Phase 1 only (AC-6 GO/NO-GO). After Wave 2 lands, re-run Phases 1+2+3 to declare Step 6 canonical for Phases 0–3."
  completed_at: 2026-04-29T20:00:00Z
  ac6_gate_run:
    date: 2026-04-30
    verdict: GO
    pytest_full_sweep: "3804 passed, 1 skipped, 0 failed (vs. baseline 3757p/4f/1s)"
    mode_a_regression: "16/16 passed (TypeError on StageClass.TASK_REVIEW resolved)"
    bdd: "219/219 passed"
    sqlite_persistence: "12/12 passed (idempotency green)"
    approval_publisher: "35/35 passed (AC-008 single-ownership green)"
  wave_2_verification:
    date: 2026-04-30
    nats_core_version: "0.3.0 (commit ae20423)"
    forge_pin: "nats-core>=0.3.0,<0.4 in pyproject.toml"
    mode_c_smoke_e2e: "17/17 passed (tests/integration/test_mode_c_smoke_e2e.py)"
    nats_core_payload_tests: "21 passed, 4 skipped (k=build_queued|task_id|mode_c)"
    pending: "Operator runbook re-run of Phases 0–3 to formally declare Step 6 canonical"
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Fix FEAT-FORGE-008 validation failures and address runbook findings

## Description

The Step 6 validation walkthrough of `RUNBOOK-FEAT-FORGE-008-validation.md`
ran on 2026-04-29 against `main` at commit `5e5cc73` (FEAT-FORGE-008 merge
`2f13eac` already in history). It produced
`docs/runbooks/RESULTS-FEAT-FORGE-008-validation.md` with **decision = Step 6
partially passed**.

Headline outcome: 3757/3762 pytest pass, 64/64 BDD-008 scenarios pass, 68/68
Mode B + Mode C integration pass, but **Mode A regressed** — the runbook §1.3
hard-stop trap fired exactly as predicted ("If any Mode A test is red,
FEAT-FORGE-008 broke `Supervisor.next_turn` — stop and triage"). In addition,
three other unit/integration reds, one collection error, one Mode C wire-schema
contradiction, and ~9 runbook-as-code copy-paste gaps were captured.

The aim of this review is to **analyse those findings, choose the fix
sequence, and decide which become implementation tasks vs. follow-up runbook
edits** — so Step 6 can be re-run cleanly and declared canonical, unblocking
Step 7 (FinProxy first real run).

## Inputs the reviewer must read first

1. **`docs/runbooks/RESULTS-FEAT-FORGE-008-validation.md`** — full per-gate
   table, error excerpts, headline metrics, runbook-gap table, follow-up list
   F008-VAL-001..007.
2. **`docs/runbooks/RUNBOOK-FEAT-FORGE-008-validation.md`** — the runbook
   that was executed (so the reviewer can see which copy-paste blocks need
   fixing).
3. **`docs/research/ideas/forge-build-plan.md`** — Step 6 spec and the LES1
   parity gate definitions; this review's exit criterion is "Step 6 ✅
   canonical" against this plan.
4. **`src/forge/pipeline/supervisor.py`** at line ~1555 — the `_dispatch`
   raise site that breaks Mode A.
5. **`src/forge/lifecycle/migrations.py`** + the v2 migration — for the
   non-idempotent `apply_at_boot` failure.
6. **`src/forge/lifecycle/recovery.py`** — for the `evidence_priors`
   single-ownership AC-008 violation.
7. **`/home/richardwoollcott/Projects/appmilla_github/nats-core/src/nats_core/events/_pipeline.py`**
   line 27 — `FEATURE_ID_PATTERN = re.compile(r"^FEAT-[A-Z0-9]{3,12}$")`
   — the regex that contradicts Mode C ASSUM-004.
8. **`tests/integration/test_mode_a_concurrency_and_integrity.py`**
   `TestMultiFeatureCatalogue` and `TestCorrelationThreading` — the two red
   Mode A tests that must go green.
9. **`tests/forge/adapters/test_sqlite_persistence.py::test_apply_at_boot_is_idempotent`**
   — the non-idempotent migration assertion.
10. **`tests/forge/test_approval_publisher.py::TestSingleOwnership::test_no_other_module_in_forge_constructs_evidence_priors`**
    — the AC-008 structural guard.

## Findings to triage (verbatim from RESULTS §"Follow-up tasks")

### Blockers (Mode A regression — runbook §1.3 hard-stop)

- **F008-VAL-003** — `Supervisor._dispatch` raises
  `TypeError: no routing for stage <StageClass.TASK_REVIEW>` whenever Mode A
  reaches `task-review`. Add a routing branch in
  `src/forge/pipeline/supervisor.py:1555`.
  *Severity: blocker.*

### High-severity (semantic correctness)

- **F008-VAL-002** — Mode C wire-payload rejects `TASK-*` identifiers.
  `nats_core.events._pipeline.BuildQueuedPayload.feature_id` regex
  `^FEAT-[A-Z0-9]{3,12}$` contradicts FEAT-FORGE-008 ASSUM-004 (Mode C
  operates on `TASK-*` IDs). Either widen the regex (and probably rename
  the field to `subject_id`) or add a sibling `subject_id` field used
  only by Mode C. **Mode C is half-shipped: the CLI accepts `TASK-*`
  but the wire layer refuses it.**
- **F008-VAL-005** — `migrations.apply_at_boot` is non-idempotent for the
  v2 schema row. The v2 migration shipped with FEAT-FORGE-008 (almost
  certainly TASK-MBC8-001 mode column or TASK-MBC8-006 TASK_REVIEW/TASK_WORK
  seed) reinserts the `schema_version=2` row on every boot. Gate the
  `INSERT INTO schema_version` on `WHERE NOT EXISTS` (or use
  `INSERT OR IGNORE`).
- **F008-VAL-007** — Phases 4–6 prerequisites: provision the canonical
  JetStream NATS (streams + KV) on this host AND author the forge
  production `Dockerfile` so the LES1 CMDW/PORT/ARFS gates can run. Without
  these, Step 6 cannot be declared canonical even after F008-VAL-003 lands.

### Medium-severity (structural/regression)

- **F008-VAL-004** — `forge.lifecycle.recovery` open-codes the
  `evidence_priors` dict shape, violating `approval_publisher.py`'s AC-008
  single-ownership guard. Move construction back into
  `forge.approval_publisher` or import the helper.
- **F008-VAL-001** — `tests/bdd/test_infrastructure_coordination.py` cannot
  collect: `ModuleNotFoundError: No module named 'forge.build'`.
  TASK-IC-009/010 (`forge.build.git_operations`,
  `forge.build.test_verification`) were never landed in `src/forge/`.
  Pre-existing — *not* introduced by FEAT-FORGE-008, but blocks a clean
  Phase 1.1 sweep. Decide: implement TASK-IC-009/010 now, mark them
  scope-deferred and skip the test, or split them out as their own feature.
- **F008-VAL-006** — Runbook gap-fold: the runbook had ~9 copy-paste
  blocks that needed manual edits to execute (see RESULTS table). Apply
  every "Suggested runbook fix" to
  `RUNBOOK-FEAT-FORGE-008-validation.md` so the next walkthrough runs
  verbatim. Specifically:
    - 0.4: pin to `uv pip install -e ".[providers,dev]"`, add `uv` install
      step, document `--break-system-packages` fallback, and the
      stale-editable-install rewire trick.
    - 0.5: add `~/.local/bin` PATH check.
    - 0.6: add the throwaway-NATS-via-docker hint and link to the
      provisioned-NATS infra runbook.
    - 2.1+: replace `--db-path` on `forge queue` with
      `export FORGE_DB_PATH=...`; use `--db` (not `--db-path`) for
      `forge history`; show how to write a minimal `forge.yaml`
      (`permissions.filesystem.allowlist` is the only required field) and
      a per-feature stub YAML; remove the false claim that Phase 2 stubs
      NATS.
    - 2.x: change example IDs to `FEAT-TESTMA` / `FEAT-TESTMB` /
      `FEAT-TESTMC` (no inner hyphens; valid per wire regex). Note the
      `TASK-*` regression (F008-VAL-002) so it isn't accidentally retried.
    - 1.3: drop the non-existent `tests/bdd/test_feat_forge_007.py` path
      OR write the missing FEAT-FORGE-007 BDD bindings.
    - 1.1: footnote
      `--ignore=tests/bdd/test_infrastructure_coordination.py` until
      F008-VAL-001 closes.
    - 6.1: either author a forge production `Dockerfile` or strike the
      runbook claim that Phase 6 can run today.

## Acceptance Criteria

This review task is complete when ALL of the following are true:

- [ ] **AC-1**: A decision has been recorded for each finding F008-VAL-001
      through F008-VAL-007: implement-now, implement-later (with linked
      task), or won't-fix-with-rationale.
- [ ] **AC-2**: Implementation tasks have been created (via `/task-create`)
      for every finding marked implement-now. Each new task links back to
      this review via `parent_review: TASK-REV-F008`.
- [ ] **AC-3**: A clear sequencing decision exists for the implementation
      tasks (which lands first, which can run in parallel, what the
      re-run order of the runbook phases is once each lands).
- [ ] **AC-4**: For F008-VAL-002 (Mode C wire-schema vs CLI mismatch), a
      design choice is recorded — widen the regex, rename to
      `subject_id`, or add a sibling field. The choice must be reconciled
      with `nats-core` ownership (the regex lives in the sibling repo).
- [ ] **AC-5**: For F008-VAL-007 (NATS provisioning + production
      Dockerfile), the review records whether these become forge tasks
      or get delegated to `nats-infrastructure` and a separate
      containerisation effort.
- [ ] **AC-6**: A "go / no-go" decision is recorded for re-running
      `RUNBOOK-FEAT-FORGE-008-validation.md` Phase 1 only (cheap signal)
      after F008-VAL-003 + F008-VAL-005 land, BEFORE attempting Phases
      4–6.
- [ ] **AC-7**: The runbook gap-fold (F008-VAL-006) is either committed
      or explicitly scheduled, since Phase 6.4 (canonical-freeze) cannot
      be passed until the runbook executes verbatim.

## Out of scope

- Implementing the fixes themselves. This is a review/decision task; each
  fix becomes its own implementation task via `/task-work TASK-XXX` after
  the review's checkpoint decision.
- Re-running the validation runbook. That happens after the implementation
  tasks land.
- Step 7 (FinProxy first run). Blocked by Step 6 going canonical.

## Context from the validation session (2026-04-29)

These are the load-bearing diagnostic facts the reviewer should NOT have to
re-derive — they came out of running the runbook end-to-end.

### Environment notes (saves the reviewer ~30 min)

- `/etc/hosts` has `127.0.0.1 promaxgb10-41b1`, so on this workstation
  "Local" and "GB10" phases of the runbook are the **same machine**. The
  runbook's two-machine framing is misleading on this host.
- The forge editable install in user-site (`~/.local/lib/python3.12/...`)
  was originally pinned at a worktree path `.guardkit/worktrees/FEAT-FORGE-005`
  that no longer exists. Rewire with
  `pip install --user --break-system-packages --force-reinstall --no-deps -e .`
  from the live tree. (Don't use `--break-system-packages` casually
  elsewhere — but here we already have a user-site install.)
- `uv` is not installed on this host. The runbook's
  `pip install -e '.[providers,dev]'` cannot resolve `nats-core` from a
  fresh venv because `[tool.uv.sources]` is uv-only. The existing
  user-site `nats-core 0.2.0` editable install (from sibling
  `/home/richardwoollcott/Projects/appmilla_github/nats-core`) is the
  reason the rewire works without re-resolving deps.
- No JetStream-provisioned NATS is running. An ephemeral
  `docker run -d --network host nats:latest -js` (no persistent volume)
  was used for Phases 2–3 only. The container was named
  `forge-validation-nats` and stopped/removed at end of session.
- No forge production `Dockerfile` exists in the repo, so Phase 6.1
  (CMDW gate) is structurally unreachable today.

### Pytest failure modes (verbatim)

```
FAILED tests/forge/adapters/test_sqlite_persistence.py::test_apply_at_boot_is_idempotent
  AssertionError: second apply must not duplicate the seed row
  rows == [(1,), (2,)] but expected [(1,)]

FAILED tests/forge/test_approval_publisher.py::TestSingleOwnership
       ::test_no_other_module_in_forge_constructs_evidence_priors
  AssertionError: Other modules reconstruct details shape (AC-008):
    [PosixPath('src/forge/lifecycle/recovery.py')]

FAILED tests/integration/test_mode_a_concurrency_and_integrity.py
       ::TestMultiFeatureCatalogue
       ::test_three_features_produce_one_inner_loop_dispatch_each
FAILED tests/integration/test_mode_a_concurrency_and_integrity.py
       ::TestCorrelationThreading
       ::test_every_lifecycle_event_for_one_build_threads_one_correlation_id
  TypeError: Supervisor._dispatch: no routing for stage
            <StageClass.TASK_REVIEW: 'task-review'>
  src/forge/pipeline/supervisor.py:1555
```

```
ERROR tests/bdd/test_infrastructure_coordination.py
  ModuleNotFoundError: No module named 'forge.build'
  → forge.build.git_operations + forge.build.test_verification (TASK-IC-009/010)
    never landed.
```

### CLI surface gaps observed

- `forge queue` flags actually present: `--mode`, `--repo`, `--branch`,
  `--feature-yaml` (required), `--max-turns`, `--timeout`,
  `--correlation-id`. **No `--db-path`** — uses `$FORGE_DB_PATH` env or
  config.
- `forge history` flag is `--db` (required) — **NOT** `--db-path`.
- `forge status` flag is `--db-path` (matches runbook).
- `forge queue` requires `forge.yaml` (via top-level `--config FILE`
  flag or `./forge.yaml` in CWD). Minimum content:
  ```yaml
  permissions:
    filesystem:
      allowlist:
        - /absolute/path/to/an/allowed/checkout
  ```
- `forge queue` always tries to publish to NATS at `$FORGE_NATS_URL`
  (default `nats://127.0.0.1:4222`). There is **no fake-mode env switch**
  in `src/forge/cli/queue.py:_publish_once`, so the runbook's claim that
  Phase 2 stubs NATS via "the integration adapter" is false.

### What was healthy (worth not re-checking)

- Mode B integration: `tests/integration/test_mode_b_smoke_e2e.py` 19/19
  pass.
- Mode C integration: `tests/integration/test_mode_c_smoke_e2e.py` 17/17
  pass.
- Cross-mode concurrency: `test_cross_mode_concurrency.py` 19/19 pass.
- Mode B/C crash recovery: `test_mode_b_c_crash_recovery.py` 13/13 pass.
- BDD-008 bindings: 64/64 pass (runbook said 56 — count went up cleanly).
- Constitutional regression (executor + prompt-layer): 5/5 pass.
- Mode A integration *minus* the two TASK_REVIEW reds: 40/42 pass — so
  the regression is specifically and only the missing dispatch branch.
- NATS round-trip: `pipeline.build-queued.<feature_id>` envelope arrives
  with threaded `correlation_id` matching the CLI-emitted UUID.
- Mode B single-feature ASSUM-006 reject: fires with the documented
  error string.
- `forge history --mode {a,b,c}` filter: works correctly.

## Suggested deliverable shape

A single decision document (could live alongside the RESULTS file or as a
new `docs/reviews/REVIEW-F008-validation-triage.md`) that contains, in
order:

1. **Triage table** — F008-VAL-001..007 × {decision, owner, target task ID
   if implement-now, sequencing}.
2. **Mode C ASSUM-004 design decision** (the F008-VAL-002 nats-core
   regex question) — three options weighed, recommendation.
3. **Re-run plan** — exact phases of the runbook to re-execute and in
   what order, after each fix lands.
4. **Phase 6 prerequisite plan** — concrete owners/dates for NATS
   provisioning and the forge production Dockerfile.
5. **Runbook gap-fold patch outline** — bullet list of edits to apply to
   `RUNBOOK-FEAT-FORGE-008-validation.md` (the §F008-VAL-006 fixes
   above).

## Implementation Notes

This task is best executed via `/task-review TASK-REV-F008
--mode=architectural`, not `/task-work`, because the deliverable is a
decision document and a fan-out of new implementation tasks rather than
code changes.

After the review checkpoint:
- `[I]mplement` should fan out to one task per finding marked
  implement-now in the triage table. Recommended hash prefix for those:
  `F8`. Example: `TASK-F8-XXXX-fix-supervisor-dispatch-task-review`.
- `[A]ccept` is appropriate only if every F008-VAL-* item has a
  decision recorded *and* the runbook gap-fold (F008-VAL-006) is
  scheduled.

## Test Execution Log

[Populated by `/task-review` / `/task-complete`.]

## Review Outcome (2026-04-29)

`/task-review TASK-REV-F008 --mode=architectural` completed with decision
`[I]mplement`. See `docs/reviews/REVIEW-F008-validation-triage.md` for the
full triage report and SOLID assessment.

**Headline correction from RESULTS file**: F008-VAL-005 was re-classified
high → low. The migration code IS idempotent; the defect is a stale test
assertion in `tests/forge/adapters/test_sqlite_persistence.py:126` written
before schema v2 existed.

**Eight fan-out tasks** were created at
`tasks/backlog/feat-f8-validation-fixes/` across three waves with an AC-6
go/no-go gate after Wave 1. See
`tasks/backlog/feat-f8-validation-fixes/IMPLEMENTATION-GUIDE.md` for the
sequencing plan.

**Mode C wire-schema design (F008-VAL-002)**: Option A (sibling `task_id`
field on `BuildQueuedPayload`) chosen over Option B (rename `feature_id` →
`subject_id`) and Option C (widen regex). Cross-repo with `nats-core`
0.3.0; additive change preserves Mode A/B publishers unchanged.
