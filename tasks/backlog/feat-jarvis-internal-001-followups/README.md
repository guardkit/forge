# FEAT-JARVIS-INTERNAL-001 First-Real-Run Follow-ups

Forge-side follow-up tasks surfaced by the **first real walkthrough**
of jarvis's FEAT-JARVIS-INTERNAL-001 runbook on 2026-05-01 on GB10
(`promaxgb10-41b1`).

## Source

All three tasks in this folder originate from a single end-to-end
walkthrough whose findings are captured in:

- **RESULTS file** (jarvis repo):
  [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run.md`](../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run.md)
- **Run correlation_id**: `a58ec9a7-27c6-485a-beac-e18675639a10`
- **Date**: 2026-05-01
- **Machine**: GB10 (`promaxgb10-41b1`)

The RESULTS file's `## Recommended follow-up tasks` section enumerates
8 follow-ups across forge, jarvis, and the runbook itself; this
folder holds the **forge-side** subset (items #1, #2, #3 in that list).
The jarvis-side (#4, #5, #6) and runbook-side (#7, #8) follow-ups are
tracked in the jarvis repo separately.

## Tasks in this folder

| Task | Title | Priority | Complexity | Status |
|---|---|---|---|---|
| [TASK-FORGE-FRR-001](../../completed/TASK-FORGE-FRR-001/TASK-FORGE-FRR-001-wire-dispatch-payload-to-real-orchestrator.md) | Wire `forge serve`'s `dispatch_payload` to the real autobuild orchestrator + stage-complete publish path | high | 6 | ⚠ superseded-by-feature (2026-05-02) |
| [TASK-FORGE-FRR-001b](../../completed/TASK-FORGE-FRR-001b/TASK-FORGE-FRR-001b-publish-pipeline-lifecycle-from-autobuild-orchestrator.md) | Publish pipeline lifecycle events (build-started, stage-complete×N, build-complete) from the autobuild orchestrator | high | 7 | ⚠ superseded-by-feature (2026-05-02) |
| [TASK-FORGE-FRR-002](../../completed/TASK-FORGE-FRR-002/TASK-FORGE-FRR-002-wire-logging-basicconfig-for-forge-log-level.md) | Wire `logging.basicConfig` in `forge serve` so `FORGE_LOG_LEVEL` actually produces visible logs | high | 2 | ✅ completed (b1da833, 2026-05-01) |
| [TASK-FORGE-FRR-003](../../completed/TASK-FORGE-FRR-003/TASK-FORGE-FRR-003-fix-build-image-script-context-path.md) | Fix `scripts/build-image.sh` so `--build-context nats-core=../nats-core` resolves on the canonical sibling layout | high | 2 | ✅ completed (fc7fd9a, 2026-05-01) |
| [TASK-REV-F010](TASK-REV-F010-bind-production-dispatch-chain-in-serve-cmd.md) | Decide how to bind `compose_dispatch_chain` to the production composer in `serve_cmd` (post-FEAT-DEA8 gap) | high | 5 | ✅ review_complete (2026-05-04 — see [.claude/reviews/TASK-REV-F010-review-report.md](../../../.claude/reviews/TASK-REV-F010-review-report.md)) |
| [TASK-FIX-F010](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md) | Bind `compose_dispatch_chain` to the production composer in `serve_cmd` via `_serve_production` wrapper | high | 4 | ✅ completed (2026-05-04 — post-merge follow-ups: runbook revalidation, FW10-011 housekeeping) |
| [TASK-FORGE-FRR-F010A](../../completed/TASK-FORGE-FRR-F010A/TASK-FORGE-FRR-F010A-apply-sqlite-migrations-on-daemon-boot.md) | Apply SQLite migrations on daemon boot in `bind_production_serve` | high | 2 | ✅ completed (2026-05-04 — code + tests; AC-6 operator runbook revalidation pending) |
| [TASK-FORGE-FRR-F010B](TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md) | Resolve `get_approved_stage_entry` AttributeError in autobuild dispatch path | high | 4 | 📥 backlog (filed 2026-05-04) |
| [TASK-FORGE-FRR-F010C](TASK-FORGE-FRR-F010C-thread-correlation-id-into-outbound-envelopes.md) | Thread inbound `correlation_id` into outbound `pipeline.*` envelopes from `pipeline_consumer` | high | 3 | 📥 backlog (filed 2026-05-04) |
| [TASK-FORGE-FRR-F010E](../../completed/TASK-FORGE-FRR-F010E/TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md) | Resolve `'StructuredTool' object has no attribute 'start_async_task'` in autobuild dispatch path | high | 4 | ✅ completed (2026-05-04 — adapter wraps StructuredTool at composition seam; AC-6 operator runbook revalidation pending) |
| [TASK-FORGE-FRR-F010F](TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md) | Publish terminal `build-failed` envelope when `dispatch_build` raises (instead of silently acking) | high | 3 | 📥 backlog (filed 2026-05-04) |
| [TASK-FORGE-FRR-F010G](../../completed/TASK-FORGE-FRR-F010G/TASK-FORGE-FRR-F010G-configure-autobuild-runner-url-or-fallback-transport.md) | Configure `autobuild_runner` async subagent for ASGI transport (or fall back to in-process invocation when `url=None`) | high | 4 | ✅ completed (2026-05-04 — Option C: chain-async, adapter awaits `tool.coroutine`; AC-5 operator runbook revalidation pending) |
| [TASK-FORGE-FRR-F010H](../../completed/TASK-FORGE-FRR-F010H/TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md) | Thread compiled `autobuild_runner` graph into `AsyncSubAgent` registration so in-process ASGI transport has a callable to invoke | high | 3 | ✅ completed as investigation deliverable (2026-05-04 — Option A hypothesis falsified; AC-1/AC-2/AC-6/AC-7 satisfied; AC-3/AC-4/AC-5 deferred to TASK-FORGE-FRR-F010I) |
| [TASK-FORGE-FRR-F010I](TASK-FORGE-FRR-F010I-decide-langgraph-deployment-shape-for-autobuild-runner.md) | Decide LangGraph deployment shape for `autobuild_runner` (B.1 sidecar URL / B.2 hand-rolled in-process ASGI / B.3 add `langgraph_api` dep) | high | 5 | ✅ review_complete (2026-05-04 — chose **B.1 sidecar**; report at [.claude/reviews/TASK-FORGE-FRR-F010I-review-report.md](../../../.claude/reviews/TASK-FORGE-FRR-F010I-review-report.md); implementation companion: F010J) |
| [TASK-FORGE-FRR-F010J](../../completed/TASK-FORGE-FRR-F010J/TASK-FORGE-FRR-F010J-wire-langgraph-runner-sidecar-url-into-async-subagent-registration.md) | Wire langgraph-runner sidecar URL into `AsyncSubAgent` registration and `bind_production_serve` (closes F010H deferred AC-3/4/5) | high | 4 | ✅ completed (2026-05-04 — AC-1/2/3/6/7 closed; AC-4 deferred — `langgraph-cli` not in dev venv; AC-5 + AC-8-cross-repo deferred to operator handoff per §Implementation Notes) |

> **Post-TASK-FIX-F010 follow-ups (2026-05-04 evening)**: TASK-FIX-F010
> shipped and was verified live on the wire (correlation_id
> `f876fd47-5e3c-4851-8f89-a7b7bcab8464`) — the production composer is
> bound, the receipt-only `_default_dispatch` stub is gone, and at
> least one outbound `pipeline.build-failed` envelope did flow back
> from `pipeline_consumer`. But three new forge-side gaps surfaced
> once the wired composer was actually exercised end-to-end:
> **F010.A** (`bind_production_serve` doesn't apply SQLite migrations
> on a fresh `FORGE_DB_PATH`), **F010.B** (`get_approved_stage_entry`
> AttributeError in the autobuild dispatcher path — wiring drift
> between FW10-005's fake persistence and the real
> `SqliteLifecyclePersistence`), and **F010.C** (outbound
> `pipeline.*` envelopes carry `correlation_id: null` instead of the
> threaded inbound value, violating DDR-029). One companion gap
> (**F010.D** — jarvis-side `forge_subscriber` subscribes only to
> `pipeline.stage-complete.>`) is jarvis-side and tracked separately
> in the jarvis repo's FRR folder. See
> [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
> "Addendum: Same-day post-TASK-FIX-F010 rerun" section for the full
> evidence (4 successive correlation_ids exercising progressively
> deeper rejection / dispatch paths).

> **Post-F010.A/B/C/D joint validation follow-ups (2026-05-04 late afternoon)**:
> Once F010.A (migrations on boot), F010.B (StageLogReader adapter),
> F010.C (correlation_id threading on rejection publishes), and
> F010.D-forge (PREPARING-recovery threading) all landed, a joint
> live-wire validation rerun (correlation_id
> `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`) verified all four fixes on
> the wire — with one regression on the jarvis side that's tracked
> separately as TASK-FRR-F010Db in the jarvis repo (Option A widening
> to `pipeline.>` causes a workqueue-overlap rejection on the
> PIPELINE stream; the fix is to switch to Option B — explicit
> four-subject filter). On the forge side, F010.B's StageLogReader
> fix unblocked the next layer of wiring drift: **F010.E**
> (`'StructuredTool' object has no attribute 'start_async_task'` —
> the autobuild dispatcher's `AsyncTaskStarter` Protocol expects a
> named-method shape, but the production wrapper resolves a raw
> LangChain `StructuredTool` from `middleware.tools` that exposes
> `invoke()` / `ainvoke()` instead) is the next dispatch-time
> blocker. **F010.F** is the safety-net companion: even when
> `dispatch_build` raises, the consumer should publish a terminal
> `build-failed` envelope so jarvis's chat REPL can render the
> failure (today the consumer logs WARNING, acks, and silently drops
> the chat thread — the same shape both F010.B and F010.E exposed
> empirically). See
> [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
> "Addendum 2: Joint live-wire validation rerun after F010.A–D"
> section for the full evidence (chat-driven queue + synthetic
> publish for the F010.C verification).

> **Phase 7 structural close achieved — last-mile gap (2026-05-04 late evening)**:
> Once F010Db (jarvis) + F010E + F010F (forge) all landed, a final
> validation rerun (correlation_id
> `db27f127-a863-4723-a4be-b8cbb68eab5a`, forge HEAD `50f646f`, jarvis
> HEAD `85f2e39`) verified all three fixes live on the wire — the
> **chat REPL now renders lifecycle notification lines between
> prompts** in the canonical runbook §7.1 shape (`[14:38] Forge
> FEAT-43DE: build-failed (RuntimeError: ...)`), threaded by the same
> correlation_id jarvis published, drained before the next supervisor
> response. Phase 7 **structural close achieved** — the DDR-030
> contract is empirically satisfied (the operator never silently
> loses the build outcome). The remaining gap (**F010.G** — this
> task) is the last layer between structural close and the canonical
> happy-path sequence: the `autobuild_runner` async subagent has
> `url=None` and `deepagents.middleware.async_subagents`'s sync
> ASGI-transport client cache fails fast on launch. F010F's
> safety-net publishes a terminal `build-failed` envelope carrying
> the deepagents `failure_reason` verbatim, so this gap is loud and
> well-routed. Once F010.G closes, the runbook should produce the
> full `build-started + stage-complete*N + build-complete` envelope
> sequence on the wire and as rendered chat lines. See
> [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
> "Addendum 3: Final validation rerun after F010Db + F010E + F010F"
> section for the full evidence chain.

> **Post-F010G rerun follow-up (2026-05-04 evening — Addendum 4)**:
> F010G shipped (`8d08b93`) and was verified live on the wire
> (correlation_id `bf697f49-3114-4c90-ae62-63936b8c53bf`,
> `forge:latest` sha256 `8ce899e7d03ab...`) — the URL=None ASGI guard
> in `_ClientCache.get_sync()` is empirically bypassed: the
> error-message change from `'has no url configured. ASGI transport
> (url=None) requires async invocation.'` (Addendum 3, F010G)
> → `''NoneType' object is not callable'` (Addendum 4, F010H) is the
> proof the call now routes through `_ClientCache.get_async()`. But
> a deeper layer surfaced — `'NoneType' object is not callable` raises
> inside the LangGraph SDK's in-process ASGI transport chain because
> the `autobuild_runner` `AsyncSubAgent` registration in
> `forge.cli.serve._build_async_subagent_middleware` likely does not
> thread the compiled subagent graph through to
> `langgraph_sdk.get_client(url=None, app=...)`. Tracked as
> **F010.H** — most likely a one-line registration change once the
> investigation step confirms the hypothesis. F010F's safety-net
> publish keeps firing (Phase 7 structural close maintained); F010H
> is about getting the actual autobuild to *run*, not about operator
> visibility. See
> [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
> "Addendum 4: Post-F010G rerun" for the full evidence chain
> (side-by-side error-message comparison, `'NoneType' object is not
> callable` log line, build-failed envelope carrying the threaded
> `bf697f49-…` correlation_id, in-process repro recipe).

> **Post-FEAT-DEA8 follow-up (2026-05-04)**: The 2026-05-04 rerun of the
> jarvis first-real-run runbook (correlation_id
> `18036705-2bb7-4564-8363-315bf7716a48`) — executed once all four
> jarvis-side FRR follow-ups (TASK-FRR-001..004) and FEAT-FORGE-010
> (FEAT-DEA8) had merged — surfaced one remaining gap on the forge
> side: even with FEAT-FORGE-010's `bind_production_dispatch_chain`
> factory in place, `serve_cmd` does not actually call it, so the
> daemon falls through to the receipt-only `_default_dispatch` stub
> at `_serve_daemon.py:166`. **TASK-REV-F010** is a `task_type:review`
> decision-mode review to choose the wiring shape; once it lands its
> implementation companion will close the production-binding loop.
> See
> [`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
> for the full rerun evidence.

> **Supersession note (2026-05-02)**: FRR-001 + FRR-001b were both
> closed as `superseded-by-feature` after the FRR-001 Phase 3
> investigation discovered that the entire pipeline orchestration
> chain (`Supervisor`, `PipelineConsumerDeps`,
> `PipelineLifecycleEmitter`, `ForwardContextBuilder`, the
> `autobuild_runner` AsyncSubAgent, plus four Protocol
> implementations) is unwired in production. F009 deferred more than
> "wire `dispatch_payload`" — it deferred the entire orchestration
> tail. The remaining work has been re-scoped to **FEAT-FORGE-010**
> (slug `forge-serve-orchestrator-wiring` — see
> [`tasks/backlog/forge-serve-orchestrator-wiring/`](../forge-serve-orchestrator-wiring/README.md)).
> The feature was filed against the findings document
> `docs/research/forge-orchestrator-wiring-gap.md` and the
> `--context` evaluation
> `docs/research/forge-orchestrator-wiring-feature-context.md`; both
> remain valid as pre-feature reference material. The
> `superseded_by` frontmatter on both FRR-001 and FRR-001b has been
> updated to point at FEAT-FORGE-010.

## Sequence (current state)

1. ~~**TASK-FORGE-FRR-003**~~ ✅ **shipped** (`fc7fd9a`, 2026-05-01) — `scripts/build-image.sh` now `cd`s into forge's parent directory before invoking buildx, so `--build-context nats-core=../nats-core` resolves correctly on the canonical sibling layout.
2. ~~**TASK-FORGE-FRR-002**~~ ✅ **shipped** (`b1da833`, 2026-05-01) — `serve_cmd` now calls `logging.basicConfig(level=config.log_level, ...)` immediately after `ServeConfig.from_env()`. `docker logs forge-prod` now actually shows the `_serve_daemon` and `_serve_healthz` log lines that were silently dropped before.
3. ~~**TASK-FORGE-FRR-001**~~ + ~~**TASK-FORGE-FRR-001b**~~ ⚠ **superseded** (2026-05-02) — see supersession note above. Subsumed by **FEAT-FORGE-010** (`forge-serve-orchestrator-wiring`). The runbook's Phase 7 close criterion ("real per-stage notifications render in the chat REPL") will be satisfied when FEAT-FORGE-010 ships.
4. ~~**FEAT-FORGE-010**~~ — code merged 2026-05-02 (`9a93808 Merge FEAT-DEA8: wire pipeline orchestrator into forge serve` + `9ef9138` finalize). All Wave 1-3 tasks completed; capstone TASK-FW10-011 is `design_approved` (not implemented). The **factory** for the production dispatch chain (`bind_production_dispatch_chain`) is shipped, but the **production binding** that calls it from `serve_cmd` is not — see TASK-REV-F010 below.
5. ~~**TASK-REV-F010**~~ ✅ **review_complete** (2026-05-04) — decision-mode review chose wiring shape (5 decisions: D1.B wrapper module / D2.A* reuse `FORGE_DB_PATH` / D3.A eager middleware / D4.B fix first FW10-011 second / D5.A testable helper via wrapper). Report at [.claude/reviews/TASK-REV-F010-review-report.md](../../../.claude/reviews/TASK-REV-F010-review-report.md).
6. ~~**TASK-FIX-F010**~~ ✅ **completed** (2026-05-04) — built `forge.cli._serve_production._serve_production` wrapper, extended `ServeConfig` with `db_path` (reuses `FORGE_DB_PATH`), decorated `serve_cmd` with `@click.pass_context`, and rebound `compose_dispatch_chain` at boot. 100/100 targeted tests pass; 2131/2132 in full forge suite (one pre-existing unrelated clock-hygiene failure). Code in working tree pending operator commit. See [tasks/completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md) for full report.
7. **Post-merge follow-up (deferred from TASK-FIX-F010 ACs 10/11/12)**:
   - AC-10: cross-link `tasks/completed/TASK-FW10-011-...md` frontmatter (`parent_review: TASK-REV-F010`, `production_binding_sibling: TASK-FIX-F010`).
   - AC-11: rebuild forge image from the new commit; re-run jarvis runbook §6.2+§7; verify `pipeline.build-started.*` envelope appears; flip RESULTS row 7.x ❌→✅; record new correlation_id in the completed task file. **Status:** partially satisfied — TASK-FIX-F010 verified live (composer bound, receipt-only stub gone, one outbound `build-failed` observed) but full lifecycle sequence still blocked by F010.A/B/C below.
   - AC-12: resurrect TASK-FW10-011 from `tasks/completed/` to this folder with `dependencies: [TASK-FIX-F010]`, status `in_progress`; land it as the codified regression lock (per D4.B sequencing). **Should now also depend on F010.B** since FW10-011's success-path assertions cannot pass until the autobuild dispatcher's AttributeError is resolved.
8. ~~**TASK-FORGE-FRR-F010A**~~ ✅ **completed** (2026-05-04 — code + tests landed, AC-6 operator runbook revalidation pending) — `bind_production_serve` now calls `apply_at_boot(connection)` after `connect_writer(...)` (Step 3.5 in the docstring pipeline). Boot log emits `[INFO] forge-serve: applied N SQLite migration(s) at boot`; idempotent re-bind logs `applied 0`. New tests in `tests/forge/test_serve_production_migrations.py` (3 passing); existing `test_cli_serve_production.py` (12 passing, AC-5 regression) and 72 sibling cli/serve tests still green. AC-2 narrowed to the 4 migration-managed tables (`builds` / `stage_log` / `sqlite_sequence` / `schema_version`); `async_tasks` is provisioned by `ensure_async_tasks_schema` at dispatcher-construction time (Step 7), out of `apply_at_boot`'s scope — see the completed task file for the full scope note. Reproducer that surfaced this: run 3 (`a55df422-…`) — `no such table: builds`. Code in working tree pending operator commit; jarvis runbook §6.2+§7 revalidation pending operator. See [tasks/completed/TASK-FORGE-FRR-F010A/](../../completed/TASK-FORGE-FRR-F010A/TASK-FORGE-FRR-F010A-apply-sqlite-migrations-on-daemon-boot.md) for full report.
9. **TASK-FORGE-FRR-F010B** 📥 backlog (filed 2026-05-04) — Resolve `'SqliteLifecyclePersistence' object has no attribute 'get_approved_stage_entry'` in the autobuild dispatcher path. Wiring drift between FW10-005's fake persistence and the real facade — fix is either method-add on the facade or caller-rename. Reproducer: run 4 (`f876fd47-…`) — exception fires after the QUEUED row is persisted but before any `build-started` envelope is emitted.
10. **TASK-FORGE-FRR-F010C** 📥 backlog (filed 2026-05-04) — Thread `inbound_envelope.correlation_id` into every outbound `pipeline.*` envelope from `pipeline_consumer` (and any caller in `pipeline.publisher` / `pipeline.lifecycle_emitter`). DDR-029 violation. Independent of F010.A/B; can land in parallel. Reproducer: runs 1+2 (`21df1258-…`, `b5c5e1e2-…`) — `pipeline.build-failed.FEAT-43DE` carried `correlation_id: null` instead of the threaded inbound value.
11. **TASK-FORGE-FRR-F010E** 📥 backlog (filed 2026-05-04) — Resolve `'StructuredTool' object has no attribute 'start_async_task'` AttributeError in the autobuild dispatcher path (next-layer wiring drift exposed once F010.B's StageLogReader adapter unblocked the dispatcher's progression). The autobuild dispatcher's `AsyncTaskStarter` Protocol at `src/forge/pipeline/dispatchers/autobuild_async.py:155-189` declares a named-method shape (`start_async_task(subagent_name, context)`), but `_resolve_async_task_starter` at `src/forge/cli/_serve_production.py:139-142` returns the raw LangChain `StructuredTool` looked up by name from `middleware.tools` — `StructuredTool` exposes `tool.invoke({...})` / `tool.ainvoke({...})`, not the named method. Fix is either (A) change the caller to use `tool.invoke({...})` (LangChain-native) or (B) wrap the `StructuredTool` in a named-method adapter at the production-composition seam (symmetric with F010.B's adapter-wrapping strategy — likely the right answer). Reproducer: late-afternoon run 1 (`dfad8e7f-…`) — exception fires immediately after the QUEUED row is persisted but before any `build-started` envelope is emitted. Independent of F010.F; can land in either order.
12. **TASK-FORGE-FRR-F010F** 📥 backlog (filed 2026-05-04) — Publish terminal `pipeline.build-failed.<feature_id>` envelope when `dispatch_build` raises an unhandled exception, **before** acking — instead of today's silent log+ack at `pipeline_consumer.py:470-506`. Re-uses F010.C's `_safe_publish_failure` / `_failure_payload` helpers so correlation_id-threading inherits transparently. Narrows ADR-ARCH-008's no-duplicate-publish protection to "when the state machine has started" (since pre-state-machine raises mean the state machine never gets to publish, and so there's no duplicate to guard against). Safety-net for *all* future dispatch failures, not just F010.E's StructuredTool case. Independent of F010.E; can land in either order. Recommended order is **F010.F first** — its AC-6 is verifiable today against the open F010.E failure mode (a chat-driven queue produces a visible `build-failed` envelope on the wire instead of a silent drop), and the implementation is small (one publish call + 3 unit tests). Reproducer (co-symptom): same late-afternoon run 1 (`dfad8e7f-…`) — `pipeline.>` tail captured zero outbound publishes from forge despite a known correlation_id and a logged WARNING.
13. ~~**TASK-FORGE-FRR-F010G**~~ ✅ **completed** (2026-05-04 — `8d08b93`) — Option C chosen and shipped: `_StructuredToolAsyncTaskStarter` grew an `astart_async_task` async method that awaits `self._tool.coroutine(...)`, the dispatcher chain went async (`dispatch_autobuild_async` is now `async def`, the supervisor branch awaits the closure), and the URL=None ASGI rejection is empirically bypassed (correlation_id `bf697f49-3114-4c90-ae62-63936b8c53bf`, error-message change from `'has no url configured. ASGI transport (url=None) requires async invocation.'` → `''NoneType' object is not callable'` — the proof the `get_async()` codepath is reached). AC-5 operator runbook revalidation will satisfy alongside F010H. See [tasks/completed/TASK-FORGE-FRR-F010G/](../../completed/TASK-FORGE-FRR-F010G/TASK-FORGE-FRR-F010G-configure-autobuild-runner-url-or-fallback-transport.md) for full report.
14. ~~**TASK-FORGE-FRR-F010H**~~ ✅ **completed as investigation deliverable** (2026-05-04 evening — `tasks/completed/TASK-FORGE-FRR-F010H/`) — F010H's mandatory investigation falsified the "thread compiled graph through `AsyncSubAgent` registration" hypothesis. Findings: (1) `deepagents.middleware.async_subagents.AsyncSubAgent` TypedDict (deepagents 0.5.3) has only five fields — `name`, `description`, `graph_id`, `url` (NotRequired), `headers` (NotRequired) — **no `graph`/`app`/`runnable`/`compiled_graph` field**; (2) `langgraph_sdk.get_client` (langgraph-sdk 0.3.13) accepts only `url`, `api_key`, `headers`, `timeout` — **no `app=` kwarg**; (3) `langgraph_api` package is **NOT installed** in the forge venv, so `get_client(url=None)`'s first branch (`from langgraph_api.server import app`) raises `ModuleNotFoundError` and falls through to creating an `ASGITransport(app=None, root_path="/noauth")` which raises `'NoneType' object is not callable` on the first request. The hypothesised one-line fix is impossible. Per the F010H task body's own decision tree ("Option A is the expected path... defer to a separate task if so" for Option B), implementation is escalated to a follow-up review task **TASK-FORGE-FRR-F010I**. AC-1 (investigation) and AC-2 (decision) closed in F010H; AC-3/AC-4 deferred to F010I's implementation companion. Full forge suite still passes (2182/2183, same pre-existing `test_clock_hygiene` exclusion F010G's AC-7 carried). See [`tasks/in_review/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010H-...md`](../../in_review/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md) for the full investigation report.
15. ~~**TASK-FORGE-FRR-F010I**~~ ✅ **review_complete** (2026-05-04 evening) — `task_type:review` decision-mode task chose **Option B.1** (sidecar `langgraph dev`) over B.2 (hand-rolled in-process ASGI app — ruled out on maintenance-burden cliff: re-implementing the LangGraph SDK threads/runs/assistants protocol) and B.3 (add `langgraph_api` as a forge dependency — ruled out on the `langgraph-api` package's own maintainer-stated contraindication: "rapid development and testing… for production use, see the various deployment options"; plus Elastic-2.0 license, 30-package transitive tree, and a duplicate `langgraph-runtime-inmem` persistence store inside the forge daemon process). Highest-weight reason: B.3 would embed a maintainer-disclaimed dev/test artifact as forge runtime. B.1 is the deployment shape deepagents and langgraph-sdk were designed for. Cost: one extra container in the operator runbook + ~30-line supervisor-side reconciliation pass for daemon-restart-during-build crash recovery (deferred to optional sibling F010K). See [.claude/reviews/TASK-FORGE-FRR-F010I-review-report.md](../../../.claude/reviews/TASK-FORGE-FRR-F010I-review-report.md) for the full option-evaluation matrix and per-cell justifications. Implementation companion **TASK-FORGE-FRR-F010J** filed alongside.
16. ~~**TASK-FORGE-FRR-F010J**~~ ✅ **completed** (2026-05-04 evening — code + tests landed; AC-5 operator runbook revalidation pending operator handoff) — Wired `FORGE_AUTOBUILD_RUNNER_URL` env var through `ServeConfig` → `bind_production_serve` → `_build_async_subagent_middleware` so the `AsyncSubAgent` registration's `url` field points at a `langgraph dev` sidecar serving forge's `autobuild_runner` graph. Step 1.5 fail-fast guard in `bind_production_serve` raises `ValueError` at boot if the URL is unset (verified via `_does_not_open_sqlite_writer` test that the guard fires before `connect_writer`, so a missing URL never leaves an orphan SQLite handle). 73/73 targeted tests pass; F010F safety-net regression 4/4 still green; full forge + tests/ suite 4287/4289 (2 pre-existing failures verified unchanged on unmodified `main` via stash-pop comparison: `test_clock_hygiene` per F010A/G/H AC-7 exclusion, plus `test_forge_serve_arfs_inside_image` image-CLI-mismatch unrelated to F010J wiring). **In-scope ACs closed**: AC-1 (config field), AC-2 (middleware threading — both URL-present and URL-omitted cases), AC-3 (fail-fast at boot), AC-6 (F010F regression), AC-7 (full suite). **Deferred**: AC-4 (loopback-dispatch integration test — `langgraph-cli` not in dev venv; not added unilaterally), AC-5 (operator runbook revalidation — needs GB10 deploy with sidecar wired), AC-8-cross-repo (jarvis-runbook prose deltas — sibling repo). **Operator handoff** in F010J §Implementation Notes covers: (1) sidecar invocation (langgraph dev / sidecar container), (2) forge daemon `docker run` command shape with `FORGE_AUTOBUILD_RUNNER_URL`, (3) boot log lines confirming F010J live + the actionable error if env var unset, (4) chat REPL line shape for canonical Phase 7 happy-path close. Phase 7 happy-path close pending operator runbook rerun. See [tasks/completed/TASK-FORGE-FRR-F010J/](../../completed/TASK-FORGE-FRR-F010J/TASK-FORGE-FRR-F010J-wire-langgraph-runner-sidecar-url-into-async-subagent-registration.md) for the full report.

The jarvis runbook
(`/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md`)
has been updated alongside this supersession to test for the real
per-stage envelope sequence FEAT-FORGE-010 will produce, not the
synthetic single-envelope output FRR-001 was originally going to
ship.

## Naming

`FRR` = "first real run" — the prefix used to disambiguate this small
batch of follow-ups from the prior FEAT-FORGE-009 (F009) follow-ups
(`TASK-FIX-F09A1`, `TASK-FIX-F09A2`) and the F0E6 fix series
(`TASK-FIX-F0E6`, `TASK-FIX-F0E6b`). All three tasks share `FORGE-FRR`
because they are all forge-side work originating from the jarvis
runbook's first real run.
