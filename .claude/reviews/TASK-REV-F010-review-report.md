# Review Report: TASK-REV-F010

> **Title**: Decide how to bind `compose_dispatch_chain` to the production composer in `serve_cmd` (post-FEAT-DEA8 gap)
> **Mode**: decision · **Depth**: standard · **Date**: 2026-05-04
> **Correlation context**: jarvis FRR rerun `18036705-2bb7-4564-8363-315bf7716a48` (GB10, 2026-05-04)

---

## Executive Summary

The review confirms the wiring gap: [serve.py:580-590](../../src/forge/cli/serve.py#L580-L590) never calls `bind_production_dispatch_chain`, so `compose_dispatch_chain` stays bound to the logged DEBUG no-op `_default_compose_dispatch_chain` and every inbound envelope falls through to the receipt-only `_default_dispatch` stub at [_serve_daemon.py:166](../../src/forge/cli/_serve_daemon.py#L166). The fix is small but the surrounding wiring requires deliberate choices on five axes.

**Score: 72 / 100** — wiring is structurally sound (factories ✓, seams ✓, contracts ✓) but the production caller is missing and the unit-test surface couples to the no-op default. Three further findings (F1–F3) shape the recommendation.

### Recommended decisions

| Axis | Choice | One-line rationale |
|---|---|---|
| **D1 — Wiring location** | **B (thin ops wrapper module `forge.cli._serve_production`)** | Mirrors the FW10-001 seams-and-wiring separation; keeps `serve_cmd` minimal and tests have one clean monkeypatch surface. |
| **D2 — SQLite pool source** | **A* (extend `ServeConfig` with `db_path`, *reuse* `FORGE_DB_PATH`, default `~/.forge/forge.db`)** | The project already standardised on `FORGE_DB_PATH` (`forge queue`, `forge status`); inventing `FORGE_SQLITE_PATH` would fragment the operator surface. Default matches `forge queue`'s local-friendly `~/.forge/forge.db` per the ADR-ARCH-001 local-only ratification. |
| **D2-bonus — `ForgeConfig` source** | **Reach into Click `ctx.obj`; fall back to `./forge.yaml`** | `main()` already loads `forge.yaml` into `ctx.obj`; `serve_cmd` should pick it up via `@click.pass_context` rather than re-loading. |
| **D3 — AsyncSubAgentMiddleware construction** | **A (eager, in the wrapper)** | DeepAgents is a hard boot dependency (FW10-008); fail-fast at boot beats fail-late on first envelope (where `_default_dispatch` would have already acked). |
| **D4 — Order vs FW10-011** | **B (fix first, FW10-011 second as regression lock)** | The jarvis runbook rerun is already a deterministic red test; landing the fix unblocks Phase 7 close immediately. FW10-011 lands as the codified regression. |
| **D5 — Unit-test compatibility** | **A (testable helper via the same wrapper module from D1.B)** | The wrapper module *is* the testable seam — one-line `monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)` per affected test. Rejects D5.B's footgun env-flag and D5.C's drift. |

---

## Findings

### F1 — `_default_compose_dispatch_chain` is a no-op; nothing rebinds it on the production path

**Evidence**: [serve.py:167-187](../../src/forge/cli/serve.py#L167-L187) defines the default and module-level binding; [serve.py:580-590](../../src/forge/cli/serve.py#L580-L590) is the only caller that boots the daemon and never calls `bind_production_dispatch_chain`.

**Effect**: `_run_serve` calls `await compose_dispatch_chain(client)` at [serve.py:539](../../src/forge/cli/serve.py#L539) but the awaited closure logs a DEBUG line and returns. `_serve_daemon.dispatch_payload` stays the `_default_dispatch` stub (acks every message; emits the line observed in the runbook log).

**Severity**: HIGH — silent functional failure of the entire FEAT-DEA8 promise. Every `pipeline.build-queued.*` envelope is acked-and-discarded; no autobuild runs; no lifecycle envelopes are published.

### F2 — `ForgeConfig` is already plumbed into Click `ctx.obj` and ignored by `serve_cmd`

**Evidence**: [main.py:69-72](../../src/forge/cli/main.py#L69-L72) (`_resolve_context_object`) loads `./forge.yaml` (or `--config` path) into `ctx.obj`. `serve_cmd` at [serve.py:580](../../src/forge/cli/serve.py#L580) is not `@click.pass_context`-decorated.

**Effect**: The decision-points doc (D2.C) entertains "drag `ForgeConfig` reachability into the daemon entry-point" as a *con* — but the plumbing already exists. `serve_cmd` only needs to *read* it. This significantly simplifies D2.

**Severity**: MEDIUM — informs the recommendation, not a bug in itself.

### F3 — `FORGE_DB_PATH` is the project-wide SQLite-path env var; `FORGE_SQLITE_PATH` would fragment

**Evidence**:
- [status.py:97-100](../../src/forge/cli/status.py#L97-L100): `_FORGE_DB_PATH_ENV = "FORGE_DB_PATH"`, default `./.forge/forge.db`
- [queue.py:101-103, 228](../../src/forge/cli/queue.py#L101-L103): `DEFAULT_DB_PATH = Path("~/.forge/forge.db")`, env override `FORGE_DB_PATH`
- [runtime.py:62-91](../../src/forge/cli/runtime.py#L62-L91): `build_cli_runtime(db_path)` is the canonical entrypoint that chains `connect_writer(db_path)` → `SqliteLifecyclePersistence(connection=connection, db_path=db_path)`

**Effect**: D2.A as written ("introduce `FORGE_SQLITE_PATH`") would create a parallel naming convention. The recommendation reuses `FORGE_DB_PATH` and defaults to `~/.forge/forge.db` (matching `queue`'s op-friendly default).

**Severity**: MEDIUM — invisible if missed at design time, painful to retire later.

### F4 — Smoke tests rely on `_default_compose_dispatch_chain`'s no-op behaviour

**Evidence**: `tests/forge/test_cli_serve_skeleton.py::TestServeCmdSmoke` ([line 218](../../tests/forge/test_cli_serve_skeleton.py#L218)) only stubs `nats_connect`, `run_daemon`, `run_healthz_server`. Nothing stubs `compose_dispatch_chain` because the default is a free no-op. `test_cli_serve_logging.py` has the same pattern.

**Effect**: Any direct call to `bind_production_dispatch_chain(...)` inside `serve_cmd` would crash the smoke tests because no `forge_config` / `sqlite_pool` is reachable in those harnesses. The wrapper module from D1.B + D5.A keeps the test patch surface to one line.

**Severity**: MEDIUM — actionable mitigation drives D5.A.

### F5 — TASK-FW10-011 is `design_approved` but archived in `tasks/completed/`

**Evidence**: [TASK-FW10-011 frontmatter](../../tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md): `status: design_approved`, located under `tasks/completed/` after the FEAT-DEA8 finalize chore.

**Effect**: D4 cannot just "bump it from `design_approved` → `in_progress`" — it lives in `completed/`. Choosing D4.A (test first) requires resurrecting it to `tasks/backlog/feat-jarvis-internal-001-followups/`. Choosing D4.B (fix first) leaves it in `completed/` until the fix lands, then resurrects it as the regression lock.

**Severity**: LOW — sequencing detail, but worth calling out so the implementation task isn't surprised.

---

## Decision Detail

### AC-D1 — Wiring location: **D1.B (thin ops wrapper module)**

**Choice**: New module `src/forge/cli/_serve_production.py` exposing:

```python
def bind_production_serve(config: ServeConfig, forge_config: ForgeConfig) -> None:
    """Compose prod deps + rebind compose_dispatch_chain. Idempotent.

    1. Open writer connection: connect_writer(config.db_path).
    2. Construct SqliteLifecyclePersistence(connection=cx, db_path=config.db_path).
    3. Construct AsyncSubAgentMiddleware via _build_async_subagent_middleware().
    4. Derive AsyncTaskStarter from middleware.tools (per FW10-008 contract).
    5. Rebind serve.compose_dispatch_chain = bind_production_dispatch_chain(
           forge_config=forge_config,
           sqlite_pool=sqlite_pool,
           async_task_starter=async_task_starter,
       )
    """
```

**`serve_cmd` becomes**:

```python
@click.command(name="serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Run the long-lived forge daemon (JetStream consumer + healthz)."""
    config = ServeConfig.from_env()
    _configure_logging(config.log_level)
    forge_config = _resolve_forge_config_for_serve(ctx)  # ctx.obj or load forge.yaml
    bind_production_serve(config, forge_config)
    state = SubscriptionState()
    asyncio.run(_run_serve(config, state))
```

**Rationale**:
- **Pro vs D1.A (inline)**: Keeps Click decoration thin; deps construction lives next to its tests.
- **Pro vs D1.C (inside `_run_serve`)**: Preserves the FW10-001 design intent — `_run_serve` owns *boot order*, the wrapper owns *production wiring*. Conflating them was explicitly rejected by FW10-001.
- **Mirrors existing seams**: `recovery_reconcile_on_boot` and `consumer_reconcile_on_boot` are already module-level rebindable; `compose_dispatch_chain` is the third such seam. A wrapper that owns the rebinding completes the symmetry.
- **Test impact**: One monkeypatch surface (`monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)`) for all smoke tests.

### AC-D2 — SQLite pool source: **D2.A* (modified — extend `ServeConfig`, reuse `FORGE_DB_PATH`)**

**Choice**: Add to `ServeConfig`:

```python
DEFAULT_DB_PATH: Path = Path("~/.forge/forge.db").expanduser()

class ServeConfig(BaseModel):
    ...
    db_path: Path = Field(default_factory=lambda: DEFAULT_DB_PATH)

    @classmethod
    def from_env(cls, environ=None) -> "ServeConfig":
        env = environ if environ is not None else os.environ
        kwargs = {}
        ...existing FORGE_NATS_URL / HEALTHZ_PORT / etc...
        if "FORGE_DB_PATH" in env:
            kwargs["db_path"] = Path(env["FORGE_DB_PATH"]).expanduser()
        return cls(**kwargs)
```

**Rationale**:
- **Reuse, don't fragment**: `FORGE_DB_PATH` is already the convention (`queue`, `status`). Operators have one knob, not two.
- **Local-friendly default**: `~/.forge/forge.db` matches `forge queue`'s default and aligns with the local-only ethos called out in the task's "Notes for the reviewer" (ADR-ARCH-001 reinforcement).
- **No Dockerfile change**: `~/.forge/` is created by `mkdir -p $HOME/.forge` at runtime if missing — no `chown forge:forge /var/forge` step. Production deployments that *want* `/var/forge/lifecycle.sqlite` set `FORGE_DB_PATH=/var/forge/lifecycle.sqlite` explicitly.
- **Rejects D2.B**: Hard-coded path breaks PaaS deployments that mount writable storage at non-`/var` paths.
- **Rejects D2.C**: Adding `db_path` to `ForgeConfig.queue` is appealing but cross-cuts unrelated subsystems. Keep `db_path` on `ServeConfig` and reach for `ForgeConfig` via `ctx.obj` for the *other* fields (`pipeline.approved_originators`, `permissions.filesystem.allowlist`).

**For `ForgeConfig`** (separate from sqlite_pool, see F2):

```python
def _resolve_forge_config_for_serve(ctx: click.Context) -> ForgeConfig:
    """Pick ForgeConfig from ctx.obj; fall back to ./forge.yaml; raise if absent."""
    if isinstance(ctx.obj, ForgeConfig):
        return ctx.obj
    if Path("forge.yaml").exists():
        return load_config(Path("forge.yaml"))
    raise click.UsageError(
        "forge serve requires a forge.yaml — pass --config <path> or run "
        "from a directory containing ./forge.yaml."
    )
```

This puts `serve_cmd`'s ForgeConfig requirement on equal footing with `forge queue`'s (which calls `_require_forge_config(ctx.obj)` already).

### AC-D3 — AsyncSubAgentMiddleware construction: **D3.A (eager)**

**Choice**: Construct the middleware once inside `bind_production_serve` (the D1.B wrapper). Pass its `start_async_task` tool surface as the `async_task_starter` to `bind_production_dispatch_chain`.

**Rationale**:
- **Fail-fast on missing deps**: DeepAgents is a hard requirement at boot per TASK-FW10-008. If it's missing, an `ImportError` at boot is far better than the same error inside the closure on the *first inbound envelope* — by which point the receipt-only stub has already acked the message and the replay window has closed.
- **Closure-bloat argument moot**: With D1.B, the closure is in the wrapper module, not in `serve_cmd`. The "bloat" cost is already paid in the right place.
- **One fewer reason for a test to surprise an operator**: lazy construction means a test that monkeypatches *part* of the import graph can leave a half-constructed middleware visible at runtime.

### AC-D4 — Order vs FW10-011: **D4.B (fix first, FW10-011 as regression lock)**

**Choice**: Land **TASK-FIX-F010** (the implementation task this review produces) first. Then resurrect TASK-FW10-011 from `tasks/completed/` to `tasks/backlog/feat-jarvis-internal-001-followups/`, mark `dependencies: [TASK-FIX-F010]`, and ship it as the codified regression lock.

**Rationale**:
- **The runbook rerun is already a deterministic red test**: jarvis runbook §6.2 + §7 with correlation `18036705-2bb7-4564-8363-315bf7716a48` reproduces the gap reliably. Strict TDD (D4.A) would have us re-prove what's already proven before shipping the fix.
- **Phase 7 close criterion is structurally unsatisfiable until the fix lands**: FW10-011 first means jarvis Phase 7 stays red for the full integration-test build window. D4.B unblocks operator-visible value immediately.
- **Bundle (D4.C) is too big a blast radius**: FW10-011 needs an embedded NATS fixture, a scripted state-transition mock, and an eight-subject subscriber. The wiring fix is small and isolated. Decoupling the PRs lets each be reviewed on its own merits.
- **Sequencing details for the implementation task**:
  1. Create `TASK-FIX-F010` (this review's `AC-IMPL-TASK`).
  2. Land `TASK-FIX-F010`.
  3. Re-run jarvis runbook §6.2 + §7; capture new correlation_id; flip RESULTS row 7.x ❌ → ✅ on a third re-run (`AC-RUNBOOK-CLOSE`).
  4. Move `tasks/completed/TASK-FW10-011-...md` → `tasks/backlog/feat-jarvis-internal-001-followups/` with `dependencies: [TASK-FIX-F010]`.
  5. Land FW10-011 as the regression lock.

### AC-D5 — Unit-test compatibility: **D5.A (testable helper via the D1.B wrapper)**

**Choice**: The wrapper module from D1.B *is* the testable seam. Affected smoke tests add one line:

```python
# In test_cli_serve_skeleton.py::TestServeCmdSmoke and ::TestRunServeBootOrder
monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)
```

**Rationale**:
- **Rejects D5.B (env-flag escape hatch)**: `FORGE_SERVE_SKIP_DISPATCH_BINDING=1` is exactly the kind of operational footgun the task author flagged in the cons column. An operator who copy-pastes a test env-block into prod silently disables the production wiring — and `_default_dispatch` keeps acking, so nothing alarms.
- **Rejects D5.C (every test grows a fixture line)**: With ~3-4 smoke tests today and an unknown number of future ones, the drift cost compounds. Centralised monkeypatch via D1.B is the same diff per test but only one *concept* to remember.
- **D5.A's "more refactor than the headline gap requires" objection dissolves under D1.B**: the wrapper module is the refactor; once it exists, the testable helper *is* the wrapper. No further work.

**Affected tests**:

| File | Tests | Patch needed |
|---|---|---|
| `tests/forge/test_cli_serve_skeleton.py` | `TestServeCmdSmoke::test_serve_cmd_exits_zero_with_stub_coroutines` (line 218), `::test_serve_cmd_uses_asyncio_gather` (line 246), `TestRunServeBootOrder::*` (line 282+) | `monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)` |
| `tests/forge/test_cli_serve_logging.py` | `test_info_record_is_emitted_after_serve_cmd_initialises` (line 58), `test_serve_cmd_calls_configure_logging_with_env_level` (line 239) | Same patch |
| `tests/forge/test_serve_healthz.py` | (line 284 invokes config construction only — no `serve_cmd` invocation; no patch needed) | None |
| `tests/forge/test_cli_serve_daemon.py` | (read-only of daemon module) | None |

---

## AC-FILE-IMPACT — Files the implementation task will touch (ranked by risk)

| Rank | Path | Lines / scope | Risk | Mitigation |
|---|---|---|---|---|
| 1 | [src/forge/cli/serve.py](../../src/forge/cli/serve.py) | `serve_cmd` body 580-590; add `@click.pass_context`; +`_resolve_forge_config_for_serve` helper | **HIGH** — every smoke test invokes this | Behind D1.B wrapper; tests one-line patch |
| 2 | NEW `src/forge/cli/_serve_production.py` | New module — `bind_production_serve(config, forge_config)` | **MEDIUM** — new code path | Dedicated test module (rank 7) |
| 3 | [src/forge/cli/_serve_config.py](../../src/forge/cli/_serve_config.py) | Add `db_path` field + `FORGE_DB_PATH` env override + `DEFAULT_DB_PATH` constant | **LOW** — additive; existing tests passthrough kwargs | Add one targeted test for the new env override |
| 4 | [tests/forge/test_cli_serve_skeleton.py](../../tests/forge/test_cli_serve_skeleton.py) | Patch `bind_production_serve` no-op in `TestServeCmdSmoke` (3 tests) and `TestRunServeBootOrder` (3+ tests) | **LOW** | Pattern is identical to existing `nats_connect` patch |
| 5 | [tests/forge/test_cli_serve_logging.py](../../tests/forge/test_cli_serve_logging.py) | Same patch in 2 tests that invoke `serve_cmd` | **LOW** | — |
| 6 | NEW `tests/forge/test_cli_serve_production.py` | Coverage for the new wrapper module: middleware constructed eagerly; `compose_dispatch_chain` rebound after call; idempotency on second call; raises if `forge_config` is None | **LOW** | New tests only |
| 7 | NEW `tasks/backlog/feat-jarvis-internal-001-followups/TASK-FIX-F010-bind-production-dispatch-chain.md` | Implementation task (`task_type: fix`) with the chosen options codified into ACs | **LOW** | — |
| 8 | [tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md](../../tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md) | Document the chosen wiring shape (wrapper module + `ctx.obj` ForgeConfig + `FORGE_DB_PATH` reuse) | **LOW** | — |
| 9 | [tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md](../../tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md) | Add frontmatter cross-link `parent_review: TASK-REV-F010` and `production_binding_sibling: TASK-FIX-F010`; physically move file to `tasks/backlog/feat-jarvis-internal-001-followups/` after TASK-FIX-F010 lands (per D4.B sequencing) | **LOW** | Move is a `git mv` |
| 10 | NOT NEEDED — `Dockerfile` | `~/.forge/` is per-user; no `mkdir -p /var/forge && chown` required (D2 default) | — | — |

---

## AC-IMPL-TASK — Skeleton for `TASK-FIX-F010`

```yaml
---
id: TASK-FIX-F010
title: "Bind compose_dispatch_chain to the production composer in serve_cmd via _serve_production wrapper"
status: backlog
priority: high
task_type: fix
parent_review: TASK-REV-F010
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-007
  - TASK-FW10-008
  - TASK-FW10-011  # follow-up regression lock (D4.B)
  - TASK-REV-F010  # this review
complexity: 4
estimated_minutes: 90
tags:
  - forge-serve
  - orchestrator-wiring
  - production-binding
  - feat-forge-010-followup
  - feat-dea8-followup
report_path: .claude/reviews/TASK-REV-F010-review-report.md
---

# Why
Close the FEAT-DEA8 production-wiring gap discovered during the jarvis FRR rerun on 2026-05-04. See review report.

# Acceptance Criteria
- [ ] AC-1: NEW `src/forge/cli/_serve_production.py` exposes `bind_production_serve(config: ServeConfig, forge_config: ForgeConfig) -> None`.
- [ ] AC-2: `bind_production_serve` constructs the SQLite writer connection from `config.db_path`, builds `SqliteLifecyclePersistence`, eagerly constructs `AsyncSubAgentMiddleware`, and rebinds `forge.cli.serve.compose_dispatch_chain` via `bind_production_dispatch_chain(forge_config=..., sqlite_pool=..., async_task_starter=...)`.
- [ ] AC-3: `bind_production_serve` is idempotent (second call replaces the binding cleanly without leaking the previous SQLite connection).
- [ ] AC-4: `ServeConfig` extended with `db_path: Path` field; `FORGE_DB_PATH` env override honoured; default `~/.forge/forge.db`.
- [ ] AC-5: `serve_cmd` decorated with `@click.pass_context`; reads `ForgeConfig` from `ctx.obj` or falls back to `./forge.yaml` via `load_config`; raises `click.UsageError` if neither is available.
- [ ] AC-6: `serve_cmd` calls `bind_production_serve(config, forge_config)` between `_configure_logging` and `asyncio.run(_run_serve(...))`.
- [ ] AC-7: All existing `tests/forge/test_cli_serve_skeleton.py` and `test_cli_serve_logging.py` smoke tests pass with one-line `monkeypatch.setattr(serve_module, "bind_production_serve", lambda *a, **kw: None)` adjustment.
- [ ] AC-8: NEW `tests/forge/test_cli_serve_production.py` covers: (a) middleware constructed eagerly; (b) `compose_dispatch_chain` rebound after call; (c) idempotency on second call; (d) `ValueError` when `forge_config` is None.
- [ ] AC-9: Re-run jarvis runbook §6.2 + §7 against forge-prod built from the new commit; capture new correlation_id; verify `pipeline.build-started.*` envelope appears on `pipeline.>`; record correlation_id in completion notes.
- [ ] AC-10: `tasks/completed/TASK-FW10-011-...md` frontmatter updated with `parent_review: TASK-REV-F010` and `production_binding_sibling: TASK-FIX-F010` (per AC-FW10-011-LINK).
- [ ] AC-11: After this task lands, file follow-up to resurrect TASK-FW10-011 from `tasks/completed/` → `tasks/backlog/feat-jarvis-internal-001-followups/` with `dependencies: [TASK-FIX-F010]`.

# References
- [Review report](../../.claude/reviews/TASK-REV-F010-review-report.md)
- [serve.py:580-590](../../src/forge/cli/serve.py#L580-L590) — the gap site
- [serve.py:190-254](../../src/forge/cli/serve.py#L190-L254) — `bind_production_dispatch_chain` factory
- [_serve_deps.py:338-457](../../src/forge/cli/_serve_deps.py#L338-L457) — `build_pipeline_consumer_deps`
```

---

## AC-FW10-011-LINK — Cross-reference plan

- **This review** names FW10-011 as the integration-test sibling (above, in F5 and D4 rationale).
- **TASK-FW10-011** receives a frontmatter update (per AC-10 of the implementation task above):

```yaml
parent_review: TASK-REV-F010                      # this review
production_binding_sibling: TASK-FIX-F010          # the implementation task this review files
```

After TASK-FIX-F010 lands, FW10-011's status moves `design_approved` → `in_progress` and the file is moved from `tasks/completed/` to `tasks/backlog/feat-jarvis-internal-001-followups/` (per D4.B sequencing).

---

## AC-RUNBOOK-CLOSE — Validation plan

After TASK-FIX-F010 lands:

1. Rebuild forge image from the `main` commit containing the fix.
2. Re-run jarvis runbook §6.2 (`Queue FEAT-43DE for build...`) against canonical NATS.
3. Verify on `nats sub "pipeline.>" --raw`:
   - One inbound `pipeline.build-queued.FEAT-43DE` (as before)
   - At least one `pipeline.build-started.FEAT-43DE` (NEW — proves rebind ran)
   - At least one `pipeline.stage-complete.FEAT-43DE` envelope (proves dispatch succeeded)
4. `docker logs forge-prod` shows the line: `forge-serve: dispatch chain composed; _serve_daemon.dispatch_payload rebound to handle_message dispatcher (receipt-only stub no longer reachable)` — emitted by `bind_production_dispatch_chain` at [serve.py:248-252](../../src/forge/cli/serve.py#L248-L252).
5. Capture the new correlation_id; record in `TASK-FIX-F010` completion notes.
6. Flip jarvis runbook RESULTS row 7.x ❌ → ✅ on this third re-run.

---

## Open Questions (none blocking)

1. Should `bind_production_serve` close the SQLite writer connection on daemon exit? Today `_close_client_quietly` only handles the NATS client. Recommendation: yes — add a `finally` close in `_run_serve` or have the wrapper return a context manager. Defer to TASK-FIX-F010 implementation; not a decision-point for this review.
2. Should the `db_path` parent directory be auto-created? Recommendation: yes, in `bind_production_serve`, with `db_path.parent.mkdir(parents=True, exist_ok=True)` — matches `forge queue`'s implicit assumption.

These are implementation details for TASK-FIX-F010, not decision points blocking this review.

---

## Appendix — Verification of code state

- `serve_cmd` source confirmed: [serve.py:580-590](../../src/forge/cli/serve.py#L580-L590) — does not call `bind_production_dispatch_chain`. ✓
- `compose_dispatch_chain` default confirmed: [serve.py:167-187](../../src/forge/cli/serve.py#L167-L187) — logged DEBUG no-op. ✓
- `bind_production_dispatch_chain` factory confirmed: [serve.py:190-254](../../src/forge/cli/serve.py#L190-L254). ✓
- `ServeConfig` schema confirmed: [_serve_config.py:58-110](../../src/forge/cli/_serve_config.py#L58-L110) — no `db_path` field today. ✓
- `FORGE_DB_PATH` convention confirmed: [status.py:97](../../src/forge/cli/status.py#L97), [queue.py:228](../../src/forge/cli/queue.py#L228). ✓
- `ForgeConfig` plumbed into Click `ctx.obj` confirmed: [main.py:69-87](../../src/forge/cli/main.py#L69-L87). ✓
- TASK-FW10-011 status confirmed: `design_approved`, located in `tasks/completed/`. ✓
