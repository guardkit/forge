"""The approved part of a feature reaches one card and one merge (step 1).

The step 1 design of 24 October 2026, "Contract: the approved part of a
feature". GuardKit exits 2 and names the
last approved task's commit on its ``GUARDKIT_STOPPED_BECAUSE`` line; Forge
points ``autobuild/<FEAT>-approved`` at it and hands that branch through the
runner's completion, the offer and the merge press, never falling back to
``autobuild/<FEAT>``, which keeps the unapproved task's work.

Real paths throughout: a real child process stands in for GuardKit, a real git
repository holds task A's and task B's commits, and the real completion
translator, offer service and merge press are driven in turn. Only GuardKit's
merge verb and the deploy stage are the fakes the press's own tests use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from langchain_core.messages import HumanMessage

from forge.deploy.candidate_tree import InContainerCandidateGit
from forge.lifecycle_bridge.translation import StreamEventTranslator, _extract_state
from forge.pipeline.merge_executor import MergeApprovalConsumer
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
    approved_branch_of,
    card_line_about_tasks_not_included,
)
from forge.subagents import autobuild_runner as ar
from forge.subagents import build_monitor as bm
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 — fixtures
    BUILD_ID,
    FEATURE_ID,
    REPO,
    _deps,
    _drain,
    _ensure_build,
    _envelope,
    _FakeGuardKit,
    _git,
    _receipts_env,
    config,
    pool,
    repo_root,
)
from tests.forge.recorded_fetch_address import origin_of

APPROVED = f"autobuild/{FEATURE_ID}-approved"


def _task_b(repo: Path) -> tuple[str, str]:
    """Task A is the feature branch's tip; task B adds one more commit."""
    task_a = _git(repo, "rev-parse", f"autobuild/{FEATURE_ID}")
    _git(repo, "checkout", "-q", f"autobuild/{FEATURE_ID}")
    (repo / "task-b.txt").write_text("unapproved\n", encoding="utf-8")
    _git(repo, "add", "task-b.txt")
    _git(repo, "commit", "-q", "-m", "task B")
    _git(repo, "checkout", "-q", "main")
    return task_a, _git(repo, "rev-parse", f"autobuild/{FEATURE_ID}")


def _stop_line(**fields: Any) -> str:
    body = {"feature_id": FEATURE_ID, "sentence": "TASK-B failed: timeout"}
    body.update(fields)
    return "GUARDKIT_STOPPED_BECAUSE " + json.dumps(body)


def _fake_guardkit(tmp_path: Path, lines: list[str], exit_code: int) -> Path:
    script = tmp_path / "bin" / "guardkit"
    script.parent.mkdir(parents=True, exist_ok=True)
    body = ["#!/bin/sh"]
    for line in lines:
        quoted = line.replace("'", "'\"'\"'")
        body.append(f"printf '%s\\n' '{quoted}'")
    body.append(f"exit {exit_code}")
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _worktree_with_ledger(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    features = worktree / ".guardkit" / "features"
    features.mkdir(parents=True)
    (worktree / ".guardkit" / "autobuild-private").mkdir(parents=True)
    (features / f"{FEATURE_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {"id": "TASK-A", "status": "completed"},
                    {
                        "id": "TASK-B",
                        "status": "failed",
                        "result": {"final_decision": "timeout"},
                    },
                ],
                "execution": {"tasks_completed": 1, "tasks_failed": 1},
            }
        ),
        encoding="utf-8",
    )
    return worktree


async def _run(tmp_path: Path, repo: Path, guardkit: Path) -> dict[str, Any]:
    worktree = _worktree_with_ledger(tmp_path)
    payload = {
        "build_id": BUILD_ID,
        "feature_id": FEATURE_ID,
        "repo": REPO,
        "branch": "main",
        "correlation_id": f"corr-{BUILD_ID}",
    }
    state = {
        "messages": [
            HumanMessage(
                content="RUN_AUTOBUILD subagent=autobuild_runner "
                f"payload={json.dumps(payload, sort_keys=True)}"
            )
        ]
    }

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _yes(*_a: Any, **_k: Any) -> bool:
        return True

    async def _here(*_a: Any, **_k: Any) -> Path:
        return worktree

    with patch.object(ar, "_resolve_repo_path", lambda _p: repo), patch.object(
        ar, "_resolve_guardkit_path", lambda: guardkit
    ), patch.object(ar, "_local_branch_exists", _yes), patch.object(
        ar, "_sweep_build_refs", _nothing
    ), patch.object(ar, "_materialise_worktree", _here), patch.object(
        ar, "_remove_worktree", _nothing
    ):
        running = await ar._node_running_wave(state)  # type: ignore[arg-type]
    if running["async_tasks"][FEATURE_ID]["lifecycle"] == "failed":
        return running["async_tasks"][FEATURE_ID]
    done = ar._node_completed({**state, **running})  # type: ignore[arg-type]
    return done["async_tasks"][FEATURE_ID]


@pytest.fixture(autouse=True)
def _no_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bm.BUILD_MONITOR_ENABLED_ENV, "0")
    monkeypatch.delenv(ar.FORGE_AUTOBUILD_TIMEOUT_ENV, raising=False)


def _offer_service(config: Any, pool: Any) -> MergeOfferService:
    class _Wire:
        def __init__(self) -> None:
            self.cards: list[Any] = []

        async def raw_publish(self, _subject: str, body: bytes) -> None:
            self.cards.append(json.loads(body))

        async def publish_build_paused(self, _payload: Any) -> None:
            return None

    wire = _Wire()
    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=wire,
        raw_publish=wire.raw_publish,
        git_surface=lambda _repo, root: InContainerCandidateGit(
            root, fetch_address=origin_of(root), repo=REPO
        ),
        scope_pass=lambda **_kwargs: None,
        finished_feature_reader=lambda *_a, **_k: (
            None,
            None,
            "nothing was exported for this build",
        ),
    )
    service.wire = wire  # type: ignore[attr-defined]
    return service


def _the_offer(pool: Any) -> dict[str, Any]:
    offers = [
        row
        for row in pool.read_stages(BUILD_ID)
        if row.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ]
    assert len(offers) == 1
    return offers[0].details[MERGE_OFFER_DETAILS_KEY]


class TestTheRunnerFinishesOnTheApprovedBranch:
    @pytest.mark.asyncio
    async def test_exit_2_with_approved_head_makes_the_branch_and_completes(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        task_a, task_b = _task_b(repo_root)
        guardkit = _fake_guardkit(
            tmp_path, [_stop_line(approved_head=task_a)], exit_code=2
        )
        snap = await _run(tmp_path, repo_root, guardkit)

        assert snap["lifecycle"] == "completed"
        assert snap["merge_branch"] == APPROVED
        assert _git(repo_root, "rev-parse", APPROVED) == task_a
        # The unapproved work stays where it was.
        assert _git(repo_root, "rev-parse", f"autobuild/{FEATURE_ID}") == task_b
        assert snap["tasks_not_included"] == [
            {"id": "TASK-B", "status": "failed", "final_decision": "timeout"}
        ]
        assert (snap["tasks_completed"], snap["tasks_failed"]) == (1, 1)

        event = StreamEventTranslator()._build_complete(
            _extract_state({"async_tasks": {FEATURE_ID: snap}}, FEATURE_ID),
            f"corr-{BUILD_ID}",
        )
        assert event.branch == APPROVED
        assert approved_branch_of(event) == APPROVED

    @pytest.mark.asyncio
    async def test_exit_2_without_approved_head_fails_as_today(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        _task_b(repo_root)
        guardkit = _fake_guardkit(tmp_path, [_stop_line()], exit_code=2)
        snap = await _run(tmp_path, repo_root, guardkit)
        assert snap["lifecycle"] == "failed"
        assert "merge_branch" not in snap
        assert _git(repo_root, "branch", "--list", APPROVED) == ""

    @pytest.mark.asyncio
    async def test_exit_0_is_unchanged_whatever_the_line_says(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        task_a, _ = _task_b(repo_root)
        guardkit = _fake_guardkit(
            tmp_path, [_stop_line(approved_head=task_a)], exit_code=0
        )
        snap = await _run(tmp_path, repo_root, guardkit)
        assert snap["lifecycle"] == "completed"
        assert "merge_branch" not in snap
        assert _git(repo_root, "branch", "--list", APPROVED) == ""

    @pytest.mark.asyncio
    async def test_a_branch_that_cannot_be_made_ends_the_build_saying_why(
        self, tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _task_b(repo_root)
        monkeypatch.setattr(ar, "RETRY_PAUSE_SECONDS", 0.0)
        missing = "f" * 40
        guardkit = _fake_guardkit(
            tmp_path, [_stop_line(approved_head=missing)], exit_code=2
        )
        snap = await _run(tmp_path, repo_root, guardkit)
        assert snap["lifecycle"] == "failed"
        assert f"could not be put on {APPROVED}" in snap["error_message"]

    @pytest.mark.parametrize(
        "head", ["", "abc123", "g" * 40, 7, None, "A" * 40]
    )
    def test_only_a_full_commit_id_is_taken(self, head: Any) -> None:
        assert ar._parse_approved_head_line(_stop_line(approved_head=head), FEATURE_ID) is None

    def test_another_features_line_is_ignored(self) -> None:
        line = _stop_line(approved_head="a" * 40, feature_id="FEAT-OTHER")
        assert ar._parse_approved_head_line(line, FEATURE_ID) is None


class TestOneCardAndOneMergeHoldOnlyTheApprovedTasks:
    async def _offered(
        self, tmp_path: Path, repo_root: Path, config: Any, pool: Any
    ) -> tuple[str, str, MergeOfferService]:
        task_a, task_b = _task_b(repo_root)
        guardkit = _fake_guardkit(
            tmp_path, [_stop_line(approved_head=task_a)], exit_code=2
        )
        snap = await _run(tmp_path, repo_root, guardkit)
        event = StreamEventTranslator()._build_complete(
            _extract_state({"async_tasks": {FEATURE_ID: snap}}, FEATURE_ID),
            f"corr-{BUILD_ID}",
        )
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        service = _offer_service(config, pool)
        await service.maybe_offer(event)
        return task_a, task_b, service

    async def _press(self, config: Any, pool: Any, task_a: str) -> Any:
        gk = _FakeGuardKit(report={"status": "merged", "merged_sha": task_a})
        deps, publisher, gk, _dp = _deps(config, pool, guardkit=gk)
        consumer = MergeApprovalConsumer(deps)
        await consumer.handle_envelope(_envelope(correlation_id=f"corr-{BUILD_ID}"))
        await _drain(consumer)
        return gk, publisher

    @pytest.mark.asyncio
    async def test_card_diff_and_merge_command_all_select_task_a(
        self, tmp_path: Path, repo_root: Path, config: Any, pool: Any
    ) -> None:
        task_a, task_b, service = await self._offered(tmp_path, repo_root, config, pool)

        offer = _the_offer(pool)
        assert offer["branch"] == APPROVED
        assert offer["merge_branch"] == APPROVED
        assert offer["candidate_sha"] == task_a
        assert pool.get_build_row(BUILD_ID).merge_branch == APPROVED
        assert offer["tasks_not_included"] == [
            {"id": "TASK-B", "status": "failed", "final_decision": "timeout"}
        ]
        assert offer["change_basis"] == "merge-base"
        assert f"{FEATURE_ID}.txt" in offer["change_summary"]
        assert "task-b.txt" not in offer["change_summary"]
        assert "task-b.txt" not in offer["change_text"]
        assert offer["change_text_truncated"] is False
        card = service.wire.cards[0]["payload"]["action_description"]  # type: ignore[attr-defined]
        assert (
            "Not included: TASK-B (failed: timeout). Approving merges only the "
            "approved tasks." in card
        )

        gk, publisher = await self._press(config, pool, task_a)
        args = gk.calls[0]["args"]
        assert args[-2:] == ["--branch", APPROVED]
        assert publisher.reports[0].branch == APPROVED

    @pytest.mark.asyncio
    async def test_a_failed_merge_branch_record_still_selects_task_a(
        self, tmp_path: Path, repo_root: Path, config: Any, pool: Any
    ) -> None:
        def _cannot(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("database is locked")

        with patch.object(type(pool), "record_merge_branch", _cannot):
            task_a, _task_b_sha, _service = await self._offered(
                tmp_path, repo_root, config, pool
            )
        assert pool.get_build_row(BUILD_ID).merge_branch is None
        offer = _the_offer(pool)
        assert offer["merge_branch"] == APPROVED
        assert offer["candidate_sha"] == task_a

        gk, publisher = await self._press(config, pool, task_a)
        assert gk.calls[0]["args"][-2:] == ["--branch", APPROVED]
        assert publisher.reports[0].branch == APPROVED


class TestTheNotIncludedLine:
    def test_unreadable_list(self) -> None:
        assert card_line_about_tasks_not_included("task list not available") == (
            "Not included: task list not available. Approving merges only the "
            "approved tasks."
        )

    def test_status_without_decision(self) -> None:
        assert card_line_about_tasks_not_included(
            [{"id": "TASK-B", "status": "failed", "final_decision": None}]
        ).startswith("Not included: TASK-B (failed).")


class TestARetainedWorktreeMismatchIsAdvice:
    """FRG-145: the candidate comes from the branch; a retained worktree that
    does not match is a line on the card and is never used for cleanup."""

    def _event(self, retained: dict[str, Any]) -> Any:
        from nats_core.events import BuildCompletePayload

        event = BuildCompletePayload(
            feature_id=FEATURE_ID,
            build_id=BUILD_ID,
            tasks_completed=1,
            tasks_failed=0,
            tasks_total=1,
            duration_seconds=1,
            summary="done",
        )
        object.__setattr__(event, "worktree_retention", retained)
        return event

    @pytest.mark.asyncio
    async def test_an_identity_naming_another_build(
        self, config: Any, pool: Any
    ) -> None:
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        service = _offer_service(config, pool)
        await service.maybe_offer(
            self._event({"ok": True, "build_id": "another", "path": "/x/another"})
        )
        offer = _the_offer(pool)
        assert "worktree_retention" not in offer
        assert "runner_worktree_retention" not in offer
        assert pool.get_build_row(BUILD_ID).worktree_path is None
        card = service.wire.cards[0]["payload"]["action_description"]  # type: ignore[attr-defined]
        assert "did not match it, so it will be left alone" in card

    @pytest.mark.asyncio
    async def test_a_retained_worktree_with_no_recorded_path(
        self, config: Any, pool: Any
    ) -> None:
        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        service = _offer_service(config, pool)
        await service.offer(
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            card_words=lambda branch, _m: f"{FEATURE_ID} on {branch}.",
            extra_details={"runner_worktree_retention": {"ok": True}},
        )
        offer = _the_offer(pool)
        assert "worktree_retention" not in offer
        card = service.wire.cards[0]["payload"]["action_description"]  # type: ignore[attr-defined]
        assert "did not match it, so it will be left alone" in card


class TestTheCardsChangeEvidence:
    """Finding 7 of the code review (25 October 2026): the summary is git's
    diff stat, and an answer with no diff text is never an empty change."""

    @pytest.mark.asyncio
    async def test_the_summary_is_the_diff_stat(
        self, tmp_path: Path, repo_root: Path, config: Any, pool: Any
    ) -> None:
        await TestOneCardAndOneMergeHoldOnlyTheApprovedTasks()._offered(
            tmp_path, repo_root, config, pool
        )
        summary = _the_offer(pool)["change_summary"]
        assert f"{FEATURE_ID}.txt" in summary
        assert "1 file changed" in summary

    def test_an_older_helper_without_diff_text_reads_as_not_read(self) -> None:
        from forge.pipeline import branch_scope
        from forge.pipeline.merge_offer import CHANGE_NOT_READ, read_the_change

        older = {"name_status": "M\0README.md\0", "added_by_file": {}, "head": "a" * 40}
        reading = branch_scope.reading_from_answer(older)
        assert reading.patch_read is False

        class _Sandbox:
            name = "older"
            sidecar_url = "http://sandbox.invalid"

        with patch("forge.config.sandboxes.sandbox_for", lambda *_a: _Sandbox()), patch.object(
            branch_scope, "read_branch_scope_in_sandbox", lambda **_k: reading
        ):
            details = read_the_change(None, REPO, "a" * 40, "b" * 40, FEATURE_ID)
        assert details["change_summary"] == CHANGE_NOT_READ
        assert details["change_text"] == ""

    def test_an_empty_diff_says_no_files_changed(self, repo_root: Path, config: Any) -> None:
        from forge.pipeline.merge_offer import read_the_change

        main = _git(repo_root, "rev-parse", "main")
        details = read_the_change(config, REPO, main, main, FEATURE_ID)
        assert details["change_summary"] == "no files changed"
        assert details["change_text"] == ""


class TestARetainedWorktreeThatCannotBeRecorded:
    """Finding 5 (25 October 2026): one retry, then the card is offered and
    the worktree is left alone."""

    @pytest.mark.asyncio
    async def test_the_card_is_offered_and_the_worktree_left_alone(
        self, config: Any, pool: Any
    ) -> None:
        from nats_core.events import BuildCompletePayload

        _ensure_build(pool, build_id=BUILD_ID, feature_id=FEATURE_ID)
        writes: list[str] = []

        def _cannot(_self: Any, build_id: str, _path: str) -> None:
            writes.append(build_id)
            raise RuntimeError("disk is full")

        event = BuildCompletePayload(
            feature_id=FEATURE_ID,
            build_id=BUILD_ID,
            tasks_completed=1,
            tasks_failed=0,
            tasks_total=1,
            duration_seconds=1,
            summary="done",
        )
        object.__setattr__(
            event,
            "worktree_retention",
            {"ok": True, "build_id": BUILD_ID, "path": f"/x/{BUILD_ID}"},
        )
        service = _offer_service(config, pool)
        with patch.object(type(pool), "record_worktree_path", _cannot):
            await service.maybe_offer(event)
        assert writes == [BUILD_ID, BUILD_ID]
        offer = _the_offer(pool)
        assert "worktree_retention" not in offer
        assert "runner_worktree_retention" not in offer
        card = service.wire.cards[0]["payload"]["action_description"]  # type: ignore[attr-defined]
        assert "could not be recorded, so it will be left alone" in card
