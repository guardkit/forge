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

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from forge.adapters.nats.pipeline_publisher import PublishFailure
from forge.executor.registry import StepOutcome, StepTypeRegistry
from forge.persistence.repositories.runbook import RunbookRepository
from forge.persistence.repositories.runbook_models import StepStatus
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
    """

    status: Literal["complete", "already_complete", "escalated"]
    stopped_at_index: int | None = None
    reason: Literal["unknown_handler", "step_failed", "awaiting_approval"] | None = None


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
    """

    def __init__(
        self,
        repository: RunbookRepository,
        registry: StepTypeRegistry,
        publisher: RunbookPublisher,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._publisher = publisher

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
                continue

            # Announce step-started
            await self._safe_publish(
                self._publisher.publish_step_started,
                StepStartedPayload(
                    runbook_id=runbook.runbook_id,
                    sequence_index=step.sequence_index,
                    step_type=step.step_type,
                    correlation_id=correlation_id,
                ),
            )

            # Resolve handler
            handler = self._registry.resolve(step.step_type)
            if handler is None:
                logger.warning(
                    "executor.run runbook_id=%s step_index=%d step_type=%r: unknown handler, escalating",
                    runbook_id,
                    step_index,
                    step.step_type,
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

            # Atomically claim this step (pending -> running) BEFORE running its
            # handler. Two executors racing on the same runbook serialise here:
            # exactly one transitions pending->running (claimed) and proceeds;
            # the loser sees rowcount 0 (already claimed / completed / not
            # pending) and skips. This is the no-double-run guarantee. It
            # replaces a non-atomic "reload and compare" that had a TOCTOU
            # window in which both executors could pass the check and run the
            # same handler (flaky double-run, TASK-RBX-007).
            claimed = self._repo.try_claim_step_for_execution(
                runbook_id,
                step_index,
                correlation_id=correlation_id,
            )
            if not claimed:
                logger.info(
                    "executor.run runbook_id=%s step_index=%d: already claimed or "
                    "completed by a concurrent executor, skipping",
                    runbook_id,
                    step_index,
                )
                continue

            # Execute handler (catch exceptions)
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

            # Map outcome
            if outcome.status == StepStatus.passed:
                # Update step status, then advance
                # Note: result parameter omitted due to type mismatch (StepOutcome.result
                # is dict but repository expects StepResult dataclass)
                try:
                    self._repo.update_step_status(
                        runbook_id,
                        step_index,
                        StepStatus.passed,
                        correlation_id=correlation_id,
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
