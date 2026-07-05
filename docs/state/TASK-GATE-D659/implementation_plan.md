# TASK-GATE-D659 Implementation Plan — Gate activation (daemon-side pre-dispatch, repaired + grafted)

**Status**: v2 — post-architectural-review (Phase 2.5B scored v1 at 66/100,
approve-with-recommendations; both CRITICAL findings, both MAJORs, and all
minors folded in below). Pending Phase 2.8 operator checkpoint.
**Date**: 2026-07-05
**Author**: Fable gate-activation session (interactive /task-work --design-only)
**Panel artifacts**: designs + verdicts in the session scratchpad (`scratchpad/panel/`);
judge scores: predispatch 58/86/86, bridge 85/64/50, runner-interrupt 74/52/58
(ground-truth / operational-risk / delivery lenses).

## Decision (D1): daemon-side PRE-DISPATCH gate, with the observer-lifecycle repair

`gate_check` runs inside the daemon's dispatch flow — after `record_pending_build`
mints `build_id`, **before any bridge observer exists and before the sidecar run
is launched**. Approve → register observer + launch; reject/expiry → terminal
before any runner exists.

**Why predispatch won**: zero fatals under the operational-risk and delivery
lenses (86/86 — best restart safety by construction: a sidecar restart mid-pause
is a non-event; shortest path to JNB-107; touches neither wireup.py nor
translation.py nor the runner graph). Its three ground-truth fatals were all one
defect — the pre-registered bridge observer left live during the pause — with a
judge-prescribed repair adopted below. **Why not bridge** (ground-truth winner,
85): single-fault pause-activation dependency on bridge identity resolution,
which recorded GB10 state (stale async_tasks rows) already breaks — the pause
itself can silently fail; plus a probe-gated critical path and the largest
regression surface (shared replay heuristics exercised ~120×/hour during any
pause). **Why not runner-interrupt**: built its broker model on
`build_consumer_config` (zero production callers); at the real ~30s redelivery
cadence its liveness-aware resume-emit suppression intermittently swallows the
only build-resumed emitter.

**The honest limitation, disclosed**: a pre-dispatch gate can only gate
permission-to-start; coach_score is structurally unavailable at dispatch forever.
When evidence-based gating arrives (post-UBS-002), the activation point moves to
a runner-side/outcome boundary — the panel's banked mechanisms for that move are
recorded in §Future. ~60-70% of this work (adapters, parse_request_id,
correlation fix, recovery binding, rearm) is activation-point-agnostic and
carries over.

## R1 — THE REPAIR (dissolves all three ground-truth fatals)

As written by its designer, predispatch gated *after*
`pipeline_consumer` registers the ack handle (`pipeline_consumer.py:519-525`) and
the wireup spawns the per-build observer (`wireup.py:574-577`). During a pause
that observer (a) resolves stale async_tasks rows and fetch-replays a dead run's
terminal — false BuildStarted/BuildComplete + early ack + detach mid-pause; or
(b) identity-times-out in ~3-5s and pops the ack handle, so no observer exists
post-approve.

**Repair — observer registration is deferred until after gate approval:**

- The consumer's flow becomes: validate → `dispatch_build` begins →
  `record_pending_build` (build_id minted) → **`maybe_gate_build`** →
  on approve: **`register_ack_handle` → `dispatch_autobuild_async`** (launch);
  on gate-terminal: ack the slot, never register.
  Mechanically: relocate the `register_ack_handle` call (currently ahead of
  `dispatch_build`) to a callback passed into `dispatch_build`, invoked between
  gate approval and launch. Exact seam (consumer edit vs dispatch_build
  parameter) confirmed at implementation; both sites verified reachable.
- Consequences, all verified against the judges' code checks:
  - No observer exists during the pause → no stale-row replay, no false
    envelopes, no early ack, nothing to detach on gate-terminal outcomes.
  - Post-approve the observer spawns adjacent to the launch → identity resolves
    the fresh run; terminal ack rides the fresh handle (fatal #2 gone).
  - The bridge registry row is never created for a paused build →
    resume-emit suppression cannot occur; we still pass **`bridge_registry=None`**
    into `build_approval_gate_parts` (static exactly-one-resume-emit-owner —
    subscriber owns it; judges' cross-cutting requirement) with a rationale
    comment naming the reintroduction condition.

## R2 — Duplicate-delivery ack posture (state-conditional, replaces the designer's unconditional ack)

`DuplicateBuildError` branch (`_serve_deps.py:276-290`): ack **only when the
builds row is terminal**; while PAUSED/in-flight → skip WITHOUT ack (the
FEAT-FORGE-010 held-slot invariant survives restarts and >ack_wait pauses;
self-heals via the duplicate-terminal ack — the wedge the original ack "fixed"
was overstated per the ground-truth check). Belt: `maybe_gate_build` starts with
an idempotency pre-read (builds row already PAUSED with
`pending_approval_request_id` → return without starting a second gate; the
rearm path owns it).

## Gate mechanics (unchanged from the winning design; all anchors judge-verified)

- **Call**: `gate_check(deps, build_id, feature_id, stage_label="autobuild",
  target_kind="subagent", target_identifier="autobuild_runner",
  coach_score=None, criterion_breakdown={}, detection_findings=[], attempt_count=0)`.
- **Honesty posture (ADR-ARCH-019/026)**: three readers return `[]`; reasoning
  callable is `degraded_dispatch_gate_model` returning static-JSON
  MANDATORY_HUMAN_APPROVAL (degraded/training mode; threshold null). NOT the
  constitutional `review_pr` shortcut — persisted GateDecisions stay truthful;
  the callable is the single seam a real reasoning adapter later replaces.
  **Consequence: every dispatched build pauses for phone approval until then**
  (DF-009 "v1 never auto-approves" ratchet; exactly what JNB-107 needs).
- **Builds-row path**: QUEUED→PREPARING→RUNNING→PAUSED via `transition_chain`
  with `pending_approval_request_id` on the final hop (synthetic-hop precedent;
  PAUSED only legal from RUNNING). Approve: PAUSED→RUNNING (auto-clears
  request id); the bridge's later BuildStarted write-back composes to a no-op.
- **Event loop**: the multi-minute await blocks only the JetStream fetch task —
  NATS reader, healthz, subscriber callbacks stay live; sequential-build
  semantics per ADR-ARCH-014/FEAT-FORGE-010.

## D2 — Envelope contract

All envelopes publish from the daemon (sidecar publishes nothing; FMDR honoured
trivially — no runner exists while paused).

- **Pause**: `_MirroredApprovalPublisher` wraps only the publish step of
  `_atomic_pause_and_publish`: AGENTS `agents.approval.forge.{build_id}` request
  FIRST (jarvis must capture request_id before the Slack post renders), then
  `emitter.emit_paused(...)` → `pipeline.build-paused.{feature_id}`.
  SQLite-before-wire preserved untouched. Defer republishes flow through the
  same wrapper → fresh build-paused per attempt → jarvis chat.update supersede
  refreshes buttons (incidentally fixing the recorded stale-button gotcha).
- **Resume**: exactly one `build-resumed` (real decision/responder) from the
  subscriber's FW10-010 decision-gated seam; static ownership via
  `bridge_registry=None`.
- **Reject/expiry**: CANCELLED in SQLite first, then exactly one
  `build-cancelled` via the JNB-102 seam. Zero build-resumed.
- **Wrinkle (disclosed)**: on approve the wire shows paused → resumed → started.
  No known jarvis invariant breaks (per-event stateless notifications); on the
  JNB-107 live-run checklist (§Checklist), not silently assumed.

## D3 — SQLite adapters (`src/forge/gating/sqlite_adapters.py`)

`build_sqlite_gate_adapters(sqlite_pool, *, clock)` → repository + state-machine
pair sharing a `_PauseHandoff` (bridges `record_paused_build`-carries-request_id
vs `transition_to_paused`-doesn't). Async shims over the sync facades.
**Single-transition-owner**: the SM owns every `builds.status` write; the
repository owns stage_log only.

- `record_decision` → GATED stage_log row, first-ever writer of
  `details_json["gate"]` (GateDecision.model_dump round-trip).
- `record_paused_build` → pause stage_log row (+`gate_pause` details:
  request_id/attempt_count/feature_id = durable home); defer (already PAUSED) →
  new facade `refresh_pending_approval_request_id` (status-preserving UPDATE,
  rowcount-0 raises).
- `list_paused_builds` → status-backed (`read_non_terminal_builds` filtered
  PAUSED); stage_label/attempt via new `parse_request_id` (inverse of
  `derive_request_id` — format is parseable by design); decision rehydrated
  from `details_json["gate"]`, degraded fallback.
- `mark_resumed` → no-op (SM owns transition). `mark_overridden` →
  `SqliteStageSkipRecorder`. `mark_cancelled` → **genuine no-op** (arch-review
  M1: the SM's `transition_to_cancelled` is the SOLE cancel writer for all four
  outcomes — the single-transition-owner rule now holds by construction, not by
  leaning on canceller idempotency; idempotency stays reserved for the
  cross-process CLI-cancel race it was built for).
- SM: `transition_to_paused` (handoff pop + chain), `transition_to_running`
  (PAUSED→RUNNING), `transition_to_failed`, `transition_to_cancelled` via
  `SqliteBuildCanceller`. **`StaleTransitionError`** (graft from bridge) raised
  on already-terminal rows BEFORE the JNB-102 publish — and (arch-review M2)
  **caught inside `await_and_dispatch`'s cancel legs**: WARNING "cancel
  superseded by concurrent terminal", return without raising — same softening
  posture as `transition_to_running` (which catches the optimistic
  RuntimeError, re-reads, terminal → warn + return). Without the catch the
  error would escape into `handle_message`'s generic handler and mis-emit
  `build-failed` after a correct `build-cancelled`. Recorded risk 1's crash
  mode closed on BOTH legs with consistent caller-visible behaviour.

## D4 — Restart recovery (REVISED per arch-review CRITICALs C1 + C2)

**Arch-review C1 (boot-order tap-drop)**: `_run_serve` awaits both reconcile
seams (serve.py:722-723) strictly BEFORE `_compose` runs — so v1's plan had
`recovery.reconcile_on_boot._handle_paused` re-publishing the approval request
(buttons envelope, verbatim request_id) at Step 2 with NO response subscriber
alive until `rearm_paused_gates` armed at Step 3.5. A tap in that window was
silently dropped on core NATS. **Fix — rearm owns BOTH re-emits**:

1. **Boot binding (Step 6.8)**: bind `recovery_reconcile_on_boot` to a closure
   building `PipelinePublisher` from the seam client and passing a
   **no-op ApprovalRepublisher** (INFO log per suppressed row) into
   `recovery.reconcile_on_boot(...)` — the PREPARING/RUNNING/FINALISING
   branches run unchanged; the PAUSED approval re-emit is deliberately
   suppressed at boot because a gate-owned sweep with a live subscriber owns it
   (comment names this contract). recovery.py itself is unmodified; its shipped
   tests stay green.
2. **`rearm_paused_gates`** (spawned from `_compose` after `bind_gate_parts`):
   per PAUSED row — parse request_id, rebuild ctx/deps/decision, start
   **`await_and_dispatch`** (pure public refactor of gate_check's tail,
   `wrappers.py:568-603`, reused by the live path — shipped four-step
   chain/dedup/emits verbatim), confirm the subscription is live, THEN re-emit
   **first** the AGENTS approval request (verbatim request_id, correlation
   stamped) and **second** the PIPELINE build-paused — full arm-before-post for
   BOTH envelopes, and request-before-paused preserves the jarvis button-join
   order. Window rebases to a full per-attempt window (DDR-027; documented).
   On approve → `resume_launcher` (dispatch minus `record_pending_build`) +
   deferred observer registration per R1 (no-op ack handle post-restart;
   terminal ack rides duplicate-terminal redelivery).
3. **Correlation landmine**: `build_recovery_approval_envelope` stamps
   `correlation_id=build.correlation_id` regardless of C1's re-routing (the
   envelope builder is reused by rearm's request re-emit; step-2b refuses only
   when BOTH sides non-None and mismatched — verify empirically in JNB-107).

**Arch-review C2 (twin seam / INTERRUPTED wedge)**: a crash inside the new
QUEUED→PREPARING→RUNNING→PAUSED hop window leaves an INTERRUPTED row (recovery
marks it) whose redelivered build-queued message would hit R2's
skip-without-ack forever — with `max_ack_pending=1` that wedges the whole
PIPELINE consumer. **Fix — bind the twin seam + status-aware duplicate arms**:

4. **Bind `consumer_reconcile_on_boot`** (Step 6.8 too) to
   `pipeline_consumer.reconcile_on_boot` with its **PAUSED scan suppressed**
   (empty `iter_paused_builds` / no-op republish fns — rearm owns PAUSED;
   ownership boundary documented at the bind site against future double-emit)
   while its Branch-2 in-flight/INTERRUPTED redispatch (real ack callback,
   `pipeline_consumer.py:1026-1045`) runs — the crash-mid-hop window heals by
   redispatch. Exact `ReconcileDeps` construction verified at implementation.
5. **R2 refined to three arms** on `DuplicateBuildError`: row terminal → ack;
   row PAUSED → skip WITHOUT ack (held slot; rearm owns); row INTERRUPTED →
   route to redispatch (the recovery-matrix "re-enters the lifecycle" path),
   never skip-without-ack.

**Ground-truth #9 contract disagreement — revised resolution**: BOTH PAUSED
re-emits (approval request AND build-paused) are owned by `rearm_paused_gates`;
`recovery.reconcile_on_boot` keeps its non-PAUSED recovery matrix. Satisfies
API-nats-pipeline-events §4 + FEAT-FORGE-010 :245-249 (build-paused re-emitted)
and API-sqlite-schema §6's intent (request re-emitted, verbatim id) — §6 gets a
clarifying ownership note covering all three boot actors.

## Refresh-loop decision (judges' cross-cutting #3)

The subscriber-level refresh loop stays **disabled for v1**: `_compose` passes
`repository=None` to `build_approval_gate_parts` (publish_refresh=None →
single-window waits, today's tested behaviour) while the SQLite repository is
threaded directly into `make_gate_check_deps`. Rationale: the refresh closure
binds the RAW publisher at parts construction — every refresh would mint a new
request_id with NO superseding build-paused → phone buttons go stale-refused
after the first window (breaks the JNB-107 run on any long pause). Window
expiry → REASON_MAX_WAIT cancel (JNB-107 scenario 5 intact). Follow-up recorded:
mirror build-paused on refresh, then enable.

## Broker posture

- Pin `ack_wait=ACK_WAIT_SECONDS` (1h, contract §2.2) in
  `_serve_daemon._attach_consumer` — fixes verified drift (config omits
  ack_wait today → 30s server default; only predispatch caught this).
- Pre-deploy runbook step: `nats consumer info` on the pre-existing GB10 durable
  (server-side config may need recreating in nats-infrastructure — flagged
  cross-repo dependency).
- All redelivery behaviour additionally reasoned AND tested at the 30s cadence
  (R2's skip-without-ack makes cadence non-load-bearing).
- Operator guidance: `approval.max_wait_seconds ≤ ack_wait`.

## D5 — C1: removal, not fix

Delete `mark_resume_pending`, `_resume_pending`, and the on_transition resume
special-case from `LifecycleEmitterAdapter` (+ tests pinning them); keep the
`awaiting_approval→emit_paused` routing row with a docstring pointing resume-emit
ownership at the daemon subscriber seam; update stale notes in
`_serve_deps_gating.py:38-49,207-215,427-431`. DDR-007:46 itself places the
resume emit in the subscriber path — removal is contract-aligned. The stale
JNB-107 task text naming mark_resume_pending is flagged to the operator.

## D6 — Recorded risks

- **CLI-cancel optimistic race**: closed structurally (StaleTransitionError +
  softening, above). Additionally (graft from runner-interrupt): wire
  `forge cancel` of PAUSED builds through `cli_cancel_build`'s synthetic-reject
  injector (now viable — `list_paused_builds` exists) so cancels flow through
  the SAME live gate_check frame. Wave 3.
- **CLI-cancel double-emit**: consciously deferred — the injector remains the
  only paused-build cancel entry; TerminalPublishLedger guarding recorded as the
  trigger-bound follow-up.

## D7 — DF-007

Full draft at `docs/state/TASK-GATE-D659/DF-007-draft.md` in DF-009's
gate-property framing ("the approval gate is a property of the forge build
lifecycle, enforced at forge's own dispatch boundary, re-armed from forge's own
ledger; callers are identity-pinned responders, never gate owners; v1 never
auto-approves; autonomy follows verification quality"). Filed to
`../ai-transition/docs/decisions/` ONLY after operator sign-off (trigger wording
conflict REGISTER vs plan-of-record noted).

## Files (~22 files, ~2,650 LOC incl. ~1,450 tests)

Src (new): `gating/degraded.py` (~70 — degraded_dispatch_gate_model +
Empty*Readers + recovery-decision fallback; arch-review minor: domain-level
stand-ins live in `forge.gating`, reused by live + rearm paths, not in a cli
composition module); `cli/_serve_gate_activation.py` (~290 — maybe_gate_build +
idempotency pre-read, _MirroredApprovalPublisher, rearm_paused_gates);
`gating/sqlite_adapters.py` (~350 — adapters + _PauseHandoff +
StaleTransitionError + factory).
Src (edit): `gating/identity.py` (+parse_request_id ~45); `gating/wrappers.py`
(await_and_dispatch pure refactor + defer-branch consolidation into it
(arch-review DRY minor) + StaleTransitionError catch on cancel legs ~95);
`lifecycle/persistence.py` (+refresh_pending_approval_request_id ~50);
`cli/_serve_deps.py` (gate call + observer-registration relocation callback +
three-arm duplicate handling (terminal→ack / PAUSED→hold / INTERRUPTED→
redispatch) + build_resume_launcher ~110); `cli/serve.py` (_compose:
repository=None to parts / SQLite repo to deps, bridge_registry=None, spawn
rearm ~35); `cli/_serve_production.py` (Step 6.8: BOTH seam bindings —
recovery with no-op ApprovalRepublisher + consumer_reconcile with suppressed
PAUSED scan ~85); `adapters/nats/pipeline_consumer.py` (register_ack_handle
relocation ~20); `cli/_serve_daemon.py` (ack_wait pin ~8);
`adapters/nats/approval_publisher.py` (correlation stamp ~8);
`subagents/autobuild_runner.py` (C1 removal, net ~-40);
`cli/_serve_deps_gating.py` (stale-note updates ~12); `cli/cli_runtime` cancel
injector wiring (Wave 3, ~40).
Tests: `tests/forge/gating/test_sqlite_gate_adapters.py` (new ~420);
`tests/integration/test_gate_activation_production_wiring.py` (new ~560);
`tests/integration/test_gate_restart_recovery.py` (new ~380);
edits to `test_lifecycle_recovery.py` (+correlation), `test_cli_serve_skeleton.py`
(+boot binding, +ack_wait pin), `test_pause_resume_publish.py` (C1 retirements).
Docs: `API-sqlite-schema.md` §6 ownership note; `DF-007-draft.md`.

## Waves (single task, three internally-green waves; /feature-plan split maps 1:1)

- **Wave 1 — foundations (activation-point-agnostic)**: sqlite_adapters +
  parse_request_id + await_and_dispatch refactor + refresh_pending... facade +
  correlation stamp + adapter/recovery tests. Mergeable alone.
- **Wave 2 — live round-trip**: maybe_gate_build + observer relocation (R1) +
  state-conditional ack (R2) + mirrored publisher + serve/_compose wiring +
  ack_wait pin + production-wiring scenario tests. Delivers the JNB-107 unblock.
- **Wave 3 — restart + closure**: recovery binding + rearm_paused_gates +
  restart scenario tests + C1 removal + CLI-cancel injector wiring + contract
  notes + DF-007 draft.

## Test plan

Pattern: `test_jnb101_production_wiring.py` — real publisher/subscriber/
injector + real PipelineLifecycleEmitter over InMemoryNats, order logs proving
envelope-before-transition and SQLite-before-wire, injected clocks, strict
asyncio, Clock.now() only, tmp-DB via the established connect_writer fixture.
Coverage per AC: pause dual-envelope order; approve→one resumed→launch fires;
reject→CANCELLED-first→one cancelled→zero resumed; expiry→REASON_MAX_WAIT;
spoof/correlation/stale-id refusals then legit reply still lands; defer→fresh
request_id + superseding build-paused; duplicate delivery mid-pause skipped
WITHOUT ack, post-terminal acked; restart: verbatim request_id + correlation on
re-emit, arm-before-post ordering asserted, post-restart approve launches via
resume_launcher, reject/expiry cancel, legacy unparseable id skipped with ERROR;
C1 guard (adapter no longer exposes mark_resume_pending); boot-binding identity
check (no warning stub); suite under the scoped pytest-9 baseline flags.
**Arch-review-driven additions**: boot-race test — response subscription
provably armed before ANY approval-request re-emit reaches the wire (order log
across the full boot sequence, not just rearm's internals); crash-mid-hop test
— row left INTERRUPTED mid transition_chain, redelivered message routes to
redispatch (never skip-without-ack; consumer not wedged); M1 proof — repo
mark_cancelled is a no-op and the SM is the sole cancel writer (assert exactly
one apply_transition across both gate_check cancel orderings);
StaleTransitionError caught inside await_and_dispatch (no build-failed
mis-emit after a superseding cancel).

## JNB-107 live-run assumption checklist (cross-repo, unverifiable here)

1. jarvis tolerates build-resumed with no prior build-started (pre-work gate).
2. AGENTS request captured before build-paused post (buttons attach; else
   text-only fallback = retest).
3. jarvis request-side dedup absorbs boot re-emits (same request_id) without
   double-posting.
4. Post-restart: operator re-taps the FRESH message; stale-button tap refusal +
   retap heals (step-2b empirical check).
5. `JARVIS_SLACK_DECIDED_BY=rich` set (OPS-001) — silent no-op otherwise.
6. Operator note: a PAUSED build does NOT appear in `forge status --in-flight`
   (correct — no sidecar attachment exists while paused; not a bug).
7. Pre-deploy: `nats consumer info` on the GB10 PIPELINE durable — the ack_wait
   pin may require recreating the pre-existing durable (nats-infrastructure).

## Arch-review outcomes folded in (v1 → v2)

Phase 2.5B (architectural-reviewer, 66/100 approve-with-recommendations):
- **C1 (CRITICAL, verified)**: boot-order tap-drop — Step-2 recovery re-emitted
  the approval request before any subscriber existed. → rearm owns BOTH PAUSED
  re-emits; recovery gets a no-op ApprovalRepublisher (D4.1-2).
- **C2 (CRITICAL, verified)**: unbound twin seam `consumer_reconcile_on_boot` +
  R2's skip-without-ack = permanent consumer wedge for crash-mid-hop
  INTERRUPTED rows. → bind the twin seam (PAUSED scan suppressed) + three-arm
  duplicate handling (D4.4-5).
- **M1**: repo `mark_cancelled` → genuine no-op; SM is sole cancel writer.
- **M2**: `StaleTransitionError` caught in `await_and_dispatch` (else a
  cancelled-then-failed mis-emit via handle_message's generic handler).
- Minors: degraded stand-ins moved to `gating/degraded.py`; defer-branch
  await/dispatch duplication consolidated into the refactor; `--in-flight`
  operator note (checklist #6).
- Verified-sound by the review (no action): R1 mechanics incl. registry-row
  non-creation; R2 no-double-gate; the repository-param split for the
  refresh-disable decision; await_and_dispatch pure extractability;
  parse_request_id invertibility; C1-removal disposition.

## Future (banked from the panel; not in scope)

Evidence-based gating (post-UBS-002) moves the activation point runner-side/
outcome-side. Banked, judge-verified mechanisms: two-node marker/interrupt split
(state lands on node return); interrupt-detection seam in the observer loop
(bypasses fetch-replay; also fixes the verified latent false-BuildStarted-on-
interrupted bug); checkpointer/persistence/SDK-resume verification method;
translator replay-guard pair (mandatory before any runner-side awaiting_approval
ever reaches the values channel — documented landmine); AUTO_APPROVE suppress
leg as the ADR-ARCH-019 relaxation seam; mid-life re-arm sweep for
ApprovalPublishError-orphaned rows.
