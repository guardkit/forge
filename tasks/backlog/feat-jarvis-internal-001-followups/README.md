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
