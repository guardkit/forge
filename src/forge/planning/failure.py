"""The one way a planning run is ended loudly.

Two callers end a run the same way: the intake consumer (an unknown
repository name is refused before any leg runs) and the chain driver (a leg
that cannot continue). Both write FAILED on the durable row, log the machine
reason verbatim, and send the person who asked one plain sentence.

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

__all__ = ["fail_run", "mark_run_failed"]


class _StoreLike(Protocol):  # pragma: no cover - structural typing only
    def transition(self, *args: Any, **kwargs: Any) -> Any: ...


Notify = Callable[..., Awaitable[Any]]
"""``async (correlation_id, message) -> Any`` — best-effort owner line."""


def mark_run_failed(
    store: _StoreLike,
    correlation_id: str,
    *,
    stage_label: str,
    reason: str,
    log: logging.Logger | None = None,
) -> None:
    """Write FAILED on the durable row; a refused transition is logged only."""
    logger = log or _logger
    refused = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.FAILED,
        actor_identity="planning-driver",
        stage_label=stage_label,
        error=reason,
    )
    if isinstance(refused, TransitionRefused):
        logger.warning(
            "planning driver: FAILED transition refused for %s "
            "(current=%s, reason=%s)",
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
    notify: Notify | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Move the run to FAILED, log it, tell the owner, and return False.

    Returns False so a leg can ``return await fail_run(...)`` and read as
    "this leg did not continue".
    """
    logger = log or _logger
    mark_run_failed(
        store, correlation_id, stage_label=stage_label, reason=reason, log=logger
    )
    logger.error(
        "planning driver: run %s FAILED at %s: %s",
        correlation_id,
        stage_label,
        reason,
    )
    if notify is not None:
        try:
            await notify(correlation_id, owner_message)
        except Exception:  # noqa: BLE001 — a notification never blocks the row
            logger.warning(
                "planning driver: failure notification did not go out for %s "
                "(best-effort)",
                correlation_id,
            )
    return False
