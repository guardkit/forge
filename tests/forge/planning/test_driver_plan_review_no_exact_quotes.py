"""The plan read against the request, at the driver, without the exact-quote rule.

WHY (16 October 2026). Rich, in his words: "insists on exact wording from an
LLM generated text is just never going to be reliable so I don't think
insisting on exact wording makes sense". So a task that says what it serves in
its own words is never sent back and never puts a line on the card. The two
checks that catch a real addition stay exactly as they were: a task naming a
web address the request did not name, and a task adding a capability nobody
asked for, are sent back once and, if the rewrite still carries them, stop the
run before anything is written.

Design of record: ai-transition
``docs/designs/planning-remove-exact-quote-rule-2026-10-16.md`` section 5
("Forge: a plan whose tasks paraphrase the request gets no rewrite round and no
card line; a task naming /notes/v2 when the request names /notes, and a task
adding rate limiting, are still sent back and then stopped").

These call the driver's own plan review step on a real SQLite store, with a
stand-in plan writer for the one rewrite round.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    ORIGINATOR,
    TARGET_REPO,
    _local_remotes,  # noqa: F401 — the autouse fixture
    _make_driver,
    store,  # noqa: F401 — the fixture, re-exported for these tests
)

REQUEST = "Add a GET /notes endpoint that lists every note."
FEATURE = "FEAT-NOTE"


def _task(task_id: str, body: str) -> dict[str, str]:
    head = f"---\nid: {task_id}\nfeature_id: {FEATURE}\n---\n\n"
    return {f"tasks/backlog/notes/{task_id}.md": head + body}


#: A plan whose tasks say what they serve in their own words — none of them an
#: exact run of the request's words.
PARAPHRASED = {
    **_task(
        "TASK-NOTE-001",
        "# List the notes\n\n"
        "## The words of the request this task serves\n\n"
        "Listing all the notes there are.\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] GET /notes answers with every stored note\n",
    ),
    **_task(
        "TASK-NOTE-002",
        "# Tests for the list\n\n"
        "## The words of the request this task serves\n\n"
        "> Show me all of my notes\n\n"
        "## Acceptance Criteria\n\n"
        "- [ ] A test proves the list holds each note once\n",
    ),
}

MOVED_ADDRESS = _task(
    "TASK-NOTE-001",
    "# List the notes\n\n"
    "## The words of the request this task serves\n\n"
    "Listing all the notes there are.\n\n"
    "## Acceptance Criteria\n\n"
    "- [ ] GET /notes/v2 answers with every stored note\n",
)

RATE_LIMITED = _task(
    "TASK-NOTE-001",
    "# List the notes\n\n"
    "## The words of the request this task serves\n\n"
    "Listing all the notes there are.\n\n"
    "## Acceptance Criteria\n\n"
    "- [ ] GET /notes applies rate limiting to each caller\n",
)


def _queue_the_notes_request(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text=REQUEST,
        triggered_by="jarvis",
        target_repo=TARGET_REPO,
    )


class _PlanWriter:
    """The stand-in plan writer for the one rewrite round: answers with the
    tree it was given, and remembers every note it was sent."""

    def __init__(self, rewrite: dict[str, str]) -> None:
        self.rewrite = rewrite
        self.notes: list[str] = []

    async def __call__(self, *, note: str, prior: Any) -> Any:
        self.notes.append(note)
        return SimpleNamespace(outcome="completed", role_output=dict(self.rewrite))


def _driver(store: SqlitePlanningRunStore) -> Any:
    _queue_the_notes_request(store)
    # The run is at the plan leg, where this review runs.
    for state in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
    ):
        assert store.transition(CID, state, "planning-driver") is None
    h = _make_driver(store)
    driver = h.driver

    async def no_facts(*_: Any, **__: Any) -> None:
        return None

    # The repository is not read here: there is no sheet of what it already
    # serves, so every address is judged against the request alone.
    driver._repository_facts_for = no_facts
    # The plan writer wired here takes the machine's note.
    driver._plan_dispatch_takes_a_note = lambda _dispatch: True
    return h


async def _review(h: Any, files: dict[str, str], writer: _PlanWriter) -> Any:
    store: SqlitePlanningRunStore = h.driver._deps.store
    return await h.driver._read_the_plan_against_the_request(
        store.get_run(CID),
        CID,
        files,
        feature_id=FEATURE,
        repo_path="/srv/repos/notes",
        test_roots=("tests",),
        ask_the_plan_writer=writer,
    )


@pytest.mark.asyncio
async def test_a_plan_that_paraphrases_the_request_gets_no_rewrite_and_no_card_line(
    store: SqlitePlanningRunStore,
) -> None:
    h = _driver(store)
    writer = _PlanWriter(PARAPHRASED)

    read = await _review(h, PARAPHRASED, writer)

    assert read is not None
    files, receipt = read
    assert files == PARAPHRASED
    assert receipt["checked"] is True
    assert receipt["flagged_tasks"] == []
    assert receipt["round"] == 0
    assert receipt["rewritten"] is False
    assert receipt["card_line"] is None
    assert "note" not in receipt
    # The plan writer was never asked to rewrite anything, and the run's one
    # note round is still unspent.
    assert writer.notes == []
    assert h.driver._plan_note_round_spent(CID) is False
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value


@pytest.mark.parametrize(
    ("plan", "said"),
    [
        (MOVED_ADDRESS, "TASK-NOTE-001 answers at /notes/v2, which the request does not name"),
        (RATE_LIMITED, "TASK-NOTE-001 asks for rate limiting, which the request never mentions"),
    ],
    ids=["notes-v2", "rate-limiting"],
)
@pytest.mark.asyncio
async def test_a_real_addition_is_still_sent_back_once_and_then_stops_the_run(
    store: SqlitePlanningRunStore, plan: dict[str, str], said: str
) -> None:
    h = _driver(store)
    # The rewrite keeps the addition, as a writer that will not let it go does.
    writer = _PlanWriter(plan)

    read = await _review(h, plan, writer)

    # Sent back once, with the finding word for word and no ask about wording.
    assert len(writer.notes) == 1
    note = writer.notes[0]
    assert said in note
    assert "words of the request" not in note
    assert "quote" not in note.lower()
    assert h.driver._plan_note_round_spent(CID) is True
    # Still there after the rewrite: the run stops before anything is written.
    assert read is None
    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    assert said in (run["error"] or "")
    card = [m for _, m, lvl in h.ctx["notifications"] if lvl == "error"]
    assert card and said in card[-1]
    assert REQUEST in card[-1]


@pytest.mark.asyncio
async def test_a_rewrite_that_drops_the_addition_carries_on_with_no_card_line(
    store: SqlitePlanningRunStore,
) -> None:
    h = _driver(store)
    writer = _PlanWriter(PARAPHRASED)

    read = await _review(h, MOVED_ADDRESS, writer)

    assert read is not None
    files, receipt = read
    assert files == PARAPHRASED
    assert receipt["round"] == 1
    assert receipt["rewritten"] is True
    assert receipt["fixed_tasks"] == ["TASK-NOTE-001"]
    assert receipt["still_flagged"] == []
    assert receipt["card_line"] is None
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value
