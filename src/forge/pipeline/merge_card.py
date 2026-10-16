"""Keeping a posted merge card true (previews design, 16 October 2026).

ONE RULE: Forge describes each preview card from what its ledger says, and
Jarvis makes the Slack message match. A description is built from three
records — the build's latest offer row, its newest preview lease and any
decision row — plus the card's revision counter
(:mod:`forge.lifecycle.card_revisions`). It travels as the existing
``build-paused`` card message with the optional ``merge_card`` block
(nats-core :class:`~nats_core.events.MergeCard`).

Forge sends a description at once whenever one of the three records changes,
and again for every live card on each five-minute preview sweep. Sending the
same description twice does no harm, and the sweep is what repairs every
failure: a crash after a row was written but before it was sent, a Slack edit
that failed, an update published while Jarvis was down.

WHEN A CARD SHOWS "READY". Only when the latest committed offer row names the
newest preview lease's run and that lease is ready. An amended offer is
written only after its merge-check reading is complete, so this also means a
rebuild's merge check has finished. A lease that is ready but not yet named
by a committed offer is shown as "rebuilding".

CARDS FROM BEFORE THIS RELEASE keep today's behaviour: only offers whose row
carries ``card_words`` (stored from this release on) and a preview record are
described.

This module holds the rules and the ledger transactions; the offer service
(:class:`forge.pipeline.merge_offer.MergeOfferService`) sends, rebuilds and
sweeps, and the merge press records decisions through
:func:`decide_previewed_offer`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from forge.lifecycle.card_revisions import (
    OFFER_DETAILS_KEY,
    OFFER_ROW,
    bump_card_revision,
    card_revision,
    decision_on_record,
    immediate_transaction,
    insert_stage_row,
    latest_offer_details,
    offer_rows,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CARD_WORDS_KEY",
    "CardFacts",
    "CardView",
    "DecisionAttempt",
    "DESCRIBE_DECIDED_FOR",
    "DESCRIBE_OPEN_FOR",
    "EXPIRED_REASON",
    "REBUILD_CAP",
    "REBUILD_NOTE",
    "REBUILD_WANTED_KEY",
    "REBUILDS_KEY",
    "UNFINISHED_REBUILD_REASON",
    "append_offer_row",
    "append_amended_offer",
    "build_paused_for",
    "card_view",
    "cards_to_describe",
    "decide_previewed_offer",
    "describable",
    "read_card_facts",
    "words_of",
]

#: The offer row's field holding the card's own sentences (from this release
#: on). An offer without it is a card from before this release.
CARD_WORDS_KEY: str = "card_words"

#: How many rebuilds an offer has had (amended rows only).
REBUILDS_KEY: str = "preview_rebuilds"

#: When a Rebuild press found no slot it could take; the sweep tries again.
REBUILD_WANTED_KEY: str = "rebuild_wanted_at"

#: The ``notes`` a Rebuild press carries, with ``decision="defer"``.
REBUILD_NOTE: str = "rebuild-preview"

#: A card is rebuilt at most this many times.
REBUILD_CAP: int = 3

#: How long a decided card is still described. Shorter than the 24 hours
#: Jarvis keeps a decided card's record, so Forge stops first and Jarvis never
#: posts a replacement for a card it has already forgotten.
DESCRIBE_DECIDED_FOR: timedelta = timedelta(hours=23)

#: How long an unanswered card is described, from its first offer. Shorter
#: than the 15 days Jarvis keeps any card's record, for the same reason.
DESCRIBE_OPEN_FOR: timedelta = timedelta(days=14)

#: The reason the preview sweep writes when a preview's time is up
#: (``ReviewPreviewService._end_expired``); the card says "expired at …".
EXPIRED_REASON: str = "the preview's time was up"

#: The reason an unfinished rebuild's preview is ended with.
UNFINISHED_REBUILD_REASON: str = "the rebuild did not finish"

#: What a lease's reason ends with while its teardown is unconfirmed.
_NOT_CONFIRMED: str = " (teardown not confirmed yet)"

CardStatus = Literal["ready", "rebuilding", "ended", "failed", "decided"]


def _parse(text: Any) -> datetime | None:
    if isinstance(text, datetime):
        value = text
    elif isinstance(text, str) and text.strip():
        try:
            value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The facts and the view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardFacts:
    """What the ledger says about one card, read in one go."""

    build_id: str
    feature_id: str
    #: The latest committed offer record.
    offer: Mapping[str, Any]
    #: When the card was first offered (the first offer row's time).
    first_offered_at: str
    #: The decision record, or None.
    decision: Mapping[str, Any] | None
    #: When the decision was recorded, or None.
    decided_at: str | None
    #: The build's newest lease (any state), or None.
    newest_lease: Any | None
    #: The lease the latest offer's preview record names, or None.
    named_lease: Any | None
    #: The card's revision counter.
    revision: int
    #: A preparation (a rebuild) for this build is running in this process.
    preparing: bool = False


@dataclass(frozen=True)
class CardView:
    """What the card shows: status, Forge's line, buttons and the record."""

    status: CardStatus
    status_text: str | None
    rebuild_offered: bool
    #: The preview record the card carries (``ReviewEvidence`` as JSON).
    evidence: Mapping[str, Any]
    #: The line about the preview on the card's ``rationale``.
    preview_line: str
    decision: str | None = None
    decided_by: str | None = None


def _reason_of(lease: Any) -> str:
    reason = str(getattr(lease, "reason", "") or "").strip()
    return reason.removesuffix(_NOT_CONFIRMED).strip().rstrip(".")


def _stopped_text(lease: Any, fallback: str) -> str:
    """ "Preview expired at 14:07 UTC." or "Preview stopped: <reason>."."""
    reason = _reason_of(lease) if lease is not None else ""
    if reason == EXPIRED_REASON:
        expiry = _parse(getattr(lease, "app_expires_at", None))
        if expiry is not None:
            return f"Preview expired at {expiry.astimezone(timezone.utc):%H:%M} UTC."
        return "Preview expired."
    reason = reason or fallback
    if reason.startswith("to make room for"):
        return f"Preview stopped {reason}."
    return f"Preview stopped: {reason}."


def _ended(evidence: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """A ready record turned into an ended one: no app link, the reason said."""
    out = dict(evidence)
    out["state"] = "ended"
    out["app_url"] = None
    out["failure_reason"] = reason or "the preview is no longer running"
    return out


def card_view(facts: CardFacts) -> CardView:
    """The card's status, by the design's one rule (see the module docstring)."""
    from forge.deploy.review_preview import is_slots_busy
    from forge.pipeline.merge_offer import card_line_about_the_preview

    offer = facts.offer
    evidence = dict(offer.get("review_evidence") or {})
    named_run = str(evidence.get("run_id") or "")
    rebuilds = int(offer.get(REBUILDS_KEY) or 0)
    capped = rebuilds >= REBUILD_CAP
    cap_words = " Rebuilt three times; reject and run the feature again."
    newest = facts.newest_lease
    named = facts.named_lease
    ready_record = evidence.get("state") == "ready"

    def without_app(reason: str) -> dict[str, Any]:
        return _ended(evidence, reason) if ready_record else evidence

    if facts.decision is not None:
        decision = str(facts.decision.get("decision") or "")
        who = str(facts.decision.get("decided_by") or "").strip() or "someone"
        word = "Approved" if decision == "approve" else "Rejected"
        still_up = named is not None and getattr(named, "state", None) == "ready"
        text = f"{word} by {who}."
        return CardView(
            status="decided",
            status_text=text,
            rebuild_offered=False,
            evidence=evidence
            if still_up
            else without_app(_reason_of(named) or "the card was decided"),
            preview_line=text,
            decision="approve" if decision == "approve" else "reject",
            decided_by=who,
        )

    def rebuilding(text: str) -> CardView:
        return CardView(
            status="rebuilding",
            status_text=text,
            rebuild_offered=False,
            evidence=without_app("the preview is being rebuilt"),
            preview_line=text,
        )

    newest_state = getattr(newest, "state", None)
    if facts.preparing or newest_state == "preparing":
        return rebuilding("Rebuilding the preview for the same build.")
    if newest_state == "ready":
        if getattr(newest, "run_id", None) == named_run and ready_record:
            return CardView(
                status="ready",
                status_text=None,
                rebuild_offered=False,
                evidence=evidence,
                preview_line=card_line_about_the_preview(_Shape(evidence)),
            )
        return rebuilding("Rebuilding the preview for the same build.")

    def ended_view(text: str, reason: str, *, status: CardStatus = "ended") -> CardView:
        if capped:
            text = f"{text}{cap_words}"
        return CardView(
            status=status,
            status_text=text,
            rebuild_offered=not capped,
            evidence=without_app(reason),
            preview_line=text,
        )

    wanted = _parse(offer.get(REBUILD_WANTED_KEY))
    named_created = _parse(evidence.get("created_at"))
    newest_created = _parse(getattr(newest, "created_at", None))
    if (
        newest is not None
        and getattr(newest, "run_id", None) != named_run
        and newest_created is not None
        and (named_created is None or newest_created > named_created)
        and (wanted is None or newest_created > wanted)
    ):
        # A preparation the offer never named — a rebuild (or a retry) cut
        # short — that has since ended. Its reason is the card's.
        return ended_view(
            _stopped_text(newest, UNFINISHED_REBUILD_REASON),
            _reason_of(newest) or UNFINISHED_REBUILD_REASON,
        )
    if wanted is not None:
        return rebuilding("Waiting for a preview slot; it will start when one frees.")
    if not ready_record:
        reason = str(evidence.get("failure_reason") or "").strip().rstrip(".")
        if is_slots_busy(reason):
            text = f"Preview not started: {reason}."
            if not capped:
                text = f"{text} Press Rebuild preview when you want to look."
            line = text
        else:
            text = f"Preview could not be prepared: {reason or 'no reason was given'}."
            line = card_line_about_the_preview(_Shape(evidence)) if not capped else text
        if capped:
            text = f"{text}{cap_words}"
            line = text
        return CardView(
            status="failed",
            status_text=text,
            rebuild_offered=not capped,
            evidence=evidence,
            preview_line=line,
        )
    # A ready record whose preview is no longer standing.
    text = _stopped_text(named, "the preview is no longer running")
    return ended_view(text, _reason_of(named) or "the preview is no longer running")


@dataclass(frozen=True)
class _Shape:
    """Lets :func:`card_line_about_the_preview` read a JSON record."""

    record: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        return self.record.get(name)


def words_of(offer: Mapping[str, Any]) -> str:
    """The card's own sentences: its words and its merge-check line."""
    from forge.cli._serve_gate_activation import card_line_about_the_merge_check
    from forge.pipeline.merge_check_reading import card_body

    words = str(offer.get(CARD_WORDS_KEY) or "")
    reading = offer.get("merge_check_reading")
    if isinstance(reading, Mapping):
        line = card_line_about_the_merge_check(reading)
        if line:
            words = f"{words}\n{line}"
    return card_body(words)


def rationale_of(offer: Mapping[str, Any], view: CardView) -> str:
    """The card's ``rationale``, laid out exactly as the first card's was:
    its words, the line about the preview, then the merge-check line."""
    from forge.cli._serve_gate_activation import card_line_about_the_merge_check
    from forge.pipeline.merge_check_reading import card_body, shown_sentence

    text = str(offer.get(CARD_WORDS_KEY) or "")
    text = f"{text}\n{shown_sentence(view.preview_line)}"
    reading = offer.get("merge_check_reading")
    if isinstance(reading, Mapping):
        line = card_line_about_the_merge_check(reading)
        if line:
            text = f"{text}\n{line}"
    return card_body(text)


def build_paused_for(facts: CardFacts, view: CardView | None = None) -> Any:
    """The ``build-paused`` card message describing ``facts``.

    Its ``build_id`` is the synthetic ``merge-<feature>`` id the buttons have
    always carried; the real build id travels only in the block, for the
    identity check.
    """
    from nats_core.events import BuildPausedPayload, MergeCard, ReviewEvidence

    from forge.pipeline.merge_offer import MERGE_OFFER_STAGE_LABEL

    view = view or card_view(facts)
    offer = facts.offer
    block = MergeCard(
        request_id=str(offer.get("request_id") or ""),
        build_id=facts.build_id,
        correlation_id=str(offer.get("correlation_id") or ""),
        approval_subject=str(offer.get("approval_subject") or ""),
        candidate_sha=str(offer.get("candidate_sha") or ""),
        status=view.status,
        status_text=view.status_text,
        rebuild_offered=view.rebuild_offered,
        revision=facts.revision,
        words=words_of(offer),
        decision=view.decision,  # type: ignore[arg-type]
        decided_by=view.decided_by,
    )
    return BuildPausedPayload(
        feature_id=facts.feature_id,
        build_id=f"merge-{facts.feature_id}",
        stage_label=MERGE_OFFER_STAGE_LABEL,
        gate_mode="MANDATORY_HUMAN_APPROVAL",
        coach_score=None,
        rationale=rationale_of(offer, view),
        approval_subject=block.approval_subject,
        paused_at=facts.first_offered_at,
        correlation_id=block.correlation_id,
        review_evidence=ReviewEvidence.model_validate(dict(view.evidence)),
        merge_card=block,
    )


# ---------------------------------------------------------------------------
# Reading the ledger
# ---------------------------------------------------------------------------


def describable(offer: Mapping[str, Any] | None) -> bool:
    """Is this an offer this release describes (has words and a preview)?"""
    return (
        isinstance(offer, Mapping)
        and CARD_WORDS_KEY in offer
        and isinstance(offer.get("review_evidence"), Mapping)
    )


def read_card_facts(
    cx: sqlite3.Connection,
    build_id: str,
    *,
    leases: Any | None,
    preparing: bool = False,
) -> CardFacts | None:
    """Everything one description needs, or None for a card not described.

    ``leases`` answers ``newest_lease(build_id)`` and ``lease_for_run(run_id)``
    (the review-preview service); without them no lease is known.
    """
    rows = offer_rows(cx, build_id)
    if not rows:
        return None
    offer = rows[-1][2]
    if not describable(offer):
        return None
    decided = decision_on_record(cx, build_id)
    run_id = str((offer.get("review_evidence") or {}).get("run_id") or "")
    newest = named = None
    ask_newest = getattr(leases, "newest_lease", None)
    ask_run = getattr(leases, "lease_for_run", None)
    if callable(ask_newest) and callable(ask_run):
        newest = ask_newest(build_id)
        named = ask_run(run_id) if run_id else None
    else:
        # No lease table to ask (a service that could not start, or a
        # stand-in): the offer's own record is the only word on the preview.
        evidence = offer.get("review_evidence") or {}
        named = newest = _RecordedLease(
            run_id=run_id,
            state="ready" if evidence.get("state") == "ready" else "failed",
            reason=evidence.get("failure_reason"),
            created_at=evidence.get("created_at"),
            app_expires_at=evidence.get("app_expires_at"),
        )
    return CardFacts(
        build_id=build_id,
        feature_id=str(offer.get("feature_id") or ""),
        offer=offer,
        first_offered_at=rows[0][1],
        decision=decided[0] if decided is not None else None,
        decided_at=decided[2] if decided is not None else None,
        newest_lease=newest,
        named_lease=named,
        revision=card_revision(cx, build_id),
        preparing=preparing,
    )


@dataclass(frozen=True)
class _RecordedLease:
    """A lease as an offer's own preview record describes it."""

    run_id: str
    state: str
    reason: Any
    created_at: Any
    app_expires_at: Any


def cards_to_describe(cx: sqlite3.Connection, now: datetime) -> list[str]:
    """Builds whose card the sweep describes: unanswered, or decided lately.

    Only offers from this release on (they carry ``card_words``), unanswered
    for at most :data:`DESCRIBE_OPEN_FOR` and decided at most
    :data:`DESCRIBE_DECIDED_FOR` ago.
    """
    found: list[str] = []
    rows = cx.execute(
        "SELECT build_id, MIN(started_at) FROM stage_log "
        "WHERE target_identifier = ? AND details_json LIKE ? "
        "GROUP BY build_id ORDER BY MIN(started_at)",
        (OFFER_ROW, f'%"{CARD_WORDS_KEY}"%'),
    ).fetchall()
    for build_id, first in rows:
        offered = _parse(first)
        if offered is None or now - offered > DESCRIBE_OPEN_FOR:
            continue
        decided = decision_on_record(cx, str(build_id))
        if decided is not None:
            when = _parse(decided[2])
            if when is None or now - when > DESCRIBE_DECIDED_FOR:
                continue
        found.append(str(build_id))
    return found


# ---------------------------------------------------------------------------
# Writing: offer rows and decisions, each with the card's counter
# ---------------------------------------------------------------------------


def _offer_entry(build_id: str, record: Mapping[str, Any], now: datetime) -> Any:
    from forge.lifecycle.persistence import StageLogEntry
    from forge.pipeline.merge_offer import MERGE_OFFER_STAGE_LABEL

    return StageLogEntry(
        build_id=build_id,
        stage_label=MERGE_OFFER_STAGE_LABEL,
        target_kind="local_tool",
        target_identifier=OFFER_ROW,
        status="GATED",
        gate_mode="MANDATORY_HUMAN_APPROVAL",
        started_at=now,
        completed_at=now,
        duration_secs=0.0,
        details={OFFER_DETAILS_KEY: dict(record)},
    )


def append_offer_row(
    cx: sqlite3.Connection, *, build_id: str, record: Mapping[str, Any], now: datetime
) -> int:
    """Write the first offer row and raise the card's counter, in one transaction."""
    with immediate_transaction(cx):
        row_id = insert_stage_row(cx, _offer_entry(build_id, record, now))
        bump_card_revision(cx, build_id, now)
    return row_id


def append_amended_offer(
    cx: sqlite3.Connection,
    *,
    build_id: str,
    now: datetime,
    amend: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    """Append an amended offer row unless a decision is on record.

    One write transaction with nothing that waits in it: the decision check,
    the copy of the latest offer with ``amend`` applied, and the card's
    counter. A decision written first makes this write nothing (False); a
    decision written after comes after the amended row. Either way a decided
    card is never reopened, and the decision's row id is later than every
    offer row's, as the idle checks require.
    """
    with immediate_transaction(cx):
        if decision_on_record(cx, build_id) is not None:
            return False
        latest = latest_offer_details(cx, build_id)
        if latest is None:
            return False
        record = amend(dict(latest))
        insert_stage_row(cx, _offer_entry(build_id, record, now))
        bump_card_revision(cx, build_id, now)
    return True


@dataclass(frozen=True)
class DecisionAttempt:
    """What :func:`decide_previewed_offer` did."""

    #: ``recorded``, ``refused`` (approval refused, nothing recorded),
    #: ``already-decided``, ``no-offer`` or ``changed`` (another request id).
    outcome: str
    #: The latest committed offer record, read inside the transaction.
    offer: dict[str, Any] | None = None
    refusal: str | None = None
    definitive: bool = False


def decide_previewed_offer(
    cx: sqlite3.Connection,
    *,
    build_id: str,
    request_id: str,
    decision: str,
    now: datetime,
    decision_entry: Callable[[dict[str, Any]], Any],
    approval_check: Callable[[dict[str, Any]], tuple[str | None, bool]] | None,
) -> DecisionAttempt:
    """Check a previewed offer and record its decision in ONE write transaction.

    Inside it: no decision may be on record yet; the latest committed offer
    must carry ``request_id``; an approval must pass ``approval_check`` (the
    preview is still ready, and so on). A refused approval raises the card's
    counter so Forge sends a fresh "undecided" description and the card gets
    its buttons back; a recorded decision raises it too. Nothing in here
    waits, so a slot-taking transaction for the same preview either comes
    first (and the approval is refused, the preview is ending) or after (and
    the preview is no longer eligible).
    """
    from forge.lifecycle.card_revisions import decision_on_record as _decided

    with immediate_transaction(cx):
        if _decided(cx, build_id) is not None:
            return DecisionAttempt(outcome="already-decided")
        offer = latest_offer_details(cx, build_id)
        if offer is None:
            return DecisionAttempt(outcome="no-offer")
        if offer.get("request_id") != request_id:
            return DecisionAttempt(outcome="changed", offer=offer)
        if decision == "approve" and approval_check is not None:
            refusal, definitive = approval_check(offer)
            if refusal is not None:
                bump_card_revision(cx, build_id, now)
                return DecisionAttempt(
                    outcome="refused",
                    offer=offer,
                    refusal=refusal,
                    definitive=definitive,
                )
        insert_stage_row(cx, decision_entry(offer))
        bump_card_revision(cx, build_id, now)
    return DecisionAttempt(outcome="recorded", offer=offer)
