---
id: TASK-REV-3EEE
title: "Plan: Pipeline State Machine and Configuration"
task_type: review
status: review_complete
priority: high
created: 2026-04-27T00:00:00Z
updated: 2026-04-27T00:00:00Z
complexity: 8
tags: [planning, review, lifecycle, state-machine, sqlite, cli, crash-recovery, feat-forge-001]
feature_spec: features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md
feature_id: FEAT-FORGE-001
upstream_dependencies: []
clarification:
  context_a:
    timestamp: 2026-04-27T00:00:00Z
    decisions:
      focus: all
      tradeoff: quality
      specific_concerns:
        - state_mutation_exclusivity
        - write_then_publish_failure_path
        - identifier_traversal_decoded_variants
        - paused_recovery_request_id_idempotency
review_results:
  mode: decision
  depth: standard
  recommended_option: "Option 1 — Dedicated forge.lifecycle + forge.cli packages"
  estimated_hours: "55-60"
  subtask_count: 13
  wave_count: 5
  aggregate_complexity: 8
  findings_count: 12
  risks_count: 10
  integration_contracts_count: 6
  report_path: .claude/reviews/TASK-REV-3EEE-review-report.md
  completed_at: 2026-04-27T00:00:00Z
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Plan Pipeline State Machine and Configuration (FEAT-FORGE-001)

## Description

Decision-making review for **FEAT-FORGE-001 — Pipeline State Machine and
Configuration**. The feature specifies the durable build lifecycle from queue
to terminal: state-machine transitions, SQLite-backed history (`builds` +
`stage_log`), crash-recovery reconciliation, sequential-queue discipline,
configuration loading from `forge.yaml`, and the CLI surface (`forge queue`,
`forge status`, `forge history`, `forge cancel`, `forge skip`).

Behaviour is described in domain terms; underlying mechanisms (SQLite WAL +
STRICT, NATS pipeline subjects, Pydantic validation) appear only as capability
observations. The 34 BDD scenarios cover all eight scenario groups: key
examples, boundary conditions, negative cases, edge cases (including crash
recovery across every non-terminal state), security (path-traversal rejection,
operator audit), concurrency, data integrity, and integration boundaries.

The review must surface the recommended technical approach, architecture
boundary against the **upstream-of-CLI** features 002–007 already in flight,
risk analysis (especially around state-mutation exclusivity, write-then-publish
ordering, identifier validation depth, and PAUSED-recovery idempotency), effort
estimation, and a subtask breakdown that downstream `/feature-build` can
execute against.

## Scope of Analysis

Review must cover **all areas (full sweep)** with a **quality** trade-off
priority — this is the durable lifecycle backbone; bugs in the state machine
or recovery layer corrupt build history permanently. Specific concerns to
receive extra scrutiny:

1. **State-mutation exclusivity** — `lifecycle/state_machine.py` must be the
   SOLE caller of any persistence write that mutates `builds.state`. CLI
   commands (`queue`, `cancel`, `skip`) and recovery code must go through the
   state machine; no module may write `status` directly.
2. **Write-then-publish failure path** — on a NATS publish failure after a
   successful SQLite write, the build row must remain visible as `QUEUED` so
   the operator can reconcile or re-queue. The CLI must report the failure
   distinctly from a SQLite failure and exit non-zero.
3. **Identifier validation depth** — `lifecycle/identifiers.py` must reject
   not just literal `../` but also URL-encoded variants (`%2F`, `%2E%2E`),
   null bytes, and any character that would escape the worktree root when
   interpolated into `build-{feature_id}-{ts}` or `/var/forge/builds/{build_id}/`.
4. **PAUSED crash-recovery idempotency** — `lifecycle/recovery.py` must
   re-issue the original approval request (preserving `request_id`), not
   generate a fresh one; otherwise the responder's approval cannot be
   correlated with the in-flight pause.

Concrete areas to examine across the 34 BDD scenarios:

- **Group A — Key Examples (6 scenarios)**: queue write path, full happy-path
  lifecycle, `forge status` non-blocking read, `forge history` per-feature
  listing, configuration default + CLI override merge, WAL responsiveness
  during active writes.
- **Group B — Boundary Conditions (6 scenarios)**: turn-budget acceptance ≥ 1
  / rejection ≤ 0, history limit clamp, default limit = 50, duplicate
  `(feature_id, correlation_id)` UNIQUE constraint, full-status stage cap.
- **Group C — Negative Cases (7 scenarios)**: path allowlist refusal, active
  in-flight duplicate refusal, skip-on-non-paused refusal, cancel-of-unknown
  refusal, validation-failure transition `preparing→failed`, hard-stop
  transition `running→failed`, invalid lifecycle jump rejection.
- **Group D — Edge Cases (9 scenarios)**: crash recovery across `preparing`,
  `running`, `finalising`, `paused`, and all four terminal states; cancel of
  paused (synthetic reject); skip of flagged stage; sequential per-project
  queue; watch-mode refresh.
- **Group E — Security (2 scenarios)**: path-traversal rejection,
  cancelling-operator distinct from originating-operator audit.
- **Group F — Concurrency (2 scenarios)**: simultaneous queues both durable
  and ordered, concurrent reader sees consistent snapshot.
- **Group G — Data Integrity (2 scenarios)**: terminal-state ⇒
  `completed_at` invariant, write-succeeded-but-publish-failed visibility.
- **Group H — Integration Boundaries (1 scenario)**: clean failure when NATS
  unreachable; status / history continue to work without it.

## Reuse vs. Build Boundary

The gap-context document (`docs/research/ideas/forge-001-gap-context.md`)
explicitly classifies what already exists in 002–007 territory and must NOT
be duplicated. The plan must produce **only** the gap-closure tasks:

**Reuse (do not modify):**
- `src/forge/config/models.py` — `ForgeConfig` Pydantic models exist; only
  add `load_config()` and a `QueueConfig` sub-model in place.
- `src/forge/pipeline/cli_steering.py` — cancel/skip executor logic, all
  Protocol seams (`BuildSnapshotReader`, `BuildCanceller`, `BuildResumer`,
  `StageLogReader`, `StageSkipRecorder`, `PauseRejectResolver`,
  `AsyncTaskCanceller`, `AsyncTaskUpdater`).
- `src/forge/pipeline/supervisor.py` — `BuildState` enum and per-turn
  reasoning loop.
- `src/forge/adapters/nats/{pipeline_consumer,pipeline_publisher,synthetic_response_injector,approval_publisher,approval_subscriber}.py`
  — all eight lifecycle subjects + synthetic-reject injection + approval
  protocol already shipped.

**Build (FEAT-FORGE-001 net-new surface):**
- `src/forge/lifecycle/` — `schema.sql`, `migrations.py`, `persistence.py`
  (concrete SQLite-backed implementations of the cli_steering Protocols),
  `state_machine.py` (transition table + invalid-transition rejection),
  `queue.py` (per-project sequential picker), `recovery.py` (startup
  reconciliation), `identifiers.py` (path-traversal validation).
- `src/forge/cli/` — `main.py` (Click entry point), `queue.py`, `status.py`
  (incl. `--watch`, `--full`), `history.py`, `cancel.py`, `skip.py`.
- `pyproject.toml` — `[project.scripts]` `forge = "forge.cli.main:main"`.

## Acceptance Criteria

- [ ] Technical options analysed with pros/cons and a recommended approach
- [ ] Architecture boundary between `lifecycle/`, `cli/`, and existing
      `pipeline/`, `adapters/`, `config/` modules documented as Protocol seams
- [ ] State-mutation exclusivity rule formalised (state_machine sole writer)
- [ ] Write-then-publish failure-mode contract specified at CLI boundary
- [ ] Identifier validation rules documented (decoded traversal + null bytes)
- [ ] PAUSED-recovery idempotency contract specified (`request_id` preserved)
- [ ] DDR-003 WAL + STRICT pragmas applied on every connection open
- [ ] Effort estimated with complexity score (1–10) per proposed subtask
- [ ] Risk register produced covering corruption, redelivery races, and
      cancel/skip handler regressions
- [ ] Subtask breakdown with dependencies and parallel-wave organisation
- [ ] Integration contracts identified (Protocol-seam handshakes between
      lifecycle/persistence and pipeline/cli_steering)
- [ ] BDD scenario coverage assessed against all 34 scenarios across 8 groups
- [ ] Decision checkpoint presented: [A]ccept / [R]evise / [I]mplement / [C]ancel
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Clarification Context

**Context A — Review Scope** (captured 2026-04-27):

- Review focus: **All areas (full sweep)**
- Trade-off priority: **Quality** (durable lifecycle backbone — corruption risk)
- Specific concerns:
  - State-mutation exclusivity (state_machine sole writer of `builds.state`)
  - Write-then-publish failure path (SQLite row survives NATS timeout)
  - Identifier validation depth (URL-encoded `%2F`/`%2E%2E` + null bytes)
  - PAUSED-recovery idempotency (preserve original `request_id`)

## Context Files

- `features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md` — feature spec summary
- `features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature` — 34 BDD scenarios
- `features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_assumptions.yaml` — 5 confirmed assumptions
- `docs/research/ideas/forge-001-gap-context.md` — explicit reuse-vs-build gap analysis
- `docs/design/contracts/API-cli.md` — Click CLI contract
- `docs/design/contracts/API-sqlite-schema.md` — SQLite DDL + read/write API
- `docs/design/decisions/DDR-003-sqlite-schema-layout-wal.md` — WAL + STRICT decision

## Upstream Dependencies

None — FEAT-FORGE-001 is the foundation. Siblings FEAT-FORGE-002 through
FEAT-FORGE-007 ship the *upstream-of-CLI* surface (NATS adapters, config
models, stage-ordering guards, executor-layer cancel/skip handler) but
deliberately do not depend on this feature; they were absorbed silently into
their own backlogs while FEAT-FORGE-001 remained unbuilt.

This review must therefore consume the existing 002–007 surface as fixed
contract and only design the gap-closure modules under
`src/forge/lifecycle/` and `src/forge/cli/`.

## Next Steps

```bash
/task-review TASK-REV-3EEE --mode=decision --depth=standard
```
