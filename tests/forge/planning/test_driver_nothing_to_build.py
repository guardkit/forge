"""Driver-level acceptance: "already done, nothing to build" (7 October 2026).

The design of record is ai-transition
``docs/designs/already-done-outcome-2026-10-07.md``, Part 2 sections 4 and 7
(Rich's simple option; Codex round 1, R1). The whole chain is driven with
``drive()`` from a queued sentence, on a real SQLite store, with fakes only
at the wire seams, and with spies on the three legs that must never run after
this outcome: the pass bars, the feature gate and the build trigger.

The headline case runs against a neutral scratch repository, so the code
windows the plan writer is checked against are the ones Forge's own
description builder really made; the plan writer's stand-in cites lines
inside them, as the real one is told to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import NothingToBuildConfig
from forge.planning import nothing_to_build
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.adapters.git.planning_runner import WorktreeGitRunner
from tests.forge.planning.test_driver_target_terminal import (
    CID,
    _init_scratch_repo,
    _make_driver,
    _plan_result_native,
    _queue,
    _semantic_review_json,
    _spec_result_native,
    _stamping_normalizer,
    store,  # noqa: F401 — the fixture, re-exported for these tests
)

GUIDE = "tasks/backlog/stats-endpoint/IMPLEMENTATION-GUIDE.md"
SCENARIO = "ok"  # the one approved example in the harness's fixture spec


def _scratch_repo(tmp_path: Path) -> Path:
    """A neutral repository that already serves ``GET /stats``, with a test."""
    import subprocess

    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "stats.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/stats")\n'
        "def stats() -> dict[str, int]:\n"
        '    """The statistics endpoint."""\n'
        '    return {"users": 3}\n',
        encoding="utf-8",
    )
    (repo / "tests" / "stats").mkdir(parents=True)
    (repo / "tests" / "stats" / "test_stats.py").write_text(
        "def test_stats_endpoint_answers(client) -> None:\n"
        '    response = client.get("/stats")\n'
        "    assert response.status_code == 200\n"
        '    assert response.json() == {"users": 3}\n',
        encoding="utf-8",
    )
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "stats"], cwd=repo, check=True, env=env)
    return repo


def _citation(window: nothing_to_build.Window) -> str:
    return f"{window.path}:{window.first_line}-{window.last_line}"


def _nothing_reply(
    parts: list[dict[str, Any]], scenarios: list[dict[str, Any]]
) -> Any:
    """The plan writer's "nothing to build" reply: the plan document only,
    its validation channel with the proof, and the accepting receipt bound to
    exactly that plan document."""
    from types import SimpleNamespace

    files = {
        GUIDE: (
            "# Stats endpoint\n\n## Already done and still to do\n"
            '- "add a GET /stats endpoint" — already done\n'
        )
    }
    role_output: dict[str, Any] = dict(files)
    role_output["validation.json"] = json.dumps(
        {
            "accepted": True,
            "outcome": "nothing_to_build",
            "parts": parts,
            "scenarios": scenarios,
            "gates_run": ["nothing_to_build"],
        }
    )
    role_output["semantic_review.json"] = _semantic_review_json(files)
    return SimpleNamespace(
        outcome=SimpleNamespace(value="completed"),
        role_output=role_output,
        reason=None,
    )


class _Recorder:
    """Everything the run said and did after the plan writer answered."""

    def __init__(self) -> None:
        self.plan_calls: list[dict[str, Any]] = []
        self.notifications_before_plan: int | None = None
        self.cards_before_plan: int | None = None
        self.complete: list[dict[str, Any]] = []
        self.failed: list[tuple[str, str]] = []
        self.legs: list[str] = []


def _wire(
    h: Any,
    rec: _Recorder,
    reply_for: Any,
) -> None:
    """Replace the plan dispatch, the two terminal projections and the three
    legs after the plan with recorders."""
    deps = h.driver._deps

    async def dispatch_plan(**kwargs: Any) -> Any:
        rec.plan_calls.append(kwargs)
        if rec.notifications_before_plan is None:
            rec.notifications_before_plan = len(h.ctx["notifications"])
            rec.cards_before_plan = len(h.ctx["publisher"].envelopes)
        return reply_for(kwargs["target_repo_descriptor"])

    async def complete(correlation_id: str, *, feature_id: str, extra: Any) -> None:
        rec.complete.append(
            {"correlation_id": correlation_id, "feature_id": feature_id, **extra}
        )

    async def failed(correlation_id: str, reason: str) -> None:
        rec.failed.append((correlation_id, reason))

    deps.dispatch_feature_plan = dispatch_plan
    deps.publish_planning_complete = complete
    deps.publish_planning_failed = failed

    def spy(name: str) -> Any:
        async def leg(row: Any, correlation_id: str) -> bool:
            rec.legs.append(name)
            return True

        return leg

    for name in (
        "_register_pass_bars_leg",
        "_register_feature_gate_leg",
        "_build_trigger_leg",
    ):
        setattr(h.driver, name, spy(name))


def _switch_on(h: Any) -> None:
    h.driver._deps.planning_config.nothing_to_build = NothingToBuildConfig(enabled=True)


def _messages_after_plan(h: Any, rec: _Recorder) -> list[tuple[str, str, str]]:
    assert rec.notifications_before_plan is not None, "the plan writer was never asked"
    return h.ctx["notifications"][rec.notifications_before_plan :]


def _proof_from_windows(descriptor: Any) -> tuple[list[dict], list[dict]]:
    windows = nothing_to_build.windows_sent(descriptor)
    code = [w for w in windows if w.path.startswith("src/")]
    tests = [w for w in windows if w.path.startswith("tests/")]
    assert code, f"Forge sent no code window to cite: {windows}"
    assert tests, f"Forge sent no test window to cite: {windows}"
    return (
        [{"quote": "add a GET /stats endpoint", "citations": [_citation(code[0])]}],
        [{"title": SCENARIO, "citations": [_citation(tests[0])]}],
    )


# ---------------------------------------------------------------------------
# The headline: the run ends, once, and nothing after the plan runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_to_build_ends_the_run_once_and_runs_no_later_leg(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = _scratch_repo(tmp_path)
    _queue(store)
    h = _make_driver(store, repo_path=str(repo))
    _switch_on(h)
    rec = _Recorder()
    _wire(h, rec, lambda descriptor: _nothing_reply(*_proof_from_windows(descriptor)))

    await h.driver.drive(CID)

    # Forge asked for this answer, because its switch is on.
    assert rec.plan_calls and all(
        call.get(nothing_to_build.REQUEST_FIELD) is True for call in rec.plan_calls
    )
    # None of the three legs after the plan ran; no build was triggered.
    assert rec.legs == []
    assert h.ctx["counters"]["build_trigger"] == 0
    # No card opened after the plan writer answered.
    assert len(h.ctx["publisher"].envelopes) == rec.cards_before_plan
    # The run ended in the existing success state, with the marker.
    run = store.get_run(CID)
    assert run["state"] == PlanningState.PLANNED_HANDOFF.value
    assert run["handoff_branch"] is None and run["handoff_path"] is None
    events = store.list_events(CID)
    labels = [e["stage_label"] for e in events]
    assert "feature-plan" not in labels  # never a feature-plan event
    terminal = [e for e in events if e["status"] == PlanningState.PLANNED_HANDOFF.value]
    assert len(terminal) == 1
    assert terminal[0]["stage_label"] == nothing_to_build.STAGE_LABEL
    details = json.loads(terminal[0]["details_json"])
    assert details["outcome"] == "nothing_to_build"
    assert len(details["proof"]) == 2
    assert details["proof"][0].startswith('"add a GET /stats endpoint" is already done: src/stats.py:')
    assert details["proof"][1].startswith(f'"{SCENARIO}" is checked by an existing test: tests/stats/test_stats.py:')
    # Exactly one owner message, and it is the nothing-to-build sentence with
    # every proof line in it.
    after = _messages_after_plan(h, rec)
    assert len(after) == 1
    _, message, level = after[0]
    assert level == "info"
    assert message.startswith(
        'Already done, nothing to build. "add a GET /stats endpoint" is already in api_test:'
    )
    for line in details["proof"]:
        assert f"- {line}" in message
    assert "Nothing was built or merged." in message
    assert details["owner_message"] == message
    # Exactly one planning-complete, carrying the marker and the proof, and no
    # planning-failed.
    assert rec.failed == []
    assert len(rec.complete) == 1
    event = rec.complete[0]
    assert event["outcome"] == "nothing_to_build"
    assert event["proof"] == details["proof"]
    assert event["summary"] == message
    assert event["feature_id"].startswith("FEAT-")

    # A second drive of the same run does nothing at all.
    plan_calls = len(rec.plan_calls)
    messages = len(h.ctx["notifications"])
    await h.driver.drive(CID)
    assert len(rec.plan_calls) == plan_calls
    assert len(h.ctx["notifications"]) == messages
    assert len(rec.complete) == 1 and rec.failed == []
    assert rec.legs == []
    assert store.get_run(CID)["state"] == PlanningState.PLANNED_HANDOFF.value


# ---------------------------------------------------------------------------
# Variant 1: the transition is refused (another writer ended the run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_transition_sends_nothing(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = _scratch_repo(tmp_path)
    _queue(store)
    h = _make_driver(store, repo_path=str(repo))
    _switch_on(h)
    rec = _Recorder()
    _wire(h, rec, lambda descriptor: _nothing_reply(*_proof_from_windows(descriptor)))

    real_transition = store.transition

    def racing_transition(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("to_state") is PlanningState.PLANNED_HANDOFF:
            # Another writer ends the run first, silently.
            assert (
                real_transition(
                    correlation_id=CID,
                    to_state=PlanningState.TIMED_OUT,
                    actor_identity="someone-else",
                )
                is None
            )
        return real_transition(*args, **kwargs)

    store.transition = racing_transition  # type: ignore[method-assign]

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.TIMED_OUT.value
    assert _messages_after_plan(h, rec) == []
    assert rec.complete == [] and rec.failed == []
    assert rec.legs == []


# ---------------------------------------------------------------------------
# Variant 2: a citation outside the windows Forge sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_citation_outside_the_windows_sent_fails_once_and_runs_no_leg(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = _scratch_repo(tmp_path)
    _queue(store)
    h = _make_driver(store, repo_path=str(repo))
    _switch_on(h)
    rec = _Recorder()

    def reply(descriptor: Any) -> Any:
        parts, scenarios = _proof_from_windows(descriptor)
        parts[0]["citations"].append("src/stats.py:900")
        return _nothing_reply(parts, scenarios)

    _wire(h, rec, reply)

    await h.driver.drive(CID)

    run = store.get_run(CID)
    assert run["state"] == PlanningState.FAILED.value
    after = _messages_after_plan(h, rec)
    assert len(after) == 1
    _, message, level = after[0]
    assert level == "error"
    assert "src/stats.py:900" in message
    assert "outside the code it was shown" in message
    assert message.endswith("Nothing was built.")
    assert rec.complete == []
    assert len(rec.failed) == 1
    assert rec.legs == []
    assert h.ctx["counters"]["build_trigger"] == 0
    assert not any(
        e["stage_label"] == "feature-plan" and e["status"] == "approved"
        for e in store.list_events(CID)
    )


# ---------------------------------------------------------------------------
# Variant 3: the re-drive shortcut never mistakes an ended run for a plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_redrive_shortcut_rechecks_the_row_before_the_pass_bars(
    store: SqlitePlanningRunStore,
) -> None:
    """A ``feature-plan`` event exists, so the plan leg's shortcut returns
    True; but the row ended while the leg ran. ``drive()`` reads the row
    again and returns before the pass-bar leg."""
    _queue(store)
    for state in (
        PlanningState.RUNNING,
        PlanningState.FEATURE_SPEC,
        PlanningState.FEATURE_PLAN,
    ):
        assert store.transition(CID, state, "planning-driver") is None
    store._record_event(
        correlation_id=CID,
        stage_label="feature-plan",
        status="approved",
        actor_identity="planning-driver",
        details_json=json.dumps({"feature_id": "FEAT-0000"}),
    )
    h = _make_driver(store)
    rec = _Recorder()
    _wire(h, rec, lambda descriptor: None)
    real_leg = h.driver._feature_plan_leg

    async def leg_while_another_writer_ends_the_run(row: Any, cid: str) -> bool:
        assert (
            store.transition(
                correlation_id=cid,
                to_state=PlanningState.PLANNED_HANDOFF,
                actor_identity="someone-else",
                stage_label=nothing_to_build.STAGE_LABEL,
            )
            is None
        )
        return await real_leg(row, cid)

    h.driver._feature_plan_leg = leg_while_another_writer_ends_the_run

    await h.driver.drive(CID)

    assert rec.legs == []
    assert rec.plan_calls == []
    assert store.get_run(CID)["state"] == PlanningState.PLANNED_HANDOFF.value


# ---------------------------------------------------------------------------
# The switch: off is today's call, and the answer is refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_sends_todays_call_and_refuses_the_answer(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = _scratch_repo(tmp_path)
    _queue(store)
    h = _make_driver(store, repo_path=str(repo))
    rec = _Recorder()
    _wire(h, rec, lambda descriptor: _nothing_reply(*_proof_from_windows(descriptor)))

    await h.driver.drive(CID)

    assert rec.plan_calls
    assert all(nothing_to_build.REQUEST_FIELD not in call for call in rec.plan_calls)
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    after = _messages_after_plan(h, rec)
    assert len(after) == 1
    assert "does not take that answer yet" in after[0][1]
    assert rec.complete == [] and len(rec.failed) == 1
    assert rec.legs == []


@pytest.mark.asyncio
async def test_switch_on_an_ordinary_plan_goes_on_as_today(
    store: SqlitePlanningRunStore,
) -> None:
    """A reply without the marker is today's plan, switch on or off: it is
    committed and the build is queued."""
    _queue(store)
    h = _make_driver(store)
    _switch_on(h)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.BUILD_QUEUED.value
    assert h.ctx["counters"]["build_trigger"] == 1
    assert h.ctx["counters"]["last_already_done_allowed"] is True


@pytest.mark.asyncio
async def test_the_flag_rides_on_the_exact_re_review_call_too(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    """The plan writer's exact re-review (``semantic_review_artifacts``)
    refuses a plan-document-only tree without the flag, so with the switch on
    every call of the run carries it: the plan call and the re-review."""
    repo = tmp_path / "api_test"
    _init_scratch_repo(repo)
    _queue(store)
    sink: dict[str, Any] = {}
    h = _make_driver(
        store,
        git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "wt"),
        repo_path=str(repo),
        spec_result=_spec_result_native(),
        plan_result_factory=_plan_result_native,
        normalize_stamps_fn=_stamping_normalizer(sink, write=True),
    )
    _switch_on(h)

    await h.driver.drive(CID)

    assert h.ctx["counters"]["semantic_rereview"] == 1
    assert h.ctx["counters"]["already_done_allowed_by_call"] == [
        ("plan", True),
        ("re-review", True),
    ]


@pytest.mark.asyncio
async def test_a_nothing_to_build_reply_that_also_plans_work_is_refused(
    store: SqlitePlanningRunStore, tmp_path: Path
) -> None:
    repo = _scratch_repo(tmp_path)
    _queue(store)
    h = _make_driver(store, repo_path=str(repo))
    _switch_on(h)
    rec = _Recorder()

    def reply(descriptor: Any) -> Any:
        result = _nothing_reply(*_proof_from_windows(descriptor))
        files = {
            GUIDE: result.role_output[GUIDE],
            ".guardkit/features/FEAT-0000.yaml": "id: FEAT-0000\n",
        }
        result.role_output[".guardkit/features/FEAT-0000.yaml"] = files[
            ".guardkit/features/FEAT-0000.yaml"
        ]
        result.role_output["semantic_review.json"] = _semantic_review_json(files)
        return result

    _wire(h, rec, reply)

    await h.driver.drive(CID)

    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    after = _messages_after_plan(h, rec)
    assert len(after) == 1
    assert "also sent a plan to build" in after[0][1]
    assert rec.complete == [] and rec.legs == []


def test_the_stage_label_is_never_the_plan_legs() -> None:
    from forge.planning import driver as driver_module

    assert nothing_to_build.STAGE_LABEL != driver_module._FEATURE_PLAN_STAGE
    assert PlanningState.PLANNED_HANDOFF in driver_module._TERMINAL_STATES


def test_the_switch_is_off_by_default() -> None:
    from forge.config.models import PlanningConfig

    assert PlanningConfig().nothing_to_build.enabled is False
