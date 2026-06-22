---
id: TASK-REV-RBX-001
title: "Plan: Runbook Executor"
status: completed
created: 2026-06-21T18:45:00Z
updated: 2026-06-21T18:45:00Z
priority: high
task_type: review
parent_feature: FEAT-RBX
feature_slug: runbook-executor
clarification:
  context_a:
    decisions:
      focus: all
      tradeoff: quality
  context_b:
    decisions:
      approach: registry-dispatch-mirror-pipeline-publisher
      execution: parallel
      testing: tdd
tags:
  - forge
  - runbook
  - executor
  - review
---

# Plan: Runbook Executor (FEAT-RBX)

**Source spec:** `features/runbook-executor/runbook-executor_summary.md`
(+ `runbook-executor.feature`, 28 scenarios; `runbook-executor_assumptions.yaml`,
9 assumptions, all human-confirmed).
**Depends on:** **Runbook and Step Persistence** (`FEAT-RSP`) — the executor
composes the repository surface `create_runbook` / `load_runbook` /
`update_step_status` / `advance` and the `Runbook` / `Step` / `StepStatus`
models. FEAT-RSP must be **built first**.

---

## Decision summary

| # | Decision | Resolution |
|---|----------|-----------|
| Approach | dispatch-by-step-type loop | **Option 1 — registry indirection + reuse `pipeline_publisher`** (only sound option; spec-prescribed). |
| ASSUM-004 | pointer-vs-persistence conflict | **R1 — relax persistence**: `advance()` may rest at `current_step_index == step_count`; that terminal pointer is the single completion marker. |
| Q2 | where lifecycle events live | **Extend `nats-core`**: 5 new `EventType` members + payloads + registry entries, mirroring the pipeline events. |

### Why R1 (relax persistence) is the best long-term solution

The two confirmed ASSUM-004s contradict each other (the spec flags this for
human reconciliation). Two resolutions were considered:

- **R1 — pointer rests at `== count`** (chosen). `advance()` is allowed to
  reach the terminal position one past the last step. Because the executor
  **stops on failure**, the pointer reaches `count` *only* when every step
  has passed. Runbook state is therefore fully and unambiguously determined
  by `(current_step_index, per-step statuses)`:
  - `current_step_index == count` → **complete** (all passed).
  - `current_step_index < count`, step there `failed` → **failed/escalated**.
  - `current_step_index < count`, step there `awaiting_approval` → **paused/escalated**.
  - `current_step_index < count`, step there `pending` → **in progress**.
  No redundant "overall status" write is needed; FEAT-RSP's ASSUM-009
  (overall status not mutated by `update`/`advance`) stays intact.

- **R2 — pointer rests on the final step, completion derived.** Rejected:
  the pointer alone is **ambiguous** at the last index (cannot tell "final
  step pending" from "final step done") — you must read the step's status to
  disambiguate. That is a dual-source-of-truth resume hazard, exactly the
  class of bug that bites months later.

**Cost of R1:** one narrow amendment to the *not-yet-built* FEAT-RSP plan —
`advance()` accepts the terminal `== count` position (refuses only `> count`).
Reconciling now, while both features are unbuilt, is the cheapest possible
time. See the cross-feature note in `IMPLEMENTATION-GUIDE.md`.

### Why extend nats-core (Q2)

`MessageEnvelope.event_type` is a **closed `EventType` enum** in `nats-core`,
and `payload_class_for_event_type()` raises on any unregistered type. Emitting
runbook lifecycle events through the same envelope/subscriber machinery as the
pipeline events therefore requires adding the members + payloads + registry
entries to `nats-core`. A forge-local enum would bypass the shared registry and
external subscribers could not resolve runbook payloads — a worse long-term
position.

---

## Technical options analysis

### Option 1 — Registry-dispatch executor + `pipeline_publisher` reuse (RECOMMENDED)

- **Complexity:** Medium (6/10 aggregate). **Effort:** ~8 hours across 7 tasks.
- A `RunbookExecutor` loads a persisted runbook, iterates from
  `current_step_index`, resolves a handler from a `StepTypeRegistry`
  (`step_type -> handler`), runs it, maps the outcome to a `StepStatus`,
  persists result-then-pointer, and publishes lifecycle events via a new
  `RunbookPublisher` that mirrors `PipelinePublisher` (fire-and-forget;
  `PublishFailure` logged, never rolled back — LES1 parity / ASSUM-009-exec).
- **Pros:** open-closed via the registry (a new step type needs only a
  handler registered); zero knowledge of step internals; reuses a proven
  publish path; SQLite remains the source of truth for resume.
- **Cons:** requires the FEAT-RSP terminal-pointer amendment (R1) and a
  cross-package `nats-core` change (Q2).

### Option 2 — Hard-coded `if step_type == ...` dispatch

- Rejected. Violates the spec's core constraint ("executor has NO knowledge of
  step internals — registry indirection only") and is not open-closed.

### Option 3 — Executor owns its own SQLite writes (bypass the repository)

- Rejected. Duplicates the `BEGIN IMMEDIATE` discipline FEAT-RSP already owns,
  reintroduces the concurrency hazard the repository serialises, and splits the
  source of truth.

---

## Risk analysis

| Risk | Mitigation |
|------|-----------|
| ASSUM-004 contradiction silently shipped | Reconcile FEAT-RSP **now** (R1); §4 `terminal_pointer` contract + seam test in TASK-RBX-004. |
| Enum/payload/registry drift in nats-core | Declarative TASK-RBX-002 + seam test in TASK-RBX-003 asserts each member resolves via `payload_class_for_event_type`. |
| Crash between result-commit and pointer-advance skips a step | Executor writes **result before advancing**; on resume, a step already `passed` at the pointer is advanced without re-running (idempotent recovery). Data-integrity scenarios in TASK-RBX-006. |
| Publish failure rolls back progress | Reuse `PipelinePublisher` semantics verbatim — catch+log `PublishFailure`, never roll back (ASSUM-009-exec, confidence=high). |
| Adversarial `step_type` executed as code | `step_type` is only ever a dict key into the registry; no-handler ⇒ escalate. Security scenario outline in TASK-RBX-006. |
| Two executors double-run a step | `advance()` / `update_step_status` use `BEGIN IMMEDIATE` (FEAT-RSP); committed progress serialises. Concurrency scenario in TASK-RBX-007. |

---

## Task breakdown (7 tasks, 5 waves)

| Task | Title | type | cx | wave | deps |
|------|-------|------|----|------|------|
| TASK-RBX-001 | Step-type registry + handler protocol | feature | 4 | 1 | — |
| TASK-RBX-002 | Runbook lifecycle events + payloads (nats-core) | declarative | 4 | 1 | — |
| TASK-RBX-003 | `RunbookPublisher` (mirror pipeline_publisher) | feature | 5 | 2 | 002 |
| TASK-RBX-004 | Executor dispatch loop (core) | feature | 7 | 3 | 001, 003 |
| TASK-RBX-005 | CLI `forge runbook run <path>` | feature | 5 | 4 | 004 |
| TASK-RBX-006 | Security & data-integrity scenario tests | testing | 5 | 5 | 004, 005 |
| TASK-RBX-007 | Concurrency & real-broker integration tests | testing | 4 | 5 | 004, 005 |

Decision: **[I]mplement** — feature structure generated. See
`IMPLEMENTATION-GUIDE.md` for the mandatory data-flow / integration-contract /
dependency diagrams and the §4 Integration Contracts.
