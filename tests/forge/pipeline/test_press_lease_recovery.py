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


#: The finishing routine's steps, in order (``merge_executor.finish_the_press``).
FINISH_STEPS = (
    "_finish_the_report_row",
    "_finish_the_bus_message",
    "_finish_the_build_row",
    "_finish_the_repair_row",
    "_finish_the_warning",
    "_finish_the_release",
)


def _stop_after_step(crash: pytest.MonkeyPatch, name: str) -> None:
    """The coordinator stops right after one finishing step has done its work."""
    import inspect

    real = getattr(merge_executor, name)

    if inspect.iscoroutinefunction(real):

        async def done_then_gone(*args: Any, **kwargs: Any) -> Any:
            await real(*args, **kwargs)
            raise _TheCoordinatorStopped()

    else:

        def done_then_gone(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            real(*args, **kwargs)
            raise _TheCoordinatorStopped()

    crash.setattr(merge_executor, name, done_then_gone)


def _stop_after_the_report(crash: pytest.MonkeyPatch) -> None:
    _stop_after_step(crash, "_finish_the_report_row")


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
    async def test_a_result_an_earlier_press_left_is_not_this_ones(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
    ) -> None:
        """A press that stopped BEFORE writing its own result, on a record
        whose result an earlier press left: not this path's — nothing is
        reported, nothing released, and the router stays blocked."""
        deps, *_ = await self._stopped(
            config_with_publication_on, pool, repo_root, "merged-deploy-failed"
        )
        # This press's own row, as it is before its result: begun, not marked.
        pool.connection.execute(
            "UPDATE publication_outcomes SET result_written = 0 WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()
        _expire(pool)
        held = _record(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        assert _reports(pool) == []
        assert (_record(pool).lease_holder, _record(pool).turn) == (held.lease_holder, held.turn)
        assert f"publication-held:{BUILD_ID}:{held.lease_holder}" in _blocked(pool)[0]

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
        ending: str,
    ) -> None:
        """The report made from the ending the press wrote down is the press's
        own report: every field and every word, only its timings differ."""
        outcome, deps, *_ = await _uninterrupted(
            config_with_publication_on, pool, repo_root, ending
        )
        pressed = deps.pipeline_publisher.reports[-1].model_dump(mode="json")
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        recovered_ending = merge_executor.the_stopped_presss_ending(
            deps, pool.get_build_row(BUILD_ID), _record(pool), written, kept=None
        )
        recovered = merge_executor._the_report_payload(
            recovered_ending, completed=datetime.now(UTC), dry_run=False, press_turn=written.turn
        ).model_dump(mode="json")
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
        assert len(_reports(pool)) == 1  # the press wrote its report row
        assert deps.pipeline_publisher.reports == []  # and stopped before the bus
        sent = _what_was_sent(publisher, deploy)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)

        reports = _reports(pool)
        assert len(reports) == 1 and reports[0].status == status  # not twice
        assert reports[0].details["result"] == word
        assert len(deps.pipeline_publisher.reports) == 1  # sent once, by the recovery
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
        assert written.result_written and written.finished_at is not None
        assert written.bus_sent_at is not None
        assert (written.repair_at is not None) == repairs_it
        assert written.outcome["cleanup_done"] is True and written.outcome["kept_because"] is None
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
        real = merge_executor._finish_the_report_row

        def watched(*args: Any, **kwargs: Any) -> Any:
            holders_at_report.append(_record(pool).lease_holder)
            return real(*args, **kwargs)

        monkeypatch.setattr(merge_executor, "_finish_the_report_row", watched)
        await _uninterrupted(config_with_publication_on, pool, repo_root, "merged-and-running")
        assert len(holders_at_report) == 1 and holders_at_report[0]
        assert _record(pool).lease_holder is None


class TestAMissionLandingIsFinishedByTheSameRoutine:
    @pytest.mark.asyncio
    async def test_the_landing_path_leaves_a_presss_result_to_it(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """A result a press wrote is never the mission landing path's: that
        path keeps only the results a reconciliation settled."""
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
        assert await MergeApprovalConsumer(deps).finish_a_settled_landing(BUILD_ID) == "nothing to do"
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == [BUILD_ID]


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
    """The selector and the recovery each refuse a live hold on their own."""

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
        assert store.read_outcome(BUILD_ID).outcome == {}  # begun, nothing written down
        assert store.read_outcome(BUILD_ID).result_written  # its result is marked
        assert store.record_outcome(
            build_id=BUILD_ID,
            turn=record.turn,
            holder=record.lease_holder,
            now=now,
            outcome={"x": 1},
            attempt_id=None,
        )
        assert store.read_outcome(BUILD_ID).outcome == {"x": 1}


# ---------------------------------------------------------------------------
# Coach round (18 October 2026): the holes it found, each pinned
# ---------------------------------------------------------------------------


def _standing_deploy(teardown: str) -> Any:
    """A promote that leaves the candidate standing, and a teardown that
    answers ``still-standing``, loses its answer (``lost``), or is where the
    coordinator stops (``crash``)."""
    from types import SimpleNamespace

    class _APromoteThatLeavesItStanding(_ADeployStepThatSays):
        async def __call__(self, **kwargs: Any) -> Any:
            leg = kwargs.get("leg")
            if leg == "candidate_down":
                self.calls.append(kwargs)
                if teardown == "lost":
                    raise ConnectionResetError("the teardown's answer was lost")
                if teardown == "still-standing":
                    return SimpleNamespace(
                        confirmed=True, outcome="failed", verdict=None,
                        detail={"candidate": "standing"},
                    )
                raise _TheCoordinatorStopped()
            answer = await super().__call__(**kwargs)
            if leg == "promote":
                answer.detail = {**answer.detail, "candidate": "standing"}
            return answer

    return _APromoteThatLeavesItStanding()


def _a_cli_journey(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
    """A build pressed straight from the command line: no decision row and
    no attempt id."""
    missions_tables.ensure_tables(pool.connection)
    _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
    pool.connection.execute(
        "UPDATE builds SET status = 'RUNNING', completed_at = NULL WHERE build_id = ?",
        (BUILD_ID,),
    )
    pool.connection.commit()


async def _cli_press(deps: Any, repo_root: Path) -> Any:  # noqa: F811
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=CORRELATION,
        decided_by="rich",
    )


class TestAnotherReportOnTheBuildIsNotThisOne:
    """Item 1: a second merge word turned away by the live hold writes a report
    of its own; that is never taken as the stopped press's."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_the_stopped_presss_real_ending_is_still_reported(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        ending: str,
    ) -> None:
        _, word, status, build_ending, repairs_it = ENDINGS[ending][1:]
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            ending,
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        bus_before = len(deps.pipeline_publisher.reports)
        second = await _cli_press(deps, repo_root)
        # TURNED AWAY: a no-op for the build — no report, no bus message, the
        # build row still RUNNING, the record still the stopped press's.
        assert second.turned_away and second.result == "merge-refused"
        assert "another worker" in second.detail
        assert _reports(pool) == []
        assert len(deps.pipeline_publisher.reports) == bus_before
        assert _build(pool)[0] == "RUNNING"
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)

        results = [r.details["result"] for r in _reports(pool)]
        assert results == [word]
        assert _build(pool)[0] == build_ending  # COMPLETE when the merge landed
        assert repairs == ([word] if repairs_it else [])
        assert _record(pool).lease_holder is None
        _everything_is_clear(pool, a_repair=repairs_it)


class TestAHoldKeptOnPurposeStaysKept:
    """Item 2: the press keeps its hold when a candidate may be standing or a
    leg's answer was lost; the recovery reports but never puts it down."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("teardown", ["still-standing", "lost"])
    async def test_after_a_normal_ending(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        teardown: str,
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        deploy = _standing_deploy(teardown)
        deps = _deps_that_can_deploy(
            config_with_publication_on,
            pool,
            publisher=_APublisherThatSays([_published("c" * 40)]),
            deploy=deploy,
        )
        outcome = await _press(deps, repo_root)
        assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
        held = _record(pool)
        assert held.lease_holder  # kept by the press, on purpose
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.outcome["kept_because"]
        legs = len(deploy.calls)
        assert written.finished_at  # finished, with the hold kept for a person
        _expire(pool)
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []
        assert await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID) == "nothing to do"
        await _passes(config_with_publication_on, pool, deps, times=2)
        after = _record(pool)
        assert (after.lease_holder, after.turn) == (held.lease_holder, held.turn)
        assert len(_reports(pool)) == 1
        assert len(deploy.calls) == legs  # nothing sent to the target again
        assert f"publication-held:{BUILD_ID}:{held.lease_holder}" in _blocked(pool)[0]

    @pytest.mark.asyncio
    async def test_a_stop_during_the_teardown_is_reported_without_claiming_it(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        deps = _deps_that_can_deploy(
            config_with_publication_on,
            pool,
            publisher=_APublisherThatSays([_published("c" * 40)]),
            deploy=_standing_deploy("crash"),
        )
        with pytest.raises(_TheCoordinatorStopped):
            await _press(deps, repo_root)
        assert _reports(pool) == []
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        reports = _reports(pool)
        assert len(reports) == 1
        detail = reports[0].details["detail"]
        assert "may still be standing" in detail
        assert "torn down" not in detail.lower()
        assert _record(pool).lease_holder  # kept: a person looks
        assert any(b.startswith(f"publication-held:{BUILD_ID}:") for b in _blocked(pool)[0])

    @pytest.mark.asyncio
    async def test_a_stop_right_after_a_result_with_a_candidate_standing(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The fallback, when the result was written with a candidate still
        standing (the promote left it up): reported FAILED, hold kept."""
        _a_journey_waiting_on_its_press(pool)
        deps = _deps_that_can_deploy(
            config_with_publication_on,
            pool,
            publisher=_APublisherThatSays([_published("c" * 40)]),
            deploy=_standing_deploy("still-standing"),
        )
        with pytest.MonkeyPatch.context() as crash:
            _stop_after_the_result(crash, RESULT_MERGED_AND_RUNNING)
            with pytest.raises(_TheCoordinatorStopped):
                await _press(deps, repo_root)
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.result_written and written.standing["candidate_may_stand"] is True
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        reports = _reports(pool)
        assert [r.details["result"] for r in reports] == [RESULT_WORD_ENDING_NOT_RECORDED]
        assert "may still be standing" in reports[0].details["detail"]
        assert _record(pool).lease_holder


class TestTheProductionWiring:
    """Item 3: the composed finisher and serve's composition bind the
    recovery to the merge consumer's own method."""

    def test_the_composed_finisher_carries_the_consumers_method(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        from types import SimpleNamespace

        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        consumer = MergeApprovalConsumer(deps)
        finisher = mission_finish.compose_mission_finisher(
            forge_config=config_with_publication_on,
            pool=pool,
            offer_service=SimpleNamespace(offer=None),
            nats_client=None,
            finish_settled=consumer.finish_a_settled_landing,
            finish_settled_press=consumer.finish_a_settled_press,
        )
        assert finisher.deps.finish_settled_press == consumer.finish_a_settled_press
        assert finisher.deps.finish_settled == consumer.finish_a_settled_landing
        assert callable(finisher.tell)

    def test_serve_passes_the_consumers_method(self) -> None:
        import ast

        import forge.cli.serve as serve

        tree = ast.parse(Path(serve.__file__).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "compose_mission_finisher"
        ]
        assert len(calls) == 1
        given = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
        assert given["finish_settled_press"] == "_merge_consumer.finish_a_settled_press"
        assert given["finish_settled"] == "_merge_consumer.finish_a_settled_landing"
        # The finishing routine says a landing's warning through the finisher's line.
        installs = [
            ast.unparse(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "install_mission_teller"
        ]
        assert installs == ["_finisher.tell"]


class TestTwoProcesses:
    """Item 4: the press renews its hold before reporting, so a recovery in
    another process cannot take the record over while it reports."""

    @pytest.mark.asyncio
    async def test_a_takeover_during_the_report_is_refused(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        real_outcome = PublicationRecordStore.record_outcome
        calls = {"n": 0}

        def written_then_lapsed(self: Any, **kwargs: Any) -> bool:
            written = real_outcome(self, **kwargs)
            calls["n"] += 1
            if calls["n"] == 2:
                _expire(pool)  # the hold ran out just before the report
            return written

        grants: list[Any] = []
        real_report = merge_executor._finish_the_report_row

        def another_process_tries(*args: Any, **kwargs: Any) -> Any:
            grants.append(
                PublicationRecordStore(pool.connection).take_lease(
                    build_id=BUILD_ID, holder="settled-press:elsewhere", now=datetime.now(UTC)
                )
            )
            return real_report(*args, **kwargs)

        monkeypatch.setattr(PublicationRecordStore, "record_outcome", written_then_lapsed)
        monkeypatch.setattr(merge_executor, "_finish_the_report_row", another_process_tries)
        await _press(deps, repo_root)
        assert grants == [None]  # the renewed hold refused it
        assert len(_reports(pool)) == 1
        assert _record(pool).lease_holder is None


class TestTheRecoverysOwnOrder:
    @pytest.mark.asyncio
    async def test_the_recovery_reports_while_it_holds_the_record(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        _expire(pool)
        holders: list[str | None] = []
        real = merge_executor._finish_the_report_row

        def watched(*args: Any, **kwargs: Any) -> Any:
            holders.append(_record(pool).lease_holder)
            return real(*args, **kwargs)

        monkeypatch.setattr(merge_executor, "_finish_the_report_row", watched)
        await _passes(config_with_publication_on, pool, deps)
        assert len(holders) == 1 and str(holders[0]).startswith("settled-press:")
        assert _record(pool).lease_holder is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_the_join_folders_go_only_at_the_records_end(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        ending: str,
    ) -> None:
        from forge.pipeline.merge_join import working_folder_path

        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            ending,
            lambda crash: _stop_after_the_result(crash, ENDINGS[ending][1]),
        )
        folder = Path(working_folder_path(repo_root, FEATURE_ID, 1))
        assert folder.is_dir()
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _record(pool).lease_holder is None
        assert folder.is_dir() == (ending != "merged-and-running")


class TestAPressWithNoAttemptId:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_at", ["result", "ending"])
    async def test_a_command_line_press_is_recovered_with_no_decision_block(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        stop_at: str,
    ) -> None:
        _a_cli_journey(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        with pytest.MonkeyPatch.context() as crash:
            if stop_at == "result":
                _stop_after_the_result(crash, RESULT_MERGED_AND_RUNNING)
            else:
                _stop_after_the_ending_is_written_down(crash, 2)
            with pytest.raises(_TheCoordinatorStopped):
                await _cli_press(deps, repo_root)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        reports = _reports(pool)
        assert len(reports) == 1
        assert MERGE_DECISION_DETAILS_KEY not in reports[0].details
        assert reports[0].details["result"] == (
            RESULT_WORD_ENDING_NOT_RECORDED if stop_at == "result" else "merged-into-the-remote-and-running"
        )
        assert reports[0].details["branch"] == f"autobuild/{FEATURE_ID}"
        _everything_is_clear(pool)


class TestALedgerWithoutMissions:
    @pytest.mark.asyncio
    async def test_a_press_is_recovered_on_a_ledger_with_no_missions_tables(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        assert not missions_tables.builds_record_missions(pool.connection)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        with pytest.MonkeyPatch.context() as crash:
            _stop_after_the_ending_is_written_down(crash, 2)
            with pytest.raises(_TheCoordinatorStopped):
                await _cli_press(deps, repo_root)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert [r.details["result"] for r in _reports(pool)] == ["merged-into-the-remote-and-running"]
        assert _record(pool).lease_holder is None


class TestTheFallbacksBranch:
    @pytest.mark.asyncio
    async def test_the_fallback_names_the_branch_the_press_merged(
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
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _reports(pool)[0].details["branch"] == f"autobuild/{FEATURE_ID}"


class TestARefusalBeforeTheJoin:
    """Item 6: the press's own refusal writes its result and puts its lease down
    in one write; a stop before its report is still finished."""

    @staticmethod
    def _no_target_branch(pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
        pool.connection.execute(
            "UPDATE builds SET target_branch = NULL WHERE build_id = ?", (BUILD_ID,)
        )
        pool.connection.commit()

    @pytest.mark.asyncio
    async def test_a_stop_before_its_report_is_reported_word_for_word(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        self._no_target_branch(pool)
        deps, publisher, deploy = _deps(config_with_publication_on, pool, "merged-and-running")
        with pytest.MonkeyPatch.context() as crash:
            def gone(*_a: Any, **_k: Any) -> Any:
                raise _TheCoordinatorStopped()

            crash.setattr(merge_executor, "_finish_the_report_row", gone)
            with pytest.raises(_TheCoordinatorStopped):
                await _press(deps, repo_root)
        record = _record(pool)
        # THE REFUSAL KEEPS ITS LEASE through its finishing (Codex round 1, R3).
        assert record.result and record.lease_holder and _reports(pool) == []
        assert f"merge-approved-pending:{BUILD_ID}" in _blocked(pool)[0]
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        reports = _reports(pool)
        assert [r.details["result"] for r in reports] == ["merge-refused"]
        assert "no target branch on its record" in reports[0].details["detail"]
        assert reports[0].details[MERGE_DECISION_DETAILS_KEY][MERGE_EXECUTION_ATTEMPT_ID_KEY] == ATTEMPT
        assert _build(pool)[0] == "FAILED"
        assert repairs == []
        assert publisher.asked == [] and deploy.calls == []
        assert _record(pool).lease_holder is None
        _everything_is_clear(pool)

    @pytest.mark.asyncio
    async def test_a_reported_refusal_is_never_picked(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        self._no_target_branch(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        await _press(deps, repo_root)
        assert len(_reports(pool)) == 1
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        assert _record(pool).lease_holder is None
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []


class TestRecordsThatAreNeverPicked:
    """Item 6's widened selector sweeps nothing old: a record with a report, a
    record no press of today began, and an approval refused before any press."""

    @pytest.mark.asyncio
    async def test_records_with_a_report_or_without_a_presss_row(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.pipeline.publication_record import ensure_outcome_table

        _a_journey_waiting_on_its_press(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        await _press(deps, repo_root)  # reported and released
        ensure_outcome_table(pool.connection)
        # Older records, as a ledger from before this has them: a result, no
        # holder, with and without a report, and no press row at all.
        for build_id, reported in (("build-OLD-REPORTED", True), ("build-OLD-UNREPORTED", False)):
            _ensure_build(pool, build_id=build_id, feature_id=build_id)
            pool.connection.execute(
                "INSERT INTO publication_records (build_id, feature_id, repo, decided_by, "
                "decided_at, target_branch, attempt, result, turn, lines_json, created_at, "
                "updated_at) VALUES (?, ?, ?, 'rich', 'x', 'main', 1, 'publication pending', "
                "1, '[]', 'x', 'x')",
                (build_id, build_id, REPO),
            )
            if reported:
                now = datetime.now(UTC)
                pool.record_stage(
                    StageLogEntry(
                        build_id=build_id,
                        stage_label="merge-deploy",
                        target_kind="local_tool",
                        target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                        status="FAILED",
                        started_at=now,
                        completed_at=now,
                        duration_secs=0.0,
                        details={"result": "merge-refused"},
                    )
                )
        pool.connection.commit()
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []

    def test_an_approval_refused_before_any_press_is_never_picked(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        """The consumer writes a refused approval down (result, released, no
        report, by design): no press row, so it is not this path's."""
        _a_journey_waiting_on_its_press(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        turn = merge_executor._write_the_refusal_down_for(
            PublicationRecordStore(pool.connection),
            deps=deps,
            build_id=BUILD_ID,
            holder="merge-press:consumer",
            feature_id=FEATURE_ID,
            repo=REPO,
            decided_by="rich",
            target_branch=None,
            refusal="the approval was refused: not the approver",
        )
        assert turn
        assert _record(pool).result and _reports(pool) == []
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []


class TestASecondPressOnTheSameRecord:
    """A record pressed again: the new press begins its own row with its lease,
    so neither the earlier press's report mark nor its result is taken as the
    new press's."""

    async def _a_first_press_that_was_refused(self, config, pool, repo_root):  # noqa: ANN001, F811
        _a_journey_waiting_on_its_press(pool)
        TestARefusalBeforeTheJoin._no_target_branch(pool)
        deps, *_ = _deps(config, pool, "merged-and-running")
        first = await _press(deps, repo_root)
        assert first.result == "merge-refused"
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        pool.connection.execute(
            "UPDATE builds SET target_branch = 'main', status = 'RUNNING' WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()

    @pytest.mark.asyncio
    async def test_the_second_presss_ending_is_reported(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        await self._a_first_press_that_was_refused(config_with_publication_on, pool, repo_root)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        with pytest.MonkeyPatch.context() as crash:
            _stop_after_the_ending_is_written_down(crash, 2)
            with pytest.raises(_TheCoordinatorStopped):
                await _press(deps, repo_root)
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.turn == _record(pool).turn and written.finished_at is None
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert [r.details["result"] for r in _reports(pool)] == [
            "merge-refused",
            "merged-into-the-remote-and-running",
        ]
        assert _record(pool).lease_holder is None

    @pytest.mark.asyncio
    async def test_a_second_press_stopped_before_its_own_result_is_left_alone(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """The record still carries the FIRST press's result; the second press
        stopped while sending. Neither layer treats that result as the second
        press's: nothing reported, nothing released."""
        await self._a_first_press_that_was_refused(config_with_publication_on, pool, repo_root)
        deps, publisher, _deploy = _deps(config_with_publication_on, pool, "merged-and-running")

        def gone(_request: Any) -> None:
            raise _TheCoordinatorStopped()

        publisher.before_answering = gone
        with pytest.raises(_TheCoordinatorStopped):
            await _press(deps, repo_root)
        held = _record(pool)
        assert held.result and held.lease_holder
        assert not PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).result_written
        _expire(pool)
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []
        assert await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID) == "nothing to do"
        assert (_record(pool).lease_holder, _record(pool).turn) == (held.lease_holder, held.turn)
        assert len(_reports(pool)) == 1


class TestATurnedAwayConsumerPress:
    @pytest.mark.asyncio
    async def test_its_note_settles_only_its_own_decision_for_the_router(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """A press with an approved decision's attempt, turned away by a live
        hold, writes no report and does not close the build; its note under
        that attempt is all the router needs, and nothing else reads it."""
        _a_journey_waiting_on_its_press(pool)
        PublicationRecordStore(pool.connection).take_lease(
            build_id=BUILD_ID, holder="merge-press:elsewhere", now=datetime.now(UTC)
        )
        deps, publisher, deploy = _deps(config_with_publication_on, pool, "merged-and-running")
        assert f"merge-approved-pending:{BUILD_ID}" in _blocked(pool)[0]
        outcome = await _press(deps, repo_root)
        assert outcome.turned_away
        assert _reports(pool) == [] and deps.pipeline_publisher.reports == []
        assert _build(pool)[0] == "RUNNING"
        assert publisher.asked == [] and deploy.calls == []
        notes = [
            s for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == merge_executor.MERGE_TURNED_AWAY_TARGET_IDENTIFIER
        ]
        assert len(notes) == 1
        assert notes[0].details[MERGE_DECISION_DETAILS_KEY][MERGE_EXECUTION_ATTEMPT_ID_KEY] == ATTEMPT
        # Its own stage label: never read as the last "merge-deploy" row.
        assert notes[0].stage_label == "merge-deploy-turned-away"
        blockers = _blocked(pool)[0]
        assert f"merge-approved-pending:{BUILD_ID}" not in blockers
        # The press holding the record is still owed its own ending.
        assert f"publication-pending:{BUILD_ID}" in blockers
        assert _record(pool).lease_holder == "merge-press:elsewhere"


# ---------------------------------------------------------------------------
# Coach follow-ups (18 October 2026, after acceptance)
# ---------------------------------------------------------------------------


class TestAStepThatCannotBeWritten:
    """A step whose own record could not be written is done again on the next
    pass, and its check stops anything being done twice."""

    @pytest.mark.asyncio
    async def test_a_report_row_that_could_not_be_written_is_written_next_pass(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sqlite3

        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        real = pool.record_stage
        failed = {"n": 0}

        def locked_once(entry: Any) -> None:
            if entry.target_identifier == merge_executor.MERGE_REPORT_TARGET_IDENTIFIER and not failed["n"]:
                failed["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            real(entry)

        monkeypatch.setattr(pool, "record_stage", locked_once)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _reports(pool) == []  # the row could not be written
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.finished_at is None  # so it is not finished
        for _ in range(3):  # its hold lapses, and the next passes finish it
            _expire(pool)
            await _passes(config_with_publication_on, pool, deps)
        reports = _reports(pool)
        assert len(reports) == 1 and reports[0].details["press_turn"] == written.turn
        assert len(deps.pipeline_publisher.reports) == 1
        assert repairs == ["merged-deploy-failed"]
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        _everything_is_clear(pool, a_repair=True)

    @pytest.mark.asyncio
    async def test_an_older_table_shape_gets_its_missing_columns(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        pool.connection.execute(
            "CREATE TABLE publication_outcomes (build_id TEXT PRIMARY KEY, "
            "turn INTEGER NOT NULL, attempt_id TEXT, outcome_json TEXT NOT NULL, "
            "recorded_at TEXT NOT NULL)"
        )
        pool.connection.commit()
        outcome, *_ = await _uninterrupted(
            config_with_publication_on, pool, repo_root, "merged-and-running"
        )
        assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
        assert _record(pool).result == RESULT_MERGED_AND_RUNNING
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.result_written and written.finished_at


class TestANewMergeWordWhileAnEndingIsOwed:
    """Follow-up 2: in the moments after a stopped press's hold runs out and
    before the recovery's tick, a new press never wipes the owed ending."""

    @pytest.mark.asyncio
    async def test_the_new_press_is_turned_away_and_the_ending_is_reported(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
    ) -> None:
        deps, publisher, deploy = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        before = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        sent = _what_was_sent(publisher, deploy)
        _expire(pool)  # the hold runs out; the recovery's tick has not come yet
        again = await _cli_press(deps, repo_root)
        assert again.turned_away
        assert "ending has not been reported yet" in again.detail
        assert _reports(pool) == [] and _build(pool)[0] == "RUNNING"
        after = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert (after.turn, after.outcome, after.result_written) == (
            before.turn,
            before.outcome,
            before.result_written,
        )
        assert _what_was_sent(publisher, deploy) == sent
        await _passes(config_with_publication_on, pool, deps)
        assert [r.details["result"] for r in _reports(pool)] == ["merged-deploy-failed"]
        assert repairs == ["merged-deploy-failed"]
        assert _build(pool)[0] == "FAILED"
        _everything_is_clear(pool, a_repair=True)


class TestTheCoachsSurvivors:
    @pytest.mark.asyncio
    async def test_a_candidate_a_passed_check_left_standing_keeps_the_hold(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """No teardown answered that the candidate the passed check stood up
        is down (this project cannot name it), so the press keeps its hold,
        and the recovery leaves it kept."""
        from tests.forge.pipeline.test_the_merge_word_publishes import (
            _deps as _deps_naming_nothing,
            _some_other_refusal,
        )

        _a_journey_waiting_on_its_press(pool)
        deps, *_ = _deps_naming_nothing(
            config_with_publication_on,
            pool,
            publisher=_APublisherThatSays([_some_other_refusal("not-allowed", "the remote said no")]),
        )
        await _press(deps, repo_root)
        held = _record(pool)
        assert held.lease_holder
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.outcome["kept_because"] == "a candidate the check stood up may still be standing"
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        assert (_record(pool).lease_holder, _record(pool).turn) == (held.lease_holder, held.turn)



# ---------------------------------------------------------------------------
# Codex round 1: ONE idempotent finishing routine, keyed by the press's turn
# ---------------------------------------------------------------------------


class TestAStopAfterEachFinishingStep:
    """A stop after any finishing step leaves the rest to the next pass, which
    does only those: every step happens exactly once."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", FINISH_STEPS)
    async def test_each_step_happens_exactly_once(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        step: str,
    ) -> None:
        deps, publisher, deploy = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_step(crash, step),
        ) if step != "_finish_the_release" else await self._stopped_after_the_release(
            config_with_publication_on, pool, repo_root
        )
        sent = _what_was_sent(publisher, deploy)
        held = _record(pool)
        # A LIVE HOLD IS NEVER TOUCHED: a pass before the hold runs out does nothing.
        if held.lease_holder:
            await _passes(config_with_publication_on, pool, deps)
            assert (_record(pool).lease_holder, _record(pool).turn) == (held.lease_holder, held.turn)
        for _ in range(3):
            _expire(pool)
            await _passes(config_with_publication_on, pool, deps)

        assert len(_reports(pool)) == 1
        assert len(deps.pipeline_publisher.reports) == 1
        assert _build(pool)[0] == "FAILED"
        assert repairs == ["merged-deploy-failed"]
        assert _what_was_sent(publisher, deploy) == sent  # nothing sent to the remote again
        assert _record(pool).lease_holder is None
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []
        _everything_is_clear(pool, a_repair=True)

    async def _stopped_after_the_release(self, config, pool, repo_root):  # noqa: ANN001, F811
        """The last step done — the hold put down and ``finished_at`` set in
        one write (R6) — then the coordinator gone: nothing is left to do."""
        _a_journey_waiting_on_its_press(pool)
        deps, publisher, deploy = _deps(config, pool, "merged-deploy-failed")
        with pytest.MonkeyPatch.context() as crash:
            _stop_after_step(crash, "_finish_the_release")
            with pytest.raises(_TheCoordinatorStopped):
                await _press(deps, repo_root)
        assert _record(pool).lease_holder is None
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        return deps, publisher, deploy


class TestOwnershipByCompareAndSwap:
    def test_a_take_on_a_turn_that_moved_on_is_refused(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        _a_journey_waiting_on_its_press(pool)
        store = PublicationRecordStore(pool.connection)
        now = datetime.now(UTC)
        first = store.take_lease(build_id=BUILD_ID, holder="a", now=now)
        assert store.release_lease(build_id=BUILD_ID, turn=first.turn, holder="a", now=now)
        assert store.take_lease(
            build_id=BUILD_ID, holder="b", now=now, expected_turn=first.turn - 1
        ) is None
        assert store.last_refusal == "moved-on"
        grant = store.take_lease(build_id=BUILD_ID, holder="b", now=now, expected_turn=first.turn)
        assert grant is not None and grant.turn == first.turn + 1

    @pytest.mark.asyncio
    async def test_two_recoveries_on_two_connections_finish_it_once(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovery B, on its own connection, finishes the press between
        recovery A's read and A's take; A then does nothing at all."""
        import asyncio
        import dataclasses
        import threading

        from forge.adapters.sqlite import connect as sqlite_connect
        from tests.forge.pipeline.test_merge_executor import _FakePublisher

        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        _expire(pool)
        ledger = Path(pool.connection.execute("PRAGMA database_list").fetchone()[2])
        bus_b = _FakePublisher()
        answers: dict[str, Any] = {}

        def recovery_b() -> None:
            other = SqliteLifecyclePersistence(connection=sqlite_connect.connect_writer(ledger))
            consumer_b = MergeApprovalConsumer(
                dataclasses.replace(deps, pool=other, pipeline_publisher=bus_b)
            )
            answers["b"] = asyncio.run(consumer_b.finish_a_settled_press(BUILD_ID))

        real_take = PublicationRecordStore.take_lease
        raced = {"done": False}

        def b_gets_there_first(self: Any, **kwargs: Any) -> Any:
            if not raced["done"] and kwargs.get("expected_turn") is not None:
                raced["done"] = True
                runner = threading.Thread(target=recovery_b)
                runner.start()
                runner.join()
            return real_take(self, **kwargs)

        monkeypatch.setattr(PublicationRecordStore, "take_lease", b_gets_there_first)
        turn_before = _record(pool).turn
        answers["a"] = await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID)

        assert answers["b"] == "finished"
        assert answers["a"] == "held"  # its compare and swap found the turn moved on
        assert len(_reports(pool)) == 1
        assert len(bus_b.reports) == 1 and deps.pipeline_publisher.reports == []
        assert repairs == ["merged-deploy-failed"]
        record = _record(pool)
        assert record.lease_holder is None and record.turn == turn_before + 1
        _everything_is_clear(pool, a_repair=True)


class TestTheRecoveryReReadsAfterTakingTheRecord:
    @pytest.mark.asyncio
    async def test_a_press_that_finished_on_its_own_turn_in_between_is_left_alone(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The press outlived its lease and finished on its own turn between
        the recovery's read and its take (so the turn did not move): the
        recovery's re-read sees it finished and puts its own lease down."""
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda crash: _stop_after_step(crash, "_finish_the_repair_row"),
        )
        _expire(pool)
        press = _record(pool)
        real_take = PublicationRecordStore.take_lease

        def the_press_finishes_first(self: Any, **kwargs: Any) -> Any:
            if kwargs.get("expected_turn") is not None:
                store = PublicationRecordStore(pool.connection)
                assert store.release_lease(
                    build_id=BUILD_ID, turn=press.turn, holder=press.lease_holder, now=datetime.now(UTC)
                )
                assert store.mark_outcome(
                    build_id=BUILD_ID, turn=press.turn, finished_at=datetime.now(UTC).isoformat()
                )
            return real_take(self, **kwargs)

        monkeypatch.setattr(PublicationRecordStore, "take_lease", the_press_finishes_first)
        said = await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID)
        assert said == "nothing to do"
        assert _record(pool).lease_holder is None
        assert len(_reports(pool)) == 1


class TestARefusalReportsUnderItsOwnLease:
    @pytest.mark.asyncio
    async def test_a_takeover_while_its_bus_message_is_on_its_way_is_refused(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        """Codex round 1, R3: the press's own refusal keeps its lease through
        finishing, so another process trying to take the record while the
        refusal's bus message is suspended is refused."""
        import asyncio

        _a_journey_waiting_on_its_press(pool)
        TestARefusalBeforeTheJoin._no_target_branch(pool)
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        let_go = asyncio.Event()
        takes: list[Any] = []

        class _ASuspendedBus:
            def __init__(self) -> None:
                self.reports: list[Any] = []

            async def publish_stage_complete(self, payload: Any) -> None:
                takes.append(
                    PublicationRecordStore(pool.connection).take_lease(
                        build_id=BUILD_ID, holder="settled-press:elsewhere", now=datetime.now(UTC)
                    )
                )
                await let_go.wait()
                self.reports.append(payload)

        import dataclasses

        deps = dataclasses.replace(deps, pipeline_publisher=_ASuspendedBus())
        pressing = asyncio.create_task(_press(deps, repo_root))
        while not takes:
            await asyncio.sleep(0)
        let_go.set()
        outcome = await pressing
        assert outcome.result == "merge-refused"
        assert takes == [None]  # its live, owned lease refused the takeover
        assert len(_reports(pool)) == 1 and len(deps.pipeline_publisher.reports) == 1
        assert _record(pool).lease_holder is None
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at


# ---------------------------------------------------------------------------
# Codex round 2: R5 (the press's own attempt), R6 (no stranded lease),
# R7 (a step is done only when its durable result is there)
# ---------------------------------------------------------------------------


def _fix_rows(pool: SqliteLifecyclePersistence) -> list[Any]:  # noqa: F811
    return pool.connection.execute(
        "SELECT id FROM work_queue WHERE kind = 'fix' AND correlation_id = ?",
        (f"fix-{BUILD_ID}",),
    ).fetchall()


class TestAPressKeepsItsOwnAttempt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_at", ["result", "ending"])
    async def test_a_command_line_press_never_settles_an_outstanding_approval(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        stop_at: str,
    ) -> None:
        """Codex round 2, R5: an approval on the build is still outstanding when
        a command line press stops; its recovered report carries no attempt,
        so the approval stays pending for the router."""
        _a_journey_waiting_on_its_press(pool)  # the approved decision, attempt ATTEMPT
        deps, *_ = _deps(config_with_publication_on, pool, "merged-and-running")
        with pytest.MonkeyPatch.context() as crash:
            if stop_at == "result":
                _stop_after_the_result(crash, RESULT_MERGED_AND_RUNNING)
            else:
                _stop_after_the_ending_is_written_down(crash, 2)
            with pytest.raises(_TheCoordinatorStopped):
                await _cli_press(deps, repo_root)
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).attempt_id is None
        raw = pool.connection.execute(
            "SELECT attempt_id FROM publication_outcomes WHERE build_id = ?", (BUILD_ID,)
        ).fetchone()[0]
        assert raw == "none"  # an explicit "no approval asked for this press"
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps, times=2)
        reports = _reports(pool)
        assert len(reports) == 1
        assert MERGE_DECISION_DETAILS_KEY not in reports[0].details
        assert f"merge-approved-pending:{BUILD_ID}" in _blocked(pool)[0]


class TestNoLeaseIsLeftStranded:
    @pytest.mark.asyncio
    async def test_the_original_finishing_after_the_recoverys_re_read(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex round 2, R6, on two connections: the original worker's
        completion lands after the recovery has taken the record and re-read
        it, before finishing reads the press's row. The original cannot
        finish a record it no longer owns; the press is finished once, and
        the lease ends put down."""
        from forge.adapters.sqlite import connect as sqlite_connect

        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda crash: _stop_after_step(crash, "_finish_the_warning"),
        )
        original = _record(pool)
        _expire(pool)
        ledger = Path(pool.connection.execute("PRAGMA database_list").fetchone()[2])
        other = PublicationRecordStore(sqlite_connect.connect_writer(ledger))
        real_renew = PublicationRecordStore.renew_lease
        landed: list[bool] = []

        def the_original_completes_first(self: Any, **kwargs: Any) -> bool:
            if not landed and kwargs.get("turn") == original.turn + 1:
                landed.append(
                    other.finish_outcome(
                        build_id=BUILD_ID,
                        press_turn=original.turn,
                        lease_turn=original.turn,
                        holder=original.lease_holder,
                        now=datetime.now(UTC),
                        release=True,
                    )
                )
            return real_renew(self, **kwargs)

        monkeypatch.setattr(PublicationRecordStore, "renew_lease", the_original_completes_first)
        said = await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID)
        assert landed == [False]  # it no longer owned the record
        assert said == "finished"
        assert _record(pool).lease_holder is None
        assert len(_reports(pool)) == 1

    @pytest.mark.asyncio
    async def test_a_press_found_finished_after_the_take_has_its_lease_put_down(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whatever finished the press between the recovery's re-read and the
        finishing routine's own read, the lease the recovery took is put
        down, never stranded."""
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-and-running",
            lambda crash: _stop_after_step(crash, "_finish_the_warning"),
        )
        original = _record(pool)
        _expire(pool)
        real_renew = PublicationRecordStore.renew_lease
        done: list[bool] = []

        def finished_meanwhile(self: Any, **kwargs: Any) -> bool:
            if not done and kwargs.get("turn") == original.turn + 1:
                done.append(
                    PublicationRecordStore(pool.connection).mark_outcome(
                        build_id=BUILD_ID, turn=original.turn,
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                )
            return real_renew(self, **kwargs)

        monkeypatch.setattr(PublicationRecordStore, "renew_lease", finished_meanwhile)
        said = await MergeApprovalConsumer(deps).finish_a_settled_press(BUILD_ID)
        assert done == [True] and said == "finished"
        assert _record(pool).lease_holder is None  # not stranded
        finisher = _finisher(config_with_publication_on, pool, MergeApprovalConsumer(deps))
        assert mission_finish._settled_presses_to_finish(finisher, pool.connection) == []
        _everything_is_clear(pool)


class TestAStepIsDoneOnlyWhenItsResultIsThere:
    @pytest.mark.asyncio
    async def test_a_build_transition_that_failed_quietly_is_retried(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex round 2, R7: the build row's transition degrades without
        saying so on the first pass; the row is read, the step is retried on
        the next pass, and everything else happens once."""
        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        real_close = merge_executor.close_build_row
        closes: list[str] = []

        def degrades_once(pool_: Any, build_id: str, outcome: Any, **kwargs: Any) -> Any:
            closes.append(build_id)
            if len(closes) == 1:
                return "the build row could not be written (degraded)"
            return real_close(pool_, build_id, outcome, **kwargs)

        monkeypatch.setattr(merge_executor, "close_build_row", degrades_once)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _build(pool)[0] == "RUNNING"
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at is None
        for _ in range(3):
            _expire(pool)
            await _passes(config_with_publication_on, pool, deps)
        assert _build(pool)[0] == "FAILED"
        assert len(closes) == 2  # once degraded, once done, never again
        assert len(_reports(pool)) == 1 and len(deps.pipeline_publisher.reports) == 1
        assert len(_fix_rows(pool)) == 1 and repairs == ["merged-deploy-failed"]
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        _everything_is_clear(pool, a_repair=True)

    @pytest.mark.asyncio
    async def test_a_repair_insert_that_failed_quietly_is_retried(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        repairs: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from forge.pipeline import fix_row_producer

        deps, *_ = await _the_press_stops(
            config_with_publication_on,
            pool,
            repo_root,
            "merged-deploy-failed",
            lambda crash: _stop_after_the_ending_is_written_down(crash, 2),
        )
        real_mint = fix_row_producer.maybe_mint_fix_row
        mints: list[int] = []

        def swallows_once(**kwargs: Any) -> Any:
            mints.append(1)
            if len(mints) == 1:
                return None  # the producer swallowed its own failure
            return real_mint(**kwargs)

        monkeypatch.setattr(fix_row_producer, "maybe_mint_fix_row", swallows_once)
        _expire(pool)
        await _passes(config_with_publication_on, pool, deps)
        assert _fix_rows(pool) == []
        written = PublicationRecordStore(pool.connection).read_outcome(BUILD_ID)
        assert written.repair_at is None and written.finished_at is None
        for _ in range(3):
            _expire(pool)
            await _passes(config_with_publication_on, pool, deps)
        assert len(_fix_rows(pool)) == 1
        assert len(mints) == 2  # once failed, once filed, never again
        assert len(_reports(pool)) == 1 and len(deps.pipeline_publisher.reports) == 1
        assert _build(pool)[0] == "FAILED"
        assert PublicationRecordStore(pool.connection).read_outcome(BUILD_ID).finished_at
        _everything_is_clear(pool, a_repair=True)
