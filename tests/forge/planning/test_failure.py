"""``forge.planning.failure`` — the one way a planning run is ended loudly.

Part I of ``docs/rewrite-on-refusal-spec-2026-09-06.md`` (rule 35): the
sentence the owner is sent is also written on the FAILED event's details,
beside the machine reason and the stage label, so the work queue can close
the row with the words Rich already read. Everything here is read back
through the real planning run store on a throwaway database; nothing opens
a socket.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.failure import (
    DRIVER_ACTOR,
    FAILURE_DETAILS_KEY,
    OWNER_MESSAGE_KEY,
    fail_run,
    failure_details,
    mark_run_failed,
)
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

USER = "U-RICH"
RUN = "696a3e38"
STAGE = "feature-plan"

#: The machine reason the live run 696a3e38 stopped with — the words that
#: reached the queue's closing line before this lane, and must never again.
MACHINE_REASON = (
    "007 dispatch error: Command 'feature_spec' failed: Registered mode "
    "'feature_spec' refused the revision"
)

#: The sentence the driver sent Rich for the same run.
OWNER_SENTENCE = (
    "Planning run 696a3e38 stopped at writing the task plan: 1 of the worked "
    "examples could not be proven as written, and when the machine asked the "
    "spec writer to rewrite them as what the endpoint does, the checker "
    "refused the rewrite twice (the rewritten example still described the "
    "database). Nothing was built. To try again, send the sentence as what "
    "the endpoint does: the method and path, the status code, and what is in "
    "the reply."
)


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture
def run_store(connection: sqlite3.Connection) -> SqlitePlanningRunStore:
    return SqlitePlanningRunStore(connection, target_terminal_enabled=True)


def _start_run(run_store: SqlitePlanningRunStore, correlation_id: str = RUN) -> None:
    """A run that has reached the spec leg, as the driver leaves it."""
    assert (
        run_store.record_queued(
            correlation_id=correlation_id,
            originating_user=USER,
            expected_approver=USER,
            request_text="add pagination to GET /users",
            triggered_by="jarvis",
        )
        is None
    )
    for state in (PlanningState.RUNNING, PlanningState.FEATURE_SPEC):
        assert run_store.transition(correlation_id, state, DRIVER_ACTOR) is None


def _failed_events(
    run_store: SqlitePlanningRunStore, correlation_id: str = RUN
) -> list[sqlite3.Row]:
    return [
        event
        for event in run_store.list_events(correlation_id)
        if str(event["status"]) == PlanningState.FAILED.value
    ]


class Recorder:
    """Captures every sentence the owner would have been sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, correlation_id: str, message: str) -> None:
        self.sent.append((correlation_id, message))


class TerminalRecorder:
    """Captures each derived planning-failed projection."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, correlation_id: str, reason: str) -> None:
        self.sent.append((correlation_id, reason))


class RecordingStore:
    """A store that only remembers how ``transition`` was called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def transition(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        return None


# ---------------------------------------------------------------------------
# fail_run writes the sentence on the FAILED event
# ---------------------------------------------------------------------------


class TestFailRunWritesTheSentence:
    """Rule 35: the FAILED event's details carry the owner's sentence, the
    machine reason and the stage label; the run's ``error`` column keeps the
    machine reason exactly as before."""

    @pytest.mark.asyncio
    async def test_the_failed_event_carries_the_sentence_beside_the_machine_reason(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        _start_run(run_store)
        notify = Recorder()

        result = await fail_run(
            run_store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
            notify=notify,
        )

        assert result is False
        run = run_store.get_run(RUN)
        assert run is not None
        assert run["state"] == PlanningState.FAILED.value
        assert run["error"] == MACHINE_REASON
        (event,) = _failed_events(run_store)
        assert event["stage_label"] == STAGE
        assert event["actor_identity"] == DRIVER_ACTOR
        assert json.loads(event["details_json"]) == {
            "failure": {
                "owner_message": OWNER_SENTENCE,
                "reason": MACHINE_REASON,
                "stage_label": STAGE,
            }
        }
        assert notify.sent == [(RUN, OWNER_SENTENCE)]

    @pytest.mark.asyncio
    async def test_the_failed_event_is_the_last_one_on_the_run(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        """The queue reads events newest-first for the terminal one; the
        store must hand it back last."""
        _start_run(run_store)
        await fail_run(
            run_store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
        )

        last = run_store.list_events(RUN)[-1]
        assert last["status"] == PlanningState.FAILED.value
        details = json.loads(last["details_json"])
        assert details[FAILURE_DETAILS_KEY][OWNER_MESSAGE_KEY] == OWNER_SENTENCE

    @pytest.mark.asyncio
    async def test_the_intake_actor_is_written_as_itself(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        """A refusal at the door is never read later as a driver failure."""
        _start_run(run_store)
        await fail_run(
            run_store,
            RUN,
            stage_label="unknown-repository",
            reason="no repository named 'api-tset' is registered",
            owner_message=(
                "Planning run 696a3e38 stopped before it started: no "
                "repository named 'api-tset' is registered."
            ),
            actor="planning-intake",
        )

        (event,) = _failed_events(run_store)
        assert event["actor_identity"] == "planning-intake"
        details = json.loads(event["details_json"])
        assert details["failure"]["stage_label"] == "unknown-repository"

    def test_the_details_helper_is_what_the_event_carries(self) -> None:
        assert failure_details(
            owner_message=OWNER_SENTENCE, reason=MACHINE_REASON, stage_label=STAGE
        ) == {
            FAILURE_DETAILS_KEY: {
                OWNER_MESSAGE_KEY: OWNER_SENTENCE,
                "reason": MACHINE_REASON,
                "stage_label": STAGE,
            }
        }
        assert FAILURE_DETAILS_KEY == "failure"
        assert OWNER_MESSAGE_KEY == "owner_message"

    @pytest.mark.asyncio
    async def test_a_refused_transition_writes_no_second_event_and_still_notifies(
        self, run_store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing a run that has already failed is refused by the store,
        logged, and the owner is still told — exactly as before."""
        _start_run(run_store)
        notify = Recorder()
        await fail_run(
            run_store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
            notify=notify,
        )

        with caplog.at_level(logging.WARNING, logger="forge.planning.failure"):
            result = await fail_run(
                run_store,
                RUN,
                stage_label=STAGE,
                reason="a second reason",
                owner_message="a second sentence",
                notify=notify,
            )

        assert result is False
        assert len(_failed_events(run_store)) == 1
        run = run_store.get_run(RUN)
        assert run is not None and run["error"] == MACHINE_REASON
        assert notify.sent == [(RUN, OWNER_SENTENCE), (RUN, "a second sentence")]
        assert any(
            "FAILED transition refused" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_terminal_projection_follows_commit_once_not_duplicate(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        _start_run(run_store)
        terminal = TerminalRecorder()

        await fail_run(
            run_store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
            publish_terminal=terminal,
        )
        await fail_run(
            run_store,
            RUN,
            stage_label=STAGE,
            reason="duplicate reason",
            owner_message="duplicate sentence",
            publish_terminal=terminal,
        )

        assert terminal.sent == [(RUN, MACHINE_REASON)]
        assert len(_failed_events(run_store)) == 1

    @pytest.mark.asyncio
    async def test_terminal_publish_failure_keeps_row_and_owner_notification(
        self, run_store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        _start_run(run_store)
        notify = Recorder()

        async def broken_terminal(_cid: str, _reason: str) -> None:
            raise RuntimeError("projection unavailable")

        with caplog.at_level(logging.WARNING, logger="forge.planning.failure"):
            await fail_run(
                run_store,
                RUN,
                stage_label=STAGE,
                reason=MACHINE_REASON,
                owner_message=OWNER_SENTENCE,
                notify=notify,
                publish_terminal=broken_terminal,
            )

        row = run_store.get_run(RUN)
        assert row is not None and row["state"] == PlanningState.FAILED.value
        assert notify.sent == [(RUN, OWNER_SENTENCE)]
        assert any(
            "planning-failed projection did not go out" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_notifier_that_throws_never_blocks_the_row(
        self, run_store: SqlitePlanningRunStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        _start_run(run_store)

        async def broken(_cid: str, _message: str) -> None:
            raise RuntimeError("slack is down")

        with caplog.at_level(logging.WARNING, logger="forge.planning.failure"):
            result = await fail_run(
                run_store,
                RUN,
                stage_label=STAGE,
                reason=MACHINE_REASON,
                owner_message=OWNER_SENTENCE,
                notify=broken,
            )

        assert result is False
        run = run_store.get_run(RUN)
        assert run is not None and run["state"] == PlanningState.FAILED.value
        (event,) = _failed_events(run_store)
        assert json.loads(event["details_json"])["failure"]["owner_message"] == (
            OWNER_SENTENCE
        )
        assert any(
            "did not go out" in record.getMessage() for record in caplog.records
        )


# ---------------------------------------------------------------------------
# mark_run_failed on its own writes today's row
# ---------------------------------------------------------------------------


class TestMarkRunFailedAlone:
    """The driver's own ``_fail`` calls ``mark_run_failed`` with no owner
    sentence; that row is written exactly as before, with no details."""

    def test_without_an_owner_sentence_the_event_has_no_details(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        _start_run(run_store)

        mark_run_failed(run_store, RUN, stage_label=STAGE, reason=MACHINE_REASON)

        run = run_store.get_run(RUN)
        assert run is not None
        assert run["state"] == PlanningState.FAILED.value
        assert run["error"] == MACHINE_REASON
        (event,) = _failed_events(run_store)
        assert event["details_json"] is None

    def test_with_an_owner_sentence_it_writes_what_fail_run_writes(
        self, run_store: SqlitePlanningRunStore
    ) -> None:
        _start_run(run_store)

        mark_run_failed(
            run_store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
        )

        (event,) = _failed_events(run_store)
        assert json.loads(event["details_json"]) == failure_details(
            owner_message=OWNER_SENTENCE, reason=MACHINE_REASON, stage_label=STAGE
        )

    def test_the_store_is_called_as_today_when_there_is_no_sentence(self) -> None:
        """Byte for byte the old call: no ``details_json`` keyword at all, so a
        store that never learned the keyword is not surprised by it."""
        store = RecordingStore()

        mark_run_failed(store, RUN, stage_label=STAGE, reason=MACHINE_REASON)

        assert store.calls == [
            {
                "correlation_id": RUN,
                "to_state": PlanningState.FAILED,
                "actor_identity": DRIVER_ACTOR,
                "stage_label": STAGE,
                "error": MACHINE_REASON,
            }
        ]

    @pytest.mark.asyncio
    async def test_fail_run_adds_only_the_details_to_that_call(self) -> None:
        store = RecordingStore()

        await fail_run(
            store,
            RUN,
            stage_label=STAGE,
            reason=MACHINE_REASON,
            owner_message=OWNER_SENTENCE,
        )

        (call,) = store.calls
        assert call["error"] == MACHINE_REASON
        assert call["actor_identity"] == DRIVER_ACTOR
        assert json.loads(call["details_json"]) == failure_details(
            owner_message=OWNER_SENTENCE, reason=MACHINE_REASON, stage_label=STAGE
        )
