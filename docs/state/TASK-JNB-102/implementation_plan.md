# TASK-JNB-102 Implementation Plan — build-cancelled on CANCELLED transitions

**Status**: v1 (light intensity — complexity 5, parent_review provenance;
design questions pre-resolved by the JNB-101 5-reader sweep + arch review;
subagent capacity degraded by the 2026-07-05 Fable usage limit, so planning
and review run inline with the cancel-emit reader's map as input)
**Date**: 2026-07-05

## Ground truth (from the verified cancel-emit sweep)

- Only the lifecycle-bridge SSE path publishes `build-cancelled` today, and
  it has no wired entry point; all three target transitions are silent.
- `publish_build_cancelled` lives at
  `src/forge/adapters/nats/pipeline_publisher.py:272` (subject
  `pipeline.build-cancelled.{feature_id}`);
  `PipelineLifecycleEmitter.emit_cancelled(ctx, *, reason, cancelled_by,
  cancelled_at)` at `src/forge/pipeline/__init__.py:543` already swallows
  `PublishFailure` via `_safe_publish` (DDR-007 built in).
- `BuildCancelledPayload` requires feature_id, build_id, reason,
  cancelled_by, cancelled_at, correlation_id.
- `CliSteeringHandler.handle_cancel` is **sync**; `BuildSnapshot` has no
  correlation_id and no feature_id on the OTHER_RUNNING branch → the CLI
  emit needs row-lookup enrichment outside the handler.
- The CLI already publishes to NATS (`forge queue`'s sync one-shot
  `publish(subject, body)` seam over `FORGE_NATS_URL`,
  `src/forge/cli/queue.py:242`) — reuse it; `cancel.py` already loads the
  BuildRow (feature_id + correlation_id) via `find_active_or_recent`.
- Bridge emit-authority conflict (noted by the sweep): theoretical
  double-emit if a bridge-attached build is CLI-cancelled AND the sidecar
  ever surfaces a terminal-cancel SSE event — today the runner never
  writes cancelled/interrupted so the bridge cancel rule cannot fire;
  documented in code comments, not defended with a ledger (task scope:
  "one emit per transition, fire-and-forget").

## Design

1. **`GateCheckDeps.publish_cancelled`** (wrappers.py, additive field):
   optional async callback `(*, reason: str, cancelled_by: str) -> None`.
   Called via a `_publish_cancelled_best_effort` helper (None-safe,
   try/except → WARNING, after the SQLite transition + mark_cancelled) at:
   - gate_check max-wait branch (REASON_MAX_WAIT, cancelled_by=SOURCE_ID)
   - _dispatch_response reject branch (reason=notes or REASON_REJECT,
     cancelled_by=response.decided_by)
   - _dispatch_response defer-timeout branch (REASON_MAX_WAIT, SOURCE_ID)
2. **`make_gate_check_deps`** (_serve_deps_gating.py): when ctx +
   parts.emitter are present, bind `publish_cancelled` to
   `parts.emitter.emit_cancelled(ctx, ...)` (correlation_id and
   feature_id come from the bound BuildContext).
3. **`CliSteeringHandler.cancelled_notifier`** (cli_steering.py, additive
   field): optional sync Protocol
   `notify_cancelled(*, build_id, reason, cancelled_by) -> None`, called
   after `mark_cancelled` on the three cancel branches (never the
   TERMINAL no-op), try/except → WARNING. cancelled_by = responder or
   "forge-cli"; reason = operator reason or "cli cancel".
4. **Production notifier** (cli/runtime.py): `_SqliteRowCancelledNotifier`
   — looks up the build row by build_id via persistence
   (`find_active_or_recent`), builds `BuildCancelledPayload` +
   `MessageEnvelope(source_id="forge", event_type=BUILD_CANCELLED,
   correlation_id=row.correlation_id)`, publishes to
   `pipeline.build-cancelled.{feature_id}` through the injected sync
   publish seam (default: `forge.cli.queue.publish`). Missing row →
   WARNING + skip (cannot build the subject). Wired by default in
   `build_cli_runtime` so `forge cancel` emits for real, best-effort.
5. **Out of scope**: no NATS consumers, no replay, no ledger claim, no
   changes to `pipeline_publisher.py` (call sites only — the task
   consumes `emit_cancelled`/`publish` as-is).

## Files

- src/forge/gating/wrappers.py — field + helper + 3 call sites
- src/forge/cli/_serve_deps_gating.py — bind publish_cancelled in
  make_gate_check_deps
- src/forge/pipeline/cli_steering.py — CancelledNotifier Protocol + field
  + 3 call sites
- src/forge/cli/runtime.py — production notifier + default wiring
- tests/integration/test_jnb102_cancelled_emits.py (NEW) — reject emit /
  max-wait emit / defer-timeout emit / negative (approve+override zero
  emits) / DDR-007 (callback raises → transition intact, WARNING, no
  propagation) — all through the JNB-101 production factory over
  InMemoryNats (wire-level payload assertions incl. correlation_id)
- tests/forge/test_cli_steering.py (extend) — notifier called exactly
  once per cancel branch with cancelled_by/reason; TERMINAL zero;
  notifier raising → outcome still returned + WARNING
- tests/cli (or forge) — unit test for _SqliteRowCancelledNotifier
  (payload/subject correctness from a row; missing row skip; publish
  failure propagates to the handler's catch)

## Estimates
~40 LOC wrappers + ~15 gating + ~45 cli_steering + ~70 runtime; ~420 test
LOC. Files: 4 src + 3 test.
