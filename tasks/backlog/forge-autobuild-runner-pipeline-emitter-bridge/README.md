# Forge Autobuild-Runner Pipeline-Emitter Bridge

**Feature ID**: FEAT-PEBR
**Parent**: TASK-FORGE-FRR-F010M (scoping deliverable)
**Review**: TASK-REV-F010M (decision-mode review, score 78/100)
**Status**: planned
**Approach**: Option C (Streaming via `runs.join_stream` + `Last-Event-ID`)
**Fallback**: Option E (Hybrid) — only if Option C's translation layer
proves untenable; pivot decision **must** be made no later than Wave 2
smoke-gate failure.

---

## Problem

When the autobuild runs inside the langgraph-runner sidecar, its
async outcomes (success / failure / pause / resume / cancel) produce no
`pipeline.*` envelope on JetStream. Jarvis's chat REPL goes silent the
moment the dispatch chain returns HTTP 200 because there is nothing on
the wire to render between prompts.

Empirical trigger: RESULTS Addendum 5 (correlation_id
`e9433033-ea80-449f-885d-b2d1bdfb839e`), 2026-05-04 — the post-F010J
rerun captured **only** the inbound `pipeline.build-queued.FEAT-43DE`
envelope on the wire; no terminal envelope.

## Solution

Wire a **lifecycle bridge** in `forge serve` that:

1. Attaches per-build on `pipeline.build-queued.*` arrival, opening an
   SSE stream to the sidecar via
   `client.runs.join_stream(thread_id, run_id, last_event_id=...)`.
2. Translates SSE `StreamPart` events into typed `pipeline.*`
   envelopes (`BuildStartedPayload`, `StageCompletePayload`,
   `BuildCompletePayload`, `BuildFailedPayload`, `BuildPausedPayload`,
   `BuildResumedPayload`, `BuildCancelledPayload`).
3. Publishes envelopes via the existing `forge.adapters.nats.publisher`
   path with the inbound `correlation_id` threaded through every emit.
4. Defers the inbound `build-queued` ack from dispatch return to
   terminal arrival (closing the redelivery storm).
5. Survives daemon restart via `Last-Event-ID` replay + recovery sweep
   on persisted SQLite registry rows.

## Subtasks (14 across 5 waves)

| ID | Title | Wave | Mode | Complexity |
|---|---|---|---|---|
| TASK-FRR-PEB-001 | Defer build-queued ack to terminal | 1 | task-work | 5 |
| TASK-FRR-PEB-002 | Bridge skeleton + SQLite registry | 1 | task-work | 6 |
| TASK-FRR-PEB-003 | SSE → typed envelope translator (§4 producer) | 2 | task-work | 7 |
| TASK-FRR-PEB-004 | Wire bridge into forge serve (§4 consumer) | 2 | task-work | 6 |
| TASK-FRR-PEB-005 | F010F coexistence boundary | 2 | task-work | 5 |
| TASK-FRR-PEB-006 | Pause/resume canonicalisation (FW10-010 amendment) | 3 | task-work | 6 |
| TASK-FRR-PEB-007 | Cancel emit ownership | 3 | task-work | 5 |
| TASK-FRR-PEB-008 | Reconnect + 300s deadline | 4 | task-work | 6 |
| TASK-FRR-PEB-009 | Restart recovery (replay + sweep) | 4 | task-work | 7 |
| TASK-FRR-PEB-010 | Version-mismatch diagnostic | 4 | task-work | 4 |
| TASK-FRR-PEB-011 | Publish-failure non-regression | 4 | direct | 4 |
| TASK-FRR-PEB-012 | forge status --in-flight surface | 5 | direct | 4 |
| TASK-FRR-PEB-013 | Sidecar-aware E2E integration test | 5 | task-work | 7 |
| TASK-FRR-PEB-014 | ASSUM-009 contract lock test | 5 | direct | 3 |

Total complexity: 75. Mean: 5.4. Median: 5.5.

## Wave Plan and Smoke Gates

```
Wave 1: T1 → T2                     (foundation, no smoke gate yet)
Wave 2: T3 → T4 → T5                (@smoke gate FIRES — pytest tests/bdd -m smoke -x)
Wave 3: T6, T7                       (@smoke continues green)
Wave 4: T8 → T9; T10, T11 parallel   (@smoke continues green)
Wave 5: T12, T13, T14                (full @smoke + @regression — landing complete)
```

## Verifications Carried Forward (locked inputs)

- **ASSUM-003** — backoff: `1.0s` initial, `30.0s` cap, exponential ×2,
  reset on success, **no fixed maximum**, terminate only on
  `CancelledError`. Plus a 300s per-build SLA deadline. Sourced from
  `src/forge/cli/_serve_daemon.py:90-93,447,468`.
- **ASSUM-009** — cross-process correlation-id is **moot under Option C**
  (single-process AST guard extends). Locked by T14 as a no-op contract
  test; insurance against option flip.

## Cross-Cutting Concerns Addressed

- F010F coexistence (T5) — first-wins, no double-publish.
- FW10-010 amendment (T6) — `approval_subscriber.py` resume emit is
  dropped when bridge is wired.
- SDK volatility (T3 + T10 + T13) — version bounds, contract test,
  fail-fast diagnostic, sidecar-aware E2E.
- Restart recovery (T9) — `Last-Event-ID` replay + recovery sweep,
  idempotent.
- Operator UX (T8 deadline + T12 status) — sidecar-unreachable surfaces
  as build-failed within 5 min; `forge status --in-flight` mid-flight.
- F010C correlation-id contract (T2 + T4 + T14) — extends existing AST
  guard.

## Files in this feature folder

- `IMPLEMENTATION-GUIDE.md` — wave-plan, mandatory diagrams, §4
  Integration Contract, smoke-gate plan, cross-cutting concerns.
- `TASK-FRR-PEB-001` through `TASK-FRR-PEB-014` — 14 subtask files,
  one per task.
- `README.md` — this file.

The companion BDD spec lives at
`features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge.feature`
and will be tagged with `@task:` annotations by the BDD scenario linker
(Step 11 of /feature-plan).

## Next steps

1. Review this folder and the IMPLEMENTATION-GUIDE.md.
2. Start Wave 1: `/task-work TASK-FRR-PEB-001` followed by `T2`.
3. After Wave 2 lands, verify `pytest tests/bdd -m smoke -x` is green
   in the **forge** tree (verify path against
   `tests/bdd/test_nats_fleet_integration.py` precedent).
4. Or use `/feature-build FEAT-PEBR` for autonomous wave-plan execution.

## Required operator follow-up (post-merge)

When all 14 tasks are merged:

- Verify a real autobuild end-to-end against the live sidecar produces
  the canonical lifecycle sequence on the chat REPL between prompts.
- Re-run the FEAT-JARVIS-INTERNAL-001 first-real-run scenario that
  empirically triggered F010M; assert no silent-on-the-wire failure.
- Update RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  with a closure addendum referencing this feature's completion.
- Mark TASK-FORGE-FRR-F010M complete per its AC-6/AC-7.
