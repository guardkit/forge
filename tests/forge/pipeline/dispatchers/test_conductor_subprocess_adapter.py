"""The conductor→dispatcher seam (Stage 2 shakeout items 1 and 2).

Two defects lived at this seam and neither was visible from either side:

1. the supervisor's Mode C dispatcher kwargs never carried ``task_id``, and
   ``task-review`` REFUSES a subject-less dispatch — so every fix journey
   failed on its first turn;
2. the supervisor passed ``rationale`` and ``forward_context``, which
   ``dispatch_subprocess_stage`` declared neither of and had no ``**kwargs``
   for — so the call would have raised ``TypeError`` before doing any work.

These pin the cure: the adapter binds the durable anchors off the build row,
translates the supervisor's vocabulary into the dispatcher's, and mints this
dispatch's own correlation id (the FTR exact-match law).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.dispatchers.conductor_subprocess import (
    LEG_BUDGET_ARGV,
    make_conductor_subprocess_dispatcher,
    mint_stage_correlation_id,
)
from forge.pipeline.dispatchers.subprocess import (
    StageDispatchStatus,
    dispatch_subprocess_stage,
)
from forge.pipeline.mode_c_planner import FixTaskRef
from forge.pipeline.stage_taxonomy import StageClass

BUILD_ID = "build-FEAT-FIX007-20260731"


@dataclass
class _Row:
    build_id: str = BUILD_ID
    task_id: str | None = "TASK-FIX007"
    correlation_id: str = "corr-build-1"
    worktree_path: str | None = "/work/build-FEAT-FIX007"
    feature_yaml_path: str | None = "/work/tasks/fix-task.yaml"
    feature_id: str = "FEAT-FIX007"
    branch: str = "fix/FEAT-FIX007"


@dataclass
class _RecordingDispatch:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, stage: Any, build_id: str, **kwargs: Any) -> str:
        self.calls.append({"stage": stage, "build_id": build_id, **kwargs})
        return "dispatched"


def _adapter(
    dispatch: Any, *, row: Any = None, writer: Any = None, **extra: Any
) -> Any:
    return make_conductor_subprocess_dispatcher(
        build_row_reader=lambda _bid: row if row is not None else _Row(),
        read_allowlist=[Path("/work")],
        worktree_allowlist=object(),
        forward_context_builder=object(),
        stage_log_writer=writer if writer is not None else object(),
        subprocess_runner=object(),
        dispatch=dispatch,
        correlation_id_minter=lambda **kw: "corr-fixed",
        **extra,
    )


class TestTaskIdBinding:
    """Item 1 — the subject the supervisor never had to hand."""

    @pytest.mark.asyncio
    async def test_review_dispatch_binds_task_id_from_the_build_row(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(
            stage=StageClass.TASK_REVIEW,
            build_id=BUILD_ID,
            feature_id=None,
            rationale="MODE_C planner chose task-review",
        )

        assert dispatch.calls[0]["task_id"] == "TASK-FIX007"

    @pytest.mark.asyncio
    async def test_a_row_without_task_id_still_reaches_the_dispatcher(self) -> None:
        """One refusal, not two.

        The adapter says loudly that the row has no subject, but it does
        NOT invent a second refusal shape: the dispatcher's structured
        FAILED result is the estate's one answer to a subject-less
        fix-journey dispatch, and the supervisor already knows how to read
        it.
        """
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, row=_Row(task_id=None))

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["task_id"] is None

    @pytest.mark.asyncio
    async def test_work_dispatch_subject_is_the_fix_task_not_the_build_task(
        self,
    ) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        call = dispatch.calls[0]
        assert call["fix_task"].fix_task_id == "TASK-FIX007-A"
        assert call["task_id"] == "TASK-FIX007"

    @pytest.mark.asyncio
    async def test_the_queues_fix_task_yaml_rides_off_the_row(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["fix_task_yaml"] == "/work/tasks/fix-task.yaml"


class TestTheKwargsSeam:
    """Item 2 — the call that would have raised ``TypeError``."""

    def test_the_dispatcher_declares_forward_context_deliberately(self) -> None:
        params = inspect.signature(dispatch_subprocess_stage).parameters
        assert "forward_context" in params
        # …and NOT rationale: a supervisor word that does not cross the
        # boundary must not grow a parameter to cross it in.
        assert "rationale" not in params

    @pytest.mark.asyncio
    async def test_supervisor_kwargs_translate_without_raising(self) -> None:
        """The exact kwarg set the Mode C turn builds, end to end."""
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        result = await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            feature_id=None,
            rationale="MODE_C planner chose task-work",
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
            forward_context={"context_entries": [], "failure_pack": None},
        )

        assert result == "dispatched"
        assert dispatch.calls[0]["forward_context"] == {
            "context_entries": [],
            "failure_pack": None,
        }
        assert "rationale" not in dispatch.calls[0]

    @pytest.mark.asyncio
    async def test_the_real_dispatcher_accepts_the_translated_kwargs(self) -> None:
        """Signature-binding, not a permissive fake.

        A fake that swallows ``**kwargs`` would pass this suite and still
        let the production call raise. Bind the translated kwargs against
        the REAL dispatcher's signature instead.
        """
        captured: dict[str, Any] = {}

        async def binding_dispatch(stage: Any, build_id: str, **kwargs: Any) -> str:
            inspect.signature(dispatch_subprocess_stage).bind(
                stage, build_id, **kwargs
            )
            captured.update(kwargs)
            return "bound"

        adapter = _adapter(binding_dispatch)
        assert (
            await adapter(
                stage=StageClass.TASK_REVIEW,
                build_id=BUILD_ID,
                feature_id=None,
                rationale="why",
                forward_context={"context_entries": []},
            )
            == "bound"
        )
        assert captured["task_id"] == "TASK-FIX007"

    @pytest.mark.asyncio
    async def test_an_unknown_supervisor_kwarg_is_reported_not_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        with caplog.at_level("ERROR"):
            await adapter(
                stage=StageClass.TASK_REVIEW,
                build_id=BUILD_ID,
                some_future_kwarg="x",
            )

        assert any("unrecognised supervisor" in r.getMessage() for r in caplog.records)


class TestCorrelationIdentity:
    """The FTR exact-match law: every dispatch mints its own id."""

    def test_two_dispatches_of_the_same_stage_get_different_ids(self) -> None:
        first = mint_stage_correlation_id(
            build_correlation_id="corr-1",
            stage=StageClass.TASK_WORK,
            subject="TASK-A",
        )
        second = mint_stage_correlation_id(
            build_correlation_id="corr-1",
            stage=StageClass.TASK_WORK,
            subject="TASK-A",
        )
        assert first != second
        # …and both stay greppable as one journey.
        assert first.startswith("corr-1:task-work:TASK-A:")
        assert second.startswith("corr-1:task-work:TASK-A:")

    @pytest.mark.asyncio
    async def test_the_minted_id_is_what_reaches_the_dispatcher(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["correlation_id"] == "corr-fixed"


class TestWorktreeRefusal:
    @pytest.mark.asyncio
    async def test_no_worktree_is_a_structured_refusal_not_a_guess(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, row=_Row(worktree_path=None))

        result = await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert result.status is StageDispatchStatus.FAILED
        assert "inferred directory" in result.rationale
        assert dispatch.calls == []


class TestPerStageTimeouts:
    """LI stage-2 §1 — ONE scalar for both legs was the defect.

    A work leg needs a far longer tripwire than a review leg, and the naive
    one-line raise would have lifted BOTH — un-fencing the review leg's
    480 < 600 inner-under-outer margin, which is the margin that keeps a
    timed-out review from silently discarding a perfect marker block.
    """

    @pytest.mark.asyncio
    async def test_each_leg_selects_its_own_tripwire(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch,
            timeout_seconds_by_stage={
                StageClass.TASK_REVIEW: 600,
                StageClass.TASK_WORK: 1800,
            },
        )

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["timeout_seconds"] == 600
        assert dispatch.calls[1]["timeout_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_a_stage_the_mapping_does_not_name_keeps_the_scalar(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch,
            timeout_seconds=600,
            timeout_seconds_by_stage={StageClass.TASK_WORK: 1800},
        )

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["timeout_seconds"] == 600

    @pytest.mark.asyncio
    async def test_no_mapping_at_all_is_exactly_todays_behaviour(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert [c["timeout_seconds"] for c in dispatch.calls] == [600, 600]


class TestTheLegSeat:
    """LI stage-2 §3.4 — the pipeline must be able to NAME the seat."""

    @pytest.mark.asyncio
    async def test_a_named_seat_rides_on_both_fix_journey_stages(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, leg_model="qwen3-coder-30b")

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["extra_args"] == ["--model", "qwen3-coder-30b"]
        assert dispatch.calls[1]["extra_args"] == ["--model", "qwen3-coder-30b"]

    @pytest.mark.asyncio
    async def test_a_non_mode_c_stage_never_carries_the_leg_seat(self) -> None:
        """The seat is the FIX JOURNEY's, not the routine path's.

        ``FEATURE_PLAN`` is dispatched by this adapter only when the
        conductor is driving a planning stage; appending the leg's seat
        there would silently re-point a routine build's model.
        """
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, leg_model="qwen3-coder-30b")

        await adapter(
            stage=StageClass.FEATURE_PLAN, build_id=BUILD_ID, feature_id="FEAT-FIX007"
        )

        assert dispatch.calls[0]["extra_args"] is None

    @pytest.mark.asyncio
    async def test_an_unset_seat_appends_nothing_at_all(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch)

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["extra_args"] is None


class TestArgvByteIdentityWithNoSeat:
    """The contract the seat may not break: unset changes NO byte of argv.

    Driven through the REAL dispatcher (and its real argv builder) rather
    than a recording double — the claim is about the command line that
    reaches the runner, and only the real builder produces one.
    """

    @staticmethod
    async def _argv(
        *, stage: StageClass = StageClass.TASK_REVIEW, **adapter_kwargs: Any
    ) -> list[str]:
        from forge.pipeline.forward_context_builder import ForwardContextBuilder

        from tests.forge.pipeline.dispatchers.test_subprocess import (
            FakeStageLogReader,
            FakeStageLogWriter,
            FakeSubprocessRunner,
            FakeWorktreeAllowlist,
        )

        runner = FakeSubprocessRunner()
        allowlist = FakeWorktreeAllowlist(
            roots_by_build={BUILD_ID: "/work/build-FEAT-FIX007"}
        )
        adapter = make_conductor_subprocess_dispatcher(
            build_row_reader=lambda _bid: _Row(),
            read_allowlist=[Path("/work")],
            worktree_allowlist=allowlist,
            forward_context_builder=ForwardContextBuilder(
                FakeStageLogReader(), allowlist
            ),
            stage_log_writer=FakeStageLogWriter(),
            subprocess_runner=runner,
            correlation_id_minter=lambda **kw: "corr-fixed",
            **adapter_kwargs,
        )
        extra: dict[str, Any] = {}
        if stage is StageClass.TASK_WORK:
            extra["fix_task"] = FixTaskRef(
                fix_task_id="TASK-FIX007-A", review_history_index=0
            )
        await adapter(stage=stage, build_id=BUILD_ID, **extra)
        return list(runner.calls[0]["args"])

    @pytest.mark.asyncio
    async def test_the_argv_is_unchanged_when_no_seat_is_named(self) -> None:
        without_the_parameter = await self._argv()
        with_an_explicit_none = await self._argv(leg_model=None)

        assert with_an_explicit_none == without_the_parameter
        assert "--model" not in without_the_parameter

    @pytest.mark.asyncio
    async def test_a_named_seat_appends_exactly_two_tokens_and_moves_nothing(
        self,
    ) -> None:
        baseline = await self._argv()
        seated = await self._argv(leg_model="qwen3-coder-30b")

        assert seated == baseline + ["--model", "qwen3-coder-30b"]


@dataclass
class _Budgets:
    """A budget profile's leg knobs, duck-typed as the adapter reads them."""

    leg_max_turns: int | None = None
    leg_sdk_timeout_seconds: int | None = None
    leg_budget_seconds: int | None = None


class TestLegBudgetsBecomeYamlKnobs:
    """The pipeline could not name a leg budget at all until this group.

    The dispatcher's only extra argv was ``--model <seat>``; the build
    system's hardcoded 2 turns / 420s / 1620s therefore governed
    production and moving them was an image-level change. These pin the
    two halves of the cure: absent changes nothing, present is threaded —
    and threaded only where the leg actually declares the flag.
    """

    @pytest.mark.asyncio
    async def test_the_sdk_timeout_rides_on_both_fix_journey_legs(self) -> None:
        """The one flag BOTH leg kinds declare."""
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch, leg_budgets=_Budgets(leg_sdk_timeout_seconds=300)
        )

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["extra_args"] == ["--sdk-timeout", "300"]
        assert dispatch.calls[1]["extra_args"] == ["--sdk-timeout", "300"]

    @pytest.mark.asyncio
    async def test_a_full_profile_gives_each_leg_the_flags_it_declares(self) -> None:
        """BOTH leg kinds are threaded — with their own accepted flags.

        ``--max-turns`` and ``--leg-budget`` are declared by ``guardkit
        task-work`` and NOT by ``guardkit task-review``. Click rejects an
        undeclared option with exit 2 before the command body runs, so
        threading them onto a review would not shorten the review — it
        would kill the journey at its opening leg, with no receipt. Same
        class of defect as the ``--parent-feature`` pair the dispatcher
        used to emit and nothing accepted.
        """
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch,
            leg_budgets=_Budgets(
                leg_max_turns=4,
                leg_sdk_timeout_seconds=300,
                leg_budget_seconds=900,
            ),
        )

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)
        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["extra_args"] == ["--sdk-timeout", "300"]
        assert dispatch.calls[1]["extra_args"] == [
            "--max-turns",
            "4",
            "--sdk-timeout",
            "300",
            "--leg-budget",
            "900",
        ]

    @pytest.mark.asyncio
    async def test_the_seat_and_the_budgets_ride_together_seat_first(self) -> None:
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch,
            leg_model="qwen3-coder-30b",
            leg_budgets=_Budgets(leg_max_turns=4),
        )

        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["extra_args"] == [
            "--model",
            "qwen3-coder-30b",
            "--max-turns",
            "4",
        ]

    @pytest.mark.asyncio
    async def test_a_non_mode_c_stage_never_carries_a_leg_budget(self) -> None:
        """These are the FIX JOURNEY's knobs, not the routine path's."""
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, leg_budgets=_Budgets(leg_sdk_timeout_seconds=300))

        await adapter(
            stage=StageClass.FEATURE_PLAN, build_id=BUILD_ID, feature_id="FEAT-FIX007"
        )

        assert dispatch.calls[0]["extra_args"] is None

    @pytest.mark.asyncio
    async def test_a_profile_with_no_leg_fields_appends_nothing(self) -> None:
        """Every profile written before this group existed resolves here."""
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, leg_budgets=_Budgets())

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["extra_args"] is None

    @pytest.mark.asyncio
    async def test_a_profile_object_that_lacks_the_fields_entirely_is_fine(
        self,
    ) -> None:
        """An old ``BudgetGuards``-shaped stand-in must not crash the leg."""
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, leg_budgets=object())

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["extra_args"] is None

    @pytest.mark.asyncio
    async def test_a_value_that_is_not_a_positive_int_is_dropped_not_threaded(
        self,
    ) -> None:
        """Degrade to the leg's own default; never mint ``--max-turns None``.

        Pydantic refuses these at config load (``ge=1``), so arriving here
        means an injected stand-in — and a malformed argv trades a config
        mistake for a dead leg, which is the worse of the two.
        """
        dispatch = _RecordingDispatch()
        adapter = _adapter(
            dispatch,
            leg_budgets=_Budgets(
                leg_max_turns=True,  # a bool IS an int — and is not a number
                leg_sdk_timeout_seconds="300",  # type: ignore[arg-type]
                leg_budget_seconds=0,
            ),
        )

        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert dispatch.calls[0]["extra_args"] is None

    def test_the_acceptance_table_matches_what_guardkit_declares(self) -> None:
        """The table is the checkable form of "which leg accepts which flag".

        Stated as data so a future flag is added to ONE place. If guardkit
        ever teaches ``task-review`` ``--max-turns``, this is the row to
        widen — and this assertion is what will be sitting there when
        somebody widens it by accident instead.
        """
        table = {
            field: (flag, stages) for field, flag, stages in LEG_BUDGET_ARGV
        }

        assert table["leg_max_turns"] == (
            "--max-turns",
            frozenset({StageClass.TASK_WORK}),
        )
        assert table["leg_budget_seconds"] == (
            "--leg-budget",
            frozenset({StageClass.TASK_WORK}),
        )
        assert table["leg_sdk_timeout_seconds"] == (
            "--sdk-timeout",
            frozenset({StageClass.TASK_REVIEW, StageClass.TASK_WORK}),
        )


class TestArgvByteIdentityWithNoLegBudgets:
    """The contract the knobs may not break, through the REAL argv builder.

    ``absent = byte-identical`` is the whole reason this group could land
    on an ``extra=forbid`` schema without a migration, so it is proven on
    the command line that actually reaches the runner — not on a recording
    double's kwargs.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stage", [StageClass.TASK_REVIEW, StageClass.TASK_WORK]
    )
    async def test_the_argv_is_unchanged_when_no_budgets_are_carried(
        self, stage: StageClass
    ) -> None:
        _argv = TestArgvByteIdentityWithNoSeat._argv

        without_the_parameter = await _argv(stage=stage)
        with_an_explicit_none = await _argv(stage=stage, leg_budgets=None)
        with_an_empty_profile = await _argv(stage=stage, leg_budgets=_Budgets())

        assert with_an_explicit_none == without_the_parameter
        assert with_an_empty_profile == without_the_parameter
        for flag in ("--max-turns", "--sdk-timeout", "--leg-budget"):
            assert flag not in without_the_parameter

    @pytest.mark.asyncio
    async def test_a_work_leg_appends_the_budget_tokens_and_moves_nothing(
        self,
    ) -> None:
        _argv = TestArgvByteIdentityWithNoSeat._argv

        baseline = await _argv(stage=StageClass.TASK_WORK)
        budgeted = await _argv(
            stage=StageClass.TASK_WORK,
            leg_budgets=_Budgets(
                leg_max_turns=4, leg_sdk_timeout_seconds=300, leg_budget_seconds=900
            ),
        )

        assert budgeted == baseline + [
            "--max-turns",
            "4",
            "--sdk-timeout",
            "300",
            "--leg-budget",
            "900",
        ]

    @pytest.mark.asyncio
    async def test_a_review_leg_appends_only_the_flag_it_declares(self) -> None:
        _argv = TestArgvByteIdentityWithNoSeat._argv

        baseline = await _argv(stage=StageClass.TASK_REVIEW)
        budgeted = await _argv(
            stage=StageClass.TASK_REVIEW,
            leg_budgets=_Budgets(
                leg_max_turns=4, leg_sdk_timeout_seconds=300, leg_budget_seconds=900
            ),
        )

        assert budgeted == baseline + ["--sdk-timeout", "300"]


class TestStageLogWriterBinding:
    @pytest.mark.asyncio
    async def test_a_work_dispatch_writes_through_a_fix_task_bound_writer(
        self,
    ) -> None:
        """The row must be attributable or the planner double-dispatches."""
        bound_with: list[str | None] = []

        class _Writer:
            def for_fix_task(self, fix_task_id: str | None) -> str:
                bound_with.append(fix_task_id)
                return f"writer-for-{fix_task_id}"

        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, writer=_Writer())

        await adapter(
            stage=StageClass.TASK_WORK,
            build_id=BUILD_ID,
            fix_task=FixTaskRef(fix_task_id="TASK-FIX007-A", review_history_index=0),
        )

        assert bound_with == ["TASK-FIX007-A"]
        assert dispatch.calls[0]["stage_log_writer"] == "writer-for-TASK-FIX007-A"

    @pytest.mark.asyncio
    async def test_a_review_dispatch_uses_the_unbound_writer(self) -> None:
        class _Writer:
            def for_fix_task(self, fix_task_id: str | None) -> str:  # pragma: no cover
                raise AssertionError("a review is not scoped to one fix task")

        writer = _Writer()
        dispatch = _RecordingDispatch()
        adapter = _adapter(dispatch, writer=writer)

        await adapter(stage=StageClass.TASK_REVIEW, build_id=BUILD_ID)

        assert dispatch.calls[0]["stage_log_writer"] is writer
