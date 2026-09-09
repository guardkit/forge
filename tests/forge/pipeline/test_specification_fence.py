"""The specification fence — the specification is not the machine's to edit.

Rich's ruling, 2026-09-09, after a work leg renamed the acceptance twin
``qa/twins/users-delete-by-email/double-delete-honest-404.hurl`` to
``…-410.hurl``, changed the expected response from 204 to 410 to match the
code it had just written, and rewrote the line recording his own approval —
keeping his name and his date on a sentence he never said. The twins ARE what
the candidate check measures the running application against, so a green card
would have been a card measured against a specification the code had just
rewritten.

This file pins the two rules and what they do to a checkpoint:

* **the path rule** (:class:`TestTheSpecificationPathRule`) — a change to a
  file the repository calls its specification, at either end of a rename;
* **the recorded-approval rule** (:class:`TestTheRecordedApprovalRule`) — a
  changed line holding ``APPROVED`` and naming whose approval it is, in ANY
  file, wherever it lives;
* **what the checkpoint does with a refusal**
  (:class:`TestTheCheckpointRefuses`) — no card, a plain sentence naming the
  files and what was done to them, and the existing red-gate path: back into
  the fix cycle while a review cycle remains, FAILED with a pack when none
  does;
* **byte for byte as before** (:class:`TestNothingChangesForACleanBranch`) —
  a branch that touches no protected file publishes its card exactly as it
  did, and no fence wired at all is the checkpoint that predates this.
"""

from __future__ import annotations

import asyncio
from typing import Any

from forge.pipeline.merge_ready_checkpoint import (
    DEFAULT_SPECIFICATION_PATHS,
    ApprovalLineChange,
    BranchFileChange,
    GatesReport,
    GateStatus,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
    RedGateAction,
    SpecificationFenceStatus,
    approval_line_owner,
    judge_branch_changes,
    parse_changed_approval_lines,
    parse_changed_files,
    path_is_specification,
    unreadable_branch_changes,
    unreadable_specification_declaration,
)

BUILD_ID = "build-FEAT-39F6-20260909195749"
TWIN = "qa/twins/users-delete-by-email/double-delete-honest-404.hurl"
RENAMED_TWIN = "qa/twins/users-delete-by-email/double-delete-honest-410.hurl"
THE_RULING = (
    "# APPROVED AS PROPOSED by Rich 2026-07-28 (interactive sit; all 4 "
    "assumptions confirmed, ASSUM-003 = 404 honest absence)"
)


def _submit(publisher: MergeReadyCheckpointPublisher, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "build_id": BUILD_ID,
        "feature_id": "FEAT-39F6",
        "auto_approve": False,
        "rationale": "mode-c-commits-present",
    }
    kwargs.update(overrides)
    return asyncio.run(publisher.submit_decision(**kwargs))


class _RecordingPublisher:
    def __init__(self, result: Any = "gate-outcome") -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self.result


def _judge(changes: tuple, approvals: tuple = (), paths: tuple = ()) -> Any:
    return judge_branch_changes(
        changes=changes,
        approval_lines=approvals,
        specification_paths=paths or DEFAULT_SPECIFICATION_PATHS,
    )


# ---------------------------------------------------------------------------
# Rule one: the paths this repository calls its specification
# ---------------------------------------------------------------------------


class TestTheSpecificationPathRule:
    def test_the_default_is_the_acceptance_twins(self) -> None:
        assert DEFAULT_SPECIFICATION_PATHS == ("qa/twins/**",)
        assert path_is_specification(TWIN, DEFAULT_SPECIFICATION_PATHS) is True
        assert path_is_specification("src/app.py", DEFAULT_SPECIFICATION_PATHS) is False

    def test_a_repository_may_declare_its_own(self) -> None:
        declared = ("docs/specs/*.md", "contracts/**")

        assert path_is_specification("docs/specs/delete.md", declared) is True
        assert path_is_specification("contracts/a/b/c.yaml", declared) is True
        # ``*`` stops at a slash; ``**`` does not.
        assert path_is_specification("docs/specs/old/delete.md", declared) is False
        # A declaration REPLACES the default, so the twins are only protected
        # if the repository says so.
        assert path_is_specification(TWIN, declared) is False

    def test_a_renamed_twin_is_refused_and_the_sentence_names_both_names(
        self,
    ) -> None:
        report = _judge(
            (BranchFileChange(status="R", path=RENAMED_TWIN, old_path=TWIN),)
        )

        assert report.status is SpecificationFenceStatus.REFUSED
        assert TWIN in report.detail and RENAMED_TWIN in report.detail
        assert "renamed to" in report.detail
        assert "a specification change is the owner's to make" in report.detail
        assert "no card was published" in report.detail

    def test_a_twins_body_being_edited_is_refused(self) -> None:
        report = _judge((BranchFileChange(status="M", path=TWIN),))

        assert report.status is SpecificationFenceStatus.REFUSED
        assert f"{TWIN} (changed)" in report.detail

    def test_a_twin_deleted_or_added_is_refused_too(self) -> None:
        for status, word in (("D", "deleted"), ("A", "added")):
            report = _judge((BranchFileChange(status=status, path=TWIN),))
            assert report.status is SpecificationFenceStatus.REFUSED
            assert f"({word})" in report.detail

    def test_a_branch_that_touches_no_twin_is_clear(self) -> None:
        report = _judge(
            (
                BranchFileChange(status="M", path="src/users/crud.py"),
                BranchFileChange(status="A", path="tests/test_crud.py"),
            )
        )

        assert report.status is SpecificationFenceStatus.CLEAR
        assert report.refuses is False
        assert report.detail == ""

    def test_the_two_rules_are_named_separately(self) -> None:
        report = _judge(
            (BranchFileChange(status="R", path=RENAMED_TWIN, old_path=TWIN),),
            (
                ApprovalLineChange(
                    path=RENAMED_TWIN, line=THE_RULING, added=True, owner="Rich"
                ),
            ),
        )

        assert len(report.failed_gates) == 2
        assert any("specification" in gate for gate in report.failed_gates)
        assert any("recorded approval" in gate for gate in report.failed_gates)
        # Every gate name carries the files it fired on, because the sentence
        # that closes a journey out is built from these names.
        assert all(".hurl" in gate for gate in report.failed_gates)


# ---------------------------------------------------------------------------
# Rule two: a recorded approval, wherever it lives
# ---------------------------------------------------------------------------


class TestTheRecordedApprovalRule:
    def test_it_reads_the_change_not_the_path(self) -> None:
        report = _judge(
            (BranchFileChange(status="M", path="docs/decisions/delete.md"),),
            (
                ApprovalLineChange(
                    path="docs/decisions/delete.md",
                    line=THE_RULING,
                    added=False,
                    owner="Rich",
                ),
            ),
        )

        assert report.status is SpecificationFenceStatus.REFUSED
        assert "docs/decisions/delete.md" in report.detail
        assert "Rich's approval" in report.detail
        assert "a recorded approval is the owner's own word" in report.detail

    def test_a_line_naming_who_approved_is_recognised(self) -> None:
        assert approval_line_owner(THE_RULING) == "Rich"
        assert approval_line_owner("# APPROVED BY RICH 2026-07-28") == "RICH"
        assert approval_line_owner("  - APPROVED by Gilbert on the call") == "Gilbert"

    def test_a_line_that_records_nobodys_approval_is_not_one(self) -> None:
        assert approval_line_owner("the plan was approved last week") == ""
        assert approval_line_owner("APPROVED_STATES = ('a', 'b')") == ""
        assert approval_line_owner("# APPROVED (no name on it)") == ""

    def test_the_incidents_own_diff_is_read_out_of_a_real_patch(self) -> None:
        patch = (
            f"diff --git a/{TWIN} b/{TWIN}\n"
            f"--- a/{TWIN}\n"
            f"+++ b/{TWIN}\n"
            "@@ -1 +1 @@\n"
            f"-{THE_RULING}\n"
            f"+{THE_RULING.replace('404 honest absence', '410 Gone for soft-deleted')}\n"
        )

        lines = parse_changed_approval_lines(patch)

        assert [line.added for line in lines] == [False, True]
        assert {line.path for line in lines} == {TWIN}
        assert {line.owner for line in lines} == {"Rich"}

    def test_a_deleted_file_is_named_from_the_half_the_line_is_in(self) -> None:
        patch = (
            f"diff --git a/{TWIN} b/{TWIN}\n"
            f"--- a/{TWIN}\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            f"-{THE_RULING}\n"
        )

        lines = parse_changed_approval_lines(patch)

        assert [(line.path, line.added) for line in lines] == [(TWIN, False)]


# ---------------------------------------------------------------------------
# git's own output, read exactly as git wrote it
# ---------------------------------------------------------------------------


class TestReadingGitsOwnOutput:
    def test_a_rename_carries_both_paths(self) -> None:
        changes = parse_changed_files(f"R100\0{TWIN}\0{RENAMED_TWIN}\0M\0src/app.py\0")

        assert changes[0] == BranchFileChange(
            status="R", path=RENAMED_TWIN, old_path=TWIN
        )
        assert changes[1] == BranchFileChange(status="M", path="src/app.py")

    def test_a_path_with_a_space_in_it_survives(self) -> None:
        changes = parse_changed_files("M\0qa/twins/a file with spaces.hurl\0")

        assert changes[0].path == "qa/twins/a file with spaces.hurl"

    def test_nothing_changed_is_no_changes(self) -> None:
        assert parse_changed_files("") == ()
        assert parse_changed_approval_lines("") == ()


# ---------------------------------------------------------------------------
# What the checkpoint does with a refusal
# ---------------------------------------------------------------------------


class TestTheCheckpointRefuses:
    @staticmethod
    def _refusing_fence(seen: list | None = None):
        def fence(**kwargs: Any) -> Any:
            if seen is not None:
                seen.append(dict(kwargs))
            return _judge(
                (BranchFileChange(status="R", path=RENAMED_TWIN, old_path=TWIN),)
            )

        return fence

    def test_no_card_is_published_and_the_gates_are_never_even_run(self) -> None:
        card = _RecordingPublisher()
        ran: list[str] = []
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: ran.append("suite") or True,
            branch_reader=lambda _bid: "fix/TASK-FEAT39F6FIX1-19957 49",
            specification_fence=self._refusing_fence(),
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert card.calls == []
        assert ran == [], "a branch that cannot be carded does not spend a suite run"

    def test_it_is_a_red_checkpoint_that_loops_back_into_the_fix_cycle(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            specification_fence=self._refusing_fence(),
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert decision.loops_back is True
        assert decision.gates is not None
        assert decision.gates.status is GateStatus.RED

    def test_with_no_cycle_left_it_closes_out_failed_naming_the_files(self) -> None:
        packs: list[dict[str, Any]] = []
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            red_gate_action=lambda *_a: RedGateAction.TERMINATE_FAILED,
            failure_pack_writer=lambda **kw: packs.append(kw) or "/packs/p.json",
            specification_fence=self._refusing_fence(),
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_FAILED
        assert decision.is_terminal_failed is True
        assert decision.failure_pack == "/packs/p.json"
        assert TWIN in packs[0]["reason"] and RENAMED_TWIN in packs[0]["reason"]

    def test_the_sentence_names_the_files_on_the_decision_and_in_the_log(
        self, caplog: Any
    ) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            specification_fence=self._refusing_fence(),
        )

        with caplog.at_level("ERROR"):
            decision = _submit(publisher)

        for text in (decision.gates.detail, decision.rationale, caplog.text):
            assert TWIN in text
            assert RENAMED_TWIN in text
        assert "a specification change is the owner's to make" in decision.gates.detail
        # The rule names ride on the gate report, which is what the journey's
        # own close-out reads when it says why it stopped.
        assert any("specification" in gate for gate in decision.gates.failed_gates)

    def test_the_fence_is_asked_about_this_build_and_this_branch(self) -> None:
        seen: list[dict[str, Any]] = []
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            branch_reader=lambda _bid: "fix/TASK-FEAT39F6FIX1",
            specification_fence=self._refusing_fence(seen),
        )

        _submit(publisher)

        assert seen == [
            {"build_id": BUILD_ID, "branch": "fix/TASK-FEAT39F6FIX1"}
        ]

    def test_a_fence_that_could_not_read_the_branch_refuses_too(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            specification_fence=lambda **_: unreadable_branch_changes(
                "git could not answer"
            ),
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert "could not be read" in decision.gates.detail

    def test_a_declaration_nobody_could_read_refuses_too(self) -> None:
        """A repository that DECLARES NOTHING takes the default. A
        declaration that is there and could not be read is a reading that did
        not happen: falling back to the default would fence the default paths
        in place of the ones this repository meant to name."""
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            specification_fence=lambda **_: unreadable_specification_declaration(
                ".guardkit/config.yaml could not be parsed"
            ),
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert (
            "which files this repository calls its specification could not be "
            "read" in decision.gates.detail
        )

    def test_a_fence_that_raises_is_a_refusal_never_a_pass(self) -> None:
        def boom(**_kw: Any) -> Any:
            raise RuntimeError("the fence fell over")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            specification_fence=boom,
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert "the fence itself raised RuntimeError" in decision.gates.detail

    def test_a_journey_with_no_commits_is_still_silent_not_refused(self) -> None:
        """§c.6 comes first: nothing was committed, so there is no branch to
        fence and no card either — a receipt is the whole delivery."""
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            has_commits_probe=lambda _bid: False,
            specification_fence=self._refusing_fence(),
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.NO_COMMITS_SILENT


# ---------------------------------------------------------------------------
# Byte for byte as before
# ---------------------------------------------------------------------------


class TestNothingChangesForACleanBranch:
    def test_a_clean_branch_publishes_its_card_exactly_as_today(self) -> None:
        card = _RecordingPublisher("APPROVED")
        with_fence = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: GatesReport(
                status=GateStatus.GREEN, detail="`qa/run-suite.sh` exited 0"
            ),
            branch_reader=lambda _bid: "fix/TASK-FEAT39F6FIX1",
            specification_fence=lambda **_: _judge(
                (BranchFileChange(status="M", path="src/users/crud.py"),)
            ),
        )
        without = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher("APPROVED"),
            gates_green_reader=lambda **_: GatesReport(
                status=GateStatus.GREEN, detail="`qa/run-suite.sh` exited 0"
            ),
            branch_reader=lambda _bid: "fix/TASK-FEAT39F6FIX1",
        )

        fenced = _submit(with_fence)
        plain = _submit(without)

        assert fenced.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert fenced.card_published is True
        assert len(card.calls) == 1
        # The decision is the same one in every field the journey reads.
        assert fenced.rationale == plain.rationale
        assert fenced.gates == plain.gates
        assert fenced.details == plain.details
        assert fenced.card_result == plain.card_result

    def test_with_no_fence_wired_nothing_runs_and_nothing_changes(self) -> None:
        card = _RecordingPublisher("APPROVED")
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: True,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(card.calls) == 1

    def test_a_fence_that_answers_nothing_at_all_is_no_fence(self) -> None:
        card = _RecordingPublisher("APPROVED")
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: True,
            specification_fence=lambda **_: None,
        )

        assert _submit(publisher).outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(card.calls) == 1

    def test_a_red_suite_still_reads_exactly_as_it_did(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: GatesReport(
                status=GateStatus.RED,
                failed_gates=("declared toolchain test",),
                detail="`qa/run-suite.sh` exited 1",
            ),
            specification_fence=lambda **_: _judge(
                (BranchFileChange(status="M", path="src/users/crud.py"),)
            ),
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert decision.gates.failed_gates == ("declared toolchain test",)
