"""One gate per managed sandbox, in the coordinator (one sandbox per project,
stage S2, design sections 2.3 and 2.3a, 18 October 2026; Codex S2
implementation review 1, 19 October 2026).

A sandbox is *managed* when every repository mapped to it says
``idle_stop: true`` in ``planning.sandboxes``, ``planning.sandbox_starter.url``
is set, and the starter lists it. Everything else passes straight through:
with no gate installed, or for an unmanaged sandbox, every entry point is
exactly what it was.

Each managed sandbox has one state (``unknown``, ``starting``, ``ready``,
``stopping``, ``stopped`` or ``suspended``), one :class:`asyncio.Lock`, a
count of the steps using it now, and the moment it was last used. Every
decision is made inside this one process, so starting and stopping never race.
The blunt rules:

- **A gated step never runs on a sandbox that is not ready.**
  ``async with gate.use(repo)`` waits out a stop, starts the sandbox, waits for
  both services and any redeploy, and only then enters the body. If that does
  not happen within the limit — the starter unreachable, a stop or a redeploy
  still pending, a service not answering — it raises :class:`SandboxNotReady`
  with a plain reason, and the caller defers or reports; the body never runs.
- **Admission** (:meth:`SandboxGate.admits`) admits work only for a ``ready``
  sandbox; otherwise the sandbox is brought up in the background and the work
  stays queued, to be tried again on a later pass.
- **Suspended is not ready.** A failed redeploy makes the starter suspend
  automatic management, durably. The gate then holds the sandbox ``suspended``:
  work that needs the restored deployment (a merge that deploys) is refused
  with one plain line; work that does not (planning, building, previews,
  landings on a mission branch) goes on while the services answer; no redeploy
  runs and no stop is asked for until a session clears it at the starter
  (``POST /clear/<name>``), after which the next look starts over.
- **Stop**: once a minute (:meth:`SandboxGate.poll_once`) the gate reads the
  starter's status for each managed sandbox. A ``ready`` sandbox that no step
  is using, for which the ledger has shown nothing unfinished for the idle
  time, is set ``stopping`` under the lock, and its stop runs as a task of its
  own: the poll never waits for it. The starter decides; success sets
  ``stopped`` and any other answer ``unknown``.
- **Leaving ``unknown``** needs the starter's status to show no operation
  queued, running or recovering; only then do the two readiness checks count
  (the helper's ``/healthz`` says ``"healthy"``; the runner's ``/ok`` says
  ``"ok":true``).
- **The deployment is always said.** Every start, stop and ready names the
  recorded deployment (:mod:`forge.sandbox_gate.deployment`), or says there is
  none: the starter keeps what it was told durably.
- **Redeploy**: when the starter reports "services ready, redeploy pending"
  (or the gate itself started a sandbox whose ledger records a deployment),
  the gate brings the recorded deployment back through the factory's own route
  and tells the starter ``ready`` for that deployment, or ``suspend`` with the
  reason.
- **Boot**: before recovery, every managed sandbox with unfinished work is
  brought up. Recovery that reaches a sandbox which did not come up is
  deferred for that sandbox's repositories (:meth:`SandboxGate.defer`) and run
  when it is ready; recovery for every other repository goes on.

The state lives in memory only. After a coordinator restart every sandbox is
``unknown`` and the idle clocks start again from zero.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forge.sandbox_gate.deployment import deployment_to_tell
from forge.sandbox_gate.idle import what_keeps_it_up
from forge.sandbox_gate.starter_client import StarterClient, StarterStatus

logger = logging.getLogger(__name__)

__all__ = [
    "RedeployOutcome",
    "SandboxGate",
    "SandboxNotReady",
    "build_sandbox_gate",
    "current_gate",
    "install_gate",
    "managed_sandboxes",
]

UNKNOWN = "unknown"
STARTING = "starting"
READY = "ready"
STOPPING = "stopping"
STOPPED = "stopped"
SUSPENDED = "suspended"

#: How often a sandbox being brought up is looked at again.
BRING_UP_POLL_SECONDS: float = 3.0
#: How long after asking for a start the gate asks again, when the starter
#: still reports the sandbox stopped and nothing queued.
START_ASK_AGAIN_SECONDS: float = 30.0


class SandboxNotReady(RuntimeError):
    """A sandbox is not ready for a step; the message says why, plainly."""


@dataclass(frozen=True)
class RedeployOutcome:
    """What the redeploy route said: back and verified, or why not.

    ``retry`` marks a redeploy that did not run for a reason that passes by
    itself (another holder of the deployment lock, an earlier promote still
    running): it is asked for again, and management is not suspended for it.
    """

    ok: bool
    sentence: str = ""
    retry: bool = False


#: ``(GET url, timeout seconds) -> (HTTP status, body text)``; raises when the
#: address cannot be reached.
HttpGet = Callable[[str, float], tuple[int, str]]


def _urllib_get(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(65536).decode("utf-8", "replace") if exc.fp is not None else ""
        return int(exc.code), body


@dataclass
class _Slot:
    name: str
    repos: tuple[str, ...]
    entry: Any
    sidecar_url: str
    runner_url: str
    state: str = UNKNOWN
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    #: When the ledger and the gate were first seen idle in a row of polls.
    idle_since: datetime | None = None
    #: When a step last finished using the sandbox.
    last_used: datetime | None = None
    bring_up: asyncio.Task[None] | None = None
    #: The stop in flight, run as a task of its own (R6).
    stop_task: asyncio.Task[None] | None = None
    #: Set whenever no stop is in flight; a bring-up waits on it.
    stop_settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: The last bring-up's failure, said once by whoever reads it.
    failure: str | None = None
    #: The starter does not manage this name: the gate passes straight through.
    unmanaged: str | None = None
    #: Lines already said, so a held queue says each once.
    said: set[str] = field(default_factory=set)
    #: The gate itself started the sandbox in this bring-up: a recorded
    #: deployment is brought back even if the starter does not report
    #: "redeploy pending".
    woke_it: bool = False
    #: Why management is suspended, while it is.
    suspended_reason: str | None = None
    #: What the starter last reported as the sandbox's deployment.
    starter_deployment: dict[str, Any] | None = None
    #: Recovery deferred until the sandbox is ready (boot).
    deferred: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stop_settled.set()


def managed_sandboxes(config: Any) -> dict[str, tuple[Any, tuple[str, ...]]]:
    """``{sandbox name: (one entry, every repository mapped to it)}`` for the
    sandboxes whose entries say ``idle_stop``. The shared sandbox never is."""
    from forge.config.sandbox_slots import LEGACY_SANDBOX
    from forge.config.sandboxes import sandboxes_of

    found: dict[str, tuple[Any, list[str]]] = {}
    for repo, entry in sandboxes_of(config).items():
        if not getattr(entry, "idle_stop", False) or entry.name == LEGACY_SANDBOX:
            continue
        found.setdefault(entry.name, (entry, []))[1].append(str(repo))
    return {name: (entry, tuple(sorted(repos))) for name, (entry, repos) in found.items()}


class SandboxGate:
    """The coordinator's gates, one per managed sandbox."""

    def __init__(
        self,
        *,
        sandboxes: dict[str, tuple[Any, tuple[str, ...]]],
        starter: StarterClient,
        connection: Callable[[], sqlite3.Connection | None],
        default_repo: str | None = None,
        idle_minutes: int = 30,
        ready_timeout_seconds: float = 600.0,
        redeploy: Callable[[str, tuple[str, ...]], Awaitable[RedeployOutcome]] | None = None,
        http_get: HttpGet | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        poll_interval_seconds: float = BRING_UP_POLL_SECONDS,
        status_poll_seconds: float = 60.0,
    ) -> None:
        self._starter = starter
        #: How often :meth:`run_forever` looks at the starter.
        self.poll_seconds = float(status_poll_seconds)
        self._connection = connection
        self._default_repo = str(default_repo or "").strip() or None
        self._idle = timedelta(minutes=int(idle_minutes))
        self._ready_timeout = float(ready_timeout_seconds)
        self._redeploy = redeploy
        self._http_get = http_get or _urllib_get
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._poll = float(poll_interval_seconds)
        self._slots: dict[str, _Slot] = {}
        self._by_repo: dict[str, _Slot] = {}
        #: Background work this gate started (deferred recovery), held so none
        #: is collected while it runs.
        self._tasks: set[asyncio.Future[Any]] = set()
        for name, (entry, repos) in sandboxes.items():
            slot = _Slot(
                name=name,
                repos=tuple(repos),
                entry=entry,
                sidecar_url=str(entry.sidecar_url).rstrip("/"),
                runner_url=str(entry.runner_url).rstrip("/"),
            )
            self._slots[name] = slot
            for repo in repos:
                self._by_repo[repo] = slot

    def set_redeploy(
        self, redeploy: Callable[[str, tuple[str, ...]], Awaitable[RedeployOutcome]] | None
    ) -> None:
        """Install the route that brings a recorded deployment back (the
        coordinator composes it once its message bus is connected)."""
        self._redeploy = redeploy

    # -- what is managed ------------------------------------------------------

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._slots))

    def state_of(self, name: str) -> str | None:
        slot = self._slots.get(name)
        return slot.state if slot is not None else None

    def users_of(self, name: str) -> int:
        slot = self._slots.get(name)
        return slot.users if slot is not None else 0

    def sandbox_for(self, repo: str | None) -> str | None:
        """The managed sandbox ``repo`` builds in, or ``None`` (pass through)."""
        slot = self._slot_for(repo)
        return slot.name if slot is not None else None

    def _slot_for(self, repo: str | None) -> _Slot | None:
        key = str(repo or "").strip() or self._default_repo
        if not key:
            return None
        slot = self._by_repo.get(key)
        if slot is None or slot.unmanaged is not None:
            return None
        return slot

    @staticmethod
    def _passes(slot: _Slot, needs_deployment: bool) -> bool:
        return slot.state == READY or (slot.state == SUSPENDED and not needs_deployment)

    def _suspended_sentence(self, slot: _Slot) -> str:
        return (
            f"automatic management of the sandbox {slot.name} is suspended "
            f"({slot.suspended_reason or 'no reason given'}), so work that needs its "
            "restored deployment waits until a session clears it at the starter "
            f"(POST /clear/{slot.name})"
        )

    # -- admission ------------------------------------------------------------

    def admits(self, repo: str | None, *, needs_deployment: bool = False) -> bool:
        """May work for ``repo`` be admitted now? Synchronous on purpose.

        True for a repository whose sandbox is not managed, or is ``ready``
        (or ``suspended``, for work that does not need its deployment).
        Otherwise the sandbox starts being brought up in the background and
        the answer is False: the work stays queued. The caller admits in the
        same step, with no wait in between, so no stop can be decided in the gap.
        """
        slot = self._slot_for(repo)
        if slot is None or self._passes(slot, needs_deployment):
            return True
        if slot.state != SUSPENDED:
            self._kick(slot)
        return False

    def why_held(self, repo: str | None) -> str | None:
        """Why work for ``repo`` is held now, in words, or None."""
        slot = self._slot_for(repo)
        if slot is None:
            return None
        if slot.state == SUSPENDED:
            return self._suspended_sentence(slot)
        if slot.failure:
            return slot.failure
        return f"waiting for the sandbox {slot.name} to start"

    def take_failure(self, repo: str | None) -> str | None:
        """The last bring-up failure for ``repo``'s sandbox, once, or None."""
        slot = self._slot_for(repo)
        if slot is None or slot.failure is None:
            return None
        failure, slot.failure = slot.failure, None
        return failure

    def say_once(self, repo: str | None, line: str) -> bool:
        """True the first time ``line`` is offered for this sandbox since it was
        last ready (the queue says "holding" once, not every ten seconds)."""
        slot = self._slot_for(repo)
        if slot is None or line in slot.said:
            return False
        slot.said.add(line)
        return True

    def defer(self, repo: str | None, action: Callable[[], Awaitable[Any]]) -> bool:
        """Run ``action`` once ``repo``'s sandbox is ready, if it is not ready
        now. True when deferred; False means run it now (unmanaged or ready)."""
        slot = self._slot_for(repo)
        if slot is None or self._passes(slot, needs_deployment=False):
            return False
        slot.deferred.append(action)
        logger.info(
            "sandbox gate: recovery for %s waits until the sandbox %s is ready",
            repo,
            slot.name,
        )
        self._kick(slot)
        return True

    def _kick(self, slot: _Slot) -> asyncio.Task[None]:
        if slot.bring_up is None or slot.bring_up.done():
            slot.bring_up = asyncio.ensure_future(self._bring_up_quietly(slot))
        return slot.bring_up

    async def _bring_up_quietly(self, slot: _Slot) -> None:
        try:
            await self._bring_up_once(slot)
        except SandboxNotReady as exc:
            slot.failure = str(exc)
            logger.warning("sandbox gate: %s", exc)
        except Exception:  # noqa: BLE001 — a background bring-up never raises
            logger.exception("sandbox gate: bringing up %s raised", slot.name)

    # -- use ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def use(
        self, repo: str | None, *, purpose: str = "a step", needs_deployment: bool = False
    ) -> AsyncIterator[None]:
        """Hold ``repo``'s sandbox up while the body runs; bring it up first.

        Raises :class:`SandboxNotReady` — and never runs the body — when the
        sandbox does not become ready within the limit, or is suspended and
        the step needs its deployment.
        """
        slot = self._slot_for(repo)
        if slot is None:
            yield
            return
        await self._enter(slot, purpose, needs_deployment)
        try:
            yield
        finally:
            slot.users = max(0, slot.users - 1)
            slot.last_used = self._clock()

    async def _enter(self, slot: _Slot, purpose: str, needs_deployment: bool) -> None:
        tried = False
        while True:
            async with slot.lock:
                if self._passes(slot, needs_deployment):
                    slot.users += 1
                    return
                if slot.state == SUSPENDED:
                    raise SandboxNotReady(f"{purpose} did not run: {self._suspended_sentence(slot)}")
            if tried:
                failure = slot.failure or f"the sandbox {slot.name} is not ready"
                raise SandboxNotReady(f"{purpose} did not run: {failure}")
            tried = True
            await asyncio.shield(self._kick(slot))

    async def ensure_ready(self, name: str) -> bool:
        """Bring one sandbox up (boot uses it). True when it is ready (or
        suspended with its services answering)."""
        slot = self._slots.get(name)
        if slot is None or slot.unmanaged is not None:
            return True
        if self._passes(slot, needs_deployment=False):
            return True
        await asyncio.shield(self._kick(slot))
        return self._passes(slot, needs_deployment=False) or slot.unmanaged is not None

    # -- bringing a sandbox up ------------------------------------------------

    def _probe(self, url: str, expected: str) -> bool:
        try:
            status, body = self._http_get(url, 5.0)
        except Exception:  # noqa: BLE001 — not answering is the answer
            return False
        return status == 200 and expected in body.replace(" ", "")

    async def _services_answer(self, slot: _Slot) -> str | None:
        """``None`` when both services answer as themselves; else what did not."""
        helper = await asyncio.to_thread(self._probe, f"{slot.sidecar_url}/healthz", '"healthy"')
        runner = await asyncio.to_thread(self._probe, f"{slot.runner_url}/ok", '"ok":true')
        if helper and runner:
            return None
        missing = []
        if not helper:
            missing.append("the deploy helper did not answer /healthz as healthy")
        if not runner:
            missing.append("the build runner did not answer /ok")
        return " and ".join(missing)

    def _has_recorded_deployment(self, slot: _Slot) -> bool:
        from forge.sandbox_gate.deployment import recorded_identity

        return recorded_identity(self._connection(), slot.repos) is not None

    def _starter_disagrees(self, slot: _Slot, status: StarterStatus) -> bool:
        """Does the starter hold another deployment than the ledger records?

        Checked before "ready" is ever accepted from the starter — at boot,
        after an outside wake or a services-only hold, on a poll (Codex S2
        implementation review 2, R1). A starter that says none while the
        ledger records one disagrees. The answer is then to tell it the
        recorded deployment, which makes it report "pending" again, and
        redeploy. A deployment that cannot be identified always disagrees.
        """
        told, problem = self._deployment_or_problem(slot)
        return problem is not None or (status.deployment or None) != (told or None)

    def _deployment_or_problem(self, slot: _Slot) -> tuple[dict[str, str] | None, str | None]:
        """What the starter is told about the recorded deployment, or the
        configuration problem that stops it being identified (said once)."""
        deployment, problem = deployment_to_tell(self._connection(), slot.repos, slot.entry)
        if problem and problem not in slot.said:
            slot.said.add(problem)
            logger.error("sandbox gate: %s", problem)
        return deployment, problem

    def _deployment_for(self, slot: _Slot) -> dict[str, str]:
        """What the starter is told: always said. Raises
        :class:`SandboxNotReady` with the configuration reason when the
        recorded deployment cannot be identified (Codex review 3, R1)."""
        deployment, problem = self._deployment_or_problem(slot)
        if problem is not None or deployment is None:
            raise SandboxNotReady(problem or f"{slot.name}'s deployment cannot be identified")
        return deployment

    async def _settle_redeploy(self, slot: _Slot) -> str:
        """Answer the starter's "redeploy pending". ``"ready"`` once it is told
        ready, ``"suspended"`` once it is told to suspend; otherwise why not.

        No cache (Codex review 3, R7): every confirmation that follows a
        "pending" runs the redeploy again — it promotes the same recorded
        images, so it is safe to repeat — and then posts /ready with the
        identity."""
        told = self._deployment_for(slot)
        if (slot.starter_deployment or None) != (told or None):
            # The starter's deployment is not the recorded one (it was told
            # another, or none, or woke without it): say it first, which makes
            # it report "pending" again, so its /ready can accept ours. A start
            # of a running supervisor changes nothing else.
            answer = await self._starter.start(slot.name, deployment=told)
            if not answer.ok:
                return f"the starter was not told {slot.name}'s deployment ({answer.sentence})"
            slot.starter_deployment = told or None
        if not self._has_recorded_deployment(slot):
            answer = await self._starter.ready(slot.name, deployment=told)
            return "ready" if answer.ok else (
                f"the starter was not told {slot.name} is ready ({answer.sentence})"
            )
        if self._redeploy is None:
            outcome = RedeployOutcome(
                ok=False,
                sentence=(
                    "this coordinator has no route to bring the recorded deployment "
                    "back, so automatic management is suspended and the sandbox stays up"
                ),
            )
        else:
            logger.info(
                "sandbox gate: bringing %s's recorded deployment back after a start",
                slot.name,
            )
            try:
                outcome = await self._redeploy(slot.name, slot.repos)
            except Exception as exc:  # noqa: BLE001 — a raise is a failed redeploy
                outcome = RedeployOutcome(
                    ok=False,
                    sentence=f"the redeploy raised ({type(exc).__name__}: {exc})",
                )
        if outcome.retry and not outcome.ok:
            return outcome.sentence or "the redeploy is asked for again"
        if outcome.ok:
            answer = await self._starter.ready(slot.name, deployment=told)
            if not answer.ok:
                return f"the starter was not told {slot.name} is ready ({answer.sentence})"
            logger.info("sandbox gate: %s's deployment is back and verified", slot.name)
            return "ready"
        answer = await self._starter.suspend(slot.name, outcome.sentence)
        logger.error(
            "sandbox gate: the redeploy for %s failed, so its automatic management "
            "is suspended and it stays up — %s",
            slot.name,
            outcome.sentence,
        )
        if not answer.ok:
            return f"the starter was not told to suspend {slot.name} ({answer.sentence})"
        self._now_suspended(slot, outcome.sentence)
        return "suspended"

    async def _bring_up_once(self, slot: _Slot) -> None:
        """Bring ``slot`` to ``ready`` (or ``suspended``), or raise
        :class:`SandboxNotReady`."""
        started = self._clock()
        deadline = started + timedelta(seconds=self._ready_timeout)
        last_start_ask: datetime | None = None
        why = "the starter did not answer"
        while True:
            if slot.state == STOPPING:
                # Waits out the stop in flight, a look at a time, within the
                # same limit: a step never meets a stopping sandbox.
                why = f"a stop of {slot.name} was still being answered"
                if self._clock() >= deadline:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(slot.stop_settled.wait(), timeout=0.05)
                if slot.state == STOPPING:
                    await self._sleep(self._poll)
                continue
            status = await self._starter.status(slot.name)
            if status.reached and status.listed:
                slot.starter_deployment = status.deployment
            if not status.reached:
                why = status.why or "the starter did not answer"
            elif not status.listed:
                slot.unmanaged = status.why or "not listed by the starter"
                slot.state = READY
                logger.warning(
                    "sandbox gate: %s says idle_stop, but the starter does not manage "
                    "it (%s); it is left alone and work passes straight through",
                    slot.name,
                    slot.unmanaged,
                )
                return
            elif status.unresolved:
                why = (
                    f"the starter has marked {slot.name} unresolved ({status.unresolved}) "
                    "until a session looks"
                )
            elif status.busy:
                why = f"the starter is still {status.phase or 'working'} on {slot.name}"
            elif status.stopped or not status.services_up:
                now = self._clock()
                if (
                    last_start_ask is None
                    or (now - last_start_ask).total_seconds() >= START_ASK_AGAIN_SECONDS
                ):
                    last_start_ask = now
                    # Raises, before anything is asked, when the recorded
                    # deployment cannot be identified (Codex review 3, R1).
                    told = self._deployment_for(slot)
                    slot.state = STARTING
                    logger.info("sandbox gate: asking the starter to start %s", slot.name)
                    answer = await self._starter.start(slot.name, deployment=told)
                    if answer.ok:
                        slot.woke_it = True
                    why = (
                        f"the starter refused to start {slot.name}: {answer.sentence}"
                        if answer.refused
                        else f"{slot.name} is starting"
                        if answer.ok
                        else f"the start of {slot.name} got no clear answer ({answer.sentence})"
                    )
                else:
                    why = f"{slot.name} is starting"
            else:
                missing = await self._services_answer(slot)
                if missing is not None:
                    why = missing
                elif status.suspended:
                    # No redeploy and no stop while suspended (R2): the
                    # services answer, and work that needs no deployment goes on.
                    self._now_suspended(slot, status.suspended)
                    return
                elif (
                    status.redeploy_pending
                    or (slot.woke_it and self._has_recorded_deployment(slot))
                    or self._starter_disagrees(slot, status)
                ):
                    settled = await self._settle_redeploy(slot)
                    if settled == "ready":
                        self._now_ready(slot)
                        return
                    if settled == "suspended":
                        return
                    why = f"its redeploy is still pending: {settled}"
                else:
                    self._now_ready(slot)
                    return
            if self._clock() >= deadline:
                break
            await self._sleep(self._poll)
        if slot.state not in (STOPPING, SUSPENDED):
            slot.state = UNKNOWN
        minutes = max(1, int(round(self._ready_timeout / 60)))
        raise SandboxNotReady(
            f"the sandbox {slot.name} did not start within {minutes} minute"
            f"{'' if minutes == 1 else 's'}: {why}"
        )

    def _now_ready(self, slot: _Slot) -> None:
        if slot.state != READY:
            logger.info("sandbox gate: %s is ready", slot.name)
        slot.state = READY
        slot.failure = None
        slot.woke_it = False
        slot.suspended_reason = None
        slot.said.clear()
        slot.idle_since = None
        self._run_deferred(slot)

    def _now_suspended(self, slot: _Slot, reason: str) -> None:
        if slot.state != SUSPENDED:
            logger.warning(
                "sandbox gate: %s is suspended (%s); no redeploy and no stop until a "
                "session clears it at the starter",
                slot.name,
                reason,
            )
        slot.state = SUSPENDED
        slot.suspended_reason = reason
        slot.failure = None
        slot.woke_it = False
        slot.idle_since = None
        # Deferred recovery needs services, not the deployment.
        self._run_deferred(slot)

    def _run_deferred(self, slot: _Slot) -> None:
        actions, slot.deferred = slot.deferred, []
        for action in actions:
            task = asyncio.ensure_future(self._run_one_deferred(slot, action))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_one_deferred(self, slot: _Slot, action: Callable[[], Awaitable[Any]]) -> None:
        try:
            await action()
        except Exception:  # noqa: BLE001 — logged; one never stops another
            logger.exception("sandbox gate: deferred recovery for %s raised", slot.name)

    # -- the once-a-minute look: redeploys and idle stops ---------------------

    def _what_keeps_it_up(self, slot: _Slot) -> str | None:
        if slot.users > 0:
            return f"{slot.users} step(s) are using it"
        connection = self._connection()
        if connection is None:
            return "the ledger cannot be read"
        return what_keeps_it_up(connection, slot.repos)

    async def poll_once(self) -> None:
        """Read each managed sandbox's status; redeploy, settle or start a stop.

        Never waits for a stop: each runs as a task of its own (R6)."""
        for slot in list(self._slots.values()):
            if slot.unmanaged is not None:
                continue
            try:
                await self._poll_slot(slot)
            except Exception:  # noqa: BLE001 — one sandbox never stops the rest
                logger.exception("sandbox gate: looking at %s raised", slot.name)

    async def _poll_slot(self, slot: _Slot) -> None:
        if slot.state == STOPPING or (slot.stop_task is not None and not slot.stop_task.done()):
            return
        status = await self._starter.status(slot.name)
        if not status.reached:
            return
        if not status.listed:
            self._kick(slot)  # marks it unmanaged, in one line
            return
        slot.starter_deployment = status.deployment
        if status.suspended:
            if status.settled and status.services_up:
                self._now_suspended(slot, status.suspended)
            elif slot.state in (READY, SUSPENDED):
                slot.state = UNKNOWN
            return
        if slot.state == SUSPENDED:
            # Cleared at the starter: start over from unknown.
            logger.info("sandbox gate: %s's suspension was cleared; looking again", slot.name)
            slot.state = UNKNOWN
            slot.suspended_reason = None
            self._kick(slot)
            return
        if status.settled and status.stopped and slot.state == READY and slot.users == 0:
            # Stopped behind the coordinator's back (a session, or the machine):
            # the next step starts it again.
            slot.state = STOPPED
            return
        if status.settled and status.services_up and (
            slot.state != READY
            or status.redeploy_pending
            or self._starter_disagrees(slot, status)
        ):
            # Woken by a hold, or waiting for its redeploy: bring it to ready
            # here, so it can also be stopped again once idle.
            if slot.state == READY:
                slot.state = UNKNOWN
            self._kick(slot)
            return
        if slot.deferred and slot.state != READY:
            self._kick(slot)  # recovery waits for it: keep trying
            return
        self._consider_stop(slot, status)

    def _consider_stop(self, slot: _Slot, status: StarterStatus) -> None:
        """Decide, synchronously, whether to stop; the stop runs on its own."""
        now = self._clock()
        if slot.lock.locked() or slot.state != READY:
            slot.idle_since = None
            return
        busy = self._what_keeps_it_up(slot)
        if busy is not None:
            slot.idle_since = None
            return
        if self._deployment_or_problem(slot)[1] is not None:
            # A deployment that cannot be identified: left up (R1).
            slot.idle_since = None
            return
        if status.suspended or status.busy or status.holds:
            # The starter would refuse; asking is noise. Still idle here.
            slot.idle_since = slot.idle_since or now
            return
        if slot.idle_since is None:
            slot.idle_since = now
            return
        since = slot.idle_since
        if slot.last_used is not None and slot.last_used > since:
            since = slot.idle_since = slot.last_used
        if now - since < self._idle:
            return
        slot.state = STOPPING
        slot.stop_settled.clear()
        slot.idle_since = None
        slot.stop_task = asyncio.ensure_future(self._run_stop(slot))

    async def _run_stop(self, slot: _Slot) -> None:
        answer = None
        try:
            logger.info(
                "sandbox gate: asking the starter to stop %s — nothing open for %d minutes",
                slot.name,
                int(self._idle.total_seconds() // 60),
            )
            answer = await self._starter.stop(slot.name, deployment=self._deployment_for(slot))
        except Exception as exc:  # noqa: BLE001 — an unclear answer is unknown
            logger.warning("sandbox gate: stopping %s raised (%s)", slot.name, exc)
        finally:
            if answer is not None and answer.ok:
                slot.state = STOPPED
                logger.info("sandbox gate: %s is stopped", slot.name)
            else:
                slot.state = UNKNOWN
                if answer is not None and answer.refused:
                    logger.info(
                        "sandbox gate: the starter kept %s up — %s", slot.name, answer.sentence
                    )
                else:
                    logger.warning(
                        "sandbox gate: the stop of %s got no clear answer (%s); it is "
                        "unknown until the starter says nothing is pending",
                        slot.name,
                        answer.sentence if answer is not None else "it raised",
                    )
            slot.stop_settled.set()

    async def run_forever(self, interval_seconds: float = 60.0) -> None:
        """Poll every ``interval_seconds``; a fault never stops the next poll."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("sandbox gate: the poll raised; carrying on")
            await self._sleep(interval_seconds)

    # -- boot ---------------------------------------------------------------------

    async def boot(self) -> list[str]:
        """Before recovery runs: bring up every managed sandbox the ledger shows
        unfinished work for, all at once. Returns the names NOT ready: recovery
        for their repositories is deferred (:meth:`defer`). Never raises."""
        connection = self._connection()
        wanted: list[_Slot] = []
        for slot in list(self._slots.values()):
            try:
                busy = what_keeps_it_up(connection, slot.repos) if connection is not None else None
            except sqlite3.Error as exc:
                logger.warning("sandbox gate: reading the ledger for %s failed (%s)", slot.name, exc)
                busy = "the ledger could not be read"
            if busy is None:
                continue
            logger.info(
                "sandbox gate: at start-up, bringing up %s before recovery (%s)",
                slot.name,
                busy,
            )
            wanted.append(slot)
        answers = await asyncio.gather(
            *(self.ensure_ready(slot.name) for slot in wanted), return_exceptions=True
        )
        not_ready: list[str] = []
        for slot, answer in zip(wanted, answers):
            if answer is True:
                continue
            not_ready.append(slot.name)
            logger.warning(
                "sandbox gate: %s was not ready at start-up — %s; recovery for %s waits "
                "for it, and every other repository's recovery goes on",
                slot.name,
                slot.failure or answer,
                ", ".join(slot.repos),
            )
        return not_ready


# ---------------------------------------------------------------------------
# The coordinator's one gate
# ---------------------------------------------------------------------------

_installed: SandboxGate | None = None


def install_gate(gate: SandboxGate | None) -> None:
    """The gate every entry point uses. ``None`` (the default) passes through."""
    global _installed
    _installed = gate


def current_gate() -> SandboxGate | None:
    return _installed


def admits(repo: str | None) -> bool:
    """:meth:`SandboxGate.admits` of the installed gate; True with none."""
    gate = _installed
    return True if gate is None else gate.admits(repo)


def using(
    repo: str | None, *, purpose: str = "a step", needs_deployment: bool = False
) -> contextlib.AbstractAsyncContextManager[None]:
    """``async with using(repo):`` — the installed gate's :meth:`SandboxGate.use`,
    or nothing at all when no gate is installed."""
    gate = _installed
    if gate is None:
        return contextlib.nullcontext()
    return gate.use(repo, purpose=purpose, needs_deployment=needs_deployment)


def defer(repo: str | None, action: Callable[[], Awaitable[Any]]) -> bool:
    """:meth:`SandboxGate.defer` of the installed gate; False with none."""
    gate = _installed
    return False if gate is None else gate.defer(repo, action)


def build_sandbox_gate(
    config: Any,
    *,
    connection: Callable[[], sqlite3.Connection | None],
    redeploy: Callable[[str, tuple[str, ...]], Awaitable[RedeployOutcome]] | None = None,
    starter: StarterClient | None = None,
) -> SandboxGate | None:
    """The coordinator's gate from its settings, or ``None`` when S2 is off:
    no starter address, or no sandbox says ``idle_stop``."""
    planning = getattr(config, "planning", None)
    settings = getattr(planning, "sandbox_starter", None)
    url = getattr(settings, "url", None)
    if not url:
        return None
    sandboxes = managed_sandboxes(config)
    if not sandboxes:
        return None
    return SandboxGate(
        sandboxes=sandboxes,
        starter=starter or StarterClient(url),
        connection=connection,
        default_repo=getattr(planning, "default_target_repo", None),
        idle_minutes=int(getattr(settings, "idle_minutes", 30)),
        ready_timeout_seconds=float(getattr(settings, "ready_timeout_seconds", 600)),
        redeploy=redeploy,
        status_poll_seconds=float(getattr(settings, "poll_seconds", 60)),
    )
