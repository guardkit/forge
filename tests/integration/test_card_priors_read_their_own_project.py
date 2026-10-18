"""A build's approval card reads priors from its own project's memory only.

Design "One sandbox per project", section 2.4a (18 October 2026), Proof: on
18 October a repair card for one project showed five records from another
project's memory, because the card's priors reader always searched one shared
project. Now the card reads only the memory name recorded on the build, and
only when the build's repository was granted that name at registration
(``memory_projects`` on its ``planning.sandboxes`` entry).

Every scenario runs the whole path a card takes: a planning run records the
project's memory name, the build row copies it, the REAL
:func:`~forge.cli._serve_gate_activation.maybe_gate_build` reads the row and
the repository's grant from a REAL validated ``ForgeConfig``, the REAL
:class:`~forge.adapters.fleet_memory.FleetMemoryPriorsReader` searches, the
REAL gate and approval publisher publish the card, and the test reads the
card's ``evidence_priors`` off the (in-memory) wire.

THE STORE IS A FAKE, AND SAYS SO. Forge's test suite has no way to stand up a
fleet-memory store of its own: its only store test
(``test_fleet_memory_priors_live.py``) reads the LIVE store, which these tests
must not write two projects' records into. So the narrowest fake is used: the
``fleet_memory.retrieval.search`` seam of the reader's backend, which holds one
recognisable outcome per project, answers a search only with the records of the
project the request names (as the real search does: ``project`` is required and
matched exactly), and records every search it is asked for. Everything above
that seam is the production code. All names are invented.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.fleet_memory.priors import (
    FleetMemoryPriorsConfig,
    FleetMemoryPriorsReader,
    _RetrievalBackend,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating
from forge.cli._serve_deps_gating import build_approval_gate_parts
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_gate_activation import maybe_gate_build
from forge.config.models import ForgeConfig
from forge.gating.identity import derive_request_id
from forge.gating.sqlite_adapters import build_sqlite_gate_adapters
from forge.gating.wrappers import GateOutcome
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.planning.run_store import SqlitePlanningRunStore

from .conftest import InMemoryNats

REPO_A = "acme/shop-a"
REPO_B = "acme/shop-b"
#: A repository with a sandbox of its own and no memory grant.
REPO_C = "acme/shop-c"
NAME_A = "shop_a"
NAME_B = "shop_b"
RECORD_A = "build_outcome:shop_a:TASK_A_ONLY"
RECORD_B = "build_outcome:shop_b:TASK_B_ONLY"
RECORD_GUARDKIT = "build_outcome:guardkit:TASK_SHARED_SHELF"
QUEUED_AT = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)
FROZEN = datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The fake store: one recognisable outcome per project, every search recorded
# ---------------------------------------------------------------------------


class _Request:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.project = kwargs["project"]


class _Store:
    """A store that answers per project and remembers every search."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {
            NAME_A: [{"natural_key": RECORD_A, "lessons": "shop a's own lesson"}],
            NAME_B: [{"natural_key": RECORD_B, "lessons": "shop b's own lesson"}],
            "guardkit": [{"natural_key": RECORD_GUARDKIT, "lessons": "the shared shelf"}],
        }
        self.searched: list[str] = []

    async def search(self, request: _Request, _store: Any) -> list[Any]:
        self.searched.append(request.project)
        await asyncio.sleep(0)  # let concurrent gates interleave
        return [
            SimpleNamespace(value=dict(value), score=0.8)
            for value in self.records.get(request.project, [])
        ]

    def backend(self) -> _RetrievalBackend:
        class _Context:
            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *_exc: Any) -> bool:
                return False

        return _RetrievalBackend(
            settings_cls=lambda **kwargs: SimpleNamespace(**kwargs),  # type: ignore[arg-type]
            store_context=lambda _settings: _Context(),
            request_cls=_Request,
            search=self.search,
        )


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def pool(tmp_path: Path):
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    persistence = SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")
    yield persistence
    cx.close()


@pytest.fixture(autouse=True)
def _reset_bound_parts():
    _serve_deps_gating._reset_for_tests()
    yield
    _serve_deps_gating._reset_for_tests()


@pytest.fixture()
def store() -> _Store:
    return _Store()


def _sandbox(name: str, port: int, memory_projects: list[str] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "sidecar_url": f"http://127.0.0.1:{port}",
        "runner_url": f"http://127.0.0.1:{port + 1}",
    }
    if memory_projects is not None:
        entry["memory_projects"] = memory_projects
    return entry


def _config(*, grant_a: list[str] | None = None, grant_b: list[str] | None = None) -> ForgeConfig:
    """Three repositories, each with a sandbox of its own; A and B granted
    their own names, C granted none."""
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "planning": {
                "target_repo_paths": {
                    REPO_A: "/srv/forge/shop-a",
                    REPO_B: "/srv/forge/shop-b",
                    REPO_C: "/srv/forge/shop-c",
                },
                "sandboxes": {
                    REPO_A: _sandbox("shop-a", 9011, grant_a if grant_a is not None else [NAME_A]),
                    REPO_B: _sandbox("shop-b", 9021, grant_b if grant_b is not None else [NAME_B]),
                    REPO_C: _sandbox("shop-c", 9031, None),
                },
            },
        }
    )


def _reader(store: _Store) -> FleetMemoryPriorsReader:
    return FleetMemoryPriorsReader(
        FleetMemoryPriorsConfig(
            enabled=True,
            pg_dsn="postgresql://reader@store.invalid:5432/memory",
            embed_url="http://embed.invalid:9000/v1",
            read_timeout_s=5.0,
        ),
        backend=store.backend(),
    )


def _queue(
    pool: SqliteLifecyclePersistence,
    *,
    repo: str,
    feature_id: str,
    correlation_id: str,
    memory_project: str | None,
    start_commit: str | None = None,
    minute: int = 0,
) -> str:
    """A planning run recording the project's memory name, then its build."""
    pool.connection.row_factory = sqlite3.Row
    runs = SqlitePlanningRunStore(pool.connection)
    if not correlation_id.startswith("fix-"):
        runs.record_queued(
            correlation_id=correlation_id,
            originating_user="U1",
            expected_approver="U1",
            request_text="a sentence",
            triggered_by="cli",
            target_repo=repo,
        )
        if memory_project is not None:
            runs.record_memory_project(correlation_id, memory_project=memory_project)
        if start_commit is not None:
            runs.record_start_point(
                correlation_id, start_commit=start_commit, target_branch="main"
            )
    queued_at = QUEUED_AT + timedelta(minutes=minute)
    payload = SimpleNamespace(
        feature_id=feature_id,
        repo=repo,
        branch="main",
        feature_yaml_path=f"/srv/forge/features/{feature_id}.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="forge-cli",
        originating_user="U1",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=queued_at,
        requested_at=queued_at,
    )
    return pool.record_pending_build(payload)


async def _card_priors(
    pool: SqliteLifecyclePersistence,
    nats: InMemoryNats,
    reader: FleetMemoryPriorsReader,
    config: ForgeConfig,
    build_id: str,
    feature_id: str,
    correlation_id: str,
) -> list[str]:
    """Gate the build through maybe_gate_build, read the published card's
    evidence_priors, then answer the card so the gate finishes."""
    _publisher, emitter = build_publisher_and_emitter(nats)
    parts = build_approval_gate_parts(
        nats, config, priors_reader=reader, emitter=emitter,
        repository=None, bridge_registry=None,
    )
    repo, sm = build_sqlite_gate_adapters(pool, clock=lambda: FROZEN)
    task = asyncio.create_task(
        maybe_gate_build(
            parts=parts,
            sqlite_pool=pool,
            gate_repository=repo,
            gate_state_machine=sm,
            build_id=build_id,
            feature_id=feature_id,
            correlation_id=correlation_id,
            clock=lambda: FROZEN,
            forge_config=config,
        )
    )
    subject = f"agents.approval.forge.{build_id}"
    mirror = f"{subject}.response"
    for _ in range(2000):
        if nats.published.get(subject) and nats.subscribers.get(mirror):
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - a hang is a failure, said plainly
        task.cancel()
        raise AssertionError(f"no card was published for {build_id}")
    card = json.loads(nats.published[subject][0])["payload"]
    await nats.deliver_response(
        build_id=build_id,
        request_id=derive_request_id(build_id=build_id, stage_label="autobuild", attempt_count=0),
        decision="reject",
        notes="test over",
    )
    outcome = await asyncio.wait_for(task, timeout=5.0)
    assert outcome is GateOutcome.CANCELLED
    return [prior["entity_id"] for prior in card["details"]["evidence_priors"]]


# ---------------------------------------------------------------------------
# Each project's card holds its own record only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_card_holds_its_own_projects_record_and_not_the_others(pool, store) -> None:
    config, reader = _config(), _reader(store)
    a = _queue(pool, repo=REPO_A, feature_id="FEAT-A1", correlation_id="corr-a1", memory_project=NAME_A)
    b = _queue(pool, repo=REPO_B, feature_id="FEAT-B1", correlation_id="corr-b1", memory_project=NAME_B, minute=1)

    priors_a = await _card_priors(pool, InMemoryNats(), reader, config, a, "FEAT-A1", "corr-a1")
    priors_b = await _card_priors(pool, InMemoryNats(), reader, config, b, "FEAT-B1", "corr-b1")

    assert priors_a == [RECORD_A]
    assert priors_b == [RECORD_B]
    assert store.searched == [NAME_A, NAME_B]


@pytest.mark.asyncio
async def test_alternating_builds_each_get_only_their_own(pool, store) -> None:
    config, reader = _config(), _reader(store)
    seen: list[tuple[str, list[str]]] = []
    for index, (repo, name) in enumerate([(REPO_A, NAME_A), (REPO_B, NAME_B)] * 2):
        feature, corr = f"FEAT-ALT{index}", f"corr-alt-{index}"
        build = _queue(pool, repo=repo, feature_id=feature, correlation_id=corr,
                       memory_project=name, minute=index)
        seen.append((name, await _card_priors(pool, InMemoryNats(), reader, config, build, feature, corr)))
    assert seen == [
        (NAME_A, [RECORD_A]), (NAME_B, [RECORD_B]), (NAME_A, [RECORD_A]), (NAME_B, [RECORD_B]),
    ]
    assert store.searched == [NAME_A, NAME_B, NAME_A, NAME_B]


@pytest.mark.asyncio
async def test_concurrent_builds_each_get_only_their_own(pool, store) -> None:
    config, reader = _config(), _reader(store)
    a = _queue(pool, repo=REPO_A, feature_id="FEAT-CA", correlation_id="corr-ca", memory_project=NAME_A)
    b = _queue(pool, repo=REPO_B, feature_id="FEAT-CB", correlation_id="corr-cb", memory_project=NAME_B, minute=1)
    nats = InMemoryNats()

    priors_a, priors_b = await asyncio.gather(
        _card_priors(pool, nats, reader, config, a, "FEAT-CA", "corr-ca"),
        _card_priors(pool, nats, reader, config, b, "FEAT-CB", "corr-cb"),
    )

    assert priors_a == [RECORD_A]
    assert priors_b == [RECORD_B]
    assert sorted(store.searched) == [NAME_A, NAME_B]


# ---------------------------------------------------------------------------
# No grant, no search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_build_with_no_recorded_name_gets_no_priors_and_no_search(pool, store) -> None:
    config, reader = _config(), _reader(store)
    build = _queue(pool, repo=REPO_A, feature_id="FEAT-N1", correlation_id="corr-n1", memory_project=None)
    assert pool.read_memory_project(build) is None

    assert await _card_priors(pool, InMemoryNats(), reader, config, build, "FEAT-N1", "corr-n1") == []
    assert store.searched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [NAME_A, "guardkit"])
async def test_a_project_declaring_another_name_gets_no_priors_and_no_search(
    pool, store, declared: str
) -> None:
    """B declares A's name, or the old shared name: its declaration grants
    nothing, and B's repository was granted only shop_b (Codex R3)."""
    config, reader = _config(), _reader(store)
    build = _queue(pool, repo=REPO_B, feature_id="FEAT-D1", correlation_id="corr-d1", memory_project=declared)
    assert pool.read_memory_project(build) == declared

    assert await _card_priors(pool, InMemoryNats(), reader, config, build, "FEAT-D1", "corr-d1") == []
    assert store.searched == []


@pytest.mark.asyncio
async def test_a_b_build_already_recorded_with_a_s_name_gets_no_priors(pool, store) -> None:
    config, reader = _config(), _reader(store)
    build = _queue(pool, repo=REPO_B, feature_id="FEAT-R1", correlation_id="corr-r1", memory_project=NAME_B)
    pool.connection.execute("UPDATE builds SET memory_project = ? WHERE build_id = ?", (NAME_A, build))
    pool.connection.commit()

    assert await _card_priors(pool, InMemoryNats(), reader, config, build, "FEAT-R1", "corr-r1") == []
    assert store.searched == []


@pytest.mark.asyncio
async def test_a_repository_without_memory_projects_gets_no_priors_and_no_search(pool, store) -> None:
    config, reader = _config(), _reader(store)
    build = _queue(pool, repo=REPO_C, feature_id="FEAT-C1", correlation_id="corr-c1", memory_project="shop_c")

    assert await _card_priors(pool, InMemoryNats(), reader, config, build, "FEAT-C1", "corr-c1") == []
    assert store.searched == []


@pytest.mark.asyncio
async def test_without_the_settings_no_repository_is_granted_anything(pool, store) -> None:
    """maybe_gate_build with no ForgeConfig grants nothing: no search."""
    config, reader = _config(), _reader(store)
    build = _queue(pool, repo=REPO_A, feature_id="FEAT-S1", correlation_id="corr-s1", memory_project=NAME_A)
    nats = InMemoryNats()
    _publisher, emitter = build_publisher_and_emitter(nats)
    parts = build_approval_gate_parts(nats, config, priors_reader=reader, emitter=emitter,
                                      repository=None, bridge_registry=None)
    gate_repo, sm = build_sqlite_gate_adapters(pool, clock=lambda: FROZEN)
    task = asyncio.create_task(maybe_gate_build(
        parts=parts, sqlite_pool=pool, gate_repository=gate_repo, gate_state_machine=sm,
        build_id=build, feature_id="FEAT-S1", correlation_id="corr-s1", clock=lambda: FROZEN,
    ))
    subject = f"agents.approval.forge.{build}"
    for _ in range(2000):
        if nats.published.get(subject) and nats.subscribers.get(f"{subject}.response"):
            break
        await asyncio.sleep(0)
    card = json.loads(nats.published[subject][0])["payload"]
    await nats.deliver_response(
        build_id=build,
        request_id=derive_request_id(build_id=build, stage_label="autobuild", attempt_count=0),
        decision="reject",
    )
    await asyncio.wait_for(task, timeout=5.0)
    assert card["details"]["evidence_priors"] == []
    assert store.searched == []


# ---------------------------------------------------------------------------
# A repair reads the memory of the build it repairs, and only that
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repair_of_a_s_build_gets_a_s_record(pool, store) -> None:
    config, reader = _config(), _reader(store)
    source = _queue(pool, repo=REPO_A, feature_id="FEAT-F1", correlation_id="corr-f1",
                    memory_project=NAME_A, start_commit="a" * 40)
    repair = _queue(pool, repo=REPO_A, feature_id="FEAT-F1", correlation_id=f"fix-{source}",
                    memory_project=None, minute=5)

    assert pool.read_memory_project(repair) == NAME_A
    assert await _card_priors(pool, InMemoryNats(), reader, config, repair, "FEAT-F1", f"fix-{source}") == [RECORD_A]
    assert store.searched == [NAME_A]


@pytest.mark.asyncio
async def test_a_repair_naming_another_repositorys_build_gets_nothing(pool, store) -> None:
    config, reader = _config(), _reader(store)
    source = _queue(pool, repo=REPO_A, feature_id="FEAT-F2", correlation_id="corr-f2", memory_project=NAME_A)
    repair = _queue(pool, repo=REPO_B, feature_id="FEAT-F2B", correlation_id=f"fix-{source}",
                    memory_project=None, minute=5)

    assert pool.read_memory_project(repair) is None
    assert await _card_priors(pool, InMemoryNats(), reader, config, repair, "FEAT-F2B", f"fix-{source}") == []
    assert store.searched == []
