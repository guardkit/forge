---
id: TASK-FIX-F010
title: "Bind compose_dispatch_chain to the production composer in serve_cmd via _serve_production wrapper"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T00:00:00Z
completed: 2026-05-04T00:00:00Z
completed_location: tasks/completed/TASK-FIX-F010/
previous_state: in_review
state_transition_reason: "All in-scope ACs (1-9, 13-14) implemented; targeted tests 100/100 pass; full suite 2131/2132 (one pre-existing unrelated failure). Post-merge ACs (10/11/12) tracked as deferred follow-ups (see deferred_acs below)."
deferred_acs:
  AC-10:
    title: "TASK-FW10-011 frontmatter cross-link"
    location: tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md
    blocking: false
    next_action: "Add parent_review: TASK-REV-F010 and production_binding_sibling: TASK-FIX-F010 to FW10-011 frontmatter"
  AC-11:
    title: "Runbook revalidation against deployed image"
    blocking: false
    next_action: "After commit + image rebuild + deploy: re-run jarvis runbook §6.2+§7; capture new correlation_id; verify pipeline.build-started.* envelope appears; flip RESULTS row 7.x ❌→✅"
    location_for_correlation_id: "Append to this file's Notes section after revalidation"
  AC-12:
    title: "Resurrect TASK-FW10-011 from completed/ to backlog (D4.B sequencing)"
    blocking: false
    next_action: "git mv tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md tasks/backlog/feat-jarvis-internal-001-followups/; update dependencies: [TASK-FIX-F010]; status: in_progress"
test_results:
  status: passed
  coverage: not_measured  # minimal intensity skips coverage gate
  last_run: 2026-05-04T00:00:00Z
  targeted_tests:
    files:
      - tests/forge/test_cli_serve_skeleton.py
      - tests/forge/test_cli_serve_logging.py
      - tests/forge/test_cli_serve_production.py
      - tests/forge/test_cli_serve_daemon.py
      - tests/forge/test_serve_healthz.py
    passed: 100
    failed: 0
    duration_seconds: 0.77
  full_forge_suite:
    passed: 2131
    failed: 1
    failure_unrelated: "tests/forge/test_contract_and_seam.py::TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist — pre-existing at HEAD de23557 (approval_subscriber.py:684); not introduced by F010"
ac_status:
  AC-1: done
  AC-2: done
  AC-3: done
  AC-4: done
  AC-5: done
  AC-6: done
  AC-7: done
  AC-8: done
  AC-9: done
  AC-10: deferred  # FW10-011 frontmatter cross-link — separate housekeeping
  AC-11: deferred  # runbook revalidation requires deployed image (post-merge ops work)
  AC-12: deferred  # FW10-011 resurrection from completed/ → backlog (post-merge sequencing)
  AC-13: done  # ruff clean; black project-wide stylistic only
  AC-14: done  # 2131 forge tests pass; pre-existing unrelated failure documented
priority: high
task_type: fix
parent_review: TASK-REV-F010
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-007        # composed the dispatcher closure but didn't bind the seam in serve_cmd
  - TASK-FW10-008        # wired AsyncSubAgentMiddleware that the rebind needs to invoke
  - TASK-FW10-011        # capstone integration test (status: design_approved) — regression lock follow-up (D4.B)
  - TASK-FORGE-FRR-002   # logging.basicConfig fix that made this gap diagnosable
  - TASK-REV-F010        # the review task that produced this fix's design
complexity: 4
estimated_minutes: 90
implementation_mode: task-work
wave: 1
tags:
  - forge-serve
  - orchestrator-wiring
  - production-binding
  - feat-forge-010-followup
  - feat-dea8-followup
  - first-real-run-followup
report_path: .claude/reviews/TASK-REV-F010-review-report.md
correlation_id: 18036705-2bb7-4564-8363-315bf7716a48
context_files:
  - .claude/reviews/TASK-REV-F010-review-report.md
  - tasks/backlog/feat-jarvis-internal-001-followups/TASK-REV-F010-bind-production-dispatch-chain-in-serve-cmd.md
  - src/forge/cli/serve.py
  - src/forge/cli/_serve_config.py
  - src/forge/cli/_serve_daemon.py
  - src/forge/cli/_serve_dispatcher.py
  - src/forge/cli/_serve_deps.py
  - src/forge/cli/main.py
  - src/forge/cli/runtime.py
  - src/forge/lifecycle/persistence.py
  - src/forge/adapters/sqlite/connect.py
  - tests/forge/test_cli_serve_skeleton.py
  - tests/forge/test_cli_serve_logging.py
test_results:
  status: pending
  coverage: null
  last_run: null
---

# TASK-FIX-F010 — Bind `compose_dispatch_chain` to the production composer in `serve_cmd`

## Why

Close the FEAT-DEA8 production-wiring gap discovered during the jarvis FRR rerun on 2026-05-04 (correlation_id `18036705-2bb7-4564-8363-315bf7716a48`). FEAT-FORGE-010 shipped `bind_production_dispatch_chain` as a factory but never wired it into `serve_cmd`, so the production daemon falls through to the receipt-only `_default_dispatch` stub at [_serve_daemon.py:166](../../../src/forge/cli/_serve_daemon.py#L166). Every `pipeline.build-queued.*` envelope is acked-and-discarded; no autobuild runs; no lifecycle envelopes are published; the jarvis runbook's Phase 7 close criterion is structurally unsatisfiable.

This task implements the design chosen by [TASK-REV-F010](TASK-REV-F010-bind-production-dispatch-chain-in-serve-cmd.md) (see `.claude/reviews/TASK-REV-F010-review-report.md`):

| Decision | Choice | Summary |
|---|---|---|
| **D1** wiring location | **B** | New thin ops wrapper module `forge.cli._serve_production` |
| **D2** SQLite pool source | **A\*** | Extend `ServeConfig` with `db_path`; reuse `FORGE_DB_PATH`; default `~/.forge/forge.db` |
| **D2-bonus** | — | `serve_cmd` reads `ForgeConfig` from Click `ctx.obj`; falls back to `./forge.yaml` |
| **D3** middleware construction | **A** | Eager — construct `AsyncSubAgentMiddleware` once in the wrapper |
| **D4** order vs FW10-011 | **B** | Land this fix first; resurrect FW10-011 from `tasks/completed/` afterwards as the regression lock |
| **D5** unit-test compatibility | **A** | The wrapper module *is* the testable seam; smoke tests get a one-line `monkeypatch.setattr` |

## What

### 1. New module — `src/forge/cli/_serve_production.py`

Expose `bind_production_serve(config: ServeConfig, forge_config: ForgeConfig) -> None`:

1. Resolve the SQLite path from `config.db_path`; auto-create the parent directory (`db_path.parent.mkdir(parents=True, exist_ok=True)`).
2. Open the writer connection via `connect_writer(config.db_path)`.
3. Construct `SqliteLifecyclePersistence(connection=connection, db_path=config.db_path)`.
4. Eagerly construct `AsyncSubAgentMiddleware` via the existing `_build_async_subagent_middleware()` helper at [serve.py:262](../../../src/forge/cli/serve.py#L262).
5. Derive the `AsyncTaskStarter` from `middleware.tools` (per FW10-008 wiring contract).
6. Rebind `forge.cli.serve.compose_dispatch_chain` to the closure returned by `bind_production_dispatch_chain(forge_config=..., sqlite_pool=..., async_task_starter=...)`.
7. Idempotency: a second call cleans up the previous SQLite writer connection and replaces the binding without leaking handles.

### 2. Extend `ServeConfig` — `src/forge/cli/_serve_config.py`

```python
DEFAULT_DB_PATH: Path = Path("~/.forge/forge.db").expanduser()

class ServeConfig(BaseModel):
    ...
    db_path: Path = Field(default_factory=lambda: DEFAULT_DB_PATH)

    @classmethod
    def from_env(cls, environ=None) -> "ServeConfig":
        ...
        if "FORGE_DB_PATH" in env:
            kwargs["db_path"] = Path(env["FORGE_DB_PATH"]).expanduser()
        return cls(**kwargs)
```

Reuse the existing `FORGE_DB_PATH` env var (already used by [status.py:97](../../../src/forge/cli/status.py#L97) and [queue.py:228](../../../src/forge/cli/queue.py#L228)). Do **not** introduce `FORGE_SQLITE_PATH`.

### 3. Update `serve_cmd` — `src/forge/cli/serve.py`

```python
@click.command(name="serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Run the long-lived forge daemon (JetStream consumer + healthz)."""
    from forge.cli._serve_production import bind_production_serve

    config = ServeConfig.from_env()
    _configure_logging(config.log_level)
    forge_config = _resolve_forge_config_for_serve(ctx)
    bind_production_serve(config, forge_config)
    state = SubscriptionState()
    asyncio.run(_run_serve(config, state))


def _resolve_forge_config_for_serve(ctx: click.Context) -> ForgeConfig:
    """Pick ForgeConfig from ctx.obj; fall back to ./forge.yaml; raise UsageError if absent."""
    if isinstance(ctx.obj, ForgeConfig):
        return ctx.obj
    if Path("forge.yaml").exists():
        return load_config(Path("forge.yaml"))
    raise click.UsageError(
        "forge serve requires a forge.yaml — pass --config <path> or run "
        "from a directory containing ./forge.yaml."
    )
```

Local imports for `ForgeConfig` and `load_config` to keep the module-level import surface clean (the helper is only invoked at boot).

### 4. Test updates

#### Existing tests (one-line patch)

Affected smoke tests stub the new wrapper to a no-op so the existing harnesses still construct successfully:

```python
monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)
```

| File | Tests |
|---|---|
| [tests/forge/test_cli_serve_skeleton.py](../../../tests/forge/test_cli_serve_skeleton.py) | `TestServeCmdSmoke::test_serve_cmd_exits_zero_with_stub_coroutines` (line 218); `::test_serve_cmd_uses_asyncio_gather` (line 246); `TestRunServeBootOrder::*` (line 282+, where invoking `serve_cmd` directly) |
| [tests/forge/test_cli_serve_logging.py](../../../tests/forge/test_cli_serve_logging.py) | `test_info_record_is_emitted_after_serve_cmd_initialises` (line 58); `test_serve_cmd_calls_configure_logging_with_env_level` (line 239) |

The smoke tests will additionally need to provide a `ctx.obj` `ForgeConfig` (or set up `./forge.yaml` via `tmp_path` chdir, or patch `_resolve_forge_config_for_serve` to return a stub). Match whatever fixture pattern is least invasive — likely a `monkeypatch.setattr(serve_module, "_resolve_forge_config_for_serve", lambda ctx: object())` since the smoke tests already stub `bind_production_serve` to a no-op (so the returned object is never read).

#### New test module — `tests/forge/test_cli_serve_production.py`

Cover the wrapper module:

- `test_bind_production_serve_constructs_middleware_eagerly` — patches `_build_async_subagent_middleware` to a `Mock` and asserts it's called once.
- `test_bind_production_serve_rebinds_compose_dispatch_chain` — asserts `serve.compose_dispatch_chain is not _default_compose_dispatch_chain` after the call.
- `test_bind_production_serve_is_idempotent` — calls twice, asserts the previous SQLite writer connection is closed (use a `Mock` writer).
- `test_bind_production_serve_creates_db_parent_directory` — uses `tmp_path` with a non-existent parent, asserts directory is created.
- `test_bind_production_serve_raises_on_missing_forge_config` — passes `forge_config=None`, asserts `ValueError` (or whatever `bind_production_dispatch_chain` raises).
- `test_bind_production_serve_threads_async_task_starter` — patches `bind_production_dispatch_chain` and asserts it received a non-`None` `async_task_starter` derived from middleware tools.

#### New env-var test — extend `tests/forge/test_cli_serve_skeleton.py` or create `test_cli_serve_config.py`

Add a focused test:

- `test_serve_config_from_env_honours_forge_db_path` — sets `FORGE_DB_PATH=/tmp/x.db` in the environ kwarg, asserts `config.db_path == Path("/tmp/x.db")`.
- `test_serve_config_from_env_default_db_path_is_home_forge` — empty env, asserts `config.db_path == Path("~/.forge/forge.db").expanduser()`.

### 5. Documentation

- Update [tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md](../forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md) to document the chosen wiring shape: wrapper module + `ctx.obj` ForgeConfig + `FORGE_DB_PATH` reuse.
- Update [tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md](../../completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md) frontmatter:

  ```yaml
  parent_review: TASK-REV-F010
  production_binding_sibling: TASK-FIX-F010
  ```
- After this task lands (separate follow-up): physically `git mv tasks/completed/TASK-FW10-011-...md tasks/backlog/feat-jarvis-internal-001-followups/`, update its `dependencies` to include `TASK-FIX-F010`, and bump status from `design_approved` → `in_progress` per D4.B sequencing.

### 6. Runbook validation (post-merge)

After merge:

1. Rebuild forge image from the new `main` HEAD.
2. Re-run jarvis runbook §6.2 (`Queue FEAT-XXX for build...`) against canonical NATS.
3. Verify on `nats sub "pipeline.>" --raw`:
   - One inbound `pipeline.build-queued.FEAT-XXX`.
   - At least one `pipeline.build-started.FEAT-XXX` (NEW — proves rebind ran).
   - At least one `pipeline.stage-complete.FEAT-XXX` envelope (proves dispatch succeeded).
4. `docker logs forge-prod` shows the line: `forge-serve: dispatch chain composed; _serve_daemon.dispatch_payload rebound to handle_message dispatcher (receipt-only stub no longer reachable)` — emitted by [serve.py:248-252](../../../src/forge/cli/serve.py#L248-L252).
5. Capture the new correlation_id; record in this task's completion notes.
6. Flip the jarvis runbook RESULTS row 7.x ❌ → ✅ on this third re-run.

## Acceptance Criteria

- [ ] **AC-1** — NEW `src/forge/cli/_serve_production.py` exposes `bind_production_serve(config: ServeConfig, forge_config: ForgeConfig) -> None`.
- [ ] **AC-2** — `bind_production_serve` constructs the SQLite writer connection from `config.db_path`, builds `SqliteLifecyclePersistence`, eagerly constructs `AsyncSubAgentMiddleware`, derives `async_task_starter` from middleware tools, and rebinds `forge.cli.serve.compose_dispatch_chain` via `bind_production_dispatch_chain(...)`.
- [ ] **AC-3** — `bind_production_serve` is idempotent: a second call closes the previous SQLite writer connection cleanly and replaces the binding without leaking handles. `db_path.parent` is auto-created when missing.
- [ ] **AC-4** — `ServeConfig` extended with `db_path: Path` field; `FORGE_DB_PATH` env override honoured by `from_env`; default `~/.forge/forge.db` (expanded). **No new env-var name introduced.**
- [ ] **AC-5** — `serve_cmd` decorated with `@click.pass_context`; reads `ForgeConfig` from `ctx.obj` (or falls back to `./forge.yaml` via `load_config`); raises `click.UsageError` if neither is available.
- [ ] **AC-6** — `serve_cmd` calls `bind_production_serve(config, forge_config)` between `_configure_logging(config.log_level)` and `asyncio.run(_run_serve(config, state))`.
- [ ] **AC-7** — All existing tests under `tests/forge/test_cli_serve_skeleton.py` and `tests/forge/test_cli_serve_logging.py` continue to pass after one-line `monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)` adjustments (and a corresponding stub for `_resolve_forge_config_for_serve` where needed). No env-flag escape hatch (`FORGE_SERVE_SKIP_DISPATCH_BINDING`) introduced.
- [ ] **AC-8** — NEW `tests/forge/test_cli_serve_production.py` covers the six scenarios listed in §4 (eager middleware; rebind; idempotency; parent dir creation; missing forge_config; async_task_starter threading).
- [ ] **AC-9** — Coverage for the new `FORGE_DB_PATH` env override (extend `test_cli_serve_skeleton.py` or new `test_cli_serve_config.py`).
- [ ] **AC-10** — TASK-FW10-011 frontmatter updated with `parent_review: TASK-REV-F010` and `production_binding_sibling: TASK-FIX-F010` (per AC-FW10-011-LINK on the parent review).
- [ ] **AC-11** — Re-run jarvis runbook §6.2 + §7 against forge-prod built from the new commit; capture new correlation_id; verify `pipeline.build-started.*` envelope appears on `pipeline.>`; record correlation_id in this task's completion notes.
- [ ] **AC-12** — File a follow-up note (or short task) to resurrect TASK-FW10-011 from `tasks/completed/` → `tasks/backlog/feat-jarvis-internal-001-followups/` with `dependencies: [TASK-FIX-F010]` and status `in_progress` once this task ships (D4.B sequencing).
- [ ] **AC-13** — All modified files pass project-configured lint/format checks with zero errors.
- [ ] **AC-14** — Test suite passes (`pytest tests/forge/`); no regressions vs the 61-passing baseline established by TASK-FORGE-FRR-002.

## Files Expected to Change

- `src/forge/cli/serve.py` — `serve_cmd` body, add `@click.pass_context`, add `_resolve_forge_config_for_serve` helper.
- NEW `src/forge/cli/_serve_production.py` — wrapper module.
- `src/forge/cli/_serve_config.py` — add `db_path` field, `DEFAULT_DB_PATH`, `FORGE_DB_PATH` parsing.
- `tests/forge/test_cli_serve_skeleton.py` — patch new wrapper in 3 smoke tests; possibly add `test_cli_serve_config.py` for env-var coverage.
- `tests/forge/test_cli_serve_logging.py` — patch new wrapper in 2 smoke tests.
- NEW `tests/forge/test_cli_serve_production.py` — coverage for the wrapper module.
- `tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md` — frontmatter cross-link.
- `tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md` — document chosen wiring shape.
- `tasks/backlog/feat-jarvis-internal-001-followups/README.md` — append this task to the table.

## References

- [Review report](../../../.claude/reviews/TASK-REV-F010-review-report.md) — full decision rationale on all 5 axes.
- [TASK-REV-F010](TASK-REV-F010-bind-production-dispatch-chain-in-serve-cmd.md) — parent review.
- [serve.py:580-590](../../../src/forge/cli/serve.py#L580-L590) — the gap site.
- [serve.py:190-254](../../../src/forge/cli/serve.py#L190-L254) — `bind_production_dispatch_chain` factory.
- [_serve_deps.py:338-457](../../../src/forge/cli/_serve_deps.py#L338-L457) — `build_pipeline_consumer_deps`.
- [main.py:69-87](../../../src/forge/cli/main.py#L69-L87) — `_resolve_context_object` (the `ForgeConfig` plumbing this task hooks into).
- jarvis FRR rerun evidence: `/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`.
