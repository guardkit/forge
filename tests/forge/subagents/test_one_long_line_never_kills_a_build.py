"""One long line of build output must never kill a build (19 October 2026).

Build FEAT-B874 died because the runner read GuardKit's output with
``readline()``, which raises ``ValueError('Separator is not found, and chunk
exceed the limit')`` on a line over asyncio's 64 KiB limit. The error left
the ``running_wave`` node, the run ended with no terminal, no failure pack,
and the GuardKit child still running.

These tests prove, through the runner's real reading code and real child
processes:

* the reader (:class:`forge.subagents.output_lines.OutputLines`) keeps every
  normal line exactly, and cuts an over-long one with a plain marker;
* a child printing a 5 MB single line, then normal lines, then exiting 0 or 3,
  runs through ``_node_running_wave`` to the right outcome, with the normal
  lines in the stdout log and the long one marked cut;
* an error of the runner's own while reading a build stops every process the
  build owns and fails the build with a readable failure pack, instead of
  crashing the node.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.pipeline.fix_journey_receipts import read_failure_pack
from forge.subagents import autobuild_runner as ar
from forge.subagents import build_monitor as bm
from forge.subagents.output_lines import (
    OUTPUT_LINE_CAP_BYTES,
    OutputLines,
    cut_marker,
)

FEATURE_ID = "FEAT-LONGLINE"
BUILD_ID = "build-FEAT-LONGLINE-1"
LONG_LINE_BYTES = 5 * 1024 * 1024

_REAL_EXEC = asyncio.create_subprocess_exec


# ---------------------------------------------------------------------------
# The reader on its own
# ---------------------------------------------------------------------------


class _Chunks:
    """A stream that hands out fixed chunks, then ``b""`` for ever."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


async def _all_lines(reader: OutputLines) -> list[str]:
    lines: list[str] = []
    while (line := await reader.next_line()) is not None:
        lines.append(line)
    return lines


class TestTheReader:
    @pytest.mark.asyncio
    async def test_normal_lines_arrive_exactly_as_readline_gave_them(self) -> None:
        reader = OutputLines(
            _Chunks([b"first\nsec", b"ond  \r\n\n", b"\xe2\x96\xb6 third\nlast"])
        )
        assert await _all_lines(reader) == ["first", "second", "", "\u25b6 third", "last"]
        assert reader.lines_cut == 0

    @pytest.mark.asyncio
    async def test_a_long_line_is_cut_and_the_next_line_is_whole(self) -> None:
        cap = 10
        stream = _Chunks([b"0123456789ABC", b"DEFGH", b"IJ\nnext line\n"])
        reader = OutputLines(stream, cap=cap, chunk=4)
        lines = await _all_lines(reader)
        assert lines == ["0123456789" + cut_marker(10), "next line"]
        assert reader.lines_cut == 1

    @pytest.mark.asyncio
    async def test_a_long_line_inside_one_chunk_is_cut_too(self) -> None:
        reader = OutputLines(_Chunks([b"x" * 50 + b"\nok\n"]), cap=8)
        assert await _all_lines(reader) == ["x" * 8 + cut_marker(42), "ok"]

    @pytest.mark.asyncio
    async def test_a_line_exactly_at_the_cap_is_kept_whole(self) -> None:
        reader = OutputLines(_Chunks([b"y" * 8 + b"\n"]), cap=8, chunk=3)
        assert await _all_lines(reader) == ["y" * 8]

    @pytest.mark.asyncio
    async def test_a_long_last_line_without_a_newline_is_cut(self) -> None:
        reader = OutputLines(_Chunks([b"z" * 30]), cap=8, chunk=7)
        assert await _all_lines(reader) == ["z" * 8 + cut_marker(22)]

    @pytest.mark.asyncio
    async def test_the_stream_is_not_read_again_after_its_end(self) -> None:
        stream = _Chunks([b"a\n"])
        reader = OutputLines(stream)
        assert await _all_lines(reader) == ["a"]
        reads = stream.reads
        assert await reader.next_line() is None
        assert stream.reads == reads

    @pytest.mark.asyncio
    async def test_a_cancel_while_waiting_loses_no_bytes(self) -> None:
        gate = asyncio.Event()

        class _Slow:
            def __init__(self) -> None:
                self.parts = [b"half a ", None, b"line\n"]

            async def read(self, n: int = -1) -> bytes:
                part = self.parts.pop(0) if self.parts else b""
                if part is None:
                    await gate.wait()
                    return b""
                return part

        reader = OutputLines(_Slow())
        task = asyncio.ensure_future(reader.next_line())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await reader.next_line() == "half a line"

    @pytest.mark.asyncio
    async def test_a_real_child_printing_5_mb_on_one_line(self) -> None:
        proc = await _REAL_EXEC(
            sys.executable,
            "-c",
            _child_script(exit_code=0),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        lines = await asyncio.wait_for(_all_lines(OutputLines(proc.stdout)), 60)
        assert await asyncio.wait_for(proc.wait(), 30) == 0
        assert lines[0] == "before the long line"
        assert lines[1] == "L" * OUTPUT_LINE_CAP_BYTES + cut_marker(
            LONG_LINE_BYTES - OUTPUT_LINE_CAP_BYTES
        )
        assert lines[2:] == _NORMAL_LINES


# ---------------------------------------------------------------------------
# Through the runner's real reading code
# ---------------------------------------------------------------------------

_NORMAL_LINES = [
    "INFO:guardkit.orchestrator.progress:[t] Completed turn 1: success - done",
    "after the long line",
    "the last line",
]


def _child_script(*, exit_code: int) -> str:
    return (
        "import sys\n"
        "out = sys.stdout\n"
        "out.write('before the long line\\n')\n"
        f"out.write('L' * {LONG_LINE_BYTES} + '\\n')\n"
        + "".join(f"out.write({line!r} + '\\n')\n" for line in _NORMAL_LINES)
        + "out.flush()\n"
        f"sys.exit({exit_code})\n"
    )


def _launch_state() -> dict[str, Any]:
    payload = {
        "build_id": BUILD_ID,
        "feature_id": FEATURE_ID,
        "repo": "appmilla/api_test",
        "branch": "main",
        "correlation_id": "corr-longline-1",
    }
    return {
        "messages": [
            HumanMessage(
                content="RUN_AUTOBUILD subagent=autobuild_runner "
                f"payload={json.dumps(payload, sort_keys=True)}"
            )
        ]
    }


@pytest.fixture(autouse=True)
def _local_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    receipts = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))
    monkeypatch.setenv(bm.BUILD_MONITOR_POLL_ENV, "0.05")
    monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(bm.BUILD_MONITOR_ENABLED_ENV, raising=False)
    return receipts


async def _run_node_with_real_child(
    tmp_path: Path, script: str, *, spawned: list[Any] | None = None
) -> dict[str, Any]:
    """Drive ``_node_running_wave`` with a REAL child in place of GuardKit.

    Every other boundary (repository, branch, worktree) is a tmp folder; the
    child is started with exactly the keyword arguments the runner passes.
    """
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    worktree = tmp_path / "worktrees" / BUILD_ID
    worktree.mkdir(parents=True, exist_ok=True)

    async def _exec(*_argv: Any, **kwargs: Any) -> Any:
        proc = await _REAL_EXEC(sys.executable, "-c", script, **kwargs)
        if spawned is not None:
            spawned.append(proc)
        return proc

    async def _nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _yes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _materialise(*_args: Any, **_kwargs: Any) -> Path:
        return worktree

    with patch.object(ar, "_resolve_repo_path", lambda payload: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: Path(sys.executable)
    ), patch.object(ar, "_local_branch_exists", _yes), patch.object(
        ar, "_sweep_build_refs", _nothing
    ), patch.object(ar, "_materialise_worktree", _materialise), patch.object(
        ar, "_remove_worktree", _nothing
    ), patch.object(asyncio, "create_subprocess_exec", _exec):
        return await asyncio.wait_for(
            ar._node_running_wave(_launch_state()),  # type: ignore[arg-type]
            timeout=120,
        )


def _stdout_log(receipts: Path) -> str:
    logs = list(receipts.rglob(ar.STDOUT_LOG_NAME))
    assert len(logs) == 1, logs
    return logs[0].read_text(encoding="utf-8")


class TestThroughTheRunner:
    @pytest.mark.asyncio
    async def test_a_5_mb_line_then_normal_lines_then_exit_0_succeeds(
        self, tmp_path: Path, _local_receipts: Path
    ) -> None:
        result = await _run_node_with_real_child(
            tmp_path, _child_script(exit_code=0)
        )

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "running_wave", snap
        # The decision line after the long one was parsed.
        assert snap["last_coach_score"] == 1.0
        log = _stdout_log(_local_receipts)
        assert "before the long line\n" in log
        for line in _NORMAL_LINES:
            assert f"{line}\n" in log
        cut = "L" * OUTPUT_LINE_CAP_BYTES + cut_marker(
            LONG_LINE_BYTES - OUTPUT_LINE_CAP_BYTES
        )
        assert f"{cut}\n" in log
        assert len(log) < 2 * OUTPUT_LINE_CAP_BYTES

    @pytest.mark.asyncio
    async def test_the_same_output_then_exit_3_fails_with_its_exit_code(
        self, tmp_path: Path, _local_receipts: Path
    ) -> None:
        result = await _run_node_with_real_child(
            tmp_path, _child_script(exit_code=3)
        )

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert "guardkit autobuild exit=3" in snap["error_message"]
        log = _stdout_log(_local_receipts)
        assert "the last line\n" in log
        assert cut_marker(LONG_LINE_BYTES - OUTPUT_LINE_CAP_BYTES) in log


class TestARunnerErrorFailsTheBuildHonestly:
    @pytest.mark.asyncio
    async def test_a_reading_error_stops_the_child_and_leaves_a_readable_pack(
        self,
        tmp_path: Path,
        _local_receipts: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The FEAT-B874 error, raised by the reader on its second line, while
        # the child would otherwise keep running for a minute.
        class _Breaks(OutputLines):
            calls = 0

            async def next_line(self) -> str | None:
                type(self).calls += 1
                if type(self).calls == 2:
                    raise ValueError(
                        "Separator is not found, and chunk exceed the limit "
                        "password=hunter2"
                    )
                return await super().next_line()

        monkeypatch.setattr(ar, "OutputLines", _Breaks)
        spawned: list[Any] = []
        script = (
            "import sys, time\n"
            "print('working on TASK-DEP-003', flush=True)\n"
            "print('more', flush=True)\n"
            "time.sleep(60)\n"
        )

        result = await _run_node_with_real_child(tmp_path, script, spawned=spawned)

        snap = result["async_tasks"][FEATURE_ID]
        assert snap["lifecycle"] == "failed"
        assert "stopped the build after an error of its own" in snap["error_message"]
        assert "ValueError" in snap["error_message"]
        assert "hunter2" not in snap["error_message"]
        # Every process the build owned is gone.
        (child,) = spawned
        assert child.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)

        # The pack the FAILED path writes, readable by the repair.
        pack = read_failure_pack(BUILD_ID, receipts_root=_local_receipts)
        assert pack is not None and pack.has_manifest
        assert pack.raw["runner_error"].startswith("ValueError: Separator is not found")
        assert "hunter2" not in pack.raw["runner_error"]
        assert pack.evidence is not None
        assert pack.evidence["subprocess_ran"] is True
        assert pack.evidence["worktree_kept"] is True
        assert pack.worktree_path == str(tmp_path / "worktrees" / BUILD_ID)
        assert pack.terminal == ar.TERMINAL_RUNNING_WAVE
        assert snap.get("failure_pack")
