---
id: TASK-FORGE-FRR-F010F
title: "Publish terminal `build-failed` envelope when `dispatch_build` raises (instead of silently acking)"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T00:00:00Z
completed: 2026-05-04T00:00:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010F/
priority: high
task_type: fix
tags:
  - forge-serve
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - dispatch
  - error-recovery
  - lifecycle-envelopes
  - correlation-id
  - ddr-029
  - pipeline-consumer
  - fw10-009-followup
complexity: 3
estimated_minutes: 60
estimated_effort: "30-90 minutes (publish call + threading + 2 unit tests)"
parent_feature: FEAT-FORGE-010
correlation_id: dfad8e7f-92af-4b5f-896f-ca75ad8343bf
related_tasks:
  - TASK-FW10-009   # validation surface and build-failed paths — possibly already on its AC list and slipped
  - TASK-FORGE-FRR-F010C   # correlation_id threading — predecessor; F010F extends its contract to a new publish site
  - TASK-FORGE-FRR-F010E   # sibling — F010E fixes the immediate cause; F010F is the safety net for all future dispatch failures
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-F010.A/B/C/D joint live-wire validation rerun late afternoon — co-symptom of F010.E surfaced the need for a terminal-envelope safety net on the dispatch-failure path"
test_results:
  status: passing
  coverage: null
  last_run: 2026-05-04T00:00:00Z
  notes: |
    AC-2/AC-3/AC-4 covered by new
    tests/forge/test_pipeline_consumer_dispatch_failure_publish.py
    (4 tests, all passing). FW10-009's
    test_pipeline_consumer_validation.py::TestDispatchErrorIsContained
    updated to assert F010.F's narrowed contract (publish + ack instead
    of ack-only). Full pipeline-consumer regression sweep: 90 passed.
    Full repo: 4267 passed, 3 skipped — two pre-existing main failures
    (clock-hygiene lint on approval_subscriber.py:684; docker integration
    test against stale forge:production-validation image) are unrelated
    to F010F and reproduce on stock main.
---

# Task: Publish terminal `build-failed` envelope when `dispatch_build` raises (instead of silently acking)

## Description

When `dispatch_build` raises an unhandled exception, the consumer's
exception handler at
`src/forge/adapters/nats/pipeline_consumer.py:470-506` currently
logs a `WARNING` line, acks the message via the idempotent
ack-callback, and moves on — silently dropping the operator's chat
thread. The handler's existing comment block (lines 484-488)
explicitly says "We do NOT publish `build-failed` here: if the state
machine got far enough to record any transition it owns the publish,
and emitting a duplicate from the consumer would violate the
single-source-of-truth contract (ADR-ARCH-008)" — but the empirical
evidence on 2026-05-04 (Gap F010.B, then again as Gap F010.E)
shows that `dispatch_build` can raise **before** the running state
machine has had a chance to record any transition. In those cases
ADR-ARCH-008's protection does not apply (there's no state machine
to be the source of truth yet), and the silent-ack behaviour leaves
the operator's chat session with zero feedback.

The two cases observed live so far:

1. **F010.B** — `'SqliteLifecyclePersistence' object has no attribute
   'get_approved_stage_entry'` (resolved by F010.B's StageLogReader
   adapter). `dispatch_build` reached `dispatching autobuild`, then
   raised inside the forward-context-builder *before* any lifecycle
   emit fired.
2. **F010.E** — `'StructuredTool' object has no attribute
   'start_async_task'` (currently open). `dispatch_build` reached
   `dispatching autobuild`, persisted the QUEUED row to `builds`,
   then raised on the `start_async_task` call *before* any
   lifecycle emit fired.

Both produced **zero outbound envelopes** on the wire — no
`build-failed`, no `build-complete`, nothing — even though jarvis's
session-tracking `correlation_id` was already known and persisted
in the SQLite QUEUED row. The validation-rejection codepath (the
one TASK-FORGE-FRR-F010C threads `correlation_id` through) **does**
publish a terminal envelope via
`pipeline_consumer._safe_publish_failure(...)` at lines 365 / 391
/ 414 / 436; the dispatch-failure codepath does not. That asymmetry
breaks the DDR-030 between-prompt notification contract for any
class of dispatch error, present or future.

## Why this is a separate task from F010.E

F010.E is the immediate fix for the StructuredTool-API gap — once
it lands, that specific dispatch failure stops happening.

**F010.F is the safety net for *all* future dispatch failures.**
The autobuild dispatch chain can fail in many ways: out-of-disk on
the SQLite write, missing git ref, autobuild_runner subagent
construction timeout, transient NATS connection loss inside a
collaborator, unforeseen wiring drift after a refactor — to name
five. The DDR-029/DDR-030 contract should be: if `dispatch_build`
raises before the running state machine takes ownership of the
publish, the consumer publishes a terminal `build-failed` envelope
before acking, with the inbound `correlation_id` threaded onto it
(per F010.C's contract). Even when F010.E lands and the
StructuredTool path stops failing, F010.F's safety net stays
valuable for every future failure mode the dispatcher hasn't
anticipated yet.

This is also the codepath that will run if any *future* refactor
re-introduces a wiring-drift gap — without F010.F, a regression
of the F010.B / F010.E shape would silently drop chat threads
again, and the only way an operator would notice is by tailing
`docker logs forge-prod` for the `dispatch_build raised` WARNING
line. With F010.F, the regression is loud on the JetStream wire and
the chat REPL renders a terminal failure card, even if the
underlying bug is identical to F010.B / F010.E.

## Why

### Empirical evidence (run 1 of 2026-05-04 late afternoon rerun)

correlation_id `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`:

```
2026-05-04T12:22:55 [INFO] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatching build feature_id=FEAT-43DE correlation_id=dfad8e7f-... originating_adapter=terminal
2026-05-04T12:22:55 [INFO] forge.cli._serve_deps: dispatch_build: persisted QUEUED row build_id=build-FEAT-43DE-20260504122255 feature_id=FEAT-43DE correlation_id=dfad8e7f-...; dispatching autobuild
2026-05-04T12:22:55 [WARNING] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatch_build raised ('StructuredTool' object has no attribute 'start_async_task') for feature_id=FEAT-43DE correlation_id=dfad8e7f-...; acking and continuing so the next build can be processed
```

`pipeline.>` tail during run 1 captured **only** the inbound
`pipeline.build-queued.FEAT-43DE` envelope; no outbound publishes
from forge despite the daemon's logs showing `dispatch_build raised
(...)` and a known correlation_id. Earlier in the day the F010.B
runs (`a55df422-…`, `f876fd47-…`) showed the same shape — the
exception fires before any lifecycle emit, the consumer logs and
acks, and the operator's chat session sees nothing on the wire.

### Why ADR-ARCH-008's "no duplicate publish" protection doesn't apply here

ADR-ARCH-008's single-source-of-truth-for-publishing contract
prevents the consumer from emitting a `build-failed` envelope when
the state machine has already started — because the state machine
will publish a `build-failed` itself on transition to a terminal
state, and a duplicate from the consumer would cause two
notifications to render in the operator's chat REPL.

But the empirical failure mode in F010.B and F010.E is that the
state machine **hasn't started yet** — `dispatch_build` raises
before any state machine transition is recorded. There's no risk of
a duplicate because the state machine never gets to publish.

The fix is therefore not "remove ADR-ARCH-008's protection" but
"narrow it": publish from the consumer **only when** the dispatcher
raises before any state machine transition has fired. In practice
this means publishing on every `dispatch_build` exception (because
if the state machine had started, it would have caught its own
internal exceptions and transitioned to a terminal state without
re-raising out to the consumer's outer try/except). If a future
refactor changes that invariant, the Implementation Notes section
below documents the audit path.

## Investigation Required

1. **Read the existing exception handler** at
   `src/forge/adapters/nats/pipeline_consumer.py:470-506`. Identify
   the exact site where the new publish call should be inserted —
   between the WARNING log line (line 489-496) and the ack_callback
   invocation (line 497-506). The publish should happen
   **before** the ack so a publish failure (network blip, NATS
   connection drop) doesn't leave the message acked but the
   notification undelivered.
2. **Confirm `_safe_publish_failure` and `_failure_payload` are
   reusable** for the dispatch-failure path. Most likely yes — same
   envelope shape, just a different `failure_reason` string. The
   call shape from F010.C's audit table (row 3) is:
   ```python
   await _safe_publish_failure(
       deps,
       _failure_payload(payload, failure_reason="..."),
       payload.feature_id,
       correlation_id=envelope.correlation_id,
   )
   ```
3. **Cross-check TASK-FW10-009's ACs**
   (`tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md`)
   to see whether this case was supposed to be covered. If it was
   meant to be covered and slipped, mention that in the task body
   so reviewers know whether F010.F is "missed AC enforcement"
   (cheap) or "new contract" (small extension). The task title
   ("validation surface and build-failed paths") and the existing
   ADR-ARCH-008 comment block at `pipeline_consumer.py:484-488`
   together suggest FW10-009 deliberately *excluded* this path —
   F010.F is the deliberate decision to add it back as a safety
   net. Confirm during investigation.
4. **Audit `lifecycle.recovery._handle_preparing`** (the path
   F010.D-forge fixed) for the same shape: does it publish on
   recovery-time exceptions? It does, per F010.D-forge's commit
   `a7eb9d5` and the AST lint guard at
   `tests/forge/test_recovery_correlation_id.py`. F010.F is the
   consumer-side companion to that recovery-side fix. Document the
   symmetry in §Implementation Notes.

## Acceptance Criteria

- [ ] **AC-1 (publish on raise + thread correlation_id)**: When
  `dispatch_build` raises an unhandled exception, the consumer
  publishes a `pipeline.build-failed.<feature_id>` envelope
  **before** acking. The envelope:
  - threads the inbound `correlation_id` (per F010.C's contract —
    re-use `_safe_publish_failure` / `_failure_payload` if possible
    so the threading work F010.C already did extends to this site
    automatically),
  - sets `failure_reason` to a human-readable string including the
    exception class name and message (e.g. `"AttributeError:
    'StructuredTool' object has no attribute 'start_async_task'"`)
    — useful for the operator triaging the chat REPL terminal-card
    rendering,
  - sets `recoverable: false` (matching the existing
    rejection-publish convention in `_failure_payload`;
    dispatch-failures are not retried by operator workflow).
- [ ] **AC-2 (AttributeError unit test)**: A unit test injects a
  `dispatch_build` that raises `AttributeError("test failure")`,
  drives `handle_message` against the resulting deps, and asserts
  the outbound envelope on `pipeline.build-failed.<feature_id>`
  carries the inbound `correlation_id` and a `failure_reason`
  containing `"AttributeError"`. This is the regression test
  against the empirical F010.E failure mode.
- [ ] **AC-3 (cross-cut for other exception classes)**: A second
  unit test asserts the same for an unrelated exception class
  (e.g. `RuntimeError("disk full"))` so the contract is not pegged
  to a single exception type. Both AttributeError and
  RuntimeError must produce envelope-correct `build-failed`
  output. Optionally parametrise.
- [ ] **AC-4 (publish-failure-still-acks)**: A third unit test
  asserts that even when **publishing the build-failed envelope
  itself fails** (e.g. NATS connection refused, encoding error),
  the inbound message is still acked. The daemon must never wedge
  the queue on an outbound publish failure — log the publish
  failure and proceed to ack. Mirror the existing
  `_safe_publish_failure` swallow-and-log pattern at
  `pipeline_consumer.py:308-322`.
- [ ] **AC-5 (regression)**: Full forge test suite passes
  (`pytest tests/forge/ tests/`). Existing dispatch-success tests
  in `tests/forge/test_pipeline_consumer_*.py` and
  `tests/cli/test_serve_deps_dispatch_real_persistence.py`
  continue to pass — the new publish only fires on the exception
  path, never on the happy path.
- [ ] **AC-6 (live wire validation)**: Pending operator action.
  Re-run jarvis runbook §6.2 + §7 with TASK-FORGE-FRR-F010E **NOT
  yet landed** (i.e., the StructuredTool dispatch failure still
  occurring) and verify that a `pipeline.build-failed.FEAT-43DE`
  envelope appears on the wire with the correct correlation_id
  and a `failure_reason` mentioning `StructuredTool`. This
  validates F010.F's safety net works against a real failing
  dispatch. Capture the rerun correlation_id in completion notes.
  After F010.E lands, re-verify against an injected synthetic
  failure (e.g. via a feature flag or a unit-style `nats pub` of
  an envelope known to break a specific collaborator).

## Files Expected to Change

- `src/forge/adapters/nats/pipeline_consumer.py` — the
  `dispatch_build` exception handler at lines 470-506. Insert the
  `_safe_publish_failure(...)` call between the WARNING log and the
  ack_callback. Construct the failure payload via
  `_failure_payload(payload, failure_reason=f"{exc.__class__.__name__}: {exc}")`.
  Estimated diff: 10-20 lines.
  - Update the comment block at lines 484-488 to reflect the new
    contract: "We *do* publish `build-failed` here when
    `dispatch_build` raises before the running state machine takes
    ownership of the publish — see TASK-FORGE-FRR-F010F. The
    no-duplicate guarantee from ADR-ARCH-008 holds because raising
    out of `dispatch_build` to the consumer's outer try/except
    means the state machine never started transitioning."
- `tests/forge/test_pipeline_consumer_*.py` (or new
  `tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`)
  — AC-2, AC-3, AC-4 tests. Mirror the fixture pattern in
  `tests/forge/test_pipeline_consumer_correlation_id.py` (the
  F010.C regression suite).
- Possibly `tests/forge/test_pipeline_consumer_correlation_id.py`
  — extend the existing AST lint guard
  (`TestAllRejectionPathsThreadCorrelationId`'s lint guard from
  F010.C's AC-4) to cover the new publish site so future drift
  there triggers the same regression alarm.

## Implementation Notes

- **Sequence vs F010.E**: F010.F lands independently. Two ordering
  preferences are equally defensible:
  - **F010.F first**: lets the safety net be visible on the wire
    immediately (F010.F's AC-6 is verifiable today using the
    existing F010.E failure mode — a chat-driven queue produces a
    visible `build-failed` envelope instead of silent drop). The
    operator gets immediate value before the deeper fix lands.
  - **F010.F second**: lets F010.E close first and verify with a
    real autobuild path; F010.F then becomes a future-proofing
    layer that is hard to test against a real failure (would need
    an injected synthetic failure). Reviewers may prefer this
    ordering because the AC-6 verification window for F010.F's
    safety net is narrower if F010.E has already landed.
  
  Either order works. Recommended: **F010.F first** unless the
  implementer has a strong reason to flip. The F010.E fix is more
  complex (option-comparison + tests against real LangChain tool
  surface) than F010.F (single publish site + 3 unit tests), so
  F010.F-first delivers a safety net while F010.E goes through
  review.

- **Don't conflate with FW10-009**: TASK-FW10-009 covered
  "validation surface and build-failed paths" — primarily
  inbound-validation failures (the F010.C-threaded codepath at
  `pipeline_consumer.py:354-446`, four `_safe_publish_failure`
  call sites). The dispatch-failure path is *post-validation*; it's
  a different stage in `handle_message` (line 459-506). If the
  audit (Investigation Required step 3) shows FW10-009 *did* mean
  to cover this, downgrade F010.F to "enforce existing FW10-009 AC
  X.Y" in the task body. If FW10-009 deliberately deferred this
  path on ADR-ARCH-008 grounds (as the existing comment block at
  lines 484-488 suggests), F010.F is the deliberate
  reconsideration of that decision under the empirical evidence
  that ADR-ARCH-008's protection doesn't apply when the state
  machine never starts.

- **`_failure_payload` is the right helper**: TASK-FORGE-FRR-F010C's
  audit table (row 3) confirms the four existing
  `_safe_publish_failure` call sites use `_failure_payload(...)`.
  Adopt the same pattern at the new site so the
  correlation_id-threading work F010.C did at the helper level
  extends transparently to F010.F's site. Do not invent a new
  payload constructor.

- **`failure_reason` shape**: include the exception class name
  prominently so triage is fast: `f"{exc.__class__.__name__}:
  {exc}"` produces `"AttributeError: 'StructuredTool' object has
  no attribute 'start_async_task'"`. Truncate to a reasonable
  length (256 chars?) if the contract has a payload-size limit;
  read `nats_core.envelope.MessageEnvelope` and the existing
  `BuildFailedPayload.failure_reason` field declaration for the
  canonical shape.

- **Symmetry with `lifecycle.recovery._handle_preparing`**:
  F010.D-forge (commit `a7eb9d5`) added `attach_correlation_id`
  threading to the PREPARING-recovery emit at
  `src/forge/lifecycle/recovery.py:266` plus an AST lint guard at
  `tests/forge/test_recovery_correlation_id.py`. F010.F is the
  consumer-side companion to that recovery-side fix — same
  defensive shape (publish a terminal failure envelope when the
  state machine doesn't get to do it itself), same threading
  contract. Cite the symmetry in the commit message and the
  comment block update.

- **Don't loosen the consumer's outer try/except itself**. The
  `pipeline_consumer.handle_message` ack-and-continue behaviour is
  intentional (DDR-019's no-wedge-the-queue contract). F010.F
  *adds* a publish call inside the existing handler before the
  ack — it does not change the ack semantics or the daemon's
  resilience properties.

- **Test fixture cost**: writing AC-2/AC-3/AC-4 tests against the
  real `_safe_publish_failure` / `_failure_payload` is cheaper
  than mocking the publish chain — the F010.C regression test
  suite at `tests/forge/test_pipeline_consumer_correlation_id.py`
  already builds a fixture that captures published envelopes; reuse
  that fixture rather than rolling new mocks.

## Ordering vs related tasks

This task has the following natural dependency order with its
siblings in the post-F010.A/B/C/D set:

1. ~~**TASK-FORGE-FRR-F010A** (apply migrations on boot)~~ —
   completed; not directly load-bearing for F010.F (the unit tests
   don't need a real DB) but the live-wire AC-6 needs it.
2. ~~**TASK-FORGE-FRR-F010B** (StageLogReader adapter)~~ —
   completed; surfaced the same co-symptom (silent drop on
   dispatch failure) that F010.F is the safety-net fix for.
3. ~~**TASK-FORGE-FRR-F010C** (correlation_id threading)~~ —
   completed; F010.F **builds on its contract** by adopting the
   `_safe_publish_failure(..., correlation_id=...)` call shape at
   the new publish site. The threading work doesn't need to be
   redone; just reused.
4. **TASK-FORGE-FRR-F010E** (sibling — `start_async_task`
   AttributeError fix) — independent of this task; land in either
   order.
5. **This task (F010.F)** — the dispatch-failure publish safety
   net. Independent of F010.E; land in either order.
6. **TASK-FW10-011** (end-to-end integration test, currently
   `design_approved` per README post-merge follow-up AC-12) —
   should land **after** F010.E and F010.F as the codified
   regression lock that asserts both behaviours never recur.

## References

- **RESULTS file** (joint validation rerun, late afternoon
  2026-05-04 — Addendum 2):
  [`../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
  — see "Recommended follow-ups (final delta)" item 3
  ("dispatch-failure-publish") for the verbatim source-of-truth
  description of this gap. Co-symptom of Gap F010.E.
- **TASK-FORGE-FRR-F010E (sibling — immediate fix)**:
  [`TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md`](TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md)
  — co-filed companion. F010.E fixes the immediate
  StructuredTool-API mismatch; F010.F is the safety net for *all*
  future dispatch failures.
- **TASK-FORGE-FRR-F010C (predecessor — correlation_id contract)**:
  [`../../completed/TASK-FORGE-FRR-F010C/TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md`](../../completed/TASK-FORGE-FRR-F010C/TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md)
  — built the threading helper and AST lint guard that F010.F
  inherits and extends.
- **TASK-FORGE-FRR-F010B (predecessor — first co-symptom)**:
  [`../../completed/TASK-FORGE-FRR-F010B/TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md`](../../completed/TASK-FORGE-FRR-F010B/TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md)
  — the first time the silent-drop-on-dispatch-failure shape was
  observed live (run 4, `f876fd47-…`). F010.F is the codified
  safety net that prevents that shape from being silent again.
- **TASK-FW10-009** (validation surface and build-failed paths) —
  audit its ACs to attribute / document whether this path was
  intended to be covered. The current pipeline_consumer comment
  block at lines 484-488 suggests this codepath was deliberately
  deferred on ADR-ARCH-008 grounds; F010.F is the deliberate
  reconsideration.
- **TASK-FIX-F010 (production-binding sibling)**:
  [`../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md`](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md)
  — introduced the wrapper that runs the production composer; this
  task closes the publish-side safety net that the wired composer
  exposes the need for.
- **DDR-029** — notification-thread contract; the source of truth
  for the rule that lifecycle envelopes must carry the inbound
  `correlation_id`. F010.C extends this to all outbound publish
  sites in `pipeline_consumer`; F010.F extends it to the new
  dispatch-failure publish site.
- **DDR-030** — between-prompt notification contract. The
  silent-ack behaviour without a `build-failed` envelope is what
  this task closes — operators expect every queued build to either
  produce a terminal envelope or be visible-as-pending in the
  state-channel; today the dispatch-failure case produces neither.
- **ADR-ARCH-008** — single-source-of-truth-for-publishing
  contract. F010.F narrows ADR-ARCH-008's protection to "when the
  state machine has started"; pre-state-machine raises now publish
  from the consumer.
- **Source files**:
  - [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py)
    — the `dispatch_build` exception handler at lines 470-506
    (AC-1 site); `_safe_publish_failure` helper at line 267-322;
    `_failure_payload` constructor at line 245-265.
  - [`src/forge/lifecycle/recovery.py`](../../../src/forge/lifecycle/recovery.py)
    — `_handle_preparing` symmetry (recovery-side analog).
  - [`tests/forge/test_pipeline_consumer_correlation_id.py`](../../../tests/forge/test_pipeline_consumer_correlation_id.py)
    — fixture pattern + AST lint guard to extend.
  - [`tests/forge/test_recovery_correlation_id.py`](../../../tests/forge/test_recovery_correlation_id.py)
    — F010.D-forge's lint-guard precedent for symmetric defensive
    publish testing.
- **Run that surfaced this**:
  - **correlation_id**: `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`
    (run 1 of 2026-05-04 late afternoon rerun); also reproduced
    earlier as the F010.B co-symptom (`f876fd47-…`, evening rerun
    earlier same day).
  - **Date**: 2026-05-04 (late afternoon rerun)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `a7eb9d5` (post `c066033` F010A + `751995f`
    F010B + `172c795` F010C + `a7eb9d5` F010D-forge)
  - **Image**: `forge:latest` = sha256 `2ae6f655ad08...`
  - **Codepath**: post-validation dispatch-failure path —
    `dispatch_build` raises after path / originator / duplicate
    checks pass, before the running state machine takes ownership
    of the publish.
