"""Starting a mission: the plan card, its answer, and the mission branch (17 October 2026).

Missions design, sections 1, 3 and 4. A ``mission:`` message arrives at the
planning intake as a queue command with the verb ``mission``. This module

* checks it (:func:`validate_mission`), all before any card, and answers a
  failure with one plain sentence in the thread;
* files it PROPOSED and shows the plan on one card — a ``build_paused`` event
  and an approval request on ``agents.approval.forge.mission-<id>``, with the
  stage label ``"the mission plan"`` and the features in the order they will
  run;
* on Approve (the expected approver only, first answer wins, a timeout counts
  as no) records who approved, which request it was and STARTING in one
  transaction, then seeds the factory's model routing for the mission ONCE,
  stores the main tip the branch is to be made at BEFORE asking the publisher
  to create ``mission/m-<6 hex>`` there, and, once the publisher confirms the
  branch, files one WAITING attempt per feature and moves it to RUNNING;
* on Reject records REJECTED and files nothing.

A routing seed that comes back unknown or failed is never retried (the gate's
seed-once rule): the mission becomes NOT_STARTED and Rich can send it again.
Nothing has been built at that point, so nothing is lost. A publisher that
cannot be reached leaves the mission STARTING; :meth:`MissionFlow.tend`, run
from the work queue loop's tick, asks again at most once a minute and after a
restart, and re-arms the plan card of a mission still PROPOSED after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from forge.lifecycle.missions import (
    MISSION_KEY_PATTERN,
    Mission,
    MissionFeature,
    MissionStore,
    StopAnswer,
    attempt_correlation_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_MISSION_FEATURES",
    "MISSION_PLAN_STAGE_LABEL",
    "MISSIONS_SWITCHED_OFF",
    "MissionFlow",
    "ROUTING_NOT_SET_UP",
    "mission_build_id",
    "stop_mission",
    "plan_card_rationale",
    "validate_mission",
    "waves",
]

#: The card's stage label; Jarvis gives it its own headline.
MISSION_PLAN_STAGE_LABEL: str = "the mission plan"

#: The most features one mission may carry.
MAX_MISSION_FEATURES: int = 20

#: What the thread is told when the switch is off.
MISSIONS_SWITCHED_OFF: str = "missions are switched off"

#: The reason a mission becomes NOT_STARTED when its routing seed fails.
ROUTING_NOT_SET_UP: str = (
    "the factory's model routing could not be set up for this mission; "
    "nothing was built"
)

#: How often a STARTING mission's branch request is sent again while the
#: publisher cannot be reached.
_RESEND_SECONDS: float = 60.0

#: How long to wait for the card's response subscription to arm.
_ARM_TIMEOUT_SECONDS: float = 30.0

_KEY = re.compile(MISSION_KEY_PATTERN)


def stop_mission(
    missions: MissionStore,
    mission_id: str,
    *,
    requested_by: str,
    approvers_only: bool = False,
    additional_approvers: Sequence[str] = (),
) -> StopAnswer:
    """Ask for a mission to stop: the one function Slack and the command line call.

    Section 11. Records the request (:meth:`MissionStore.request_stop`) and
    answers in one plain sentence; the coordinator carries it out on its
    next pass. ``approvers_only`` is the Slack route's rule, the same as for
    the mission's cards (``is_accepted_approver``): the person the cards are
    for, or one of ``approval.additional_approvers``. The command line is the
    operator's route and passes neither.
    """
    from forge.gating.identity import is_accepted_approver

    mission_id = str(mission_id or "").strip().lower()
    if approvers_only:
        mission = missions.get(mission_id)
        if mission is not None and not is_accepted_approver(
            requested_by, mission.originating_user, additional_approvers
        ):
            return StopAnswer(
                False,
                f"only the person who sent mission {mission_id}, or an approver "
                "named in the factory's settings, can stop it",
                mission,
            )
    return missions.request_stop(mission_id, requested_by=requested_by)


def mission_build_id(mission_id: str) -> str:
    """The plan card's subject token: ``mission-<id>`` (never ``plan-…``)."""
    return f"mission-{mission_id}"


# ---------------------------------------------------------------------------
# Checking the message
# ---------------------------------------------------------------------------


def validate_mission(
    command: Mapping[str, Any], *, mission_correlation_id: str
) -> tuple[str, list[MissionFeature]] | str:
    """``(title, features)``, or the one sentence that says what is wrong.

    The checks of design section 1, except the repository and the switch,
    which the caller makes: 1–20 features; keys of 1–12 letters or digits,
    upper-cased, unique; every ``needs`` names a key in the mission; no cycle;
    no blank sentence; and the longest attempt correlation id
    (``<mission correlation id>-<KEY>-a2``) no longer than 128 characters and
    accepted by the routing check and the intake contract.
    """
    from forge.adapters.nats.planning_consumer import _is_valid_correlation_id
    from forge.lifecycle.feature_routing import validate_feature_routing_id

    title = str(command.get("title") or "").strip()
    if not title:
        return "The mission has no title, so nothing was started."
    raw = command.get("features")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raw = []
    if not 1 <= len(raw) <= MAX_MISSION_FEATURES:
        return (
            f"A mission has between 1 and {MAX_MISSION_FEATURES} features, and "
            f"this one has {len(raw)}, so nothing was started."
        )
    features: list[MissionFeature] = []
    seen: set[str] = set()
    for item in raw:
        item = item if isinstance(item, Mapping) else {}
        key = str(item.get("key") or "").strip().upper()
        if not _KEY.fullmatch(key):
            return (
                f"{key or 'A feature'} is not a usable key: a key is 1 to 12 "
                "letters or digits. Nothing was started."
            )
        if key in seen:
            return f"The key {key} is used twice, so nothing was started."
        seen.add(key)
        sentence = " ".join(str(item.get("sentence") or "").split())
        if not sentence:
            return f"Feature {key} has no sentence, so nothing was started."
        needs_raw = item.get("needs") or []
        if isinstance(needs_raw, str):
            needs_raw = [needs_raw]
        needs = tuple(dict.fromkeys(str(n).strip().upper() for n in needs_raw if str(n).strip()))
        features.append(
            MissionFeature(
                key=key,
                sentence=sentence,
                needs=needs,
                no_sign_in=item.get("no_sign_in") is True,
            )
        )
    for feature in features:
        for needed in feature.needs:
            if needed not in seen:
                return (
                    f"{feature.key} needs {needed}, which is not in this mission, "
                    "so nothing was started."
                )
    if waves(features) is None:
        return (
            "The features' needs go round in a circle, so no feature could ever "
            "start. Nothing was started."
        )
    longest = max(features, key=lambda f: len(f.key)).key
    attempt_id = attempt_correlation_id(mission_correlation_id, longest, 2)
    try:
        validate_feature_routing_id(attempt_id)
        acceptable = len(attempt_id) <= 128 and _is_valid_correlation_id(attempt_id)
    except ValueError:
        acceptable = False
    if not acceptable:
        return (
            "This mission's message reference is too long to name its features' "
            "builds, so nothing was started. Use shorter keys."
        )
    return title, features


def waves(features: Sequence[MissionFeature]) -> list[list[MissionFeature]] | None:
    """The features in the order they can run, grouped; None when there is a cycle.

    A topological sort by levels: a feature with no needs is in the first
    wave, any other in the wave after the last of the ones it needs. Within a
    wave the features keep the order they were given.
    """
    by_key = {f.key: f for f in features}
    level: dict[str, int] = {}
    remaining = list(features)
    while remaining:
        progressed = False
        for feature in list(remaining):
            if all(n in level for n in feature.needs if n in by_key):
                level[feature.key] = 1 + max(
                    (level[n] for n in feature.needs if n in by_key), default=-1
                )
                remaining.remove(feature)
                progressed = True
        if not progressed:
            return None
    grouped: list[list[MissionFeature]] = []
    for feature in features:
        index = level[feature.key]
        while len(grouped) <= index:
            grouped.append([])
        grouped[index].append(feature)
    return grouped


def plan_card_rationale(mission: Mission, features: Sequence[MissionFeature]) -> str:
    """The plan card's words: what will run, in what order (design section 3)."""
    repo = mission.target_repo.rsplit("/", 1)[-1]
    count = len(features)
    lines = [
        f'Mission "{mission.title}" — {count} feature{"s" if count != 1 else ""} on {repo}'
    ]
    for index, wave in enumerate(waves(features) or [list(features)]):
        if index == 0:
            label = "First:" if len(wave) == 1 else "First, together:"
        else:
            label = "Then:" if len(wave) == 1 else "Then, together:"
        shown = "   ".join(
            f"{f.key}  {f.sentence}" + ("   (you said: no sign-in)" if f.no_sign_in else "")
            for f in wave
        )
        lines.append(f"{label:<18}{shown}")
    lines.append(
        f"Approve = start now. Each feature merges into {mission.branch} once its "
        "checks pass;"
    )
    lines.append("nothing reaches main until you approve the merge card at the end.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


class MissionFlow:
    """Proposes a mission, waits for its plan card's answer, and starts it.

    Everything outside the ledger arrives as a callable, so a test drives it
    with fakes:

    * ``publish_card(envelope)`` — the planning pause publisher (the approval
      request first, then the ``build_paused`` mirror Jarvis draws the card
      from);
    * ``subscriber_factory(expected_approver, armed)`` — an approval
      subscriber, as the planning driver uses;
    * ``notify(correlation_id, message, parent_request_id=…)`` — one line in
      the mission's thread;
    * ``routing_gate`` — the feature routing gate, or None when routing is
      not configured;
    * ``read_remote_tip(target_repo)`` — the project's remote default branch:
      an answer with ``ok``, ``commit`` and ``refusal``;
    * ``create_branch(project=, mission_id=, branch=, commit=)`` — the
      publisher's ``create-mission-branch`` request; its answer has
      ``outcome`` ``"created"``, ``"refused"`` or ``"unreachable"`` and a
      ``refusal`` sentence.
    """

    def __init__(
        self,
        missions: MissionStore,
        *,
        publish_card: Callable[[Any], Awaitable[None]],
        subscriber_factory: Callable[[str | None, asyncio.Event | None], Any],
        notify: Callable[..., Awaitable[None]],
        read_remote_tip: Callable[[str], Awaitable[Any]],
        create_branch: Callable[..., Awaitable[Any]] | None,
        approval_wait_seconds: int,
        routing_gate: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        spawn: Callable[[Awaitable[None]], Any] | None = None,
        merge_executor_enabled: bool = True,
        publication_enabled: bool = True,
        target_terminal_enabled: bool = True,
    ) -> None:
        self._missions = missions
        self._merge_executor_enabled = bool(merge_executor_enabled)
        self._publication_enabled = bool(publication_enabled)
        self._target_terminal_enabled = bool(target_terminal_enabled)
        self._publish_card = publish_card
        self._subscriber_factory = subscriber_factory
        self._notify = notify
        self._read_remote_tip = read_remote_tip
        self._create_branch = create_branch
        self._approval_wait_seconds = int(approval_wait_seconds)
        self._routing_gate = routing_gate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._spawn_task = spawn or asyncio.ensure_future
        #: The background tasks this flow started, held so none is collected
        #: while it runs.
        self._tasks: set[Any] = set()
        #: Missions whose card has a live waiter in this process.
        self._waiting: set[str] = set()
        #: Missions whose branch request is in flight, and when each was last sent.
        self._starting: set[str] = set()
        self._last_sent: dict[str, datetime] = {}

    def _in_the_background(self, work: Awaitable[None]) -> None:
        task = self._spawn_task(work)
        if isinstance(task, asyncio.Future):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @property
    def can_create_branches(self) -> bool:
        """Is a publisher client wired to create mission branches with?"""
        return self._create_branch is not None

    def cannot_start_reason(self) -> str | None:
        """Why no mission can run on this factory as it is set up, or None.

        Checked when a mission message arrives and again when its plan is
        approved. A mission needs a way to create its branch, the merge
        executor to merge each finished feature into that branch (without it
        nothing lands, and every admitted feature would keep its place for
        ever), publication to send each merge to the remote (without it every
        feature would stop at "publication pending"), and the planning chain
        that writes the spec and plan into the project, which is the only
        path that reads the mission branch as its start.
        """
        if self._create_branch is None:
            return "this factory cannot create a mission branch yet"
        if not self._target_terminal_enabled:
            return (
                "missions need the planning chain that writes the spec and plan "
                "into the project, and it is switched off here"
            )
        if not self._merge_executor_enabled:
            return (
                "missions need the merge step to merge each finished feature "
                "into the mission branch, and it is switched off here"
            )
        if not self._publication_enabled:
            return (
                "missions need publication to send each merged feature to the "
                "project's remote, and it is switched off here"
            )
        return None

    @property
    def missions(self) -> MissionStore:
        return self._missions

    async def _say(self, mission: Mission, message: str) -> None:
        try:
            await self._notify(
                mission.correlation_id, message, parent_request_id=mission.parent_request_id
            )
        except Exception as exc:  # noqa: BLE001 — a line never stops a mission
            logger.warning("missions: could not tell %s (%s)", mission.id, exc)

    # -- proposing --------------------------------------------------------

    def propose(
        self,
        *,
        title: str,
        target_repo: str,
        correlation_id: str,
        originating_user: str,
        features: Sequence[MissionFeature],
        parent_request_id: str | None = None,
        originating_adapter: str | None = None,
        triggered_by: str | None = None,
    ) -> tuple[Mission, bool]:
        """File the mission PROPOSED and put its plan card up (in the background).

        Idempotent on the mission message's correlation id: a redelivered
        message files nothing new and does not post a second card.
        """
        mission, created = self._missions.create_mission(
            title=title,
            target_repo=target_repo,
            correlation_id=correlation_id,
            originating_user=originating_user,
            features=features,
            parent_request_id=parent_request_id,
            originating_adapter=originating_adapter,
            triggered_by=triggered_by,
        )
        if created:
            self._start_waiting(mission, republish=False)
        return mission, created

    def _start_waiting(self, mission: Mission, *, republish: bool) -> None:
        if mission.id in self._waiting:
            return
        self._waiting.add(mission.id)
        self._in_the_background(self._wait_for_answer(mission.id))

    def _envelope(self, mission: Mission) -> tuple[Any, str]:
        from nats_core.envelope import EventType, MessageEnvelope
        from nats_core.events import ApprovalRequestPayload

        from forge.gating.identity import derive_request_id

        build_id = mission_build_id(mission.id)
        request_id = derive_request_id(
            build_id=build_id, stage_label=MISSION_PLAN_STAGE_LABEL, attempt_count=0
        )
        features = self._missions.features(mission.id)
        details = {
            "build_id": build_id,
            "feature_id": build_id,
            "feature_title": mission.title,
            "stage_label": MISSION_PLAN_STAGE_LABEL,
            "gate_mode": "MANDATORY_HUMAN_APPROVAL",
            "coach_score": None,
            "rationale": plan_card_rationale(mission, features),
            "expected_approver": mission.originating_user,
            "attempt_count": 0,
            "parent_request_id": mission.parent_request_id,
            "mission_id": mission.id,
        }
        payload = ApprovalRequestPayload(
            request_id=request_id,
            agent_id="forge",
            action_description=(
                f'The mission "{mission.title}" is waiting for your go-ahead.'
            ),
            risk_level="medium",
            details=details,
        )
        envelope = MessageEnvelope(
            source_id="forge",
            event_type=EventType.APPROVAL_REQUEST,
            correlation_id=mission.correlation_id,
            payload=payload.model_dump(mode="json"),
        )
        return envelope, request_id

    def _remaining_seconds(self, mission: Mission) -> float:
        try:
            created = datetime.fromisoformat(mission.created_at)
        except ValueError:
            return float(self._approval_wait_seconds)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        waited = (self._clock() - created).total_seconds()
        return float(self._approval_wait_seconds) - waited

    async def _wait_for_answer(self, mission_id: str) -> None:
        """Arm, post the card, and act on the first answer that counts."""
        try:
            await self._wait_for_answer_inner(mission_id)
        except Exception:  # noqa: BLE001 — logged; tend() re-arms next tick
            logger.exception("missions: waiting for %s's plan card failed", mission_id)
        finally:
            self._waiting.discard(mission_id)

    async def _wait_for_answer_inner(self, mission_id: str) -> None:
        published = False
        while True:
            mission = self._missions.get(mission_id)
            if mission is None or mission.status != "PROPOSED" or mission.approved_at:
                return
            remaining = self._remaining_seconds(mission)
            if remaining <= 0:
                if self._missions.set_status(
                    mission.id,
                    "REJECTED",
                    expected=("PROPOSED",),
                    reason="nobody answered the mission plan card in time",
                ):
                    await self._say(
                        mission,
                        f'Mission "{mission.title}" was not started: nobody answered '
                        "its plan card in time. Nothing was built.",
                    )
                return
            envelope, request_id = self._envelope(mission)
            armed = asyncio.Event()
            subscriber = self._subscriber_factory(mission.originating_user, armed)
            wait_task = asyncio.ensure_future(
                subscriber.await_response(
                    mission_build_id(mission.id),
                    stage_label=MISSION_PLAN_STAGE_LABEL,
                    attempt_count=0,
                    timeout_seconds=max(1, int(remaining)),
                )
            )
            try:
                await asyncio.wait_for(armed.wait(), timeout=_ARM_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                wait_task.cancel()
                logger.error(
                    "missions: %s's plan card subscription did not arm; trying again",
                    mission.id,
                )
                await asyncio.sleep(1.0)
                continue
            if not published:
                # Arm before post, so an answer can never outrun the waiter. A
                # card re-armed after a restart is the same card, word for word.
                await self._publish_card(envelope)
                self._missions.record_plan_request(mission.id, request_id)
                published = True
            response = await wait_task
            if response is None:
                continue  # the window is re-read at the top
            if getattr(response, "request_id", request_id) != request_id:
                continue
            decision = str(getattr(response, "decision", ""))
            if decision in ("approve", "override"):
                if self._missions.record_approval(
                    mission.id,
                    approved_by=str(response.decided_by),
                    plan_request_id=request_id,
                ):
                    approved = self._missions.get(mission.id)
                    assert approved is not None
                    await self.start_branch(approved)
                else:
                    await self._answered_after_a_stop(mission)
                return
            if decision == "reject":
                if self._missions.set_status(
                    mission.id, "REJECTED", expected=("PROPOSED",), reason="rejected by you"
                ):
                    await self._say(
                        mission,
                        f'Mission "{mission.title}" was not started: you rejected '
                        "its plan. Nothing was built.",
                    )
                else:
                    await self._answered_after_a_stop(mission)
                return
            # A defer keeps the card waiting.

    async def _answered_after_a_stop(self, mission: Mission) -> None:
        """A plan card answered after its mission was stopped (section 11).

        A stop while PROPOSED rejects the mission at once, but its card is
        still on the screen and its waiter still listening, so the answer
        that comes later is told, once, that it changes nothing.
        """
        current = self._missions.get(mission.id)
        if current is not None and current.stopping and current.status == "REJECTED":
            await self._say(
                current,
                f'Mission "{current.title}" was stopped before it started, so '
                "this answer changes nothing. Nothing was built.",
            )

    # -- after Approve: STARTING -------------------------------------------

    async def _ready_to_start(self, mission: Mission) -> bool:
        """Check the factory can run it and seed routing, from STARTING. False
        after moving the mission to NOT_STARTED.

        Run on every pass over a STARTING mission — straight after the
        approval and again after a restart — before the branch is asked for.
        The seed is idempotent: one that SUCCEEDED returns its stored receipt
        and makes no second seed. One that is UNKNOWN or FAILED is never
        retried (the gate's seed-once rule): the mission becomes NOT_STARTED.
        """
        current = self._missions.get(mission.id)
        if current is not None and current.stopping:
            return False  # a stop was asked for: no seed, no branch; it drains
        unable = self.cannot_start_reason()
        if unable is not None:
            # The factory is set up differently from when the card went out (a
            # restart with other settings). Nothing has been seeded or built.
            await self._not_started(mission, unable)
            return False
        if self._routing_gate is None:
            return True
        try:
            await self._routing_gate.ensure_seeded(
                mission.correlation_id, origin_kind="mission", origin_id=mission.id
            )
        except Exception as exc:  # noqa: BLE001 — never retried (seed once)
            logger.warning(
                "missions: %s routing seed did not succeed (%s: %s); NOT_STARTED",
                mission.id,
                type(exc).__name__,
                exc,
            )
            if self._missions.set_status(
                mission.id, "NOT_STARTED", expected=("STARTING",), reason=ROUTING_NOT_SET_UP
            ):
                await self._say(
                    mission,
                    f'Mission "{mission.title}" was not started: '
                    f"{ROUTING_NOT_SET_UP}. You can send it again.",
                )
            return False
        return True

    async def start_branch(self, mission: Mission) -> None:
        """Seed routing, store the main tip, ask for the branch, and start on yes.

        The mission is already STARTING (the approval wrote it), so every
        maintenance check counts it as active from the first step. Each step is
        written down before the next: the routing seed, then ``base_commit`` on
        the mission row before the request is sent, and the attempts filed only
        once the branch is confirmed. A restart therefore repeats the same
        steps — the seed returns its receipt, the same request is sent again —
        and can never find a feature merged on an unconfirmed branch.
        """
        if mission.id in self._starting:
            return
        self._starting.add(mission.id)
        try:
            # THE PROJECT'S OWN SANDBOX IS STARTED FIRST when it may have been
            # stopped (one sandbox per project, stage S2, design 2.3a): before
            # any helper call, on approval and on every tend. With no sandbox
            # gate installed this is nothing at all.
            from forge.sandbox_gate.gate import using

            async with using(mission.target_repo, purpose=f"mission {mission.id}'s branch start"):
                await self._start_branch(mission)
        finally:
            self._starting.discard(mission.id)

    async def _start_branch(self, mission: Mission) -> None:
        self._last_sent[mission.id] = self._clock()
        if not await self._ready_to_start(mission):
            return
        commit = mission.base_commit
        if not commit:
            try:
                tip = await self._read_remote_tip(mission.target_repo)
            except Exception as exc:  # noqa: BLE001 — read again next time
                logger.warning("missions: %s main tip not read (%s)", mission.id, exc)
                return
            if tip is None or not getattr(tip, "ok", False):
                reason = str(getattr(tip, "refusal", None) or "the remote gave no answer")
                await self._not_started(
                    mission, f"the project's remote could not be read: {reason}"
                )
                return
            commit = str(tip.commit)
            if not self._missions.set_base_commit(mission.id, commit):
                again = self._missions.get(mission.id)
                commit = (again.base_commit if again else None) or commit
        if self._create_branch is None:
            logger.error(
                "missions: %s is STARTING but nothing here can ask the publisher "
                "for a mission branch",
                mission.id,
            )
            return
        answer = await self._create_branch(
            project=mission.target_repo,
            mission_id=mission.id,
            branch=mission.branch,
            commit=commit,
        )
        outcome = str(getattr(answer, "outcome", "unreachable"))
        if outcome == "created":
            # The move to RUNNING and the attempts are one transaction that
            # checks STARTING and no stop asked for (section 11): a branch
            # answer that arrives after a stop files nothing.
            if self._missions.start_running(mission.id) is not None:
                self._last_sent.pop(mission.id, None)
                await self._say(
                    mission,
                    f'Mission "{mission.title}" has started on {mission.branch}. '
                    "Each feature merges there once its checks pass; nothing "
                    "reaches main until you approve the merge card at the end.",
                )
            return
        sentence = str(getattr(answer, "refusal", None) or "the publisher gave no reason")
        if outcome == "refused":
            await self._not_started(
                mission, f"the mission branch was not created: {sentence}"
            )
            return
        logger.warning(
            "missions: %s branch request had no answer (%s); asking again later",
            mission.id,
            sentence,
        )

    async def _not_started(self, mission: Mission, reason: str) -> None:
        if self._missions.set_status(
            mission.id, "NOT_STARTED", expected=("STARTING",), reason=reason
        ):
            await self._say(
                mission,
                f'Mission "{mission.title}" was not started: {reason}. Nothing was built.',
            )

    # -- each tick --------------------------------------------------------

    async def tend(self) -> None:
        """Re-arm PROPOSED missions' cards and resend STARTING missions' requests.

        Run from the work queue loop's tick. An unapproved PROPOSED mission
        with no waiter in this process (the forge restarted) gets its card
        re-armed and posted again, the same card. A STARTING mission goes
        through its start again — the seed (its stored receipt, or NOT_STARTED
        if it is unknown or failed), the stored main tip, the branch request —
        at most once a minute, the first time straight after a restart.
        """
        for mission in self._missions.list_by_status(("PROPOSED",)):
            if mission.approved_at:
                continue  # never left so by this version; nothing to re-arm
            self._start_waiting(mission, republish=True)
        now = self._clock()
        for mission in self._missions.list_by_status(("STARTING",)):
            if mission.stopping:
                continue  # a stopped mission asks for no branch; it drains
            last = self._last_sent.get(mission.id)
            if last is not None and (now - last).total_seconds() < _RESEND_SECONDS:
                continue
            if mission.id in self._starting:
                continue
            self._in_the_background(self.start_branch(mission))
