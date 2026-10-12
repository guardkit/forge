"""The review preview before a merge offer (web readiness W7, 12 October 2026).

Everything here runs without Docker, a sandbox or a network: the sandbox's
venue, the deploy dispatch, the clock and the sleep are fakes, and the
functions the sandbox's routes run are given a fake command runner. Two
tests use real git in a temporary repository, because the point of them is
that the preview's join has exactly the tree the build system's merge makes.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import stat
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.adapters.sqlite.connect import connect_writer
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.review_preview import (
    EVIDENCE_FOLDER,
    LEASE_LABEL,
    PREVIEW_ENV_KEYS,
    PREVIEW_PROBE_ROUTE,
    PREVIEW_PROJECT_REMOVE_ROUTE,
    PREVIEW_PROJECTS_ROUTE,
    PREVIEW_TREE_ROUTE,
    PreviewLease,
    PreviewLeaseStore,
    PreviewRequest,
    PreviewTree,
    ReviewPreviewService,
    list_preview_leases,
    make_preview_tree,
    pack_preview_evidence,
    probe_preview_link,
    remove_preview_resources,
    remove_preview_tree,
    serve_preview_operation,
    store_evidence_once,
)
from forge.deploy.stage import DeployStageResult
from forge.lifecycle import migrations

START = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
BASE_URL = "https://promaxgb10-41b1.tailebf801.ts.net/factory-evidence"
PROJECT = "guardkit/notes-fixture"
SHA_MAIN = "a" * 40
#: The target branch as freshly fetched from the remote: ahead of the clone's
#: local main, which is what the offer's ``expect_main_sha`` pins.
SHA_FETCHED = "f" * 40
SHA_CAND = "b" * 40
TREE_CAND = "c" * 40
JOIN_COMMIT = "d" * 40
JOIN_TREE = "e" * 40
SLOTS = [
    {
        "port": 8950,
        "internal_port": 18950,
        "public_url": "https://spark-fcf6.tailebf801.ts.net:8950",
    },
    {
        "port": 8951,
        "internal_port": 18951,
        "public_url": "https://spark-fcf6.tailebf801.ts.net:8951",
    },
]


def _profile(**preview: Any):
    section = {"enabled": True, "slots": SLOTS, **preview}
    return parse_deploy_profile(
        {
            "env_id": "notes",
            "compose": {"file": "compose.yml", "script": "deploy/deploy.sh"},
            "preview": section,
        }
    )


def _request(build: str = "build-1", feature: str = "FEAT-AB12") -> PreviewRequest:
    return PreviewRequest(
        project=PROJECT,
        feature_id=feature,
        build_id=build,
        branch=f"autobuild/{feature}",
        candidate_sha=SHA_CAND,
        candidate_tree=TREE_CAND,
        expect_main_sha=SHA_MAIN,
        target_branch="main",
        preview_base_sha=SHA_FETCHED,
    )


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


EVIDENCE_FILES = {
    "index.html": b"<html>report</html>",
    "data/attachment.txt": b"attachment",
    "shots/sign-in.png": b"png",
    "video/journey.webm": b"webm",
    "trace.zip": b"zip",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []
        self.on_sleep: Any = None

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            await self.on_sleep()


class FakeVenue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.join_refusal: str | None = None
        self.probe_answer: dict[str, Any] = {
            "no_token": 410,
            "wrong_token": 410,
            "right_token": 200,
        }
        self.evidence: tuple[bytes | None, str | None] = (_archive(EVIDENCE_FILES), None)
        self.running: set[str] | None = set()
        self.remove_ok = True

    async def lay_out(self, lease_id: str, *, onto: str, candidate: str) -> PreviewTree:
        self.calls.append(("lay_out", (lease_id, onto, candidate)))
        if self.join_refusal:
            return PreviewTree(refusal=self.join_refusal)
        return PreviewTree(
            commit=JOIN_COMMIT,
            tree=JOIN_TREE,
            path=f"/clone/.forge-candidates/preview-{lease_id}",
        )

    async def remove_tree(self, lease_id: str) -> bool:
        self.calls.append(("remove_tree", lease_id))
        return True

    async def probe(self, lease_id: str, port: int, token: str) -> dict[str, Any]:
        self.calls.append(("probe", (lease_id, port, token)))
        return dict(self.probe_answer)

    async def fetch_evidence(self, lease_id: str) -> tuple[bytes | None, str | None]:
        self.calls.append(("fetch_evidence", lease_id))
        return self.evidence

    async def list_leases(self) -> set[str] | None:
        self.calls.append(("list_leases", None))
        return None if self.running is None else set(self.running)

    async def remove_project(self, lease_id: str) -> bool:
        self.calls.append(("remove_project", lease_id))
        return self.remove_ok

    def named(self, op: str) -> list[Any]:
        return [arg for name, arg in self.calls if name == op]


def _journey(lease_dir: str, **changes: Any) -> dict[str, Any]:
    evidence = f"{lease_dir}/{EVIDENCE_FOLDER}"
    return {
        "verdict": "pass",
        "report_ref": f"{evidence}/index.html",
        "screenshot_refs": [f"{evidence}/shots/sign-in.png"],
        "media_refs": ["video/journey.webm"],
        "trace_refs": ["trace.zip"],
        "outcome": {"passed": 3, "failed": 0, "skipped": None, "flaky": None},
        **changes,
    }


class FakeDispatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.up: Any = "complete"
        self.journey_changes: dict[str, Any] = {}
        self.journey_outcome = "complete"
        self.raise_on: str | None = None

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(0)  # let a concurrent preparation interleave
        leg = kwargs["leg"]
        if self.raise_on == leg:
            raise RuntimeError(f"{leg} blew up")
        if leg == "preview_up":
            if self.up is None:
                return None
            if self.up != "complete":
                return DeployStageResult(
                    outcome="failed",
                    deploy_run_id=kwargs["deploy_run_id"],
                    failed_step="deploy_compose",
                    detail={"refusal": self.up},
                )
            return DeployStageResult(outcome="complete", deploy_run_id=kwargs["deploy_run_id"])
        if self.journey_outcome != "complete":
            return DeployStageResult(
                outcome="failed",
                deploy_run_id=kwargs["deploy_run_id"],
                failed_step="preview_journey",
                detail={"reason": self.journey_outcome},
            )
        journey = _journey(kwargs["candidate_cwd"], **self.journey_changes)
        return DeployStageResult(
            outcome="complete",
            deploy_run_id=kwargs["deploy_run_id"],
            verdict=journey["verdict"],
            detail={"journey": journey},
        )

    def legs(self) -> list[str]:
        return [call["leg"] for call in self.calls]


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return cx


class World:
    def __init__(self, tmp_path: Path, connection: sqlite3.Connection) -> None:
        self.clock = FakeClock()
        self.venue = FakeVenue()
        self.dispatch = FakeDispatch()
        self.profile = _profile()
        self.store = PreviewLeaseStore(connection)
        self.root = tmp_path / "evidence"
        self.leases = iter(f"pv{n:012x}" for n in range(1, 100))
        self.runs = iter(f"pvrun-{n:012x}" for n in range(1, 100))
        self.service = ReviewPreviewService(
            store=self.store,
            profile_reader=lambda project: self.profile,
            venue_for=lambda project: self.venue,
            deploy_dispatcher=self.dispatch,
            repo_paths={PROJECT: "/clone"},
            evidence_root=self.root,
            evidence_base_url=BASE_URL,
            clock=self.clock,
            sleep=self.clock.sleep,
            token_factory=lambda: "the-right-token",
            lease_id_factory=lambda: next(self.leases),
            run_id_factory=lambda: next(self.runs),
        )

    def lease(self, lease_id: str) -> PreviewLease:
        found = self.store.get(lease_id)
        assert found is not None, lease_id
        return found


@pytest.fixture
def world(tmp_path: Path, connection: sqlite3.Connection) -> World:
    return World(tmp_path, connection)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_enabled_only_for_a_profile_with_an_enabled_preview(world: World) -> None:
    assert world.service.enabled_for(PROJECT) is True
    world.profile = _profile(enabled=False)
    assert world.service.enabled_for(PROJECT) is False
    world.profile = parse_deploy_profile({"env_id": "x", "compose": {"file": "c.yml"}})
    assert world.service.enabled_for(PROJECT) is False
    world.profile = None
    assert world.service.enabled_for(PROJECT) is False


def test_a_profile_that_cannot_be_read_is_not_enabled(world: World) -> None:
    def broken(project: str):
        raise OSError("sidecar down")

    world.service._profile_reader = broken
    assert world.service.enabled_for(PROJECT) is False


# ---------------------------------------------------------------------------
# A preview that is prepared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prepared_preview_is_ready_with_its_links_and_evidence(world: World) -> None:
    evidence = await world.service.prepare(_request())

    assert evidence.state == "ready", evidence.failure_reason
    assert evidence.app_url == "https://spark-fcf6.tailebf801.ts.net:8950/?preview=the-right-token"
    folder = f"{BASE_URL}/guardkit/notes-fixture/FEAT-AB12/{SHA_CAND}/pvrun-000000000001"
    assert evidence.report_url == f"{folder}/index.html"
    assert evidence.media_urls == [f"{folder}/shots/sign-in.png", f"{folder}/video/journey.webm"]
    assert evidence.preview_tree == JOIN_TREE
    assert (evidence.candidate_sha, evidence.candidate_tree) == (SHA_CAND, TREE_CAND)
    assert evidence.outcome.passed == 3 and evidence.outcome.failed == 0
    assert evidence.outcome.skipped is None and evidence.outcome.flaky is None
    assert evidence.app_expires_at == START + timedelta(hours=24)
    assert evidence.evidence_expires_at == START + timedelta(days=14)

    # The join is onto the target branch as fetched from the remote (what the
    # merge press joins onto), never the clone's local main; the slot is held.
    assert world.venue.named("lay_out") == [("pv000000000001", SHA_FETCHED, SHA_CAND)]
    lease = world.lease("pv000000000001")
    assert (lease.state, lease.slot, lease.port) == ("ready", 0, 8950)
    assert lease.compose_project == "notes-pv0"
    assert lease.preview_tree == JOIN_TREE
    assert world.venue.named("remove_project") == []

    # The deploy steps and the journey run in the laid-out tree with the overlay.
    up, journey = world.dispatch.calls
    assert (up["leg"], journey["leg"]) == ("preview_up", "preview_journey")
    tree = "/clone/.forge-candidates/preview-pv000000000001"
    assert up["candidate_cwd"] == journey["candidate_cwd"] == tree
    assert up["identity_env"] == {
        "COMPOSE_PROJECT_NAME": "notes-pv0",
        "FACTORY_PREVIEW_PORT": "8950",
        "FACTORY_PREVIEW_INTERNAL_PORT": "18950",
        "FACTORY_PREVIEW_URL": "https://spark-fcf6.tailebf801.ts.net:8950",
        "FACTORY_PREVIEW_TOKEN": "the-right-token",
        "FACTORY_RESOURCE_LABEL": f"{LEASE_LABEL}=pv000000000001",
        "FACTORY_EVIDENCE_DIR": f"{tree}/{EVIDENCE_FOLDER}",
    }
    assert journey["identity_env"] == {**up["identity_env"], "FACTORY_RECORD_VIDEO": "1"}
    assert set(journey["identity_env"]) <= PREVIEW_ENV_KEYS

    # The evidence was kept once, read-only, before anything else.
    kept = world.root / "guardkit" / "notes-fixture" / "FEAT-AB12" / SHA_CAND / "pvrun-000000000001"
    assert (kept / "index.html").read_bytes() == b"<html>report</html>"
    assert not os.access(kept / "index.html", os.W_OK) or os.geteuid() == 0
    assert stat.S_IMODE((kept / "index.html").stat().st_mode) == 0o444
    assert stat.S_IMODE(kept.stat().st_mode) == 0o555
    # Ready is approvable until the window ends.
    record = evidence.model_dump(mode="json")
    assert world.service.approval_refusal(record, now=START + timedelta(hours=1)) is None


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_preparations_get_different_slots(world: World) -> None:
    first, second = await asyncio.gather(
        world.service.prepare(_request("build-1", "FEAT-AB12")),
        world.service.prepare(_request("build-2", "FEAT-CD34")),
    )
    assert first.state == second.state == "ready"
    assert {first.app_url.split("?")[0], second.app_url.split("?")[0]} == {
        "https://spark-fcf6.tailebf801.ts.net:8950/",
        "https://spark-fcf6.tailebf801.ts.net:8951/",
    }
    ports = {lease.port for lease in world.store.live()}
    assert ports == {8950, 8951}
    projects = {call["identity_env"]["COMPOSE_PROJECT_NAME"] for call in world.dispatch.calls}
    assert projects == {"notes-pv0", "notes-pv1"}


@pytest.mark.asyncio
async def test_with_every_slot_held_it_waits_then_fails_in_plain_words(world: World) -> None:
    await world.service.prepare(_request("build-1", "FEAT-AB12"))
    await world.service.prepare(_request("build-2", "FEAT-CD34"))
    world.dispatch.calls.clear()

    third = await world.service.prepare(_request("build-3", "FEAT-EF56"))

    assert third.state == "failed"
    assert third.failure_reason == "no preview slot came free within 30 minutes"
    assert third.app_url is None and third.report_url is None
    assert world.clock.now - START >= timedelta(minutes=30)
    assert len(world.clock.sleeps) >= 2
    assert world.dispatch.calls == []  # nothing was stood up
    assert "pv000000000003" in world.venue.named("remove_tree")
    assert world.lease("pv000000000003").state == "failed"
    assert world.lease("pv000000000003").slot is None


@pytest.mark.asyncio
async def test_a_slot_that_comes_free_while_waiting_is_taken(world: World) -> None:
    await world.service.prepare(_request("build-1", "FEAT-AB12"))
    await world.service.prepare(_request("build-2", "FEAT-CD34"))

    async def free_one() -> None:
        if world.clock.now - START >= timedelta(minutes=5):
            world.clock.on_sleep = None
            await world.service.release(
                feature_id="FEAT-AB12", build_id="build-1", reason="rejected"
            )

    world.clock.on_sleep = free_one
    third = await world.service.prepare(_request("build-3", "FEAT-EF56"))
    assert third.state == "ready", third.failure_reason
    assert third.app_url.startswith("https://spark-fcf6.tailebf801.ts.net:8950/")
    assert world.clock.now - START < timedelta(minutes=30)


@pytest.mark.asyncio
async def test_an_expired_preview_does_not_keep_a_waiting_one_out(world: World) -> None:
    world.profile = _profile(slots=SLOTS[:1], app_ttl_hours=1)
    await world.service.prepare(_request("build-1", "FEAT-AB12"))
    world.clock.now = START + timedelta(hours=2)
    second = await world.service.prepare(_request("build-2", "FEAT-CD34"))
    assert second.state == "ready", second.failure_reason
    assert world.lease("pv000000000001").state == "ended"
    assert world.lease("pv000000000001").reason == "the preview's time was up"
    assert "pv000000000001" in world.venue.named("remove_project")


@pytest.mark.asyncio
async def test_a_second_preview_for_the_same_build_ends_the_first(world: World) -> None:
    await world.service.prepare(_request())
    again = await world.service.prepare(_request())
    assert again.state == "ready"
    assert world.lease("pv000000000001").state == "ended"
    assert [lease.lease_id for lease in world.store.live()] == ["pv000000000002"]


# ---------------------------------------------------------------------------
# The link check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, words",
    [
        ({"no_token": 200, "wrong_token": 410, "right_token": 200}, "with no token got 200"),
        ({"no_token": 410, "wrong_token": 200, "right_token": 200}, "with a wrong token got 200"),
        ({"no_token": 403, "wrong_token": 403, "right_token": 410}, "with the right token got 410"),
        ({"no_token": None, "wrong_token": 410, "right_token": 200}, "no answer"),
        ({"error": "sidecar unreachable"}, "could not be checked"),
    ],
)
@pytest.mark.asyncio
async def test_a_preview_that_does_not_enforce_its_link_fails(
    world: World, answer: dict[str, Any], words: str
) -> None:
    world.venue.probe_answer = answer
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason.startswith("the preview does not enforce its link")
    assert words in evidence.failure_reason
    assert evidence.app_url is None
    assert world.dispatch.legs() == ["preview_up"]  # no journey was recorded
    assert world.venue.named("remove_project") == ["pv000000000001"]
    assert world.lease("pv000000000001").state == "failed"
    assert world.store.live() == []


@pytest.mark.asyncio
async def test_403_and_410_both_count_as_refused(world: World) -> None:
    world.venue.probe_answer = {"no_token": 403, "wrong_token": 410, "right_token": 302}
    evidence = await world.service.prepare(_request())
    assert evidence.state == "ready"
    assert world.venue.named("probe") == [("pv000000000001", 8950, "the-right-token")]


# ---------------------------------------------------------------------------
# Every failure: torn down, failed, and never approvable
# ---------------------------------------------------------------------------


def _fail_join(w: World) -> None:
    w.venue.join_refusal = "the candidate does not join cleanly onto aaaaaaaaaa: x.py"


def _fail_up(w: World) -> None:
    w.dispatch.up = "sidecar refused (HTTP 400): env key not allowlisted"


def _deploy_off(w: World) -> None:
    w.dispatch.up = None


def _up_raises(w: World) -> None:
    w.dispatch.raise_on = "preview_up"


def _journey_red(w: World) -> None:
    w.dispatch.journey_changes = {
        "verdict": "fail",
        "outcome": {"passed": 2, "failed": 1},
    }


def _journey_lost(w: World) -> None:
    w.dispatch.journey_outcome = "the journey gave no answer"


def _evidence_refused(w: World) -> None:
    w.venue.evidence = (None, "the preview left no evidence folder")


def _no_report(w: World) -> None:
    w.venue.evidence = (_archive({"shots/a.png": b"png"}), None)
    w.dispatch.journey_changes = {"report_ref": None}


def _failed_count_with_pass(w: World) -> None:
    w.dispatch.journey_changes = {"outcome": {"passed": 2, "failed": 1}}


@pytest.mark.parametrize(
    "arrange, reason, torn_down, diagnostics",
    [
        (_fail_join, "the candidate could not be joined onto main: the candidate does not join", False, False),
        (_fail_up, "the preview could not be started: it stopped at deploy_compose — sidecar refused", True, True),
        (_deploy_off, "the preview could not be started: the deploy stage is switched off", True, True),
        (_up_raises, "the preview could not be started: RuntimeError: preview_up blew up", True, True),
        (_journey_red, "the key journey did not pass (verdict: fail)", True, True),
        (_journey_lost, "the key journey could not be run: it stopped at preview_journey", True, True),
        (_evidence_refused, "the evidence could not be kept: the preview left no evidence folder", True, False),
        (_no_report, "the key journey left no test report", True, True),
        (_failed_count_with_pass, "the key journey did not pass (verdict: pass)", True, True),
    ],
)
@pytest.mark.asyncio
async def test_every_failure_is_torn_down_and_never_approvable(
    world: World, arrange: Any, reason: str, torn_down: bool, diagnostics: bool
) -> None:
    arrange(world)
    evidence = await world.service.prepare(_request())

    assert evidence.state == "failed"
    assert evidence.failure_reason.startswith(reason), evidence.failure_reason
    assert evidence.app_url is None
    if torn_down:
        assert world.venue.named("remove_project") == ["pv000000000001"]
    else:
        assert world.venue.named("remove_project") == []
    assert "pv000000000001" in world.venue.named("remove_tree")
    lease = world.lease("pv000000000001")
    assert lease.state == "failed" and lease.reason.startswith(reason)
    assert world.store.live() == []
    if diagnostics:
        assert evidence.diagnostics_url and evidence.diagnostics_url.startswith(BASE_URL)
    else:
        assert evidence.diagnostics_url is None
    record = evidence.model_dump(mode="json")
    assert world.service.approval_refusal(record, now=START) is not None


@pytest.mark.asyncio
async def test_a_failed_journey_links_its_report_as_diagnostics(world: World) -> None:
    _journey_red(world)
    evidence = await world.service.prepare(_request())
    assert evidence.diagnostics_url.endswith("/pvrun-000000000001/index.html")
    assert evidence.outcome.failed == 1 and evidence.outcome.skipped is None


@pytest.mark.asyncio
async def test_a_teardown_that_is_not_confirmed_keeps_the_slot_until_the_sweep(world: World) -> None:
    world.venue.remove_ok = False
    world.venue.probe_answer = {"no_token": 200, "wrong_token": 410, "right_token": 200}
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert world.lease("pv000000000001").state == "ending"
    world.venue.remove_ok = True
    await world.service.sweep(now=world.clock.now)
    lease = world.lease("pv000000000001")
    assert lease.state == "ended"
    assert lease.reason.startswith("the preview does not enforce its link")
    assert "not confirmed" not in lease.reason


@pytest.mark.asyncio
async def test_something_unexpected_after_the_slot_still_tears_down(world: World) -> None:
    async def probe(lease_id: str, port: int, token: str) -> dict[str, Any]:
        raise KeyError("surprise")

    world.venue.probe = probe
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason.startswith("the preview could not be prepared (KeyError")
    assert world.venue.named("remove_project") == ["pv000000000001"]
    assert world.lease("pv000000000001").state == "failed"
    assert world.store.live() == []


@pytest.mark.asyncio
async def test_a_target_branch_that_could_not_be_fetched_starts_nothing(
    world: World,
) -> None:
    from dataclasses import replace as _replace

    request = _replace(
        _request(), preview_base_sha=None, preview_base_refusal="origin answered 503"
    )
    evidence = await world.service.prepare(request)
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "main could not be fetched from the remote, so there is nothing to join "
        "the candidate onto: origin answered 503"
    )
    assert world.venue.calls == [] and world.dispatch.calls == []


@pytest.mark.asyncio
async def test_without_evidence_storage_nothing_is_started(world: World) -> None:
    world.service._evidence_root = None
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert "evidence storage is not set up" in evidence.failure_reason
    assert world.venue.calls == [] and world.dispatch.calls == []


@pytest.mark.asyncio
async def test_a_repository_without_a_sandbox_gets_a_plain_failure(world: World) -> None:
    world.service._venue_for = lambda project: None
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "previews are prepared only for a repository that has a sandbox"
    )
    assert evidence.preview_tree == TREE_CAND


# ---------------------------------------------------------------------------
# Write-once evidence
# ---------------------------------------------------------------------------


def test_evidence_is_written_once_and_made_read_only(tmp_path: Path) -> None:
    dest = tmp_path / "root" / "p" / "run"
    kept = store_evidence_once(_archive(EVIDENCE_FILES), dest)
    assert kept == sorted(EVIDENCE_FILES)
    for name in EVIDENCE_FILES:
        assert stat.S_IMODE((dest / name).stat().st_mode) == 0o444
    assert stat.S_IMODE((dest / "shots").stat().st_mode) == 0o555
    with pytest.raises(FileExistsError):
        store_evidence_once(_archive({"index.html": b"replaced"}), dest)
    assert (dest / "index.html").read_bytes() == b"<html>report</html>"


def test_an_archive_that_escapes_its_folder_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "root" / "run"
    with pytest.raises(tarfile.TarError):
        store_evidence_once(_archive({"../outside.txt": b"x"}), dest)
    assert not dest.exists()
    assert not (tmp_path / "root" / "outside.txt").exists()


@pytest.mark.asyncio
async def test_an_existing_evidence_folder_is_never_overwritten(world: World) -> None:
    existing = world.root / "guardkit" / "notes-fixture" / "FEAT-AB12" / SHA_CAND / "pvrun-000000000001"
    existing.mkdir(parents=True)
    (existing / "index.html").write_text("earlier")
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert "never overwritten" in evidence.failure_reason
    assert (existing / "index.html").read_text() == "earlier"
    assert world.venue.named("remove_project") == ["pv000000000001"]


def test_packed_evidence_leaves_links_out(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    folder = clone / ".forge-candidates" / "preview-pv000000000001" / EVIDENCE_FOLDER
    (folder / "shots").mkdir(parents=True)
    (folder / "index.html").write_text("report")
    (folder / "shots" / "a.png").write_bytes(b"png")
    secret = tmp_path / "secret.txt"
    secret.write_text("never sent")
    (folder / "link.txt").symlink_to(secret)

    packed = pack_preview_evidence(clone, lease_id="pv000000000001")
    assert packed["files"] == 2
    import base64

    kept = store_evidence_once(base64.b64decode(packed["archive"]), tmp_path / "kept")
    assert kept == ["index.html", "shots/a.png"]
    assert pack_preview_evidence(clone, lease_id="pv000000000001", cap=3)["refusal"]
    assert "no evidence folder" in pack_preview_evidence(clone, lease_id="pv0000000000ff")["refusal"]


# ---------------------------------------------------------------------------
# Release, sweep, start-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_is_idempotent_and_keeps_the_evidence(world: World) -> None:
    evidence = await world.service.prepare(_request())
    await world.service.release(feature_id="FEAT-AB12", build_id="build-1", reason="rejected")
    await world.service.release(feature_id="FEAT-AB12", build_id="build-1", reason="rejected")
    assert world.venue.named("remove_project") == ["pv000000000001"]
    lease = world.lease("pv000000000001")
    assert (lease.state, lease.reason) == ("ended", "rejected")
    assert Path(lease.evidence_dir, "index.html").is_file()
    # An ended preview cannot be approved, but only because the record says
    # so; the folder is still there for the report link.
    assert world.service.approval_refusal(evidence.model_dump(mode="json"), now=START) is None
    await world.service.release(feature_id="FEAT-ZZ99", build_id="no-such-build", reason="x")


@pytest.mark.asyncio
async def test_the_sweep_ends_expired_previews_and_deletes_only_old_evidence(world: World) -> None:
    await world.service.prepare(_request("build-1", "FEAT-AB12"))
    world.clock.now = START + timedelta(hours=12)
    await world.service.prepare(_request("build-2", "FEAT-CD34"))
    stranger = world.root / "guardkit" / "not-ours"
    stranger.mkdir(parents=True)

    await world.service.sweep(now=START + timedelta(hours=25))
    assert world.lease("pv000000000001").state == "ended"
    assert world.lease("pv000000000002").state == "ready"
    first_dir = Path(world.lease("pv000000000001").evidence_dir)
    second_dir = Path(world.lease("pv000000000002").evidence_dir)
    assert first_dir.is_dir()  # evidence outlives the preview

    await world.service.sweep(now=START + timedelta(days=14, minutes=1))
    assert not first_dir.exists()
    assert world.lease("pv000000000001").evidence_deleted_at is not None
    # The second preview's own time was up too, so it ended in the same sweep,
    # but its evidence is not yet fourteen days old.
    assert world.lease("pv000000000002").state == "ended"
    assert second_dir.is_dir()
    assert stranger.is_dir()


@pytest.mark.asyncio
async def test_reconcile_on_boot_touches_only_preview_leases(world: World) -> None:
    await world.service.prepare(_request("build-1", "FEAT-AB12"))  # running
    await world.service.prepare(_request("build-2", "FEAT-CD34"))  # gone after restart
    now = world.clock.now
    world.store.insert(
        PreviewLease(
            lease_id="pv0000000000aa", slot=None, port=None, token="t", project=PROJECT,
            feature_id="FEAT-EF56", build_id="build-3", candidate_sha=SHA_CAND,
            candidate_tree=TREE_CAND, preview_tree=None, run_id="pvrun-0000000000aa",
            compose_project=None, evidence_dir=None, state="preparing", reason=None,
            created_at=now.isoformat(), app_expires_at=now.isoformat(),
            evidence_expires_at=now.isoformat(),
        ),
        now=now,
    )
    world.venue.calls.clear()
    world.venue.running = {"pv000000000001", "pv0000000000ff"}

    await world.service.reconcile_on_boot()

    assert world.lease("pv000000000001").state == "ready"
    assert world.lease("pv000000000002").state == "ended"
    assert world.lease("pv000000000002").reason == "its preview was no longer running after a restart"
    assert world.lease("pv0000000000aa").state == "failed"
    removed = world.venue.named("remove_project")
    assert "pv0000000000ff" in removed  # an orphan with no live lease
    assert "pv0000000000aa" in removed  # interrupted while being prepared
    assert "pv000000000001" not in removed  # a live preview is left alone


@pytest.mark.asyncio
async def test_reconcile_does_nothing_where_previews_cannot_be_listed(world: World) -> None:
    await world.service.prepare(_request())
    world.venue.running = None
    world.venue.calls.clear()
    await world.service.reconcile_on_boot()
    assert world.lease("pv000000000001").state == "ready"
    assert world.venue.named("remove_project") == []


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_refusal_reasons(world: World) -> None:
    ready = (await world.service.prepare(_request())).model_dump(mode="json")
    service = world.service
    assert service.approval_refusal(ready, now=START) is None
    assert "carries no preview record" in service.approval_refusal(None, now=START)
    assert "was not prepared" in service.approval_refusal({**ready, "state": "failed"}, now=START)
    expired = service.approval_refusal(ready, now=START + timedelta(hours=24))
    assert expired.startswith("the preview and its approval window ended at Tue 13 Oct 09:00 UTC")
    elsewhere = {**ready, "report_url": "https://elsewhere.example/index.html"}
    assert "evidence folder is missing" in service.approval_refusal(elsewhere, now=START)
    folder = Path(world.lease("pv000000000001").evidence_dir)
    from forge.deploy.review_preview import delete_evidence

    assert delete_evidence(folder, world.root)
    assert "evidence folder is missing" in service.approval_refusal(ready, now=START)


# ---------------------------------------------------------------------------
# Inside the sandbox: the routes' own work, with a fake command runner
# ---------------------------------------------------------------------------


class FakeEngine:
    """Answers the container engine's listing and removal commands."""

    def __init__(self, labelled: dict[str, list[str]]) -> None:
        self.labelled = labelled  # lease id -> container ids
        self.argv: list[list[str]] = []

    def __call__(self, argv) -> tuple[int, str, str]:
        argv = list(argv)
        self.argv.append(argv)
        if "--format" in argv:
            leases = sorted(k for k, ids in self.labelled.items() if ids)
            return 0, "\n".join(leases) + "\n", ""
        if "--quiet" in argv:
            selector = argv[argv.index("--filter") + 1]
            lease = selector.split("=", 2)[2]
            if argv[1] == "ps":
                return 0, "\n".join(self.labelled.get(lease, [])), ""
            return 0, "", ""
        if argv[1] == "rm":
            for lease, ids in self.labelled.items():
                self.labelled[lease] = [i for i in ids if i not in argv[3:]]
            return 0, "", ""
        return 0, "", ""


def test_listing_and_removal_use_only_the_preview_label() -> None:
    engine = FakeEngine({"pv000000000001": ["c1", "c2"], "pv000000000002": ["c3"]})
    assert list_preview_leases(engine) == {"leases": ["pv000000000001", "pv000000000002"]}
    answer = remove_preview_resources("pv000000000001", engine)
    assert answer == {"removed": True, "remaining": 0}
    assert engine.labelled["pv000000000002"] == ["c3"]  # another lease is left alone
    flat = " ".join(" ".join(argv) for argv in engine.argv)
    assert "guardkit.fixture.owner" not in flat
    for argv in engine.argv:
        filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
        if argv[1] == "rm" or argv[2:3] == ["rm"]:
            # A removal names only what the lease's own listing returned.
            assert not filters and set(argv[3:]) <= {"c1", "c2"}
            continue
        assert filters and all(f.startswith(f"label={LEASE_LABEL}") for f in filters)
        if "--quiet" in argv:
            assert filters == [f"label={LEASE_LABEL}=pv000000000001"]


def test_an_engine_that_cannot_be_asked_is_unknown_not_empty() -> None:
    def broken(argv) -> tuple[int, str, str]:
        return 1, "", "Cannot connect to the Docker daemon"

    assert "refusal" in list_preview_leases(broken)
    assert remove_preview_resources("pv000000000001", broken)["removed"] is False


def test_the_link_probe_asks_loopback_three_times() -> None:
    asked: list[str] = []

    def fetch(url: str) -> int | None:
        asked.append(url)
        return 200 if "preview=the-token" in url else 410

    answer = probe_preview_link(port=8950, token="the-token", fetch=fetch)
    assert answer == {"no_token": 410, "wrong_token": 410, "right_token": 200}
    assert all(url.startswith("http://127.0.0.1:8950/") for url in asked)
    assert asked[0] == "http://127.0.0.1:8950/"


def test_the_routes_check_what_they_are_sent(tmp_path: Path) -> None:
    status, body = serve_preview_operation(
        PREVIEW_TREE_ROUTE, {"lease_id": "../x"}, repo_path=tmp_path, slot_ports=frozenset()
    )
    assert status == 400 and "lease id" in body["error"]
    status, body = serve_preview_operation(
        PREVIEW_TREE_ROUTE,
        {"lease_id": "pv000000000001", "onto": "main", "candidate": SHA_CAND},
        repo_path=tmp_path,
        slot_ports=frozenset(),
    )
    assert status == 400 and "'onto'" in body["error"]
    status, body = serve_preview_operation(
        PREVIEW_PROBE_ROUTE,
        {"lease_id": "pv000000000001", "port": 22, "token": "t"},
        repo_path=tmp_path,
        slot_ports=frozenset({8950}),
    )
    assert status == 400 and "preview ports" in body["error"]
    engine = FakeEngine({"pv000000000001": ["c1"]})
    status, body = serve_preview_operation(
        PREVIEW_PROJECTS_ROUTE, {}, repo_path=tmp_path, slot_ports=frozenset(), run=engine
    )
    assert (status, body) == (200, {"leases": ["pv000000000001"]})
    status, body = serve_preview_operation(
        PREVIEW_PROJECT_REMOVE_ROUTE,
        {"lease_id": "pv000000000001"},
        repo_path=tmp_path,
        slot_ports=frozenset(),
        run=engine,
    )
    assert (status, body["removed"]) == (200, True)


def test_a_conflicting_candidate_is_refused_naming_the_files(tmp_path: Path) -> None:
    def git(argv) -> tuple[int, str, str]:
        assert argv[3] == "merge-tree"
        return 1, f"{'f' * 40}\nsrc/app.py\nREADME.md\n\nAuto-merging src/app.py\n", ""

    answer = make_preview_tree(
        tmp_path, lease_id="pv000000000001", onto=SHA_MAIN, candidate=SHA_CAND, run=git
    )
    assert answer == {
        "refusal": (
            "the candidate does not join cleanly onto aaaaaaaaaa: these files "
            "conflict — src/app.py, README.md"
        )
    }


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_the_preview_join_has_the_tree_the_build_systems_merge_makes(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text("one\ntwo\nthree\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "autobuild/FEAT-AB12")
    (repo / "feature.txt").write_text("feature\n")
    (repo / "shared.txt").write_text("one\ntwo\nthree\nfour\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main moved\n")
    (repo / "shared.txt").write_text("zero\none\ntwo\nthree\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moved")
    onto = _git(repo, "rev-parse", "HEAD")

    answer = make_preview_tree(repo, lease_id="pv000000000001", onto=onto, candidate=candidate)
    assert "refusal" not in answer, answer

    # What the build system's merge makes: git merge --no-ff onto main.
    _git(repo, "worktree", "add", "-q", "-b", "join", str(tmp_path / "join"), onto)
    _git(tmp_path / "join", "merge", "--no-ff", "-q", "-m", "join", candidate)
    assert answer["tree"] == _git(tmp_path / "join", "rev-parse", "HEAD^{tree}")

    assert _git(repo, "rev-parse", "refs/forge/preview/pv000000000001") == answer["commit"]
    assert _git(repo, "rev-parse", f"{answer['commit']}^1") == onto
    assert _git(repo, "rev-parse", f"{answer['commit']}^2") == candidate
    laid = Path(answer["path"])
    assert laid == repo / ".forge-candidates" / "preview-pv000000000001"
    assert (laid / "shared.txt").read_text() == "zero\none\ntwo\nthree\nfour\n"
    assert _git(repo, "status", "--porcelain") == ""  # the checkout looks clean

    assert remove_preview_tree(repo, lease_id="pv000000000001")["removed"] is True
    assert not laid.exists()
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "rev-parse", "--verify", "refs/forge/preview/pv000000000001")
    assert remove_preview_tree(repo, lease_id="pv000000000001")["removed"] is True


# ---------------------------------------------------------------------------
# The sidecar's door and the stage's two preview legs
# ---------------------------------------------------------------------------


def test_the_sidecar_permits_the_overlay_only_when_the_preview_is_on() -> None:
    from forge.deploy_sidecar.service import allowed_env_keys

    assert PREVIEW_ENV_KEYS <= allowed_env_keys(_profile())
    assert not PREVIEW_ENV_KEYS & allowed_env_keys(_profile(enabled=False))
    plain = parse_deploy_profile({"env_id": "x", "compose": {"file": "c.yml"}})
    assert not PREVIEW_ENV_KEYS & allowed_env_keys(plain)


def test_a_host_sidecar_refuses_the_preview_routes(monkeypatch) -> None:
    from forge.deploy_sidecar import service

    monkeypatch.setattr(service, "sidecar_is_inside_sandbox", lambda: False)
    status, body = service.process_preview_request(
        PREVIEW_TREE_ROUTE, {"repo": PROJECT}, config=None
    )
    assert status == 400 and "not inside a repository's" in body["error"]


class _Invoker:
    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.cwd, self.env = cwd, dict(env or {})

    def with_repo_path(self, path: Any) -> "_Invoker":
        return _Invoker(str(path), self.env)

    def with_extra_env(self, overlay: dict[str, str]) -> "_Invoker":
        return _Invoker(self.cwd, {**self.env, **overlay})

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        from forge.deploy.live_gate import LiveGateInvocation

        return LiveGateInvocation(
            verdict="pass",
            run_id="journey-1",
            report_ref="index.html",
            media_refs=("video/journey.webm",),
            outcome={"passed": 1, "failed": 0, "skipped": None, "flaky": None},
            detail={"cwd": self.cwd, "env": self.env},
        )


@pytest.mark.asyncio
async def test_the_stage_runs_the_preview_with_the_candidate_legs_own_steps(tmp_path: Path) -> None:
    from forge.config.models import DeployStageConfig
    from forge.deploy.live_gate import DryRunBrokerInspector
    from forge.deploy.reservation import InProcessReservationLease
    from forge.deploy.stage import DeployStageRunner
    from forge.persistence.migrations.runbook import apply
    from forge.persistence.repositories.runbook import RunbookRepository

    conn = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(conn)
    repository = RunbookRepository(connection=conn)
    publisher = AsyncMock()
    deploy_publisher = AsyncMock()
    runner = DeployStageRunner(
        repository=repository,
        runbook_publisher=publisher,
        deploy_publisher=deploy_publisher,
        reservation=InProcessReservationLease(),
        live_gate_invoker=_Invoker(),
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "records"),
        dry_run=True,
        clock=lambda: START,
    )
    profile = parse_deploy_profile(
        {
            "env_id": "notes",
            "compose": {"file": "compose.yml", "script": "deploy/deploy.sh"},
            "health_checks": [{"cmd": "deploy/health.sh"}],
            "preview": {"enabled": True, "slots": SLOTS},
        }
    )
    overlay = {"COMPOSE_PROJECT_NAME": "notes-pv0", "FACTORY_PREVIEW_PORT": "8950"}
    tree = "/clone/.forge-candidates/preview-pv000000000001"

    up = await runner.preview_up(
        profile, correlation_id="c", deploy_run_id="r1", preview_env=overlay, cwd=tree
    )
    assert up.outcome == "complete", up
    rb = repository.load_runbook("deploy-pv-r1", correlation_id="c")
    compose, health = rb.steps
    assert compose.step_type == "deploy_compose"
    assert compose.params["extra_env"] == {"PREVIEW": "1", **overlay}
    assert compose.params["cwd"] == tree
    assert health.params["extra_env"] == overlay and health.params["cwd"] == tree

    journey = await runner.preview_journey(
        profile,
        correlation_id="c",
        deploy_run_id="r1",
        feature="FEAT-AB12",
        preview_env={**overlay, "FACTORY_RECORD_VIDEO": "1"},
        cwd=tree,
    )
    assert journey.outcome == "complete" and journey.verdict == "pass"
    answer = journey.detail["journey"]
    assert answer["report_ref"] == "index.html"
    assert answer["media_refs"] == ["video/journey.webm"]
    assert answer["outcome"]["passed"] == 1
    # A preview is not a deploy: nothing was published on the deploy domain.
    assert deploy_publisher.method_calls == []


@pytest.mark.asyncio
async def test_the_dispatch_carries_the_two_preview_legs(tmp_path: Path) -> None:
    from forge.config.models import DeployStageConfig
    from forge.deploy.composition import dispatch_deploy_stage

    runner_calls: dict[str, Any] = {}

    class _Runner:
        async def preview_up(self, profile, **kwargs):
            runner_calls["up"] = kwargs
            return "up"

        async def preview_journey(self, profile, **kwargs):
            runner_calls["journey"] = kwargs
            return "journey"

    import forge.deploy.composition as composition

    original = composition.build_deploy_stage_runner
    composition.build_deploy_stage_runner = lambda *a, **k: _Runner()
    try:
        common = dict(
            correlation_id="c",
            deploy_run_id="r1",
            repository=None,
            runbook_publisher=None,
            deploy_publisher=None,
            candidate_cwd="/tree",
            identity_env={"FACTORY_PREVIEW_PORT": "8950"},
            feature="FEAT-AB12",
        )
        config = DeployStageConfig(enabled=True)
        assert await dispatch_deploy_stage(config, _profile(), leg="preview_up", **common) == "up"
        assert await dispatch_deploy_stage(config, _profile(), leg="preview_journey", **common) == "journey"
        with pytest.raises(ValueError, match="laid-out tree"):
            await dispatch_deploy_stage(
                config, _profile(), leg="preview_up", **{**common, "candidate_cwd": None}
            )
    finally:
        composition.build_deploy_stage_runner = original
    assert runner_calls["up"]["preview_env"] == {"FACTORY_PREVIEW_PORT": "8950"}
    assert runner_calls["up"]["cwd"] == "/tree"
    assert runner_calls["journey"]["feature"] == "FEAT-AB12"


def test_the_ledger_allows_one_live_lease_per_port(connection: sqlite3.Connection) -> None:
    store = PreviewLeaseStore(connection)
    lease = PreviewLease(
        lease_id="pv000000000001", slot=0, port=8950, token="t", project=PROJECT,
        feature_id="FEAT-AB12", build_id="b1", candidate_sha=SHA_CAND,
        candidate_tree=TREE_CAND, preview_tree=None, run_id="r1", compose_project=None,
        evidence_dir=None, state="ready", reason=None, created_at=START.isoformat(),
        app_expires_at=START.isoformat(), evidence_expires_at=START.isoformat(),
    )
    store.insert(lease, now=START)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert(
            PreviewLease(**{**lease.__dict__, "lease_id": "pv000000000002"}), now=START
        )
    # An ended lease on the same port is only a record.
    store.update("pv000000000001", now=START, state="ended")
    store.insert(PreviewLease(**{**lease.__dict__, "lease_id": "pv000000000002"}), now=START)
    assert json.dumps([row.lease_id for row in store.live()]) == '["pv000000000002"]'
