---
id: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B
title: Spike — trace silent SSE→envelope translator (placeholder thread_id rebind vs. translator shape mismatch)
status: completed
created: 2026-05-08T11:30:00Z
updated: 2026-05-08T16:00:00Z
completed: 2026-05-08T16:00:00Z
previous_state: in_progress
state_transition_reason: "/task-complete — exit branch (b) confirmed via instrumented run; FIX task (FOLLOWUP-B-FIX, Option B1) landed at b9e9585 and completed at a35c03d; AC-5 instrumentation revert already shipped; AC-6 handover artefact at /tmp/runbook-evidence-FOLLOWUP-B/SPIKE-OUTCOME.md. Independent deadline-timer-publish bug filed as TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH (still backlog)."
completed_location: tasks/completed/forge-autobuild-runner-pipeline-emitter-bridge/
spike_outcome:
  exit_branch: b
  ac1_satisfied: true   # repro evidenced from existing wave-2 dry-run + fresh instrumented run
  ac2_satisfied: true   # Path 1 instrumentation captured (identity-resolved log, real thread_id/run_id)
  ac3_satisfied: true   # Path 2 instrumentation captured (parts_received=30, event_types={'values'}, data_keys=[files,messages,todos] — no async_tasks key)
  ac4_satisfied: true   # exit branch (b) chosen; FOLLOWUP-B-FIX spawned + completed (b9e9585, a35c03d)
  ac5_satisfied: true   # FOLLOWUP-B trace instrumentation reverted before FIX work began
  ac6_satisfied: true   # SPIKE-OUTCOME.md written to /tmp/runbook-evidence-FOLLOWUP-B/
  follow_up_filed: TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH  # independent deadline-publish bug surfaced by spike
priority: high
task_type: spike
parent_review: TASK-REV-PEBR-004
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
parent_feature: FEAT-PEBR
unblocks_parent_ac: TASK-FORGE-FRR-PEBR-WIREUP::AC-11
depends_on:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A   # need the registry table to exist before the bridge can attach for tracing
related_tasks:
  - TASK-FRR-PEB-003          # SSE→envelope translator under investigation
  - TASK-FRR-PEB-009          # published_lifecycles cursor (related state)
complexity: 4
estimated_minutes: 90
implementation_mode: direct
wave: 2
intensity: light
intensity_reason: provenance=parent_review (TASK-REV-PEBR-004), complexity=4, time-boxed spike with two well-defined exit branches
tags:
  - forge-serve
  - lifecycle-bridge
  - sse-translation
  - identity-provider
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - investigation-spike
discovered_during: TASK-REV-PEBR-004 (jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001 post-PEBR-WIREUP revalidation, 2026-05-08)
forge_head_at_discovery: 1b82236
time_box_minutes: 90
spike_evidence_dir: /tmp/runbook-evidence-FOLLOWUP-B/
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Spike FOLLOWUP-B — trace the silent SSE→envelope translator

## TL;DR

After FOLLOWUP-A's hot-fix on the rebuilt image, the bridge attached cleanly:

```
forge.lifecycle_bridge.bridge: lifecycle_bridge.attach feature_id=FEAT-43DE thread_id=… run_id=…; observer task scheduled
forge.lifecycle_bridge.stream_source: SSE GET 200 thread_id=… run_id=…
```

But **zero outbound** `pipeline.*` envelopes reached JetStream, even though the deepagents tool loop ran for 13+ minutes against llama-swap. Two genuinely-distinct candidates remain. This is a **time-boxed spike**: instrument with logging, capture the wire trace, then decide between fix-here vs. spawn-followup-fix.

## Why

[TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11 also requires a real `pipeline.build-started.FEAT-*` envelope to flow before promotion to `completed/`. FOLLOWUP-A unblocks the bridge's attach path; FOLLOWUP-B unblocks the publish path. Without it, AC-11 stays open even on a clean migration.

The two candidate root causes (carried over from the parent review's AC-2):

### Path 1 — placeholder `thread_id` rebind

`lifecycle_bridge.attach` runs on `pipeline.build-queued.FEAT-43DE` BEFORE the dispatcher writes the real `task_id` to the `async_tasks` mirror. The wireup's `register_ack_handle` runs before `dispatch_autobuild_async`, so the `IdentityProvider` is polled with `feature_id=FEAT-43DE` and (per parent task's AC-3 wiring) returns `None` until the dispatcher writes the row, then resolves `(thread_id, run_id)`. If the resolved tuple is *never* re-queried after dispatch, the bridge could be polling a stale placeholder and `runs.join_stream` would target a non-existent run.

### Path 2 — translator state-update shape mismatch

The bridge logged `observer task scheduled` and `SSE GET 200`, so the stream IS open. But [TASK-FRR-PEB-003](../../completed/TASK-FRR-PEB-003-sse-to-envelope-translation.md) defines what `StreamPart` shapes the translator (`forge.lifecycle_bridge.translation._translate_event`) recognises. `autobuild_runner._update_state` in [src/forge/subagents/autobuild_runner.py](../../../src/forge/subagents/autobuild_runner.py) emits `Command(update={...})` whose payload shape may not match what the translator's switch is looking for — every event arrives, every event is dropped silently.

## Acceptance Criteria

- [ ] **AC-1** — **Repro on local rebuild.** Stand up a local forge build with FOLLOWUP-A applied, attach to jarvis (or use the runbook's wire-tap fixture from `/tmp/jarvis-runbook-evidence/`), trigger one `queue_build`. Confirm the bridge attaches and the SSE GET returns 200 — i.e. reproduce the silent-translator state.

- [ ] **AC-2** — **Path 1 instrumentation.** Add temporary trace-logging at:
  - Every `IdentityProvider.__call__` (or whatever the production factory's invocation seam is — see [_serve_production.py `_build_async_tasks_identity_provider`](../../../src/forge/cli/_serve_production.py)) — log the `feature_id` queried, the resolved `(thread_id, run_id)`, and whether the `async_tasks` row was found.
  - Every `langgraph_sdk.client.runs.join_stream(...)` call in `forge.lifecycle_bridge.stream_source` — log the `thread_id` and `run_id` arguments at call time.
  - Every `register_ack_handle` invocation — log the `feature_id` it was given and the `(thread_id, run_id)` it tried to resolve.
  Capture one full build's worth of these logs to `${spike_evidence_dir}/path1-trace.log`.

- [ ] **AC-3** — **Path 2 instrumentation.** Add temporary trace-logging at:
  - Every event yielded by `runs.join_stream` (the raw `StreamPart`) — log the event type and payload to `${spike_evidence_dir}/path2-stream-events.jsonl`.
  - Every entry to `forge.lifecycle_bridge.translation._translate_event` — log the input event and which branch (if any) of the switch matched.
  - Every `autobuild_runner._update_state` `Command(update=...)` payload at emit time — log to `${spike_evidence_dir}/path2-update-state.jsonl`.
  Cross-reference to determine whether events are arriving and being silently dropped, or never arriving at all.

- [ ] **AC-4** — **Diagnosis & exit.** Within the 90-minute time-box, capture enough evidence to declare ONE of:
  - **(a) Path 1 confirmed** — placeholder `thread_id` is being polled forever; `runs.join_stream` targets a non-existent run; the SSE GET 200 is on the placeholder thread, not the real one. Spawn `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX` with the targeted rebind fix (re-poll IdentityProvider after dispatch returns the real `task_id`, or restructure the wireup to register-after-dispatch).
  - **(b) Path 2 confirmed** — events arrive on the real run, but `_translate_event` drops them. Spawn `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX` with a translator shape contract update (extend the switch, or repair the autobuild_runner emit shape). Hand back to FEAT-PEBR if the contract change is wider than a localised fix.
  - **(c) Neither path** — capture the actual root cause in a 1-paragraph write-up; spawn a re-scoped followup task with the new diagnosis.
  - **(d) Time-box exhausted with no clear root cause** — capture all logs to `${spike_evidence_dir}/`, document the dead ends in the task's "Notes for the Next Spike" section, hand off to the next operator session.

- [ ] **AC-5** — **Trace cleanup.** After the diagnosis is captured, REMOVE all temporary trace-logging from this spike. The fix task spawned at AC-4 brings its own production-grade observability (or none, depending on the fix shape). Do NOT leave stop-gap loggers in production code.

- [ ] **AC-6** — **Handover artefact.** All trace logs persisted to `${spike_evidence_dir}/` (the `/tmp/runbook-evidence-FOLLOWUP-B/` path declared in frontmatter). On AC-4 exit, write a short `${spike_evidence_dir}/SPIKE-OUTCOME.md` that names the chosen exit branch, links to the spawned fix task, and points to the evidence files used.

## Time-Box

**90 minutes** of tracing on a local rebuild before escalating. If the time-box is exhausted without a confirmed exit, take exit (d) — do NOT keep instrumenting past the box. Hand off cleanly.

## Implementation Notes

- **Spike, not fix.** This task explicitly does NOT fix the bug. It investigates, captures evidence, and spawns a fix task. Resist the temptation to apply a one-line guess-fix during the spike — the cost of being wrong is a redeploy, and the runbook revalidation is expensive.

- **Trace evidence > log statements.** Prefer structured trace files (jsonl, .log) over scattered print statements; the next operator (or the AC-4 fix-task implementer) needs to be able to read the evidence cold.

- **Don't bundle Path 1 + Path 2.** Even if both turn out to need fixing, spawn two separate fix tasks rather than one combined task. The blast radii are independent (one is a wireup ordering bug; the other is a contract bug between two modules).

- **Out of scope.** Do NOT modify `_translate_event`'s logic, do NOT modify `autobuild_runner._update_state`'s emit shape, do NOT modify the wireup ordering. All such changes belong in the spawned fix task.

## Inputs / Evidence

- **Parent review**: [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — scoping for the two candidate paths.
- **Bridge skeleton**: [src/forge/lifecycle_bridge/bridge.py](../../../src/forge/lifecycle_bridge/bridge.py)
- **Translator**: [src/forge/lifecycle_bridge/translation.py](../../../src/forge/lifecycle_bridge/translation.py)
- **Stream source**: [src/forge/lifecycle_bridge/stream_source.py](../../../src/forge/lifecycle_bridge/stream_source.py)
- **Wireup**: [src/forge/lifecycle_bridge/wireup.py](../../../src/forge/lifecycle_bridge/wireup.py)
- **IdentityProvider factory**: `_build_async_tasks_identity_provider` in [src/forge/cli/_serve_production.py](../../../src/forge/cli/_serve_production.py)
- **autobuild_runner emit shape**: [src/forge/subagents/autobuild_runner.py](../../../src/forge/subagents/autobuild_runner.py)
- **Translation contract test**: `tests/forge/lifecycle_bridge/test_translation_contract.py`
- **Operator evidence (post-FOLLOWUP-A hot-fix)**: `/tmp/jarvis-runbook-evidence/` — wire-tap, forge logs, sidecar logs, stream/consumer info pre/post hot-fix.

## References

- [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — parent review.
- [TASK-FRR-PEB-003](../../completed/TASK-FRR-PEB-003-sse-to-envelope-translation.md) — translator definition; Path 2's contract source.
- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix's AC-3 (IdentityProvider wiring) and AC-11 (deferred runbook validation).

## Spike progress (2026-05-08)

### Static-analysis pre-spike (assistant-driven, 2026-05-08 ~13:00 BST)

Read `/tmp/jarvis-runbook-evidence-dryrun-20260508-120044/` (dry-run wave-2
results: 30-min forge-prod log, 254 bridge-attach lines, 15 observer-loop
"stream ended without terminal envelope" warnings, zero outbound envelopes,
ack_floor=11 stuck) plus the lifecycle_bridge source (`bridge.py`,
`wireup.py`, `stream_source.py`, `translation.py`) and the
`_build_async_tasks_identity_provider` factory in `_serve_production.py`.

Findings:

- **AC-1 already evidenced** at 30-min scale by the operator's wave-2
  dry-run; no need to re-trigger to prove silence.
- **The spike's frontmatter Path 1 ("never re-queried after dispatch") is
  wrong as stated.** `wireup._observer_loop` (`wireup.py:565`) re-polls
  `IdentityProvider` exactly once before opening the stream.
- **Hypothesis P1' (identity-unresolved exit) is ruled out** by the
  dry-run: observers ARE getting past identity resolution (we see the
  "stream ended" warning, which only fires post-identity).
- **Remaining hypothesis space is binary**:
  - **P1''** — `IdentityProvider` resolves, but `client.runs.list(thread_id,
    limit=1)` returns a stale/terminated run; `join_stream` against it
    yields zero events and StopAsyncIteration fires immediately.
  - **P2** — `join_stream` opens against the right live run, but
    `stream_mode="values"` does not surface the autobuild_runner's
    `_update_state` payload shape (translator drops at
    `translation.py:362-372`, currently DEBUG-logged).
- **Independent bug surfaced (out of scope for this spike)**: the bridge's
  300s deadline timer should fire `pipeline.build-failed.*` for stuck
  builds; over a 30-min window with 4 unacked feature_ids the wire tap
  captured zero. The deadline-publish path is independently broken.
  Recommend filing as a separate wave-3 candidate against
  `RUNBOOK-FEAT-JARVIS-INTERNAL-001`.

Detailed write-up: `/tmp/runbook-evidence-FOLLOWUP-B/STATIC-ANALYSIS-PRE-SPIKE.md`.

### Instrumentation applied (assistant-driven, 2026-05-08 ~13:10 BST)

5 temporary log additions disambiguating P1'' vs P2 in a single
rebuild+retrigger cycle. **All marked `FOLLOWUP-B trace: ... TEMP — AC-5
cleanup` in source so AC-5 revert is a grep-scoped diff.**

- `wireup.py` `_observer_loop` after `_wait_for_identity` returns: log
  resolved `(thread_id, run_id)` at INFO.
- `wireup.py` `_drive_stream_session` before the `async for`: log
  "stream session open" at INFO.
- `wireup.py` `_drive_stream_session` inside the `async for` body: log
  per-StreamPart `event` and `data_keys` at INFO.
- `wireup.py` `_drive_stream_session` on StopAsyncIteration exit: log
  `parts_received`, `event_types_seen`, `terminal_seen` at INFO.
- `translation.py` non-`"values"` early-return: promoted DEBUG → INFO so
  silent drops appear in the production INFO-level capture.

Both files syntax-check (`python3 -m py_compile`).

### Remaining work (operator-driven)

1. Rebuild the forge image at current HEAD (HEAD `1b82236` plus the
   instrumentation diff above and the staged FOLLOWUP-A patch on
   `_serve_production.py`).
2. Restart `forge-prod` against the rebuilt image.
3. Re-trigger ONE `queue_build` from a fresh jarvis chat session; wait
   one full 300s deadline window for the observer to log open + iterate +
   exhaust.
4. Capture the fresh forge-prod docker log to
   `/tmp/runbook-evidence-FOLLOWUP-B/path1-trace.log` (greppable on
   `_observer_loop` / `_drive_stream_session` / `translation:`).
5. Read the new log: pick exit branch (a) [P1'' confirmed], (b) [P2
   confirmed], or (c) [neither — capture root cause].
6. Write `/tmp/runbook-evidence-FOLLOWUP-B/SPIKE-OUTCOME.md` naming the
   chosen exit branch and linking the spawned fix task.
7. AC-5: revert the 5 instrumentation lines (one commit; grep on
   `FOLLOWUP-B trace`).

### Outcome (assistant-driven, 2026-05-08 ~13:30 BST → operator follow-through ~15:45 BST)

Operator ran the rebuild + retrigger cycle against the instrumented
image. Cycle 1 captured 30 `event='values'` parts with
`data_keys=['files','messages','todos']` — **no `async_tasks` key**, no
`lifecycle`/`build_id` flat fallback. Translator's `_extract_state`
returned None for every part. Zero outbound `pipeline.*` envelopes.

**Exit branch (b) confirmed**: events arrive on the real run, but the
deepagents `stream_mode="values"` projection does not surface the
AutobuildState. The autobuild_runner's `StateChannelWriter` writes
through a side-effect channel, not a langgraph state channel.

**Spawned tasks**:

- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX](../../completed/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape.md)
  — Option B1 (wire `async_tasks` channel into autobuild_runner StateGraph)
  landed at `b9e9585`, completed at `a35c03d` 2026-05-08 ~15:45 BST.
- [TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH](TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix.md)
  — independent deadline-timer-publish bug surfaced by this spike (5-min
  observer deadline passed without `pipeline.build-failed.*` envelope;
  deadline path is gated on stream unreachability, not silence). Filed
  to `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/`.

**AC-5 cleanup**: instrumentation commit `e1eef81` was reverted before
the FIX task started — verified by `grep 'FOLLOWUP-B trace'
src/forge/lifecycle_bridge/` returning nothing.

**AC-6 handover**: `/tmp/runbook-evidence-FOLLOWUP-B/SPIKE-OUTCOME.md`
captures the diagnosis, exit branch, spawned-task IDs, and pivot space
(B1 → A → C). Pre-spike static-analysis note at the same path.

**Wave-3 candidate flagged for the runbook**: `RUNBOOK-FEAT-JARVIS-INTERNAL-001`'s
documented Signature B says `parts_received=0`. With FOLLOWUP-A applied
+ FOLLOWUP-B-FIX *not yet* applied, fresh runs see `parts_received=30+`
on cycle 1 and `parts_received=0` on subsequent redeliveries. Signature
B should be split into cycle-1-rich vs cycle-2+-drained variants.
