# Review Report: TASK-REV-83E4 — Plan: Mode P Planning Chain (FEAT-SPL-002)

**Mode**: decision · **Depth**: standard · **Date**: 2026-07-06
**Reviewers**: 3-agent adversarial panel (architecture-fit / red-team / plan-critique), each grounded in live forge source + the 2026-07-06 7-agent state verification
**Session**: autonomous Fable (ACTION 7); clarification defaults focus=all, tradeoff=quality
**Inputs**: `features/mode-p-planning-chain/` (29-scenario spec, 16 deferred assumptions), SPL scope, DF-009, state-verification digest

## Executive Summary

FEAT-SPL-002 is buildable offline by guardkit autobuild in a 5-build-wave plan, but
**only on the separate-planning-lifecycle route**. All three reviewers independently
refuted the BuildMode.MODE_P route on hard evidence and converged on a standalone
`src/forge/planning/` package with its own additive persistence. The red team found
one **critical** unstated seam (per-run, escalation-mutable expected approver — the
static `ApprovalConfig`-threaded plumbing would silently refuse James's approvals)
and one **critical** assumption inconsistency (ASSUM-001/002/016 are one architecture
decision, not three independent assumptions). Both are resolved by the recommended
architecture. Panel score: **82/100** — sound spec, two assumption defects fixed
pre-build (the FEAT-0760 precedent).

## Recommended Approach (unanimous)

**Standalone planning subsystem, additive-only** — zero edits to builds machinery,
Mode B logic, `ApprovalConfig`, or the guardkit seam:

1. **Persistence**: `src/forge/lifecycle/schema_v3.sql` — additive `CREATE TABLE
   planning_runs` (PK `correlation_id`; `originating_user`/`expected_approver` NOT
   NULL; state CHECK QUEUED/RUNNING/PAUSED/FAILED/CANCELLED/TIMED_OUT/PLANNED_HANDOFF;
   `pending_approval_request_id`, `defer_count`, `paused_at`/`escalated_at` wall-clock
   anchors, handoff fields) + `planning_run_events` history sibling (FK to
   planning_runs — `stage_log.build_id` has an **enforced FK to builds**
   (schema.sql:68, PRAGMA foreign_keys=ON), so ASSUM-016's "same store" is
   unimplementable as written). CAS transitions (`UPDATE … WHERE state=?`) are the
   race arbitration primitive.
2. **Intake**: second durable (`forge-serve-planning`) on the PIPELINE workqueue
   stream, filter `pipeline.planning-queued.*` — non-overlap with build-queued.*
   confirmed against stream-definitions.json; **ack-on-persist** (ASSUM-015 upheld
   strongly: escalation windows exceed the 1h ack_wait; held-slot would redeliver
   into the FWD-003 wedge class). `INSERT OR IGNORE` on the PK = dedup. Malformed →
   ack+log. **correlation_id is validated at the trust boundary** (charset/length)
   before use as key/path/branch/subject (RT-03: frozen payload applies NO
   validation; '../../..' traverses, '~^:?*[' break git refs).
3. **Runner**: pure-function planner over `planning_run_events` history
   (`src/forge/planning/planner.py`, mode_b_planner shape); PLANNING_PERMITTED /
   FORBIDDEN stage sets enforced in the planning package — `mode_chains_data.py`
   stays **byte-identical**.
4. **PO dispatch**: first production composition of DispatchOrchestrator +
   NatsSpecialistDispatchAdapter (+ FleetWatcher/DiscoveryCache/CorrelationRegistry/
   TimeoutCoordinator/SqliteHistoryWriter) in serve, wrapped behind ONE injectable
   callable seam so all tests use fakes. Outcome mapping: Degraded→FLAG_FOR_REVIEW,
   exception→run FAILED, AsyncPending→ERROR.
5. **Checkpoint**: planning-backed `GateRepository`/`StateMachine` protocol adapters
   + `checkpoint.py` reusing `derive_request_id` and `_atomic_pause_and_publish`
   (exported via `__all__`, no behaviour change) + **per-run ApprovalSubscriber
   pinned to the row's `expected_approver`** (rearm precedent). Planning-scoped
   dispatch tail: approve→resume, reject→CANCELLED, defer→cap-3→escalate,
   first-timeout→durable re-target to escalation approver→own ceiling→TIMED_OUT.
   `wrappers.await_and_dispatch` (build policy; JNB-107 dependency) untouched.
   Never auto-approve (DF-009).
6. **Terminal**: registry-indirected (`terminal_registry.py`, StepTypeRegistry
   precedent); PLANNED-HANDOFF handler commits `feature_spec_inputs/<cid>.md` on
   branch `planning/<cid>` via injected GitRunner over adapters/git/operations
   worktree ops (no push, v1); **idempotent re-execution** (RT-08); notification =
   NotificationPayload on `jarvis.notification.slack` (exists in frozen 0.5.0, live
   jarvis subscriber) — **mint NO new pipeline.* subjects** (workqueue accrual).
   Notification built only from validated components — raw request_text never
   interpolated (RT-09).
7. **Config + DF-004**: new `PlanningConfig` sibling section (extra="forbid";
   enabled=False, escalation_approver, thresholds, defer_cap=3,
   default_target_repo, target_repo_paths, model_resolution{fallbacks:[]},
   frontier{enabled=False}); ApprovalConfig untouched. Boot audit is a **pure
   function** — on violation: loud ERROR, planning durable not attached, build
   intake boots normally (must be an audit, not a Pydantic validator).
8. **Frontier (DF-006)**: SecondOpinionProvider protocol; FLAG-only, compressed
   **policy-filtered** JSON brief (field allowlist — "policy-filtered" restored
   from DF-009 §2.3 verbatim, RT-09), degrade-to-human, provider returns data and
   structurally cannot approve.

## Key Findings (18 total; full detail in the panel transcript)

| ID | Sev | Finding |
|----|-----|---------|
| RT-01 | critical | Per-run escalation-mutable expected approver contradicts static gate plumbing — resolved by per-run subscriber + durable expected_approver column |
| RT-02 | critical | ASSUM-001/002/016 are one architecture decision with a schema migration either way — collapsed into the recommended route |
| ARCH-001 | high | builds-row reuse refuted (NOT NULL feature columns, derive_build_id, CHECK rebuild, missing terminal) |
| ARCH-002/PS-001 | high | stage_log FK to builds blocks ASSUM-016 → planning_run_events sibling table |
| ARCH-003 | high | gate_check tail hardcodes build policy (timeout→CANCEL, unbounded defer) → planning-scoped tail |
| ARCH-004/PS-002 | high | DispatchOrchestrator has NO production composition — first-class serve task required or Mode P is dead code |
| RT-03 | high | correlation_id unvalidated at trust boundary (path/ref/subject/PK injection) → intake sanitisation + new negative scenario |
| RT-04 | high | escalation/defer state must be durable wall-clock anchored or restarts reset thresholds (escalation-DoS) |
| RT-05 | high | ack-on-persist needs a boot sweep for non-paused runs or crashed pre-dispatch runs are orphaned forever |
| RT-06 | high | planning must have a SEPARATE handler/consumer — reusing dispatch_build inherits maybe_gate_build, held-slot, terminal-only dedup |
| RT-07 | medium | 3 scenarios re-anchored: coexistence (config assertion + operator AC), thresholds (injected clock), race (CAS arbitration) |
| RT-08 | medium | handoff idempotency scenario added (crash between commit and record) |
| RT-09 | medium | "policy-filtered" restored to frontier brief; notification sanitisation AC added |
| RT-10 | medium | terminal-run retry: duplicate of a terminal run acks WITH a notification back to the originator (no silent drop) |
| ARCH-006 | medium | expected_approver durable per-run; update-before-publish on escalation; single-coroutine ownership resolves the race |
| ARCH-007 | medium | wire surface: AGENTS request + jarvis.notification.slack; run ids namespaced `plan-{cid}`; jarvis rendering is SPL-001/003 territory (cross-repo dependency, not forge scope) |
| PS-005 | medium | CAS primitive lives in the store task so the escalation task consumes it |
| PS-007/8 | low | smoke-gate temporal sequencing; GitRunner protocol for offline handoff tests |

## Assumption Verdicts (panel consensus; human_response stays deferred for Rich)

UPHOLD: 001, 003, 005, 007, 008, 010, 011, 013, 015 (strongly)
AMEND: **002** (protocol-level composition + per-request approver seam), **004**
(durable wall-clock anchors; restart re-arms to current escalation target; single
hop v1), **006** (worktree commit via GitRunner + correlation_id sanitisation +
target_repo_paths), **009** (enforcement locus = planning package;
mode_chains_data byte-identical), **012** (+policy-filtered), **014** (+boot
sweep twin; terminal-retry notification), **016** (sibling planning_run_events
table)

## Spec Amendments Applied (2026-07-06, this session)

Four scenarios added (29→33): invalid-correlation-id rejection (RT-03),
restart-after-escalation re-arm (RT-04), boot recovery of non-paused runs (RT-05),
idempotent handoff re-execution (RT-08). Assumptions manifest annotated with panel
amendments; all human_response values remain `deferred`.

## Task Breakdown

11 tasks (MP-004 pre-split per PS-004), 6 waves, ~740 min; TASK-MP-010 is
`operator_handoff` (live GB10/jarvis/kill-NATS validation — autobuild skips; gated
on TASK-FWD-004 completion per RT-12). Full breakdown, coverage matrix (33/33
scenarios), and constraint-compliance map in
`tasks/backlog/mode-p-planning-chain/IMPLEMENTATION-GUIDE.md`.

## Decision

**[I]mplement** — autonomous session, defaults (panel-recommended approach,
auto-detected waves, standard testing). Context B recorded in the task file.
