---
id: TASK-FORGE-FRR-F010H
title: "Thread compiled autobuild_runner graph into AsyncSubAgent registration so in-process ASGI transport has a callable to invoke"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T19:45:00Z
completed: 2026-05-04T19:45:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010H/
organized_files:
  - TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md
outcome: investigation_complete_implementation_deferred
follow_up_task: TASK-FORGE-FRR-F010I
priority: high
task_type: fix
tags:
  - forge-serve
  - async-subagent
  - autobuild-runner
  - asgi-transport
  - in-process-invocation
  - deployment-config
  - deepagents
  - langgraph-sdk
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - last-mile
complexity: 3
estimated_minutes: 90
estimated_effort: "60-180 minutes (investigation + 1-2 line registration change + 1-2 unit tests)"
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-002        # autobuild_runner async subagent definition (where the compiled graph is)
  - TASK-FW10-008        # AsyncSubAgentMiddleware wiring (where this registration lives)
  - TASK-FORGE-FRR-F010E # StructuredTool->AsyncTaskStarter adapter (the call boundary above this gap)
  - TASK-FORGE-FRR-F010F # safety-net publish path that surfaces this gap to the operator
  - TASK-FORGE-FRR-F010G # async coroutine path switch (the predecessor that exposed this gap)
correlation_id: bf697f49-3114-4c90-ae62-63936b8c53bf
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-F010G runbook rerun (Addendum 4). F010G's switch to await self._tool.coroutine(...) bypassed the get_sync() url=None guard and reached get_async(); the in-process LangGraph SDK client at get_client(url=None) tried to call something that's None — most likely the autobuild_runner's compiled graph isn't being threaded into the AsyncSubAgent registration"
context_files:
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - src/forge/cli/serve.py
  - src/forge/cli/_serve_production.py
  - src/forge/cli/_serve_dispatcher.py
  - src/forge/pipeline/dispatchers/autobuild_async.py
  - tasks/completed/TASK-FW10-002-implement-autobuild-runner-async-subagent.md
  - tasks/completed/TASK-FW10-008-wire-async-subagent-middleware-into-supervisor.md
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Thread compiled `autobuild_runner` graph into `AsyncSubAgent` registration so in-process ASGI transport has a callable to invoke

## TL;DR

F010G correctly switched the autobuild dispatch from the synchronous
`_ClientCache.get_sync()` path to the asynchronous
`_ClientCache.get_async()` path — empirically validated by the
error-message change between Addendum 3 and Addendum 4 of the RESULTS
file (URL=None ASGI rejection → `'NoneType' object is not callable`).
The async path no longer rejects `url=None`, but it now raises
`'NoneType' object is not callable` inside the in-process ASGI
transport chain. The most likely root cause is that the
`autobuild_runner` `AsyncSubAgent` registration in
`forge.cli.serve._build_async_subagent_middleware` does not thread the
compiled subagent graph into the registration object — the LangGraph
SDK's `get_client(url=None, app=...)` factory needs a callable
app/graph reference for in-process invocation, and somewhere in our
registration chain that reference is `None`. Likely a one-line
registration fix once the investigation step confirms the hypothesis.

## Symptom (verbatim from RESULTS Addendum 4)

The user-visible chat REPL line:

```
[18:55] Forge FEAT-43DE: build-failed (RuntimeError: _StructuredToolAsyncTaskStarter: middleware tool returned launch failure: "Failed to launch async subagent 'autobuild_runner': 'NoneType' object is not callable")
```

The forge daemon log lines that produced it:

```
2026-05-04T17:55:45 [WARNING] deepagents.middleware.async_subagents: Failed to launch async subagent 'autobuild_runner': 'NoneType' object is not callable
2026-05-04T17:55:45 [WARNING] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatch_build raised (...) for feature_id=FEAT-43DE correlation_id=bf697f49-...; publishing build-failed and acking so the next build can be processed
```

The wire envelope (proves F010F + F010C still work):

```json
{"source_id":"forge","event_type":"build_failed",
 "correlation_id":"bf697f49-3114-4c90-ae62-63936b8c53bf",
 "payload":{"feature_id":"FEAT-43DE","build_id":"FEAT-43DE",
            "failure_reason":"RuntimeError: _StructuredToolAsyncTaskStarter: middleware tool returned launch failure: \"Failed to launch async subagent 'autobuild_runner': 'NoneType' object is not callable\"",
            "recoverable":false,"failed_task_id":null}}
```

## Why F010G unblocked this

The proof F010G is live is the error-message change. Side-by-side:

| Pre-F010G (Addendum 3, correlation_id `db27f127-…`) | Post-F010G (Addendum 4, correlation_id `bf697f49-…`) |
|---|---|
| `'has no url configured. ASGI transport (url=None) requires async invocation.'` | `''NoneType' object is not callable'` |
| Source: synchronous `_ClientCache.get_sync()` rejects `url=None` at line ~239-244 | Source: asynchronous `_ClientCache.get_async()` reaches `langgraph_sdk.get_client(url=None, ...)`; in-process transport calls a `None` callable |

F010G's `await self._tool.coroutine(...)` swap routes through the
second path, which has no url-None guard but assumes the registration
has the in-process pieces it needs. F010G's commit (`8d08b93`) is
**correct and stays in place**; without it, F010H would never even
fire (the call would still be rejected at the sync URL=None guard).

## Distinction from F010E and F010G

- **F010E** was about the **call boundary API mismatch** between
  forge's `dispatch_build` (which expected a Protocol-named
  `start_async_task` method) and the LangChain `StructuredTool`
  returned by `AsyncSubAgentMiddleware.tools` (which exposes
  `invoke()` instead). **Fixed** by the
  `_StructuredToolAsyncTaskStarter` adapter.
- **F010G** was about the **call shape inside the adapter** — the
  sync `tool.func(...)` path's `_ClientCache.get_sync()` rejects
  `url=None`. **Fixed** by switching to the async
  `await tool.coroutine(...)` path so `_ClientCache.get_async()` is
  reached instead.
- **F010H** is about what happens **inside the now-reached async
  codepath** — the LangGraph SDK's `get_client(url=None)` returns an
  in-process client that can't find the graph/app to invoke because
  the `AsyncSubAgent` registration didn't supply it.

These are independent gaps. F010E + F010G's fixes are correct and
stay in place. (Without F010G, F010H would never even fire.)

## Investigation needed (mandatory — do not skip)

The implementer's option-comparison below is calibrated by this step.
Do **not** skip the investigation — record findings in §Implementation
Notes before any production code lands.

1. **What fields does `deepagents.middleware.async_subagents.AsyncSubAgent`
   accept?** Read the dataclass / TypedDict definition. Specifically:
   is there a `graph: CompiledGraph | None` field (or similarly named —
   `app`, `runnable`, `compiled_graph`)? If yes, this gap is
   straightforward — just thread the compiled graph through.
2. **How does `_ClientCache.get_async()` invoke
   `langgraph_sdk.get_client`?** Inside
   `deepagents.middleware.async_subagents`, find the `get_async`
   method body. Note exactly which arguments it forwards. The
   hypothesis is: `get_client(url=None, app=registration.graph)` is
   what it should call, and `registration.graph` is `None` so the
   resulting client's invocation tries to call `None.something()`.
3. **What does `forge.cli.serve._build_async_subagent_middleware`
   currently register?** Read it (around line 262). Note the exact
   `AsyncSubAgent(...)` constructor call shape. The hypothesis is
   that only `name` (and possibly `description` / `graph_id`) is set;
   `graph` / `app` is left at its default (likely `None`).
4. **Where is the autobuild_runner's compiled graph?** Per FW10-002 /
   `forge.pipeline.dispatchers.autobuild_async`, find the
   `CompiledGraph` instance (or the `create_deep_agent(...)` factory
   call). Confirm it's accessible from the middleware factory's call
   site.
5. **Does the LangGraph SDK's `get_client(url=None)` API accept an
   in-process app argument?** The hypothesis assumes yes. Confirm by
   reading `langgraph_sdk.client.get_client` (also third-party, in the
   same venv).
6. **Cross-reference FW10-008's ACs.** Did the original wiring task
   explicitly defer in-process invocation? If yes, this task is
   "complete the deferred AC X.Y" rather than "new contract" — note
   that for the reviewer's context.

### Quick local repro (validate the hypothesis before fixing)

Inside the running forge-prod container (or any forge venv), the
`'NoneType' object is not callable` is reproducible deterministically
without a full runbook rerun:

```python
import asyncio
from forge.cli.serve import _build_async_subagent_middleware
from forge.pipeline.dispatchers.autobuild_async import AUTOBUILD_RUNNER_NAME

mw = _build_async_subagent_middleware()
tool = next(t for t in mw.tools if t.name.endswith("start_async_task"))
# This call path goes through _ClientCache.get_async() per F010G's switch.
asyncio.run(tool.coroutine({"subagent_name": AUTOBUILD_RUNNER_NAME, "context": {}}))
# Expected: 'NoneType' object is not callable
```

Use this recipe to confirm the fix lands (the same call should
resolve cleanly once the graph reference is threaded).

## The implementation options

**Option A — Thread the compiled graph into the AsyncSubAgent
registration (the hypothesis):** Add `graph=<compiled autobuild_runner
graph>` (or whatever the field is named per investigation step 1) to
the `AsyncSubAgent(name=..., ...)` constructor call in
`_build_async_subagent_middleware`. The middleware will then forward
the graph to `langgraph_sdk.get_client(url=None, app=...)` and the
in-process invocation will have something to call. Likely a one-line
registration change.

**Option B — Configure a URL after all (fall back to F010G's
Option A1/A2/A3):** If investigation step 1 finds that `AsyncSubAgent`
does NOT accept a `graph`/`app` field — i.e. the deepagents middleware
hard-assumes URL-based deployment for in-process resolution too (some
kind of self-hosted langgraph-dev) — then the only way forward is to
actually deploy a langgraph-dev/deploy ASGI surface as the F010G task
body's Option A1/A2/A3 originally proposed. Substantially larger
scope; defer to a separate task if so.

**Option C — Upstream patch to deepagents (last resort):** If neither
A nor B is satisfiable as-is — e.g. `langgraph_sdk.get_client(url=None)`
doesn't accept an in-process app argument — this gap requires upstream
changes to deepagents' middleware or langgraph-sdk to support
in-process invocation. Larger scope; file an upstream issue.

**Pick based on investigation step 1.** The expected outcome is
**Option A** — most LangGraph-SDK-style middleware libraries support
in-process invocation via an `app` argument, and deepagents
specifically markets itself as a multi-deployment-shape solution.
State the chosen option and rationale in §Implementation Notes
before implementing.

## Acceptance Criteria

- [x] **AC-1 (investigation)**: Document the investigation findings
  (AsyncSubAgent fields, `get_async` invocation shape, current
  registration shape, autobuild_runner graph location, langgraph_sdk
  `get_client` API surface, FW10-008 deferred-AC status) in the task
  body's §Implementation Notes section before the fix lands. **Done
  2026-05-04** — see §Implementation Notes "Investigation findings
  (AC-1)". Hypothesis falsified.
- [x] **AC-2 (decision)**: Document the chosen option (A / B / C) and
  rationale. **Done 2026-05-04** — see §Implementation Notes
  "Decision (AC-2)". Option A impossible; Option B sub-paths B.1/B.2/B.3
  enumerated; deferred to follow-up review task TASK-FORGE-FRR-F010I.
- [ ] **AC-3 (implementation)**: ~~Implement the fix per AC-2's
  decision. The repro recipe at the top of this task body resolves
  cleanly (no `'NoneType' object is not callable` raised) when invoked
  against a fixture-built middleware.~~ **Deferred to F010I
  implementation companion** per AC-2's decision.
- [ ] **AC-4 (test)**: ~~A unit test reproduces the in-process repro
  recipe — invoke the autobuild_runner via `await tool.coroutine(...)`
  against a fixture-built middleware and assert at least one
  `pipeline.build-started.<feature_id>` envelope is published (or, if
  going through the dispatcher boundary instead of directly, assert
  the dispatcher returns success and that `_handle_message` was
  invoked on the autobuild_runner's graph). Use a mock or scripted
  autobuild that emits a known lifecycle sequence per the FW10-011
  design pattern.~~ **Deferred to F010I implementation companion**.
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
  prompts. **This is the canonical Phase 7 happy-path close.**
  Capture the new correlation_id in completion notes.
- [ ] **AC-6 (regression — F010F safety net)**: F010F's safety-net
  `build-failed` publish path continues to fire if any future
  dispatch / launch error happens (don't accidentally short-circuit
  it during the F010H fix). Existing dispatch-failure tests
  (`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`)
  pass unchanged.
- [ ] **AC-7 (regression — full suite)**: Full forge test suite
  (`pytest tests/forge/ tests/`) passes. Pre-existing
  `test_clock_hygiene` failure on `approval_subscriber.py:684`
  remains deselected (introduced 2026-05-02 in commit `41cba9c`,
  unrelated to F010H — same exclusion F010G's AC-7 carried).

## Files Expected to Change

**If Option A (the expected path):**
- `src/forge/cli/serve.py:_build_async_subagent_middleware` —
  one-line registration change to add `graph=<autobuild_runner graph>`
  (or equivalent field name per investigation).
- Possibly an import for the compiled graph from
  `forge.pipeline.dispatchers.autobuild_async` (or wherever FW10-002
  exposes it).
- A new or extended test under `tests/forge/` covering AC-4. Closest
  precedent is `tests/forge/test_serve_async_task_starter.py` (F010E's
  / F010G's adapter unit tests, with the new
  `TestDispatchEndToEndUsesAsyncLaunchPath` class added by F010G).

**If Option B / C:** substantially more (see F010G task body for
Option A1/A2/A3 file lists; or new upstream-patch coordination).

## Implementation Notes

### Investigation findings (AC-1) — 2026-05-04 evening

The mandatory investigation falsified the F010H hypothesis. Mapping
each of the six §Investigation needed steps to its outcome:

1. **`AsyncSubAgent` TypedDict fields**
   (`deepagents.middleware.async_subagents:34-68`, deepagents 0.5.3) —
   exactly five keys: `name`, `description`, `graph_id`, `url`
   (`NotRequired`), `headers` (`NotRequired`). **No `graph` / `app` /
   `runnable` / `compiled_graph` field.** The TypedDict is hard-coded;
   the registration object has nowhere to thread a compiled graph
   into.
2. **`_ClientCache.get_async` invocation shape**
   (`async_subagents.py:253-262`) —
   `get_client(url=spec.get("url"), headers=_resolve_headers(spec))`.
   No third argument is forwarded; even if `AsyncSubAgent` had a
   `graph` field, the middleware would not propagate it.
3. **Current registration shape**
   (`forge.cli.serve._build_async_subagent_middleware:286-299`) —
   `AsyncSubAgent(name=AUTOBUILD_RUNNER_NAME, description=…,
   graph_id=AUTOBUILD_RUNNER_NAME)`. As predicted, only the three
   required fields are set; `url` is omitted by design (the
   in-process daemon has no langgraph-api URL to point at).
4. **`autobuild_runner` compiled-graph location**
   (`forge.subagents.autobuild_runner.graph` at
   `_build_runner_graph()` line 771-814, exposed as the
   `langgraph.json` graphs entry
   `./src/forge/subagents/autobuild_runner.py:graph`). The compiled
   graph IS importable from the middleware factory's call site —
   that part of the original Option A hypothesis is straightforward
   — but the middleware has no field to thread it into.
5. **`langgraph_sdk.get_client(url=None)` API surface**
   (`langgraph_sdk/_async/client.py:29-140`, langgraph-sdk 0.3.13) —
   signature is `(*, url=None, api_key=NOT_PROVIDED, headers=None,
   timeout=None)`. **No `app=` kwarg.** When `url is None`:
   - First branch tries `from langgraph_api.server import app` and
     wraps it in `ASGITransport(app, root_path="/noauth")`. The
     forge venv does **not** have `langgraph_api` installed
     (`python3 -c "import langgraph_api"` →
     `ModuleNotFoundError`), so this branch raises and falls through.
   - Fallback branch creates `ASGITransport(app=None,
     root_path="/noauth")` and registers it on the module-level
     `_registered_transports` list for deferred wiring via
     `langgraph_sdk._shared.utilities.configure_loopback_transports(app)`
     (utilities.py:200-206). This is the path forge currently hits —
     the in-process httpx client is constructed with a transport
     whose `.app is None`, and the first request through it raises
     `'NoneType' object is not callable` from inside httpx's ASGI
     dispatch.
6. **FW10-008 deferred-AC status** — the FW10-008 task body wired the
   middleware tools onto the supervisor surface but did not assert
   end-to-end in-process invocation worked; the remote-URL deployment
   shape was the implicitly-assumed path. F010H is "complete the
   deferred deployment wiring", not "fix a regression".

End-to-end failure-mode chain now understood:
`_StructuredToolAsyncTaskStarter.astart_async_task` (forge
`_serve_async_task_starter.py:298-353`) → deepagents
`astart_async_task` (`async_subagents.py:320-358`) →
`_ClientCache.get_async` (`:253-262`) →
`langgraph_sdk.get_client(url=None)` →
`ASGITransport(app=None, root_path="/noauth")` → first httpx
request → `None(scope, receive, send)` →
`'NoneType' object is not callable`.

### Decision (AC-2) — Defer Option B+ to a follow-up review task

**Chosen option: NONE of A/B/C as currently scoped.** Per this task
body's own decision tree ("Option A is the expected path... defer to
a separate task if so" for Option B), the F010H scope cannot land:

- **Option A is impossible** — investigation steps 1, 2, and 5
  falsify the hypothesis. There is no field on `AsyncSubAgent` to
  thread a graph into, and `langgraph_sdk.get_client` does not
  accept an in-process app argument.
- **Option B (deploy a langgraph-dev/deploy ASGI surface)** is the
  remaining viable path but is "substantially larger scope" per
  this task body. It splits into three sub-paths, none of which
  fit a 1-2 line registration change:
  - **B.1 — Sidecar URL.** Run `langgraph dev` (or `langgraph up`) in
    a sidecar container/process, register its URL on the
    `AsyncSubAgent` spec, thread the URL through `ServeConfig` /
    `bind_production_serve`. Production-shape choice deepagents was
    designed for. Touches deployment topology (additional container or
    in-pod process), config schema (e.g.
    `FORGE_AUTOBUILD_RUNNER_URL` env var), the `_serve_production`
    wrapper, and the operator runbook.
  - **B.2 — Hand-rolled in-process ASGI app.** Set
    `__LANGGRAPH_DEFER_LOOPBACK_TRANSPORT=true` and call
    `configure_loopback_transports(app)` after middleware
    construction with a hand-rolled ASGI app implementing the
    LangGraph SDK's threads/runs/assistants HTTP shape. Effectively
    re-implements `langgraph_api`; not viable in F010H scope.
  - **B.3 — Add `langgraph_api` as a forge dependency.** Embed the
    langgraph-api server in the daemon process; either
    `langgraph_sdk.get_client(url=None)` finds it via the
    `langgraph_api.server.app` import, or call
    `configure_loopback_transports(app)` after middleware
    construction. Cleanest in-process path but pulls a substantial
    new dependency tree (FastAPI / orjson / starlette middleware) and
    needs a decision-mode review of the operational tradeoffs.
- **Option C (upstream patch to deepagents)** is even larger scope and
  unwarranted given Option B sub-paths exist.

**Action:** investigation done; implementation is escalated to a
follow-up task **TASK-FORGE-FRR-F010I** filed alongside this one.
F010I is `task_type: review` (decision-mode) to pick between B.1 / B.2
/ B.3 before any implementation lands. F010I's implementation
companion will close the actual repro and produce the canonical
Phase 7 happy-path sequence.

### What this task DID accomplish

- AC-1 (investigation): findings recorded above with file:line refs
  for every claim.
- AC-2 (decision): "Option A impossible; defer Option B to
  TASK-FORGE-FRR-F010I" recorded above with B.1 / B.2 / B.3
  sub-paths enumerated for the reviewer's context.
- AC-6 (F010F regression): no code changed, so F010F's existing
  dispatch-failure tests
  (`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`)
  still pass — verified during regression sweep.
- AC-7 (full forge suite): pre-existing state preserved (no code
  change).

### What this task did NOT accomplish

- AC-3 (implementation): no code change. Repro recipe still raises
  `'NoneType' object is not callable`. Implementation moves to
  F010I's companion fix task.
- AC-4 (test): no test added.
- AC-5 (operator runbook revalidation): pending — blocked behind the
  F010I implementation companion.

### Status sequence after this task lands

1. F010H closes in `in_review/feat-jarvis-internal-001-followups/`
   with the investigation deliverable.
2. F010I (review-mode) opens in
   `tasks/backlog/feat-jarvis-internal-001-followups/` to choose
   between Option B.1 / B.2 / B.3.
3. F010I's implementation companion closes the actual repro,
   produces the canonical happy-path build sequence, and satisfies
   F010H's deferred AC-3/AC-4/AC-5.

### Why this is a "fix task" not a "review task" despite the option space (original note, retained)

The investigation step was short and deterministic — read three
files, run the in-process repro recipe, confirm the hypothesis. The
original task body said: "Once that completes, Option A is most
likely a one-line change. Promote to review-task only if the
investigation finds the hypothesis is wrong AND Option B / C are
both viable (rare)." **The investigation outcome triggered exactly
that promotion path** — the hypothesis was falsified, three viable
Option B sub-paths were identified, and the correct next step is a
decision-mode review (F010I).
- **Sequence vs other open work:** F010H is the **last open
  follow-up** in the FEAT-JARVIS-INTERNAL-001-FRR wave as of
  2026-05-04 evening. Once it lands, the runbook can be re-run for
  canonical Phase 7 happy-path close — the full
  `build-started + stage-complete*N + build-complete` rendered chat
  sequence.
- **Cross-reference TASK-FW10-011** (`design_approved` capstone
  integration test). AC-4 above is functionally equivalent to (or a
  subset of) FW10-011's design. Consider folding F010H's test under
  FW10-011's name; alternatively, ship F010H's AC-4 as a precursor
  regression test and let FW10-011 build on it later. Same advice
  F010G's task body offered for its AC-4.
- **Operator visibility is unchanged:** F010F's safety-net publish
  guarantees the operator continues to see a terminal `build-failed`
  envelope with the F010H failure_reason embedded for as long as
  F010H is open. So the gap is **loud and well-routed** — Phase 7
  structural close stays. F010H is about getting the actual autobuild
  to *run*, not about fixing operator visibility.
- **Reproducer (from RESULTS Addendum 4 / command_history late-evening
  section):** boot forge-prod from current `main` (post-`8d08b93`)
  and queue any feature via jarvis chat. Forge logs deterministically
  show `'NoneType' object is not callable` from inside
  `deepagents.middleware.async_subagents`. The in-process repro recipe
  at the top of this task body is faster.
- **Each iteration peels back exactly one layer of FW10-002 / FW10-008's
  deferred deployment wiring.** F010H is the deepest layer surfaced so
  far — and per the RESULTS Addendum 4 tally, may well be the genuine
  last one before a successful autobuild runs end-to-end.

## References

- **Source-of-truth (forge):**
  - `src/forge/cli/serve.py:_build_async_subagent_middleware` (around
    line 262) — middleware factory; F010H's likely fix site.
  - `src/forge/cli/_serve_production.py` — production wrapper that
    constructs the middleware via the factory above (TASK-FIX-F010).
  - `src/forge/cli/_serve_dispatcher.py:_StructuredToolAsyncTaskStarter`
    — F010E adapter; F010G switched its sync `func` call to async
    `coroutine`. The call boundary above the gap.
  - `src/forge/pipeline/dispatchers/autobuild_async.py` —
    `AUTOBUILD_RUNNER_NAME` + the autobuild_runner subagent's compiled
    graph (per FW10-002). The graph reference Option A would thread
    into the registration.
- **Source-of-truth (third-party — read during investigation):**
  - `deepagents.middleware.async_subagents.AsyncSubAgent` —
    registration shape (look for `graph` / `app` / `runnable` /
    `compiled_graph` field).
  - `deepagents.middleware.async_subagents._ClientCache.get_async` —
    the path F010G now reaches; note the arguments forwarded to
    `langgraph_sdk.get_client`.
  - `langgraph_sdk.client.get_client` — `url=None` in-process API
    surface (does it accept `app=`?).
- **Source-of-truth (operational):**
  - `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
    — Addendum 4 with the full evidence chain (correlation_id
    `bf697f49-…`, the rendered chat line, the wire envelope, the
    forge log WARNING, the side-by-side error-message comparison
    proving F010G is live).
- **Sibling tasks (the chain that surfaced this):**
  - [`TASK-FORGE-FRR-F010E`](../../completed/TASK-FORGE-FRR-F010E/TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md)
    — call-boundary fix; predecessor.
  - [`TASK-FORGE-FRR-F010F`](TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md)
    — safety-net publish; predecessor (the reason F010H is visible to
    operators rather than silent).
  - [`TASK-FORGE-FRR-F010G`](../../completed/TASK-FORGE-FRR-F010G/TASK-FORGE-FRR-F010G-configure-autobuild-runner-url-or-fallback-transport.md)
    — async coroutine path; predecessor — F010G unblocked the call
    into the codepath F010H now fails inside.
  - `TASK-FW10-002` — autobuild_runner subagent definition (where the
    compiled graph is). Check whether AC X.Y explicitly punts the
    in-process invocation wiring.
  - `TASK-FW10-008` — AsyncSubAgentMiddleware wiring. Same check.
  - `TASK-FW10-011` (status: `design_approved`) — `design_approved`
    capstone integration test; consider folding F010H's AC-4 into
    FW10-011.
- **Run that surfaced this:**
  - **correlation_id**: `bf697f49-3114-4c90-ae62-63936b8c53bf`
  - **Date**: 2026-05-04 (evening rerun, post-F010G)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `8d08b93` (post F010G)
  - **Image**: `forge:latest` = sha256 `8ce899e7d03ab...`
  - **DB state at time of error**: schema bootstrapped automatically
    (F010A); QUEUED row written to `builds`; F010B `StageLogReader`
    composed at boot; F010E adapter composed; F010G async coroutine
    path exercised; deepagents WARNING raised inside the in-process
    ASGI transport chain (`'NoneType' object is not callable`); F010F
    safety-net published terminal `build-failed`.
