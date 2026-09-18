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

Since 2026-09-07 the sentence a person reads is written down too: the FAILED
transition's event carries it in its details, under ``failure``, beside the
machine reason and the stage label. The work queue reads it from there and
closes the run's row with the words Rich already read, never with the machine
reason. The ``error`` column still holds the machine reason, as before.

References:
- ``docs/target-repo-intake-fix-spec-2026-09-05.md`` rule 4.
- ``docs/rewrite-on-refusal-spec-2026-09-06.md`` Part I (rule 35).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Protocol

from forge.planning.run_store import TransitionRefused
from forge.planning.states import PlanningState

_logger = logging.getLogger(__name__)

__all__ = [
    "DRIVER_ACTOR",
    "FAILURE_DETAILS_KEY",
    "OWNER_MESSAGE_KEY",
    "fail_run",
    "failure_details",
    "mark_run_failed",
]


class _StoreLike(Protocol):  # pragma: no cover - structural typing only
    def transition(self, *args: Any, **kwargs: Any) -> Any: ...


Notify = Callable[..., Awaitable[Any]]
"""``async (correlation_id, message) -> Any`` — best-effort owner line."""

PublishTerminal = Callable[[str, str], Awaitable[Any]]
"""``async (correlation_id, reason) -> Any`` — derived terminal projection."""


DRIVER_ACTOR = "planning-driver"
"""The chain driver's identity in the durable row and in its log lines."""


FAILURE_DETAILS_KEY = "failure"
"""The key on the FAILED event's details under which the failure is written."""


OWNER_MESSAGE_KEY = "owner_message"
"""Inside that block: the plain sentence the owner was sent."""


def _actor_label(actor: str) -> str:
    """The actor's identity as it reads at the front of a log line."""
    return actor.replace("-", " ")


def failure_details(
    *, owner_message: str, reason: str, stage_label: str
) -> dict[str, Any]:
    """What the FAILED event's details carry.

    The owner's sentence sits beside the machine reason and the stage label
    so a reader of the event has the three together: what Rich read, what
    the machine said, and where the run was when it stopped.
    """
    return {
        FAILURE_DETAILS_KEY: {
            OWNER_MESSAGE_KEY: owner_message,
            "reason": reason,
            "stage_label": stage_label,
        }
    }


def mark_run_failed(
    store: _StoreLike,
    correlation_id: str,
    *,
    stage_label: str,
    reason: str,
    actor: str = DRIVER_ACTOR,
    owner_message: str | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Write FAILED on the durable row and report whether it was committed.

    ``actor`` is who ended the run — the chain driver by default, the intake
    consumer when a name it cannot resolve is refused before any leg runs. It
    is written to the durable row so a receipt can tell the two apart.

    ``owner_message``, when given, is written on the FAILED event's details
    (see :func:`failure_details`) so the work queue can close the row with
    the sentence Rich read. When it is not given the transition is written
    exactly as before, with no details on the event.
    """
    logger = log or _logger
    extra: dict[str, Any] = {}
    if owner_message is not None:
        extra["details_json"] = json.dumps(
            failure_details(
                owner_message=owner_message,
                reason=reason,
                stage_label=stage_label,
            )
        )
    refused = store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.FAILED,
        actor_identity=actor,
        stage_label=stage_label,
        error=reason,
        **extra,
    )
    if isinstance(refused, TransitionRefused):
        logger.warning(
            "%s: FAILED transition refused for %s (current=%s, reason=%s)",
            _actor_label(actor),
            correlation_id,
            refused.current_state,
            reason,
        )
        return False
    return True


async def fail_run(
    store: _StoreLike,
    correlation_id: str,
    *,
    stage_label: str,
    reason: str,
    owner_message: str,
    actor: str = DRIVER_ACTOR,
    notify: Notify | None = None,
    publish_terminal: PublishTerminal | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Move the run to FAILED, log it, tell the owner, and return False.

    Returns False so a leg can ``return await fail_run(...)`` and read as
    "this leg did not continue". ``actor`` names who ended the run (see
    :func:`mark_run_failed`). The owner's sentence is both sent and written
    on the FAILED event, so the queue closes the row with the same words.
    """
    logger = log or _logger
    label = _actor_label(actor)
    transitioned = mark_run_failed(
        store,
        correlation_id,
        stage_label=stage_label,
        reason=reason,
        actor=actor,
        owner_message=owner_message,
        log=logger,
    )
    logger.error(
        "%s: run %s FAILED at %s: %s",
        label,
        correlation_id,
        stage_label,
        reason,
    )
    if transitioned and publish_terminal is not None:
        try:
            await publish_terminal(correlation_id, reason)
        except Exception:
            logger.warning(
                "%s: planning-failed projection did not go out for %s "
                "(durable row remains FAILED)",
                label,
                correlation_id,
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
