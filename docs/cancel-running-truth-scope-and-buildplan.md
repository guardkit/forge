# U2 / FEAT-FCT — forge-cancel truth, RUNNING half (scope + buildplan)
## 2026-07-30 · BINDING SPEC · plan-of-record U2 (register 2b; FWD-005 completion)

> Grounded in the mission/plan + the 2026-07-30 handoff §5-U2. Design pass:
> coordinator-direct (the day's subagent outage; seams pinned by own reads).
> Measurable: **M0** (removes the lying-ledger surgery class on RUNNING cancels).

## The problem

`forge cancel` on a RUNNING build calls `cancel_async_task(task_id)` then marks
the row CANCELLED (`cli_steering.py` AUTOBUILD_RUNNING branch, ≈:134) — but
production injects the literal no-op (`runtime.py:202` →
`_noop_async_call:62`), so the sidecar's guardkit run keeps executing. The
paused-at-gate half is live-proven (07-27/28); this wires the designed
interrupt (FRR-PEB scope §7: `runs.cancel(thread_id, run_id,
action="interrupt")` is the universal cancel surface). Second gap (scope doc's
flagged concern, confirmed at `autobuild_runner.py:2082-2107`): the subprocess
wait reaps on TimeoutError but NOT on `asyncio.CancelledError` — an interrupt
would orphan the guardkit child alive (the 07-28 orphan class).

## The design

1. **`_langgraph_interrupt_canceller(runner_url)`** factory in
   `src/forge/cli/runtime.py`: returns a SYNC callable `(task_id) -> bool`
   running `asyncio.run(...)`: `langgraph_sdk.get_client(url=runner_url)` →
   `runs.list(task_id, limit=1)` (task_id == thread_id — the FTR-established
   identity) → if a run exists: `runs.cancel(task_id, run_id,
   action="interrupt")`, log INFO "interrupt issued"; no runs → log WARNING,
   False. ANY exception → log WARNING, False — **best-effort by design**: an
   unreachable sidecar must not strand the CLI cancel; the row flip proceeds
   either way (Group D truth unchanged; the honest residual is stated in the
   rationale/ledger, not hidden).
2. **Wire as the production default** in `build_cli_runtime`: the canceller
   becomes `AsyncTaskCanceller(async_task_canceller or
   _langgraph_interrupt_canceller(runner_url))` with `runner_url` from env
   `FORGE_AUTOBUILD_RUNNER_URL`, default from the ServeConfig default constant
   (`_serve_config.py` — reuse, don't duplicate the literal). Test overrides
   keep working (the optional param wins).
3. **Runner reaping** (`autobuild_runner.py` wait block ≈:2082): add
   `except asyncio.CancelledError:` mirroring the timeout branch — `proc.kill()`
   (ProcessLookupError-safe) + bounded `proc.wait()` reap (5s) + **re-raise**
   (the node's cancellation must propagate so langgraph records the interrupt;
   no snapshot is emitted — the worktree survives implicitly because removal is
   success-path-only, so receipts are preserved on cancel).
4. **Tests**: canceller factory (mocked `langgraph_sdk.get_client`: cancel
   called with `action="interrupt"`; empty runs → False, no cancel; exception →
   False, never raises) in a new `tests/forge/test_cli_runtime_canceller.py`;
   runner reaping (fake proc + cancelled task → kill called + CancelledError
   re-raised) beside the worktree tests.

## Honest limits (ledger these)

- Interrupt delivery is best-effort; the row's CANCELLED remains the Group D
  truth it already was. What changes: the run now actually gets interrupted in
  the reachable-sidecar case, and the child is reaped. Live proof needs a real
  RUNNING build — parks for the next attended window (unit tests today).
- The bridge's observed-terminal emit on interrupt (scope §7's open question)
  is NOT built here — the existing false-terminal-hardened observer path
  handles the stream end; anything further is a future lane.

## Fences

Forge venue only; no cli_steering semantic changes (rationales/states
untouched); no broker access in tests (mock the SDK); `.guardkit/**`/`uv.lock`
untouched; path-limited commits; push after own review; deploy = forge-prod
image rebuild + recreation (handoff §2.5) — the CLI runs in the container.
