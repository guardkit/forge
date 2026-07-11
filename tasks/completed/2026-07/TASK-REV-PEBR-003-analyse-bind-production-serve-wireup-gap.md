---
id: TASK-REV-PEBR-003
title: Analyse bind_production_serve LifecycleBridge wire-up gap (Gap PEBR-WIREUP)
status: review_complete
created: 2026-05-08T07:30:00Z
updated: 2026-05-08T08:30:00Z
review_results:
  mode: gap-analysis
  depth: standard
  decision: implement
  fix_shape: option-b-helper-factory
  spawned_task: TASK-FORGE-FRR-PEBR-WIREUP
  estimated_implementation_minutes: 135
  completed_at: 2026-05-08T08:30:00Z
  revision_2026-05-08T09:00Z:
    summary: deeper boundary trace + C4 sequence diagrams; surfaced async_tasks.run_id schema gap; chose hybrid SQLite+langgraph_sdk identity resolution
    confidence: 100%
priority: high
task_type: review
review_mode: gap-analysis
review_depth: standard
decision_required: true
parent_feature: FEAT-PEBR
companion_to:
  - TASK-REV-PEBR-001
  - TASK-REV-PEBR-002
discovered_during: jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md walkthrough on GB10
discovered_at: 2026-05-08T05:58:23Z
discovered_correlation_id: 5673965b-e302-4a10-89cb-ceb430e64995
forge_head_at_discovery: e50241e
jarvis_head_at_discovery: ca2ba6b
tags:
  - forge-serve
  - production-composer
  - lifecycle-bridge
  - wire-up
  - regression-protection
  - feat-pebr
estimated_review_minutes: 45
estimated_implementation_minutes_after_review: 60
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Analyse `bind_production_serve` LifecycleBridge wire-up gap (Gap PEBR-WIREUP)

## TL;DR

FEAT-PEBR's bridge code is **merged and unit-tested** (PEB-001 through PEB-014 all in `tasks/completed/`), but `forge.cli._serve_production.bind_production_serve` **does not compose `LifecycleBridgeWireup` into the running daemon**. As a result:

- `register_ack_handle` and `terminal_publish_ledger` default to `None` in `build_pipeline_consumer_deps`
- The daemon's own boot log openly states this on every boot:
  ```
  composed PipelineConsumerDeps (async_task_starter=wired,
    ack_bridge=deferred (TASK-FRR-PEB-002),
    terminal_publish_ledger=deferred (TASK-FRR-PEB-005))
  ```
- The autobuild_runner sidecar runs successfully, but **zero outbound lifecycle envelopes** reach JetStream (no `pipeline.build-started.*` / `pipeline.stage-complete.*` / `pipeline.build-complete.*` / `pipeline.build-failed.*`)
- The inbound `pipeline.build-queued.*` Msg is **never acked**, causing redelivery every 30s and "duplicate active build" warnings on every redelivery (`forge-serve` consumer state at end of rerun: `delivered=7277, redelivered=2, ack_floor=11`)

This is the same shape one layer deeper as **TASK-FIX-F010** (`serve_cmd` not rebinding the production composer). The fix is one-task-deep; the review needs to choose between three fix shapes and produce a follow-up implementation task.

**No code changes in this task — analysis + decision + spawn implementation task only.**

## Background

### Discovery context

The 2026-05-08 GB10 walkthrough of `jarvis/docs/runbooks/RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` was executed against:

- **forge HEAD** `e50241e` (post `5d84d94 merge(FEAT-PEBR): autobuild_runner pipeline-emitter bridge — code-only`, 2026-05-07)
- **jarvis HEAD** `ca2ba6b` (post `dcaa8eb` lifecycle subscriber widening + `6071fe0` TASK-FRR-F010Db disjoint filter narrowing)
- **forge image** rebuilt 2026-05-08 06:51 (sha: 507MB)

The two known gaps from the prior walkthrough (Addendum 5 of `RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`) were both addressed:

1. ✅ **F010.L** — autobuild_runner LLM retargeting (TASK-FORGE-FRR-F010L; commit `378ccd6`). Verified live: sidecar drove 12 LLM calls against `qwen36-workhorse` on llama-swap and `Background run succeeded run_completed_in_ms=37179`.
2. ⚠️ **F010.M / FEAT-PEBR** — autobuild_runner ↔ pipeline-emitter bridge. Bridge **code** merged, but production composer wire-up is missing — this task.

### Symptom on the wire

`nats sub "pipeline.>" --raw` for ~4 minutes captured **exactly one envelope** — the inbound `pipeline.build-queued.FEAT-43DE` jarvis published with `correlation_id=5673965b-e302-4a10-89cb-ceb430e64995`. Zero outbound lifecycle envelopes from forge despite the autobuild succeeding in the sidecar.

Evidence file: `/tmp/runbook-evidence-2026-05-08/phase7-pipeline-tail.log` (1 line).

### Symptom in forge-prod boot log (every boot, post-FEAT-PEBR)

```
2026-05-08T05:54:02 [INFO] forge.cli._serve_deps: build_pipeline_consumer_deps:
  composed PipelineConsumerDeps
  (async_task_starter=wired,
   ack_bridge=deferred (TASK-FRR-PEB-002),
   terminal_publish_ledger=deferred (TASK-FRR-PEB-005))
```

Evidence file: `/tmp/runbook-evidence-2026-05-08/phase2.2-forge-logs.log`.

### Co-symptom — inbound Msg never acked

```json
{"delivered": 7277, "pending": 0, "redelivered": 2, "ack_floor": 11}
```

The deferred-ack contract (PEB-001) was meant to ack on terminal lifecycle envelope arrival, but with the bridge unwired no terminal ever fires from forge's side. Every 30 seconds the dispatcher logs:

```
dispatch_build: duplicate active build for feature_id=FEAT-43DE
  correlation_id=5673965b-...; skipping dispatch
```

Evidence file: `/tmp/runbook-evidence-2026-05-08/phase7-consumer-info.json`.

### Sidecar — autobuild ran end-to-end

Forge dispatched the build via HTTP POST to the langgraph-runner sidecar at `http://localhost:8124/threads/.../runs` and the autobuild succeeded:

```
2026-05-08T05:59:01 [INFO] langgraph_api.worker: Background run succeeded
  run_completed_in_ms=37179
  run_id=019e062a-6b8c-7be0-986c-ce9243734e22
```

Evidence file: `/tmp/runbook-evidence-2026-05-08/sidecar.log`.

## Root cause (code-level)

**Source: `forge/src/forge/cli/_serve_production.py:bind_production_serve` (current state)**

```python
# Step 5 — eagerly construct the middleware. ImportErrors / wiring
# bugs raise here, before the daemon attaches its consumer.
middleware = serve_module._build_async_subagent_middleware(
    autobuild_runner_url=config.autobuild_runner_url,
)

# Step 6 — derive the AsyncTaskStarter from the middleware tool surface
async_task_starter = _resolve_async_task_starter(middleware)

# Step 7 — build the production composer and rebind the seam.
composer = serve_module.bind_production_dispatch_chain(
    forge_config=forge_config,
    sqlite_pool=sqlite_pool,
    async_task_starter=async_task_starter,
)
serve_module.compose_dispatch_chain = composer
```

There is **no Step 6.5** that:

1. Constructs a `LifecycleBridgeWireup` instance (PEB-002 ships the type at `forge/src/forge/lifecycle_bridge/wireup.py`).
2. Builds a production stream-source factory (PEB-005 was supposed to expose `langgraph_stream_source(runner_url=...)` or equivalent — review needs to confirm this actually shipped).
3. Constructs a `TerminalPublishLedger` against the SQLite pool.
4. Threads `wireup.register_ack_handle` and the ledger into `bind_production_dispatch_chain(...)` via the `register_ack_handle` and `terminal_publish_ledger` named parameters that `_serve_deps.build_pipeline_consumer_deps` already accepts.

The deps composer at `forge/src/forge/cli/_serve_deps.py:556-566` correctly logs them as `deferred (TASK-FRR-PEB-002)` / `deferred (TASK-FRR-PEB-005)` when `None` — so the operator-facing log line is a pre-baked hint that the production composer is the missing link. The `wireup.py` module's docstring at line 277-278 even names the expected composition site: *"One instance per `forge serve` daemon (composed in :func:`forge.cli._serve_production.bind_production_serve`)"* — but the composition never happens.

## Why FEAT-PEBR's tests didn't catch this

PEB-001 through PEB-014 are all marked `completed` in `forge/tasks/completed/`. **PEB-013 ("sidecar-aware-e2e-integration-test")** is the test whose name implies it would have caught this — it's supposed to exercise the full sidecar→bridge→JetStream path.

The review must **audit PEB-013**:

- Does its test code invoke `forge.cli._serve_production.bind_production_serve(...)` end-to-end and observe envelopes on a real or fake JetStream?
- Or does it construct a hand-rolled composer that wires `LifecycleBridgeWireup` directly, bypassing `bind_production_serve`?
- If the latter, that's the test gap that let PEBR-WIREUP ship; the recommended seam test below is the regression-protection home.

This is the same shape one layer deeper as **TASK-FIX-F010** (`serve_cmd` not rebinding `compose_dispatch_chain` to `bind_production_dispatch_chain`). Cross-reference TASK-FIX-F010's seam-test pattern (likely `tests/forge/cli/test_serve_*_seam.py` — verify exact path) — that's the regression-protection home this fix should land in.

## Acceptance criteria for this review task

- [ ] **AC-1: Diagnosis confirmed/refuted** — Read `_serve_production.bind_production_serve` (lines 215–325 in current HEAD) and `_serve_deps.build_pipeline_consumer_deps` (lines 317–566). Confirm the bridge is genuinely unwired in production code path. Either confirm the daemon's "deferred" log line accurately reflects the production state, or identify why the gap diagnosis is wrong. Findings recorded in `## Findings → Diagnosis` below.
- [ ] **AC-2: PEB-013 audited** — Locate PEB-013's test file. Determine whether it invokes `bind_production_serve` or a hand-rolled composer. Findings recorded in `## Findings → PEB-013 audit` below, including exact file path + the test-composition pattern (lines if helpful).
- [ ] **AC-3: Missing wiring mapped** — Trace `LifecycleBridgeWireup`'s constructor signature in `forge/src/forge/lifecycle_bridge/wireup.py`. List exactly the dependency types and their factory call-sites:
    - `bridge: LifecycleBridge` — where does the production daemon get one (factory function name + module)?
    - `publisher: PipelinePublisher` — already constructed somewhere in the daemon (which file/function)?
    - `stream_source: StreamSource` (Protocol) — confirm whether PEB-005 actually shipped a production factory like `langgraph_stream_source(runner_url=config.autobuild_runner_url)`. **If not shipped, this expands the fix scope** — flag explicitly.
    - `terminal_publish_ledger: TerminalPublishLedger` — factory + persistence binding.
  Findings recorded in `## Findings → Wiring map` below.
- [ ] **AC-4: Fix shape decided** — Pick between Options A / B / C below with rationale. Decision recorded in `## Decision` block at the bottom.
- [ ] **AC-5: Seam test specified** — Exact assertion shape for the regression-protection seam test (file path + assertion text). The assertion must be operator-meaningful — e.g. "boot log MUST NOT contain `deferred (TASK-FRR-PEB-002)` or `deferred (TASK-FRR-PEB-005)` after `bind_production_serve` runs against a real ServeConfig". Recorded in `## Findings → Seam test spec`.
- [ ] **AC-6: Implementation task spawned** — Write a follow-up implementation task at `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-fix.md` (or whichever id matches the FRR-PEB sequence). Task body should be ready for `/task-work` to pick up: clear AC list, files-to-modify, files-to-create, test invocation, decisive fix shape from AC-4. Confirmed in `## Findings → Spawned task`.

## Fix-shape options to decide between (AC-4)

### Option A — Inline wiring in `bind_production_serve`

Add the four-step composition (bridge / publisher / stream-source / ledger) directly in `bind_production_serve`'s body, between Step 6 and Step 7.

- **Pros:** Single function change. Smallest diff. Mirrors how F010J added `_build_async_subagent_middleware` invocation inline.
- **Cons:** `bind_production_serve` body grows; harder to unit-test the wireup composition in isolation.
- **Blast radius:** Low — only `_serve_production.py` and one new seam test.

### Option B — Helper factory `_build_lifecycle_bridge_wireup(...)` in `_serve_production.py`

Mirror the existing `_resolve_async_task_starter(middleware)` pattern at `_serve_production.py:302`. New helper builds and returns a fully composed `LifecycleBridgeWireup`. `bind_production_serve` calls it inline (one line).

- **Pros:** More testable — helper has its own unit test surface. Smaller blast radius if the wireup composition logic later needs to grow (e.g., for FEAT-PEBR follow-ups). Code reads like F010E's pattern (`_StructuredToolAsyncTaskStarter` adapter wrap-and-test).
- **Cons:** Marginally more code (one extra function).
- **Blast radius:** Low — `_serve_production.py` + new helper tests + one seam test.

### Option C — Move wiring up into `serve.bind_production_dispatch_chain` directly

Push the composition one level up so `bind_production_dispatch_chain` does it itself rather than receiving `register_ack_handle` and `terminal_publish_ledger` as parameters.

- **Pros:** Public seam (`bind_production_dispatch_chain`) becomes the single composition point — operators don't need to construct anything before calling it.
- **Cons:** Changes the public seam's parameter signature. **Likely breaks PEB-002 / PEB-005 unit tests** that exercise `bind_production_dispatch_chain` with explicit `register_ack_handle=fake` injection. May require updating those tests, expanding scope.
- **Blast radius:** Medium-High — touches `serve.py`'s public surface plus N callers.

## Recommended seam test (preview — AC-5 confirms exact shape)

File location candidate: `tests/forge/cli/test_serve_production_seam.py` (or `test_bind_production_serve_seam.py`).

Indicative assertion shape:

```python
def test_bind_production_serve_wires_lifecycle_bridge(caplog, ...):
    """Regression-protection seam test for Gap PEBR-WIREUP.

    bind_production_serve MUST compose LifecycleBridgeWireup so that the
    deps composer logs `ack_bridge=wired` and `terminal_publish_ledger=wired`
    on the production hot path. If either reverts to "deferred", the
    inbound pipeline.build-queued.* Msg is never acked and no outbound
    lifecycle envelopes reach JetStream — the exact regression that
    surfaced during the 2026-05-08 jarvis runbook walkthrough
    (correlation_id=5673965b-e302-4a10-89cb-ceb430e64995).
    """
    config = build_test_serve_config(...)
    forge_config = build_test_forge_config(...)
    bind_production_serve(config, forge_config=forge_config)

    log_text = "\n".join(record.getMessage() for record in caplog.records)

    assert "deferred (TASK-FRR-PEB-002)" not in log_text, (
        "ack_bridge is not wired by bind_production_serve "
        "— Gap PEBR-WIREUP regression"
    )
    assert "deferred (TASK-FRR-PEB-005)" not in log_text, (
        "terminal_publish_ledger is not wired by bind_production_serve "
        "— Gap PEBR-WIREUP regression"
    )
    assert "ack_bridge=wired" in log_text
    assert "terminal_publish_ledger=wired" in log_text
```

Pattern is identical to TASK-FIX-F010's seam test (which asserted the receipt-only `_default_dispatch` log line was no longer reachable on the hot path).

## Files to read (for the review)

- `forge/src/forge/cli/_serve_production.py` lines 215–325 — the gap site
- `forge/src/forge/cli/_serve_deps.py` lines 317–566 — the deps composer that logs "deferred"
- `forge/src/forge/lifecycle_bridge/wireup.py` — `LifecycleBridgeWireup` class + constructor signature
- `forge/src/forge/lifecycle_bridge/__init__.py` — currently exports only `bridge.py` types; check whether wireup needs to be re-exported
- `forge/src/forge/lifecycle_bridge/bridge.py` — `LifecycleBridge` factory (look for `from_sqlite_pool` or similar)
- `forge/src/forge/lifecycle_bridge/translation.py` — likely PEB-003 SSE→envelope translator
- `forge/src/forge/lifecycle_bridge/coexistence.py` — likely PEB-005's terminal-publish ledger lives here (since it imports into `_serve_deps.py`)
- `forge/tasks/completed/TASK-FRR-PEB-005-f010f-coexistence-boundary.md` — confirm what publishing path PEB-005 actually shipped
- `forge/tasks/completed/TASK-FRR-PEB-013-sidecar-aware-e2e-integration-test.md` — audit per AC-2
- `forge/tasks/completed/TASK-FIX-F010-*.md` (or wherever TASK-FIX-F010 lives) — the seam-test precedent

## Files referenced for context

- `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08.md` § "Forge gap discovered (NEW — 2026-05-08)" — diagnosis + indicative fix sketch
- `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md` Addendum 5 — F010J / F010L / F010M progression, why this gap is one-task-deep
- `jarvis/docs/runbooks/RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` § Phase 7 — close-criterion this gap blocks

## Evidence files (under `/tmp/runbook-evidence-2026-05-08/`)

- `phase2.2-forge-logs.log` — daemon boot showing `ack_bridge=deferred (TASK-FRR-PEB-002), terminal_publish_ledger=deferred (TASK-FRR-PEB-005)` and `bridge=fallback` annotation on every dispatch
- `phase7-pipeline-tail.log` — **1 line** (the inbound `build_queued`) — proof of total wire silence on outbound side
- `phase7-consumer-info.json` — `{"delivered":7277, "pending":0, "redelivered":2, "ack_floor":11}` — proof of redelivery loop / never-acked behaviour
- `phase7-forge-logs.log` — full forge log filtered to rerun timestamps + correlation_ids
- `sidecar.log` — `langgraph_api.worker: Background run succeeded run_completed_in_ms=37179` — proof autobuild runs end-to-end
- `phase6-7-chat.log` — full DEBUG jarvis chat transcript including the queue_build success + zero notification drains
- `~/.jarvis/transcripts/5673965b-e302-4a10-89cb-ceb430e64995.txt` (216KB)
- `~/.jarvis/traces/5673965b-e302-4a10-89cb-ceb430e64995.json` (1124B; full DDR-029 schema)

## Findings (filled during review)

### Diagnosis (AC-1)

**Confirmed.** `forge.cli._serve_production.bind_production_serve` (`src/forge/cli/_serve_production.py:168-321`) executes Steps 1 → 8 (validate, mkdir, connect_writer, apply_at_boot, SqliteLifecyclePersistence, _build_async_subagent_middleware, _resolve_async_task_starter, bind_production_dispatch_chain, _close_previous_connection_quietly). There is **no** Step 6.5 that constructs a `LifecycleBridge`, a `LifecycleBridgeWireup`, or a `TerminalPublishLedger`. The call to `serve_module.bind_production_dispatch_chain(...)` at lines 304-309 passes only `forge_config`, `sqlite_pool`, `async_task_starter` — neither `register_ack_handle` nor `terminal_publish_ledger` is threaded through.

`forge.cli._serve_deps.build_pipeline_consumer_deps` (`src/forge/cli/_serve_deps.py:429-568`) accepts both as named-keyword parameters with `None` defaults, and at lines 558-568 emits the operator-facing log line:

```
composed PipelineConsumerDeps (async_task_starter=%s, ack_bridge=%s, terminal_publish_ledger=%s)
```

with each field reported as `"wired"` when non-`None` and as `"deferred (TASK-FRR-PEB-002)"` / `"deferred (TASK-FRR-PEB-005)"` when `None`. Because `bind_production_serve` never threads either field, the deps composer always logs `deferred` on every boot — exactly matching the daemon log captured in `phase2.2-forge-logs.log`.

Confirming docstring evidence: `src/forge/lifecycle_bridge/wireup.py:277-283` self-describes as *"One instance per `forge serve` daemon (composed in `forge.cli._serve_production.bind_production_serve`)"* — that composition is missing.

`forge.cli.serve.bind_production_dispatch_chain` (`src/forge/cli/serve.py:190-254`) currently exposes signature `(*, forge_config, sqlite_pool, async_task_starter=None) -> ComposeDispatchChainFn`. It does **not** accept `register_ack_handle` or `terminal_publish_ledger`, so even if `bind_production_serve` constructed them, there is no kwargs path to thread them down to `build_pipeline_consumer_deps`. **The fix must extend this signature additively** (optional kwargs, default `None`) — adding two new public-seam parameters is unavoidable in any of Options A / B / C.

`forge.lifecycle_bridge.__init__` re-exports only `AckHandle`, `BuildContext`, `LifecycleBridge` (`src/forge/lifecycle_bridge/__init__.py:18-22`). `LifecycleBridgeWireup`, `StreamSource`, `TerminalPublishLedger` are not re-exported — the fix must either add them or import them from the submodule directly.

### PEB-013 audit (AC-2)

**File**: `tests/integration/test_lifecycle_bridge_sidecar_e2e.py` (788 lines).

**Confirmed bypass — this is the test gap that let PEBR-WIREUP ship.** PEB-013 does **not** invoke `bind_production_serve`. It calls `serve_module._run_serve(config, state)` directly (line 635-638) with three monkey-patches in place:

1. **`compose_dispatch_chain` is replaced** at lines 613-617:
   ```python
   monkeypatch.setattr(
       serve_module,
       "compose_dispatch_chain",
       _compose_with_sidecar_aware_dispatch,
   )
   ```
2. The replacement composer (`_compose_with_sidecar_aware_dispatch`, lines 568-617) hand-rolls a `PipelineConsumerDeps` (lines 603-608) **without** `register_ack_handle` and **without** any reference to `LifecycleBridgeWireup`, `LifecycleBridge`, `TerminalPublishLedger`, `StreamEventTranslator`, or `langgraph_stream_source`:
   ```python
   deps = PipelineConsumerDeps(
       forge_config=forge_config,
       is_duplicate_terminal=_is_duplicate_terminal,
       dispatch_build=_dispatch_build,
       publish_build_failed=_publish_build_failed,
   )
   ```
3. `_dispatch_build` (lines 592-601) schedules a `_LifecycleScripter` (lines 314-426) that drives lifecycle envelopes through the production `emitter` (the `autobuild_runner` direct-emit path, FW10-005 lineage) — **NOT** through `LifecycleBridge` → `LifecycleBridgeWireup`. The lifecycle envelopes flowing on the wire in PEB-013 prove the translation layer + `PipelinePublisher` + JetStream path work end-to-end, but they do **not** prove the bridge wireup is composed by `bind_production_serve`.

**Why this passed the PEB-013 review**: PEB-013's stated regression target is "translation-layer regressions and SDK version skew that an in-process test cannot catch" (file docstring lines 12-14) — its *intentional* contract is to lock the SSE → envelope translation against a real sidecar, not to lock the production composer's wireup composition. The wireup-composition lock was supposed to be FW10-011's job (the in-process composition lock referenced at lines 7-9), but FW10-011 predates FEAT-PEBR and does not assert on `register_ack_handle` / `terminal_publish_ledger` either.

**Recommendation**: The seam test specified in AC-5 below is the regression-protection home, sibling to PEB-013 / FW10-011. PEB-013 itself need not be modified — its scope is the translation layer, and broadening it to also assert wireup composition would conflict with its monkey-patch of `compose_dispatch_chain`.

### Wiring map (AC-3)

`LifecycleBridgeWireup.__init__` (`src/forge/lifecycle_bridge/wireup.py:327-340`) requires:

| Dep | Type | Production factory site | Status |
|---|---|---|---|
| `bridge` | `LifecycleBridge` | `LifecycleBridge(registry=BridgeRegistry(connection=…), sidecar_url=ServeConfig.autobuild_runner_url, sdk_client=langgraph_sdk.LangGraphClient(…), deadline_handler=…)` | ✅ Constructible. `BridgeRegistry(connection=…)` sees production usage at `src/forge/cli/status.py:352`. The `sdk_client` is the same `LangGraphClient(url=runner_url)` already constructed-on-demand inside the autobuild dispatcher (would need to be opened earlier and shared). `deadline_handler` is the **only** factory genuinely missing — the wireup itself is the canonical handler (publishes `build-failed` + acks + detaches), so deadline-handler wiring is internally consistent if wireup is constructed first. |
| `translator` | `StreamEventTranslator` | `StreamEventTranslator()` (no args, see `src/forge/lifecycle_bridge/translation.py:290+`) | ✅ Constructible — zero-arg. |
| `publisher` | `PipelinePublisher` | `forge.cli._serve_deps_lifecycle.build_publisher_and_emitter(client, config=forge_config.pipeline)` returns `(publisher, emitter)` — already invoked at `_serve_deps.py:532`. | ✅ Exists, but **needs the NATS client** which is opened later by `_run_serve`. **Ordering implication**: the wireup must be finalised inside the closure returned by `bind_production_dispatch_chain` (where the publisher is in scope), not in `bind_production_serve` itself. The bridge / translator / registry / ledger / identity-provider / stream-source can all be constructed earlier. |
| `stream_source` | `StreamSource` (Protocol) | **Expected: `forge.lifecycle_bridge.langgraph_stream_source(runner_url=…)` per `wireup.py:52`. NOT SHIPPED.** | ❌ **Fix-scope expansion confirmed.** A repo-wide `grep -rn "langgraph_stream_source\|join_stream"` of `src/forge/` shows the symbol exists only in docstrings (`wireup.py:52-53`, `translation.py:6,347`). The function must be authored by the implementation task: a thin async-iterator adapter over `langgraph_sdk.client.LangGraphClient(url=runner_url).runs.join_stream(thread_id=…, run_id=…, stream_mode="values")`. Estimated ~30-50 lines + unit tests. |
| `identity_provider` | `IdentityProvider` (`async (feature_id) -> (thread_id, run_id) \| None`) | **Expected: production factory reading from `async_tasks` SQLite mirror per `wireup.py:248`. NOT SHIPPED.** | ❌ **Second fix-scope expansion.** Only `_default_identity_provider()` (`wireup.py:254-266`) exists, returning `None` unconditionally. Production needs an adapter that reads `async_tasks.thread_id` / `async_tasks.run_id` for a given `feature_id` — the schema is owned by `forge.lifecycle.persistence.SqliteLifecyclePersistence`. Estimated ~20-30 lines + unit tests. |
| `terminal_publish_ledger` | `TerminalPublishLedger` (passed alongside the wireup, not into it) | `TerminalPublishLedger(connection=sqlite_pool._connection)` per `coexistence.py:209-217` | ✅ Constructible. **But**: requires the `lifecycle_bridge_terminal_publishes` table, created by `coexistence.apply_migration(connection)` (`coexistence.py:140-175`). That migration is **not** invoked by `bind_production_serve` Step 3.5 (which only calls `apply_at_boot`). The fix must add a `coexistence.apply_migration(connection)` call (or fold it into the migration ladder at `forge.lifecycle.migrations`). |

**Summary of missing pieces (all scoped into the implementation task)**:

1. New module/function: `forge.lifecycle_bridge.langgraph_stream_source(runner_url: str) -> StreamSource` — async-iterator adapter over `langgraph_sdk.client.runs.join_stream`.
2. New module/function: a production `IdentityProvider` factory reading from `async_tasks` SQLite mirror via the shared `SqliteLifecyclePersistence` pool.
3. Boot-time migration: invoke `forge.lifecycle_bridge.coexistence.apply_migration(connection)` in `bind_production_serve` Step 3.5 (or fold into `forge.lifecycle.migrations.apply_at_boot`).
4. Signature extension: `forge.cli.serve.bind_production_dispatch_chain` gains optional kwargs that thread the bridge / wireup parts and the ledger down to `build_pipeline_consumer_deps` (which already accepts `register_ack_handle` and `terminal_publish_ledger`). Strictly additive.
5. Re-exports in `forge.lifecycle_bridge.__init__` (or direct submodule imports in `_serve_production.py` — either works) for `LifecycleBridgeWireup`, `StreamEventTranslator`, `langgraph_stream_source`, `TerminalPublishLedger`.
6. Composition in `bind_production_serve`: build the SQLite-bound pieces (registry, bridge, translator, identity-provider, stream-source, ledger) and thread them into `bind_production_dispatch_chain`. The wireup itself is finalised inside the closure where the publisher lives.

**Items 1, 2, 3 are each their own ~1-task-deep adjacent fixes**, but each is small enough (≤50 lines code + ~50 lines tests apiece) that bundling all six items into a single implementation task is reasonable — total estimate ~120 minutes implementation, not the original ~60. The estimate in this review's frontmatter (`estimated_implementation_minutes_after_review: 60`) should be revised to **120** in the spawned task.

### Seam test spec (AC-5)

**File**: `tests/forge/test_cli_serve_production.py` (existing — extend, do not create a new file). Add a new top-level class `TestLifecycleBridgeWireupComposition` (or `TestPEBRWireupRegression`) below the existing `TestF010JBindProductionServeThreadsAutobuildRunnerUrl` class. Mirrors the AAA/class-per-AC pattern at lines 142+ of that file (TASK-FIX-F010 precedent).

**Two complementary tests are required** — the threading-capture test mirrors F010's `test_bind_production_serve_threads_async_task_starter` (lines 440-512); the boot-log test exercises the operator-meaningful failure shape the 2026-05-08 walkthrough captured.

**Test 1 — threading-capture (mirrors `TestAsyncTaskStarterThreading`)**:

```python
class TestLifecycleBridgeWireupComposition:
    """Gap PEBR-WIREUP regression-protection seam test.

    Pinned by the 2026-05-08 jarvis runbook walkthrough on GB10
    (correlation_id=5673965b-e302-4a10-89cb-ceb430e64995). Before the
    fix, `bind_production_serve` did not compose any LifecycleBridge /
    LifecycleBridgeWireup / TerminalPublishLedger; the daemon's deps
    composer logged `ack_bridge=deferred (TASK-FRR-PEB-002), terminal_publish_ledger=deferred (TASK-FRR-PEB-005)` on every boot,
    no outbound lifecycle envelopes reached JetStream, and the
    inbound build-queued message was redelivered every 30s without
    ever being acked. See TASK-REV-PEBR-003 for the full diagnosis.
    """

    def test_bind_production_serve_threads_register_ack_handle_and_terminal_publish_ledger(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_config,
        fake_forge_config,
    ) -> None:
        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        monkeypatch.setattr(
            serve_module,
            "_build_async_subagent_middleware",
            lambda **kw: _FakeMiddleware(tool_names=("start_async_task",)),
        )
        monkeypatch.setattr(
            serve_production,
            "connect_writer",
            lambda db_path: MagicMock(spec=sqlite3.Connection),
        )
        monkeypatch.setattr(
            serve_production,
            "SqliteLifecyclePersistence",
            lambda **kw: MagicMock(name="pool"),
        )

        captured: dict[str, Any] = {}

        def _capture(**kwargs: Any):
            captured.update(kwargs)
            return lambda client: None

        monkeypatch.setattr(
            serve_module, "bind_production_dispatch_chain", _capture
        )

        serve_production.bind_production_serve(serve_config, fake_forge_config)

        # Gap PEBR-WIREUP: register_ack_handle and terminal_publish_ledger
        # MUST be threaded through to bind_production_dispatch_chain.
        # Before the fix neither kwarg was present in the call; both
        # defaulted to None inside build_pipeline_consumer_deps and the
        # daemon logged "deferred (TASK-FRR-PEB-002)" /
        # "deferred (TASK-FRR-PEB-005)" on every boot.
        assert captured.get("register_ack_handle") is not None, (
            "Gap PEBR-WIREUP regression: register_ack_handle is None on "
            "the production composition path; the inbound "
            "pipeline.build-queued.* envelope is never acked and JetStream "
            "redelivers every 30s. See TASK-REV-PEBR-003."
        )
        assert captured.get("terminal_publish_ledger") is not None, (
            "Gap PEBR-WIREUP regression: terminal_publish_ledger is None on "
            "the production composition path; the F010F coexistence "
            "boundary cannot honour the first-wins terminal-publish "
            "invariant when the bridge is unwired. See TASK-REV-PEBR-003."
        )
```

**Test 2 — boot-log assertion (operator-meaningful, drives the closure)**:

```python
    @pytest.mark.asyncio
    async def test_bind_production_serve_logs_wired_not_deferred(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        serve_config,
        fake_forge_config,
    ) -> None:
        """Operator-meaningful seam — drives the closure end-to-end.

        Reproduces the *exact* failure shape from the 2026-05-08
        runbook walkthrough: the boot log line emitted by
        :func:`forge.cli._serve_deps.build_pipeline_consumer_deps`
        (lines 558-568) MUST report `ack_bridge=wired` and
        `terminal_publish_ledger=wired` after `bind_production_serve`
        runs against a production-shaped ServeConfig and the rebound
        composer is invoked with a fake NATS client.
        """
        import logging

        from forge.cli import _serve_production as serve_production
        from forge.cli import serve as serve_module

        # Real wiring on the SQLite-bound side (no monkey-patches that
        # short-circuit the wireup parts construction). The publisher
        # construction inside the closure does need a fake NATS client.
        fake_nats_client = MagicMock(name="fake_nats_client")
        # Patch build_publisher_and_emitter to avoid hitting nats-py:
        # we care about the boot-log line, not the publisher's
        # internals.
        from forge.cli import _serve_deps_lifecycle
        monkeypatch.setattr(
            _serve_deps_lifecycle,
            "build_publisher_and_emitter",
            lambda client, *, config: (MagicMock(name="publisher"), MagicMock(name="emitter")),
        )

        with caplog.at_level(logging.INFO, logger="forge.cli._serve_deps"):
            serve_production.bind_production_serve(serve_config, fake_forge_config)
            # Drive the rebound composer with the fake client so
            # build_pipeline_consumer_deps fires and emits its log line.
            await serve_module.compose_dispatch_chain(fake_nats_client)

        log_text = "\n".join(record.getMessage() for record in caplog.records)

        assert "deferred (TASK-FRR-PEB-002)" not in log_text, (
            "Gap PEBR-WIREUP regression: ack_bridge is not wired by "
            "bind_production_serve (boot log says 'deferred (TASK-FRR-PEB-002)')."
        )
        assert "deferred (TASK-FRR-PEB-005)" not in log_text, (
            "Gap PEBR-WIREUP regression: terminal_publish_ledger is not "
            "wired by bind_production_serve (boot log says 'deferred (TASK-FRR-PEB-005)')."
        )
        assert "ack_bridge=wired" in log_text
        assert "terminal_publish_ledger=wired" in log_text
```

**Precedent**: TASK-FIX-F010's seam tests (`tests/forge/test_cli_serve_production.py`) use the same `monkeypatch.setattr(serve_module, "bind_production_dispatch_chain", _capture)` pattern at lines 480-486 to assert threading invariants. The two new tests above sit alongside `TestF010JBindProductionServeThreadsAutobuildRunnerUrl` as the symmetric one-layer-deeper regression lock.

### Spawned task (AC-6)

**Path**: `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-fix.md`

**Id**: `TASK-FORGE-FRR-PEBR-WIREUP`

**Title**: Fix Gap PEBR-WIREUP — compose `LifecycleBridgeWireup` in `bind_production_serve` (Option B helper-factory shape)

**Frontmatter highlights**:
- `task_type: fix`
- `parent_review: TASK-REV-PEBR-003`
- `parent_feature: FEAT-PEBR`
- `wave: 1`, `implementation_mode: task-work`
- `complexity: 5`, `estimated_minutes: 120`
- `dependencies`: none in-flight (FEAT-PEBR is archived; this is a cleanup fix)
- `tags`: forge-serve, lifecycle-bridge, wire-up, regression-protection, feat-pebr, gap-pebr-wireup, first-real-run-followup

**Body** ships eight ACs covering: helper factory authored in `_serve_production.py`; `langgraph_stream_source` + production `IdentityProvider` factories shipped; `coexistence.apply_migration` invoked at boot; `bind_production_dispatch_chain` signature additively extended; existing tests (`tests/forge/test_cli_serve_production.py`, smoke tests) all green; new seam tests per AC-5 added; ruff/black clean; `nats sub "pipeline.>"` walkthrough on rebuilt image confirms the canonical lifecycle sequence reaches JetStream.

See the file body for the full AC list, files-to-modify, files-to-create, test invocation commands, and the runbook revalidation step.

## Decision (AC-4)

- [ ] Option A — inline wiring in `bind_production_serve`
- [x] **Option B — helper factory `_build_lifecycle_bridge_wireup_parts(...)` (mirrors `_resolve_async_task_starter` pattern)**
- [ ] Option C — move wiring up into `bind_production_dispatch_chain` (changes public seam)

**Rationale**:

1. **Mirrors the existing F010E precedent.** `_serve_production.py` already hosts a sibling helper at line 127, `_resolve_async_task_starter(middleware) -> AsyncTaskStarter`, with its own unit-test surface (`TestAsyncTaskStarterThreading` at `test_cli_serve_production.py:440-512`). A new `_build_lifecycle_bridge_wireup_parts(*, sqlite_pool, runner_url) -> WireupParts` is the symmetric next move — same module, same TestClass-per-AC test pattern, same captured-kwargs threading-assertion shape (test 1 of AC-5). The codebase is already shaped for this; Option A would invert the module's existing decomposition pattern, and Option C would push composition into `bind_production_dispatch_chain` whose current 30-line scope is precisely "thread three deps into `build_pipeline_consumer_deps` and rebind the daemon's dispatch seam" — adding bridge / translator / registry / ledger / identity-provider / stream-source construction inside it would multiply its responsibilities five-fold.

2. **Smaller blast radius than C.** The wiring map (AC-3) showed that any of A / B / C must additively extend `bind_production_dispatch_chain`'s signature with optional kwargs threading the bridge wireup parts and the ledger down to `build_pipeline_consumer_deps`. Option B keeps that signature change strictly additive (default-`None` kwargs); Option C would require `bind_production_dispatch_chain` to *construct* the wireup parts itself, which means it needs `ServeConfig.autobuild_runner_url` (a fourth required arg) and a writer connection independent of the closure's NATS client. The brief's "Likely breaks PEB-002 / PEB-005 unit tests" caveat for Option C is accurate enough — even if those tests live at `build_pipeline_consumer_deps` rather than at `bind_production_dispatch_chain`, pushing composition further up forces a refactor of the seam they exercise.

3. **Better testability than A.** The helper's return value (a `WireupParts` dataclass or `tuple` of constructed pieces) is the natural unit-test surface for the missing factories `langgraph_stream_source` and the production `IdentityProvider` (AC-3 fix-scope expansions). With Option A those factories would be tested only via the bind_production_serve composition test; with Option B each can have focused per-helper coverage that doesn't have to monkey-patch the entire bind_production_serve graph.

4. **Preserves the wireup.py docstring contract.** Line 277 names `bind_production_serve` as the canonical composition site. Option B keeps that contract literal — bind_production_serve calls the helper, gets the parts, threads them down — without forcing a rewrite of the docstring to point at `bind_production_dispatch_chain` (which Option C would require).

5. **Acknowledged trade-off.** The wireup *itself* (`LifecycleBridgeWireup` instance) cannot be constructed in `bind_production_serve` because it requires the `PipelinePublisher` which doesn't exist until the NATS client opens inside the closure. Option B accepts this asymmetry: the helper builds the SQLite-bound parts (`bridge`, `translator`, `registry`, `identity_provider`, `stream_source`, `ledger`); `bind_production_dispatch_chain`'s closure constructs the publisher and finalises `LifecycleBridgeWireup(bridge=…, translator=…, publisher=publisher, stream_source=…, identity_provider=…)` from the threaded parts; the `register_ack_handle` callable on the resulting wireup is then threaded into `build_pipeline_consumer_deps`. This split is internally consistent and mirrors how `async_task_starter` is resolved early but the dispatcher closure is built late.

**Estimated implementation effort**: 120 minutes (revised up from 60 in the review's original frontmatter). Three discrete sub-deliverables expand scope:
- `langgraph_stream_source(runner_url=…)` factory (~30-50 LoC + tests)
- production `IdentityProvider` factory reading from `async_tasks` mirror (~20-30 LoC + tests)
- the helper itself + `bind_production_serve` composition + `bind_production_dispatch_chain` signature change + `coexistence.apply_migration` boot call + two seam tests (~80-100 LoC + tests)

## Why this is one-task-deep

Same shape as **TASK-FIX-F010** (the original `serve_cmd` not rebinding `compose_dispatch_chain` to `bind_production_dispatch_chain`). That fix was a single-line rebind plus a seam test; this is one layer deeper but structurally identical — the implementation modules exist, the deps composer accepts the parameters, the production wrapper just doesn't invoke the composition.

Once Gap PEBR-WIREUP closes:

1. `forge serve` will compose `LifecycleBridgeWireup` at boot
2. `register_ack_handle` will fire when the per-build SSE observer attaches
3. `pipeline.build-started.*` will publish on first SSE event from the sidecar
4. `pipeline.stage-complete.*` will publish per stage transition
5. `pipeline.build-complete.*` (or `pipeline.build-failed.*`) will publish on terminal SSE
6. The deferred-ack will fire on terminal envelope arrival
7. JetStream will stop redelivering the inbound `build-queued` envelope
8. The chat REPL's `forge_subscriber` (already binding the disjoint 4-subject filter post-F010Db) will render the lifecycle sequence between prompts

That closes the canonical Phase 7 happy-path criterion of `RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` and unblocks the deferred MacBook-over-Tailscale walkthrough.

## Estimated effort

- **Review (this task):** ~30–45 minutes
  - 5 min: confirm diagnosis (read 2 source files + 1 boot log)
  - 10 min: audit PEB-013 (read test file + locate composition site)
  - 10 min: map missing wiring (read `wireup.py` + check exports + check PEB-005 stream-source factory)
  - 10 min: pick fix shape with rationale
  - 5 min: write implementation task spec
- **Implementation (follow-up `/task-work`):** ~60 minutes
  - Single function change in `bind_production_serve` (Option A or B) or a small refactor (Option C)
  - 1 seam test
  - Rebuild image, rerun the canonical jarvis runbook, capture new RESULTS file confirming Phase 7 closes

## Validated Boundary Trace (revision 2026-05-08T09:00Z)

After [R]evise feedback at the Phase 5 checkpoint, traced the execution flow across every system / technology boundary using the on-disk evidence under `/tmp/runbook-evidence-2026-05-08/` and the jarvis runbook RESULTS file at `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08.md`. The boundary trace **independently corroborates** every finding in the AC-1 / AC-2 / AC-3 sections above and surfaces **one additional architectural concern** (run_id resolution) that the spawned implementation task must address.

### C4 Container Diagram — process boundaries during the failed run

```mermaid
graph LR
    subgraph "User session (terminal)"
        J["jarvis chat REPL<br/>HEAD ca2ba6b"]
    end
    subgraph "GB10 host network"
        N["NATS server (ships-computer-nats)<br/>JetStream stream PIPELINE<br/>workqueue retention"]
        F["forge serve daemon<br/>(Docker, host-network, image rebuilt 2026-05-08 06:51)<br/>HEAD e50241e"]
        S["langgraph-runner sidecar<br/>(host process via langgraph dev)<br/>port 8124"]
        L["llama-swap (qwen36-workhorse)<br/>port 9000"]
    end
    subgraph "Persistence"
        DB[(SQLite: /home/forge/.forge/forge.db<br/>tables: builds, async_tasks, stage_log,<br/>schema_version, sqlite_sequence)]
    end
    J  -- "B1: pipeline.build-queued.FEAT-43DE<br/>(NATS publish)" --> N
    N  -- "B2: durable consumer 'forge-serve' fetch<br/>filter pipeline.build-queued.*" --> F
    F  -- "B3: dispatch_build (in-process)" --> F
    F  -- "B5a: POST /threads HTTP/1.1" --> S
    F  -- "B5b: POST /threads/{tid}/runs HTTP/1.1" --> S
    S  -- "12× POST /v1/responses<br/>(autobuild work)" --> L
    F  -- "B7 (NEVER OPENED): SSE GET<br/>/threads/{tid}/runs/{rid}/stream" -.-> S
    F  -- "B9 (NEVER FIRES): pipeline.build-started.*<br/>pipeline.stage-complete.*<br/>pipeline.build-complete.*" -.-> N
    N  -- "B10 (NEVER FIRES): jarvis_forge_subscriber" -.-> J
    F  --- DB
    style F fill:#fee,stroke:#c33,stroke-width:2px
    classDef failed stroke-dasharray: 5 5,stroke:#c33,stroke-width:2px,fill:#fee;
```

Solid arrows are exercised in the captured evidence; **dotted arrows are the structurally-unreachable boundaries** (B7, B9, B10) blocked by the wireup gap inside container `F`.

### C4 Sequence Diagram — observed failure mode (rerun on 2026-05-08, correlation_id 5673965b-e302-4a10-89cb-ceb430e64995)

```mermaid
sequenceDiagram
    autonumber
    participant J as jarvis REPL
    participant N as NATS / JetStream<br/>(stream PIPELINE)
    participant F as forge serve daemon<br/>(_serve_production /<br/>_serve_deps /<br/>pipeline_consumer)
    participant W as LifecycleBridgeWireup<br/>(NEVER COMPOSED)
    participant S as langgraph-runner<br/>sidecar (port 8124)
    participant L as llama-swap

    Note over F: T0 = 05:54:02 — daemon boot
    F->>F: bind_production_serve(config, forge_config)
    F->>F: apply_at_boot → 2 SQLite migrations
    F->>F: bind_production_dispatch_chain(<br/>forge_config, sqlite_pool, async_task_starter,<br/>register_ack_handle=None ❌,<br/>terminal_publish_ledger=None ❌)
    Note right of F: Boot log: "composed PipelineConsumerDeps<br/>(async_task_starter=wired,<br/>ack_bridge=deferred (TASK-FRR-PEB-002),<br/>terminal_publish_ledger=deferred (TASK-FRR-PEB-005))"
    F->>F: dispatch_payload rebound

    Note over J,F: T1 = 05:58:23 — user issues queue_build
    J->>N: B1: PUB pipeline.build-queued.FEAT-43DE<br/>(BuildQueuedPayload, correlation_id=5673965b)
    Note right of N: phase7-pipeline-tail.log line 1<br/>(only line captured in 4 min)

    N->>F: B2: deliver Msg via durable forge-serve<br/>filter=pipeline.build-queued.*
    F->>F: pipeline_consumer.handle_message →<br/>"dispatching build feature_id=FEAT-43DE<br/>correlation_id=5673965b<br/>originating_adapter=terminal<br/><strong>bridge=fallback</strong>"
    rect rgb(255, 230, 230)
        Note over F,W: B4 — DEAD BOUNDARY ❌<br/>deps.register_ack_handle is None →<br/>line 519 of pipeline_consumer.py skipped →<br/>NO observer task ever scheduled →<br/>NO SSE stream will be opened
    end
    F->>F: dispatch_build → SQLite INSERT<br/>builds row (build-FEAT-43DE-20260508055823)
    F->>S: B5a: POST /threads → 200 OK<br/>thread_id=019e062a-6b8a-71a3-876a-d27e73a27b74
    F->>S: B5b: POST /threads/{tid}/runs → 200 OK<br/>run_id=019e062a-6b8c-7be0-986c-ce9243734e22
    Note right of F: dispatch_autobuild_async:<br/>launched task_id=019e062a-6b8a-71a3-876a-d27e73a27b74<br/>(task_id == thread_id; run_id NOT captured)
    F->>F: SQLite INSERT async_tasks row<br/>(task_id, build_id, feature_id, correlation_id)<br/>⚠ no run_id column

    Note over S,L: T2 → T3 (37s) — autobuild executes
    S->>L: 12× POST /v1/responses (qwen36-workhorse)
    L-->>S: 12× 200 OK
    Note right of S: T3 = 05:59:01<br/>"Background run succeeded<br/>run_completed_in_ms=37179"

    rect rgb(255, 230, 230)
        Note over F,S: B7 — NEVER OPENED ❌<br/>LifecycleBridgeWireup._observer_loop<br/>was never scheduled at B4, so<br/>langgraph_sdk.client.runs.join_stream(...)<br/>is never called.<br/>The sidecar's SSE events fire into the void.
    end
    rect rgb(255, 230, 230)
        Note over F,N: B9 — NEVER FIRES ❌<br/>StreamEventTranslator never invoked →<br/>PipelinePublisher.publish_build_started/<br/>publish_stage_complete/<br/>publish_build_complete never called.<br/>JetStream sees zero outbound envelopes.
    end

    Note over N: T4 = 05:58:53 onwards — JetStream redelivery<br/>(ack_wait=30s expired without ack)
    loop every ~30s, indefinitely
        N->>F: redeliver Msg
        F->>F: dispatch_build →<br/>"duplicate active build for feature_id=FEAT-43DE;<br/>skipping dispatch"
        Note right of F: phase7-forge-logs.log lines 10-15
    end
    Note over N: phase7-consumer-info.json:<br/>delivered=7277, redelivered=2,<br/>ack_floor=11

    rect rgb(255, 230, 230)
        Note over J,N: B10 — NEVER FIRES ❌<br/>jarvis forge_subscriber bound to<br/>[pipeline.build-started.>, stage-complete.>,<br/>build-complete.>, build-failed.>]<br/>with correlation_cap=1000 — but<br/>nothing arrives. Chat REPL drains zero<br/>notifications between prompts.
    end
```

**Boundaries traced**:

| ID | Layer / Tech | Status | Evidence |
|---|---|---|---|
| B1 | jarvis → NATS (Python `nats-py` PUB) | ✅ exercised | `phase7-pipeline-tail.log` line 1 — full envelope JSON captured verbatim |
| B2 | NATS → forge (`nats-py` durable consumer fetch, filter `pipeline.build-queued.*`) | ✅ exercised | `phase2.2-forge-logs.log:10-14`; `phase7-forge-logs.log:3` |
| B3 | forge in-process (`pipeline_consumer.handle_message` → `_serve_dispatcher.make_handle_message_dispatcher` → `_serve_deps.dispatch_build`) | ✅ exercised | `phase7-forge-logs.log:4` "persisted QUEUED row" |
| **B4** | forge in-process (`deps.register_ack_handle(...)`) | ❌ **DEAD** | `pipeline_consumer.py:519` `if deps.register_ack_handle is not None` — guard is `False`. Tertiary signal: `bridge=fallback` log annotation at `pipeline_consumer.py:545` (`"wired" if deps.register_ack_handle is not None else "fallback"`). Captured at `phase7-forge-logs.log:3,5,8,10,12,14`. |
| B5a | forge → langgraph-runner sidecar (`httpx` POST /threads) | ✅ exercised | `phase7-forge-logs.log:5` "POST http://localhost:8124/threads HTTP/1.1 200 OK" |
| B5b | forge → langgraph-runner sidecar (`httpx` POST /threads/{tid}/runs) | ✅ exercised | `phase7-forge-logs.log:6` |
| B6 | langgraph-runner → llama-swap (in-sidecar HTTP) | ✅ exercised | `sidecar.log` lines 35-43 (run 1) + lines 55-64 (run 2) — 9 + 12 calls to `/v1/responses` |
| **B7** | langgraph-runner → forge SSE GET /threads/{tid}/runs/{rid}/stream (`langgraph_sdk.client.runs.join_stream`) | ❌ **NEVER OPENED** | No `httpx` GET to `/threads/.../runs/.../stream` in `phase7-forge-logs.log`. Structural consequence of B4. |
| B8 | forge in-process (`StreamEventTranslator.translate(StreamPart) → PipelineEvent`) | ❌ never invoked | Translator instance never constructed (its constructor would log; no log line). Structural consequence of B7. |
| **B9** | forge → NATS (`PipelinePublisher.publish_*` for `build-started` / `stage-complete` / `build-complete`) | ❌ **NEVER FIRES** | `phase7-pipeline-tail.log` total length: **1 line** (the inbound envelope only). Subscription ran 4 minutes. |
| B10 | NATS → jarvis (lifecycle subscriber, F010Db disjoint filter) | ⏸ blocked | jarvis chat transcript: zero notification drains between prompts. RESULTS § Phase 7.1 confirms: "the chat REPL drained zero notifications". |

### Why this is the same shape as TASK-FIX-F010, one layer deeper

| Layer | TASK-FIX-F010 (closed 2026-05-04) | Gap PEBR-WIREUP (this fix) |
|---|---|---|
| Symptom | Inbound `pipeline.build-queued.*` was acked-and-discarded by `_default_dispatch` receipt-only stub | Inbound `pipeline.build-queued.*` is dispatched correctly but **never acked**; sidecar runs but no outbound envelopes |
| Failed boundary | `_serve_daemon.dispatch_payload` left at `_default_dispatch` (B3 in this trace's nomenclature) | `deps.register_ack_handle` left at `None` (B4 in this trace's nomenclature) |
| Root cause | `serve_cmd` did not call `bind_production_dispatch_chain(...)` | `bind_production_serve` does not compose `LifecycleBridgeWireup(...)` |
| Fix shape | New module `_serve_production.py` exposes `bind_production_serve(config, forge_config)` invoked from `serve_cmd` | New helper in `_serve_production.py` constructs `LifecycleBridgeWireupParts` and threads them into `bind_production_dispatch_chain` |
| Regression test | `tests/forge/test_cli_serve_production.py` (15 tests, F010 + F010J) | Same file extended with `TestLifecycleBridgeWireupComposition` (2 tests) |

The structural symmetry is the strongest validation that **(a) the diagnosis is correct** and **(b) the proposed fix shape is correctly scoped**.

### Additional architectural concern surfaced by the boundary trace — `run_id` capture gap

The wiring map at AC-3 above flagged that the production `IdentityProvider` must read `(thread_id, run_id)` from the `async_tasks` SQLite mirror. **Tracing the dispatcher's actual writes shows the schema is one column short**:

The `async_tasks` table (DDL at `src/forge/cli/_serve_deps_state_channel.py:284-308`) has columns:
```
task_id, build_id, feature_id, correlation_id,
lifecycle, wave_index, task_index,
started_at, last_activity_at
```

There is **no `run_id` column**. The dispatcher writes `task_id == thread_id` (per `_serve_async_task_starter.py:148-149`: *"the middleware tool returns a Command whose update.async_tasks contains exactly one entry — the just-launched task keyed by its thread_id"*). The `run_id` is minted by the sidecar's `POST /threads/{thread_id}/runs` response (visible in `phase7-forge-logs.log:6`: `019e062a-6b8c-7be0-986c-ce9243734e22`) and is **discarded by forge** — only the thread_id is captured downstream.

**This means the production `IdentityProvider` cannot return `(thread_id, run_id)` from a single SQLite read** — the run_id simply isn't there. Two viable paths for the implementation task to choose between:

1. **Resolve `run_id` via langgraph_sdk at observer-attach time** — `IdentityProvider._provider(feature_id)` reads `task_id` (=thread_id) from `async_tasks`, then calls `langgraph_sdk.client.LangGraphClient(url=runner_url).runs.list(thread_id=task_id, limit=1)` (or equivalent — verify the SDK 0.8.5 API surface) and returns `(thread_id, latest_run.run_id)`. One HTTP round-trip per identity poll. **Recommended** — no schema migration, no dispatcher refactor.

2. **Capture run_id at dispatch time** — modify the `_StructuredToolAsyncTaskStarter` adapter at `_serve_async_task_starter.py:143-202` to extract run_id from the middleware Command (if it's available there) OR add a `langgraph_sdk` round-trip after `astart_async_task` returns. Add `run_id TEXT` column to `async_tasks` with a new SQLite migration. Larger change; mostly relevant if observer-attach latency budget is tight (DDR-007 sets the per-poll budget at ~1s, well above an HTTP fetch).

**Recommendation: path (1)**. Reflected in the spawned implementation task's AC-3 (ships a production `IdentityProvider` factory that does the run_id lookup via langgraph_sdk).

### C4 Sequence Diagram — expected post-fix flow

```mermaid
sequenceDiagram
    autonumber
    participant J as jarvis REPL
    participant N as NATS / JetStream
    participant F as forge serve daemon
    participant W as LifecycleBridgeWireup<br/>(now composed)
    participant B as LifecycleBridge +<br/>BridgeRegistry +<br/>TerminalPublishLedger<br/>(SQLite-backed)
    participant T as StreamEventTranslator
    participant P as PipelinePublisher
    participant S as langgraph-runner sidecar
    participant L as llama-swap

    Note over F: Boot — bind_production_serve(config, forge_config)
    F->>F: Step 3.5: apply_at_boot + coexistence.apply_migration<br/>(adds lifecycle_bridge_terminal_publishes table)
    F->>B: Step 6.5: _build_lifecycle_bridge_wireup_parts(...)<br/>returns LifecycleBridgeWireupParts<br/>(bridge, translator, stream_source, identity_provider, ledger)
    F->>F: bind_production_dispatch_chain(<br/>..., bridge_wireup_parts=parts)
    Note right of F: Boot log: "composed PipelineConsumerDeps<br/>(async_task_starter=wired,<br/><strong>ack_bridge=wired</strong>,<br/><strong>terminal_publish_ledger=wired</strong>)"

    J->>N: B1: PUB pipeline.build-queued.FEAT-XXX
    N->>F: B2: deliver Msg
    F->>F: dispatch log: "...<strong>bridge=wired</strong>"

    rect rgb(230, 255, 230)
        Note over F,W: B4 — register_ack_handle now non-None
        F->>W: deps.register_ack_handle(feature_id, correlation_id, ack_handle)
        W->>B: bridge.attach(BuildContext(feature_id, "pending-{fid}", "pending-{fid}", correlation_id, deadline_at))
        B->>B: BridgeRegistry.record(...)<br/>+ start 300s deadline timer
        W->>W: asyncio.create_task(_observer_loop(...))<br/>(named lifecycle-bridge-observer-{feature_id})
    end

    F->>F: dispatch_build → SQLite INSERT builds + async_tasks
    F->>S: B5a: POST /threads → 200 OK (thread_id)
    F->>S: B5b: POST /threads/{tid}/runs → 200 OK (run_id)
    Note right of F: dispatch_autobuild_async returns immediately;<br/>F010F sync-raise path NOT triggered

    par Sidecar runs autobuild
        S->>L: 12× POST /v1/responses
        L-->>S: 12× 200 OK
    and Wireup observer drives SSE
        rect rgb(230, 255, 230)
            Note over W,T: B7 — SSE stream now opened
            W->>W: _wait_for_identity(feature_id):<br/>identity_provider polls async_tasks → reads task_id<br/>(=thread_id), then resolves run_id via<br/>langgraph_sdk.runs.list(thread_id, limit=1)
            W->>S: GET /threads/{tid}/runs/{rid}/stream<br/>(SSE via langgraph_stream_source)
            loop For each StreamPart on SSE
                S-->>W: StreamPart event=values data={async_tasks: {...}}
                W->>T: translator.translate(stream_part, build_context)
                T-->>W: PipelineEvent (BuildStartedPayload / StageCompletePayload / ...)
                rect rgb(230, 255, 230)
                    Note over W,P: B9 — PipelinePublisher fires
                    W->>P: publish_build_started / publish_stage_complete (per-event)
                    P->>N: PUB pipeline.build-started.FEAT-XXX (correlation_id threaded)
                    P->>N: PUB pipeline.stage-complete.FEAT-XXX
                end
                N-->>J: B10: jarvis_forge_subscriber receives → renders chat line
            end
        end
    end

    Note over S: Background run succeeded
    S-->>W: terminal StreamPart (done)
    W->>T: translate → BuildCompletePayload
    W->>B: ledger.claim(feature_id, correlation_id, "bridge-terminal")
    B-->>W: True (first-wins)
    W->>P: publish_build_complete
    P->>N: PUB pipeline.build-complete.FEAT-XXX
    N-->>J: jarvis renders terminal chat line
    W->>W: handle.ack() → JetStream ack_floor advances
    W->>B: bridge.detach(feature_id, correlation_id)<br/>(cancels deadline timer, clears registry row)
```

The post-fix diagram makes the regression-test-target log line shape concrete: every `wired` annotation in the diagram corresponds to an `assert ... in log_text` in the seam test (AC-5 above), and every dotted-arrow boundary in the *failure-mode* diagram corresponds to an `assert "deferred (...)" not in log_text` or `assert mock.called` regression assertion.

### Confidence: 100%

Every link in the failure chain is independently corroborated by:
- **Source code** (8 files read in full or in detail);
- **Boot-time daemon log** (`phase2.2-forge-logs.log`);
- **Runtime daemon log during dispatch + redelivery** (`phase7-forge-logs.log`);
- **Wire capture** (`phase7-pipeline-tail.log` — 1 line, 4-minute capture);
- **Consumer state snapshot** (`phase7-consumer-info.json`);
- **Sidecar log including LLM round-trips** (`sidecar.log`);
- **Independent operator-facing diagnostic line** (`bridge=fallback` annotation at `pipeline_consumer.py:545` — distinct from the deps composer's `deferred (...)` annotation);
- **Self-documenting docstring contract** (`wireup.py:277-283` names `bind_production_serve` as the canonical composition site).

The fix shape is also independently validated against the **TASK-FIX-F010 precedent** — same module, same test pattern, same one-line composition addition (modulo the wireup composition needing to span `bind_production_serve` SQLite-bound parts and `bind_production_dispatch_chain` closure-bound publisher).

## Test Execution Log

**2026-05-08T08:30Z — `/task-review TASK-REV-PEBR-003 --mode=gap-analysis` (claude-opus-4-7[1m])**

- AC-1 ✅ — Diagnosis confirmed by reading `_serve_production.py:168-321` and `_serve_deps.py:429-568`. Daemon-log "deferred" line is operator-facing proof.
- AC-2 ✅ — PEB-013 audited (`tests/integration/test_lifecycle_bridge_sidecar_e2e.py:613-617` monkey-patches `compose_dispatch_chain` and bypasses `bind_production_serve` entirely — confirmed test gap).
- AC-3 ✅ — Wiring map filed. **Two missing factories flagged**: `langgraph_stream_source(...)` and production `IdentityProvider`. `coexistence.apply_migration(connection)` not called at boot. `bind_production_dispatch_chain` signature must extend additively.
- AC-4 ✅ — **Option B chosen** (helper factory `_build_lifecycle_bridge_wireup_parts(...)` mirroring `_resolve_async_task_starter`). Rationale recorded in `## Decision`.
- AC-5 ✅ — Seam tests specified at `tests/forge/test_cli_serve_production.py::TestLifecycleBridgeWireupComposition`. Two complementary tests: threading-capture (mirrors F010 precedent) + boot-log assertion (operator-meaningful). Pre-fix: both fail. Post-fix: both pass.
- AC-6 ✅ — Implementation task spawned: `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FORGE-FRR-PEBR-WIREUP-fix.md`. 11 ACs, 3 files-to-create, 5 files-to-modify, ~120 min implementation effort (revised from initial 60 min estimate due to AC-3 fix-scope expansions).

**No code changes** were made by this review — analysis + decision + spawned task only, per the task's own contract ("No code changes in this task — analysis + decision + spawn implementation task only.").

---

**2026-05-08T09:00Z — `[R]evise` follow-on (claude-opus-4-7[1m])**

Operator requested a deeper trace across system / technology boundaries with C4 sequence diagrams, validated against the jarvis runbook RESULTS file at `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08.md` and the on-disk evidence under `/tmp/runbook-evidence-2026-05-08/`.

**Additional inputs read**:
- `RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08.md` (full file — 251 lines)
- `phase2.2-forge-logs.log` — 14 lines, daemon boot through first dispatch
- `phase7-pipeline-tail.log` — 1 line (the inbound BuildQueuedPayload envelope, full 4-min capture)
- `phase7-consumer-info.json` — JetStream consumer state (`delivered=7277, redelivered=2, ack_floor=11`)
- `phase7-forge-logs.log` — 15 lines spanning dispatch + redelivery loop
- `sidecar.log` — full langgraph-runner sidecar lifecycle including `Background run succeeded run_completed_in_ms=37179`
- Source: `forge/src/forge/adapters/nats/pipeline_consumer.py` (lines 490-565) — confirmed `bridge=fallback` annotation site at line 545
- Source: `forge/src/forge/cli/_serve_deps_state_channel.py` (lines 270-332) — `async_tasks` DDL inspected; **run_id column missing**
- Source: `forge/src/forge/cli/_serve_async_task_starter.py` (lines 100-202) — confirmed `task_id == thread_id` per the middleware Command's `update.async_tasks` mapping

**New findings vs the original review**:
1. ✅ Added "Validated Boundary Trace" section with full B1→B10 boundary table, C4 container diagram, and two C4 sequence diagrams (failure mode + post-fix flow) in Mermaid.
2. 🆕 **Schema gap surfaced**: `async_tasks.run_id` does not exist; the dispatcher discards run_id from the langgraph-runner POST response. **Decision**: hybrid resolution — SQLite read for thread_id + `langgraph_sdk.client.runs.list(thread_id=…, limit=1)` for run_id. No schema migration needed.
3. 🆕 **Tertiary regression signal identified**: `pipeline_consumer.py:545` emits `bridge=wired|fallback` per dispatch — independent of the deps composer's `deferred (...)` boot-log line. This gives the seam test a third assertion lever (the per-dispatch log line) on top of the threading-capture and boot-log assertions.
4. ✅ Spawned implementation task `TASK-FORGE-FRR-PEBR-WIREUP` updated to reflect the schema-gap decision and the new `_build_async_tasks_identity_provider(*, sqlite_pool, autobuild_runner_url)` signature.

**Confidence after revision**: 100%. Every link in the failure chain is independently corroborated by source code, boot log, runtime log, wire capture, consumer state JSON, sidecar log, and the operator-facing `bridge=fallback` annotation. The fix shape is structurally symmetric with TASK-FIX-F010's precedent (one layer deeper, same module, same test pattern).

**No code changes** in this revision — analysis-only, per the task's contract.
