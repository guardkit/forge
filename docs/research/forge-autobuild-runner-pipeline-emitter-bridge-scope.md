# Scoping: forge `autobuild_runner` ↔ pipeline-lifecycle-emitter bridge

## Status

DRAFT — produced by TASK-FORGE-FRR-F010M Phase 1. Will be passed to
`/feature-spec` and `/feature-plan` as `--context` once the implementer
completes the design-space analysis below. **The implementer fills in each
section; this skeleton intentionally pre-resolves nothing.**

---

## Problem

(Verbatim from F010M task body's §Symptom + §Why sections — the wire-side and
chat-side observations from RESULTS Addendum 5, plus the DDR-030 contract gap.
Restate concisely here so the scoping doc is self-contained when passed as
`--context`.)

> *Implementer: paste the F010M Symptom + Why sections here, with the
> correlation_id `e9433033-…` and the literal chat-supervisor honest-answer
> quote. Goal: a `/feature-spec` reader can understand the gap without
> chasing back to the RESULTS file.*

---

## Existing wiring audit

> *Implementer: each subsection is a question to answer, not a claim. Read the
> referenced files; record findings inline with file:line refs.*

### FW10-009 (validation surface and build-failed paths)

What does it cover? Does it touch the async path?

- [ ] Read `tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md`
  end-to-end.
- [ ] Read `src/forge/adapters/nats/pipeline_consumer.py` for the validation
  surface FW10-009 wired.
- [ ] State explicitly: does FW10-009's contract include the async-completion
  path, or is it sync-only (like F010F)?

### FW10-010 (pause-resume publish round-trip)

What does it cover? Does it commit to a polling/streaming/webhook shape?

- [ ] Read `tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md`.
- [ ] Note that FW10-010's status is `design_approved` (not implemented as of
  2026-05-06).
- [ ] State: does FW10-010 assume `_update_state` runs in-process to forge?
  If yes, that assumption is broken under the F010J sidecar deployment shape
  — call this out.

### FW10-011 (capstone integration test)

What was its `design_approved` spec? Does it commit to a bridge shape?

- [ ] Read `tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md`
  in full, including §Implementation notes.
- [ ] Note: FW10-011 mocks `AutobuildDispatcher.dispatch` at the boundary so
  the autobuild "runs" by emitting a scripted sequence through the real
  emitter. Does this commit to Option D or is it agnostic on the bridge
  question?
- [ ] If FW10-011's design already constrains F010M, fold (don't duplicate)
  its commitments into F010M's wave-plan.

### F010F safety-net publish

Where does it sit in the dispatcher contract? Does it constrain the async-bridge shape?

- [ ] Re-read `tasks/completed/TASK-FORGE-FRR-F010F/...` for the exact
  contract: F010F publishes when `dispatch_build` raises **synchronously**
  inside `pipeline_consumer.handle_message`. The async path is out of scope.
- [ ] State the boundary: F010M's bridge must publish in cases F010F does not
  cover, **without** double-publishing in cases F010F does cover.

### Existing `PipelineLifecycleEmitter`

Where does it live? What's its API? Where is it invoked today?

- [ ] Locate `PipelineLifecycleEmitter` (per `forge-orchestrator-wiring-gap.md`
  it's exported from `src/forge/pipeline/__init__.py`).
- [ ] Catalog every existing call site (post-FEAT-FORGE-010 wiring): which
  `emit_*` methods are called, from where, in what process (forge daemon vs
  sidecar)?
- [ ] State the bridge's relationship to the emitter: does it call the emitter
  directly, or post the equivalent shape to a NATS topic the emitter
  subscribes to?

### langgraph-runner SDK surface

What endpoints does it expose for run-status, run-streaming, run-completion-callback?

- [ ] `GET /threads/<thread_id>/runs/<run_id>` — synchronous status (Option A
  reads this).
- [ ] `GET /threads/<thread_id>/runs/<run_id>/stream` (or `/events`) — SSE /
  websocket stream (Option C reads this).
- [ ] Webhook configuration on run-completion — does `langgraph-cli` /
  `langgraph-api` support outbound webhooks (Option B)? Verify against
  langgraph-cli 0.4.24 / langgraph-api 0.8.5.
- [ ] `DELETE /threads/<thread_id>/runs/<run_id>` — cancellation surface
  (Cross-cutting concern #7).
- [ ] Document each finding with the SDK file:line ref.

---

## Design space

> *For each option, sketch (a) what it does, (b) pros, (c) cons, (d) open
> questions specific to this option. Don't pre-rank; the §Recommended option
> section comes last.*

### Option A — Polling

(Sketch. Pros / cons. Open questions.)

### Option B — Webhooks

(Sketch. Pros / cons. Open questions.)

### Option C — Streaming subscription

(Sketch. Pros / cons. Open questions.)

### Option D — In-process emit from inside the subagent

(Sketch. Pros / cons. Open questions.)

### Option E — Hybrid

(Sketch. Pros / cons. Open questions.)

### Option F+ — (any additional options the implementer finds)

(Sketch. Pros / cons. Open questions.)

---

## Cross-cutting concerns

> *For every option above, the implementer answers each numbered concern.
> Concerns shared across multiple options can be analysed once with
> per-option deltas called out.*

1. **Daemon-restart recovery** — the bridge state must survive `forge serve`
   restart mid-build.
2. **Deferred-ack contract** — when does the inbound `pipeline.build-queued.*`
   get acked? F010F currently acks on sync-raise; the async-completion path
   needs a parallel rule.
3. **FW10-010 pause-resume interaction** — FW10-010 (`design_approved`)
   commits to in-process `emit_build_paused` / `emit_build_resumed` calls; how
   does that compose with the chosen bridge shape?
4. **Correlation_id threading on every emit** — re-validates F010C's contract
   for every new publish site.
5. **Observability of in-flight builds** — is there a `forge status` command
   that lists in-flight builds? Should it grow?
6. **Retry semantics on transient sidecar failures** — 5xx from
   langgraph-runner / timeout / connection drop.
7. **Cancellation paths** — operator cancels a build mid-flight; how does the
   bridge propagate that to the sidecar?

---

## Open questions for `/feature-spec`

> *Surface 5-10 specific questions `/feature-spec` should answer when
> generating BDD scenarios. These are the questions whose answers split the
> design space into testable behaviors.*

- (Question 1)
- (Question 2)
- (Question 3)
- (Question 4)
- (Question 5)
- ...

---

## Recommended option

> *Filled in last. The implementer's recommended pick + rationale, to be
> ratified or revised during `/feature-spec`. Include the highest-weight
> reason and the dominant risk if the pick turns out wrong. F010I's review
> report is a precedent for this section's shape.*

(Recommendation. Rationale. Risks.)

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
