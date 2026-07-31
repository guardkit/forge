"""The merge-ready checkpoint — one publisher, four call sites, no PRs.

Revival design pass §c (``supervisor-revival-design-pass-2026-07-31``),
Stage 1c.

**The ruling (Rich, twice): no pull-request-review surfaces for Rich,
ever.** His acts per feature are exactly three — the spec word, the gate
tap, the merge word. The mode chains' "pull-request-review" leg is
pre-ruling vocabulary; the delivery primitive is a **gates-green branch
plus the merge word** (DF-021, approve-click delivery, settled live
2026-07-23).

One important finding the design pass pinned first: the conductor's
``pr_review_gate`` collaborator was never a GitHub machine. It is a
protocol over the FEAT-FORGE-004 *approval gate* — the approve-click card
surface. Nothing in the chain code has ever opened a pull request. So the
adaptation is (i) vocabulary, (ii) delivery shape, and (iii) one hard
precondition — not a rip-out.

What this module is
-------------------

:class:`MergeReadyCheckpointPublisher` is **one implementation** of the
``PRReviewGate`` protocol, wired at the four ``submit_decision`` call
sites the supervisor owns (the dispatch branch, the Mode B
post-autobuild route, the Mode C planner-chosen dispatch, the Mode C
terminal route). Its act, when the leg fires:

1. **Push the fixed branch** — MODELLED at this stage. The push is
   described, recorded on the decision and left to the injected
   ``push_branch`` seam; with no seam wired the decision honestly says
   ``pushed=False, push_modelled=True`` rather than claiming a push that
   never happened.
2. **Run the full gate set on it** — through the injected
   ``gates_green_reader``.
3. **On green, publish the merge card** — the SAME approve-click card the
   routine path already delivers, through the SAME publisher seam
   (``publish_card``, composed in the daemon over ``gate_check`` and the
   mirrored approval publisher). It never opens a PR, never renders a
   diff surface, never asks Rich to read code.

The three laws it enforces
--------------------------

* **Gates green first** (§c.3). A red gate is NEVER a card. It loops back
  into the fix cycle (the conductor's next review pass) or terminates
  FAILED with a failure pack. "Fix loops run BEFORE the merge word" made
  structural.
* **No-commit terminals stay silent** (§c.6). A journey that finds
  nothing to fix, or produces no commits, ends with a receipt — not a
  card. Rich hears about work only when there is a merge word to say.
* **Never auto-merge** (§c.4, ADR-ARCH-026). ``auto_approve=True`` is
  refused here as well as by the constitutional guard upstream: belt and
  braces on "the merge word is human forever".
* **ONE card per journey — the publish latch** (§c.5 / risk h.5, Stage 2
  shakeout item 6). The moment a publish is *attempted* on a build the
  checkpoint latches: any later ``submit_decision`` for that build answers
  :attr:`MergeCardOutcome.ALREADY_CHECKPOINTED` and publishes nothing.

  Why a latch and not a re-issue. Before this, a publish that raised
  reported ``PUBLISH_FAILED``, the supervisor mapped that to ``WAITING``,
  and the driver re-planned — so a single flaky publish could re-issue the
  card up to three more times. Three cards for one merge word is act
  inflation dressed up as a retry. And re-issuing is not even the estate's
  mechanism for a lost card: the merge card rides the SAME approve-click
  machinery as the pre-dispatch gate, whose ``request_id`` is durable and
  whose re-emit is owned by ``rearm_paused_gates`` at boot and by the
  subscriber's refresh loop within the window. Re-publishing from here
  would duplicate a job something else already owns, against a row that is
  already PAUSED. So a raised publish is an **honest terminal** carrying
  ``card_may_be_on_the_wire`` — the journey stops, the pack says what
  happened, and the existing rearm path is what gets the card in front of
  the owner.

The stage enum is deliberately NOT renamed — ``PULL_REQUEST_REVIEW``
names durable ``stage_log`` rows and renaming it would be cosmetic churn
across history. The *user surfaces* speak the plain name
(:data:`MERGE_READY_CHECKPOINT_LABEL`); the codename stays in the code.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "MERGE_READY_CHECKPOINT_LABEL",
    "GateStatus",
    "GatesReport",
    "MergeCardDecision",
    "MergeCardOutcome",
    "MergeReadyCheckpointPublisher",
    "RedGateAction",
]


#: The phrase-book plain name. This string is what a human reads — the
#: approval card's stage copy and every operator-facing message. The
#: codename (``pull-request-review``) stays on the durable stage rows.
MERGE_READY_CHECKPOINT_LABEL: str = "the merge-ready checkpoint"


class GateStatus(StrEnum):
    """Outcome of the full gate set on the candidate branch.

    Members:
        GREEN: Every gate passed. The only status that may publish a card.
        RED: At least one gate failed.
        UNKNOWN: The gate set could not be evaluated (no reader wired, a
            reader raised, a probe timed out). Treated exactly as RED —
            the precondition is "proven green", never "not proven red".
    """

    GREEN = "green"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GatesReport:
    """What the gate set said about the candidate branch.

    Attributes:
        status: The :class:`GateStatus`.
        failed_gates: Names of the gates that failed (empty when green).
        detail: Free-form summary, recorded verbatim on the decision.
    """

    status: GateStatus
    failed_gates: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_green(self) -> bool:
        """``True`` only for a proven-green gate set."""
        return self.status is GateStatus.GREEN


class RedGateAction(StrEnum):
    """What a red gate does instead of publishing a card (§c.3).

    Members:
        LOOP_BACK: Return to the fix cycle — the conductor's next review
            pass picks the branch up again. The default.
        TERMINATE_FAILED: Stop the journey FAILED and leave a failure
            pack. Chosen when there is no cycle left to loop into.
    """

    LOOP_BACK = "loop-back"
    TERMINATE_FAILED = "terminate-failed"


class MergeCardOutcome(StrEnum):
    """The closed set of things the merge-ready checkpoint can do.

    Members:
        CARD_PUBLISHED: Gates green, the merge card is out, Rich's merge
            word is the only remaining act. **The only member that means
            a card was published.**
        NO_COMMITS_SILENT: Nothing was committed — a receipt, no card
            (§c.6).
        RED_GATE_LOOP_BACK: A gate is red; back into the fix cycle.
        RED_GATE_FAILED: A gate is red and there is no cycle left; FAILED
            with a pack.
        PUBLISH_FAILED: The gates were green and the card publish RAISED.
            The envelope may already be on the wire, so this is a
            **terminal** — never a retry. See the class-level note below.
        DELIVERY_NOT_WIRED: The gates were green and no card publisher is
            wired at all (the shadow-replay posture, design pass Stage 2
            where delivery is deliberately OFF). Nothing reached the wire
            and nothing will; the journey ends honestly rather than
            waiting for a delivery that has no mechanism.
        ALREADY_CHECKPOINTED: A checkpoint already fired for this build
            and its publish was attempted. Bounded to the ONE card
            (design pass risk h.5) — this decision publishes nothing.
    """

    CARD_PUBLISHED = "card-published"
    NO_COMMITS_SILENT = "no-commits-silent"
    RED_GATE_LOOP_BACK = "red-gate-loop-back"
    RED_GATE_FAILED = "red-gate-failed"
    PUBLISH_FAILED = "publish-failed"
    DELIVERY_NOT_WIRED = "delivery-not-wired"
    ALREADY_CHECKPOINTED = "already-checkpointed"


@dataclass(frozen=True, slots=True)
class MergeCardDecision:
    """Structured result of one merge-ready checkpoint firing.

    Returned from ``submit_decision`` and threaded onto the supervisor's
    :class:`~forge.pipeline.supervisor.TurnReport` as
    ``dispatch_result``, so the audit trail records exactly what the leg
    did — including, crucially, whether a card was published.

    Attributes:
        outcome: The :class:`MergeCardOutcome`.
        build_id / feature_id: Identity of the checkpoint.
        card_published: The h.5 audit's counter. ``True`` for exactly one
            decision per fix journey, and zero for a clean/no-commit run.
        branch: Branch the checkpoint ran against, when known.
        pushed: Whether a real push happened (an injected ``push_branch``
            seam ran and reported success).
        push_modelled: ``True`` when the push step was described but not
            executed — the honest Stage-1 posture.
        gates: The :class:`GatesReport` the precondition read.
        auto_approve_refused: ``True`` when a caller asked for
            auto-approve and this publisher refused it.
        rationale: The rationale carried through from the caller, plus
            this leg's own note.
        card_result: Whatever the card publisher returned — for the
            production publisher this is the owner's verdict on the card
            (approved / declined / expired). Never interpreted here; the
            driver loop reads it to pick the honest outcome WORD for the
            run report (Stage 2 shakeout item 7).
        card_may_be_on_the_wire: ``True`` when a publish was attempted and
            raised, so the envelope may or may not have reached the owner.
            The reason this decision is terminal rather than retried.
        failure_pack: Path of the failure pack written on the
            TERMINATE_FAILED branch, when one was written.
    """

    outcome: MergeCardOutcome
    build_id: str
    feature_id: str = ""
    card_published: bool = False
    branch: str | None = None
    pushed: bool = False
    push_modelled: bool = True
    gates: GatesReport | None = None
    auto_approve_refused: bool = False
    rationale: str = ""
    card_result: Any | None = None
    card_may_be_on_the_wire: bool = False
    failure_pack: Any | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_terminal_failed(self) -> bool:
        """``True`` when the journey should end FAILED on this decision."""
        return self.outcome is MergeCardOutcome.RED_GATE_FAILED

    @property
    def loops_back(self) -> bool:
        """``True`` when the journey should re-enter the fix cycle."""
        return self.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it unchanged.

    Every injected seam here may be sync (tests, in-memory fakes) or
    async (the production wire). Normalising once at the boundary keeps
    both shapes first-class without a second publisher implementation.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class MergeReadyCheckpointPublisher:
    """The one merge-card publisher behind every ``submit_decision``.

    Satisfies :class:`~forge.pipeline.supervisor.PRReviewGate` (the
    supervisor awaits the result, so ``submit_decision`` may be async).

    Args:
        publish_card: ``(*, build_id, feature_id, rationale, branch,
            gates) -> Any`` — the approve-click card seam. In production
            this is composed in the daemon over the SAME ``gate_check`` +
            mirrored-approval-publisher machinery the routine path uses;
            this class never builds an envelope of its own. ``None``
            means no delivery is wired: the checkpoint still runs its
            precondition and reports honestly (used by shadow replays,
            design pass Stage 2, where delivery is deliberately OFF).
        gates_green_reader: ``(build_id, branch) -> GatesReport | bool``
            — the full gate set on the candidate branch. ``None`` or a
            raise yields :attr:`GateStatus.UNKNOWN`, which is treated as
            red. The precondition is "proven green".
        has_commits_probe: ``(build_id) -> bool`` — the belt on §c.6. A
            checkpoint reached with no commits publishes nothing.
            ``None`` skips the belt (the callers' own no-commit
            terminals already stay silent).
        branch_reader: ``(build_id) -> str | None`` — the branch to push
            and gate.
        push_branch: ``(build_id, branch) -> bool`` — the real push. When
            ``None`` (the Stage-1 default) the push step is MODELLED:
            described, recorded, not executed.
        red_gate_action: ``(build_id, GatesReport) -> RedGateAction`` —
            loop back into the fix cycle, or terminate FAILED. Defaults
            to :attr:`RedGateAction.LOOP_BACK`.
        failure_pack_writer: ``(*, build_id, feature_id, reason, gates)
            -> Any`` — writes the journey's own failure pack on the
            TERMINATE_FAILED branch.
    """

    def __init__(
        self,
        *,
        publish_card: Callable[..., Any | Awaitable[Any]] | None = None,
        gates_green_reader: Callable[..., Any] | None = None,
        has_commits_probe: Callable[[str], Any] | None = None,
        branch_reader: Callable[[str], Any] | None = None,
        push_branch: Callable[..., Any] | None = None,
        red_gate_action: Callable[[str, GatesReport], RedGateAction] | None = None,
        failure_pack_writer: Callable[..., Any] | None = None,
    ) -> None:
        self._publish_card = publish_card
        self._gates_green_reader = gates_green_reader
        self._has_commits_probe = has_commits_probe
        self._branch_reader = branch_reader
        self._push_branch = push_branch
        self._red_gate_action = red_gate_action
        self._failure_pack_writer = failure_pack_writer
        # THE PUBLISH LATCH — build ids whose card publish has been
        # ATTEMPTED. Armed before the await, so even a raise inside the
        # publisher leaves it armed: "we may have put a card on the wire"
        # is the state that must never be retried. A red gate does NOT
        # arm it — looping back into the fix cycle and checkpointing again
        # later is the design, and that path never reached a publisher.
        self._published: set[str] = set()

    async def submit_decision(
        self,
        *,
        build_id: str,
        feature_id: str,
        auto_approve: bool,
        rationale: str,
    ) -> MergeCardDecision:
        """Fire the merge-ready checkpoint for ``build_id``.

        The signature is the ``PRReviewGate`` protocol's verbatim, so all
        four supervisor call sites reach this one implementation without
        changing what they pass.
        """
        if build_id in self._published:
            # Bounded to the ONE card. See the class docstring's latch
            # note: a second checkpoint on a build whose publish was
            # already attempted publishes nothing, ever.
            logger.warning(
                "%s: build_id=%s has ALREADY had its card publish attempted "
                "— publishing NOTHING. A fix journey delivers exactly one "
                "merge card; re-issuing it would be act inflation, and the "
                "gate's own rearm/refresh path owns re-emitting a card the "
                "owner has not answered",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.ALREADY_CHECKPOINTED,
                build_id=build_id,
                feature_id=feature_id,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: already "
                    "checkpointed — one card per journey"
                ).strip(" |"),
            )

        auto_approve_refused = False
        if auto_approve:
            # ADR-ARCH-026 / §c.4 — the merge word is human forever. The
            # constitutional guard already vetoes this upstream; refusing
            # again here means no future wiring can route around it.
            auto_approve_refused = True
            logger.warning(
                "%s: auto_approve was requested for build_id=%s and is "
                "REFUSED — the merge word is human forever (ADR-ARCH-026); "
                "publishing the card for a human decision instead",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )

        branch = await self._read_branch(build_id)

        # §c.6 — no commits, no card. A receipt is the whole delivery.
        if self._has_commits_probe is not None:
            has_commits = await self._read_has_commits(build_id)
            if has_commits is False:
                logger.info(
                    "%s: build_id=%s produced no commits — ending with a "
                    "receipt, publishing NO card (design pass §c.6)",
                    MERGE_READY_CHECKPOINT_LABEL,
                    build_id,
                )
                return MergeCardDecision(
                    outcome=MergeCardOutcome.NO_COMMITS_SILENT,
                    build_id=build_id,
                    feature_id=feature_id,
                    branch=branch,
                    push_modelled=self._push_branch is None,
                    auto_approve_refused=auto_approve_refused,
                    rationale=(
                        f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: no "
                        "commits — receipt only, no card"
                    ).strip(" |"),
                )

        # Step (a) — push the fixed branch. MODELLED at Stage 1.
        pushed = await self._push(build_id, branch)

        # Step (b) — the full gate set. THE HARD PRECONDITION.
        gates = await self._read_gates(build_id, branch)

        if not gates.is_green:
            action = self._resolve_red_gate_action(build_id, gates)
            logger.warning(
                "%s: gates are %s for build_id=%s (failed=%s) — NO card is "
                "published; action=%s (design pass §c.3: fix loops run "
                "BEFORE the merge word)",
                MERGE_READY_CHECKPOINT_LABEL,
                gates.status.value,
                build_id,
                ", ".join(gates.failed_gates) or "unnamed",
                action.value,
            )
            failure_pack = None
            if action is RedGateAction.TERMINATE_FAILED:
                failure_pack = await self._write_failure_pack(
                    build_id=build_id, feature_id=feature_id, gates=gates
                )
            return MergeCardDecision(
                outcome=(
                    MergeCardOutcome.RED_GATE_FAILED
                    if action is RedGateAction.TERMINATE_FAILED
                    else MergeCardOutcome.RED_GATE_LOOP_BACK
                ),
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                push_modelled=self._push_branch is None,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates "
                    f"{gates.status.value} ({gates.detail or 'no detail'}) — "
                    "no card"
                ).strip(" |"),
                failure_pack=failure_pack,
            )

        # Step (c) — green. Publish the approve-click merge card.
        if self._publish_card is None:
            logger.info(
                "%s: gates GREEN for build_id=%s but no card publisher is "
                "wired (delivery OFF) — recording the decision without "
                "publishing",
                MERGE_READY_CHECKPOINT_LABEL,
                build_id,
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.DELIVERY_NOT_WIRED,
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                push_modelled=self._push_branch is None,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates "
                    "green, delivery not wired"
                ).strip(" |"),
                details={"delivery_wired": False},
            )

        # ARM THE LATCH BEFORE THE AWAIT. From here on the envelope may
        # have reached the wire, and "may have" is the state that must
        # never be retried.
        self._published.add(build_id)
        try:
            card_result = await _maybe_await(
                self._publish_card(
                    build_id=build_id,
                    feature_id=feature_id,
                    rationale=rationale,
                    branch=branch,
                    gates=gates,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a publish failure is not a merge
            logger.error(
                "%s: card publish raised %s: %s for build_id=%s — the journey "
                "STOPS here and never re-publishes. The envelope may already "
                "be on the wire; the gate's durable request_id plus "
                "rearm_paused_gates own getting it in front of the owner, and "
                "a second card from here would be a second act",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            failure_pack = await self._write_failure_pack(
                build_id=build_id,
                feature_id=feature_id,
                gates=gates,
                reason=(
                    f"{MERGE_READY_CHECKPOINT_LABEL}: card publish raised "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            return MergeCardDecision(
                outcome=MergeCardOutcome.PUBLISH_FAILED,
                build_id=build_id,
                feature_id=feature_id,
                branch=branch,
                pushed=pushed,
                push_modelled=self._push_branch is None,
                gates=gates,
                auto_approve_refused=auto_approve_refused,
                card_may_be_on_the_wire=True,
                rationale=(
                    f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: publish "
                    f"failed ({type(exc).__name__}) — the card may be on the "
                    "wire; NOT re-published"
                ).strip(" |"),
                failure_pack=failure_pack,
                details={"publish_error": f"{type(exc).__name__}: {exc}"},
            )

        logger.info(
            "%s: gates GREEN for build_id=%s on branch=%s — merge card "
            "published; the merge word is the owner's (DF-021)",
            MERGE_READY_CHECKPOINT_LABEL,
            build_id,
            branch,
        )
        return MergeCardDecision(
            outcome=MergeCardOutcome.CARD_PUBLISHED,
            build_id=build_id,
            feature_id=feature_id,
            card_published=True,
            branch=branch,
            pushed=pushed,
            push_modelled=self._push_branch is None,
            gates=gates,
            auto_approve_refused=auto_approve_refused,
            rationale=(
                f"{rationale} | {MERGE_READY_CHECKPOINT_LABEL}: gates green, "
                "merge card published"
            ).strip(" |"),
            card_result=card_result,
        )

    # -- internals ----------------------------------------------------

    async def _read_branch(self, build_id: str) -> str | None:
        if self._branch_reader is None:
            return None
        try:
            value = await _maybe_await(self._branch_reader(build_id))
        except Exception as exc:  # noqa: BLE001 — an unknown branch is not fatal
            logger.warning(
                "%s: branch_reader raised %s: %s for build_id=%s — "
                "continuing with an unnamed branch",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None
        return str(value) if value else None

    async def _read_has_commits(self, build_id: str) -> bool | None:
        assert self._has_commits_probe is not None
        try:
            return bool(await _maybe_await(self._has_commits_probe(build_id)))
        except Exception as exc:  # noqa: BLE001 — an unknown answer is not "no"
            logger.warning(
                "%s: has_commits_probe raised %s: %s for build_id=%s — "
                "skipping the no-commit belt (the gate precondition still "
                "guards the card)",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None

    async def _push(self, build_id: str, branch: str | None) -> bool:
        if self._push_branch is None:
            logger.info(
                "%s: push of branch=%s for build_id=%s is MODELLED at this "
                "stage — described, not executed (design pass §c.2 step a)",
                MERGE_READY_CHECKPOINT_LABEL,
                branch,
                build_id,
            )
            return False
        try:
            return bool(
                await _maybe_await(self._push_branch(build_id=build_id, branch=branch))
            )
        except Exception as exc:  # noqa: BLE001 — a failed push is a red leg
            logger.warning(
                "%s: push_branch raised %s: %s for build_id=%s — reported as "
                "not pushed; the gate precondition decides the leg",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return False

    async def _read_gates(self, build_id: str, branch: str | None) -> GatesReport:
        if self._gates_green_reader is None:
            return GatesReport(
                status=GateStatus.UNKNOWN,
                detail=(
                    "no gates_green_reader is wired; the merge-ready "
                    "checkpoint requires PROVEN green and refuses to assume it"
                ),
            )
        try:
            raw = await _maybe_await(
                self._gates_green_reader(build_id=build_id, branch=branch)
            )
        except Exception as exc:  # noqa: BLE001 — unknown is red, never green
            return GatesReport(
                status=GateStatus.UNKNOWN,
                detail=(
                    f"gates_green_reader raised {type(exc).__name__}: {exc}; "
                    "treated as red (the precondition is proven green)"
                ),
            )
        if isinstance(raw, GatesReport):
            return raw
        if raw is True:
            return GatesReport(status=GateStatus.GREEN, detail="reader returned True")
        if raw is False:
            return GatesReport(
                status=GateStatus.RED, detail="reader returned False"
            )
        return GatesReport(
            status=GateStatus.UNKNOWN,
            detail=(
                f"gates_green_reader returned {type(raw).__name__}, which is "
                "neither a GatesReport nor a bool; treated as red"
            ),
        )

    def _resolve_red_gate_action(
        self, build_id: str, gates: GatesReport
    ) -> RedGateAction:
        if self._red_gate_action is None:
            return RedGateAction.LOOP_BACK
        try:
            action = self._red_gate_action(build_id, gates)
        except Exception as exc:  # noqa: BLE001 — default to the safer branch
            logger.warning(
                "%s: red_gate_action raised %s: %s for build_id=%s — "
                "defaulting to looping back into the fix cycle",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return RedGateAction.LOOP_BACK
        return action if isinstance(action, RedGateAction) else RedGateAction.LOOP_BACK

    async def _write_failure_pack(
        self,
        *,
        build_id: str,
        feature_id: str,
        gates: GatesReport,
        reason: str | None = None,
    ) -> Any | None:
        if self._failure_pack_writer is None:
            return None
        try:
            return await _maybe_await(
                self._failure_pack_writer(
                    build_id=build_id,
                    feature_id=feature_id,
                    reason=reason
                    or (
                        f"{MERGE_READY_CHECKPOINT_LABEL}: gates "
                        f"{gates.status.value} "
                        f"({', '.join(gates.failed_gates) or gates.detail})"
                    ),
                    gates=gates,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a pack failure is not fatal
            logger.warning(
                "%s: failure_pack_writer raised %s: %s for build_id=%s — the "
                "terminal stands, the pack is missing",
                MERGE_READY_CHECKPOINT_LABEL,
                type(exc).__name__,
                exc,
                build_id,
            )
            return None
