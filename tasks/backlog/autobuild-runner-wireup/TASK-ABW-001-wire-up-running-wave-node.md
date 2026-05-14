---
id: TASK-ABW-001
title: Wire up _node_running_wave to invoke guardkit autobuild
task_type: feature
parent_feature: autobuild-runner-wireup
feature_id: FEAT-ABW1
wave: 1
implementation_mode: task-work
complexity: 6
dependencies: []
estimated_minutes: 180
status: pending
---

# TASK-ABW-001 — Wire up `_node_running_wave` to invoke guardkit autobuild

## Context

`src/forge/subagents/autobuild_runner.py` currently has four lifecycle-stub
nodes (`starting` → `planning_waves` → `running_wave` → `completed`) that
write `AutobuildState` snapshots but **do no autobuild work**. The state-shape
contract closed by FOLLOWUP-B-FIX is intact (the SSE bridge emits
`pipeline.build-started` and `pipeline.build-complete` correctly) but no code
is ever written into the target repo. This task closes that gap.

After this task, a `pipeline.build-queued.FEAT-XXX` envelope dequeued by
`pipeline_consumer.py` triggers an actual `guardkit autobuild feature
<feature_id> --fresh --verbose` subprocess against the resolved local repo
checkout, with exit-code mapping to the terminal lifecycle.

Source plan: [`docs/research/ideas/autobuild-runner-wireup-plan.md`](../../../docs/research/ideas/autobuild-runner-wireup-plan.md).

Demo target: FEAT-9E59 in `~/Projects/appmilla_github/api_test` on the
`ddd-demo` branch (DDDSW 2026-05-16).

## Scope

### In scope

1. **Repo-slug resolver.** A new helper `_resolve_repo_path(payload) -> Path`
   that maps `payload["repo"]` (e.g. `"appmilla/api_test"`) to an absolute
   local checkout under `<FORGE_REPO_BASE>/<basename>`. `FORGE_REPO_BASE`
   defaults to `~/Projects/appmilla_github` and is overridable via env var.
   Validates that the resolved path:
   - Exists on disk.
   - Is a git repo (`.git/` present).
   - Is inside `forge_config.permissions.filesystem.allowlist` (reuse
     `forge.adapters.nats.pipeline_consumer._path_inside_allowlist`; do not
     duplicate the resolver logic).

   On any failure, return `None` and surface a structured failure reason
   so `_node_running_wave` can transition to `failed`.

2. **guardkit path resolution.** `shutil.which("guardkit")` with
   `FORGE_GUARDKIT_PATH` env-var override. If neither resolves to an
   executable file, treat as a failure (same `failed` transition).

3. **`_node_running_wave` body.** Replace the current stub with:
   - Resolve repo path + guardkit path (above).
   - On resolver failure: emit a `failed` snapshot with the reason and
     return; the bridge translator's existing
     `_build_failed` path publishes `pipeline.build-failed` via
     `PipelineLifecycleEmitter.emit_failed`.
   - On success: write the `running_wave` snapshot first (preserves the
     current build-started timing), then invoke
     `asyncio.create_subprocess_exec(guardkit_path, "autobuild", "feature",
     feature_id, "--fresh", "--verbose", cwd=resolved_repo_path,
     env=os.environ.copy(), stdout=PIPE, stderr=STDOUT)`.
   - Stream stdout line-by-line. On each
     `[guardkit-checkpoint] Turn N complete (tests: pass|fail)` line, emit
     one `stage_complete` snapshot (use the existing
     `build_stage_complete_kwargs` helper to keep the bridge translator
     happy). If streaming is harder than expected, the acceptance fallback
     is **one** `stage_complete` snapshot emitted between `running_wave`
     and `completed` when the subprocess returns 0.
   - On subprocess exit:
     - Exit code 0 → return the `completed` snapshot update.
     - Non-zero exit, signal, or timeout → return the `failed` snapshot
       update with `tasks_failed=1` and a reason string of the form
       `"guardkit autobuild exit=<code>"`.

4. **`_node_failed` terminal node + conditional edge.**
   - Add `_node_failed(state) -> dict` emitting a `failed` snapshot.
   - In `_build_runner_graph()`, replace the unconditional
     `running_wave → completed` edge with a conditional edge that selects
     `completed` vs `failed` based on the snapshot the `_node_running_wave`
     body wrote (read it back from `async_tasks[feature_id].lifecycle`).
   - Add `failed → END` edge.
   - `AutobuildLifecycle` already includes `"failed"` and the bridge
     translator already maps `failed` → `emit_failed` (see
     `LIFECYCLE_TO_PIPELINE_EMIT` and `lifecycle_bridge/translation.py:459`),
     so no schema changes are needed.

5. **Timeout.** Default subprocess timeout of 60 minutes (configurable via
   `FORGE_AUTOBUILD_TIMEOUT_SECONDS`, default `3600`). On timeout, kill the
   subprocess and treat as a non-zero exit.

6. **Integration tests.** Two direct pytest tests against `autobuild_runner`:
   - `test_running_wave_invokes_guardkit_and_completes_on_zero_exit`:
     monkey-patches `asyncio.create_subprocess_exec` to return a fake
     process with exit code 0 and a small stdout, drives the graph, and
     asserts the final `async_tasks[feature_id].lifecycle == "completed"`
     and at least one `stage_complete` snapshot was visible mid-stream.
   - `test_running_wave_transitions_to_failed_on_nonzero_exit`: same shape
     but the fake subprocess exits with code 1; asserts terminal lifecycle
     is `failed` and `tasks_failed == 1`.
   - Place at `tests/integration/test_autobuild_runner_subprocess.py`.

   Do **not** create BDD scenarios — direct pytest is sufficient
   verification per the source plan's preface.

### Out of scope

- Concurrent autobuilds. `max_ack_pending=1` single-flight per ADR-ARCH-014
  is preserved.
- Branch parameter honouring beyond what `guardkit autobuild` itself does.
  Operator must have the branch checked out before queueing.
- PR creation / merge automation. `guardkit autobuild` leaves the worktree
  on `autobuild/<feature_id>` for human review; that is the contract.
- Multi-repo path mapping config. The `<FORGE_REPO_BASE>/<basename>`
  convention plus env var is sufficient for the single-host layout.
- Operational artefacts (allowlist update on GB10, sidecar restart). Owned
  by TASK-ABW-OPS (operator_handoff).

## Acceptance criteria

- [ ] `_resolve_repo_path(payload)` returns an absolute `Path` for a valid
  payload (`repo="appmilla/api_test"`, default `FORGE_REPO_BASE`) and
  returns `None` (with a logged reason) when the path is missing, not a
  git repo, or outside the configured allowlist.
- [ ] `_resolve_repo_path` honours `FORGE_REPO_BASE` env-var override.
- [ ] guardkit path resolution honours `FORGE_GUARDKIT_PATH` env-var
  override; falls back to `shutil.which("guardkit")` otherwise.
- [ ] `_node_running_wave` invokes `asyncio.create_subprocess_exec` with
  argv `[guardkit_path, "autobuild", "feature", feature_id, "--fresh",
  "--verbose"]` and `cwd=resolved_repo_path`.
- [ ] On subprocess exit code 0, the final state has
  `async_tasks[feature_id].lifecycle == "completed"`.
- [ ] On non-zero subprocess exit, the final state has
  `async_tasks[feature_id].lifecycle == "failed"` and
  `tasks_failed == 1`.
- [ ] On subprocess timeout (`FORGE_AUTOBUILD_TIMEOUT_SECONDS` exceeded),
  the subprocess is killed and the runner transitions to `failed`.
- [ ] `_node_failed` exists and is reachable via a conditional edge from
  `running_wave`.
- [ ] At least one `stage_complete` snapshot is emitted between
  `running_wave` and `completed` on a successful run (either streamed
  per-checkpoint or the single-emit fallback).
- [ ] `tests/integration/test_autobuild_runner_subprocess.py` exists with
  the two tests described in §Scope item 6 and both pass under
  `pytest tests/integration/test_autobuild_runner_subprocess.py -v`.
- [ ] All modified files pass project-configured lint/format checks with
  zero errors.

## Implementation notes

- The launch payload threaded into the runner already contains `repo`,
  `feature_id`, `feature_yaml_path`, etc. — see
  `BuildQueuedPayload.model_fields` (`nats_core.events`). Pull `repo`
  out of `_extract_launch_payload(state["messages"])`.
- `_path_inside_allowlist` lives at
  `src/forge/adapters/nats/pipeline_consumer.py:248`. Import it from
  there rather than re-implementing; if you would rather avoid the
  adapter→subagent direction, lift it to a shared module — but do not
  copy-paste.
- The bridge translator's `_build_failed`
  (`src/forge/lifecycle_bridge/translation.py:549`) already constructs a
  `BuildFailedPayload` from the `failed` snapshot and `emit_failed`
  publishes it. You do **not** need to call `emit_failed` directly from
  the subagent; just write the `failed` snapshot to the channel and the
  existing bridge wiring will publish.
- Test mode strategy: have the integration tests inject a fake
  `_resolve_repo_path` and a fake `asyncio.create_subprocess_exec` via
  monkey-patching at the module surface; do not require a real
  guardkit-installed sandbox. The mocked subprocess's stdout should
  include one `[guardkit-checkpoint] Turn 1 complete (tests: pass)` line
  so the `stage_complete` assertion has something to latch onto.
- `_extract_launch_payload` falls back to `{}` on a malformed launch.
  Preserve that contract: treat a missing `repo` or `feature_id` as a
  `failed` transition with a clear reason ("missing repo in launch
  payload"), not a crash.

## Sidecar restart reminder

Per the multi-specialist runbook §2.0, editing `autobuild_runner.py`
requires a sidecar restart because LangGraph dev is launched with
`--no-reload`:

```bash
pkill -f "langgraph dev" && rm -rf .langgraph_api/ && <re-launch>
```

The restart workflow itself is owned by TASK-ABW-OPS; this task only
needs to call it out so the next runner is aware.
