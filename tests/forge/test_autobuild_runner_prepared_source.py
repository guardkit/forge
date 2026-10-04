"""The runner builds the commit a prepared feature was admitted at.

4 October 2026 (project initialisation design, Part 6, point 4). When the
launch carries ``source_commit``, the runner makes its own local branch
``forge/source/<build_id>`` at that commit in the build clone (refusing if the
commit is absent — it never fetches), lays its detached worktree there, passes
that branch as ``--base-branch``, runs ``guardkit feature validate <id>
--json`` in the worktree before ``guardkit autobuild feature``, and deletes the
branch when it removes the worktree. Without ``source_commit`` nothing changes.

A throwaway repository stands in for the build clone; only the guardkit
subprocess is stubbed, and real git runs everywhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from forge.cli._db_resolve import FORGE_DB_PATH_ENV
from forge.subagents import autobuild_runner as ar

BUILD_ID = "build-FEAT-AB12-20261004120000"
FEATURE = "FEAT-AB12"
CORR = "corr-prepared-runner"
QUEUED_BRANCH = "feature/prepared"
SOURCE_BRANCH = f"forge/source/{BUILD_ID}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    monkeypatch.setenv(
        FORGE_DB_PATH_ENV, str(tmp_path_factory.mktemp("no-ledger") / "absent.db")
    )


@pytest.fixture()
def clone(tmp_path: Path) -> tuple[Path, str]:
    """A build clone with ``main`` checked out and the queued branch at A."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "checkout", "-q", "-b", QUEUED_BRANCH)
    (repo / "prepared.md").write_text("admitted\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "admitted")
    admitted = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, admitted


class _Out:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = [*lines, b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _Proc:
    def __init__(self, code: int, output: bytes = b"") -> None:
        self.pid = 4243
        self.returncode = code
        self._output = output
        self.stdout = _Out([output] if output else [])

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, None]:
        return self._output, None

    def kill(self) -> None:
        return None


def _stub(
    calls: list[dict[str, Any]],
    *,
    validate_code: int = 0,
    validate_out: bytes = b"{}",
    build_code: int = 0,
):
    real = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            cwd = kwargs.get("cwd")
            head = _git(Path(cwd), "rev-parse", "HEAD") if cwd else None
            calls.append({"argv": list(args), "cwd": cwd, "head": head})
            if len(args) > 2 and args[1] == "feature" and args[2] == "validate":
                return _Proc(validate_code, validate_out)
            return _Proc(build_code, b"guardkit running\n")
        return await real(*args, **kwargs)

    return _exec


def _run(repo: Path, payload: dict[str, Any], calls: list[dict[str, Any]], **stub_kw: Any):
    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner payload=" + json.dumps(payload)
    )
    with patch.object(ar, "_resolve_repo_path", lambda _p: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit")
    ), patch.object(asyncio, "create_subprocess_exec", _stub(calls, **stub_kw)):
        graph = ar._build_runner_graph()
        return asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=description)]}))


def _lifecycle(result: dict[str, Any]) -> str | None:
    snap = (result.get("async_tasks") or {}).get(FEATURE)
    return snap.get("lifecycle") if isinstance(snap, dict) else None


def _payload(admitted: str, **extra: Any) -> dict[str, Any]:
    return {
        "build_id": BUILD_ID,
        "feature_id": FEATURE,
        "correlation_id": CORR,
        "branch": QUEUED_BRANCH,
        "repo": "synthetic/project",
        "source_commit": admitted,
        **extra,
    }


def test_the_admitted_commit_is_built_even_after_the_branch_moves(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    repo, admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    # The queued branch moves on after admission.
    _git(repo, "checkout", "-q", QUEUED_BRANCH)
    (repo / "later.md").write_text("later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "later")
    _git(repo, "checkout", "-q", "main")
    assert _git(repo, "rev-parse", QUEUED_BRANCH) != admitted

    calls: list[dict[str, Any]] = []
    result = _run(repo, _payload(admitted), calls)

    validate, build = calls
    worktree = str((tmp_path / "wt" / BUILD_ID).resolve())
    # GuardKit's own check ran first, in the worktree, at the admitted commit.
    assert validate["argv"][1:] == ["feature", "validate", FEATURE, "--json"]
    assert validate["cwd"] == worktree and validate["head"] == admitted
    # Then the build, in the same worktree, on the build's own base branch.
    assert build["argv"][1:4] == ["autobuild", "feature", FEATURE]
    assert build["cwd"] == worktree and build["head"] == admitted
    bb = build["argv"].index("--base-branch")
    assert build["argv"][bb + 1] == SOURCE_BRANCH
    # Success removes the worktree and the build's own branch.
    assert _lifecycle(result) == "completed"
    assert not Path(worktree).exists()
    assert _git(repo, "branch", "--list", SOURCE_BRANCH) == ""
    # The shared checkout was never moved.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_failed_feature_validate_stops_the_build_with_guardkits_words(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch, caplog
) -> None:
    repo, admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    said = json.dumps(
        {
            "feature_id": FEATURE,
            "valid": False,
            "errors": ["Task file not found: tasks/backlog/x/TASK-AB12-001.md"],
        }
    ).encode()

    calls: list[dict[str, Any]] = []
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        result = _run(repo, _payload(admitted), calls, validate_code=1, validate_out=said)

    assert _lifecycle(result) == "failed"
    assert len(calls) == 1 and calls[0]["argv"][1] == "feature"  # never built
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "guardkit feature validate refused FEAT-AB12" in joined
    assert "Task file not found: tasks/backlog/x/TASK-AB12-001.md" in joined
    # Kept for forensics, like every other failed build's worktree; the
    # build's own branch goes anyway (the kept tree is detached at the commit).
    assert (tmp_path / "wt" / BUILD_ID).exists()
    assert _git(repo, "branch", "--list", SOURCE_BRANCH) == ""


def test_a_commit_missing_from_the_clone_is_refused_without_fetching(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch, caplog
) -> None:
    repo, _admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))

    calls: list[dict[str, Any]] = []
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        result = _run(repo, _payload("f" * 40), calls)

    assert _lifecycle(result) == "failed"
    assert calls == []
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "is not in" in joined and "refusing to fetch" in joined
    assert not (tmp_path / "wt" / BUILD_ID).exists()
    assert _git(repo, "branch", "--list", SOURCE_BRANCH) == ""


def test_without_a_source_commit_the_launch_is_unchanged(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    repo, _admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    payload = _payload("unused")
    payload.pop("source_commit")

    calls: list[dict[str, Any]] = []
    result = _run(repo, payload, calls)

    (build,) = calls  # no feature validate on this path
    assert build["argv"][1:3] == ["autobuild", "feature"]
    assert build["argv"][-2:] == ["--base-branch", QUEUED_BRANCH]
    assert _lifecycle(result) == "completed"
    assert _git(repo, "branch", "--list", "forge/source/*") == ""


def test_a_failed_build_deletes_its_own_branch_and_keeps_its_worktree(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    repo, admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))

    calls: list[dict[str, Any]] = []
    result = _run(repo, _payload(admitted), calls, build_code=1)

    assert _lifecycle(result) == "failed"
    assert len(calls) == 2  # validate, then the build that failed
    assert (tmp_path / "wt" / BUILD_ID).exists()
    assert _git(repo, "branch", "--list", SOURCE_BRANCH) == ""


def test_the_requeue_sweep_deletes_a_prior_prepared_builds_branch(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    """A prior prepared build of the same feature left its kept worktree (with
    GuardKit's inner tree on autobuild/<feature>) and, from before this rule,
    its own branch. The fresh dispatch's sweep clears both."""
    repo, admitted = clone
    base = tmp_path / "wt"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(base))
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
    prior = "build-FEAT-AB12-20261003090000"
    prior_branch = f"forge/source/{prior}"
    _git(repo, "branch", prior_branch, admitted)
    outer = base / prior
    base.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(outer), admitted)
    inner = outer / ".guardkit" / "worktrees" / FEATURE
    inner.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "-b", f"autobuild/{FEATURE}", str(inner), admitted)

    calls: list[dict[str, Any]] = []
    result = _run(repo, _payload(admitted), calls)

    assert _lifecycle(result) == "completed"
    assert _git(repo, "branch", "--list", prior_branch) == ""
    assert _git(repo, "branch", "--list", SOURCE_BRANCH) == ""
    assert not outer.exists()


def test_the_sweep_clears_a_prior_branch_left_with_no_worktree_at_all(
    clone: tuple[Path, str], tmp_path: Path, monkeypatch
) -> None:
    """A prior prepared build's runner was stopped after it made its own
    branch and before GuardKit made any worktree: only the branch is left. A
    branch of another feature, and of a build still running, are left alone."""
    repo, admitted = clone
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(tmp_path / "wt"))
    monkeypatch.setenv(ar.RECEIPTS_DIR_ENV, str(tmp_path / "receipts"))
    prior_branch = "forge/source/build-FEAT-AB12-20261003080000"
    other_feature = "forge/source/build-FEAT-CD34-20261003080000"
    live_branch = "forge/source/build-FEAT-AB12-20261003070000"
    _git(repo, "branch", prior_branch, admitted)
    _git(repo, "branch", other_feature, admitted)
    _git(repo, "branch", live_branch, admitted)
    monkeypatch.setattr(
        ar,
        "_prior_build_status",
        lambda build_id: "RUNNING" if build_id.endswith("070000") else None,
    )

    calls: list[dict[str, Any]] = []
    result = _run(repo, _payload(admitted), calls)

    assert _lifecycle(result) == "completed"
    assert _git(repo, "branch", "--list", prior_branch) == ""
    assert _git(repo, "branch", "--list", other_feature) != ""
    assert _git(repo, "branch", "--list", live_branch) != ""
