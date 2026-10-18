"""Stopping a mission: landings settled honestly, and the finisher fenced (18 October 2026).

Missions design, section 11, its Tests list: the press's half. The repository
is real git with a bare "remote" on disk (the landing harness of
``test_mission_landing``); the build system, the deploy stage and the
publisher are stand-ins at the boundaries the press already has.

- The decision row is made inside the repository lock, in one transaction
  that re-reads the stop: a stop that commits first means no decision and no
  press; a landing already decided goes on to its end and is LANDED.
- Before any FAILED landing report, on the retry-limit path and the exception
  path, the remote is read: a send that reached it is settled published and
  LANDED; one that did not is settled "publication pending"; an unreadable
  remote writes nothing. Recovery re-checks a reported landing whose record
  never settled.
- The finisher re-checks the stop at the frozen tip, the merge build and the
  offer: a stop that lands while it is paused leaves nothing recorded after it
  and no card. The press refuses a stopped mission's own merge build.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.lifecycle.mission_builds import mission_merge_build_id
from forge.lifecycle.missions import MissionStore, attempt_correlation_id
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import merge_executor, mission_finish
from forge.pipeline.merge_executor import (
    MERGE_DECISION_TARGET_IDENTIFIER,
    MERGE_REPORT_TARGET_IDENTIFIER,
    MISSION_LANDING_REQUEST_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
    MissionMergeRefused,
    execute_merge_deploy,
)
from forge.pipeline.mission_finish import finish_ready_mission
from forge.pipeline.publication_record import (
    LINE_DONE,
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_TO_MISSION_BRANCH,
    STEP_SEND,
    PublicationRecordStore,
)
from forge.planning.mission_step import MissionStep, mission_landing
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.work_queue_loop import LANDED, count_in_flight
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    REPO,
    _git,
    _receipts_env,
    pool,
    repo_root,
)
from tests.forge.pipeline import test_merge_executor as merge_executor_fakes
from tests.forge.pipeline.test_mission_finish import _finish_deps, _land
from tests.forge.pipeline.test_mission_landing import (  # noqa: F401 - fixtures
    B_BUILD,
    MISSION_BRANCH,
    MISSION_CORRELATION,
    MISSION_ID,
    JoinsTheBranch,
    a_mission_feature_build,
    a_running_mission,
    config_on,
    press_deps,
    remote_tip,
    the_mission_branch_on_the_remote,
)

B_CID = attempt_correlation_id(MISSION_CORRELATION, "B", 1)


def _stages(pool: SqliteLifecyclePersistence, target: str) -> list[Any]:  # noqa: F811
    return [s for s in pool.read_stages(B_BUILD) if s.target_identifier == target]


def _record(pool: SqliteLifecyclePersistence) -> Any:  # noqa: F811
    return PublicationRecordStore(pool.connection).read(B_BUILD)


def _stop(pool: SqliteLifecyclePersistence) -> Any:  # noqa: F811
    answer = MissionStore(pool.connection).request_stop(MISSION_ID, requested_by="rich")
    assert answer.accepted, answer.sentence
    return answer


def _planned(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
    """B's planning run, handed over (the mission step reads it)."""
    runs = SqlitePlanningRunStore(pool.connection, target_terminal_enabled=True)
    runs.record_queued(
        correlation_id=B_CID,
        originating_user="rich",
        expected_approver="rich",
        request_text="feature B",
        triggered_by="jarvis",
        target_repo=REPO,
    )
    pool.connection.execute(
        "UPDATE planning_runs SET state = 'BUILD_QUEUED' WHERE correlation_id = ?", (B_CID,)
    )


def _step(pool: SqliteLifecyclePersistence) -> MissionStep:  # noqa: F811
    missions = MissionStore(pool.connection)
    runs = SqlitePlanningRunStore(pool.connection, target_terminal_enabled=True)

    async def start(_admission: Any) -> None:
        raise AssertionError("nothing is admitted for a stopped mission")

    return MissionStep(
        missions,
        planning_run=runs.get_run,
        count_in_flight=lambda: count_in_flight(pool.connection),
        max_in_flight=4,
        start_run=start,
        failure_reason=lambda _run, state, _cid: state,
        clock=lambda: datetime.now(UTC),
    )


class PausedJoin(JoinsTheBranch):
    """The build system's merge, held until the test lets it go."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.go = asyncio.Event()

    async def __call__(self, **kwargs: Any) -> Any:
        if list(kwargs["args"])[:1] == ["merge"]:
            self.entered.set()
            await self.go.wait()
        return await super().__call__(**kwargs)


def _a_send_whose_answer_was_lost(
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    *,
    on_the_remote: bool,
) -> str:
    """A joined commit J, and a record that said "about to send" and stopped.

    With ``on_the_remote`` the send had landed: J is on the mission branch.
    The record's lease ran out long ago, and its result is empty.
    """
    _git(repo_root, "fetch", "-q", "origin")
    _git(repo_root, "checkout", "-q", "-b", "the-join", f"origin/{MISSION_BRANCH}")
    (repo_root / "joined.txt").write_text("joined\n", encoding="utf-8")
    _git(repo_root, "add", "joined.txt")
    _git(repo_root, "commit", "-q", "-m", "the join")
    j_commit = _git(repo_root, "rev-parse", "HEAD")
    if on_the_remote:
        _git(repo_root, "push", "-q", "origin", f"HEAD:refs/heads/{MISSION_BRANCH}")
    _git(repo_root, "checkout", "-q", "main")
    store = PublicationRecordStore(pool.connection)
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    grant = store.take_lease(
        build_id=B_BUILD,
        holder="merge-press:old",
        now=long_ago,
        seconds=60,
        feature_id="FEAT-MX1",
        repo=REPO,
        target_branch=MISSION_BRANCH,
    )
    assert grant is not None
    assert store.record(build_id=B_BUILD, turn=grant.turn, now=long_ago, j_commit=j_commit)
    assert store.about_to(
        build_id=B_BUILD,
        turn=grant.turn,
        now=long_ago,
        step=STEP_SEND,
        attempt=1,
        inputs={"j_commit": j_commit, "target_branch": MISSION_BRANCH},
    )
    return j_commit


def _decided_with_every_attempt_used(
    consumer: MergeApprovalConsumer, pool: SqliteLifecyclePersistence, used: int  # noqa: F811
) -> None:
    assert consumer._decide_landing(
        B_BUILD,
        mission_id=MISSION_ID,
        decided_by=f"mission {MISSION_ID} (approved by rich)",
        request_id=f"merge-{B_BUILD}",
    )
    row = pool.get_build_row(B_BUILD)
    for number in range(1, used + 1):
        consumer._record_landing_attempt(row, number)


# ---------------------------------------------------------------------------
# A stop and a landing decision, in both orders
# ---------------------------------------------------------------------------


class TestAStopAndALandingDecision:
    @pytest.mark.asyncio
    async def test_a_stop_first_means_no_decision_and_no_press(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, joins, deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        _stop(pool)

        assert await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD) is None

        assert _stages(pool, MERGE_DECISION_TARGET_IDENTIFIER) == []
        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "FAILED"
        assert reports[0].details["detail"].endswith("mission stopped")
        assert joins.calls == [] and publisher.asked == [] and deploy.calls == []

    @pytest.mark.asyncio
    async def test_a_stop_while_the_landing_waits_for_the_lock_before_its_decision(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        lock = consumer._lock_for(REPO)
        await lock.acquire()  # another press holds the repository
        landing = asyncio.create_task(consumer.land_mission_feature(B_BUILD))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if _stages(pool, MISSION_LANDING_REQUEST_TARGET_IDENTIFIER):
                break
        assert _stages(pool, MISSION_LANDING_REQUEST_TARGET_IDENTIFIER)
        assert _stages(pool, MERGE_DECISION_TARGET_IDENTIFIER) == []
        _stop(pool)
        lock.release()
        assert await landing is None

        assert _stages(pool, MERGE_DECISION_TARGET_IDENTIFIER) == []
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)[0].status == "FAILED"
        assert joins.calls == [] and publisher.asked == []

    @pytest.mark.asyncio
    async def test_a_landing_decided_before_the_stop_finishes_and_is_landed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        joins = PausedJoin()
        deps, _joins, _deploy, publisher, _bus = press_deps(
            config_on, pool, repo_root, joins=joins
        )
        landing = asyncio.create_task(MergeApprovalConsumer(deps).land_mission_feature(B_BUILD))
        await asyncio.wait_for(joins.entered.wait(), timeout=10)
        assert len(_stages(pool, MERGE_DECISION_TARGET_IDENTIFIER)) == 1
        answer = _stop(pool)  # the landing is already in the press
        assert "work already merging into the mission branch will finish" in answer.sentence
        joins.go.set()
        outcome = await landing

        assert outcome is not None and outcome.status == "PASSED"
        assert _record(pool).result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert remote_tip(repo_root, MISSION_BRANCH) == _record(pool).j_commit
        assert [a["target_branch"] for a in publisher.asked] == [MISSION_BRANCH]
        # The mission step records it LANDED, and the stopped mission drains.
        assert mission_landing(pool.connection, B_CID)[0] == LANDED
        await _step(pool).run_once()
        mission = MissionStore(pool.connection).get(MISSION_ID)
        assert MissionStore(pool.connection).attempt_by_correlation_id(B_CID).status == "LANDED"
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 1 landed on the branch, 0 did not")


# ---------------------------------------------------------------------------
# Reconcile before failing (every mission, R4)
# ---------------------------------------------------------------------------


class TestASendOnTheLastAllowedAttempt:
    @pytest.mark.asyncio
    async def test_a_crash_after_the_send_is_reconciled_as_landed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        j_commit = _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)

        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert record.lease_holder is None
        found = [
            line for line in record.lines
            if line.kind == LINE_DONE and line.step == STEP_SEND
        ]
        assert found and found[-1].detail["found_by_looking"] is True
        assert found[-1].detail["published"] is True
        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "PASSED"
        assert remote_tip(repo_root, MISSION_BRANCH) == j_commit
        assert joins.calls == [] and publisher.asked == []  # nothing pressed again
        assert mission_landing(pool.connection, B_CID)[0] == LANDED

    @pytest.mark.asyncio
    async def test_a_send_that_never_reached_the_remote_is_settled_not_published(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=False)
        _decided_with_every_attempt_used(consumer, pool, used=3)

        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result == RESULT_PUBLICATION_PENDING and record.lease_holder is None
        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "FAILED"

    @pytest.mark.asyncio
    async def test_a_remote_that_cannot_be_read_writes_nothing(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)

        async def unreachable(*_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(ok=False, refusal="the remote could not be reached")

        monkeypatch.setattr(merge_executor, "target_branch_now", unreachable)
        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result is None and record.lease_holder is None
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []
        # The attempt waits; it is never turned into FAILED on an unknown.
        assert MissionStore(pool.connection).attempt_by_correlation_id(B_CID).status == "ADMITTED"

    @pytest.mark.asyncio
    async def test_the_exception_path_reconciles_too(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=2)

        async def the_press_dies(**_kwargs: Any) -> Any:
            raise RuntimeError("the coordinator's press stopped mid-way")

        monkeypatch.setattr(merge_executor, "execute_merge_deploy", the_press_dies)
        assert await consumer.land_mission_feature(B_BUILD) is None

        assert _record(pool).result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)[0].status == "PASSED"


class TestRecoveryReChecksAnUnsettledReportedLanding:
    @pytest.mark.asyncio
    async def test_a_reported_landing_with_no_result_is_settled_by_looking(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        press, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(press)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        # An older press reported FAILED and never settled its record.
        consumer._landing_failed(pool.get_build_row(B_BUILD), "the press gave up")
        assert mission_landing(pool.connection, B_CID)[0] != LANDED

        async def offer(**_kwargs: Any) -> bool:
            raise AssertionError("a RUNNING mission is not offered")

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        deps.reconcile = consumer.reconcile_mission_landing
        started = await mission_finish.finish_ready_missions(deps, wait=True)

        assert started == [B_BUILD]
        assert _record(pool).result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert mission_landing(pool.connection, B_CID)[0] == LANDED
        # Settled: nothing more to look at.
        assert await mission_finish.finish_ready_missions(deps, wait=True) == []


# ---------------------------------------------------------------------------
# The finisher and the final merge
# ---------------------------------------------------------------------------


class _PausedFetch:
    """The press's git venue, with the mission-branch fetch held."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.waiting = asyncio.Event()
        self.go = asyncio.Event()

    async def fetch_remote_start_point(self, *args: Any, **kwargs: Any) -> Any:
        self.waiting.set()
        await self.go.wait()
        return await self._inner.fetch_remote_start_point(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestTheFinisherIsFenced:
    @pytest.mark.asyncio
    async def test_a_stop_while_the_finisher_fetches_records_nothing_after_it(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        main_tip = _git(repo_root, "rev-parse", "main")
        a_running_mission(pool, keys=("B",), base_commit=main_tip)
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        offers: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offers.append(kwargs)
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        paused: list[_PausedFetch] = []
        inner_git_for = deps.git_for

        def git_for(repo: str, root: Path) -> Any:
            paused.append(_PausedFetch(inner_git_for(repo, root)))
            return paused[-1]

        deps.git_for = git_for
        finishing = asyncio.create_task(finish_ready_mission(deps, MISSION_ID))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if paused and paused[0].waiting.is_set():
                break
        assert paused and paused[0].waiting.is_set()
        _stop(pool)
        paused[0].go.set()

        assert await finishing == "stopping"
        mission = store.get(MISSION_ID)
        assert mission.final_commit is None
        assert mission_merge_build_id(pool, MISSION_ID) is None
        assert offers == []
        # A stopped READY mission is never offered again, and it drains.
        assert await finish_ready_mission(deps, MISSION_ID) == "stopping"
        await _step(pool).run_once()
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 1 landed on the branch, 0 did not")

    @pytest.mark.asyncio
    async def test_a_stop_just_before_the_merge_build_leaves_no_build_and_no_card(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main_tip = _git(repo_root, "rev-parse", "main")
        a_running_mission(pool, keys=("B",), base_commit=main_tip)
        the_mission_branch_on_the_remote(repo_root)
        await _land(config_on, pool, repo_root)
        offers: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offers.append(kwargs)
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        real = pool.record_mission_merge_build

        def stop_first(**kwargs: Any) -> Any:
            _stop(pool)  # the stop commits while the finisher is mid-offer
            return real(**kwargs)

        monkeypatch.setattr(pool, "record_mission_merge_build", stop_first)

        assert await finish_ready_mission(deps, MISSION_ID) == "stopping"
        assert mission_merge_build_id(pool, MISSION_ID) is None
        assert offers == []

    @pytest.mark.asyncio
    async def test_the_press_refuses_a_stopped_missions_merge_build(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        build_id = pool.record_mission_merge_build(
            mission_id=MISSION_ID,
            correlation_id=MISSION_CORRELATION,
            feature_id="FEAT-MX1",
            repo=REPO,
            branch=MISSION_BRANCH,
            feature_yaml_path="f.yaml",
            originating_user="rich",
            start_commit="0" * 40,
            target_branch="main",
            queued_at=datetime.now(UTC),
        )
        assert build_id is not None
        # Never reachable through the request (refused once a merge build
        # exists): written directly, to show the press's own refusal.
        pool.connection.execute(
            "UPDATE missions SET stop_requested_at = ?, stop_requested_by = 'rich' WHERE id = ?",
            (datetime.now(UTC).isoformat(), MISSION_ID),
        )
        deps, joins, *_ = press_deps(config_on, pool, repo_root)
        with pytest.raises(MissionMergeRefused, match="was stopped by rich"):
            await execute_merge_deploy(
                deps=deps,
                build_id=build_id,
                feature_id="FEAT-MX1",
                repo=REPO,
                repo_root=repo_root,
                expect_main_sha="0" * 40,
                correlation_id=MISSION_CORRELATION,
                decided_by="rich",
            )
        assert joins.calls == []


# ---------------------------------------------------------------------------
# Coach round 1: across a restart, and what holds the drain open
# ---------------------------------------------------------------------------


def _resuming_deps(config_on: ForgeConfig, pool: SqliteLifecyclePersistence, consumer: Any) -> Any:  # noqa: F811
    async def offer(**_kwargs: Any) -> bool:
        raise AssertionError("a RUNNING mission is not offered")

    deps, _thread = _finish_deps(config_on, pool, offer=offer)
    deps.land = consumer.land_mission_feature
    mission_finish._resuming.clear()
    return deps


class TestAcrossARestart:
    @pytest.mark.asyncio
    async def test_a_decision_made_before_the_stop_is_resumed_and_landed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        # The decision was made, then the coordinator stopped before the press.
        assert MergeApprovalConsumer(deps)._decide_landing(
            B_BUILD, mission_id=MISSION_ID, decided_by="x", request_id=f"merge-{B_BUILD}"
        )
        _stop(pool)
        restarted = MergeApprovalConsumer(deps)
        resuming = _resuming_deps(config_on, pool, restarted)
        assert mission_finish._landings_to_resume(resuming, pool.connection) == [(B_BUILD, True)]

        outcome = await restarted.land_mission_feature(B_BUILD)

        assert outcome is not None and outcome.status == "PASSED"
        assert _record(pool).result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert len(_stages(pool, MERGE_DECISION_TARGET_IDENTIFIER)) == 1
        assert mission_landing(pool.connection, B_CID)[0] == LANDED
        await _step(pool).run_once()
        mission = MissionStore(pool.connection).get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 1 landed on the branch, 0 did not")

    @pytest.mark.asyncio
    async def test_a_landing_requested_then_stopped_across_a_restart_never_sends(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        tip = the_mission_branch_on_the_remote(repo_root)
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        # Asked to land, then the coordinator stopped before the lock.
        MergeApprovalConsumer(deps)._record_landing_requested(pool.get_build_row(B_BUILD))
        _stop(pool)
        resuming = _resuming_deps(config_on, pool, MergeApprovalConsumer(deps))

        assert B_BUILD in await mission_finish.finish_ready_missions(resuming, wait=True)

        assert _stages(pool, MERGE_DECISION_TARGET_IDENTIFIER) == []
        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "FAILED"
        assert joins.calls == [] and publisher.asked == []
        assert remote_tip(repo_root, MISSION_BRANCH) == tip
        await _step(pool).run_once()
        mission = MissionStore(pool.connection).get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 0 landed on the branch, 1 did not")

    @pytest.mark.asyncio
    async def test_a_landing_requested_with_no_stop_is_landed_after_a_restart(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        MergeApprovalConsumer(deps)._record_landing_requested(pool.get_build_row(B_BUILD))
        resuming = _resuming_deps(config_on, pool, MergeApprovalConsumer(deps))

        await mission_finish.finish_ready_missions(resuming, wait=True)

        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "PASSED"
        assert _record(pool).result == RESULT_PUBLISHED_TO_MISSION_BRANCH


class TestWhatHoldsTheDrainOpen:
    def test_a_held_lease_after_the_attempt_closed(
        self, pool: SqliteLifecyclePersistence  # noqa: F811
    ) -> None:
        store = a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        store.mark_landed(B_CID)
        _stop(pool)
        pool.connection.execute(
            "INSERT INTO publication_records (build_id, result, lease_holder, lease_expires_at) "
            "VALUES (?, ?, 'merge-press:1', ?)",
            (
                B_BUILD,
                RESULT_PUBLISHED_TO_MISSION_BRANCH,
                (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            ),
        )
        assert store.close_drained(MISSION_ID, active_build_states=["QUEUED", "RUNNING"]) is None
        pool.connection.execute(
            "UPDATE publication_records SET lease_holder = NULL, lease_expires_at = NULL"
        )
        assert store.close_drained(MISSION_ID, active_build_states=["QUEUED", "RUNNING"]) is not None

    def test_an_active_build_after_the_attempt_closed(
        self, pool: SqliteLifecyclePersistence  # noqa: F811
    ) -> None:
        store = a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        pool.connection.execute("UPDATE builds SET status = 'RUNNING' WHERE build_id = ?", (B_BUILD,))
        store.mark_failed(B_CID, "its run was refused")
        _stop(pool)
        assert store.close_drained(MISSION_ID, active_build_states=["QUEUED", "RUNNING"]) is None
        pool.connection.execute("UPDATE builds SET status = 'FAILED' WHERE build_id = ?", (B_BUILD,))
        assert store.close_drained(MISSION_ID, active_build_states=["QUEUED", "RUNNING"]) is not None


class TestASettlementThatWasTakenOver:
    @pytest.mark.asyncio
    async def test_a_refused_write_is_unknown_and_reports_nothing(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)
        # Another worker takes the record over the moment this one looks.
        monkeypatch.setattr(PublicationRecordStore, "done", lambda self, **_k: False)

        assert await consumer.land_mission_feature(B_BUILD) is None

        assert _record(pool).result is None
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []

    @pytest.mark.asyncio
    async def test_the_offer_re_checks_the_stop(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        main_tip = _git(repo_root, "rev-parse", "main")
        a_running_mission(pool, keys=("B",), base_commit=main_tip)
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        offers: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offers.append(kwargs)
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        real = pool.record_mission_merge_build

        def recorded_then_stopped(**kwargs: Any) -> Any:
            build_id = real(**kwargs)
            # Never reachable through the request (refused once a merge build
            # exists): written directly, to show the offer's own re-check.
            pool.connection.execute(
                "UPDATE missions SET stop_requested_at = ?, stop_requested_by = 'rich' "
                "WHERE id = ?",
                (datetime.now(UTC).isoformat(), MISSION_ID),
            )
            return build_id

        pool.record_mission_merge_build = recorded_then_stopped  # type: ignore[method-assign]
        assert await finish_ready_mission(deps, MISSION_ID) == "stopping"
        assert offers == []
        assert store.get(MISSION_ID).final_commit is not None


# ---------------------------------------------------------------------------
# Codex implementation round 1, R1: a settlement is never left held
# ---------------------------------------------------------------------------


class TestASettlementIsNeverLeftHeld:
    @pytest.mark.asyncio
    async def test_a_crash_straight_after_the_settlement_cannot_leave_the_lease_held(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The result and the release are one write: a coordinator that stops
        the instant after the settlement has nothing left to put down, so the
        stopped mission drains and maintenance is free again."""
        from forge.lifecycle.feature_routing import router_restart_blockers
        from tests.forge.deploy.test_rollout_quiesce_back import q, r

        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)
        _stop(pool)

        def the_coordinator_stops(self: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("the coordinator stopped before it put the lease down")

        monkeypatch.setattr(PublicationRecordStore, "release_lease", the_coordinator_stops)
        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert record.lease_holder is None and record.lease_expires_at is None
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)[0].status == "PASSED"
        await _step(pool).run_once()
        mission = MissionStore(pool.connection).get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 1 landed on the branch, 0 did not")
        assert not any(
            b.startswith(("mission:", "publication-")) for b in router_restart_blockers(pool.connection)
        )
        ledger = Path(pool.connection.execute("PRAGMA database_list").fetchone()[2])
        problems = q.work_problems(r.ledger_state(ledger))
        assert [x for x in problems if x["table"] in ("publication_records", "missions")] == []

    @pytest.mark.asyncio
    async def test_a_live_unexpired_lease_is_still_left_alone(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)
        live_until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        pool.connection.execute(
            "UPDATE publication_records SET lease_holder = 'merge-press:live', "
            "lease_expires_at = ? WHERE build_id = ?",
            (live_until, B_BUILD),
        )

        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result is None
        assert record.lease_holder == "merge-press:live"
        assert record.lease_expires_at == live_until
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []


# ---------------------------------------------------------------------------
# Codex implementation rounds 2 and 3, R2: a settled landing whose bookkeeping
# did not finish (the press stopped after its result)
# ---------------------------------------------------------------------------


class _TheCoordinatorStopped(BaseException):
    """The process going away: nothing in the press catches it."""


class _ACheckThatLeavesNothingStanding(merge_executor_fakes._FakeDeploy):
    """A candidate check that answers with nothing left standing."""

    async def __call__(self, **kwargs: Any) -> Any:
        answer = await super().__call__(**kwargs)
        if kwargs.get("leg") == "candidate_check" and answer is not None:
            answer.detail = {**answer.detail, "candidate": "absent"}
        return answer


class TestASettledLandingWhoseBookkeepingDidNotFinish:
    async def _the_press_stops_right_after_its_result(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        *,
        a_candidate_standing: bool = False,
    ) -> Any:
        """A landing decided before the stop, and the REAL press interrupted the
        moment it has written "published to the mission branch": no report, no
        release, the hold still on the record.

        Press lease recovery (coach round, item 2): a press stopped there
        with the candidate its check stood up still standing keeps its hold
        for a person, so these cases use a check that leaves nothing standing
        unless ``a_candidate_standing`` says otherwise."""
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(
            config_on,
            pool,
            repo_root,
            deploy=None if a_candidate_standing else _ACheckThatLeavesNothingStanding(),
        )
        assert MergeApprovalConsumer(deps)._decide_landing(
            B_BUILD, mission_id=MISSION_ID, decided_by="x", request_id=f"merge-{B_BUILD}"
        )
        _stop(pool)
        real_record = PublicationRecordStore.record

        def written_then_gone(self: Any, **kwargs: Any) -> bool:
            written = real_record(self, **kwargs)
            if kwargs.get("result") == RESULT_PUBLISHED_TO_MISSION_BRANCH:
                raise _TheCoordinatorStopped()
            return written

        with monkeypatch.context() as crash:
            crash.setattr(PublicationRecordStore, "record", written_then_gone)
            with pytest.raises(_TheCoordinatorStopped):
                await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)
        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert record.lease_holder  # held
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []  # no report
        return deps

    def _finisher(self, config_on: ForgeConfig, pool: SqliteLifecyclePersistence, consumer: Any) -> Any:  # noqa: F811
        deps = _resuming_deps(config_on, pool, consumer)
        deps.reconcile = consumer.reconcile_mission_landing
        deps.finish_settled = consumer.finish_a_settled_landing
        return deps

    @staticmethod
    def _expire(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
        pool.connection.execute(
            "UPDATE publication_records SET lease_expires_at = ? WHERE build_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), B_BUILD),
        )

    @staticmethod
    def _everything_is_clear(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
        from forge.lifecycle.feature_routing import router_restart_blockers
        from tests.forge.deploy.test_rollout_quiesce_back import q, r

        assert router_restart_blockers(pool.connection) == ()
        ledger = Path(pool.connection.execute("PRAGMA database_list").fetchone()[2])
        assert q.work_problems(r.ledger_state(ledger)) == []

    async def _passes(self, config_on, pool, consumer, times: int = 1) -> None:  # noqa: ANN001
        step = _step(pool)
        for _ in range(times):
            await step.run_once()
            await mission_finish.finish_ready_missions(
                self._finisher(config_on, pool, consumer), wait=True
            )
            await step.run_once()

    @pytest.mark.asyncio
    async def test_recovery_writes_the_report_then_puts_the_hold_down(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = await self._the_press_stops_right_after_its_result(
            config_on, pool, repo_root, monkeypatch
        )
        self._expire(pool)
        await self._passes(config_on, pool, MergeApprovalConsumer(deps))

        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "PASSED"
        decision = _stages(pool, MERGE_DECISION_TARGET_IDENTIFIER)[0]
        assert reports[0].details["merge_decision"]["execution_attempt_id"] == (
            decision.details["merge_decision"]["execution_attempt_id"]
        )
        assert _record(pool).lease_holder is None
        mission = MissionStore(pool.connection).get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert mission.closed_reason.endswith("; 1 landed on the branch, 0 did not")
        self._everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_with_the_candidate_still_standing_the_person_is_told_and_the_mission_drains(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Press lease recovery (Rich's coordinator, 18 October 2026): a
        landing that stopped with its candidate still standing is reported,
        the mission's thread is told once — the feature, the candidate, how to
        remove it — and the hold is put down after its usual expiry, so the
        mission drains."""
        deps = await self._the_press_stops_right_after_its_result(
            config_on, pool, repo_root, monkeypatch, a_candidate_standing=True
        )
        identity = ((_record(pool).checked or {}).get("identity") or {}).get(
            "handed_to_the_check"
        )
        self._expire(pool)
        finisher, thread = _finish_deps(config_on, pool, offer=_never_offered)
        consumer = MergeApprovalConsumer(deps)
        finisher.land = consumer.land_mission_feature
        finisher.reconcile = consumer.reconcile_mission_landing
        finisher.finish_settled = consumer.finish_a_settled_landing
        mission_finish._resuming.clear()
        step = _step(pool)
        for _ in range(2):
            await step.run_once()
            await mission_finish.finish_ready_missions(finisher, wait=True)
            await step.run_once()

        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "PASSED"
        said = [line for _mission, line in thread.lines if "teardown" in line]
        assert len(said) == 1  # once, however many passes
        assert said[0].startswith("FEAT-MX1 landed, but its check's candidate ")
        assert "remove it with the project's own teardown step" in said[0]
        if identity:
            assert identity in said[0]
        assert _record(pool).lease_holder is None
        assert MissionStore(pool.connection).get(MISSION_ID).status == "NOT_MERGED"
        self._everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_a_crash_between_the_report_and_the_release_is_picked_up_next(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = await self._the_press_stops_right_after_its_result(
            config_on, pool, repo_root, monkeypatch
        )
        self._expire(pool)
        consumer = MergeApprovalConsumer(deps)
        with monkeypatch.context() as crash:
            # The recovery's own coordinator stops after the report.
            crash.setattr(
                PublicationRecordStore,
                "release_lease",
                lambda self, **_k: (_ for _ in ()).throw(RuntimeError("stopped")),
            )
            await mission_finish.finish_ready_missions(
                self._finisher(config_on, pool, consumer), wait=True
            )
        assert len(_stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)) == 1
        assert _record(pool).lease_holder  # taken over, and still held
        self._expire(pool)
        await self._passes(config_on, pool, MergeApprovalConsumer(deps))

        assert len(_stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)) == 1  # not twice
        assert _record(pool).lease_holder is None
        assert MissionStore(pool.connection).get(MISSION_ID).status == "NOT_MERGED"
        self._everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_a_live_lease_at_that_point_is_left_alone_and_stays_blocked(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.lifecycle.feature_routing import router_restart_blockers

        deps = await self._the_press_stops_right_after_its_result(
            config_on, pool, repo_root, monkeypatch
        )
        held = _record(pool)
        assert held.lease_is_live(datetime.now(UTC))
        await self._passes(config_on, pool, MergeApprovalConsumer(deps), times=2)

        record = _record(pool)
        assert record.lease_holder == held.lease_holder
        assert record.lease_expires_at == held.lease_expires_at
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []
        assert MissionStore(pool.connection).get(MISSION_ID).status == "RUNNING"
        assert router_restart_blockers(pool.connection) != ()


class TestARefusedSettlementClaimsNothing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("on_the_remote", [True, False])
    async def test_a_refused_settle_reports_nothing_and_puts_its_lease_down(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        on_the_remote: bool,
    ) -> None:
        """Another worker took the record over as the remote was looked at:
        whichever way the remote answered, nothing is claimed — no result, no
        report — and the lease taken to look is put down again."""
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=on_the_remote)
        _decided_with_every_attempt_used(consumer, pool, used=3)
        monkeypatch.setattr(PublicationRecordStore, "settle", lambda self, **_k: False)

        assert await consumer.land_mission_feature(B_BUILD) is None

        record = _record(pool)
        assert record.result is None
        assert record.lease_holder is None
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []


# ---------------------------------------------------------------------------
# Codex implementation round 4, R2: no mission leaves RUNNING with a landing
# decided and its report unwritten
# ---------------------------------------------------------------------------


class TestNoMissionLeavesRunningWithAnUnreportedLanding:
    async def _the_press_stops_after_its_release(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        *,
        stopped: bool,
    ) -> Any:
        """A settled, released record and no report.

        Before press lease recovery (18 October 2026) the REAL press put its
        lease down and then reported, so a coordinator gone in between left
        exactly this. The press now reports before it puts its lease down, so
        it is made here as that older press left it: the real press stopped
        at its report, and its hold then put down on its own turn. Records
        written by the older press are still on ledgers, and the mission's
        drain must still wait for their report."""
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        assert MergeApprovalConsumer(deps)._decide_landing(
            B_BUILD, mission_id=MISSION_ID, decided_by="x", request_id=f"merge-{B_BUILD}"
        )
        if stopped:
            _stop(pool)

        async def gone_before_the_report(*_a: Any, **_k: Any) -> Any:
            raise _TheCoordinatorStopped()

        with monkeypatch.context() as crash:
            crash.setattr(merge_executor, "publish_the_press_report", gone_before_the_report)
            with pytest.raises(_TheCoordinatorStopped):
                await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)
        held = _record(pool)
        assert held.lease_holder  # the press now keeps its hold until after its report
        assert PublicationRecordStore(pool.connection).release_lease(
            build_id=B_BUILD, turn=held.turn, holder=held.lease_holder, now=datetime.now(UTC)
        )
        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert record.lease_holder is None  # released
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []  # not reported
        return deps

    @pytest.mark.asyncio
    async def test_a_stopped_mission_waits_for_the_report_then_ends(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bookkeeping = TestASettledLandingWhoseBookkeepingDidNotFinish()
        deps = await self._the_press_stops_after_its_release(
            config_on, pool, repo_root, monkeypatch, stopped=True
        )
        step = _step(pool)
        await step.run_once()  # the tick's mission step runs before the finisher
        store = MissionStore(pool.connection)
        assert store.attempt_by_correlation_id(B_CID).status == "LANDED"
        assert store.get(MISSION_ID).status == "RUNNING"  # not closed yet
        await bookkeeping._passes(config_on, pool, MergeApprovalConsumer(deps))

        reports = _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)
        assert len(reports) == 1 and reports[0].status == "PASSED"
        assert store.get(MISSION_ID).status == "NOT_MERGED"
        bookkeeping._everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_an_atomic_settlement_then_a_stop_before_the_report(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bookkeeping = TestASettledLandingWhoseBookkeepingDidNotFinish()
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)
        _a_send_whose_answer_was_lost(pool, repo_root, on_the_remote=True)
        _decided_with_every_attempt_used(consumer, pool, used=3)
        _stop(pool)

        def gone(self: Any, *_a: Any, **_k: Any) -> None:
            raise _TheCoordinatorStopped()

        with monkeypatch.context() as crash:
            crash.setattr(MergeApprovalConsumer, "_landing_reconciled", gone)
            with pytest.raises(_TheCoordinatorStopped):
                await consumer.land_mission_feature(B_BUILD)
        record = _record(pool)
        assert record.result == RESULT_PUBLISHED_TO_MISSION_BRANCH and record.lease_holder is None
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER) == []
        step = _step(pool)
        await step.run_once()
        assert MissionStore(pool.connection).get(MISSION_ID).status == "RUNNING"
        await bookkeeping._passes(config_on, pool, MergeApprovalConsumer(deps))

        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)[0].status == "PASSED"
        assert MissionStore(pool.connection).get(MISSION_ID).status == "NOT_MERGED"
        bookkeeping._everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_a_mission_that_was_not_stopped_is_not_ready_until_the_report_exists(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = await self._the_press_stops_after_its_release(
            config_on, pool, repo_root, monkeypatch, stopped=False
        )
        step = _step(pool)
        store = MissionStore(pool.connection)
        await step.run_once()
        assert store.attempt_by_correlation_id(B_CID).status == "LANDED"
        assert store.get(MISSION_ID).status == "RUNNING"  # not READY without its report
        bookkeeping = TestASettledLandingWhoseBookkeepingDidNotFinish()
        await mission_finish.finish_ready_missions(
            bookkeeping._finisher(config_on, pool, MergeApprovalConsumer(deps)), wait=True
        )
        assert _stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)[0].status == "PASSED"
        await step.run_once()
        assert store.get(MISSION_ID).status == "READY"


# ---------------------------------------------------------------------------
# Press lease recovery, coach round item 2: a landing runs the candidate
# check too, so its press can keep its hold ON PURPOSE (a teardown that did
# not confirm the candidate down); the landing recovery never puts that down
# ---------------------------------------------------------------------------


class _ATeardownThatLeavesItStanding(merge_executor_fakes._FakeDeploy):
    async def __call__(self, **kwargs: Any) -> Any:
        if kwargs.get("leg") == "candidate_down":
            self.calls.append(kwargs)
            return SimpleNamespace(
                confirmed=True, outcome="failed", verdict=None, detail={"candidate": "standing"}
            )
        return await super().__call__(**kwargs)


class TestALandingsHoldKeptOnPurpose:
    @pytest.mark.asyncio
    async def test_the_landing_recovery_says_so_then_puts_it_down(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        _planned(pool)
        the_mission_branch_on_the_remote(repo_root)
        deps, *_ = press_deps(config_on, pool, repo_root, deploy=_ATeardownThatLeavesItStanding())
        consumer = MergeApprovalConsumer(deps)
        assert consumer._decide_landing(
            B_BUILD, mission_id=MISSION_ID, decided_by="x", request_id=f"merge-{B_BUILD}"
        )
        await consumer.land_mission_feature(B_BUILD)
        held = _record(pool)
        assert held.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert held.lease_holder  # kept by the press: the candidate may be standing
        assert len(_stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)) == 1
        bookkeeping = TestASettledLandingWhoseBookkeepingDidNotFinish()
        assert await MergeApprovalConsumer(deps).finish_a_settled_landing(B_BUILD) == "held"
        bookkeeping._expire(pool)
        assert (
            await MergeApprovalConsumer(deps).finish_a_settled_landing(B_BUILD)
            == merge_executor.LANDING_LEFT_SOMETHING_STANDING
        )
        assert _record(pool).lease_holder is None
        noted = _stages(pool, merge_executor.MISSION_LANDING_LEFT_STANDING_TARGET_IDENTIFIER)
        assert len(noted) == 1
        assert "FEAT-MX1 landed" in noted[0].details["sentence"]
        assert len(_stages(pool, MERGE_REPORT_TARGET_IDENTIFIER)) == 1


async def _never_offered(**_kwargs: Any) -> bool:
    raise AssertionError("a stopped mission is not offered")
