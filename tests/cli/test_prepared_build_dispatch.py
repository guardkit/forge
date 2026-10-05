"""A prepared feature through the normal build route: the dispatch half.

4 October 2026 (project initialisation design, Part 6, points 2, 4 and 5).
``dispatch_build`` is driven against a REAL ledger in a temporary directory,
with the production admission built by ``build_prepared_build_admission``
over the coordinator's own git runner and a bare remote on disk. Only the
async-task starter (the boundary to the runner) and the NATS client are
stand-ins. Nothing touches a live service, a sandbox or a model.

What is held down here:

* a build with no planning run is admitted at its branch's commit, and the
  row records the target branch, ``start_commit`` = ``source_commit`` = that
  commit, the memory name and the launch settings; the launch carries the
  commit, so the runner builds it even after the branch moves;
* a refusal writes no row, notes the refusal, publishes ``build-failed`` and
  acknowledges;
* a planned build keeps copying its planning run's facts and launches exactly
  as before, and a row written ahead is never re-admitted;
* nothing on this route dispatches a planning capability.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps import (
    build_pipeline_consumer_deps,
    build_prepared_build_admission,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.planning.run_store import SqlitePlanningRunStore

from tests.forge.pipeline.test_prepared_admission import (
    BRANCH,
    FEATURE,
    REPO,
    Project,
    bundle,
    config_text,
)

CID = "corr-prepared-0001"


class _StubNatsClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None


class _RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_async_task(self, subagent_name: str, context: dict[str, Any]) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-prepared"

    async def astart_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-prepared"


@pytest.fixture()
def persistence(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    return Project(tmp_path / "project")


@pytest.fixture()
def forge_config(tmp_path: Path, project: Project) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {"target_repo_paths": {REPO: str(project.copy)}},
        }
    )


@pytest.fixture(autouse=True)
def _no_planning_capability(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any planning capability reached on this route fails the test loudly."""
    from forge.planning import driver

    calls: list[str] = []

    def _refuse(*_a: Any, **_k: Any) -> Any:
        calls.append("planning")
        raise AssertionError("a planning capability was dispatched")

    monkeypatch.setattr(driver.PlanningRunDriver, "__init__", _refuse)
    monkeypatch.setattr(driver.PlanningRunDriver, "drive", _refuse)
    return calls


def _payload(**overrides: Any) -> SimpleNamespace:
    values = dict(
        feature_id=FEATURE,
        repo=REPO,
        branch=BRANCH,
        feature_yaml_path=f".guardkit/features/{FEATURE}.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="jarvis",
        originating_adapter="slack",
        originating_user="tester",
        correlation_id=CID,
        parent_request_id=None,
        queued_at=datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _row(persistence: SqliteLifecyclePersistence, correlation_id: str = CID) -> Any:
    persistence.connection.row_factory = sqlite3.Row
    return persistence.connection.execute(
        "SELECT * FROM builds WHERE correlation_id = ?", (correlation_id,)
    ).fetchone()


class _Spy:
    """Wraps the production admission and counts calls."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    async def __call__(self, payload: Any) -> Any:
        self.calls += 1
        return await self.inner(payload)


def _deps(
    client: _StubNatsClient,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    starter: _RecordingStarter,
    admission: Any,
    rejections: list[tuple[str, str]],
) -> Any:
    return build_pipeline_consumer_deps(
        client,
        forge_config,
        persistence,
        async_task_starter=starter,
        record_build_rejection=lambda cid, reason: rejections.append((cid, reason)),
        prepared_build_admission=admission,
    )


def _admission(forge_config: ForgeConfig, tmp_path: Path) -> _Spy:
    return _Spy(
        build_prepared_build_admission(
            forge_config, git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt")
        )
    )


# ---------------------------------------------------------------------------
# Admitted, recorded, launched at the admitted commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prepared_feature_is_admitted_recorded_and_launched_at_its_commit(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    _no_planning_capability: list[str],
) -> None:
    source = project.commit_on(
        BRANCH, bundle(config=config_text(settings=("PROJECT_REGION",)))
    )
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    admission = _admission(forge_config, tmp_path)
    deps = _deps(client, forge_config, persistence, starter, admission, rejections)
    acks: list[str] = []

    async def _ack() -> None:
        acks.append("ack")

    await deps.dispatch_build(_payload(), _ack)
    # The branch moves after admission: nothing about this build changes.
    project.commit_on(BRANCH, {"later.md": "later\n"})

    row = _row(persistence)
    assert admission.calls == 1
    assert row["target_branch"] == "main"
    assert row["start_commit"] == source
    assert row["source_commit"] == source
    assert row["memory_project"] == "synthetic_project"
    assert json.loads(row["launch_settings"]) == ["PROJECT_REGION"]
    assert row["feature_yaml_path"] == f".guardkit/features/{FEATURE}.yaml"

    (_name, context), = starter.calls
    assert context["source_commit"] == source
    assert context["memory_project"] == "synthetic_project"
    assert context["launch_settings"] == ["PROJECT_REGION"]
    assert context["branch"] == BRANCH
    assert persistence.read_source_commit(row["build_id"]) == source
    # The merge press reads the deploy profile and launch settings, stamps
    # declared_at and has the sidecar enforce against, this recorded start
    # point: for a prepared build it is the admitted commit itself (R1).
    start = persistence.read_start_point(row["build_id"])
    assert start.recorded and start.start_commit == source
    assert start.target_branch == "main"
    assert rejections == [] and acks == []
    assert _no_planning_capability == []
    assert not [s for s, _ in client.published if s.startswith("agents.command.")]


@pytest.mark.asyncio
async def test_a_refused_prepared_feature_writes_no_row_and_says_why(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    files = bundle()
    files.pop(f"qa/pass-bar-seed-count-things.yaml")
    project.commit_on(BRANCH, files)
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )
    acks: list[str] = []

    async def _ack() -> None:
        acks.append("ack")

    await deps.dispatch_build(_payload(), _ack)

    assert _row(persistence) is None
    assert starter.calls == []
    assert acks == ["ack"]
    ((cid, reason),) = rejections
    assert cid == CID and "qa/pass-bar-seed-count-things.yaml" in reason
    failed = [body for subject, body in client.published if "build-failed" in subject]
    assert failed and b"qa/pass-bar-seed-count-things.yaml" in failed[0]


@pytest.mark.asyncio
async def test_a_missing_memory_name_refuses_the_prepared_feature(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    project.commit_on(BRANCH, bundle(config=config_text(memory=None)))
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )

    async def _ack() -> None:
        return None

    await deps.dispatch_build(_payload(), _ack)

    assert _row(persistence) is None
    assert "memory" in rejections[0][1]


# ---------------------------------------------------------------------------
# Planned builds, and rows written ahead, are unchanged
# ---------------------------------------------------------------------------


def _planning_run(persistence: SqliteLifecyclePersistence) -> None:
    persistence.connection.row_factory = sqlite3.Row
    store = SqlitePlanningRunStore(persistence.connection)
    store.record_queued(
        correlation_id=CID,
        originating_user="U1",
        expected_approver="U1",
        request_text="a sentence",
        triggered_by="cli",
        target_repo=REPO,
    )
    store.record_start_point(CID, start_commit="a" * 40, target_branch="main")
    store.record_memory_project(CID, memory_project="planned_memory")
    store.record_launch_settings(CID, names=["PLANNED_SETTING"])


@pytest.mark.asyncio
async def test_a_planned_build_keeps_its_planning_runs_facts_and_launch(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    _no_planning_capability: list[str],
) -> None:
    _planning_run(persistence)
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    admission = _admission(forge_config, tmp_path)
    deps = _deps(client, forge_config, persistence, starter, admission, rejections)

    async def _ack() -> None:
        return None

    payload = _payload(branch=f"planning/{CID}", triggered_by="forge-internal")
    await deps.dispatch_build(payload, _ack)

    row = _row(persistence)
    assert admission.calls == 0
    assert row["start_commit"] == "a" * 40
    assert row["target_branch"] == "main"
    assert row["memory_project"] == "planned_memory"
    assert json.loads(row["launch_settings"]) == ["PLANNED_SETTING"]
    assert row["source_commit"] is None
    (_name, context), = starter.calls
    assert "source_commit" not in context
    assert set(context) == {
        "build_id",
        "feature_id",
        "correlation_id",
        "context_entries",
        "lifecycle_emitter",
        "branch",
        "repo",
        "memory_project",
        "launch_settings",
    }
    assert _no_planning_capability == []


@pytest.mark.asyncio
async def test_without_the_admission_wired_nothing_is_admitted_or_recorded(
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(client, forge_config, persistence, starter, None, rejections)

    async def _ack() -> None:
        return None

    await deps.dispatch_build(_payload(), _ack)

    row = _row(persistence)
    assert row["start_commit"] is None and row["source_commit"] is None
    (_name, context), = starter.calls
    assert "source_commit" not in context


@pytest.mark.asyncio
async def test_a_row_written_ahead_is_never_re_admitted(
    tmp_path: Path,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    payload = _payload()
    persistence.record_pending_build(payload)  # what `forge queue` does
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    admission = _admission(forge_config, tmp_path)
    deps = _deps(client, forge_config, persistence, starter, admission, rejections)

    async def _ack() -> None:
        return None

    await deps.dispatch_build(payload, _ack)

    assert admission.calls == 0
    assert _row(persistence)["source_commit"] is None


@pytest.mark.asyncio
async def test_an_unregistered_repository_is_refused_by_name(
    tmp_path: Path,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )

    async def _ack() -> None:
        return None

    await deps.dispatch_build(_payload(repo="someone/else"), _ack)

    assert _row(persistence) is None
    assert "someone/else is not registered" in rejections[0][1]


@pytest.mark.asyncio
async def test_the_boot_reconcile_composition_wires_the_admission_too(
    monkeypatch: pytest.MonkeyPatch,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    """The boot-time reconcile builds its own consumer deps; the admission is
    wired there as well, so it never fails open on that path."""
    from forge.cli import _serve_deps
    from forge.cli._serve_production import _build_consumer_reconcile_seam

    seen: dict[str, Any] = {}
    real = _serve_deps.build_pipeline_consumer_deps

    def _capture(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_serve_deps, "build_pipeline_consumer_deps", _capture)

    await _build_consumer_reconcile_seam(
        persistence, forge_config, _RecordingStarter()
    )(_StubNatsClient())

    assert callable(seen.get("prepared_build_admission"))


@pytest.mark.asyncio
async def test_a_prepared_payload_marked_mode_c_is_refused(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    """A prepared feature is a whole feature, not a single-task fix."""
    project.commit_on(BRANCH, bundle())
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )

    async def _ack() -> None:
        return None

    await deps.dispatch_build(_payload(mode="mode-c", task_id="TASK-AB12-001"), _ack)

    assert _row(persistence) is None
    assert starter.calls == []
    assert "single-task fix (mode-c)" in rejections[0][1]


# ---------------------------------------------------------------------------
# A prepared submission names the one feature file (Codex review round 1, R4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong",
    [
        lambda copy: str(copy / ".guardkit" / "features" / "FEAT-OTHER.yaml"),
        lambda copy: str(copy.parent / "another-repo" / ".guardkit" / "features" / f"{FEATURE}.yaml"),
        lambda copy: f".guardkit/features/FEAT-OTHER.yaml",
        lambda copy: f" .guardkit/features/{FEATURE}.yaml",
        lambda copy: str(copy / ".guardkit" / "features" / f"{FEATURE}.yaml") + " ",
    ],
    ids=[
        "absolute-wrong-feature",
        "absolute-wrong-repository",
        "relative-wrong-feature",
        "relative-leading-space",
        "absolute-trailing-space",
    ],
)
async def test_a_prepared_feature_file_must_be_its_own_in_the_registered_checkout(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    wrong: Any,
    request: pytest.FixtureRequest,
) -> None:
    project.commit_on(BRANCH, bundle())
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )

    async def _ack() -> None:
        return None

    await deps.dispatch_build(_payload(feature_yaml_path=wrong(project.copy)), _ack)

    assert _row(persistence) is None
    assert starter.calls == []
    assert f".guardkit/features/{FEATURE}.yaml" in rejections[0][1]
    if "space" not in request.node.callspec.id:
        assert "the one file a build of" in rejections[0][1]
    else:
        assert "begins or ends with a space" in rejections[0][1]


@pytest.mark.asyncio
async def test_a_prepared_feature_file_given_as_its_absolute_path_is_admitted(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    project.commit_on(BRANCH, bundle())
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    deps = _deps(
        client, forge_config, persistence, starter, _admission(forge_config, tmp_path), rejections
    )

    async def _ack() -> None:
        return None

    absolute = str(project.copy / ".guardkit" / "features" / f"{FEATURE}.yaml")
    await deps.dispatch_build(_payload(feature_yaml_path=absolute), _ack)

    assert rejections == []
    assert _row(persistence) is not None


@pytest.mark.asyncio
async def test_a_planned_build_keeps_todays_acceptance_of_an_absolute_path(
    tmp_path: Path,
    project: Project,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> None:
    """A planned build never meets the prepared check: an absolute path
    elsewhere (the factory's own planning trigger writes one) is accepted."""
    _planning_run(persistence)
    client, starter, rejections = _StubNatsClient(), _RecordingStarter(), []
    admission = _admission(forge_config, tmp_path)
    deps = _deps(client, forge_config, persistence, starter, admission, rejections)

    async def _ack() -> None:
        return None

    elsewhere = str(tmp_path / "planning-worktree" / ".guardkit" / "features" / "FEAT-X.yaml")
    payload = _payload(
        branch=f"planning/{CID}", triggered_by="forge-internal", feature_yaml_path=elsewhere
    )
    await deps.dispatch_build(payload, _ack)

    assert admission.calls == 0
    assert rejections == []
    assert _row(persistence) is not None
