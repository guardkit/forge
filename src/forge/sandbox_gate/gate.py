"""One gate per managed sandbox, in the coordinator (one sandbox per project,
stage S2, design sections 2.3 and 2.3a, 18 October 2026).

A sandbox is *managed* when every repository mapped to it says
``idle_stop: true`` in ``planning.sandboxes``, ``planning.sandbox_starter.url``
is set, and the starter lists it. Everything else passes straight through:
with no gate installed, or for an unmanaged sandbox, every entry point is
exactly what it was.

Each managed sandbox has one state (``unknown``, ``starting``, ``ready``,
``stopping`` or ``stopped``), one :class:`asyncio.Lock`, a count of the steps
using it now, and the moment it was last used. Every decision is made inside
this one process, so starting and stopping never race:

- **Use** (``async with gate.use(repo)``): a step that reaches the sandbox
  takes the gate. ``ready``: it goes on at once. Otherwise the gate brings the
  sandbox up — waits out a stop, asks the starter to start it, waits for both
  services to answer and for any redeploy the starter is waiting for — and
  then lets the step go on. A sandbox that is not ready within the limit is
  said in one plain line naming what did not answer, and the step goes on to
  meet its own failure, which it already reports.
- **Admission** (:meth:`SandboxGate.admits`): the work queue and the mission
  step admit work for a sandbox only when it is ``ready``. Otherwise the gate
  starts bringing it up in the background, and the work stays queued.
- **Stop**: once a minute (:meth:`SandboxGate.poll_once`) the gate reads the
  starter's status for each managed sandbox. A sandbox that is ``ready``,
  that no step is using, and for which the ledger has shown nothing
  unfinished (:func:`forge.sandbox_gate.idle.what_keeps_it_up`) for the idle
  time, is set ``stopping`` under the lock; then the starter is asked to stop
  it. The starter decides: it refuses while a hold is open, an operation is
  queued or running, the sandbox is suspended, the helper is still executing
  something, or a container other than the recorded deployment is running.
  Success sets ``stopped``; any other answer sets ``unknown``.
- **Leaving ``unknown``** needs the starter's status to show no operation
  queued, running or recovering; only then do the two readiness checks count
  (the helper's ``/healthz`` says ``"healthy"``; the runner's ``/ok`` says
  ``"ok":true``).
- **Redeploy**: after the starter has started a sandbox it reports "redeploy
  pending". The gate brings the recorded deployment back through the
  factory's own route (the ``redeploy`` it is given), then tells the starter
  ``ready``, or ``suspend`` with the reason when the redeploy failed. A
  sandbox whose repositories have no recorded deployment is told ``ready`` at
  once.

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

from forge.sandbox_gate.idle import what_keeps_it_up
from forge.sandbox_gate.starter_client import (
    NO_DEPLOYMENT,
    UNSAID,
    StarterClient,
    StarterStatus,
)

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

#: How often a sandbox being brought up is looked at again.
BRING_UP_POLL_SECONDS: float = 3.0
#: How long after asking for a start the gate asks again, when the starter
#: still reports the sandbox stopped and nothing queued.
START_ASK_AGAIN_SECONDS: float = 30.0


class SandboxNotReady(Exception):
    """A sandbox did not become ready within the limit; the message says why."""


@dataclass(frozen=True)
class RedeployOutcome:
    """What the redeploy route said: back and verified, or why not."""

    ok: bool
    sentence: str = ""


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
    #: Set whenever no stop is in flight; a bring-up waits on it.
    stop_settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: The last bring-up's failure, for the work queue to close held rows with.
    failure: str | None = None
    #: The starter does not manage this name: the gate passes straight through.
    unmanaged: str | None = None
    #: Lines already said, so a held queue says each once.
    said: set[str] = field(default_factory=set)

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
        deployment_of: Callable[[str, tuple[str, ...]], dict[str, str] | None] | None = None,
    ) -> None:
        #: The recorded deployment's compose project and identity label, as
        #: the starter checks the engine's containers against them, for a
        #: sandbox whose repositories have one. ``None`` from it, or no reader,
        #: leaves the starter's own settings file to say.
        self._deployment_of = deployment_of
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
        for name, (entry, repos) in sandboxes.items():
            slot = _Slot(
                name=name,
                repos=tuple(repos),
                sidecar_url=str(entry.sidecar_url).rstrip("/"),
                runner_url=str(entry.runner_url).rstrip("/"),
            )
            self._slots[name] = slot
            for repo in repos:
                self._by_repo[repo] = slot

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

    # -- admission ------------------------------------------------------------

    def admits(self, repo: str | None) -> bool:
        """May work for ``repo`` be admitted now? Synchronous on purpose.

        True for a repository whose sandbox is not managed, or is ``ready``.
        Otherwise the sandbox starts being brought up in the background and the
        answer is False: the work stays queued. The caller admits in the same
        step, with no wait in between, so no stop can be decided in the gap.
        """
        slot = self._slot_for(repo)
        if slot is None or slot.state == READY:
            return True
        self._kick(slot)
        return False

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
    async def use(self, repo: str | None, *, purpose: str = "a step") -> AsyncIterator[None]:
        """Hold ``repo``'s sandbox up while the body runs; start it first if needed."""
        slot = self._slot_for(repo)
        if slot is None:
            yield
            return
        await self._enter(slot, purpose)
        try:
            yield
        finally:
            slot.users = max(0, slot.users - 1)
            slot.last_used = self._clock()

    async def _enter(self, slot: _Slot, purpose: str) -> None:
        while True:
            async with slot.lock:
                if slot.state == READY:
                    slot.users += 1
                    return
            task = self._kick(slot)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            if slot.state == READY:
                continue
            failure = slot.failure or f"the sandbox {slot.name} is not ready"
            logger.warning(
                "sandbox gate: %s for %s goes ahead without a ready sandbox — %s",
                purpose,
                slot.name,
                failure,
            )
            # Counted all the same: the step may still reach the sandbox, and a
            # step in progress must never meet a stop.
            slot.users += 1
            return

    async def ensure_ready(self, name: str) -> bool:
        """Bring one sandbox up (boot uses it). True when it is ready."""
        slot = self._slots.get(name)
        if slot is None or slot.unmanaged is not None:
            return True
        if slot.state == READY:
            return True
        await asyncio.shield(self._kick(slot))
        return slot.state == READY or slot.unmanaged is not None

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
        connection = self._connection()
        if connection is None:
            return False
        clauses = " OR ".join(
            "(target = ? OR substr(target, 1, ?) = ?)" for _ in slot.repos
        )
        params: list[object] = []
        for repo in slot.repos:
            params.extend([repo, len(repo) + 2, f"{repo}::"])
        try:
            row = connection.execute(
                f"SELECT 1 FROM deployment_targets WHERE ({clauses}) "
                "AND running_commit IS NOT NULL AND trim(running_commit) <> '' LIMIT 1",
                tuple(params),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            raise
        return row is not None

    def _deployment_for(self, slot: _Slot) -> dict[str, str] | None:
        """What to tell the starter about the recorded deployment: none when
        the ledger has none; its compose project and identity label when they
        can be read; otherwise nothing, and the starter's settings decide."""
        if not self._has_recorded_deployment(slot):
            return NO_DEPLOYMENT
        if self._deployment_of is None:
            return UNSAID
        try:
            return self._deployment_of(slot.name, slot.repos)
        except Exception:  # noqa: BLE001 — unsaid is the starter's own settings
            logger.exception("sandbox gate: reading %s's deployment raised", slot.name)
            return UNSAID

    async def _settle_redeploy(self, slot: _Slot) -> str | None:
        """Answer the starter's "redeploy pending". ``None`` once it has been
        told ready or suspended; otherwise why not (tried again)."""
        if not self._has_recorded_deployment(slot):
            answer = await self._starter.ready(slot.name)
            return None if answer.ok else (
                f"the starter was not told {slot.name} is ready ({answer.sentence})"
            )
        if self._redeploy is None:
            reason = (
                "this coordinator has no route to bring the recorded deployment "
                "back, so automatic management is suspended and the sandbox stays up"
            )
            outcome = RedeployOutcome(ok=False, sentence=reason)
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
        if outcome.ok:
            answer = await self._starter.ready(slot.name)
            if not answer.ok:
                return f"the starter was not told {slot.name} is ready ({answer.sentence})"
            logger.info("sandbox gate: %s's deployment is back and verified", slot.name)
            return None
        answer = await self._starter.suspend(slot.name, outcome.sentence)
        logger.error(
            "sandbox gate: the redeploy for %s failed, so its automatic management "
            "is suspended and it stays up — %s",
            slot.name,
            outcome.sentence,
        )
        if not answer.ok:
            return f"the starter was not told to suspend {slot.name} ({answer.sentence})"
        return None

    async def _bring_up_once(self, slot: _Slot) -> None:
        """Bring ``slot`` to ``ready``, or raise :class:`SandboxNotReady`."""
        started = self._clock()
        deadline = started + timedelta(seconds=self._ready_timeout)
        last_start_ask: datetime | None = None
        why = "the starter did not answer"
        while True:
            if slot.state == STOPPING:
                remaining = max(0.0, (deadline - self._clock()).total_seconds())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(slot.stop_settled.wait(), timeout=remaining or 0.001)
                why = f"a stop of {slot.name} was still being answered"
                if self._clock() >= deadline:
                    break
                continue
            status = await self._starter.status(slot.name)
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
                    slot.state = STARTING
                    logger.info("sandbox gate: asking the starter to start %s", slot.name)
                    answer = await self._starter.start(
                        slot.name, deployment=self._deployment_for(slot)
                    )
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
                elif status.redeploy_pending:
                    not_settled = await self._settle_redeploy(slot)
                    if not_settled is None:
                        self._now_ready(slot)
                        return
                    why = not_settled
                else:
                    self._now_ready(slot)
                    return
            if self._clock() >= deadline:
                break
            await self._sleep(self._poll)
        if slot.state != STOPPING:
            slot.state = UNKNOWN
        minutes = max(1, int(round(self._ready_timeout / 60)))
        raise SandboxNotReady(
            f"the sandbox {slot.name} was not ready within {minutes} minute"
            f"{'' if minutes == 1 else 's'}: {why}"
        )

    def _now_ready(self, slot: _Slot) -> None:
        if slot.state != READY:
            logger.info("sandbox gate: %s is ready", slot.name)
        slot.state = READY
        slot.failure = None
        slot.said.clear()
        slot.idle_since = None

    # -- the once-a-minute look: redeploys and idle stops ---------------------

    def _what_keeps_it_up(self, slot: _Slot) -> str | None:
        if slot.users > 0:
            return f"{slot.users} step(s) are using it"
        connection = self._connection()
        if connection is None:
            return "the ledger cannot be read"
        return what_keeps_it_up(connection, slot.repos)

    async def poll_once(self) -> None:
        """Read each managed sandbox's status; redeploy, settle or stop it."""
        for slot in list(self._slots.values()):
            if slot.unmanaged is not None:
                continue
            try:
                await self._poll_slot(slot)
            except Exception:  # noqa: BLE001 — one sandbox never stops the rest
                logger.exception("sandbox gate: looking at %s raised", slot.name)

    async def _poll_slot(self, slot: _Slot) -> None:
        status = await self._starter.status(slot.name)
        if not status.reached:
            return
        if not status.listed:
            self._kick(slot)  # marks it unmanaged, in one line
            return
        if slot.state == STOPPING:
            return
        if status.settled and status.stopped and slot.state == READY and slot.users == 0:
            # Stopped behind the coordinator's back (a session, or the machine):
            # the next step starts it again.
            slot.state = STOPPED
            return
        if status.settled and status.services_up and (
            slot.state != READY or status.redeploy_pending
        ):
            # Woken by a hold, or waiting for its redeploy: bring it to ready
            # here, so it can also be stopped again once idle.
            if status.redeploy_pending and slot.state == READY:
                slot.state = UNKNOWN
            self._kick(slot)
            return
        await self._consider_stop(slot, status)

    async def _consider_stop(self, slot: _Slot, status: StarterStatus) -> None:
        now = self._clock()
        async with slot.lock:
            if slot.state != READY:
                slot.idle_since = None
                return
            busy = self._what_keeps_it_up(slot)
            if busy is not None:
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
        try:
            logger.info(
                "sandbox gate: asking the starter to stop %s — nothing open for %d minutes",
                slot.name,
                int(self._idle.total_seconds() // 60),
            )
            answer = await self._starter.stop(
                slot.name, deployment=self._deployment_for(slot)
            )
        except Exception as exc:  # noqa: BLE001 — an unclear answer is unknown
            answer = None
            logger.warning("sandbox gate: stopping %s raised (%s)", slot.name, exc)
        finally:
            slot.idle_since = None
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
        unfinished work for, all at once. Returns the names that are ready;
        never raises."""
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
        brought: list[str] = []
        for slot, answer in zip(wanted, answers):
            if answer is True:
                brought.append(slot.name)
                continue
            logger.warning(
                "sandbox gate: %s was not ready at start-up — %s; recovery goes on",
                slot.name,
                slot.failure or answer,
            )
            slot.failure = None
        return brought


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


def using(repo: str | None, *, purpose: str = "a step") -> contextlib.AbstractAsyncContextManager[None]:
    """``async with using(repo):`` — the installed gate's :meth:`SandboxGate.use`,
    or nothing at all when no gate is installed."""
    gate = _installed
    if gate is None:
        return contextlib.nullcontext()
    return gate.use(repo, purpose=purpose)


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

