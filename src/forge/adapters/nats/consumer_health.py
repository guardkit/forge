"""Ack-slot health inspection and phantom-ack cure for the Forge pull consumer.

Background — the phantom-ack wedge (FEAT-PAC):

The daemon's pull consumer (stream ``PIPELINE``, durable ``forge-serve`` by
default) runs with ``max_ack_pending`` set to the configured build limit
(``pipeline.max_concurrent_builds``, default 1 — strict one-at-a-time, as
ADR-ARCH-014 had it). The ack for an accepted build is DEFERRED to the
terminal publish (ADR-SP-013), so a daemon death in that window strands an
ack-pending place. Pull consumers redeliver only on pulls, so dispatch can
then jam silently. When a stranded message is later PURGED from the stream,
some broker versions keep counting it as ack-pending forever against a
message that no longer exists — a *phantom ack* that no ack can ever release.
Neither boot reconcile sees it (both read live/SQLite state, not the
JetStream ack floor), so the wedge survived two restarts and 25h live before
manual broker surgery cleared it. (nats-server 2.11 clears the outstanding
ack itself when the message is deleted or purged; 2.10 does not after
``delete_msg``.)

The discriminator (the load-bearing idea):

Every outstanding message was delivered after the consumer's ack floor and no
later than its delivered watermark, so its stream sequence lies in::

    ack_floor.stream_seq < seq <= delivered.stream_seq

(both are Optional :class:`~nats.js.api.SequenceInfo` on
:class:`~nats.js.api.ConsumerInfo`.) The range also holds other subjects'
messages — the stream is multi-subject, and a gate-paused build was once
nearly misread as a phantom because ``ack_floor + 1`` pointed at a consumed
jarvis-side message (live-proven 2026-07-27). So a single honest probe asks
the stream for the first message on the consumer's own filter subject
(``pipeline.build-queued.*``) at or after ``ack_floor + 1`` — JetStream's
"next by subject" get:

- a build message is found inside the range → at least one outstanding build
  still exists → ``held``. NEVER cure: deleting the durable would drop real
  held builds and, under ``DeliverPolicy.ALL``, replay history.
- nothing is found, or only a message after the delivered watermark (waiting
  work, not outstanding) → every outstanding message is GONE → **PHANTOM**:
  no ack can ever release those places. Cure by deleting the consumer.

With one outstanding message (the default limit) this gives exactly the old
single-slot answer: the only build message in the range is the last-delivered
one. Known limit with several outstanding: if one of them is a phantom and
the others are real, the probe finds a real one and reports ``held``; the
phantom cannot be singled out, so that one place stays lost until it is
cleared by hand or every real build finishes. The watchdog shows the
outstanding count against the configured limit so the loss is visible.
Already-acknowledged build messages that the stream still keeps also read as
``held`` — the safe direction (never a false phantom).

The idle signature alone (``ack_pending>0 + waiting>0 + no deliveries for N
min``) is IDENTICAL for a legitimate hours-long build and the phantom, so it may
alarm but must NEVER auto-cure. The stream probe is the only honest
discriminator.

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

#: The build consumer's filter subject, used when the consumer's own config
#: does not name one. Matches ``forge.cli._serve_daemon.BUILD_QUEUED_SUBJECT_FILTER``.
DEFAULT_BUILD_SUBJECT_FILTER = "pipeline.build-queued.*"

AckSlotStatus = Literal["healthy", "held", "phantom", "unknown", "absent"]


@dataclass
class AckSlotReport:
    """The outcome of inspecting the consumer's ack-pending build places.

    Attributes:
        status: One of ``"healthy"`` (nothing ack-pending — every place
            free), ``"held"`` (at least one outstanding build message still
            exists — a legitimate long-held ack), ``"phantom"`` (every
            ack-pending message is gone from the stream — the wedge; safe to
            cure), ``"unknown"`` (the inspection could not reach a verdict —
            an API error or a missing delivered position; never cured), or
            ``"absent"`` (the durable consumer does
            not exist — no ack slot at all; normal pre-first-attach and right
            after a cure deleted it).
        pending_seq: For ``held``, the stream sequence of the outstanding
            build message the probe found (with one outstanding message, the
            last-delivered one). For ``phantom`` and probe errors, the
            delivered watermark (``delivered.stream_seq``). ``None`` when
            nothing is outstanding or the sequence could not be derived.
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
    """Inspect the durable's ack-pending build places and classify them.

    Reads ``consumer_info`` once, then — only when something is ack-pending —
    probes the stream once with a "next by subject" ``get_msg`` from
    ``ack_floor + 1`` to tell legitimately held builds from phantoms. Follows absence-of-failure
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

    # 3. Something is outstanding ⇒ name the range it must lie in. Every
    #    outstanding message was delivered after the ack floor and no later
    #    than the delivered watermark. Without the watermark we cannot bound
    #    the range, so we cannot honestly classify — report unknown, never
    #    phantom. A missing ack floor only widens the search (start at 1),
    #    which can only turn a phantom into "held", never the reverse.
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

    delivered_seq = delivered.stream_seq
    ack_floor = getattr(info, "ack_floor", None)
    floor_seq = getattr(ack_floor, "stream_seq", None) or 0
    start_seq = floor_seq + 1
    consumer_config = getattr(info, "config", None)
    subject_filter = getattr(consumer_config, "filter_subject", None)
    if not isinstance(subject_filter, str) or not subject_filter:
        subject_filter = DEFAULT_BUILD_SUBJECT_FILTER
    outstanding = (
        f"{num_ack_pending} outstanding (stream sequences "
        f"{start_seq}..{delivered_seq})"
    )
    if start_seq > delivered_seq:
        # The broker says something is outstanding yet its ack floor is at or
        # past everything delivered, so the range is empty. Fall back to the
        # single probe at the delivered position — exactly the one-place check
        # this module made before several places existed.
        return await _probe_delivered(
            js,
            stream,
            durable,
            delivered_seq,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
        )

    # 4. Probe the stream for the first build message from the ack floor on.
    #    Found inside the range ⇒ held; NotFoundError, or the first one lies
    #    beyond the delivered watermark ⇒ phantom; any other error ⇒ unknown
    #    (never phantom).
    try:
        found = await js.get_msg(
            stream, seq=start_seq, subject=subject_filter, next=True
        )
    except NotFoundError:
        found = None
    except Exception as exc:  # noqa: BLE001 — absence-of-failure: never claim phantom
        logger.warning(
            "ack-slot inspect: get_msg(%s, next %s from seq=%d) failed (%s: %s); "
            "reporting status=unknown — an API error is not a phantom, so no "
            "cure will be attempted",
            stream,
            subject_filter,
            start_seq,
            type(exc).__name__,
            exc,
        )
        return AckSlotReport(
            status="unknown",
            pending_seq=delivered_seq,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' has {outstanding}, but probing stream "
                f"'{stream}' for those build messages failed "
                f"({type(exc).__name__}: {exc}) — cannot confirm whether they "
                "are legitimate holds or phantoms"
            ),
        )

    found_seq = getattr(found, "seq", None) if found is not None else None
    if found is not None and not isinstance(found_seq, int):
        # A message came back but its sequence is unreadable: it exists, so
        # take the safe reading (held) and name the watermark.
        found_seq = delivered_seq

    if found_seq is None or found_seq > delivered_seq:
        logger.error(
            "ack-slot inspect: PHANTOM ack on consumer '%s' (stream '%s') — "
            "%s, and no build message in that range still exists in the "
            "stream; those places are wedged and no ack can release them",
            durable,
            stream,
            outstanding,
        )
        return AckSlotReport(
            status="phantom",
            pending_seq=delivered_seq,
            num_ack_pending=num_ack_pending,
            num_waiting=num_waiting,
            num_pending=num_pending,
            detail=(
                f"consumer '{durable}' has {outstanding}, but none of those "
                f"build messages exists in stream '{stream}' any more (purged "
                "or deleted) — this is a phantom ack and dispatch is wedged; "
                "safe to cure by deleting the consumer"
            ),
        )

    # A build message is still present in the range ⇒ at least one real,
    # legitimately held build. Never cure this.
    return AckSlotReport(
        status="held",
        pending_seq=found_seq,
        num_ack_pending=num_ack_pending,
        num_waiting=num_waiting,
        num_pending=num_pending,
        detail=(
            f"consumer '{durable}' has {outstanding}; the build message at "
            f"sequence {found_seq} still exists in stream '{stream}' — a "
            "legitimate in-flight or redeliverable build; leave it alone"
        ),
    )


async def _probe_delivered(
    js,
    stream: str,
    durable: str,
    pending_seq: int,
    *,
    num_ack_pending: int,
    num_waiting: int,
    num_pending: int,
) -> AckSlotReport:
    """Classify by probing the single delivered sequence (the one-place check).

    Present ⇒ ``held``; :class:`NotFoundError` ⇒ ``phantom``; any other error
    ⇒ ``unknown``. Used only when the ack floor and delivered position leave
    no range to search.
    """
    try:
        await js.get_msg(stream, seq=pending_seq)
    except NotFoundError:
        logger.error(
            "ack-slot inspect: PHANTOM ack on consumer '%s' (stream '%s') — "
            "the ack-pending message at seq=%d is gone from the stream",
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
                "dispatch is wedged; safe to cure by deleting the consumer"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — absence-of-failure: never claim phantom
        logger.warning(
            "ack-slot inspect: get_msg(%s, seq=%d) failed (%s: %s); reporting "
            "status=unknown — no cure will be attempted",
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
                f"failed ({type(exc).__name__}: {exc})"
            ),
        )
    return AckSlotReport(
        status="held",
        pending_seq=pending_seq,
        num_ack_pending=num_ack_pending,
        num_waiting=num_waiting,
        num_pending=num_pending,
        detail=(
            f"consumer '{durable}' holds the ack slot for stream sequence "
            f"{pending_seq}, and that message still exists in stream "
            f"'{stream}' — a legitimate in-flight or redeliverable build; "
            "leave it alone"
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
