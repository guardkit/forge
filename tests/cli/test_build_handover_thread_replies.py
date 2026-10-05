"""A feature handed over from Slack is answered in the thread it came from.

Register-projects design (5 October 2026), part 3, "Forge replies" acceptance
checks, driven through the connected route: the planning intake turns a
``build: FEAT-XXXX from <branch>`` queue command into the bytes the build
consumer reads; the build consumer (``pipeline_consumer.handle_message``)
checks them; ``dispatch_build`` admits the prepared feature against a real
bare remote and a real ledger, refuses or launches it. Exactly one anchored
notification answers each outcome:

* accepted — "Building FEAT-XXXX for <repo> from <branch> at <commit>", and
  only after the build was launched (after its approval gate, when wired);
* invalid bundle (prepared admission) — one refusal;
* unregistered repository (the consumer) — one refusal;
* feature already building (one build per feature) — one refusal;
* a build request without ``parent_request_id`` — nothing new at all.

Stand-ins: the NATS client (a recorder), the async-task starter (the
boundary to the runner) and, in two tests, the approval gate's decision.
Nothing touches a live service, a sandbox or a model.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildQueuedPayload, NotificationPayload

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.nats.pipeline_consumer import handle_message
from forge.adapters.nats.planning_consumer import (
    PlanningConsumerDeps,
    handle_planning_message,
    publish_build_request,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating, _serve_gate_activation
from forge.cli._serve_deps import (
    build_pipeline_consumer_deps,
    build_prepared_build_admission,
)
from forge.config.models import ForgeConfig
from forge.gating.wrappers import GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.planning.notifications import NOTIFICATION_SUBJECT
from forge.planning.run_store import SqlitePlanningRunStore

from tests.forge.pipeline.test_prepared_admission import (
    BRANCH,
    FEATURE,
    REPO,
    Project,
    bundle,
)

USER = "U-RICH"
THREAD = "1759660000.000300"
THREAD_2 = "1759660100.000400"


class _Bus:
    """The one NATS client: records every publish, in order."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None

    def replies(self) -> list[NotificationPayload]:
        return [
            NotificationPayload.model_validate(
                MessageEnvelope.model_validate_json(body).payload
            )
            for subject, body in self.published
            if subject == NOTIFICATION_SUBJECT
        ]

    def build_requests(self) -> list[bytes]:
        return [
            body
            for subject, body in self.published
            if subject.startswith("pipeline.build-queued.")
        ]


class _Starter:
    """The runner boundary. Notes how many answers had gone out at launch."""

    def __init__(self, bus: _Bus) -> None:
        self.bus = bus
        self.replies_at_launch: list[int] = []

    def start_async_task(self, subagent_name: str, context: dict[str, Any]) -> str:
        self.replies_at_launch.append(len(self.bus.replies()))
        return "task-handover"

    async def astart_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        return self.start_async_task(subagent_name, context)


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
def planning_store(tmp_path: Path) -> Iterator[SqlitePlanningRunStore]:
    cx = sqlite_connect.connect_writer(tmp_path / "planning.db")
    migrations.apply_at_boot(cx)
    try:
        yield SqlitePlanningRunStore(cx)
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
            "planning": {
                "target_repo_paths": {REPO: str(project.copy)},
                "default_target_repo": REPO,
            },
        }
    )


@pytest.fixture()
def bus() -> _Bus:
    return _Bus()


@pytest.fixture()
def starter(bus: _Bus) -> _Starter:
    return _Starter(bus)


@pytest.fixture()
def build_deps(
    tmp_path: Path,
    bus: _Bus,
    starter: _Starter,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
) -> Any:
    # The production composition: the thread answer is the default one, on
    # the same client every other publish uses.
    return build_pipeline_consumer_deps(
        bus,
        forge_config,
        persistence,
        async_task_starter=starter,
        record_build_rejection=lambda _cid, _reason: None,
        prepared_build_admission=build_prepared_build_admission(
            forge_config, git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt")
        ),
    )


@pytest.fixture()
def planning_deps(
    bus: _Bus,
    forge_config: ForgeConfig,
    planning_store: SqlitePlanningRunStore,
) -> PlanningConsumerDeps:
    async def _publish(build: BuildQueuedPayload) -> None:
        await publish_build_request(bus, build)

    return PlanningConsumerDeps(
        store=planning_store,
        publish_notification=AsyncMock(),
        planning_config=forge_config.planning,
        publish_build_queued=_publish,
    )


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acks = 0

    async def ack(self) -> None:
        self.acks += 1


def _slack_message(
    *, thread: str = THREAD, correlation_id: str = "plan-handover-0001"
) -> _Msg:
    """What jarvis publishes for ``build: FEAT-AB12 from feature/prepared``."""
    payload = {
        "stage": "planning",
        "request_text": f"build: {FEATURE} from {BRANCH}",
        "target_repo": None,
        "triggered_by": "jarvis",
        "originating_adapter": "slack",
        "originating_user": USER,
        "correlation_id": correlation_id,
        "parent_request_id": thread,
        "requested_at": datetime(2026, 10, 5, 15, 0, tzinfo=UTC).isoformat(),
        "queued_at": datetime(2026, 10, 5, 15, 0, tzinfo=UTC).isoformat(),
        "queue_command": {"verb": "build", "feature_id": FEATURE, "branch": BRANCH},
    }
    envelope = MessageEnvelope(
        source_id="jarvis",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload,
    )
    return _Msg(envelope.model_dump_json().encode("utf-8"))


async def _hand_over(
    planning_deps: PlanningConsumerDeps, build_deps: Any, bus: _Bus, **slack: Any
) -> _Msg:
    """Slack message → planning intake → build request bytes → build route."""
    before = len(bus.build_requests())
    await handle_planning_message(_slack_message(**slack), planning_deps)
    (request,) = bus.build_requests()[before:]
    msg = _Msg(request)
    await handle_message(msg, build_deps)
    return msg


def _direct(bus: _Bus, **overrides: Any) -> _Msg:
    """A build request put on the bus by some other caller."""
    values: dict[str, Any] = dict(
        feature_id=FEATURE,
        repo=REPO,
        branch=BRANCH,
        feature_yaml_path=f".guardkit/features/{FEATURE}.yaml",
        triggered_by="jarvis",
        originating_adapter="slack",
        originating_user=USER,
        correlation_id="corr-direct-0001",
        parent_request_id=THREAD,
        requested_at=datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
        queued_at=datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
    )
    values.update(overrides)
    payload = BuildQueuedPayload(**values)
    envelope = MessageEnvelope(
        source_id="forge",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=payload.correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    return _Msg(envelope.model_dump_json().encode("utf-8"))


def _rows(persistence: SqliteLifecyclePersistence) -> list[Any]:
    persistence.connection.row_factory = sqlite3.Row
    return persistence.connection.execute("SELECT * FROM builds").fetchall()


# ---------------------------------------------------------------------------
# Accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_accepted_hand_over_is_answered_once_after_the_launch(
    project: Project,
    bus: _Bus,
    starter: _Starter,
    planning_deps: PlanningConsumerDeps,
    build_deps: Any,
    persistence: SqliteLifecyclePersistence,
) -> None:
    source = project.commit_on(BRANCH, bundle())

    msg = await _hand_over(planning_deps, build_deps, bus)

    (reply,) = bus.replies()
    assert (
        reply.message == f"Building {FEATURE} for {REPO} from {BRANCH} at {source[:7]}"
    )
    assert reply.parent_request_id == THREAD and reply.thread_ts == THREAD
    assert reply.level == "info"
    # Launched first, answered after: no answer had gone out at launch.
    assert starter.replies_at_launch == [0]
    (row,) = _rows(persistence)
    assert row["source_commit"] == source
    assert row["repo"] == REPO and row["parent_request_id"] == THREAD
    # The build holds its place until it finishes; nothing about that changed.
    assert msg.acks == 0


@pytest.mark.asyncio
async def test_a_redelivered_hand_over_is_not_answered_twice(
    project: Project,
    bus: _Bus,
    starter: _Starter,
    planning_deps: PlanningConsumerDeps,
    build_deps: Any,
) -> None:
    project.commit_on(BRANCH, bundle())
    await _hand_over(planning_deps, build_deps, bus)
    (request,) = bus.build_requests()

    await handle_message(_Msg(request), build_deps)

    assert [r.message.split(" ")[0] for r in bus.replies()] == ["Building"]
    assert len(starter.replies_at_launch) == 1


@pytest.mark.asyncio
async def test_with_the_gate_wired_the_answer_waits_for_the_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project: Project,
    bus: _Bus,
    starter: _Starter,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    planning_deps: PlanningConsumerDeps,
) -> None:
    project.commit_on(BRANCH, bundle())
    seen_at_gate: list[int] = []

    async def _approved(**_kwargs: Any) -> Any:
        seen_at_gate.append(len(bus.replies()))
        return GateOutcome.RESUMED

    monkeypatch.setattr(_serve_deps_gating, "bound_gate_parts", lambda: object())
    monkeypatch.setattr(_serve_gate_activation, "maybe_gate_build", _approved)
    deps = build_pipeline_consumer_deps(
        bus,
        forge_config,
        persistence,
        async_task_starter=starter,
        gate_repository=object(),
        gate_state_machine=object(),
        record_build_rejection=lambda _cid, _reason: None,
        prepared_build_admission=build_prepared_build_admission(
            forge_config, git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt")
        ),
    )

    await _hand_over(planning_deps, deps, bus)

    assert seen_at_gate == [0], "nothing is said before the approval"
    assert [r.message.split(" ")[0] for r in bus.replies()] == ["Building"]


@pytest.mark.asyncio
async def test_a_build_declined_at_its_gate_is_never_said_to_be_building(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project: Project,
    bus: _Bus,
    starter: _Starter,
    forge_config: ForgeConfig,
    persistence: SqliteLifecyclePersistence,
    planning_deps: PlanningConsumerDeps,
) -> None:
    project.commit_on(BRANCH, bundle())

    async def _declined(**_kwargs: Any) -> Any:
        return GateOutcome.CANCELLED

    monkeypatch.setattr(_serve_deps_gating, "bound_gate_parts", lambda: object())
    monkeypatch.setattr(_serve_gate_activation, "maybe_gate_build", _declined)
    deps = build_pipeline_consumer_deps(
        bus,
        forge_config,
        persistence,
        async_task_starter=starter,
        gate_repository=object(),
        gate_state_machine=object(),
        record_build_rejection=lambda _cid, _reason: None,
        prepared_build_admission=build_prepared_build_admission(
            forge_config, git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt")
        ),
    )

    msg = await _hand_over(planning_deps, deps, bus)

    assert bus.replies() == []
    assert starter.replies_at_launch == []
    assert msg.acks == 1


# ---------------------------------------------------------------------------
# Refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invalid_bundle_is_answered_once(
    project: Project,
    bus: _Bus,
    starter: _Starter,
    planning_deps: PlanningConsumerDeps,
    build_deps: Any,
    persistence: SqliteLifecyclePersistence,
) -> None:
    files = bundle()
    files.pop("qa/pass-bar-seed-count-things.yaml")
    project.commit_on(BRANCH, files)

    msg = await _hand_over(planning_deps, build_deps, bus)

    (reply,) = bus.replies()
    assert reply.message.startswith(f"{FEATURE} was not started: ")
    assert "qa/pass-bar-seed-count-things.yaml" in reply.message
    assert reply.parent_request_id == THREAD and reply.level == "warning"
    assert _rows(persistence) == [] and starter.replies_at_launch == []
    assert msg.acks == 1


@pytest.mark.asyncio
async def test_an_unregistered_repository_is_answered_once_by_the_consumer(
    bus: _Bus,
    starter: _Starter,
    build_deps: Any,
    persistence: SqliteLifecyclePersistence,
) -> None:
    msg = _direct(bus, repo="synthetic/unregistered")

    await handle_message(msg, build_deps)

    (reply,) = bus.replies()
    assert reply.message.startswith(f"{FEATURE} was not started: ")
    assert "synthetic/unregistered is not registered" in reply.message
    assert reply.parent_request_id == THREAD
    assert msg.acks == 1
    assert _rows(persistence) == [] and starter.replies_at_launch == []
    # The refusal's own build-failed event is unchanged and comes first.
    subjects = [s for s, _ in bus.published]
    assert subjects.index(f"pipeline.build-failed.{FEATURE}") < subjects.index(
        NOTIFICATION_SUBJECT
    )


@pytest.mark.asyncio
async def test_a_feature_already_building_is_answered_once(
    project: Project,
    bus: _Bus,
    starter: _Starter,
    planning_deps: PlanningConsumerDeps,
    build_deps: Any,
) -> None:
    project.commit_on(BRANCH, bundle())
    await _hand_over(planning_deps, build_deps, bus)

    second = await _hand_over(
        planning_deps,
        build_deps,
        bus,
        thread=THREAD_2,
        correlation_id="plan-handover-0002",
    )

    first_reply, second_reply = bus.replies()
    assert first_reply.message.startswith("Building ")
    assert first_reply.parent_request_id == THREAD
    assert second_reply.message == (
        f"{FEATURE} was not started: another build of {FEATURE} is already in progress."
    )
    assert second_reply.parent_request_id == THREAD_2
    assert second.acks == 1
    assert len(starter.replies_at_launch) == 1


# ---------------------------------------------------------------------------
# Requests without a thread: nothing new
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_without_parent_request_id_gets_no_answer(
    project: Project, bus: _Bus, starter: _Starter, build_deps: Any
) -> None:
    project.commit_on(BRANCH, bundle())

    # Accepted, unregistered, already building: all silent without a thread.
    await handle_message(_direct(bus, parent_request_id=None), build_deps)
    await handle_message(
        _direct(
            bus,
            parent_request_id=None,
            repo="synthetic/unregistered",
            correlation_id="corr-direct-0002",
        ),
        build_deps,
    )
    await handle_message(
        _direct(bus, parent_request_id=None, correlation_id="corr-direct-0003"),
        build_deps,
    )

    assert len(starter.replies_at_launch) == 1
    assert bus.replies() == []
    assert not [s for s, _ in bus.published if s == NOTIFICATION_SUBJECT]
    failed = [
        json.loads(body)["payload"]["failure_reason"]
        for subject, body in bus.published
        if subject.startswith("pipeline.build-failed.")
    ]
    assert len(failed) == 2, "the refusals themselves are unchanged"


@pytest.mark.asyncio
async def test_a_request_from_another_adapter_gets_no_answer(
    project: Project, bus: _Bus, starter: _Starter, build_deps: Any
) -> None:
    """Jarvis chat's ``queue_build`` sets a parent_request_id that is its own
    dispatch id, not a Slack message: it is never answered in a thread."""
    project.commit_on(BRANCH, bundle())

    await handle_message(
        _direct(
            bus, originating_adapter="telegram", parent_request_id="dispatch-abc123"
        ),
        build_deps,
    )
    await handle_message(
        _direct(
            bus,
            originating_adapter="telegram",
            parent_request_id="dispatch-abc124",
            repo="synthetic/unregistered",
            correlation_id="corr-direct-0002",
        ),
        build_deps,
    )

    assert len(starter.replies_at_launch) == 1
    assert bus.replies() == []
