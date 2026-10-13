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
BASE_URL = "https://evidence.example.test/factory-evidence"
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
        "public_url": "https://preview.example.test:8950",
    },
    {
        "port": 8951,
        "internal_port": 18951,
        "public_url": "https://preview.example.test:8951",
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
        # Whether the started preview's containers carry the lease's label,
        # and any more of its compose project's containers that do not.
        self.labelled = True
        self.unlabelled_members: list[str] = []

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

    async def preview_containers(
        self, compose_project: str
    ) -> tuple[set[str], list[tuple[str, str]]] | None:
        self.calls.append(("preview_containers", compose_project))
        lease = self.named("lay_out")[-1][0]
        label = lease if self.labelled else ""
        members = [(f"{compose_project}-app-1", label)]
        members += [(name, "") for name in self.unlabelled_members]
        return ({lease} if self.labelled else set()), members

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
        self.blocking_sleeps: list[float] = []
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
            blocking_sleep=self.blocking_sleeps.append,
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


def test_a_profile_that_cannot_be_read_fails_closed(world: World) -> None:
    """Unreadable twice is not "no preview": the card must not be approvable."""
    asked: list[str] = []

    def broken(project: str):
        asked.append(project)
        raise OSError("sidecar down")

    world.service._profile_reader = broken
    assert world.service.enabled_for(PROJECT) is True
    assert asked == [PROJECT, PROJECT]  # asked once more before giving up
    assert world.blocking_sleeps == [3.0]  # after a short pause


@pytest.mark.asyncio
async def test_an_unreadable_profile_is_a_failed_preview_without_approve(
    world: World,
) -> None:
    def broken(project: str):
        raise OSError("the sandbox sidecar answered 502")

    world.service._profile_reader = broken
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "could not read the deploy profile (OSError: the sandbox sidecar answered 502)"
    )
    assert world.venue.calls == [] and world.dispatch.calls == []
    assert world.service.approval_refusal(
        evidence.model_dump(mode="json"), now=START
    ) is not None


def test_one_failed_read_is_asked_again(world: World) -> None:
    answers = iter([OSError("blip"), world.profile])

    def flaky(project: str):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    world.service._profile_reader = flaky
    assert world.service.enabled_for(PROJECT) is True


def test_the_production_reader_raises_when_the_sidecar_does_not_answer_200(
    monkeypatch,
) -> None:
    from forge.config.models import ForgeConfig
    from forge.deploy.review_preview import _profile_reader_for

    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {PROJECT: "/clone"},
                "sandboxes": {
                    PROJECT: {
                        "name": "notes-sbx",
                        "sidecar_url": "http://127.0.0.1:8225",
                        "runner_url": "http://127.0.0.1:8224",
                    }
                },
            },
        }
    )
    monkeypatch.setattr(
        "forge.planning.sidecar_git_runner._urllib_post",
        lambda url, body, timeout: (502, {"error": "bad gateway"}),
    )
    with pytest.raises(OSError, match="answered 502"):
        _profile_reader_for(config)(PROJECT)


def test_the_production_reader_says_none_for_a_project_without_a_profile(
    tmp_path: Path,
) -> None:
    from forge.config.models import ForgeConfig
    from forge.deploy.review_preview import _profile_reader_for

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "first")
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {PROJECT: str(repo)}},
        }
    )
    assert _profile_reader_for(config)(PROJECT) is None


# ---------------------------------------------------------------------------
# A preview that is prepared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prepared_preview_is_ready_with_its_links_and_evidence(world: World) -> None:
    evidence = await world.service.prepare(_request())

    assert evidence.state == "ready", evidence.failure_reason
    assert evidence.app_url == "https://preview.example.test:8950/?preview=the-right-token"
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
    assert evidence.preview_base_sha == SHA_FETCHED
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
        "FACTORY_PREVIEW_URL": "https://preview.example.test:8950",
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
        "https://preview.example.test:8950/",
        "https://preview.example.test:8951/",
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
    assert third.app_url.startswith("https://preview.example.test:8950/")
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


@pytest.mark.parametrize(
    "files",
    [
        pytest.param({"index.html": b"report"}, id="html-only"),
        pytest.param({"index.html": b"report", "shots/a.png": b"png"}, id="screenshots-only"),
        pytest.param({"index.html": b"report", "video/j.webm": b"webm"}, id="video-only"),
        pytest.param(
            {"index.html": b"report", "shots/a.png": b"png", "trace.zip": b"zip"},
            id="video-only-on-failure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_passing_journey_without_its_recording_is_not_ready(
    world: World, files: dict[str, bytes]
) -> None:
    world.venue.evidence = (_archive(files), None)
    world.dispatch.journey_changes = {"screenshot_refs": [], "media_refs": []}
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "the passing journey left no video/screenshots (failure-only recording?)"
    )
    assert world.lease("pv000000000001").state == "failed"
    assert world.venue.named("remove_project") == ["pv000000000001"]
    assert world.service.approval_refusal(evidence.model_dump(mode="json"), now=START)


@pytest.mark.asyncio
async def test_a_passing_journey_with_video_and_screenshots_is_ready(world: World) -> None:
    world.venue.evidence = (
        _archive({"index.html": b"r", "shots/a.png": b"png", "video/j.webm": b"webm"}),
        None,
    )
    world.dispatch.journey_changes = {"screenshot_refs": [], "media_refs": []}
    evidence = await world.service.prepare(_request())
    assert evidence.state == "ready", evidence.failure_reason
    assert any(url.endswith(".webm") for url in evidence.media_urls)
    assert any(url.endswith(".png") for url in evidence.media_urls)


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


def _evidence_folder(clone: Path, lease: str = "pv000000000001") -> Path:
    return clone / ".forge-candidates" / f"preview-{lease}" / EVIDENCE_FOLDER


def test_an_evidence_folder_that_is_a_link_is_refused(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    folder = _evidence_folder(clone)
    folder.parent.mkdir(parents=True)
    secrets_dir = tmp_path / "sandbox-secrets"
    secrets_dir.mkdir()
    (secrets_dir / "id_rsa").write_text("PRIVATE")
    folder.symlink_to(secrets_dir)

    packed = pack_preview_evidence(clone, lease_id="pv000000000001")
    assert "archive" not in packed
    assert "symbolic link" in packed["refusal"]


def test_a_preview_tree_reached_through_a_link_is_refused(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / EVIDENCE_FOLDER).mkdir(parents=True)
    (elsewhere / EVIDENCE_FOLDER / "index.html").write_text("not ours")
    (clone / ".forge-candidates").mkdir(parents=True)
    (clone / ".forge-candidates" / "preview-pv000000000001").symlink_to(elsewhere)

    packed = pack_preview_evidence(clone, lease_id="pv000000000001")
    assert "archive" not in packed
    assert "symbolic link" in packed["refusal"]


def test_a_folder_swapped_for_a_link_after_the_checks_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """The race: the lstat checks pass, then the folder is a link when opened."""
    clone = tmp_path / "clone"
    folder = _evidence_folder(clone)
    folder.parent.mkdir(parents=True)
    secrets_dir = tmp_path / "sandbox-secrets"
    secrets_dir.mkdir()
    (secrets_dir / "id_rsa").write_text("PRIVATE")
    folder.symlink_to(secrets_dir)
    real_lstat = Path.lstat
    # The checks see what was there a moment earlier: a real folder.
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: self.stat() if self == folder else real_lstat(self),
    )

    packed = pack_preview_evidence(clone, lease_id="pv000000000001")
    assert "archive" not in packed
    assert "without following a link" in packed["refusal"]


def test_a_linked_folder_inside_the_evidence_is_never_walked(tmp_path: Path) -> None:
    import base64

    clone = tmp_path / "clone"
    folder = _evidence_folder(clone)
    (folder / "report").mkdir(parents=True)
    (folder / "report" / "index.html").write_text("ok")
    secrets_dir = tmp_path / "sandbox-secrets"
    secrets_dir.mkdir()
    (secrets_dir / "id_rsa").write_text("PRIVATE")
    (folder / "sub").symlink_to(secrets_dir)

    packed = pack_preview_evidence(clone, lease_id="pv000000000001")
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(packed["archive"]))).getnames()
    assert sorted(names) == ["report", "report/index.html"]


@pytest.mark.asyncio
async def test_every_link_built_from_evidence_names_is_plain(world: World) -> None:
    """File names a project chose are quoted, so no link carries a backslash,
    a space or a control character, and nats-core accepts every one."""
    world.venue.evidence = (
        _archive(
            {
                "index.html": b"report",
                "shots/a b\\c.png": b"png",
                "shots/tab\there.png": b"png",
                "video/a b.webm": b"webm",
            }
        ),
        None,
    )
    world.dispatch.journey_changes = {"screenshot_refs": [], "media_refs": []}
    evidence = await world.service.prepare(_request())
    assert evidence.state == "ready", evidence.failure_reason
    links = [evidence.app_url, evidence.report_url, *evidence.media_urls]
    assert len(links) == 5
    for link in links:
        assert "\\" not in link and not any(ch.isspace() for ch in link)
        assert all(ord(ch) >= 32 for ch in link)
        assert link.startswith("https://") and "@" not in link.split("/")[2]
    assert any("a%20b%5Cc.png" in link for link in links)


def test_the_sidecar_answer_is_read_only_up_to_its_bound() -> None:
    """An over-long answer is refused before it is parsed; a short one reads."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from forge.planning.sidecar_git_runner import _urllib_post

    class _Answer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"archive": "A" * 1000}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/preview/evidence"
        status, decoded = _urllib_post(url, {}, 5.0, max_bytes=100)
        assert status == 413
        assert "larger than 100 bytes" in decoded["error"]
        status, decoded = _urllib_post(url, {}, 5.0, max_bytes=5000)
        assert status == 200 and decoded["archive"] == "A" * 1000
        status, decoded = _urllib_post(url, {}, 5.0)
        assert status == 200 and decoded["archive"] == "A" * 1000
    finally:
        server.shutdown()
        server.server_close()


def test_the_transfer_cap_is_64_mib_on_both_sides() -> None:
    from forge.deploy.review_preview import EVIDENCE_TRANSFER_CAP_BYTES

    assert EVIDENCE_TRANSFER_CAP_BYTES == 64 * 1024 * 1024


def test_an_archive_that_would_unpack_past_the_cap_stops_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    class _Zeros:
        def __init__(self, n: int) -> None:
            self.left = n

        def read(self, k: int = -1) -> bytes:
            k = self.left if k < 0 else min(k, self.left)
            self.left -= k
            return bytes(k)

    buffer = io.BytesIO()
    size = 2 * 1024 * 1024
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("big.bin")
        info.size = size
        archive.addfile(info, _Zeros(size))
    small = buffer.getvalue()
    assert len(small) < 64 * 1024

    dest = tmp_path / "root" / "run"
    with pytest.raises(ValueError, match="unpack to more than"):
        store_evidence_once(small, dest, cap=1024 * 1024)
    assert not dest.exists()


def test_an_archive_with_too_many_entries_stops_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "root" / "run"
    many = _archive({f"f{n}.txt": b"x" for n in range(5)})
    with pytest.raises(ValueError, match="more than 4 entries"):
        store_evidence_once(many, dest, max_members=4)
    assert not dest.exists()


def test_an_archive_larger_than_the_cap_is_refused_before_unpacking(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "root" / "run"
    with pytest.raises(ValueError, match="larger than"):
        store_evidence_once(_archive(EVIDENCE_FILES), dest, cap=16)
    assert not dest.exists()


@pytest.mark.asyncio
async def test_an_oversized_evidence_answer_is_refused_before_it_is_decoded(
    monkeypatch,
) -> None:
    import forge.deploy.review_preview as module
    from forge.deploy.review_preview import SidecarPreviewVenue

    monkeypatch.setattr(module, "EVIDENCE_TRANSFER_CAP_BYTES", 30)
    venue = SidecarPreviewVenue(
        "http://sidecar.example.test",
        repo=PROJECT,
        post=lambda url, body, timeout, **_kw: (200, {"archive": "A" * 100}),
    )
    archive, why = await venue.fetch_evidence("pv000000000001")
    assert archive is None
    assert "larger than" in why


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
    # The folder is still there for the report link, but an ended preview
    # is not approvable: its lease is no longer running.
    assert "no longer running" in world.service.approval_refusal(
        evidence.model_dump(mode="json"), now=START
    )
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


@pytest.mark.asyncio
async def test_a_preview_whose_lease_has_ended_is_not_approvable(world: World) -> None:
    ready = (await world.service.prepare(_request())).model_dump(mode="json")
    assert world.service.approval_refusal(ready, now=START) is None

    await world.service.release(
        feature_id="FEAT-AB12", build_id="build-1", reason="rejected"
    )

    refusal = world.service.approval_refusal(ready, now=START)
    assert refusal == (
        "the preview for this offer is no longer running (rejected), so it cannot "
        "be approved — reject it and run the feature again"
    )


@pytest.mark.asyncio
async def test_a_record_with_no_lease_behind_it_is_not_approvable(world: World) -> None:
    ready = (await world.service.prepare(_request())).model_dump(mode="json")
    forged = {**ready, "run_id": "pvrun-nobodywrote"}
    assert "no longer running" in world.service.approval_refusal(forged, now=START)


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
    assert list_preview_leases(engine) == {
        "leases": ["pv000000000001", "pv000000000002"],
        "running": ["pv000000000001", "pv000000000002"],
    }
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
        if argv[1:3] == ["image", "inspect"]:
            # Reads the labels of images the labelled listing returned.
            continue
        assert filters and all(f.startswith(f"label={LEASE_LABEL}") for f in filters)
        if "--quiet" in argv:
            assert filters == [f"label={LEASE_LABEL}=pv000000000001"]


class FakeImageEngine:
    """An engine with containers and images, each container naming its image."""

    def __init__(
        self,
        containers: dict[str, tuple[str, str]],
        images: dict[str, str],
    ) -> None:
        self.containers = dict(containers)  # id -> (image id, lease label or "")
        self.images = dict(images)  # image id -> lease label
        self.argv: list[list[str]] = []

    def __call__(self, argv) -> tuple[int, str, str]:
        argv = list(argv)
        self.argv.append(argv)
        filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
        if argv[1:3] == ["image", "ls"]:
            lease = filters[0].split("=", 2)[2]
            return 0, "\n".join(i for i, l in self.images.items() if l == lease), ""
        if argv[1:3] == ["image", "rm"]:
            for image in argv[3:]:
                if any(c[0] == image for c in self.containers.values()):
                    return 1, "", f"image {image} is in use"
                self.images.pop(image, None)
            return 0, "", ""
        if argv[1] == "ps" and filters and filters[0].startswith("ancestor="):
            image = filters[0].split("=", 1)[1]
            lines = [f"{cid} {label}" for cid, (img, label) in self.containers.items() if img == image]
            return 0, "\n".join(lines), ""
        if argv[1] == "ps" and "--quiet" in argv:
            lease = filters[0].split("=", 2)[2]
            return 0, "\n".join(c for c, (_i, l) in self.containers.items() if l == lease), ""
        if argv[1] == "rm":
            for cid in argv[3:]:
                self.containers.pop(cid, None)
            return 0, "", ""
        return 0, "", ""


def test_teardown_removes_the_lease_s_images_but_never_one_still_in_use() -> None:
    lease = "pv000000000001"
    engine = FakeImageEngine(
        containers={
            "c1": ("sha256:app", lease),  # the preview's own container
            "c9": ("sha256:shared", ""),  # not a preview: uses a labelled image
        },
        images={"sha256:app": lease, "sha256:shared": lease, "sha256:other": "pv000000000002"},
    )

    answer = remove_preview_resources(lease, engine)

    assert answer == {
        "removed": True,
        "remaining": 0,
        "images_kept_in_use": ["sha256:shared"],
    }
    assert "sha256:app" not in engine.images  # this lease's image, removed
    assert "sha256:shared" in engine.images  # still used by c9, kept
    assert "sha256:other" in engine.images  # another lease's, never touched
    removals = [argv for argv in engine.argv if argv[1:3] == ["image", "rm"]]
    assert removals == [[removals[0][0], "image", "rm", "sha256:app"]]
    assert "--force" not in removals[0]


@pytest.mark.asyncio
async def test_a_preview_that_did_not_label_its_resources_fails(world: World) -> None:
    world.venue.labelled = False
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "the project did not label its preview resources: notes-pv0-app-1 "
        "carries no forge.preview.lease=pv000000000001"
    )
    assert world.venue.named("probe") == []  # stopped before the link check
    assert world.venue.named("remove_project") == ["pv000000000001"]


@pytest.mark.asyncio
async def test_one_unlabelled_container_in_the_preview_s_project_fails_it(
    world: World,
) -> None:
    """Partly labelled is not labelled: the one left out would outlive teardown."""
    world.venue.unlabelled_members = ["notes-pv0-worker-1"]
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == (
        "the project did not label its preview resources: notes-pv0-worker-1 "
        "carries no forge.preview.lease=pv000000000001"
    )
    assert world.venue.named("preview_containers") == ["notes-pv0"]
    assert world.venue.named("probe") == []


def test_the_projects_route_lists_the_compose_project_s_containers() -> None:
    def engine(argv) -> tuple[int, str, str]:
        argv = list(argv)
        filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
        if filters == ["label=com.docker.compose.project=notes-pv0"]:
            return 0, "notes-pv0-app-1 pv000000000001\nnotes-pv0-worker-1 \n", ""
        return 0, "pv000000000001\n", ""

    answer = list_preview_leases(engine, compose_project="notes-pv0")
    assert answer["compose_containers"] == [
        {"name": "notes-pv0-app-1", "lease": "pv000000000001"},
        {"name": "notes-pv0-worker-1", "lease": ""},
    ]
    status, body = serve_preview_operation(
        PREVIEW_PROJECTS_ROUTE,
        {"compose_project": "Bad Name"},
        repo_path=Path("/nowhere"),
        slot_ports=frozenset(),
        run=engine,
    )
    assert status == 400 and "compose project name" in body["error"]


@pytest.mark.asyncio
async def test_running_previews_that_cannot_be_listed_fail_the_preparation(
    world: World,
) -> None:
    async def unknown(compose_project: str) -> None:
        return None

    world.venue.preview_containers = unknown
    evidence = await world.service.prepare(_request())
    assert evidence.state == "failed"
    assert evidence.failure_reason == "the preview's containers could not be listed"


class ContractEngine:
    """A container engine that answers only the commands Docker documents.

    ``docker image ls`` has no ``.Label`` format field (its fields are ID,
    Repository, Tag, Digest, CreatedSince, CreatedAt and Size), so asking for
    one is answered the way Docker answers it: an error. ``ps``, ``network ls``
    and ``volume ls`` do have ``.Label``.
    """

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        # name -> (lease label, running, compose project, image id)
        self.containers = {
            "notes-pv0-app-1": ("pv000000000001", True, "notes-pv0", "sha256:app"),
        }
        self.networks = {"notes-pv0_default": "pv000000000001"}
        self.volumes = {"notes-pv0_data": "pv000000000001"}
        # image id -> lease label; the second is left over by a crashed run.
        self.images = {"sha256:app": "pv000000000001", "sha256:orphan": "pv0000000000aa"}

    def __call__(self, argv) -> tuple[int, str, str]:
        argv = list(argv)[1:]
        self.argv.append(argv)
        label = 'label=forge.preview.lease'
        fmt = argv[argv.index("--format") + 1] if "--format" in argv else None
        if argv[:2] == ["image", "ls"]:
            if fmt is not None and ".Label" in fmt:
                return 1, "", (
                    'template: :1:2: executing "" at <.Label>: can\'t evaluate '
                    "field Label in type *formatter.imageContext"
                )
            if argv == ["image", "ls", "--filter", label, "--format", "{{.ID}}"]:
                return 0, "\n".join(self.images) + "\n", ""
        if argv[:2] == ["image", "inspect"]:
            assert argv[2:4] == ["--format", '{{index .Config.Labels "forge.preview.lease"}}']
            return 0, "\n".join(self.images[i] for i in argv[4:]) + "\n", ""
        if argv == ["ps", "--all", "--filter", label, "--format", '{{.Label "forge.preview.lease"}}']:
            return 0, "\n".join(c[0] for c in self.containers.values()) + "\n", ""
        if argv == ["ps", "--filter", label, "--format", '{{.Label "forge.preview.lease"}}']:
            return 0, "\n".join(c[0] for c in self.containers.values() if c[1]) + "\n", ""
        if argv == ["network", "ls", "--filter", label, "--format", '{{.Label "forge.preview.lease"}}']:
            return 0, "\n".join(self.networks.values()) + "\n", ""
        if argv == ["volume", "ls", "--filter", label, "--format", '{{.Label "forge.preview.lease"}}']:
            return 0, "\n".join(self.volumes.values()) + "\n", ""
        if argv == [
            "ps", "--all", "--filter", "label=com.docker.compose.project=notes-pv0",
            "--format", '{{.Names}} {{.Label "forge.preview.lease"}}',
        ]:
            lines = [f"{n} {c[0]}" for n, c in self.containers.items() if c[2] == "notes-pv0"]
            return 0, "\n".join(lines) + "\n", ""
        return 2, "", f"unexpected command: {argv}"


#: The exact commands the listing sends, in order (for the Dell's own check).
LISTING_ARGV = [
    ["ps", "--all", "--filter", "label=forge.preview.lease", "--format", '{{.Label "forge.preview.lease"}}'],
    ["network", "ls", "--filter", "label=forge.preview.lease", "--format", '{{.Label "forge.preview.lease"}}'],
    ["volume", "ls", "--filter", "label=forge.preview.lease", "--format", '{{.Label "forge.preview.lease"}}'],
    ["image", "ls", "--filter", "label=forge.preview.lease", "--format", "{{.ID}}"],
    ["image", "inspect", "--format", '{{index .Config.Labels "forge.preview.lease"}}', "sha256:app", "sha256:orphan"],
    ["ps", "--filter", "label=forge.preview.lease", "--format", '{{.Label "forge.preview.lease"}}'],
    [
        "ps", "--all", "--filter", "label=com.docker.compose.project=notes-pv0",
        "--format", '{{.Names}} {{.Label "forge.preview.lease"}}',
    ],
]


def test_the_listing_sends_only_documented_commands_and_finds_labelled_images() -> None:
    engine = ContractEngine()
    answer = list_preview_leases(engine, compose_project="notes-pv0")
    assert engine.argv == LISTING_ARGV
    # The lease that is left only as a labelled image is found too.
    assert answer["leases"] == ["pv000000000001", "pv0000000000aa"]
    assert answer["running"] == ["pv000000000001"]
    assert answer["compose_containers"] == [
        {"name": "notes-pv0-app-1", "lease": "pv000000000001"}
    ]


def test_an_image_listing_that_asks_for_label_is_refused_by_the_engine() -> None:
    """The regression this guards: ``.Label`` on ``image ls`` is an error."""
    engine = ContractEngine()
    code, _out, err = engine(
        ["docker", "image", "ls", "--filter", "label=forge.preview.lease",
         "--format", '{{.Label "forge.preview.lease"}}']
    )
    assert code == 1 and "imageContext" in err


def test_with_no_labelled_images_nothing_is_inspected() -> None:
    engine = ContractEngine()
    engine.images = {}
    list_preview_leases(engine)
    assert not any(argv[:2] == ["image", "inspect"] for argv in engine.argv)


def test_the_teardown_commands_are_pinned() -> None:
    lease = "pv000000000001"
    engine = FakeImageEngine(
        containers={"c1": ("sha256:app", lease)},
        images={"sha256:app": lease},
    )
    remove_preview_resources(lease, engine)
    selector = f"label=forge.preview.lease={lease}"
    assert [argv[1:] for argv in engine.argv] == [
        ["ps", "--all", "--quiet", "--filter", selector],
        ["rm", "--force", "c1"],
        ["network", "ls", "--quiet", "--filter", selector],
        ["volume", "ls", "--quiet", "--filter", selector],
        ["image", "ls", "--quiet", "--no-trunc", "--filter", selector],
        ["ps", "--all", "--filter", "ancestor=sha256:app", "--format",
         '{{.ID}} {{.Label "forge.preview.lease"}}'],
        ["image", "rm", "sha256:app"],
        ["ps", "--all", "--quiet", "--filter", selector],
        ["network", "ls", "--quiet", "--filter", selector],
        ["volume", "ls", "--quiet", "--filter", selector],
        ["image", "ls", "--quiet", "--no-trunc", "--filter", selector],
    ]


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
    assert (status, body) == (
        200, {"leases": ["pv000000000001"], "running": ["pv000000000001"]}
    )
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
    """Answers with the journey's evidence only when it was asked to read it."""

    def __init__(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        journey: bool = False,
    ) -> None:
        self.cwd, self.env, self.journey = cwd, dict(env or {}), journey

    def with_repo_path(self, path: Any) -> "_Invoker":
        return _Invoker(str(path), self.env, self.journey)

    def with_extra_env(self, overlay: dict[str, str]) -> "_Invoker":
        return _Invoker(self.cwd, {**self.env, **overlay}, self.journey)

    def with_journey_evidence(self) -> "_Invoker":
        return _Invoker(self.cwd, self.env, True)

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        from forge.deploy.live_gate import LiveGateInvocation

        if not self.journey:
            return LiveGateInvocation(verdict="pass", run_id="journey-1")
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


# ---------------------------------------------------------------------------
# Cancellation never leaves a preview behind (Codex R8)
# ---------------------------------------------------------------------------


async def _cancel_when(world: World, blocked: "asyncio.Event", task: "asyncio.Task") -> None:
    await asyncio.wait_for(blocked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancelled_while_waiting_for_a_slot(world: World) -> None:
    await world.service.prepare(_request("build-1", "FEAT-AB12"))
    await world.service.prepare(_request("build-2", "FEAT-CD34"))
    blocked = asyncio.Event()

    async def hold() -> None:
        blocked.set()
        await asyncio.Event().wait()

    world.clock.on_sleep = hold
    task = asyncio.create_task(world.service.prepare(_request("build-3", "FEAT-EF56")))
    await _cancel_when(world, blocked, task)

    lease = world.lease("pv000000000003")
    assert (lease.state, lease.reason) == ("failed", "the preparation was cancelled")
    assert lease.slot is None
    assert "pv000000000003" in world.venue.named("remove_tree")


@pytest.mark.asyncio
async def test_cancelled_right_after_the_preview_was_started(world: World) -> None:
    blocked = asyncio.Event()

    async def containers(compose_project: str) -> Any:
        blocked.set()
        await asyncio.Event().wait()

    world.venue.preview_containers = containers
    task = asyncio.create_task(world.service.prepare(_request()))
    await _cancel_when(world, blocked, task)

    assert world.dispatch.legs() == ["preview_up"]
    lease = world.lease("pv000000000001")
    assert (lease.state, lease.reason) == ("failed", "the preparation was cancelled")
    assert world.venue.named("remove_project") == ["pv000000000001"]
    assert world.store.live() == []


@pytest.mark.asyncio
async def test_cancelled_during_the_journey(world: World) -> None:
    blocked = asyncio.Event()
    real = world.dispatch.__call__

    async def dispatch(**kwargs: Any) -> Any:
        if kwargs["leg"] == "preview_journey":
            blocked.set()
            await asyncio.Event().wait()
        return await real(**kwargs)

    world.service._dispatch = dispatch
    task = asyncio.create_task(world.service.prepare(_request()))
    await _cancel_when(world, blocked, task)

    lease = world.lease("pv000000000001")
    assert (lease.state, lease.reason) == ("failed", "the preparation was cancelled")
    assert world.venue.named("remove_project") == ["pv000000000001"]
    assert world.store.live() == []


@pytest.mark.asyncio
async def test_the_sweep_reclaims_a_preparation_that_never_finished(world: World) -> None:
    stale = PreviewLease(
        lease_id="pv0000000000bb", slot=0, port=8950, token="t", project=PROJECT,
        feature_id="FEAT-AB12", build_id="b1", candidate_sha=SHA_CAND,
        candidate_tree=TREE_CAND, preview_tree=None, run_id="r1",
        compose_project="notes-pv0", evidence_dir=None, state="preparing", reason=None,
        created_at=(START - timedelta(days=30)).isoformat(),
        app_expires_at=START.isoformat(), evidence_expires_at=START.isoformat(),
    )
    world.store.insert(stale, now=START)
    young = PreviewLease(
        **{
            **stale.__dict__,
            "lease_id": "pv0000000000cc",
            "slot": 1,
            "port": 8951,
            "created_at": (START - timedelta(minutes=50)).isoformat(),
        }
    )
    world.store.insert(young, now=START)

    await world.service.sweep(now=START)

    ended = world.lease("pv0000000000bb")
    assert (ended.state, ended.reason) == ("failed", "its preparation never finished")
    assert world.venue.named("remove_project") == ["pv0000000000bb"]
    # One that could still be running is left alone.
    assert world.lease("pv0000000000cc").state == "preparing"


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


# ---------------------------------------------------------------------------
# The slot's public address: the profile's, else the coordinator's host
# ---------------------------------------------------------------------------

SLOTS_WITHOUT_ADDRESSES = [
    {"port": 8950, "internal_port": 18950},
    {"port": 8951, "internal_port": 18951},
]


@pytest.mark.asyncio
async def test_a_profile_without_addresses_uses_the_coordinator_s_host(
    world: World,
) -> None:
    world.profile = _profile(slots=SLOTS_WITHOUT_ADDRESSES)
    world.service._public_host = "preview.example.test"

    evidence = await world.service.prepare(_request())

    assert evidence.state == "ready", evidence.failure_reason
    assert evidence.app_url == "https://preview.example.test:8950/?preview=the-right-token"
    up = next(call for call in world.dispatch.calls if call["leg"] == "preview_up")
    assert up["identity_env"]["FACTORY_PREVIEW_URL"] == "https://preview.example.test:8950"


@pytest.mark.asyncio
async def test_a_profile_s_own_address_still_wins(world: World) -> None:
    world.service._public_host = "other.example.test"

    evidence = await world.service.prepare(_request())

    assert evidence.app_url.startswith("https://preview.example.test:8950/?preview=")


@pytest.mark.asyncio
async def test_with_no_address_anywhere_nothing_is_prepared(world: World) -> None:
    world.profile = _profile(slots=SLOTS_WITHOUT_ADDRESSES)

    evidence = await world.service.prepare(_request())

    assert evidence.state == "failed"
    assert evidence.failure_reason == "no public address configured for preview slot 8950"
    assert world.venue.calls == [] and world.dispatch.calls == []
    assert world.service.approval_refusal(evidence.model_dump(mode="json"), now=START)


def test_the_production_service_takes_the_coordinator_s_host(tmp_path: Path) -> None:
    from forge.config.models import ForgeConfig
    from forge.deploy.review_preview import build_review_preview_service

    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "review_preview": {"enabled": True, "public_host": "preview.example.test"},
        }
    )
    cx = connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    service = build_review_preview_service(
        config, connection=cx, deploy_dispatcher=AsyncMock()
    )
    assert service._public_host == "preview.example.test"


@pytest.mark.parametrize(
    "host",
    [
        "h\x80x.test", "0x7f000001", "a|b.test", "a..b",
        "127.0.0x1", "127.0x0.0.1", "a.123", "a.0x1", "a.0X1F",
        "preview.example.test\n", "\npreview.example.test",
    ],
)
@pytest.mark.asyncio
async def test_a_host_that_is_not_a_dns_name_never_reaches_a_link(
    world: World, host: str
) -> None:
    """Even handed to the service directly, past the settings' own check."""
    world.profile = _profile(slots=SLOTS_WITHOUT_ADDRESSES)
    world.service._public_host = host

    evidence = await world.service.prepare(_request())

    assert evidence.state == "failed"
    assert evidence.app_url is None
    assert evidence.failure_reason == "no public address configured for preview slot 8950"
    assert world.store.live() == []


@pytest.mark.asyncio
async def test_a_mixed_case_host_is_used_lower_cased(world: World) -> None:
    world.profile = _profile(slots=SLOTS_WITHOUT_ADDRESSES)
    world.service._public_host = "Preview.Example.Test"

    evidence = await world.service.prepare(_request())

    assert evidence.app_url.startswith("https://preview.example.test:8950/?preview=")


@pytest.mark.parametrize(
    "host",
    ["preview.example.test", "123.example.test", "xn--exmple-cua.test", "a" * 63 + ".test"],
)
@pytest.mark.asyncio
async def test_an_ordinary_dns_name_reaches_the_link(world: World, host: str) -> None:
    world.profile = _profile(slots=SLOTS_WITHOUT_ADDRESSES)
    world.service._public_host = host

    evidence = await world.service.prepare(_request())

    assert evidence.state == "ready", evidence.failure_reason
    assert evidence.app_url.startswith(f"https://{host}:8950/?preview=")
