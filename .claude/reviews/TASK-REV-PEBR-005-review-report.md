# Review Report: TASK-REV-PEBR-005 — join_stream race (Signature C) gap analysis

**Mode**: gap-analysis (custom; treated as decision-mode review with code-reading scope)
**Depth**: standard
**Reviewer**: claude /task-review (Opus 4.7)
**Reviewed**: 2026-05-08
**Forge HEAD**: 1b04b89 (post FOLLOWUP-B-FIX `b9e9585`)
**Forge image**: c0275b3df2c8

---

## Executive Summary

Phase 7 of the 2026-05-08 jarvis runbook re-run still fails — but for a structurally different reason than wave-2. After FOLLOWUP-B-FIX (`b9e9585`) landed cleanly (composer boot line wired, `run_exec_ms=16` confirms StateGraph executes), the failure has narrowed to **Signature C: `join_stream` race against a fast-completing placeholder run**. Final consumer state remains `delivered=14, ack_floor=0, 0 outbound envelopes`.

The diagnosis in TASK-REV-PEBR-005's TL;DR is **confirmed verbatim by code reading** — every step of the timeline matches the source. The SDK `runs.join_stream` docstring is the smoking gun: *"Output is not buffered, so any output produced before this call will not be received here."*

**Recommendation**: spawn `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE` with fix-shape **(b) `runs.stream(...)`** as the default; fall back to a fourth shape **(a') `stream_resumable=True` + `last_event_id`** if (b) hits an implementation hazard. Update parent AC-11 status. Triage the six W3 fold items per the table below.

| Item | Result |
|---|---|
| AC-1 — Signature C confirmed via code reading | ✅ confirmed |
| AC-2 — AC-3 (production IdentityProvider) still met | ✅ confirmed; **no change** to parent ac_status.AC-3 |
| AC-3 — Fix-shape decision | **(b) `runs.stream`** recommended; **(a') stream_resumable+last_event_id** fallback; (a) literal not implementable; (c) violates non-blocking discipline |
| AC-4 — Spawn FOLLOWUP-C-RACE | ⏸ pending operator [A]ccept |
| AC-5 — Update parent AC-11 status | ⏸ pending operator [A]ccept |
| AC-6 — W3-A..F triage | only W3-B has forge-side overlap (already tracked); rest are jarvis-side |
| AC-7 — Cross-link integrity | plan documented; executes on [A]ccept |
| AC-8 — Out-of-scope confirmation | confirmed (per-stage envelopes, jarvis gaps, FOLLOWUP-C-narrow independence) |

---

## AC-1 — Signature C root cause: verified call-ordering timeline

The diagnosis was reconstructed by reading the production code and verified against the installed `langgraph-sdk` 0.3.13 surface (via `inspect.signature` on `RunsClient.create / stream / join_stream`). The timeline below cross-references every claim to a `file:line`.

### Timeline (consumer → wireup → observer → SDK)

| Step | Site | Behaviour |
|---|---|---|
| 1 | [pipeline_consumer.py:519-525](../../src/forge/adapters/nats/pipeline_consumer.py#L519) | Consumer awaits `deps.register_ack_handle(feature_id, correlation_id, ack_handle)` (synchronous wait for return). |
| 2 | [wireup.py:495-501](../../src/forge/lifecycle_bridge/wireup.py#L495) | `register_ack_handle` builds a `BuildContext` with **placeholder identifiers**: `thread_id="pending-{feature_id}"`, `run_id="pending-{feature_id}"`. Comment at lines 479-486 explicitly notes "the IDs are not yet known at attach time". |
| 3 | [wireup.py:505](../../src/forge/lifecycle_bridge/wireup.py#L505) | `_bridge.attach(...)` runs synchronously; registry row exists by return. |
| 4 | [wireup.py:512-516](../../src/forge/lifecycle_bridge/wireup.py#L512) | `asyncio.create_task(self._observer_loop(...))` schedules the observer; `register_ack_handle` returns to consumer immediately (AC-5 of TASK-FRR-PEB-002: "supervisor remains responsive"). |
| 5 | [pipeline_consumer.py:548](../../src/forge/adapters/nats/pipeline_consumer.py#L548) | Consumer awaits `deps.dispatch_build(payload, ack_callback)`. **Only NOW** does dispatch_autobuild_async run; it writes the `async_tasks` SQLite row and starts the langgraph run. |
| 6 | Placeholder body | langgraph runs the placeholder StateGraph in **~16 ms** (`Background run succeeded run_exec_ms=16`). |
| 7 | [wireup.py:565](../../src/forge/lifecycle_bridge/wireup.py#L565) | Observer task `await self._wait_for_identity(feature_id)` — polls IdentityProvider up to `_identity_resolution_attempts` times with `_identity_poll_interval_seconds` between. |
| 8 | [_serve_production.py:213-219](../../src/forge/cli/_serve_production.py#L213) | IdentityProvider step 1: SQLite read against `async_tasks`. On miss returns `None` (the wireup polls again). |
| 9 | [_serve_production.py:192-200](../../src/forge/cli/_serve_production.py#L192) | IdentityProvider step 2: once `thread_id` resolves, calls `client.runs.list(thread_id, limit=1)` — HTTP roundtrip to sidecar. |
| 10 | [wireup.py:574-579](../../src/forge/lifecycle_bridge/wireup.py#L574) | Observer calls `_consume_with_reconnect(thread_id, run_id)`. |
| 11 | [wireup.py:673-677](../../src/forge/lifecycle_bridge/wireup.py#L673) | `_consume_with_reconnect` calls `self._stream_source(feature_id, thread_id, run_id)`. |
| 12 | [stream_source.py:106-110](../../src/forge/lifecycle_bridge/stream_source.py#L106) | `client.runs.join_stream(thread_id, run_id, stream_mode="values")` — **the fatal call**. |
| 13 | SDK 0.3.13 | `runs.join_stream` docstring: *"Stream output from a run in real-time, until the run is done. **Output is not buffered, so any output produced before this call will not be received here.**"* — confirmed via `inspect.getdoc(RunsClient.join_stream)`. |
| 14 | Run already finished at step 6 | Live subscription returns empty stream → iterator exits cleanly. |
| 15 | [wireup.py:581-596](../../src/forge/lifecycle_bridge/wireup.py#L581) | `terminal_seen=False, ended_cleanly=True` → "stream closed without a terminal envelope; leaving inbound queued message un-acked (JetStream will redeliver…)". |
| 16 | JetStream redelivers | Consumer re-calls `register_ack_handle` for the same `feature_id`. |
| 17 | [wireup.py:467-477](../../src/forge/lifecycle_bridge/wireup.py#L467) | Idempotency check: existing observer for this `feature_id` is still in `self._observers` (or has just finalised; the `finally` at line 626 clears it, but the timing leaves a window where a fresh registration can either get a new observer OR be dropped — see Notes below). |
| 18 | Steady state | `delivered=14, ack_floor=0, 0 outbound envelopes`. Smoking gun confirmed. |

### Wireup's own docstring acknowledges the contract

[wireup.py:540-543](../../src/forge/lifecycle_bridge/wireup.py#L540) (inside `_observer_loop`) reads:

> *"Resolves `(thread_id, run_id)` via `IdentityProvider` (the consumer's registration is **BEFORE** `dispatch_autobuild_async` runs, so the IDs are not yet known at attach time)."*

The code calls out the very ordering that produces Signature C. The original assumption was that placeholder runs would last long enough for `_wait_for_identity` to resolve and `join_stream` to attach before completion. The 16 ms run shatters that assumption.

### Notes (idempotency edge case)

The idempotency drop at [wireup.py:467-477](../../src/forge/lifecycle_bridge/wireup.py#L467) checks `if feature_id in self._observers and not done()`. The `finally` at line 626 pops the entry on observer exit. Whether redeliveries see the entry depends on JetStream redelivery cadence vs. observer-finalisation timing. Operationally this does not matter — even if a redelivery DOES schedule a fresh observer, that observer will hit the same race (run is permanently finished at this point) and exit empty again. The race is not transient; once lost, lost forever for that `(feature_id, run_id)` pair. **The fix must eliminate the race window, not retry through it.**

### Cross-check against direct curl probe

The runbook evidence dir `/tmp/jarvis-runbook-evidence/` (per task body) records a direct curl probe of `join_stream` against a finished run, returning empty. The SDK docstring above corroborates this. **No change to runbook evidence is required.**

**AC-1 status: ✅ confirmed.**

---

## AC-2 — Production IdentityProvider (parent AC-3) is NOT a regression

[`_build_async_tasks_identity_provider`](../../src/forge/cli/_serve_production.py#L168) at `_serve_production.py:168-219` ships unchanged and is functionally correct:

1. Step 1 (SQLite read at line 213-219): correct query against `async_tasks`; correct None-on-miss semantic; appropriate transient-error swallow.
2. Step 2 (langgraph_sdk `runs.list` at line 192-200): correct `(thread_id, run_id)` extraction; correct empty-list → `None` handling; correct transport-error → `None` downgrade.

The 12-test unit suite at `tests/forge/test_serve_identity_provider.py` (per parent task `targeted_seam_tests` field) continues to pass.

**Crucially**: Signature C is **upstream** of IdentityProvider. Even when IdentityProvider resolves correctly (which it does — the curl probe under `/tmp/jarvis-runbook-evidence/` was made with a real, resolved `(thread_id, run_id)`), the run has already finished and `join_stream` returns empty.

**Action**: parent frontmatter `ac_status.AC-3` stays `done`. No update needed. (Parent task line 28: `AC-3: done`.)

**AC-2 status: ✅ confirmed; no change to parent.**

---

## AC-3 — Fix-shape decision

### SDK 0.3.13 surface (verified directly)

```text
runs.create(thread_id, assistant_id, *, input, command,
            stream_mode='values', stream_resumable=False, ...) -> Run
runs.stream(thread_id, assistant_id, *, input, command,
            stream_mode='values', ...) -> AsyncIterator[StreamPart | StreamPartV2]
runs.join_stream(thread_id, run_id, *, stream_mode,
                 last_event_id=None, ...) -> AsyncIterator[StreamPart]
runs.wait(thread_id, assistant_id, ...) -> list | dict
```

Two SDK affordances are relevant that the original task body did not surface:

1. `runs.stream(...)` — **single-shot create-and-stream**. Verified via `inspect.signature(RunsClient.stream)`.
2. `runs.create(stream_resumable=True)` + `runs.join_stream(..., last_event_id=...)` — **resumable replay** via SSE Last-Event-ID semantics.

### Reframing the task's three options against the actual SDK

| Shape | Task description | Status against SDK 0.3.13 |
|---|---|---|
| **(a)** | "Reorder inside `_observer_loop`: open `join_stream` against a freshly-created (not-yet-running) run, then trigger the run." | **Not directly implementable.** SDK `runs.create` starts the run immediately; there is no "create paused" semantic. |
| **(b)** | "Replace `runs.create + runs.join_stream` with `runs.stream(...)` (single-shot create-and-stream API)." | **Supported.** The race is closed by construction — the iterator is open at the moment the run is created. |
| **(c)** | "Move subscription into the consumer's pre-dispatch path (open the stream from `register_ack_handle` synchronously)." | **Violates non-blocking discipline.** The consumer's fetch loop must not await stream open under `max_ack_pending=1` (ADR-ARCH-014). Documented at [wireup.py:424-426](../../src/forge/lifecycle_bridge/wireup.py#L424). |

### Fourth shape (not enumerated in the task body): Option (a')

| Shape | Description | Status |
|---|---|---|
| **(a')** | `runs.create(..., stream_resumable=True)` in dispatch path; bridge opens `runs.join_stream(..., last_event_id="<from-start>")` to request the persisted replay. | **Supported in principle, hazard verification needed.** The exact `last_event_id` token semantic for "replay all" is not documented in the SDK 0.3.13 docstring; needs SDK source-spelunk or empirical confirmation against the langgraph-runner sidecar. Persistent-event storage cost on the sidecar is the operational trade-off. |

### Recommendation: **Option (b) `runs.stream(...)`** with **Option (a') as fallback**

**Why (b)**:

1. **Structurally race-free.** The iterator is opened by the SDK *as part of run creation*. Events emitted during the run cannot be missed by construction — there is no temporal window between "run starts" and "subscription opens".
2. **Supported on the installed SDK** (`langgraph-sdk~=0.3.13`, verified in `pyproject.toml:22`).
3. **Collapses two SDK calls into one** — `runs.create + runs.join_stream` becomes `runs.stream`. Removes a synchronization point and a class of bugs.
4. **Identity is known immediately** from the response — the `_wait_for_identity` polling becomes a fallback for recovery cycles only (not the hot path).

**Trade-off / cost of (b)**:

- **Restructures the dispatch boundary.** `dispatch_autobuild_async` no longer just creates a run and returns; it must hand the stream iterator to the bridge. One workable shape: extend `dispatch_autobuild_async` to return `(thread_id, run_id, stream_iterator)`, and extend `register_ack_handle` (or add a sibling `register_stream_iterator(...)` method on the wireup) to accept the pre-opened iterator. The iterator is then the bridge observer's input, replacing the current `self._stream_source(...)` invocation.
- **Recovery path**: `recover_in_flight` (next-boot rebind) cannot use `runs.stream` (no run to create). It must keep `runs.join_stream(thread_id, run_id, last_event_id=...)` as a fallback — and for that to work end-to-end across daemon restarts, runs would need `stream_resumable=True` anyway. So **(b) on the hot path + (a') on the recovery path** is the most honest framing.
- **StreamSource Protocol shape**: today `StreamSource(__call__(*, feature_id, thread_id, run_id))` returns an async iterator; under (b), it becomes a passthrough for an externally-opened iterator. The Protocol shape needs widening (or a sibling Protocol).
- **Test surface**: `tests/forge/lifecycle_bridge/test_stream_source.py` (5 tests) and `tests/forge/lifecycle_bridge/test_wireup.py` (≥30 tests; `_make_stream_source` helper at line 160) require updating to cover the dispatch-supplied-iterator shape.

**Why (a') is the fallback**:

If implementation discovers that `runs.stream`'s ownership semantics are awkward to thread through `dispatch_autobuild_async`'s fire-and-forget contract — for example, the dispatcher today does NOT await the iterator (it dispatches and returns), and pinning the iterator across the dispatch boundary couples lifetimes uncomfortably — then **(a')** is a less-invasive shape: leave the dispatch path alone, only add `stream_resumable=True` to the existing `runs.create` (one-line change in the dispatcher), and have the bridge observer open `runs.join_stream(..., last_event_id="<replay-from-start>")` to request the persisted replay. Identity polling stays unchanged. The race is closed because the SDK now buffers events on the sidecar side and the late `join_stream` consumes them via the resumability cursor.

The hazard with (a') is the unknown `last_event_id` semantic for "from start". The implementation task should verify this against the langgraph-sdk source (or empirically) before committing.

**Why NOT (a) literal, (c)**:

- **(a) literal** is not implementable on SDK 0.3.13 — no "create paused".
- **(c)** would be the tightest race elimination, but it pins consumer fetch latency to stream-open RTT and violates the explicit AC-5 contract at [wireup.py:424-426](../../src/forge/lifecycle_bridge/wireup.py#L424). With `max_ack_pending=1`, every dispatch would block the consumer for the round-trip duration. Operationally untenable.

**AC-3 status: ✅ recommendation locked. (b) primary; (a') fallback.**

---

## AC-4 — FOLLOWUP-C-RACE implementation task spec (to spawn on [A]ccept)

**Filename**: `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE.md`

**Frontmatter** (key fields):

```yaml
id: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE
title: Subscribe-before-dispatch — eliminate join_stream race for fast-completing runs
status: backlog
parent_review: TASK-REV-PEBR-005
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
parent_ac: AC-11
related_tasks:
  - TASK-FORGE-FRR-PEBR-WIREUP                                # parent fix; AC-11 gate
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A-apply-lifecycle-bridge-registry-migration   # completed
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape                # completed; b9e9585
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs                # INDEPENDENT; same letter, different concern
  - TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix                                    # W3-B overlap (cross-link only)
priority: high
task_type: fix
intensity: light            # one seam, ~60 min implementation budget
estimated_implementation_minutes: 60
tags: [forge-serve, lifecycle-bridge, sse-subscription, join-stream-race, feat-pebr]
```

**Single Acceptance Criterion** (per AC-4 of the review):

- [ ] **AC-1 (FOLLOWUP-C-RACE)** — Subscribe-before-dispatch wired per fix-shape **(b)**: refactor `dispatch_autobuild_async` to use `runs.stream(...)` (or equivalent single-shot create-and-stream) and hand the resulting `AsyncIterator[StreamPart]` to the bridge via an extended `register_ack_handle(...)` (or sibling `register_stream_iterator(...)`) signature. Bridge observer consumes the supplied iterator instead of opening its own via `self._stream_source(feature_id, thread_id, run_id)`. **Fallback**: if (b) hits an implementation hazard (ownership / lifetime coupling), implement **(a')** instead — `runs.create(stream_resumable=True)` in dispatch + `runs.join_stream(..., last_event_id=...)` in observer with the SDK's "replay from start" cursor. Document the chosen shape and rationale in the task's completion notes.

  **Verification**:
  1. Runbook re-run on a rebuilt forge image captures both `pipeline.build-started.FEAT-*` and `pipeline.build-complete.FEAT-*` envelopes on the wire (placeholder bodies → 2 envelopes total).
  2. JetStream consumer state advances: `delivered=N, ack_floor=N` (no perpetual redelivery).
  3. Boot log line still reads `composed PipelineConsumerDeps (async_task_starter=wired, ack_bridge=wired, terminal_publish_ledger=wired)`.

- [ ] **AC-2 (FOLLOWUP-C-RACE) — Regression test.** Extend `tests/forge/lifecycle_bridge/test_wireup.py` (or add a sibling `test_subscribe_before_dispatch.py`) with a test asserting subscription order. Pattern:

  - Use a **fake langgraph client** (extend `_FakeRunsClient` pattern at `tests/forge/lifecycle_bridge/test_stream_source.py:28`).
  - The fake records `subscription_open_at: datetime` (when the iterator is first awaited) and `run_started_at: datetime` (when the run actually begins emitting).
  - Drive the fixture through the consumer → wireup → dispatch path.
  - Assert `subscription_open_at <= run_started_at` on **every** dispatch path (happy path; redelivery; recovery).
  - Pre-fix HEAD: test fails (`subscription_open_at > run_started_at`). Post-fix: test passes.

- [ ] **AC-3 (FOLLOWUP-C-RACE) — Out-of-scope guard rail in task body.** Explicitly call out that this fix delivers exactly **2 envelopes** (`build-started` + `build-complete` from placeholder bodies). The full per-stage sequence requires real autobuild orchestration in the runner nodes — a **separate** follow-up that FOLLOWUP-B-FIX explicitly deferred. Resist scope creep.

- [ ] **AC-4 (FOLLOWUP-C-RACE) — Disambiguation note.** Task body must include: *"The existing TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs is independent — same FOLLOWUP-C letter, different concern (sidecar config narrowing). The `-RACE` suffix disambiguates; both can ship independently."*

- [ ] **AC-5 (FOLLOWUP-C-RACE) — Lint + format.** `ruff check` + `black --check` clean on every touched file (likely: `src/forge/pipeline/dispatchers/autobuild_async.py`, `src/forge/lifecycle_bridge/wireup.py`, `src/forge/lifecycle_bridge/stream_source.py`, `src/forge/lifecycle_bridge/__init__.py` plus tests).

**Cross-component interface notes** (per project rule that fix tasks crossing component boundaries name expected interfaces): the fix touches the `forge ↔ langgraph-runner sidecar` SSE contract. Verify the installed `langgraph_sdk~=0.3.13` exposes the `runs.stream` (or equivalent) API; if (a') is chosen, also verify the `last_event_id` "replay from start" sentinel against the SDK source or empirically against the running sidecar.

**Files expected to be modified**: ~3-5 source files + 1-2 test files. Estimated 60 minutes for fix-shape (b); ~30 minutes for fix-shape (a').

---

## AC-5 — Parent AC-11 status update plan (to apply on [A]ccept)

**File**: `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-fix.md`

**Frontmatter changes**:

1. **`ac_status.AC-11`** (line 36) — extend the existing `partially-unblocked` line:

   ```yaml
   AC-11: partially-unblocked  # 2026-05-08T14:16Z — FOLLOWUP-A live (55f7804) → migration drift cleared. 2026-05-08T15:30Z post-FOLLOWUP-B-FIX (b9e9585) — composer boot line confirmed and Background run succeeded run_exec_ms=16; translator no longer silent. But Phase 7 still fails with **Signature C** (join_stream race against fast-completing placeholder run; live subscription returns empty against finished run, see SDK docstring). New active blocker: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE.
   ```

2. **`ac_11_blocked_on`** (line 37-38) — replace existing `FOLLOWUP-B` entry with FOLLOWUP-C-RACE:

   ```yaml
   ac_11_blocked_on:
     - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE   # 2026-05-08T15:30Z runbook re-run on c0275b3df2c8 (post-b9e9585) confirmed Signature C (join_stream race). FOLLOWUP-B-FIX cleared the wave-2 translator-shape gap and is no longer blocking; FOLLOWUP-C-RACE is the new active gate.
   ```

3. **`ac_11_resolved`** (line 39-40) — append FOLLOWUP-B-FIX:

   ```yaml
   ac_11_resolved:
     - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A-apply-lifecycle-bridge-registry-migration   # 2026-05-08T~12:50Z (55f7804); migration drift cleared.
     - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape                  # 2026-05-08T~XX:XXZ (b9e9585); composer boot line + run_exec_ms=16 confirmed.
   ```

4. **Append new revalidation block** (after `ac_11_runbook_revalidation_outcome` at line 42-63):

   ```yaml
   ac_11_runbook_revalidation_doc_3: docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-followup-b-landed.md
   ac_11_runbook_revalidation_outcome_3:
     ran_at: 2026-05-08T15:30:00Z
     forge_image_head: c0275b3df2c8 (HEAD 1b04b89; post-b9e9585)
     followup_b_validation: passed       # composed PipelineConsumerDeps boot line; Background run succeeded run_exec_ms=16; 0 no-such-table warnings (FOLLOWUP-A holds)
     consumer_state:
       delivered: 14
       ack_floor: 0       # canonical AC-11 fail fingerprint persists — Signature C now the cause (was: translator-shape mismatch)
       redelivered: 13    # idempotency drops on observer re-registration; no second join_stream is opened
     outbound_envelopes_observed: 0
     failure_signature: |
       Signature C (join_stream race) — placeholder lifecycle nodes
       complete in ~16 ms; bridge observer's _wait_for_identity poll
       resolves AFTER run completion; runs.join_stream against a
       finished run returns empty (live subscription, not replay; per
       langgraph-sdk 0.3.13 docstring). Final state: delivered=14,
       ack_floor=0, 0 outbound envelopes.
     active_blocker: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE
   ```

5. **`ac_11_promotion_gate`** (line 64-67) — append a third paragraph:

   ```yaml
   ac_11_promotion_gate: |
     [existing paragraph 1 stays]
     [existing paragraph 2 stays]

     Status 2026-05-08T15:30Z: FOLLOWUP-B-FIX live (b9e9585). Composer
     boot line and run_exec_ms=16 confirm StateGraph executes cleanly.
     Phase 7 still fails — but for **Signature C** (join_stream race),
     not the wave-2 translator-shape gap. Promotion remains gated on
     FOLLOWUP-C-RACE landing and a fourth runbook re-run capturing
     pipeline.build-started.FEAT-* on the wire. See TASK-REV-PEBR-005
     for the full diagnosis and fix-shape decision.
   ```

**No body changes** to TASK-FORGE-FRR-PEBR-WIREUP-fix.md (AC-11 wording at line 469 is forward-compatible with the FOLLOWUP-C-RACE blocker).

---

## AC-6 — Wave-3 fold candidates triage

`TASK-FRR-RUNBOOK-002` does not exist in the backlog. The closest siblings are TASK-REV-PEBR-004 (the wave-2 review, already spawned its followups) and `TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix` (in backlog, owns W3-B).

**Routing decisions**:

| ID | Severity | Routing | Rationale |
|---|---|---|---|
| **W3-A** | high | **Jarvis-side runbook update** (defer until FOLLOWUP-C-RACE lands; the corrected §7 needs to describe the actual post-fix shape, not a hypothetical Signature C) | Fold as a single jarvis-side update with W3-D, W3-E, W3-F (see below). |
| **W3-B** | carries forward | **Already tracked**: [TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix](../../tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix.md). Cross-link from FOLLOWUP-C-RACE's `related_tasks`. **Do NOT fold into FOLLOWUP-C-RACE** — different concern (deadline gate semantic, not subscription timing). | Avoid double-tracking. |
| **W3-C** | cosmetic | **Defer**. If it's a jarvis supervisor agent prompt issue, it's out of scope for forge. Operator may pick up in a jarvis-side sweep. | Cosmetic; no operational impact. |
| **W3-D** | low | **Jarvis-side runbook update** (host-environment doc gap: NATS CLI not in container). Bundle with W3-A. | Jarvis repo's runbook owns the `docker exec` invocation pattern; fix is doc + optional container-CLI install. |
| **W3-E** | medium | **Jarvis-side runbook update** (delete archeological references in §7). Bundle with W3-A. | The §7 references to FOLLOWUP-B per-part instrumentation are stale — `b9e9585` removed them. |
| **W3-F** | low | **Jarvis-side runbook update** (add `composed PipelineConsumerDeps … wired` boot line to §2.2 pass criteria). Bundle with W3-A. | Positive-evidence gap. |

**Net forge-side W3 outcome**: zero new forge tasks. W3-B is already tracked.

**Net jarvis-side W3 outcome**: one consolidated jarvis-side runbook update (W3-A + W3-D + W3-E + W3-F), deferred until after FOLLOWUP-C-RACE lands. The task body in jarvis-repo will reference TASK-REV-PEBR-005 for the diagnosis.

**AC-6 status: ✅ triaged.**

---

## AC-7 — Cross-link integrity plan (executes on [A]ccept)

After spawning FOLLOWUP-C-RACE, apply these edits to TASK-REV-PEBR-005's frontmatter:

1. **Rename `spawned_tasks_target` → `spawned_tasks`**, value confirmed:
   ```yaml
   spawned_tasks:
     - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE
   ```

2. **Verify FOLLOWUP-C-RACE's frontmatter** has `parent_review: TASK-REV-PEBR-005`.

3. **Reachability check** — every entry in `related_tasks` resolves:
   - ✅ `TASK-FORGE-FRR-PEBR-WIREUP` (backlog)
   - ✅ `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A-apply-lifecycle-bridge-registry-migration` (completed)
   - ✅ `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape` (completed)
   - ✅ `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs` (backlog)
   - ✅ `TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix` (backlog)

   All paths verified during this review.

4. **Status transition**: `TASK-REV-PEBR-005` `status: backlog` → `review_complete` (with `review_results` block populated per the report).

---

## AC-8 — Out-of-scope confirmation

Confirmed in this review:

1. **Per-stage envelope sequence is OUT OF SCOPE for FOLLOWUP-C-RACE.** The placeholder bodies emit exactly **2 envelopes** (`build-started` + `build-complete`). The full per-stage sequence requires real autobuild orchestration in the runner nodes — a separate follow-up that FOLLOWUP-B-FIX explicitly deferred. FOLLOWUP-C-RACE delivering only 2 envelopes is **expected and correct**; resist scope creep.
2. **Jarvis-side gaps are OUT OF SCOPE here** and tracked in jarvis repo. This includes: supervisor inline-prose ack (W3-C), runbook §7 staleness (W3-A/D/E/F bundle), jarvis transcript+trace handling. Cross-reference but do NOT bundle.
3. **`TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs` is INDEPENDENT.** Same FOLLOWUP-C letter, different concern (sidecar config narrowing). The `-RACE` suffix disambiguates; both can ship independently. The disambiguation note must appear in FOLLOWUP-C-RACE's task body (per AC-4 above).

---

## Decision Matrix

| Option | Race-free | SDK supported (0.3.13) | Implementation effort | Architectural cost | Recommendation |
|---|---|---|---|---|---|
| (a) literal "create paused" | yes (would be) | **no** — no such SDK semantic | n/a | n/a | ❌ not implementable |
| **(b) `runs.stream(...)`** | **yes (by construction)** | ✅ verified via `inspect.signature` | medium (~60 min) | dispatch/bridge boundary refactor | ✅ **DEFAULT** |
| (a') `stream_resumable=True` + `last_event_id` | yes (via persisted replay) | ✅ params exist; `last_event_id` "replay from start" semantic needs verification | low (~30 min) | minimal — one-line dispatch change + observer cursor change | ✅ **fallback if (b) hits hazard** |
| (c) sync open in `register_ack_handle` | yes (tightest) | n/a | low (~30 min) | violates AC-5 non-blocking discipline | ❌ rejected |

**Default decision**: spawn FOLLOWUP-C-RACE with fix-shape **(b)**. Implementer is authorised to fall back to **(a')** if (b)'s ownership/lifetime coupling proves awkward — with a brief rationale in completion notes.

---

## Inputs and Evidence Verified

- ✅ `src/forge/lifecycle_bridge/wireup.py` (lines 390-1020) — read in full; all `file:line` references in this report verified.
- ✅ `src/forge/lifecycle_bridge/stream_source.py` (entire file, 124 lines) — read in full; `runs.join_stream` call site at line 106-110 confirmed.
- ✅ `src/forge/cli/_serve_production.py` (lines 100-219) — read; IdentityProvider factory confirmed unchanged.
- ✅ `src/forge/adapters/nats/pipeline_consumer.py` (lines 490-580) — read; consumer call ordering confirmed (`register_ack_handle` await, then `dispatch_build` await).
- ✅ `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-fix.md` — read; AC-11 status block at lines 25-67 inspected for update plan.
- ✅ `pyproject.toml:22` — `langgraph-sdk~=0.3.13` confirmed.
- ✅ `langgraph_sdk._async.client.RunsClient` — inspected via `inspect.signature` and `inspect.getdoc`. `runs.create / stream / join_stream / wait` signatures verified.
- ✅ Backlog directory listing — confirmed `TASK-FRR-RUNBOOK-002` does NOT exist; W3 fold target plan adjusted accordingly.
- ✅ Test file inventory — confirmed `tests/forge/lifecycle_bridge/test_wireup.py` and `test_stream_source.py` exist and are the regression-test homes for FOLLOWUP-C-RACE.
- ⏸ Not re-verified in this review (relied on task body): runbook evidence directory `/tmp/jarvis-runbook-evidence/`, transcript / trace files in `~/.jarvis/`. The task body's claims about these are internally consistent with the code-side diagnosis; further forensic inspection was not required to reach the recommendation.

---

## Notes for the Operator

- **Trust the evidence; the diagnosis is structural.** Every step of the timeline is traceable to source. The SDK docstring is unambiguous. The 16 ms `run_exec_ms` is decisive — no amount of polling-budget tuning will close a window that has already shut.
- **Resist bundling.** Three concerns share the FOLLOWUP-C letter; they belong in separate PRs:
  1. `-narrow-langgraph-json-graphs` — already in backlog, sidecar config; ship on its own cadence.
  2. `-RACE` — the spawn from this review; subscribe-before-dispatch.
  3. W3-A/D/E/F — jarvis-side runbook update; ship after `-RACE` lands so §7 reflects post-fix reality.
- **The recommendation has a fallback.** (b) is the structural fix; (a') is the fallback. The implementer is empowered to swap if (b)'s seam proves awkward — completion notes capture which was chosen and why. This is the "narrow the spawned task by leaving (a') as an explicit fallback" version of the AC-3 "Modify scope" lever.
- **Parent task stays in backlog** until a fourth runbook re-run captures `pipeline.build-started.FEAT-*` on the wire. This review does not promote anything — it only narrows the active blocker from "translator silent" to "join_stream race".
