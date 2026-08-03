"""The conductor router's taken-and-terminal vocabulary (activation §3/§4).

Why this module exists
----------------------

``conductor_router`` used to answer a bare ``bool``: ``True`` = "the
conductor took this build", ``False`` = "not mine — launch it the routine
way". That contract could say only two things, and the fix journey needs a
third: **taken, and already over**.

The measured defect (design pass §3): the cap-law refusal marks the row
FAILED and answers ``True``. Correct — it is never a downgrade — but
nothing acks the JetStream slot and nothing reaches the daemon's event
stream, so the bridge observer (which acks when it *sees* a terminal
event) never fires. With ``max_ack_pending=1`` on the pipeline consumer
that one refusal wedges the WHOLE consumer until the 1h ``ack_wait``
redelivery. A build that is over in milliseconds holds the queue for an
hour.

The vocabulary
--------------

Three outcomes, replacing the bool outright (the contract is REPLACED,
not dual-shaped — see :func:`check_router_outcome`):

* :data:`DECLINED` — "not mine". The routine autobuild launch proceeds,
  byte-for-byte as before.
* :data:`TAKEN_RUNNING` — the fix journey's turn loop was spawned. No
  launch, no ack: the journey owns its own terminal.
* :class:`TakenTerminal` — taken AND finished. The row is already FAILED
  with the reason on ``builds.error``; the caller acks the slot and emits
  ``build-failed`` so every observer sees the terminal it is waiting for.
  The reason RIDES the outcome rather than living only on the row, so the
  emit needs no database re-read to be honest.

The named template is the gate-terminal pattern
(:mod:`forge.cli._serve_gate_activation`): a typed outcome distinguishes
taken-running from taken-terminal, the terminal writer emits its own
event, and ``dispatch_build`` owns the ack on the terminal arm.

Why a typed RETURN and not a typed exception
--------------------------------------------

``launch_or_conduct`` CATCHES a raising router and degrades to the ROUTINE
launch ("the conductor earns jobs, it never blocks one"). A terminal
raised through that seam would be swallowed into precisely the silent
downgrade the cap law's own comment forbids. The typed return is the
smaller, safer diff.

One rule, one writer
--------------------

:func:`finish_mode_c_build` is the ONE writer that moves a mode-c row to a
terminal state, and :func:`fail_mode_c_build` is its FAILED-shaped face —
the phrasing four callers already use ("this mode-c build is refused: mark
it FAILED with the reason on the row"). The router's cap belt, the
router's per-build setup arm, the dispatch launch arm and the boot-rearm
resume launcher all go through it, each passing its own logger so the
message still names the seam it came from.

The journey's own close-out (:func:`forge.cli._serve_conductor.make_conductor_close_out`)
is the fifth caller and the reason the general form exists: a journey that
ends CLEAN needs the same careful writer pointed at ``COMPLETE``. Before
it, the close-out wrote a ``stage_log`` row and nothing else, so a fix
journey that reached its terminal left ``builds.status = RUNNING`` with an
empty ``error`` — forever (observed on the first production journey,
2026-08-03).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DECLINED",
    "TAKEN_RUNNING",
    "ConductorOutcome",
    "ConductorOutcomeContractError",
    "RouterOutcome",
    "TakenTerminal",
    "check_router_outcome",
    "fail_mode_c_build",
    "finish_mode_c_build",
    "is_mode_c_build",
]


class ConductorOutcome:
    """An identity-compared sentinel in the router's vocabulary.

    Only the two module-level instances (:data:`DECLINED` and
    :data:`TAKEN_RUNNING`) are ever constructed; callers compare with
    ``is``, never ``==``, so a look-alike from another module can never
    pass for one of these.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        """The sentinel's name (for logs and assertion messages)."""
        return self._name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ConductorOutcome.{self._name}"


#: The conductor is not driving this build — take the routine autobuild
#: launch. The flag-off default and the answer for every mode-a build.
DECLINED: ConductorOutcome = ConductorOutcome("DECLINED")

#: The conductor took the build and its turn loop is RUNNING. No routine
#: launch, no ack — the journey owns its own terminal.
TAKEN_RUNNING: ConductorOutcome = ConductorOutcome("TAKEN_RUNNING")


@dataclass(frozen=True)
class TakenTerminal:
    """Taken by the conductor AND already terminal, with the reason.

    The row is marked FAILED with ``reason``'s one-line summary on
    ``builds.error`` before this object is returned; the dispatch caller
    then acks the JetStream slot and emits ``build-failed`` carrying
    ``reason``. Carrying the reason here (rather than making the caller
    re-read ``builds.error``) is what keeps the emit honest without a
    second database round trip.

    Attributes:
        reason: One-line human-readable refusal. Non-blank — a terminal
            with nothing to say would publish an empty
            ``failure_reason``, which is the silent failure this whole
            vocabulary exists to abolish.
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError(
                "TakenTerminal.reason must be a non-blank one-line refusal: a "
                "terminal outcome that cannot say WHY is the silent failure "
                "this vocabulary exists to abolish"
            )


#: Everything the widened router contract may answer. Note this admits
#: ``ConductorOutcome`` structurally; the CONTRACT is the two named
#: sentinels only, and :func:`check_router_outcome` is what enforces it.
RouterOutcome = ConductorOutcome | TakenTerminal


class ConductorOutcomeContractError(RuntimeError):
    """A router answered outside the taken-and-terminal vocabulary.

    Raised — never swallowed — so a legacy ``True``/``False`` router (or
    any other stray value) fails LOUDLY at the seam instead of being read
    as "not mine" and silently downgraded onto the routine autobuild
    path. The contract is replaced, not dual-shaped.
    """


def check_router_outcome(value: Any, *, build_id: str | None = None) -> Any:
    """Validate ``value`` against the widened router contract.

    Args:
        value: Whatever the router answered.
        build_id: The build under dispatch, named in the error.

    Returns:
        ``value`` unchanged when it is :data:`DECLINED`,
        :data:`TAKEN_RUNNING`, or a :class:`TakenTerminal`.

    Raises:
        ConductorOutcomeContractError: For anything else — notably a bare
            ``bool``, the LEGACY contract. Note ``isinstance(True, int)``
            is irrelevant here: bools are not
            :class:`ConductorOutcome` instances, so they land in the
            refusal arm like every other stray value.
    """
    if value is DECLINED or value is TAKEN_RUNNING:
        return value
    if isinstance(value, TakenTerminal):
        return value
    legacy = " (the LEGACY bare-bool contract)" if isinstance(value, bool) else ""
    raise ConductorOutcomeContractError(
        "conductor_router answered "
        f"{value!r}{legacy} for build_id={build_id}, which is outside the "
        "taken-and-terminal vocabulary (DECLINED / TAKEN_RUNNING / "
        "TakenTerminal). Refusing loudly: reading an unknown answer as "
        "'not mine' would run a fix task through the routine autobuild "
        "launch — the silent downgrade the cap law forbids."
    )


def is_mode_c_build(pool: Any, build_id: str, *, log: logging.Logger) -> bool:
    """Return ``True`` iff ``build_id``'s row is a mode-c (fix journey) build.

    The same degrade rail the router's own mode read takes: an unreadable
    pool is NOT evidence that a build wants the fix journey, so a raising
    read answers ``False`` (the routine path) with a loud log rather than
    stranding a routine build on a database hiccup.

    Args:
        pool: The lifecycle persistence facade (``get_build_row``).
        build_id: Build whose mode to read.
        log: The caller's logger, so the message names the caller's seam.
    """
    from forge.lifecycle.modes import BuildMode

    if not build_id:
        return False
    try:
        row = pool.get_build_row(build_id)
    except Exception as exc:  # noqa: BLE001 — degrade to the routine path
        log.error(
            "conductor: mode read raised %s: %s for build_id=%s; treating it "
            "as a routine build (the degrade rail)",
            type(exc).__name__,
            exc,
            build_id,
        )
        return False
    if row is None:
        return False
    return getattr(row, "mode", None) is BuildMode.MODE_C


def finish_mode_c_build(
    pool: Any,
    build_id: str,
    *,
    to_state: Any,
    summary: str,
    what: str,
    log: logging.Logger,
) -> str:
    """Move ``build_id`` to ``to_state`` and return the terminal reason.

    THE one writer that takes a mode-c row terminal (see the module
    docstring). Composed through
    :func:`~forge.lifecycle.state_machine.transition_chain` so
    ``apply_transition`` stays the sole writer of ``builds.status`` (a
    ``QUEUED`` row reaches ``FAILED`` via the legal ``PREPARING`` hop, a
    ``RUNNING`` one reaches ``COMPLETE`` via ``FINALISING``).

    Never raises. Each degraded arm — no row, an already-terminal row, an
    unwritable row — is logged loudly AND named in the returned reason,
    so the event the caller emits says exactly what happened rather than
    claiming a transition that did not land.

    Args:
        pool: The lifecycle persistence facade.
        build_id: The build being finished.
        to_state: Target :class:`~forge.lifecycle.state_machine.BuildState`.
        summary: The ONE-LINE sentence that goes on ``builds.error``
            (multi-line essays belong in the log, not a database column).
            NOT written when ``to_state`` is ``COMPLETE``: the column is
            what ``forge status`` renders as the failure text, and a
            successful row carrying prose there reads as a failure to
            every human and every dashboard. It is still logged.
        what: Short phrase naming the calling seam, for the log.
        log: The caller's logger.

    Returns:
        ``summary``, annotated when the row write degraded.
    """
    from forge.lifecycle.persistence import Build
    from forge.lifecycle.state_machine import BuildState, transition_chain

    terminal = (
        BuildState.COMPLETE,
        BuildState.FAILED,
        BuildState.CANCELLED,
        BuildState.SKIPPED,
    )
    target = getattr(to_state, "value", to_state)
    try:
        row = pool.get_build_row(build_id)
        if row is None:
            log.error(
                "conductor: %s for build_id=%s but no build row exists to "
                "mark %s; the outcome stands but it is recorded only in "
                "this log",
                what,
                build_id,
                target,
            )
            return f"{summary} (no builds row existed to mark {target})"
        current = row.status
        if current in terminal:
            log.warning(
                "conductor: %s for build_id=%s whose row is already terminal "
                "(%s); leaving it alone",
                what,
                build_id,
                current.value,
            )
            return f"{summary} (the row was already terminal: {current.value})"
        fields: dict[str, Any] = (
            {} if to_state is BuildState.COMPLETE else {"error": summary}
        )
        for hop in transition_chain(
            Build(build_id=build_id, status=current), to_state, **fields
        ):
            pool.apply_transition(hop)
        log.info(
            "conductor: build_id=%s marked %s after %s — %s",
            build_id,
            target,
            what,
            summary,
        )
    except Exception as exc:  # noqa: BLE001 — the outcome must still hold
        log.error(
            "conductor: could not mark build_id=%s %s after %s (%s: %s). "
            "The row may be left mid-flight and needs a hand",
            build_id,
            target,
            what,
            type(exc).__name__,
            exc,
        )
        return f"{summary} (the row could NOT be marked {target} and needs a hand)"
    return summary


def fail_mode_c_build(
    pool: Any,
    build_id: str,
    *,
    summary: str,
    what: str,
    log: logging.Logger,
) -> str:
    """Mark ``build_id`` FAILED with ``summary`` and return the terminal reason.

    The refusal-shaped face of :func:`finish_mode_c_build`. Kept as its own
    name because four call sites read as refusals, and "fail this build"
    is what they mean; the FAILED target is not a parameter they should
    have to spell.

    Returns:
        The reason to carry on the :class:`TakenTerminal` — ``summary``,
        annotated when the row write degraded.
    """
    from forge.lifecycle.state_machine import BuildState

    return finish_mode_c_build(
        pool,
        build_id,
        to_state=BuildState.FAILED,
        summary=summary,
        what=what,
        log=log,
    )
