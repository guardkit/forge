"""The merge-ready checkpoint — the precondition, the silence, the card.

Revival design pass §c, Stage 1c.

Coverage map:

- **THE HARD PRECONDITION** (:class:`TestGatesGreenPrecondition`): a red
  gate NEVER publishes a card. It loops back into the fix cycle or
  terminates FAILED with a pack. An unknown / unwired / raising gate
  reader is treated as red — the precondition is *proven green*, never
  *not proven red*.
- **NO-COMMIT TERMINALS STAY SILENT** (:class:`TestNoCommitSilence`).
- **Never auto-merge** (:class:`TestAutoApproveRefusal`).
- The delivery itself (:class:`TestCardDelivery`): the push step is
  MODELLED and honestly reported, the card goes through the injected
  publisher seam, and a publish failure is never recorded as a delivery.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from forge.pipeline.merge_ready_checkpoint import (
    MERGE_READY_CHECKPOINT_LABEL,
    GatesReport,
    GateStatus,
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
    RedGateAction,
)

BUILD_ID = "build-FEAT-MRC-20260731000000"


def _submit(publisher: MergeReadyCheckpointPublisher, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "build_id": BUILD_ID,
        "feature_id": "FEAT-MRC",
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


# ---------------------------------------------------------------------------
# The hard precondition
# ---------------------------------------------------------------------------


class TestGatesGreenPrecondition:
    def test_a_red_gate_publishes_no_card_and_loops_back(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: GatesReport(
                status=GateStatus.RED,
                failed_gates=("pytest", "ruff"),
                detail="2 gates failed",
            ),
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert decision.card_published is False
        assert card.calls == []
        assert decision.loops_back is True

    def test_a_red_gate_can_terminate_failed_with_a_pack(self) -> None:
        card = _RecordingPublisher()
        packs: list[dict[str, Any]] = []

        def write_pack(**kwargs: Any) -> str:
            packs.append(kwargs)
            return "/packs/fix-journey.json"

        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: GatesReport(
                status=GateStatus.RED, failed_gates=("pytest",)
            ),
            red_gate_action=lambda _bid, _gates: RedGateAction.TERMINATE_FAILED,
            failure_pack_writer=write_pack,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_FAILED
        assert decision.is_terminal_failed is True
        assert decision.card_published is False
        assert card.calls == []
        assert decision.failure_pack == "/packs/fix-journey.json"
        assert "pytest" in packs[0]["reason"]

    def test_no_gate_reader_is_treated_as_red(self) -> None:
        """Proven green, never 'not proven red'."""
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(publish_card=card)

        decision = _submit(publisher)

        assert decision.card_published is False
        assert decision.gates is not None
        assert decision.gates.status is GateStatus.UNKNOWN
        assert card.calls == []

    def test_a_raising_gate_reader_is_treated_as_red(self) -> None:
        card = _RecordingPublisher()

        def boom(**_: Any) -> Any:
            raise RuntimeError("gate runner died")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=card, gates_green_reader=boom
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert decision.gates is not None
        assert decision.gates.status is GateStatus.UNKNOWN
        assert "RuntimeError" in decision.gates.detail
        assert card.calls == []

    def test_a_nonsense_gate_reader_result_is_treated_as_red(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card, gates_green_reader=lambda **_: "probably fine"
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert card.calls == []

    @pytest.mark.parametrize("green", [True, GatesReport(status=GateStatus.GREEN)])
    def test_a_green_gate_publishes_exactly_one_card(self, green: Any) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card, gates_green_reader=lambda **_: green
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert decision.card_published is True
        assert len(card.calls) == 1

    def test_a_false_gate_reader_result_is_red(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card, gates_green_reader=lambda **_: False
        )

        decision = _submit(publisher)

        assert decision.card_published is False
        assert card.calls == []


# ---------------------------------------------------------------------------
# No-commit silence
# ---------------------------------------------------------------------------


class TestNoCommitSilence:
    def test_no_commits_ends_with_a_receipt_and_no_card(self) -> None:
        card = _RecordingPublisher()
        gate_reads: list[Any] = []

        def gates(**kwargs: Any) -> Any:
            gate_reads.append(kwargs)
            return True

        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=gates,
            has_commits_probe=lambda _bid: False,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.NO_COMMITS_SILENT
        assert decision.card_published is False
        assert card.calls == []
        # The belt short-circuits BEFORE the gate set even runs — a
        # journey with nothing to merge spends no gate time.
        assert gate_reads == []

    def test_commits_present_proceeds_to_the_gates(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: True,
            has_commits_probe=lambda _bid: True,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(card.calls) == 1

    def test_a_raising_commit_probe_does_not_silence_the_leg(self) -> None:
        """An unknown answer is not 'no' — the gate precondition still guards."""
        card = _RecordingPublisher()

        def boom(_bid: str) -> bool:
            raise RuntimeError("git gone")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: True,
            has_commits_probe=boom,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED


# ---------------------------------------------------------------------------
# Never auto-merge
# ---------------------------------------------------------------------------


class TestAutoApproveRefusal:
    def test_auto_approve_is_refused_and_the_card_still_asks_a_human(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card, gates_green_reader=lambda **_: True
        )

        decision = _submit(publisher, auto_approve=True)

        assert decision.auto_approve_refused is True
        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        # The publisher seam is never handed an auto-approve flag: the
        # card it composes is an approve-CLICK card, by construction.
        assert "auto_approve" not in card.calls[0]

    def test_auto_approve_on_a_red_gate_still_publishes_nothing(self) -> None:
        card = _RecordingPublisher()
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: False,
        )

        decision = _submit(publisher, auto_approve=True)

        assert decision.card_published is False
        assert card.calls == []


# ---------------------------------------------------------------------------
# Delivery mechanics
# ---------------------------------------------------------------------------


class TestCardDelivery:
    def test_the_push_step_is_modelled_and_says_so(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            branch_reader=lambda _bid: "fix/FEAT-MRC",
        )

        decision = _submit(publisher)

        assert decision.push_modelled is True
        assert decision.pushed is False
        assert decision.branch == "fix/FEAT-MRC"

    def test_a_wired_push_seam_is_executed_and_reported(self) -> None:
        pushes: list[dict[str, Any]] = []

        def push(*, build_id: str, branch: str | None) -> bool:
            pushes.append({"build_id": build_id, "branch": branch})
            return True

        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            branch_reader=lambda _bid: "fix/FEAT-MRC",
            push_branch=push,
        )

        decision = _submit(publisher)

        assert decision.pushed is True
        assert decision.push_modelled is False
        assert pushes == [{"build_id": BUILD_ID, "branch": "fix/FEAT-MRC"}]

    def test_the_card_seam_receives_the_branch_and_the_gates(self) -> None:
        card = _RecordingPublisher()
        gates = GatesReport(status=GateStatus.GREEN, detail="12/12 green")
        publisher = MergeReadyCheckpointPublisher(
            publish_card=card,
            gates_green_reader=lambda **_: gates,
            branch_reader=lambda _bid: "fix/FEAT-MRC",
        )

        _submit(publisher)

        assert card.calls[0]["build_id"] == BUILD_ID
        assert card.calls[0]["feature_id"] == "FEAT-MRC"
        assert card.calls[0]["branch"] == "fix/FEAT-MRC"
        assert card.calls[0]["gates"] is gates

    def test_a_publish_failure_is_never_recorded_as_a_delivery(self) -> None:
        async def exploding(**_: Any) -> Any:
            raise ConnectionError("broker down")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=exploding, gates_green_reader=lambda **_: True
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.PUBLISH_FAILED
        assert decision.card_published is False
        assert "ConnectionError" in decision.details["publish_error"]

    def test_delivery_off_records_the_decision_without_publishing(self) -> None:
        """Design-pass Stage 2's shadow replay: gates run, no card goes out.

        Its own outcome word since Stage 2: "no publisher is wired" is a
        deliberate posture, not a failed publish, and conflating the two
        made the driver wait for a delivery that had no mechanism.
        """
        publisher = MergeReadyCheckpointPublisher(
            publish_card=None, gates_green_reader=lambda **_: True
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.DELIVERY_NOT_WIRED
        assert decision.card_may_be_on_the_wire is False
        assert decision.card_published is False
        assert decision.details == {"delivery_wired": False}
        assert decision.gates is not None and decision.gates.is_green

    def test_a_sync_card_seam_is_supported(self) -> None:
        calls: list[dict[str, Any]] = []

        def sync_card(**kwargs: Any) -> str:
            calls.append(dict(kwargs))
            return "ok"

        publisher = MergeReadyCheckpointPublisher(
            publish_card=sync_card, gates_green_reader=lambda **_: True
        )

        decision = _submit(publisher)

        assert decision.card_published is True
        assert decision.card_result == "ok"
        assert len(calls) == 1

    def test_the_rationale_speaks_the_plain_name(self) -> None:
        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
        )

        decision = _submit(publisher)

        assert MERGE_READY_CHECKPOINT_LABEL in decision.rationale
        assert "pull request" not in decision.rationale.lower()

    def test_a_raising_branch_reader_does_not_stop_the_leg(self) -> None:
        def boom(_bid: str) -> str:
            raise RuntimeError("no worktree row")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: True,
            branch_reader=boom,
        )

        decision = _submit(publisher)

        assert decision.branch is None
        assert decision.card_published is True

    def test_a_raising_red_gate_action_defaults_to_looping_back(self) -> None:
        def boom(_bid: str, _gates: GatesReport) -> RedGateAction:
            raise RuntimeError("policy blew up")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: False,
            red_gate_action=boom,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK

    def test_a_raising_failure_pack_writer_leaves_the_terminal_standing(
        self,
    ) -> None:
        def boom(**_: Any) -> str:
            raise OSError("disk full")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=_RecordingPublisher(),
            gates_green_reader=lambda **_: False,
            red_gate_action=lambda _b, _g: RedGateAction.TERMINATE_FAILED,
            failure_pack_writer=boom,
        )

        decision = _submit(publisher)

        assert decision.outcome is MergeCardOutcome.RED_GATE_FAILED
        assert decision.failure_pack is None
