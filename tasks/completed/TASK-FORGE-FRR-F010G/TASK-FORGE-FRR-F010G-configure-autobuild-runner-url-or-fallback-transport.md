---
id: TASK-FORGE-FRR-F010G
title: "Configure autobuild_runner async subagent for ASGI transport (or fall back to in-process invocation when url=None)"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T00:00:00Z
completed: 2026-05-04T00:00:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010G/
priority: high
task_type: fix
tags:
  - forge-serve
  - async-subagent
  - autobuild-runner
  - asgi-transport
  - deployment-config
  - deepagents
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - last-mile
complexity: 4
estimated_minutes: 90
estimated_effort: "60-180 minutes (depends on Option A vs B; investigation step is mandatory)"
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-002        # autobuild_runner async subagent definition
  - TASK-FW10-008        # AsyncSubAgentMiddleware wiring
  - TASK-FORGE-FRR-F010E # StructuredTool->AsyncTaskStarter adapter; predecessor — the call boundary it bridges is correct, this gap is one layer deeper inside the launched coroutine
  - TASK-FORGE-FRR-F010F # safety-net publish path that surfaces this gap to the operator (rendered chat line includes the failure_reason in full)
correlation_id: db27f127-a863-4723-a4be-b8cbb68eab5a
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Final validation rerun late evening, post-F010Db/E/F. Phase 7 structural close achieved (chat REPL renders build-failed line with threaded correlation_id), but the happy-path build-started + stage-complete*N + build-complete sequence is blocked because autobuild_runner async subagent has url=None and AsyncSubAgentMiddleware's ASGI transport rejects None-url launches"
context_files:
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - src/forge/cli/_serve_production.py
  - src/forge/cli/serve.py
  - src/forge/pipeline/dispatchers/autobuild_async.py
  - tasks/completed/TASK-FW10-002-implement-autobuild-runner-async-subagent.md
  - tasks/completed/TASK-FW10-008-wire-async-subagent-middleware-into-supervisor.md
test_results:
  status: passed
  coverage: null
  last_run: 2026-05-04
  notes: "tests/forge/ + tests/cli/ + tests/integration/ + tests/unit/ all passing; pre-existing test_clock_hygiene failure on approval_subscriber.py:684 remains deselected per AC-7 (introduced 2026-05-02 in 41cba9c, unrelated to F010G); pre-existing slow docker test test_forge_serve_arfs_inside_image fails on missing python entrypoint, unrelated to F010G"
---

# Task: Configure `autobuild_runner` async subagent for ASGI transport (or fall back to in-process invocation when `url=None`)

## TL;DR

F010E's `_StructuredToolAsyncTaskStarter` adapter correctly bridges
the `AsyncTaskStarter` Protocol boundary between forge's autobuild
dispatcher and the LangChain `StructuredTool` returned by
`AsyncSubAgentMiddleware.tools`. F010F's safety-net publishes a
terminal `build-failed` envelope when the launched subagent fails.
The chat REPL now renders the failure between prompts (Phase 7
**structural close** achieved on 2026-05-04 late evening,
correlation_id `db27f127-a863-4723-a4be-b8cbb68eab5a`). The remaining
gap is **one layer deeper inside the launched coroutine**:
`deepagents.middleware.async_subagents` rejects every launch because
the `autobuild_runner` registration's `url` field is `None`, and the
middleware's sync ASGI transport requires a URL.

## Symptom (verbatim from RESULTS Addendum 3)

The user-visible chat REPL line:

```
[14:38] Forge FEAT-43DE: build-failed (RuntimeError: _StructuredToolAsyncTaskStarter: middleware tool returned launch failure: "Failed to launch async subagent 'autobuild_runner': Async subagent 'autobuild_runner' has no url configured. ASGI transport (url=None) requires async invocation.")
```

The forge daemon log line that produced it:

```
2026-05-04T13:38:56 [WARNING] deepagents.middleware.async_subagents:
  Failed to launch async subagent 'autobuild_runner':
  Async subagent 'autobuild_runner' has no url configured.
  ASGI transport (url=None) requires async invocation.
```

The wire envelope (proves F010F + F010C work):

```json
{"source_id":"forge","event_type":"build_failed",
 "correlation_id":"db27f127-a863-4723-a4be-b8cbb68eab5a",
 "payload":{"feature_id":"FEAT-43DE","build_id":"FEAT-43DE",
            "failure_reason":"RuntimeError: _StructuredToolAsyncTaskStarter: middleware tool returned launch failure: \"Failed to launch async subagent 'autobuild_runner': Async subagent 'autobuild_runner' has no url configured. ASGI transport (url=None) requires async invocation.\"",
            "recoverable":false,"failed_task_id":null}}
```

## Why

The `AsyncSubAgentMiddleware` shipped by `deepagents` (third-party
package — see `pyproject.toml:11` `deepagents>=0.5.3,<0.6`) launches
each named subagent by issuing an HTTP request to a configured URL
via the LangGraph SDK Agent Protocol client. The URL is per-subagent:
each `AsyncSubAgent` registration carries its own `url` field, and
the middleware's **sync** client cache fails fast on launch when that
field is `None` (see
`deepagents/middleware/async_subagents.py:239-244`):

```python
def get_sync(self, name: str) -> SyncLangGraphClient:
    spec = self._agents[name]
    if spec.get("url") is None:
        msg = (
            f"Async subagent '{name}' has no url configured. "
            f"ASGI transport (url=None) requires async invocation."
        )
        raise ValueError(msg)
    ...
```

In the forge production composer
(`forge.cli._serve_production.bind_production_serve` →
`forge.cli.serve._build_async_subagent_middleware` at line 262-299),
the `autobuild_runner` subagent is registered with **only** `name`,
`description`, and `graph_id` — **no `url`**:

```python
return AsyncSubAgentMiddleware(
    async_subagents=[
        {
            "name": AUTOBUILD_RUNNER_NAME,
            "description": (...),
            "graph_id": AUTOBUILD_RUNNER_NAME,
        }
    ],
)
```

Likely because the original FW10-002 / FW10-008 wiring assumed
in-process construction was sufficient and the URL wiring was
deferred to a later "deploy autobuild_runner as an ASGI graph" task
that never landed. Confirmed by grep across both task files: neither
mentions `url` configuration — FW10-002 only references "ASGI
co-deployment" once in the consumer-context preamble (line 19) and
FW10-008 not at all.

The `forge/langgraph.json` registers the `autobuild_runner` graph at
`./src/forge/subagents/autobuild_runner.py:graph` (alongside the main
`orchestrator` graph), so a `langgraph dev` / `langgraph deploy`
surface CAN address it — but `forge serve` today does not run any
ASGI surface alongside its NATS daemon, so there is no live URL the
registration could point at.

## Distinction from F010E

- **F010E** was about the **call boundary** between forge's
  autobuild dispatcher (which expected a Protocol-named
  `start_async_task` method) and the LangChain `StructuredTool`
  returned by `AsyncSubAgentMiddleware.tools` (which exposes
  `invoke()` instead). **Fixed** by wrapping the `StructuredTool` in
  a `_StructuredToolAsyncTaskStarter` adapter that satisfies the
  `AsyncTaskStarter` Protocol (commit `4438c47`).
- **F010G** is about what happens **inside** the launched coroutine
  after the adapter forwards the call:
  `deepagents.middleware.async_subagents` rejects the launch because
  the subagent's URL is `None`. F010E unblocked the call boundary;
  F010G is the next layer.

These are independent gaps; F010E's fix is correct and stays in
place. The F010E adapter's name appears verbatim in the F010G
failure_reason, confirming the boundary it bridges is being crossed
successfully.

## Investigation needed (the implementer's first task — mandatory)

This task body's option-comparison below is calibrated by the
investigation. Do **not** skip the investigation step — record findings
in §Implementation Notes before any production code lands:

1. **Where is `autobuild_runner` registered?** Grep `forge/src/` for
   `AUTOBUILD_RUNNER_NAME` (the constant defined in
   `forge.pipeline.dispatchers.autobuild_async`) and trace each call
   site. Identify:
   - The site that constructs the `AsyncSubAgent` registration object
     (currently `forge.cli.serve._build_async_subagent_middleware` at
     `serve.py:262-299`).
   - Whether the registration accepts a `url` argument (yes — see
     `deepagents/middleware/async_subagents.py:60-65`,
     `url: NotRequired[str]`).
2. **Is `deepagents` a third-party package or a local fork?** Already
   confirmed third-party at `pyproject.toml:11`
   (`deepagents>=0.5.3,<0.6`). **Option B (extend the middleware)
   becomes an upstream PR or a local monkeypatch**, not a same-repo
   edit. State the finding in the task body's Implementation Notes
   and decide accordingly.
3. **Does forge run a langgraph dev / langgraph deploy ASGI surface
   alongside `forge serve`?** Confirm by reading
   `src/forge/cli/_serve_daemon.py:_run_serve` and
   `src/forge/cli/_serve_healthz.py` — today's daemon group runs
   exactly two coroutines (the NATS consumer and the healthz HTTP
   surface). No ASGI surface today. `langgraph.json` exists at the
   forge repo root and registers `autobuild_runner` under
   `"./src/forge/subagents/autobuild_runner.py:graph"`, so the graph
   IS langgraph-addressable — but no daemon currently exposes it.
4. **Cross-reference FW10-002 + FW10-008 ACs.** Did either task
   explicitly defer the URL wiring? Grep both files for `url`,
   `ASGI`, `deploy`, `langgraph dev`. Findings: FW10-002 mentions
   "ASGI co-deployment" once in passing (line 19); FW10-008 does not
   reference URL configuration at all. **Conclusion: this URL wiring
   was implicitly deferred and never tracked as a follow-up.**
5. **Read the deepagents source for the launch site.** Confirmed:
   `_build_start_tool` at
   `deepagents/middleware/async_subagents.py:273-318` calls
   `clients.get_sync(subagent_type)` (the **sync** path), which fails
   on `url=None`. The **async** path (`_build_start_tool`'s
   `astart_async_task` at line 320-339) calls `clients.get_async()`
   which **does NOT have the url-None guard** — `get_async` at
   line 253-262 unconditionally calls `get_client(url=spec.get("url"))`,
   and the LangGraph SDK's `get_client(url=None)` falls back to the
   in-process ASGI transport per the docstring at line 60-65. This
   surfaces a **fourth implementation option** (Option C below) the
   task brief did not anticipate: switch the F010E adapter to invoke
   `tool.coroutine` (async path) instead of `tool.func` (sync path),
   and the url-None constraint dissolves without any URL wiring or
   middleware extension. Investigate whether this is viable given the
   dispatcher's sync call site at `autobuild_async.py:473`.

## The implementation options

**Option A — Configure a URL at boot.** The autobuild_runner is
exposed at a langgraph dev / langgraph deploy ASGI surface (or at a
new ASGI surface added to the forge daemon itself), and the
registration in `_build_async_subagent_middleware` declares the URL
pointing at that surface. The middleware then launches subagents over
HTTP via the configured Agent Protocol client.

Sub-options inside Option A:

- **A1: langgraph dev as a sidecar service.** Operator runs
  `langgraph dev` in a separate container/process; URL is
  `http://localhost:<langgraph-port>`. Requires deployment-config
  doc updates and probably a docker-compose addition.
- **A2: in-process ASGI surface inside `forge serve`.** Extend the
  forge daemon to expose its compiled subagent graphs at HTTP routes
  (e.g. `http://127.0.0.1:8088/subagents/autobuild_runner`). No
  separate process; one-binary deployment. Requires the daemon to
  grow an HTTP-app surface.
- **A3: external langgraph deploy.** Production deployment uses a
  hosted langgraph deployment; URL points at the deployment
  endpoint. Operator-config-only at the forge level.

**Option B — Extend `AsyncSubAgentMiddleware` to support `url=None`
fallback in the sync path.** When a subagent's URL is `None`, the
middleware falls back to direct in-process `astream`/`ainvoke`
against the subagent's compiled graph (referenced by `graph_id`). No
HTTP transport involved. This is the simplest deployment story (one
process, no ASGI surface), but it's a behavioural change to the
deepagents middleware itself — and since `deepagents>=0.5.3,<0.6` is
third-party (confirmed), this is **either an upstream PR** to
deepagents (slow) **or a local monkeypatch** in `forge.cli.serve`
(fast but a deprecation-cliff risk on the next deepagents bump).

**Option C — Switch the F010E adapter to the middleware's async path
(`tool.coroutine`).** The deepagents middleware's `astart_async_task`
async closure (line 320-339 of `async_subagents.py`) calls
`clients.get_async()` at line 253-262 — and `get_async()` has **no
url-None guard**. The LangGraph SDK's `get_client(url=None)` falls
back to the in-process ASGI transport per the docstring at line
60-65 ("Defaults to the LangGraph SDK's default endpoint. Omit to
use ASGI transport for local servers"). So the same registration
shape (`name` + `description` + `graph_id`, no `url`) **works
end-to-end through the async path**. Requires:

- Updating `_StructuredToolAsyncTaskStarter.start_async_task` (or
  adding an `astart_async_task` mirror) to call
  `await self._tool.coroutine(...)` instead of `self._tool.func(...)`.
- Resolving the Protocol-vs-async-call mismatch: the dispatcher's
  `AsyncTaskStarter.start_async_task` is declared sync at
  `autobuild_async.py:155-189`. Either (i) make the dispatcher call
  site `await`-shaped (one-line change at `autobuild_async.py:473`,
  since `dispatch_autobuild_async` is already `async def`), or
  (ii) bridge sync→async via `asyncio.run_coroutine_threadsafe` or
  similar (uglier, but keeps the Protocol shape).

**Pick based on the investigation findings.** Indicative
recommendations (validate during investigation):

- If Option C is viable (the dispatcher's call site can be made
  `await`-shaped without ripple) → **Option C is the minimum-deviation
  fix**. No deployment-shape change, no upstream PR, no monkeypatch
  — just a one-line adapter change plus a one-line dispatcher change,
  and the existing `_StructuredToolAsyncTaskStarter` adapter unit
  tests grow an `await` for free. **Most likely the right answer.**
- If Option C ripples too far (e.g. the Protocol's sync shape is
  load-bearing for some other caller) and `deepagents` cannot be
  monkeypatched safely → **Option A2 (in-process ASGI surface inside
  `forge serve`)** is the fallback: stays inside forge's repo and
  matches the "one daemon" deployment shape.
- If a langgraph-dev sidecar is already part of the deployment story
  → **Option A1** is cheapest.

## Acceptance Criteria

- [ ] **AC-1 (investigation)**: Document the investigation findings
  (deepagents type already confirmed third-party; langgraph surface
  presence; FW10-002/008 deferred-AC status; deepagents launch-site
  fallback presence — particularly the **Option C async-path
  finding**) in the task body's §Implementation Notes section
  before the fix lands.
- [ ] **AC-2 (decision)**: Document the chosen option (A1 / A2 / A3 /
  B / C) and rationale in §Implementation Notes. Reviewers should be
  able to verify the decision was made deliberately.
- [ ] **AC-3 (implementation)**: Implement the fix per AC-2's
  decision. The `autobuild_runner` async subagent launches
  end-to-end through the middleware without raising `Async subagent
  'autobuild_runner' has no url configured`.
- [ ] **AC-4 (integration test)**: An integration test drives
  `dispatch_build` end-to-end against the chosen surface and asserts
  that **at least one `pipeline.build-started.<feature_id>` envelope**
  is published on the wire (this is the first proof that the
  autobuild_runner actually launches). Use a mock autobuild that
  emits a scripted lifecycle sequence per the FW10-011 design pattern
  (the still-pending capstone integration test). Prefer extending an
  existing test file under `tests/forge/` over creating a new one;
  the closest precedent is
  `tests/forge/test_serve_async_task_starter.py` (F010E's adapter
  unit test).
- [ ] **AC-5 (operator runbook revalidation — pending)**: re-run
  jarvis runbook §6.2+§7. Expected outcome: chat REPL renders the
  **full lifecycle sequence** for a successful build:

  ```text
  [HH:MM] Forge FEAT-43DE: build-started (RUNNING)
  [HH:MM] Forge FEAT-43DE: stage <stage_label> (PASSED)
  [HH:MM] Forge FEAT-43DE: stage <stage_label> (PASSED)
  ...
  [HH:MM] Forge FEAT-43DE: build-complete (PASSED)
  ```

  All threaded by the same `correlation_id`, all drained between
  prompts. Capture the new correlation_id in the completion notes.
- [ ] **AC-6 (regression — F010F safety net)**: F010F's safety-net
  `build-failed` publish path continues to fire when ANY future
  dispatch / launch error happens (don't accidentally short-circuit
  it during the F010G fix). Existing dispatch-failure unit tests
  (`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`)
  pass unchanged.
- [ ] **AC-7 (regression — full suite)**: Full forge test suite
  (`pytest tests/forge/ tests/`) passes. Pre-existing
  `test_clock_hygiene` failure on `approval_subscriber.py:684`
  remains deselected (introduced 2026-05-02 in commit `41cba9c`,
  unrelated to F010G).

## Files Expected to Change

Conditional on the AC-2 decision:

**If AC-2 chooses Option A1 (langgraph dev sidecar):**
- `src/forge/cli/serve.py:_build_async_subagent_middleware` — add
  `url=` to autobuild_runner registration (e.g.
  `os.environ.get("FORGE_AUTOBUILD_RUNNER_URL")` with a documented
  default).
- `docker-compose.yml` (or operator runbook addendum) — add a
  `langgraph-dev` sidecar service.
- `langgraph.json` — confirm the autobuild_runner graph entry is
  reachable from the sidecar's import path.

**If AC-2 chooses Option A2 (in-process ASGI surface):**
- `src/forge/cli/serve.py:_build_async_subagent_middleware` — add
  `url="http://127.0.0.1:<port>"` (port read from
  `ServeConfig.subagent_app_port` or similar).
- New module `src/forge/cli/_serve_subagents_app.py` — ASGI app
  exposing the compiled subagent graphs over the LangGraph Agent
  Protocol.
- `src/forge/cli/_serve_daemon.py:_run_serve` — add a third
  coroutine to the `asyncio.wait` group for the ASGI app's HTTP
  server.
- `src/forge/cli/_serve_config.py:ServeConfig` — add a port field
  for the new surface.

**If AC-2 chooses Option B (extend middleware):**
- If accepting the upstream-PR latency: file an issue / PR against
  `deepagents` (https://github.com/langchain-ai/deepagents or
  wherever the canonical repo lives) for the `url=None` fallback in
  `_ClientCache.get_sync`. While that lands, optionally add a local
  monkeypatch in `src/forge/cli/serve.py:_build_async_subagent_middleware`
  + a deprecation-warning so this task isn't blocked indefinitely.
  Document the deprecation cliff: when deepagents bumps past
  `0.5.x`, audit the monkeypatch.

**If AC-2 chooses Option C (async-path adapter):**
- `src/forge/cli/_serve_async_task_starter.py` — change the
  `_StructuredToolAsyncTaskStarter.start_async_task` implementation
  to call `await self._tool.coroutine(...)` (or add a sibling
  `astart_async_task` if the Protocol grows an async mirror).
- `src/forge/pipeline/dispatchers/autobuild_async.py:473` —
  one-line change: `task_id = await async_task_starter.astart_async_task(...)`
  (the function is already `async def`).
- `src/forge/pipeline/dispatchers/autobuild_async.py:155-189` —
  Protocol declaration grows an `astart_async_task` async method (or
  the existing `start_async_task` is redeclared async, depending on
  whether any other caller needs the sync shape).
- `tests/forge/test_serve_async_task_starter.py` — existing 17
  tests grow `await` and async fixtures; add explicit assertion that
  `tool.coroutine` is invoked and the launched coroutine resolves
  with no URL configured.
- Tests under `tests/forge/` covering AC-4.

## Implementation Notes

### AC-1 — Investigation findings (recorded 2026-05-04)

1. **deepagents type:** confirmed third-party at
   `pyproject.toml:11` (`deepagents>=0.5.3,<0.6`); installed copy at
   `~/.local/lib/python3.12/site-packages/deepagents/middleware/async_subagents.py`.
   Option B (extend the middleware) would therefore be either an
   upstream PR or a local monkeypatch, not a same-repo edit.
2. **`_ClientCache.get_sync` (line 239-244)** explicitly raises
   `ValueError` on `url=None` — the F010G symptom on the wire. The
   forced-error message matches the failure_reason in the addendum-3
   envelope verbatim.
3. **`_ClientCache.get_async` (line 253-262)** does **not** carry
   the URL guard — it calls `get_client(url=spec.get("url"))`
   unconditionally. The LangGraph SDK's `get_client(url=None)` falls
   back to in-process ASGI transport per the `AsyncSubAgent.url`
   docstring at lines 60-65 ("Defaults to the LangGraph SDK's default
   endpoint. Omit to use ASGI transport for local servers"). This
   confirms the brief's Option C async-path finding.
4. **`_build_start_tool`** registers BOTH a sync `start_async_task`
   (line 280, calls `get_sync` — the failing path) and an async
   `astart_async_task` (line 320, calls `get_async` — the working
   path) on a single `StructuredTool` via
   `StructuredTool.from_function(func=…, coroutine=…)`. The adapter
   at `forge.cli._serve_async_task_starter._StructuredToolAsyncTaskStarter`
   was wrapping `tool.func` (the sync, broken path).
5. **Brief inaccuracy.** The brief Option C subnote (i) said
   `dispatch_autobuild_async` is "already `async def`". It is not —
   `src/forge/pipeline/dispatchers/autobuild_async.py:295` is `def`,
   not `async def`. Option C therefore requires extending the chain
   to async (a wider ripple than the brief's "one-line change"
   suggested), but the ripple is contained inside forge.
6. **No ASGI surface today.** `_run_serve` at
   `src/forge/cli/_serve_daemon.py` runs only the NATS consumer + the
   healthz HTTP coroutine; `langgraph.json` registers the
   `autobuild_runner` graph at
   `./src/forge/subagents/autobuild_runner.py:graph` but no daemon
   exposes it. Option A1/A2 would either require a sidecar deployment
   or growing the daemon a third coroutine.
7. **FW10-002 / FW10-008 deferral confirmed.** Neither task's
   acceptance criteria reference `url`, ASGI transport, or langgraph
   dev/deploy beyond passing mentions; the URL wiring was implicitly
   deferred and never tracked.

### AC-2 — Decision: Option C (async-path adapter, chain async)

**Chosen:** Option C — implement `astart_async_task` on the adapter
(awaits `tool.coroutine`); convert `dispatch_autobuild_async` to
`async def`; flip `AutobuildDispatcher = Callable[..., Awaitable[Any]]`
and add `await` at the supervisor's call site; make
`_make_autobuild_dispatcher_closure`'s closure `async def`.

**Why Option C over A/B:**
- **Option A (URL config):** every sub-option ripples beyond forge's
  internal call chain — A1 needs an out-of-process langgraph-dev
  sidecar (deployment-shape change), A2 grows the daemon a third
  coroutine plus a new ASGI app module, A3 hands deployment
  responsibility to a hosted langgraph deployment. None of these
  match the "one binary on a workstation" constraint the FRR rerun
  set up reflects, and all of them are larger changes than C.
- **Option B (monkeypatch):** carries deprecation-cliff risk on the
  next deepagents bump (`>=0.5.3,<0.6` is the current pin) and
  requires either an upstream PR (indeterminate latency) or a local
  monkeypatch with a TODO. The behavioural change to the deepagents
  middleware itself is not justified when an existing async path
  already does the right thing on the same registration shape.
- **Option C:** uses a deepagents feature (`get_async` ASGI fallback)
  that is explicitly documented and clearly intended for local
  servers. Self-contained inside forge: ~5 production files +
  per-test fake updates. The asymmetry between sync (URL-required)
  and async (URL-optional) paths in deepagents is a feature, not a
  bug — Option C aligns with the upstream design rather than working
  around it.

### AC-3 — Implementation summary

Production changes (5 files):

1. `src/forge/pipeline/dispatchers/autobuild_async.py`
   - `AsyncTaskStarter` Protocol grew `astart_async_task` async
     sibling (the sync `start_async_task` is preserved for back-compat
     with in-memory test fakes; production wires the async one).
   - `dispatch_autobuild_async` is now `async def`.
   - Line 473's call site changed to
     `await async_task_starter.astart_async_task(...)`.

2. `src/forge/cli/_serve_async_task_starter.py`
   - `_StructuredToolAsyncTaskStarter` grew an `astart_async_task`
     async method that awaits `self._tool.coroutine(...)` (parallel
     to the legacy sync `start_async_task` that calls
     `self._tool.func(...)`).
   - `build_async_task_starter` factory's duck-type check now also
     verifies `tool.coroutine` is callable (raises `TypeError` with
     a F010G-tagged message at composition time if not).
   - Module docstring updated to describe both paths and call out
     the F010G fix.

3. `src/forge/cli/_serve_deps.py:295`
   - Added `await` in the `dispatch_build` async closure.

4. `src/forge/cli/serve.py:_make_autobuild_dispatcher_closure`
   - `dispatcher` closure is now `async def` and awaits
     `dispatch_autobuild_async(...)`.

5. `src/forge/pipeline/supervisor.py`
   - `AutobuildDispatcher = Callable[..., Awaitable[Any]]` (was
     `Callable[..., Any]`).
   - `Supervisor._dispatch`'s autobuild branch now does
     `return await self.autobuild_dispatcher(...)`.

### AC-4 — Integration test

Added `TestDispatchEndToEndUsesAsyncLaunchPath` in
`tests/forge/test_serve_async_task_starter.py` (closest precedent
per the brief's AC-4 wording). The test composes the production
adapter + dispatcher + a tool double whose sync `func` raises the
exact F010G ValueError shape (`Async subagent 'autobuild_runner' has
no url configured. ASGI transport (url=None) requires async
invocation.`) and whose async `coroutine` returns a Command-shaped
object with one `async_tasks` entry. It then drives
`dispatch_autobuild_async` end-to-end and asserts:

1. The async path was taken (`tool.func` was NOT invoked, so the
   F010G ValueError did NOT fire).
2. The minted task_id flows through the full chain.
3. The lifecycle_emitter is threaded onto the launched task's
   description payload (the precondition for the runner publishing
   `pipeline.build-started.<feature_id>` once it actually runs).
4. The `async_tasks` state-channel entry is initialised with the
   minted task_id, the threaded correlation_id, and
   `lifecycle="starting"`.

The class is the async-path mirror of `TestHappyPathTranslation`
(the sync-path translation tests). It deliberately stops short of
standing up a real LangGraph runtime (that would require
network/in-process ASGI plumbing — the FW10-011 capstone integration
test surface, which AC-4 explicitly references and which is
`status: design_approved`). The key proof — "the launch reaches the
middleware's async success branch instead of the sync url-None
ValueError" — is captured by sentinel #1 above.

Plus six unit tests in the new `TestAsyncLaunchPath` class covering
the adapter's async path translations (subagent-name mapping,
task_id extraction, runtime synthesis, lifecycle_emitter drop,
failure-string Command unpacking, contract violations).

### AC-5 — operator runbook revalidation

Pending operator action — rerun jarvis runbook §6.2+§7 against a
forge-prod boot from this branch. Capture the new correlation_id in
the completion notes when the canonical Phase 7 happy-path sequence
(`build-started → stage <stage_label> (PASSED)*N → build-complete
(PASSED)`) renders all-threaded between prompts. Cannot be exercised
from /task-work because it requires a live broker + jarvis chat
session.

### AC-6 — F010F regression check

`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`:
4 tests pass unchanged. The safety-net `build-failed` publish path
remains shaped exactly as F010F left it; no short-circuit was
introduced.

### AC-7 — full-suite regression check

`pytest tests/forge/ tests/`:
- 4277 passed, 3 skipped, 1 deselected (the AC-7 acknowledged
  `test_clock_hygiene` pre-existing failure on
  `approval_subscriber.py:684`, introduced 2026-05-02 in commit
  `41cba9c`, unrelated to F010G).
- 1 failed: the pre-existing `@pytest.mark.slow` docker test
  `test_forge_serve_arfs_inside_image` in
  `tests/integration/test_forge_production_image.py` — failing on
  `Error: No such command 'python'.` from a docker-run subprocess
  that expected a `python` entrypoint. Infrastructure issue,
  unrelated to F010G; reproduces on `main` pre-F010G as well.

Test ripple from chain-async: ~25 production-test methods grew
`@pytest.mark.asyncio` + `async`/`await`; 7 in-memory dispatcher
fakes grew an async `__call__` (or async `astart_async_task`
alongside their existing sync `start_async_task`). One concurrency
test in `test_dispatch_autobuild_async.py` was rewritten from
`threading.Thread` to `asyncio.gather`, and one test in
`test_supervisor_async_subagent_wiring.py`
(`test_supervisor_dispatcher_closure_is_synchronous`) was renamed +
inverted to assert the closure IS now async (it returns a coroutine
that resolves to the handle).

### Non-Implementation Notes (background)

- **Why this is a "fix task" and not a "review task" despite the
  option space.** The decision space is narrow once the investigation
  runs (most likely Option C, with Option A2 as the fallback). A
  dedicated review task adds latency without much added value — the
  implementer's investigation step + chosen-option documentation in
  the body is sufficient. Promote to review-task only if the
  investigation finds genuine architectural ambiguity (e.g.
  langgraph-dev sidecar requires a major deployment-shape change,
  or the Option C async-path ripples force the dispatcher's
  Protocol shape to change in a way that breaks FW10-002 callers).
- **Sequence vs other open work:** F010G is the **last open
  follow-up** in the FEAT-JARVIS-INTERNAL-001-FRR wave as of
  2026-05-04 late evening. Once it lands, the runbook can be re-run
  for canonical Phase 7 happy-path close.
- **TASK-FW10-011 (status: `design_approved`)** — the FEAT-FORGE-010
  capstone integration test — is the ideal place to lock this
  contract in. AC-4 above defines the test the F010G fix needs;
  arguably F010G's AC-4 *is* TASK-FW10-011, or a meaningful subset
  of it. Cross-link the two tasks; consider closing FW10-011 as
  part of this work or filing F010G's test under FW10-011's name.
- **Nothing on the wire today blocks operator visibility:** F010F's
  safety-net publish guarantees the operator sees a terminal
  `build-failed` envelope with the F010G failure_reason embedded,
  so this gap is **not silent** — it's loud and well-routed. That's
  why the runbook can declare structural Phase 7 close even with
  F010G open. F010G is about *the build actually running*, not
  about *the operator knowing what happened*.
- **Reproducer:** boot forge-prod from a current `main` (post
  `50f646f`) and queue any feature via jarvis chat. The
  dispatch-failure log line is deterministic;
  `pipeline.build-failed.*` envelope appears immediately. Recipe is
  in RESULTS Addendum 3 / `command_history.md` 2026-05-04
  late-evening section.

## References

- **Source-of-truth (forge):**
  - `src/forge/cli/_serve_production.py` — production wrapper that
    constructs the middleware (line 168-282).
  - `src/forge/cli/serve.py:_build_async_subagent_middleware` (line
    262-299) — middleware factory; the registration site that omits
    `url`.
  - `src/forge/pipeline/dispatchers/autobuild_async.py` —
    `AUTOBUILD_RUNNER_NAME` constant + the `AsyncTaskStarter`
    Protocol (line 155-189) + the call site at line 473.
  - `src/forge/cli/_serve_async_task_starter.py` — F010E's adapter,
    which today calls `self._tool.func(...)` (sync) at line 269. The
    Option C path edits this file.
  - `src/forge/cli/_serve_dispatcher.py` and `_serve_daemon.py` —
    call boundary between consumer and middleware (where F010E's
    adapter sits).
  - `langgraph.json` — registers the `autobuild_runner` graph at
    `./src/forge/subagents/autobuild_runner.py:graph`. The Option A
    paths point a URL at this graph's hosted ASGI surface.
- **Source-of-truth (third-party — read during investigation):**
  - `deepagents.middleware.async_subagents` (vendored at
    `<venv>/lib/python3.12/site-packages/deepagents/middleware/async_subagents.py`)
    — the launch site. Key lines:
    - `:60-65` — `AsyncSubAgent.url: NotRequired[str]` docstring
      ("Omit to use ASGI transport for local servers").
    - `:239-244` — `_ClientCache.get_sync` — the url-None guard
      (the line that raises on F010G).
    - `:253-262` — `_ClientCache.get_async` — **no** url-None
      guard (the entrypoint Option C exploits).
    - `:273-318` — `_build_start_tool` sync path
      (`start_async_task` → `clients.get_sync`).
    - `:320-339` — `_build_start_tool` async path
      (`astart_async_task` → `clients.get_async`).
- **Source-of-truth (operational):**
  - `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
    — Addendum 3 with the full evidence chain (correlation_id
    `db27f127-…`, the rendered chat line, the wire envelope, the
    forge log WARNING).
- **Sibling tasks:**
  - [`TASK-FORGE-FRR-F010E`](../../completed/TASK-FORGE-FRR-F010E/TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md)
    — the call-boundary fix; predecessor.
  - [`TASK-FORGE-FRR-F010F`](../../completed/TASK-FORGE-FRR-F010F/TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md)
    — the safety-net publish; predecessor (the reason F010G is
    visible to operators rather than silent).
  - `TASK-FW10-002` — the autobuild_runner subagent definition. The
    URL deferral happened here implicitly; check whether AC X.Y
    explicitly punts the URL wiring.
  - `TASK-FW10-008` — the middleware wiring. Same check.
  - `TASK-FW10-011` (status: `design_approved`) — capstone
    integration test; consider folding F010G's AC-4 into FW10-011's
    implementation.
- **Run that surfaced this:**
  - **correlation_id**: `db27f127-a863-4723-a4be-b8cbb68eab5a`
  - **Date**: 2026-05-04 (late evening rerun)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `50f646f` (post F010E commit `4438c47` + F010F
    commit `50f646f`)
  - **jarvis HEAD**: `85f2e39` (post F010Db commit `6071fe0`)
  - **Image**: `forge:latest` = sha256 `dac09cbfa4da6...`
  - **DB state at time of error**: schema bootstrapped automatically
    (F010A); QUEUED row written to `builds`; F010B `StageLogReader`
    composed at boot; F010E adapter composed; dispatcher reached the
    middleware; deepagents WARNING raised inside the launched
    coroutine; F010F safety-net published terminal `build-failed`.
