# Scoping: forge `autobuild_runner` ↔ pipeline-lifecycle-emitter bridge

## Status

DRAFT — produced by TASK-FORGE-FRR-F010M Phase 1. Will be passed to
`/feature-spec` and `/feature-plan` as `--context` once Phase 1 completes.

**Phase 1 status (2026-05-06)**: COMPLETE — ready for `/feature-spec`.

- ✅ Existing wiring audit (FW10-009 / FW10-010 / FW10-011 / F010F / emitter
  call sites) — key finding: F010M is real feature work; the existing FW10
  surface does not close the gap, and **FW10-010's design is structurally
  broken by F010J's sidecar shape**.
- ✅ Problem (Symptom + Why restatement) — self-contained for `/feature-spec`
  readers.
- ✅ Design space — six options (A polling, B webhooks, C streaming,
  D in-sidecar emit, E hybrid, F `runs.join`) with pros/cons/open-questions
  and per-option `langgraph-sdk==0.3.13` endpoint audit.
- ✅ Cross-cutting concerns — seven concerns × six options matrix; concerns
  #5 and #7 flagged as option-orthogonal so they don't drift into per-option
  work.
- ✅ Open questions for `/feature-spec` — eight questions whose answers
  split the design space into testable behaviors.
- ✅ Recommended option — **C (Streaming subscription)** with **E (Hybrid)**
  as named fallback. Pick + rationale + dominant risk written F010I-shape.

---

## Problem

After F010J, forge's autobuild dispatch chain works end-to-end on the
synchronous side: the inbound `pipeline.build-queued.<feature_id>` envelope
is consumed by `pipeline_consumer.handle_message`, validated, persisted to
SQLite, and dispatched via `dispatch_autobuild_async` which posts a run to
the langgraph-runner sidecar at `http://localhost:8124/runs` and returns
HTTP 200 with a real `task_id`. The autobuild_runner graph then executes
asynchronously **inside the sidecar's process**.

**The gap**: no outbound `pipeline.build-started.*`, `pipeline.stage-complete.*`,
`pipeline.build-complete.*`, `pipeline.build-failed.*`, `pipeline.build-paused.*`,
or `pipeline.build-resumed.*` envelope ever reaches the JetStream wire from
forge for an autobuild that runs (or fails) inside the sidecar. F010F's
sync-raise safety net only fires when `dispatch_build` raises before
`astart_async_task` returns; once the HTTP 200 lands, F010F is out of scope.
DDR-007 Option A's in-process emitter handle (passed as
`ctx['lifecycle_emitter']` through `start_async_task`'s `context` kwarg) is
not JSON-serialisable and is silently dropped at the HTTP boundary, so
`autobuild_runner._update_state` running inside the sidecar has no working
emitter to call.

**Empirical evidence** (RESULTS Addendum 5, correlation_id
`e9433033-ea80-449f-885d-b2d1bdfb839e`, 2026-05-04 evening rerun on GB10):

- The `pipeline.>` JetStream tail captured **only** the inbound
  `pipeline.build-queued.FEAT-43DE` envelope. Zero outbound publishes from
  forge, despite the dispatch chain logging an HTTP 200 launch and a real
  `task_id` (`019df49e-d419-79a2-9f9b-307a935b9157`).
- The supervisor's chat-side answer was honest about the silence:
  > "No progress events have come through yet — the build is still sitting
  > in the queue waiting for Forge to pick it up. ... I don't have a direct
  > way to check the live status of a queued build — I'd need to wait for
  > the pipeline progress events to arrive."
- The sidecar's run did fail asynchronously (the
  `ANTHROPIC_API_KEY`-not-resolvable TypeError that F010L addresses), so
  the operator has a real "build failed" condition to be notified of —
  yet sees nothing on the wire and nothing in the chat REPL.

**Contract violated**: DDR-030 between-prompt notification — every
state-transition the autobuild reaches must produce a wire-visible envelope
the operator can render in chat. The DDR makes no exception for "the run
happens in a sidecar"; the location of the runtime is an implementation
detail the operator should not see.

**What the bridge must publish**: per `docs/design/contracts/API-nats-pipeline-events.md`,
the eight `pipeline.{event}.{feature_id}` subjects. Of those, today's wire
gap is on the post-launch subset: `build-started`, `stage-complete`,
`build-paused` (sidecar-side; `build-resumed` from `approval_subscriber`
survives F010J), `build-complete`, `build-failed` (async-failure case),
`build-cancelled`. `build-queued` is operator-side; `build-progress` is
heartbeat-shaped and follows the same emit-site rules as `stage-complete`.

**Post-F010L expected steady state** (note for `/feature-spec` readers): once
F010L lands, the autobuild_runner runs against `openai:qwen36-workhorse` on
the local llama-swap workhorse and progresses through real lifecycle stages
inside the sidecar. The bridge problem is the reason none of those
transitions become wire-visible — F010L gives the wire something to
publish, F010M gives forge the way to publish it.

---

## Existing wiring audit

> *Audit completed 2026-05-06 by F010M Phase 1. The headline finding: the
> existing FW10-009 / FW10-010 / FW10-011 / F010F surface, taken together,
> does not close the F010M gap. FW10-009 and F010F are sync-only (forge-side
> validation + sync-raise safety net). FW10-010's design predates the F010J
> sidecar shape and is structurally broken by it. FW10-011 deliberately
> short-circuits over the sidecar boundary (mocks `AutobuildDispatcher.dispatch`)
> and therefore neither constrains nor validates the bridge. F010M remains
> real feature-shaped work.*

### FW10-009 (validation surface and build-failed paths)

**What it covers** (per task body + `pipeline_consumer.py:354-446`): three
synchronous validation paths in `pipeline_consumer.handle_message`:

1. Malformed payload → `_safe_publish_failure(...)` + ack (ASSUM-013).
2. Duplicate `(feature_id, correlation_id)` → ack and skip; no second
   `build-started` (ASSUM-014).
3. Worktree-allowlist failure → `_safe_publish_failure(...)` + ack **before**
   any orchestrator dispatch (ASSUM-015).

Plus a "dispatch errors contained" AC: an exception during dispatch leaves the
affected build at `failed`, the daemon stays running, the next message is
processed.

**Status**: `design_approved` per frontmatter; landed per F010F's task body
(F010F builds on FW10-009's `_safe_publish_failure` helper) and per the
README post-merge follow-up tracking. Live in
`src/forge/adapters/nats/pipeline_consumer.py` lines 354-446 (publishes) and
365 / 391 / 414 / 436 (call sites).

**Does it cover the async-completion path?** **No.** Every FW10-009 publish
fires **before** `dispatch_build` is invoked, in the consumer's sync
validation phase. The post-validation, post-dispatch async path (sidecar
finishes / fails / stalls) is firmly out of scope. The "dispatch error
contained" AC narrowed to "the daemon doesn't wedge"; it explicitly does
**not** mandate a terminal envelope on async failure (that's F010F-shaped,
which itself only covers sync raises).

**Bottom line for F010M**: FW10-009 is the sync-side counterpart F010M's
async-side bridge must compose with **without** double-publishing the
validation paths. No coverage to fold; no constraint to honour beyond
"don't double-publish".

### FW10-010 (pause-resume publish round-trip)

**What it commits to** (per task body + DDR-007 §Decision Option A): two
in-process emit call sites:

1. `emit_build_paused` from `autobuild_runner._update_state` when lifecycle
   transitions to `awaiting_approval`. Wired today via
   `LifecycleEmitterAdapter` at `src/forge/subagents/autobuild_runner.py:478`,
   with the routing table `LIFECYCLE_TO_PIPELINE_EMIT` mapping
   `awaiting_approval → emit_paused`, `running_wave (after awaiting_approval)
   → emit_resumed`, `completed → emit_complete`, `cancelled → emit_cancelled`,
   `failed → emit_failed` (`autobuild_runner.py:466-475`).
2. `emit_build_resumed` from `src/forge/adapters/nats/approval_subscriber.py`
   resume path (one-line addition per DDR-007 §Decision).

**Status**: `design_approved`, not yet implemented per frontmatter (as of
2026-05-06). The adapter scaffolding is in place; the in-process happy path
is exercised only by `tests/forge/test_autobuild_runner.py`.

**Does it assume in-process emitter?** **Yes — explicitly.** The module
docstring at `autobuild_runner.py:60-70` and class docstring at line 481
both state DDR-007 Option A: the `PipelineLifecycleEmitter` is threaded
onto the subagent's context payload as an in-process Python object, and
`LifecycleEmitterAdapter._schedule` (line 685) calls
`asyncio.get_running_loop().create_task(...)` expecting the subagent's loop
to be the same loop that owns the NATS publisher.

**Is that assumption broken under F010J?** **Yes — structurally.** F010J
shipped `dispatch_autobuild_async` (`pipeline/dispatchers/autobuild_async.py:325`)
threading `lifecycle_emitter` into the `launch_payload` dict
(`autobuild_async.py:498-504`) which is then JSON-serialised and POSTed to
langgraph-runner at `http://localhost:8124/runs`. The
`PipelineLifecycleEmitter` instance holds:

- a `PipelinePublisher` reference (open NATS connection),
- a `Clock` (sync/async hybrid),
- per-build `asyncio.Task` handles in `_progress_tasks`,
- a logger.

None of those are JSON-serialisable. So one of two things happens at the
HTTP boundary:

- (a) the langgraph SDK silently drops non-serialisable fields (most likely
  given F010J's runbook captured HTTP 200) → `ctx['lifecycle_emitter']`
  inside the sidecar is `None` or missing → `LifecycleEmitterAdapter.on_transition`
  is never invoked; no per-stage envelope reaches the wire; or
- (b) serialisation raises and the dispatch fails → contradicted by the
  HTTP 200 from the runbook.

Either way, the in-process emit channel from `_update_state` to the wire
is **structurally severed** under F010J. **FW10-010's design predates the
sidecar shape and cannot be implemented as written under it** — the pause
emit site (inside the subagent) does not work; the resume emit site (inside
forge daemon's `approval_subscriber`) does work because that one is purely
in-process to forge.

**Bottom line for F010M**: FW10-010 needs revisiting. The cleanest paths are
either (i) replace the in-process emitter handle with an out-of-process emit
mechanism (F010M's bridge problem proper), or (ii) co-deploy the autobuild
in the forge daemon process (revert F010J — out of scope here, ADR-ARCH-031
already chose against this for cancellability). The sub-feature-runner emits
are part of F010M's bridge surface, not a separable concern. **F010M's
wave-plan should fold FW10-010's commitments rather than duplicate them.**

### FW10-011 (capstone integration test)

**What it commits to** (per task body §Files + §Implementation notes): an
end-to-end test in `tests/integration/test_forge_serve_orchestrator_e2e.py`
that:

1. Spins `forge serve` against an embedded NATS server + temp SQLite.
2. **Mocks `AutobuildDispatcher.dispatch` at the boundary** — the autobuild
   "runs" as a scripted sequence of `_update_state` transitions through the
   **real in-process** `PipelineLifecycleEmitter`. No real worktree, no real
   DeepAgents subagent, no real sidecar.
3. Publishes one `pipeline.build-queued.FEAT-XXX` envelope and asserts the
   canonical lifecycle sequence + correlation_id threading + ordering
   invariants.

**Status**: `design_approved`, not yet implemented (no
`tests/integration/test_forge_serve_orchestrator_e2e.py` on disk as of
2026-05-06).

**Does it commit to a bridge shape?** **No — it explicitly avoids the
bridge question.** Mocking `AutobuildDispatcher.dispatch` short-circuits
over the sidecar boundary. The test exercises the in-process composition
(emitter ↔ publisher ↔ NATS) and the consumer's sync paths (FW10-009 +
F010F territory). It does **not** exercise the cross-process boundary
F010J introduced.

**Implication for F010M**: FW10-011 neither constrains F010M's option
choice nor validates the production sidecar deployment. **A new
sidecar-aware integration test is part of F010M's wave-plan deliverables**,
either as a separate test file or by amending FW10-011's design to
optionally exercise a real sidecar spin-up. The current FW10-011 design
should still land — it locks the in-process composition contract — but
F010M's wave-plan must add the missing sidecar-aware coverage so
"the wire goes silent when the sidecar runs" cannot regress into prod again.

### F010F safety-net publish

**What it covers** (per task body + `pipeline_consumer.py:470-506`): when
`dispatch_build` raises **synchronously** before the running state machine
records any transition, the consumer publishes `pipeline.build-failed.<feature_id>`
before acking, threading the inbound `correlation_id` via the existing
`_safe_publish_failure` / `_failure_payload` helpers. Comment block updated
to narrow ADR-ARCH-008's "no duplicate publish" protection to "when the
state machine has started".

**Boundary**: F010F publishes when `dispatch_build` raises out of
`pipeline_consumer.handle_message`'s outer try/except. Once `astart_async_task`
returns (HTTP 200 from the sidecar), `dispatch_build` exits cleanly and F010F
is out of scope. Async outcomes from inside the sidecar — success, async
failure, async stall, operator cancel — are **never** observed by F010F's
exception handler.

**Bottom line for F010M**: F010F is the sync-raise floor; F010M is the
async-completion ceiling. The contract F010M's bridge must honour:
**publish in cases F010F does not cover, without double-publishing in cases
F010F does cover**. The split is clean — F010F fires inside the consumer's
exception handler; F010M's bridge fires from the post-dispatch async-result
path, never reaching that handler.

### Existing `PipelineLifecycleEmitter` call sites

**Class location**: `src/forge/pipeline/__init__.py:273` (`PipelineLifecycleEmitter`).
The eight `emit_*` methods are at lines 320 (`emit_started`), 332
(`emit_progress`), 354 (`emit_stage_complete`), 390 (`emit_paused`), 423
(`emit_paused_then_interrupt`), 468 (`emit_resumed`), 491 (`emit_complete`),
522 (`emit_failed`), 543 (`emit_cancelled`), plus the periodic loop at 638
and the dispatcher at 594 (`on_transition`). All emit methods are coroutines;
all call `_safe_publish` (line 731) which catches `PublishFailure` per
ADR-ARCH-008.

**Existing in-process call sites** (audit by `grep` of the forge tree):

| Call site | Process | Coverage |
|---|---|---|
| `autobuild_runner._update_state` → `LifecycleEmitterAdapter.on_transition` (`autobuild_runner.py:539`) → schedules `emit_paused` / `emit_resumed` / `emit_complete` / `emit_cancelled` / `emit_failed` on the running loop | **sidecar** under F010J | **broken** — emitter Python handle does not survive HTTP boundary |
| `approval_subscriber.py` resume path (FW10-010 deliverable, not yet on disk) → `emit_resumed` | forge daemon | will work once FW10-010 lands |
| `pipeline_consumer.py` sync validation publishes (FW10-009) → bypasses the emitter entirely; calls `PipelinePublisher.publish_*` directly via `_safe_publish_failure` helper | forge daemon | unaffected |
| `pipeline_consumer.py` F010F sync-raise publish → same shape as FW10-009 (direct publisher call) | forge daemon | unaffected |
| `forge.lifecycle.recovery._handle_preparing` (F010D-forge) → publishes a recovery-time terminal envelope | forge daemon, recovery time | unaffected |
| `autobuild_runner.build_stage_complete_kwargs` + per-stage `emit_stage_complete` (ASSUM-018, target_kind="subagent") | **sidecar** under F010J | **broken** — same root cause as the lifecycle adapter |

**Bridge's relationship to the emitter** — the design space splits on
this question:

- **In-process replacement of the emitter handle** (Option D-shape) — the
  bridge runs in-sidecar, calls a different `Emitter`-shaped Protocol
  whose implementation is an HTTP/NATS client posting back to forge.
  Re-uses `_update_state`'s call boundary; replaces what
  `ctx['lifecycle_emitter']` points to.
- **Out-of-process observation of the run** (Options A/B/C) — the bridge
  runs in forge, observes the sidecar's runtime via polling/webhook/SSE,
  translates observed transitions into `emit_*` calls on the in-forge
  emitter. Does **not** re-use `_update_state`'s call boundary; the
  in-sidecar emit is dropped on the floor.
- **Hybrid** (Option E) — terminal events via observation; per-stage events
  via in-sidecar bridge.

The §Design space subsections expand each.

### langgraph-runner SDK surface (audit deferred to §Design space)

The langgraph-cli 0.4.24 / langgraph-api 0.8.5 endpoint surface that
Options A / B / C / E rely on (`/threads`, `/runs`, `/runs/<id>` polling,
`/runs/<id>/stream` SSE, `DELETE /runs/<id>`, optional webhooks) is audited
inline in each option's subsection rather than here, because the relevant
endpoint differs by option. The audit's job at this stage is "what FW10
already commits to"; concrete SDK call shapes belong with the option that
consumes them.

---

## Design space

> *Each option is sketched with (a) what it does, (b) pros, (c) cons, (d)
> per-option open questions, plus a langgraph-sdk endpoint audit where
> relevant. Verified against `langgraph-sdk==0.3.13` (the version on the
> implementer's machine; langgraph-cli 0.4.24 / langgraph-api 0.8.5 per the
> runbook expose the same surface). The §Recommended option section comes
> last; per F010I-shape this section pre-ranks nothing.*

### Option A — Polling

**(a) Sketch.** After `dispatch_autobuild_async` returns, a forge-side
background coroutine (per build, or one shared poller against a registry
of in-flight `(thread_id, run_id)` pairs) calls
`langgraph_sdk.LangGraphClient.runs.get(thread_id, run_id)` (`GET
/threads/{tid}/runs/{rid}`) on a fixed cadence. On observing a state
transition (or terminal `Run.status`), the poller calls the corresponding
in-forge `PipelineLifecycleEmitter.emit_*` coroutine, which publishes onto
NATS via the existing in-forge publisher. The pollers are owned by the
forge daemon; they spin down when the run reaches a terminal state.

**(b) Pros.**

- Simplest plumbing — one HTTP call shape (`runs.get`), no SSE / no webhook
  endpoint, no in-sidecar code change.
- The full emit chain stays in forge's process — re-uses the existing
  in-forge `PipelineLifecycleEmitter` instance, the existing
  `PipelinePublisher`, the existing `_safe_publish` swallow-and-log
  semantics, the existing F010C correlation_id threading.
- Composes cleanly with daemon-restart recovery: the registry of in-flight
  `(thread_id, run_id)` pairs can be persisted in SQLite alongside the
  existing `builds` / `stage_log` / `async_tasks` rows; on restart, forge
  re-reads the registry and re-launches the pollers.
- No constraint on the in-sidecar code — the autobuild_runner doesn't need
  to know forge is observing it.

**(c) Cons.**

- Latency = poll-interval. A 1-second poller introduces up to 1s of latency
  per state transition; a 100ms poller burns ~600 RPM per in-flight build.
  ADR-ARCH-014 caps in-flight builds at one, so the burn is bounded — but
  a long-running build is doing 600 polls/min for hours.
- The bridge has to **diff** the observed state against the previous poll
  to detect transitions, which means the bridge needs a model of "what
  state we last published". That state lives in memory (lost on daemon
  restart) or in SQLite (an additional schema concern).
- `runs.get` returns a `Run` object — a snapshot of the run's status, not
  per-stage `stage-complete` events. To emit `stage-complete` envelopes
  the bridge would need to inspect the **thread state** (the
  `async_tasks` channel content via `client.threads.get_state(tid)`),
  diff it across polls, and synthesise `stage-complete` envelopes from
  observed `AutobuildState` mutations. That's substantial state-diffing
  logic on the bridge side.
- The diffing approach is fragile: any transition the poller misses
  between polls (e.g. fast `running_wave → awaiting_approval → running_wave`
  resume) is permanently lost. The wire would show only the snapshot at
  poll time, not the actual sequence.

**(d) Open questions.**

- What's the right poll cadence? 1s feels too slow for the operator (chat
  REPL renders staleness). 100ms is wasteful but achievable. Adaptive
  (slow when no transitions observed; fast when one was just seen)?
- Does the bridge need per-feature stateful diffing, or can it lean on
  `client.threads.get_history(tid)` to read every checkpoint since the
  last observed `last_event_id`-equivalent?
- Where does the `(thread_id, run_id)` registry live? In-memory dict keyed
  on `build_id`, or a new SQLite table, or a new column on `builds`?

**Endpoint audit.**

- `client.runs.get(thread_id, run_id)` → `langgraph_sdk/_async/runs.py:901-934`
  → `GET /threads/{tid}/runs/{rid}`. Returns the SDK's `Run` model with
  `status` ∈ {pending, running, error, success, timeout, interrupted}.
- `client.threads.get_state(tid)` → reads the channel-state snapshot of the
  thread. Needed to inspect the `async_tasks` channel content for
  per-stage transitions (the `Run` object alone doesn't expose
  `AutobuildState`).
- Reconnect-after-restart shape: re-call `runs.get` with the same
  `(thread_id, run_id)`; HTTP 404 means the run was garbage-collected
  (config-dependent; default 24h retention in langgraph-api 0.8.5 — verify
  during implementation).

### Option B — Webhooks

**(a) Sketch.** When `dispatch_autobuild_async` calls
`runs.create(..., webhook="http://localhost:<forge_port>/webhooks/run-completed")`,
the langgraph-runner posts a JSON payload to forge on **terminal-state
arrival only**. Forge spins up an ASGI surface (httpx-style ASGI app or a
small FastAPI routing) bound to a dedicated port; the webhook handler
parses the run-result body and calls the matching `emit_complete` /
`emit_failed` / `emit_cancelled` coroutine on the in-forge emitter.
Per-stage events (start, stage-complete, paused) are **out of scope** for
B alone — webhook is terminal-only — so B is most useful as the terminal
half of a Hybrid (Option E).

**(b) Pros.**

- Zero polling cost on the happy path — terminal arrives, callback fires,
  envelope published.
- Lowest latency on terminal (≈ network round-trip).
- Run-create signature already supports webhook as a first-class kwarg;
  no in-sidecar code change required.

**(c) Cons.**

- Requires forge to expose an inbound HTTP surface — a new attack surface,
  a new port to manage, a new authentication concern (today the sidecar
  is trusted because it's local-only, but webhook *path* still needs a
  shared-secret to prevent third-party callers from spoofing terminals).
- **Webhook is terminal-only by design.** Per-stage transitions
  (`stage-complete`, paused, resumed, started) cannot be observed via
  webhook. So B is incomplete on its own.
- Webhook delivery is best-effort. If the webhook POST fails (forge daemon
  down, mid-restart, etc.), the langgraph-runner does not retry (verify
  this — langgraph-api 0.8.5 may have a retry config). Daemon-restart
  recovery would still need a polling sweep for "any in-flight run we
  didn't get a webhook for".
- Adds an ASGI surface to the forge daemon, which today is a pure NATS
  consumer. The codebase is structured around `forge serve` being a NATS
  worker; landing an HTTP server inside it is a non-trivial wiring
  choice (see F010G's deferred Option A2 — "ASGI surface in forge"
  was already considered and deferred).

**(d) Open questions.**

- Does `langgraph-api` 0.8.5 retry webhook delivery on transient failures?
  If no, the recovery story collapses to "polling sweep on restart" — at
  which point Option A is most of the work anyway.
- What's the auth model? HMAC over the body with a shared secret? IP
  allowlist (only `127.0.0.1`)? Both?
- Where in forge does the ASGI surface live — co-deployed with `forge
  serve`, or a separate `forge webhook-server` daemon? F010G already
  surfaced this question and deferred it.

**Endpoint audit.**

- `client.runs.create(..., webhook=str|None)` → `langgraph_sdk/_async/runs.py:379,408,436`
  → `POST /threads/{tid}/runs` with `webhook` field on the body. Confirmed
  first-class in 0.3.13.
- `runs.create(..., on_completion="delete"|"keep")` → controls whether
  langgraph-api auto-deletes the run after terminal. Set to "keep" so a
  webhook-loss recovery sweep can still find the terminal state on disk.
- No `/threads/{tid}/runs/{rid}/webhooks` endpoint — webhook is per-run,
  set at create time only.

### Option C — Streaming subscription

**(a) Sketch.** After `dispatch_autobuild_async` returns the `(thread_id,
run_id)`, a forge-side background coroutine opens
`client.runs.join_stream(thread_id, run_id, last_event_id=...)` (`GET
/threads/{tid}/runs/{rid}/stream`, SSE), iterating the
`AsyncIterator[StreamPart]` and translating each event into the
corresponding `emit_*` call on the in-forge emitter. The coroutine runs
for the duration of the run; on disconnect it reconnects with the
last-seen `last_event_id` (the SDK's `join_stream` `last_event_id` kwarg
threads through the `Last-Event-ID` HTTP header per
`runs.py:1142-1147`).

**(b) Pros.**

- Real-time, no polling cost — events are pushed as they happen.
- Native to langgraph-runner — `stream_mode` was set on `runs.create`
  anyway (default `"values"`); the SDK ships `join_stream` for exactly
  this re-attach pattern.
- `last_event_id` resume semantics solve the daemon-restart recovery
  problem natively: forge persists the last event id observed per run,
  and on restart resumes from there. No diffing logic.
- Captures every transition with no race window — SSE is ordered per
  connection.

**(c) Cons.**

- Long-lived HTTP connection per in-flight build. ADR-ARCH-014 caps
  in-flight at one so it's a single connection, but the connection
  reconnect logic is non-trivial (drop on transient network blip; resume
  with `Last-Event-ID`).
- The SSE payload shape is `StreamPart` — not the same shape as
  `pipeline.*` envelopes. The bridge needs a translation layer that maps
  raw graph events / channel updates → typed `BuildStartedPayload` /
  `StageCompletePayload` / etc. That mapping requires inspecting the
  `AutobuildState` mutations carried in `values` mode (the channel-state
  snapshots).
- Every reconnect is a new HTTP connection; if the sidecar enforces a
  per-IP connection limit forge could be temporarily blocked from
  re-attaching during a hot loop.
- The SDK's `cancel_on_disconnect=False` default is what we want, but it's
  a footgun: a misconfigured `True` would cancel the run on a forge
  daemon crash, which would conflate "forge restarted" with "operator
  cancelled" — needs an assertion in the bridge wiring.

**(d) Open questions.**

- What's the canonical event shape for an `AutobuildState` mutation in
  `stream_mode="values"`? Does the SDK guarantee monotonic event-id
  numbering across reconnects?
- How does the bridge handle the "stream ends because the run hit
  terminal" case vs "stream ends because the network dropped" case? The
  SDK's `join_stream` returns a normal generator — distinguishing the
  two requires checking the run's terminal status via `runs.get` after
  the stream closes.
- Does `cancel_on_disconnect=False` survive a forge daemon SIGTERM cleanly,
  or does the connection's TCP RST count as a "cancel"? Probably no, but
  needs verification.

**Endpoint audit.**

- `client.runs.join_stream(...)` → `langgraph_sdk/_async/runs.py:1090-1147`
  → `GET /threads/{tid}/runs/{rid}/stream`. SSE; `last_event_id` is
  threaded as `Last-Event-ID` header.
- `stream_mode` parameters and the `StreamPart` model are documented in
  `langgraph_sdk/_async/runs.py:73-194` (the `stream` overloads) and
  `langgraph_sdk/schema.py` (the `StreamPart` shape — verify during
  implementation).

### Option D — In-process emit from inside the subagent (replace the handle)

**(a) Sketch.** The autobuild_runner's existing in-process emit boundary
(`_update_state` → `LifecycleEmitterAdapter.on_transition`) is preserved,
**but the object passed in `ctx['lifecycle_emitter']` is replaced with a
serialisable proxy** that, when called, posts a JSON envelope back to
forge over a small HTTP/NATS surface forge owns. In effect, the bridge
runs **inside the sidecar** — the autobuild_runner emits via a remote
client; the in-forge daemon receives the publish requests on a dedicated
endpoint and writes them onto the existing `PipelinePublisher`.

There are two sub-shapes:

- **D-HTTP**: forge exposes an inbound HTTP endpoint (similar to Option B's
  webhook surface but receiving the full lifecycle emit catalogue, not
  terminal only). The proxy in the sidecar is an httpx client.
- **D-NATS**: forge exposes a NATS subject (e.g.
  `pipeline.bridge-emit.<feature_id>`) that the proxy publishes onto from
  the sidecar; forge's existing NATS consumer subscribes and translates
  to the canonical `pipeline.{event}.<feature_id>` subject.

**(b) Pros.**

- Re-uses `_update_state`'s call boundary — FW10-010's design (the
  `LIFECYCLE_TO_PIPELINE_EMIT` table, the awaiting_approval → emit_paused
  routing, the running_wave-after-awaiting_approval → emit_resumed edge)
  composes unchanged. The pause-resume contract is preserved.
- Captures every transition the runner records — no polling diffing, no
  SSE translation logic.
- D-NATS variant has zero new ASGI surface; reuses the NATS bus forge is
  already on.

**(c) Cons.**

- Adds an in-sidecar dependency. The autobuild_runner's `LifecycleEmitterAdapter`
  needs to know how to talk to forge (URL, subject name, auth) — that
  configuration must be threaded through `ctx` at dispatch time. The
  config-threading itself is small but is a new contract (a new field on
  the launch payload that **is** JSON-serialisable, unlike the current
  `PipelineLifecycleEmitter` reference).
- D-HTTP creates the same ASGI-on-forge concern as Option B — already
  surfaced and deferred in F010G.
- D-NATS requires the sidecar to know forge's NATS broker address and
  hold credentials. The sidecar today has no NATS dependency; adding
  one expands the sidecar's surface area meaningfully (a new dep, a new
  config knob, a new failure mode at sidecar startup).
- Every emit is a synchronous round-trip from inside the runner's event
  loop. The runner's emit raises today are caught and logged at WARNING;
  remote emits would surface network errors at the same log level, but
  every emit becomes I/O-bound rather than in-memory.
- Running the bridge in-sidecar means the sidecar holds the threading
  invariant (correlation_id on every publish). The sidecar would have to
  be trusted to thread `correlation_id` correctly; today that invariant
  is enforced by AST lint guards inside forge's test suite (see
  `tests/forge/test_recovery_correlation_id.py`'s F010D-forge precedent).
  Lint guards do not naturally extend across the process boundary.

**(d) Open questions.**

- Which sub-shape: D-HTTP (single ASGI surface forge already considers
  for B) or D-NATS (sidecar gains a NATS dep)?
- How is the in-sidecar proxy configured? An entry in the
  `start_async_task` `context` dict (e.g. `{"emit_endpoint": "http://...",
  "emit_secret": "..."}`)? Or environment variables?
- Does the in-sidecar adapter need backpressure / batching (the runner
  could in principle emit faster than the network can drain)?
- How is sidecar code change managed? The autobuild_runner module is
  shipped via langgraph.json; updates require a sidecar restart. The
  rollout story is non-trivial.

**Endpoint audit.**

- N/A on the langgraph SDK side — D doesn't consume langgraph endpoints;
  it ships forge-owned endpoints the sidecar consumes. The langgraph
  surface is unaffected.

### Option E — Hybrid (terminal-via-webhook OR -join, per-stage in-sidecar)

**(a) Sketch.** Combine D's per-stage in-sidecar emit (preserving FW10-010's
contract for `awaiting_approval` / resume / per-stage `stage-complete`)
with B's webhook (or A's `runs.join` polling, whichever survives the
recovery audit) for terminal envelopes. Per-stage events go via D's
in-sidecar bridge; `build-complete` / `build-failed` (async-failure case)
/ `build-cancelled` go via the terminal-observer surface in forge. The
crash-recovery story rests entirely on the terminal-observer (forge can
recover an in-flight build's terminal state via the sidecar's run record
even if every per-stage event was lost during a daemon restart).

**(b) Pros.**

- Per-stage events arrive with the lowest possible latency (in-sidecar
  emit, no observation lag).
- Terminal events have a **belt-and-braces** path — webhook on the
  happy path, recovery-time polling sweep on the unhappy. The wire is
  guaranteed to see the terminal envelope.
- Crash-recovery is structurally clean: forge's terminal observer
  reconciles in-flight builds against `runs.list(thread_id)` on startup
  and emits any missed terminals.
- Splits the cross-cutting concerns: pause-resume (FW10-010) stays
  in-sidecar; terminal-state-survives-restart stays in-forge.

**(c) Cons.**

- Two sub-paths to maintain — the in-sidecar emit chain and the
  terminal-observer chain. The wave-plan would carry both.
- Risk of double-publishing on terminal: if the in-sidecar runner emits
  `failed` *and* the webhook arrives, the wire sees two
  `pipeline.build-failed.<feature_id>`. Mitigated by enforcing
  "terminal envelope on the wire is owned by exactly one of {sidecar,
  forge-observer}, not both" — which means the in-sidecar adapter must
  drop terminal emits and let the observer own them. That's a routing
  rule on top of D's existing routing rule.
- All of D's cons + most of B's cons + a routing-table rule that doesn't
  exist today.

**(d) Open questions.**

- Which observer surface for terminal — Option B (webhook) or Option A
  (`runs.join` blocking call per build)? `runs.join` reads cleaner
  because it's already SDK-shaped and doesn't need an inbound ASGI
  surface in forge.
- How is the "in-sidecar adapter drops terminals" rule enforced — by
  removing the terminal entries from `LIFECYCLE_TO_PIPELINE_EMIT`, or by
  routing them to a no-op proxy?
- Does the recovery-time terminal sweep on daemon restart happen in
  `forge.lifecycle.recovery._handle_preparing` (F010D-forge's recovery
  shape) or in a new module?

### Option F — runs.join (blocking-await per build)

**(a) Sketch.** A degenerate form of A. After `dispatch_autobuild_async`
returns, forge spins up one background coroutine per in-flight build that
calls `client.runs.join(thread_id, run_id)` (`GET
/threads/{tid}/runs/{rid}/join`) — a single HTTP call that **blocks
server-side** until the run reaches a terminal state, then returns the
final thread state. On return, the bridge emits `build-complete` /
`build-failed` / `build-cancelled` and exits. **Terminal-only; never sees
per-stage events.**

**(b) Pros.**

- Single HTTP call per run, no polling, no SSE — the simplest possible
  bridge surface.
- The returned `dict` is the final thread state, so the bridge can
  inspect terminal `AutobuildState` and emit a fully-populated
  `build-complete` (tasks_completed, tasks_failed, etc.).

**(c) Cons.**

- Terminal-only — cannot publish `build-started`, `stage-complete`,
  `build-paused`, or `build-resumed`. The chat REPL still goes silent
  during the run, which is the headline DDR-030 violation.
- Long-running blocking HTTP call — connections held for the duration of
  the build (potentially hours). Daemon restart drops the connection;
  recovery has to re-establish, which means re-calling `runs.join` on
  restart — workable but needs the registry of in-flight pairs that A
  also needs.
- Useful only as the terminal half of a Hybrid (Option E with `runs.join`
  in place of webhook).

**(d) Open questions.**

- Worth listing as a standalone option, or fold it into Option E as the
  preferred terminal-observer mechanism (since it's strictly less
  surface-area than webhook)?

**Endpoint audit.**

- `client.runs.join(...)` → `langgraph_sdk/_async/runs.py:1053-1088`
  → `GET /threads/{tid}/runs/{rid}/join` → returns final thread state on
  terminal. SDK uses `request_reconnect` so the call survives transient
  network blips at the HTTP layer.

---

## Cross-cutting concerns

> *Each concern from the F010M task body, with per-option answers. Concerns
> with option-orthogonal answers (#5, #7) are flagged so they don't get
> duplicated work in the wave-plan.*

### 1. Daemon-restart recovery

The bridge state must survive `forge serve` restart mid-build. Every option
needs an **in-flight build registry** persisted in SQLite (the natural
shape: a new column on `builds` carrying `(thread_id, run_id, last_event_id)`
or a new sibling table). The registry is reconciled on startup against the
sidecar's `runs.list` to detect builds that are still alive.

| Option | Recovery shape |
|---|---|
| **A — Polling** | Re-launch pollers from registry; resume from "last observed status". Risk: any transition that fired during the restart window is **lost** (the poller next sees the new state, not the in-between transitions). |
| **B — Webhooks** | Webhooks fired during downtime are **lost** unless langgraph-api retries (verify in 0.8.5). Recovery requires an A-shaped sweep on startup → at which point B alone isn't sufficient. |
| **C — Streaming** | SSE `Last-Event-ID` replays missed events from the langgraph-api server-side buffer (verify buffer retention). **Cleanest recovery story** — no diffing, no missed transitions during the in-buffer window. |
| **D — In-sidecar emit** | Emits fired during forge daemon downtime are lost (D-HTTP) or queued in NATS (D-NATS, if the in-sidecar adapter publishes onto a JetStream stream forge consumes durably). D-NATS-on-JetStream gets cleaner recovery than D-HTTP. **Recovery still requires an A-shaped sweep** because the in-sidecar adapter has no way to learn forge's process state. |
| **E — Hybrid** | Per-stage events lost during downtime are gone (D's con). Terminal events recovered via the terminal observer's startup sweep (B+sweep, A, or F). Wire sees a **gap** in per-stage coverage but no lost terminals. |
| **F — `runs.join`** | Re-call `runs.join` on restart for every registry entry. SDK's `request_reconnect` handles the resumed call. Terminal-only; per-stage gap not addressable here. |

**Headline finding**: only **C** has a structurally clean per-stage recovery
story (via `Last-Event-ID`). Every other option either accepts a per-stage
gap or layers extra recovery infrastructure.

### 2. Deferred-ack contract

The inbound `pipeline.build-queued.*` envelope's ack timing under F010M.
Today: **never acked on the async path** — Addendum 5 captured a
redelivery storm every 30s absorbed by duplicate-detection (loud-but-harmless,
conditional on the in-flight build never actually completing). F010F acks
on sync-raise; FW10-009's validation paths ack-and-skip. The async-completion
path needs a parallel rule.

| Option | Ack point |
|---|---|
| **A** | Poller observes terminal status → invokes the inbound message's ack callback. |
| **B** | Webhook handler invokes ack callback on terminal arrival. |
| **C** | Stream observer invokes ack callback on terminal SSE event. |
| **D** | The in-forge receiver of the in-sidecar terminal emit invokes ack callback. |
| **E** | Same as the chosen terminal observer (B/A/F). |
| **F** | `runs.join` returns → ack. |

**Sub-question shared across all options**: the inbound message's
`ack_callback` lifetime currently scopes to `pipeline_consumer.handle_message`.
Under F010M, ack happens **after** `dispatch_build` returns successfully —
which means the consumer must hand the ack callback off to the bridge.
That's a **structural change to the consumer contract** — the consumer no
longer owns the ack lifetime on the async-success path; it transfers
ownership to the bridge.

**Implication for the wave-plan**: a separate sub-task to refactor the
consumer's ack contract is needed regardless of which bridge option is
chosen. This is its own piece of work.

### 3. FW10-010 pause-resume interaction

FW10-010's `design_approved` commits today: pause emit from
`autobuild_runner._update_state` (in-sidecar under F010J — broken),
resume emit from `approval_subscriber` (in-forge — survives F010J).

| Option | Pause emit site | Resume emit site |
|---|---|---|
| **A** | Bridge polls `threads.get_state`, observes `lifecycle="awaiting_approval"`, emits `build-paused` from forge. | Bridge observes `running_wave` (after awaiting_approval), or `approval_subscriber` direct emit. **Risk: double-publish** if both fire. |
| **B** | Out — terminal-only. | Out. |
| **C** | Bridge observes SSE `AutobuildState` mutation `lifecycle=awaiting_approval`, emits `build-paused` from forge. | SSE observation OR `approval_subscriber` direct emit. **Same double-publish risk.** |
| **D** | FW10-010's `LifecycleEmitterAdapter.on_transition` carries over unchanged — pause emit ships via the in-sidecar proxy. | `LifecycleEmitterAdapter` running_wave-after-awaiting_approval rule fires emit_resumed via the proxy. The `approval_subscriber` resume emit is **also** designed to fire (FW10-010 §Why). **Both fire → double-publish.** |
| **E** | Per-stage path is D-shaped → in-sidecar adapter ships pause emit. | D-shaped + `approval_subscriber` → still double-publish. |
| **F** | Out. | Out. |

**Headline finding**: every option that supports pause/resume at all
introduces a **double-publish risk** between the bridge and the
`approval_subscriber` resume emit. **F010M's wave-plan must pick one
canonical resume emit site** (most natural: the bridge owns pause+resume,
`approval_subscriber.py` is amended to skip the emit when a bridge is wired).
This **reshapes FW10-010's design** under any option choice — FW10-010
should be folded into F010M's wave-plan and its pause/resume site choices
re-decided.

### 4. Correlation_id threading on every emit

F010C's contract: every outbound `pipeline.*` envelope carries the inbound
`correlation_id`. F010C also installed AST lint guards in
`tests/forge/test_pipeline_consumer_correlation_id.py` and
`tests/forge/test_recovery_correlation_id.py` that fail CI when a publish
site drops correlation_id.

| Option | Threading shape | Lint-guard reach |
|---|---|---|
| **A, B, C, F** | Bridge runs in forge with access to the in-forge `BuildContext.correlation_id` (loaded from SQLite registry on dispatch). Threading is **trivial** — same shape as today's in-forge emits. | Existing AST lint guards extend naturally; new guards added for the bridge call site. |
| **D** | Sidecar must thread `state.correlation_id` onto every emit it ships back. The runner already carries it on `AutobuildState.correlation_id`. **AST lint guards do not extend across the process boundary** — need server-side schema validation on the in-forge receive endpoint that **rejects emits missing correlation_id**. New enforcement mechanism. |
| **E** | Per-stage emits inherit D's concern (sidecar threading + server-side validator). Terminal emits inherit A/B/F's trivial story. |

**Headline finding**: A/B/C/F preserve F010C's enforcement model unchanged.
**D and E** introduce a new enforcement burden and **D-NATS** in particular
needs a JetStream subject schema that contractually requires correlation_id.
This is a meaningful argument against D/E that didn't appear in the
design space sketches.

### 5. Observability of in-flight builds — option-orthogonal

Every option needs the in-flight build registry surfaced for
`forge status --in-flight`. The registry is the same artefact as #1's
recovery state. **F010M's wave-plan should grow `forge status --in-flight`
as a single sub-task that all options share** — it is not an
option-discriminator.

Open question deferred to `/feature-spec`: in-flight registry sub-task
in F010M's wave-plan, or separable follow-up? Recommend **in scope** for
F010M because the current redelivery-storm symptom (Addendum 5) would
have been visible-as-pending if the registry existed.

### 6. Retry semantics on transient sidecar failures

| Option | Transient failure shape |
|---|---|
| **A** | Network blip on `runs.get` → next poll retries. **Cheap and automatic.** No loss. |
| **B** | If langgraph-api retries the webhook (verify), retries are upstream. If not, recovery sweep covers transient failure-during-terminal. |
| **C** | SSE disconnect → reconnect with `Last-Event-ID`. **Bridge needs explicit reconnect-and-resume logic** with bounded backoff. SDK's `request_reconnect` is the building block (`langgraph_sdk/_async/runs.py:1082-1088`). |
| **D-HTTP** | Forge endpoint down → emit lost. Need retry-with-backoff in the in-sidecar adapter (substantial new code in the sidecar). |
| **D-NATS** | NATS reconnect handled by the NATS client library; emits queued in JetStream survive transient failures. **Cleanest of the D variants.** |
| **E** | Per-stage retry depends on D variant; terminal retry depends on observer. |
| **F** | SDK's `request_reconnect` handles transient failures during the long-blocking call. Clean. |

### 7. Cancellation paths — option-orthogonal

`client.runs.cancel(thread_id, run_id, action="interrupt"|"rollback")` is
the universal cancel surface (`langgraph_sdk/_async/runs.py:936-993`).
**Every option uses the same cancel mechanism**; the only delta is "how
does the bridge observe the cancel terminating?" — which is option-specific
and inherits each option's general termination-observation path.

The operator-side flow: forge receives a `pipeline.build-cancelled.*`
operator request → forge calls `runs.cancel(thread_id, run_id,
action="interrupt")` → the run reaches terminal → the bridge observes the
terminal (per its option-specific path) → emits `pipeline.build-cancelled`.

**Open question deferred to `/feature-spec`**: who emits `build-cancelled`
on the wire — forge's cancel handler synthesises it directly, or the
bridge synthesises it on observed terminal=interrupted? Either is
defensible. The latter unifies "all terminal emits flow through the
bridge"; the former is lower-latency.

---

## Cross-cutting summary table

| Concern | A | B | C | D-HTTP | D-NATS | E | F |
|---|---|---|---|---|---|---|---|
| #1 Recovery | OK (registry + sweep) | Weak (need sweep) | **Best (Last-Event-ID)** | Weak (need sweep) | OK (JetStream durable) | OK (per-stage gap, terminal robust) | OK (registry + re-join) |
| #2 Ack | OK | OK | OK | OK | OK | OK | OK |
| #3 FW10-010 | Reshape | Out | Reshape | Preserve | Preserve | Preserve | Out |
| #4 Correlation_id | **Trivial** | **Trivial** | **Trivial** | New enforcer | New enforcer + schema | Mixed | **Trivial** |
| #5 forge status | Free (registry) | Free (registry) | Free (registry) | Free (registry) | Free (registry) | Free (registry) | Free (registry) |
| #6 Transient retry | OK | OK | OK (explicit) | Weak (sidecar code) | OK (NATS client) | Mixed | OK |
| #7 Cancel | Same | Same | Same | Same | Same | Same | Same |

---

## Open questions for `/feature-spec`

> *Eight questions whose answers split the design space into testable
> behaviors. Each is paired with the cross-cutting concern it surfaces and
> the options it discriminates between, so `/feature-spec` can score
> scenarios against the option matrix above.*

1. **Q1 — Per-stage envelope coverage**: must the operator see one wire
   envelope per `stage-complete` (high fidelity), or is "at least one
   `build-progress` envelope per N seconds" sufficient (low fidelity)?
   Discriminator for **B/F (terminal-only) vs A/C/D/E (per-stage)**.
   BDD scenarios should specify the canonical per-build wire sequence; if
   per-stage is required, B and F are out.

2. **Q2 — Restart-window per-stage tolerance**: when forge daemon restarts
   mid-build for 30 seconds, what's the operator-visible expectation?
   - (a) gap-then-resume — operator sees pre-restart envelopes, no
     in-window envelopes, post-restart envelopes;
   - (b) replay — operator sees in-window envelopes after restart;
   - (c) terminal-only matters — gap is acceptable as long as terminal
     fires.
   Discriminator for **C (replay via Last-Event-ID) vs A/D/E (gap) vs
   E/F (terminal-only acceptable)**.

3. **Q3 — Deferred-ack contract**: is the inbound `pipeline.build-queued.*`
   acked
   - (a) when `dispatch_build` returns successfully (today's broken shape
     — produces redelivery storm);
   - (b) when the bridge observes terminal (proposed F010M shape);
   - (c) hybrid — sync ack on validation success, terminal-state recorded
     on the wire instead of via ack?
   This is independent of bridge option choice but is **load-bearing for
   the wave-plan structure** (consumer contract refactor sub-task).

4. **Q4 — Pause/resume site canonicalisation (folds FW10-010)**: under any
   F010M option that publishes `build-paused`/`build-resumed`, both the
   bridge AND the existing FW10-010 design's `approval_subscriber.py`
   resume site can publish `build-resumed`. Which is canonical?
   - (a) bridge owns both; `approval_subscriber.py` is amended to skip
     the emit when a bridge is wired;
   - (b) `approval_subscriber.py` owns resume; bridge skips resume;
   - (c) both fire, dedup at the JetStream layer (problematic — JetStream
     does not natively dedup on payload).
   Discriminator for **how FW10-010 folds into F010M's wave-plan**.

5. **Q5 — Correlation_id enforcement across the process boundary**: under
   D/E, the in-sidecar adapter must thread `correlation_id` onto every
   emit, and F010C's AST lint guards do not extend cross-process. What's
   the new enforcement mechanism?
   - (a) a server-side validator on forge's in-receive endpoint that
     rejects emits missing correlation_id (logs at ERROR, drops the emit);
   - (b) a JSON schema on the in-receive endpoint enforced at parse time;
   - (c) the in-sidecar adapter is treated as trusted (no enforcement)
     because it's local-only.
   Discriminator only relevant if D/E are picked.

6. **Q6 — `forge status --in-flight` scope**: the in-flight build registry
   that #1 (recovery) requires is the same artefact `forge status` would
   surface. Is the `forge status --in-flight` command in scope for F010M's
   wave-plan, or a separable follow-up?
   Recommend **in scope** — the current redelivery-storm symptom (Addendum
   5) would have been visible-as-pending if the registry existed.

7. **Q7 — Cancel-emit ownership**: when the operator cancels an in-flight
   build, who emits `pipeline.build-cancelled.*`?
   - (a) forge's cancel handler synthesises it directly after calling
     `runs.cancel`;
   - (b) the bridge observes the run reaching terminal=interrupted and
     synthesises it via the option's general termination path;
   - (c) both (with dedup);
   - (d) the in-sidecar `LifecycleEmitterAdapter` (Option D/E only).
   Discriminator for the cancel sequence's wire shape.

8. **Q8 — Sidecar-aware integration test scope**: FW10-011 is
   `design_approved` but mocks `AutobuildDispatcher.dispatch` and so does
   not exercise the production sidecar boundary. F010M's wave-plan needs
   to either:
   - (a) keep FW10-011 as the in-process composition lock and add a
     **separate** sidecar-aware E2E test (new test file);
   - (b) amend FW10-011's design to optionally exercise a real sidecar
     spin-up (parametrised fixture).
   Discriminator for the wave-plan's test deliverables.

---

## Recommended option

> *Implementer's recommended pick. To be ratified or revised during
> `/feature-spec`; this section is the working hypothesis, not a binding
> decision.*

### Pick: **Option C — Streaming subscription** (`runs.join_stream` SSE)

### Highest-weight reason

**C is the only option that captures every transition (per-stage AND
terminal) with a structurally clean crash-recovery story.** Per the
cross-cutting summary table:

- C is the only "best" entry on concern #1 (recovery via `Last-Event-ID`);
- C scores "trivial" on concern #4 (correlation_id threading) because the
  bridge runs in forge with access to the in-forge `BuildContext`, so
  F010C's AST lint guard model extends unchanged;
- C provides per-stage coverage natively (eliminating B/F as standalone
  options);
- C keeps the sidecar's surface area unchanged — no in-sidecar code
  change, no new sidecar-side dep (D's biggest cost);
- C is fully implementable with `langgraph-sdk==0.3.13`'s shipped surface
  — `client.runs.join_stream(...)` with `last_event_id=` is in
  `langgraph_sdk/_async/runs.py:1090-1147`, no version bumps needed.

The combination "per-stage coverage + clean recovery + correlation_id
trivial" is unique to C in the matrix.

### Rationale (per concern)

| Concern | C's answer |
|---|---|
| #1 Recovery | SSE `Last-Event-ID` replays missed events from langgraph-api's server-side buffer. Forge persists the last event id per run; on restart, resumes from there. |
| #2 Ack | Stream observer invokes the inbound message's ack callback on terminal SSE event. The consumer-contract refactor (handing the ack callback off to the bridge) is C-independent and would be needed for any option. |
| #3 FW10-010 | Reshape: bridge observes `awaiting_approval` SSE event → emits `build-paused` from forge; bridge observes `running_wave-after-awaiting_approval` → emits `build-resumed`. The `approval_subscriber.py` resume emit is **dropped** (Q4 sub-option (a) — bridge owns both). FW10-010 folds into F010M's wave-plan. |
| #4 Correlation_id | Trivial — bridge runs in forge with access to the in-forge `BuildContext.correlation_id` loaded from SQLite. Existing AST lint guards extend to the new bridge call site. |
| #5 forge status | Free — the SSE bridge's per-build connection registry IS the in-flight registry. Surface as `forge status --in-flight`. |
| #6 Transient retry | Bridge implements explicit reconnect-with-backoff using `Last-Event-ID`. SDK's `request_reconnect` shape is the building block. |
| #7 Cancel | Forge calls `runs.cancel(thread_id, run_id, action="interrupt")`; bridge observes the run reaching `terminal=interrupted` via SSE; emits `pipeline.build-cancelled`. Q7 sub-option (b) — single emit site. |

### Dominant risk if C turns out wrong

**Risk**: the `StreamPart` event shape may not carry enough information to
synthesise typed `pipeline.*` payloads (e.g. `BuildStartedPayload`,
`StageCompletePayload`) cleanly. The translation layer might require
inspecting the full `AutobuildState` channel-state snapshots carried in
`stream_mode="values"` and reconstructing typed payloads from raw channel
mutations. If the translation logic turns out to be brittle (e.g. silent
schema drift across langgraph-api minor versions), the bridge could
silently emit malformed payloads or miss transitions.

**Probability**: medium. The `StreamPart` shape is documented but the
mapping from "raw graph-state mutation" to "typed pipeline event" is a
new piece of code with no precedent in the forge codebase. The autobuild
runner's `LifecycleEmitterAdapter` already does an analogous mapping
(lifecycle string → emit method) in-process — replicating that out-of-process
on raw channel mutations is achievable but non-trivial.

**Impact**: high if it manifests — silent malformed envelopes would defeat
the chat REPL's terminal-card rendering and could trigger F010F-shape
operator confusion all over again. Mitigated by:

- Locking `langgraph-sdk` and `langgraph-api` versions in `pyproject.toml`
  with explicit upper bounds; treating any bump as a test surface.
- Asserting the translation layer with a contract test that round-trips
  a known sequence of `AutobuildState` mutations through the SSE stream
  and validates the emitted `pipeline.*` envelopes against the
  `nats_core.events` schema.
- Including the FW10-011 sidecar-aware E2E test (Q8 sub-option (a) —
  separate test file) so a translation regression is caught in CI.

### Fallback if C is rejected during `/feature-spec`

**Fallback: Option E — Hybrid** (per-stage in-sidecar D-NATS + terminal via
`runs.join` F-shape).

E's appeal: preserves FW10-010's `LifecycleEmitterAdapter` design
unchanged; D-NATS gets clean recovery via JetStream durability;
`runs.join` for terminal is the cleanest possible terminal observer.

E's costs that argue against it as the primary recommendation: doubles
the maintenance surface; introduces the cross-process correlation_id
enforcement burden (Q5); requires the sidecar to gain a NATS dep; and
the pause/resume double-publish risk (Q4) is concrete.

If `/feature-spec` finds C's translation layer untenable, E is the
defensible fallback. **Do not fall back to A** — A's per-stage diffing
fragility is structurally worse than C's translation-layer risk.

### What `/feature-spec` should ratify or revise

1. The pick (C). If revised to E, the wave-plan reshapes around D-NATS +
   F's terminal observer.
2. The Q4 canonicalisation (bridge owns pause+resume; `approval_subscriber.py`
   amended).
3. The Q8 test scope (separate sidecar-aware E2E rather than FW10-011
   amendment).
4. The acceptable per-stage gap window during forge restart (Q2 sub-option
   (b) — replay via `Last-Event-ID` with bounded buffer retention).

---

## References

- **RESULTS Addendum 5** —
  `/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  (the empirical trigger for this scoping work; correlation_id
  `e9433033-ea80-449f-885d-b2d1bdfb839e`).
- **F010F task file** —
  `tasks/completed/TASK-FORGE-FRR-F010F/...` (the sync-raise safety-net; the
  contract F010M's bridge extends).
- **F010J task file** —
  `tasks/completed/TASK-FORGE-FRR-F010J/...` (the sidecar URL threading; the
  prerequisite that produced the live HTTP 200 dispatch path).
- **FW10-009 task file** —
  `tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md`.
- **FW10-010 task file** —
  `tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md`.
- **FW10-011 task file** —
  `tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md`.
- **forge-orchestrator-wiring-gap.md** — the precedent scoping doc that
  anchored FEAT-FORGE-010; this document follows the same shape at smaller
  scope.
- **forge-orchestrator-wiring-feature-context.md** — the `--context` evaluation
  that fed `/feature-spec` for FEAT-FORGE-010; precedent for what F010M's
  Phase 2 `--context` payload should look like.
- **langgraph-runner / langgraph-cli / langgraph-sdk** reference docs —
  consulted during §Existing wiring audit > "langgraph-runner SDK surface".
- **API contract** —
  `docs/design/contracts/API-nats-pipeline-events.md` (the eight
  `pipeline.{event}.{feature_id}` subjects the bridge publishes onto).
- **DDR-030** — between-prompt notification contract (the user-visible
  requirement F010M satisfies; cite the exact DDR file path once located).
