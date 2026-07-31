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


def _adapter(dispatch: Any, *, row: Any = None, writer: Any = None) -> Any:
    return make_conductor_subprocess_dispatcher(
        build_row_reader=lambda _bid: row if row is not None else _Row(),
        read_allowlist=[Path("/work")],
        worktree_allowlist=object(),
        forward_context_builder=object(),
        stage_log_writer=writer if writer is not None else object(),
        subprocess_runner=object(),
        dispatch=dispatch,
        correlation_id_minter=lambda **kw: "corr-fixed",
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
