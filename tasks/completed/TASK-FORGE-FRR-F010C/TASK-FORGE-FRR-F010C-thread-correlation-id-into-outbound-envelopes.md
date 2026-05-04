---
id: TASK-FORGE-FRR-F010C
title: "Thread inbound `correlation_id` into outbound `pipeline.*` envelopes from `pipeline_consumer`"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T11:22:21Z
completed: 2026-05-04T11:22:21Z
previous_state: in_review
completed_location: tasks/completed/TASK-FORGE-FRR-F010C/
state_transition_reason: "AC-1..AC-5 satisfied with regression-locked tests (4244 passed, 2 pre-existing main failures unrelated to this task); AC-6 left as operator follow-up (live-wire jarvis runbook rerun against rebuilt forge image — same pattern as TASK-FORGE-FRR-F010B)"
organized_files:
  - TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md
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
  - pipeline-consumer
  - fw10-009-followup
complexity: 3
estimated_minutes: 60
estimated_effort: "30-60 minutes (find publish sites, thread the field, add unit test)"
parent_feature: FEAT-FORGE-010
correlation_id: 21df1258-63cb-4e8a-9bef-89234833b68e
related_tasks:
  - TASK-FW10-009   # validation surface and build-failed paths — should have wired this
  - TASK-FW10-010   # pause-resume publish round-trip
  - TASK-FIX-F010   # the wiring this exposes
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-TASK-FIX-F010 jarvis FRR runbook rerun on the GB10 — production composer wired (TASK-FIX-F010 verified) but autobuild dispatch path errored once exercised end-to-end"
test_results:
  status: passing
  coverage: null
  last_run: 2026-05-04
  passed: 4244
  failed: 0
  pre_existing_failures: 2  # unrelated: clock-hygiene in approval_subscriber.py + docker integration test
  new_tests:
    - tests/forge/test_pipeline_consumer_correlation_id.py (7 tests)
    - tests/cli/test_serve_deps.py::test_publish_build_failed_threads_correlation_id_onto_envelope (1 test)
---

# Task: Thread inbound `correlation_id` into outbound `pipeline.*` envelopes from `pipeline_consumer`

## Description

DDR-029's notification-thread contract requires every outbound
lifecycle envelope (`build-started`, `stage-complete`, `build-complete`,
`build-failed`) to carry the **same** `correlation_id` as the inbound
`build-queued` envelope that triggered it. That's how jarvis's
`forge_subscriber` routes notifications back to the originating chat
session — without a matching `correlation_id`, even a wire-correct
envelope is unrouteable.

Runs 1 and 2 of the post-TASK-FIX-F010 rerun on 2026-05-04 captured
an outbound `pipeline.build-failed.FEAT-43DE` envelope (path-allowlist
rejection codepath) carrying `correlation_id: null` instead of the
inbound `21df1258-…` / `b5c5e1e2-…`. So even when Gap F010.B
(autobuild can't start) closes and Gap F010.D (jarvis subscription
narrower than rendering surface) closes, the chat REPL still couldn't
render the notification against the right session — the routing
field is missing at the source.

This is plausibly already on FW10-009's AC list and just slipped, or
it's genuinely out of scope and was never wired. Either way, the
fix is small and the test surface is a fixture-able publish round-trip.

## Why

### Empirical evidence (run 1 of post-FIX-F010 rerun, 2026-05-04 evening)

correlation_id `21df1258-63cb-4e8a-9bef-89234833b68e`.

**Inbound (jarvis-published, captured by `nats sub "pipeline.>" --raw`):**

```json
{"correlation_id":"21df1258-63cb-4e8a-9bef-89234833b68e", "payload":{"feature_id":"FEAT-43DE", ...}}
```

**Outbound (forge-published, on `pipeline.build-failed.FEAT-43DE`):**

```json
{"correlation_id":null, "source_id":"forge", "event_type":"build_failed", "payload":{"feature_id":"FEAT-43DE","build_id":"FEAT-43DE","failure_reason":"path outside allowlist","recoverable":false,"failed_task_id":null}}
```

The `failure_reason: "path outside allowlist"` confirms the
publish site is the path-validation rejection path inside
`pipeline_consumer` (FW10-009's surface). The `correlation_id: null`
confirms the inbound `correlation_id` is not threaded into the
outbound envelope.

Run 2 (`b5c5e1e2-dd5d-4df9-bc26-a5ec36f6db8f`) reproduces identically
with the path normalised to absolute and back to relative —
correlation_id still null on the outbound.

### Why it matters even after F010.B and F010.D close

- F010.D widens jarvis's subscription so it can SEE
  `pipeline.build-failed.>` and `pipeline.build-started.>` envelopes.
- F010.B unblocks the autobuild dispatcher so those envelopes are
  actually emitted in the success path.
- **But** without F010.C's threading, jarvis's chat REPL (driven by
  `routing_history`-keyed correlation_id) can't render the
  notification against the same session that issued the build-queued
  envelope. The notification is visible-on-the-wire-but-unrouteable.

The DDR-029 contract is the source of truth here — every lifecycle
envelope MUST carry the originating correlation_id. The current state
violates it for `build-failed`; it would violate it for
`build-started` / `stage-complete` / `build-complete` too if those
publish sites are also missing the threading (audit needed).

## Investigation Required

1. **Find every publish site** in
   `forge.adapters.nats.pipeline_consumer` (and any caller in
   `forge.pipeline.lifecycle_emitter` / `forge.pipeline.publisher`)
   that constructs an outbound `MessageEnvelope`. Grep for
   `MessageEnvelope(` and `_failure_payload`.
2. **For each site**, check whether it reads `correlation_id` from
   the inbound envelope. The current `_failure_payload` site
   provably does not (run-1 evidence).
3. **Cross-reference `nats_core.envelope.MessageEnvelope`** for the
   `correlation_id` field shape — confirm it's still the canonical
   string field, and that `MessageEnvelope.correlation_id` accepts
   the inbound's value as-is.
4. **Audit FW10-009's ACs** to see if this was intended-but-missed
   or genuinely out of scope. If on-AC, document the slip; if
   off-AC, this task closes the gap that FW10-009 left open.

## Acceptance Criteria

- [ ] **AC-1 (publish-site audit)**: Every outbound publish site in
  `forge.adapters.nats.pipeline_consumer` (and any caller in
  `forge.pipeline.lifecycle_emitter` / `forge.pipeline.publisher`)
  threads `inbound_envelope.correlation_id` into the outbound
  envelope. Document the audited list (publish sites visited) in
  this task body before closing.
- [ ] **AC-2 (path-rejection unit test)**: A unit test publishes an
  inbound `pipeline.build-queued.*` with a known `correlation_id`,
  drives a path-allowlist-rejection (the simplest failure path —
  the one runs 1 and 2 reproduced), and asserts the outbound
  `pipeline.build-failed.*` envelope carries the **same**
  `correlation_id`.
- [ ] **AC-3 (dispatch-failure unit test)**: A second unit test
  asserts the same for the dispatch-failure rejection path — the
  one Gap F010.B is hitting once F010.B closes (and the one any
  future inner-exception-during-dispatch path will hit). Mock the
  dispatcher boundary to raise; assert the outbound
  `pipeline.build-failed.*` envelope carries the inbound
  `correlation_id`.
- [ ] **AC-4 (cross-cut for future stages)**: A parametrized
  fixture (or a single test with multiple assertions) covers any
  per-stage `stage-complete` / `build-started` / `build-complete`
  publish site present today — and is written so any future publish
  site added under `pipeline_consumer` is forced to satisfy the same
  invariant. The contract should not be lost again.
- [ ] **AC-5 (regression)**: Full forge test suite passes
  (`pytest tests/forge/ tests/`). Existing FW10-009 / FW10-010 unit
  tests continue to pass; if they were asserting `correlation_id is
  None` (i.e. testing the bug as the spec), update them to assert
  the correct threaded value.
- [ ] **AC-6 (live wire validation)**: Once landed, re-run jarvis
  runbook §6.2 + §7 against a forge image built from the new commit
  with a known inbound `correlation_id`; confirm the outbound
  `build-failed` envelope carries the same correlation_id end-to-end.
  Capture the rerun correlation_id in this task's completion notes.

## Files Expected to Change

- `src/forge/adapters/nats/pipeline_consumer.py` —
  `_failure_payload` and any other publish sites
- Possibly `src/forge/pipeline/publisher.py` and / or
  `src/forge/pipeline/lifecycle_emitter.py` — if they own the
  envelope construction (publish-site audit will tell us)
- A new or extended test file under `tests/forge/` covering
  AC-2 / AC-3 / AC-4 — likely
  `tests/forge/test_pipeline_consumer_correlation_id.py` or
  extension of an existing `test_pipeline_consumer_*.py` fixture

## Implementation Notes

- **Source of truth for `correlation_id`**: the inbound
  `MessageEnvelope` decoded by the consumer. Do NOT generate a new
  UUID; do NOT fall back to `payload.correlation_id` if the
  envelope-level field is missing — those are different fields per
  DDR-029 and must agree but the envelope-level field is canonical.
- **Threading shape**: the outbound `MessageEnvelope` constructor
  takes `correlation_id` as a kwarg. Pass it explicitly at every
  site rather than relying on a default — defaults are how this bug
  was introduced in the first place.
- **Fixture for AC-4**: write the test so it discovers publish sites
  via grep (or a registry). If a future PR adds a new publish site
  without threading the field, the test should fail. Cheapest
  implementation: a parametrized test that walks the module's
  callable surface and asserts each publish-site result is keyed by
  the input `correlation_id`. If that's too elaborate, a tightly
  scoped lint-style test ("`grep MessageEnvelope( in
  pipeline_consumer.py | grep -v 'correlation_id='` returns empty")
  is acceptable.
- **Don't break the inbound-vs-outbound symmetry test**: if any
  existing fixture asserts `correlation_id is None` because that's
  what today's code produces, those assertions are testing the bug
  as the spec — update them, don't preserve them.

## Ordering vs related tasks

This task is **independent of F010.A and F010.B** — the threading
fix can land alone, in any order, and produces visible improvement
for the path-rejection codepath without depending on the autobuild
chain working. Ordering preference:

1. Land in parallel with F010.A and F010.B.
2. Until F010.B closes, only the failure-path threading is testable
   on the wire (because the success path doesn't reach a publish
   site). Unit tests at AC-2/AC-3/AC-4 don't require F010.B.
3. After F010.B closes, the AC-6 live-wire validation can verify the
   `build-started` and `stage-complete` envelopes too.

## References

- **RESULTS file** (post-FIX-F010 addendum, evening 2026-05-04):
  [`../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
  — see "Gap F010.C — Path-allowlist rejection publishes
  `build-failed` with `correlation_id: null`".
- **TASK-FIX-F010 (production-binding sibling)**:
  [`../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md`](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md)
  — introduced the wrapper that runs the production composer; this
  task patches the publish-side contract that the wired composer
  exposes.
- **TASK-REV-F010 review report**:
  [`../../../.claude/reviews/TASK-REV-F010-review-report.md`](../../../.claude/reviews/TASK-REV-F010-review-report.md)
- **TASK-FW10-009** (validation surface + build-failed paths) — the
  task that should plausibly have wired the threading in
  `_failure_payload`. Audit its ACs to attribute / document the slip.
- **TASK-FW10-010** (pause-resume publish round-trip) — the other
  publish surface FW10 added; cross-check its publish sites against
  the same invariant.
- **DDR-029** — notification-thread contract; the source of truth
  for the rule that lifecycle envelopes must carry the inbound
  `correlation_id`.
- **Source files**:
  - [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py)
    — `_failure_payload` and other publish sites
  - [`src/forge/pipeline/publisher.py`](../../../src/forge/pipeline/publisher.py)
  - [`src/forge/pipeline/lifecycle_emitter.py`](../../../src/forge/pipeline/lifecycle_emitter.py)
  - `nats_core.envelope.MessageEnvelope` (sibling repo
    `nats-core/src/nats_core/envelope.py`)
- **Run that surfaced this**:
  - **correlation_id**: `21df1258-63cb-4e8a-9bef-89234833b68e`
    (run 1); also reproduced as `b5c5e1e2-dd5d-4df9-bc26-a5ec36f6db8f`
    (run 2)
  - **Date**: 2026-05-04 (evening rerun, post-`32b67f8`)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `af62d5c`
  - **Image**: `forge:latest` = sha256 `ebc4311026cc...`
  - **Codepath**: `pipeline_consumer` path-allowlist rejection
    (`failure_reason: "path outside allowlist"`)

## Completion Notes (2026-05-04)

### AC-1 publish-site audit results

| # | Site | File:line | Pre-fix threading? | Action taken |
|---|---|---|---|---|
| 1 | Lifecycle emitter (`emit_started`/`emit_progress`/`emit_failed`/`emit_complete`/`emit_paused`/`emit_resumed`/`emit_cancelled`) | `src/forge/pipeline/__init__.py` (multiple `attach_correlation_id` sites) | ✅ Yes | None — already correct |
| 2 | Central envelope construction in `_publish_envelope` | `src/forge/adapters/nats/pipeline_publisher.py:180` | ✅ Indirect (reads `getattr(payload, "correlation_id", None)`) — works iff upstream attaches | None — pattern preserved |
| 3 | Consumer `_safe_publish_failure` (4 failure call sites) | `src/forge/adapters/nats/pipeline_consumer.py:343,371,393,415` | ❌ **No** | Threaded `envelope.correlation_id` (or `None` for malformed-envelope path) — see §"Files changed" |
| 4 | Production wrapper for `publish_build_failed` | `src/forge/cli/_serve_deps.py:309-348` | ❌ **No** (discarded the field; took only `(payload, feature_id)`) | Added `correlation_id` kwarg + `attach_correlation_id` before delegating |
| 5 | `lifecycle.recovery._handle_preparing` (PREPARING-recovery emit) | `src/forge/lifecycle/recovery.py:266` | ❌ Out of scope (boot-recovery, not consumer-driven) | **Follow-up filed** below — `BuildRow.correlation_id` is available, simple fix |
| 6 | Approval / synthetic-response / queue envelopes | `src/forge/adapters/nats/{approval_publisher,synthetic_response_injector}.py`, `src/forge/cli/queue.py`, `src/forge/gating/wrappers.py` | ✅/N/A — not pipeline.* lifecycle subjects, separate contracts | Out of scope for this task |

The publish-site audit visited every `MessageEnvelope(` call in `src/forge/` (8 hits, of which 2 were the pipeline-publisher-level construction). The bug was confirmed isolated to the consumer→wrapper→publisher chain on rejection paths. The lifecycle emitter was verified already-correct — the `attach_correlation_id` helper at `src/forge/pipeline/__init__.py:224` was added explicitly to bridge v1 payloads (`BuildStartedPayload`/`BuildProgressPayload`/`BuildCompletePayload`/`BuildFailedPayload`) which omit a `correlation_id` field, and is invoked at every `emit_*` site — so build-started, build-progress, build-complete, stage-complete, build-paused, build-resumed, and build-cancelled envelopes already carry the inbound value when published from the running state machine. The only path where the threading was missing is the consumer's pre-state-machine rejection branches, which is exactly what the empirical evidence (`build-failed` with `correlation_id: null` on the path-allowlist rejection codepath) shows.

### Files changed

- `src/forge/adapters/nats/pipeline_consumer.py`
  - `PublishBuildFailed` type alias: now `Callable[..., Awaitable[None]]` with documented `(payload, feature_id, *, correlation_id)` shape.
  - `_safe_publish_failure(...)`: added keyword-only `correlation_id: str | None`.
  - `handle_message`: all four `_safe_publish_failure` call sites updated to pass `correlation_id=envelope.correlation_id` (or `None` for the malformed-envelope path where the envelope itself failed to parse and no source is available).

- `src/forge/cli/_serve_deps.py`
  - `_build_publish_build_failed`: wrapper now takes `correlation_id` kwarg, calls `attach_correlation_id(payload, correlation_id)` before delegating to `publisher.publish_build_failed(payload)` so the existing publisher-level `getattr(payload, "correlation_id", None)` lookup picks up the inbound value.

- `tests/forge/test_pipeline_consumer_correlation_id.py` (new, 7 tests)
  - `TestPathRejectionThreadsCorrelationId` (AC-2): regression test against the empirical `21df1258-…` scenario.
  - `TestProductionWrapperThreadsCorrelationId` (AC-3): two cases — happy-path threading and malformed-envelope `None` no-op.
  - `TestAllRejectionPathsThreadCorrelationId` (AC-4): three behaviour tests (originator rejection, malformed-payload-with-parseable-envelope, malformed-envelope) plus an AST-based lint guard that fails if a future `_safe_publish_failure` call site omits the `correlation_id=` kwarg.

- `tests/cli/test_serve_deps.py`
  - Existing delegation test updated to pass `correlation_id=None` (signature change).
  - New regression test `test_publish_build_failed_threads_correlation_id_onto_envelope` decodes the on-the-wire envelope and asserts `correlation_id == "21df1258-…"`.

### AC results

- **AC-1** ✅ — audit table above.
- **AC-2** ✅ — `test_path_outside_allowlist_threads_inbound_correlation_id` passes.
- **AC-3** ✅ — `TestProductionWrapperThreadsCorrelationId` covers the wrapper roundtrip; the consumer's dispatch-failure path itself does NOT publish `build-failed` (per ADR-ARCH-008, lines 460-462 of `pipeline_consumer.py`) — the running state machine owns that publish via the lifecycle emitter, which already threads `correlation_id` correctly. The wrapper test covers the boundary that *is* exercised in production on rejection. AC-4's lint guard ensures future inner-exception-during-dispatch publish sites (if anyone re-litigates ADR-ARCH-008) cannot regress the invariant.
- **AC-4** ✅ — parametrised cross-cut + AST lint guard.
- **AC-5** ✅ — full `pytest tests/forge/ tests/`: **4244 passed, 3 skipped**. Two failures pre-exist on `main` (verified by `git stash` baseline run): `tests/forge/test_contract_and_seam.py::TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist` (clock-hygiene violation in `approval_subscriber.py:684`, unrelated) and `tests/integration/test_forge_production_image.py::test_forge_serve_arfs_inside_image` (docker-run integration, environment-dependent).
- **AC-6** ⏳ — pending. Live-wire validation requires rebuilding the forge image and re-running jarvis runbook §6.2 + §7. Recommended order: this commit → image rebuild → jarvis FRR rerun. The runbook capture path under `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md` should record the new correlation_id and confirm the outbound envelope carries it.

### Follow-up filed

- `src/forge/lifecycle/recovery.py:266` — the PREPARING-recovery emit at `_handle_preparing` calls `publisher.publish_build_failed(_build_failed_payload(build))` without threading `BuildRow.correlation_id`. Same DDR-029 violation, different surface (boot-recovery rather than consumer-driven). The fix is symmetrical: pass `BuildRow.correlation_id` to `_build_failed_payload` and call `attach_correlation_id` before the publisher call. **Out of scope for this task** per the spec ("publish sites in `forge.adapters.nats.pipeline_consumer`"); should be filed as a separate FRR follow-up if the FRR rerun shows recovery-emitted `build-failed` envelopes also losing the field.
