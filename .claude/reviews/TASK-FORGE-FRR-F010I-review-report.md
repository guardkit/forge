---
review_task: TASK-FORGE-FRR-F010I
parent_review: null
review_mode: decision
review_depth: standard
generated: 2026-05-04
reviewer: claude-opus-4-7
correlation_id: bf697f49-3114-4c90-ae62-63936b8c53bf
recommendation: B.1
---

# Review Report — TASK-FORGE-FRR-F010I

**Task:** Decide LangGraph deployment shape for `autobuild_runner`
(sidecar URL / hand-rolled in-process ASGI / add `langgraph_api` dep).

## Executive Summary

**Recommendation: B.1 — Sidecar `langgraph dev`.**

The single highest-weight reason: the `langgraph-api` PyPI package's
own README explicitly steers users away from in-process production use
("This package implements the LangGraph API for rapid development and
testing… For production use, see the various deployment options").
B.3 would embed a dev/test artifact (Elastic-2.0 licensed, 30-package
transitive tree including grpcio / opentelemetry / uvicorn / uvloop)
as forge's production runtime — which is the misuse case the
maintainers spell out in the very first paragraph of the package
description.

B.2 is ruled out independently on maintenance-burden grounds: the
LangGraph SDK's threads/runs/assistants HTTP shape is non-trivial and
hand-rolling it makes forge own a moving protocol.

B.1 is the deployment shape deepagents and langgraph-sdk were designed
for. The URL-based `AsyncSubAgent` registration path is the well-tested
upstream path. The cost is one extra container in the operator runbook,
which is a bounded one-time delta.

## Empirical context

This review is calibrated by F010H's investigation findings (verified
on 2026-05-04 against deepagents 0.5.3, langgraph-sdk 0.3.13, and the
forge venv):

- `AsyncSubAgent` TypedDict has five fields only: `name`, `description`,
  `graph_id`, `url` (NotRequired), `headers` (NotRequired). No graph
  field. (`deepagents/middleware/async_subagents.py:34-68`)
- `langgraph_sdk.get_client` signature is
  `(*, url=None, api_key=NOT_PROVIDED, headers=None, timeout=None)`.
  No `app=` kwarg. (`langgraph_sdk/_async/client.py:29-140`)
- `langgraph_api` is **not installed** in the forge venv
  (`python3 -c "import langgraph_api"` → `ModuleNotFoundError`).
- `langgraph.json` already exposes `autobuild_runner` at
  `./src/forge/subagents/autobuild_runner.py:graph`, so `langgraph dev`
  can serve it without further config.

Additional empirical findings introduced by this review:

- **`langgraph-api` 0.8.5** (current) ships under **Elastic License
  2.0** (License-File: LICENSE, Metadata-Version 2.4). Elastic 2.0
  restricts offering the software as a managed service to third
  parties — not blocking for forge-as-internal-jarvis-tool, but a
  yellow flag for any future commercialization.
- **`langgraph-api`'s own README** (PyPI metadata, line ~38): "This
  package implements the LangGraph API for **rapid development and
  testing**… **For production use, see the various deployment
  options** for the LangGraph API, which are backed by a
  production-grade database." This is a maintainer-stated
  contraindication for B.3.
- **30-package transitive tree**: cloudpickle, cryptography, grpcio,
  grpcio-tools, grpcio-health-checking, httptools, jsonschema-rs,
  langchain-core, langgraph-checkpoint, langgraph-runtime-inmem,
  langgraph-sdk, langgraph, langsmith, opentelemetry-api,
  opentelemetry-exporter-otlp-proto-http, opentelemetry-sdk, orjson,
  protobuf, pyjwt, sse-starlette, starlette, structlog, tenacity,
  truststore, uuid-utils, uvicorn, uvloop, watchfiles, zstandard,
  httpx (already present). Image growth ~150-250 MB uncompressed.
- **`langgraph-runtime-inmem`** is `langgraph-api`'s persistence
  backend — a separate in-memory thread/run store with disk
  snapshotting. Embedding `langgraph-api` therefore introduces a
  second persistence store inside the forge daemon process,
  duplicating the role forge's `async_tasks` SQLite table plays.

## AC-1 — Option-evaluation matrix

Scoring rubric: ✅ wins this criterion / ➖ ties / ❌ loses this
criterion. Score is relative to the other two options for the same
criterion (lexicographic by criterion priority, not weighted-sum).

| # | Criterion (priority order) | B.1 Sidecar | B.2 Hand-rolled ASGI | B.3 Add `langgraph_api` |
|---|---|---|---|---|
| 1 | Operational simplicity | ❌ | ✅ | ✅ |
| 2 | Maintenance burden | ✅ | ❌ | ➖ |
| 3 | Crash-recovery preservation | ❌ | ✅ | ✅ |
| 4 | State-channel coherence | ➖ | ✅ | ❌ |
| 5 | Dependency footprint | ✅ | ✅ | ❌ |

**Per-cell justifications:**

### Criterion 1 — Operational simplicity

- **B.1 ❌** — Adds a sidecar (`langgraph dev`/`langgraph up` running
  alongside forge-prod). Operator runbook becomes multi-container.
  Healthcheck + start-order dependency. ~30-line delta in
  docker-compose / k8s manifest.
- **B.2 ✅** — Single-container deploy preserved. No new operational
  surface for the runbook.
- **B.3 ✅** — Single-container deploy preserved. Image grows but
  runtime ops is unchanged.

### Criterion 2 — Maintenance burden

- **B.1 ✅** — Forge owns ~5 lines of code (config field +
  pass-through to registration). The langgraph-api wire shape is
  upstream-maintained inside the sidecar process.
- **B.2 ❌** — Cliff. Forge would re-implement `langgraph_api`'s
  threads/runs/assistants HTTP shape (POST /threads, run state
  machine pending→running→success/error/cancelled/interrupted,
  multitask strategies interrupt/enqueue/reject, cancel propagation,
  list endpoints, …). Every minor langgraph-sdk wire-shape bump is
  a forge bug. Test surface owned entirely by forge.
- **B.3 ➖** — Wire shape upstream-owned, but forge inherits a
  30-package transitive tree to track for CVEs and version-skew
  (between langgraph-api / langgraph-sdk / langgraph itself).
  Comparable in net effort to B.1; different shape.

### Criterion 3 — Crash-recovery preservation

The invariant is FW10-007's "stage_log before start_async_task" —
forge writes a stage_log row in SQLite *before* the async dispatch,
so on a daemon crash mid-dispatch the row provides ground truth for
replay.

- **B.1 ❌** — Splits the dispatch boundary across processes. If the
  sidecar crashes after `start_async_task` returns but before the
  runner emits its first lifecycle event, the supervisor's stage_log
  reads "STARTED" but the sidecar has lost the run. Reconciliation
  on supervisor startup needs distinct logic. Net delta: ~30 lines
  in a startup reconciliation pass; not a deal-breaker but
  non-trivial.
- **B.2 ✅** — In-process. Process death is atomic; the FW10-007
  invariant holds without rework.
- **B.3 ✅** — In-process. Same atomic-death property as B.2.

### Criterion 4 — State-channel coherence

The supervisor's reasoning loop reads the `async_tasks` channel via
`check_async_task` to know what each running autobuild is doing.

- **B.1 ➖** — Two stores of truth (forge SQLite + sidecar's
  langgraph-api inmem state). Behind a clean process boundary, with
  the URL hop as the single integration point. Divergence is
  detectable and recoverable on the supervisor side.
- **B.2 ✅** — Single in-process state. The hand-rolled ASGI app can
  write through to forge's `async_tasks` SQLite directly — coherent
  by construction.
- **B.3 ❌** — Two stores of truth in the same process: forge's
  `async_tasks` SQLite + langgraph-runtime-inmem's
  threads/runs/assistants. langgraph-api's persistence can't be
  bypassed; it's how the package works. The duplication is
  by-design and not a bug forge can paper over.

### Criterion 5 — Dependency footprint

- **B.1 ✅** — Zero new forge runtime deps. New deployment artifact
  (sidecar image) but the deps live there, not in forge's image.
- **B.2 ✅** — Zero new third-party runtime deps. (Maybe a Starlette
  pin, already a transitive of langgraph itself.)
- **B.3 ❌** — 30-package transitive tree including grpcio /
  grpcio-tools / opentelemetry / cryptography / uvloop / uvicorn.
  Image growth ~150-250 MB uncompressed. Elastic 2.0 license
  obligation. Maintainer-stated contraindication for production
  use.

## AC-2 — Decision

**Chosen: B.1 — Sidecar `langgraph dev`.**

**Highest-weight reason:** `langgraph-api`'s own maintainers
explicitly contraindicate B.3 in the package's README — "rapid
development and testing… for production use, see the various
deployment options." Embedding a dev/test artifact as forge's
production runtime would be a known misuse, with the supply-chain
weight (30-package tree, Elastic 2.0 licensing) compounding the
maintainer-stated contraindication. Even if B.3's transient
single-container appeal looked attractive on paper, the act of
typing `langgraph-api` into `pyproject.toml` is acknowledging that
forge would be running a tool against its own published intent.

**2-of-3 elimination trail:**

1. **B.2 ruled out first** on maintenance-burden cliff. Re-implementing
   the LangGraph SDK threads/runs/assistants protocol is a
   parallel-implementation project, not a fix. Scope creep risk over
   the project's lifetime is severe (every supervisor reasoning-loop
   feature that touches run state forces protocol-shape work in
   forge). Tests for "we own the protocol" are unbounded.
2. **B.3 ruled out second** on the maintainer contraindication +
   licensing + transitive-dep cost. The criterion-1 / criterion-3
   wins for B.3 over B.1 don't outweigh the package-misuse signal.
3. **B.1 by remaining viability** — and it's the path deepagents and
   langgraph-sdk were designed for (URL-addressed `AsyncSubAgent`
   with `langgraph_sdk.get_client(url=...)` over httpx). No fighting
   the framework.

**Tradeoff acknowledged:** B.1 loses on the highest-priority criterion
(operational simplicity). The cost is bounded — one extra container,
two-line operator runbook delta, ~30 lines of supervisor-side
reconciliation logic for crash-recovery. None of these are unbounded;
all three are well-trodden patterns in the
distributed-systems-microservice-supervisor space.

## AC-3 — Implementation companion task spec

Implementation companion: **TASK-FORGE-FRR-F010J — Wire B.1 sidecar
URL into `AsyncSubAgent` registration and `bind_production_serve`.**

### File:line landings

1. **`src/forge/cli/_serve_config.py`** — add a single field to
   `ServeConfig` and matching `from_env` parsing (~10-line delta):
   ```python
   #: URL of the langgraph-runner sidecar serving the autobuild_runner
   #: graph. ``None`` is rejected at boot by ``bind_production_serve``
   #: since the in-process ASGI fallback path raises
   #: ``'NoneType' object is not callable`` (TASK-FORGE-FRR-F010I).
   DEFAULT_AUTOBUILD_RUNNER_URL: str | None = None

   class ServeConfig(BaseModel):
       ...
       autobuild_runner_url: str | None = Field(
           default=DEFAULT_AUTOBUILD_RUNNER_URL
       )

   # In from_env:
   if "FORGE_AUTOBUILD_RUNNER_URL" in env:
       kwargs["autobuild_runner_url"] = env["FORGE_AUTOBUILD_RUNNER_URL"]
   ```

2. **`src/forge/cli/serve.py:_build_async_subagent_middleware`**
   (lines 260-299) — accept the URL via parameter and thread it into
   the `AsyncSubAgent` registration:
   ```python
   def _build_async_subagent_middleware(
       *, autobuild_runner_url: str | None = None
   ) -> Any:
       ...
       return AsyncSubAgentMiddleware(
           async_subagents=[
               {
                   "name": AUTOBUILD_RUNNER_NAME,
                   "description": (...),
                   "graph_id": AUTOBUILD_RUNNER_NAME,
                   **(
                       {"url": autobuild_runner_url}
                       if autobuild_runner_url else {}
                   ),
               }
           ],
       )
   ```

3. **`src/forge/cli/_serve_production.py:bind_production_serve`**
   (Step 5 area, around the eager-construct middleware call):
   - Before Step 5, validate `config.autobuild_runner_url` is not
     `None`/empty; raise `ValueError` with a fail-fast operator
     message if so:
     ```
     "bind_production_serve: 'autobuild_runner_url' is required;
     the in-process ASGI fallback path raises 'NoneType' object is
     not callable on every dispatch (TASK-FORGE-FRR-F010I).
     Set FORGE_AUTOBUILD_RUNNER_URL to the langgraph-runner sidecar
     URL (e.g. http://localhost:8124) and restart."
     ```
   - At Step 5, pass the URL through:
     ```python
     middleware = serve_module._build_async_subagent_middleware(
         autobuild_runner_url=config.autobuild_runner_url,
     )
     ```

4. **`tests/forge/test_serve_async_task_starter.py`** — add three
   test classes:
   - `TestF010IServeConfigWiring`: asserts `ServeConfig.from_env`
     picks up `FORGE_AUTOBUILD_RUNNER_URL`.
   - `TestF010IBuildMiddlewareThreadsUrl`: builds the middleware via
     `_build_async_subagent_middleware(autobuild_runner_url="http://x")`
     and asserts the `AsyncSubAgent` spec contains
     `url="http://x"`.
   - `TestF010IBindProductionServeFailsFastOnMissingUrl`: invokes
     `bind_production_serve` with `autobuild_runner_url=None` and
     asserts a `ValueError` mentioning F010I is raised.

5. **`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`**
   — verify F010F's safety-net path still fires when the sidecar URL
   is unreachable (regression). Use a mocked httpx transport that
   returns 503; assert the `build-failed` envelope publishes with
   the F010F failure reason embedded.

### Config-schema additions

- One Pydantic v2 field on `ServeConfig`: `autobuild_runner_url:
  str | None` with `FORGE_AUTOBUILD_RUNNER_URL` env var override.
- Default `None` (rejected at boot — fail-fast). Operators must set
  the env var; there is no implicit "use loopback" default.

### Deployment-runbook updates

See AC-4 below.

### Test plan outline

1. **Unit (config wiring)** — `TestF010IServeConfigWiring` above.
2. **Unit (middleware threading)** —
   `TestF010IBuildMiddlewareThreadsUrl` above.
3. **Unit (fail-fast)** —
   `TestF010IBindProductionServeFailsFastOnMissingUrl` above.
4. **Integration (loopback dispatch)** — boot a `langgraph dev`
   subprocess on a free port serving forge's `langgraph.json`,
   point `FORGE_AUTOBUILD_RUNNER_URL` at it, run the F010H repro
   recipe (`asyncio.run(tool.coroutine({...}))`), assert no
   `'NoneType' object is not callable` and at least one
   `pipeline.build-started.<feature_id>` envelope is published.
   (Closest pattern: existing
   `TestDispatchEndToEndUsesAsyncLaunchPath` from F010G.)
5. **Regression (F010F safety net)** — dispatch with the sidecar URL
   pointing at a non-listening port; assert F010F's `build-failed`
   publish path fires with a network-error failure_reason.
6. **Operator runbook revalidation** — re-run jarvis runbook §6.2 +
   §7 on a real GB10 deploy with the sidecar wired. Capture the new
   correlation_id. Expected: full happy-path build sequence
   (`build-started + stage-complete*N + build-complete`) renders in
   the chat REPL. This satisfies F010H's deferred AC-5.

### Crash-recovery reconciliation

Defer to a sibling task **TASK-FORGE-FRR-F010K** (file alongside
F010J): on supervisor startup, scan `async_tasks` table for rows in
`STARTED` state without a corresponding active sidecar run; either
re-dispatch the build or transition the row to `FAILED` with a
"sidecar lost run during daemon restart" failure_reason. ~30 lines
of code. Not blocking for F010J's happy-path close — F010K can land
in a follow-up wave.

## AC-4 — Operator runbook deltas

**Required:**

1. **Sidecar service definition.** Add a `forge-autobuild-runner`
   service (docker-compose) or pod sibling (k8s) that runs
   `langgraph dev --port 8124 --host 0.0.0.0` with forge's
   `langgraph.json` mounted. The image can be built from a slim
   base + `pip install langgraph-cli[inmem]` (the CLI bundles
   `langgraph-api` as a transitive). Sidecar source — the container
   image — is the only place the Elastic 2.0 dep lives, isolated
   from forge's own image.

2. **Env var on the forge service.** `FORGE_AUTOBUILD_RUNNER_URL=
   http://forge-autobuild-runner:8124` (compose service-name
   resolution) or
   `http://localhost:8124` (in-pod sidecar via shared loopback).

3. **Healthcheck on the sidecar.** Standard
   `GET http://<sidecar>/ok` (langgraph-api's healthz). Forge's own
   healthz reports ready only after the sidecar healthz is green —
   add this dependency in the orchestrator manifest, not in forge
   itself (forge already fails fast at `bind_production_serve` if
   the URL is unset; runtime reachability is the operator's
   problem).

4. **Image build.** `scripts/build-image.sh` keeps forge's image
   slim (no langgraph-api dep). A new
   `scripts/build-autobuild-runner-image.sh` (or a section in the
   existing build script) builds the sidecar image. CI publishes
   both images.

5. **Operator runbook (`jarvis/docs/runbooks/...`)** — under
   "Prerequisites" section, add:
   > **Sidecar:** Forge's autobuild stage runs in a separate
   > `forge-autobuild-runner` container (TASK-FORGE-FRR-F010I/J).
   > Start the sidecar before the forge daemon and confirm
   > `curl http://localhost:8124/ok` returns 200 before queuing
   > builds. Set `FORGE_AUTOBUILD_RUNNER_URL` on the forge service
   > to the sidecar URL.

6. **Phase 7 happy-path close** — re-run §6.2 + §7 with the sidecar
   wired. The expected chat REPL sequence is unchanged:
   ```
   [HH:MM] Forge FEAT-XXXX: build-started (RUNNING)
   [HH:MM] Forge FEAT-XXXX: stage <stage_label> (PASSED)
   ...
   [HH:MM] Forge FEAT-XXXX: build-complete (PASSED)
   ```
   All threaded by the same correlation_id, all drained between
   prompts.

**Not changed:** the chat REPL surface, the wire-envelope schema,
the SQLite migration set, the F010F safety-net path, the F010A
schema-bootstrap flow.

## AC-5 — File implementation companion task

Companion task to be filed: **TASK-FORGE-FRR-F010J — Wire B.1
sidecar URL into `AsyncSubAgent` registration and
`bind_production_serve` (closes F010H deferred AC-3/4/5)**.

Location:
`tasks/backlog/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010J-wire-langgraph-runner-sidecar-url-into-async-subagent-registration.md`

Frontmatter must include:
- `task_type: fix`
- `parent_review: TASK-FORGE-FRR-F010I`
- `parent_task: TASK-FORGE-FRR-F010H` (the investigation that
  surfaced the gap)
- `parent_feature: FEAT-FORGE-010`
- `correlation_id: bf697f49-3114-4c90-ae62-63936b8c53bf` (continuity
  with the F010H/F010I correlation chain)
- Tags include `b1-sidecar`, `decision-mode-followup`,
  `feat-forge-010-followup`, `first-real-run-followup`.
- ACs mirroring F010H's deferred AC-3/4/5 plus the matrix above
  (config field, middleware threading, fail-fast,
  loopback-dispatch integration test, F010F regression, runbook
  revalidation).

Sibling task to be considered: **TASK-FORGE-FRR-F010K —
Supervisor-startup reconciliation pass for sidecar-lost runs**
(deferred from F010J for scope reasons; not blocking happy-path
close).

## Context Used

No knowledge graph context was queried for this review (Graphiti
unavailable in this session). The review draws on:

- F010H investigation findings (`tasks/completed/TASK-FORGE-FRR-F010H/`)
- Current code surface (`src/forge/cli/serve.py`,
  `src/forge/cli/_serve_production.py`,
  `src/forge/cli/_serve_config.py`)
- `langgraph.json` graph registry
- `langgraph-api 0.8.5` PyPI metadata + README
- `langgraph-sdk 0.3.13` source (verified in venv)
- `deepagents 0.5.3` source (verified in venv)

## References

- Parent investigation: `TASK-FORGE-FRR-F010H` (completed)
- Sibling tasks: F010E, F010F, F010G (the chain that surfaced this gap)
- Operational evidence: `RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  Addendum 4, correlation_id `bf697f49-3114-4c90-ae62-63936b8c53bf`
- LangGraph CLI deployment options:
  https://langchain-ai.github.io/langgraph/concepts/deployment_options/
  (referenced by langgraph-api's own README as the production path)
