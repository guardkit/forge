"""The process-group kill and the bounded post-kill read (LI stage 2, FB1).

Design of record: ``leg-invocation-stage2-design-2026-08-02`` §1, the
paragraph headed *"The kill fix rides in the same forge stage"*.

Two defects lived in :func:`forge.adapters.guardkit.run._execute_subprocess`
and both are latent at 600s and dangerous at 1800s:

1. **The spawn shared the daemon's process group**, so ``terminate()`` /
   ``kill()`` reached the ``guardkit`` child and nothing it had started. A
   work leg's test runner is a *grandchild*; on a timeout it survived, kept
   the CPU and — because it inherited the child's pipes — kept the read
   open.
2. **The post-kill ``communicate()`` was unbounded.** An orphaned
   pipe-holder means EOF never comes, so the await never returns, and the
   conductor's turn has no watchdog above it (``conductor_driver.py:357``
   is a bare await). A timeout that cannot time out is worse than no
   timeout at all.

The FB1 coach round found two more, both driven here:

3. **The SIGKILL escalation reached nothing at all.** The signaller read
   the group back with ``os.getpgid(pid)``; by escalation time the child
   is dead from the group SIGTERM *and reaped* by asyncio's child
   watcher, so that call raises ``ProcessLookupError`` and the signaller
   returned having sent no signal — not to the group, not to the child.
   The ladder's whole purpose is the grandchild that IGNORES SIGTERM, and
   that is exactly the case it silently skipped. The child leads its own
   group (``start_new_session=True``) and a group outlives its dead
   leader, so the pgid is the pid and must never be read back.
4. **The surrendered output was named only in the daemon log.** A leg
   that LOST 30 minutes of tails was byte-indistinguishable, in the
   stage_log row and the receipt, from a leg that produced none. The loss
   now rides the result as a ``post_kill_output_surrendered`` warning,
   and the dispatcher threads warnings verbatim into the rationale.

Every test here spawns **real** processes. Stubbing the seam would prove
nothing: the defect lives in how the spawn is grouped and how the kill is
signalled, which is exactly what a stub abstracts away.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from forge.adapters.guardkit import run as run_module
from forge.adapters.guardkit.context_resolver import ResolvedContext

# A child that spawns ONE grandchild and then sleeps forever. The
# grandchild inherits the child's stdout/stderr pipes (Popen's default),
# which is the whole point: it is a pipe holder that outlives its parent.
#
# ``argv[1]`` is the file the grandchild's pid is written to; ``argv[2]``
# selects the grandchild's shape:
#
# * ``escape``  — a session of its own, so the process-group kill CANNOT
#   reach it: the pipe holder the bounded read exists for.
# * ``ignore``  — the child's OWN group, but with SIGTERM ignored: it
#   survives the group SIGTERM and only the SIGKILL escalation can end
#   it. This is the case the escalation ladder exists for, and the case
#   the getpgid-first signaller silently skipped.
# * anything else — the child's own group, default SIGTERM handling.
_CHILD_WITH_GRANDCHILD = """
import os
import subprocess
import sys
import time

pid_file, mode = sys.argv[1], sys.argv[2]
if mode == "ignore":
    body = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(300)"
    )
else:
    body = "import time; time.sleep(300)"
grandchild = subprocess.Popen(
    [sys.executable, "-c", body],
    start_new_session=(mode == "escape"),
)
with open(pid_file, "w") as handle:
    handle.write(str(grandchild.pid))
    handle.flush()
    os.fsync(handle.fileno())
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    """Return ``True`` iff ``pid`` names a process we can still signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — not reachable as one uid
        return True
    return True


async def _await_death(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not _alive(pid)


def _reap(pid: int) -> None:
    """Best-effort cleanup so an escaped grandchild never outlives the run."""
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


async def _read_pid(pid_file: Path, *, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            raw = pid_file.read_text(encoding="utf-8").strip()
            if raw:
                return int(raw)
        await asyncio.sleep(0.05)
    raise AssertionError(f"the child never wrote its grandchild pid to {pid_file}")


class TestTheSpawnIsItsOwnSession:
    @pytest.mark.asyncio
    async def test_the_child_does_not_share_the_daemons_process_group(
        self, tmp_path: Path
    ) -> None:
        """``start_new_session=True`` — the precondition for a group kill.

        The child leads a session and a group of ITS OWN, so ``pgid ==
        pid`` by construction and the signaller passes the pid straight
        to :func:`os.killpg` — it never reads the group back (defect 3
        above: ``os.getpgid(pid)`` raises once the child is dead and
        reaped, and a getpgid-first signaller then sends nothing at all).
        Without the new session that identity is gone twice over: the
        child would sit in the FORGE DAEMON's group, so a group signal
        aimed at the daemon's pgid would take the daemon down with the
        leg, while the pid-as-pgid call this code actually makes would
        name a group nobody leads and signal nothing.
        """
        stdout, _stderr, exit_code, _duration, timed_out, surrendered = (
            await run_module._execute_subprocess(
                command=[
                    sys.executable,
                    "-c",
                    "import os; print(os.getpid(), os.getpgrp())",
                ],
                cwd=str(tmp_path),
                timeout=60,
            )
        )

        assert exit_code == 0
        assert timed_out is False
        assert surrendered is False
        child_pid, child_pgrp = (int(x) for x in stdout.split())
        # The docstring's load-bearing identity, proven not just implied:
        # the child LEADS its own group (pgid == pid), which is what lets the
        # signaller pass the pid straight to os.killpg with no read-back.
        assert child_pid == child_pgrp
        assert child_pgrp != os.getpgrp()

    @pytest.mark.asyncio
    async def test_the_ordinary_path_still_returns_output_and_exit_code(
        self, tmp_path: Path
    ) -> None:
        """The happy path is unmoved — no session change may cost output."""
        stdout, stderr, exit_code, _duration, timed_out, surrendered = (
            await run_module._execute_subprocess(
                command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('out'); "
                    "sys.stderr.write('err'); sys.exit(3)",
                ],
                cwd=str(tmp_path),
                timeout=60,
            )
        )

        assert (stdout, stderr, exit_code, timed_out, surrendered) == (
            "out",
            "err",
            3,
            False,
            False,
        )


class TestTheGroupKill:
    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_grandchild_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect, driven: a work leg's test runner is a grandchild."""
        monkeypatch.setattr(run_module, "_KILL_GRACE_SECONDS", 0.5)
        pid_file = tmp_path / "grandchild.pid"

        task = asyncio.ensure_future(
            run_module._execute_subprocess(
                command=[
                    sys.executable,
                    "-c",
                    _CHILD_WITH_GRANDCHILD,
                    str(pid_file),
                    "group",
                ],
                cwd=str(tmp_path),
                timeout=2,
            )
        )
        grandchild_pid = await _read_pid(pid_file)
        try:
            (
                _stdout,
                _stderr,
                _exit_code,
                _duration,
                timed_out,
                _surrendered,
            ) = await asyncio.wait_for(task, timeout=30)
            assert timed_out is True
            assert await _await_death(grandchild_pid), (
                f"grandchild {grandchild_pid} survived the timeout kill — the "
                "signal reached the child only, which is the defect"
            )
        finally:
            _reap(grandchild_pid)

    @pytest.mark.asyncio
    async def test_the_sigkill_escalation_reaches_a_sigterm_ignoring_grandchild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE escalation case — and the one no earlier test exercised.

        The sibling test above uses a grandchild that dies on SIGTERM, so
        the grace read succeeds and the SIGKILL branch never runs; the
        bounded-read test uses a ``setsid``-escaped grandchild that the
        group kill cannot reach by definition. Neither can see the FB1
        blocker: reading the group back with ``os.getpgid(pid)`` at
        escalation time raises ``ProcessLookupError`` (the child is dead
        AND reaped by then) and the signaller returns having sent
        nothing.

        Here the grandchild sits in the child's own group and IGNORES
        SIGTERM. It survives step 1 and holds the pipes open, so only a
        SIGKILL that actually reaches the group ends the dispatch. The
        timings are the proof: returning well inside the post-kill bound
        can only mean EOF arrived, and EOF can only mean the grandchild
        died.
        """
        monkeypatch.setattr(run_module, "_KILL_GRACE_SECONDS", 0.5)
        pid_file = tmp_path / "grandchild.pid"

        task = asyncio.ensure_future(
            run_module._execute_subprocess(
                command=[
                    sys.executable,
                    "-c",
                    _CHILD_WITH_GRANDCHILD,
                    str(pid_file),
                    "ignore",
                ],
                cwd=str(tmp_path),
                timeout=2,
            )
        )
        grandchild_pid = await _read_pid(pid_file)
        try:
            started = time.monotonic()
            (
                _stdout,
                _stderr,
                _exit_code,
                _duration,
                timed_out,
                surrendered,
            ) = await asyncio.wait_for(task, timeout=60)
            elapsed = time.monotonic() - started

            assert timed_out is True
            assert await _await_death(grandchild_pid), (
                f"grandchild {grandchild_pid} ignored SIGTERM and SURVIVED — "
                "the SIGKILL escalation signalled nothing at all, so a work "
                "leg's test runner outlives its own timeout and burns the "
                "seat for the rest of the journey"
            )
            # The escalation landing means EOF, which means no surrender.
            # Without it the orphan holds the pipe for its full 300s sleep
            # and the dispatch pays the whole 10s bound instead.
            assert surrendered is False
            assert elapsed < run_module._POST_KILL_READ_SECONDS, (
                f"the dispatch took {elapsed:.2f}s — that is the post-kill "
                "bound being paid, i.e. the pipe holder was never killed"
            )
        finally:
            _reap(grandchild_pid)


class TestTheBoundedPostKillRead:
    @pytest.mark.asyncio
    async def test_an_unreachable_pipe_holder_cannot_hang_the_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """EOF never comes; the read must end anyway, and say what it lost.

        The grandchild here puts itself in a session of its own, so the
        group kill cannot reach it and it holds the child's pipes open for
        five minutes. Unbounded, this await never returns.
        """
        # Only the SIGTERM grace is shortened. The post-kill bound itself
        # is left at its production value on purpose: the thing under test
        # is that a bound exists at all, and monkeypatching the constant
        # would make the test pass trivially on a build that has none.
        monkeypatch.setattr(run_module, "_KILL_GRACE_SECONDS", 0.5)
        pid_file = tmp_path / "grandchild.pid"

        task = asyncio.ensure_future(
            run_module._execute_subprocess(
                command=[
                    sys.executable,
                    "-c",
                    _CHILD_WITH_GRANDCHILD,
                    str(pid_file),
                    "escape",
                ],
                cwd=str(tmp_path),
                timeout=2,
            )
        )
        grandchild_pid = await _read_pid(pid_file)
        try:
            with caplog.at_level("WARNING"):
                started = time.monotonic()
                (
                    stdout,
                    stderr,
                    _exit_code,
                    _duration,
                    timed_out,
                    surrendered,
                ) = await asyncio.wait_for(task, timeout=60)
                elapsed = time.monotonic() - started

            assert timed_out is True
            # Degraded honestly: empty tails, not a hang.
            assert stdout == ""
            assert stderr == ""
            # And the seam SAYS it degraded — the flag run() turns into a
            # warning on the result. Empty-because-lost must never read as
            # empty-because-silent.
            assert surrendered is True
            # The escaped pipe holder sleeps for 300s. Anything under that
            # can only be the bound firing.
            assert elapsed < run_module._POST_KILL_READ_SECONDS + 10
            assert any(
                "post-kill" in record.getMessage() for record in caplog.records
            ), "the lost output must be NAMED, never silently dropped"
        finally:
            _reap(grandchild_pid)


class TestTheSurrenderedOutputIsNamedOnTheResult:
    """The daemon log is NOT the only channel — the FB1 spec deviation.

    ``GuardKitResult.warnings`` are threaded verbatim into the dispatch
    rationale (``pipeline/dispatchers/subprocess.py``, the
    ``warning[{code}]: {message}`` line), so a warning here is what puts
    the loss in the stage_log row and the receipt. Without it a 1800s
    work leg that LOST its tails is byte-indistinguishable from a leg
    that produced no output at all — the silent failure mode M5 exists to
    close.

    This is a REAL drive of ``run()``: a real spawn of a real fake
    ``guardkit`` binary that leaks a session-escaped pipe holder.
    """

    @pytest.mark.asyncio
    async def test_run_carries_a_post_kill_output_surrendered_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "build"
        worktree.mkdir()
        pid_file = tmp_path / "grandchild.pid"

        # The fake binary: a shell shim that execs the same
        # grandchild-leaking child every other test in this module uses.
        helper = tmp_path / "leaky_child.py"
        helper.write_text(_CHILD_WITH_GRANDCHILD, encoding="utf-8")
        fake_binary = tmp_path / "bin" / "guardkit"
        fake_binary.parent.mkdir(parents=True, exist_ok=True)
        fake_binary.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{helper}" "{pid_file}" escape\n',
            encoding="utf-8",
        )
        fake_binary.chmod(0o755)

        monkeypatch.setattr(run_module, "_resolved_guardkit_binary", None)
        monkeypatch.setenv(run_module.GUARDKIT_PATH_ENV, str(fake_binary))
        monkeypatch.setattr(
            run_module,
            "resolve_context_flags",
            lambda *a, **kw: ResolvedContext(flags=[], paths=[], warnings=[]),
        )
        monkeypatch.setattr(run_module, "_KILL_GRACE_SECONDS", 0.5)
        # The BOUND's existence is proven by the sibling test at its
        # production value; here the subject is what the surrender says,
        # so the window is shortened to keep the drive quick.
        monkeypatch.setattr(run_module, "_POST_KILL_READ_SECONDS", 2.0)

        task = asyncio.ensure_future(
            run_module.run(
                subcommand="task-work",
                args=["--task-id", "TASK-FB1-001"],
                repo_path=worktree,
                read_allowlist=[tmp_path],
                timeout_seconds=2,
                with_nats_streaming=False,
            )
        )
        grandchild_pid = await _read_pid(pid_file)
        try:
            result = await asyncio.wait_for(task, timeout=60)
        finally:
            _reap(grandchild_pid)

        assert result.status == "timeout"
        assert result.stdout_tail == ""
        codes = [w.code for w in result.warnings]
        assert "post_kill_output_surrendered" in codes, (
            "the surrendered tails were named only in the daemon log — the "
            f"result carries {codes!r}, so the stage rationale reads exactly "
            "like a leg that produced no output"
        )
        surrender = next(
            w for w in result.warnings if w.code == "post_kill_output_surrendered"
        )
        assert "SURRENDERED" in surrender.message
        assert surrender.details["subcommand"] == "task-work"
