# ruff: noqa: F811 - pytest fixtures are imported and then named as arguments
"""An ordinary build's press that stopped after its result (18 October 2026).

The press writes its result, writes its ending down, cleans up, reports and
puts its hold down LAST. A coordinator that stops anywhere after the result
used to leave a held record with no report, which the router and the
rollout's idle check waited on for ever, because one record result covers
several different reports. These tests stop the REAL press at each boundary,
run the REAL per-tick finishing pass, and check what a person would: the
report and the build's ending are the ones the press would have written (a
repair row for a red ending), nothing is sent a second time, nothing blocks
the router or the rollout, and a live hold is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.lifecycle import missions as missions_tables
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline import merge_executor, mission_finish
from forge.pipeline.merge_executor import (
    ENDING_NOT_RECORDED_SENTENCE,
    MERGE_DECISION_DETAILS_KEY,
    MERGE_DECISION_TARGET_IDENTIFIER,
    MERGE_EXECUTION_ATTEMPT_ID_KEY,
    MERGE_EXECUTION_ATTEMPT_VERSION,
    MERGE_EXECUTION_ATTEMPT_VERSION_KEY,
    MERGE_REPORT_TARGET_IDENTIFIER,
    RESULT_WORD_ENDING_NOT_RECORDED,
    MergeApprovalConsumer,
    execute_merge_deploy,
)
from forge.pipeline.publication_record import (
    RESULT_MERGED_AND_RUNNING,
    RESULT_PUBLISHED_DEPLOYMENT_PENDING,
    PublicationRecordStore,
)
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    BUILD_ID,
    CORRELATION,
    FEATURE_ID,
    MAIN_SHA,
    REPO,
    _ensure_build,
    _receipts_env,
    pool,
    repo_root,
)
from tests.forge.pipeline.test_the_merge_word_publishes import (  # noqa: F401
    _ADeployStepThatSays,
    _APublisherThatSays,
    _deps_that_can_deploy,
    _published,
    config_with_publication_on,
)

ATTEMPT = "a" * 32

#: The two endings: one green, one red with a repair row. Each is
#: (the deploy stand-in's arguments, the record's result, the report's word,
#: its status, the build's ending, files a repair).
ENDINGS = {
    "merged-and-running": (
        {},
        RESULT_MERGED_AND_RUNNING,
        "merged-into-the-remote-and-running",
        "PASSED",
        "COMPLETE",
        False,
    ),
    "merged-deploy-failed": (
        {"reports": "j-somebodyelse@0000"},
        RESULT_PUBLISHED_DEPLOYMENT_PENDING,
        "merged-deploy-failed",
        "FAILED",
        "FAILED",
        True,
    ),
}


class _TheCoordinatorStopped(BaseException):
    """The process going away: nothing in the press catches it."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _a_journey_waiting_on_its_press(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
    """A build whose row the press ends (a repair journey's is RUNNING), with
    the approved decision the consumer writes before it presses."""
    missions_tables.ensure_tables(pool.connection)
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    pool.connection.execute(
        "UPDATE builds SET status = 'RUNNING', completed_at = NULL WHERE build_id = ?",
        (BUILD_ID,),
    )
    pool.connection.commit()
    now = datetime.now(UTC)
    pool.record_stage(
        StageLogEntry(
            build_id=BUILD_ID,
            stage_label="merge-deploy",
            target_kind="local_tool",
            target_identifier=MERGE_DECISION_TARGET_IDENTIFIER,
            status="PASSED",
            started_at=now,
            completed_at=now,
            duration_secs=0.0,
            details={
                MERGE_DECISION_DETAILS_KEY: {
                    MERGE_EXECUTION_ATTEMPT_VERSION_KEY: MERGE_EXECUTION_ATTEMPT_VERSION,
                    MERGE_EXECUTION_ATTEMPT_ID_KEY: ATTEMPT,
                    "decided_by": "rich",
                }
            },
        )
    )


def _deps(config: ForgeConfig, pool: SqliteLifecyclePersistence, ending: str) -> Any:  # noqa: F811
    publisher = _APublisherThatSays([_published("c" * 40)])
    deploy = _ADeployStepThatSays(**ENDINGS[ending][0])
    deps = _deps_that_can_deploy(config, pool, publisher=publisher, deploy=deploy)
    return deps, publisher, deploy


async def _press(deps: Any, repo_root: Path) -> Any:  # noqa: F811
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=CORRELATION,
        decided_by="rich",
        merge_execution_attempt_id=ATTEMPT,
    )


def _record(pool: SqliteLifecyclePersistence) -> Any:  # noqa: F811
    return PublicationRecordStore(pool.connection).read(BUILD_ID)


def _reports(pool: SqliteLifecyclePersistence) -> list[Any]:  # noqa: F811
    return [
        s for s in pool.read_stages(BUILD_ID) if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
    ]


def _build(pool: SqliteLifecyclePersistence) -> tuple[str, str | None]:  # noqa: F811
    row = pool.connection.execute(
        "SELECT status, error FROM builds WHERE build_id = ?", (BUILD_ID,)
    ).fetchone()
    return str(row[0]), row[1]


def _expire(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
    pool.connection.execute(
        "UPDATE publication_records SET lease_expires_at = ? WHERE build_id = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), BUILD_ID),
    )
    pool.connection.commit()


def _finisher(config: ForgeConfig, pool: SqliteLifecyclePersistence, consumer: Any) -> Any:  # noqa: F811
    async def offer(**_kwargs: Any) -> bool:
        raise AssertionError("no mission is offered here")

    async def notify(*_args: Any) -> None:
        raise AssertionError("no mission is told anything here")

    return mission_finish.MissionFinishDeps(
        pool=pool,
        config=config,
        offer=offer,
        git_for=lambda *_a: None,
        notify=notify,
        finish_settled_press=consumer.finish_a_settled_press,
    )


async def _passes(config: ForgeConfig, pool: SqliteLifecyclePersistence, deps: Any, times: int = 1) -> None:  # noqa: F811
    for _ in range(times):
        await mission_finish.finish_ready_missions(
            _finisher(config, pool, MergeApprovalConsumer(deps)), wait=True
        )


def _blocked(pool: SqliteLifecyclePersistence) -> tuple[tuple[str, ...], list[Any]]:  # noqa: F811
    from forge.lifecycle.feature_routing import router_restart_blockers
    from tests.forge.deploy.test_rollout_quiesce_back import q, r

    ledger = Path(pool.connection.execute("PRAGMA database_list").fetchone()[2])
    return router_restart_blockers(pool.connection), q.work_problems(r.ledger_state(ledger))


def _everything_is_clear(pool: SqliteLifecyclePersistence, *, a_repair: bool = False) -> None:  # noqa: F811
    """Nothing blocks the router, and the rollout's idle check lists nothing
    but the repair row a red ending filed (new work, queued, not a press)."""
    blockers, problems = _blocked(pool)
    assert blockers == ()
    repair = [
        p
        for p in problems
        if p.get("table") == "work_queue"
        and p["row"].get("correlation_id") == f"fix-{BUILD_ID}"
        and p["row"].get("status") == "QUEUED"
    ]
    assert len(repair) == (1 if a_repair else 0)
    assert [p for p in problems if p not in repair] == []


@pytest.fixture
def repairs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every repair row the press's report path files, by ending."""
    filed: list[str] = []
    real = merge_executor._mint_repair_row

    def counted(pool: Any, build_id: str, outcome: Any, **kwargs: Any) -> None:
        filed.append(outcome.result)
        real(pool, build_id, outcome, **kwargs)

    monkeypatch.setattr(merge_executor, "_mint_repair_row", counted)
    return filed


async def _uninterrupted(config, pool, repo_root, ending):  # noqa: ANN001, F811
    """The press with nothing stopped: what the recovery must match."""
    _a_journey_waiting_on_its_press(pool)
    deps, publisher, deploy = _deps(config, pool, ending)
    outcome = await _press(deps, repo_root)
    return outcome, deps, publisher, deploy


# The three places a coordinator can stop after the result --------------------


def _stop_after_the_result(crash: pytest.MonkeyPatch, result: str) -> None:
    real = PublicationRecordStore.record

    def written_then_gone(self: Any, **kwargs: Any) -> bool:
        written = real(self, **kwargs)
        if kwargs.get("result") == result:
            raise _TheCoordinatorStopped()
        return written

    crash.setattr(PublicationRecordStore, "record", written_then_gone)


def _stop_after_the_ending_is_written_down(crash: pytest.MonkeyPatch, which: int) -> None:
    """``which``: 1 = the first writing-down (before the clean-up), 2 = the
    second (words final, just before the report)."""
    real = PublicationRecordStore.record_outcome
    calls = {"n": 0}

    def written_then_gone(self: Any, **kwargs: Any) -> bool:
        written = real(self, **kwargs)
        calls["n"] += 1
        if calls["n"] == which:
            raise _TheCoordinatorStopped()
        return written

    crash.setattr(PublicationRecordStore, "record_outcome", written_then_gone)


def _stop_after_the_report(crash: pytest.MonkeyPatch) -> None:
    real = merge_executor.publish_the_press_report

    async def reported_then_gone(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise _TheCoordinatorStopped()

    crash.setattr(merge_executor, "publish_the_press_report", reported_then_gone)


async def _the_press_stops(config, pool, repo_root, ending, stop):  # noqa: ANN001, F811
    _a_journey_waiting_on_its_press(pool)
    deps, publisher, deploy = _deps(config, pool, ending)
    with pytest.MonkeyPatch.context() as crash:
        stop(crash)
        with pytest.raises(_TheCoordinatorStopped):
            await _press(deps, repo_root)
    record = _record(pool)
    assert record.result == ENDINGS[ending][1]
    assert record.lease_holder  # the hold is still on the record
    assert record.lease_is_live(datetime.now(UTC))
    return deps, publisher, deploy


def _what_was_sent(publisher: Any, deploy: Any) -> tuple[int, list[Any]]:
    return len(publisher.asked), [c.get("leg") for c in deploy.calls]


# ---------------------------------------------------------------------------
# 1. After the result, before the ending is written down: the plain FAILED
# ---------------------------------------------------------------------------


class TestStoppedBeforeTheEndingWasWrittenDown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_a_plain_failed_ending_no_repair_and_the_hold_put_down(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        ending: str,
    ) -> None:
        deps, publisher, deploy = await self._stopped(
            config_with_publication_on, pool, repo_root, ending
        )
        sent = _what_was_sent(publisher, deploy)
        assert _reports(pool) == []
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)

        reports = _reports(pool)
        assert len(reports) == 1
        said = reports[0].details
        assert reports[0].status == "FAILED"
        assert said["result"] == RESULT_WORD_ENDING_NOT_RECORDED
        assert said["detail"] == ENDING_NOT_RECORDED_SENTENCE
        assert said[MERGE_DECISION_DETAILS_KEY][MERGE_EXECUTION_ATTEMPT_ID_KEY] == ATTEMPT
        status, error = _build(pool)
        assert status == "FAILED"
        assert ENDING_NOT_RECORDED_SENTENCE in str(error)
        assert repairs == []  # not a finding about the code
        assert _what_was_sent(publisher, deploy) == sent  # nothing sent again
        assert _record(pool).lease_holder is None
        assert _record(pool).result == ENDINGS[ending][1]  # the record is not rewritten
        _everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_an_earlier_presss_ending_is_not_this_ones(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
    ) -> None:
        """An ending written down on an earlier turn is never reported for a
        press that stopped before writing its own."""
        import json

        deps, *_ = await self._stopped(
            config_with_publication_on, pool, repo_root, "merged-deploy-failed"
        )
        record = _record(pool)
        merge_executor_outcome = {
            "outcome": {"result": "merged-into-the-remote-and-running", "status": "PASSED", "detail": "an earlier press"},
            "feature_id": FEATURE_ID,
            "correlation_id": CORRELATION,
            "started_at": datetime.now(UTC).isoformat(),
            "receipts_dir": "/nonexistent",
            "digest_warning": None,
            "join_as": FEATURE_ID,
            "reports_before": 0,
        }
        from forge.pipeline.publication_record import ensure_outcome_table

        ensure_outcome_table(pool.connection)
        pool.connection.execute(
            "INSERT OR REPLACE INTO publication_outcomes VALUES (?, ?, ?, ?, ?)",
            (BUILD_ID, record.turn - 1, ATTEMPT, json.dumps(merge_executor_outcome), "x"),
        )
        pool.connection.commit()
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        reports = _reports(pool)
        assert len(reports) == 1
        assert reports[0].details["result"] == RESULT_WORD_ENDING_NOT_RECORDED
        assert _build(pool)[0] == "FAILED"
        assert repairs == []
        _everything_is_clear(pool)

    async def _stopped(self, config, pool, repo_root, ending):  # noqa: ANN001, F811
        return await _the_press_stops(
            config,
            pool,
            repo_root,
            ending,
            lambda crash: _stop_after_the_result(crash, ENDINGS[ending][1]),
        )


# ---------------------------------------------------------------------------
# 2. After the ending is written down, before the report: reported word for word
# ---------------------------------------------------------------------------


class TestStoppedAfterTheEndingWasWrittenDown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("which", [1, 2], ids=["before-the-clean-up", "words-final"])
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_the_report_the_press_would_have_written(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
        repairs: list[str],
        ending: str,
        which: int,
    ) -> None:
        _, word, status, build_ending, repairs_it = ENDINGS[ending][1:]
        deps, publisher, deploy = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            ending,
            lambda crash: _stop_after_the_ending_is_written_down(crash, which),
        )
        sent = _what_was_sent(publisher, deploy)
        assert _reports(pool) == []
        bus_before = len(deps.pipeline_publisher.reports)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)

        reports = _reports(pool)
        assert len(reports) == 1
        assert reports[0].status == status
        said = reports[0].details
        assert said["result"] == word
        assert said[MERGE_DECISION_DETAILS_KEY][MERGE_EXECUTION_ATTEMPT_ID_KEY] == ATTEMPT
        assert said["branch"] == f"autobuild/{FEATURE_ID}"
        # The bus message and the receipt go through the same path.
        assert len(deps.pipeline_publisher.reports) == bus_before + 1
        receipt = tmp_path / "receipts" / f"merge-{BUILD_ID}" / "merge_deploy_report.json"
        assert receipt.is_file()
        assert _build(pool)[0] == build_ending
        assert repairs == ([word] if repairs_it else [])
        assert _what_was_sent(publisher, deploy) == sent  # nothing sent again
        assert _record(pool).lease_holder is None
        _everything_is_clear(pool, a_repair=repairs_it)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_word_for_word_what_the_uninterrupted_press_reports(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
        ending: str,
    ) -> None:
        """The report made from the ending written down is the press's own
        report: every field and every word, only its timings differ. The
        uninterrupted press reports; the recovery's path is then run from what
        that press wrote down, on a ledger of its own, and the two compared."""
        import dataclasses
        import sqlite3

        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.lifecycle import migrations
        from tests.forge.pipeline.test_merge_executor import _FakePublisher

        outcome, deps, *_ = await _uninterrupted(
            config_with_publication_on, pool, repo_root, ending
        )
        pressed = deps.pipeline_publisher.reports[-1].model_dump(mode="json")
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written is not None

        other_root = tmp_path / "recovered"
        other_root.mkdir()
        cx: sqlite3.Connection = sqlite_connect.connect_writer(other_root / "forge.db")
        migrations.apply_at_boot(cx)
        other = SqliteLifecyclePersistence(connection=cx)
        _ensure_build(other, build_id=BUILD_ID, feature_id=FEATURE_ID)
        bus = _FakePublisher()
        said = written.outcome
        await merge_executor.publish_the_press_report(
            dataclasses.replace(deps, pool=other, pipeline_publisher=bus),
            build_id=BUILD_ID,
            feature_id=said["feature_id"],
            correlation_id=said["correlation_id"],
            outcome=merge_executor.outcome_from_stored(said),
            started=datetime.fromisoformat(said["started_at"]),
            receipts_dir=other_root / "receipts",
            dry_run=False,
            merge_execution_attempt_id=written.attempt_id,
            digest_warning=said["digest_warning"],
        )
        recovered = bus.reports[-1].model_dump(mode="json")
        timings = {"completed_at", "duration_secs"}
        assert {k: v for k, v in recovered.items() if k not in timings} == {
            k: v for k, v in pressed.items() if k not in timings
        }
        assert recovered["detail"] == outcome.detail


# ---------------------------------------------------------------------------
# 3. After the report, before the hold is put down: the hold, put down
# ---------------------------------------------------------------------------


class TestStoppedAfterTheReport:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_not_reported_twice_and_the_hold_put_down(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        ending: str,
    ) -> None:
        _, word, status, build_ending, repairs_it = ENDINGS[ending][1:]
        deps, publisher, deploy = await _the_press_stops(
            config_with_publication_on, pool, repo_root, ending, _stop_after_the_report
        )
        assert len(_reports(pool)) == 1  # the press reported
        sent = _what_was_sent(publisher, deploy)
        bus_before = len(deps.pipeline_publisher.reports)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)

        reports = _reports(pool)
        assert len(reports) == 1 and reports[0].status == status  # not twice
        assert reports[0].details["result"] == word
        assert len(deps.pipeline_publisher.reports) == bus_before
        assert _build(pool)[0] == build_ending
        assert repairs == ([word] if repairs_it else [])  # filed once, by the press
        assert _what_was_sent(publisher, deploy) == sent
        assert _record(pool).lease_holder is None
        _everything_is_clear(pool, a_repair=repairs_it)


# ---------------------------------------------------------------------------
# A live hold, and the uninterrupted press
# ---------------------------------------------------------------------------


class TestALiveHoldIsNeverTouched:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stop",
        [
            pytest.param(lambda c: _stop_after_the_result(c, RESULT_MERGED_AND_RUNNING), id="after-the-result"),
            pytest.param(lambda c: _stop_after_the_ending_is_written_down(c, 2), id="after-the-ending"),
            pytest.param(_stop_after_the_report, id="after-the-report"),
        ],
    )
    async def test_nothing_is_done_and_the_router_stays_blocked(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        stop: Any,
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on, pool, repo_root, "merged-and-running", stop
        )
        held = _record(pool)
        reports_before = len(_reports(pool))
        await _passes(config_with_publication_on, pool, deps, times=2)

        record = _record(pool)
        assert (record.lease_holder, record.lease_expires_at, record.turn) == (
            held.lease_holder,
            held.lease_expires_at,
            held.turn,
        )
        assert len(_reports(pool)) == reports_before
        assert f"publication-held:{BUILD_ID}:{held.lease_holder}" in _blocked(pool)[0]


class TestTheUninterruptedPress:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_reports_then_puts_its_hold_down_and_the_pass_does_nothing(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        ending: str,
    ) -> None:
        _, word, status, build_ending, repairs_it = ENDINGS[ending][1:]
        outcome, deps, *_ = await _uninterrupted(
            config_with_publication_on, pool, repo_root, ending
        )
        assert outcome.result == word
        assert _record(pool).lease_holder is None
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written is not None and written.turn == _record(pool).turn
        assert written.outcome["outcome"]["result"] == word
        assert written.attempt_id == ATTEMPT
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert len(_reports(pool)) == 1
        assert _build(pool)[0] == build_ending
        assert repairs == ([word] if repairs_it else [])
        _everything_is_clear(pool, a_repair=repairs_it)


class TestTheReportOrder:
    @pytest.mark.asyncio
    async def test_the_report_is_written_while_the_hold_is_still_on(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        holders_at_report: list[str | None] = []
        real = merge_executor.publish_the_press_report

        async def watched(*args: Any, **kwargs: Any) -> Any:
            holders_at_report.append(_record(pool).lease_holder)
            return await real(*args, **kwargs)

        monkeypatch.setattr(merge_executor, "publish_the_press_report", watched)
        await _uninterrupted(config_with_publication_on, pool, repo_root, "merged-and-running")
        assert len(holders_at_report) == 1 and holders_at_report[0]
        assert _record(pool).lease_holder is None


class TestAMissionBuildIsNotThisPathsToFinish:
    @pytest.mark.asyncio
    async def test_a_mission_feature_record_is_left_to_its_own_recovery(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda c: _stop_after_the_result(c, RESULT_MERGED_AND_RUNNING),
        )
        pool.connection.execute("PRAGMA foreign_keys = OFF")
        pool.connection.execute(
            "UPDATE builds SET mission_id = 'MIS-1', mission_role = 'feature' WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()
        _expire(pool)
        held = _record(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _reports(pool) == []
        assert _record(pool).lease_holder == held.lease_holder
        assert await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID) == "nothing to do"


class TestATakenOverPressDoesNotReportTwice:
    @pytest.mark.asyncio
    async def test_a_press_whose_record_was_taken_over_after_its_result_leaves_the_report(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A press still alive after its hold ran out, whose record the
        recovery took over and reported: the press does not report again."""
        _a_journey_waiting_on_its_press(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        real = PublicationRecordStore.record_outcome
        calls = {"n": 0}

        def written_then_taken_over(self: Any, **kwargs: Any) -> bool:
            written = real(self, **kwargs)
            calls["n"] += 1
            if calls["n"] == 2:
                _expire(pool)
                grant = PublicationRecordStore(pool.connection).take_lease(
                    build_id=BUILD_ID, holder="settled-press:elsewhere", now=datetime.now(UTC)
                )
                assert grant is not None
            return written

        monkeypatch.setattr(PublicationRecordStore, "record_outcome", written_then_taken_over)
        await _press(deps, repo_root)
        assert _reports(pool) == []
        assert _record(pool).lease_holder == "settled-press:elsewhere"


class TestEachGuardOnItsOwn:
    """The selector and the recovery each refuse on their own, so neither
    depends on the other to leave a live hold or a mission's record alone."""

    @pytest.mark.asyncio
    async def test_the_selector_offers_no_live_hold(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on, pool, repo_root, "merged-and-running", _stop_after_the_report
        )
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []
        _expire(pool)
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == [BUILD_ID]

    @pytest.mark.asyncio
    async def test_the_recovery_refuses_a_live_hold_when_asked_directly(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on, pool, repo_root, "merged-and-running", _stop_after_the_report
        )
        held = _record(pool)
        assert await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID) == "held"
        assert (_record(pool).lease_holder, _record(pool).turn) == (held.lease_holder, held.turn)

    @pytest.mark.asyncio
    async def test_the_selector_offers_no_mission_build(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on, pool, repo_root, "merged-and-running", _stop_after_the_report
        )
        _expire(pool)
        pool.connection.execute("PRAGMA foreign_keys = OFF")
        pool.connection.execute(
            "UPDATE builds SET mission_id = 'MIS-1', mission_role = 'feature' WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []

    @pytest.mark.asyncio
    async def test_an_ending_is_written_down_only_by_the_holder_on_its_turn(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda c: _stop_after_the_result(c, RESULT_MERGED_AND_RUNNING),
        )
        record = _record(pool)
        store = PublicationRecordStore(pool.connection)
        now = datetime.now(UTC)
        for turn, holder in ((record.turn, "somebody-else"), (record.turn + 1, record.lease_holder)):
            assert not store.record_outcome(
                build_id=BUILD_ID, turn=turn, holder=holder, now=now, outcome={"x": 1}, attempt_id=None
            )
        assert store.read_outcome(BUILD_ID) is None
        assert store.record_outcome(
            build_id=BUILD_ID,
            turn=record.turn,
            holder=record.lease_holder,
            now=now,
            outcome={"x": 1},
            attempt_id=None,
        )
        assert store.read_outcome(BUILD_ID).outcome == {"x": 1}
