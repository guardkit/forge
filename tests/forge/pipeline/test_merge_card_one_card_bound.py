"""ONE card per journey, and the honest word for how it ended.

Stage 2 shakeout items 6 and 7 — two defects that shared a root: the
checkpoint's audit counter (``card_published``) was the only thing anyone
looked at, so nothing noticed what happened *after* the publish.

* **Item 6, the raise path.** ``PUBLISH_FAILED`` mapped to ``WAITING``,
  the driver re-planned, and one flaky publish could re-issue the merge
  card up to three more times. Three cards for one merge word.
* **Item 7, the word.** A REJECTED or expired card was written up as
  ``DELIVERED``, because the loop keyed on "a card went out" and never on
  what came back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from forge.pipeline.conductor_driver import (
    ConductorRunOutcome,
    _classify_card_result,
)
from forge.pipeline.merge_ready_checkpoint import (
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
)
from forge.pipeline.supervisor import Supervisor, TurnOutcome

BUILD_ID = "build-FEAT-MRC-20260731"


def _submit(publisher: MergeReadyCheckpointPublisher, **over: Any) -> Any:
    kwargs = {
        "build_id": BUILD_ID,
        "feature_id": "FEAT-MRC",
        "auto_approve": False,
        "rationale": "clean follow-up review with commits",
    }
    kwargs.update(over)
    return asyncio.run(publisher.submit_decision(**kwargs))


class TestTheOneCardLatch:
    def test_a_second_checkpoint_publishes_nothing(self) -> None:
        publishes: list[dict[str, Any]] = []

        publisher = MergeReadyCheckpointPublisher(
            publish_card=lambda **kw: publishes.append(kw) or "RESUMED",
            gates_green_reader=lambda **_: True,
        )

        first = _submit(publisher)
        second = _submit(publisher)

        assert first.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert second.outcome is MergeCardOutcome.ALREADY_CHECKPOINTED
        assert second.card_published is False
        assert len(publishes) == 1

    def test_a_raised_publish_still_arms_the_latch(self) -> None:
        """"May have reached the wire" is the state that must not be retried."""
        attempts: list[int] = []

        def boom(**_kw: Any) -> Any:
            attempts.append(1)
            raise ConnectionError("broker down")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=boom, gates_green_reader=lambda **_: True
        )

        first = _submit(publisher)
        second = _submit(publisher)

        assert first.outcome is MergeCardOutcome.PUBLISH_FAILED
        assert first.card_may_be_on_the_wire is True
        assert second.outcome is MergeCardOutcome.ALREADY_CHECKPOINTED
        assert len(attempts) == 1

    def test_a_red_gate_does_not_arm_the_latch(self) -> None:
        """Looping back into the fix cycle and checkpointing later IS the design."""
        gates = {"green": False}
        publishes: list[dict[str, Any]] = []

        publisher = MergeReadyCheckpointPublisher(
            publish_card=lambda **kw: publishes.append(kw) or "RESUMED",
            gates_green_reader=lambda **_: gates["green"],
        )

        red = _submit(publisher)
        assert red.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert publishes == []

        gates["green"] = True
        green = _submit(publisher)
        assert green.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(publishes) == 1

    def test_the_latch_is_per_build(self) -> None:
        publishes: list[str] = []
        publisher = MergeReadyCheckpointPublisher(
            publish_card=lambda **kw: publishes.append(kw["build_id"]) or "RESUMED",
            gates_green_reader=lambda **_: True,
        )

        _submit(publisher, build_id="build-a")
        _submit(publisher, build_id="build-b")

        assert publishes == ["build-a", "build-b"]

    def test_a_failed_publish_leaves_a_pack(self) -> None:
        packs: list[dict[str, Any]] = []

        def boom(**_kw: Any) -> Any:
            raise ConnectionError("broker down")

        publisher = MergeReadyCheckpointPublisher(
            publish_card=boom,
            gates_green_reader=lambda **_: True,
            failure_pack_writer=lambda **kw: packs.append(kw) or "/packs/x.json",
        )

        decision = _submit(publisher)

        assert decision.failure_pack == "/packs/x.json"
        assert "ConnectionError" in packs[0]["reason"]


class TestTheSupervisorStopsRatherThanRetries:
    @pytest.mark.parametrize(
        "outcome",
        [
            MergeCardOutcome.PUBLISH_FAILED,
            MergeCardOutcome.DELIVERY_NOT_WIRED,
            MergeCardOutcome.ALREADY_CHECKPOINTED,
        ],
    )
    def test_the_no_card_endings_are_terminal(
        self, outcome: MergeCardOutcome
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import MergeCardDecision

        decision = MergeCardDecision(outcome=outcome, build_id=BUILD_ID)
        assert Supervisor._merge_card_turn_outcome(decision) is TurnOutcome.TERMINAL

    def test_a_red_gate_loop_back_is_still_a_wait(self) -> None:
        from forge.pipeline.merge_ready_checkpoint import MergeCardDecision

        decision = MergeCardDecision(
            outcome=MergeCardOutcome.RED_GATE_LOOP_BACK, build_id=BUILD_ID
        )
        assert Supervisor._merge_card_turn_outcome(decision) is TurnOutcome.WAITING


class _Report:
    def __init__(self, card_result: Any) -> None:
        from forge.pipeline.merge_ready_checkpoint import MergeCardDecision

        self.dispatch_result = MergeCardDecision(
            outcome=MergeCardOutcome.CARD_PUBLISHED,
            build_id=BUILD_ID,
            card_published=True,
            card_result=card_result,
        )


class TestTheHonestOutcomeWord:
    @pytest.mark.parametrize(
        ("verdict", "expected"),
        [
            ("RESUMED", ConductorRunOutcome.DELIVERED),
            ("OVERRIDDEN", ConductorRunOutcome.DELIVERED),
            ("CANCELLED", ConductorRunOutcome.DECLINED),
            ("FAILED", ConductorRunOutcome.DECLINED),
            ("TIMED_OUT", ConductorRunOutcome.EXPIRED),
        ],
    )
    def test_each_verdict_gets_its_own_word(
        self, verdict: str, expected: ConductorRunOutcome
    ) -> None:
        assert _classify_card_result(_Report(verdict)) is expected

    def test_the_real_gate_outcome_enum_maps(self) -> None:
        """Against the real vocabulary, not a re-spelled string.

        The driver duck-types so it keeps no import edge to the gating
        package — which is exactly why the mapping has to be pinned
        against the real enum somewhere.
        """
        from forge.gating.wrappers import GateOutcome

        assert (
            _classify_card_result(_Report(GateOutcome.CANCELLED))
            is ConductorRunOutcome.DECLINED
        )
        assert (
            _classify_card_result(_Report(GateOutcome.TIMED_OUT))
            is ConductorRunOutcome.EXPIRED
        )
        assert (
            _classify_card_result(_Report(GateOutcome.RESUMED))
            is ConductorRunOutcome.DELIVERED
        )

    def test_an_unreadable_verdict_says_so_rather_than_inventing_a_word(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            assert (
                _classify_card_result(_Report("SOMETHING-NEW"))
                is ConductorRunOutcome.DELIVERED
            )
        assert any("not a verdict" in r.getMessage() for r in caplog.records)
