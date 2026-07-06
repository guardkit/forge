---
id: TASK-MP-012
title: "Mode P production wiring + composition — make the merged planning library actually run (post-merge review follow-up)"
status: backlog
created: 2026-07-06T16:15:00Z
updated: 2026-07-06T16:15:00Z
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
- [ ] Fix `serve.py:445-458`: the calls to `compose_planning_consumer_and_dispatch`,
      `sweep_interrupted_planning_runs`, `rearm_paused_planning_runs` use keyword
      names that do not exist (`client=`, `planning_config=`, `sqlite_pool=` vs the
      real `db_path`, `nats_client`, `config`). All three currently raise TypeError,
      swallowed by the DDR-007 except.
- [ ] Replace the permissive `*args/**kwargs` fakes in
      `tests/cli/test_serve_planning_wiring.py` with signature-binding pins
      (`inspect.signature(...).bind(...)` or real-callable monkeypatches) so a
      kwargs drift fails CI. This is the PS-002 class, recursively: the pin test
      codified the wrong contract.

**B. Put the consumer on the wire**
- [ ] Actually create/bind the durable `forge-serve-planning` pull consumer on
      PIPELINE filtering `pipeline.planning-queued.*` and subscribe
      `handle_planning_message` (today the durable is declared, never bound).
      Preserve the reviewed non-overlap vs `forge-serve` (`pipeline.build-queued.*`)
      and jarvis's lifecycle filters — workqueue stream, err-10100 class.

**C. Real composition below the boot shim**
- [ ] PO stage dispatch: replace the logging stub with real dispatch via the
      existing specialist dispatcher (Session-3 requirement 2).
- [ ] Approval-decision consumption: wire the checkpoint to the production
      approval machinery so a decision actually resumes/cancels a planning run.
- [ ] Approval envelope: publish valid `ApprovalRequestPayload` via the
      production approval publisher — the current checkpoint/escalation envelopes
      fail jarvis JNB-103 payload validation (WARN-drop).
- [ ] `rearm_paused_planning_runs`: implement (log-only stub today) — re-emit the
      pending approval request and re-arm the wait after restart (ASSUM-015's
      compensating half; restart-while-paused currently orphans the run).
- [ ] Escalation/defer driver: schedule threshold evaluation (nothing evaluates
      `originator_wait`/`escalated_wait` today) and wire the checkpoint defer
      branch to the escalation policy (below-cap defer is a dead end;
      `escalation.py:268` TODO).
- [ ] Handoff terminal: provide the production GitRunner binding, publish the
      handoff notification, and write the PLANNED_HANDOFF row + branch/path
      columns (all currently unreachable/absent in production).

**D. State-machine fixes surfaced by review**
- [ ] Boot sweep: QUEUED→FAILED is not a legal transition and the refusal
      sentinel is ignored — QUEUED runs stick forever. Either legalise the sweep
      transition or route via a legal path; add the RT-08 nuance (do not
      terminally FAIL a RUNNING run crashed between branch commit and record
      update without an event trail).
- [ ] Intake ack-on-store-failure: a transient SQLite error currently acks and
      permanently drops the request — nak/term decision needed (at-least-once).
- [ ] RT-03 correlation_id pattern permits dots, which fragment the approval
      subject past jarvis's 4-token gate (silent drop) — restrict the pattern.

**E. Tracker + docs cleanup**
- [ ] Reconcile the TASK-MP-* tracker state (no MP task file is in
      tasks/in_review/ despite commit/doc claims; six tasks' surviving files say
      design_approved; five exist twice with conflicting statuses; FEAT-3ED2.yaml
      + FEAT-DD4F.yaml carry six stale file_path pointers).
- [ ] Update MP-010's gate text: TASK-FWD-004's unit-disable half was DONE
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
