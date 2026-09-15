"""The machine's one note to the plan writer (2026-09-15, the planner fix).

Before this, forge could tell the SPEC writer that its draft added something
nobody asked for, and could tell the plan writer nothing at all — even though
the plan writer's own tool has accepted a note since it was written. Twelve
plans for one sentence proved that is where the drift enters: the plan moved
the web address three times and added something uninvited in ten of the twelve,
and every refusal in the whole experiment came from the plan.

So the plan is now read against the request at the moment the tree exists and
before anything is committed, and when it does not follow it, it goes back to
its writer ONCE with the finding word for word. What these tests prove, by
driving the real leg through a real scratch repository rather than by asserting
that something was called:

* a flagged plan opens exactly one note round, the note travels verbatim, and
  the rewritten plan is the one that gets committed;
* a plan that still contradicts the request after that one round stops the run
  before anything is written to the branch and before anything is built;
* a task that merely cannot quote the request NEVER stops a run — it is said in
  one plain line and the plan carries on;
* the round is spent at most once per run, and whether it has been spent is
  read from the durable rows, so a crash and a re-drive cannot spend it twice
  — which is what keeps the plan writer to three calls in a run, never four;
* a plan that follows the request opens no round at all and the wire is exactly
  what it was.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.planning.task_traceability import review_task_traceability
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _Harness,
    _init_scratch_repo,
    _make_driver,
    _queue,
)

#: The sentence every run in this file carries — the one ``_queue`` records.
REQUEST = "add a GET /stats endpoint"


@pytest.fixture()
def store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "note.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=True)


# ---------------------------------------------------------------------------
# Plan trees, written the way the plan writer writes them
# ---------------------------------------------------------------------------


def _tree(feature_id: str, tasks: dict[str, str]) -> dict[str, str]:
    """A plan tree: the feature file and one document per task."""
    listed = "".join(f"- id: {task_id}\n" for task_id in tasks)
    files = {
        f"features/stats-endpoint/{feature_id}.yaml": (
            f"id: {feature_id}\ntasks:\n{listed}"
        )
    }
    for task_id, body in tasks.items():
        files[f"tasks/backlog/stats-endpoint/{task_id}.md"] = (
            f"---\nid: {task_id}\nfeature_id: {feature_id}\n---\n\n{body}"
        )
    return files


def _moves_the_address(feature_id: str) -> dict[str, str]:
    """A plan that answers somewhere the request never named — the shape that
    got three of the twelve runs refused at the live gate an hour later."""
    return _tree(
        feature_id,
        {
            "TASK-STAT-001": (
                "# Create the statistics endpoint\n\n"
                "Create the endpoint.\n\n"
                "## Acceptance Criteria\n\n"
                "- [ ] GET /statistics/daily returns 200 OK\n"
            ),
        },
    )


def _quotes_nothing(feature_id: str) -> dict[str, str]:
    """A plan whose task cannot point at any of the words of the request. Nine
    of the fifty-eight tasks measured drew this and nothing else, and three or
    four of them were innocent — so it must never cost anybody a re-send."""
    return _tree(
        feature_id,
        {"TASK-STAT-001": "# Create the response schema\n\nDefine the models.\n"},
    )


def _follows_the_request(feature_id: str) -> dict[str, str]:
    return _tree(
        feature_id,
        {
            "TASK-STAT-001": (
                "# Add the endpoint\n\nAdd a GET /stats endpoint that answers.\n"
            )
        },
    )


def _result(feature_id: str, files: dict[str, str]) -> Any:
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output={"feature_id": feature_id, "files": files},
        reason=None,
    )


class _PlanWriter:
    """A plan writer that takes the machine's note, and remembers every call.

    It answers with the trees handed to it, in order, keeping the last one for
    any further call — so a test can say "flagged, then clean" or "flagged
    twice" and read back exactly what forge sent each time.
    """

    def __init__(self, *trees: Any) -> None:
        self.trees = list(trees)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        plan_run_id: str,
        correlation_id: str,
        feature_id: str,
        spec_feature: str,
        spec_summary: str,
        target_repo_descriptor: dict[str, Any],
        spec_assumptions: str | None = None,
        spec_feature_paths: list[str] | None = None,
        request_text: str | None = None,
        repository_facts: str | None = None,
        revision_of: dict[str, str] | None = None,
        validate_feedback: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "feature_id": feature_id,
                "request_text": request_text,
                "revision_of": revision_of,
                "validate_feedback": validate_feedback,
            }
        )
        make = self.trees[min(len(self.calls) - 1, len(self.trees) - 1)]
        files = make(feature_id)
        self.calls[-1]["files"] = files
        return _result(feature_id, files)


def _with_plan_writer(h: _Harness, writer: _PlanWriter) -> _PlanWriter:
    """Wire a plan writer that can carry a note into the driver under test."""
    h.driver._deps.dispatch_feature_plan = writer
    return writer


def _note_rows(store: SqlitePlanningRunStore) -> list[Any]:
    return [
        event
        for event in store.list_events(CID)
        if event["stage_label"] == "feature-plan" and event["status"] == "plan-note-sent"
    ]


def _plan_row(store: SqlitePlanningRunStore) -> dict[str, Any] | None:
    import json

    latest = None
    for event in store.list_events(CID):
        if event["stage_label"] == "feature-plan" and event["status"] == "approved":
            latest = json.loads(event["details_json"] or "{}")
    return latest


def _drive(store: SqlitePlanningRunStore, tmp_path: Path, writer: _PlanWriter) -> Any:
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    _with_plan_writer(h, writer)
    return h


# ---------------------------------------------------------------------------
# One round, the note word for word, the rewrite committed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_flagged_plan_opens_exactly_one_round_and_the_rewrite_is_committed(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    writer = _PlanWriter(_moves_the_address, _follows_the_request)
    h = _drive(store, tmp_path, writer)

    await h.driver.drive(CID)

    # Two calls to the plan writer: the first attempt and the one rewrite.
    assert len(writer.calls) == 2
    first, second = writer.calls

    # The first call is byte for byte the call that shipped before this: no
    # note, nothing to rewrite from.
    assert first["validate_feedback"] is None
    assert first["revision_of"] is None

    # The second carries the machine's note WORD FOR WORD — the guard's own
    # sentences, never summarised on the way — and the tree it rewrites from.
    expected = review_task_traceability(
        first["files"], request_text=REQUEST
    ).note()
    assert second["validate_feedback"] == expected
    assert "/statistics/daily" in second["validate_feedback"]
    assert second["revision_of"] == {
        str(rel).rsplit("/", 1)[-1]: content for rel, content in first["files"].items()
    }

    # The plan that got committed is the REWRITTEN one, read back off the
    # branch rather than believed from the receipt.
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value
    row = _plan_row(store)
    assert row is not None
    assert sorted(row["plan_files"]) == sorted(second["files"])
    task = "tasks/backlog/stats-endpoint/TASK-STAT-001.md"
    committed = await h.driver._deps.git_runner.read_file_from_branch(
        repo_path=str(tmp_path / "api_test"),
        branch=f"planning/{CID}",
        file_path=task,
    )
    assert committed == second["files"][task]
    assert "/statistics/daily" not in (committed or "")

    # Exactly one durable mark that the round was spent, carrying the note.
    rows = _note_rows(store)
    assert len(rows) == 1

    # And the receipt says what happened, in the leg's own row.
    receipt = row["plan_review"]
    assert receipt["checked"] is True
    assert receipt["round"] == 1
    assert receipt["rewritten"] is True
    assert receipt["flagged_tasks"] == ["TASK-STAT-001"]
    assert receipt["still_flagged"] == []
    assert receipt["fixed_tasks"] == ["TASK-STAT-001"]


@pytest.mark.asyncio
async def test_a_plan_that_follows_the_request_opens_no_round_at_all(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    writer = _PlanWriter(_follows_the_request)
    h = _drive(store, tmp_path, writer)

    await h.driver.drive(CID)

    assert len(writer.calls) == 1
    assert _note_rows(store) == []
    row = _plan_row(store)
    assert row is not None and row["plan_review"]["checked"] is True
    assert row["plan_review"]["round"] == 0
    assert row["plan_review"]["flagged_tasks"] == []
    # Nothing was said to anybody about the plan's traceability.
    assert not any(
        "quote any of the words" in message
        for _, message, _ in h.ctx["notifications"]
    )


# ---------------------------------------------------------------------------
# Still contradicting after the one round: the run stops here, not an hour later
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plan_that_still_moves_the_address_stops_the_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    writer = _PlanWriter(_moves_the_address)  # the rewrite says the same thing
    h = _drive(store, tmp_path, writer)

    await h.driver.drive(CID)

    assert len(writer.calls) == 2  # the attempt and its one rewrite, never three
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value

    # Nothing was committed and nothing was built.
    assert _plan_row(store) is None
    assert h.ctx["counters"]["build_trigger"] == 0

    # The card says why, in ordinary words, and quotes the request back.
    cards = [m for _, m, level in h.ctx["notifications"] if level == "error"]
    assert cards, "the stop must reach the person"
    card = cards[-1]
    assert "TASK-STAT-001 answers at /statistics/daily" in card
    assert "which the request does not name" in card
    assert "The machine already asked the plan writer once" in card
    assert REQUEST in card
    assert "nothing was written to the branch and nothing was built" in card


@pytest.mark.asyncio
async def test_a_plan_that_still_asks_for_what_nobody_asked_for_stops_the_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    def invents_a_login(feature_id: str) -> dict[str, str]:
        return _tree(
            feature_id,
            {
                "TASK-STAT-001": (
                    "# Add the endpoint\n\nAdd a GET /stats endpoint.\n\n"
                    "## Acceptance Criteria\n\n"
                    "- [ ] Endpoint requires authentication\n"
                )
            },
        )

    writer = _PlanWriter(invents_a_login)
    h = _drive(store, tmp_path, writer)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    card = [m for _, m, level in h.ctx["notifications"] if level == "error"][-1]
    assert "TASK-STAT-001 asks for authentication" in card
    assert "which the request never mentions" in card


# ---------------------------------------------------------------------------
# A task that cannot quote the request never stops a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_quote_the_request_is_said_once_and_never_stops_a_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    writer = _PlanWriter(_quotes_nothing)  # and the rewrite quotes nothing either
    h = _drive(store, tmp_path, writer)

    await h.driver.drive(CID)

    # It opened the one round — every finding gets the writer's one turn — and
    # then let the plan through.
    assert len(writer.calls) == 2
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value
    row = _plan_row(store)
    assert row is not None
    assert row["plan_review"]["still_flagged"] == ["TASK-STAT-001"]
    assert not any(level == "error" for _, _, level in h.ctx["notifications"])
    assert h.ctx["counters"]["build_trigger"] == 1

    # ONE plain line, in ordinary words, with no @mention and nothing to do.
    lines = [
        message
        for _, message, level in h.ctx["notifications"]
        if "quote any of the words" in message and level == "info"
    ]
    assert len(lines) == 1
    assert "TASK-STAT-001" in lines[0]
    # Plain English, and true: any finding at all opens the one note round, so
    # what did NOT happen is that the run was stopped.
    assert lines[0].startswith("One task in this plan does not quote")
    assert "The run was not stopped for that on its own." in lines[0]
    assert (lines[0], False) in h.ctx["mentions"]
    assert row["plan_review"]["card_line_sent"] == "sent"


# ---------------------------------------------------------------------------
# The round is spent once per run, and the durable row is what says so
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_re_drive_after_a_crash_does_not_spend_the_round_twice(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The round is marked spent before the rewrite is asked for, so a crash in
    the middle of it cannot buy the plan writer a second turn on a re-drive."""
    writer = _PlanWriter(_quotes_nothing)
    h = _drive(store, tmp_path, writer)
    # What the drive that crashed had already written down.
    h.driver._record_plan_note_round(
        CID, feature_id="FEAT-C5B0", note="the note it already sent", flagged=["T"]
    )

    await h.driver.drive(CID)

    assert len(writer.calls) == 1
    assert writer.calls[0]["validate_feedback"] is None
    assert len(_note_rows(store)) == 1  # still one — nothing new was written
    row = _plan_row(store)
    assert row is not None
    assert row["plan_review"]["note_round"] == "already spent on this run"
    # It could not quote the request, so the plan still carries on.
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value


@pytest.mark.asyncio
async def test_a_contradiction_with_the_round_already_spent_stops_the_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    writer = _PlanWriter(_moves_the_address)
    h = _drive(store, tmp_path, writer)
    h.driver._record_plan_note_round(
        CID, feature_id="FEAT-C5B0", note="the note it already sent", flagged=["T"]
    )

    await h.driver.drive(CID)

    # No second note: the plan writer is called once here, and the run stops.
    assert len(writer.calls) == 1
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    assert _plan_row(store) is None


# ---------------------------------------------------------------------------
# A plan dispatch that predates the note keeps working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plan_dispatch_that_cannot_carry_a_note_is_said_not_raised(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The two images are deployed separately and either may go first. An older
    plan dispatch takes no note: the finding is receipted, the plain line is
    still said, and the plan carries on exactly as it did before this existed.
    """
    calls: list[dict[str, Any]] = []

    async def older_dispatch(
        *,
        plan_run_id: str,
        correlation_id: str,
        feature_id: str,
        spec_feature: str,
        spec_summary: str,
        target_repo_descriptor: dict[str, Any],
        spec_assumptions: str | None = None,
        spec_feature_paths: list[str] | None = None,
        request_text: str | None = None,
        repository_facts: str | None = None,
    ) -> Any:
        calls.append({"feature_id": feature_id})
        return _result(feature_id, _moves_the_address(feature_id))

    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    git = WorktreeGitRunner(worktrees_root=tmp_path / "wt")
    _queue(store)
    h = _make_driver(store, git_runner=git, repo_path=str(repo))
    h.driver._deps.dispatch_feature_plan = older_dispatch

    await h.driver.drive(CID)

    assert len(calls) == 1
    assert store.get_run(CID)["state"] != PlanningState.FAILED.value
    row = _plan_row(store)
    assert row is not None
    assert row["plan_review"]["note_round"] == (
        "not sent: the plan dispatch wired here does not take the machine's note"
    )
    assert row["plan_review"]["still_flagged"] == ["TASK-STAT-001"]
    assert _note_rows(store) == []


# ---------------------------------------------------------------------------
# The wire itself: the two names the plan writer's own tool has always accepted
# ---------------------------------------------------------------------------


def test_the_plan_wire_carries_the_note_and_what_it_rewrites_from() -> None:
    from forge.cli._serve_planning import build_feature_plan_command_args

    args = build_feature_plan_command_args(
        feature_id="FEAT-AAAA",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "r", "test_roots": []},
        validate_feedback="the machine's note, word for word",
        revision_of={"TASK-STAT-001.md": "# task\n"},
    )

    assert args["validate_feedback"] == "the machine's note, word for word"
    assert args["revision_of"] == {"TASK-STAT-001.md": "# task\n"}


def test_a_first_round_plan_dispatch_sends_neither() -> None:
    from forge.cli._serve_planning import build_feature_plan_command_args

    args = build_feature_plan_command_args(
        feature_id="FEAT-AAAA",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "r", "test_roots": []},
        validate_feedback="   ",
        revision_of={},
    )

    assert set(args) == {
        "feature_id",
        "spec_feature",
        "spec_summary",
        "target_repo_descriptor",
    }


# ---------------------------------------------------------------------------
# The ceiling: three calls to the plan writer in one run, never four
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_plan_writer_is_called_three_times_at_most_in_one_run(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """Two note rounds exist in a run and they belong to different writers: the
    spec writer's, when the verifier stamps are refused, and this one, when the
    plan does not follow the request. Both can fire in the same run. What must
    never happen is a fourth call to the plan writer, and the durable mark is
    what stops it: the first attempt spends the plan review's one round, and
    the second attempt — the one the spec rewrite causes — reads the mark and
    sends no note, however the plan reads.

    The run here goes: attempt one writes a plan that moves the web address
    (call 1), the review sends it back once (call 2), the stamp normalizer then
    refuses two worked examples, the SPEC writer rewrites, and attempt two
    writes the plan again from the rewritten spec (call 3). Three calls.
    """
    from tests.forge.planning.test_driver_target_terminal import (
        _enforced_repo,
        _refusal_outcome,
        _rewritten_spec_result,
        _sequenced_normalizer,
        _share_order,
        _spec_by_round,
        _spec_result_native,
    )

    def _native(feature_id: str, task_body: str) -> dict[str, str]:
        """The deployed plan-tree shape: repository-relative paths."""
        return {
            f".guardkit/features/{feature_id}.yaml": (
                f"id: {feature_id}\ntasks:\n- id: TASK-STAT-001\n"
            ),
            "tasks/backlog/stats-endpoint/TASK-STAT-001.md": (
                f"---\nid: TASK-STAT-001\nfeature_id: {feature_id}\n---\n\n"
                f"{task_body}"
            ),
        }

    def moves_the_address(feature_id: str) -> dict[str, str]:
        return _native(
            feature_id,
            "# Create the statistics endpoint\n\n"
            "## Acceptance Criteria\n\n"
            "- [ ] GET /statistics/daily returns 200 OK\n",
        )

    def follows_the_request(feature_id: str) -> dict[str, str]:
        return _native(
            feature_id, "# Add the endpoint\n\nAdd a GET /stats endpoint.\n"
        )

    class _NativePlanWriter(_PlanWriter):
        async def __call__(self, **kwargs: Any) -> Any:  # type: ignore[override]
            self.calls.append(
                {
                    "validate_feedback": kwargs.get("validate_feedback"),
                    "revision_of": kwargs.get("revision_of"),
                }
            )
            make = self.trees[min(len(self.calls) - 1, len(self.trees) - 1)]
            files = make(kwargs["feature_id"])
            self.calls[-1]["files"] = files
            return SimpleNamespace(
                outcome=SimpleNamespace(value="completed"),
                role_output={"feature_id": kwargs["feature_id"], **files},
                reason=None,
            )

    repo, git = _enforced_repo(tmp_path)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=git,
        repo_path=str(repo),
        spec_result_factory=_spec_by_round(
            _spec_result_native(), _rewritten_spec_result()
        ),
        normalize_stamps_fn=_sequenced_normalizer(sink, [_refusal_outcome(), None]),
    )
    _share_order(sink, h)
    writer = _NativePlanWriter(moves_the_address, follows_the_request)
    _with_plan_writer(h, writer)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    # Three calls to the plan writer, and only ONE of them carried a note.
    assert len(writer.calls) == 3
    assert [bool(call["validate_feedback"]) for call in writer.calls] == [
        False,
        True,
        False,
    ]
    # The spec writer had its own round as well — two different notes, two
    # different writers, one run.
    assert h.ctx["counters"]["spec"] == 2
    assert len(_note_rows(store)) == 1
