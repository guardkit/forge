"""The one way a planning run is ended loudly.

Two callers end a run the same way: the intake consumer (an unknown
repository name is refused before any leg runs) and the chain driver (a leg
that cannot continue). Both write FAILED on the durable row, log the machine
reason verbatim, and send the person who asked one plain sentence.

Which of the two ended the run is recorded, not guessed: ``actor`` is
written to the durable row and stands at the front of the log line, so a
refusal at the door is never read later as a driver failure.

The split of audiences is the 2026-07-31 stage-names ruling and is preserved
here: the durable row and the logs keep ``stage_label`` and ``reason``
VERBATIM (grep and every receipt depend on them), while ``owner_message`` is
the sentence a person reads — the caller composes it.

References:
- ``docs/target-repo-intake-fix-spec-2026-09-05.md`` rule 4.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

from forge.planning.run_store import TransitionRefused
from forge.planning.states import PlanningState

_logger = logging.getLogger(__name__)

__all__ = ["DRIVER_ACTOR", "fail_run", "mark_run_failed"]


class _StoreLike(Protocol):  # pragma: no cover - structural typing only
    def transition(self, *args: Any, **kwargs: Any) -> Any: ...


Notify = Callable[..., Awaitable[Any]]
"""``async (correlation_id, message) -> Any`` — best-effort owner line."""


DRIVER_ACTOR = "planning-driver"
"""The chain driver's identity in the durable row and in its log lines."""


def _actor_label(actor: str) -> str:
    """The actor's identity as it reads at the front of a log line."""
    return actor.replace("-", " ")


def mark_run_failed(
    store: _StoreLike,
    correlation_id: str,
    *,
    stage_label: str,
    reason: str,
    actor: str = DRIVER_ACTOR,
    log: logging.Logger | None = None,
) -> None:
    """Write FAILED on the durable row; a refused transition is logged only.

    ``actor`` is who ended the run — the chain driver by default, the intake
    consumer when a name it cannot resolve is refused before any leg runs. It
    is written to the durable row so a receipt can tell the two apart.
    """
    logger = log or _logger
    refused = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.FAILED,
        actor_identity=actor,
        stage_label=stage_label,
        error=reason,
    )
    if isinstance(refused, TransitionRefused):
        logger.warning(
            "%s: FAILED transition refused for %s (current=%s, reason=%s)",
            _actor_label(actor),
            correlation_id,
            refused.current_state,
            reason,
        )


async def fail_run(
    store: _StoreLike,
    correlation_id: str,
    *,
    stage_label: str,
    reason: str,
    owner_message: str,
    actor: str = DRIVER_ACTOR,
    notify: Notify | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Move the run to FAILED, log it, tell the owner, and return False.

    Returns False so a leg can ``return await fail_run(...)`` and read as
    "this leg did not continue". ``actor`` names who ended the run (see
    :func:`mark_run_failed`).
    """
    logger = log or _logger
    label = _actor_label(actor)
    mark_run_failed(
        store,
        correlation_id,
        stage_label=stage_label,
        reason=reason,
        actor=actor,
        log=logger,
    )
    logger.error(
        "%s: run %s FAILED at %s: %s",
        label,
        correlation_id,
        stage_label,
        reason,
    )
    if notify is not None:
        try:
            await notify(correlation_id, owner_message)
        except Exception:  # noqa: BLE001 — a notification never blocks the row
            logger.warning(
                "%s: failure notification did not go out for %s (best-effort)",
                label,
                correlation_id,
            )
    return False
