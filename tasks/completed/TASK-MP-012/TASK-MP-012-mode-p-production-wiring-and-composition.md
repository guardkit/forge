---
id: TASK-MP-012
title: "Mode P production wiring + composition — make the merged planning library actually run (post-merge review follow-up)"
status: completed
created: 2026-07-06T16:15:00Z
updated: 2026-07-07T09:15:00Z
completed: 2026-07-07T09:15:00Z
completed_location: tasks/completed/TASK-MP-012/
previous_state: in_review
state_transition_reason: "Rollup 2026-07-07 (Rich's instruction, ops session): JNB-107 live validation complete 2026-07-07 (all four scenarios, Gate G1 PASS) validated the gate/approval chain; Mode P assumptions all 16 accepted by Rich 2026-07-07 (1909a40); MP-010 remains the live planning validation."
design:
  status: approved
  approved_at: "2026-07-06T17:45:00Z"
  approved_by: "auto (autonomous session; architectural-reviewer 72/100 APPROVED WITH RECOMMENDATIONS, 3 recommendations adopted)"
  architectural_review_score: 72
  complexity_score: 7
  implementation_plan: docs/state/TASK-MP-012/implementation_plan.md
  design_notes: >
    Arch-review recommendations adopted: (1) reuse ApprovalSubscriber per-run
    instead of a bespoke waiter; (2) fix the ReplyChannel seam at the root
    (CorrelationRegistry.matched_agent_for + adapter conformance methods)
    instead of a wildcard bridge; (3) reuse adapters/git/operations primitives
    in WorktreeGitRunner instead of a second subprocess surface.
priority: high
task_type: implementation
feature_id: FEAT-SPL-002
repo: forge
implementation_mode: task-work
complexity: 7
dependencies: []
blocks: [TASK-MP-010]
tags: [mode-p, spl-002, wiring, found-2026-07-06, post-merge-review]
---

# Task: Mode P production wiring + composition

## Why

The 2026-07-06 post-merge review of FEAT-3ED2 + FEAT-DD4F
(`docs/reviews/feat-spl-002-post-merge-review-2026-07-06.md` — read it FIRST;
16 confirmed critical/high findings, 0/32 adversarial refutation votes
succeeded) found the merged Mode P is a sound component library with **no
working production path**: with `planning.enabled=true` the daemon boots,
soft-fails planning composition, and continues without Mode P. TASK-MP-010
(live GB10 validation) would fail at step 1 — it is blocked on this task.

No production urgency: `planning.enabled` defaults False, forge-prod is not
redeployed on this code, jarvis intake is no-op'd until its config keys are set.

## Acceptance Criteria

**A. Boot composition (the CRITICAL)**
- [x] Fix `serve.py:445-458`: the calls to `compose_planning_consumer_and_dispatch`,
      `sweep_interrupted_planning_runs`, `rearm_paused_planning_runs` use keyword
      names that do not exist (`client=`, `planning_config=`, `sqlite_pool=` vs the
      real `db_path`, `nats_client`, `config`). All three currently raise TypeError,
      swallowed by the DDR-007 except.
- [x] Replace the permissive `*args/**kwargs` fakes in
      `tests/cli/test_serve_planning_wiring.py` with signature-binding pins
      (`inspect.signature(...).bind(...)` or real-callable monkeypatches) so a
      kwargs drift fails CI. This is the PS-002 class, recursively: the pin test
      codified the wrong contract.

**B. Put the consumer on the wire**
- [x] Actually create/bind the durable `forge-serve-planning` pull consumer on
      PIPELINE filtering `pipeline.planning-queued.*` and subscribe
      `handle_planning_message` (today the durable is declared, never bound).
      Preserve the reviewed non-overlap vs `forge-serve` (`pipeline.build-queued.*`)
      and jarvis's lifecycle filters — workqueue stream, err-10100 class.

**C. Real composition below the boot shim**
- [x] PO stage dispatch: replace the logging stub with real dispatch via the
      existing specialist dispatcher (Session-3 requirement 2).
- [x] Approval-decision consumption: wire the checkpoint to the production
      approval machinery so a decision actually resumes/cancels a planning run.
- [x] Approval envelope: publish valid `ApprovalRequestPayload` via the
      production approval publisher — the current checkpoint/escalation envelopes
      fail jarvis JNB-103 payload validation (WARN-drop).
- [x] `rearm_paused_planning_runs`: implement (log-only stub today) — re-emit the
      pending approval request and re-arm the wait after restart (ASSUM-015's
      compensating half; restart-while-paused currently orphans the run).
- [x] Escalation/defer driver: schedule threshold evaluation (nothing evaluates
      `originator_wait`/`escalated_wait` today) and wire the checkpoint defer
      branch to the escalation policy (below-cap defer is a dead end;
      `escalation.py:268` TODO).
- [x] Handoff terminal: provide the production GitRunner binding, publish the
      handoff notification, and write the PLANNED_HANDOFF row + branch/path
      columns (all currently unreachable/absent in production).

**D. State-machine fixes surfaced by review**
- [x] Boot sweep: QUEUED→FAILED is not a legal transition and the refusal
      sentinel is ignored — QUEUED runs stick forever. Either legalise the sweep
      transition or route via a legal path; add the RT-08 nuance (do not
      terminally FAIL a RUNNING run crashed between branch commit and record
      update without an event trail).
- [x] Intake ack-on-store-failure: a transient SQLite error currently acks and
      permanently drops the request — nak/term decision needed (at-least-once).
- [x] RT-03 correlation_id pattern permits dots, which fragment the approval
      subject past jarvis's 4-token gate (silent drop) — restrict the pattern.

**E. Tracker + docs cleanup**
- [x] Reconcile the TASK-MP-* tracker state (no MP task file is in
      tasks/in_review/ despite commit/doc claims; six tasks' surviving files say
      design_approved; five exist twice with conflicting statuses; FEAT-3ED2.yaml
      + FEAT-DD4F.yaml carry six stale file_path pointers).
- [x] Update MP-010's gate text: TASK-FWD-004's unit-disable half was DONE
      2026-07-06 (`forge-autobuild-runner` disabled on the GB10); its
      attended-override revert half remains.

## Design questions to surface to Rich (do not decide silently)

1. **Approver identity across repos:** planning pins per-run
   `expected_approver = originating_user`, but jarvis's reply path publishes a
   single static `decided_by` (`JARVIS_SLACK_DECIDED_BY=rich`). With James as
   originator, every phone approval would be refused. Properly FEAT-SPL-003
   scope — needs a decision before any non-Rich originator.
2. ASSUM-004 wait thresholds (300s/1800s) were invented by the build and
   contradict ASSUM-015's own ">1h" rationale — confirm or amend values.
3. Whether FEAT-SPL-004 is closed by escalation.py (build plan says assess) —
   the contract exists but has no production driver until item C lands.

## Constraints

- The guardkit seam (`src/forge/adapters/guardkit/run.py`) and
  `src/forge/pipeline/` stay untouched — both verified zero-diff in review;
  keep it that way.
- `planning.enabled` stays default-False until MP-010 passes.
- Full-suite green vs the pre-existing-failure baseline; the review's
  checks_passed inventory is the do-not-regress list.

## Implementation record (2026-07-06, task-work autonomous session)

**All acceptance criteria implemented, tested, and multi-agent reviewed.**

- **A. Boot composition**: serve.py calls the real signatures with `db_path` +
  `nats_url` threaded from `ServeConfig` via `bind_production_dispatch_chain`;
  pin tests replaced with `SignatureBindingFake` (`inspect.signature(...).bind`
  inside `__call__` — call-site drift now fails CI).
- **B. Consumer on the wire**: durable `forge-serve-planning` pull consumer
  bound on PIPELINE, filter `pipeline.planning-queued.*`, `ack_wait=3600`
  (D659 lesson), `max_ack_pending=1`, fetch loop as supervised task; loud
  ERROR (never silent non-durable) when the client has no JetStream context.
- **C. Real composition**: first production composition of
  DispatchOrchestrator + NatsSpecialistDispatchAdapter (reply-channel seam
  fixed at the root: `CorrelationRegistry.matched_agent_for` + adapter
  `subscribe`/`unsubscribe` conformance; dedicated nats_core fleet client
  feeds DiscoveryCache); re-entrant `PlanningRunDriver`
  (`src/forge/planning/driver.py`) drives QUEUED→…→PLANNED_HANDOFF from
  durable history via `plan_next_step`; wire-valid `ApprovalRequestPayload`
  envelopes (single builder shared by checkpoint/escalation/defer/rearm) +
  `pipeline.build-paused.FEAT-PLANNING` mirror (jarvis JNB-103 join);
  per-run `ApprovalSubscriber` (RT-04 pinning) with arm-before-post
  re-emits; rearm implemented (verbatim persisted request_id, exactly once);
  two-phase escalation as a structured wait over durable anchors; defer
  wired end-to-end (persist round → armed re-emit); production
  `WorktreeGitRunner` (isolated worktree, RT-08 idempotent) + PLANNED_HANDOFF
  row writes + `jarvis.notification.slack` frozen-0.5.0 publisher.
- **D. State-machine fixes**: sweep re-drives QUEUED/RUNNING through the
  driver (non-destructive when composition failed — one bad boot no longer
  destroys pending runs), sentinels checked, `sqlite3.Row.get` bugs fixed;
  intake naks (or leaves unacked) on store failure; `CORRELATION_ID_PATTERN`
  excludes dots.
- **E. Tracker/docs**: 11 built MP task files → `tasks/in_review/`
  (duplicates deleted, one MP-010 in backlog), 12 feature-YAML `file_path`
  pointers fixed, MP-010 gate annotated (MP-012 + FWD-004 partial-completion
  incl. the still-gating JARVIS_NATS_PASSWORD rotation), FWD-004 dated tick,
  D659 deploy-verification addendum.

**Verification**: full suite 5289+ passed with only the pre-existing 8+2
infra baseline; 426 planning-related tests green incl. new driver /
git-runner / durable-bind / mirror / defer suites. Architectural review
72/100 (3 recommendations adopted); 4-dimension multi-agent review — 2 HIGH
+ 3 MED + 6 LOW findings fixed, all pinned by regression tests
(`docs/state/TASK-MP-012/review-findings-resolution.md`).

**Design questions — DECIDED by Rich 2026-07-06 (follow-up session):**
1. Approver identity → **truthful member IDs**: jarvis sends the actual
   clicker's Slack member ID as decided_by; forge build-gate
   expected_approver config aligned to Rich's member ID. Filed as jarvis
   TASK-JNB-110 (gates the JNB-107/MP-010 live round-trips).
2. Wait thresholds → **1h / 4h** (originator_wait=3600,
   escalated_wait=14400). PlanningConfig defaults updated; ASSUM-004
   human_response=accepted with ratification note; set explicitly in the
   GB10 config per MP-010.
3. Build-path ApprovalSubscriber raw-client mismatch → **fixed now** as
   TASK-JNB-109 (shared EnvelopeSubscribeClient wired into
   build_approval_gate_parts + rearm_paused_gates; production-signature
   regression tests).
Whether escalation.py closes FEAT-SPL-004: the contract now HAS a
production driver (the wait loop) — assess at SPL-004 review.
