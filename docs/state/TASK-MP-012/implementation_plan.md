# TASK-MP-012 — Mode P production wiring + composition — Implementation Plan

**Date:** 2026-07-06 · **Complexity:** 7/10 · **Mode:** task-work standard (autonomous session — checkpoint auto-approved, noted per protocol)
**Source review:** `docs/reviews/feat-spl-002-post-merge-review-2026-07-06.md`

## Objective

Make the merged Mode P library actually process a planning request end-to-end:
intake → PO dispatch → product-docs checkpoint (valid approval envelope +
build-paused mirror) → approve/reject/defer/escalate/timeout → planned-handoff
(real GitRunner, PLANNED_HANDOFF row, jarvis notification), with boot
sweep/rearm recovery, plus the state-machine fixes and tracker cleanup.

## Constraints (hard)

- `src/forge/pipeline/` and `src/forge/adapters/guardkit/run.py`: **zero diff** (call-only).
- `planning.enabled` stays default False.
- Full suite green vs pre-existing baseline; review's checks_passed list is the do-not-regress inventory.

## Architecture decisions

1. **Chain driver lives in `src/forge/planning/driver.py`** (domain, injected
   collaborators); `src/forge/cli/_serve_planning.py` stays the composition
   root. The driver is re-entrant from durable state: it translates
   `planning_run_events` rows into `plan_next_step` history events (closing the
   "ExecuteHandoff unreachable from durable history" gap) and resumes at the
   right phase after restart.
2. **PO dispatch composes the existing specialist stack** (first production
   composition, as `_serve_planning.py` docstring intended):
   `DiscoveryCache` (fed by `fleet_watcher.watch` background task) +
   `CorrelationRegistry` + new **wildcard ReplyChannel bridge**
   (`agents.result.*.{correlation_key}` — closes the TASK-SAD-011 gap without
   touching pipeline/) + `NatsSpecialistDispatchAdapter` (publisher) +
   `TimeoutCoordinator` (via a `_RegistryWaitAdapter`) + `SqliteHistoryWriter`
   → `DispatchOrchestrator` → `dispatch_specialist_stage(stage=PRODUCT_OWNER)`.
   `ForwardContextBuilder` gets null reader/allowlist stubs (PRODUCT_OWNER is
   the entry stage → `build_for` short-circuits to `[]`). A
   `_PlanningStageLogWriter` writes `planning_run_events` rows (never
   `stage_log`, which FKs `builds`).
3. **Valid approval envelope**: `checkpoint.build_planning_approval_envelope`
   constructs a real `ApprovalRequestPayload` (`agent_id="forge"`,
   `action_description`, `risk_level="medium"`, `details` incl. the
   publisher-required `details["build_id"]=plan_run_id`, `stage_label`,
   `gate_mode`, `summary`, `expected_approver`, `attempt_count`).
   `escalation.py` imports the same builder (no duplicated envelope logic).
4. **build-paused mirror**: a `_PlanningPausePublisher` wrapper publishes the
   AGENTS approval request FIRST, then `BuildPausedPayload` on
   `pipeline.build-paused.{plan_run_id}` (jarvis JNB-103 join is on build_id;
   ordering mirrors `_MirroredApprovalPublisher`).
5. **Approval decisions**: the driver owns a per-run response waiter
   (core-NATS subscribe on `agents.approval.forge.{plan_run_id}.response`,
   arm-before-post). Identity validation stays at the spec's designed locus —
   `_dispatch_approval_response` verbatim-compares against the RUN ROW's
   `expected_approver`; a mismatched/late response is dropped and the wait
   continues. This avoids the build `ApprovalSubscriber`'s static
   `expected_approver` (which cannot express per-run pinning) rather than
   fighting it. (Cross-repo decided_by drift stays a surfaced design question
   for Rich — NOT decided here.)
6. **Escalation as structured wait, not a poller**: phase and remaining-time
   are computed from durable `paused_at`/`escalated_at` anchors each loop
   iteration; phase-1 expiry escalates durably (CAS `expected_from_state=PAUSED`,
   persist new `pending_approval_request_id` via a new public
   `run_store.update_pending_approval_request_id`) then re-publishes; phase-2
   expiry → TIMED_OUT (existing CAS). Restart neither resets nor double-fires
   (rearm recomputes remaining from the same anchors).
7. **Defer**: checkpoint dispatch tail routes `defer` to
   `handle_defer_request` (optional escalation-context arg); below-cap defer
   now derives attempt+1 request_id, persists it, resets `paused_at`
   (new round = new phase-1 window), and re-publishes a valid envelope.
8. **Handoff**: new `WorktreeGitRunner` in
   `src/forge/adapters/git/planning_runner.py` — isolated
   `git worktree add <tmp> -b planning/{cid}` (never touches the primary
   checkout), file write, commit, sha capture, worktree remove; idempotent
   (branch exists + identical content → success without commit); returns
   `GitOpResult`, never raises. Driver writes the PLANNED_HANDOFF transition
   with `handoff_branch`/`handoff_path`, then best-effort publishes nats-core
   `NotificationPayload` on `jarvis.notification.slack`.
9. **Sweep fixes**: QUEUED with dispatcher → re-drive via driver; QUEUED
   without dispatcher → QUEUED→CANCELLED (legal terminal) with structured
   reason, **checking** the `TransitionRefused` sentinel. RUNNING → re-drive
   via driver (idempotent handoff satisfies RT-08) instead of unconditional
   FAIL; FAIL only when no dispatcher, sentinel checked. `sqlite3.Row.get`
   bugs in exception handlers fixed.
10. **Intake**: store-write failure → `nak()` when available (else no-ack →
    ack_wait redelivery); validation rejects still ack (term semantics).
    `CORRELATION_ID_PATTERN` drops `.` → `^[A-Za-z0-9_-]{1,128}$`.
    New optional `PlanningConsumerDeps.on_recorded` async callback fires after
    a successful QUEUED persist + ack, so the composition can spawn the driver.
11. **Boot fix**: serve.py calls the real signatures; `db_path` threaded into
    `bind_production_dispatch_chain` (new optional kwarg) from
    `ServeConfig.db_path` in `_serve_production.py`. Consumer bind mirrors
    `_serve_daemon._attach_consumer`: durable `forge-serve-planning`, stream
    PIPELINE, filter `pipeline.planning-queued.*`, `ack_wait=3600.0`
    (D659 lesson), `max_ack_pending=1`, fetch(1, timeout=1.0) loop as a
    retained asyncio task. Compose keeps DDR-007 soft-fail.
12. **Pin tests**: `SignatureBindingFake` — records AND
    `inspect.signature(real_fn).bind(*args, **kwargs)` inside `__call__`, so
    kwargs drift fails CI (the PS-002-class fix).

## Files

**Create**
- `src/forge/planning/driver.py` — PlanningRunDriver, deps, history translation, wait loop
- `src/forge/adapters/git/planning_runner.py` — WorktreeGitRunner
- `src/forge/adapters/nats/reply_channel.py` — WildcardReplyChannel
- `tests/forge/planning/test_driver.py`
- `tests/forge/adapters/test_planning_runner.py` (real git repo in tmp_path)
- `tests/forge/adapters/test_reply_channel.py`

**Modify**
- `src/forge/cli/serve.py` (call-site fix, db_path threading, composition→sweep/rearm handoff)
- `src/forge/cli/_serve_production.py` (pass db_path)
- `src/forge/cli/_serve_planning.py` (real composition, consumer bind + loop, sweep/rearm implementations)
- `src/forge/planning/checkpoint.py` (valid envelope, expected_approver, provider try/except, defer wiring)
- `src/forge/planning/escalation.py` (shared envelope, persist request_id, CAS, defer re-publish, persist-on-no-publisher)
- `src/forge/planning/run_store.py` (`update_pending_approval_request_id`, `_record_event` returns row id)
- `src/forge/adapters/nats/planning_consumer.py` (pattern, nak, on_recorded)
- `tests/cli/test_serve_planning_wiring.py`, `tests/cli/test_serve_planning.py`,
  `tests/forge/planning/test_checkpoint.py`, `tests/forge/planning/test_escalation.py`,
  `tests/forge/adapters/test_planning_consumer.py` (update to new contracts)

**Tracker/docs (section E)**
- Move built TASK-MP-001..009,011 files → `tasks/in_review/` (status corrected), delete design_approved + duplicate backlog copies, keep exactly one MP-010 in backlog
- Fix six stale `file_path` pointers in `.guardkit/features/FEAT-3ED2.yaml` + `FEAT-DD4F.yaml`
- MP-010 gate annotation (FWD-004 unit-disable half done 2026-07-06; override-revert + password rotation open)
- FWD-004 dated checklist tick; dated addendum to `docs/state/TASK-GATE-D659/deploy-verification-2026-07-06.md`

## Risks

- Dispatch stack composition is first-of-kind → mitigated by unit tests with fakes at every seam + DDR-007 soft-fail at boot.
- Multiple SQLite writer connections → single shared writer connection threaded through composition; WAL + busy_timeout as backstop.
- Test churn in existing planning tests →每 change is contract-tightening flagged by the review itself.

## Test strategy

Unit: driver phase transitions (fakes for dispatch/publisher/waiter/git), envelope validity (`ApprovalRequestPayload.model_validate` + publisher `details.build_id` requirement), escalation persist/CAS/defer re-publish, sweep sentinel handling, consumer nak/dot-rejection/on_recorded, git runner against a real tmp git repo (idempotency, isolation), reply channel with fake nats. Integration: compose over InMemoryNats + real v3 tmp DB driving QUEUED→PLANNED_HANDOFF with scripted approvals. Regression: full planning selection + full suite vs baseline (8+2 known infra failures).
