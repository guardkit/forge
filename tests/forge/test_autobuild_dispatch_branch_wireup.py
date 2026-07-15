"""Branch/repo dispatch wire-up + placeholder loud-fail (B4 round-17 stretch).

Two coach findings from the first live gate-approved autobuild launch:

* FINDING 1 — the runner's branch-aware isolated-worktree path (DEFECT #19)
  only engages when the RUN_AUTOBUILD launch payload carries ``branch``, but
  the live dispatch never threaded it. The round-17 wire bytes carried only
  ``{build_id, context_entries, correlation_id, feature_id}`` while the
  consumed :class:`BuildQueuedPayload` carried ``branch`` (and ``repo``). The
  dispatcher now forwards both from the already-validated payload into the
  launch payload, so the worktree path engages on the live path.

* FINDING 2 — :func:`_build_placeholder_graph` used to be a noop→END graph
  served whenever the real graph failed to construct at import (DEFECT #18a
  dependency drift). It let every gate-approved build silently 'succeed'. The
  placeholder now emits a terminal ``failed`` lifecycle naming the original
  construction error, so a sidecar that cannot build its real graph fails every
  run LOUD (while still booting to report the failure).

Strategy (per the task non-negotiables): the dispatch path is driven for real
with in-memory NATS/langgraph stubs; the runner graph is driven against a
THROWAWAY git repo with only the guardkit subprocess stubbed (real ``git
worktree`` verbs run). The live api_test checkout is never touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from langchain_core.messages import HumanMessage
from nats_core.events import BuildQueuedPayload

from forge.cli._serve_async_task_starter import _synthesise_description
from forge.pipeline.dispatchers.autobuild_async import (
    AUTOBUILD_RUNNER_NAME,
    dispatch_autobuild_async,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.subagents import autobuild_runner as ar

# --- round-17 identifiers (the live gate-approved launch that surfaced both) ---
ROUND17_BUILD_ID = "build-FEAT-A058-20260715124214"
ROUND17_FEATURE_ID = "FEAT-A058"
ROUND17_CORR = "a2a4dcd8-2d35-479e-9a11-da4d6641a016"
PLANNING_BRANCH = f"planning/{ROUND17_CORR}"
ROUND17_REPO = "appmilla/api_test"

ROUND14_ENVELOPE = (
    Path(__file__).parent / "fixtures" / "round14_build_queued_660d487e.json"
)


# ---------------------------------------------------------------------------
# Minimal in-memory dispatch collaborators (stub NATS / langgraph)
# ---------------------------------------------------------------------------


@dataclass
class _CapturingStarter:
    """``AsyncTaskStarter`` that records the launch context and mints a task_id."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def astart_async_task(
        self, subagent_name: str, context: Mapping[str, Any]
    ) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-0001"

    def start_async_task(
        self, subagent_name: str, context: Mapping[str, Any]
    ) -> str:  # pragma: no cover - async path is the production one
        self.calls.append((subagent_name, dict(context)))
        return "task-0001"


@dataclass
class _NullForwardContextBuilder:
    """Returns an empty context — the branch/repo wire-up is orthogonal to it."""

    def build_for(
        self, *, stage: StageClass, build_id: str, feature_id: str | None
    ) -> list[Any]:
        return []


@dataclass
class _NullStageLogRecorder:
    def record_running(
        self,
        build_id: str,
        feature_id: str,
        stage: StageClass,
        details_json: Mapping[str, Any],
    ) -> None:
        return None


@dataclass
class _NullStateChannel:
    def initialise_autobuild_state(
        self,
        build_id: str,
        feature_id: str,
        task_id: str,
        correlation_id: str,
        lifecycle: str,
        wave_index: int,
        task_index: int,
    ) -> None:
        return None


async def _dispatch(**kwargs: Any) -> dict[str, Any]:
    """Drive the real dispatcher and return the captured launch context."""
    starter = _CapturingStarter()
    await dispatch_autobuild_async(
        build_id=kwargs.pop("build_id", ROUND17_BUILD_ID),
        feature_id=kwargs.pop("feature_id", ROUND17_FEATURE_ID),
        correlation_id=kwargs.pop("correlation_id", ROUND17_CORR),
        forward_context_builder=_NullForwardContextBuilder(),
        async_task_starter=starter,
        stage_log_recorder=_NullStageLogRecorder(),
        state_channel=_NullStateChannel(),
        **kwargs,
    )
    assert len(starter.calls) == 1
    subagent_name, context = starter.calls[0]
    assert subagent_name == AUTOBUILD_RUNNER_NAME
    return context


# ---------------------------------------------------------------------------
# FINDING 1 — dispatch threads branch/repo into the launch payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_and_repo_threaded_into_launch_payload() -> None:
    """The dispatcher forwards branch/repo verbatim into the launch context."""
    context = await _dispatch(branch=PLANNING_BRANCH, repo=ROUND17_REPO)
    assert context["branch"] == PLANNING_BRANCH
    assert context["repo"] == ROUND17_REPO
    # And they survive real launch-description serialisation onto the wire.
    description = _synthesise_description(AUTOBUILD_RUNNER_NAME, context)
    payload = json.loads(description.split("payload=", 1)[1])
    assert payload["branch"] == PLANNING_BRANCH
    assert payload["repo"] == ROUND17_REPO


@pytest.mark.asyncio
async def test_no_branch_case_stays_byte_compatible_with_pre_fix_payload() -> None:
    """The legacy / boot-rearm launch (no branch/repo) omits both keys.

    ``BuildQueuedPayload.branch`` is required-with-default ("main") and
    ``.repo`` is required, so on the LIVE consumer path both are always present
    — the None case at the dispatcher is only reachable from the boot-rearm
    resume launcher (which restores the row from SQLite and has no payload in
    scope). For that path the launch bytes must stay byte-compatible with the
    pre-fix shape the F2 CLI path proved: branch/repo keys entirely absent.
    """
    context = await _dispatch()  # no branch/repo passed
    assert "branch" not in context
    assert "repo" not in context
    # The runner's legacy shared-checkout fallback keys off exactly this
    # absence (payload.get("branch") is None).
    description = _synthesise_description(AUTOBUILD_RUNNER_NAME, context)
    payload = json.loads(description.split("payload=", 1)[1])
    assert "branch" not in payload
    assert "repo" not in payload


# ---------------------------------------------------------------------------
# Throwaway repo + guardkit stub (drive the runner graph end-to-end)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def throwaway_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "api_test"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "feature.yaml").write_text("id: FEAT-A058\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", PLANNING_BRANCH)  # planning branch NOT checked out
    return repo


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = [*lines, b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, exit_code: int) -> None:
        self.pid = 4242
        self.returncode = exit_code
        self.stdout = _FakeStdout([b"guardkit running\n"])

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


def _exec_stub(recorded: dict[str, Any], *, exit_code: int):
    real_exec = asyncio.create_subprocess_exec

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        prog = str(args[0]) if args else ""
        if prog.endswith("guardkit"):
            recorded["cwd"] = kwargs.get("cwd")
            return _FakeProc(exit_code)
        return await real_exec(*args, **kwargs)

    return _stub


# ---------------------------------------------------------------------------
# INTEGRATION — real dispatch code builds the launch payload from the round-17
# BuildQueuedPayload envelope, and the runner's worktree path now engages.
# ---------------------------------------------------------------------------


def test_round17_dispatch_engages_worktree_path(
    throwaway_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UPDATED round-17 expectation: worktree path engages on the live path.

    Build a :class:`BuildQueuedPayload` from the round-17 values, threaded onto
    the committed round-14 envelope SHAPE, drive the REAL dispatch code to
    construct the launch payload (as the consumer's launch closure does —
    branch=payload.branch, repo=payload.repo), synthesise the real launch
    description, then drive the runner graph. Pre-fix the branch-blind payload
    fell into the legacy shared-checkout mode; now guardkit runs in the ISOLATED
    worktree of the planning branch.
    """
    wt_base = tmp_path / "worktrees"
    monkeypatch.setenv(ar.FORGE_AUTOBUILD_WORKTREE_BASE_ENV, str(wt_base))

    # round-17 BuildQueuedPayload on the round-14 envelope shape.
    env = json.loads(ROUND14_ENVELOPE.read_text())
    raw = dict(env["payload"])
    raw.update(
        {
            "feature_id": ROUND17_FEATURE_ID,
            "correlation_id": ROUND17_CORR,
            "branch": PLANNING_BRANCH,
            "repo": ROUND17_REPO,
        }
    )
    payload = BuildQueuedPayload.model_validate(raw)

    # Real dispatch code builds the launch payload, forwarding branch/repo the
    # way the consumer's launch closure does.
    context = asyncio.run(
        _dispatch(
            build_id=ROUND17_BUILD_ID,
            feature_id=payload.feature_id,
            correlation_id=payload.correlation_id,
            branch=payload.branch,
            repo=payload.repo,
        )
    )
    description = _synthesise_description(AUTOBUILD_RUNNER_NAME, context)

    # Drive the runner graph against the throwaway repo (guardkit stubbed).
    recorded: dict[str, Any] = {}
    stub = _exec_stub(recorded, exit_code=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ar, "_resolve_repo_path", lambda payload: throwaway_repo)
        mp.setattr(ar, "_resolve_guardkit_path", lambda: Path("/usr/bin/guardkit"))
        mp.setattr(asyncio, "create_subprocess_exec", stub)
        graph = ar._build_runner_graph()
        result = asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=description)]})
        )

    expected_wt = (wt_base / ROUND17_BUILD_ID).resolve()
    assert recorded["cwd"] == str(expected_wt), (
        "post-fix the round-17 dispatch must engage the isolated worktree; got "
        f"cwd={recorded.get('cwd')!r}"
    )
    assert str(recorded["cwd"]) != str(throwaway_repo)
    assert not expected_wt.exists(), "success path removes the worktree"
    snap = (result.get("async_tasks") or {}).get(ROUND17_FEATURE_ID)
    assert snap and snap.get("lifecycle") == "completed"


# ---------------------------------------------------------------------------
# FINDING 2 — placeholder graph fails LOUD naming the construction error
# ---------------------------------------------------------------------------


def _force_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the REAL graph builder raise, without breaking the placeholder.

    ``_build_runner_graph`` calls ``add_conditional_edges`` (the placeholder
    does not), so raising there forces the except branch → placeholder build
    while leaving the placeholder's own construction intact.
    """
    from langgraph.graph import StateGraph

    def _boom(self: Any, *a: Any, **k: Any) -> Any:
        raise RuntimeError("simulated langgraph dependency drift")

    monkeypatch.setattr(StateGraph, "add_conditional_edges", _boom)


def test_placeholder_graph_fails_loud_naming_construction_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Isolate the module-scope capture (restored after the test).
    monkeypatch.setattr(ar, "_RUNNER_GRAPH_CONSTRUCTION_ERROR", None)
    _force_construction_failure(monkeypatch)

    graph = ar._build_runner_graph()  # falls back to the placeholder
    assert ar._RUNNER_GRAPH_CONSTRUCTION_ERROR is not None
    assert isinstance(ar._RUNNER_GRAPH_CONSTRUCTION_ERROR, RuntimeError)

    description = (
        "RUN_AUTOBUILD subagent=autobuild_runner "
        'payload={"feature_id": "FEAT-A058", "build_id": "b1"}'
    )
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        result = asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content=description)]})
        )

    # The channel carries a terminal failed lifecycle — never a silent success.
    snap = (result.get("async_tasks") or {}).get("FEAT-A058")
    assert snap and snap.get("lifecycle") == "failed"
    # And the failure NAMES the original construction error.
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "graph failed to construct at import" in joined
    assert "simulated langgraph dependency drift" in joined


def test_real_graph_path_unaffected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean build serves the real graph — no capture, no placeholder log."""
    monkeypatch.setattr(ar, "_RUNNER_GRAPH_CONSTRUCTION_ERROR", None)

    graph = ar._build_runner_graph()  # no forced failure
    assert ar._RUNNER_GRAPH_CONSTRUCTION_ERROR is None

    # Drive an unactionable payload: the REAL graph still reaches a terminal
    # failed via its own resolution/finalize path — NOT the placeholder node.
    with caplog.at_level(logging.WARNING, logger="forge.subagents.autobuild_runner"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ar, "_resolve_guardkit_path", lambda: None)
            result = asyncio.run(graph.ainvoke({"messages": []}))

    lifecycles = {
        snap.get("lifecycle") for snap in (result.get("async_tasks") or {}).values()
    }
    assert lifecycles == {"failed"}
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "graph failed to construct at import" not in joined
