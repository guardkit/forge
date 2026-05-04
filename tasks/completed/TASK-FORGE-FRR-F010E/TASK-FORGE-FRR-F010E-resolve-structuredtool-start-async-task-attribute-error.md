---
id: TASK-FORGE-FRR-F010E
title: "Resolve `'StructuredTool' object has no attribute 'start_async_task'` in autobuild dispatch path"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T13:00:00Z
completed: 2026-05-04T13:00:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010E/
previous_state: in_review
state_transition_reason: "Code + tests complete; AC-1..AC-5 satisfied. AC-6 (operator runbook revalidation) explicitly pending — same shape as F010.A's completion note."
organized_files:
  - TASK-FORGE-FRR-F010E-resolve-structuredtool-start-async-task-attribute-error.md
priority: high
task_type: fix
tags:
  - forge-serve
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - autobuild
  - dispatch
  - langchain
  - structuredtool
  - attribute-error
  - wiring-drift
  - fw10-008-followup
complexity: 4
estimated_minutes: 90
estimated_effort: "60-120 minutes (find caller, decide invoke-vs-wrapper, add unit test)"
parent_feature: FEAT-FORGE-010
correlation_id: dfad8e7f-92af-4b5f-896f-ca75ad8343bf
related_tasks:
  - TASK-FW10-002   # autobuild_runner subagent
  - TASK-FW10-008   # AsyncSubAgentMiddleware wiring (the source of the StructuredTool surface)
  - TASK-FORGE-FRR-F010B   # StageLogReader adapter — predecessor; its fix unblocked this gap
  - TASK-FIX-F010   # the wiring this exposes
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-F010.A/B/C/D joint live-wire validation rerun late afternoon — F010B's StageLogReader fix unblocked the next layer of wiring drift in the autobuild dispatch path"
test_results:
  status: passing
  coverage: "adapter unit tests + composition-seam integration test"
  last_run: 2026-05-04T12:50:00Z
  summary: |
    tests/forge/test_serve_async_task_starter.py — 17 passed (new file)
    tests/forge/test_cli_serve_production.py — 11 passed (1 updated for adapter)
    tests/forge/test_serve_production_migrations.py — 4 passed (fake updated)
    Full suite (tests/cli/ + tests/forge/ + tests/unit/) — 3780 passed, 0 failed.
    (One pre-existing TestClockHygiene failure in approval_subscriber.py:684
    deselected; introduced 2026-05-02 in commit 41cba9c, unrelated to F010.E.)
---

# Task: Resolve `'StructuredTool' object has no attribute 'start_async_task'` in autobuild dispatch path

## Description

Run 1 of the late-afternoon joint validation rerun on 2026-05-04
(correlation_id `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`) reached the
**deepest point yet** in the production dispatch chain — past
`pipeline_consumer: dispatching build`, past
`dispatch_build: persisted QUEUED row`, past `dispatching autobuild`
— and then raised:

```
AttributeError: 'StructuredTool' object has no attribute 'start_async_task'
```

This is **wiring drift** between FW10-008 (which built the
`AsyncSubAgentMiddleware` and exposes its tool surface as a sequence
of LangChain `StructuredTool` instances) and the autobuild
dispatcher's expectation of how to invoke its `AsyncTaskStarter`
collaborator. The dispatcher calls
`async_task_starter.start_async_task(subagent_name=..., context=...)`
(see
`src/forge/pipeline/dispatchers/autobuild_async.py:473`); the object
it receives at runtime is the production wrapper's resolved tool —
the `StructuredTool` looked up by name from `middleware.tools` at
`src/forge/cli/_serve_production.py:139-142`. `StructuredTool` exposes
`tool.invoke({...})` and `tool.ainvoke({...})`, not
`tool.start_async_task(...)`.

The `pipeline_consumer.handle_message` outer try/except catches the
AttributeError, logs at WARNING, and acks — so the JetStream queue
isn't wedged, but no `pipeline.build-started.*` envelope is ever
published, no autobuild_runner is ever launched, and the operator's
chat session sees nothing on the wire from their queued build.

## Distinction from F010.B

- **F010.B** was about `'SqliteLifecyclePersistence' object has no
  attribute 'get_approved_stage_entry'` — a *persistence-layer*
  method missing. **Resolved** by adding a thin `StageLogReader`
  adapter at the production composition seam (commit `751995f`,
  task `TASK-FORGE-FRR-F010B`).
- **F010.E** is about `'StructuredTool' object has no attribute
  'start_async_task'` — a *tool-invocation API* mismatch. The
  dispatcher progressed past F010.B's site (the StageLogReader
  contract is now satisfied) and bombed at the very next call site:
  the `start_async_task` invocation on the
  `AsyncSubAgentMiddleware`-resolved tool.

These are independent gaps. F010.B's fix is correct and stays in
place; F010.E is the next layer of wiring drift that F010.B's fix
exposed.

## Why

### Empirical evidence (run 1, 2026-05-04 late afternoon rerun)

correlation_id `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`:

```
2026-05-04T12:22:55 [INFO] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatching build feature_id=FEAT-43DE correlation_id=dfad8e7f-... originating_adapter=terminal
2026-05-04T12:22:55 [INFO] forge.cli._serve_deps: dispatch_build: persisted QUEUED row build_id=build-FEAT-43DE-20260504122255 feature_id=FEAT-43DE correlation_id=dfad8e7f-...; dispatching autobuild
2026-05-04T12:22:55 [WARNING] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatch_build raised ('StructuredTool' object has no attribute 'start_async_task') for feature_id=FEAT-43DE correlation_id=dfad8e7f-...; acking and continuing so the next build can be processed
```

The QUEUED row IS successfully persisted to `builds` before the
exception (so partial state is committed), and the F010.B
`StageLogReader` is composed correctly (the new
`build_stage_log_reader: composed SQLite-backed StageLogReader`
log line appears at boot per the RESULTS Addendum 2). The next
thing the dispatcher tries is:

```python
# src/forge/pipeline/dispatchers/autobuild_async.py:473
task_id = async_task_starter.start_async_task(
    subagent_name=AUTOBUILD_RUNNER_NAME,
    context=launch_payload,
)
```

…and `async_task_starter` at runtime is the LangChain
`StructuredTool` returned by
`_resolve_async_task_starter(middleware)` at
`src/forge/cli/_serve_production.py:139-142`:

```python
def _resolve_async_task_starter(middleware: Any) -> Any:
    tools = tuple(getattr(middleware, "tools", ()) or ())
    for tool in tools:
        if getattr(tool, "name", None) == "start_async_task":
            return tool                       # <-- returns the raw StructuredTool
    ...
```

`StructuredTool` does not satisfy the `AsyncTaskStarter` Protocol at
`src/forge/pipeline/dispatchers/autobuild_async.py:155-189` — the
Protocol declares a `start_async_task(subagent_name, context) -> str`
method, but `StructuredTool` exposes `invoke(input)` /
`ainvoke(input)` (LangChain's tool-invocation surface). Forge unit
tests for the dispatcher pass an in-memory fake satisfying the
Protocol directly; tests for the wrapper assert the tool resolution
returns "an object with `.name == 'start_async_task'`" — neither
exercises the seam end-to-end against a real
`AsyncSubAgentMiddleware`-built tool surface, so the mismatch was
never caught before TASK-FIX-F010 wired the production composer.

### Why this is wiring drift, not a bug-in-isolation

- The dispatcher's `AsyncTaskStarter` Protocol (FW10-002 surface)
  expects a *named-method* shape:
  `obj.start_async_task(subagent_name=..., context=...)`.
- The middleware's tool-list shape (FW10-008 surface) is *LangChain
  tool-invocation*: `tool.invoke({"subagent_name": ...,
  "context": ...})` (or `ainvoke` if async).
- Each side is internally consistent; the seam between them was
  never bridged. The fake in the dispatcher's tests satisfies the
  Protocol shape directly (so the dispatcher believes it works);
  the wrapper resolution at `_serve_production.py:139` assumes a
  named-method shape exists on the tool itself (so the wrapper
  believes it works). Production is the first place both shapes
  meet, and they don't agree.

This is exactly the class of failure that TASK-FW10-011 (end-to-end
integration test, currently `design_approved` per the README post-merge
follow-up) is designed to catch — see Ordering note below.

## Investigation Required

This task starts with an investigation step before the fix:

1. **Confirm the call site** with a clean grep:
   `grep -rn "start_async_task(" src/forge/`. Expected hits:
   - `src/forge/pipeline/dispatchers/autobuild_async.py:473` — the
     call site that raises (caller).
   - `src/forge/pipeline/dispatchers/autobuild_async.py:169` — the
     Protocol declaration (caller's contract).
   - `src/forge/cli/_serve_production.py:139-142` — the wrapper's
     resolution by name.
2. **Confirm the runtime type** of the object passed as
   `async_task_starter` to `dispatch_autobuild_async`. Read
   `_serve_production._resolve_async_task_starter` (returns the raw
   `StructuredTool`) and the chain by which it reaches the
   dispatcher (`bind_production_serve` →
   `bind_production_dispatch_chain` → `make_handle_message_dispatcher`
   → `PipelineConsumerDeps` → eventual `_make_autobuild_dispatcher_closure`
   at `serve.py:302`). Document the chain in the §Implementation
   Notes section before implementing.
3. **Compare against the FW10-008 contract** in
   `tasks/completed/TASK-FW10-008-wire-async-subagent-middleware-into-supervisor.md`:
   what shape did FW10-008 ship? A LangChain `StructuredTool` per
   tool, by name, on `middleware.tools`. Was the dispatcher's
   Protocol expectation re-stated in FW10-008's ACs, or was the
   bridging deferred to whatever wired them together (FW10-007 deps
   composition)?
4. **Read the FW10-002 dispatcher tests** to confirm the
   in-memory fake's shape (it satisfies `AsyncTaskStarter` directly
   — the named-method form). That's the contract the production
   side has to satisfy.
5. **Decide between Option A and Option B** below before writing
   any code. Document the decision and rationale in the
   §Implementation Notes section.

## The two implementation options

### Option A — change the caller (LangChain-native)

The dispatcher (or, more precisely, the closure between the
dispatcher and the resolved tool) calls
`tool.invoke({"subagent_name": ..., "context": ...})` instead of
`tool.start_async_task(...)`. If the call site is async, use
`tool.ainvoke({...})`. The `AsyncTaskStarter` Protocol is replaced
by a LangChain-tool-shaped Protocol (or kept and adapted via a thin
wrapper that bridges the named-method form to `invoke`).

**Pros**:
- `StructuredTool` is the canonical LangChain shape. Every other
  tool in the project is wired this way (DeepAgents middleware
  exposes its toolset uniformly).
- Aligns the dispatcher with the rest of the codebase's tool
  invocation pattern.
- Least surprising for a future contributor reading the autobuild
  dispatcher: "it's a tool, you call `.invoke()`."

**Cons**:
- Touches FW10-002's `AsyncTaskStarter` Protocol (or replaces it).
  The Protocol's purpose-shaped naming
  (`start_async_task(subagent_name, context)`) communicates intent
  better than a generic `tool.invoke({...})` call — moving to the
  generic shape loses some of that documentation.
- Existing FW10-002 unit tests will need to update their fakes.
- The async/sync question matters: `dispatch_autobuild_async` is
  declared async (line 327: `async def dispatch_autobuild_async`),
  so the natural choice is `await tool.ainvoke({...})`. Confirm in
  the implementation note.

### Option B — change the middleware-resolution wrapper (named-method bridge)

`_resolve_async_task_starter()` in `_serve_production.py` returns a
small adapter class instead of the raw `StructuredTool`. The
adapter holds the underlying tool and exposes `.start_async_task(
subagent_name, context) -> str` as a named method that delegates to
`self._tool.invoke({"subagent_name": subagent_name, "context":
context})` (or `await self._tool.ainvoke({...})` if the dispatcher
expects an awaitable). The dispatcher's `AsyncTaskStarter` Protocol
stays as-is; the FW10-002 fake stays as-is; only the wrapper
changes shape.

**Pros**:
- Zero churn to the dispatcher and its tests.
- Preserves the named-method abstraction the dispatcher's API
  documents.
- Symmetric with how F010.B was resolved (adapter-wrapping at the
  production composition seam, *not* re-shaping the underlying
  facade — see TASK-FORGE-FRR-F010B's AC-2 decision rationale).

**Cons**:
- Adds a tiny wrapper class to `_serve_production.py` (or a sibling
  module).
- Slightly diverges from "tools are LangChain tools, call `.invoke`"
  for this one named-method bridge. Future readers may wonder why
  this one tool gets a wrapper.

### Recommended option

**Likely Option B**, on three grounds:

1. **Symmetry with F010.B**: F010.B was resolved by adapter-wrapping
   at the production composition seam (`StageLogReader` adapter
   over `sqlite_pool` in `build_stage_log_reader()`). F010.E is the
   exact same shape of fix at the same seam — wrap the
   `StructuredTool` in an adapter that satisfies the
   `AsyncTaskStarter` Protocol. The FW10-002 dispatcher and its
   tests stay untouched.
2. **The Protocol communicates intent**: `start_async_task(
   subagent_name, context)` is more readable than `tool.invoke({
   "subagent_name": ..., "context": ...})` at the dispatcher's
   call site — and the dispatcher's purpose is dispatching, not
   tool-invocation. Keep the dispatcher API purpose-shaped.
3. **Test surface symmetry**: F010.B's regression test
   (`tests/cli/test_serve_deps_dispatch_real_persistence.py`) only
   mocks `AsyncTaskStarter` at the boundary (it's the *only* mock
   in an otherwise-real composition). With Option B, the same test
   pattern works — replace the `AsyncTaskStarter` mock with a real
   `_StructuredToolAsyncTaskStarter` adapter wrapping a fake
   `StructuredTool`, and the seam is exercised end-to-end without
   booting a LangGraph runtime.

The implementer should still document the Option-A vs Option-B
decision and rationale in §Implementation Notes before the fix
lands — the recommendation here is informed by the empirical
F010.B precedent but the implementer may surface a reason to flip
the choice (e.g. the FW10-008 contract explicitly mandates
`invoke`-shaped calls and a full audit of the AsyncSubAgent surface
prefers Option A).

## Acceptance Criteria

- [ ] **AC-1 (root cause)**: Identify the exact call site of
  `tool.start_async_task(...)` (file + line — likely
  `src/forge/pipeline/dispatchers/autobuild_async.py:473` plus the
  Protocol declaration at line 155-189) and document the actual
  runtime type of the object being called (a LangChain
  `StructuredTool` resolved by name from `middleware.tools` at
  `src/forge/cli/_serve_production.py:139-142`). Document in the
  task body's §Investigation Findings before implementing.
- [ ] **AC-2 (decision)**: Document the chosen option (A or B) and
  rationale in §Implementation Notes before any production code
  diff lands. Reviewers should be able to verify the decision was
  made deliberately, not accidentally by which file got edited
  first.
- [ ] **AC-3 (implementation)**: Implement the fix per AC-2's
  decision. Whichever side changes, the dispatcher's call to
  `start_async_task` (or its `invoke`-replacement) succeeds against
  the production-built `AsyncSubAgentMiddleware` tool surface.
- [ ] **AC-4 (integration test)**: A test (extension of
  `tests/forge/test_pipeline_consumer_*.py` or `tests/cli/`) drives
  `dispatch_build` against a real
  `_build_async_subagent_middleware()`-built tool surface (or a
  `StructuredTool` mock that exposes the same API surface as the
  real one) and asserts the autobuild dispatcher reaches at least
  the `pipeline.build-started.*` publish (or, equivalently, returns
  a `task_id` from the dispatcher closure) without raising.
  Crucially, the test must exercise the production composition seam
  — not bypass it with a Protocol-shaped fake. The cheapest shape is
  the same pattern used by F010.B's
  `tests/cli/test_serve_deps_dispatch_real_persistence.py` —
  real composition with only the LangGraph runtime mocked at the
  innermost boundary.
- [ ] **AC-5 (regression)**: Full forge test suite passes
  (`pytest tests/forge/ tests/`). Existing FW10-002 / FW10-008
  unit tests continue to pass; if they were asserting a now-stale
  Protocol shape (Option A path), update them to match the new
  shape.
- [ ] **AC-6 (live wire validation)**: Pending operator action. Once
  landed, re-run jarvis runbook §6.2 + §7 against a forge image
  built from the new commit (alongside TASK-FRR-F010Db on the
  jarvis side, since notifications still won't render until the
  jarvis subscriber's workqueue-overlap regression is resolved);
  confirm a `pipeline.build-started.FEAT-43DE` envelope appears on
  `pipeline.>` for the queued correlation_id, and capture the new
  correlation_id in completion notes.

## Files Expected to Change

Conditional on AC-2 outcome:

**If AC-2 chooses Option A — change the caller (LangChain-native):**
- `src/forge/pipeline/dispatchers/autobuild_async.py` —
  `AsyncTaskStarter` Protocol replaced or relaxed; the
  `start_async_task(...)` call at line 473 becomes
  `await tool.ainvoke({...})` (or equivalent). Tests for the
  dispatcher updated.
- `src/forge/cli/_serve_production.py` — `_resolve_async_task_starter`
  may stay as-is (returns raw `StructuredTool`).
- A new test under `tests/forge/` or `tests/cli/` exercising the
  dispatch path against the real (or realistic) tool surface.

**If AC-2 chooses Option B — change the middleware-resolution wrapper:**
- `src/forge/cli/_serve_production.py` — `_resolve_async_task_starter`
  returns a thin adapter class (`_StructuredToolAsyncTaskStarter` or
  similar) that wraps the `StructuredTool` and exposes
  `start_async_task(subagent_name, context) -> str` as a named
  method delegating to `self._tool.invoke({...})` /
  `await self._tool.ainvoke({...})`. The dispatcher and its tests
  stay untouched.
- A new test under `tests/forge/` or `tests/cli/` covering the
  adapter unit (delegation correctness, async vs sync invocation
  shape, error propagation) and an integration test exercising the
  full dispatch path against the wrapped tool.

In both options, prefer extending an existing test file under
`tests/cli/test_serve_deps*.py` over creating a new one — F010.B's
`test_serve_deps_dispatch_real_persistence.py` is the closest
precedent.

## Investigation Findings (AC-1)

The grep + runtime-type confirmation surfaced one important correction
to the task brief's premise. Recording it here so reviewers see the
deeper-than-expected impedance the adapter has to bridge.

**Confirmed call site**:
- `src/forge/pipeline/dispatchers/autobuild_async.py:473` — the
  `start_async_task(...)` call that raises.
- `src/forge/pipeline/dispatchers/autobuild_async.py:155-189` — the
  `AsyncTaskStarter` Protocol declaration.
- `src/forge/cli/_serve_production.py:139-142` — `_resolve_async_task_starter`
  returns the raw `StructuredTool` looked up by name from
  `middleware.tools`.

**Confirmed runtime type** at the dispatcher's call site:
`langchain_core.tools.StructuredTool` instance built by
`StructuredTool.from_function(name="start_async_task", ...)` at
`deepagents/middleware/async_subagents.py:360`.

**Brief premise correction — the API mismatch is structural, not just
a method-name shape**. The task description says the middleware tool
takes `tool.invoke({"subagent_name": ..., "context": ...})`. It does
not. Inspecting the `StructuredTool`'s `args_schema`
(`StartAsyncTaskSchema` at `async_subagents.py:129-134`) and the
underlying `tool.func` / `tool.coroutine` shows the actual signature
is:

```python
def start_async_task(
    description: str,                     # natural-language prompt forwarded as messages[0].content
    subagent_type: str,                   # registered async-subagent name
    runtime: ToolRuntime,                 # LangGraph tool-call runtime (provides tool_call_id)
) -> str | Command:                       # Command on success, error string on failure
```

Three structural mismatches with the dispatcher's `AsyncTaskStarter`
Protocol:

1. **Argument names differ.** Protocol: `(subagent_name, context)`.
   Tool: `(subagent_type, description, runtime)`.
2. **No "context" parameter on the tool surface.** The tool only
   accepts a natural-language `description`; there is no slot for the
   dispatcher's structured `context_entries` / `lifecycle_emitter` /
   `correlation_id` payload. The dispatcher's launch_payload at
   `autobuild_async.py:466-472` carries `lifecycle_emitter` (an
   in-process Python object) which **cannot** cross to the LangGraph
   deployment regardless — so the adapter must drop it at the seam
   and the dispatcher's DDR-007 §Decision Option A (pass
   `lifecycle_emitter` as an in-process object on the launched task's
   context) is itself only achievable in a non-distributed runner
   (which `autobuild_runner` is not — it runs as a remote LangGraph
   thread per ADR-ARCH-031).
3. **Return type differs.** Protocol returns bare `task_id: str`;
   tool returns `Command` whose `update.async_tasks` dict carries the
   `task_id` as a key (or a `"Failed to launch ..."` error string).
4. **Calling without LangGraph runtime fails.** Empirically
   (Python 3.12, langgraph 1.x):
   `tool.invoke({"description": ..., "subagent_type": ...})` raises
   `TypeError: start_async_task() missing 1 required positional
   argument: 'runtime'`. The `ToolRuntime` is normally injected by
   the LangGraph tool-execution loop; it is NOT auto-synthesised by
   `StructuredTool.invoke()`.

The fake in `tests/forge/test_supervisor_async_subagent_wiring.py`
(`_FakeStructuredTool` carrying just a `.name`) and the fake in
`tests/forge/test_cli_serve_production.py` (`_FakeStartAsyncTaskTool`
carrying just a `.name`) both stop short of the call surface — they
exercise *only* the resolution-by-name behaviour of
`_resolve_async_task_starter`, never `tool.invoke(...)` against a
real or simulated middleware tool. That is exactly why the regression
slipped past unit tests until production wired the composer.

**Composition chain that delivers the StructuredTool to the
dispatcher** (re-confirmed):

```
forge.cli._serve_production.bind_production_serve
  → serve._build_async_subagent_middleware()        # builds AsyncSubAgentMiddleware
  → _resolve_async_task_starter(middleware)         # returns raw StructuredTool (BUG)
  → bind_production_dispatch_chain(async_task_starter=tool, ...)
  → compose_dispatch_chain (closure)
  → handle_message_dispatcher.dispatch_build
  → _make_autobuild_dispatcher_closure(async_task_starter=tool, ...)
  → dispatch_autobuild_async(async_task_starter=tool, ...)
  → tool.start_async_task(...)                      # AttributeError raised here
```

## Implementation Notes (AC-2 Decision: Option B — adapter at the composition seam, with broader translation than the task brief anticipated)

**Decision: Option B**. The investigation findings above only
strengthen the F010.B-precedent rationale — the impedance translation
the adapter has to perform is sufficiently non-trivial
(name-mapping + description synthesis + `Command` unpacking +
`ToolRuntime` synthesis) that it absolutely belongs at the seam and
not embedded in the dispatcher's call site (Option A would force the
dispatcher to know about all four translations).

**Why not Option A**: pushing all four translations into
`autobuild_async.py:473` would (i) couple the dispatcher to LangChain
+ LangGraph internals, (ii) require updating every FW10-002 test fake
to mimic the StructuredTool surface, and (iii) bury the translation
knowledge at the call site instead of at the seam where it is
discoverable next to the rest of the production composition wiring
(`_serve_deps_stage_log.py`, `_serve_deps_state_channel.py`,
`_serve_deps_forward_context.py`).

**The adapter must do four pieces of work** (not just one), folded
into the single Protocol method `start_async_task(subagent_name,
context) -> str`:

1. **Synthesize a `description` string** from the dispatcher's
   `context` mapping. Strategy: build a deterministic JSON-ish prompt
   that names the build_id / feature_id / correlation_id and embeds
   the resolved context_entries — this becomes the first user
   message the launched LangGraph thread receives, so it must be
   parseable by the autobuild_runner subagent. (Out of scope for
   this task: validating the runner can actually parse this prompt;
   the runner-side contract is FW10-002's territory and is currently
   `design_approved`.)
2. **Drop `lifecycle_emitter`** from the context before
   serialization. It is a Python object that cannot cross the
   LangGraph deployment boundary. The dispatcher's DDR-007 §Decision
   Option A in-process emitter contract is structurally incompatible
   with a remote-deployment runner; recording this here so a future
   reader does not "fix" the drop and reintroduce a `JSONDecodeError`
   at the SDK boundary.
3. **Synthesize a minimal `ToolRuntime`-shaped object** carrying just
   `tool_call_id` (a deterministic per-dispatch identifier — the
   dispatcher's `correlation_id` is the natural choice). The
   underlying `start_async_task` closure only reads
   `runtime.tool_call_id` to construct the `ToolMessage` inside the
   returned `Command` — no other runtime fields are touched. We
   bypass `tool.invoke({...})` (which fails without a real LangGraph
   runtime) and call `tool.func(description=..., subagent_type=...,
   runtime=<stub>)` directly so the StructuredTool wrapper does not
   try to drive LangGraph's runtime injection machinery.
4. **Unpack the `Command` return value** to extract the `task_id`.
   On success the tool returns a `langgraph.types.Command` whose
   `update["async_tasks"]` dict has exactly one key — the freshly-
   minted `task_id`. On failure (runner-side launch error caught by
   the closure's broad `except Exception`) the tool returns a string
   starting with `"Failed to launch async subagent"` — the adapter
   raises `RuntimeError` so the dispatcher's outer
   `pipeline_consumer.handle_message` ack-and-continue WARNING fires,
   matching the existing failure-mode observability.

**Sync vs async**: The dispatcher calls `start_async_task`
synchronously at line 473 (no `await`), so the adapter is sync and
delegates to `tool.func`. If the dispatcher ever changes to await the
call, the adapter can grow an `astart_async_task` mirror that
delegates to `tool.coroutine` — but the FW10-002 Protocol is sync
today and we do not change it.

**Files this lands in**:
- `src/forge/cli/_serve_async_task_starter.py` (NEW) — adapter module
  matching the `_serve_deps_stage_log.py` / `_serve_deps_state_channel.py`
  precedent: a private `_StructuredToolAsyncTaskStarter` class plus a
  public factory `build_async_task_starter(tool) -> AsyncTaskStarter`.
- `src/forge/cli/_serve_production.py` —
  `_resolve_async_task_starter` is updated to wrap the resolved tool
  through the adapter factory before returning. The resolution-by-
  name pre-check stays (so a missing tool still fails fast at boot
  with the same RuntimeError shape FW10-008 mandates).
- `tests/forge/test_serve_async_task_starter.py` (NEW) — unit
  coverage of the adapter's four translations.
- `tests/forge/test_cli_serve_production.py` — extended with a
  Protocol-conformance assertion: after `bind_production_serve`, the
  resolved `async_task_starter` is `isinstance(...,
  AsyncTaskStarter)` (the `runtime_checkable` Protocol). This is the
  cheapest-shape integration test that exercises the full production
  composition seam — it fails-loud the moment `_resolve_async_task_starter`
  stops returning a Protocol-shaped object, which is the exact
  regression class F010.E represents.

**Why we do not extend `tests/cli/test_serve_deps_dispatch_real_persistence.py`**:
The F010.B real-persistence test stops at the `start_async_task`
boundary — it asserts the dispatcher *reaches* the seam without
raising AttributeError, then mocks the starter (`_RecordingAsyncTaskStarter`)
to avoid needing a real LangGraph deployment. F010.E's regression
fires *at* that boundary, so reusing the F010.B pattern would just
move the mock from the dispatcher's seam to the adapter's underlying
tool — which is exactly the unit-test surface the new
`test_serve_async_task_starter.py` covers cleanly. The Protocol-
conformance assertion in `test_cli_serve_production.py` is the
narrowest seam-level test that catches the regression without
booting LangGraph.

## Implementation Notes (general)

- **The dispatcher is async**:
  `dispatch_autobuild_async` is declared `async def` (line 327 of
  `autobuild_async.py`). On the Option A path, the call becomes
  `await tool.ainvoke({...})`; on Option B, the wrapper's
  `start_async_task` either remains synchronous (since the Protocol
  declares it sync at line 169-189) by calling `tool.invoke({...})`,
  or — if the dispatcher's call site at line 473 is changed to be
  `await`-shaped — the wrapper goes async too. Confirm the call
  shape during implementation.
- **The dispatch-failure error path does not currently publish a
  terminal envelope** — see TASK-FORGE-FRR-F010F (sibling task,
  filed alongside this one). The two tasks are independent: F010.E
  is the actual fix to the tool-invocation API; F010.F is the
  safety net so that if F010.E's fix breaks again in future, the
  operator at least sees a `build-failed` envelope on the wire
  instead of silently dropped state. Land in either order.
- **Don't loosen the consumer's outer try/except**. The
  `pipeline_consumer.handle_message` ack-and-continue behaviour at
  line 470-506 is intentional (matches DDR-019's
  no-wedge-the-queue contract and ADR-ARCH-008's
  state-machine-owns-publish contract). The fix is to make the
  inner code path not raise, not to reduce the outer safety net.
  TASK-FORGE-FRR-F010F is the safety-net publish work; this task
  is the inner-codepath fix.
- **F010.B's adapter-wrapping precedent is the strongest
  argument for Option B**: read TASK-FORGE-FRR-F010B's
  §"Investigation Findings — AC-2 Decision: option (b) plus a thin
  wrapper, NOT bloating the facade" before deciding. The same three
  rationale points (codebase pattern of wrapping at the seam;
  semantic translation belongs in an adapter; the fake's contract
  IS the canonical Protocol) apply directly to F010.E.
- **Once F010.E lands**, the chat-driven jarvis runbook §7 should
  produce a complete `build-started + stage-complete*N +
  build-complete` envelope sequence (assuming the autobuild itself
  succeeds). That's the canonical Phase 7 close criterion the
  runbook tests against. F010.E is therefore the last
  *structural-blocker* between the production composer and the
  green-runbook close — once it lands, the only remaining gap is
  the jarvis-side subscription regression (TASK-FRR-F010Db).

## Ordering vs related tasks

This task has the following natural dependency order with its
siblings in the post-F010.A/B/C/D set:

1. ~~**TASK-FORGE-FRR-F010A** (apply migrations on boot)~~ —
   completed; satisfied (gives F010.E's integration test a real
   schema-bootstrapped DB to run against).
2. ~~**TASK-FORGE-FRR-F010B** (StageLogReader adapter)~~ —
   completed; F010.E is the next blocker the dispatcher hits after
   the StageLogReader contract is satisfied.
3. ~~**TASK-FORGE-FRR-F010C** (correlation_id threading)~~ —
   completed; the publish-site contract that TASK-FORGE-FRR-F010F
   (sibling) extends to the dispatch-failure path.
4. **This task (F010.E)** — the next dispatcher-time blocker.
   Independent of F010.F (sibling); land in either order.
5. **TASK-FORGE-FRR-F010F** — sibling safety-net for the
   dispatch-failure publish path. Independent of F010.E; land in
   either order. F010.F's AC-6 is verifiable today against the
   open F010.E failure mode.
6. **TASK-FW10-011** (end-to-end integration test, currently
   `design_approved` per README post-merge follow-up AC-12) —
   should land **after** F010.E and F010.F as the codified
   regression lock that asserts this exact failure mode never
   recurs.

## References

- **RESULTS file** (joint validation rerun, late afternoon
  2026-05-04 — Addendum 2):
  [`../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
  — see "Gap F010.E — `'StructuredTool' object has no attribute
  'start_async_task'` in autobuild dispatch path".
- **TASK-FIX-F010 (production-binding sibling)**:
  [`../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md`](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md)
  — introduced the wrapper that runs the production composer; this
  task closes the next deepest gap that the wired composer
  surfaces.
- **TASK-FORGE-FRR-F010B (predecessor)**:
  [`../../completed/TASK-FORGE-FRR-F010B/TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md`](../../completed/TASK-FORGE-FRR-F010B/TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md)
  — the StageLogReader adapter fix; its strategy
  (adapter-wrap-at-seam) is the recommended template for F010.E's
  Option B.
- **TASK-FORGE-FRR-F010F (sibling — safety net)**:
  [`TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md`](TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md)
  — co-filed companion. F010.F is the dispatch-failure publish
  safety net; F010.E is the immediate fix to the tool-invocation
  API.
- **TASK-FW10-002** (`autobuild_runner` AsyncSubAgent) — owner of
  the `AUTOBUILD_RUNNER_NAME` constant and the runner-side
  contract.
- **TASK-FW10-008** (AsyncSubAgentMiddleware wiring into the
  supervisor) — built the `_build_async_subagent_middleware()`
  factory at `src/forge/cli/serve.py:262` that returns the
  `StructuredTool`-shaped tools list.
- **TASK-FW10-011** (end-to-end integration test, `design_approved`)
  — the integration test that would have caught this; depends on
  this task closing first.
- **ADR-ARCH-031** — `AsyncSubAgent` / `start_async_task` decision;
  the architectural source of truth for the Protocol.
- **Source files**:
  - [`src/forge/pipeline/dispatchers/autobuild_async.py`](../../../src/forge/pipeline/dispatchers/autobuild_async.py)
    — `AsyncTaskStarter` Protocol (line 155-189) and the call site
    that raises (line 473).
  - [`src/forge/cli/_serve_production.py`](../../../src/forge/cli/_serve_production.py)
    — `_resolve_async_task_starter` at line 125-151 (returns the
    raw `StructuredTool`).
  - [`src/forge/cli/serve.py`](../../../src/forge/cli/serve.py)
    — `_build_async_subagent_middleware` factory at line 262 and
    `_make_autobuild_dispatcher_closure` at line 302.
  - [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py)
    — outer try/except that swallows the AttributeError (line
    470-506).
- **Run that surfaced this**:
  - **correlation_id**: `dfad8e7f-92af-4b5f-896f-ca75ad8343bf`
  - **Date**: 2026-05-04 (late afternoon rerun)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `a7eb9d5` (post `c066033` F010A + `751995f`
    F010B + `172c795` F010C + `a7eb9d5` F010D-forge)
  - **Image**: `forge:latest` = sha256 `2ae6f655ad08...`
  - **DB state at time of error**: schema bootstrapped automatically
    (F010.A win); QUEUED row written to `builds` table; F010.B
    StageLogReader composed at boot; AttributeError raised inside
    `dispatch_autobuild_async`'s `start_async_task` invocation,
    after the QUEUED row's `stage_log` pre-dispatch entry was
    recorded.
