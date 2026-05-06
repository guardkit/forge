---
id: TASK-FORGE-FRR-F010M
title: "Scope the autobuild_runner ↔ pipeline-lifecycle-emitter bridge (produce context doc → /feature-spec → /feature-plan)"
status: backlog
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
priority: high
task_type: scoping
tags:
  - forge-serve
  - autobuild-runner
  - pipeline-lifecycle-emitter
  - async-bridge
  - feat-forge-010-followup
  - first-real-run-followup
  - scoping
  - feature-spec-prep
  - feature-plan-prep
  - sub-feature
complexity: 6
estimated_minutes: 240
estimated_effort: "3-6 hours (read existing FW10-009/010/011 docs, audit current emitter wiring, draft the scoping doc, then drive /feature-spec + /feature-plan)"
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-009        # validation surface and build-failed paths — likely partial coverage
  - TASK-FW10-010        # pause-resume publish round-trip
  - TASK-FW10-011        # capstone integration test (status: design_approved)
  - TASK-FORGE-FRR-F010F # safety-net publish (sync-raise only; F010M extends to async-completion path)
  - TASK-FORGE-FRR-F010J # production composer + sidecar URL threading (prerequisite)
  - TASK-FORGE-FRR-F010L # sibling — autobuild_runner model retargeting (lands first; F010M then builds on a working autobuild)
correlation_id: e9433033-ea80-449f-885d-b2d1bdfb839e
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Joint live-wire validation rerun late evening — F010J wired the autobuild dispatch path through the sidecar; the autobuild_runner graph launched with a real task_id; jarvis's chat REPL drained no notification line because the autobuild's async failure (or future async completion) produces no outbound pipeline.* envelope from forge"
context_files:
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md
  - tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md
  - tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md
  - tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md
  - src/forge/pipeline/
  - src/forge/pipeline/dispatchers/autobuild_async.py
  - src/forge/cli/_serve_dispatcher.py
  - src/forge/adapters/nats/pipeline_consumer.py
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Scope the `autobuild_runner` ↔ pipeline-lifecycle-emitter bridge (produce context doc → `/feature-spec` → `/feature-plan`)

## TL;DR

F010J wired forge's autobuild dispatch into the langgraph-runner sidecar — the
`autobuild_runner` graph launches with a real `task_id` and runs asynchronously
inside the sidecar's in-memory runtime. **What's missing**: a path that
translates the run's outcome (success / failure / stall / cancellation) back
into the corresponding `pipeline.build-started.*` / `pipeline.stage-complete.*`
/ `pipeline.build-complete.*` / `pipeline.build-failed.*` envelopes on the
JetStream wire. F010F's safety-net publish path only fires on synchronous raises
in `dispatch_build` — async outcomes from the sidecar produce no terminal
envelope, so jarvis's chat REPL never receives lifecycle notifications. This
task scopes the bridge: produce a context doc capturing the design space
(existing FW10-009/010 wiring, candidate architectures, decisions to make), then
drive `/feature-spec` + `/feature-plan` to break it into an implementable
feature.

## Symptom (verbatim from RESULTS Addendum 5)

### The wire side

The `pipeline.>` tail captured during the post-F010J rerun captured **only** the
inbound `pipeline.build-queued.FEAT-43DE` envelope:

```json
{"correlation_id":"e9433033-ea80-449f-885d-b2d1bdfb839e",
 "source_id":"jarvis","event_type":"build_queued",
 "payload":{"feature_id":"FEAT-43DE", ...}}
```

NO outbound `pipeline.build-started.*`, `pipeline.stage-complete.*`,
`pipeline.build-complete.*`, or `pipeline.build-failed.*` envelopes appeared.
The sidecar's run failed asynchronously (per F010.L's symptom) but no terminal
envelope reached the wire.

### The chat side

The supervisor's second-turn answer was honest:

> "No progress events have come through yet — the build is still sitting in
> the queue waiting for Forge to pick it up. ... I don't have a direct way to
> check the live status of a queued build — I'd need to wait for the pipeline
> progress events to arrive."

## Why

The DDR-030 between-prompt notification contract requires forge to publish
lifecycle envelopes for every state transition the autobuild reaches —
including async completion, async failure, async stall (e.g. via timeout), and
operator cancellation. The current shape:

- **F010F** publishes a terminal `build-failed` when `dispatch_build` raises
  synchronously inside `pipeline_consumer.handle_message`.
- **F010C** threads the inbound `correlation_id` through that publish.
- The async path — the autobuild actually runs on the sidecar; the run
  completes / fails / stalls **after** `dispatch_build` returned successfully —
  has **no equivalent publish path**.

This is the reason the operator can't see anything happen after queuing a
build, despite the entire wiring chain F010A → F010J being verified live.
**The bridge from sidecar run-result → `pipeline.*` envelope is the next layer
of work.**

## Why this is a scoping task, not a fix task

The fix shape is non-obvious and the design space is large. Candidate
architectures (starter set — the implementer may add more during analysis):

- **(A) Polling** — forge holds the `task_id` returned by
  `dispatch_autobuild_async` and a background coroutine polls langgraph-runner's
  `/threads/<thread_id>/runs/<run_id>` endpoint, emitting `pipeline.*` envelopes
  when state changes are observed. Pros: simple, no sidecar-side change. Cons:
  latency vs poll-interval tradeoff; the bridge holds in-memory state that's
  lost across daemon restart.
- **(B) Webhooks** — configure langgraph-runner to call a forge HTTP endpoint
  on run-completion. Pros: zero polling latency. Cons: requires a forge ASGI
  surface (similar to F010G's deferred Option A2) + langgraph-runner support
  for webhooks (verify it's a thing in `langgraph-cli`).
- **(C) Streaming subscription** — forge opens a streaming subscription
  (SSE / websocket) to `/threads/<thread_id>/runs/<run_id>/stream` and
  translates each event into a `pipeline.*` envelope. Pros: real-time, native
  to langgraph-runner. Cons: long-lived connections; reconnect logic.
- **(D) In-process emit from inside the autobuild_runner subagent** — the
  subagent itself calls `PipelineLifecycleEmitter.emit_*()` at every stage
  transition. Pros: simplest plumbing once wired. Cons: requires the emitter to
  be reachable from inside the sidecar's runtime — it's a separate process from
  forge, so the emitter would need to be replaced with an HTTP client that
  posts back to forge (which is essentially Option B reversed).
- **(E) Hybrid** — poll for terminal state only (`build-complete` /
  `build-failed`); rely on the autobuild_runner to emit per-stage envelopes via
  Option D.

Each option has cross-cutting concerns: where does the daemon-restart recovery
state live? How is the inbound message acked when an async build completes
successfully (vs F010F which acks on synchronous raise)? Does TASK-FW10-010's
pause-resume round-trip already prescribe a shape? Does TASK-FW10-011's
capstone integration test already commit to a shape? Are there reference
implementations in the deepagents / langgraph-runner ecosystem?

The scope is genuine feature-shaped work, not a single fix. Hence:

1. **Phase 1 of this task — produce the scoping doc** at
   `docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md`.
   It captures: the symptom, the design space, the existing wiring, the
   cross-cutting concerns, the open questions. Skeleton already filed alongside
   this task; the implementer fills in each section.
2. **Phase 2 — drive `/feature-spec`** with
   `--context docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md`
   (and any other relevant existing docs the implementer finds, e.g.
   FW10-009/010 contracts, the langgraph-runner SDK reference). Output: BDD
   scenarios capturing the bridge's expected behavior.
3. **Phase 3 — drive `/feature-plan`** with the same `--context` plus the BDD
   scenarios from Phase 2. Output: a wave-plan of implementation tasks.
4. **Phase 4 — file each plan task** as a child of this F010M scope, then
   implement.

## Acceptance Criteria

- [ ] **AC-1 (scoping doc exists with required sections):** the scoping doc at
  `docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md` is
  populated and contains all eight required sections (Status, Problem, Existing
  wiring audit, Design space, Cross-cutting concerns, Open questions for
  /feature-spec, Recommended option, References).
- [ ] **AC-2 (FW10 audit performed):** the scoping doc has audited the existing
  FW10-009 / FW10-010 / FW10-011 surface and explicitly states whether either of
  those tasks already partially covers the async bridge — and if so, what's
  missing. (This audit may close the gap entirely, in which case F010M reduces
  to "complete the deferred FW10-XXX AC Y.Z".)
- [ ] **AC-3 (≥ 4 candidate architectures enumerated):** the scoping doc
  enumerates at least four candidate architectures (Options A-D above are the
  starting point; the implementer may add E+).
- [ ] **AC-4 (cross-cutting concerns enumerated):** the scoping doc lists, at
  minimum, the seven cross-cutting concerns named in §Implementation Notes
  below (daemon-restart recovery, deferred-ack contract, FW10-010 pause-resume
  interaction, correlation_id threading, observability of in-flight builds,
  retry semantics on transient sidecar failures, cancellation paths).
- [ ] **AC-5 (/feature-spec invoked, BDD scenarios saved):** `/feature-spec` is
  invoked with the scoping doc + relevant FW10 task files + the langgraph-runner
  SDK reference as `--context`. Output BDD scenarios are saved as a sibling
  file under `docs/design/` (or wherever existing feature-spec output lives;
  match the precedent set by FEAT-FORGE-010 / `forge-orchestrator-wiring-feature-context.md`).
- [ ] **AC-6 (/feature-plan invoked, wave-plan saved):** `/feature-plan` is
  invoked with the scoping doc + BDD scenarios. Output wave-plan is saved as a
  sibling folder under `tasks/backlog/` named
  `forge-autobuild-runner-pipeline-emitter-bridge/` (mirroring the existing
  `forge-serve-orchestrator-wiring/` convention).
- [ ] **AC-7 (plan tasks parented):** each plan task in the wave is filed as a
  child of F010M (`parent_task: TASK-FORGE-FRR-F010M` in their frontmatter).
- [ ] **AC-8 (operator runbook revalidation — deferred to wave-plan
  implementation):** the runbook re-run is deferred to whenever the wave-plan
  implementation completes, which won't be the same session as F010M closes.
  Once the bridge is implemented, re-run jarvis runbook §6.2 + §7. Expected
  outcome: chat REPL renders the **full lifecycle sequence**
  (`build-started + stage-complete*N + build-complete` or `build-failed`) for
  an autobuild that runs to completion on the llama-swap-targeted sidecar
  (post-F010L). This AC is explicitly carried forward to the final
  implementation task in the F010M wave-plan; F010M itself closes once
  AC-1..AC-7 are satisfied.

## Files Expected to Change

- **New** (Phase 1 deliverable, skeleton filed alongside this task):
  `docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md` — the
  scoping doc the implementer fills in. The file is created with placeholder
  section headers; do **not** pre-resolve the design analysis.
- **New** (Phase 2 deliverable): `docs/design/forge-autobuild-runner-pipeline-emitter-bridge-spec.md`
  (or whatever path `/feature-spec` writes to — match existing precedent).
- **New** (Phase 3 deliverable): `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/`
  folder with multiple wave-plan tasks (each carrying
  `parent_task: TASK-FORGE-FRR-F010M`).
- **Possibly** amendments to FW10-009 / FW10-010 / FW10-011 cross-references
  if the audit shows partial coverage. Specifically, FW10-011 is currently
  `design_approved` (not implemented) — its design may already commit to a
  bridge shape, in which case F010M's wave-plan should fold (rather than
  duplicate) FW10-011's commitments.

## Implementation Notes

### Why not just write the implementation directly?

Five candidate architectures all touch load-bearing infrastructure (the
wire-protocol envelope contract per `docs/design/contracts/API-nats-pipeline-events.md`,
the SQLite lifecycle state machine per FW10-005 / DDR-006, the daemon's
recovery shape per ADR-SP-013). Picking one without scoping risks landing the
wrong shape and re-doing it. `/feature-spec` + `/feature-plan` are the existing
forge-side tools for this kind of design work; they were used for
FEAT-FORGE-010 itself (anchored by `docs/research/forge-orchestrator-wiring-gap.md`
and `docs/research/forge-orchestrator-wiring-feature-context.md`).

### Cross-cutting concerns the scoping doc must cover

For every candidate option, the implementer must answer:

1. **Daemon-restart recovery.** The bridge state must survive `forge serve`
   restart mid-build. F010K (deferred sibling) sketches a supervisor-side
   reconciliation pass; F010M's bridge design needs to compose with that.
2. **Deferred-ack contract.** When does the inbound `pipeline.build-queued.*`
   get acked? F010F currently acks on sync-raise; FW10-001's contract acks on
   terminal lifecycle. The async-completion path needs a parallel rule that
   fits both. Per Addendum 5, today's behavior is a redelivery storm every 30s
   absorbed by duplicate-detection — loud-but-harmless but conditional on the
   in-flight build never actually completing.
3. **FW10-010 pause-resume interaction.** FW10-010 (`design_approved`) commits
   to `emit_build_paused` / `emit_build_resumed` calls inside the
   `autobuild_runner._update_state` and the `approval_subscriber` resume path.
   Both call sites are in-process to forge — does that imply Option D for those
   two states (in-process emit) and a different option for build-started /
   stage-complete / build-complete / build-failed? Or is FW10-010's contract
   already broken by the sidecar deployment shape (i.e. `_update_state` runs
   inside the sidecar, not inside forge)?
4. **Correlation_id threading on every emit.** F010C's contract requires every
   outbound `pipeline.*` envelope to carry the inbound `correlation_id`. Every
   new publish site (whether polling, webhook, streaming, or in-process) needs
   to thread the correlation_id from the dispatch context (where it's persisted
   onto the QUEUED row in SQLite) through to the publish call. This is a
   load-bearing invariant — call it out explicitly in the scoping doc.
5. **Observability of in-flight builds.** Is there a `forge status` command
   that lists in-flight builds? Should it grow? (Addendum 5's redelivery storm
   would be much less alarming if the operator could see "build X has been
   ACTIVE for N minutes inside the sidecar" without `nats consumer info`.)
6. **Retry semantics on transient sidecar failures.** 5xx from
   langgraph-runner / timeout / connection drop. Polling-vs-streaming options
   expose different reconnect surfaces.
7. **Cancellation paths.** Operator cancels a build mid-flight (via
   `pipeline.build-cancelled.*` — already in `API-nats-pipeline-events.md`'s
   subject catalogue); how does the bridge propagate that to the sidecar?
   `langgraph-cli` exposes a `DELETE /threads/<thread_id>/runs/<run_id>`
   endpoint — does the bridge use it?

### Sequence vs F010L

F010L lands first (model retarget) — it's smaller, can validate independently
against the runbook (sidecar log will show the autobuild executing real LLM
calls). F010M then builds on a working autobuild base — the scoping work is
easier when the autobuild can actually progress through stages and the
implementer can observe what the emitter site needs to capture.

### Operator handoff (post-F010L runbook rerun)

Once F010L is in, run the runbook one more time. The sidecar log should show
the autobuild progressing through real stages (`qwen3-code-next` responses)
instead of failing on auth. **Note the run shape — that's the empirical input
to the F010M scoping doc's "what does the autobuild_runner actually produce"
section.** Specifically: capture (1) the order and shape of `_update_state`
transitions inside the sidecar, (2) any stdout / log output the sidecar emits
that forge could consume, (3) the time-from-launch to terminal state for the
canonical run, and (4) the sidecar's behavior on operator-Ctrl+C.

### Cross-reference with TASK-FW10-011

TASK-FW10-011 is currently `design_approved` (not implemented). If its design
has already been resolved, F010M may largely be "implement FW10-011 + add the
bridge" rather than greenfield design. **Check status during the scoping
audit.** Specifically: read FW10-011's "Implementation notes" section in full
to see whether the embedded-NATS + mocked-`AutobuildDispatcher.dispatch`
fixture commits to a publish shape that constrains F010M's option choice.

### Existing forge-side scoping doc precedent

`docs/research/forge-orchestrator-wiring-gap.md` was the scoping doc that
anchored FEAT-FORGE-010 (which became FEAT-DEA8 / TASK-FW10-001..011). Use it
as a shape reference for the new scoping doc — particularly its layered
`Executive summary` → `What's wired vs what isn't` → `Why this is a feature,
not a follow-up task` → `Constraints carried from existing architecture` →
`Proposed feature scope (to feed /feature-spec)` → `Empirical evidence chain`
→ `References` arc. F010M's scoping doc does not need every section verbatim
but should match the rigor.

### Why this is a "scoping task" not a "fix task" or "review task"

- A **fix task** would land a one-or-few-line change against a known root
  cause. F010M has no root cause — it has a *design space*.
- A **review task** (F010I shape) chooses between enumerated options to feed an
  immediate implementation companion. F010M needs to enumerate the options
  *first*, and the implementation is multi-task wave-plan-shaped, not a single
  companion.
- A **scoping task** produces a context doc that drives `/feature-spec` +
  `/feature-plan`. That is what F010M is.

The shape precedent is `docs/research/forge-orchestrator-wiring-gap.md` →
FEAT-FORGE-010. Same arc, smaller scope (single bridge surface, not the whole
orchestrator).

## References

### Source-of-truth (forge)

- `src/forge/pipeline/__init__.py` — `PipelineLifecycleEmitter` definition
  (re-exported entry point); audit during scoping for the existing emit-site
  catalogue.
- `src/forge/pipeline/dispatchers/autobuild_async.py:dispatch_autobuild_async`
  — returns a `task_id` after launching the run; understand whether forge
  holds any reference to the run after that point. The bridge needs to capture
  whatever reference is durable across daemon restart.
- `src/forge/cli/_serve_dispatcher.py:make_handle_message_dispatcher` — the
  dispatcher's contract; F010F's safety-net publish lives here.
- `src/forge/adapters/nats/pipeline_consumer.py` (lines 470-506) — F010F's
  safety-net publish path; constrains where the new bridge's publish calls
  fit relative to the existing `handle_message` envelope.
- `src/forge/adapters/nats/pipeline_publisher.py` — the publisher F010M's
  bridge will call; understand its API surface and threading model.
- `src/forge/subagents/autobuild_runner.py` — where `_update_state` lives;
  the emitter call sites for FW10-010's pause/resume contract; relevant for
  Option D analysis.

### Source-of-truth (third-party)

- `langgraph-cli` / `langgraph-sdk` — the `/threads`, `/runs`, run-streaming,
  run-cancellation API surfaces; consult during the scoping audit for what
  Options A / B / C / E can actually rely on.
- `deepagents.middleware.async_subagents` — the in-process call boundary for
  Option D analysis.

### Source-of-truth (operational)

- `/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  — Addendum 5 with the full evidence chain (correlation_id `e9433033-…`,
  the chat-side observation, the wire-side null-publish observation, the
  sidecar-side TypeError that masks the eventual happy-path question).

### Existing scoping doc (shape reference)

- `docs/research/forge-orchestrator-wiring-gap.md` — the FEAT-FORGE-010 anchor
  doc; same shape F010M's scoping doc follows at smaller scope.
- `docs/research/forge-orchestrator-wiring-feature-context.md` — the
  `--context` evaluation that fed `/feature-spec` for FEAT-FORGE-010; precedent
  for what AC-5's `--context` payload should look like.

### Sibling tasks (the chain that surfaced this)

- [`TASK-FORGE-FRR-F010F`](../../completed/TASK-FORGE-FRR-F010F/TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md)
  — sync-raise safety-net; predecessor — F010M extends the contract to the
  async-completion path.
- [`TASK-FORGE-FRR-F010J`](../../completed/TASK-FORGE-FRR-F010J/TASK-FORGE-FRR-F010J-wire-langgraph-runner-sidecar-url-into-async-subagent-registration.md)
  — sidecar URL threading; F010M's prerequisite (without a working dispatch,
  there's no async run to bridge from).
- [`TASK-FORGE-FRR-F010L`](TASK-FORGE-FRR-F010L-retarget-autobuild-runner-to-llama-swap-qwen3-code-next.md)
  — sibling, lands first; once F010L closes the autobuild can actually
  progress through stages and the F010M scoping doc has empirical input.
- `TASK-FW10-009` — validation surface and build-failed paths — likely partial
  coverage; audit during scoping.
- `TASK-FW10-010` — pause-resume publish round-trip — `design_approved`;
  cross-cutting concern #3 above.
- `TASK-FW10-011` — capstone integration test — `design_approved`; may
  already commit to a bridge shape per cross-cutting concern in
  §Implementation Notes.

### Run that surfaced this

- **correlation_id**: `e9433033-ea80-449f-885d-b2d1bdfb839e`
- **Date**: 2026-05-04 (late evening rerun, post-F010J)
- **Machine**: GB10 (`promaxgb10-41b1`)
- **forge HEAD**: working tree post `8d08b93` (F010J in working tree,
  uncommitted at the time)
- **Image**: `forge:latest` = sha256 `807c65f13c842...`
- **Sidecar**: `langgraph dev --config forge.langgraph.json --port 8124`
  (langgraph-cli 0.4.24 / langgraph-api 0.8.5)
- **Dispatch outcome**: `httpx: HTTP Request: POST http://localhost:8124/runs
  "HTTP/1.1 200 OK"` + `dispatch_autobuild_async: launched
  task_id=019df49e-d419-79a2-9f9b-307a935b9157` — autobuild graph launched on
  the sidecar successfully.
- **Async outcome on the sidecar**: `TypeError: "Could not resolve
  authentication method..."` — the autobuild_runner's first node calls
  Anthropic Claude; sidecar has no `ANTHROPIC_API_KEY`. F010L addresses this.
- **Wire outcome**: zero outbound `pipeline.*` envelopes from forge (this is
  the F010M gap; the sidecar's failure is invisible to the operator because
  there's no bridge from sidecar → wire).
