---
id: TASK-FORGE-FRR-PEBR-WIREUP
title: Fix Gap PEBR-WIREUP — compose LifecycleBridgeWireup in bind_production_serve (Option B helper-factory shape)
status: in_review
created: 2026-05-08T08:30:00Z
updated: 2026-05-08T10:30:00Z
intensity: light
intensity_reason: provenance=parent_review (TASK-REV-PEBR-003), complexity=5, no high-risk keywords
langgraph_sdk_verified:
  version: 0.3.13
  get_client_pattern: "from langgraph_sdk import get_client; client = get_client(url=runner_url)"
  runs_list_signature: "list(thread_id: str, *, limit: int = 10, ...) -> list[Run]"
  runs_join_stream_signature: "join_stream(thread_id: str, run_id: str, *, stream_mode=..., ...) -> AsyncIterator[StreamPart]"
test_results:
  status: passed
  total: 2481
  passed: 2481
  failed: 0
  pre_existing_failures_outside_scope: 1  # TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist (approval_subscriber.py:684) — verified pre-existing on HEAD e50241e
  targeted_seam_tests: "2/2 passed (TestLifecycleBridgeWireupComposition)"
  helper_unit_tests: "12/12 passed (5 stream_source + 7 identity_provider)"
  test_cli_serve_production_full: "19/19 passed (was 17 pre-fix; +2 new seam tests)"
  lifecycle_bridge_dir_full: "220/220 passed"
  last_run: 2026-05-08T10:25:00Z
ac_status:
  AC-1: done    # _build_lifecycle_bridge_wireup_parts(...) helper + Step 6.5 invocation
  AC-2: done    # langgraph_stream_source(runner_url=...) shipped
  AC-3: done    # _build_async_tasks_identity_provider(...) shipped (hybrid SQLite + langgraph_sdk)
  AC-4: done    # coexistence.apply_migration(connection) called in Step 3.5b
  AC-5: done    # bind_production_dispatch_chain signature extended additively (bridge_wireup_parts kwarg)
  AC-6: done    # build_pipeline_consumer_deps accepts optional publisher kwarg
  AC-7: done    # forge.lifecycle_bridge.__init__ re-exports added
  AC-8: done    # TestLifecycleBridgeWireupComposition class with 2 seam tests
  AC-9: done    # existing tests pass (test_cli_serve_production fixture upgrade was required and is documented)
  AC-10: done   # ruff check + black --check both clean on touched files
  AC-11: partially-unblocked  # 2026-05-08T14:16Z — FOLLOWUP-A live in production (commit 55f7804) and runbook re-run confirmed migration applies cleanly (0 no-such-table warnings across 12 dispatches). Remaining gate: no pipeline.build-started.FEAT-* envelope captured on the wire (FOLLOWUP-B is the active translator-shape mismatch gap).
ac_11_blocked_on:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B   # 2026-05-08T14:16Z runbook narrowed surface to Path 2 (translator-shape mismatch on deepagents event='values' parts; parts_received=30, event_types={'values'} during cycle 1). Path 1 (SSE unreachability / placeholder thread_id rebind) eliminated — autobuild_runner IS streaming state updates. NOTE: per ac_11_promotion_gate, FOLLOWUP-B is NOT hard-blocking for promotion; the runbook envelope-on-wire gate is.
ac_11_resolved:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A   # 2026-05-08T12:30Z code via /task-work → /task-complete (light intensity); committed 2026-05-08T~12:50Z as 55f7804; AC-5 (operator handoff) satisfied 2026-05-08T14:16Z via outcome-(b) clause when post-rebuild runbook re-run confirmed the migration applies cleanly with no fallback to legacy ack_callback.
ac_11_runbook_revalidation_doc: docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-fresh-followup-b-instrumented.md
ac_11_runbook_revalidation_outcome:
  ran_at: 2026-05-08T14:16:00Z
  correlation_id: 1506e6c4-cc6a-4591-8dc0-d9258b231b11
  forge_image_head: 1b82236+55f7804  # parent fix HEAD plus FOLLOWUP-A commit; rebuilt and redeployed before this run
  followup_a_validation: passed       # 0 no-such-table warnings across 12 dispatches
  consumer_state:
    delivered: 12
    ack_floor: 0       # canonical AC-11 fail fingerprint — no envelope on wire → consumer never advances
    redelivered: 1
  outbound_envelopes_observed: 0
  followup_b_surface_narrowed_to: |
    Path 2 (translator-shape mismatch) confirmed active. Path 1
    (SSE unreachability / placeholder thread_id rebind) eliminated
    by parts_received=30, event_types={'values'} on cycle 1: the
    autobuild_runner IS streaming state updates; the bridge
    translator does not recognise deepagents' event='values' parts
    as stage transitions.
  side_observation: |
    Deadline path is gated on stream unreachability, not silence
    — 5-min observer deadline passed without build-failed envelope
    emit. Worth filing as a separate concern if silence-triggered
    timeouts were expected.
ac_11_promotion_gate: |
  This task remains in tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/ (NOT promoted to tasks/completed/) until at minimum FOLLOWUP-A lands and Phase 7 of the runbook captures a real pipeline.build-started.FEAT-* envelope on the wire. FOLLOWUP-B may land post-hoc if its spike escalates to a wider scope, but FOLLOWUP-A is hard-blocking. FOLLOWUP-C is independent of this gate.

  Status 2026-05-08T14:16Z: FOLLOWUP-A complete and validated live in production (zero migration-drift warnings, bridge attaches cleanly). Runbook re-run did NOT capture a pipeline.build-started.FEAT-* envelope on the wire (the canonical envelope-on-wire gate). FOLLOWUP-B (translator-shape mismatch on deepagents event='values' parts) is the active gap. Promotion to completed/ remains blocked on the runbook capturing the build-started envelope; this happens after FOLLOWUP-B lands and the runbook is re-run a third time.
code_review:
  verdict: APPROVED
  critical_findings: 0
  recommendations: 2  # both cosmetic / non-blocking (annotation polish on stream_source._source; lazy import positioning in _serve_deps.py)
  recommendations_applied: 2  # 2026-05-08T10:55Z — applied both. _serve_deps.py: lifted PipelineConfig + PipelineLifecycleEmitter to module level. stream_source.py: added comment clarifying the sync-def-returning-async-iterator-value pattern. 309/309 targeted tests still pass; ruff + black clean; no circular import.
plan_audit:
  verdict: PASSED
  severity: low
  loc_variance: "+44%"  # within LIGHT-intensity 50% tolerance
  scope_creep: 0
  missing_files: 0
  extra_files: 0
  extra_dependencies: 0
  notes: |
    - Manual audit (no docs/state/{task_id}/implementation_plan.json was generated under LIGHT intensity — the implementation-task spec served as the plan).
    - Test LoC ran higher than estimated because the IdentityProvider gained a 7th test covering dict-shaped SDK return values (defensive against the langgraph-sdk runtime returning dicts vs. dataclass-shaped Run objects), and the existing test_cli_serve_production.py fixtures required upgrading to real sqlite3.connect(":memory:") so the new BridgeRegistry isinstance(sqlite3.Connection) check passes. Both adjustments are within scope and documented.
priority: high
task_type: fix
parent_review: TASK-REV-PEBR-003
parent_feature: FEAT-PEBR
related_tasks:
  - TASK-FIX-F010                # one-layer-shallower precedent (serve_cmd → bind_production_dispatch_chain rebind)
  - TASK-FRR-PEB-002             # bridge skeleton + LifecycleBridgeWireup class (the unwired type)
  - TASK-FRR-PEB-003             # SSE→envelope translator the wireup consumes
  - TASK-FRR-PEB-005             # F010F coexistence boundary + TerminalPublishLedger
  - TASK-FRR-PEB-013             # sidecar-aware E2E (audited as bypassing bind_production_serve)
complexity: 5
estimated_minutes: 120
implementation_mode: task-work
wave: 1
tags:
  - forge-serve
  - lifecycle-bridge
  - production-binding
  - wire-up
  - regression-protection
  - feat-pebr
  - gap-pebr-wireup
  - first-real-run-followup
discovered_during: TASK-REV-PEBR-003 (jarvis runbook walkthrough on GB10, 2026-05-08)
discovered_correlation_id: 5673965b-e302-4a10-89cb-ceb430e64995
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Fix Gap PEBR-WIREUP — compose `LifecycleBridgeWireup` in `bind_production_serve`

## TL;DR

Close the production-wiring gap surfaced by TASK-REV-PEBR-003: `forge.cli._serve_production.bind_production_serve` does not compose `LifecycleBridgeWireup` into the running daemon. As a result the deps composer logs `ack_bridge=deferred (TASK-FRR-PEB-002), terminal_publish_ledger=deferred (TASK-FRR-PEB-005)` on every boot, no outbound `pipeline.*` lifecycle envelopes reach JetStream, and the inbound `pipeline.build-queued.*` envelope is redelivered every 30s without ever being acked.

This task implements the **Option B (helper factory)** fix shape chosen by TASK-REV-PEBR-003, plus the three fix-scope expansions identified in that review's AC-3 wiring map: ship `langgraph_stream_source(...)`, ship a production `IdentityProvider` factory, invoke `coexistence.apply_migration(connection)` at boot.

Same shape as TASK-FIX-F010 (one layer deeper). The implementation modules exist, the deps composer accepts the parameters, the production wrapper just doesn't invoke the composition.

## Why

See [TASK-REV-PEBR-003](TASK-REV-PEBR-003-analyse-bind-production-serve-wireup-gap.md) `## Findings → Diagnosis (AC-1)` for the full root-cause walkthrough. Operator-facing evidence on the rebuilt 2026-05-08 image:

```
2026-05-08T05:54:02 [INFO] forge.cli._serve_deps: build_pipeline_consumer_deps:
  composed PipelineConsumerDeps
  (async_task_starter=wired,
   ack_bridge=deferred (TASK-FRR-PEB-002),
   terminal_publish_ledger=deferred (TASK-FRR-PEB-005))
```

Co-symptom — JetStream consumer state on the same boot:

```json
{"delivered": 7277, "pending": 0, "redelivered": 2, "ack_floor": 11}
```

Until this gap closes, jarvis ↔ forge ↔ langgraph-runner cannot complete the canonical Phase 7 happy-path (`pipeline.build-started.* → pipeline.stage-complete.* → pipeline.build-complete.*`) of `RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md`. The autobuild runs end-to-end in the sidecar (`run_completed_in_ms=37179`), so this is purely a wire-up gap — no algorithmic / dispatch / model work needed.

## What

### 1. New helper — `_build_lifecycle_bridge_wireup_parts(...)` in `_serve_production.py`

Mirror the existing `_resolve_async_task_starter(middleware)` helper at `src/forge/cli/_serve_production.py:127`. Add a new module-level helper:

```python
@dataclass(frozen=True)
class LifecycleBridgeWireupParts:
    """SQLite-bound dependencies for LifecycleBridgeWireup.

    The wireup itself cannot be constructed here because it requires the
    PipelinePublisher which is only available inside the closure
    returned by bind_production_dispatch_chain (where the NATS client
    has been opened). This struct carries the parts that DO depend on
    the SQLite pool / runner_url so they can be threaded through
    bind_production_dispatch_chain into its closure.
    """
    bridge: "LifecycleBridge"
    translator: "StreamEventTranslator"
    stream_source: "StreamSource"
    identity_provider: "IdentityProvider"
    terminal_publish_ledger: "TerminalPublishLedger"


def _build_lifecycle_bridge_wireup_parts(
    *,
    sqlite_pool: SqliteLifecyclePersistence,
    autobuild_runner_url: str,
) -> LifecycleBridgeWireupParts:
    """Construct the SQLite-bound dependencies for LifecycleBridgeWireup.

    Pipeline:
      1. BridgeRegistry(connection=sqlite_pool._connection).
      2. LifecycleBridge(registry=registry, sidecar_url=autobuild_runner_url, sdk_client=…, deadline_handler=None_for_now).
      3. StreamEventTranslator() — zero-arg.
      4. langgraph_stream_source(runner_url=autobuild_runner_url) — see §2.
      5. _build_async_tasks_identity_provider(sqlite_pool) — see §3.
      6. TerminalPublishLedger(connection=sqlite_pool._connection).

    Returns a frozen LifecycleBridgeWireupParts dataclass.
    """
```

`bind_production_serve` adds **one new step** between Step 6 (resolve async_task_starter) and Step 7 (bind_production_dispatch_chain):

```python
    # Step 6.5 — construct the lifecycle-bridge wireup parts (Gap PEBR-WIREUP).
    wireup_parts = _build_lifecycle_bridge_wireup_parts(
        sqlite_pool=sqlite_pool,
        autobuild_runner_url=config.autobuild_runner_url,
    )
```

Then thread `wireup_parts` into `bind_production_dispatch_chain(...)` via a new optional kwarg.

### 2. New module/function — `forge.lifecycle_bridge.langgraph_stream_source`

The wireup's `StreamSource` Protocol expects `__call__(*, feature_id, thread_id, run_id) -> AsyncIterator[StreamPart]`. Production needs an adapter over `langgraph_sdk.client.LangGraphClient(url=runner_url).runs.join_stream(thread_id=…, run_id=…, stream_mode="values")`.

Add to (or create) `src/forge/lifecycle_bridge/stream_source.py` (new file):

```python
"""Production StreamSource — adapts langgraph_sdk.client.runs.join_stream.

Referenced by src/forge/lifecycle_bridge/wireup.py:52 docstring as
TASK-FRR-PEB-005, but PEB-005's actual scope shipped the
TerminalPublishLedger only — this factory was never authored.
TASK-FORGE-FRR-PEBR-WIREUP closes the gap.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from forge.lifecycle_bridge.wireup import StreamSource


def langgraph_stream_source(*, runner_url: str) -> StreamSource:
    """Return a StreamSource bound to a real langgraph-runner sidecar.

    The factory captures `runner_url` and returns a callable that opens
    a langgraph_sdk.client.LangGraphClient(url=runner_url) per call and
    yields from runs.join_stream(thread_id=…, run_id=…, stream_mode="values").

    StopAsyncIteration on a finished run is a clean iterator exit
    (matches the StreamSource Protocol's "yielding zero events is a
    legitimate no-live-SSE signal" contract). Transport errors raise
    out of the iterator and are caught by the wireup's reconnect loop
    (TRANSIENT_STREAM_ERRORS at wireup.py:126).
    """

    def _source(
        *,
        feature_id: str,
        thread_id: str | None,
        run_id: str | None,
    ) -> AsyncIterator[Any]:
        from langgraph_sdk.client import LangGraphClient
        if thread_id is None or run_id is None:
            return _empty_async_iterator()
        client = LangGraphClient(url=runner_url)
        return client.runs.join_stream(
            thread_id=thread_id,
            run_id=run_id,
            stream_mode="values",
        )

    return _source


async def _empty_async_iterator() -> AsyncIterator[Any]:
    """Yield zero events — matches StreamSource's legitimate no-live-SSE signal."""
    return
    yield  # unreachable, but makes this an async generator
```

(Final shape may differ depending on the langgraph_sdk client API surface — implementer to verify against the installed version. The Protocol contract at `wireup.py:211-233` is what the helper must satisfy.)

Add unit tests at `tests/forge/lifecycle_bridge/test_stream_source.py`: monkey-patch `LangGraphClient` to a recording fake; assert the factory threads `runner_url` / `thread_id` / `run_id` correctly.

### 3. New helper — production `IdentityProvider` (hybrid SQLite + langgraph_sdk)

`wireup.py:248-251` contracts `IdentityProvider = Callable[[str], Awaitable[tuple[str, str] | None]]`.

**Schema gap discovered during the TASK-REV-PEBR-003 boundary trace** (see that review's "Validated Boundary Trace" section): the `async_tasks` SQLite table (`_serve_deps_state_channel.py:284-308`) has columns `task_id, build_id, feature_id, correlation_id, lifecycle, wave_index, task_index, started_at, last_activity_at`. **There is no `run_id` column.** The dispatcher writes `task_id == thread_id` (per `_serve_async_task_starter.py:148-149`: *"Command whose update.async_tasks contains exactly one entry — the just-launched task keyed by its thread_id"*), but the run_id minted by the sidecar's `POST /threads/{thread_id}/runs` response is **discarded** by forge.

**Decision: hybrid resolution — read thread_id from SQLite, resolve run_id via langgraph_sdk** (path 1 in the review's recommendation). No schema migration; one HTTP round-trip per identity poll (well within the per-poll budget):

Add to `src/forge/cli/_serve_production.py` (or a small `_serve_identity_provider.py` sibling):

```python
def _build_async_tasks_identity_provider(
    *,
    sqlite_pool: SqliteLifecyclePersistence,
    autobuild_runner_url: str,
) -> "IdentityProvider":
    """Return an IdentityProvider that resolves (thread_id, run_id) per feature_id.

    Two-step resolution:
      1. SQLite read: SELECT task_id FROM async_tasks WHERE feature_id = ?
         (task_id == thread_id — see _serve_async_task_starter.py:148-149).
         The dispatcher writes this row inside dispatch_autobuild_async
         BEFORE start_async_task returns, but the wireup's
         register_ack_handle runs BEFORE dispatch_autobuild_async — so
         the row is not yet present at first poll. Return None on
         miss; the wireup retries up to identity_resolution_attempts
         times (default 3) with identity_poll_interval_seconds spacing
         (default 1.0s). Production poll budget therefore: ~3s before
         observer surrenders.

      2. langgraph_sdk fetch: once thread_id is known, call
         langgraph_sdk.client.LangGraphClient(url=autobuild_runner_url)
                       .runs.list(thread_id=thread_id, limit=1)
         and return (thread_id, latest_run.run_id). Verify the
         installed langgraph_sdk version's API surface — 0.8.5 is the
         version captured in the 2026-05-08 sidecar log.

    Args:
        sqlite_pool: shared writer connection for the async_tasks read.
        autobuild_runner_url: validated by ServeConfig fail-fast guard;
            same URL forwarded into the sidecar's middleware tool.

    Returns:
        An async callable conforming to IdentityProvider. Returns None
        when (a) no async_tasks row exists yet (dispatcher hasn't run),
        or (b) langgraph_sdk reports zero runs for the thread. Both
        are retryable conditions — the wireup polls and ultimately
        exits cleanly to JetStream redelivery if neither resolves.
    """

    async def _provider(feature_id: str) -> tuple[str, str] | None:
        # Step 1 — SQLite lookup
        cx = sqlite_pool.connection
        row = cx.execute(
            "SELECT task_id FROM async_tasks WHERE feature_id = ? LIMIT 1",
            (feature_id,),
        ).fetchone()
        if row is None:
            return None
        thread_id = row[0]

        # Step 2 — langgraph_sdk lookup for run_id
        try:
            from langgraph_sdk.client import LangGraphClient
            client = LangGraphClient(url=autobuild_runner_url)
            runs = await client.runs.list(thread_id=thread_id, limit=1)
            if not runs:
                return None
            return (thread_id, runs[0].run_id)
        except Exception as exc:
            # Treat transport / SDK errors as "not yet" — the wireup
            # retries; persistent failure exits the observer cleanly
            # and JetStream redelivery + recover_in_flight pick it up.
            logger.warning(
                "_provider: failed to resolve run_id for feature_id=%s "
                "thread_id=%s: %s; treating as None",
                feature_id, thread_id, exc,
            )
            return None

    return _provider
```

Add unit tests at `tests/forge/test_serve_identity_provider.py`:
- `test_identity_provider_returns_none_when_async_tasks_row_missing`
- `test_identity_provider_reads_thread_id_from_async_tasks_row`
- `test_identity_provider_calls_langgraph_sdk_runs_list_with_thread_id` (monkey-patches `LangGraphClient`)
- `test_identity_provider_returns_thread_id_and_latest_run_id`
- `test_identity_provider_returns_none_when_runs_list_empty`
- `test_identity_provider_returns_none_when_sdk_raises`

### 4. Boot migration for `lifecycle_bridge_terminal_publishes` table

`forge.lifecycle_bridge.coexistence.apply_migration(connection)` (`coexistence.py:140-175`) creates the `lifecycle_bridge_terminal_publishes` table required by `TerminalPublishLedger`. **This migration is not currently invoked at boot.** Two options:

- **(a) Direct invocation** (smaller change): Add `coexistence.apply_migration(connection)` immediately after the existing `apply_at_boot(connection)` call at `_serve_production.py:273`. Idempotent (`CREATE TABLE IF NOT EXISTS`).
- **(b) Fold into the migration ladder**: Add the table to `forge.lifecycle.migrations` — preferred long-term, but expands scope.

**Choose (a)** for this task; flag (b) as a follow-up if the migration ladder consolidation is later required.

### 5. Extend `bind_production_dispatch_chain` signature additively

`src/forge/cli/serve.py:190-254` currently exposes:

```python
def bind_production_dispatch_chain(
    *,
    forge_config: Any,
    sqlite_pool: Any,
    async_task_starter: Any | None = None,
) -> ComposeDispatchChainFn:
```

Extend additively — strictly optional kwargs, default `None`:

```python
def bind_production_dispatch_chain(
    *,
    forge_config: Any,
    sqlite_pool: Any,
    async_task_starter: Any | None = None,
    bridge_wireup_parts: "LifecycleBridgeWireupParts | None" = None,
) -> ComposeDispatchChainFn:
```

The closure body finalises the wireup inside the `_compose(client)` async function (where the publisher is in scope):

```python
async def _compose(client: Any) -> None:
    publisher, emitter = build_publisher_and_emitter(client, config=forge_config.pipeline)

    register_ack_handle = None
    terminal_publish_ledger = None
    if bridge_wireup_parts is not None:
        from forge.lifecycle_bridge.wireup import LifecycleBridgeWireup
        wireup = LifecycleBridgeWireup(
            bridge=bridge_wireup_parts.bridge,
            translator=bridge_wireup_parts.translator,
            publisher=publisher,
            stream_source=bridge_wireup_parts.stream_source,
            identity_provider=bridge_wireup_parts.identity_provider,
        )
        register_ack_handle = wireup.register_ack_handle
        terminal_publish_ledger = bridge_wireup_parts.terminal_publish_ledger

    deps = build_pipeline_consumer_deps(
        client,
        forge_config,
        sqlite_pool,
        async_task_starter=async_task_starter,
        register_ack_handle=register_ack_handle,
        terminal_publish_ledger=terminal_publish_ledger,
    )
    dispatcher = make_handle_message_dispatcher(deps)
    _serve_daemon.dispatch_payload = dispatcher
    logger.info("forge-serve: dispatch chain composed; …")
```

Note: this also requires hoisting the `build_publisher_and_emitter(client, …)` call from inside `build_pipeline_consumer_deps` up into the closure, OR threading the publisher into `build_pipeline_consumer_deps`. **Recommendation**: thread the publisher in. Currently `build_pipeline_consumer_deps` constructs the publisher itself (`_serve_deps.py:532`); refactor to accept an optional injected `publisher` (default-`None` → construct internally for the legacy path). Strictly additive on that signature too.

### 6. Re-exports / imports

`src/forge/lifecycle_bridge/__init__.py` currently exports only `AckHandle, BuildContext, LifecycleBridge`. **Either**:
- Add re-exports for `LifecycleBridgeWireup`, `StreamEventTranslator`, `StreamSource`, `langgraph_stream_source`, `TerminalPublishLedger` (preferred — clean public surface); **or**
- Import from submodules directly in `_serve_production.py` (acceptable — submodule imports are not private here).

Choose the re-export option for consistency with `bridge.py`'s existing exports.

### 7. Seam tests (regression protection)

Add to `tests/forge/test_cli_serve_production.py` per TASK-REV-PEBR-003 §AC-5:

- **`TestLifecycleBridgeWireupComposition::test_bind_production_serve_threads_register_ack_handle_and_terminal_publish_ledger`** — captures kwargs to `bind_production_dispatch_chain`, asserts `register_ack_handle is not None` and `terminal_publish_ledger is not None`.
- **`TestLifecycleBridgeWireupComposition::test_bind_production_serve_logs_wired_not_deferred`** — drives the closure with a fake NATS client and `caplog`, asserts the boot log line says `ack_bridge=wired` and `terminal_publish_ledger=wired` (not `deferred`).

Pattern mirrors `TestAsyncTaskStarterThreading` (lines 440-512 of the existing file) and `TestF010JBindProductionServeThreadsAutobuildRunnerUrl` (lines 671-718).

### 8. Runbook revalidation (post-merge)

After this task lands and the forge image is rebuilt:

1. Re-run jarvis runbook §6.2 (`Queue FEAT-XXX for build...`) against canonical NATS on GB10 (or the local test broker).
2. Tail `nats sub "pipeline.>" --raw` and verify:
   - One inbound `pipeline.build-queued.FEAT-XXX`.
   - At least one `pipeline.build-started.FEAT-XXX` (NEW — proves wireup composed).
   - At least one `pipeline.stage-complete.FEAT-XXX`.
   - One terminal `pipeline.build-complete.FEAT-XXX` (or `build-failed`).
3. `docker logs forge-prod` (or local equivalent) shows `ack_bridge=wired, terminal_publish_ledger=wired` in the boot log line.
4. JetStream consumer state shows `ack_floor` advancing past the inbound message (no perpetual redelivery loop).
5. Capture the new correlation_id and append to this task's completion notes.

## Acceptance Criteria

- [ ] **AC-1** — `src/forge/cli/_serve_production.py` exposes `_build_lifecycle_bridge_wireup_parts(*, sqlite_pool, autobuild_runner_url) -> LifecycleBridgeWireupParts` and `bind_production_serve` invokes it as Step 6.5.
- [ ] **AC-2** — `forge.lifecycle_bridge.langgraph_stream_source(runner_url: str) -> StreamSource` is shipped (new module or new function). Unit-tested at `tests/forge/lifecycle_bridge/test_stream_source.py`.
- [ ] **AC-3** — A production `IdentityProvider` factory is shipped that reads `task_id` (=thread_id) from the `async_tasks` SQLite mirror AND resolves `run_id` via `langgraph_sdk.client.LangGraphClient(url=autobuild_runner_url).runs.list(thread_id=…, limit=1)`. **No `async_tasks` schema migration** — run_id is fetched on demand. Unit-tested at `tests/forge/test_serve_identity_provider.py` with the six tests listed above (or equivalent set covering: SQLite miss → None; SQLite hit → SDK call; SDK empty → None; SDK raise → None and warning logged).
- [ ] **AC-4** — `bind_production_serve` Step 3.5 calls `coexistence.apply_migration(connection)` so the `lifecycle_bridge_terminal_publishes` table exists at boot. Idempotent.
- [ ] **AC-5** — `forge.cli.serve.bind_production_dispatch_chain` signature is extended with `bridge_wireup_parts: LifecycleBridgeWireupParts | None = None`. Strictly additive — existing callers (smoke tests, FW10-011-style integration tests) pass without change. The closure finalises `LifecycleBridgeWireup` from the parts + the constructed `PipelinePublisher` and threads `register_ack_handle` + `terminal_publish_ledger` into `build_pipeline_consumer_deps`.
- [ ] **AC-6** — `forge.cli._serve_deps.build_pipeline_consumer_deps` accepts an optional `publisher` kwarg so the closure can inject the shared publisher (default-None preserves legacy behaviour). Strictly additive.
- [ ] **AC-7** — `forge.lifecycle_bridge.__init__` re-exports `LifecycleBridgeWireup`, `StreamEventTranslator`, `StreamSource`, `langgraph_stream_source`, `TerminalPublishLedger`.
- [ ] **AC-8** — `tests/forge/test_cli_serve_production.py` gains class `TestLifecycleBridgeWireupComposition` with the two seam tests specified in TASK-REV-PEBR-003 §AC-5: threading-capture and boot-log assertion. Both fail on the pre-fix HEAD and pass after the fix.
- [ ] **AC-9** — Existing tests pass unchanged: `tests/forge/test_cli_serve_production.py` (15 tests pre-fix), `tests/forge/test_cli_serve_skeleton.py`, `tests/forge/test_cli_serve_logging.py`, `tests/forge/test_cli_serve_daemon.py`, `tests/forge/lifecycle_bridge/` (entire dir), `tests/integration/test_lifecycle_bridge_sidecar_e2e.py` (PEB-013 — its monkey-patch of `compose_dispatch_chain` is intentionally bypassed and remains the translation-layer regression lock).
- [ ] **AC-10** — All modified files pass project-configured lint/format checks (`ruff check`, `black --check`) with zero errors.
- [ ] **AC-11** — Post-merge: re-run jarvis runbook §6.2+§7 against rebuilt forge image; capture new correlation_id; verify `pipeline.build-started.FEAT-*` envelope reaches JetStream and JetStream `ack_floor` advances. Record correlation_id in completion notes.

## Files to Create

- `src/forge/lifecycle_bridge/stream_source.py` — `langgraph_stream_source` factory.
- `tests/forge/lifecycle_bridge/test_stream_source.py` — unit tests for §2.
- `tests/forge/test_serve_identity_provider.py` — unit tests for §3 (or extend `tests/forge/test_cli_serve_production.py` if the helper lives there).

## Files to Modify

- `src/forge/cli/_serve_production.py` — add `LifecycleBridgeWireupParts` dataclass, `_build_lifecycle_bridge_wireup_parts(...)` helper, `_build_async_tasks_identity_provider(...)` helper (or sibling module), Step 3.5 `coexistence.apply_migration(connection)` call, Step 6.5 wireup-parts construction, and the new threaded kwarg into the `bind_production_dispatch_chain` call.
- `src/forge/cli/serve.py` — extend `bind_production_dispatch_chain` signature with `bridge_wireup_parts: LifecycleBridgeWireupParts | None = None`; finalise `LifecycleBridgeWireup` inside the closure; thread `register_ack_handle` + `terminal_publish_ledger` into `build_pipeline_consumer_deps`.
- `src/forge/cli/_serve_deps.py` — add optional `publisher: PipelinePublisher | None = None` kwarg to `build_pipeline_consumer_deps`; if None, construct internally (legacy); if provided, use the injected publisher.
- `src/forge/lifecycle_bridge/__init__.py` — re-exports per AC-7.
- `tests/forge/test_cli_serve_production.py` — new `TestLifecycleBridgeWireupComposition` class.

## Test invocation

```bash
# Targeted seam tests (the regression lock for this fix)
PYTHONPATH=src python -m pytest tests/forge/test_cli_serve_production.py -x -v -k "TestLifecycleBridgeWireupComposition"

# All bind_production_serve coverage (regression check on existing tests)
PYTHONPATH=src python -m pytest tests/forge/test_cli_serve_production.py -x -v

# New helpers
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_stream_source.py tests/forge/test_serve_identity_provider.py -x -v

# Full forge unit suite (parity check)
PYTHONPATH=src python -m pytest tests/forge/ -x

# Lint + format
ruff check src/forge/cli/_serve_production.py src/forge/cli/serve.py src/forge/cli/_serve_deps.py src/forge/lifecycle_bridge/__init__.py src/forge/lifecycle_bridge/stream_source.py
black --check src/forge/cli/_serve_production.py src/forge/cli/serve.py src/forge/cli/_serve_deps.py src/forge/lifecycle_bridge/
```

## Cross-component interface notes

The fix touches **two cross-component boundaries** that the implementation must verify (per the project rule that fix tasks crossing component boundaries name the expected interface format):

1. **`forge ↔ langgraph-runner sidecar` SSE contract** — `langgraph_stream_source` adapts `langgraph_sdk.client.runs.join_stream(thread_id, run_id, stream_mode="values")` and yields `langgraph_sdk.schema.StreamPart` events. Verify the installed `langgraph_sdk` version exposes this API surface; if `runs.join_stream` is renamed/refactored, the adapter must follow.
2. **`async_tasks` SQLite mirror schema** — production `IdentityProvider` reads `(thread_id, run_id)` from the `async_tasks` table written by the autobuild dispatcher (DDR-006 / FW10-005). Schema is owned by `forge.cli._serve_deps_state_channel.build_autobuild_state_initialiser` (`_serve_deps.py:523`). Confirm the column names (`thread_id` / `run_id`) match the table DDL before writing the SQL query.

## References

- [TASK-REV-PEBR-003](TASK-REV-PEBR-003-analyse-bind-production-serve-wireup-gap.md) — parent review with full diagnosis, wiring map, fix-shape decision rationale, and seam-test specification.
- [TASK-FIX-F010](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md) — one-layer-shallower precedent (the `serve_cmd` → `bind_production_dispatch_chain` rebind). Same shape, smaller blast radius.
- [TASK-FRR-PEB-002](../../completed/TASK-FRR-PEB-002-bridge-skeleton-and-registry.md) — `LifecycleBridgeWireup` + `LifecycleBridge` + `BridgeRegistry` skeleton this task wires up.
- [TASK-FRR-PEB-005](../../completed/TASK-FRR-PEB-005-f010f-coexistence-boundary.md) — `TerminalPublishLedger` + `coexistence.apply_migration` this task invokes at boot.
- [TASK-FRR-PEB-013](../../completed/TASK-FRR-PEB-013-sidecar-aware-e2e-integration-test.md) — sidecar-aware E2E that locks the translation layer; bypasses `bind_production_serve` and so does NOT lock wireup composition.
- `src/forge/lifecycle_bridge/wireup.py:277-283` — docstring contract naming `bind_production_serve` as the canonical wireup composition site.
- `phase2.2-forge-logs.log` (under `/tmp/runbook-evidence-2026-05-08/`) — the operator-facing "deferred" log line on the rebuilt 2026-05-08 image.
