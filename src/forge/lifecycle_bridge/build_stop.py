"""Asking a build runner to stop a build, and holding a cancelled build's place.

A cancel used to be ``runs.cancel(action="interrupt")``: the run was marked
interrupted at once, the factory acknowledged the build's queued message —
releasing its place — and only then did the runner kill one process, while
whatever else the build had started kept going (design of 3 October 2026,
"When the build's place is released", finding R2). Two changes close that, and
both are here:

1. A cancel asks the build's runner to stop the build
   (:func:`ask_runner_to_stop`, ``POST /forge/builds/{build_id}/stop``). The
   runner answers only when everything the build owns is gone, or says what is
   still there.
2. Every acknowledgement of a cancelled build first asks the same route
   (:class:`AckAfterStop`). If the runner says "not stopped", or cannot be
   reached, the message stays unacknowledged — the place stays held — the
   reason is logged with what remains, and the question is asked again every
   30 seconds until the answer is yes. The question is stateless on the
   runner's side, so the answer is right after a runner restart and after a
   factory restart.

Builds that ended complete or failed in the ordinary way are not affected:
the check answers "not applicable" for them and the acknowledgement happens
exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

logger = logging.getLogger(__name__)

__all__ = [
    "AckAfterStop",
    "DEFAULT_RECHECK_SECONDS",
    "StopAnswer",
    "StopCheck",
    "ask_runner_to_stop",
    "stop_route_url",
]

#: How often a held acknowledgement asks again.
DEFAULT_RECHECK_SECONDS: float = 30.0

#: The runner answers after its own stop (SIGTERM grace, then SIGKILL and a
#: confirmation), so the request is given well beyond that grace.
_STOP_REQUEST_TIMEOUT_SECONDS: float = 120.0


@dataclass(frozen=True, slots=True)
class StopAnswer:
    """What the runner said about one build."""

    stopped: bool
    remaining: Any = None
    reason: str = ""
    #: The runner has no stop route (an older runner image): it can neither
    #: stop the build this way nor confirm a stop.
    route_missing: bool = False
    runner_url: str = ""


#: ``async (feature_id, correlation_id, cancelled) -> StopAnswer | None``.
#: ``None`` means the build is not one whose acknowledgement waits on a stop
#: (it ended complete or failed). ``cancelled`` is ``True`` when the caller
#: already knows the build was cancelled, ``None`` when the check should read
#: the build's recorded state itself.
StopCheck = Callable[[str, str, "bool | None"], Awaitable["StopAnswer | None"]]


def stop_route_url(runner_url: str, build_id: str) -> str:
    return f"{runner_url.rstrip('/')}/forge/builds/{quote(build_id, safe='')}/stop"


async def ask_runner_to_stop(
    runner_url: str,
    build_id: str,
    *,
    timeout_seconds: float = _STOP_REQUEST_TIMEOUT_SECONDS,
) -> StopAnswer:
    """``POST`` the runner's stop route for ``build_id``. Never raises."""
    import httpx

    url = stop_route_url(runner_url, build_id)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url)
    except Exception as exc:  # noqa: BLE001 — unreachable is an answer
        return StopAnswer(
            stopped=False,
            reason=(
                f"the build runner at {runner_url} could not be reached "
                f"({type(exc).__name__}: {exc})"
            ),
        )
    if response.status_code == 404:
        return StopAnswer(
            stopped=False,
            reason=f"the build runner at {runner_url} has no stop route (older runner image)",
            route_missing=True,
            runner_url=runner_url,
        )
    if response.status_code != 200:
        return StopAnswer(
            stopped=False,
            reason=(
                f"the build runner at {runner_url} answered "
                f"{response.status_code} to the stop request: "
                f"{response.text[:300]}"
            ),
        )
    try:
        body = response.json()
    except ValueError:
        return StopAnswer(
            stopped=False,
            reason=f"the build runner at {runner_url} answered something that is not JSON",
        )
    if isinstance(body, dict) and body.get("stopped") is True:
        return StopAnswer(stopped=True)
    remaining = body.get("remaining") if isinstance(body, dict) else body
    reason = str(body.get("reason") or "") if isinstance(body, dict) else ""
    return StopAnswer(stopped=False, remaining=remaining, reason=reason)


class AckAfterStop:
    """Acknowledge a cancelled build's message only once its runner says stopped.

    One instance per factory process. A held acknowledgement is kept by a
    supervised task that asks again every ``recheck_seconds``; a second
    request to hold the same build joins the first rather than starting
    another. A factory restart loses the tasks, and the message — still
    unacknowledged — comes back through the consumer, which asks again.
    """

    def __init__(
        self,
        check: StopCheck,
        *,
        recheck_seconds: float = DEFAULT_RECHECK_SECONDS,
    ) -> None:
        self._check = check
        self._recheck_seconds = recheck_seconds
        self._held: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def confirm(
        self, feature_id: str, correlation_id: str, *, cancelled: bool | None = None
    ) -> StopAnswer | None:
        """Ask once. ``None`` = not applicable. Never raises."""
        try:
            answer = await self._check(feature_id, correlation_id, cancelled)
        except Exception as exc:  # noqa: BLE001 — a failed check holds the place
            return StopAnswer(
                stopped=False,
                reason=f"the stop check raised {type(exc).__name__}: {exc}",
            )
        if answer is not None and answer.route_missing:
            # An older runner image cannot be asked; holding the place for
            # ever would stall every build behind it, and that runner never
            # had anything better than the old acknowledgement.
            logger.warning(
                "build stop: older runner image at %s has no stop route; "
                "acknowledging feature_id=%s as before, unconfirmed",
                answer.runner_url,
                feature_id,
            )
            return None
        return answer

    def _say_held(
        self, feature_id: str, correlation_id: str, answer: StopAnswer, where: str
    ) -> None:
        logger.warning(
            "build stop: %s — NOT acknowledging cancelled build feature_id=%s "
            "correlation_id=%s: its runner has not confirmed that everything "
            "it owns is gone (%s; remaining=%s). Its place stays held; asking "
            "again every %ss",
            where,
            feature_id,
            correlation_id,
            answer.reason or "the runner said not stopped",
            answer.remaining,
            self._recheck_seconds,
        )

    async def wait_until_stopped(
        self,
        feature_id: str,
        correlation_id: str,
        *,
        cancelled: bool | None = None,
        where: str,
    ) -> None:
        """Return only when the build is confirmed stopped (or not applicable)."""
        while True:
            answer = await self.confirm(feature_id, correlation_id, cancelled=cancelled)
            if answer is None or answer.stopped:
                return
            self._say_held(feature_id, correlation_id, answer, where)
            await asyncio.sleep(self._recheck_seconds)

    async def ack_when_stopped(
        self,
        feature_id: str,
        correlation_id: str,
        ack: Callable[[], Awaitable[Any]],
        *,
        cancelled: bool | None = None,
        where: str,
    ) -> bool:
        """Ack now if stopped (or not applicable); otherwise hold and return False.

        A held acknowledgement is completed later by a supervised task.
        """
        key = (feature_id, correlation_id)
        if key in self._held:
            logger.info(
                "build stop: %s — feature_id=%s correlation_id=%s is already "
                "held until its runner confirms the stop",
                where,
                feature_id,
                correlation_id,
            )
            return False
        answer = await self.confirm(feature_id, correlation_id, cancelled=cancelled)
        if answer is None or answer.stopped:
            await ack()
            return True
        self._say_held(feature_id, correlation_id, answer, where)

        async def _hold() -> None:
            try:
                await asyncio.sleep(self._recheck_seconds)
                await self.wait_until_stopped(
                    feature_id, correlation_id, cancelled=cancelled, where=where
                )
                logger.info(
                    "build stop: %s — feature_id=%s correlation_id=%s is "
                    "confirmed stopped; acknowledging, which releases its place",
                    where,
                    feature_id,
                    correlation_id,
                )
                await ack()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the message stays unacked
                logger.exception(
                    "build stop: %s — holding feature_id=%s failed; the "
                    "message stays unacknowledged and will come back",
                    where,
                    feature_id,
                )
            finally:
                self._held.pop(key, None)

        self._held[key] = asyncio.get_running_loop().create_task(_hold())
        return False

    def held(self) -> list[tuple[str, str]]:
        return list(self._held)

    async def shutdown(self) -> None:
        tasks = list(self._held.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._held.clear()
