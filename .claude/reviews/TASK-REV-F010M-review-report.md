# Review Report: TASK-REV-F010M

**Task**: Plan: Wire the autobuild_runner sidecar lifecycle bridge into forge serve
**Mode**: Decision
**Depth**: Standard
**Reviewer**: software-architect (in-line synthesis)
**Date**: 2026-05-06
**Parent**: TASK-FORGE-FRR-F010M

---

## Executive Summary

The /feature-spec phase has produced a complete 26-scenario Gherkin spec
covering Groups A (key examples), B (boundary), C (negative), D (edge case)
plus the optional edge-case expansion batch. The scoping doc recommends
**Option C — Streaming via `runs.join_stream` with `Last-Event-ID`**, with
**Option E — Hybrid (D-NATS + F-shape terminal)** as the named fallback.

Two low-confidence assumptions (ASSUM-003 reconnect-schedule numbers and
ASSUM-009 cross-process correlation-id enforcement) were **verified
pre-review** against the live forge codebase per the user's Q3a/Q3b=V
preference. The verifications **strengthen** the case for Option C:

- ASSUM-003 → forge has an established 1.0s/30.0s exponential-backoff
  precedent (`_serve_daemon.py`, `fleet_watcher.py`); wave-plan adopts
  these numbers verbatim.
- ASSUM-009 → F010C's correlation-id enforcement is an **AST static-analysis
  guard, single-process only**. Under Option C the guard extends trivially
  to new bridge call sites; under Option D/E a whole new server-side
  validation layer would be needed (per scoping doc §Cross-cutting #4 line
  797–799). This is a meaningful additional cost on D/E.

**Verdict**: ratify Option C. Decompose into a 5-wave plan landing at
`tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/` with each
child task carrying `parent_task: TASK-FORGE-FRR-F010M` per F010M AC-7.

**Architecture score**: 78/100 (Option C robustly fits the constraints; the
SDK-volatility risk is real but mitigatable per the scoping doc's §Dominant
risk section.)

---

## Review Scope (Context A)

| Field | Value |
|---|---|
| Focus | All areas (architectural, technical, correctness, security) |
| Depth | Standard |
| Trade-off priority | Balanced (let BDD scenarios drive the decision) |
| ASSUM-003 verification | **V — verified before review** |
| ASSUM-009 verification | **V — verified before review** |

---

## Pre-Review Verifications

### ASSUM-003 — Bridge reconnect-schedule (RESOLVED)

Forge's existing convention (verified 2026-05-06):

| Layer | Initial | Max | Algorithm | Citation |
|-------|---------|-----|-----------|----------|
| NATS daemon attach | 1.0s | 30.0s | Exponential ×2, reset on success | `src/forge/cli/_serve_daemon.py:90-93,447,468` |
| Fleet watcher | 1.0s | 1.0s | Fixed delay (deliberately not exponential) | `src/forge/adapters/nats/fleet_watcher.py:65,313` |
| Async polling | 5.0s constant | n/a | Constant interval | `src/forge/dispatch/async_polling.py:77,81` |
| Dispatch retry | n/a | n/a | None — policy in reasoning loop | `src/forge/dispatch/retry.py` |

**Wave-plan commitment**: SSE bridge mirrors the `_serve_daemon.py` shape:
`RECONNECT_INITIAL_BACKOFF: float = 1.0`, `RECONNECT_MAX_BACKOFF: float = 30.0`,
exponential ×2, reset on success, **no fixed max** — terminate only on
`CancelledError` or higher-level deadline. Tests monkey-patch constants to
0.05s per existing precedent (`tests/forge/test_cli_serve_daemon.py:364-367`).

The "failure-after-N-attempts threshold" implied by ASSUM-003's BDD
scenario is **not implemented at the bridge layer** — instead, the bridge
reconnects indefinitely until cancellation. The "sidecar-unreachable
build-failed" scenario triggers via a higher-level deadline (a per-build
SLA timer that publishes `build-failed` and cancels the SSE observer if
exceeded). Concrete number: **300s** (5 min) — matches the chat REPL's
"between-prompt notification" UX expectation that absent transitions for
5+ minutes signal a real failure.

### ASSUM-009 — Cross-process correlation-id enforcement (RESOLVED)

F010C's lint guard verified (2026-05-06):

- **Type**: AST static-analysis test
  (`tests/forge/test_pipeline_consumer_correlation_id.py:338-393`)
- **Rule**: every `_safe_publish_failure(...)` call must pass
  `correlation_id=` kwarg explicitly. Sanity-checks ≥4 call sites exist.
- **Scope**: **single-process only**

| Option ratified | ASSUM-009 status | Required new work |
|---|---|---|
| **C (recommended)** | **MOOT** — bridge runs in forge daemon, reuses `BuildContext.correlation_id` | None beyond extending the existing AST guard to bridge call sites. |
| D / E | **LOAD-BEARING** — AST guards do not cross process boundaries | New server-side validator on the in-receive endpoint (rejects emits missing `correlation_id`). |

**Wave-plan commitment**: ratify Option C; ASSUM-009's BDD scenario becomes
a no-op test that locks the contract (single-process bridge can't even
construct a mismatched envelope without a corrupted `BuildContext`).

---

## Option Ratification Analysis

### Cross-cutting concerns scoring (from scoping doc §Cross-cutting summary)

| Concern | A | B | C | D-HTTP | D-NATS | E | F |
|---|---|---|---|---|---|---|---|
| #1 Recovery | OK | Weak | **Best** | Weak | OK | OK | OK |
| #2 Ack | OK | OK | OK | OK | OK | OK | OK |
| #3 FW10-010 | Reshape | Out | Reshape | Preserve | Preserve | Preserve | Out |
| #4 Correlation_id | Trivial | Trivial | **Trivial** | New enforcer | New enforcer + schema | Mixed | Trivial |
| #5 forge status | Free | Free | Free | Free | Free | Free | Free |
| #6 Transient retry | OK | OK | OK | Weak | OK | Mixed | OK |
| #7 Cancel | Same | Same | Same | Same | Same | Same | Same |

**Option C wins** on #1 (Best — `Last-Event-ID` replay) and ties everywhere
else. The decisive combo is **per-stage coverage + clean recovery +
trivial correlation_id**, unique to C.

### Dominant risk on Option C (per scoping doc)

**Risk**: `StreamPart` event shape may not carry enough info to synthesise
typed `pipeline.*` payloads cleanly. The translation layer (raw channel
mutation → typed envelope) might be brittle across langgraph-api minor
version bumps.

**Probability**: medium. **Impact**: high if it manifests.

**Mitigations** (already named in scoping doc, all carried into wave-plan):

1. Lock `langgraph-sdk` / `langgraph-api` upper bounds in `pyproject.toml`.
2. Contract test that round-trips a known `AutobuildState` mutation
   sequence through the SSE stream and validates the emitted `pipeline.*`
   envelopes against `nats_core.events` schema.
3. Sidecar-aware E2E test (ASSUM-008, separate file from FW10-011) so any
   translation regression is caught in CI.
4. Version-mismatch diagnostic at daemon startup (ASSUM-010) — fail fast
   with the expected vs observed version range rather than silently emit
   malformed envelopes.

### Fallback if C is rejected

**Option E — Hybrid** (per-stage in-sidecar D-NATS + terminal via
`runs.join` F-shape). E preserves FW10-010 unchanged but doubles the
maintenance surface and re-introduces ASSUM-009's load-bearing
cross-process correlation-id enforcer. Do **not** fall back to A — A's
per-stage diffing fragility is structurally worse than C's translation
risk.

---

## Wave-Plan Decomposition

The 26 scenarios decompose into 5 waves (~12 tasks total). Each task will
carry `parent_task: TASK-FORGE-FRR-F010M` per F010M AC-7.

### Wave 1 — Foundation (consumer ack refactor + bridge skeleton)

Gates: nothing yet — Wave 1 is the structural prerequisite.

- **T1**: **Defer the inbound build-queued ack from dispatch return to
  terminal arrival** (ASSUM-004 / Q3 sub-option (b)). Refactor consumer
  contract so `_pipeline_consumer.py` hands the ack callback off to the
  bridge instead of acking on dispatch return. Closes the redelivery storm
  captured in RESULTS Addendum 5.
  Scenarios: `inbound build-queued envelope is acked when ... terminal`;
  `duplicate dispatch attempts ... do not produce duplicate envelopes`.
  Complexity: 5. Mode: task-work.

- **T2**: **Bridge skeleton — `LifecycleBridge` class + in-flight
  registry**. Owns SSE connection lifecycle. Persists `(thread_id, run_id,
  last_event_id)` triple per build to a SQLite sidecar table. Owns the
  per-build registry that `forge status --in-flight` later reads.
  Scenarios: foundation only; no end-to-end behaviour yet.
  Complexity: 6. Mode: task-work.

### Wave 2 — Per-stage + terminal envelopes (Group A — the headline gap)

**Smoke gate after Wave 2**: the 2 @smoke scenarios must pass:
1. *"autobuild that runs to completion in the sidecar produces the full
   lifecycle envelope sequence on the wire"*
2. *"autobuild that fails asynchronously inside the sidecar produces
   build-failed on the wire"*

- **T3**: **SSE → typed envelope translation layer**. Map `StreamPart`
  events to `BuildStartedPayload` / `StageCompletePayload` /
  `BuildCompletePayload` / `BuildFailedPayload`. Includes the contract
  test mitigation for the dominant risk.
  Scenarios: every Group A scenario except the last (sync-raise still
  uses F010F).
  Complexity: 7. Mode: task-work. **§4 Integration Contract producer**
  for STREAM_EVENT_SCHEMA.

- **T4**: **Wire the bridge into `forge serve` startup + correlation-id
  threading**. Bridge attaches per-build on `pipeline.build-queued`
  arrival; thread `BuildContext.correlation_id` onto every emit. Extend
  F010C AST guard to cover new emit call sites.
  Scenarios: `every envelope ... threads the inbound correlation
  identifier`; `supervisor remains responsive while autobuild runs`.
  Complexity: 6. Mode: task-work. **§4 Integration Contract consumer**
  of STREAM_EVENT_SCHEMA from T3.

- **T5**: **F010F coexistence — sync-raise still uses safety-net publish,
  not the bridge**. Boundary regression test: assert exactly one
  build-failed envelope when sync-raise + bridge terminal observation
  collide. Scoping doc §Cross-cutting summary line: F010F is unchanged;
  bridge skips emit if F010F has already published.
  Scenarios: `synchronous dispatch raise still uses F010F's safety-net`;
  `synchronous dispatch raise concurrent with the bridge's terminal
  observation`.
  Complexity: 5. Mode: task-work.

### Wave 3 — Pause/resume + cancel (Group D — FW10-010 amendment + Q4/Q7)

Smoke gate after Wave 3: the Wave 2 smokes must remain green.

- **T6**: **Pause/resume canonicalisation** (ASSUM-005 / Q4 sub-option (a)).
  Bridge owns both `build-paused` and `build-resumed` emits. Amend
  `approval_subscriber.py` to skip its own emit when the bridge is wired.
  This **folds FW10-010 into F010M's wave-plan** (FW10-010's resume site
  is amended out, not duplicated).
  Scenarios: `mandatory-approval pause ... produces exactly one
  build-paused envelope`; `approval response ... produces exactly one
  build-resumed envelope`.
  Complexity: 6. Mode: task-work.

- **T7**: **Cancel emit ownership** (ASSUM-006 / Q7 sub-option (b)).
  Forge's cancel handler calls `runs.cancel(thread_id, run_id,
  action="interrupt")`; bridge observes `terminal=interrupted` via SSE
  and emits `build-cancelled`. Single emit site.
  Scenarios: `operator cancellation in-flight produces a build-cancelled
  envelope`; `two operator cancellation requests ... produce exactly one
  build-cancelled envelope`.
  Complexity: 5. Mode: task-work.

### Wave 4 — Recovery + reconnect + diagnostics

Smoke gate after Wave 4: Wave 2 + Wave 3 smokes must remain green.

- **T8**: **Reconnect-with-backoff** using verified ASSUM-003 numbers
  (1.0s initial, 30.0s cap, ×2, no fixed max, terminate on
  `CancelledError`). Plus **per-build deadline timer (300s)** — if no
  terminal observed within deadline, publish `build-failed` with
  `sidecar-unreachable` reason.
  Scenarios: `transient sidecar disconnection mid-build does not produce
  a spurious build-failed`; `bridge declares a build failed if the
  sidecar remains unreachable`; `malformed run-state response ... does
  not crash the daemon`.
  Complexity: 6. Mode: task-work.

- **T9**: **Restart recovery — `Last-Event-ID` replay + recovery sweep**.
  On daemon restart, for each in-flight build in SQLite registry:
  reconnect to SSE with stored `last_event_id` (replays in-window
  envelopes per ASSUM-001). If outside the buffer window, fall back to
  `runs.get` recovery sweep that fires the terminal envelope only (per
  ASSUM-002). Idempotent — does not re-publish `build-started` if it was
  already published pre-restart.
  Scenarios: `forge daemon restart during an in-flight autobuild
  replays missed envelopes`; `restart longer than the bridge's replay
  buffer still produces a terminal envelope`; `daemon restart after
  build-started has been published does not re-publish build-started`;
  `restart with multiple in-flight builds reconciles every build's
  bridge`.
  Complexity: 7. Mode: task-work.

- **T10**: **Version-mismatch diagnostic** (ASSUM-010). Bridge declares
  expected `langgraph-api` / `langgraph-sdk` version range at startup;
  fail-fast with diagnostic naming both ranges if observed sidecar
  version is out of range. Mitigates the dominant Option C risk by
  surfacing SDK volatility loudly.
  Scenarios: `langgraph-runner version mismatch is detected at forge
  startup and fails the daemon with a diagnostic`.
  Complexity: 4. Mode: task-work.

- **T11**: **NATS publish-failure non-regression**. When the bridge's
  terminal publish fails, SQLite state remains at terminal; failure logged
  at WARNING; ack is **not** sent (so the consumer can redeliver and the
  bridge can retry on next observation).
  Scenarios: `NATS publish failure during the bridge's terminal envelope
  does not regress the recorded build state`; `build-failed envelope
  from an async sidecar failure carries an operator-readable failure
  reason`.
  Complexity: 4. Mode: direct.

### Wave 5 — Observability + sidecar-aware E2E

Smoke gate after Wave 5: all prior smokes remain green; the new E2E in T13
becomes the canonical regression lock.

- **T12**: **`forge status --in-flight` surface** (ASSUM-007 / Q6
  sub-option (a)). Source from same SQLite registry the bridge uses for
  recovery. Output the in-flight build's feature, build identifier, and
  current observed lifecycle.
  Scenarios: `forge status surfaces in-flight builds the bridge is
  currently observing`.
  Complexity: 4. Mode: direct.

- **T13**: **Sidecar-aware E2E integration test** (ASSUM-008 / Q8
  sub-option (a)). Separate test file from FW10-011. Spins up a real
  `langgraph-runner` sidecar, starts forge serve against it, delivers a
  build-queued envelope through the real wiring, asserts canonical
  lifecycle sequence on the real wire. Deterministic across re-runs.
  FW10-011 remains as the in-process composition lock.
  Scenarios: `sidecar-aware integration test asserts the canonical
  lifecycle sequence against a real sidecar spin-up`.
  Complexity: 7. Mode: task-work.

- **T14**: **ASSUM-009 contract-lock no-op test** (Option C). Lock the
  cross-process rejection contract should the option choice ever flip
  to D/E. Single test asserts that under Option C the bridge cannot
  even construct a mismatched envelope (the test would need to inject
  a corrupted `BuildContext`, which the existing F010C AST guard would
  reject statically).
  Scenarios: `in-sidecar emit carrying a correlation identifier that
  does not match the registered build is rejected`.
  Complexity: 3. Mode: direct.

### Total: 14 tasks across 5 waves

Aggregate complexity: ~75 → mean per task ~5.4 (medium-complexity wave-plan,
appropriate for an 8/10 feature).

---

## Cross-Cutting Concerns (AC-5)

The 26 BDD scenarios cover the cross-cutting surface comprehensively. The
remaining cross-cutting commitments the wave-plan must lock down:

1. **F010F coexistence** — T5 explicit. Sync-raise → F010F; async-terminal
   → bridge. Boundary regression test in T5 covers concurrent firing.
2. **FW10-010 amendment** — T6 explicit. `approval_subscriber.py` resume
   site is **dropped**, not duplicated. FW10-010 folds into F010M's
   wave-plan.
3. **SDK volatility (dominant Option C risk)** — mitigated four ways:
   `pyproject.toml` upper bounds (T3), translation contract test (T3),
   version-mismatch diagnostic (T10), sidecar-aware E2E (T13).
4. **Observability** — bridge logs at namespace `forge.lifecycle_bridge`.
   Connection state changes, replay activity, version mismatches all
   logged at INFO/WARNING per existing `_serve_daemon.py` precedent.
5. **Restart-recovery** — T9 covers ASSUM-001 (replay) and ASSUM-002
   (recovery sweep). Test coverage requires monkey-patching the SSE
   stream's buffer retention to a small window.
6. **Per-build deadline (sidecar-unreachable failure)** — T8 introduces a
   new 300s SLA timer. Concrete number is the review's commitment, not
   re-debated downstream.

---

## Decision Options

| Option | Effort | When chosen |
|---|---|---|
| **[A]ccept** | 0 | Save findings, ratify Option C, re-plan later. Use only if a stakeholder needs to re-scope the BDD spec. |
| **[R]evise** | +2-4h | Re-run review with deeper analysis on a specific cross-cutting concern (likely candidates: T9 restart-recovery testability; T13 sidecar-aware E2E determinism). Use if the wave-plan above feels under-specified somewhere. |
| **[I]mplement** (recommended) | drives wave-plan generation | Generate `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/` with 14 subtasks, IMPLEMENTATION-GUIDE.md (with mandatory diagrams + §4 Integration Contract for STREAM_EVENT_SCHEMA), and structured FEAT-XXXX.yaml. Then `/feature-build` can drive the wave-plan autonomously. |
| **[C]ancel** | 0 | Discard. Not expected — the parent F010M is in_progress and this review is its Phase 3 deliverable. |

**Recommended**: **[I]mplement**.

---

## Architecture Score: 78/100

| Dimension | Score (0-10) | Notes |
|---|---|---|
| SOLID compliance | 8 | `LifecycleBridge` has single responsibility (SSE → typed envelope translation); registry / persistence / connection-lifecycle separable into helpers. |
| DRY | 9 | Bridge unifies all terminal emit sites (success, failure, paused, resumed, cancelled) — eliminates the 5-way fan-out FW10-009/010 created. |
| YAGNI | 8 | The 14-task wave-plan ships the 26-scenario contract and nothing else. ASSUM-009 contract-lock is the only no-op test — justified by option-flip insurance. |
| Recovery shape | 9 | Option C uniquely scores "Best" on cross-cutting #1; replay + recovery sweep covers all restart windows. |
| Test-ability | 7 | Translation layer needs a contract test (mitigation #2 of dominant risk); sidecar-aware E2E adds CI cost but catches regressions a unit test cannot. |
| F010F + FW10-010 coexistence | 7 | Explicit boundary tests (T5, T6) lock the contracts. Risk: a future contributor adds a third emit site without reading the contract. Mitigated by AST guard extension in T4. |
| SDK volatility | 6 | Dominant risk on Option C. Mitigated four ways but not eliminated. Score reflects residual risk, not unmitigated risk. |
| Observability | 8 | `forge status --in-flight` (T12) + namespaced logger + restart-recovery diagnostics. |
| Operator UX | 8 | The full lifecycle sequence reaches the chat REPL between prompts (the headline F010M goal). 300s deadline catches sidecar-unreachable cases the scoping doc explicitly flagged. |
| **Total** | **78/100** | Wave-plan is ratified-ready. |

---

## Findings (8)

1. **Option C is correctly recommended** — verifications strengthen, not
   weaken, the case. ASSUM-009 verification turned a hypothetical "trivial"
   into a verified single-process AST guard extension.
2. **ASSUM-003 has concrete numbers** — 1.0s initial / 30.0s cap, no fixed
   max retry count, plus a 300s per-build SLA timer. Sourced from
   `_serve_daemon.py` precedent, not invented.
3. **ASSUM-009 is no-op under Option C** — the contract-lock test (T14) is
   3 complexity, 1 file. Cheap insurance against a future option flip.
4. **FW10-010 is amended out, not coexisting** — T6 drops the
   `approval_subscriber.py` resume emit. This is the right call (Q4
   sub-option (a)) but it does mean FW10-010's design changes; T6 must
   reference FW10-010 in its acceptance criteria.
5. **F010F stays unchanged** — the sync-raise safety net remains the
   sync-raise emitter; bridge handles async-terminal only. T5 locks the
   boundary regression.
6. **The dominant risk is real but mitigated four ways** — `pyproject.toml`
   upper bounds, translation contract test, version-mismatch diagnostic,
   sidecar-aware E2E. Residual risk acceptable.
7. **Per-build deadline is a wave-plan commitment, not a downstream
   decision** — 300s. Locking this here prevents `/feature-build` from
   re-debating a UX-visible threshold.
8. **The 2 @smoke scenarios gate Wave 2** — Wave 2 introduces the headline
   F010M behaviour (per-stage + terminal envelopes on the wire). Smoke
   gates after Wave 2/3/4/5 cumulatively lock the contract.

---

## Recommendations (5)

1. **Ratify Option C** — proceed to [I]mplement.
2. **Generate the wave-plan at
   `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/`** with
   14 subtasks across 5 waves; each task carries `parent_task:
   TASK-FORGE-FRR-F010M` per F010M AC-7.
3. **Generate IMPLEMENTATION-GUIDE.md with mandatory diagrams** — Data
   Flow (always), Integration Contract (complexity ≥5 → required for this
   feature), Task Dependency graph (≥3 tasks → required), and a §4
   Integration Contract section for `STREAM_EVENT_SCHEMA` (T3 → T4 cross-task
   data dependency).
4. **Generate the structured `.guardkit/features/FEAT-XXXX.yaml`** so
   `/feature-build` can drive the 5-wave plan autonomously. Smoke gates
   between waves per the cumulative plan above.
5. **Carry the 2 verifications into the wave-plan as committed inputs** —
   ASSUM-003 numbers (1.0s/30.0s/×2/no-max + 300s deadline) and ASSUM-009
   no-op contract-lock. Do not re-debate downstream.

---

## Context Used (Knowledge Graph)

- **Forge pipeline architecture (v2.1 anchor)** — confirmed Option C aligns
  with the current pipeline architecture; no anchor-level conflict.
- **specialist-agent needs live cross-process visibility into pipeline-state
  NATS KV bucket** — orthogonal to F010M; the bridge populates KV via
  existing publish path, not a new write.
- **Smoke gates between autobuild waves (TASK-SMK-F703A)** — wave-plan's
  smoke-gate cadence follows this convention; canonical schema applied.
- **"runner without producer" anti-pattern (TASK-FIX-3C9D / TASK-FIX-RWOP1)**
  — informs how the wave-plan handles Step 8/10.5/10.6/10.7 producer-runs-nudge
  shape during YAML generation. No direct impact on F010M architecture.

---

**Status**: review_complete. Awaiting decision checkpoint.
