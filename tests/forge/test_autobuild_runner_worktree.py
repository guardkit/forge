"""Branch-aware isolated-worktree + loud-no-op tests (B4 round-17 stretch).

These cover the two live-caught defects from the first gate-approved
autobuild launch (build ``build-FEAT-A058-20260715124214``):

* DEFECT #19 — BRANCH-BLIND BUILDS. The runner ran ``guardkit autobuild``
  with ``cwd`` = the SHARED repo checkout AS-IS, ignoring the branch the
  dispatch was scoped to, so a build targeted the wrong tree. The runner now
  materialises an ISOLATED git worktree of ``payload["branch"]`` and runs the
  subprocess there, never touching the shared checkout — cleaning it up on
  success, KEEPING it (named in the failure event) on failure.
* DEFECT #18b — SILENT NO-OP. A runner run that ends without a terminal
  lifecycle must FAIL LOUD, never end ``success`` silently. The ``finalize``
  guard node makes that structural.

Test strategy (per the task's non-negotiables): a THROWAWAY git repo fixture
with a planning-style branch stands in for the live api_test checkout; only the
guardkit subprocess is stubbed. Real ``git worktree`` verbs run against the
throwaway repo so the isolation is exercised end-to-end. The live api_test
checkout is NEVER touched and no real build is ever run.

Wire-true replay: ``tests/forge/fixtures/round17_launch_message_019f65d2.txt``
holds the EXACT launch message bytes retrieved from the running sidecar
(``GET /threads/019f65d2-.../state``). That payload carries NO ``branch`` key,
so it exercises the legacy shared-checkout path (DEFECT #19's byte-compatible
fallback) and the #18b terminal-state guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.subagents import autobuild_runner as ar

FIXTURE = (
    Path(__file__).parent / "fixtures" / "round17_launch_message_019f65d2.txt"
)

ROUND17_BUILD_ID = "build-FEAT-A058-20260715124214"
ROUND17_FEATURE_ID = "FEAT-A058"
ROUND17_CORR = "a2a4dcd8-2d35-479e-9a11-da4d6641a016"
PLANNING_BRANCH = f"planning/{ROUND17_CORR}"


# ---------------------------------------------------------------------------
# Throwaway git repo + guardkit stub
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def throwaway_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose HEAD sits on ``main`` with a separate planning branch.

    Mirrors the live shape: the shared checkout is on some *other* lane's
    branch (here ``main``) while the machine-made artifacts live on the
    ``planning/<corr>`` branch — which is NOT checked out in the main tree, so
    ``git worktree add`` of it is legal.
    """
    repo = tmp_path / "api_test"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "feature.yaml").write_text("id: FEAT-A058\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    # Planning branch at the same commit; main stays checked out.
    _git(repo, "branch", PLANNING_BRANCH)
    return repo


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = [*lines, b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, exit_code: int, lines: list[bytes]) -> None:
        self.pid = 4242
        self.returncode = exit_code
        self.stdout = _FakeStdout(lines)

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


def _make_exec_stub(recorded: dict[str, Any], *, exit_code: int, lines: list[bytes]):
    """Return a create_subprocess_exec stub: fake guardkit, REAL git.

    Dispatches on ``argv[0]``: a guardkit invocation returns a
    :class:`_FakeProc` (recording the ``cwd`` it was launched in); anything
    else (git ``rev-parse`` / ``worktree`` verbs) delegates to the real
    ``asyncio.create_subprocess_exec`` so the worktree machinery is exercised
    for real against the throwaway repo.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            recorded["cwd"] = kwargs.get("cwd")
            recorded["argv"] = list(args)
            return _FakeProc(exit_code, lines)
        return await real_exec(*args, **kwargs)

    return _stub


def _launch(payload_json: str) -> str:
    return f"RUN_AUTOBUILD subagent=autobuild_runner payload={payload_json}"


def _invoke(description: str, repo: Path, *, exit_code: int, recorded: dict[str, Any]):
    stub = _make_exec_stub(
        recorded, exit_code=exit_code, lines=[b"guardkit running\n"]
    )
    with patch.object(ar, "_resolve_repo_path", lambda payload: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit")
    ), patch.object(asyncio, "create_subprocess_exec", stub):
        graph = ar._build_runner_graph()
        return asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=description)]})
        )


def _lifecycle(result: dict[str, Any], feature_id: str) -> str | None:
    ats = result.get("async_tasks") or {}
    snap = ats.get(feature_id)
    return snap.get("lifecycle") if isinstance(snap, dict) else None


# ---------------------------------------------------------------------------
# Fixture is the literal round-17 bytes
# ---------------------------------------------------------------------------


def test_round17_fixture_is_literal_and_branch_blind() -> None:
    """The committed fixture is the exact sidecar bytes — and carries no branch."""
    content = FIXTURE.read_text()
    assert content.startswith("RUN_AUTOBUILD subagent=autobuild_runner payload=")
    payload = ar._extract_launch_payload([HumanMessage(content=content)])
    assert payload["build_id"] == ROUND17_BUILD_ID
    assert payload["feature_id"] == ROUND17_FEATURE_ID
    assert payload["correlation_id"] == ROUND17_CORR
    # The round-17 dispatch never threaded a branch — this is the defect's
    # origin and the reason the replay exercises the legacy path.
    assert "branch" not in payload


# ---------------------------------------------------------------------------
# Scenario 1 — branch-aware happy path: worktree isolation + cleanup
# ---------------------------------------------------------------------------


def test_branch_aware_worktree_happy_path(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_base = tmp_path / "worktrees"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))

    head_before = _git(throwaway_repo, "rev-parse", "HEAD")
    status_before = _git(throwaway_repo, "status", "--porcelain")

    description = _launch(
        '{"build_id": "%s", "feature_id": "%s", "correlation_id": "%s", '
        '"branch": "%s", "repo": "appmilla/api_test"}'
        % (ROUND17_BUILD_ID, ROUND17_FEATURE_ID, ROUND17_CORR, PLANNING_BRANCH)
    )
    recorded: dict[str, Any] = {}
    result = _invoke(description, throwaway_repo, exit_code=0, recorded=recorded)

    # guardkit ran in the ISOLATED worktree, not the shared checkout.
    expected_wt = (wt_base / ROUND17_BUILD_ID).resolve()
    assert recorded["cwd"] == str(expected_wt), (
        "guardkit must run with cwd=the isolated worktree, got "
        f"{recorded.get('cwd')!r}"
    )
    assert str(recorded["cwd"]) != str(throwaway_repo)

    # Success cleans the worktree up.
    assert not expected_wt.exists(), "success path must remove the worktree"

    # Shared checkout byte-untouched: same HEAD, same (clean) status.
    assert _git(throwaway_repo, "rev-parse", "HEAD") == head_before
    assert _git(throwaway_repo, "status", "--porcelain") == status_before

    assert _lifecycle(result, ROUND17_FEATURE_ID) == "completed"


# ---------------------------------------------------------------------------
# Scenario 2 — subprocess fails: worktree KEPT + named in the event
# ---------------------------------------------------------------------------


def test_subprocess_failure_keeps_worktree_and_names_it(
    throwaway_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wt_base = tmp_path / "worktrees"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))

    description = _launch(
        '{"build_id": "%s", "feature_id": "%s", "correlation_id": "%s", '
        '"branch": "%s"}'
        % (ROUND17_BUILD_ID, ROUND17_FEATURE_ID, ROUND17_CORR, PLANNING_BRANCH)
    )
    recorded: dict[str, Any] = {}
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        result = _invoke(description, throwaway_repo, exit_code=1, recorded=recorded)

    expected_wt = (wt_base / ROUND17_BUILD_ID).resolve()
    # Failure KEEPS the worktree for forensics ...
    assert expected_wt.exists(), "failure path must KEEP the worktree"
    # ... and NAMES it in the failure event (logged reason).
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "worktree KEPT for forensics" in joined
    assert str(expected_wt) in joined

    # Shared checkout untouched; run failed loud.
    assert _git(throwaway_repo, "status", "--porcelain") == ""
    assert _lifecycle(result, ROUND17_FEATURE_ID) == "failed"


# ---------------------------------------------------------------------------
# Scenario 3 — branch missing locally: loud failure, no litter, no fetch
# ---------------------------------------------------------------------------


def test_missing_branch_fails_loud_without_litter(
    throwaway_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wt_base = tmp_path / "worktrees"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))

    head_before = _git(throwaway_repo, "rev-parse", "HEAD")
    description = _launch(
        '{"build_id": "%s", "feature_id": "%s", "correlation_id": "%s", '
        '"branch": "planning/does-not-exist-anywhere"}'
        % (ROUND17_BUILD_ID, ROUND17_FEATURE_ID, ROUND17_CORR)
    )
    recorded: dict[str, Any] = {}
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        result = _invoke(description, throwaway_repo, exit_code=0, recorded=recorded)

    # Loud failure naming the branch, explicitly refusing to fetch.
    assert _lifecycle(result, ROUND17_FEATURE_ID) == "failed"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "does not exist locally" in joined
    assert "refusing to fetch" in joined

    # guardkit never ran; no worktree litter; shared checkout untouched.
    assert "cwd" not in recorded, "guardkit must not run for a missing branch"
    assert not (wt_base / ROUND17_BUILD_ID).exists()
    assert _git(throwaway_repo, "rev-parse", "HEAD") == head_before
    assert _git(throwaway_repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# Scenario 4 — legacy (no branch): the LITERAL round-17 replay -> shared cwd
# ---------------------------------------------------------------------------


def test_round17_literal_replay_uses_legacy_shared_checkout(
    throwaway_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wt_base = tmp_path / "worktrees"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))

    description = FIXTURE.read_text()  # the exact sidecar bytes, branch-blind
    recorded: dict[str, Any] = {}
    with caplog.at_level(logging.INFO, logger="forge.subagents.autobuild_runner"):
        result = _invoke(description, throwaway_repo, exit_code=0, recorded=recorded)

    # No branch -> byte-compatible legacy path: cwd == the shared checkout,
    # no worktree materialised, and the mode is logged.
    assert recorded["cwd"] == str(throwaway_repo)
    assert not (wt_base / ROUND17_BUILD_ID).exists()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "legacy shared-checkout mode" in joined
    assert _lifecycle(result, ROUND17_FEATURE_ID) == "completed"


# ---------------------------------------------------------------------------
# Scenario 5 — unactionable payload: loud failure, NEVER silent success (#18b)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "total garbage with no payload marker at all",
        "RUN_AUTOBUILD subagent=autobuild_runner payload={not valid json",
        'RUN_AUTOBUILD subagent=autobuild_runner payload={}',
    ],
)
def test_unactionable_payload_drives_to_terminal_failure(description: str) -> None:
    """Drive the graph to its end; the terminal state must be failed, never silent.

    This is the #18b tripwire: the July-3 sidecar ended ``status='success'``
    with zero lifecycle. Regardless of payload shape the graph must reach a
    terminal lifecycle on the channel.
    """
    recorded: dict[str, Any] = {}
    # No repo resolves for a garbage payload, so guardkit never runs; the stub
    # is only present to keep create_subprocess_exec inert if reached.
    stub = _make_exec_stub(recorded, exit_code=0, lines=[b""])
    with patch.object(ar, "_resolve_guardkit_path", lambda: None), patch.object(
        asyncio, "create_subprocess_exec", stub
    ):
        graph = ar._build_runner_graph()
        result = asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=description)]})
        )

    ats = result.get("async_tasks") or {}
    assert ats, "graph must not end with an empty async_tasks channel"
    # Whatever feature key it resolved to, the terminal lifecycle must be failed.
    lifecycles = {snap.get("lifecycle") for snap in ats.values()}
    assert lifecycles == {"failed"}, (
        f"unactionable payload must end 'failed', never silently; got {lifecycles!r}"
    )


def test_empty_messages_drive_to_terminal_failure() -> None:
    """An empty messages channel must still reach a terminal failed state."""
    with patch.object(ar, "_resolve_guardkit_path", lambda: None):
        graph = ar._build_runner_graph()
        result = asyncio.run(graph.ainvoke({"messages": []}))
    ats = result.get("async_tasks") or {}
    assert {snap.get("lifecycle") for snap in ats.values()} == {"failed"}


# ---------------------------------------------------------------------------
# #18b guard in isolation
# ---------------------------------------------------------------------------


def test_finalize_forces_failure_on_non_terminal_channel() -> None:
    """``_node_finalize`` forces a loud failure when the channel is non-terminal.

    Simulates the silent-no-op: the channel carries a non-terminal lifecycle
    (or nothing) when the graph reaches finalize. The guard must overwrite it
    with a failed snapshot rather than let the run end clean.
    """
    description = _launch('{"feature_id": "FEAT-A058", "build_id": "b1"}')
    state = {
        "messages": [HumanMessage(content=description)],
        "async_tasks": {"FEAT-A058": {"lifecycle": "running_wave"}},
    }
    update = ar._node_finalize(state)  # type: ignore[arg-type]
    snap = update["async_tasks"]["FEAT-A058"]
    assert snap["lifecycle"] == "failed"


def test_finalize_passthrough_when_already_terminal() -> None:
    """``_node_finalize`` is a no-op when the channel is already terminal."""
    description = _launch('{"feature_id": "FEAT-A058", "build_id": "b1"}')
    state = {
        "messages": [HumanMessage(content=description)],
        "async_tasks": {"FEAT-A058": {"lifecycle": "completed"}},
    }
    update = ar._node_finalize(state)  # type: ignore[arg-type]
    assert update == {}


# ---------------------------------------------------------------------------
# #18a — boot-visible code version stamp
# ---------------------------------------------------------------------------


def test_code_version_stamp_is_present() -> None:
    """A non-empty code-version stamp is resolved at import (DEFECT #18a)."""
    assert isinstance(ar.RUNNER_CODE_VERSION, str)
    assert ar.RUNNER_CODE_VERSION
    # In this repo it resolves to the git rev.
    assert ar.RUNNER_CODE_VERSION.startswith(("git-", "pkg-")) or (
        ar.RUNNER_CODE_VERSION == "unknown"
    )
