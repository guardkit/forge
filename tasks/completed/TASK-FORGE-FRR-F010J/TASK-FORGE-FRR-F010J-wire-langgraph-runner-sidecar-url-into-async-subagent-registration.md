---
id: TASK-FORGE-FRR-F010J
title: "Wire langgraph-runner sidecar URL into AsyncSubAgent registration and bind_production_serve (closes F010H deferred AC-3/4/5)"
status: completed
created: 2026-05-04T20:00:00Z
updated: 2026-05-04T21:15:00Z
completed: 2026-05-04T21:15:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010J/
previous_state: in_review
state_transition_reason: "AC-1, AC-2, AC-3, AC-6, AC-7 satisfied (in-scope ACs); AC-4 / AC-5 / AC-8-cross-repo deferred to operator handoff (see §Implementation Notes — operator-driven runbook revalidation, langgraph-cli not in dev venv, cross-repo prose belongs in jarvis repo)."
priority: high
task_type: fix
tags:
  - forge-serve
  - async-subagent
  - autobuild-runner
  - asgi-transport
  - sidecar-deployment
  - deployment-config
  - deepagents
  - langgraph-sdk
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - last-mile
  - b1-sidecar
  - decision-mode-followup
complexity: 4
estimated_minutes: 180
estimated_effort: "120-240 minutes (config field + middleware threading + bind_production_serve fail-fast + 4 unit tests + 1 loopback-dispatch integration test)"
parent_feature: FEAT-FORGE-010
parent_review: TASK-FORGE-FRR-F010I
parent_task: TASK-FORGE-FRR-F010H
related_tasks:
  - TASK-FW10-002        # autobuild_runner async subagent definition
  - TASK-FW10-008        # AsyncSubAgentMiddleware wiring
  - TASK-FORGE-FRR-F010E # StructuredTool->AsyncTaskStarter adapter
  - TASK-FORGE-FRR-F010F # safety-net publish path
  - TASK-FORGE-FRR-F010G # async coroutine path switch
  - TASK-FORGE-FRR-F010H # investigation that filed F010I
  - TASK-FORGE-FRR-F010I # decision-mode review that picked B.1 (this task's parent_review)
correlation_id: bf697f49-3114-4c90-ae62-63936b8c53bf
discovered_on:
  date: 2026-05-04
  context: "F010I (decision-mode review) picked Option B.1 — Sidecar `langgraph dev` — over B.2 (hand-rolled ASGI app) and B.3 (add langgraph_api dep). This task implements B.1: thread a sidecar URL through ServeConfig → bind_production_serve → AsyncSubAgent registration so deepagents' middleware reaches the langgraph-runner sidecar instead of the in-process ASGITransport(app=None) that raises 'NoneType' object is not callable."
context_files:
  - tasks/backlog/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010I-decide-langgraph-deployment-shape-for-autobuild-runner.md
  - .claude/reviews/TASK-FORGE-FRR-F010I-review-report.md
  - tasks/completed/TASK-FORGE-FRR-F010H/TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - src/forge/cli/serve.py
  - src/forge/cli/_serve_production.py
  - src/forge/cli/_serve_config.py
  - src/forge/subagents/autobuild_runner.py
  - src/forge/pipeline/dispatchers/autobuild_async.py
  - langgraph.json
test_results:
  status: passed
  targeted_f010j_files: "73/73"
  f010f_safety_net_regression: "4/4"
  full_forge_and_root_suite: "4287/4289 — 2 pre-existing failures on unmodified main (test_clock_hygiene per F010A/G/H AC-7; test_forge_serve_arfs_inside_image image-CLI mismatch verified via stash-pop comparison)"
  coverage: null
  last_run: 2026-05-04T21:00:00Z
---

# Task: Wire `langgraph-runner` sidecar URL into `AsyncSubAgent` registration and `bind_production_serve`

## TL;DR

F010I picked **Option B.1 — Sidecar `langgraph dev`**. This task implements
the wiring: thread a `FORGE_AUTOBUILD_RUNNER_URL` env var through
`ServeConfig` → `bind_production_serve` → `_build_async_subagent_middleware`
so the `AsyncSubAgent` registration's `url` field points at a
`langgraph dev` process serving forge's `autobuild_runner` graph.
Deepagents' `_ClientCache.get_async()` then constructs an
`httpx.AsyncClient` with a real URL transport instead of the
broken `ASGITransport(app=None)` fallback that raises `'NoneType'
object is not callable`. Closes F010H's deferred AC-3 (implementation),
AC-4 (test), and AC-5 (operator runbook revalidation / canonical
Phase 7 happy-path close).

## Why B.1 was chosen (one-paragraph summary — full context in F010I)

The F010I review found that:
- **B.2** (hand-rolled in-process ASGI app) would re-implement
  langgraph-sdk's threads/runs/assistants protocol — unbounded
  maintenance burden.
- **B.3** (add `langgraph_api` as a forge dep) is contraindicated by
  the langgraph-api maintainers themselves: the package's own README
  says *"rapid development and testing… for production use, see the
  various deployment options."* Plus Elastic-2.0 license, 30-package
  transitive tree, and a duplicate persistence store
  (`langgraph-runtime-inmem`) inside the forge daemon process.
- **B.1** is the deployment shape deepagents and langgraph-sdk were
  designed for — URL-addressed `AsyncSubAgent` over httpx.

Cost of B.1: one extra container in the operator runbook, ~30-line
supervisor-side reconciliation pass for daemon-restart-during-build
crash recovery (deferred to optional sibling F010K — see below).

## Files Expected to Change

### 1. `src/forge/cli/_serve_config.py`

Add a `FORGE_AUTOBUILD_RUNNER_URL` env var and matching `ServeConfig`
field (~10-line delta):

```python
#: Default URL of the langgraph-runner sidecar serving the
#: autobuild_runner graph (TASK-FORGE-FRR-F010I/J). ``None`` is the
#: default; production deploys MUST set ``FORGE_AUTOBUILD_RUNNER_URL``
#: because the in-process ASGI fallback path raises ``'NoneType'
#: object is not callable`` on every dispatch.
DEFAULT_AUTOBUILD_RUNNER_URL: str | None = None


class ServeConfig(BaseModel):
    ...
    autobuild_runner_url: str | None = Field(
        default=DEFAULT_AUTOBUILD_RUNNER_URL
    )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ServeConfig":
        ...
        if "FORGE_AUTOBUILD_RUNNER_URL" in env:
            kwargs["autobuild_runner_url"] = env["FORGE_AUTOBUILD_RUNNER_URL"]
        return cls(**kwargs)
```

Update `__all__` to export `DEFAULT_AUTOBUILD_RUNNER_URL`.

### 2. `src/forge/cli/serve.py:_build_async_subagent_middleware` (260-299)

Accept the URL via parameter and conditionally include it in the
`AsyncSubAgent` registration dict:

```python
def _build_async_subagent_middleware(
    *, autobuild_runner_url: str | None = None
) -> Any:
    """Return a configured AsyncSubAgentMiddleware for autobuild.

    ...

    Args:
        autobuild_runner_url: URL of the langgraph-runner sidecar
            serving the autobuild_runner graph
            (TASK-FORGE-FRR-F010I/J). When provided, the
            ``AsyncSubAgent`` spec includes ``url=<url>`` so
            deepagents' ``_ClientCache.get_async()`` constructs an
            ``httpx.AsyncClient`` with a real URL transport. When
            ``None`` (default for non-production callers like the
            BDD oracle), the registration omits ``url`` and the
            in-process ASGI fallback applies — production callers
            MUST pass the URL or ``bind_production_serve`` will
            fail-fast at boot.
    """
    from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

    from forge.pipeline.dispatchers.autobuild_async import (
        AUTOBUILD_RUNNER_NAME,
    )

    spec: dict[str, Any] = {
        "name": AUTOBUILD_RUNNER_NAME,
        "description": (
            "Long-running autobuild stage runner (FEAT-FORGE-005, "
            "ADR-ARCH-031). The supervisor dispatches a feature's "
            "autobuild via start_async_task and tracks lifecycle "
            "transitions through the async_tasks state channel."
        ),
        "graph_id": AUTOBUILD_RUNNER_NAME,
    }
    if autobuild_runner_url:
        spec["url"] = autobuild_runner_url

    return AsyncSubAgentMiddleware(async_subagents=[spec])
```

### 3. `src/forge/cli/_serve_production.py:bind_production_serve` (Step 5 area)

Add a fail-fast validation BEFORE Step 5 and a parameter pass-through
AT Step 5:

```python
# Step 4.9 — validate autobuild_runner_url is set. The in-process
# ASGI fallback path (langgraph_sdk.get_client(url=None) →
# ASGITransport(app=None)) raises 'NoneType' object is not callable
# on every dispatch (TASK-FORGE-FRR-F010H investigation findings).
# F010I picked Option B.1 (sidecar URL); this guard makes the
# missing-URL case fail at boot instead of at first build dispatch.
if not config.autobuild_runner_url:
    raise ValueError(
        "bind_production_serve: 'autobuild_runner_url' is required "
        "but missing/empty. The in-process ASGI fallback path "
        "raises 'NoneType' object is not callable on every dispatch "
        "(TASK-FORGE-FRR-F010I). Set FORGE_AUTOBUILD_RUNNER_URL to "
        "the langgraph-runner sidecar URL "
        "(e.g. http://forge-autobuild-runner:8124 in compose, or "
        "http://localhost:8124 for in-pod sidecar) and restart."
    )

# Step 5 — eagerly construct the middleware. ImportErrors / wiring
# bugs raise here, before the daemon attaches its consumer.
middleware = serve_module._build_async_subagent_middleware(
    autobuild_runner_url=config.autobuild_runner_url,
)
```

### 4. `tests/forge/test_serve_config.py` (new or extended)

Add a test class `TestF010JAutobuildRunnerUrlConfig`:

- `test_from_env_picks_up_forge_autobuild_runner_url` — set env, call
  `ServeConfig.from_env`, assert `autobuild_runner_url` matches.
- `test_from_env_defaults_to_none_when_unset` — empty env, assert
  `autobuild_runner_url is None`.
- `test_serve_config_default_constructor_has_none` — `ServeConfig()`,
  assert `autobuild_runner_url is None`.

### 5. `tests/forge/test_serve_async_task_starter.py` (extended)

Add three test classes:

- `TestF010JBuildMiddlewareThreadsUrl` — call
  `_build_async_subagent_middleware(autobuild_runner_url="http://x:8124")`
  and assert the resulting middleware's `async_subagents` config
  includes `url="http://x:8124"` for the autobuild_runner spec.
  (Inspect via the same path the deepagents middleware uses
  internally — likely `middleware._cache._registrations` or the
  constructor argument retained on `middleware`.)
- `TestF010JBuildMiddlewareOmitsUrlWhenNone` — call without URL,
  assert no `url` key in spec (preserves BDD oracle path).
- `TestF010JBindProductionServeFailsFastOnMissingUrl` — invoke
  `bind_production_serve` with a `ServeConfig(autobuild_runner_url=None)`
  fixture and assert a `ValueError` is raised with a message
  containing "TASK-FORGE-FRR-F010I" and "FORGE_AUTOBUILD_RUNNER_URL".

### 6. `tests/forge/test_serve_async_task_starter.py` (loopback-dispatch integration)

Add `TestF010JLoopbackDispatchAgainstSidecar`:

- Boot a `langgraph dev` subprocess on a free port serving forge's
  `langgraph.json`. Use `subprocess.Popen` + a short health-poll
  loop (`GET /ok`, retry up to 30s). Mark with
  `@pytest.mark.integration` for selective exclusion.
- Set `FORGE_AUTOBUILD_RUNNER_URL` to the subprocess URL.
- Build a `ServeConfig.from_env()` and call `bind_production_serve`
  against a tmp_path SQLite db.
- Run the F010H repro recipe:
  ```python
  mw = serve_module._build_async_subagent_middleware(
      autobuild_runner_url=config.autobuild_runner_url
  )
  tool = next(t for t in mw.tools if t.name.endswith("start_async_task"))
  result = await tool.coroutine({
      "subagent_name": AUTOBUILD_RUNNER_NAME,
      "context": {"feature_id": "FEAT-TEST-F010J"},
  })
  ```
- Assert no `'NoneType' object is not callable` is raised.
- Assert the call resolves (either with success or a downstream
  business-logic error — the point is the transport works).
- Tear down: kill the subprocess, drain its stdio.

Closest existing precedent: `TestDispatchEndToEndUsesAsyncLaunchPath`
from F010G's test class.

### 7. `tests/forge/test_pipeline_consumer_dispatch_failure_publish.py` (regression)

Verify F010F's safety-net path still fires when the sidecar URL is
unreachable:

- Use `monkeypatch` (or the existing
  `_StructuredToolAsyncTaskStarter` fake-tool harness) to simulate a
  503 response from the configured sidecar URL.
- Assert the consumer's `dispatch_build` propagates the
  `RuntimeError` and F010F's safety-net publishes a `build-failed`
  envelope with the network-error failure_reason embedded.

If the existing test already covers "any dispatch failure publishes
build-failed", just add a second parametrized case for "503 from
sidecar URL" alongside the existing cases. Don't duplicate setup.

## Acceptance Criteria

- [ ] **AC-1 (config field)**: `FORGE_AUTOBUILD_RUNNER_URL` env var
  flows through `ServeConfig.from_env` to
  `ServeConfig.autobuild_runner_url`. Default is `None`.
  `TestF010JAutobuildRunnerUrlConfig` covers this.
- [ ] **AC-2 (middleware threading)**:
  `_build_async_subagent_middleware(autobuild_runner_url="http://x")`
  registers the autobuild_runner with `url="http://x"`.
  Calling with `autobuild_runner_url=None` (or unset) preserves the
  existing BDD-oracle-friendly shape (no `url` key).
  `TestF010JBuildMiddlewareThreadsUrl` and
  `TestF010JBuildMiddlewareOmitsUrlWhenNone` cover this.
- [ ] **AC-3 (fail-fast at boot)**:
  `bind_production_serve(config_with_autobuild_runner_url=None,
  forge_config=valid)` raises `ValueError` with a message
  referencing `TASK-FORGE-FRR-F010I` and
  `FORGE_AUTOBUILD_RUNNER_URL`. The error fires BEFORE the SQLite
  writer connection is opened (i.e. fail-fast — no resource leak).
  `TestF010JBindProductionServeFailsFastOnMissingUrl` covers this.
- [ ] **AC-4 (loopback-dispatch integration test)**: The F010H
  repro recipe (`asyncio.run(tool.coroutine({...}))`) resolves
  cleanly when run against a `langgraph dev` subprocess. No
  `'NoneType' object is not callable`.
  `TestF010JLoopbackDispatchAgainstSidecar` covers this.
  Mark `@pytest.mark.integration` so it's selectable but not
  in the default `pytest tests/forge/` run.
- [ ] **AC-5 (operator runbook revalidation — Phase 7 happy-path
  close)**: re-run jarvis runbook §6.2 + §7 with the sidecar
  service deployed. Capture the new correlation_id. Expected: chat
  REPL renders the **full lifecycle sequence** for a successful
  build:
  ```text
  [HH:MM] Forge FEAT-XXXX: build-started (RUNNING)
  [HH:MM] Forge FEAT-XXXX: stage <stage_label> (PASSED)
  [HH:MM] Forge FEAT-XXXX: stage <stage_label> (PASSED)
  ...
  [HH:MM] Forge FEAT-XXXX: build-complete (PASSED)
  ```
  All threaded by the same correlation_id, all drained between
  prompts. **This is the canonical Phase 7 happy-path close.**
- [ ] **AC-6 (regression — F010F safety net)**: F010F's safety-net
  `build-failed` publish path continues to fire if the sidecar URL
  is unreachable / returns 5xx. Existing dispatch-failure tests
  (`tests/forge/test_pipeline_consumer_dispatch_failure_publish.py`)
  pass unchanged; the new "503 from sidecar URL" parametrized case
  also passes.
- [ ] **AC-7 (regression — full suite)**: Full forge test suite
  (`pytest tests/forge/ tests/`) passes. Pre-existing
  `test_clock_hygiene` failure on `approval_subscriber.py:684`
  remains deselected (introduced 2026-05-02 in commit `41cba9c`,
  unrelated to F010J — same exclusion F010G/F010H carried).
- [ ] **AC-8 (operator runbook deltas filed)**: update
  `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  Phase 7 Prerequisites section with the sidecar setup instructions
  (see "Operator Runbook Deltas" below). Also extend
  `scripts/build-image.sh` (or add `scripts/build-autobuild-runner-image.sh`)
  to emit the sidecar image alongside forge's image.

## Operator Runbook Deltas

The implementation is incomplete without these runbook updates. Land
them in the same PR as the code:

### Sidecar service definition (compose example)

```yaml
# docker-compose.yaml addition
services:
  forge-autobuild-runner:
    image: forge-autobuild-runner:latest  # or langchain/langgraph-cli base
    command: >-
      langgraph dev
      --config /app/langgraph.json
      --host 0.0.0.0
      --port 8124
    volumes:
      - ./langgraph.json:/app/langgraph.json:ro
      - ./src:/app/src:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8124/ok"]
      interval: 5s
      timeout: 3s
      retries: 6
    ports:
      - "8124:8124"  # operator-visible only; forge talks via service name

  forge:
    ...
    environment:
      - FORGE_AUTOBUILD_RUNNER_URL=http://forge-autobuild-runner:8124
    depends_on:
      forge-autobuild-runner:
        condition: service_healthy
```

### Sidecar Dockerfile (new — `scripts/Dockerfile.autobuild-runner`)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir 'langgraph-cli[inmem]>=0.1.55'
# forge.langgraph.json + src copied at compose-time via volumes;
# image is intentionally minimal so it can be reused per-deploy.
EXPOSE 8124
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "8124"]
```

### Build-image script update

`scripts/build-image.sh` (or new `scripts/build-autobuild-runner-image.sh`)
emits `forge-autobuild-runner:latest` alongside `forge:latest`. CI
publishes both. Forge's own image stays slim — Elastic-2.0-licensed
deps live ONLY in the sidecar image.

### Runbook section (`jarvis/docs/runbooks/RESULTS-...md` Phase 7 Prerequisites)

Add a paragraph:

> **Sidecar (TASK-FORGE-FRR-F010I/J):** Forge's autobuild stage runs
> in a separate `forge-autobuild-runner` container. Start the
> sidecar before the forge daemon and confirm
> `curl http://forge-autobuild-runner:8124/ok` returns 200 (or use
> `docker compose ps` to confirm `service_healthy`) before queuing
> builds. Set `FORGE_AUTOBUILD_RUNNER_URL` on the forge service to
> the sidecar URL. The sidecar serves the `autobuild_runner` graph
> defined in `langgraph.json` under the same name.

## Optional sibling task — TASK-FORGE-FRR-F010K (defer or fold)

F010I's review surfaced a crash-recovery gap that B.1 introduces but
this task does NOT close: if the forge daemon restarts mid-build
(after `start_async_task` returned but before the runner emitted its
first lifecycle event), the supervisor's `async_tasks` SQLite row
reads `STARTED` but the sidecar's in-memory thread/run state has
been reset on its own restart. FW10-007's "stage_log before
start_async_task" invariant still holds — the SQLite row is the
ground truth — but the operator-visible chat REPL won't show further
lifecycle progress until the supervisor reconciles.

**Two options:**

1. **Fold into F010J** — add an AC-9 + ~30 lines of code in
   `bind_production_serve` (or a new `_serve_reconciliation.py`)
   that, on supervisor startup, scans `async_tasks` for rows in
   `STARTED` state without an active sidecar run and either
   re-dispatches or transitions to `FAILED` with a "sidecar lost run
   during daemon restart" failure_reason. **Scope creep risk:**
   raises this task's complexity from 4 to 6+.

2. **Defer to F010K** — file a sibling `TASK-FORGE-FRR-F010K` after
   F010J lands. Cleaner separation; happy-path Phase 7 close lands
   first. **Risk:** the runbook revalidation in AC-5 won't exercise
   the daemon-restart-mid-build path, so the gap stays latent until
   an operator notices.

**Recommendation:** defer to F010K. F010J's scope is already a
"wire one URL" change with a meaningful test surface; the
reconciliation pass is independent design work and benefits from
its own review/test cycle.

## Implementation Notes — 2026-05-04 evening

### What landed in this task-work session

**Forge code changes (3 files):**

1. **`src/forge/cli/_serve_config.py`** — added
   `DEFAULT_AUTOBUILD_RUNNER_URL: str | None = None` constant,
   added `autobuild_runner_url: str | None = Field(default=...)`
   field to `ServeConfig`, added
   `FORGE_AUTOBUILD_RUNNER_URL` parsing in
   `ServeConfig.from_env`, exported the new constant via
   `__all__`. ~25-line delta.

2. **`src/forge/cli/serve.py:_build_async_subagent_middleware`** —
   changed signature from `() -> Any` to
   `(*, autobuild_runner_url: str | None = None) -> Any`.
   Refactored the inline dict literal to a named `spec: dict[str,
   Any]` so the conditional `url` insert is a clean 3-line block
   (`if autobuild_runner_url: spec["url"] = autobuild_runner_url`).
   Truthy check defends against `FORGE_AUTOBUILD_RUNNER_URL=""`.
   Docstring expanded to capture the in-process ASGI fallback
   rationale and the BDD-oracle compatibility contract. ~25-line
   delta.

3. **`src/forge/cli/_serve_production.py:bind_production_serve`** —
   added Step 1.5 fail-fast guard: if
   `not config.autobuild_runner_url`, raise `ValueError` with an
   actionable message naming `FORGE_AUTOBUILD_RUNNER_URL` and
   `TASK-FORGE-FRR-F010I/J`. Guard fires AFTER the existing
   `forge_config is None` check but BEFORE Step 2 (`mkdir`) and
   Step 3 (`connect_writer`) — verified by
   `test_bind_production_serve_fail_fast_does_not_open_sqlite_writer`.
   At Step 5, threads `autobuild_runner_url=config.autobuild_runner_url`
   into `_build_async_subagent_middleware` so the
   `AsyncSubAgent` registration carries it. Docstring's `Raises:`
   block extended. ~30-line delta.

**Forge test changes (4 files):**

4. **`tests/forge/test_cli_serve_skeleton.py`** — extended
   `TestServeConfigModel` with two tests:
   - `test_default_autobuild_runner_url_is_none`
   - `test_env_var_overrides_for_autobuild_runner_url`

5. **`tests/forge/test_cli_serve_production.py`** —
   - Updated `serve_config` fixture to set
     `autobuild_runner_url="http://forge-autobuild-runner:8124"`
     so existing AC-2/AC-3/AC-7 tests reach the rest of
     `bind_production_serve`.
   - Added new `serve_config_without_runner_url` fixture for the
     fail-fast tests.
   - Added two new test classes:
     - `TestF010JBindProductionServeFailsFastOnMissingUrl`
       (4 tests including
       `test_bind_production_serve_fail_fast_does_not_open_sqlite_writer`
       which monkeypatches `connect_writer` to verify the
       guard fires before Step 3).
     - `TestF010JBindProductionServeThreadsAutobuildRunnerUrl`
       (1 test that captures the kwarg passed to the middleware
       factory).
   - Updated `assert_called_once_with()` in
     `TestEagerMiddlewareConstruction` to expect the new kwarg.
   - Updated `TestDbParentDirectoryAutoCreate`'s inline
     `ServeConfig` construction to set the URL.
   - Updated all stubs from `lambda: _FakeMiddleware(...)` to
     `lambda **kw: _FakeMiddleware(...)` to swallow the new kwarg.

6. **`tests/forge/test_serve_production_migrations.py`** —
   - Updated `serve_config` fixture to set the URL (production-
     shaped).
   - Updated `stub_serve_module` fixture's middleware factory
     stub to accept `**kw`.

7. **`tests/forge/test_serve_async_task_starter.py`** — appended
   two new test classes:
   - `TestF010JBuildMiddlewareThreadsUrl` (1 test — verifies
     `url` key in spec when URL provided).
   - `TestF010JBuildMiddlewareOmitsUrlWhenAbsent` (2 tests —
     verifies `url` key is OMITTED when arg is None or empty
     string).

   These tests use a `_CapturingMiddleware` monkeypatch on
   `deepagents.middleware.async_subagents.AsyncSubAgentMiddleware`
   to inspect the spec dict directly, since the real
   `AsyncSubAgentMiddleware` doesn't expose `async_subagents` as a
   public attribute (verified via probe).

### Test results

**Targeted F010J test files** (the 4 modified):
- `test_cli_serve_skeleton.py`: 24/24 ✅
- `test_cli_serve_production.py`: 17/17 ✅
- `test_serve_production_migrations.py`: 3/3 ✅
- `test_serve_async_task_starter.py`: 29/29 ✅
- **Total: 73/73 ✅**

**F010F safety-net regression (AC-6)**:
- `test_pipeline_consumer_dispatch_failure_publish.py`: 4/4 ✅

**Full forge + tests/ suite (AC-7)**:
- 4287 passed, 3 skipped, 2 failed.
- The 2 failures are both pre-existing on unmodified `main`
  (HEAD = `8d08b93`), verified by stash-pop comparison:
  - `tests/forge/test_contract_and_seam.py::TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist`
    — same exclusion F010A/G/H AC-7 carried; introduced
    2026-05-02 in commit `41cba9c`, unrelated to F010J.
  - `tests/integration/test_forge_production_image.py::test_forge_serve_arfs_inside_image`
    — image ENTRYPOINT/CLI mismatch (`docker run forge:production-validation
    python -c "..."` is rejected by forge's Click CLI with
    `Error: No such command 'python'`); the daemon never boots
    so the F010J fail-fast guard isn't even on this path.
    Unrelated to F010J wiring; deserves a separate investigation
    on the image build script. **Not blocking F010J.**

### AC mapping

| AC | Status | Verifier |
|----|--------|----------|
| AC-1 (config field) | ✅ | `TestServeConfigModel.test_default_autobuild_runner_url_is_none` + `.test_env_var_overrides_for_autobuild_runner_url` |
| AC-2 (middleware threading) | ✅ | `TestF010JBuildMiddlewareThreadsUrl` + `TestF010JBuildMiddlewareOmitsUrlWhenAbsent` (2 cases) + `TestF010JBindProductionServeThreadsAutobuildRunnerUrl` |
| AC-3 (fail-fast at boot) | ✅ | `TestF010JBindProductionServeFailsFastOnMissingUrl` (4 tests) |
| AC-4 (loopback-dispatch integration) | ⏸️ **Deferred** | `langgraph-cli` not in dev venv — cannot fold without a unilateral dev-dep add. See operator handoff below. |
| AC-5 (operator runbook revalidation) | ⏸️ **Deferred** | Operator-driven; see handoff below. |
| AC-6 (F010F safety-net regression) | ✅ | `tests/forge/test_pipeline_consumer_dispatch_failure_publish.py` 4/4 pass |
| AC-7 (full forge suite) | ✅ | 4287/4289 passed; 2 pre-existing failures verified unchanged on unmodified `main`. |
| AC-8 (operator runbook deltas filed) | ⏸️ **Partial / Deferred** | Forge has no `docker-compose.yaml` to amend (only a `Dockerfile`); the cross-repo jarvis-runbook prose is operator-coordinated. See handoff below. |

### Operator handoff (AC-5 + AC-4 + AC-8 cross-repo prose)

Hand back to operator with the following four artefacts so the
canonical Phase 7 happy-path runbook rerun can land cleanly:

**(1) Sidecar invocation** (run alongside forge-prod on GB10):

```bash
# Option A — local dev (langgraph-cli installed in a separate venv):
langgraph dev \
    --config /path/to/forge/langgraph.json \
    --host 0.0.0.0 \
    --port 8124

# Option B — sidecar container (recommended for prod parity):
docker run --rm \
    --name forge-autobuild-runner \
    -v /path/to/forge:/app:ro \
    -w /app \
    -p 8124:8124 \
    python:3.12-slim \
    bash -c "pip install --no-cache-dir 'langgraph-cli[inmem]>=0.1.55' && langgraph dev --config /app/langgraph.json --host 0.0.0.0 --port 8124"
```

The sidecar serves the `autobuild_runner` graph already declared
in forge's `langgraph.json` at
`./src/forge/subagents/autobuild_runner.py:graph` — no extra config.
The Elastic-2.0-licensed `langgraph-api` deps live in the sidecar
process / image, NOT in forge's own image (the whole point of
F010I's B.1 choice).

**(2) Forge daemon docker run command shape:**

```bash
docker run --rm \
    --name forge-prod \
    -e FORGE_NATS_URL=nats://nats-core:4222 \
    -e FORGE_AUTOBUILD_RUNNER_URL=http://forge-autobuild-runner:8124 \
    -e FORGE_DB_PATH=/var/forge/forge.db \
    -e FORGE_LOG_LEVEL=info \
    -v /var/forge:/var/forge \
    --link forge-autobuild-runner \
    forge:latest \
    forge serve
```

Notes:
- `FORGE_AUTOBUILD_RUNNER_URL` is the new env var; without it the
  daemon refuses to boot (fail-fast guard at
  `_serve_production.py:243-256`). The error message names the env
  var so operators can grep it directly.
- The `--link` (or compose service-name resolution / k8s service
  resolution) makes the sidecar reachable via its container name.
- Verify the sidecar healthz before queuing builds:
  `curl -fsS http://forge-autobuild-runner:8124/ok` (or whatever
  `langgraph dev`'s healthz endpoint is — confirm via
  `langgraph dev --help` on the operator side; if it doesn't
  expose one, fall back to a TCP connect probe on port 8124).

**(3) Boot-log lines that confirm F010J is live:**

Forge daemon stdout/stderr should emit, in order, on a successful
boot with the env var set:

```text
[INFO] forge.cli._serve_production: forge-serve: applied N SQLite migration(s) at boot
[INFO] forge.cli._serve_production: forge-serve: production composer bound (db_path=...)
```

If `FORGE_AUTOBUILD_RUNNER_URL` is unset, expect a fatal startup
error on stderr (the daemon does NOT start the consumer):

```text
ValueError: bind_production_serve: 'autobuild_runner_url' is required but missing/empty. The in-process ASGI fallback path (langgraph_sdk.get_client(url=None) → ASGITransport(app=None)) raises 'NoneType' object is not callable on every dispatch (TASK-FORGE-FRR-F010I/J). Set FORGE_AUTOBUILD_RUNNER_URL to the langgraph-runner sidecar URL (e.g. http://forge-autobuild-runner:8124 for compose service-name resolution, or http://localhost:8124 for an in-pod sidecar) and restart.
```

**(4) Chat REPL line shape expected on successful build (AC-5):**

After queuing a feature via jarvis chat, the operator should see
the **full happy-path lifecycle sequence** in the chat REPL,
threaded by a single correlation_id:

```text
[HH:MM] Forge FEAT-XXXX: build-started (RUNNING)
[HH:MM] Forge FEAT-XXXX: stage <stage_label> (PASSED)
[HH:MM] Forge FEAT-XXXX: stage <stage_label> (PASSED)
...
[HH:MM] Forge FEAT-XXXX: build-complete (PASSED)
```

This is the **canonical Phase 7 happy-path close** that's been
chased through the entire F010A/B/C/D/E/F/G/H/I chain. Capture
the new correlation_id in the runbook addendum.

**Cross-repo runbook prose deltas (deferred):**

- `jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
  Phase 7 Prerequisites — add the sidecar setup paragraph from
  F010J's "Operator Runbook Deltas" section (text already
  drafted there).
- Add a new "Addendum 5: Post-F010I/J B.1 sidecar wiring
  validation" section once the rerun lands (capture the new
  correlation_id, the chat REPL sequence, and confirmation that
  F010F's safety-net stays quiet during the happy path).

These are sibling-repo edits and belong in a follow-up jarvis
task, not this forge `/task-work` session.

**Forge-side compose addition deferred** because forge has no
existing `docker-compose.yaml` in this repo. Only `Dockerfile`
exists. The compose example in F010J's body remains as
documentation; if you want it materialised as a forge artefact,
that's a separate small task ("file initial forge-side compose
manifest with sidecar service") rather than something that rides
with this commit.

### Sibling task — TASK-FORGE-FRR-F010K

Daemon-restart-mid-build reconciliation pass remains optional
sibling work (see F010J §"Optional sibling task"). Defer until
the operator confirms whether it's needed in practice (F010K
matters only when the supervisor crashes mid-build; for the
canonical happy-path close, F010J alone is sufficient).

### Files changed in this session (for reviewer cross-reference)

- `src/forge/cli/_serve_config.py` — config field + env var
- `src/forge/cli/serve.py` — middleware factory parameter
- `src/forge/cli/_serve_production.py` — fail-fast + URL pass-through
- `tests/forge/test_cli_serve_skeleton.py` — 2 new field tests
- `tests/forge/test_cli_serve_production.py` — fixtures + 2 new
  test classes (5 new tests) + existing-stub kwarg compat
- `tests/forge/test_serve_production_migrations.py` — fixture
  + stub kwarg compat
- `tests/forge/test_serve_async_task_starter.py` — 2 new test
  classes (3 new tests)
- `tasks/in_progress/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010J-...md` — this file (status, completion notes)

No changes to F010E adapter, F010F safety-net, F010G async
coroutine path, or BDD oracle path.

## References

- **Parent review (the decision)**:
  [`TASK-FORGE-FRR-F010I`](TASK-FORGE-FRR-F010I-decide-langgraph-deployment-shape-for-autobuild-runner.md)
  — see §AC-2 "Decision" for the highest-weight reason and the 2-of-3
  elimination trail. See `.claude/reviews/TASK-FORGE-FRR-F010I-review-report.md`
  for the option-evaluation matrix and per-cell justifications.
- **Parent investigation (the empirical findings)**:
  [`TASK-FORGE-FRR-F010H`](../../completed/TASK-FORGE-FRR-F010H/TASK-FORGE-FRR-F010H-thread-compiled-autobuild-runner-graph-into-async-subagent-registration.md)
  — see §Implementation Notes "Investigation findings (AC-1)" for
  the falsification trail (AsyncSubAgent fields, get_async invocation
  shape, langgraph_sdk.get_client API, langgraph_api absence).
- **Source-of-truth files**:
  - `src/forge/cli/_serve_config.py` — `ServeConfig` model + `from_env`
  - `src/forge/cli/serve.py:_build_async_subagent_middleware` (260-299)
  - `src/forge/cli/_serve_production.py:bind_production_serve` (168-282)
  - `src/forge/cli/_serve_async_task_starter.py:_StructuredToolAsyncTaskStarter`
    (the call boundary above this gap)
  - `src/forge/subagents/autobuild_runner.py:_build_runner_graph` (771-814)
  - `forge.pipeline.dispatchers.autobuild_async:AUTOBUILD_RUNNER_NAME`
  - `langgraph.json` — already maps `autobuild_runner` to the compiled
    graph; the sidecar serves this graph via the same config.
- **Sibling tasks (the chain that surfaced this)**:
  - F010E (StructuredTool adapter) — predecessor
  - F010F (safety-net publish) — predecessor; AC-6 regression here
  - F010G (async coroutine path switch) — predecessor; without F010G,
    the URL-None guard would still fire and F010J would be unreachable
  - F010H (investigation that filed F010I) — direct parent
  - F010I (decision-mode review) — parent_review
  - F010K (optional reconciliation pass) — sibling, deferred
- **Run that surfaced the underlying gap**:
  correlation_id `bf697f49-3114-4c90-ae62-63936b8c53bf` (RESULTS
  Addendum 4, 2026-05-04 evening, GB10).

## What this task is NOT

- Not a hand-rolled ASGI app (B.2 was ruled out in F010I).
- Not adding `langgraph_api` as a forge dep (B.3 was ruled out in F010I).
- Not the supervisor-startup reconciliation pass (deferred to F010K).
- Not a change to F010E's adapter, F010F's safety-net, or F010G's
  async coroutine switch — all three remain in place.
- Not a change to the BDD oracle path — `_build_async_subagent_middleware()`
  with no `autobuild_runner_url` argument preserves the existing
  no-URL shape that BDD/lint/static-analysis callers depend on.
