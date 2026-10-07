"""Unit tests: Forge's half of "already done, nothing to build" (7 October 2026).

Design of record: ai-transition ``docs/designs/already-done-outcome-2026-10-07.md``
Part 2 sections 4 and 7. Every repository here is a neutral scratch one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.adapters.nats.pipeline_publisher import PipelinePublisher
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_planning import (
    build_feature_plan_command_args,
    planning_complete_payload,
)
from forge.config.models import NothingToBuildConfig, PlanningConfig
from forge.lifecycle import migrations
from forge.planning import nothing_to_build as ntb
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import (
    PLANNING_TRANSITIONS,
    PLANNING_TRANSITIONS_TARGET_TERMINAL,
    PlanningState,
)
from forge.planning.work_queue_commands import (
    ALREADY_DONE,
    closed_word,
    was_already_done,
)
from forge.planning.work_queue_loop import (
    BUILT_NOTHING,
    LOOP_ACTOR,
    WorkQueueLoop,
    work_landing,
)
from forge.planning.work_queue_store import WorkQueueStore
from nats_core.envelope import EventType
from nats_core.events import PlanningCompletePayload

GUIDE = "tasks/backlog/stats-endpoint/IMPLEMENTATION-GUIDE.md"

DESCRIPTOR: dict[str, Any] = {
    "repo": "example/service",
    "test_roots": ["tests/stats"],
    "where_the_specs_words_already_appear": [
        {
            "words": "stats",
            "already_in": ["src/stats.py:6", "tests/stats/test_stats.py:2"],
            "evidence": [
                {"path": "src/stats.py", "first_line": 3, "last_line": 18, "score": 2, "text": "..."},
                {"path": "tests/stats/test_stats.py", "first_line": 1, "last_line": 14, "score": 1, "text": "..."},
            ],
            "more_hits": 0,
        }
    ],
}


def _validation(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "accepted": True,
        "outcome": "nothing_to_build",
        "parts": [{"quote": "add a GET /stats endpoint", "citations": ["src/stats.py:6", "src/stats.py:7-9"]}],
        "scenarios": [{"title": "ok", "citations": ["tests/stats/test_stats.py:1-4"]}],
        "gates_run": ["nothing_to_build"],
    }
    body.update(overrides)
    return json.dumps(body)


def _reply(validation: str, files: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    files = files if files is not None else {GUIDE: "# plan\n"}
    return {**files, "validation.json": validation}, files


# ---------------------------------------------------------------------------
# Recognising the answer
# ---------------------------------------------------------------------------


def test_a_reply_without_the_marker_is_todays_plan() -> None:
    for validation in (
        json.dumps({"accepted": True, "errors": [], "gates_run": ["feature_validate"]}),
        "",
        "not json",
    ):
        role_output, files = _reply(validation)
        assert ntb.read_claim(role_output, files) == (None, None)
    assert ntb.read_claim({GUIDE: "x"}, {GUIDE: "x"}) == (None, None)


def test_a_well_formed_answer_is_read() -> None:
    role_output, files = _reply(_validation())
    claim, problem = ntb.read_claim(role_output, files)
    assert problem is None and claim is not None
    assert claim.parts == (("add a GET /stats endpoint", ("src/stats.py:6", "src/stats.py:7-9")),)
    assert claim.scenarios == (("ok", ("tests/stats/test_stats.py:1-4",)),)
    # A README beside the plan document is allowed.
    role_output, files = _reply(_validation(), {GUIDE: "# plan\n", "tasks/backlog/x/README.md": "r"})
    assert ntb.read_claim(role_output, files)[0] is not None


@pytest.mark.parametrize(
    ("overrides", "files", "expected"),
    [
        ({"accepted": False}, None, "did not accept"),
        ({}, {GUIDE: "x", ".guardkit/features/FEAT-0001.yaml": "id: x\n"}, "also sent a plan to build"),
        ({}, {GUIDE: "x", "tasks/backlog/x/TASK-0001-001.md": "# t\n"}, "also sent a plan to build"),
        ({"parts": []}, None, "listed no parts of the request"),
        ({"scenarios": []}, None, "listed no approved examples"),
        ({"parts": [{"quote": "x", "citations": []}]}, None, '"x" cites no line of code'),
        ({"scenarios": [{"title": "", "citations": ["a:1"]}]}, None, "has no title"),
        ({"parts": ["just words"]}, None, "is not a list entry"),
    ],
)
def test_a_malformed_answer_says_why(overrides: dict, files: Any, expected: str) -> None:
    role_output, files = _reply(_validation(**overrides), files)
    claim, problem = ntb.read_claim(role_output, files)
    assert claim is None
    assert problem is not None and expected in problem


# ---------------------------------------------------------------------------
# Checking the proof against the windows Forge sent
# ---------------------------------------------------------------------------


def test_windows_are_read_wherever_the_description_carries_them() -> None:
    windows = ntb.windows_sent(DESCRIPTOR)
    assert windows == [
        ntb.Window("src/stats.py", 3, 18),
        ntb.Window("tests/stats/test_stats.py", 1, 14),
    ]
    assert ntb.windows_sent({"repo": "x", "test_roots": []}) == []
    assert ntb.windows_sent(None) == []


def test_every_citation_inside_a_window_passes() -> None:
    role_output, files = _reply(_validation())
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    assert ntb.citations_outside(claim, ntb.windows_sent(DESCRIPTOR)) == []


@pytest.mark.parametrize(
    "citation",
    [
        "src/stats.py:2",  # one line before the window
        "src/stats.py:17-19",  # runs past its end
        "src/other.py:6",  # a file never shown
        "src/stats.py",  # no line
        "src/stats.py:9-7",  # backwards
        "src/stats.py:6 the route",  # not a bare citation
    ],
)
def test_a_citation_outside_every_window_is_named(citation: str) -> None:
    role_output, files = _reply(
        _validation(parts=[{"quote": "q", "citations": ["src/stats.py:6", citation]}])
    )
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    assert ntb.citations_outside(claim, ntb.windows_sent(DESCRIPTOR)) == [citation]


def test_a_citation_splits_at_its_last_colon() -> None:
    """The plan writer's rule: a path may hold a colon; the line is after
    the last one."""
    windows = [ntb.Window("docs/a:b.md", 10, 20)]
    role_output, files = _reply(
        _validation(
            parts=[{"quote": "q", "citations": ["docs/a:b.md:12", "docs/a:b.md:11-19"]}],
            scenarios=[{"title": "t", "citations": ["docs/a:b.md:20"]}],
        )
    )
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    assert ntb.citations_outside(claim, windows) == []
    role_output, files = _reply(
        _validation(parts=[{"quote": "q", "citations": ["docs/a:12"]}])
    )
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    assert ntb.citations_outside(claim, windows) == ["docs/a:12", "tests/stats/test_stats.py:1-4"]


def test_the_planners_full_validation_shape_is_read() -> None:
    """Exactly what the plan writer sends (specialist-agent 820d701): the
    scenario-ownership note rides along and is not read."""
    role_output, files = _reply(
        _validation(scenario_owners={"status": "not needed", "note": "nothing to build"})
    )
    files["tasks/backlog/stats-endpoint/README.md"] = "# readme\n"
    role_output["tasks/backlog/stats-endpoint/README.md"] = "# readme\n"
    claim, problem = ntb.read_claim(role_output, files)
    assert problem is None and claim is not None


def test_no_windows_sent_means_nothing_is_inside() -> None:
    role_output, files = _reply(_validation())
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    assert ntb.citations_outside(claim, []) == [
        "src/stats.py:6",
        "src/stats.py:7-9",
        "tests/stats/test_stats.py:1-4",
    ]


# ---------------------------------------------------------------------------
# What it says, and what it records
# ---------------------------------------------------------------------------


def _claim() -> ntb.Claim:
    role_output, files = _reply(_validation())
    claim, _ = ntb.read_claim(role_output, files)
    assert claim is not None
    return claim


def test_the_owner_message_lists_every_proof_line_in_plain_words() -> None:
    message = ntb.owner_message("add a  GET /stats\nendpoint", "example/service", _claim())
    assert message == (
        'Already done, nothing to build. "add a GET /stats endpoint" is already in service:\n'
        '- "add a GET /stats endpoint" is already done: src/stats.py:6, src/stats.py:7-9\n'
        '- "ok" is checked by an existing test: tests/stats/test_stats.py:1-4\n'
        "Nothing was built or merged. If something is missing, send the sentence "
        "again and say what."
    )


def test_the_details_carry_the_marker_and_the_proof() -> None:
    details = ntb.terminal_details(_claim(), "the message")
    assert details == {
        "outcome": "nothing_to_build",
        "proof": [
            '"add a GET /stats endpoint" is already done: src/stats.py:6, src/stats.py:7-9',
            '"ok" is checked by an existing test: tests/stats/test_stats.py:1-4',
        ],
        "owner_message": "the message",
    }
    assert json.loads(json.dumps(details)) == details


def test_the_queue_reason() -> None:
    details = ntb.terminal_details(_claim(), "m")
    assert ntb.queue_reason(details) == (
        'already done, nothing to build: "add a GET /stats endpoint" is already '
        "done: src/stats.py:6, src/stats.py:7-9"
    )
    assert ntb.queue_reason({"outcome": "nothing_to_build"}) == "already done, nothing to build"
    assert ntb.queue_reason({"failure": {}}) is None
    assert ntb.queue_reason(None) is None


# ---------------------------------------------------------------------------
# The switch, the state table and the wire
# ---------------------------------------------------------------------------


def test_the_switch_is_off_by_default_and_forbids_unknown_keys() -> None:
    assert PlanningConfig().nothing_to_build.enabled is False
    assert PlanningConfig(nothing_to_build={"enabled": True}).nothing_to_build.enabled
    with pytest.raises(Exception):
        NothingToBuildConfig(enabled=True, sometimes=True)  # type: ignore[call-arg]


def test_the_edge_exists_in_the_machine_chain_table_only() -> None:
    assert PlanningState.PLANNED_HANDOFF in PLANNING_TRANSITIONS_TARGET_TERMINAL[PlanningState.FEATURE_PLAN]
    assert PlanningState.BUILD_QUEUED in PLANNING_TRANSITIONS_TARGET_TERMINAL[PlanningState.FEATURE_PLAN]
    assert PlanningState.FEATURE_PLAN not in PLANNING_TRANSITIONS
    assert PLANNING_TRANSITIONS[PlanningState.RUNNING] == {
        PlanningState.PAUSED,
        PlanningState.FAILED,
        PlanningState.TIMED_OUT,
        PlanningState.PLANNED_HANDOFF,
    }


def test_the_plan_call_asks_only_when_told_to() -> None:
    base = dict(
        feature_id="FEAT-BEEF",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "example/service", "test_roots": []},
    )
    assert "already_done_allowed" not in build_feature_plan_command_args(**base)
    assert "already_done_allowed" not in build_feature_plan_command_args(
        **base, already_done_allowed=False
    )
    args = build_feature_plan_command_args(**base, already_done_allowed=True)
    assert args["already_done_allowed"] is True
    assert set(args) == {
        "feature_id",
        "spec_feature",
        "spec_summary",
        "target_repo_descriptor",
        ntb.REQUEST_FIELD,
    }


# ---------------------------------------------------------------------------
# The event: pipeline.planning-complete, with nats-core unchanged
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "originating_user": "U-OWNER",
        "started_at": "2026-10-07T10:00:00+00:00",
        "queued_at": "2026-10-07T09:59:00+00:00",
    }
    row.update(overrides)
    return row


def test_the_payload_fills_its_typed_fields_honestly_and_keeps_the_extras() -> None:
    proof = ntb.proof_lines(_claim())
    payload = planning_complete_payload(
        _row(),
        correlation_id="cid-1",
        feature_id="FEAT-AB12",
        extra={"outcome": "nothing_to_build", "proof": proof, "summary": "the message"},
        completed_at=datetime(2026, 10, 7, 10, 5, tzinfo=timezone.utc),
    )
    assert isinstance(payload, PlanningCompletePayload)
    dumped = payload.model_dump(mode="json")
    assert dumped["terminal_state"] == "planned_handoff"
    assert dumped["originator"] == "U-OWNER"
    assert dumped["feat_id"] == "FEAT-AB12"
    assert dumped["duration_seconds"] == 300
    assert dumped["outcome"] == "nothing_to_build"
    assert dumped["proof"] == proof
    assert dumped["summary"] == "the message"
    # It validates against the unchanged payload, extras and all.
    again = PlanningCompletePayload.model_validate(dumped)
    assert again.model_dump(mode="json") == dumped
    # No readable start: no duration, never a guess.
    no_start = planning_complete_payload(
        _row(started_at=None, queued_at="garbled"),
        correlation_id="cid-1",
        feature_id="FEAT-AB12",
        extra={},
        completed_at=datetime(2026, 10, 7, 10, 5, tzinfo=timezone.utc),
    )
    assert no_start.duration_seconds is None


@pytest.mark.asyncio
async def test_planning_complete_is_published_on_its_own_subject() -> None:
    client = AsyncMock()
    client.publish = AsyncMock(return_value=None)
    payload = planning_complete_payload(
        _row(),
        correlation_id="cid-1",
        feature_id="FEAT-AB12",
        extra={"outcome": "nothing_to_build", "proof": ["a"], "summary": "s"},
        completed_at=datetime(2026, 10, 7, 10, 5, tzinfo=timezone.utc),
    )
    await PipelinePublisher(nats_client=client).publish_planning_complete(payload)
    client.publish.assert_awaited_once()
    subject, body = client.publish.call_args.args
    envelope = json.loads(body)
    assert subject == "pipeline.planning-complete.cid-1"
    assert envelope["event_type"] == EventType.PLANNING_COMPLETE.value
    assert envelope["correlation_id"] == "cid-1"
    assert envelope["source_id"] == "forge"
    assert envelope["payload"]["outcome"] == "nothing_to_build"
    assert envelope["payload"]["proof"] == ["a"]
    assert envelope["payload"]["summary"] == "s"


# ---------------------------------------------------------------------------
# The queue: the row closes DONE with the reason; what waits behind it goes
# ---------------------------------------------------------------------------


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


class _Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def _end_nothing_to_build(run_store: SqlitePlanningRunStore, cid: str) -> dict[str, Any]:
    assert (
        run_store.record_queued(
            correlation_id=cid,
            originating_user="U-OWNER",
            expected_approver="U-OWNER",
            request_text="add a GET /stats endpoint",
            triggered_by="jarvis",
        )
        is None
    )
    for state in (PlanningState.RUNNING, PlanningState.FEATURE_SPEC, PlanningState.FEATURE_PLAN):
        assert run_store.transition(cid, state, "planning-driver") is None
    details = ntb.terminal_details(_claim(), "m")
    assert (
        run_store.transition(
            correlation_id=cid,
            to_state=PlanningState.PLANNED_HANDOFF,
            actor_identity="planning-driver",
            stage_label=ntb.STAGE_LABEL,
            details_json=json.dumps(details),
            expected_from_state=PlanningState.FEATURE_PLAN,
        )
        is None
    )
    return details


@pytest.mark.asyncio
async def test_the_row_closes_done_with_the_reason_and_the_next_row_is_taken(
    connection: sqlite3.Connection,
) -> None:
    clock = _Clock()
    store = WorkQueueStore(connection, clock=clock.now)
    run_store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    started: list[str] = []

    async def start_run(admission: Any) -> None:
        started.append(admission.correlation_id)

    async def notify(*args: Any, **kwargs: Any) -> None:
        return None

    loop = WorkQueueLoop(
        store,
        count_in_flight=lambda: 0,
        planning_run=run_store.get_run,
        paused_repositories=set,
        start_run=start_run,
        notify=notify,
        clock=clock.now,
        run_events=run_store.list_events,
    )
    first = store.file_sentence(
        correlation_id="plan-1",
        sentence="add a GET /stats endpoint",
        originating_user="U-OWNER",
        target_repo="example/service",
    ).queue_id
    assert await loop.take_next() == first
    second = store.file_sentence(
        correlation_id="plan-2",
        sentence="add a GET /stats/today endpoint",
        originating_user="U-OWNER",
        target_repo="example/service",
    ).queue_id
    assert store.link(second, first, actor_identity="U-OWNER")
    assert store.get(second)["after_id"] == first
    # While the first is still planning, the row behind it waits.
    assert await loop.take_next() is None

    details = _end_nothing_to_build(run_store, "plan-1")
    assert loop.close_finished() == 1

    row = store.get(first)
    assert row["status"] == "DONE"
    assert row["closed_reason"] == ntb.queue_reason(details)
    assert was_already_done(row)
    assert closed_word(row) == ALREADY_DONE == "already done, nothing to build"
    # No build, none promised: the work behind it may go.
    assert work_landing(connection, "plan-1") == BUILT_NOTHING
    assert await loop.take_next() == second
    assert started == ["plan-1", "plan-2"]


def test_an_ordinary_done_row_still_reads_done(connection: sqlite3.Connection) -> None:
    clock = _Clock()
    store = WorkQueueStore(connection, clock=clock.now)
    queue_id = store.file_sentence(
        correlation_id="plan-9",
        sentence="s",
        originating_user="U-OWNER",
    ).queue_id
    store.admit(queue_id, actor_identity=LOOP_ACTOR)
    assert store.close(queue_id, status="DONE", actor_identity=LOOP_ACTOR)
    row = store.get(queue_id)
    assert closed_word(row) == "done"
    assert not was_already_done(row)


def test_a_build_queued_run_without_the_marker_closes_with_no_reason(
    connection: sqlite3.Connection,
) -> None:
    clock = _Clock()
    store = WorkQueueStore(connection, clock=clock.now)
    run_store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    queue_id = store.file_sentence(
        correlation_id="plan-3", sentence="s", originating_user="U-OWNER"
    ).queue_id
    store.admit(queue_id, actor_identity=LOOP_ACTOR)
    run_store.record_queued(
        correlation_id="plan-3",
        originating_user="U-OWNER",
        expected_approver="U-OWNER",
        request_text="s",
        triggered_by="jarvis",
    )
    for state in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
        PlanningState.BUILD_QUEUED,
    ):
        assert run_store.transition("plan-3", state, "planning-driver") is None

    async def notify(*args: Any, **kwargs: Any) -> None:
        return None

    loop = WorkQueueLoop(
        store,
        count_in_flight=lambda: 0,
        planning_run=run_store.get_run,
        paused_repositories=set,
        start_run=AsyncMock(),
        notify=notify,
        clock=clock.now,
        run_events=run_store.list_events,
    )
    assert loop.close_finished() == 1
    row = store.get(queue_id)
    assert row["status"] == "DONE" and row["closed_reason"] is None


def test_the_dispatcher_carries_the_flag_as_a_boolean_on_both_calls() -> None:
    from forge.pipeline.dispatchers.specialist import (
        FINAL_PLAN_REVIEW_FEEDBACK,
        build_specialist_command,
    )
    from forge.pipeline.stage_taxonomy import StageClass

    plan_only = {GUIDE: "# plan\n"}
    for extra in (
        {"feature_id": "FEAT-1234", "already_done_allowed": True},
        {
            "feature_id": "FEAT-1234",
            "revision_of": plan_only,
            "validate_feedback": FINAL_PLAN_REVIEW_FEEDBACK,
            "already_done_allowed": True,
        },
    ):
        _command, args = build_specialist_command(
            StageClass.FEATURE_PLAN,
            request_text=None,
            context_entries=[],
            extra_command_args=extra,
        )
        assert args["already_done_allowed"] is True
    assert args["semantic_review_artifacts"] == plan_only
    assert json.loads(json.dumps(args))["already_done_allowed"] is True
