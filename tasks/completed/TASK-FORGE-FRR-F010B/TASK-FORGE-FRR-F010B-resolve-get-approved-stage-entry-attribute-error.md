---
id: TASK-FORGE-FRR-F010B
title: "Resolve `get_approved_stage_entry` AttributeError in autobuild dispatch path"
status: completed
created: 2026-05-04T00:00:00Z
updated: 2026-05-04T20:05:00Z
completed: 2026-05-04T20:05:00Z
previous_state: in_review
completed_location: tasks/completed/TASK-FORGE-FRR-F010B/
state_transition_reason: "AC-1..AC-5 satisfied with regression-locked tests; AC-6 left as operator follow-up (live-wire jarvis runbook rerun against rebuilt forge image)"
organized_files:
  - TASK-FORGE-FRR-F010B-resolve-get-approved-stage-entry-attribute-error.md
priority: high
task_type: fix
tags:
  - forge-serve
  - feat-forge-010-followup
  - first-real-run-followup
  - task-fix-f010-followup
  - autobuild
  - dispatch
  - persistence
  - attribute-error
  - wiring-drift
complexity: 4
estimated_minutes: 90
estimated_effort: "60-120 minutes (find caller, decide method-add vs caller-rename, add unit test)"
parent_feature: FEAT-FORGE-010
correlation_id: f876fd47-5e3c-4851-8f89-a7b7bcab8464
related_tasks:
  - TASK-FW10-002   # autobuild_runner subagent
  - TASK-FW10-005   # AutobuildStateInitialiser binding (likely the caller's origin)
  - TASK-FW10-007   # composed PipelineConsumerDeps
  - TASK-FIX-F010   # the wiring this exposes
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Post-TASK-FIX-F010 jarvis FRR runbook rerun on the GB10 — production composer wired (TASK-FIX-F010 verified) but autobuild dispatch path errored once exercised end-to-end"
test_results:
  status: passing
  coverage: 143/143 dispatch+persistence surface; 1 pre-existing unrelated failure (clock-hygiene lint, commit 41cba9cc)
  last_run: 2026-05-04T19:50:00Z
---

# Task: Resolve `get_approved_stage_entry` AttributeError in autobuild dispatch path

## Description

Run 4 of the post-TASK-FIX-F010 rerun (correlation_id
`f876fd47-5e3c-4851-8f89-a7b7bcab8464`) reached the **deepest** point
in the production dispatch chain that any rerun has ever reached:
the dispatcher passed validation, `dispatch_build` persisted the
QUEUED row, the consumer logged `dispatching autobuild` — and then
raised:

```
AttributeError: 'SqliteLifecyclePersistence' object has no attribute 'get_approved_stage_entry'
```

This is **internal-to-forge wiring drift** between FW10-005's
`AutobuildStateInitialiser` binding (or one of its forward-context
builders under `forge.cli._serve_deps_*`) and the persistence facade.
The caller expects a method that the facade does not expose. The
`pipeline_consumer.handle_message` outer try/except catches it,
logs, and acks — so the JetStream queue is not wedged, but no
`pipeline.build-started.*` envelope is ever published, so no
downstream consumer (jarvis chat REPL, observers) sees the build
attempt at all.

Forge unit tests do not cover this path because FW10-005's tests use
a **fake persistence object** that satisfies whatever interface the
caller expects — the fake never surfaces the
mismatch with the real `SqliteLifecyclePersistence` facade.

## Why

### Empirical evidence (run 4, 2026-05-04 evening)

correlation_id `f876fd47-5e3c-4851-8f89-a7b7bcab8464`:

```
[INFO] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatching build feature_id=FEAT-43DE correlation_id=f876fd47-... originating_adapter=terminal
[INFO] forge.cli._serve_deps: dispatch_build: persisted QUEUED row build_id=build-FEAT-43DE-20260504073635 feature_id=FEAT-43DE correlation_id=f876fd47-...; dispatching autobuild
[WARNING] forge.adapters.nats.pipeline_consumer: pipeline_consumer: dispatch_build raised ('SqliteLifecyclePersistence' object has no attribute 'get_approved_stage_entry') for feature_id=FEAT-43DE correlation_id=f876fd47-...; acking and continuing so the next build can be processed
```

Note that the QUEUED row IS successfully persisted before the
exception — partial state is committed to SQLite, but no outbound
lifecycle envelope flows back. From the operator's perspective:
"forge said it was dispatching autobuild and then disappeared."

### Why this is wiring drift, not a bug-in-isolation

The caller (somewhere in the autobuild dispatcher / state initialiser
chain) was written against a Protocol or duck-typed expectation that
the FW10-005 unit-test fake satisfies. The real
`SqliteLifecyclePersistence` does not — either the method exists
under a different name (rename caller) or it genuinely doesn't exist
on the facade (add it). Either way, the seam between
`AutobuildStateInitialiser` (FW10-005) and `SqliteLifecyclePersistence`
(FW10-007) was never exercised end-to-end before TASK-FIX-F010 ran
the production composer for real.

This is exactly the class of failure that TASK-FW10-011 (the
end-to-end integration test, status `design_approved` per the
post-merge follow-ups in the README) is designed to catch — see
ordering note below.

## Investigation Required

This task starts with an investigation step before the fix:

1. **Locate the caller**: grep `forge/src/` for
   `get_approved_stage_entry`. The caller is likely inside one of:
   - `forge.pipeline.dispatchers.autobuild_async`
   - `forge.lifecycle.recovery`
   - A forward-context builder under `forge.cli._serve_deps_*`
     (most likely candidate: `_serve_deps_state_channel.py`, since
     run 4's log line
     `build_autobuild_state_initialiser: composed SQLite-backed AutobuildStateInitialiser against pool db_path=/var/forge/forge.db`
     names that module).
2. **Compare against `forge.lifecycle.persistence.SqliteLifecyclePersistence`**:
   - Does the method exist under a different name? (e.g.
     `get_stage_entry`, `read_approved_stage`, `fetch_stage_log_entry`)
   - If it doesn't exist anywhere on the facade, what's the closest
     existing method? Read `docs/design/contracts/API-sqlite-schema.md`
     for the canonical stage-entry retrieval shape.
3. **Read the FW10-005 fake** (in `tests/forge/...`) to understand
   what shape the caller expects — that's the contract the real
   facade has to satisfy.
4. **Decide between (a) and (b) below before writing any code.**

## Acceptance Criteria

- [x] **AC-1 (root cause)**: Documented in §Investigation Findings
  above — the production composer at
  `_serve_deps.py:417` passes the bare `SqliteLifecyclePersistence`
  facade to `build_forward_context_builder`, which does not
  validate the duck-typed `StageLogReader` Protocol contract;
  `get_approved_stage_entry` lives only on the test fakes, not on
  the facade.
- [x] **AC-2 (decision)**: Chose **option (b)** with a thin wrapper —
  added `_SqliteStageLogReader` and `build_stage_log_reader()` factory
  symmetric with the existing Wave-2 wrappings. Rationale documented
  in §Investigation Findings above.
- [x] **AC-3 (implementation)**: Three production diffs (see
  §Implementation summary).
- [x] **AC-4 (integration test)**: New file
  `tests/cli/test_serve_deps_dispatch_real_persistence.py` — drives
  `deps.dispatch_build(...)` against a real
  `SqliteLifecyclePersistence` over a `tmp_path` SQLite DB with
  `apply_at_boot` applied, with only `AsyncTaskStarter` mocked at
  the boundary. Verified the test catches the bug by stashing the
  wiring change — same AttributeError as run 4 fired.
- [x] **AC-5 (regression)**: `pytest tests/forge/ tests/cli/` —
  2197 passed, 1 pre-existing failure unrelated to this fix
  (`tests/forge/test_contract_and_seam.py::TestClockHygiene` —
  introduced by commit `41cba9cc` on 2026-05-02, two commits before
  this task). FW10-005 unit tests pass unchanged (option (b)
  preserves the facade surface).
- [ ] **AC-6 (live wire validation)**: Pending — depends on a
  forge image rebuild and jarvis runbook §6.2 + §7 rerun. Capture
  the rerun correlation_id in this task's completion notes once
  performed.

## Files Expected to Change

Conditional on AC-2 outcome:

**If AC-2 chooses (a) — add to facade:**
- `src/forge/lifecycle/persistence.py` — add
  `get_approved_stage_entry` to `SqliteLifecyclePersistence`
- Possibly `src/forge/lifecycle/persistence_protocol.py` (if there
  is one) — add the method to the Protocol so the FW10-005 fake is
  forced to grow it
- `tests/forge/test_persistence*.py` — unit-test the new method
- A new or extended file under `tests/forge/` for AC-4

**If AC-2 chooses (b) — rename caller:**
- `src/forge/cli/_serve_deps_state_channel.py` (most likely) OR
  `src/forge/pipeline/dispatchers/autobuild_async.py` (fallback) —
  rename the call site
- A new or extended file under `tests/forge/` for AC-4
- Possibly nothing else, since the caller's tests use a fake that
  can be regenerated

## Investigation Findings (2026-05-04)

### AC-1 — Root cause

**Single-line root cause**: The production composer at
`src/forge/cli/_serve_deps.py:417` (TASK-FW10-007) hands the bare
`SqliteLifecyclePersistence` facade to
`build_forward_context_builder(sqlite_pool, forge_config)` — but the
facade does **not** expose the `StageLogReader` Protocol surface
(`get_approved_stage_entry` / `get_all_approved_stage_entries`)
declared in `src/forge/pipeline/forward_context_builder.py:146-219`.
The first `forward_context_builder.build_for(...)` call (from
`dispatch_autobuild_async` at line 427) reaches the reader call at
`forward_context_builder.py:562` and raises AttributeError.

The factory at `_serve_deps_forward_context.py:185-254` casts the
input to `StageLogReader` purely as a documentation marker (line 240:
`stage_log_reader: StageLogReader = sqlite_pool`) — the contract is
duck-typed and is **not** validated. Tests that go through this seam
(`tests/forge/test_cli_serve_deps_forward_context.py`,
`tests/cli/test_serve_deps.py`) pass a `FakeStageLogReader` directly
or mock `dispatch_autobuild_async` itself, so the bare-facade path
was never exercised end-to-end before TASK-FIX-F010 wired the
production composer.

The empirical evidence (run 4, correlation_id
`f876fd47-5e3c-4851-8f89-a7b7bcab8464`) shows the QUEUED `builds`
row is persisted (`record_pending_build` succeeds), then the
`stage_log` write inside `dispatch_autobuild_async` is preceded by
the `forward_context_builder.build_for` call that raises — leaving
exactly the partial-state shape ("QUEUED row in `builds`, empty
`stage_log`, no outbound envelope") observed on the GB10.

### AC-2 — Decision: option (b) plus a thin wrapper, NOT bloating the facade

Chose to **add a narrow Protocol-shaped wrapper** at the production
composition seam rather than expand `SqliteLifecyclePersistence`.

**Rationale**:

1. **Codebase pattern**: every existing Wave-2 collaborator factory
   (`build_stage_log_recorder` for FW10-004,
   `build_autobuild_state_initialiser` for FW10-005,
   `build_publisher_and_emitter` for FW10-006) wraps the shared
   `SqliteLifecyclePersistence` in a narrow class rather than
   bloating the facade. The fix follows the established pattern by
   adding `build_stage_log_reader(sqlite_pool)` as the symmetric
   FW10-003 factory. The composer at `_serve_deps.py:417` already
   does this for every other Wave-2 collaborator — `forward_context_builder`
   was the outlier.

2. **Semantic translation belongs in an adapter**: the Protocol's
   `gate_decision == "approved"` filter does not map to a single
   SQLite column — `stage_log.status` is `'PASSED'/'FAILED'/'GATED'/
   'SKIPPED'`, and the gate-decision vocabulary
   (`'approved'/'failed'/'rejected'/'cancelled'` per
   `forge.pipeline.mode_c_planner._STATUS_APPROVED`) lives in
   `details_json`. Encoding that translation inside
   `SqliteLifecyclePersistence` would couple the facade to the
   forward-context-builder's vocabulary; an adapter keeps that
   coupling local to the read site.

3. **The fake's contract IS the canonical Protocol**: the
   `FakeStageLogReader` in
   `tests/forge/test_forward_context_builder.py:80` and
   `tests/cli/test_serve_deps_forward_context.py:81` already names
   the canonical methods. The new adapter exposes the same surface
   over real SQLite rows, so the fake-vs-real divergence the bug
   exploited is closed structurally.

### AC-3 — Implementation summary

- `src/forge/cli/_serve_deps_forward_context.py` —
  added `_SqliteStageLogReader` class and
  `build_stage_log_reader(sqlite_pool)` factory. The reader projects
  matching `stage_log` rows into `ApprovedStageEntry` instances,
  filtering on `details_json["gate_decision"] == "approved"` and
  the per-feature `details_json["feature_id"]` echo. Empty
  `stage_log` returns `None` / `()`, never raises.
- `src/forge/cli/_serve_deps.py:417` — wraps `sqlite_pool` via
  `build_stage_log_reader(sqlite_pool)` before passing to
  `build_forward_context_builder`, symmetric with the existing
  Wave-2 wrappings.
- `tests/forge/test_cli_serve_deps_forward_context.py` — added
  `TestSqliteStageLogReader` with 8 unit tests covering empty
  stage_log, type-rejection, unapproved filtering, path / text
  artefact projection, per-feature scoping, and multi-row ordering.
- `tests/cli/test_serve_deps_dispatch_real_persistence.py` (new) —
  AC-4 regression lock. Drives `dispatch_build` against a real
  `SqliteLifecyclePersistence` over a tmp-path SQLite DB with
  migrations applied; asserts the empty-stage_log path reaches
  `start_async_task` without raising and persists the QUEUED `builds`
  row + at least one `stage_log` row.

**Verification**: I confirmed the regression test catches the original
bug by stashing the wiring change and re-running — the test failed with
the same AttributeError observed in run 4. With the fix in place, all
24 new + 119 existing tests on the dispatch/persistence surface pass.

The FW10-005 fake update mentioned as a possible AC-5 cost did **not**
prove necessary: option (b) preserves the facade surface, so existing
fakes/tests continue to pass without changes.

## Implementation Notes

- **Don't fix it twice**: if the method exists under another name
  AND the contract doc names `get_approved_stage_entry` as the
  canonical name, prefer (a) — add the canonical name to the facade
  and (optionally) deprecate the old one. Don't create a third name.
- **The QUEUED-row-then-bomb sequence is informative**: it tells us
  exactly where the AttributeError fires. Search for any code that
  reads from `stage_log` or `approved_*` tables and is reachable
  from the autobuild dispatcher's first call after
  `dispatch_build`'s SQLite write.
- **FW10-005's fake is your contract spec**: whatever methods that
  fake exposes, the real facade should expose under the same names.
  If the fake exposes `get_approved_stage_entry`, the real facade
  needs it too. The fake won the contract by being the production
  caller's only test surface.
- **Don't loosen the consumer's outer try/except**. The
  `pipeline_consumer.handle_message` ack-and-continue behaviour is
  intentional (matches DDR-019's no-wedge-the-queue contract). The
  fix is to make the inner code path not raise, not to reduce the
  outer safety net.
- **Cross-check FW10-009 ACs**: if FW10-009 ("validation surface and
  build-failed paths") was supposed to publish a `build-failed`
  envelope when the dispatcher raises, audit whether that publish
  fires here — see TASK-FORGE-FRR-F010C, which is the
  correlation-id-threading sibling of this work.

## Ordering vs related tasks

This task has a natural dependency order with its siblings in the
post-FIX-F010 set:

1. **TASK-FORGE-FRR-F010A** (apply migrations on boot) — must land
   first; otherwise this task's AC-4 integration test can't run a
   real `SqliteLifecyclePersistence` against an empty DB.
2. **This task (F010.B)** — once schema is bootstrapped, the
   AttributeError is the next blocker.
3. **TASK-FORGE-FRR-F010C** (correlation_id threading) — independent
   of (1) and (2); can land in parallel. Without (3), even a
   successful `build-started` publish won't be routable to the
   correct chat session.
4. **TASK-FW10-011** (end-to-end integration test, currently
   `design_approved` per README post-merge follow-up AC-12) — should
   land **after** this task as the codified regression lock that
   asserts this exact failure mode never recurs.

## References

- **RESULTS file** (post-FIX-F010 addendum, evening 2026-05-04):
  [`../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`](../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md)
  — see "Gap F010.B — `dispatch_build` raises `AttributeError`".
- **TASK-FIX-F010 (production-binding sibling)**:
  [`../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md`](../../completed/TASK-FIX-F010/TASK-FIX-F010-bind-production-dispatch-chain.md)
  — introduced the wrapper that runs the production composer; this
  task closes the next deepest gap that the wired composer
  surfaces.
- **TASK-REV-F010 review report**:
  [`../../../.claude/reviews/TASK-REV-F010-review-report.md`](../../../.claude/reviews/TASK-REV-F010-review-report.md)
- **TASK-FW10-005** (`AutobuildStateInitialiser` binding) — the most
  likely origin of the caller; cross-check its fake-vs-real contract.
- **TASK-FW10-007** (composed `PipelineConsumerDeps` against the
  persistence facade) — the seam where the fake-vs-real divergence
  was first allowed to exist.
- **TASK-FW10-011** (end-to-end integration test, `design_approved`)
  — the integration test that would have caught this bug;
  resurrecting it from `tasks/completed/` is README post-merge
  follow-up AC-12.
- **Source files** (start search here):
  - [`src/forge/lifecycle/persistence.py`](../../../src/forge/lifecycle/persistence.py)
    — `SqliteLifecyclePersistence` facade
  - [`src/forge/cli/_serve_deps_state_channel.py`](../../../src/forge/cli/_serve_deps_state_channel.py)
    — most likely caller (per run 4 log line)
  - [`src/forge/pipeline/dispatchers/`](../../../src/forge/pipeline/dispatchers/)
    — fallback caller location
  - [`src/forge/adapters/nats/pipeline_consumer.py`](../../../src/forge/adapters/nats/pipeline_consumer.py)
    — outer try/except that swallows the AttributeError
  - `docs/design/contracts/API-sqlite-schema.md` — canonical
    persistence contract
- **Run that surfaced this**:
  - **correlation_id**: `f876fd47-5e3c-4851-8f89-a7b7bcab8464`
  - **Date**: 2026-05-04 (evening rerun, post-`32b67f8`)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **forge HEAD**: `af62d5c`
  - **Image**: `forge:latest` = sha256 `ebc4311026cc...`
  - **DB state at time of error**: schema bootstrapped manually
    via `docker exec ... apply_at_boot`; QUEUED row written to
    `builds` table; `stage_log` empty; AttributeError raised on the
    next read.
