---
id: TASK-REV-F010
title: "Decide how to bind `compose_dispatch_chain` to the production composer in `serve_cmd` (post-FEAT-DEA8 gap)"
status: review_complete
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T00:00:00Z
priority: high
task_type: review
review_mode: decision
review_depth: standard
review_results:
  mode: decision
  depth: standard
  score: 72
  findings_count: 5
  recommendations_count: 5
  decision: refactor
  report_path: .claude/reviews/TASK-REV-F010-review-report.md
  decisions:
    D1_wiring_location: "B - thin ops wrapper module (forge.cli._serve_production)"
    D2_sqlite_pool_source: "A* - extend ServeConfig with db_path, reuse FORGE_DB_PATH, default ~/.forge/forge.db"
    D2_bonus_forge_config: "Read from Click ctx.obj; fall back to ./forge.yaml"
    D3_async_subagent_middleware: "A - eager construction in wrapper"
    D4_order_vs_fw10_011: "B - fix first (TASK-FIX-F010), FW10-011 second as regression lock"
    D5_unit_test_compatibility: "A - testable helper via the D1.B wrapper module"
  completed_at: 2026-05-04T00:00:00Z
tags:
  - review
  - forge-serve
  - orchestrator-wiring
  - feat-forge-010-followup
  - feat-dea8-followup
  - first-real-run-followup
  - production-binding
  - dispatch-chain
complexity: 5
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-007  # composed the dispatcher closure but didn't bind the seam in serve_cmd
  - TASK-FW10-008  # wired AsyncSubAgentMiddleware that the rebind needs to invoke
  - TASK-FW10-011  # capstone integration test (status: design_approved) — would have caught this
  - TASK-FORGE-FRR-002  # logging.basicConfig fix that made this gap diagnosable
correlation_id: 18036705-2bb7-4564-8363-315bf7716a48
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Rerun of jarvis FEAT-JARVIS-INTERNAL-001 first-real-run runbook after all four jarvis-side FRR follow-ups (TASK-FRR-001..004) and FEAT-FORGE-010 (FEAT-DEA8) merged"
context_files:
  # jarvis-side rerun evidence
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run.md
  - ../../../../../jarvis/docs/runbooks/RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md
  # forge-side source-of-truth
  - src/forge/cli/serve.py
  - src/forge/cli/_serve_daemon.py
  - src/forge/cli/_serve_dispatcher.py
  - src/forge/cli/_serve_deps.py
  - tasks/completed/TASK-FW10-007-compose-pipeline-consumer-deps.md
  - tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md
  - tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md
  - tasks/backlog/forge-serve-orchestrator-wiring/README.md
  - .guardkit/archive/FEAT-DEA8/   # if archived; otherwise .guardkit/features/FEAT-DEA8.yaml
external_evidence:
  inbound_envelope_captured_on_wire: true
  outbound_envelopes_observed: 0
  forge_log_line: "forge-serve: received build-queued envelope feature_id=FEAT-43DE correlation_id=18036705-2bb7-4564-8363-315bf7716a48"
  consumer_state: "delivered=2, pending=0, redelivered=0 (forge consumed + acked via receipt-only stub)"
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Decide how to bind `compose_dispatch_chain` to the production composer in `serve_cmd` (post-FEAT-DEA8 gap)

## TL;DR for the next person picking this up

FEAT-FORGE-010 (FEAT-DEA8 — *"Wire the production pipeline orchestrator into forge serve"*) merged on 2026-05-02. It ships **all the parts** of the production dispatch chain — `make_handle_message_dispatcher`, `bind_production_dispatch_chain`, `build_pipeline_consumer_deps`, the `_compose` closure that rebinds `_serve_daemon.dispatch_payload`. **It does not actually invoke any of them at boot.** `serve_cmd` calls `_run_serve(config, state)`, `_run_serve` calls `await compose_dispatch_chain(client)`, but `compose_dispatch_chain` is still the module-level default `_default_compose_dispatch_chain` — a logged DEBUG no-op. The daemon falls through to the receipt-only `_default_dispatch` stub at `_serve_daemon.py:166`. Every inbound `pipeline.build-queued.*` envelope is logged + acked. **No autobuild runs. No lifecycle envelopes are published.** The Phase 7 close criterion of the jarvis first-real-run runbook is structurally unsatisfiable until this is fixed.

This is a **review task** because the fix is non-obvious: the rebind needs `forge_config`, `sqlite_pool`, and `async_task_starter` constructed from `ServeConfig.from_env()`, and the right place / shape for that wiring is a design call. See "Decision points" below.

---

## Why a review task and not a fix task

The mechanical fix is one assignment — but the surrounding wiring requires choices:

1. **Where does the SQLite pool come from?** `bind_production_dispatch_chain` requires a `sqlite_pool` (`SqliteLifecyclePersistence`). `ServeConfig` has no DB-path field today; `_serve_deps.build_pipeline_consumer_deps` expects one already constructed. Options: (a) extend `ServeConfig` with a `FORGE_SQLITE_PATH` env var with a sane default; (b) pull from a separate ops-only config file; (c) construct it inside `_run_serve` from a hard-coded `/var/forge/lifecycle.sqlite`.
2. **Where does `forge_config` come from?** Today `ServeConfig` covers daemon-only knobs (NATS URL, healthz port, log level, durable name). The full `ForgeConfig` (allowlist / approved_originators / etc.) is owned by another path. Either re-derive from env on `serve_cmd` entry or thread it through.
3. **Where is `_build_async_subagent_middleware()` called?** It exists at `serve.py:262` (TASK-FW10-008). Should it be invoked once at boot inside `serve_cmd` (cleanest), or lazily inside the `_compose` closure (matches the existing pattern but bloats the closure)?
4. **Should `serve_cmd` rebind `compose_dispatch_chain` directly, or should this go in a thin ops wrapper?** Direct rebind in `serve_cmd` is the simplest; an ops wrapper module (e.g. `forge.cli._serve_production`) keeps `serve_cmd` minimal and matches the FW10-001 documented pattern of seams-and-wiring separation.
5. **Order of operations vs FW10-011.** TASK-FW10-011 is `design_approved` (not implemented) and is the integration test that mocks `AutobuildDispatcher.dispatch(...)` at the boundary and asserts the wired-in-production stack publishes the full lifecycle envelope sequence. **The fix and the test both want to land** — should the test come first (red → green → fix) or after (TDD-after) given that this gap is already reproduced by the jarvis runbook rerun?
6. **Backwards compat / unit-test impact.** Several existing unit tests (`tests/forge/test_cli_serve_skeleton.py`, `test_cli_serve_daemon.py`, `test_serve_healthz.py` — 61 passing per TASK-FORGE-FRR-002 regression check) depend on the current `serve_cmd` behaviour. The rebind should be implemented in a way that doesn't break them — that may require either splitting `serve_cmd` body into testable helpers or adding an env-flag escape hatch (e.g. `FORGE_SERVE_SKIP_DISPATCH_BINDING=1`) for unit tests that don't want the full deps graph.

---

## Description

### Symptom (verbatim from rerun)

Setup:
- jarvis `main` includes all four FRR follow-ups (TASK-FRR-001..004) and the runbook Phase 7 rewrite for FEAT-FORGE-010 (`bb6056c`).
- forge `main` includes FEAT-DEA8 merge (`9a93808 Merge FEAT-DEA8: wire pipeline orchestrator into forge serve`, 2026-05-02) and the FEAT-DEA8 finalize chore (`9ef9138`).
- forge image rebuilt fresh on 2026-05-04 from forge `main` HEAD `de23557` (430 MB; tags `forge:latest` + `forge:production-validation`).
- NATS canonical (7 streams + 4 KV) green; `forge serve` daemon up healthy on `:8088`; `forge-serve` durable consumer attached on PIPELINE.

Drive `jarvis chat` with the runbook §6.2 prompt:

```text
> Queue FEAT-43DE for build. The feature YAML is at .guardkit/archive/FEAT-43DE/feature_state.yaml on the main branch of guardkit/jarvis.
```

The supervisor returns:

```text
FEAT-43DE is queued for build. Correlation ID: `18036705-2bb7-4564-8363-315bf7716a48`.
Forge will pick it up from the JetStream topic `pipeline.build-queued.FEAT-43DE` —
I'll notify you via events as it progresses.
```

`nats sub "pipeline.>" --raw` captures one inbound envelope (verbatim):

```json
{"message_id":"6aef137e-b408-42b8-8496-dcec1ea2619d","timestamp":"2026-05-04T06:14:52.389455Z","version":"1.0","source_id":"jarvis","event_type":"build_queued","project":null,"correlation_id":"18036705-2bb7-4564-8363-315bf7716a48","payload":{"feature_id":"FEAT-43DE","repo":"guardkit/jarvis","branch":"main","feature_yaml_path":".guardkit/archive/FEAT-43DE/feature_state.yaml","max_turns":5,"sdk_timeout_seconds":1800,"wave_gating":false,"config_overrides":null,"triggered_by":"jarvis","originating_adapter":"terminal","originating_user":null,"correlation_id":"18036705-2bb7-4564-8363-315bf7716a48","parent_request_id":null,"retry_count":0,"requested_at":"2026-05-04T06:14:52.389391Z","queued_at":"2026-05-04T06:14:52.389405Z","task_id":null,"mode":"mode-a"}}
```

`docker logs forge-prod` (TASK-FORGE-FRR-002 made this visible — first time we've seen daemon logs):

```
2026-05-04T06:12:20 [INFO] forge.cli._serve_healthz: healthz server listening on 0.0.0.0:8088 (durable=forge-serve)
2026-05-04T06:12:40 [INFO] aiohttp.access: 127.0.0.1 [04/May/2026:06:12:40 +0000] "GET /healthz HTTP/1.1" 200 180 "-" "curl/8.5.0"
2026-05-04T06:14:52 [INFO] forge.cli._serve_daemon: forge-serve: received build-queued envelope feature_id=FEAT-43DE correlation_id=18036705-2bb7-4564-8363-315bf7716a48
```

That last log line is the `_default_dispatch` stub at [`_serve_daemon.py:209-214`](src/forge/cli/_serve_daemon.py). **Critically absent**: the line `forge-serve: dispatch chain composed; _serve_daemon.dispatch_payload rebound to handle_message dispatcher (receipt-only stub no longer reachable)` that `bind_production_dispatch_chain` would emit at [`serve.py:248-252`](src/forge/cli/serve.py).

`nats consumer info PIPELINE forge-serve -j`:

```json
{"delivered": 2, "pending": 0, "redelivered": 0, "last_delivered_msg_ts": "2026-05-04T06:14:52.39243119Z"}
```

So forge dequeued + acked (the receipt-only stub `awaits msg.ack()` at line 215). **Zero outbound envelopes** on `pipeline.>` — no `pipeline.build-started.*`, no `pipeline.stage-complete.*`, no `pipeline.build-complete.*`. The chat REPL's second turn drained nothing — the supervisor's honest answer was *"Progress events (like `pipeline.*`) should arrive via notifications as Forge processes it, but I don't have a way to actively poll the build pipeline's current state right now."*

### Root cause (where the gap is)

[`forge/src/forge/cli/serve.py:580-590`](src/forge/cli/serve.py):

```python
@click.command(name="serve")
def serve_cmd() -> None:
    """Run the long-lived forge daemon (JetStream consumer + healthz)."""
    config = ServeConfig.from_env()
    # Attach the stderr handler BEFORE _run_serve schedules the daemon
    # / healthz coroutines, so their first ``logger.info`` lines reach
    # ``docker logs`` and ``journalctl`` instead of the silent root
    # logger. TASK-FORGE-FRR-002.
    _configure_logging(config.log_level)
    state = SubscriptionState()
    asyncio.run(_run_serve(config, state))
```

`_run_serve` at [`serve.py:490-540+`](src/forge/cli/serve.py) calls `await compose_dispatch_chain(client)` at line 539 (after both reconcile-on-boot routines, before `state.set_chain_ready(True)` and the daemon's first fetch). But `compose_dispatch_chain` is bound at module level to `_default_compose_dispatch_chain` ([`serve.py:167-187`](src/forge/cli/serve.py)) — a logged DEBUG no-op. **`serve_cmd` does not rebind it.**

The factory that *would* produce the production composer exists — `bind_production_dispatch_chain` at [`serve.py:190-254`](src/forge/cli/serve.py) — and its docstring even names this gap explicitly:

> *"Production wiring (``serve_cmd`` and ops scripts) rebinds this seam to a real composer that builds the `PipelineConsumerDeps` and rebinds `_serve_daemon.dispatch_payload`. Until that wiring runs the daemon falls back to the receipt-only `_default_dispatch` stub inside `_serve_daemon` — that stub still acks every message, so a misconfigured deployment can never wedge the JetStream queue even when the chain composer is missing."*

— but `serve_cmd` doesn't actually do the rebind. Hence: factory ✓, seam ✓, contract ✓, but no caller ⨯.

### Why the existing FW10 test surface didn't catch it

- FW10 unit tests cover `bind_production_dispatch_chain` in isolation (it rebinds correctly when invoked).
- FW10 unit tests cover `_run_serve` against the seam (it calls `compose_dispatch_chain(client)` at the right point in the boot order).
- **No test invokes `serve_cmd` and asserts that `compose_dispatch_chain` has been rebound by the time `_run_serve` runs.** That's the integration-vs-unit gap.

TASK-FW10-011 (status `design_approved`, not implemented) is exactly that test:

> *"Spins up `forge serve` against an embedded NATS server (or a `docker-compose` fixture if embedded NATS isn't viable) and a temporary SQLite database. Mocks `AutobuildDispatcher.dispatch(...)` at the boundary so the autobuild "runs" by emitting a scripted sequence of `_update_state` transitions through the real `PipelineLifecycleEmitter` — no real worktree, no real DeepAgents subagent invocation. Publishes one `pipeline.build-queued.FEAT-XXX` envelope with a known `correlation_id`."*

The FW10-011 design even calls itself the *"capstone test for FEAT-FORGE-010 [...] proves the production composition sends every envelope it should send."* So FW10-011 is both a sibling and a sequel of this fix — see "Decision points" below for the order-of-operations call.

---

## Decision points (the review's job)

This task is `task_type: review`, `review_mode: decision`. The review's outputs should be a chosen path on each of the following:

### D1. Wiring location

| Option | Pros | Cons |
|---|---|---|
| **A — Inline in `serve_cmd`**: rebind in the click command body | Simplest; matches the existing `_configure_logging` precedent that lives inline | Bloats `serve_cmd`; couples click decoration to ops-deps construction |
| **B — Thin ops wrapper module** (`forge.cli._serve_production` or similar): construct deps + rebind there, leaving `serve_cmd` as a 2-line entry | Cleanest separation of concerns; mirrors FW10-001's seams-and-wiring decoupling pattern | One more file; one more import edge for tests to navigate |
| **C — Inside `_run_serve` itself**: have `_run_serve` construct deps and rebind via `bind_production_dispatch_chain(...)` itself | Keeps `serve_cmd` 1-liner | Conflates the "boot order" abstraction with "production wiring" — breaks the FW10-001 design intent |

### D2. SQLite pool source

| Option | Pros | Cons |
|---|---|---|
| **A — Extend `ServeConfig`** with `FORGE_SQLITE_PATH` env var, default `/var/forge/lifecycle.sqlite` | Operator-visible knob; matches existing `FORGE_*_*` env-var convention | Schema change to `ServeConfig`; needs default-handling for tests |
| **B — Hard-coded `/var/forge/lifecycle.sqlite`** | Zero schema change | Operators can't override; PaaS deployments with non-`/var` writable paths break |
| **C — Re-derive `ForgeConfig` from env at boot** and let it own the SQLite path | Aligns with the broader ForgeConfig owning all deployment knobs | Drags `ForgeConfig` reachability all the way into the daemon entry-point |

### D3. AsyncSubAgentMiddleware construction

| Option | Pros | Cons |
|---|---|---|
| **A — Eager**: invoke `_build_async_subagent_middleware()` once in `serve_cmd` (or the wrapper) | Fail-fast if DeepAgents/middleware deps are missing; one-time cost | Requires DeepAgents installable at daemon boot (already a hard requirement per FW10-008) |
| **B — Lazy**: call from inside the `_compose` closure on first `compose_dispatch_chain(client)` | Matches the existing closure-based composition pattern | Late failure if middleware deps are misconfigured; harder to grep |

### D4. Order vs FW10-011

| Option | Pros | Cons |
|---|---|---|
| **A — Land FW10-011 first (red); fix this task second (green)** | Strict TDD; the test asserts the gap; the fix proves the test | FW10-011 is `design_approved` — it would have to move to `in_progress` first; no other FW10 child blocks on it |
| **B — Land this fix first; FW10-011 second (TDD-after / regression lock)** | Unblocks the jarvis Phase 7 close immediately; jarvis runbook rerun is the ad-hoc test until FW10-011 lands | TDD purists' eyebrow; integration-test contract not codified before the production wiring | 
| **C — Bundle them: ship both in one PR** | Atomic; the test exists alongside the binding it asserts | Bigger blast radius; harder to revert one without the other |

### D5. Unit-test compatibility

| Option | Pros | Cons |
|---|---|---|
| **A — Refactor `serve_cmd` body into a testable helper** (e.g. `_build_and_run_serve(config, state)`) and have unit tests pass mocked deps | Keeps tests honest; matches FW10-001 seams pattern | More refactor than the headline gap requires |
| **B — Add an env-flag escape hatch** (`FORGE_SERVE_SKIP_DISPATCH_BINDING=1`) for tests that want today's behaviour | Smallest blast radius; existing tests untouched | New runtime flag = new operational footgun |
| **C — Don't fight it; let the existing tests rebind `compose_dispatch_chain` themselves to a no-op as needed** | Zero new test infra | Every affected test grows a fixture line; risk of drift |

---

## Acceptance Criteria (review deliverables)

- [ ] **AC-D1** — A documented decision (D1.A / D1.B / D1.C) for the wiring location, with rationale and a one-liner code skeleton showing the chosen shape.
- [ ] **AC-D2** — A documented decision (D2.A / D2.B / D2.C) for the SQLite pool source, including the default value and the env-var name (if any).
- [ ] **AC-D3** — A documented decision (D3.A / D3.B) for AsyncSubAgentMiddleware construction.
- [ ] **AC-D4** — A documented decision (D4.A / D4.B / D4.C) for FW10-011 ordering, with explicit calling out of which task gets bumped from `design_approved` → `in_progress` first.
- [ ] **AC-D5** — A documented decision (D5.A / D5.B / D5.C) for unit-test compatibility, including a list of the tests that need touching.
- [ ] **AC-FILE-IMPACT** — Concrete list of files that the implementation task will touch, with line-number ranges, ranked by risk. At minimum:
  - `src/forge/cli/serve.py` — `serve_cmd` body (lines 580-590)
  - Possibly `src/forge/cli/_serve_config.py` — if D2.A is chosen
  - Possibly new module `src/forge/cli/_serve_production.py` — if D1.B is chosen
  - `tests/forge/test_cli_serve_skeleton.py`, `test_cli_serve_daemon.py` — per D5
- [ ] **AC-IMPL-TASK** — A `TASK-FIX-F010` (or similar) implementation task filed in `tasks/backlog/feat-jarvis-internal-001-followups/` referencing this review's `report_path`, with the chosen options codified into Acceptance Criteria.
- [ ] **AC-FW10-011-LINK** — Cross-reference back-and-forth with TASK-FW10-011: this review's report names FW10-011 as the integration-test sibling; FW10-011's frontmatter is updated to name this review as its production-binding sibling.
- [ ] **AC-RUNBOOK-CLOSE** — Once the implementation task lands, the jarvis runbook RESULTS table at row 7.x flips ❌ → ✅ on a third re-run. Capture the third re-run's correlation_id in the implementation task's completion notes.

---

## Files Expected to Change (after implementation)

(For sizing only — the review's job is to choose; the implementation task does the work.)

- `src/forge/cli/serve.py` — `serve_cmd` (mandatory; D1 determines whether 1 line or full body)
- `src/forge/cli/_serve_config.py` — schema extension (conditional on D2.A)
- `src/forge/cli/_serve_production.py` — new module (conditional on D1.B)
- `tests/forge/test_cli_serve_*.py` — fixture / mock updates (per D5)
- `tests/integration/test_forge_serve_orchestrator_e2e.py` — new file (per FW10-011 design; conditional on D4.A or D4.C)
- Possibly `Dockerfile` — `RUN mkdir -p /var/forge && chown forge:forge /var/forge` if D2.A defaults to `/var/forge/lifecycle.sqlite` and the runtime image doesn't already create it
- `tasks/backlog/forge-serve-orchestrator-wiring/IMPLEMENTATION-GUIDE.md` — update with the chosen wiring shape
- `tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md` — frontmatter cross-link to this review (per AC-FW10-011-LINK)

---

## References

### Source-of-truth (forge)

- [`src/forge/cli/serve.py`](src/forge/cli/serve.py) — `serve_cmd` (`@click.command(name="serve")`, line 580); `_run_serve` (line 490); `compose_dispatch_chain` seam (line 187); `_default_compose_dispatch_chain` no-op (line 167); `bind_production_dispatch_chain` factory (line 190); `_build_async_subagent_middleware` (line 262)
- [`src/forge/cli/_serve_daemon.py`](src/forge/cli/_serve_daemon.py) — `_default_dispatch` receipt-only stub (line 166); `dispatch_payload` rebindable seam (line 222); `_process_message` invocation (line 270, calls `await dispatch_payload(msg)` at line 298)
- [`src/forge/cli/_serve_dispatcher.py`](src/forge/cli/_serve_dispatcher.py) — `make_handle_message_dispatcher` (the closure that `bind_production_dispatch_chain` wraps)
- [`src/forge/cli/_serve_deps.py`](src/forge/cli/_serve_deps.py) — `build_pipeline_consumer_deps` factory
- [`tasks/completed/TASK-FW10-007-compose-pipeline-consumer-deps.md`](../../completed/TASK-FW10-007-compose-pipeline-consumer-deps.md) — the task that built `bind_production_dispatch_chain` but documented the wiring as a separate `serve_cmd` rebind step
- [`tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md`](../../completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md) — `design_approved`; the integration test that would have caught this

### Source-of-truth (jarvis runbook lineage)

- `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md` — the rerun that surfaced this gap (correlation_id `18036705-2bb7-4564-8363-315bf7716a48`)
- `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run.md` — the 2026-05-01 baseline (correlation_id `a58ec9a7-27c6-485a-beac-e18675639a10`)
- `../../../../../jarvis/docs/runbooks/RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` — Phase 7 rewrite for FEAT-FORGE-010 (`20014fb`)

### Sibling forge-side follow-ups

- [`TASK-FORGE-FRR-001` (superseded-by-feature → FEAT-FORGE-010)](../../completed/TASK-FORGE-FRR-001/TASK-FORGE-FRR-001-wire-dispatch-payload-to-real-orchestrator.md) — the original forge-followup-1 from 2026-05-01 that was rolled up into FEAT-FORGE-010
- [`TASK-FORGE-FRR-001b` (superseded-by-feature → FEAT-FORGE-010)](../../completed/TASK-FORGE-FRR-001b/TASK-FORGE-FRR-001b-publish-pipeline-lifecycle-from-autobuild-orchestrator.md) — also rolled up
- [`TASK-FORGE-FRR-002` ✅ landed](../../completed/TASK-FORGE-FRR-002/TASK-FORGE-FRR-002-wire-logging-basicconfig-for-forge-log-level.md) — the logging fix that made *this* gap diagnosable; without it the receipt-only stub's log line wouldn't have been visible in `docker logs forge-prod`
- [`TASK-FORGE-FRR-003` ✅ landed](../../completed/TASK-FORGE-FRR-003/TASK-FORGE-FRR-003-fix-build-image-script-context-path.md) — for context only

### Operational context

- **Discovered-on machine**: GB10 (`promaxgb10-41b1`)
- **Discovered-on date**: 2026-05-04
- **Reproducer**: jarvis runbook §6.2 + §7 against forge-prod built from `de23557` (FEAT-DEA8 finalize)
- **Reproduction is deterministic**: this is a wiring gap, not a flake — every `pipeline.build-queued.*` envelope reproduces the symptom

### Cross-machine state (rerun 2026-05-04)

- NATS canonical (`ships-computer-nats`, host-network): 7 streams + 4 KV ✅
- forge-prod (host-network, `forge:latest`, FEAT-DEA8-merged): up healthy on `:8088` ✅; `forge-serve` durable consumer attached ✅; `_configure_logging` emits to `docker logs` ✅ (TASK-FORGE-FRR-002 win)
- jarvis (`.venv/bin/jarvis chat`): clean boot — zero NATS subscription errors ✅ (TASK-FRR-001 win); `~/.jarvis/traces/` autocreated ✅ (TASK-FRR-003 win)
- llama-swap (`:9000`): `qwen36-workhorse` reasoner, `nomic-embed` embeddings ✅
- graphiti-mcp: container healthy this run; `:8080` shadowed by open-webui — used FRR-003 soft-fail offload path

---

## Notes for the reviewer

- **One-line fix is a trap.** The fix LOOKS like one assignment in `serve_cmd`. It isn't, because the `bind_production_dispatch_chain(forge_config=..., sqlite_pool=..., async_task_starter=...)` call needs three constructed deps. The review must decide where each one is built.
- **TASK-FW10-011 is your friend.** Read its acceptance criteria carefully — it already documents the integration shape this fix needs to satisfy. The decision is whether to land it before, with, or after the production binding.
- **`_default_dispatch` is intentionally still safe.** It acks every message — so a misconfigured deployment never wedges the JetStream queue. That's why this gap is a *silent* failure rather than a redelivery storm. The fix's purpose is not to make the daemon safer; it's to make it actually do its job.
- **Local repro is one command:** rebuild forge image from current `main`, point at canonical NATS, drive `jarvis chat` with the §6.2 prompt. The receipt-only log line is the entire signal. Full evidence at `/tmp/runbook-evidence-rerun-2026-05-04/` on GB10.
- **Don't be tempted to extend `_default_dispatch` instead.** That's a step backwards — the FW10-007 contract explicitly says the receipt-only stub *"no longer reachable on the production code path"* once the rebind happens. The right answer is to make the rebind actually run, not to make the stub do more.
- **Operator gating consideration:** the user has ratified the local-only ethos (per ADR-ARCH-001 reinforcement during the FRR-002 work). Any new env-var introduced (D2.A, D5.B) should default to local-friendly values — `/var/forge/lifecycle.sqlite` is fine for the SQLite path; `localhost`-flavoured for any new networking knob.
- **Once chosen, the implementation task should not be a review task.** It's a `task_type: fix` with concrete ACs. This review's job is to compress the option space; the implementation's job is to execute the chosen option.
