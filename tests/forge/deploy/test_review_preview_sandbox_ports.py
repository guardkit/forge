"""Each sandbox's previews use that sandbox's own preview ports (18 October 2026).

Found live: with one sandbox per project, a project in a sandbox of its own
(slot 1, which publishes previews on 8960 and 8961) was given port 8950, the
shared sandbox's port, because every preview took its ports from its
project's deploy profile. Its preview listened on a port its sandbox does not
publish, and its card's link opened the shared sandbox's preview instead.

The rule these tests hold to (:func:`sandbox_preview`): a project's profile
cannot know which sandbox it lands in, so a sandbox that names its own
``preview_ports`` REPLACES the profile's published ports with them. Slot *i*
uses ``preview_ports[i]`` and keeps the profile's internal port; there are as
many slots as the smaller of the two counts; the card's link names the
sandbox's port. A sandbox of its own that names none is refused; the shared
sandbox keeps its profiles exactly as before, except a port another sandbox
names as its own. The sandbox's own link check reaches the same ports.

Everything is a fake: the venues, the deploy dispatch and the clock (the
same fakes as ``test_review_preview.py``). Every name and address is made up.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.config.models import PlanningConfig, SandboxEntry
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.review_preview import (
    PreviewLeaseStore,
    PreviewRequest,
    ReviewPreviewService,
    build_review_preview_service,
    is_slots_busy,
    sandbox_preview,
)
from forge.lifecycle import migrations
from tests.forge.deploy.test_review_preview import (
    BASE_URL,
    SHA_CAND,
    SHA_FETCHED,
    SHA_MAIN,
    TREE_CAND,
    FakeClock,
    FakeDispatch,
    FakeVenue,
)

PUBLIC_HOST = "previews.example.test"
SHARED = "acme/shared-notes"
PRIVATE = "acme/private-notes"
LATE = "acme/registered-before"

SHARED_ENTRY = SandboxEntry(
    name="shared-sbx",
    sidecar_url="http://127.0.0.1:8925",
    runner_url="http://127.0.0.1:8924",
)
PRIVATE_ENTRY = SandboxEntry(
    name="private-notes",
    sidecar_url="http://192.0.2.10:8935",
    runner_url="http://192.0.2.10:8934",
    git_door="http://192.0.2.1:31831/",
    preview_ports=[8960, 8961],
)
#: A sandbox of its own registered before its preview ports were recorded.
LATE_ENTRY = SandboxEntry(
    name="registered-before",
    sidecar_url="http://192.0.2.10:8945",
    runner_url="http://192.0.2.10:8944",
    git_door="http://192.0.2.1:31832/",
)
SANDBOXES = {SHARED: SHARED_ENTRY, PRIVATE: PRIVATE_ENTRY, LATE: LATE_ENTRY}


def _profile(env_id: str, ports: tuple[int, ...], *, internal: tuple[int, ...] | None = None, **extra: Any):
    """A profile with one slot per port and no public_url, so the link comes
    from ``review_preview.public_host`` and the slot's port. Internal ports
    are 10000 above the published ones unless given."""
    inside = internal or tuple(port + 10000 for port in ports)
    return parse_deploy_profile(
        {
            "env_id": env_id,
            "compose": {"file": "compose.yml", "script": "deploy/deploy.sh"},
            "preview": {
                "enabled": True,
                "slots": [
                    {"port": port, "internal_port": inner, **extra}
                    for port, inner in zip(ports, inside)
                ],
            },
        }
    )


def _request(project: str, build: str, feature: str) -> PreviewRequest:
    return PreviewRequest(
        project=project,
        feature_id=feature,
        build_id=build,
        branch=f"autobuild/{feature}",
        candidate_sha=SHA_CAND,
        candidate_tree=TREE_CAND,
        expect_main_sha=SHA_MAIN,
        target_branch="main",
        preview_base_sha=SHA_FETCHED,
    )


@pytest.fixture
def connection(tmp_path: Path):
    cx = connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


class Estate:
    """Two sandboxes on one machine: the shared one and a project's own."""

    def __init__(self, tmp_path: Path, connection: sqlite3.Connection, *, sandboxes: Any) -> None:
        self.clock = FakeClock()
        self.venues = {SHARED: FakeVenue(), PRIVATE: FakeVenue(), LATE: FakeVenue()}
        self.dispatch = FakeDispatch()
        self.profiles = {
            SHARED: _profile("shared", (8950, 8951)),
            # As the live fixture: its profile names the shared ports.
            PRIVATE: _profile("private", (8950, 8951)),
            LATE: _profile("late", (8970, 8971)),
        }
        self.store = PreviewLeaseStore(connection)
        leases = iter(f"pv{n:012x}" for n in range(1, 100))
        runs = iter(f"pvrun-{n:012x}" for n in range(1, 100))
        self.service = ReviewPreviewService(
            store=self.store,
            profile_reader=lambda project: self.profiles[project],
            venue_for=lambda project: self.venues[project],
            deploy_dispatcher=self.dispatch,
            repo_paths={SHARED: "/clone", PRIVATE: "/clone", LATE: "/clone"},
            evidence_root=tmp_path / "evidence",
            evidence_base_url=BASE_URL,
            public_host=PUBLIC_HOST,
            clock=self.clock,
            sleep=self.clock.sleep,
            token_factory=lambda: "the-right-token",
            lease_id_factory=lambda: next(leases),
            run_id_factory=lambda: next(runs),
            blocking_sleep=lambda _s: None,
            sandboxes=sandboxes,
        )

    def calls_for(self, project: str) -> list[dict[str, Any]]:
        return [c for c in self.dispatch.calls if c["issued_env"]["COMPOSE_PROJECT_NAME"].startswith(
            "private" if project == PRIVATE else "shared"
        )]


@pytest.fixture
def estate(tmp_path: Path, connection: sqlite3.Connection) -> Estate:
    return Estate(tmp_path, connection, sandboxes=lambda: SANDBOXES)


def _link(evidence: Any) -> str:
    return str(evidence.app_url).split("?")[0]


# ---------------------------------------------------------------------------
# Two sandboxes previewing at once


@pytest.mark.asyncio
async def test_two_sandboxes_previewing_at_once_each_get_their_own_port_and_slot(
    estate: Estate,
) -> None:
    """Both profiles name 8950/8951; the project in slot 1 previews on 8960."""
    shared, private = await asyncio.gather(
        estate.service.prepare(_request(SHARED, "build-s1", "FEAT-5001")),
        estate.service.prepare(_request(PRIVATE, "build-p1", "FEAT-9001")),
    )
    assert shared.state == private.state == "ready", (shared.failure_reason, private.failure_reason)
    # Each is the first slot of its own sandbox, on that sandbox's own port.
    by_project = {lease.project: lease for lease in estate.store.live()}
    assert (by_project[SHARED].slot, by_project[SHARED].port) == (0, 8950)
    assert (by_project[PRIVATE].slot, by_project[PRIVATE].port) == (0, 8960)
    # The card links name those ports, never the other sandbox's.
    assert _link(shared) == f"https://{PUBLIC_HOST}:8950/"
    assert _link(private) == f"https://{PUBLIC_HOST}:8960/"
    # The project is handed its sandbox's published port and the profile's own
    # internal port, and its link check asks that port, in its own sandbox.
    (up, _journey) = estate.calls_for(PRIVATE)
    assert up["issued_env"]["FACTORY_PREVIEW_PORT"] == "8960"
    assert up["issued_env"]["FACTORY_PREVIEW_INTERNAL_PORT"] == "18950"
    assert up["issued_env"]["FACTORY_PREVIEW_URL"] == f"https://{PUBLIC_HOST}:8960"
    assert up["issued_env"]["COMPOSE_PROJECT_NAME"] == "private-pv0"
    assert [port for _lease, port, _token in estate.venues[PRIVATE].named("probe")] == [8960]
    assert [port for _lease, port, _token in estate.venues[SHARED].named("probe")] == [8950]


@pytest.mark.asyncio
async def test_a_slot_one_project_previews_on_8960_while_the_shared_one_holds_8950(
    estate: Estate,
) -> None:
    """The live case: a shared preview is ready on 8950 when the slot-1
    project, whose profile still names 8950 and 8951, asks for one."""
    assert (await estate.service.prepare(_request(SHARED, "build-s1", "FEAT-5001"))).state == "ready"
    private = await estate.service.prepare(_request(PRIVATE, "build-p1", "FEAT-9001"))
    assert private.state == "ready", private.failure_reason
    assert private.app_url == f"https://{PUBLIC_HOST}:8960/?preview=the-right-token"
    assert sorted((lease.project, lease.port) for lease in estate.store.live()) == [
        (PRIVATE, 8960),
        (SHARED, 8950),
    ]


@pytest.mark.asyncio
async def test_a_full_sandbox_never_takes_or_blocks_another_sandbox_s_slots(
    estate: Estate,
) -> None:
    for build, feature in (("build-s1", "FEAT-5001"), ("build-s2", "FEAT-5002")):
        assert (await estate.service.prepare(_request(SHARED, build, feature))).state == "ready"
    # The shared sandbox is full; the project's own sandbox is not.
    assert await estate.service.has_room(PRIVATE, build_id="build-p1", make_room=False)
    first = await estate.service.prepare(_request(PRIVATE, "build-p1", "FEAT-9001"))
    second = await estate.service.prepare(_request(PRIVATE, "build-p2", "FEAT-9002"))
    assert (_link(first), _link(second)) == (
        f"https://{PUBLIC_HOST}:8960/",
        f"https://{PUBLIC_HOST}:8961/",
    )
    # Now both are full, and each names only its own holders.
    assert not await estate.service.has_room(PRIVATE, build_id="build-p3", make_room=False)
    third = await estate.service.prepare(_request(PRIVATE, "build-p3", "FEAT-9003"))
    assert third.state == "failed" and is_slots_busy(third.failure_reason)
    assert third.failure_reason == "both preview slots are in use by FEAT-9001 and FEAT-9002"
    ports = sorted(lease.port for lease in estate.store.live())
    assert ports == [8950, 8951, 8960, 8961]


# ---------------------------------------------------------------------------
# How many slots


@pytest.mark.asyncio
async def test_a_profile_with_more_slots_than_the_sandbox_has_ports_gets_only_as_many(
    estate: Estate,
) -> None:
    estate.profiles[PRIVATE] = _profile("private", (8950, 8951, 8952))
    previews = [
        await estate.service.prepare(_request(PRIVATE, f"build-p{n}", f"FEAT-900{n}"))
        for n in (1, 2, 3)
    ]
    assert [p.state for p in previews] == ["ready", "ready", "failed"]
    assert [_link(p) for p in previews[:2]] == [
        f"https://{PUBLIC_HOST}:8960/",
        f"https://{PUBLIC_HOST}:8961/",
    ]
    assert previews[2].failure_reason == "both preview slots are in use by FEAT-9001 and FEAT-9002"
    assert sorted(lease.port for lease in estate.store.live()) == [8960, 8961]


def test_a_profile_with_fewer_slots_than_the_sandbox_has_ports_keeps_its_count() -> None:
    own, refused = sandbox_preview(PRIVATE, _profile("p", (8950,)).preview, SANDBOXES)
    assert refused is None
    assert [(s.port, s.internal_port) for s in own.slots] == [(8960, 18950)]


def test_a_profile_s_public_url_is_not_used_in_a_sandbox_of_its_own() -> None:
    """It names a port the profile cannot know; the link comes from the
    coordinator's public host and the sandbox's port."""
    profile = _profile("p", (8950, 8951), public_url="https://preview.example.test:8950")
    own, refused = sandbox_preview(PRIVATE, profile.preview, SANDBOXES)
    assert refused is None
    assert [(s.port, s.public_url) for s in own.slots] == [(8960, None), (8961, None)]


# ---------------------------------------------------------------------------
# The shared sandbox is unchanged


@pytest.mark.asyncio
async def test_the_shared_sandbox_previews_exactly_as_without_the_rule(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    with_rule = Estate(tmp_path / "a", connection, sandboxes=lambda: SANDBOXES)
    evidence_with = await with_rule.service.prepare(_request(SHARED, "build-s1", "FEAT-5001"))

    other = connect_writer(tmp_path / "other.db")
    migrations.apply_at_boot(other)
    without = Estate(tmp_path / "b", other, sandboxes=None)
    try:
        evidence_without = await without.service.prepare(_request(SHARED, "build-s1", "FEAT-5001"))
        lease_without = without.store.live()[0]
    finally:
        other.close()

    assert evidence_with.state == evidence_without.state == "ready"
    assert evidence_with.app_url == evidence_without.app_url
    assert evidence_with.app_url == f"https://{PUBLIC_HOST}:8950/?preview=the-right-token"
    lease_with = with_rule.store.live()[0]
    assert (lease_with.slot, lease_with.port, lease_with.compose_project) == (
        lease_without.slot,
        lease_without.port,
        lease_without.compose_project,
    )
    assert [c["issued_env"].as_dict() for c in with_rule.dispatch.calls] == [
        c["issued_env"].as_dict() for c in without.dispatch.calls
    ]


def test_the_shared_sandbox_keeps_its_profile_exactly() -> None:
    for ports in ((8950, 8951), (9950, 9951)):
        preview = _profile("s", ports, public_url=f"https://preview.example.test:{ports[0]}").preview
        own, refused = sandbox_preview(SHARED, preview, SANDBOXES)
        assert refused is None and own is preview, "the very same profile, untouched"


def test_a_repository_without_a_sandbox_entry_keeps_its_profile() -> None:
    preview = _profile("x", (8950, 8951)).preview
    assert sandbox_preview("acme/elsewhere", preview, SANDBOXES) == (preview, None)


# ---------------------------------------------------------------------------
# Refusals, in plain words, before anything starts


@pytest.mark.asyncio
async def test_a_sandbox_of_its_own_without_recorded_ports_is_refused(estate: Estate) -> None:
    assert await estate.service.has_room(LATE, build_id="build-l1", make_room=True)
    refused = await estate.service.prepare(_request(LATE, "build-l1", "FEAT-7001"))
    assert refused.state == "failed"
    assert refused.failure_reason == (
        "acme/registered-before's sandbox registered-before has no preview ports "
        "recorded in the settings, so no preview is started (it would listen on a "
        "port the sandbox may not publish); register the project again with forge "
        "register-repo --slot to record them"
    )
    assert estate.venues[LATE].calls == []
    assert estate.store.live() == []


@pytest.mark.asyncio
async def test_the_shared_sandbox_may_not_take_another_sandbox_s_port(estate: Estate) -> None:
    estate.profiles[SHARED] = _profile("shared", (8961, 8951))
    refused = await estate.service.prepare(_request(SHARED, "build-s1", "FEAT-5001"))
    assert refused.state == "failed"
    assert refused.failure_reason == (
        "acme/shared-notes's deploy profile gives its preview port 8961, which the "
        "sandbox private-notes publishes for its own previews, so no preview is "
        "started in shared-sbx"
    )
    assert estate.venues[SHARED].calls == []


@pytest.mark.asyncio
async def test_an_internal_port_that_is_one_of_the_sandbox_s_ports_is_refused(
    estate: Estate,
) -> None:
    estate.profiles[PRIVATE] = _profile("private", (8950, 8951), internal=(8961, 18951))
    refused = await estate.service.prepare(_request(PRIVATE, "build-p1", "FEAT-9001"))
    assert refused.state == "failed"
    assert refused.failure_reason == (
        "acme/private-notes's deploy profile gives a preview internal port 8961, "
        "which its sandbox private-notes publishes as a preview port, so no preview "
        "is started; the profile's internal ports must differ from its sandbox's "
        "preview ports"
    )
    assert estate.venues[PRIVATE].calls == []


# ---------------------------------------------------------------------------
# The production composition, and the sandbox's own link check


@pytest.mark.asyncio
async def test_the_production_service_takes_the_ports_from_planning_sandboxes(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    planning = PlanningConfig.model_validate(
        {
            "target_repo_paths": {SHARED: "/srv/shared", PRIVATE: "/srv/private"},
            "sandboxes": {
                SHARED: SHARED_ENTRY.model_dump(),
                PRIVATE: PRIVATE_ENTRY.model_dump(),
            },
        }
    )
    config = SimpleNamespace(
        planning=planning,
        review_preview=SimpleNamespace(
            evidence_root=str(tmp_path / "evidence"),
            evidence_base_url=BASE_URL,
            public_host=PUBLIC_HOST,
            max_app_ttl_minutes=None,
        ),
    )
    dispatch = FakeDispatch()
    service = build_review_preview_service(config, connection=connection, deploy_dispatcher=dispatch)
    # The profile and the venue are the sandbox's in production; here the
    # profile is the live case's, naming the shared sandbox's ports.
    venue = FakeVenue()
    service._profile_reader = lambda project: _profile("private", (8950, 8951))
    service._venue_for = lambda project: venue
    service._repo_paths = {PRIVATE: "/clone"}
    service._token_factory = lambda: "the-right-token"
    evidence = await service.prepare(_request(PRIVATE, "build-p1", "FEAT-9001"))
    assert evidence.state == "ready", evidence.failure_reason
    assert _link(evidence) == f"https://{PUBLIC_HOST}:8960/"
    assert dispatch.calls[0]["issued_env"]["FACTORY_PREVIEW_PORT"] == "8960"


@pytest.mark.parametrize(
    "repo, entry, ports",
    [
        (PRIVATE, PRIVATE_ENTRY, {8960, 8961}),
        (SHARED, SHARED_ENTRY, {8950, 8951}),
        (LATE, LATE_ENTRY, set()),
    ],
)
def test_the_sandbox_s_link_check_reaches_the_ports_its_previews_use(
    monkeypatch: pytest.MonkeyPatch, repo: str, entry: SandboxEntry, ports: set[int]
) -> None:
    """Inside the sandbox, the link check is allowed exactly the ports the
    coordinator gives the previews: the sandbox's own when it names them."""
    from forge.deploy_sidecar import service

    seen: dict[str, Any] = {}

    def capture(route, payload, *, repo_path, slot_ports):
        seen["ports"] = set(slot_ports)
        return 200, {}

    monkeypatch.setattr(service, "sidecar_is_inside_sandbox", lambda: True)
    monkeypatch.setattr(
        service, "profile_at_commit", lambda path: (_profile("p", (8950, 8951)), None)
    )
    monkeypatch.setattr(service, "serve_preview_operation", capture)
    config = SimpleNamespace(
        planning=PlanningConfig.model_validate(
            {"target_repo_paths": {repo: "/clone"}, "sandboxes": {repo: entry.model_dump()}}
        )
    )
    status, _body = service.process_preview_request(
        "/preview/probe", {"repo": repo, "lease_id": "pv000000000001"}, config=config
    )
    assert status == 200
    assert seen["ports"] == ports
