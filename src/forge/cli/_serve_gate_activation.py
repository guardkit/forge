"""Daemon-side pre-dispatch approval gate (TASK-GATE-D659, Wave 2 / plan §D1).

This module holds the **first production call site of** ``gate_check``.
:func:`maybe_gate_build` runs inside the daemon's ``dispatch_build`` flow —
after ``record_pending_build`` mints the ``build_id`` but **before** any
bridge observer exists and before the sidecar run is launched (the R1
observer-lifecycle repair). Approve → the caller registers the observer and
launches; reject / expiry / hard-stop → terminal before any runner exists.

Honesty posture (ADR-ARCH-019 / ADR-ARCH-026, plan "Gate mechanics"): the
gate runs against :mod:`forge.gating.degraded`'s empty adjustments/rules
readers plus :func:`degraded_dispatch_gate_model`, which returns a static
``MANDATORY_HUMAN_APPROVAL`` decision. Priors are the exception: every
activation path reads ``parts.priors_reader`` (composed env-gated at serve
boot — the fleet-memory reader when memory is ON, ``EmptyPriorsReader``
otherwise), and retrieved priors ride the decision as *evidence* only.
Consequence: **every dispatched build pauses for phone approval** until
evidence-based gating lands (DF-009 "v1 never auto-approves" ratchet) —
exactly what JNB-107 needs.

Envelope contract (plan §D2): :class:`_MirroredApprovalPublisher` wraps the
publish step of :func:`forge.gating.wrappers._atomic_pause_and_publish` so the
AGENTS ``agents.approval.forge.{build_id}`` request publishes **first** (jarvis
must capture the ``request_id`` before the Slack post renders), then
``emitter.emit_paused(...)`` publishes ``pipeline.build-paused.{feature_id}``.
SQLite-before-wire is preserved untouched (the SQLite PAUSED row + request_id
are written by ``_atomic_pause_and_publish`` before either publish). Defer
re-publishes flow through the same wrapper, so each attempt gets a fresh
build-paused.

Restart recovery (:func:`rearm_paused_gates`, plan §D4.2) re-arms every PAUSED
build's approval round-trip on boot: per PAUSED row it starts
``await_and_dispatch`` (reusing the shipped four-step chain verbatim), CONFIRMS
the response subscription is live (arm-before-post), THEN re-emits the AGENTS
approval request (verbatim persisted ``request_id``, correlation stamped via the
recovery envelope builder) FIRST and the PIPELINE build-paused SECOND. On
approve it launches via the injected ``resume_launcher``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from forge.gating.degraded import (
    EmptyAdjustmentsReader,
    EmptyRulesReader,
    degraded_dispatch_gate_model,
)
from forge.gating.wrappers import GateOutcome, await_and_dispatch, gate_check
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
    StageLogEntry,
)
from forge.lifecycle.state_machine import (
    BuildState,
    InvalidTransitionError,
    transition_chain,
)
from forge.pipeline import BuildContext
from forge.pipeline.merge_offer import approval_subject_for, merge_request_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nats_core.envelope import MessageEnvelope

    from forge.cli._serve_deps_gating import ApprovalGateParts
    from forge.gating.wrappers import GateRepository, PausedBuildSnapshot, StateMachine
    from forge.pipeline import PipelineLifecycleEmitter

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_PAUSED",
    "HOLD_SLOT",
    "GateDispatchOutcome",
    "MergeCardNotPublished",
    "make_merge_card_publisher",
    "merge_card_words",
    "maybe_gate_build",
    "outcome_launches",
    "rearm_paused_gates",
]

#: Upper bound (seconds) on how long the boot sweep waits for a per-build
#: response subscription to arm before giving up on that build's re-emit.
#: Without a bound a per-build ``subscribe`` that raises (transient broker
#: error / closed conn) leaves ``armed`` unset, so ``await armed.wait()``
#: would block FOREVER — wedging the whole sweep and, with it, ``_run_serve``
#: boot (no dispatch, no healthz), unrecoverable without a kill and recurring
#: every restart for a persistently-bad row. On timeout the sweep logs, skips
#: that build's re-emit (arm-before-post: never post into a dead subscription),
#: and moves on; the next boot retries the still-PAUSED row.
_REARM_ARM_TIMEOUT_SECONDS: float = 10.0

#: Strong references to the per-build rearm background tasks. ``asyncio``
#: only keeps a weak reference to a bare :func:`asyncio.create_task` result,
#: so a rearmed await could be garbage-collected mid-pause without this set.
#: Each task removes itself via a done-callback (see :func:`_track_rearm_task`).
_REARM_TASKS: set["asyncio.Task[Any]"] = set()


def _track_rearm_task(task: "asyncio.Task[Any]") -> None:
    """Hold a strong reference to ``task`` until it completes."""
    _REARM_TASKS.add(task)
    task.add_done_callback(_REARM_TASKS.discard)


def _log_rearm_task_exception(task: "asyncio.Task[Any]") -> None:
    """Retrieve + log a background rearm task's exception so it is not orphaned.

    Without this, a ``_rearm_dispatch`` that dies (e.g. a per-build
    ``subscribe`` raising a transient broker error) leaves an unretrieved
    exception that asyncio surfaces as a noisy "Task exception was never
    retrieved" at GC. The arm-wait timeout already skipped the re-emit for
    that build (next boot retries the still-PAUSED row), so here we only
    surface the underlying cause. Cancelled tasks carry no error to retrieve.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "rearm_paused_gates: background task %s died: %s",
            task.get_name(),
            exc,
        )


#: The pre-dispatch gate always targets the autobuild runner subagent. These
#: are the plan "Gate mechanics" call constants — a fixed target rather than
#: a static stage registry (ADR-ARCH-019 compliant).
_GATE_STAGE_LABEL: str = "autobuild"
_GATE_TARGET_KIND: str = "subagent"
_GATE_TARGET_IDENTIFIER: str = "autobuild_runner"

#: ``stage_log.target_identifier`` of the row the merge-ready checkpoint
#: writes when its card goes out. Same target KIND as the pre-dispatch gate
#: (a subagent), different identifier, so the checkpoint's card and the
#: dispatch gate's are distinguishable in the build's own records. Two
#: readers key on it and neither may lose it: the one-card latch (a restart
#: mid-journey must never card twice) and the measure that counts how many
#: repairs the factory closed by itself.
_MERGE_CARD_TARGET_IDENTIFIER: str = "merge_ready_checkpoint"

#: ``stage_log.details_json`` key under which that row keeps the card's facts.
_MERGE_CARD_DETAILS_KEY: str = "merge_card"

#: :class:`GateOutcome` members that mean "permission granted — launch the
#: build". The remaining members (``FAILED`` / ``CANCELLED`` / ``TIMED_OUT``)
#: are terminal: the caller acks the queue slot and never launches.
_LAUNCH_OUTCOMES: frozenset[GateOutcome] = frozenset(
    {
        GateOutcome.AUTO_APPROVED,
        GateOutcome.RESUMED,
        GateOutcome.OVERRIDDEN,
    }
)


class GateDispatchOutcome:
    """Sentinel returned by :func:`maybe_gate_build` for the already-paused case.

    Distinct from every :class:`GateOutcome` member so the dispatch flow can
    tell "the gate already owns this build (a rearm/redelivery re-entry) —
    hold the slot, do not launch, do not ack" apart from a fresh gate result.
    Instances compare by identity; only :data:`ALREADY_PAUSED` is used.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"GateDispatchOutcome.{self._name}"


#: The builds row was already PAUSED with a ``pending_approval_request_id``
#: when ``maybe_gate_build`` ran — the (Wave-3) rearm path owns it. The
#: dispatch flow must NOT start a second gate, NOT launch, and NOT ack (the
#: FEAT-FORGE-010 held-slot invariant).
ALREADY_PAUSED: GateDispatchOutcome = GateDispatchOutcome("ALREADY_PAUSED")

#: The gate committed the SQLite PAUSED row but a downstream step failed in a
#: way that must NOT surface as a build failure: the AGENTS approval publish
#: raised :class:`ApprovalPublishError` (transport down — the PAUSED row is
#: durable, rearm re-emits next boot), or a synthetic pre-gate hop lost the
#: optimistic-concurrency race to a concurrent terminal write (the other writer
#: is authoritative). Either way the dispatch flow holds the slot exactly like
#: :data:`ALREADY_PAUSED` — NO launch, NO ack, and crucially NO ``build-failed``
#: emit (which would be factually wrong for a PAUSED / already-terminal build).
HOLD_SLOT: GateDispatchOutcome = GateDispatchOutcome("HOLD_SLOT")


def outcome_launches(outcome: "GateOutcome | GateDispatchOutcome") -> bool:
    """Return True when ``outcome`` grants permission to launch the build."""
    return isinstance(outcome, GateOutcome) and outcome in _LAUNCH_OUTCOMES


# ---------------------------------------------------------------------------
# Mirrored approval publisher — AGENTS request FIRST, then build-paused.
# ---------------------------------------------------------------------------


class _MirroredApprovalPublisher:
    """Wrap an :class:`ApprovalPublisher` to also emit ``build-paused``.

    Satisfies ``forge.gating.wrappers.ApprovalPublisherProto`` (``publish_request``
    only). :func:`_atomic_pause_and_publish` calls ``publish_request`` as its
    wire step — after the SQLite PAUSED row + ``pending_approval_request_id``
    are committed. This wrapper turns that single call into the jarvis
    dual-envelope contract (plan §D2 / ground truth #8):

    1. publish the AGENTS ``agents.approval.forge.{build_id}`` request via the
       inner publisher (jarvis captures the ``request_id`` from it), THEN
    2. ``emitter.emit_paused(...)`` → ``pipeline.build-paused.{feature_id}``
       (the PIPELINE envelope that triggers the Slack post).

    Request BEFORE build-paused so jarvis has the ``request_id`` before the
    Slack message renders its buttons. The defer round-trip re-enters
    ``publish_request`` per attempt, so each attempt produces a fresh
    build-paused (jarvis ``chat.update`` supersede refreshes the buttons).
    """

    def __init__(
        self,
        inner: Any,
        *,
        emitter: "PipelineLifecycleEmitter",
        build_context: BuildContext,
        clock: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._emitter = emitter
        self._ctx = build_context
        self._clock = clock

    async def publish_request(self, envelope: "MessageEnvelope") -> None:
        # 1. AGENTS approval request FIRST (jarvis captures request_id).
        await self._inner.publish_request(envelope)

        # 2. PIPELINE build-paused SECOND. Derive the emit fields from the
        #    just-published approval envelope so the two envelopes describe
        #    the same pause (details are the canonical eleven-key dict built
        #    by ``approval_publisher._build_approval_details``).
        payload: Any = envelope.payload if isinstance(envelope.payload, dict) else {}
        details: dict[str, Any] = (
            payload.get("details", {}) if isinstance(payload, dict) else {}
        )
        if not isinstance(details, dict):  # pragma: no cover - defensive
            details = {}

        await self._emitter.emit_paused(
            self._ctx,
            stage_label=details.get("stage_label", _GATE_STAGE_LABEL),
            gate_mode=details.get("gate_mode", "MANDATORY_HUMAN_APPROVAL"),
            coach_score=details.get("coach_score"),
            rationale=details.get("rationale", ""),
            approval_subject=f"agents.approval.forge.{self._ctx.build_id}",
            paused_at=self._clock().isoformat(),
        )


# ---------------------------------------------------------------------------
# The first production gate_check call site.
# ---------------------------------------------------------------------------


async def maybe_gate_build(
    *,
    parts: "ApprovalGateParts",
    sqlite_pool: SqliteLifecyclePersistence,
    gate_repository: "GateRepository",
    gate_state_machine: "StateMachine",
    build_id: str,
    feature_id: str,
    correlation_id: str | None,
    clock: Callable[[], datetime],
) -> "GateOutcome | GateDispatchOutcome":
    """Run the pre-dispatch approval gate for one freshly-recorded build.

    The FIRST production caller of :func:`forge.gating.wrappers.gate_check`.

    Flow:

    1. **Idempotency pre-read** (plan §R2 belt): if the ``builds`` row is
       already PAUSED with a ``pending_approval_request_id`` this is a
       rearm / redelivery re-entry — return :data:`ALREADY_PAUSED` without
       starting a second gate (the rearm path owns it).
    2. Drive the builds row QUEUED → PREPARING → RUNNING via the synthetic
       transition chain (PAUSED is only legal from RUNNING; the gate's
       ``transition_to_paused`` → ``mark_paused`` does the final RUNNING →
       PAUSED hop). Composed via :func:`transition_chain` so the single
       producer of :class:`Transition` value objects is preserved.
    3. Assemble a per-build :class:`GateCheckDeps` around ``parts`` +
       the degraded collaborators, then swap the publisher for a
       :class:`_MirroredApprovalPublisher` so the pause emits the jarvis
       dual envelope in order.
    4. ``gate_check`` runs: the honest degraded posture pauses the build for
       ``MANDATORY_HUMAN_APPROVAL``, publishes the request, awaits Rich's
       response and dispatches it (resume / cancel / override / defer).

    Args:
        parts: The boot-scoped :class:`ApprovalGateParts` (publisher,
            subscriber, injector, emitter, expected_approver).
        sqlite_pool: The shared :class:`SqliteLifecyclePersistence` facade —
            source of the builds row read + the synthetic pre-gate hops.
        gate_repository: SQLite gate repository (owns ``stage_log``).
        gate_state_machine: SQLite gate state machine (owns ``builds.status``).
        build_id: The just-recorded build's identifier.
        feature_id: ``FEAT-XXXX`` of the build.
        correlation_id: The build's pipeline correlation id (stamped on the
            approval envelope + build-paused / build-resumed / build-cancelled).
        clock: Injected ``() -> datetime`` (UTC). Clock hygiene — never
            ``datetime.now()``.

    Returns:
        A :class:`GateOutcome` when the gate ran to a decision;
        :data:`ALREADY_PAUSED` when the row was already gated (hold the slot);
        or :data:`HOLD_SLOT` when the SQLite PAUSED row is durable but a
        downstream publish / concurrent-terminal hop failed (hold the slot, no
        launch, no ack, no build-failed — the rearm sweep recovers it).
    """
    # Local import breaks the composition-time import cycle
    # (_serve_deps_gating imports gating wrappers which import sqlite_adapters).
    from forge.cli._serve_deps_gating import make_gate_check_deps

    # 1. Idempotency pre-read — a rearm / duplicate re-entry.
    status, pending_request_id = _read_status_and_pending(sqlite_pool, build_id)
    if status is BuildState.PAUSED and pending_request_id:
        logger.info(
            "maybe_gate_build: build_id=%s already PAUSED with "
            "pending_approval_request_id=%s — rearm path owns it; not "
            "starting a second gate (held slot)",
            build_id,
            pending_request_id,
        )
        return ALREADY_PAUSED

    # 2. Synthetic hops QUEUED → PREPARING → RUNNING (PAUSED is only legal
    #    from RUNNING). The gate's ``transition_to_paused`` does RUNNING →
    #    PAUSED. Empty when the row is already RUNNING (idempotent re-entry).
    #    A concurrent terminal write landing mid-chain makes a hop illegal
    #    (``InvalidTransitionError``) or lose the optimistic-concurrency race
    #    (0-row ``RuntimeError``). The other writer is authoritative — hold
    #    the slot rather than let the error escape to handle_message as a
    #    (factually wrong) build-failed.
    try:
        for hop in transition_chain(
            Build(build_id=build_id, status=status), BuildState.RUNNING
        ):
            sqlite_pool.apply_transition(hop)
    except (InvalidTransitionError, RuntimeError) as exc:
        logger.warning(
            "maybe_gate_build: synthetic pre-gate hop for build_id=%s failed "
            "(%s) — a concurrent terminal write is authoritative; holding the "
            "slot (no launch, no ack, no build-failed)",
            build_id,
            exc,
        )
        return HOLD_SLOT

    # 3. Assemble per-build deps against the degraded collaborators.
    ctx = BuildContext(
        feature_id=feature_id,
        build_id=build_id,
        # correlation_id is a required BuildQueuedPayload field; the
        # None → "" coercion keeps the four-step correlation guard armed.
        correlation_id=correlation_id or "",
        wave_total=1,
    )
    deps = make_gate_check_deps(
        parts,
        priors_reader=parts.priors_reader,
        adjustments_reader=EmptyAdjustmentsReader(),
        rules_reader=EmptyRulesReader(),
        repository=gate_repository,
        state_machine=gate_state_machine,
        reasoning_model_call=degraded_dispatch_gate_model,
        ctx=ctx,
        clock=clock,
    )
    # Swap the raw approval publisher for the mirrored one so the pause
    # emits the AGENTS request AND the PIPELINE build-paused, in order. The
    # emitter is present on the production parts; when absent (unit tiers)
    # keep the raw publisher (approval request only, no build-paused).
    if parts.emitter is not None:
        deps.publisher = _MirroredApprovalPublisher(
            parts.publisher,
            emitter=parts.emitter,
            build_context=ctx,
            clock=clock,
        )

    # 4. Run the gate. Degraded posture ⇒ MANDATORY_HUMAN_APPROVAL ⇒ pause.
    #    ``_atomic_pause_and_publish`` commits the SQLite PAUSED row + request_id
    #    BEFORE the wire publish (SQLite-before-wire), then publishes the AGENTS
    #    approval request. A transport failure there raises
    #    ``ApprovalPublishError`` AFTER the durable PAUSED row is committed
    #    (no-rollback contract) — hold the slot; the rearm sweep re-emits on the
    #    next boot. Letting it escape to handle_message would emit a
    #    (factually wrong) build-failed and prematurely ack a PAUSED build.
    from forge.adapters.nats.approval_publisher import ApprovalPublishError

    try:
        outcome, _decision = await gate_check(
            deps=deps,
            build_id=build_id,
            feature_id=feature_id,
            stage_label=_GATE_STAGE_LABEL,
            target_kind=_GATE_TARGET_KIND,  # type: ignore[arg-type]
            target_identifier=_GATE_TARGET_IDENTIFIER,
            coach_score=None,
            criterion_breakdown={},
            detection_findings=[],
            attempt_count=0,
        )
    except ApprovalPublishError as exc:
        logger.warning(
            "maybe_gate_build: approval publish failed for build_id=%s (%s) — "
            "the SQLite PAUSED row is durable; holding the slot, rearm re-emits "
            "next boot (no launch, no ack, no build-failed)",
            build_id,
            exc,
        )
        return HOLD_SLOT
    logger.info(
        "maybe_gate_build: gate decided build_id=%s outcome=%s",
        build_id,
        outcome.value,
    )
    return outcome


# ---------------------------------------------------------------------------
# The merge-ready checkpoint's card — the card the merge press consumes.
# ---------------------------------------------------------------------------


class MergeCardNotPublished(RuntimeError):
    """Raised when the merge-ready checkpoint's card did not go out.

    The checkpoint treats a raise here as terminal and never asks again,
    which is what we want: every reason the offer refuses is either a
    missing fact (no builds row, no repository path, no correlation id, no
    commit to pin the merge to) or a card that is already on record for
    this build. None of them get better by trying a second time, and a
    second card for one merge word is the thing the whole one-card rule
    exists to prevent.

    ``card_reached_the_wire`` is ``False`` and says the one thing the
    checkpoint's record needs: NOTHING was published. Every refusal that
    raises this happens before the offer touches the wire, so the journey
    must write "no card was published, and here is why" rather than the
    hedge it writes for a raise mid-publish ("the card may be on the
    wire"). The checkpoint reads the attribute rather than this class, so
    it keeps its no-import-edge discipline.
    """

    card_reached_the_wire = False


def merge_card_words(*, feature_id: str, branch: str, gates: Any = None) -> str:
    """The sentences on the face of the merge-ready checkpoint's card.

    Four things a person needs and nothing else: what was checked and what
    it said, what was NOT checked here and who checks it, which branch the
    merge word merges, and what the merge word does. No codenames, no
    counters, no house words — this text goes straight onto a Slack card.

    Args:
        feature_id: ``FEAT-XXXX`` of the build, as the card names it.
        branch: The branch the merge word will merge.
        gates: The checkpoint's own :class:`GatesReport`, read for its
            ``detail`` (what the declared suite did) and for WHETHER its
            ``deferred_detail`` says anything at all. Only the fact that
            something was left unproved reaches the card, said here in
            ordinary words; the internal sentence — which names the
            checks by their own ids and their homes — stays on the
            decision and in the log, because a card is read by a person.
            Anything missing simply leaves that sentence out — the card
            never claims a check it cannot name.
    """
    detail = str(getattr(gates, "detail", "") or "").strip().rstrip(".")
    deferred = str(getattr(gates, "deferred_detail", "") or "").strip()
    named = feature_id or "this repair"
    checked = detail or "the checks this repository declares came back green"
    sentences = [
        f"{named} is ready to merge on branch {branch}.",
        f"What was checked: {checked}.",
    ]
    if deferred:
        # Ordinary words for the fact, never the internal sentence: that one
        # names check ids and their homes, which mean nothing to the person
        # holding the card. The full sentence is on the decision and the log.
        sentences.append(
            "Some of the checks this repository asks for could not be proved "
            "on this branch here; they are run against the candidate in the "
            "sandbox before anything is merged."
        )
    sentences.append(
        "Approve = check the candidate in the sandbox, merge the branch into "
        "main and promote it."
    )
    sentences.append("Reject = nothing changes; the branch is kept either way.")
    return " ".join(sentences)


def make_merge_card_publisher(
    *,
    offer_service: Any,
    sqlite_pool: SqliteLifecyclePersistence,
    clock: Callable[[], datetime],
) -> Callable[..., Any]:
    """Compose the merge card's ``publish_card`` seam (design pass §c.2).

    The conductor's merge-ready checkpoint must deliver "the SAME
    approve-click merge card the consumer path already delivers"
    (DF-021, settled live 2026-07-23). It now delivers literally that
    card: this seam calls :meth:`~forge.pipeline.merge_offer.MergeOfferService.offer`,
    the routine build's own merge-card publisher, handing it the
    checkpoint's own words. Nothing here builds an envelope, writes a
    request id, or talks to jarvis.

    Why it changed (2026-09-09, the twentieth attempt at the fix journey).
    This seam used to publish through the ordinary approval gate, which
    mints its own request id (``<build id>:<stage label>:0``) and writes
    no merge-offer row. The merge press listens for a request id beginning
    ``merge-`` backed by a durable ``merge_deploy_offer`` row, so it
    ignored the card completely: the owner answered approve in Slack,
    forge logged that the card was published, the journey reported
    delivered — and nothing checked the candidate, nothing merged, nothing
    promoted. The owner's merge word fell on the floor. There are no
    longer two merge cards in this estate.

    What follows from sharing the routine path's card:

    * the request id is ``merge-<build id>`` and the durable
      ``merge_deploy_offer`` row is latched before the card reaches the
      wire — the press's own double-run fence and its match check;
    * the ``build-paused`` envelope carries the synthetic
      ``merge-<feature id>`` join key, so the tap works in Slack on a
      build the terminal registry has already seen;
    * the owner's answer goes to the merge press and NOT back through
      this seam, so nothing here waits for it and the build is not paused
      and resumed around it. The journey's own record says a card was
      delivered, which is exactly what happened; what the owner then said
      is the press's story, told in the press's own receipts.

    One durable row is written here after the card goes out, carrying the
    merge-ready checkpoint's own identifier: it is the one-card latch's
    durable half (a restart mid-journey must never card twice) and the row
    the self-closed-defect measure counts a raised card by. It is written
    after the publish so it can never claim a card that was refused.

    Returns:
        ``async (*, build_id, feature_id, rationale, branch, gates) ->
        None`` — the seam
        :class:`~forge.pipeline.merge_ready_checkpoint.MergeReadyCheckpointPublisher`
        calls once its gates-green precondition has passed. It returns
        nothing because there is no verdict to return; it raises
        :class:`MergeCardNotPublished` when no card went out.
    """
    from forge.pipeline.merge_ready_checkpoint import MERGE_READY_CHECKPOINT_LABEL

    async def publish_card(
        *,
        build_id: str,
        feature_id: str = "",
        rationale: str = "",
        branch: str | None = None,
        gates: Any = None,
    ) -> Any:
        # ``rationale`` is the journey's own note to itself
        # ("mode-c-commits-present" and the like). It stays off the card on
        # purpose: the card is read by a person, and the words it carries
        # are built below from what the checkpoint actually checked.
        del rationale
        row = sqlite_pool.get_build_row(build_id)
        resolved_feature = feature_id or (
            getattr(row, "feature_id", "") or "" if row is not None else ""
        )
        gated_branch = str(branch or "").strip() or None

        def _words(merge_target: str, _merge_branch: str | None) -> str:
            if gated_branch is not None and gated_branch != merge_target:
                logger.warning(
                    "merge card: the checkpoint ran its checks on branch %s "
                    "but the merge word merges %s (that is the branch on the "
                    "build's own row, and the press reads the row) — the card "
                    "names the branch that will be merged",
                    gated_branch,
                    merge_target,
                )
            return merge_card_words(
                feature_id=resolved_feature, branch=merge_target, gates=gates
            )

        logger.info(
            "merge card: publishing %s for build_id=%s branch=%s (feature_id=%s)",
            MERGE_READY_CHECKPOINT_LABEL,
            build_id,
            branch,
            resolved_feature,
        )
        published = await offer_service.offer(
            build_id=build_id,
            feature_id=resolved_feature,
            card_words=_words,
        )
        if not published:
            raise MergeCardNotPublished(
                f"no merge card was published for build_id={build_id}: the "
                "merge offer refused before anything reached the wire (the "
                "reason is the logged sentence just above this one). Nothing "
                "is retried from here — a second card would be a second act "
                "for one merge word"
            )

        # The checkpoint's own durable row, written only now that a card is
        # really out. Two readers need it and both key on the identifier:
        # the one-card latch (a restart must not card twice) and the measure
        # that counts how many repairs reached a merge card.
        now = clock()
        try:
            sqlite_pool.record_stage(
                StageLogEntry(
                    build_id=build_id,
                    stage_label=MERGE_READY_CHECKPOINT_LABEL,
                    target_kind=_GATE_TARGET_KIND,
                    target_identifier=_MERGE_CARD_TARGET_IDENTIFIER,
                    status="GATED",
                    gate_mode="MANDATORY_HUMAN_APPROVAL",
                    started_at=now,
                    completed_at=now,
                    duration_secs=0.0,
                    details={
                        _MERGE_CARD_DETAILS_KEY: {
                            "build_id": build_id,
                            "feature_id": resolved_feature,
                            "branch": gated_branch,
                            "request_id": merge_request_id(build_id),
                            "approval_subject": approval_subject_for(
                                resolved_feature
                            ),
                        }
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — the card is out either way
            logger.error(
                "merge card: the card for build_id=%s is published, but "
                "recording the checkpoint's own row raised %s: %s — the "
                "merge press is unaffected (it reads the offer's row), and "
                "the one-card latch falls back to the offer's own refusal "
                "to card a build twice",
                build_id,
                type(exc).__name__,
                exc,
            )
        # Nothing to return: the owner's answer goes to the merge press.
        return None

    return publish_card


def _read_status_and_pending(
    pool: SqliteLifecyclePersistence, build_id: str
) -> tuple[BuildState, str | None]:
    """Read ``(status, pending_approval_request_id)`` off the writer connection."""
    row = pool.connection.execute(
        "SELECT status, pending_approval_request_id FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"maybe_gate_build: no build row for build_id={build_id!r}")
    try:
        status_raw = row["status"]
        pending = row["pending_approval_request_id"]
    except (TypeError, IndexError):  # pragma: no cover - non-Row fallback
        status_raw, pending = row[0], row[1]
    return BuildState(status_raw), pending


# ---------------------------------------------------------------------------
# Restart recovery — rearm_paused_gates (plan §D4.2).
# ---------------------------------------------------------------------------


class _ArmSignallingClient:
    """Wrap a NATS client so ``subscribe`` fires an :class:`asyncio.Event`.

    :func:`rearm_paused_gates` must re-emit the approval request only AFTER the
    per-build response subscription is provably live — on core NATS a response
    posted before the subscription exists is silently dropped (the C1
    boot-order tap-drop the arch review flagged). :meth:`ApprovalSubscriber.await_response`
    subscribes as its FIRST action and then blocks on the response queue, so
    wrapping the client's ``subscribe`` gives the rearm coroutine a clean
    "subscription is live" signal to gate the re-emit on (arm-before-post).
    Every other attribute proxies straight through to the wrapped client.
    """

    def __init__(self, inner: Any, armed: "asyncio.Event") -> None:
        self._inner = inner
        self._armed = armed

    async def subscribe(self, subject: str, callback: Any) -> Any:
        sub = await self._inner.subscribe(subject, callback)
        # Interest is registered client-side once ``subscribe`` returns; fire
        # the arm signal so the rearm coroutine may re-emit the request.
        self._armed.set()
        return sub

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self._inner, name)


async def rearm_paused_gates(
    *,
    parts: "ApprovalGateParts",
    sqlite_pool: SqliteLifecyclePersistence,
    gate_repository: "GateRepository",
    gate_state_machine: "StateMachine",
    resume_launcher: Callable[..., Any],
    client: Any,
    clock: Callable[[], datetime],
) -> list["asyncio.Task[Any]"]:
    """Re-arm every PAUSED build's approval round-trip after a daemon restart.

    This is the boot-time owner of BOTH PAUSED re-emits (plan §D4.1-2,
    arch-review C1): ``recovery.reconcile_on_boot`` is bound with a no-op
    ``ApprovalRepublisher`` and the consumer twin seam suppresses its PAUSED
    scan, so this function is the single actor that re-arms a live response
    subscriber and re-emits the jarvis dual envelope.

    Per PAUSED row from :meth:`GateRepository.list_paused_builds` (which already
    parses the persisted ``request_id`` and skips + logs ERROR on a legacy /
    unparseable id):

    1. Rebuild the build's :class:`BuildContext` + per-build
       :class:`GateCheckDeps` (degraded collaborators; the persisted decision
       snapshot, or a degraded fallback, is carried on the snapshot).
    2. Swap in an arm-signalling subscriber and the
       :class:`_MirroredApprovalPublisher` so re-emits carry the dual envelope.
    3. Start :func:`forge.gating.wrappers.await_and_dispatch` as a background
       task — it subscribes to the response mirror (arming the subscription)
       and then blocks awaiting the operator's decision.
    4. **Arm-before-post**: wait for the subscription to be live, THEN re-emit
       FIRST the AGENTS approval request (verbatim persisted ``request_id`` +
       correlation, via :func:`build_recovery_approval_envelope`) and SECOND
       the PIPELINE build-paused (via the mirrored publisher). request-before-
       paused preserves the jarvis button-join order.
    5. On approve/override/auto the background task launches the build via the
       injected ``resume_launcher`` (dispatch minus ``record_pending_build``;
       R1 no-op ack handle post-restart — the terminal ack rides the
       duplicate-terminal redelivery of the still-held build-queued message).

    DDR-027: the rearm window rebases to a full per-attempt window (in-memory
    posture) — a restart mid-pause restarts the clock, which is the safe
    default when the pre-crash elapsed time is unknown.

    Args:
        parts: The boot-scoped :class:`ApprovalGateParts` (publisher,
            subscriber template, injector, emitter, expected_approver).
        sqlite_pool: Shared :class:`SqliteLifecyclePersistence` facade — source
            of the :class:`BuildRow` for the recovery envelope builder.
        gate_repository: SQLite gate repository (``list_paused_builds``).
        gate_state_machine: SQLite gate state machine (PAUSED → RUNNING /
            CANCELLED on the operator's decision).
        resume_launcher: ``async (*, build_id, feature_id, correlation_id)``
            launch closure (``build_serve_resume_launcher``) — dispatch minus
            ``record_pending_build``; invoked on the approve path.
        client: The daemon's shared NATS client — wrapped per build in an
            :class:`_ArmSignallingClient` so the re-emit waits for a live
            subscription.
        clock: Injected ``() -> datetime`` (UTC). Clock hygiene.

    Returns:
        The list of per-build background tasks (one per re-armed PAUSED build).
        Callers may hold them for observability; a strong reference is already
        retained internally (see :data:`_REARM_TASKS`) so they survive GC.
    """
    # Local imports break the composition-time import cycle (the gating parts
    # and subscriber pull in wrappers → sqlite_adapters).
    from forge.adapters.nats.approval_publisher import (
        build_recovery_approval_envelope,
    )
    from forge.adapters.nats.approval_subscriber import (
        ApprovalSubscriber,
        ApprovalSubscriberDeps,
    )
    from forge.cli._serve_deps_gating import (
        _BoundContextSubscriber,
        make_gate_check_deps,
    )

    if parts.emitter is None:
        # Without the emitter the mirrored publisher cannot emit build-paused,
        # so jarvis would never re-render the pause. Skip rather than re-emit a
        # buttonless half-envelope (production always wires the emitter).
        logger.warning(
            "rearm_paused_gates: parts.emitter is None — cannot re-emit the "
            "jarvis dual envelope; no PAUSED build re-armed this boot"
        )
        return []

    snapshots = await gate_repository.list_paused_builds()
    tasks: list["asyncio.Task[Any]"] = []

    for snap in snapshots:
        # One persistently-bad row must never abort the whole sweep (and with
        # it daemon boot): wrap the per-snapshot body so any unexpected error
        # logs and continues to the next PAUSED build.
        try:
            ctx = BuildContext(
                feature_id=snap.feature_id,
                build_id=snap.build_id,
                # correlation_id is a required BuildQueuedPayload field; the
                # None → "" coercion keeps the correlation guard armed.
                correlation_id=snap.correlation_id or "",
                wave_total=1,
            )

            build_row = sqlite_pool.get_build_row(snap.build_id)
            if build_row is None:
                logger.error(
                    "rearm_paused_gates: no builds row for PAUSED build_id=%s; "
                    "skipping (corrupt state)",
                    snap.build_id,
                )
                continue
            try:
                recovery_envelope = build_recovery_approval_envelope(build_row)
            except ValueError as exc:  # pragma: no cover - guarded by list scan
                logger.error(
                    "rearm_paused_gates: cannot build recovery envelope for "
                    "build_id=%s (%s); skipping",
                    snap.build_id,
                    exc,
                )
                continue

            deps = make_gate_check_deps(
                parts,
                priors_reader=parts.priors_reader,
                adjustments_reader=EmptyAdjustmentsReader(),
                rules_reader=EmptyRulesReader(),
                repository=gate_repository,
                state_machine=gate_state_machine,
                reasoning_model_call=degraded_dispatch_gate_model,
                ctx=ctx,
                clock=clock,
            )

            # Per-build arm signal — fires once the response subscription is
            # live. TASK-JNB-109: the raw daemon client must be adapted to
            # the envelope-aware subscribe surface the subscriber expects
            # (same fix as build_approval_gate_parts — without it the
            # rearm-path callback binds to the raw client's ``queue``
            # parameter and no response is ever received).
            from forge.adapters.nats.envelope_subscribe import (
                EnvelopeSubscribeClient,
            )

            armed = asyncio.Event()
            arming_subscriber = ApprovalSubscriber(
                ApprovalSubscriberDeps(
                    nats_client=_ArmSignallingClient(
                        EnvelopeSubscribeClient(client), armed
                    ),
                    config=parts.approval_config,
                    publish_refresh=None,
                    expected_approver=parts.expected_approver,
                    project=None,
                    bridge_registry_lookup=None,
                    # The rearm wait honours the SAME build-gate total-wait
                    # knob the live gate uses (0 = wait indefinitely,
                    # 2026-08-26) — a daemon restart mid-pause must not turn
                    # an indefinite wait back into a give-up.
                    max_total_wait_seconds=parts.gate_approval_max_wait_seconds,
                )
            )
            # Wrap the arm-signalling subscriber in the per-build bound context
            # so the FW10-010 resume emit + the four-step correlation guard stay
            # live on the re-armed await (same behaviour the live path gets from
            # make_gate_check_deps).
            deps.subscriber = _BoundContextSubscriber(
                arming_subscriber,
                lifecycle_emitter=parts.emitter,
                build_context=ctx,
                expected_correlation_id=ctx.correlation_id,
            )
            # Re-emits (initial + any defer) flow through the mirrored publisher
            # so each attempt refreshes the jarvis buttons (fresh build-paused).
            deps.publisher = _MirroredApprovalPublisher(
                parts.publisher,
                emitter=parts.emitter,
                build_context=ctx,
                clock=clock,
            )

            # Start the response-await + dispatch (+ launch on approve). It
            # subscribes FIRST (arming the subscription), then blocks awaiting
            # the decision. ``armed`` is only set once that subscribe RETURNS,
            # so a subscribe that raises leaves it unset — hence the bounded
            # wait below. The done-callback retrieves any exception the task
            # dies with so asyncio does not warn about an orphaned failure.
            # SECOND-REPO LAW: the boot-rearm launch must name its repo. The
            # sweep already holds ``build_row`` (fetched above for the recovery
            # envelope) and ``builds.repo`` is a REQUIRED column, so the value
            # is right here — the historical ``repo=None`` was a gap, not a
            # shortage of information. Without it the runner's repo-less
            # refusal would terminate every re-armed build (and, before that
            # refusal existed, it silently built FORGE_DEFAULT_REPO — repo #1 —
            # whatever repo the feature actually belonged to).
            task = asyncio.create_task(
                _rearm_dispatch(
                    deps=deps,
                    snap=snap,
                    resume_launcher=resume_launcher,
                    repo=getattr(build_row, "repo", None),
                ),
                name=f"rearm-gate-{snap.build_id}",
            )
            _track_rearm_task(task)
            task.add_done_callback(_log_rearm_task_exception)

            # Arm-before-post with a BOUND: block until the subscription is
            # provably live, THEN re-emit (request FIRST via the inner
            # publisher, build-paused SECOND). If the per-build subscribe
            # raised / hung, ``armed`` never fires — time out, skip this
            # build's re-emit (never post into a dead subscription), cancel the
            # stuck/dead task, and move on; the next boot retries the row.
            try:
                await asyncio.wait_for(armed.wait(), timeout=_REARM_ARM_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                logger.error(
                    "rearm_paused_gates: build_id=%s request_id=%s subscription "
                    "did not arm within %ss — skipping re-emit, will retry next "
                    "boot",
                    snap.build_id,
                    snap.request_id,
                    _REARM_ARM_TIMEOUT_SECONDS,
                )
                # Cancel the task whose subscribe never armed so we do not leak
                # a hung await; if it already died, cancel is a harmless no-op
                # and _log_rearm_task_exception has surfaced the cause.
                task.cancel()
                continue

            try:
                await deps.publisher.publish_request(recovery_envelope)
            except Exception as exc:  # noqa: BLE001 — operational signal
                logger.error(
                    "rearm_paused_gates: re-emit failed build_id=%s "
                    "request_id=%s err=%s — will retry on next boot",
                    snap.build_id,
                    snap.request_id,
                    exc,
                )
            else:
                logger.info(
                    "rearm_paused_gates: re-armed build_id=%s stage=%s "
                    "request_id=%s attempt=%d (verbatim id + correlation=%s)",
                    snap.build_id,
                    snap.stage_label,
                    snap.request_id,
                    snap.attempt_count,
                    snap.correlation_id,
                )
            tasks.append(task)
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort sweep
            logger.error(
                "rearm_paused_gates: unexpected error re-arming build_id=%s "
                "(%s); skipping to next PAUSED build",
                snap.build_id,
                exc,
            )
            continue

    logger.info("rearm_paused_gates: re-armed %d PAUSED build(s)", len(tasks))
    return tasks


async def _rearm_dispatch(
    *,
    deps: Any,
    snap: "PausedBuildSnapshot",
    resume_launcher: Callable[..., Any],
    repo: str | None = None,
) -> "GateOutcome":
    """Await the re-armed decision and launch on approve.

    Reuses :func:`forge.gating.wrappers.await_and_dispatch` verbatim (the
    shipped four-step chain / dedup / resume+cancel emits) so the rearm path
    and the live pre-dispatch path stay behaviourally identical. On an approve/
    override/auto outcome the build is launched via ``resume_launcher`` (R1
    deferred launch); a terminal outcome (reject / expiry) needs no launch —
    its ack rides the duplicate-terminal redelivery of the held build-queued
    message.

    ``repo`` is the ``builds.repo`` of the row being re-armed, threaded so the
    resumed launch names its own target repository instead of leaning on the
    daemon's environment default. It is forwarded verbatim (``None`` only when
    the caller could not read a row, which the sweep already treats as corrupt
    state).
    """
    outcome, _decision = await await_and_dispatch(
        deps=deps,
        build_id=snap.build_id,
        stage_label=snap.stage_label,
        decision=snap.decision_snapshot,
        feature_id=snap.feature_id,
        attempt_count=snap.attempt_count,
        artefact_paths=snap.artefact_paths,
    )
    if outcome_launches(outcome):
        logger.info(
            "rearm_paused_gates: build_id=%s approved post-restart "
            "(outcome=%s); launching via resume_launcher",
            snap.build_id,
            outcome.value,
        )
        await resume_launcher(
            build_id=snap.build_id,
            feature_id=snap.feature_id,
            correlation_id=snap.correlation_id,
            repo=repo,
        )
    return outcome
