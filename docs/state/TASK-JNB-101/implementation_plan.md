# TASK-JNB-101 Implementation Plan — ApprovalSubscriber production wiring

**Status**: v2 — post-architectural-review (v1 scored 64/100, Changes Required;
all four CRITICAL findings folded in below)
**Date**: 2026-07-05
**Author**: Fable forge-JNB session (interactive /task-work)

## Ground truth discovered before planning (verified, 5-reader sweep)

1. **Nothing in production pauses a build today.** The autobuild runner graph
   (langgraph sidecar process) has five nodes — starting, planning_waves,
   running_wave, completed, failed — and never enters `awaiting_approval`
   (`src/forge/subagents/autobuild_runner.py:1584-1604`).
2. `gate_check` / `GateCheckDeps` (`src/forge/gating/wrappers.py:366,408`) have
   **zero production construction/call sites** — the whole CGCP gating stack is
   built and integration-tested but unwired ("highest-uncertainty task" per the
   task file itself).
3. `ApprovalPublisher`, `ApprovalSubscriber`, `LifecycleEmitterAdapter` (and its
   `mark_resume_pending`, `autobuild_runner.py:594`) are never constructed in
   src/; the launch-payload synthesiser strips `lifecycle_emitter` before
   sidecar serialisation (`_serve_async_task_starter.py:131-134`).
4. **No production SQLite implementations exist** for the `GateRepository` and
   `StateMachine` protocols — only in-memory fakes in
   `tests/integration/conftest.py` (:226, :299).
5. Composition-root pattern: `LifecycleBridgeWireupParts`
   (`_serve_production.py:146`) — SQLite-bound parts at bind time, NATS-bound
   finalisation inside the `_compose(client)` closure (`serve.py:307-357`).
6. Config: `ApprovalConfig` (`src/forge/config/models.py:162`, extra="forbid")
   loads from forge.yaml via `load_config`; serve loads it at
   `serve.py:753-784` → `bind_production_serve` (:808).
7. Canonical test harness: `tests/integration/conftest.py` — `InMemoryNats`
   (:110), `build_gate_check_deps` (:407, wires REAL
   publisher/subscriber/injector over the fake transport;
   `publish_refresh=None` today), `_drive_response` wait-for-subscriber
   pattern; pytest-asyncio strict mode.

## Architectural review outcomes folded in (v1 → v2)

- **C1 (CONFIRMED BUG in the AC-3-named mechanism)**:
  `LifecycleEmitterAdapter.on_transition` fires `emit_resumed` only when
  `lifecycle=="running_wave" AND _last_lifecycle=="awaiting_approval" AND
  _resume_pending` (`autobuild_runner.py:559-566`). `mark_resume_pending()`
  sets only `_resume_pending` — on a fresh adapter (the daemon-restart case it
  exists for) the emit silently never fires. → **The adapter path is dropped
  from this design entirely.**
- **C2**: synthesized `AutobuildState` cannot be honestly populated
  (fabricated `task_id`, hardcoded `gate_mode`, lost `coach_score`) and the
  fire-and-forget `_schedule` gives no ordering guarantee. → Same conclusion:
  do not route daemon emits through the adapter.
- **C3**: `LifecycleBridgeWireupParts` exposes no registry. → **Add a public
  `registry: BridgeRegistry` field** to the Parts container
  (`_serve_production.py` added to the Files list) and thread it through
  `_compose`.
- **C4**: pause-side emit ordering race. → **Dissolved**: no AC asks for a
  daemon-side `build-paused` emit and v2 adds none. The only pause-side
  publish is the pre-existing, untouched `ApprovalRequestPayload` publish in
  `_atomic_pause_and_publish`. Zero new ordering hazards.
- **R1/R2/R3/R4/R5** all adopted (reset-for-tests twin; frozen Parts
  dataclass; explicit-kwarg guard test; changelog/runbook callout for the
  permissive→enforcing default flip; REAL `PipelineLifecycleEmitter` over
  `InMemoryNats` in resume-path tests).

## Additional defect found while folding (resume-emit trigger set)

The subscriber's FW10-010 emit step (`approval_subscriber.py:719-770`) emits
`build-resumed` for **any** first-arrival valid response — including
`reject` and `defer`. The phone contract (FEAT-BF39 / JNB-102) is: reject →
CANCELLED → `build-cancelled` only; a `build-resumed` on reject would render
resumed-then-cancelled on the operator's phone. AC-3's own wording scopes the
resume emit to approve/override. The emit step is FW10-010 *emit wiring* (not
the four-step validation chain, not the wait-loop internals), and this task
IS the resume-emit wiring task → **v2 adds a decision gate to that emit step:
only `approve` and `override` publish `build-resumed`.** Existing tests that
pin the unconditional emit (if any) are updated with this justification.

## The v2 design

**The resume emit uses the subscriber's own designed seam (FW10-010), fully
threaded for the first time, instead of any state-machine decorator or the
dead adapter:**

`_BoundContextSubscriber` (new, in `_serve_deps_gating.py`) satisfies
`ApprovalSubscriberProto` and binds three existing, documented kwargs into
every `await_response` call:
- `lifecycle_emitter` = the daemon's real `PipelineLifecycleEmitter`,
- `build_context` = the build's pipeline `BuildContext`,
- `expected_correlation_id` = `ctx.correlation_id`.

What this activates (all pre-built, tested, dormant-until-now):
1. **Resume emit with full fidelity**: `emit_resumed(ctx, stage_label,
   decision=payload.decision, responder=payload.decided_by)` — awaited
   *before* `queue.put`, i.e. on the wire before the wait loop returns and
   before `transition_to_running` (FW10-010's documented ordering contract).
   No fabricated fields, no fire-and-forget.
2. **The correlation-id validation step (2b)**: without
   `expected_correlation_id` threaded, AC-2's "four-step chain" is actually a
   three-step chain — step 2b only runs when the resume-publish context is
   registered. v2 makes the full four-step chain live for the first time.
3. **Bridge canonicalisation as designed**: `bridge_registry_lookup` wired to
   the real `BridgeRegistry.get` (True iff entry non-None) — the subscriber
   skips its emit when the LifecycleBridge owns the build's resume envelope.

Trigger-set correctness comes from the decision gate (defect fix above):
approve/override emit; reject/defer do not. Reject's terminal phone signal is
TASK-JNB-102's `build-cancelled` (untouched here).

**AC-3 deviation (recorded, deliberate)**: AC-3 names
`autobuild_runner.mark_resume_pending` as the mechanism. The arch review
proved that mechanism broken for its own cited scenario (C1) on a dead adapter
path (ground truth #3). v2 satisfies AC-3's intent — `build-resumed` emitted
on approve/override decision dispatch, exactly once, with real
decision/responder values — through the seam that was actually designed for
production (FW10-010 + PEB-006). `mark_resume_pending` remains uncalled; the
C1 guard bug is documented as a follow-up for whichever task activates the
runner-side (sidecar) pause path.

## Files

### 1. `src/forge/config/models.py` (edit)
`ApprovalConfig.expected_approver: str | None = Field(default="rich", ...)`
— description documents the APPROVER_IDENTITY contract: verbatim string
equality with jarvis `JARVIS_SLACK_DECIDED_BY` (pinned `rich`,
operator-chosen 2026-07-04); `None` = permissive dev mode; mismatch silently
refuses every phone approval.

### 2. `src/forge/adapters/nats/approval_subscriber.py` (edit, small)
Decision gate on the FW10-010 emit step: only `payload.decision in
("approve", "override")` publishes `build-resumed`. Validation chain
(steps 1/2/2b/3), dedup, queue semantics, await_response internals: untouched.

### 3. `src/forge/cli/_serve_deps_gating.py` (NEW)
- `ApprovalGateParts` (`@dataclass(frozen=True, slots=True)`): `publisher`,
  `subscriber` (the raw `ApprovalSubscriber`), `injector`,
  `approval_config`, `expected_approver`, `emitter`,
  `bridge_registry_lookup`.
- `build_approval_gate_parts(client, forge_config, *, emitter=None,
  bridge_registry=None, repository=None, project=None)`:
  - `ApprovalPublisher(client, project=project)`
  - `publish_refresh`: only when `repository` provided — closure that looks
    up the paused snapshot (`list_paused_builds`), derives the refreshed
    `request_id` (`derive_request_id`), records the refreshed row
    (`record_paused_build` — so boot recovery re-emits the *current* id),
    and publishes via the canonical envelope builder (reuse
    `wrappers._build_request_envelope` via import — single source of truth,
    no duplication). `repository=None` → `publish_refresh=None` (single-shot
    waits, matching today's conftest behaviour).
  - `ApprovalSubscriberDeps(nats_client=client, config=forge_config.approval,
    expected_approver=forge_config.approval.expected_approver  # ALWAYS
    explicit — R3 guard test, publish_refresh=..., project=project,
    bridge_registry_lookup=<BridgeRegistry.get wrapper> if bridge_registry)`
  - `SyntheticResponseInjector(nats_client=client)`
- `_BoundContextSubscriber(inner, emitter, ctx, expected_correlation_id)` —
  the per-build proto adapter described above.
- `make_gate_check_deps(parts, *, ctx, priors_reader, adjustments_reader,
  rules_reader, repository, state_machine, reasoning_model_call, clock=None,
  per_attempt_wait_seconds=None) -> GateCheckDeps` — the AC-1 typed seam:
  `subscriber=_BoundContextSubscriber(parts.subscriber, parts.emitter, ctx,
  ctx.correlation_id)` when the emitter is present, else the raw subscriber.
  Collaborators without production adapters are typed parameters (documented
  follow-up), never silently faked.
- Module-level `_bound_gate_parts` + `bind_gate_parts()` / `bound_gate_parts()`
  accessors + `_reset_for_tests()` (R1), mirroring `_serve_production`.

### 4. `src/forge/cli/_serve_production.py` (edit, small — C3)
Add public `registry: BridgeRegistry` field to `LifecycleBridgeWireupParts`;
populate it in `_build_lifecycle_bridge_wireup_parts` (the instance already
exists locally at :312).

### 5. `src/forge/cli/serve.py` (edit, ~8 lines in `_compose`)
Construct `build_approval_gate_parts(client, forge_config,
emitter=<compose's emitter>, bridge_registry=<wireup parts registry>)` and
anchor via `_serve_deps_gating.bind_gate_parts(...)`.

### 6. Tests
- `tests/test_approval_config.py` (edit): round-trip dicts gain the key;
  new `TestExpectedApprover` — default pinned `"rich"` (config-alignment AC,
  comment names the jarvis contract), yaml override, explicit None.
- `tests/cli/test_serve_deps_gating.py` (NEW, unit tier): factory always
  passes `expected_approver` explicitly (R3); parts frozen; reset-for-tests
  isolation; refresh closure records-then-publishes with refreshed id;
  no-repository → refresh disabled; bridge lookup wrapper truthiness.
- `tests/integration/test_jnb101_production_wiring.py` (NEW): all scenarios
  drive `gate_check` through `make_gate_check_deps(build_approval_gate_parts(
  InMemoryNats, forge_config...))` with a REAL `PipelineLifecycleEmitter`
  over the same `InMemoryNats` (R5) + in-memory repo/state-machine + fake
  clocks. Scenario classes (Test Requirements names):
  - `TestApproveResumesOnce` — approve → RESUMED; exactly one
    `BuildResumedPayload` ON THE WIRE (`pipeline.build-resumed.{feature_id}`)
    with `decision="approve"`, `responder="rich"`; wire envelope precedes the
    RUNNING transition (order log); duplicate same-request_id reply inside
    300s deduped — no second resume, no second envelope.
  - `TestRejectCancels` — reject → CANCELLED transition + mark_cancelled;
    ZERO `build-resumed` envelopes (the decision-gate defect fix, pinned).
  - `TestDeferRepublishWithRefreshedRequestId` — defer → republish with
    attempt_count+1 and refreshed `derive_request_id`; no resume emit on the
    defer itself.
  - `TestWindowExpiryCancels` — no-refresh config: 300s window expiry →
    `transition_to_cancelled(REASON_MAX_WAIT)`.
  - `TestCeilingBreachCancels` — refresh wired: refreshes until the 3600s
    ceiling → cancelled.
  - `TestSpoofedReplyRefused` — wrong `decided_by` / mismatched
    `correlation_id` (now live via the bound context — four-step chain
    proof) / stale `request_id` → refused, zero transitions, zero emits.
  - `TestConfigAlignment` — factory threads config default `"rich"` into the
    wired deps; `decided_by="rich"` accepted, others refused.
- `tests/forge/adapters/test_approval_subscriber.py` (edit if needed): any
  test pinning the unconditional FW10-010 emit updated to the decision-gated
  contract, with the phone-loop justification in the docstring.

### 7. Docs
- `docs/design/contracts/API-nats-approval-protocol.md` — add
  `approval.expected_approver` + alignment contract + the decision-gated
  resume-emit note.
- `docs/runbooks/RUNBOOK-FEAT-FORGE-008-validation.md` — example approval
  block gains the key; explicit callout (R4): the `"rich"` default flips
  permissive→enforcing for deployments omitting the `approval:` block, and
  deploying yaml-with-key before image-with-field fails boot loudly
  (extra="forbid").

## Risks / mitigations
- R-emit-contract: decision gate changes a (test-only-observed) FW10-010
  behaviour — justified above; updated tests pin the new contract.
- R-reconnect: subscriber subscribes per-await_response call, so a daemon
  reconnect affects only in-flight waits — documented on the parts docstring.
- R-config-forbid ordering + default flip: R4 callouts.
- R-stale line numbers: uncommitted operator revert in autobuild_runner.py
  working tree (unrelated; left unstaged; -7 line drift).

## Estimates
LOC: ~170 new module + ~15 config + ~4 subscriber + ~6 parts field + ~8
serve.py + ~550 tests. Files: 5 src + 4 test + 2 docs = 11. One session
segment.

## Follow-ups (documented, out of scope)
- Production SQLite adapters for GateRepository/StateMachine + the gate_check
  activation point in the dispatch flow (needed before a real build pauses;
  prerequisite for JNB-107's live gated toy build).
- `serve.py:148` recovery seam → `reconcile_on_boot` binding (boot re-emit of
  approval requests for PAUSED builds; the handoff §6 assumes it).
- C1 guard bug in `LifecycleEmitterAdapter.mark_resume_pending`
  (`autobuild_runner.py:559-605`) — fix belongs to the runner-side (sidecar)
  pause activation task.
