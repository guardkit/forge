---
id: TASK-REV-PEBR-005
title: Analyse join_stream race (Signature C) — bridge subscribes to SSE after run finishes; AC-11 still gated
status: review_complete
created: 2026-05-08T16:00:00Z
updated: 2026-05-08T17:00:00Z
priority: high
task_type: review
review_mode: gap-analysis
review_depth: standard
decision_required: true
parent_feature: FEAT-PEBR
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
companion_to:
  - TASK-REV-PEBR-001
  - TASK-REV-PEBR-002
  - TASK-REV-PEBR-003
  - TASK-REV-PEBR-004
review_results:
  mode: gap-analysis
  depth: standard
  decision: implement-in-session
  fix_shape: option-(e)-bridge-side-fetch-on-empty
  fix_shape_rationale: |
    Originally-scoped shapes (a) reorder / (b) runs.stream / (a') stream_resumable
    were all blocked by the autobuild dispatcher routing through deepagents 0.5.6
    AsyncSubAgentMiddleware.astart_async_task, which calls runs.create with no
    resumability passthrough — modifying that middleware is outside forge's
    modify-able surface. Option (e) closes the race deterministically without
    touching the dispatch path: when runs.join_stream closes empty, the bridge
    observer asks an injected RunStateFetcher whether the run terminated and
    replays the final state through the existing translator (BuildStarted +
    terminal payload).
  files_modified:
    - src/forge/lifecycle_bridge/run_state_source.py    # NEW — RunStateFetcher Protocol + langgraph_run_state_fetcher factory
    - src/forge/lifecycle_bridge/wireup.py              # RunStateFetcher kwarg + _fetch_and_replay_on_empty + _replay_run_state_snapshot + _project_running_wave_state + _translate_and_publish helper
    - src/forge/lifecycle_bridge/__init__.py            # re-exports RunStateFetcher / RunStateSnapshot / RUN_STATUS_TERMINAL / langgraph_run_state_fetcher
    - src/forge/cli/_serve_production.py                # run_state_fetcher field on LifecycleBridgeWireupParts; production langgraph_run_state_fetcher constructed
    - src/forge/cli/serve.py                            # run_state_fetcher threaded into LifecycleBridgeWireup composition
    - tests/forge/lifecycle_bridge/test_fetch_on_empty.py  # NEW — 7 regression tests covering AC-FETCH-1..7
  test_results:
    targeted_fetch_on_empty: 7/7 passed
    lifecycle_bridge_dir: 220+ tests pass (entire dir)
    cli_serve_production: 20/20 passed
    serve_identity_provider: 7/7 passed
    pipeline_consumer_correlation_id: 9/9 passed
    cli_serve_skeleton + cli_serve_logging: 32/32 passed
    sweep_total: 299/299 passed
    last_run: 2026-05-08T17:00:00Z
  lint:
    ruff: clean on all touched files
    black: clean on all touched files (after auto-format)
  ac_status:
    AC-1: done   # Signature C root cause verified via code reading + SDK 0.3.13 inspection. See review report §AC-1 timeline (consumer pipeline_consumer.py:519-548 → wireup.py:495-516 → wireup.py:565 → _serve_production.py:213-219 → stream_source.py:106-110); SDK docstring on runs.join_stream confirmed via inspect.getdoc.
    AC-2: done   # AC-3 (production IdentityProvider factory) confirmed unchanged — Signature C is upstream of identity resolution. Parent ac_status.AC-3 stays done.
    AC-3: done   # Fix-shape decision. Originally recommended (b) runs.stream(...); deepagents middleware indirection investigation revised the choice to (e) bridge-side fetch-on-empty. Documented in review report Decision Matrix.
    AC-4: superseded   # Originally "spawn FOLLOWUP-C-RACE implementation task" — superseded by in-session implementation per operator's "Fix now in this session" choice. The fetch-on-empty fix is in main; no separate implementation task spawned.
    AC-5: done   # Parent TASK-FORGE-FRR-PEBR-WIREUP frontmatter ac_status.AC-11 line extended; ac_11_blocked_on rewritten (now: RUNBOOK-RE-RUN-4 only); ac_11_resolved appended FOLLOWUP-B-FIX and TASK-REV-PEBR-005; ac_11_promotion_gate paragraph 3 added with full Signature C narrative + fix-shape rationale.
    AC-6: done   # W3-A..F triaged. W3-B already tracked (DEADLINE-TIMER-PUBLISH-fix); W3-A/D/E/F bundled as deferred jarvis-side runbook update; W3-C deferred (cosmetic).
    AC-7: done   # Cross-link integrity: spawned_tasks_target retained for traceability but marked superseded; status: review_complete; review_results block populated; related_tasks paths verified.
    AC-8: done   # Out-of-scope confirmed: per-stage envelopes, jarvis-side gaps, FOLLOWUP-C-narrow independence — all documented in review report §AC-8.
  report_path: .claude/reviews/TASK-REV-PEBR-005-review-report.md
  completed_at: 2026-05-08T17:00:00Z
spawned_tasks_target:    # superseded — operator chose in-session implementation instead of spawning FOLLOWUP-C-RACE; original target retained for traceability
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE   # NOT SPAWNED — fix-shape (e) landed in-session; see review_results.fix_shape
discovered_during: jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md re-run on 2026-05-08 against rebuilt forge image c0275b3df2c8 (HEAD 1b04b89)
discovered_at: 2026-05-08T15:30:00Z
forge_head_at_discovery: 1b04b89
forge_image_at_discovery: c0275b3df2c8
runbook_results_doc: docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-followup-b-landed.md
runbook_evidence_dir: /tmp/jarvis-runbook-evidence/
parent_ac_blocked: TASK-FORGE-FRR-PEBR-WIREUP::AC-11 (still un-met) — Phase 7 fails for a NEW reason after FOLLOWUP-B-FIX (b9e9585): join_stream race, not translator-shape mismatch
gaps_in_scope:
  - SIGNATURE-C        # join_stream live-subscription race against fast-completing placeholder run
  - W3-A               # runbook §7 needs a new Signature C section
  - W3-B               # deadline gate is on stream unreachability, not silence/empty (carries forward from prior runbook)
  - W3-C               # supervisor produced inline-prose ack rather than documented bullet shape (cosmetic)
  - W3-D               # runbook's `docker exec ... nats` pattern fails on this host (CLI not in container)
  - W3-E               # §7 references to FOLLOWUP-B SSE per-part instrumentation are archeological — b9e9585 removed them
  - W3-F               # add composed PipelineConsumerDeps … wired boot line to §2.2 pass criteria
out_of_scope:
  - per-stage envelope sequence (full autobuild orchestration in runner nodes — separate follow-up FOLLOWUP-B-FIX explicitly deferred)
  - jarvis-side gaps (separate review in jarvis repo)
  - existing TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs (independent — same FOLLOWUP-C letter, different concern; the -RACE suffix disambiguates)
related_tasks:
  - TASK-FORGE-FRR-PEBR-WIREUP                                     # parent fix; AC-11 still gated by this race
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A-apply-lifecycle-bridge-registry-migration   # completed; migration drift no longer in play
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape   # completed (b9e9585); composed PipelineConsumerDeps now boots cleanly
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs   # unrelated FOLLOWUP-C namesake (independent concern)
  - TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix                 # related (W3-B side-observation overlap)
priority: high
tags:
  - forge-serve
  - lifecycle-bridge
  - production-binding
  - join-stream-race
  - sse-subscription
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - regression-protection
estimated_review_minutes: 45
estimated_implementation_minutes_after_review: 60   # FOLLOWUP-C-RACE: subscribe-before-dispatch reorder + 1 regression test
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Analyse join_stream race (Signature C) and scope the FOLLOWUP-C-RACE fix

## TL;DR

The 2026-05-08 runbook re-run on rebuilt image `c0275b3df2c8` (HEAD `1b04b89`, post-FOLLOWUP-B-FIX `b9e9585`) confirms Phases 0–6 GREEN. The composer boot line lands cleanly:

```
composed PipelineConsumerDeps (async_task_starter=wired, ack_bridge=wired, terminal_publish_ledger=wired)
```

`autobuild_runner` StateGraph executes cleanly: `Background run succeeded run_exec_ms=16`. FOLLOWUP-A migration continues to hold (0 `no such table` warnings).

But Phase 7 still **fails** with a brand-new fingerprint — the runbook does not yet describe it. **AC-3 / AC-11 remain unmet, but for a different reason than wave-2.**

**Why Phase 7 still fails (Signature C):** the placeholder lifecycle nodes complete in ~16 ms, so by the time the bridge's observer task resolves IdentityProvider and calls `langgraph_sdk.runs.join_stream(thread_id, run_id)`, the run is already finished. `join_stream` against a finished run is a **live subscription**, not a replay — it returns an empty stream and closes. Bridge sees zero `values` events → stream ends without a terminal envelope → no ack → JetStream redelivers → repeat. Final consumer state on the wire: `delivered=14, ack_floor=0, 0 outbound envelopes`.

**No code changes in this task — analysis + decision + spawn implementation task only.**

## Why

**Parent AC-11 is still the gate.** [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11 requires the rebuilt image to publish a real `pipeline.build-started.FEAT-*` envelope and JetStream `ack_floor` to advance past the inbound. After FOLLOWUP-B-FIX landed, `ack_floor=0` is no longer a translator-shape symptom — it is a **subscription-timing** symptom. The fix shape is structurally different from FOLLOWUP-B's, so it warrants its own review and its own implementation task.

**Runbook §7 is now stale in three distinct ways** (W3-A, W3-B, W3-E) and the wave-3 fold also surfaces three smaller hygiene items (W3-C, W3-D, W3-F). These get triaged here so they do not silently rot through another revalidation cycle.

The parent task must remain in `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/` (NOT `tasks/completed/`) until FOLLOWUP-C-RACE lands and Phase 7 captures a real `pipeline.build-started.FEAT-*` envelope on the wire.

## Background

### Discovery context (operator narrative)

End-to-end runbook walkthrough on GB10 against:

- **forge HEAD** `1b04b89` (post FOLLOWUP-B-FIX `b9e9585`)
- **forge image** rebuilt to sha `c0275b3df2c8` from current main
- **Phases 0–6** all GREEN; composed `PipelineConsumerDeps` boot line confirmed
- `autobuild_runner` StateGraph executes cleanly (`run_exec_ms=16`)
- FOLLOWUP-A migration: 0 `no such table` warnings
- **Phase 7** FAIL — Signature C

### What FOLLOWUP-B-FIX (b9e9585) actually delivered

Observable in this run (positive):

| Signal | Evidence |
|---|---|
| `async_tasks` channel wired into autobuild_runner StateGraph | composed PipelineConsumerDeps boot line: `async_task_starter=wired, ack_bridge=wired, terminal_publish_ledger=wired` |
| Background run executes | `Background run succeeded run_exec_ms=16` |
| FOLLOWUP-A migration holds | 0 `no such table: lifecycle_bridge_registry` warnings across 14 dispatches |

### Why Phase 7 still fails — Signature C

The placeholder lifecycle nodes inside the autobuild_runner finish in ~16 ms. The bridge's observer task — scheduled via `asyncio.create_task` in [`wireup.py:512`](../../../src/forge/lifecycle_bridge/wireup.py#L512) — has to:

1. Wait for IdentityProvider (`_wait_for_identity` at [`wireup.py:565`](../../../src/forge/lifecycle_bridge/wireup.py#L565)) to resolve `(thread_id, run_id)` — IdentityProvider polls the `async_tasks` SQLite mirror and `langgraph_sdk.runs.list(...)`, which only returns rows after `dispatch_autobuild_async` writes them.
2. Then call `client.runs.join_stream(thread_id, run_id, stream_mode="values")` ([`stream_source.py:106`](../../../src/forge/lifecycle_bridge/stream_source.py#L106)).

By the time those two steps complete, the run has finished. `join_stream` against a **finished** run is a live subscription, not a replay — it returns an empty stream and closes. Confirmed via direct curl probe captured under `/tmp/jarvis-runbook-evidence/`.

The bridge then enters [`wireup.py:580-596`](../../../src/forge/lifecycle_bridge/wireup.py#L580): "stream closed without a terminal envelope — leaving message un-acked, JetStream will redeliver." JetStream redelivers → consumer re-calls `register_ack_handle` → idempotency drops the duplicate registration ([`wireup.py:467-477`](../../../src/forge/lifecycle_bridge/wireup.py#L467)) → no second observer is scheduled → no second `join_stream` is opened. Final state: `delivered=14, ack_floor=0, 0 outbound envelopes`.

### Wave-3 fold candidates surfaced for §7 staleness

| ID | Severity | One-line |
|---|---|---|
| **W3-A** | high | §7 needs a new **Signature C** section describing the join_stream race |
| **W3-B** | carries forward | deadline gate is on stream unreachability, not silence/empty (still un-fixed since wave-2) |
| **W3-C** | cosmetic | supervisor produced inline-prose ack rather than the documented bullet shape |
| **W3-D** | low | runbook's `docker exec … nats` pattern fails on this host (NATS CLI not in container) |
| **W3-E** | medium | §7 references to FOLLOWUP-B SSE per-part instrumentation are archeological — `b9e9585` removed them |
| **W3-F** | low | add the composed `PipelineConsumerDeps … wired` boot line to §2.2 pass criteria |

### The recommended fix shape (to be confirmed in AC-3)

**Subscribe the bridge to the SSE stream BEFORE dispatching the run.** This removes the race deterministically: the live subscription is open before any state updates can fire, so every event is observed.

Mechanically: reorder the consumer-side wireup so `client.runs.join_stream(...)` is opened (or at minimum the run is created with `stream_mode` configured) **before** `dispatch_autobuild_async` returns. The exact seam needs scoping in the spawned task — candidates include:

1. **Reorder inside `_observer_loop`**: open `join_stream` against a freshly-created (not-yet-running) run, then trigger the run.
2. **Replace `runs.create + runs.join_stream`** with `runs.stream(...)` (single-shot create-and-stream API) — eliminates the seam entirely.
3. **Use `runs.create_run(stream_mode=...)` + persistent stream**: the langgraph-SDK API surface for "stream from creation" — needs verification against the installed SDK version.

Once landed, expect **2 envelopes** from the placeholder bodies (`build-started` + `build-complete`); the full per-stage sequence still requires real autobuild orchestration in the runner nodes — the FOLLOWUP-B-FIX commit explicitly deferred that, and it is **out of scope** for FOLLOWUP-C-RACE.

## Acceptance Criteria

- [ ] **AC-1** — **Confirm Signature C root cause via code reading.** Read [`wireup.py:404-525`](../../../src/forge/lifecycle_bridge/wireup.py#L404) (`register_ack_handle`) and [`wireup.py:530-627`](../../../src/forge/lifecycle_bridge/wireup.py#L530) (`_observer_loop`) and [`stream_source.py:106`](../../../src/forge/lifecycle_bridge/stream_source.py#L106) (`runs.join_stream` call site) and verify:
  - `register_ack_handle` schedules the observer via `asyncio.create_task` ([`wireup.py:512`](../../../src/forge/lifecycle_bridge/wireup.py#L512)) and returns to the consumer.
  - The observer first awaits `_wait_for_identity` ([`wireup.py:565`](../../../src/forge/lifecycle_bridge/wireup.py#L565)) — a poll loop against IdentityProvider that depends on `dispatch_autobuild_async` having already written the `async_tasks` row.
  - The observer only calls `join_stream` AFTER identity resolution.
  - The dispatch path itself does not gate on subscription readiness — the run can complete entirely in the window between `register_ack_handle` returning and the observer reaching `join_stream`.
  - Cross-check the curl evidence under `/tmp/jarvis-runbook-evidence/` against the langgraph-sdk source-of-truth: `runs.join_stream` against a finished run is a live subscription that returns empty.

  Document the verified call ordering as a numbered timeline in the review notes (consumer → wireup → observer → SDK), with file:line references at each step.

- [ ] **AC-2** — **Confirm AC-3 is still met as written, but Signature C breaks AC-11.** Re-read [TASK-FORGE-FRR-PEBR-WIREUP::AC-3](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) (the production `IdentityProvider` factory) and confirm:
  - The factory at [`_serve_production.py`](../../../src/forge/cli/_serve_production.py) still ships and unit-tests still pass — Signature C is **not** an IdentityProvider regression.
  - The break is upstream of IdentityProvider: even when identity resolves correctly, the run has already finished. Update parent frontmatter `ac_status.AC-3` only if the analysis surfaces a contradiction (expected: no change, AC-3 stays `done`).

- [ ] **AC-3** — **FOLLOWUP-C-RACE fix-shape decision.** Choose between three subscribe-before-dispatch shapes and document why:
  - **(a) Reorder inside `_observer_loop`**: create the run, open `join_stream`, THEN trigger the run body. Pros: localised; touches one seam. Cons: requires the langgraph-SDK to support "create run paused" or equivalent.
  - **(b) Replace `runs.create + runs.join_stream` with `runs.stream(...)`**: single-shot create-and-stream API. Pros: eliminates the race window by construction. Cons: changes the call signature; needs SDK-version verification.
  - **(c) Move subscription into the consumer's pre-dispatch path**: open the stream from `register_ack_handle` directly (synchronously) before scheduling the observer. Pros: tightest race elimination. Cons: violates the AC-5 "consumer remains responsive" non-blocking discipline at [`wireup.py:424-426`](../../../src/forge/lifecycle_bridge/wireup.py#L424).

  Default recommendation: **(b)** if the SDK supports it on the installed version; otherwise **(a)**. Confirm against `langgraph-sdk` in `pyproject.toml`.

- [ ] **AC-4** — **Spawn TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE.** Create the implementation task with:
  - **Single AC**: subscribe-before-dispatch wired per the chosen option (AC-3); the bridge observes both `build-started` and `build-complete` envelopes from the placeholder bodies in a runbook re-run.
  - **Regression test**: extend `tests/forge/test_lifecycle_bridge_wireup.py` (or sibling) with a test that asserts the SSE subscription is open BEFORE the run is triggered. Use a fake langgraph client with a settable "subscription-open-at" timestamp and a "run-started-at" timestamp; assert subscription_open_at <= run_started_at on every dispatch path.
  - **Out-of-scope guard rail**: explicitly call out in the task body that this fix delivers **2 envelopes** (`build-started` + `build-complete` from placeholder bodies) — the full per-stage sequence requires real autobuild orchestration in the runner nodes and is a **separate** follow-up that FOLLOWUP-B-FIX explicitly deferred.
  - **Naming disambiguation note**: the existing [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs.md) is unrelated — same FOLLOWUP-C letter, different concern. The `-RACE` suffix disambiguates; both can ship independently.
  - Lint + format clean on touched files.
  - `parent_review: TASK-REV-PEBR-005` in frontmatter.

- [ ] **AC-5** — **Update parent AC-11 status.** Update [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) frontmatter to record:
  - `ac_status.AC-11`: `partially-unblocked` → unchanged label, but extend the line: "FOLLOWUP-B-FIX live (b9e9585) and runbook re-run on c0275b3df2c8 confirmed translator no longer silent — but Phase 7 now fails with **Signature C** (join_stream race) instead. New blocker: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE."
  - Add `TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE` to `ac_11_blocked_on`.
  - Append a third-revalidation entry to the body's promotion-gate section: "2026-05-08T15:30Z: Phase 7 still fails for Signature C; promotion remains gated on FOLLOWUP-C-RACE landing and a fourth runbook re-run capturing `pipeline.build-started.FEAT-*` on the wire."

- [ ] **AC-6** — **Wave-3 runbook fold candidates triage.** For each of W3-A through W3-F, document one of: (i) fold into TASK-FRR-RUNBOOK-002 (if it exists) or open a new runbook gap-fold task; (ii) fold into FOLLOWUP-C-RACE if it is a code-side concern (e.g. W3-B if the deadline gate fix is in scope); (iii) defer with explicit rationale. Default routing:
  - **W3-A** (Signature C section) → fold into the runbook gap-fold task (it's a documentation gap).
  - **W3-B** (deadline gate on unreachability not silence) → already tracked under [TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix](TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix.md); confirm cross-link and avoid double-tracking.
  - **W3-C** (supervisor inline-prose ack) → defer (cosmetic; consider for jarvis-side review if it is a supervisor agent prompt issue, not a forge concern).
  - **W3-D** (`docker exec … nats` not in container) → fold into runbook gap-fold (host-environment doc gap).
  - **W3-E** (FOLLOWUP-B archeological references) → fold into runbook gap-fold (delete archeological references in §7).
  - **W3-F** (add composed PipelineConsumerDeps boot line to §2.2 pass criteria) → fold into runbook gap-fold (positive-evidence gap).

- [ ] **AC-7** — **Cross-link integrity.** After spawning FOLLOWUP-C-RACE, update this task's `spawned_tasks_target` → `spawned_tasks` (i.e. promote the planned-target field to the actual-spawned field) and verify the spawned task's `parent_review` field points back to `TASK-REV-PEBR-005`. Verify all tasks in `related_tasks` are reachable.

- [ ] **AC-8** — **Out-of-scope confirmation.** Explicitly confirm in the review notes that:
  - The full per-stage envelope sequence (real autobuild orchestration in runner nodes) is **out of scope** for FOLLOWUP-C-RACE — it is the deferred follow-up that FOLLOWUP-B-FIX explicitly left for a later task.
  - Jarvis-side gaps surfaced in this run are **out of scope** here and tracked in a separate jarvis-repo review.
  - The existing TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs is **independent** and not bundled with FOLLOWUP-C-RACE despite the shared FOLLOWUP-C letter.

## Inputs / Evidence

- **Runbook results doc** (jarvis repo): `docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-followup-b-landed.md` (280 lines, 34 KB) — full per-phase RESULTS, Signature C deep dive, six wave-3 gap-folds.
- **Runbook evidence dir**: `/tmp/jarvis-runbook-evidence/` — wire-tap, forge logs, sidecar logs, direct curl probe of `join_stream` against finished run, consumer state pre/post.
- **Transcript**: `~/.jarvis/transcripts/10c80f94-…txt`
- **Trace**: `~/.jarvis/traces/10c80f94-…json`
- **Command history**: `docs/history/command_history.md` — appended runbook entry pointing to RESULTS + evidence.
- **Parent task**: [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — AC-11 is the gate.
- **Wireup register-and-attach path**: [src/forge/lifecycle_bridge/wireup.py:404-525](../../../src/forge/lifecycle_bridge/wireup.py#L404) — `register_ack_handle` schedules observer task, returns to consumer.
- **Observer loop**: [src/forge/lifecycle_bridge/wireup.py:530-627](../../../src/forge/lifecycle_bridge/wireup.py#L530) — `_wait_for_identity` then `_consume_with_reconnect`; the un-acked-on-empty-stream branch lives at line 580.
- **Stream source**: [src/forge/lifecycle_bridge/stream_source.py:106](../../../src/forge/lifecycle_bridge/stream_source.py#L106) — the `client.runs.join_stream(thread_id, run_id, stream_mode="values")` call site that returns empty against a finished run.
- **Forge image**: rebuilt to sha `c0275b3df2c8` from current main `1b04b89`.

## Decision Checkpoint

After review, present the operator with:

1. **[A]ccept** — spawn TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-RACE as scoped (AC-3 default option), update parent AC-11, route W3-A..F per AC-6 defaults.
2. **[M]odify scope** — adjust before spawning (e.g. pick fix-shape (a) vs (b) vs (c) explicitly, expand FOLLOWUP-C-RACE to fold W3-B in, etc.).
3. **[D]efer** — leave Signature C unfixed if the operator judges priority lower than other in-flight work (parent AC-11 stays gated indefinitely).
4. **[C]ancel review** — discard if the operator finds the diagnosis flawed or the SDK-API survey changes the fix shape entirely.

Default recommendation: **[A]ccept** with fix-shape **(b) `runs.stream(...)`** if SDK supports it; **(a) reorder** otherwise. The race is structural and deterministic — subscribe-before-dispatch eliminates it by construction; any retry/replay-aware fallback would be working around the wrong layer.

## References

- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix; AC-11 still gated.
- [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — wave-2 review that spawned FOLLOWUP-A/B/C-narrow.
- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A](../../completed/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A-apply-lifecycle-bridge-registry-migration.md) — completed migration-wireup fix.
- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX](../../completed/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX-translator-vs-emit-shape.md) — completed translator-shape fix (`b9e9585`).
- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C-narrow-langgraph-json-graphs.md) — independent FOLLOWUP-C namesake (langgraph.json narrowing).
- [TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix](TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix.md) — overlapping concern with W3-B (deadline gate).
- `forge.lifecycle_bridge.wireup.LifecycleBridgeWireup.register_ack_handle` — the seam where the observer is scheduled.
- `forge.lifecycle_bridge.wireup.LifecycleBridgeWireup._observer_loop` — the observer that resolves identity then opens the (already-too-late) stream.
- `forge.lifecycle_bridge.stream_source.langgraph_stream_source` — the `runs.join_stream` adapter.
- `langgraph_sdk.client.runs.join_stream` — live-subscription API; replay against finished run returns empty.

## Notes for the Reviewer

- **Trust the evidence.** The composer boot line and `run_exec_ms=16` confirm FOLLOWUP-B-FIX landed cleanly. The direct curl probe under `/tmp/jarvis-runbook-evidence/` confirms `join_stream` against a finished run returns empty. Final consumer state `delivered=14, ack_floor=0, 0 outbound envelopes` is the smoking gun — every JetStream redelivery hits the idempotency drop in `register_ack_handle` and never reschedules the observer.
- **Signature C is structural.** It is not a config tweak, not a translator-shape fix, not a missing migration. The dispatch path returns control to the consumer before the bridge has opened a live subscription, and a fast run finishes inside that window. Any fix that does not reorder dispatch vs. subscription is working around the wrong layer.
- **Scope discipline.** The placeholder bodies will emit exactly two envelopes (`build-started` + `build-complete`). The full per-stage sequence is a separate, deferred follow-up. Resist letting FOLLOWUP-C-RACE swell to cover real autobuild orchestration — that was explicitly deferred by FOLLOWUP-B-FIX and remains out of scope.
- **Don't bundle.** Three independent concerns share the FOLLOWUP-C letter prefix:
  1. `-narrow-langgraph-json-graphs` — already in backlog, sidecar config.
  2. `-RACE` — this task's spawn, dispatch-vs-subscription ordering.
  3. The wave-3 fold candidates W3-A/D/E/F — runbook documentation, route to TASK-FRR-RUNBOOK-002.
  Each ships on its own cadence; do not collapse them into one PR.
- **AC-11 promotion gate.** The parent task remains in `tasks/backlog/` until a fourth runbook re-run captures `pipeline.build-started.FEAT-*` on the wire. This review does not change that — it only narrows the active blocker from "translator silent" to "join_stream race".
