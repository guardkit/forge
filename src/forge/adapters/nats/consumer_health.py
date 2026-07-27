"""Ack-slot health inspection and phantom-ack cure for the Forge pull consumer.

Background — the phantom-ack wedge (FEAT-PAC):

The daemon's pull consumer (stream ``PIPELINE``, durable ``forge-serve`` by
default) runs with ``max_ack_pending=1`` — deliberately strict serialization
(ADR-ARCH-014). The ack for an accepted build is DEFERRED to the terminal
publish (ADR-SP-013), so a daemon death in that window strands the single
ack-pending slot. Pull consumers redeliver only on pulls, so all dispatch then
jams silently. When the stranded message is later PURGED from the stream, the
consumer keeps ``num_ack_pending == 1`` forever against a message that no longer
exists — a *phantom ack* that no ack can ever release. Neither boot reconcile
sees it (both read live/SQLite state, not the JetStream ack floor), so the wedge
survived two restarts and 25h live before manual broker surgery cleared it.

The discriminator (the load-bearing idea):

With ``max_ack_pending=1`` the ack-pending set is a singleton — and the single
outstanding message is exactly the LAST-DELIVERED one, so its stream sequence is
recoverable from ``consumer_info`` alone::

    pending_seq = delivered.stream_seq

(:class:`~nats.js.api.ConsumerInfo.delivered` is an Optional
:class:`~nats.js.api.SequenceInfo`; guard ``None``. NOT ``ack_floor + 1``: on a
multi-subject stream the sequences between the consumer's ack floor and its
delivered watermark belong to other subjects' consumed messages, so ``+1`` can
point at a legitimately-deleted foreign message and misclassify a real held ack
as a phantom — live-proven 2026-07-27 against a gate-paused build.) Then a single honest probe
tells legitimate from phantom:

- ``js.get_msg('PIPELINE', seq=pending_seq)`` **succeeds** → the held message
  still exists → a LEGITIMATE long-held ack (an in-flight or redeliverable
  build). NEVER cure this: deleting the durable would drop its position and,
  under ``DeliverPolicy.ALL``, replay history.
- ``get_msg`` raises :class:`~nats.js.errors.NotFoundError` → the message is GONE
  (purge or ``delete_msg`` hole) → **PHANTOM**: no ack can ever release the slot.
  Cure by deleting the consumer.

The idle signature alone (``ack_pending>0 + waiting>0 + no deliveries for N
min``) is IDENTICAL for a legitimate hours-long build and the phantom, so it may
alarm but must NEVER auto-cure. ``get_msg`` is the only honest discriminator,
and it is robust where a ``first_seq`` floor comparison would miss a single
``delete_msg`` hole.

Absence-of-failure discipline: any API error while inspecting yields ``unknown``
(logged WARNING), never ``phantom`` — an inspection failure must never be
mistaken for a wedge, and ``unknown`` never triggers a cure.

This is a pure broker-state module: no SQLite, no ledger writes, no envelope
publishes — so there is no ledger-lie surface by construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

AckSlotStatus = Literal["healthy", "held", "phantom", "unknown", "absent"]


@dataclass
class AckSlotReport:
    """The outcome of inspecting the consumer's single ack-pending slot.

    Attributes:
        status: One of ``"healthy"`` (slot free — nothing ack-pending),
            ``"held"`` (a real message occupies the slot — a legitimate
            long-held ack), ``"phantom"`` (the ack-pending message is gone from
            the stream — the wedge; safe to cure), ``"unknown"`` (the
            inspection could not reach a verdict — an API error or a missing
            ack-floor; never cured), or ``"absent"`` (the durable consumer does
            not exist — no ack slot at all; normal pre-first-attach and right
            after a cure deleted it).
        pending_seq: The stream sequence of the ack-pending message
            (``delivered.stream_seq`` — the singleton outstanding message is the
            last-delivered one under ``max_ack_pending=1``), or ``None`` when
            the slot is free or the sequence could not be derived.
        num_ack_pending: ``ConsumerInfo.num_ack_pending`` (``0``/``None`` ⇒ free).
        num_waiting: ``ConsumerInfo.num_waiting`` (a parked pull is idle-good).
        num_pending: ``ConsumerInfo.num_pending`` (undelivered stream backlog).
        detail: A plain-language, operator-readable one-line explanation.
    """

    status: AckSlotStatus
    pending_seq: int | None
    num_ack_pending: int
    num_waiting: int
    num_pending: int
    detail: str


async def inspect_ack_slot(js, stream: str, durable: str) -> AckSlotReport:
    """Inspect the durable's single ack-pending slot and classify it.

    Reads ``consumer_info`` once, then — only when a slot is occupied — derives
    the pending sequence and probes the stream with a single ``get_msg`` to tell
    a legitimate held message from a phantom. Follows absence-of-failure
    discipline throughout: any API error yields ``unknown`` (logged), never a
    false ``phantom``, so an inspection hiccup can never trigger a cure.

    Args:
        js: A JetStream context (``nats.js.JetStreamContext``). Only
            ``consumer_info`` and ``get_msg`` are called; no connection is
            opened here.
        stream: The JetStream stream name (e.g. ``"PIPELINE"``).
        durable: The durable consumer name (e.g. ``"forge-serve"``).

    Returns:
        An :class:`AckSlotReport`. Never raises for broker/API errors — those
        are folded into an ``"unknown"`` report.
    """
    # 1. Read consumer state. A missing consumer is its own honest verdict:
    #    no consumer ⇒ no ack slot exists at all ⇒ trivially no wedge. This is
    #    the normal state on a first-ever boot (before the daemon's
    #    bind-or-create attach) and immediately after a phantom cure deleted
    #    the durable — the post-cure re-inspect MUST land here, not in the
    #    generic error branch, or a successful cure could never verify.
    try:
        info = await js.consumer_info(stream, durable)
    except NotFoundError:
        return AckSlotReport(
            status="absent",
            pending_seq=None,
            num_ack_pending=0,
            num_waiting=0,
            num_pending=0,
            detail=(
                f"consumer '{durable}' does not exist on stream '{stream}' — "
                "no ack slot exists (normal before the daemon's first attach, "
                "or right after a cure deleted it; the daemon recreates it "
                "bind-or-create on attach)"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — absence-of-failure: never claim phantom
        logger.warning(
            "ack-slot inspect: consumer_info(%s, %s) failed (%s: %s); "
            "reporting status=unknown — no cure will be attempted",
            stream,
            durable,
            type(exc).__name__,
            exc,
        )
        return AckSlotReport(
            status="unknown",
            pending_seq=None,
            num_ack_pending=0,
            num_waiting=0,
            num_pending=0,
            detail=(
                f"could not read consumer '{durable}' on stream '{stream}': "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # All ConsumerInfo counters are Optional — treat missing as 0.
    num_ack_pending = info.num_ack_pending or 0
    num_waiting = info.num_waiting or 0
    num_pending = info.num_pending or 0

    # 2. Free slot ⇒ healthy. Nothing is ack-pending, so there is nothing to probe.
    if num_ack_pending == 0:
        return AckSlotReport(
            status="healthy",
            pending_seq=None,
            num_ack_pending=0,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' has no ack-pending message "
                f"(num_pending={num_pending}, num_waiting={num_waiting}) — "
                "the ack slot is free"
            ),
        )

    # 3. Slot occupied ⇒ derive the pending sequence from the DELIVERED
    #    watermark. With max_ack_pending=1 the single outstanding message is
    #    exactly the last-delivered one, so pending_seq =
    #    delivered.stream_seq. NOT ack_floor.stream_seq + 1: on a
    #    MULTI-SUBJECT stream the sequences between the consumer's ack floor
    #    and its delivered watermark belong to OTHER subjects (other
    #    consumers' consumed-and-removed messages), so ack_floor+1 can point
    #    at a legitimately-deleted foreign message and misclassify a REAL
    #    held ack as a phantom. Live-proven 2026-07-27: a gate-paused build
    #    held seq 653 while ack_floor+1 = 649 was a consumed jarvis-side
    #    message already gone from the stream — the +1 formula would have
    #    cured (deleted the consumer under) a legitimately paused build.
    #    delivered is Optional; without it we cannot name the sequence to
    #    probe, so we cannot honestly classify — report unknown, never
    #    phantom.
    delivered = info.delivered
    if delivered is None or delivered.stream_seq is None:
        logger.warning(
            "ack-slot inspect: consumer '%s' on '%s' has %d ack-pending but no "
            "delivered.stream_seq; reporting status=unknown — cannot name the "
            "held sequence, so no cure will be attempted",
            durable,
            stream,
            num_ack_pending,
        )
        return AckSlotReport(
            status="unknown",
            pending_seq=None,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' has {num_ack_pending} ack-pending but "
                "no ack-floor sequence to identify the held message"
            ),
        )

    pending_seq = delivered.stream_seq

    # 4. Probe the stream for the held message. Present ⇒ legitimate hold;
    #    NotFoundError ⇒ phantom; any other error ⇒ unknown (never phantom).
    try:
        await js.get_msg(stream, seq=pending_seq)
    except NotFoundError:
        logger.error(
            "ack-slot inspect: PHANTOM ack on consumer '%s' (stream '%s') — "
            "the ack-pending message at seq=%d is gone from the stream; the "
            "single ack slot is wedged and no ack can release it",
            durable,
            stream,
            pending_seq,
        )
        return AckSlotReport(
            status="phantom",
            pending_seq=pending_seq,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' holds the ack slot for stream sequence "
                f"{pending_seq}, but that message no longer exists in stream "
                f"'{stream}' (purged or deleted) — this is a phantom ack and "
                "the dispatch queue is wedged; safe to cure by deleting the "
                "consumer"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — absence-of-failure: never claim phantom
        logger.warning(
            "ack-slot inspect: get_msg(%s, seq=%d) failed (%s: %s); reporting "
            "status=unknown — an API error is not a phantom, so no cure will "
            "be attempted",
            stream,
            pending_seq,
            type(exc).__name__,
            exc,
        )
        return AckSlotReport(
            status="unknown",
            pending_seq=pending_seq,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' holds the ack slot for stream sequence "
                f"{pending_seq}, but probing stream '{stream}' for that message "
                f"failed ({type(exc).__name__}: {exc}) — cannot confirm whether "
                "it is a legitimate hold or a phantom"
            ),
        )

    # Message present ⇒ a real, legitimately held ack. Never cure this.
    return AckSlotReport(
        status="held",
        pending_seq=pending_seq,
        num_ack_pending=num_ack_pending,
        num_waiting=num_waiting,
        num_pending=num_pending,
        detail=(
            f"consumer '{durable}' holds the ack slot for stream sequence "
            f"{pending_seq}, and that message still exists in stream "
            f"'{stream}' — this is a legitimate in-flight or redeliverable "
            "build; leave it alone"
        ),
    )


async def cure_phantom(js, stream: str, durable: str) -> bool:
    """Cure a phantom ack by deleting the wedged durable consumer.

    The cure is ``delete_consumer`` **only** — deliberately no recreate here.
    The daemon recreates the durable itself when it re-attaches: nats-py's
    ``pull_subscribe`` is bind-or-create, so ``_serve_daemon._attach_consumer``
    re-establishes the consumer with the correct config on the next boot. Adding
    a recreate in this module would duplicate that ownership and risk two
    definitions of the consumer config drifting apart.

    Caller contract: only invoke this after :func:`inspect_ack_slot` has
    returned ``status == "phantom"``, and — in v1 — only at boot, before the
    live ``PullSubscription`` exists. Deleting the durable underneath a live
    subscription would invalidate it mid-fetch (see the FEAT-PAC runtime
    watchdog scope: alarm-only, no mid-run cure).

    Args:
        js: A JetStream context. Only ``delete_consumer`` is called.
        stream: The JetStream stream name (e.g. ``"PIPELINE"``).
        durable: The durable consumer name to delete (e.g. ``"forge-serve"``).

    Returns:
        ``True`` if the consumer was deleted; ``False`` on any error. Never
        raises — a cure failure is logged (WARNING) and reported as ``False`` so
        the caller can carry on and re-inspect.
    """
    try:
        await js.delete_consumer(stream, durable)
    except Exception as exc:  # noqa: BLE001 — cure failure must never propagate
        logger.warning(
            "phantom cure: delete_consumer(%s, %s) failed (%s: %s); the wedge "
            "was NOT cleared — an operator may need to delete the consumer "
            "manually",
            stream,
            durable,
            type(exc).__name__,
            exc,
        )
        return False

    logger.warning(
        "phantom cure: deleted wedged consumer '%s' on stream '%s'; the daemon "
        "will recreate it on attach (bind-or-create) with the ack slot free",
        durable,
        stream,
    )
    return True
