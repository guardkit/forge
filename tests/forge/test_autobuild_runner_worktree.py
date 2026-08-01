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
import json
import logging
import os
import subprocess
from datetime import datetime
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

    # F12 — the outer worktree is DETACHED, so guardkit's cwd carries no
    # current branch; the runner must pin guardkit's base explicitly with the
    # SAME payload branch, else the inner build lands on 'main' (live receipt
    # FEAT-UCNT, cured by selective merge 8403739). Assert the flag rides the
    # argv immediately after --verbose, carrying the planning branch.
    argv = recorded["argv"]
    assert "--base-branch" in argv, (
        f"branch-aware launch must pin guardkit's base branch (F12); "
        f"got argv={argv!r}"
    )
    bb_idx = argv.index("--base-branch")
    assert argv[bb_idx + 1] == PLANNING_BRANCH, (
        f"--base-branch must carry the payload branch {PLANNING_BRANCH!r}, "
        f"got {argv[bb_idx + 1]!r}"
    )
    assert argv[-2:] == ["--base-branch", PLANNING_BRANCH], (
        f"--base-branch <branch> must be the argv tail after --verbose; "
        f"got {argv!r}"
    )

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
    # F12 — the legacy no-branch launch stays byte-identical: NO --base-branch
    # flag (guardkit's cwd-current-branch resolution holds on the shared
    # checkout, which is on a named branch), and the argv is exactly the
    # F2-proven six-token shape.
    argv = recorded["argv"]
    assert "--base-branch" not in argv, (
        f"legacy no-branch launch must NOT pin a base branch (byte-identical); "
        f"got argv={argv!r}"
    )
    assert argv[1:] == [
        "autobuild",
        "feature",
        ROUND17_FEATURE_ID,
        "--fresh",
        "--verbose",
    ], f"legacy argv tail must be unchanged; got {argv[1:]!r}"
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


# ---------------------------------------------------------------------------
# F2 — worktree add uses --detach (branch REF not claimed)
# ---------------------------------------------------------------------------


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _write_guardkit_feature(
    repo: Path, feature_id: str, task_ids: list[str]
) -> None:
    """Write a minimal ``.guardkit/features/<feature_id>.yaml`` with a task list."""
    features_dir = repo / ".guardkit" / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    tasks = "\n".join(f"- id: {t}\n  name: {t}" for t in task_ids)
    (features_dir / f"{feature_id}.yaml").write_text(
        f"id: {feature_id}\ntasks:\n{tasks}\n"
    )


def test_materialise_uses_detach_and_does_not_claim_branch(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: ``git worktree add --detach`` — the branch commit, ref UNCLAIMED."""
    monkeypatch.setenv(
        ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt")
    )
    calls: list[list[str]] = []
    real = ar._run_git

    async def _rec(args: list[str], *, cwd: Path):
        calls.append(list(args))
        return await real(args, cwd=cwd)

    monkeypatch.setattr(ar, "_run_git", _rec)
    wt = asyncio.run(
        ar._materialise_worktree(throwaway_repo, PLANNING_BRANCH, "build-DETACH")
    )

    add = next(c for c in calls if c[:2] == ["worktree", "add"])
    assert add == ["worktree", "add", "--detach", str(wt), PLANNING_BRANCH], (
        f"worktree add must be --detach; got {add!r}"
    )
    # The worktree checked out the branch's COMMIT at a DETACHED HEAD — the
    # branch ref is not claimed by any worktree.
    porcelain = _git(throwaway_repo, "worktree", "list", "--porcelain")
    assert f"branch refs/heads/{PLANNING_BRANCH}" not in porcelain
    assert _git(wt, "rev-parse", "HEAD") == _git(
        throwaway_repo, "rev-parse", PLANNING_BRANCH
    )
    assert _git(wt, "branch", "--show-current") == ""


# ---------------------------------------------------------------------------
# F3 — preflight residue sweep
# ---------------------------------------------------------------------------


def test_prune_preflight_runs_before_materialise(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: ``git worktree prune`` is issued BEFORE the worktree add."""
    monkeypatch.setenv(
        ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt")
    )
    _write_guardkit_feature(throwaway_repo, ROUND17_FEATURE_ID, ["TASK-X-1"])

    seq: list[list[str]] = []
    real = ar._run_git

    async def _rec(args: list[str], *, cwd: Path):
        seq.append(list(args))
        return await real(args, cwd=cwd)

    monkeypatch.setattr(ar, "_run_git", _rec)

    description = _launch(
        '{"build_id": "%s", "feature_id": "%s", "correlation_id": "%s", '
        '"branch": "%s"}'
        % (ROUND17_BUILD_ID, ROUND17_FEATURE_ID, ROUND17_CORR, PLANNING_BRANCH)
    )
    recorded: dict[str, Any] = {}
    _invoke(description, throwaway_repo, exit_code=0, recorded=recorded)

    prune_idx = next(
        i for i, c in enumerate(seq) if c[:2] == ["worktree", "prune"]
    )
    add_idx = next(
        i for i, c in enumerate(seq) if c[:2] == ["worktree", "add"]
    )
    assert prune_idx < add_idx, "prune must precede worktree add"


def test_sweep_detaches_stale_inner_worktree_then_deletes_branch(
    throwaway_repo: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F3(c): stale same-task branch checked out in an on-disk prior inner
    worktree → detached FIRST (forensics preserved), THEN branch deleted."""
    _write_guardkit_feature(throwaway_repo, "FEAT-SWEEP", ["TASK-X-1"])
    inner = tmp_path / "stale_inner"
    _git(
        throwaway_repo, "worktree", "add", "-b", "autobuild/TASK-X-1",
        str(inner), "main",
    )
    # A kept forensic file (the F11 read-only-forensics law protects it).
    (inner / "forensic.txt").write_text("evidence")
    head_before = _git(inner, "rev-parse", "HEAD")

    with caplog.at_level(
        logging.INFO, logger="forge.subagents.autobuild_runner"
    ):
        asyncio.run(ar._sweep_build_refs(throwaway_repo, "FEAT-SWEEP"))

    # Branch deleted; the inner worktree KEPT on disk, detached at same commit,
    # forensic file preserved.
    assert not _branch_exists(throwaway_repo, "autobuild/TASK-X-1")
    assert inner.exists()
    assert (inner / "forensic.txt").read_text() == "evidence"
    assert _git(inner, "rev-parse", "HEAD") == head_before
    assert _git(inner, "branch", "--show-current") == ""

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "detached stale inner worktree" in joined
    assert "deleted stale branch autobuild/TASK-X-1" in joined


def test_sweep_leaves_other_feature_branches_untouched(
    throwaway_repo: Path,
) -> None:
    """F3(d): a branch owned by ANOTHER feature is never swept."""
    _write_guardkit_feature(throwaway_repo, "FEAT-MINE", ["TASK-X-1"])
    _git(throwaway_repo, "branch", "autobuild/TASK-X-1", "main")
    _git(throwaway_repo, "branch", "autobuild/TASK-OTHER-1", "main")

    asyncio.run(ar._sweep_build_refs(throwaway_repo, "FEAT-MINE"))

    assert not _branch_exists(throwaway_repo, "autobuild/TASK-X-1"), (
        "this feature's stale branch must be swept"
    )
    assert _branch_exists(throwaway_repo, "autobuild/TASK-OTHER-1"), (
        "another feature's branch must be left alone"
    )


def test_sweep_yaml_missing_falls_back_to_prune_only(
    throwaway_repo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F3(e): no feature yaml → loud warning + prune-only (no branch sweep)."""
    # No .guardkit/features/<id>.yaml written at all.
    _git(throwaway_repo, "branch", "autobuild/TASK-X-1", "main")

    with caplog.at_level(
        logging.WARNING, logger="forge.subagents.autobuild_runner"
    ):
        asyncio.run(ar._sweep_build_refs(throwaway_repo, "FEAT-NOFILE"))

    # prune-only: the stale branch is NOT deleted because task ids are unknown.
    assert _branch_exists(throwaway_repo, "autobuild/TASK-X-1")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "falling back to prune-only" in joined


def test_sweep_malformed_yaml_falls_back_to_prune_only(
    throwaway_repo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F3(e): unparseable feature yaml → loud warning + prune-only."""
    features_dir = throwaway_repo / ".guardkit" / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    (features_dir / "FEAT-BAD.yaml").write_text("tasks: [unterminated\n")
    _git(throwaway_repo, "branch", "autobuild/TASK-X-1", "main")

    with caplog.at_level(
        logging.WARNING, logger="forge.subagents.autobuild_runner"
    ):
        asyncio.run(ar._sweep_build_refs(throwaway_repo, "FEAT-BAD"))

    assert _branch_exists(throwaway_repo, "autobuild/TASK-X-1")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "falling back to prune-only" in joined


def test_sweep_never_crashes_on_unexpected_error(
    throwaway_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F3: any unexpected sweep failure is logged loud, never raised."""

    async def _boom(args: list[str], *, cwd: Path):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(ar, "_run_git", _boom)
    with caplog.at_level(
        logging.WARNING, logger="forge.subagents.autobuild_runner"
    ):
        # Must not raise.
        asyncio.run(ar._sweep_build_refs(throwaway_repo, "FEAT-ANY"))

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "preflight sweep failed unexpectedly" in joined


# ---------------------------------------------------------------------------
# FEAT-DRC — durable receipt export before success-path removal (register 2a4)
# ---------------------------------------------------------------------------


#: The families :func:`_make_receipt_tree` seeds in the OUTER tree. Not the
#: whole of ``_RECEIPT_FAMILIES`` — ``dcl-capture`` is deliberately absent so
#: every assertion below also proves the honest "missing" accounting.
_OUTER_SEEDED_FAMILIES: tuple[str, ...] = (
    ".guardkit/autobuild-private",
    ".guardkit/qav-shadow",
    ".guardkit/autobuild",
)


def _make_receipt_tree(worktree: Path) -> dict[str, Path]:
    """Populate a fake outer-worktree .guardkit with three receipt families."""
    files = {}
    for rel in (
        ".guardkit/autobuild-private/TASK-X-001/coach_turn_1.json",
        ".guardkit/autobuild-private/TASK-X-001/spec_conformance/conformance.json",
        ".guardkit/qav-shadow/queue.jsonl",
        ".guardkit/autobuild/FEAT-X/review-summary.md",
    ):
        p = worktree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"receipt::{rel}")
        files[rel] = p
    return files


def _make_inner_receipt_tree(worktree: Path, name: str = "FEAT-X") -> list[str]:
    """Seed an INNER task worktree the way guardkit's WorktreeManager does.

    Mirrors the kept FEAT-153C tree: the task worker's OWN receipts —
    ``player_turn_*.json``, ``qav_shadow_turn_*.json``,
    ``task_work_results.json`` — plus an inner ``qav-shadow/queue.jsonl`` that
    holds a record the OUTER queue never received.
    """
    rels = [
        f".guardkit/worktrees/{name}/.guardkit/autobuild/TASK-X-001/player_turn_1.json",
        f".guardkit/worktrees/{name}/.guardkit/autobuild/TASK-X-001/qav_shadow_turn_1.json",
        f".guardkit/worktrees/{name}/.guardkit/autobuild/TASK-X-001/task_work_results.json",
        f".guardkit/worktrees/{name}/.guardkit/qav-shadow/queue.jsonl",
        f".guardkit/worktrees/{name}/.guardkit/dcl-capture/queue.jsonl",
    ]
    for rel in rels:
        p = worktree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"inner::{rel}")
    return rels


def _skip_reason(result: "ar.ReceiptExport", family: str) -> str | None:
    for row in result.skipped:
        if row["family"] == family:
            return row["reason"]
    return None


class TestExportReceipts:
    def test_exports_all_families_preserving_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-X-1")
        assert result.ok is True
        assert sorted(result.exported) == sorted(_OUTER_SEEDED_FAMILIES)

        for rel in (
            ".guardkit/autobuild-private/TASK-X-001/coach_turn_1.json",
            ".guardkit/autobuild-private/TASK-X-001/spec_conformance/conformance.json",
            ".guardkit/qav-shadow/queue.jsonl",
            ".guardkit/autobuild/FEAT-X/review-summary.md",
        ):
            exported = dest_root / "build-X-1" / rel
            assert exported.is_file(), rel
            assert exported.read_text() == f"receipt::{rel}"

    def test_missing_families_still_succeed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        (worktree / ".guardkit/qav-shadow").mkdir(parents=True)
        (worktree / ".guardkit/qav-shadow/queue.jsonl").write_text("{}")
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))

        result = ar._export_receipts(worktree, "build-X-2")
        assert result.ok is True
        assert result.exported == [".guardkit/qav-shadow"], (
            "only the family that exists rides the per-run exported list"
        )
        assert (
            tmp_path / "receipts/build-X-2/.guardkit/qav-shadow/queue.jsonl"
        ).is_file()

    def test_empty_worktree_export_is_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
        result = ar._export_receipts(worktree, "build-X-3")
        assert result.ok is True
        assert result.exported == []
        assert result.file_counts == {}

    def test_copy_failure_returns_false_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
        with patch.object(
            ar.shutil, "copytree", side_effect=OSError("disk full")
        ):
            result = ar._export_receipts(worktree, "build-X-4")
        assert result.ok is False
        assert result.exported == [], (
            "a family that never copied must not be claimed"
        )
        assert _skip_reason(result, ".guardkit/qav-shadow").startswith(
            "copy-failed:"
        )

    def test_default_destination_expands_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default rides ~/forge-state/receipts; point HOME at tmp so the
        # test never touches the real estate.
        monkeypatch.delenv(ar.RECEIPTS_DIR_ENV, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)

        result = ar._export_receipts(worktree, "build-X-5")
        assert result.ok is True
        assert (
            tmp_path
            / "forge-state/receipts/build-X-5/.guardkit/qav-shadow/queue.jsonl"
        ).is_file()


class TestInnerWorktreeReceiptsLand:
    """THE FIND: the task worker's receipts live in the INNER worktree.

    Until this lane the export read only the OUTER tree, so the richest
    per-turn evidence of every succeeded build — and the run's own shadow
    verdict — was removed with the worktree (FEAT-UDBE's 07-28 loss, one
    level down).
    """

    def test_inner_families_land_as_namespaced_subdirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        rels = _make_inner_receipt_tree(worktree)
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-IN-1")
        assert result.ok is True

        pack = dest_root / "build-IN-1"
        for rel in rels:
            # The inner tree's layout is preserved verbatim under the pack.
            landed = pack / rel.replace(".guardkit/worktrees/", "worktrees/", 1)
            assert landed.is_file(), rel
            assert landed.read_text() == f"inner::{rel}"

        assert "worktrees/FEAT-X/.guardkit/autobuild" in result.exported
        assert "worktrees/FEAT-X/.guardkit/qav-shadow" in result.exported
        assert "worktrees/FEAT-X/.guardkit/dcl-capture" in result.exported

    def test_inner_copy_never_clobbers_the_outer_family(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both queues survive — the outer's and the inner's fuller one."""
        worktree = tmp_path / "wt"
        (worktree / ".guardkit/qav-shadow").mkdir(parents=True)
        (worktree / ".guardkit/qav-shadow/queue.jsonl").write_text("outer-6\n")
        inner_q = worktree / ".guardkit/worktrees/FEAT-X/.guardkit/qav-shadow"
        inner_q.mkdir(parents=True)
        (inner_q / "queue.jsonl").write_text("outer-6\ninner-7\n")
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        ar._export_receipts(worktree, "build-IN-2")

        pack = dest_root / "build-IN-2"
        assert (
            pack / ".guardkit/qav-shadow/queue.jsonl"
        ).read_text() == "outer-6\n"
        assert (
            pack / "worktrees/FEAT-X/.guardkit/qav-shadow/queue.jsonl"
        ).read_text() == "outer-6\ninner-7\n"

    def test_every_inner_worktree_is_exported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_inner_receipt_tree(worktree, "TASK-X-001")
        _make_inner_receipt_tree(worktree, "TASK-X-002")
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-IN-3")

        for name in ("TASK-X-001", "TASK-X-002"):
            assert (
                dest_root
                / "build-IN-3"
                / f"worktrees/{name}/.guardkit/autobuild"
                / "TASK-X-001/task_work_results.json"
            ).is_file(), name
            assert f"worktrees/{name}/.guardkit/qav-shadow" in result.exported

    def test_no_inner_worktrees_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))

        result = ar._export_receipts(worktree, "build-IN-4")
        assert result.ok is True
        assert not any(f.startswith("worktrees/") for f in result.exported)

    def test_dcl_capture_family_is_exported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DCL machine-authoring corpus is a receipt family too."""
        worktree = tmp_path / "wt"
        (worktree / ".guardkit/dcl-capture").mkdir(parents=True)
        (worktree / ".guardkit/dcl-capture/queue.jsonl").write_text("{}\n")
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-DCL-1")
        assert ".guardkit/dcl-capture" in result.exported
        assert (
            dest_root / "build-DCL-1/.guardkit/dcl-capture/queue.jsonl"
        ).is_file()


class TestExportAccountingIsHonest:
    """The manifest must never claim an export that produced nothing."""

    def test_empty_family_is_skipped_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        # The directory EXISTS but holds no file — the pre-lane code copied it
        # and claimed the family in the manifest.
        (worktree / ".guardkit/qav-shadow").mkdir(parents=True)
        (worktree / ".guardkit/autobuild/FEAT-X").mkdir(parents=True)
        (worktree / ".guardkit/autobuild/FEAT-X/events.jsonl").write_text("{}")
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-HON-1")
        assert result.exported == [".guardkit/autobuild"]
        assert _skip_reason(result, ".guardkit/qav-shadow") == "empty"
        assert _skip_reason(result, ".guardkit/autobuild-private") == "missing"
        assert result.file_counts == {".guardkit/autobuild": 1}
        assert not (dest_root / "build-HON-1/.guardkit/qav-shadow").exists(), (
            "an empty family must not leave an empty directory in the pack"
        )

    def test_file_counts_match_the_pack_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        _make_inner_receipt_tree(worktree)
        dest_root = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest_root))

        result = ar._export_receipts(worktree, "build-HON-2")
        pack = dest_root / "build-HON-2"
        for family, count in result.file_counts.items():
            on_disk = sum(1 for p in (pack / family).rglob("*") if p.is_file())
            assert on_disk == count, family
        assert sum(result.file_counts.values()) == 9

    def test_one_bad_family_never_costs_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
        real_copytree = ar.shutil.copytree

        def _flaky(*args: Any, **kwargs: Any) -> Any:
            # shutil.copytree recurses through the module global, so this stub
            # sees the inner calls too — match on the source only.
            src = args[0] if args else kwargs.get("src")
            if str(src).endswith("qav-shadow"):
                raise OSError("disk full")
            return real_copytree(*args, **kwargs)

        with patch.object(ar.shutil, "copytree", _flaky):
            result = ar._export_receipts(worktree, "build-HON-3")

        assert result.ok is False, "a real copy failure keeps the worktree"
        assert sorted(result.exported) == [
            ".guardkit/autobuild",
            ".guardkit/autobuild-private",
        ]
        assert _skip_reason(result, ".guardkit/qav-shadow").startswith(
            "copy-failed:"
        )


class TestFinalizeSuccessWorktree:
    def test_export_success_then_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
        calls: list[tuple[Path, Path]] = []

        async def _fake_remove(repo: Path, wt: Path) -> None:
            calls.append((repo, wt))

        with patch.object(ar, "_remove_worktree", _fake_remove):
            asyncio.run(
                ar._finalize_success_worktree(tmp_path, worktree, "build-X-6")
            )

        assert calls == [(tmp_path, worktree)]
        assert (
            tmp_path / "receipts/build-X-6/.guardkit/qav-shadow/queue.jsonl"
        ).is_file()

    def test_export_failure_keeps_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FEAT-DRC crux: removal is CONDITIONAL on the export — a failed
        # export keeps the worktree (forensics posture), and the call still
        # returns cleanly (the build outcome is never altered).
        worktree = tmp_path / "wt"
        _make_receipt_tree(worktree)
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
        calls: list[tuple[Path, Path]] = []

        async def _fake_remove(repo: Path, wt: Path) -> None:
            calls.append((repo, wt))

        with (
            patch.object(ar, "_remove_worktree", _fake_remove),
            patch.object(
                ar.shutil, "copytree", side_effect=OSError("disk full")
            ),
        ):
            asyncio.run(
                ar._finalize_success_worktree(tmp_path, worktree, "build-X-7")
            )

        assert calls == []  # never removed
        assert worktree.is_dir()  # kept on disk


# ---------------------------------------------------------------------------
# FEAT-DRF — the FAILURE PACK: failure-path export + stdout tee + manifest
# (debugging-residual design pass, Lane 1)
# ---------------------------------------------------------------------------


_DRF_STDOUT_LINES = [
    b"[guardkit] wave 0 starting\n",
    b"[guardkit-coach] TASK-X-001 turn 1: feedback\n",
    b"Traceback (most recent call last):\n",
]


def _invoke_seeding_receipts(
    description: str,
    repo: Path,
    *,
    exit_code: int,
    recorded: dict[str, Any],
    lines: list[bytes] | None = None,
):
    """Drive a full graph run whose fake guardkit WRITES receipts in its cwd.

    The real guardkit leaves the three ``.guardkit`` receipt families in the
    tree it ran in; the stub reproduces that so the failure-path export has
    real receipts to copy.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            cwd = kwargs.get("cwd")
            recorded["cwd"] = cwd
            recorded["argv"] = list(args)
            if cwd:
                _make_receipt_tree(Path(cwd))
            return _FakeProc(exit_code, list(lines or _DRF_STDOUT_LINES))
        return await real_exec(*args, **kwargs)

    with patch.object(ar, "_resolve_repo_path", lambda payload: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit")
    ), patch.object(asyncio, "create_subprocess_exec", _stub):
        graph = ar._build_runner_graph()
        return asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=description)]})
        )


def _branch_aware_launch() -> str:
    return _launch(
        '{"build_id": "%s", "feature_id": "%s", "correlation_id": "%s", '
        '"branch": "%s", "repo": "appmilla/api_test"}'
        % (ROUND17_BUILD_ID, ROUND17_FEATURE_ID, ROUND17_CORR, PLANNING_BRANCH)
    )


class TestFailurePackEndToEnd:
    def test_failed_build_exports_receipts_writes_manifest_and_keeps_worktree(
        self,
        throwaway_repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failure path gains DURABILITY without losing the kept tree.

        Before FEAT-DRF a failed build's whole evidence base sat under /tmp
        (the 07-30 cold boot deleted it). The export is purely additive: the
        worktree must STILL be kept and STILL be named in the failure reason.
        """
        wt_base = tmp_path / "worktrees"
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))

        recorded: dict[str, Any] = {}
        result = _invoke_seeding_receipts(
            _branch_aware_launch(), throwaway_repo, exit_code=1, recorded=recorded
        )

        assert _lifecycle(result, ROUND17_FEATURE_ID) == "failed"

        # (1) The worktree is STILL KEPT — the export never took it away.
        expected_wt = (wt_base / ROUND17_BUILD_ID).resolve()
        assert expected_wt.is_dir(), "failure must still KEEP the worktree"

        # (2) All three receipt families are now durable, layout preserved.
        pack = receipts / ROUND17_BUILD_ID
        for rel in (
            ".guardkit/autobuild-private/TASK-X-001/coach_turn_1.json",
            ".guardkit/autobuild-private/TASK-X-001/spec_conformance/conformance.json",
            ".guardkit/qav-shadow/queue.jsonl",
            ".guardkit/autobuild/FEAT-X/review-summary.md",
        ):
            assert (pack / rel).is_file(), rel
            assert (pack / rel).read_text() == f"receipt::{rel}"

        # (3) The manifest indexes the pack.
        manifest = json.loads((pack / ar.FAILURE_MANIFEST_NAME).read_text())
        assert manifest["build_id"] == ROUND17_BUILD_ID
        assert manifest["feature_id"] == ROUND17_FEATURE_ID
        assert manifest["correlation_id"] == ROUND17_CORR
        assert manifest["reason"] == "guardkit autobuild exit=1"
        assert manifest["timed_out"] is False
        assert manifest["exit_code"] == 1
        assert manifest["worktree_path"] == str(expected_wt)
        assert manifest["branch"] == PLANNING_BRANCH
        assert sorted(manifest["receipt_families_exported"]) == sorted(
            _OUTER_SEEDED_FAMILIES
        )
        # ...and the family the stub never wrote is named honestly instead of
        # being claimed as an export that produced nothing.
        assert {
            row["family"]: row["reason"]
            for row in manifest["receipt_families_skipped"]
        }[".guardkit/dcl-capture"] == "missing"
        assert manifest["receipt_export_ok"] is True
        assert manifest["receipt_file_counts"][".guardkit/qav-shadow"] == 1
        # An ISO-8601 UTC instant the diagnoser can order packs by.
        assert datetime.fromisoformat(manifest["failed_at"]).tzinfo is not None

        # (4) The stdout narrative survived the run.
        tee_log = (pack / ar.STDOUT_LOG_NAME).read_text()
        assert "Traceback (most recent call last):" in tee_log

    def test_success_path_writes_no_failure_manifest(
        self,
        throwaway_repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FEAT-DRC's success behaviour is untouched: export + removal, no manifest."""
        wt_base = tmp_path / "worktrees"
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))

        recorded: dict[str, Any] = {}
        result = _invoke_seeding_receipts(
            _branch_aware_launch(), throwaway_repo, exit_code=0, recorded=recorded
        )

        assert _lifecycle(result, ROUND17_FEATURE_ID) == "completed"
        assert not (wt_base / ROUND17_BUILD_ID).exists(), "success still removes"

        pack = receipts / ROUND17_BUILD_ID
        assert (pack / ".guardkit/qav-shadow/queue.jsonl").is_file()
        assert not (pack / ar.FAILURE_MANIFEST_NAME).exists(), (
            "a succeeded build must never carry a failure manifest"
        )
        # The tee rides every build, not just failed ones.
        assert "[guardkit] wave 0 starting" in (pack / ar.STDOUT_LOG_NAME).read_text()

    def test_unwritable_receipts_root_never_regresses_the_failure(
        self,
        throwaway_repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Whole pack is best-effort: an unwritable root warns, never raises.

        The receipts root is a regular FILE here, so every pack write (tee,
        export, manifest) fails at mkdir. The build must still fail loud with
        its worktree kept and its forensics pointer intact.
        """
        wt_base = tmp_path / "worktrees"
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(blocked))

        recorded: dict[str, Any] = {}
        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            result = _invoke_seeding_receipts(
                _branch_aware_launch(),
                throwaway_repo,
                exit_code=1,
                recorded=recorded,
            )

        assert _lifecycle(result, ROUND17_FEATURE_ID) == "failed"
        expected_wt = (wt_base / ROUND17_BUILD_ID).resolve()
        assert expected_wt.is_dir(), "the kept-worktree posture is unconditional"

        messages = [r.getMessage() for r in caplog.records]
        joined = " ".join(messages)
        assert "worktree KEPT for forensics" in joined
        assert "stdout tee DISABLED" in joined
        assert "failure manifest NOT written" in joined
        # ONE tee warning for the whole run, whatever the line count.
        assert sum("stdout tee DISABLED" in m for m in messages) == 1
        assert blocked.read_text() == "not a directory"  # nothing clobbered it


class TestStdoutTee:
    def test_lazy_open_then_appends_every_line(self, tmp_path: Path) -> None:
        log = tmp_path / "pack" / ar.STDOUT_LOG_NAME
        tee = ar._StdoutTee(log)
        assert not log.exists(), "no line drained yet -> no file"

        tee.write("first")
        tee.write("second")
        tee.close()

        assert log.read_text() == "first\nsecond\n"
        assert tee.disabled is False

    def test_file_error_warns_once_and_disables(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("file, not a dir")
        tee = ar._StdoutTee(blocker / "sub" / ar.STDOUT_LOG_NAME)

        with caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            for _ in range(5):
                tee.write("line")  # must never raise
            tee.close()

        assert tee.disabled is True
        warnings = [
            r.getMessage()
            for r in caplog.records
            if "stdout tee DISABLED" in r.getMessage()
        ]
        assert len(warnings) == 1


class TestFailureManifest:
    def test_legacy_no_worktree_failure_still_gets_a_manifest(
        self, tmp_path: Path
    ) -> None:
        """No worktree (legacy shared-checkout path) -> nulls, never a crash."""
        receipts = tmp_path / "receipts"
        with patch.dict(os.environ, {ar.RECEIPTS_DIR_ENV: str(receipts)}):
            ar._write_failure_manifest(
                build_id="build-DRF-1",
                payload={"feature_id": "FEAT-DRF", "correlation_id": "corr-1"},
                reason="guardkit autobuild timed out after 30s",
                timed_out=True,
                exit_code=-1,
                worktree_path=None,
                branch=None,
                receipts=None,
            )
        manifest = json.loads(
            (receipts / "build-DRF-1" / ar.FAILURE_MANIFEST_NAME).read_text()
        )
        assert manifest["worktree_path"] is None
        assert manifest["branch"] is None
        assert manifest["timed_out"] is True
        assert manifest["exit_code"] == -1
        assert manifest["receipt_families_exported"] == []

    def test_manifest_write_failure_is_swallowed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        with patch.dict(os.environ, {ar.RECEIPTS_DIR_ENV: str(blocked)}), caplog.at_level(
            logging.WARNING, logger="forge.subagents.autobuild_runner"
        ):
            ar._write_failure_manifest(  # must not raise
                build_id="build-DRF-2",
                payload={},
                reason="boom",
                timed_out=False,
                exit_code=2,
                worktree_path=None,
                branch=None,
                receipts=None,
            )
        assert "failure manifest NOT written" in " ".join(
            r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# FEAT-FCT — CancelledError reaps the guardkit child (register 2b, RUNNING half)
# ---------------------------------------------------------------------------


class _HangingProc:
    """A fake guardkit proc that runs until kill() — cancel-reap fixture."""

    pid = 424242

    def __init__(self) -> None:
        self._dead = asyncio.Event()
        self.kill_calls = 0
        self.returncode: int | None = None

    class _Stdout:
        def __init__(self, dead: asyncio.Event) -> None:
            self._dead = dead

        async def readline(self) -> bytes:
            await self._dead.wait()
            return b""

    @property
    def stdout(self) -> "_HangingProc._Stdout":
        return _HangingProc._Stdout(self._dead)

    async def wait(self) -> int:
        await self._dead.wait()
        self.returncode = -9
        return -9

    def kill(self) -> None:
        self.kill_calls += 1
        self._dead.set()


def test_cancelled_run_kills_and_reaps_the_guardkit_child(
    tmp_path: Path,
) -> None:
    """FEAT-FCT: a langgraph interrupt cancels the node's task — the runner
    must kill+reap the guardkit subprocess and re-raise, never orphan it
    (the 2026-07-28 orphan class)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "seed")

    proc = _HangingProc()
    started = asyncio.Event()
    real_exec = asyncio.create_subprocess_exec

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            started.set()
            return proc
        return await real_exec(*args, **kwargs)

    async def _drive() -> None:
        with patch.object(
            ar, "_resolve_repo_path", lambda payload: repo
        ), patch.object(
            ar, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit")
        ), patch.object(asyncio, "create_subprocess_exec", _stub):
            graph = ar._build_runner_graph()
            payload = '{"feature_id": "FEAT-FCT1", "build_id": "build-FEAT-FCT1-1"}'
            task = asyncio.ensure_future(
                graph.ainvoke(
                    {"messages": [HumanMessage(content=_launch(payload))]}
                )
            )
            await asyncio.wait_for(started.wait(), timeout=10.0)
            await asyncio.sleep(0.05)  # let the wait block engage
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_drive())
    assert proc.kill_calls >= 1  # the child was killed, not orphaned
    assert proc.returncode == -9  # and reaped


def test_pack_permissions_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FEAT-DRF coach finding: the pack persists the full subprocess narrative
    durably — dirs must be 0700 and files 0600 regardless of umask."""
    worktree = tmp_path / "wt"
    _make_receipt_tree(worktree)
    dest = tmp_path / "receipts"
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(dest))

    assert ar._export_receipts(worktree, "build-PERM-1").ok is True

    pack = dest / "build-PERM-1"
    assert (pack.stat().st_mode & 0o777) == 0o700
    qav = pack / ".guardkit/qav-shadow/queue.jsonl"
    assert (qav.stat().st_mode & 0o777) == 0o600
    assert (qav.parent.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# FEAT-DRF residuals (07-30 coach findings 2/3) — run-scoped packs
# ---------------------------------------------------------------------------


class TestRunScopedStdoutLog:
    """Finding 2 (logs): build_id reuse appends runs into ONE log — the tee
    now delimits each run's segment with a header line on the lazy open."""

    def test_each_runs_segment_is_delimited(self, tmp_path: Path) -> None:
        log = tmp_path / "pack" / ar.STDOUT_LOG_NAME
        payload = {"build_id": "build-R-1", "correlation_id": "corr-1"}

        first = ar._StdoutTee(
            log, run_header=ar._stdout_run_header(payload, "FEAT-R")
        )
        first.write("run-one line")
        first.close()

        second = ar._StdoutTee(
            log, run_header=ar._stdout_run_header(payload, "FEAT-R")
        )
        second.write("run-two line")
        second.close()

        lines = log.read_text().splitlines()
        headers = [
            i
            for i, line in enumerate(lines)
            if line.startswith(ar.STDOUT_RUN_HEADER_PREFIX)
        ]
        assert len(headers) == 2, "one separator per run segment"
        # Each run's narrative sits under its own header.
        assert lines[headers[0] + 1] == "run-one line"
        assert lines[headers[1] + 1] == "run-two line"
        # The header names the run's identity for the diagnoser.
        assert "feature_id=FEAT-R" in lines[headers[0]]
        assert "build_id=build-R-1" in lines[headers[0]]
        assert "correlation_id=corr-1" in lines[headers[0]]

    def test_silent_run_still_leaves_no_file(self, tmp_path: Path) -> None:
        log = tmp_path / "pack" / ar.STDOUT_LOG_NAME
        tee = ar._StdoutTee(
            log, run_header=ar._stdout_run_header({}, "FEAT-R")
        )
        tee.close()
        assert not log.exists(), (
            "the header rides the lazy open — a build that prints nothing "
            "must still leave no file"
        )

    def test_headerless_tee_is_byte_identical_legacy(self, tmp_path: Path) -> None:
        log = tmp_path / "pack" / ar.STDOUT_LOG_NAME
        tee = ar._StdoutTee(log)
        tee.write("only line")
        tee.close()
        assert log.read_text() == "only line\n"


class TestFailureManifestArchiveOnReuse:
    """Finding 2 (manifests): a reused build_id must never DESTROY the prior
    run's failure-manifest.json — it is archived aside, uniquely named."""

    def _write(self, build_id: str, reason: str) -> None:
        ar._write_failure_manifest(
            build_id=build_id,
            payload={"feature_id": "FEAT-RM", "correlation_id": "corr-rm"},
            reason=reason,
            timed_out=False,
            exit_code=1,
            worktree_path=None,
            branch=None,
            receipts=None,
        )

    def test_prior_manifest_survives_a_second_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))
        pack = receipts / "build-RM-1"

        self._write("build-RM-1", "first failure")
        self._write("build-RM-1", "second failure")

        latest = json.loads((pack / ar.FAILURE_MANIFEST_NAME).read_text())
        assert latest["reason"] == "second failure"

        archived = sorted(
            p
            for p in pack.glob("failure-manifest.*.json")
            if p.name != ar.FAILURE_MANIFEST_NAME
        )
        assert len(archived) == 1, "the first run's manifest was archived"
        prior = json.loads(archived[0].read_text())
        assert prior["reason"] == "first failure"

    def test_three_runs_leave_two_uniquely_named_archives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))
        pack = receipts / "build-RM-2"

        self._write("build-RM-2", "first")
        self._write("build-RM-2", "second")
        self._write("build-RM-2", "third")

        latest = json.loads((pack / ar.FAILURE_MANIFEST_NAME).read_text())
        assert latest["reason"] == "third"
        archived = sorted(
            p
            for p in pack.glob("failure-manifest.*.json")
            if p.name != ar.FAILURE_MANIFEST_NAME
        )
        assert len(archived) == 2
        assert len({p.name for p in archived}) == 2, "unique archive names"
        reasons = {json.loads(p.read_text())["reason"] for p in archived}
        assert reasons == {"first", "second"}

    def test_unparseable_prior_manifest_still_archived_by_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))
        pack = receipts / "build-RM-3"
        pack.mkdir(parents=True)
        (pack / ar.FAILURE_MANIFEST_NAME).write_text("{not json")

        self._write("build-RM-3", "fresh failure")

        latest = json.loads((pack / ar.FAILURE_MANIFEST_NAME).read_text())
        assert latest["reason"] == "fresh failure"
        archived = [
            p
            for p in pack.glob("failure-manifest.*.json")
            if p.name != ar.FAILURE_MANIFEST_NAME
        ]
        assert len(archived) == 1
        assert archived[0].read_text() == "{not json"


class TestExportedFamiliesAreThisRunsOnly:
    """Finding 2 (manifest honesty): ``receipt_families_exported`` reports
    ONLY families THIS run exported — never a destination read-back that
    claims an earlier run's leftovers."""

    def test_stale_destination_family_is_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "receipts"
        monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(receipts))
        # An earlier run of the reused build_id left the qav-shadow family.
        stale = receipts / "build-STALE-1/.guardkit/qav-shadow"
        stale.mkdir(parents=True)
        (stale / "queue.jsonl").write_text("{}")

        # THIS run's worktree carries only the autobuild family.
        worktree = tmp_path / "wt"
        (worktree / ".guardkit/autobuild/FEAT-X").mkdir(parents=True)
        (worktree / ".guardkit/autobuild/FEAT-X/review-summary.md").write_text("r")

        result = ar._export_receipts(worktree, "build-STALE-1")
        assert result.ok is True
        assert result.exported == [".guardkit/autobuild"], (
            "the stale qav-shadow leftover must not be claimed as this "
            "run's export"
        )


class TestLegacyPendingPackUniqueness:
    """Finding 3: legacy no-build_id packs must not collapse into one shared
    ``-pending`` location — each run gets a unique pack directory."""

    def test_two_legacy_resolutions_differ(self) -> None:
        first = ar._resolve_receipt_build_id({}, None, "FEAT-L")
        second = ar._resolve_receipt_build_id({}, None, "FEAT-L")
        assert first != second
        assert first.startswith("build-FEAT-L-pending-")
        assert second.startswith("build-FEAT-L-pending-")

    def test_payload_build_id_tier_is_untouched(self) -> None:
        assert (
            ar._resolve_receipt_build_id(
                {"build_id": "build-EXPLICIT-1"}, None, "FEAT-L"
            )
            == "build-EXPLICIT-1"
        )

    def test_worktree_name_tier_is_untouched(self, tmp_path: Path) -> None:
        wt = tmp_path / "build-FROM-WT-1"
        assert ar._resolve_receipt_build_id({}, wt, "FEAT-L") == "build-FROM-WT-1"
