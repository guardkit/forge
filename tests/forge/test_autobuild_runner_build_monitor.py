"""The build monitor WIRED INTO the runner — wedge handling end to end.

Design of record: ``ai-transition/docs/build-monitor-design-pass-2026-07-31.md``
(Rich's 2026-07-30 ruling — kill-clocks are dead as a liveness mechanism).

``tests/forge/test_build_monitor.py`` proves the detector, the honest counts and
the relaunch decision as units. This module proves the RUNNER does the right
thing with them:

* a wedged build gets an honest *semantic* terminal (never "timed out"), the
  existing failure pack, and a manifest whose ``resume`` block carries a
  ``--resume`` command — never ``--fresh``;
* a healthy build being watched is never touched (the negative control at the
  integration level: this is the killed-healthy-build defect the ruling names);
* the exit snapshot's ``tasks_completed`` comes from the build's own ledger,
  read BEFORE the success path removes the worktree;
* the wall clock is demoted to an insanity bound while the per-build BUDGET cap
  (FEAT-UBS-002) keeps working exactly as ruled.

Network-free by construction: no broker, no docker, no git, no guardkit — the
subprocess is a double and every path is a tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from langchain_core.messages import HumanMessage

from forge.subagents import autobuild_runner as ar
from forge.subagents import build_monitor as bm

FEATURE_ID = "FEAT-BMW"
BUILD_ID = "build-FEAT-BMW-1"
BRANCH = "lane/build-monitor"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _BlockingStdout:
    """Yields canned lines, then blocks until the process is reaped."""

    def __init__(self, done: asyncio.Event, lines: list[bytes]) -> None:
        self._done = done
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(0)
            return self._lines.pop(0)
        await self._done.wait()
        return b""


class _LiveFakeProc:
    """A guardkit double that stays alive until something kills it.

    This is what makes the monitor testable at the runner level: the drain
    reaches EOF only when ``kill()`` is called, so a test can prove BOTH that a
    wedge kills the build and that a healthy build is never killed.
    """

    def __init__(self, lines: list[bytes]) -> None:
        self.pid = 9911
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()
        self.stdout = _BlockingStdout(self._done, lines)

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode if self.returncode is not None else -9

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    def finish(self, exit_code: int = 0) -> None:
        """Let the build end on its own terms (the healthy path)."""
        self.returncode = exit_code
        self._done.set()


def _make_exec(proc: Any) -> tuple[Any, dict[str, Any]]:
    captured: dict[str, Any] = {"args": (), "kwargs": {}}

    async def _fake(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    return _fake, captured


def _launch_state(*, budget: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "build_id": BUILD_ID,
        "feature_id": FEATURE_ID,
        "repo": "appmilla/api_test",
        "branch": BRANCH,
        "correlation_id": "corr-bmw-1",
    }
    if budget is not None:
        payload["budget"] = budget
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner "
        f"payload={json.dumps(payload, sort_keys=True)}"
    )
    return {"messages": [HumanMessage(content=description)]}


def _seed_worktree(
    worktree: Path,
    *,
    statuses: dict[str, str],
    tasks_completed: int,
    tasks_failed: int = 0,
    current_wave: int = 1,
) -> None:
    """Materialise the build's own ledger + a receipt family in the worktree."""
    features = worktree / ".guardkit" / "features"
    features.mkdir(parents=True, exist_ok=True)
    (features / f"{FEATURE_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "id": FEATURE_ID,
                "tasks": [
                    {"id": task_id, "name": task_id, "status": status}
                    for task_id, status in statuses.items()
                ],
                "execution": {
                    "tasks_completed": tasks_completed,
                    "tasks_failed": tasks_failed,
                    "current_wave": current_wave,
                    "completed_waves": list(range(1, current_wave)),
                    "last_updated": "2026-07-31T12:00:00",
                    "worktree_path": str(worktree),
                },
            }
        ),
        encoding="utf-8",
    )
    progress = worktree / ".guardkit" / "autobuild" / "TASK-BMW-003"
    progress.mkdir(parents=True, exist_ok=True)
    (progress / "progress.log").write_text(
        "[2026-07-31T12:00:00] START TASK-BMW-003: Player invocation\n"
        "[2026-07-31T12:01:00] SNAPSHOT TASK-BMW-003: elapsed=60s, "
        "phase=Player invocation, files_changed=4, last_tool=Edit\n",
        encoding="utf-8",
    )


async def _run_node(
    state: dict[str, Any],
    *,
    proc: Any,
    worktree: Path,
    repo: Path,
    remove_worktree: Any = None,
) -> dict[str, Any]:
    """Drive ``_node_running_wave`` with every external boundary doubled."""
    fake_exec, _captured = _make_exec(proc)

    async def _no_sweep(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _branch_exists(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _materialise(*_args: Any, **_kwargs: Any) -> Path:
        return worktree

    async def _default_remove(*_args: Any, **_kwargs: Any) -> None:
        return None

    with patch.object(ar, "_resolve_repo_path", lambda payload: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: Path("/usr/local/bin/guardkit")
    ), patch.object(ar, "_local_branch_exists", _branch_exists), patch.object(
        ar, "_sweep_build_refs", _no_sweep
    ), patch.object(
        ar, "_materialise_worktree", _materialise
    ), patch.object(
        ar, "_remove_worktree", remove_worktree or _default_remove
    ), patch.object(
        asyncio, "create_subprocess_exec", fake_exec
    ):
        return await ar._node_running_wave(state)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fast_polling_and_local_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poll fast, and keep every receipt inside tmp (the live-receipts fence)."""
    monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "0.01")
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
    monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(bm.BUILD_MONITOR_ENABLED_ENV, raising=False)
    for _var in (
        bm.GUARDKIT_TIMEOUT_MULTIPLIER_ENV,
        bm.BACKEND_BASE_URL_ENV,
        bm.ESTIMATE_TIMEOUT_FACTOR_ENV,
        bm.GUARDKIT_TASK_TIMEOUT_FLOOR_ENV,
    ):
        monkeypatch.delenv(_var, raising=False)


def _force_wedge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the (unit-tested) detector answer WEDGED on its first poll.

    The detector's own thresholds are proven with a fake clock in
    ``test_build_monitor.py``; sleeping out a real 2520s window here would test
    nothing except patience. What this module tests is the runner's HANDLING of
    the verdict.
    """

    def _wedged(self: bm.BuildMonitor, *, now: float | None = None) -> bm.WedgeVerdict:
        return bm.WedgeVerdict(
            wedged=True,
            silent_seconds=2600.0,
            window_seconds=2520.0,
            last_state=self.describe_last_state(),
        )

    monkeypatch.setattr(bm.BuildMonitor, "poll", _wedged)


# ---------------------------------------------------------------------------
# The wedge: kill + honest terminal + pack + resume
# ---------------------------------------------------------------------------


class TestWedgeHandling:
    @pytest.mark.asyncio
    async def test_a_wedged_build_is_killed_with_an_honest_semantic_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree,
            statuses={
                "TASK-BMW-001": "completed",
                "TASK-BMW-002": "completed",
                "TASK-BMW-003": "in_progress",
            },
            tasks_completed=2,
            current_wave=2,
        )
        _force_wedge(monkeypatch)
        proc = _LiveFakeProc(
            [
                b"Starting Wave Execution (task timeout: 40 min)\n",
                b"Wave 2/2: TASK-BMW-003\n",
                b"\xe2\x96\xb6 Executing TASK-BMW-003: the wedged one\n",
                b"INFO:guardkit.orchestrator.progress:[t] Completed turn 5: "
                b"feedback - still red\n",
            ]
        )

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        assert proc.killed is True, "a wedged build must actually be stopped"
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        reason = snap["error_message"]
        assert reason.startswith("wedged: no semantic progress or state movement")
        assert "timed out" not in reason, (
            "the terminal must say WHY, not blame a clock that did not fire"
        )
        assert "task=TASK-BMW-003" in reason, "the terminal names the stuck task"
        assert "worktree KEPT for forensics" in reason
        # A wedge is NOT a budget breach: it must never arm the D659 gate.
        assert "budget_cap_killed" not in snap

    @pytest.mark.asyncio
    async def test_a_wedged_build_reports_the_tasks_it_really_finished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old code said ``tasks_completed=1`` for every wedged build."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree,
            statuses={
                "TASK-BMW-001": "completed",
                "TASK-BMW-002": "completed",
                "TASK-BMW-003": "in_progress",
            },
            tasks_completed=2,
            current_wave=2,
        )
        _force_wedge(monkeypatch)
        proc = _LiveFakeProc(
            [
                # Nine turns, three tasks: the stdout stream that used to be
                # miscounted as nine (or, when wedged, as one) completed tasks.
                *[
                    b"[guardkit-checkpoint] Turn %d complete (tests: pass)\n" % turn
                    for turn in range(1, 10)
                ],
            ]
        )

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["tasks_completed"] == 2, (
            "the ledger says 2 of 3 tasks were finished before the wedge"
        )
        assert snap["tasks_completed_source"] == bm.SOURCE_FEATURE_LEDGER
        assert snap["tasks_failed"] >= 1
        assert snap["wave_index"] == 1

    @pytest.mark.asyncio
    async def test_the_failure_pack_carries_the_RESUME_command_never_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree,
            statuses={"TASK-BMW-001": "completed", "TASK-BMW-003": "in_progress"},
            tasks_completed=1,
        )
        _force_wedge(monkeypatch)
        proc = _LiveFakeProc([b"Starting Wave Execution (task timeout: 40 min)\n"])

        await _run_node(_launch_state(), proc=proc, worktree=worktree, repo=repo)

        manifest = json.loads(
            (
                tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME
            ).read_text(encoding="utf-8")
        )
        assert manifest["wedged"] is True
        assert manifest["timed_out"] is False, "no clock fired — the monitor called it"
        assert manifest["worktree_path"] == str(worktree), "the resume point is kept"
        assert manifest["branch"] == BRANCH
        assert manifest["tasks_completed"] == 1
        assert manifest["tasks_completed_source"] == bm.SOURCE_FEATURE_LEDGER

        resume = manifest["resume"]
        assert resume["possible"] is True
        assert resume["cwd"] == str(worktree)
        assert "--resume" in resume["argv"]
        assert "--fresh" not in resume["argv"], (
            "--fresh destroys the saved state: it is never the relaunch"
        )
        # F12: the base branch is pinned again so resume cannot fall to main.
        assert resume["argv"][resume["argv"].index("--base-branch") + 1] == BRANCH
        assert resume["attempt_no"] == 1

        state = manifest["semantic_state_at_kill"]
        assert state["ledger"]["in_progress"] == ["TASK-BMW-003"]
        assert "files_changed=4" in state["description"]

    @pytest.mark.asyncio
    async def test_a_second_resume_attempt_is_stamped_and_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ladder's attempt bound survives across separate node runs."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        _force_wedge(monkeypatch)

        state = _launch_state()
        payload = json.loads(state["messages"][0].content.split("payload=", 1)[1])
        payload["resume_attempt"] = bm.MAX_RESUME_ATTEMPTS
        state = {
            "messages": [
                HumanMessage(
                    content="RUN_AUTOBUILD subagent=autobuild_runner "
                    f"payload={json.dumps(payload, sort_keys=True)}"
                )
            ]
        }

        proc = _LiveFakeProc([])
        await _run_node(state, proc=proc, worktree=worktree, repo=repo)

        manifest = json.loads(
            (
                tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME
            ).read_text(encoding="utf-8")
        )
        resume = manifest["resume"]
        assert resume["possible"] is False
        assert "attempt cap reached" in resume["reason"]
        assert resume["argv"] == []


# ---------------------------------------------------------------------------
# THE NEGATIVE CONTROL at the runner level
# ---------------------------------------------------------------------------


class TestHealthyBuildIsNeverTouched:
    @pytest.mark.asyncio
    async def test_a_slow_but_progressing_build_runs_to_completion_unharmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REAL detector, polled repeatedly, must never kill this build.

        W is at least 2520s and this build finishes in milliseconds, so any kill
        here would be the monitor firing on a healthy build — the exact defect
        the lane exists to remove. The poll counter keeps the control honest: a
        watch loop that never ran would prove nothing.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree,
            statuses={"TASK-BMW-001": "completed", "TASK-BMW-002": "completed"},
            tasks_completed=2,
            current_wave=2,
        )
        polls: list[bm.WedgeVerdict] = []
        real_poll = bm.BuildMonitor.poll

        def _counting_poll(
            self: bm.BuildMonitor, *, now: float | None = None
        ) -> bm.WedgeVerdict:
            verdict = real_poll(self, now=now)
            polls.append(verdict)
            return verdict

        monkeypatch.setattr(bm.BuildMonitor, "poll", _counting_poll)
        proc = _LiveFakeProc([b"Starting Wave Execution (task timeout: 40 min)\n"])

        async def _finish_later() -> None:
            await asyncio.sleep(0.4)  # ~40 poll ticks at the 0.01s cadence
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        assert len(polls) >= 5, (
            "the watch loop must actually have polled — otherwise this control "
            f"proves nothing (polls={len(polls)})"
        )
        assert all(not verdict.wedged for verdict in polls)
        # W = max(the run's declared 40-min budget, guardkit's own reconstructed
        # resolution: max(3000s floor, 2400s yaml) × multiplier 1.0) + 120s.
        assert polls[0].window_seconds == 3120.0, (
            "W must never sit below the budget guardkit itself will enforce"
        )
        assert proc.killed is False, "a progressing build must never be killed"
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "running_wave"
        assert snap["tasks_completed"] == 2

    @pytest.mark.asyncio
    async def test_a_monitor_defect_never_kills_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the monitor itself throws, the build survives and says so."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "completed"}, tasks_completed=1
        )

        def _boom(self: bm.BuildMonitor, *, now: float | None = None) -> Any:
            raise RuntimeError("monitor bug")

        monkeypatch.setattr(bm.BuildMonitor, "poll", _boom)
        proc = _LiveFakeProc([])

        async def _finish_later() -> None:
            await asyncio.sleep(0.1)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        with caplog_at_warning() as records:
            result = await _run_node(
                _launch_state(), proc=proc, worktree=worktree, repo=repo
            )
        await finisher

        assert proc.killed is False
        assert result["async_tasks"][FEATURE_ID]["lifecycle"] == "running_wave"
        assert any("build monitor poll failed" in message for message in records)


class TestInterruptPathReapsTheWatch:
    """FEAT-FCT: a langgraph interrupt cancels the node and RE-RAISES.

    Anything placed after that try/except never runs, so the watch task's
    cancellation is drained inside the ``finally`` — otherwise the very path
    that cancels the node is the one that leaves the watch task pending
    ("Task was destroyed but it is pending" on ``runs.cancel action=interrupt``).

    Honest limit of this test: it asserts the OBSERVABLE end state on the
    interrupt path (the watch task is cancelled, done, and its outcome
    retrievable, and the guardkit child is still reaped). It does not pin the
    exact loop iteration on which the drain happens — a cancelled task that is
    never awaited also settles once the loop runs again, so the two structures
    are indistinguishable from outside except when the loop closes first.
    """

    @pytest.mark.asyncio
    async def test_the_watch_task_is_cancelled_AND_retrieved_on_interrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        proc = _LiveFakeProc([])  # never finishes on its own

        real_ensure_future = asyncio.ensure_future
        spawned: list[asyncio.Future[Any]] = []

        def _capturing_ensure_future(coro: Any, **kwargs: Any) -> Any:
            task = real_ensure_future(coro, **kwargs)
            spawned.append(task)
            return task

        # The runner resolves ``asyncio.ensure_future`` through the package
        # attribute; asyncio.gather's internal lookup is unaffected, so this
        # captures exactly the monitor's watch task.
        monkeypatch.setattr(asyncio, "ensure_future", _capturing_ensure_future)

        node_task = real_ensure_future(
            _run_node(_launch_state(), proc=proc, worktree=worktree, repo=repo)
        )
        await asyncio.sleep(0.05)  # let the watch loop start polling
        assert spawned, "the watch task must have been spawned"

        node_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await node_task

        watch_task = spawned[0]
        assert watch_task.done(), (
            "the watch task must be reaped on the interrupt path, not left "
            "pending for the event loop to complain about"
        )
        # Retrieving the outcome must not raise anything but the cancellation.
        if not watch_task.cancelled():
            assert watch_task.exception() is None
        assert proc.killed is True, "the guardkit child is still reaped on cancel"


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class caplog_at_warning:  # noqa: N801 — a context manager, used like a fixture
    """caplog is unreliable across the runner's own task; attach a handler."""

    def __enter__(self) -> list[str]:
        self._handler = _CapturingHandler()
        self._logger = logging.getLogger("forge.subagents.autobuild_runner")
        self._logger.addHandler(self._handler)
        self._previous = self._logger.level
        self._logger.setLevel(logging.WARNING)
        return self._handler.messages

    def __exit__(self, *_exc: Any) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous)


# ---------------------------------------------------------------------------
# Honest counts on the success path
# ---------------------------------------------------------------------------


class TestHonestCountsOnSuccess:
    @pytest.mark.asyncio
    async def test_the_ledger_is_read_BEFORE_the_worktree_is_removed(
        self, tmp_path: Path
    ) -> None:
        """Ordering crux: the success path deletes the tree the ledger lives in.

        The removal double really deletes the worktree, so a runner that read
        the ledger after cleanup would fall back to the assumed-single-unit
        tier and this test would catch it.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree,
            statuses={
                "TASK-BMW-001": "completed",
                "TASK-BMW-002": "completed",
                "TASK-BMW-003": "completed",
            },
            tasks_completed=3,
            current_wave=3,
        )

        async def _really_remove(_repo: Path, wt: Path) -> None:
            shutil.rmtree(wt, ignore_errors=True)

        proc = _LiveFakeProc(
            [
                b"[guardkit-checkpoint] Turn 1 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 2 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 3 complete (tests: fail)\n",
                b"[guardkit-checkpoint] Turn 4 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 5 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 6 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 7 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 8 complete (tests: pass)\n",
                b"[guardkit-checkpoint] Turn 9 complete (tests: pass)\n",
            ]
        )

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        result = await _run_node(
            _launch_state(),
            proc=proc,
            worktree=worktree,
            repo=repo,
            remove_worktree=_really_remove,
        )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["tasks_completed"] == 3, (
            "nine checkpoint TURNS across three tasks must report 3, not 9"
        )
        assert snap["tasks_completed_source"] == bm.SOURCE_FEATURE_LEDGER
        assert snap["wave_index"] == 2
        assert not worktree.exists(), "the success path still cleans up"

    @pytest.mark.asyncio
    async def test_no_ledger_labels_its_last_resort_assumption(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()  # no .guardkit at all
        proc = _LiveFakeProc([b"[guardkit-checkpoint] Turn 1 complete (tests: pass)\n"])

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["tasks_completed"] == 1
        assert snap["tasks_completed_source"] == bm.SOURCE_ASSUMED_SINGLE_UNIT

    @pytest.mark.asyncio
    async def test_a_zero_ledger_on_success_still_moves_the_wire(
        self, tmp_path: Path
    ) -> None:
        """exit 0 + ``tasks_completed: 0`` must not silence stage_complete.

        The bridge translator only emits stage_complete when
        ``snap.tasks_completed > prev.tasks_completed``
        (``lifecycle_bridge/translation.py:504-515``). Trusting a zero from a
        succeeded build reports less than the build did AND kills that
        envelope — the guarantee the old ``max(count, 1)`` hardcode was really
        carrying. The floor keeps it and labels itself on the wire.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "completed"}, tasks_completed=0
        )
        proc = _LiveFakeProc([b"\xe2\x96\xb6 Executing TASK-BMW-001: the only task\n"])

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        with caplog_at_warning() as records:
            result = await _run_node(
                _launch_state(), proc=proc, worktree=worktree, repo=repo
            )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["tasks_completed"] == 1
        assert snap["tasks_completed_source"] == bm.SOURCE_FEATURE_LEDGER_SUCCESS_FLOOR
        assert any("flooring tasks_completed" in message for message in records)

    def test_the_completed_terminal_inherits_the_measured_counts(self) -> None:
        """The wire's terminal must carry the ledger's number, not the plan's."""
        payload = {
            "build_id": BUILD_ID,
            "feature_id": FEATURE_ID,
            "correlation_id": "corr-bmw-1",
            "wave_total": 3,
            "task_total": 9,  # the PLAN said nine
        }
        state = {
            "messages": [
                HumanMessage(
                    content="RUN_AUTOBUILD subagent=autobuild_runner "
                    f"payload={json.dumps(payload, sort_keys=True)}"
                )
            ],
            "async_tasks": {
                FEATURE_ID: {
                    "feature_id": FEATURE_ID,
                    "lifecycle": "running_wave",
                    "tasks_completed": 3,  # the LEDGER said three
                    "tasks_failed": 0,
                    "wave_index": 2,
                    "tasks_completed_source": bm.SOURCE_FEATURE_LEDGER,
                }
            },
        }
        update = ar._node_completed(state)  # type: ignore[arg-type]
        snap = update["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "completed"
        assert snap["tasks_completed"] == 3
        assert snap["wave_index"] == 2
        assert snap["tasks_completed_source"] == bm.SOURCE_FEATURE_LEDGER

    def test_a_payload_only_completed_terminal_is_unchanged(self) -> None:
        """No measurement on the channel → the historical payload shape stands."""
        payload = {
            "build_id": BUILD_ID,
            "feature_id": FEATURE_ID,
            "wave_total": 2,
            "task_total": 4,
        }
        state = {
            "messages": [
                HumanMessage(
                    content="RUN_AUTOBUILD subagent=autobuild_runner "
                    f"payload={json.dumps(payload, sort_keys=True)}"
                )
            ],
            "async_tasks": {},
        }
        snap = ar._node_completed(state)["async_tasks"][FEATURE_ID]  # type: ignore[arg-type]
        assert snap["tasks_completed"] == 4
        assert snap["wave_index"] == 1
        assert "tasks_completed_source" not in snap


# ---------------------------------------------------------------------------
# (e) Timer demotion — and (f) the budget cap, untouched
# ---------------------------------------------------------------------------


class TestTimerDemotion:
    def test_the_default_wall_clock_is_an_insanity_bound_not_an_hour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
        assert ar.DEFAULT_AUTOBUILD_TIMEOUT_SECONDS == 86400
        assert ar._resolve_autobuild_timeout_seconds() == 86400.0

    def test_an_operator_can_still_set_the_bound_with_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "7200")
        assert ar._resolve_autobuild_timeout_seconds() == 7200.0

    @pytest.mark.asyncio
    async def test_an_insanity_bound_expiry_names_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clock firing now means the MONITOR is broken — say so."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")  # clock alone
        proc = _LiveFakeProc([])

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert "timed out after 0.05s" in snap["error_message"]
        assert "insanity bound" in snap["error_message"]
        assert "budget_cap_killed" not in snap, (
            "an operator/default clock expiry is not a budget breach"
        )

    @pytest.mark.asyncio
    async def test_the_budget_cap_still_binds_and_still_arms_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FEAT-UBS-002 is UNTOUCHED: spend bounds are a different job (§f)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        proc = _LiveFakeProc([])

        result = await _run_node(
            _launch_state(
                budget={"max_wallclock_seconds": 0.05, "profile_name": "unattended"}
            ),
            proc=proc,
            worktree=worktree,
            repo=repo,
        )
        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert "budget wall-clock cap of 0.05s" in snap["error_message"]
        assert "UBS-002" in snap["error_message"]
        assert snap["budget_cap_killed"] is True, (
            "the demoted clock must not disarm the D659 breach gate"
        )
        assert proc.killed is True, "a cap expiry is still a genuine kill"

    @pytest.mark.asyncio
    async def test_the_kill_switch_disarms_the_monitor_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "completed"}, tasks_completed=1
        )
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        _force_wedge(monkeypatch)  # would kill instantly IF the monitor ran
        proc = _LiveFakeProc([])

        async def _finish_later() -> None:
            await asyncio.sleep(0.1)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        with caplog_at_warning() as records:
            result = await _run_node(
                _launch_state(), proc=proc, worktree=worktree, repo=repo
            )
        await finisher

        assert proc.killed is False
        assert result["async_tasks"][FEATURE_ID]["lifecycle"] == "running_wave"
        assert any("build monitor DISABLED" in message for message in records)
