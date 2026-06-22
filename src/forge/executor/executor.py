"""Runbook executor dispatch loop (TASK-RBX-004).

The :class:`RunbookExecutor` is the heart of the runbook execution feature.
It loads a persisted runbook, walks steps from ``current_step_index``,
dispatches each to its registered handler, persists the result before
advancing the pointer, and announces lifecycle events.

Design invariants:

- **Result-before-advance ordering**: ``update_step_status(result)`` commits
  **before** ``advance()``. A crash in the gap leaves the pointer on a
  ``passed`` step; the recovery shortcut handles it on the next run.
- **Publish failures never roll back**: Each ``publisher.*`` call is wrapped
  so ``PublishFailure`` is caught + logged and the run continues regardless
  (ASSUM-009-exec).
- **Escalation on unknown handler**: If a step's handler resolves to ``None``,
  the run stops and escalates without marking the step passed (ASSUM-002).
- **Graceful exception handling**: A handler that raises is contained and
  mapped to ``StepOutcome(status=failed)``; the executor never crashes
  (ASSUM-008).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from forge.adapters.nats.pipeline_publisher import PublishFailure
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    RunbookRepository,
)
from forge.persistence.repositories.runbook_models import StepResult, StepStatus
from nats_core.events import (
    EscalatedPayload,
    RunbookCompletePayload,
    RunbookStartedPayload,
    StepResultPayload,
    StepStartedPayload,
)

if TYPE_CHECKING:  # pragma: no cover
    from forge.adapters.nats.runbook_publisher import RunbookPublisher

logger = logging.getLogger(__name__)

__all__ = ["RunbookExecutor", "RunResult"]


#: Default backoff (seconds) between reload attempts when the step at the resume
#: pointer is ``running`` but not claimable — held by a live peer executor, or
#: by a crashed peer whose lease has not yet expired. The ``await asyncio.sleep``
#: keeps the dispatch loop from hot-spinning while it waits for the peer to
#: advance the pointer or for the lease to expire (TASK-RBX-009).
_DEFAULT_STALL_BACKOFF_SECONDS: Final[float] = 0.5


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    """Overall execution result returned by :meth:`RunbookExecutor.run`.

    Attributes:
        status: Overall run status (complete, already_complete, or escalated).
        stopped_at_index: Step index where execution stopped (None if completed).
        reason: Escalation reason if status is escalated (None otherwise).
            ``stalled`` means the step at the resume pointer stayed ``running``
            and un-claimable across the configured number of no-progress
            backoff cycles — the executor stops instead of busy-spinning
            (TASK-RBX-009). Unlike the other reasons, ``stalled`` is not
            published as a NATS escalated event (the sibling ``nats_core``
            ``EscalatedPayload.reason`` Literal does not include it); it is
            logged and surfaced via this result.
    """

    status: Literal["complete", "already_complete", "escalated"]
    stopped_at_index: int | None = None
    reason: (
        Literal["unknown_handler", "step_failed", "awaiting_approval", "stalled"] | None
    ) = None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class RunbookExecutor:
    """Runbook execution dispatch loop.

    Composes the ``RunbookRepository`` (persistence), ``StepTypeRegistry``
    (handler resolution), and ``RunbookPublisher`` (lifecycle events).

    Args:
        repository: Persistence repository for loading/updating runbooks.
        registry: Step type registry for resolving handlers.
        publisher: Event publisher for announcing lifecycle events.
        claim_lease_seconds: Claim-lease window passed to the repository when
            claiming a step. A ``running`` step claimed longer ago than this is
            reclaimable as crash recovery (TASK-RBX-009).
        stall_backoff_seconds: Seconds to sleep between reload attempts while
            the step at the resume pointer is ``running`` but not claimable.
        max_stall_cycles: Number of consecutive no-progress backoff cycles on a
            single un-claimable ``running`` step before the executor stops with
            ``reason="stalled"`` instead of busy-spinning. Defaults to a value
            derived from the lease and backoff so the lease-reclaim path (which
            restores progress within ``claim_lease_seconds``) always wins over
            this safety net for a crashed peer; it then only fires if the step
            is truly wedged.
    """

    def __init__(
        self,
        repository: RunbookRepository,
        registry: StepTypeRegistry,
        publisher: RunbookPublisher,
        *,
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
        stall_backoff_seconds: float = _DEFAULT_STALL_BACKOFF_SECONDS,
        max_stall_cycles: int | None = None,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._publisher = publisher
        self._claim_lease_seconds = claim_lease_seconds
        self._stall_backoff_seconds = stall_backoff_seconds
        if max_stall_cycles is not None:
            self._max_stall_cycles = max_stall_cycles
        elif stall_backoff_seconds > 0:
            # Bound the wait at roughly one lease window plus a margin so a
            # crashed peer's expired lease is reclaimed before this net trips.
            self._max_stall_cycles = (
                int(claim_lease_seconds / stall_backoff_seconds) + 2
            )
        else:
            self._max_stall_cycles = 1

    async def run(self, runbook_id: str, *, correlation_id: str) -> RunResult:
        """Execute a runbook from its current resume point.

        Loads the runbook, walks steps from ``current_step_index``, dispatches
        each to its registered handler, persists results before advancing the
        pointer, and announces lifecycle events.

        Args:
            runbook_id: Unique identifier for the runbook to execute.
            correlation_id: Correlation ID for tracing this execution.

        Returns:
            RunResult describing the overall execution outcome.

        Raises:
            ValueError: If runbook_id or correlation_id is empty.
            RunbookNotFoundError: If the runbook does not exist (via repository).
        """
        if not runbook_id:
            raise ValueError("RunbookExecutor.run: runbook_id must be non-empty")
        if not correlation_id:
            raise ValueError("RunbookExecutor.run: correlation_id must be non-empty")

        # 1. Load runbook
        runbook = self._repo.load_runbook(runbook_id, correlation_id=correlation_id)
        if runbook is None:
            from forge.persistence.repositories.runbook import RunbookNotFoundError

            raise RunbookNotFoundError(runbook_id)

        # 2. Refuse empty runbooks (ASSUM-006)
        step_count = len(runbook.steps)
        if step_count == 0:
            logger.warning(
                "executor.run runbook_id=%s: refusing empty runbook (step_count=0)",
                runbook_id,
            )
            return RunResult(status="escalated", reason="unknown_handler")

        # 3. Already complete check (ASSUM-005)
        if runbook.current_step_index == step_count:
            logger.info(
                "executor.run runbook_id=%s: already complete, no-op",
                runbook_id,
            )
            return RunResult(status="already_complete")

        # 4. Announce runbook-started
        await self._safe_publish(
            self._publisher.publish_runbook_started,
            RunbookStartedPayload(
                runbook_id=runbook.runbook_id,
                target=runbook.target,
                step_count=step_count,
                correlation_id=correlation_id,
            ),
        )

        # 5. Loop through steps
        #
        # Crash-recovery stall guard (TASK-RBX-009): track consecutive
        # no-progress backoff cycles on a single un-claimable ``running`` step
        # so the loop never hot-spins. Reset whenever the pointer moves on
        # (a peer advanced) or we win a claim.
        stall_count = 0
        stalled_index: int | None = None
        while True:
            # Reload runbook at the start of each iteration to detect concurrent changes
            runbook = self._repo.load_runbook(runbook_id, correlation_id=correlation_id)
            if runbook is None:
                from forge.persistence.repositories.runbook import RunbookNotFoundError

                raise RunbookNotFoundError(runbook_id)

            # Check if we've completed all steps
            if runbook.current_step_index >= step_count:
                break

            step_index = runbook.current_step_index
            step = runbook.steps[step_index]

            # Recovery shortcut: skip already-passed steps
            if step.status == StepStatus.passed:
                logger.info(
                    "executor.run runbook_id=%s step_index=%d: already passed, advancing without re-run",
                    runbook_id,
                    step_index,
                )
                self._repo.advance(runbook_id, correlation_id=correlation_id)
                stall_count = 0
                stalled_index = None
                continue

            # Resolve handler BEFORE claiming so an unknown handler escalates
            # without leaving a step claimed/running behind us.
            handler = self._registry.resolve(step.step_type)
            if handler is None:
                logger.warning(
                    "executor.run runbook_id=%s step_index=%d step_type=%r: unknown handler, escalating",
                    runbook_id,
                    step_index,
                    step.step_type,
                )
                # Announce step-started for observability before escalating.
                await self._safe_publish(
                    self._publisher.publish_step_started,
                    StepStartedPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        step_type=step.step_type,
                        correlation_id=correlation_id,
                    ),
                )
                await self._safe_publish(
                    self._publisher.publish_escalated,
                    EscalatedPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        reason="unknown_handler",
                        correlation_id=correlation_id,
                    ),
                )
                return RunResult(
                    status="escalated",
                    stopped_at_index=step_index,
                    reason="unknown_handler",
                )

            # Atomically claim this step BEFORE running its handler. Two
            # executors racing on the same runbook serialise here: exactly one
            # transitions a runnable step -> running (claimed) and proceeds; the
            # loser sees rowcount 0 and skips. This is the no-double-run
            # guarantee. It replaces a non-atomic "reload and compare" that had
            # a TOCTOU window in which both executors could pass the check and
            # run the same handler (flaky double-run, TASK-RBX-007).
            #
            # A step left ``running`` by a crashed executor is reclaimed here
            # once its lease (claimed_at) expires; until then the claim returns
            # False and we back off rather than hot-spin (TASK-RBX-009).
            claimed = self._repo.try_claim_step_for_execution(
                runbook_id,
                step_index,
                correlation_id=correlation_id,
                lease_seconds=self._claim_lease_seconds,
                owner=correlation_id,
            )
            if not claimed:
                # The step at the pointer is ``running`` with a live lease
                # (a genuinely in-flight peer, or a crashed peer whose lease has
                # not yet expired), or it was just completed/advanced by a peer.
                # Either way, do NOT run it (no-double-run). Back off so the loop
                # cannot busy-spin; the peer will advance the pointer, or the
                # lease will expire and a later iteration will reclaim it.
                if step_index != stalled_index:
                    # New step under contention — reset the no-progress counter.
                    stall_count = 0
                    stalled_index = step_index
                stall_count += 1
                if stall_count > self._max_stall_cycles:
                    logger.warning(
                        "executor.run runbook_id=%s step_index=%d: step stuck in "
                        "running across %d no-progress cycles, escalating stalled",
                        runbook_id,
                        step_index,
                        stall_count,
                    )
                    # Not published as a NATS escalated event: the sibling
                    # nats_core EscalatedPayload.reason Literal has no "stalled".
                    return RunResult(
                        status="escalated",
                        stopped_at_index=step_index,
                        reason="stalled",
                    )
                logger.info(
                    "executor.run runbook_id=%s step_index=%d: running step not "
                    "claimable (cycle %d/%d), backing off",
                    runbook_id,
                    step_index,
                    stall_count,
                    self._max_stall_cycles,
                )
                if self._stall_backoff_seconds > 0:
                    await asyncio.sleep(self._stall_backoff_seconds)
                continue

            # Claim won — progress. Reset the stall guard and announce
            # step-started now that we own the step (announced once per step we
            # actually run, not on every backoff poll).
            stall_count = 0
            stalled_index = None
            await self._safe_publish(
                self._publisher.publish_step_started,
                StepStartedPayload(
                    runbook_id=runbook.runbook_id,
                    sequence_index=step.sequence_index,
                    step_type=step.step_type,
                    correlation_id=correlation_id,
                ),
            )

            # Execute handler (catch exceptions); bracket with timestamps so the
            # persisted StepResult records real start/finish times.
            started_at = datetime.now(UTC)
            try:
                outcome = handler(step)
            except Exception as exc:
                logger.exception(
                    "executor.run runbook_id=%s step_index=%d: handler raised, mapping to failed",
                    runbook_id,
                    step_index,
                )
                outcome = StepOutcome(
                    status=StepStatus.failed,
                    result={"error": str(exc), "exception_type": type(exc).__name__},
                )
            completed_at = datetime.now(UTC)

            # Adapt the handler's free-form result dict into the persistence
            # StepResult so the outcome is durably recorded, not just announced.
            step_result = self._build_step_result(outcome, started_at, completed_at)

            # Map outcome
            if outcome.status == StepStatus.passed:
                # Persist status + result, THEN advance (result-before-advance).
                try:
                    self._repo.update_step_status(
                        runbook_id,
                        step_index,
                        StepStatus.passed,
                        correlation_id=correlation_id,
                        result=step_result,
                    )
                    self._repo.advance(runbook_id, correlation_id=correlation_id)
                except Exception as advance_err:
                    # Handle concurrent executor race: if another executor completed this
                    # step while we were running the handler, the advance may fail.
                    # Log and continue to next iteration to pick up where we left off.
                    from forge.persistence.repositories.runbook import (
                        RunbookAdvanceError,
                    )

                    if isinstance(advance_err, RunbookAdvanceError):
                        logger.info(
                            "executor.run runbook_id=%s step_index=%d: concurrent executor advanced past this step",
                            runbook_id,
                            step_index,
                        )
                        continue
                    # Re-raise other errors
                    raise

                # Announce step-result (success)
                await self._safe_publish(
                    self._publisher.publish_step_result,
                    StepResultPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        step_type=step.step_type,
                        status="passed",
                        result=outcome.result,
                        correlation_id=correlation_id,
                    ),
                )

                # Reload runbook to get updated pointer
                runbook = self._repo.load_runbook(
                    runbook_id, correlation_id=correlation_id
                )
                if runbook is None:
                    from forge.persistence.repositories.runbook import (
                        RunbookNotFoundError,
                    )

                    raise RunbookNotFoundError(runbook_id)

            elif outcome.status == StepStatus.failed:
                # Update step status (do NOT advance)
                self._repo.update_step_status(
                    runbook_id,
                    step_index,
                    StepStatus.failed,
                    correlation_id=correlation_id,
                    result=step_result,
                )

                # Announce step-result (failure)
                await self._safe_publish(
                    self._publisher.publish_step_result,
                    StepResultPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        step_type=step.step_type,
                        status="failed",
                        result=outcome.result,
                        correlation_id=correlation_id,
                    ),
                )

                # Announce escalated
                await self._safe_publish(
                    self._publisher.publish_escalated,
                    EscalatedPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        reason="step_failed",
                        correlation_id=correlation_id,
                    ),
                )

                # Stop execution
                return RunResult(
                    status="escalated",
                    stopped_at_index=step_index,
                    reason="step_failed",
                )

            elif outcome.status == StepStatus.awaiting_approval:
                # Update step status (do NOT advance)
                self._repo.update_step_status(
                    runbook_id,
                    step_index,
                    StepStatus.awaiting_approval,
                    correlation_id=correlation_id,
                    result=step_result,
                )

                # Announce escalated
                await self._safe_publish(
                    self._publisher.publish_escalated,
                    EscalatedPayload(
                        runbook_id=runbook.runbook_id,
                        sequence_index=step.sequence_index,
                        reason="awaiting_approval",
                        correlation_id=correlation_id,
                    ),
                )

                # Stop execution
                return RunResult(
                    status="escalated",
                    stopped_at_index=step_index,
                    reason="awaiting_approval",
                )

        # 6. Announce runbook-complete
        await self._safe_publish(
            self._publisher.publish_runbook_complete,
            RunbookCompletePayload(
                runbook_id=runbook.runbook_id,
                step_count=step_count,
                correlation_id=correlation_id,
            ),
        )

        logger.info("executor.run runbook_id=%s: complete", runbook_id)
        return RunResult(status="complete")

    @staticmethod
    def _build_step_result(
        outcome: StepOutcome,
        started_at: datetime,
        completed_at: datetime,
    ) -> StepResult | None:
        """Adapt a handler's free-form ``StepOutcome.result`` into a ``StepResult``.

        The handler's structured, JSON-serialisable dict is persisted as a
        first-class value in ``StepResult.payload`` (round-tripped verbatim),
        not stuffed into ``captured_output`` as a JSON blob (TASK-RBX-008
        reconciled the model). ``exit_code`` is derived from the outcome status
        (0 for passed, 1 otherwise); ``captured_output`` is empty because these
        in-process handlers report their outcome structurally rather than as a
        stdout/stderr stream. Returns ``None`` when the handler produced no
        result, so the step's ``result`` column stays NULL.
        """
        if outcome.result is None:
            return None
        return StepResult(
            exit_code=0 if outcome.status == StepStatus.passed else 1,
            captured_output="",
            started_at=started_at,
            completed_at=completed_at,
            payload=outcome.result,
        )

    async def _safe_publish(self, publish_method, payload) -> None:
        """Wrap publish calls to catch + log PublishFailure (ASSUM-009-exec).

        Publish failures never roll back persisted progress; the event stream
        is a derived projection that downstream subscribers re-read from
        JetStream replay.
        """
        try:
            await publish_method(payload)
        except PublishFailure as exc:
            logger.warning(
                "executor: publish failed subject=%s error=%s (continuing)",
                exc.subject,
                exc.cause,
            )
        except Exception as exc:
            logger.exception("executor: unexpected publish error (continuing): %s", exc)
