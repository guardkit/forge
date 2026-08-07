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
    """Yields canned lines, then blocks until the process is reaped.

    ``tail_lines`` model the real pipe buffer: bytes the child had already
    written but nobody had read when the kill landed. They become readable only
    AFTER the reap, exactly like a real ``asyncio.StreamReader`` draining a
    dead child's pipe, and then the stream reaches EOF. With no ``tail_lines``
    this double behaves byte-identically to the pre-lane one.
    """

    def __init__(
        self,
        done: asyncio.Event,
        lines: list[bytes],
        tail_lines: list[bytes] | None = None,
    ) -> None:
        self._done = done
        self._lines = list(lines)
        self._tail = list(tail_lines or [])
        self.reads_after_eof = 0

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(0)
            return self._lines.pop(0)
        await self._done.wait()
        if self._tail:
            await asyncio.sleep(0)
            return self._tail.pop(0)
        self.reads_after_eof += 1
        return b""


class _LiveFakeProc:
    """A guardkit double that stays alive until something kills it.

    This is what makes the monitor testable at the runner level: the drain
    reaches EOF only when ``kill()`` is called, so a test can prove BOTH that a
    wedge kills the build and that a healthy build is never killed.
    """

    def __init__(
        self, lines: list[bytes], tail_lines: list[bytes] | None = None
    ) -> None:
        self.pid = 9911
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()
        self.stdout = _BlockingStdout(self._done, lines, tail_lines)

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


# ---------------------------------------------------------------------------
# TAIL LOSS — the ordinary timeout keeps the subprocess's last words
# ---------------------------------------------------------------------------
#
# The residue (Sunday handoff §4.4). Two kill paths, asymmetric honesty:
#
#   * the WEDGE path kills and returns, so the drain runs on to EOF and reads
#     every buffered line — the tail survives for free;
#   * the ORDINARY-TIMEOUT path goes through ``asyncio.wait_for``, which
#     CANCELS the drain before the kill. Whatever sat in the pipe buffer was
#     read by nobody, teed nowhere, and never reached the monitor. On a dying
#     build that is exactly the interesting part.
#
# Every test below drives the REAL node. The lines named ``tail`` are readable
# only after the reap, which is what a real pipe buffer does.


class TestTailLossOnTheOrdinaryTimeout:
    """The bounded post-kill read the wedge path got by accident."""

    @staticmethod
    def _stdout_log(tmp_path: Path) -> Path:
        return tmp_path / "receipts" / BUILD_ID / ar.STDOUT_LOG_NAME

    @pytest.mark.asyncio
    async def test_the_wall_clock_kill_recovers_the_buffered_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTATION PROOF: this test fails on the pre-lane tree.

        Before the bounded tail read, the ``wait_for`` cancel closed the tee
        first and killed second, so neither of these lines existed anywhere.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")
        proc = _LiveFakeProc(
            [b"Starting Wave Execution (task timeout: 40 min)\n"],
            tail_lines=[
                b"TIMEOUT TASK-BMW-001: guardkit task clock fired\n",
                b"Traceback (most recent call last):\n",
                b'  File "orchestrator.py", line 1, in run\n',
            ],
        )

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        assert result["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        log = self._stdout_log(tmp_path).read_text(encoding="utf-8")
        assert "Starting Wave Execution" in log, "the pre-kill narrative survives"
        assert "TIMEOUT TASK-BMW-001: guardkit task clock fired" in log, (
            "the killed build's LAST WORDS are the evidence this lane exists "
            "for — they used to be dropped on the floor"
        )
        assert "Traceback (most recent call last):" in log
        assert 'File "orchestrator.py", line 1, in run' in log

    @pytest.mark.asyncio
    async def test_the_tail_reaches_the_monitor_not_just_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recovered lines are LIVENESS input, not just bytes on disk."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-007": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")
        proc = _LiveFakeProc(
            [],
            tail_lines=[
                b"\xe2\x96\xb6 Executing TASK-BMW-007: the one that died\n",
                b"INFO:guardkit.orchestrator.progress:[t] Completed turn 9: "
                b"feedback - still red\n",
            ],
        )

        await _run_node(_launch_state(), proc=proc, worktree=worktree, repo=repo)

        manifest = json.loads(
            (
                tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME
            ).read_text(encoding="utf-8")
        )
        state = manifest["semantic_state_at_kill"]
        assert state["last_turn"] == 9, (
            "turn 9 was announced ONLY in the post-kill tail — without the "
            "bounded read the pack would say turn=?"
        )
        assert state["last_decision"] == "feedback"
        assert "turn=9" in state["description"]
        assert "TASK-BMW-007" in state["stdout_task_ids"]

    @pytest.mark.asyncio
    async def test_the_budget_cap_kill_keeps_its_tail_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FEAT-UBS-002's cap takes the SAME branch — and the D659 gate holds."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
        proc = _LiveFakeProc(
            [], tail_lines=[b"the last thing the capped build ever said\n"]
        )

        result = await _run_node(
            _launch_state(
                budget={"max_wallclock_seconds": 0.05, "profile_name": "unattended"}
            ),
            proc=proc,
            worktree=worktree,
            repo=repo,
        )

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["budget_cap_killed"] is True, (
            "the tail read must not disturb the D659 breach gate"
        )
        assert "the last thing the capped build ever said" in self._stdout_log(
            tmp_path
        ).read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_the_wedge_path_is_not_double_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEGATIVE CONTROL: the wedge already drained to EOF — leave it alone.

        Its kill does not go through ``wait_for``, so no tail read runs; the
        drain reads these lines itself, exactly once each, exactly as before.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        _force_wedge(monkeypatch)
        proc = _LiveFakeProc([], tail_lines=[b"post-kill line\n"])

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        assert proc.killed is True
        assert result["async_tasks"][FEATURE_ID]["lifecycle"] == "failed"
        lines = self._stdout_log(tmp_path).read_text(encoding="utf-8").splitlines()
        assert lines.count("post-kill line") == 1, (
            "the wedge path must read its tail once — not twice"
        )
        assert (
            len([line for line in lines if line.startswith(ar.STDOUT_RUN_HEADER_PREFIX)])
            == 1
        ), "one run, one header"

    @pytest.mark.asyncio
    async def test_a_silent_tail_leaves_the_log_exactly_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BYTE-IDENTITY control: nothing buffered, nothing new written."""
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")
        proc = _LiveFakeProc([b"only line the build ever printed\n"])

        await _run_node(_launch_state(), proc=proc, worktree=worktree, repo=repo)

        lines = self._stdout_log(tmp_path).read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith(ar.STDOUT_RUN_HEADER_PREFIX)
        assert lines[1:] == ["only line the build ever printed"]
        assert len(lines) == 2, "the tail read must add nothing when there is none"


class TestTailReadCannotAlterTheTerminal:
    """The tee's standing posture: best-effort, never load-bearing."""

    @pytest.mark.asyncio
    async def test_a_hung_tail_expires_into_one_warning(self, tmp_path: Path) -> None:
        """A kill that did not take must not hold the terminal for ever."""

        class _NeverEnds:
            async def readline(self) -> bytes:
                await asyncio.sleep(3600)
                return b""

        class _Proc:
            stdout = _NeverEnds()

        tee = ar._StdoutTee(tmp_path / "pack" / ar.STDOUT_LOG_NAME)
        with caplog_at_warning() as records:
            recovered = await ar._drain_tail(
                _Proc(), tee, None, budget=0.01, feature_id=FEATURE_ID
            )

        assert recovered == 0
        assert any("post-kill tail read stopped" in message for message in records)

    @pytest.mark.asyncio
    async def test_a_raising_monitor_stops_the_tail_without_raising(
        self, tmp_path: Path
    ) -> None:
        class _OneLine:
            def __init__(self) -> None:
                self._lines = [b"a line\n"]

            async def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

        class _Proc:
            stdout = _OneLine()

        class _AngryMonitor:
            def note_stdout_line(self, line: str) -> None:
                raise RuntimeError("monitor defect")

        tee = ar._StdoutTee(tmp_path / "pack" / ar.STDOUT_LOG_NAME)
        with caplog_at_warning() as records:
            recovered = await ar._drain_tail(
                _Proc(),
                tee,
                _AngryMonitor(),  # type: ignore[arg-type]
                feature_id=FEATURE_ID,
            )

        assert recovered == 0
        assert any("post-kill tail read stopped" in message for message in records)

    @pytest.mark.asyncio
    async def test_a_process_without_a_pipe_is_a_no_op(self, tmp_path: Path) -> None:
        class _Proc:
            stdout = None

        tee = ar._StdoutTee(tmp_path / "pack" / ar.STDOUT_LOG_NAME)
        assert await ar._drain_tail(_Proc(), tee, None) == 0
        assert not (tmp_path / "pack" / ar.STDOUT_LOG_NAME).exists()

    def test_the_budget_is_named_not_folklore(self) -> None:
        assert ar.TAIL_READ_BUDGET_SECONDS == 5.0
        assert "TAIL_READ_BUDGET_SECONDS" in ar.__all__

    @pytest.mark.asyncio
    async def test_a_slow_tail_cannot_relabel_a_timeout_as_a_wedge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tail read must not WIDEN the leaked-pid race — it closes it.

        If the kill does not take, ``returncode`` stays None and the wedge
        watch keeps polling. A poll landing during the tail read would stamp a
        wall-clock death as a WEDGE — a lie about which death this was, and
        one the timeout-truth lane would then carry onto the row. The watch is
        therefore stopped BEFORE the tail read.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        # The clock must fire FIRST and the poll must land during the reap /
        # tail read — so the wall clock is short and the poll interval is long.
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.01")
        monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "0.05")
        _force_wedge(monkeypatch)  # every poll tick says WEDGED

        class _LeakyFakeProc(_LiveFakeProc):
            """A kill that does not take: the reap returns, returncode stays None."""

            def kill(self) -> None:
                self.killed = True
                self._done.set()  # the reap completes...
                # ...but returncode is deliberately NOT set, so the watch loop
                # still believes the process is alive.

        async def _slow_tail(*_args: Any, **_kwargs: Any) -> int:
            await asyncio.sleep(0.2)  # ~20 poll ticks at the fixture's 0.01s
            return 0

        monkeypatch.setattr(ar, "_drain_tail", _slow_tail)
        proc = _LeakyFakeProc([])

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        reason = result["async_tasks"][FEATURE_ID]["error_message"]
        assert "insanity bound" in reason, (
            "the wall clock fired — the terminal must say so"
        )
        assert not reason.startswith("wedged:"), (
            "a poll landing during the tail read must not relabel this death"
        )


# ---------------------------------------------------------------------------
# TIMEOUT TRUTH — the runner stamps WHICH death this was
# ---------------------------------------------------------------------------
#
# The residue (Sunday handoff §4.4): five structurally different terminal
# causes all left this node as one thing — a ``failed`` snapshot whose only
# distinguishing carrier was a prose string. These tests drive all five through
# the real node and pin what each one now leaves behind, including the one that
# must leave NOTHING new.


def _append_progress_line(worktree: Path, task_id: str, line: str) -> None:
    """Append a raw record to a task's progress.log inside the worktree."""
    path = worktree / ".guardkit" / "autobuild" / task_id / "progress.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _read_manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        (tmp_path / "receipts" / BUILD_ID / ar.FAILURE_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )


class TestTerminalClass:
    @pytest.mark.asyncio
    async def test_a_monitor_kill_is_classed_timeout_wedge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-003": "in_progress"}, tasks_completed=0
        )
        _force_wedge(monkeypatch)
        proc = _LiveFakeProc([])

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["terminal_class"] == bm.TERMINAL_CLASS_WEDGE
        manifest = _read_manifest(tmp_path)
        assert manifest["terminal_class"] == bm.TERMINAL_CLASS_WEDGE
        assert "monitor" in manifest["terminal_class_evidence"]
        assert manifest["evidence"]["terminal_class"] == bm.TERMINAL_CLASS_WEDGE
        # The pre-lane fields keep their EXACT meanings beside the new one.
        assert manifest["timed_out"] is False, (
            "``timed_out`` still means 'the runner's own arm fired' — a wedge "
            "kill is not that, and nothing already reading it may shift"
        )
        assert manifest["wedged"] is True

    @pytest.mark.asyncio
    async def test_a_budget_cap_kill_is_classed_AND_still_arms_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The D659 gate is load-bearing: the new class must not displace it."""
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
        assert snap["terminal_class"] == bm.TERMINAL_CLASS_BUDGET_CAP
        assert snap["budget_cap_killed"] is True, (
            "TASK-GATE-D659 must still arm — the class rides BESIDE the "
            "cap-kill marker, never instead of it"
        )
        assert _read_manifest(tmp_path)["terminal_class"] == (
            bm.TERMINAL_CLASS_BUDGET_CAP
        )

    @pytest.mark.asyncio
    async def test_an_insanity_bound_expiry_is_classed_wall_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        proc = _LiveFakeProc([])

        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["terminal_class"] == bm.TERMINAL_CLASS_WALL_CLOCK
        assert "budget_cap_killed" not in snap
        manifest = _read_manifest(tmp_path)
        assert manifest["terminal_class"] == bm.TERMINAL_CLASS_WALL_CLOCK
        assert manifest["timed_out"] is True

    @pytest.mark.asyncio
    async def test_a_guardkit_in_band_timeout_is_no_longer_a_plain_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE ONE THAT USED TO BE INVISIBLE.

        No forge-side clock fires. guardkit's own task clock does, writes a
        TIMEOUT marker to the task's progress.log, and exits non-zero. Before
        this lane that was byte-indistinguishable from a compile error.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-003": "in_progress"}, tasks_completed=0
        )
        _append_progress_line(
            worktree,
            "TASK-BMW-003",
            "[2026-08-07T10:40:00] TIMEOUT TASK-BMW-003: elapsed=2400s",
        )
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        proc = _LiveFakeProc([])

        async def _exit_nonzero_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(2)

        finisher = asyncio.ensure_future(_exit_nonzero_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert snap["terminal_class"] == bm.TERMINAL_CLASS_IN_BAND
        # The prose the operator reads is UNCHANGED, byte for byte.
        assert snap["error_message"].startswith("guardkit autobuild exit=2")
        manifest = _read_manifest(tmp_path)
        assert manifest["terminal_class"] == bm.TERMINAL_CLASS_IN_BAND
        assert "TASK-BMW-003" in manifest["terminal_class_evidence"]
        assert manifest["timed_out"] is False, (
            "no FORGE-side clock fired; the flag keeps its exact old meaning"
        )

    @pytest.mark.asyncio
    async def test_an_events_jsonl_failure_category_also_proves_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-003": "in_progress"}, tasks_completed=0
        )
        events = worktree / ".guardkit" / "autobuild" / FEATURE_ID / "events.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(
            json.dumps(
                {"task_id": "TASK-BMW-003", "failure_category": "sdk_timeout"}
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        proc = _LiveFakeProc([])

        async def _exit_nonzero_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(1)

        finisher = asyncio.ensure_future(_exit_nonzero_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        assert result["async_tasks"][FEATURE_ID]["terminal_class"] == (
            bm.TERMINAL_CLASS_IN_BAND
        )


class TestOrdinaryFailuresAreBYTE_IDENTICAL:
    """THE CONTROL. ``error`` is never written — its absence IS its value.

    Every failure route this lane did not teach anything new must emit exactly
    the snapshot it emitted before. If this test ever needs relaxing, the lane
    stopped being additive.
    """

    @pytest.mark.asyncio
    async def test_a_plain_nonzero_exit_stamps_no_class_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed_worktree(
            worktree, statuses={"TASK-BMW-001": "in_progress"}, tasks_completed=0
        )
        monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
        proc = _LiveFakeProc([])

        async def _exit_nonzero_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(1)

        finisher = asyncio.ensure_future(_exit_nonzero_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert "terminal_class" not in snap, (
            "an ordinary broken build gets a byte-identical snapshot: the "
            "class vocabulary never writes 'error'"
        )
        assert snap["error_message"].startswith("guardkit autobuild exit=1")
        # The PACK, unlike the snapshot, always states it — a forensic record
        # read by a human is exactly where "no clock fired at all" is worth
        # writing down.
        manifest = _read_manifest(tmp_path)
        assert manifest["terminal_class"] == bm.TERMINAL_CLASS_ERROR

    @pytest.mark.asyncio
    async def test_a_healthy_build_snapshot_is_untouched(
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
        proc = _LiveFakeProc([])

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            proc.finish(0)

        finisher = asyncio.ensure_future(_finish_later())
        result = await _run_node(
            _launch_state(), proc=proc, worktree=worktree, repo=repo
        )
        await finisher

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "running_wave"
        assert "terminal_class" not in snap


class TestTheClassSurvivesTheFailedTerminal:
    """The fast-failure replay path — where the cap-kill marker was lost once."""

    def test_the_terminal_carries_the_class_forward(self) -> None:
        state = {
            "messages": _launch_state()["messages"],
            "async_tasks": {
                FEATURE_ID: {
                    "error_message": "guardkit autobuild exit=2",
                    "terminal_class": bm.TERMINAL_CLASS_IN_BAND,
                    "tasks_failed": 1,
                }
            },
        }
        refreshed = ar._node_failed(state)["async_tasks"][FEATURE_ID]  # type: ignore[arg-type]
        assert refreshed["terminal_class"] == bm.TERMINAL_CLASS_IN_BAND

    def test_an_unclassified_terminal_stays_unclassified(self) -> None:
        state = {
            "messages": _launch_state()["messages"],
            "async_tasks": {
                FEATURE_ID: {
                    "error_message": "guardkit autobuild exit=1",
                    "tasks_failed": 1,
                }
            },
        }
        refreshed = ar._node_failed(state)["async_tasks"][FEATURE_ID]  # type: ignore[arg-type]
        assert "terminal_class" not in refreshed
