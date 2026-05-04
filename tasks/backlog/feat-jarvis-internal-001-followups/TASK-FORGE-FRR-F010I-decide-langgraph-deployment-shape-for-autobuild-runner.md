---
id: TASK-FORGE-FRR-F010I
title: "Decide LangGraph deployment shape for autobuild_runner (sidecar URL / hand-rolled in-process ASGI / add langgraph_api dep)"
status: review_complete
created: 2026-05-04T19:30:00Z
updated: 2026-05-04T20:00:00Z
priority: high
task_type: review
review_results:
  mode: decision
  depth: standard
  recommendation: B.1
  rationale: "langgraph-api's own README contraindicates production use ('rapid development and testing… for production use, see the various deployment options'); B.3 would embed a maintainer-disclaimed dev/test artifact (Elastic 2.0, 30-package transitive tree) as forge runtime. B.2 ruled out on maintenance-burden cliff (re-implementing langgraph-sdk wire shape). B.1 by remaining viability — the path deepagents/langgraph-sdk were designed for."
  followup_task: TASK-FORGE-FRR-F010J
  optional_sibling_task: TASK-FORGE-FRR-F010K
  report_path: .claude/reviews/TASK-FORGE-FRR-F010I-review-report.md
tags:
  - forge-serve
  - async-subagent
  - autobuild-runner
  - asgi-transport
  - in-process-invocation
  - deployment-config
  - deepagents
  - langgraph-sdk
  - langgraph-api
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - decision-mode
parent_feature: FEAT-FORGE-010
parent_task: TASK-FORGE-FRR-F010H
related_tasks:
  - TASK-FW10-002        # autobuild_runner async subagent definition (where the compiled graph is)
  - TASK-FW10-008        # AsyncSubAgentMiddleware wiring (where this registration lives)
  - TASK-FORGE-FRR-F010E # StructuredTool->AsyncTaskStarter adapter
  - TASK-FORGE-FRR-F010F # safety-net publish path
  - TASK-FORGE-FRR-F010G # async coroutine path switch
  - TASK-FORGE-FRR-F010H # investigation that filed this review (parent)
correlation_id: bf697f49-3114-4c90-ae62-63936b8c53bf
discovered_on:
  date: 2026-05-04
  context: "F010H investigation falsified the 'thread compiled graph through registration' hypothesis (AsyncSubAgent has no graph field, langgraph_sdk.get_client has no app= kwarg, langgraph_api package is not installed). Three viable Option B sub-paths remain — this task picks one before the implementation companion lands."
context_files:
  - TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - src/forge/cli/serve.py
  - src/forge/cli/_serve_production.py
  - src/forge/cli/_serve_async_task_starter.py
  - src/forge/subagents/autobuild_runner.py
  - src/forge/pipeline/dispatchers/autobuild_async.py
test_results:
  status: n/a_review_task
---

# Task: Decide LangGraph deployment shape for `autobuild_runner` (review-mode)

## TL;DR

F010H's mandatory investigation falsified the "thread compiled graph
through `AsyncSubAgent` registration" hypothesis. Three viable Option
B sub-paths remain for closing the in-process ASGI transport gap that
makes `_StructuredToolAsyncTaskStarter.astart_async_task` raise
`'NoneType' object is not callable` on every dispatch. This is a
**decision-mode review** — pick **B.1**, **B.2**, or **B.3** before
the implementation companion task lands.

## Why this is a review and not a fix

F010H's investigation findings (recorded in
[F010H §Implementation Notes](TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md#investigation-findings-ac-1--2026-05-04-evening))
demonstrated that the original 1-line registration fix is impossible:

- `deepagents.middleware.async_subagents.AsyncSubAgent` TypedDict
  (deepagents 0.5.3, lines 34-68) has only five fields: `name`,
  `description`, `graph_id`, `url` (NotRequired), `headers`
  (NotRequired). **No `graph` / `app` / `runnable` /
  `compiled_graph` field.**
- `langgraph_sdk.get_client` (langgraph-sdk 0.3.13,
  `_async/client.py:29-140`) has signature
  `(*, url=None, api_key=NOT_PROVIDED, headers=None, timeout=None)`
  — **no `app=` kwarg**.
- `langgraph_api` package is **NOT installed** in the forge venv —
  `python3 -c "import langgraph_api"` raises `ModuleNotFoundError`,
  so `get_client(url=None)`'s first branch
  (`from langgraph_api.server import app`) falls through to the
  fallback that registers an `ASGITransport(app=None,
  root_path="/noauth")` and raises `'NoneType' object is not callable`
  on the first request.

The three viable Option B sub-paths to fix this each have different
deployment / dependency / operational implications. Picking the
right one needs a deliberate review rather than an opportunistic
implementation choice.

## The three Option B sub-paths

### B.1 — Sidecar `langgraph dev` / `langgraph up` server

**Shape:** Run a separate `langgraph dev` (or `langgraph up`) process
in a sidecar container or pod sibling. It serves the
`autobuild_runner` graph at `langgraph.json`'s graphs entry. The
forge daemon's `AsyncSubAgent` registration sets
`url="http://localhost:8124"` (or whatever port the sidecar binds).

**Code change:**
- Add a `FORGE_AUTOBUILD_RUNNER_URL` env var and corresponding
  `ServeConfig` field.
- `_build_async_subagent_middleware()` reads the URL from config and
  passes `url=<config.autobuild_runner_url>` into the
  `AsyncSubAgent` registration dict.
- Possibly `bind_production_serve` validation that the URL is set
  before the daemon attaches the consumer (fail-fast).

**Deployment changes:**
- New container in the forge-prod pod (or sibling docker-compose
  service) running `langgraph dev` with forge's `langgraph.json`
  mounted.
- Healthcheck on the sidecar before forge's healthz reports ready.
- Operator runbook updated to start the sidecar alongside the daemon.
- `scripts/build-image.sh` may need to emit a sidecar image too.

**Pros:**
- This is the deployment shape deepagents was designed for. No
  fighting the framework; the URL-based path is well-tested upstream.
- Clean separation of concerns — the supervisor process and the
  subagent runtime are isolated.
- The autobuild_runner can be horizontally scaled independently of
  the supervisor.
- LangGraph SDK's existing thread/run/persistence machinery works
  out of the box (no reimplementation).

**Cons:**
- Operational footprint grows from one container to two
  (forge-prod + langgraph-runner-sidecar).
- Inter-process communication adds latency and a failure mode (sidecar
  unreachable → builds queue indefinitely).
- The runbook becomes a multi-container affair.
- The state-channel write in `dispatch_autobuild_async` is in-process
  (SQLite), but the autobuild_runner runs out-of-process. Crash-
  recovery semantics need re-verification (FW10-007's "stage_log
  before start_async_task" invariant might need adjustment).

### B.2 — Hand-rolled in-process ASGI app + `configure_loopback_transports`

**Shape:** Set
`os.environ["__LANGGRAPH_DEFER_LOOPBACK_TRANSPORT"] = "true"` BEFORE
the middleware is constructed. The middleware's `_ClientCache`
creates its `ASGITransport(app=None, ...)` and registers it on the
module-level `_registered_transports` list. After construction, call
`langgraph_sdk._shared.utilities.configure_loopback_transports(app)`
with a hand-rolled ASGI app that handles the LangGraph SDK's HTTP
contract by routing to the compiled `autobuild_runner` graph.

**Code change:**
- New module `forge.cli._serve_loopback_app` that builds a
  Starlette / FastAPI app implementing the LangGraph SDK request
  shape:
  - `POST /threads` → mint a thread_id
  - `POST /threads/{thread_id}/runs` → kick off
    `autobuild_runner.graph.ainvoke(...)` and return a run_id
  - `GET /threads/{thread_id}/runs/{run_id}` → return run status +
    output values
  - `POST /threads/{thread_id}/runs/{run_id}/cancel` → cancel the
    background task
  - `GET /threads/{thread_id}` → return thread state
  - At minimum, the four endpoints deepagents'
    `start/check/update/cancel/list_async_task` tools call.
- Wiring in `_serve_production.bind_production_serve`:
  set the env var, build the middleware (registering deferred
  transports), build the loopback app, call
  `configure_loopback_transports(app)`.

**Deployment changes:**
- None (single-container deploy preserved).

**Pros:**
- Single-container deploy preserved.
- No new operational surface for the operator runbook.
- Direct access to the in-process compiled graph; no IPC latency.

**Cons:**
- **Effectively re-implementing `langgraph_api`.** The LangGraph SDK's
  thread/run state machine is non-trivial — threads have values,
  runs have status transitions (`pending` → `running` →
  `success` / `error` / `cancelled` / `interrupted` /
  `timeout`), persistence semantics, multitask strategies
  (`interrupt`, `enqueue`, `reject`), and so on. The minimum viable
  hand-roll is significant code.
- High maintenance burden — every minor langgraph-sdk bump that
  changes the wire shape may break the hand-rolled app.
- Test surface grows substantially (we own the protocol, so we own
  every edge case).
- Weakest link: subtle differences from the real langgraph-api
  surface will produce hard-to-diagnose runtime failures.

### B.3 — Add `langgraph_api` as a forge dependency

**Shape:** Add `langgraph_api` (and its transitive deps) to forge's
`pyproject.toml`. The forge daemon imports / instantiates a
langgraph-api server in-process. The `AsyncSubAgent` registration
keeps `url=None`. The deepagents middleware's `get_client(url=None)`
finds the langgraph-api server's ASGI app via the
`from langgraph_api.server import app` import inside
`langgraph_sdk._async.client.get_client`, and the in-process ASGI
transport routes requests through it.

Alternative phrasing: same as B.2 but using the upstream
langgraph-api implementation instead of hand-rolling the ASGI app.

**Code change:**
- `pyproject.toml` adds `langgraph-api` as a runtime dep (not just
  optional).
- Possibly an init step in `bind_production_serve` that registers
  the autobuild_runner graph with the langgraph-api server (the
  exact shape depends on how langgraph-api exposes its registration
  surface — needs investigation as part of this review).
- May need
  `os.environ["__LANGGRAPH_DEFER_LOOPBACK_TRANSPORT"] = "true"` +
  `configure_loopback_transports(app)` if the import-time discovery
  doesn't pick up the in-process app.

**Deployment changes:**
- Container image grows (langgraph-api transitive deps include
  FastAPI, orjson, starlette middleware, etc. — easily 50+ MB
  uncompressed).
- Single-container deploy preserved.
- May need to expose a healthz endpoint for the embedded
  langgraph-api server (or confirm it's not exposing one we'd
  conflict with).

**Pros:**
- Uses the upstream langgraph-api implementation — no hand-rolled
  wire shape to maintain.
- Single-container deploy.
- Aligns with deepagents' own assumptions about how `url=None`
  works (the `from langgraph_api.server import app` first branch
  is precisely the path this option exercises).

**Cons:**
- Substantial new dependency tree. License / supply-chain review
  may be needed.
- Image size growth.
- langgraph-api may bring its own startup/shutdown ceremony that
  conflicts with forge's daemon lifecycle.
- The in-process langgraph-api server may need its own SQLite /
  postgres config for thread persistence — duplicating storage
  concerns with the existing forge SQLite.
- Possible version-skew issues between langgraph-api,
  langgraph-sdk, and langgraph itself if any sub-path is pinned.

## Decision criteria

The reviewer should pick the option that best satisfies (in priority
order):

1. **Operational simplicity** — fewest moving parts the operator has
   to deploy, monitor, and debug.
2. **Maintenance burden** — least amount of forge-owned code that
   tracks upstream protocol changes.
3. **Crash-recovery semantics preservation** — FW10-007's "stage_log
   before start_async_task" invariant should hold without rework.
4. **State-channel coherence** — the `async_tasks` state channel
   that the supervisor's reasoning loop reads via `check_async_task`
   should reflect ground truth without per-option special casing.
5. **Dependency footprint** — fewer / smaller new deps preferred.

A spike implementation (or even just a paper exercise looking at the
exact langgraph-api API surface) may be needed before the reviewer
can confidently rank B.3 against B.1 — note that as a sub-investigation
in the chosen option's implementation companion task if applicable.

## Acceptance Criteria

- [ ] **AC-1 (option-evaluation matrix)**: Score each option (B.1 /
  B.2 / B.3) against each of the five decision criteria. A short
  prose justification per cell (1-2 sentences) is enough; no
  numerical rubric needed.
- [ ] **AC-2 (decision)**: Document the chosen option and the single
  highest-weight reason that drove the choice. If a 2-of-3
  comparison was decisive (e.g. "ruled out B.2 because hand-roll
  complexity; ruled out B.3 because dependency review pending; B.1
  by elimination") record that reasoning.
- [ ] **AC-3 (companion-task spec)**: Specify the implementation
  companion task in enough detail that a fix-mode `/task-work` can
  pick it up without further design choices: file:line landings,
  config-schema additions, deployment-runbook updates, test plan
  outline.
- [ ] **AC-4 (operator runbook deltas)**: Enumerate the operator
  runbook updates the chosen option will require. Even "none" is a
  valid answer — record it explicitly so the F010-series
  followups thread cleanly.
- [ ] **AC-5 (file the implementation companion task)**: Open the
  fix-mode follow-up under
  `tasks/backlog/feat-jarvis-internal-001-followups/` with a
  task ID like `TASK-FORGE-FRR-F010J` (or whatever the next
  available F010-series letter is). Frontmatter must reference this
  review task as `parent_review`.

## References

- **Parent task that filed this review**:
  [`TASK-FORGE-FRR-F010H`](TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md)
  — see §Implementation Notes "Investigation findings (AC-1)" and
  "Decision (AC-2)" for the empirical work that constrains this
  decision.
- **Source-of-truth files** (re-read during review for context):
  - `src/forge/cli/serve.py:_build_async_subagent_middleware` (286-299)
  - `src/forge/cli/_serve_production.py:bind_production_serve` (168-282)
  - `src/forge/cli/_serve_async_task_starter.py:_StructuredToolAsyncTaskStarter` (205-353)
  - `src/forge/subagents/autobuild_runner.py:_build_runner_graph` (771-814)
  - `src/forge/pipeline/dispatchers/autobuild_async.py:dispatch_autobuild_async`
- **Third-party files** (read during F010H investigation):
  - `deepagents.middleware.async_subagents.AsyncSubAgent` (lines 34-68)
  - `deepagents.middleware.async_subagents._ClientCache.get_async`
    (lines 253-262)
  - `langgraph_sdk._async.client.get_client` (lines 29-140)
  - `langgraph_sdk._shared.utilities.configure_loopback_transports`
    (lines 200-206)
- **Operational evidence**:
  `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  Addendum 4 (correlation_id `bf697f49-3114-4c90-ae62-63936b8c53bf`).
- **Sibling tasks**: F010E (StructuredTool adapter), F010F
  (safety-net publish), F010G (async coroutine path switch), F010H
  (this review's parent investigation).
