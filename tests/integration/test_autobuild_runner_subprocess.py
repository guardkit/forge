"""Integration tests for the autobuild_runner guardkit subprocess wiring.

TASK-ABW-001 — covers the two acceptance-criteria tests called out in
§Scope item 6:

* ``test_running_wave_invokes_guardkit_and_completes_on_zero_exit`` —
  asserts the subprocess argv shape, that exit code 0 lands the runner
  on ``completed``, and that a stage_complete-shaped snapshot is visible
  in the values stream mid-flight.
* ``test_running_wave_transitions_to_failed_on_nonzero_exit`` — asserts
  exit code 1 lands the runner on ``failed`` with ``tasks_failed == 1``.

The tests monkey-patch :func:`_resolve_repo_path`, :func:`_resolve_guardkit_path`
and :func:`asyncio.create_subprocess_exec` at the module surface so they
exercise the subagent without requiring a real guardkit install or a
clone of the demo repo (per TASK-ABW-001 §Implementation notes).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.subagents import autobuild_runner as ar_mod
from forge.subagents.autobuild_runner import _build_runner_graph


class _FakeStdout:
    """Async-iterable stdout double yielding canned bytes lines."""

    def __init__(self, lines: list[bytes]) -> None:
        # Append EOF sentinel — ``readline`` returns ``b""`` on EOF.
        self._lines: list[bytes] = [*lines, b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Minimal ``asyncio.subprocess.Process`` double.

    Captures the argv + kwargs handed to
    :func:`asyncio.create_subprocess_exec` so the AC-bound argv shape can
    be asserted post-run.
    """

    captured_args: tuple[Any, ...] = ()
    captured_kwargs: dict[str, Any] = {}

    def __init__(self, *, exit_code: int, stdout_lines: list[bytes]) -> None:
        self.returncode: int | None = exit_code
        self.pid = 4242
        self.stdout = _FakeStdout(stdout_lines)
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code

    def kill(self) -> None:  # pragma: no cover — exit happens before kill
        return None


def _make_fake_subprocess(*, exit_code: int, stdout_lines: list[bytes]):
    """Build a fake ``create_subprocess_exec`` recording call args."""
    captured: dict[str, Any] = {"args": (), "kwargs": {}}

    async def _fake(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc(exit_code=exit_code, stdout_lines=stdout_lines)

    return _fake, captured


def _launch_description(*, feature_id: str, build_id: str, repo: str) -> str:
    return (
        "RUN_AUTOBUILD subagent=autobuild_runner "
        'payload={"build_id": "' + build_id + '", '
        '"feature_id": "' + feature_id + '", '
        '"repo": "' + repo + '", '
        '"correlation_id": "corr-int-001"}'
    )


# ---------------------------------------------------------------------------
# AC: exit 0 → completed; stage_complete snapshot visible mid-stream
# ---------------------------------------------------------------------------


class TestRunningWaveSubprocessSuccess:
    """``_node_running_wave`` completes the graph on guardkit exit code 0."""

    def test_running_wave_invokes_guardkit_and_completes_on_zero_exit(
        self,
    ) -> None:
        """Exit code 0 lands the runner on ``completed``.

        Asserts:

        1. ``asyncio.create_subprocess_exec`` was called with argv
           ``[guardkit_path, "autobuild", "feature", feature_id,
           "--fresh", "--verbose"]`` and ``cwd=resolved_repo_path``.
        2. The final ``async_tasks[feature_id].lifecycle == "completed"``.
        3. At least one stage_complete-shaped snapshot was visible mid-stream
           (a ``running_wave`` snapshot whose ``tasks_completed >= 1``).
        """
        fake_repo = Path("/tmp/fake-api_test")
        fake_guardkit = Path("/usr/local/bin/guardkit-fake")
        feature_id = "FEAT-INT-OK"

        fake_exec, captured = _make_fake_subprocess(
            exit_code=0,
            stdout_lines=[
                b"== guardkit autobuild start ==\n",
                b"[guardkit-checkpoint] Turn 1 complete (tests: pass)\n",
                b"== guardkit autobuild end ==\n",
            ],
        )

        async def _drive() -> dict[str, Any]:
            stage_complete_seen: list[dict[str, Any]] = []

            with patch.object(
                ar_mod, "_resolve_repo_path", lambda payload: fake_repo
            ), patch.object(
                ar_mod, "_resolve_guardkit_path", lambda: fake_guardkit
            ), patch.object(
                asyncio, "create_subprocess_exec", fake_exec
            ):
                graph = _build_runner_graph()
                terminal: dict[str, Any] = {}
                async for chunk in graph.astream(
                    {
                        "messages": [
                            HumanMessage(
                                content=_launch_description(
                                    feature_id=feature_id,
                                    build_id="build-FEAT-INT-OK-1",
                                    repo="appmilla/api_test",
                                )
                            )
                        ]
                    },
                    stream_mode="values",
                ):
                    if not isinstance(chunk, dict):
                        continue
                    terminal = chunk
                    ats = chunk.get("async_tasks") or {}
                    snap = (
                        ats.get(feature_id) if isinstance(ats, dict) else None
                    )
                    if (
                        isinstance(snap, dict)
                        and snap.get("lifecycle") == "running_wave"
                        and int(snap.get("tasks_completed") or 0) >= 1
                    ):
                        stage_complete_seen.append(snap)

            return {"terminal": terminal, "stage_complete": stage_complete_seen}

        result = asyncio.run(_drive())

        # --- argv shape (AC: guardkit_path autobuild feature <feature_id>
        # --fresh --verbose) and cwd=resolved_repo_path ----------------------
        argv = captured["args"]
        assert argv[0] == str(fake_guardkit), (
            f"first positional must be guardkit path, got {argv[0]!r}"
        )
        assert argv[1:6] == (
            "autobuild",
            "feature",
            feature_id,
            "--fresh",
            "--verbose",
        ), f"unexpected argv tail: {argv[1:]!r}"
        assert captured["kwargs"].get("cwd") == str(fake_repo), (
            "cwd must be the resolved repo path; "
            f"got {captured['kwargs'].get('cwd')!r}"
        )

        # --- final lifecycle -------------------------------------------------
        terminal = result["terminal"]
        snap = terminal["async_tasks"][feature_id]
        assert snap["lifecycle"] == "completed", (
            f"exit code 0 must land on completed; got {snap['lifecycle']!r}"
        )

        # --- stage_complete-shaped snapshot mid-stream -----------------------
        assert result["stage_complete"], (
            "at least one running_wave snapshot with tasks_completed >= 1 "
            "must be visible in the values stream between running_wave and "
            "completed (stage_complete fallback)"
        )


# ---------------------------------------------------------------------------
# AC: exit non-zero → failed + tasks_failed == 1
# ---------------------------------------------------------------------------


class TestRunningWaveSubprocessFailure:
    """``_node_running_wave`` routes to ``_node_failed`` on non-zero exit."""

    def test_running_wave_transitions_to_failed_on_nonzero_exit(self) -> None:
        """Exit code 1 lands the runner on ``failed`` with ``tasks_failed == 1``."""
        fake_repo = Path("/tmp/fake-api_test")
        fake_guardkit = Path("/usr/local/bin/guardkit-fake")
        feature_id = "FEAT-INT-FAIL"

        fake_exec, _captured = _make_fake_subprocess(
            exit_code=1,
            stdout_lines=[
                b"== guardkit autobuild start ==\n",
                b"error: tests failed\n",
            ],
        )

        async def _drive() -> dict[str, Any]:
            with patch.object(
                ar_mod, "_resolve_repo_path", lambda payload: fake_repo
            ), patch.object(
                ar_mod, "_resolve_guardkit_path", lambda: fake_guardkit
            ), patch.object(
                asyncio, "create_subprocess_exec", fake_exec
            ):
                graph = _build_runner_graph()
                result = await graph.ainvoke(
                    {
                        "messages": [
                            HumanMessage(
                                content=_launch_description(
                                    feature_id=feature_id,
                                    build_id="build-FEAT-INT-FAIL-1",
                                    repo="appmilla/api_test",
                                )
                            )
                        ]
                    }
                )
            return result

        result = asyncio.run(_drive())
        snap = result["async_tasks"][feature_id]
        assert snap["lifecycle"] == "failed", (
            f"non-zero exit must land on failed; got {snap['lifecycle']!r}"
        )
        assert snap["tasks_failed"] == 1, (
            f"failed snapshot must carry tasks_failed=1; got {snap['tasks_failed']!r}"
        )


# ---------------------------------------------------------------------------
# Defensive: missing repo / missing guardkit / timeout — all → failed
# ---------------------------------------------------------------------------


class TestRunningWaveResolutionFailures:
    """Resolver-level failures route to ``_node_failed`` without spawning."""

    def test_missing_repo_in_payload_transitions_to_failed(self) -> None:
        """A launch payload without ``repo`` shortcircuits to ``failed``."""

        async def _drive() -> dict[str, Any]:
            graph = _build_runner_graph()
            # No ``repo`` key — the resolver short-circuits before
            # _resolve_repo_path even runs.
            description = (
                "RUN_AUTOBUILD subagent=autobuild_runner "
                'payload={"build_id": "build-X", '
                '"feature_id": "FEAT-NOREPO", '
                '"correlation_id": "corr-X"}'
            )
            return await graph.ainvoke(
                {"messages": [HumanMessage(content=description)]}
            )

        result = asyncio.run(_drive())
        snap = result["async_tasks"]["FEAT-NOREPO"]
        assert snap["lifecycle"] == "failed"
        assert snap["tasks_failed"] == 1

    def test_guardkit_path_missing_transitions_to_failed(self) -> None:
        """Missing guardkit binary lands the runner on ``failed``."""
        fake_repo = Path("/tmp/fake-api_test")

        async def _drive() -> dict[str, Any]:
            with patch.object(
                ar_mod, "_resolve_repo_path", lambda payload: fake_repo
            ), patch.object(
                ar_mod, "_resolve_guardkit_path", lambda: None
            ):
                graph = _build_runner_graph()
                return await graph.ainvoke(
                    {
                        "messages": [
                            HumanMessage(
                                content=_launch_description(
                                    feature_id="FEAT-NOGK",
                                    build_id="build-FEAT-NOGK-1",
                                    repo="appmilla/api_test",
                                )
                            )
                        ]
                    }
                )

        result = asyncio.run(_drive())
        snap = result["async_tasks"]["FEAT-NOGK"]
        assert snap["lifecycle"] == "failed"

    def test_resolved_repo_outside_allowlist_transitions_to_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The resolver returns ``None`` when the path is not under the allowlist."""
        # Build a fake repo NOT under FORGE_REPO_BASE; the resolver
        # convention forces the resolved candidate inside FORGE_REPO_BASE,
        # so a path mismatch surfaces as ``None``.
        bad_base = tmp_path / "elsewhere"
        bad_base.mkdir()
        monkeypatch.setenv(ar_mod.FORGE_REPO_BASE_ENV, str(bad_base))
        # The resolved candidate would be <bad_base>/api_test, which does
        # not exist on disk → resolver returns None.
        result = ar_mod._resolve_repo_path({"repo": "appmilla/api_test"})
        assert result is None


# ---------------------------------------------------------------------------
# AC: timeout → killed + failed
# ---------------------------------------------------------------------------


class TestRunningWaveSubprocessTimeout:
    """Subprocess timeout → kill + ``failed`` transition."""

    def test_timeout_kills_subprocess_and_transitions_to_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A subprocess that exceeds the timeout is killed and lands on failed."""
        fake_repo = Path("/tmp/fake-api_test")
        fake_guardkit = Path("/usr/local/bin/guardkit-fake")
        feature_id = "FEAT-INT-TIMEOUT"

        kill_called: list[bool] = []

        class _HangStdout:
            async def readline(self) -> bytes:
                # Block forever — the runner's wait_for triggers the kill.
                await asyncio.sleep(60)
                return b""

        class _HangProc:
            returncode = None  # populated after kill()
            pid = 9999
            stdout = _HangStdout()

            async def wait(self) -> int:
                await asyncio.sleep(60)
                return 137

            def kill(self) -> None:
                kill_called.append(True)
                self.returncode = -9

        async def _fake_exec(*args: Any, **kwargs: Any) -> _HangProc:
            return _HangProc()

        # Force a very short timeout so the test runs quickly.
        monkeypatch.setenv(ar_mod.FORGE_AUTOBUILD_TIMEOUT_ENV, "0.05")

        async def _drive() -> dict[str, Any]:
            with patch.object(
                ar_mod, "_resolve_repo_path", lambda payload: fake_repo
            ), patch.object(
                ar_mod, "_resolve_guardkit_path", lambda: fake_guardkit
            ), patch.object(
                asyncio, "create_subprocess_exec", _fake_exec
            ):
                graph = _build_runner_graph()
                return await graph.ainvoke(
                    {
                        "messages": [
                            HumanMessage(
                                content=_launch_description(
                                    feature_id=feature_id,
                                    build_id="build-FEAT-INT-TIMEOUT-1",
                                    repo="appmilla/api_test",
                                )
                            )
                        ]
                    }
                )

        result = asyncio.run(_drive())
        snap = result["async_tasks"][feature_id]
        assert snap["lifecycle"] == "failed"
        assert kill_called == [True], (
            "subprocess.kill() must be called when the timeout expires"
        )
