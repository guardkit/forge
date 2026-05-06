---
id: TASK-REV-F010M
title: "Plan: Wire the autobuild_runner sidecar lifecycle bridge into forge serve"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: review
tags:
  - feature-plan
  - decision-mode
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - first-real-run-followup
parent_task: TASK-FORGE-FRR-F010M
related_tasks:
  - TASK-FORGE-FRR-F010M  # parent scoping deliverable (Phase 2 output is this review's input)
  - TASK-FORGE-FRR-F010F  # sync-raise safety net (already merged; this plan must coexist with it)
  - TASK-FORGE-FRR-F010J  # sidecar URL threading prerequisite
  - TASK-FW10-009         # validation surface
  - TASK-FW10-010         # pause/resume design
  - TASK-FW10-011         # E2E capstone (must remain green or be amended)
context_files:
  - features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge_summary.md
  - features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge.feature
  - features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge_assumptions.yaml
  - docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md
  - tasks/in_progress/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010M-scope-autobuild-runner-pipeline-emitter-bridge.md
  - tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md
  - tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md
  - tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md
  - docs/design/contracts/API-nats-pipeline-events.md
  - docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md
  - docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md
test_results:
  status: pending
  coverage: null
  last_run: null
clarification:
  context_a:
    timestamp: 2026-05-06T00:00:00Z
    decisions:
      focus: all
      tradeoff: balanced
      verify_assum_003: true
      verify_assum_009: true
---

# Task: Plan — Wire the autobuild_runner sidecar lifecycle bridge into forge serve

## Summary

Decision-mode review for the F010M wave-plan. Closes the F010J → F010M wire
gap: every state transition the autobuild reaches inside the langgraph-runner
sidecar (success, async failure, pause, resume, cancel) must produce a
wire-visible `pipeline.*` envelope on JetStream so jarvis's chat REPL can
render between-prompt notifications.

The /feature-spec phase has already accepted 26 Gherkin scenarios across
groups A–D (plus the optional edge-case batch). The scoping doc recommends
**Option C — Streaming via `runs.join_stream` with `Last-Event-ID`** with
**Option E — Hybrid** as the named fallback. This review ratifies (or
revises) that recommendation and decomposes the implementation into a
wave-plan landing at `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/`
per F010M AC-6/AC-7.

## Pre-review verification (Q3a/Q3b = V)

The two low-confidence assumptions were verified against the live forge
codebase before this review begins:

### ASSUM-003 — Bridge reconnect-schedule bound (verified 2026-05-06)

Forge's existing reconnect convention is **exponential backoff with cap, no
fixed retry maximum**:

| Layer | Initial | Max | Algorithm | Citation |
|-------|---------|-----|-----------|----------|
| NATS daemon attach | 1.0s | 30.0s | Exponential ×2, reset on success | `src/forge/cli/_serve_daemon.py:90-93,447,468` |
| Fleet watcher | 1.0s | 1.0s | Fixed delay (deliberately not exponential) | `src/forge/adapters/nats/fleet_watcher.py:65,313` |
| Async polling | 5.0s constant | n/a | Constant interval (deliberately no backoff) | `src/forge/dispatch/async_polling.py:77,81` (docstring rationale) |
| Dispatch retry | n/a | n/a | None — policy lives in reasoning loop | `src/forge/dispatch/retry.py` |

**Implication for the SSE bridge**: match the `_serve_daemon.py` shape
(initial 1.0s, cap 30.0s, ×2, reset on success, no fixed max — terminate
only on `CancelledError` or higher-level deadline). Tests monkey-patch the
constants to `0.05s` (precedent: `tests/forge/test_cli_serve_daemon.py:364-367`).

ASSUM-003 is now **resolved**. `/feature-plan` should commit these numbers
into the wave-plan rather than re-debating them.

### ASSUM-009 — Cross-process correlation-id enforcement (verified 2026-05-06)

The F010C lint guard is a **per-process AST static-analysis test**, not a
runtime check or a contract:

- **Implementation**: `tests/forge/test_pipeline_consumer_correlation_id.py:338-393`
  (`test_every_safe_publish_failure_call_passes_correlation_id_kwarg`)
- **Rule**: walks AST of `src/forge/adapters/nats/pipeline_consumer.py`,
  asserts every `_safe_publish_failure(` call passes `correlation_id=` kwarg
  explicitly. Sanity-checks ≥4 call sites exist.
- **Scope**: **single-process only** — does not extend across the sidecar
  process boundary.

**Implication for option choice**:

| Option ratified | ASSUM-009 status | Required new work |
|---|---|---|
| **C (streaming, single-process)** | **MOOT** — the bridge runs in the forge daemon process and reuses `BuildContext.correlation_id` directly. Existing AST guard naturally extends to any new `_safe_publish_*` call sites the bridge introduces. | None beyond what the AST guard already enforces. |
| D / E (cross-process emit-back) | **LOAD-BEARING** — AST guards do not extend across process boundaries. New mechanism required: server-side validator on the in-receive endpoint that rejects emits missing `correlation_id`. | A whole new validation layer (per scoping doc line 797–799). |

This is a **strong argument for Option C** and a meaningful cost on D/E.
ASSUM-009 is now **resolved conditional on the option choice** — moot if C
is ratified, load-bearing if not.

## Review scope (Context A)

- **Focus**: all areas (architectural, technical, correctness, security)
- **Trade-off priority**: balanced — let the 26 Gherkin scenarios and the
  cross-cutting concern matrix drive the decision, not a pre-committed
  bias toward speed/quality/cost/maintainability.
- **Specific concerns**: ASSUM-003 and ASSUM-009 are verified above. Carry
  the verifications forward into the wave-plan as fixed inputs.

## Acceptance criteria for this review

- AC-1: Ratify (or revise) the scoping doc's Option C recommendation against
  the 26 BDD scenarios. Document the rationale for Option E as fallback if
  C is rejected.
- AC-2: Resolve all 8 option-discriminating assumptions (ASSUM-001 through
  ASSUM-008) into wave-plan-ready commitments. ASSUM-009 follows from the
  Option choice (see verification above).
- AC-3: Identify the wave-plan task list (each task carries
  `parent_task: TASK-FORGE-FRR-F010M` per F010M AC-7).
- AC-4: Identify which @smoke and @regression scenarios gate which wave.
- AC-5: Surface any cross-cutting concern from the scoping doc that the
  current scenario set does not lock down (e.g. observability, restart
  recovery, F010F coexistence, FW10-010 amendment, sidecar-aware E2E test).
- AC-6: Output the wave-plan to
  `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/` (per
  F010M AC-6) with each child task carrying `parent_task: TASK-FORGE-FRR-F010M`
  in its frontmatter (per F010M AC-7).
- AC-7: Generate the structured `.guardkit/features/FEAT-XXXX.yaml` so
  `/feature-build` can drive the wave-plan autonomously.
- AC-8: Generate the IMPLEMENTATION-GUIDE.md with mandatory Mermaid diagrams
  (data-flow always, integration-contract for complexity ≥5, task-dependency
  graph for ≥3 tasks). The data-flow diagram must show every write path and
  every read path; flag any disconnections.

## Decision options (presented at checkpoint)

- **[A]ccept** — approve the review findings without generating the
  wave-plan. Useful if the scoping doc's recommendation is wrong and the
  feature needs re-scoping before planning.
- **[R]evise** — re-run the review with deeper analysis on a specific
  cross-cutting concern (likely candidates: restart-recovery semantics,
  FW10-010 amendment shape, sidecar-aware E2E test scope).
- **[I]mplement** — generate the wave-plan: subfolder, subtasks,
  IMPLEMENTATION-GUIDE.md with mandatory diagrams, structured FEAT-XXXX.yaml.
  This is the expected path given /feature-spec has accepted all four
  groups + the optional batch.
- **[C]ancel** — discard the review (not expected — F010M is the parent
  scoping deliverable and this review is its Phase 3 output).

## Review notes

The /feature-spec phase has already done the heavy lifting on scenario
coverage. This review's job is **option ratification + wave decomposition**,
not re-deriving scenarios.

The 26 scenarios are partitioned across:
- Group A (envelope contract): 5 scenarios
- Group B (lifecycle coverage): 6 scenarios
- Group C (recovery + restart): 7 scenarios
- Group D (negative + edge): 7 scenarios
- Optional expansion: 6 edge-case scenarios

Wave decomposition should follow this partition: A → B → (C ∥ D) → E2E.
The smoke gates are the 2 @smoke scenarios that must remain green between
waves (per `/feature-build` R3 oracle convention, TASK-SMK-F703A).

## Test execution log

(populated by `/feature-build` once the wave-plan is generated)
