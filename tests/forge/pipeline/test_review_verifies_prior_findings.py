"""The follow-up review verifies instead of repeating itself.

Attempt eight, 2026-09-08. Five work legs ran inside the sandbox and every
one was approved, with commits that fixed exactly the three things the
first review named. The follow-up review then reported those same three
findings again, word for word, off a tree that no longer had them: it had
re-derived them from the task's description instead of reading the code.

The cure proven here: a review leg that follows at least one work leg in
the same journey is handed one more context document carrying the prior
review's findings, the commits made since, and the instruction to check
each finding against the code that is there now.

What each test pins:

* a second review gets the document, with the right findings and the right
  commits, and its path rides the dispatch as one more ``--context`` value;
* a first review gets nothing at all, and its dispatch is exactly what it
  was before;
* a repository whose legs run inside its sandbox carries the document's
  path in the request the sandbox door posts;
* receipts that are missing, empty or malformed produce no document and
  one plain line in the log, never a dead journey.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.dispatchers.conductor_subprocess import (
    make_conductor_subprocess_dispatcher,
    with_review_verification,
)
from forge.pipeline.dispatchers.subprocess import dispatch_subprocess_stage
from forge.pipeline.fix_task_context_builder import (
    VERIFY_DOCUMENT_NAME,
    VERIFY_INSTRUCTION,
    build_review_verification_context,
    read_prior_review_evidence,
)
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-VER1-20260908110853"
TASK_ID = "TASK-VER1FIX1"


# ---------------------------------------------------------------------------
# A journey's receipts, written the way the fix journey writes them
# ---------------------------------------------------------------------------


def _stage_dir(receipts_root: Path, key: str, task: str) -> Path:
    path = receipts_root / BUILD_ID / "stages" / key / ".guardkit" / "autobuild" / task
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_review_stage(receipts_root: Path, key: str = "001-task-review") -> None:
    """One review stage export, with the findings a review reported."""
    findings = {
        "clean": False,
        "findings": [
            {
                "id": "F1",
                "severity": "critical",
                "title": "Timezone mismatch on the deleted_at column",
                "file": "alembic/versions/39f6_add_deleted_at.py",
                "line": 35,
                "detail": "The migration creates a naive column; the code writes an aware value.",
            },
            {
                "id": "F2",
                "severity": "medium",
                "title": "delete_user has no database error handling",
                "file": "src/users/router.py",
                "line": 584,
                "detail": "A database error leaves as a 500 instead of the documented 503.",
            },
        ],
    }
    (_stage_dir(receipts_root, key, TASK_ID) / "review_findings.json").write_text(
        json.dumps(findings), encoding="utf-8"
    )


def _write_work_stage(
    receipts_root: Path,
    key: str,
    *,
    fix_task: str,
    subject: str,
    sha: str,
    files: list[str],
    committed: bool = True,
) -> None:
    """One work stage export, carrying the commit that leg made."""
    leg_dir = _stage_dir(receipts_root, key, fix_task)
    (leg_dir / "task_work_leg_results.json").write_text(
        json.dumps(
            {
                "leg": "task-work",
                "task_id": fix_task,
                "status": "approved",
                "final_decision": "approved",
                "commit": {
                    "attempted": True,
                    "committed": committed,
                    "head_before": "0" * 40,
                    "message": subject,
                    "head_after": sha,
                },
            }
        ),
        encoding="utf-8",
    )
    (leg_dir / "task_work_results.json").write_text(
        json.dumps({"task_id": fix_task, "files_modified": files, "files_created": []}),
        encoding="utf-8",
    )


def _journey_with_one_cycle(receipts_root: Path) -> None:
    """A review, then two work legs — the shape a follow-up review follows."""
    _write_review_stage(receipts_root)
    _write_work_stage(
        receipts_root,
        "002-task-work",
        fix_task="TASK-VER1-001",
        subject="fix(TASK-VER1-001): make the deleted_at column timezone-aware",
        sha="a" * 40,
        files=[
            "alembic/versions/39f6_add_deleted_at.py",
            ".guardkit/memory-query-log.jsonl",
        ],
    )
    # The receipts fold re-copies every earlier leg into every later stage,
    # so the second export carries the first leg's files too. One commit,
    # one row: the document must not say the same fix twice.
    _write_work_stage(
        receipts_root,
        "003-task-work",
        fix_task="TASK-VER1-001",
        subject="fix(TASK-VER1-001): make the deleted_at column timezone-aware",
        sha="a" * 40,
        files=["alembic/versions/39f6_add_deleted_at.py"],
    )
    _write_work_stage(
        receipts_root,
        "003-task-work",
        fix_task="TASK-VER1-002",
        subject="fix(TASK-VER1-002): answer a database error on delete_user with 503",
        sha="b" * 40,
        files=["src/users/router.py"],
    )


# ---------------------------------------------------------------------------
# The document itself
# ---------------------------------------------------------------------------


class TestTheDocument:
    def test_a_second_review_gets_the_findings_and_the_commits(
        self, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)

        entry = build_review_verification_context(
            build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
        )

        assert entry is not None
        assert entry["flag"] == "--context"
        assert entry["kind"] == "path"
        document = Path(entry["value"])
        assert document == (
            worktree / ".guardkit" / "autobuild" / TASK_ID / VERIFY_DOCUMENT_NAME
        )
        text = document.read_text(encoding="utf-8")

        # The prior review's findings, whole: id, severity, title, file,
        # line and detail.
        assert "F1" in text and "F2" in text
        assert "critical" in text and "medium" in text
        assert "Timezone mismatch on the deleted_at column" in text
        assert "alembic/versions/39f6_add_deleted_at.py" in text
        assert "584" in text
        assert "A database error leaves as a 500" in text

        # The commits made since, one row each, with the files they touched.
        assert text.count("make the deleted_at column timezone-aware") == 1
        assert "answer a database error on delete_user with 503" in text
        assert "src/users/router.py" in text
        assert "Commits made since that review (2)" in text

        # A leg's own receipts are not code and are not listed.
        assert ".guardkit/memory-query-log.jsonl" not in text

        # And the instruction, word for word.
        assert VERIFY_INSTRUCTION in text

        # It says where its facts came from, so nobody has to guess.
        assert "receipts" in text
        assert "001-task-review" in text

    def test_a_first_review_gets_nothing(self, tmp_path: Path) -> None:
        """No stages exported yet — this IS the journey's opening review."""
        receipts = tmp_path / "receipts"
        receipts.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        assert (
            build_review_verification_context(
                build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
            )
            is None
        )
        assert not (worktree / ".guardkit").exists()

    def test_a_review_with_no_work_since_it_gets_nothing(self, tmp_path: Path) -> None:
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _write_review_stage(receipts)

        assert (
            build_review_verification_context(
                build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
            )
            is None
        )

    def test_a_clean_previous_review_gets_nothing(self, tmp_path: Path) -> None:
        """Nothing was found, so there is nothing to verify."""
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (
            _stage_dir(receipts, "001-task-review", TASK_ID) / "review_findings.json"
        ).write_text(json.dumps({"clean": True, "findings": []}), encoding="utf-8")
        _write_work_stage(
            receipts,
            "002-task-work",
            fix_task="TASK-VER1-001",
            subject="fix(TASK-VER1-001): something",
            sha="c" * 40,
            files=["src/a.py"],
        )

        assert (
            build_review_verification_context(
                build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
            )
            is None
        )

    def test_the_latest_review_is_the_one_verified(self, tmp_path: Path) -> None:
        """A three-cycle journey checks the review it actually followed."""
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)
        (
            _stage_dir(receipts, "004-task-review", TASK_ID) / "review_findings.json"
        ).write_text(
            json.dumps(
                {
                    "clean": False,
                    "findings": [
                        {
                            "id": "G1",
                            "severity": "high",
                            "title": "The second cycle's own finding",
                            "file": "src/users/crud.py",
                            "line": 160,
                            "detail": "Still writing a naive value here.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _write_work_stage(
            receipts,
            "005-task-work",
            fix_task="TASK-VER1-003",
            subject="fix(TASK-VER1-003): write an aware value in crud",
            sha="d" * 40,
            files=["src/users/crud.py"],
        )

        evidence = read_prior_review_evidence(BUILD_ID, receipts_root=receipts)

        assert evidence is not None
        assert evidence.review_stage_key == "004-task-review"
        assert [f.id for f in evidence.findings] == ["G1"]
        assert [c.commit for c in evidence.commits] == ["d" * 40]

    def test_a_third_review_is_not_shown_the_first_cycle_s_commits(
        self, tmp_path: Path
    ) -> None:
        """The commits must be the ones made SINCE the review being checked.

        Every stage export re-copies the whole receipts folder, so a work
        stage that runs in the second cycle still carries copies of the
        first cycle's leg paperwork. Without a guard the reader would hand
        the third review the first cycle's commits as though they were made
        after the second review — and the reviewer could then conclude that
        a finding raised by that review was already fixed by a commit made
        before it.
        """
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)
        (
            _stage_dir(receipts, "004-task-review", TASK_ID) / "review_findings.json"
        ).write_text(
            json.dumps(
                {
                    "clean": False,
                    "findings": [
                        {
                            "id": "G1",
                            "severity": "high",
                            "title": "The second cycle's own finding",
                            "file": "src/users/crud.py",
                            "line": 160,
                            "detail": "Still writing a naive value here.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # The second cycle's work stage, exported the way the fold really
        # exports it: the new leg AND re-copied paperwork from cycle one.
        _write_work_stage(
            receipts,
            "005-task-work",
            fix_task="TASK-VER1-003",
            subject="fix(TASK-VER1-003): write an aware value in crud",
            sha="d" * 40,
            files=["src/users/crud.py"],
        )
        _write_work_stage(
            receipts,
            "005-task-work",
            fix_task="TASK-VER1-001",
            subject="fix(TASK-VER1-001): make the deleted_at column timezone-aware",
            sha="a" * 40,
            files=["alembic/versions/39f6_add_deleted_at.py"],
        )
        _write_work_stage(
            receipts,
            "005-task-work",
            fix_task="TASK-VER1-002",
            subject="fix(TASK-VER1-002): answer a database error on delete_user with 503",
            sha="b" * 40,
            files=["src/users/router.py"],
        )

        evidence = read_prior_review_evidence(BUILD_ID, receipts_root=receipts)

        assert evidence is not None
        assert evidence.review_stage_key == "004-task-review"
        assert [c.commit for c in evidence.commits] == ["d" * 40]

    def test_a_leg_that_committed_nothing_is_not_listed_as_a_commit(
        self, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _write_review_stage(receipts)
        _write_work_stage(
            receipts,
            "002-task-work",
            fix_task="TASK-VER1-001",
            subject="",
            sha="e" * 40,
            files=[],
            committed=False,
        )

        evidence = read_prior_review_evidence(BUILD_ID, receipts_root=receipts)

        assert evidence is not None
        assert evidence.commits == ()
        entry = build_review_verification_context(
            build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
        )
        assert entry is not None
        assert "Commits made since that review (0)" in Path(entry["value"]).read_text(
            encoding="utf-8"
        )


class TestBadReceiptsDegrade:
    def test_malformed_findings_give_no_document_and_one_plain_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (
            _stage_dir(receipts, "001-task-review", TASK_ID) / "review_findings.json"
        ).write_text("{not json at all", encoding="utf-8")
        _write_work_stage(
            receipts,
            "002-task-work",
            fix_task="TASK-VER1-001",
            subject="fix(TASK-VER1-001): something",
            sha="f" * 40,
            files=["src/a.py"],
        )

        with caplog.at_level(logging.INFO):
            entry = build_review_verification_context(
                build_id=BUILD_ID, worktree_path=worktree, receipts_root=receipts
            )

        assert entry is None
        assert not (worktree / ".guardkit").exists()
        said = [record.getMessage() for record in caplog.records]
        assert any("found no earlier findings to check" in line for line in said)

    def test_a_missing_receipts_root_is_simply_no_document(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        assert (
            build_review_verification_context(
                build_id=BUILD_ID,
                worktree_path=worktree,
                receipts_root=tmp_path / "nowhere",
            )
            is None
        )

    def test_a_worktree_that_is_not_here_sends_the_same_words_inline(
        self, tmp_path: Path
    ) -> None:
        """The sandbox wall, stated as behaviour.

        For a repository with a sandbox the journey's tree is inside that
        sandbox and forge can neither read nor write it. The document is
        not written to a file it could not put there; the same Markdown
        travels with the dispatch instead, the way the failure-pack
        summary already travels, and the leg reads it the same way.
        """
        receipts = tmp_path / "receipts"
        _journey_with_one_cycle(receipts)

        entry = build_review_verification_context(
            build_id=BUILD_ID,
            worktree_path=tmp_path / "inside-the-sandbox",
            receipts_root=receipts,
        )

        assert entry is not None
        assert entry["kind"] == "text"
        assert VERIFY_INSTRUCTION in entry["value"]
        assert "F1" in entry["value"]
        assert not (tmp_path / "inside-the-sandbox").exists()


# ---------------------------------------------------------------------------
# The dispatch seam
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    build_id: str = BUILD_ID
    task_id: str | None = TASK_ID
    correlation_id: str = "corr-build-ver1"
    worktree_path: str | None = None
    feature_yaml_path: str | None = None
    feature_id: str = "FEAT-VER1"
    branch: str = "fix/FEAT-VER1"


@dataclass
class _RecordingDispatch:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, stage: Any, build_id: str, **kwargs: Any) -> str:
        self.calls.append({"stage": stage, "build_id": build_id, **kwargs})
        return "dispatched"


def _adapter(dispatch: Any, *, row: Any, receipts_root: Path) -> Any:
    return make_conductor_subprocess_dispatcher(
        build_row_reader=lambda _bid: row,
        read_allowlist=[Path("/work")],
        worktree_allowlist=object(),
        forward_context_builder=object(),
        stage_log_writer=object(),
        subprocess_runner=object(),
        dispatch=dispatch,
        correlation_id_minter=lambda **kw: "corr-fixed",
        receipts_root=receipts_root,
    )


class TestTheDispatchCarriesIt:
    @pytest.mark.asyncio
    async def test_a_follow_up_review_carries_the_document_beside_what_it_had(
        self, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch, row=_Row(worktree_path=str(worktree)), receipts_root=receipts
        )

        await adapter(
            stage=StageClass.TASK_REVIEW,
            build_id=BUILD_ID,
            forward_context={
                "context_entries": [
                    {"flag": "--context", "value": "/work/plan.md", "kind": "path"}
                ],
                "failure_pack": None,
            },
        )

        entries = dispatch.calls[0]["forward_context"]["context_entries"]
        assert entries[0] == {
            "flag": "--context",
            "value": "/work/plan.md",
            "kind": "path",
        }
        assert entries[1]["flag"] == "--context"
        assert entries[1]["kind"] == "path"
        assert entries[1]["value"].endswith(VERIFY_DOCUMENT_NAME)
        # The failure pack the conductor already carried is untouched.
        assert dispatch.calls[0]["forward_context"]["failure_pack"] is None

    @pytest.mark.asyncio
    async def test_a_first_review_dispatches_exactly_as_before(
        self, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        receipts.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch, row=_Row(worktree_path=str(worktree)), receipts_root=receipts
        )
        given = {"context_entries": [], "failure_pack": None}

        await adapter(
            stage=StageClass.TASK_REVIEW, build_id=BUILD_ID, forward_context=given
        )

        assert dispatch.calls[0]["forward_context"] == given

    @pytest.mark.asyncio
    async def test_a_work_leg_is_never_handed_the_document(
        self, tmp_path: Path
    ) -> None:
        """Only the review is asked to verify. The work legs are untouched."""
        receipts = tmp_path / "receipts"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _journey_with_one_cycle(receipts)
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch, row=_Row(worktree_path=str(worktree)), receipts_root=receipts
        )

        from forge.pipeline.mode_c_planner import FixTaskRef

        given = {"context_entries": [], "failure_pack": None}
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-VER1-009", review_history_index=0),
            forward_context=given,
        )

        assert dispatch.calls[0]["forward_context"] == given

    def test_any_other_stage_keeps_the_context_it_came_with(
        self, tmp_path: Path
    ) -> None:
        receipts = tmp_path / "receipts"
        _journey_with_one_cycle(receipts)
        given = {"context_entries": [], "failure_pack": None}

        assert (
            with_review_verification(
                stage=StageClass.AUTOBUILD,
                build_id=BUILD_ID,
                worktree_path=tmp_path,
                forward_context=given,
                receipts_root=receipts,
            )
            is given
        )


class TestASandboxRepositorysRequest:
    @pytest.mark.asyncio
    async def test_the_document_path_rides_in_the_request_the_sandbox_door_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The request fields, not the sidecar.

        For a repository with a sandbox the leg's arguments are assembled
        inside, from the fields of the request the sandbox door posts. So
        this asserts on that request body: the document must be one of the
        context paths it carries. Nothing is started and nothing is sent —
        the one function that would talk over the wire is replaced.
        """
        from forge.adapters.guardkit import run_via_sidecar

        repo = tmp_path / "api_test"
        worktree = repo / ".forge" / "worktrees" / BUILD_ID
        worktree.mkdir(parents=True)
        receipts = tmp_path / "receipts"
        _journey_with_one_cycle(receipts)

        posted: dict[str, Any] = {}

        def _fake_post(url: str, body: Any, timeout: float) -> tuple[int, Any]:
            posted["url"] = url
            posted["body"] = body
            return 200, {"exit_code": 0, "stdout": "", "stderr_tail": ""}

        monkeypatch.setattr(run_via_sidecar, "_post", _fake_post)
        runner = run_via_sidecar.build_sidecar_leg_run(
            base_url="http://127.0.0.1:8125",
            repo_paths={"api_test": str(repo)},
        )

        adapter = make_conductor_subprocess_dispatcher(
            build_row_reader=lambda _bid: _Row(worktree_path=str(worktree)),
            read_allowlist=[repo],
            worktree_allowlist=object(),
            forward_context_builder=object(),
            stage_log_writer=object(),
            subprocess_runner=runner,
            correlation_id_minter=lambda **kw: "corr-fixed",
            receipts_root=receipts,
        )

        await adapter(
            stage=StageClass.TASK_REVIEW,
            build_id=BUILD_ID,
            forward_context={"context_entries": [], "failure_pack": None},
        )

        body = posted["body"]
        assert body["subcommand"] == "task-review"
        assert body["cwd"] == str(worktree)
        document = str(
            worktree / ".guardkit" / "autobuild" / TASK_ID / VERIFY_DOCUMENT_NAME
        )
        assert document in body["extra_context_paths"]
        assert Path(document).is_file()

    def test_the_translated_kwargs_still_bind_to_the_real_dispatcher(self) -> None:
        """The seam's own signature check, kept beside the new field."""
        params = inspect.signature(make_conductor_subprocess_dispatcher).parameters
        assert "receipts_root" in params
        assert params["receipts_root"].default is None
        assert (
            "forward_context" in inspect.signature(dispatch_subprocess_stage).parameters
        )
