---
id: TASK-FORGE-FRR-F010D
title: "Thread `correlation_id` into PREPARING-recovery `build-failed` emit (DDR-029, recovery surface)"
status: in_review
created: 2026-05-04T11:25:00Z
updated: 2026-05-04T12:30:00Z
priority: high
task_type: fix
tags:
  - forge-serve
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - correlation-id
  - lifecycle-envelopes
  - ddr-029
  - lifecycle-recovery
  - psm-007-followup
complexity: 2
estimated_minutes: 45
estimated_effort: "20-45 minutes (single publish site, symmetrical fix to F010C)"
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FORGE-FRR-F010C   # the consumer-side DDR-029 fix this task mirrors
  - TASK-PSM-007           # the original recovery module that introduced the gap
discovered_on:
  date: 2026-05-04
  context: "TASK-FORGE-FRR-F010C audit (AC-1) flagged this as the only out-of-scope publish site that fails the same DDR-029 invariant. Logged in F010C completion notes; this task closes the gap on the boot-recovery surface."
test_results:
  status: passing
  coverage: null  # skipped per MINIMAL intensity
  last_run: 2026-05-04T12:30:00Z
  notes: "tests/forge/ → 2151 passed, 1 deselected (pre-existing clock-hygiene at approval_subscriber.py:684, spec'd in AC-5). Wider tests/ run (excl. integration/docker) → 4086 passed, 1 deselected. New tests: tests/forge/test_recovery_correlation_id.py (2 tests: AC-3 happy-path threading + AC-4 AST lint guard)."
---

# Task: Thread `correlation_id` into PREPARING-recovery `build-failed` emit (DDR-029, recovery surface)

## Description

DDR-029's notification-thread contract requires every outbound
`pipeline.build-failed.*` envelope to carry the **same** `correlation_id`
as the build it relates to, so that jarvis's `forge_subscriber` can
route the notification back to the originating chat session. TASK-FORGE-FRR-F010C
closed this on the consumer-side rejection paths in
`forge.adapters.nats.pipeline_consumer`. The same invariant is violated
on the boot-recovery surface at
[`src/forge/lifecycle/recovery.py:266`](../../../src/forge/lifecycle/recovery.py#L266):

```python
# In _handle_preparing(build: BuildRow, ...):
await publisher.publish_build_failed(_build_failed_payload(build))
```

`_build_failed_payload(build)` constructs a v1
[`BuildFailedPayload`](../../../../../nats-core/src/nats_core/events/_pipeline.py)
without ever calling
[`attach_correlation_id`](../../../src/forge/pipeline/__init__.py#L224).
The publisher's central envelope construction at
[`pipeline_publisher._publish_envelope`](../../../src/forge/adapters/nats/pipeline_publisher.py#L176)
then reads `getattr(payload, "correlation_id", None)` and writes
`correlation_id: null` onto the outbound envelope — same shape of bug as
F010C's empirical case, different surface.

This task is the **symmetrical fix** to F010C on the boot-recovery
surface. `BuildRow.correlation_id` is already available (it is a
required field in
[`forge.lifecycle.persistence.BuildRow`](../../../src/forge/lifecycle/persistence.py#L177-L197)),
so the fix is mechanically trivial — the work is mostly the publish-site
audit (other recovery branches), the regression test, and the live-wire
validation.

## Why

### Why a separate task

TASK-FORGE-FRR-F010C's spec scope was explicitly:

> AC-1 (publish-site audit): Every outbound publish site in
> `forge.adapters.nats.pipeline_consumer` (and any caller in
> `forge.pipeline.lifecycle_emitter` / `forge.pipeline.publisher`)

The recovery module is `forge.lifecycle.recovery`, which is **not**
under either of those package paths. The audit table in F010C's
completion notes flagged the recovery emit as out-of-scope and
recommended this follow-up. Bundling the fix into F010C would have
violated the spec scope and would have made the F010C diff harder to
review against its acceptance criteria.

### Why it matters operationally

The PREPARING-recovery emit fires every time forge restarts with a
build that was mid-PREPARING when the previous process died. The
`recoverable=True` flag on the payload tells jarvis subscribers to
render this as a transient "will retry" notification rather than a
terminal failure card — but if `correlation_id` is `null`, the
notification cannot be routed to the originating chat session, so
the user never sees it. The user is left wondering why their queued
build never started, with no notification thread to discover the
recovery state on.

### Live-wire validation that's already pending

TASK-FORGE-FRR-F010C left AC-6 (live-wire validation) as an operator
follow-up — rebuild the forge image with the F010C fix, rerun jarvis
runbook §6.2 + §7. Landing F010D in the same cycle means **one** image
rebuild + **one** runbook rerun validates both fixes end-to-end. The
runbook should exercise both surfaces:

- F010C surface: queue a build with a path outside the allowlist;
  capture the outbound `pipeline.build-failed.*` envelope; assert the
  inbound correlation_id is threaded.
- F010D surface: queue a build, kill `forge serve` while the build is
  in PREPARING, restart `forge serve`; capture the outbound
  `pipeline.build-failed.*` envelope from the recovery pass; assert
  the original correlation_id is threaded.

## Investigation Required

1. **Audit `lifecycle/recovery.py` for every publish site**.
   `_handle_preparing` is the known one (line 266). Grep the module
   for `publish_build_failed`, `publish_build_paused`,
   `publish_request`, and any other publisher calls; confirm each one
   either threads correlation_id or is on a path where threading is
   N/A (e.g. duplicate-elimination skip).

2. **Verify `_build_failed_payload(build)` call sites**.
   `_build_failed_payload` is a small helper at line 214; check
   whether other recovery branches (RUNNING, FINALISING, etc.)
   construct their own payloads and what their threading looks like.

3. **Cross-reference `ApprovalRepublisher.publish_request`**
   (line 202). PAUSED-recovery re-issues the original
   `ApprovalRequestPayload` verbatim — confirm the original
   `request_id` carries `correlation_id` already, or thread it
   explicitly if not.

4. **Check the PREPARING-recovery test fixture**. The existing test
   suite around recovery is in `tests/forge/test_recovery.py` (or
   similar — confirm by grep). Find the existing PREPARING-emit test
   and either extend it or add a sibling test that asserts
   correlation_id threading on the outbound envelope.

## Acceptance Criteria

- [ ] **AC-1 (recovery publish-site audit)**: Every outbound publish
  site in `src/forge/lifecycle/recovery.py` threads the correct
  `correlation_id` (typically `BuildRow.correlation_id`, or the
  `ApprovalRequestPayload.request_id` that already carries the
  original correlation context). Document the audited list in this
  task's completion notes (mirroring F010C's audit table style).

- [ ] **AC-2 (PREPARING-recovery emit threading)**: The `build-failed`
  envelope emitted from `_handle_preparing` carries
  `BuildRow.correlation_id` on the outbound `MessageEnvelope`. The
  fix shape mirrors F010C: call
  `attach_correlation_id(payload, build.correlation_id)` on the v1
  `BuildFailedPayload` before passing it to
  `publisher.publish_build_failed(...)` — same pattern the lifecycle
  emitter at `src/forge/pipeline/__init__.py` already uses for v1
  payloads, so the publisher's existing `getattr(payload,
  "correlation_id", None)` lookup picks it up.

- [ ] **AC-3 (PREPARING-recovery unit test)**: A unit test asserts
  the outbound `pipeline.build-failed.*` envelope from
  `_handle_preparing` carries the `BuildRow.correlation_id` of the
  recovered row. Cover both happy-path threading and (if any new
  publish sites are uncovered by AC-1) any other recovery-branch
  threading.

- [ ] **AC-4 (cross-cut lint guard)**: Extend (or add a sibling to)
  the AST-based lint guard introduced in F010C
  (`tests/forge/test_pipeline_consumer_correlation_id.py::TestAllRejectionPathsThreadCorrelationId::test_every_safe_publish_failure_call_passes_correlation_id_kwarg`)
  to cover `recovery.py`'s publisher-call surface. Cheapest shape: a
  test that AST-walks `recovery.py`, finds every
  `publisher.publish_build_failed(...)` / `publisher.publish_request(...)`
  call, and asserts each one is preceded by an `attach_correlation_id`
  call (or carries an explicit `correlation_id=` kwarg if a future
  publisher API gains one). The contract MUST NOT be lost again on
  any future recovery branch.

- [ ] **AC-5 (regression)**: Full forge test suite passes
  (`pytest tests/forge/ tests/`). The two pre-existing main failures
  unrelated to this work (clock-hygiene in
  `src/forge/adapters/nats/approval_subscriber.py:684` and the
  docker-run integration test) are still expected; everything else
  green.

- [ ] **AC-6 (live-wire validation, joint with F010C)**: Rebuild the
  forge image including both F010C (already landed) and F010D
  (this task). Rerun jarvis runbook §6.2 + §7 with two scenarios:
  - **Scenario A — F010C surface**: queue a build with a path
    outside the allowlist; assert outbound
    `pipeline.build-failed.*` carries the inbound correlation_id.
  - **Scenario B — F010D surface**: queue a build, kill `forge serve`
    while the build is in PREPARING (the SQLite `builds` row
    has `status=PREPARING`), restart `forge serve`; assert the
    recovery-emitted `pipeline.build-failed.*` envelope carries
    the original `BuildRow.correlation_id`.
  Capture both rerun correlation_ids in this task's completion
  notes. This closes both F010C's deferred AC-6 and this task's AC-6
  in one runbook session.

## Files Expected to Change

- `src/forge/lifecycle/recovery.py` — `_handle_preparing`
  (line 248-266), and possibly `_build_failed_payload` (line 214)
  if the threading is best done at construction time. Plus any
  other recovery branches uncovered by AC-1.
- A new or extended test under `tests/forge/` covering AC-3 / AC-4
  — likely
  `tests/forge/test_recovery_correlation_id.py` (mirroring
  F010C's `test_pipeline_consumer_correlation_id.py` shape) or
  extension of an existing `tests/forge/test_recovery_*.py`
  fixture if one exists.

## Implementation Notes

- **Source of truth for `correlation_id`**: the `BuildRow.correlation_id`
  field on the row being recovered. It is a required (non-Optional)
  field on `BuildRow` per
  [`persistence.py:197`](../../../src/forge/lifecycle/persistence.py#L197),
  so there is no None-handling needed — the BuildRow is hydrated
  from a `builds` row whose `correlation_id` column is NOT NULL by
  schema (`API-sqlite-schema.md`).

- **Threading shape**: the same as F010C —
  `attach_correlation_id(payload, build.correlation_id)` on the v1
  `BuildFailedPayload` before
  `await publisher.publish_build_failed(payload)`. Do NOT change the
  publisher signature; the existing `attach_correlation_id` pattern
  is the cross-codebase convention for v1 payloads (used by every
  `emit_*` site in `src/forge/pipeline/__init__.py`).

- **Don't widen the fix beyond the audit**. F010C's spec scope
  bounded the fix to the consumer surface. This task's spec scope is
  bounded to the recovery surface. If AC-1 surfaces a publish site
  in a third module (e.g. `forge.gating.wrappers`,
  `forge.adapters.nats.approval_publisher`), file it as a separate
  follow-up rather than rolling it into this task — keeping the diff
  scoped to one surface keeps the review tractable and matches the
  F010A → F010B → F010C → F010D cadence.

- **Test fixture for AC-3**: PREPARING-recovery requires a
  `BuildRow` with `status=PREPARING` and a `SqliteLifecyclePersistence`
  that can apply the INTERRUPTED transition. Existing recovery tests
  almost certainly have this fixture shape; reuse it rather than
  rebuilding from scratch.

## Ordering vs related tasks

- **Independent of F010A and F010B** (those landed before F010C).
- **Builds on F010C**: F010C introduced the `attach_correlation_id`
  pattern usage in the consumer/wrapper layer; this task applies the
  same pattern at the recovery layer. The fix is mechanically
  identical.
- **Should land before the joint AC-6 live-wire validation**: the
  runbook rerun for F010C's deferred AC-6 should include this fix
  so one image rebuild covers both surfaces. Recommended order:

  1. Land this task (F010D).
  2. Rebuild forge image with F010C + F010D combined.
  3. Run the joint AC-6 runbook scenarios (A and B above).
  4. Backfill F010C's AC-6 in its completion notes by reference.

## References

- **TASK-FORGE-FRR-F010C completion notes**:
  [`../../completed/TASK-FORGE-FRR-F010C/TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md`](../../completed/TASK-FORGE-FRR-F010C/TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md)
  — the consumer-side fix this task mirrors; see the §"Completion
  Notes (2026-05-04)" / "Follow-up filed" section which logged this
  task.
- **DDR-029** — notification-thread contract; the source of truth
  for the rule that lifecycle envelopes must carry the inbound /
  originating `correlation_id`.
- **TASK-PSM-007** — the original recovery module's brief; cross-check
  whether this threading was intended-but-missed or genuinely
  out-of-scope at the time.
- **Source files**:
  - [`src/forge/lifecycle/recovery.py`](../../../src/forge/lifecycle/recovery.py)
    — `_handle_preparing` (line 248-266) and `_build_failed_payload`
    (line 214)
  - [`src/forge/lifecycle/persistence.py`](../../../src/forge/lifecycle/persistence.py)
    — `BuildRow.correlation_id` field (line 197)
  - [`src/forge/pipeline/__init__.py`](../../../src/forge/pipeline/__init__.py#L224)
    — `attach_correlation_id` helper (line 224)
  - [`src/forge/adapters/nats/pipeline_publisher.py`](../../../src/forge/adapters/nats/pipeline_publisher.py#L176)
    — central envelope construction reading
    `getattr(payload, "correlation_id", None)` (line 176)
- **F010C tests to mirror**:
  - [`tests/forge/test_pipeline_consumer_correlation_id.py`](../../../tests/forge/test_pipeline_consumer_correlation_id.py)
    — pattern for AC-3 unit tests + AC-4 AST-based lint guard

## Completion Notes (2026-05-04)

### AC-1: recovery publish-site audit

| Handler | Publish call | Threads `correlation_id`? | Notes |
|---|---|---|---|
| `_handle_preparing` (line 248) | `publisher.publish_build_failed(payload)` | ✅ **Now yes** | The F010D fix. `attach_correlation_id(payload, build.correlation_id)` mirrors the canonical pattern at `forge.pipeline.__init__.emit_failed` (line 538). |
| `_handle_running` (line 269) | — | N/A | No wire publish; SQL-only INTERRUPTED transition. Re-pickup is via JetStream NACK on the next pull. |
| `_handle_paused` (line 284) | `approval_publisher.publish_request(envelope)` | Out of scope | Envelope is constructed in `forge.adapters.nats.approval_publisher.build_recovery_approval_envelope` (separate module, AC-008 single-ownership). PAUSED-recovery re-issues the verbatim `request_id` (concern `sc_004`), and the responder correlates by `request_id` not `correlation_id`. Per F010D's "don't widen the fix beyond the audit" boundary, threading the envelope-level `correlation_id` in the approval_publisher module would be a separate follow-up if it ever becomes necessary — the current responder does not require it. |
| `_handle_finalising` (line 316) | — | N/A | No wire publish; SQL-only INTERRUPTED transition + operator warning. PR reconciliation is manual via `forge history` (TASK-PSM-007). |
| `_reconcile_one` (line 411) | — | N/A | Dispatcher only. |
| `reconcile_on_boot` (line 342) | — | N/A | Top-level driver; failure-isolation try/except only. |

**Conclusion**: `_handle_preparing` is the **only** publish site in
`forge.lifecycle.recovery` that emits an outbound v1
`pipeline.build-failed.*` envelope. The F010D fix at
`recovery.py:269-271` is sufficient for the recovery surface.

### AC-2 / AC-3 / AC-4: implementation summary

- **AC-2 (PREPARING-recovery emit threading)**: applied at
  `src/forge/lifecycle/recovery.py:264-271`. The fix constructs the v1
  `BuildFailedPayload`, calls
  `attach_correlation_id(payload, build.correlation_id)`, then publishes —
  identical pattern to `forge.pipeline.__init__.emit_failed` (line 538).
  `BuildRow.correlation_id` is a required (non-Optional) field per
  `persistence.py:197`, so no None-handling is needed.
- **AC-3 (PREPARING-recovery unit test)**:
  `tests/forge/test_recovery_correlation_id.py::TestPreparingRecoveryThreadsCorrelationId::test_preparing_recovery_payload_carries_buildrow_correlation_id`.
  Drives a real PREPARING build through `reconcile_on_boot` against an
  in-memory SQLite db (using the same fixture shape as
  `test_lifecycle_recovery.py`) and asserts the captured payload's
  `getattr(payload, "correlation_id", None)` matches the seeded
  `BuildRow.correlation_id`. Pre-fix this assertion would have read `None`.
- **AC-4 (cross-cut lint guard)**:
  `tests/forge/test_recovery_correlation_id.py::TestRecoveryPublishSitesThreadCorrelationId::test_every_publish_build_failed_call_in_recovery_threads_correlation_id`.
  AST-walks `recovery.py`, finds every
  `publisher.publish_build_failed(...)` call, and asserts each one is in a
  function body that also contains an `attach_correlation_id` call. A
  future PR adding a new recovery branch with a publish-without-thread
  MUST fail this test.

### AC-5: regression

- `pytest tests/forge/` → **2151 passed, 1 deselected** (the pre-existing
  clock-hygiene failure at
  `src/forge/adapters/nats/approval_subscriber.py:684`, called out as
  expected in AC-5).
- `pytest tests/` (excluding `tests/integration/`) →
  **4086 passed, 1 deselected**.
- New tests: `tests/forge/test_recovery_correlation_id.py` — 2 passing.
- Existing recovery suite: `tests/forge/test_lifecycle_recovery.py` —
  15 passing (no behavioural regression from the F010D fix; the
  PREPARING tests in that suite assert recoverable-flag and SQL state, not
  envelope correlation_id, so they are orthogonal to this fix).
- The F010C lint guard
  (`tests/forge/test_pipeline_consumer_correlation_id.py`) still passes
  (consumer-side surface unchanged).

### AC-6: live-wire validation (deferred to operator, joint with F010C)

Per the task spec, AC-6 is the joint live-wire runbook with F010C:
rebuild the forge image with both fixes, rerun jarvis runbook §6.2 + §7
with Scenarios A and B. This requires an operator-driven docker rebuild
+ runbook execution and is **not** covered by the unit-test suite — it
is filed as the next operator follow-up. Recommended order from the spec
(unchanged):

1. ✅ Land F010D (this task).
2. Rebuild forge image with F010C + F010D combined.
3. Run joint AC-6 runbook scenarios:
   - **Scenario A (F010C surface)**: queue a build with a path outside
     the allowlist; assert outbound `pipeline.build-failed.*` carries
     the inbound correlation_id.
   - **Scenario B (F010D surface)**: queue a build, kill `forge serve`
     while the build is in PREPARING (SQLite `builds` row has
     `status=PREPARING`), restart `forge serve`; assert the
     recovery-emitted `pipeline.build-failed.*` carries the original
     `BuildRow.correlation_id`.
4. Backfill F010C's deferred AC-6 in its completion notes by reference.

### Files changed

- `src/forge/lifecycle/recovery.py` (+12 / −1):
  - +1 import: `from forge.pipeline import attach_correlation_id`
  - +11 in `_handle_preparing`: extract payload to a local, attach
    `correlation_id`, publish.
- `tests/forge/test_recovery_correlation_id.py` (new, +247):
  - `TestPreparingRecoveryThreadsCorrelationId` (AC-3, 1 test)
  - `TestRecoveryPublishSitesThreadCorrelationId` (AC-4, 1 AST guard)

### Follow-up filed

None. The audit confirmed `_handle_preparing` is the only outbound
publish site in `recovery.py`. The PAUSED-recovery envelope-construction
question (whether `build_recovery_approval_envelope` should set
`MessageEnvelope.correlation_id`) is intentionally not filed: the
responder correlates by `request_id` (sc_004), not by `correlation_id`,
so DDR-029's notification-thread contract is not violated by the
current PAUSED-recovery shape. If a future change makes the responder
correlate by `correlation_id`, file a follow-up against
`forge.adapters.nats.approval_publisher` then.
