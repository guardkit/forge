"""The coordinator's bus connections: reconnect for ever, and say so when lost.

Bus reconnect (19 October 2026). Every long-lived Forge connection used the
NATS client's defaults: about 60 reconnect attempts two seconds apart, so a
bus outage of more than about two minutes closed the connection for good.
The build loop then closed the SHARED client — the one the publisher, the
dispatcher and the planning loop hold by reference — and attached itself
to a fresh one, so planning intake retried for ever, publishing was dead,
and ``/healthz`` said healthy again.

Two blunt rules replace that:

1. **Never give up reconnecting.** Every long-lived connection — the shared
   client, the fleet client and the planning loop's fleet client — is
   opened with ``max_reconnect_attempts=-1`` (reconnect for ever) and a
   two-second wait between attempts, so an outage of any length heals by
   itself on the same client, and every holder keeps working.
2. **A lost connection is loud.** If a connection still reaches *closed*,
   :class:`BusWatch` records it and the serve loop exits non-zero
   (:class:`BusConnectionLost`), so the container's restart policy brings
   the coordinator back — the pattern the Jarvis gateway uses. Nothing
   swaps a client underneath its holders. While the shared connection is
   disconnected or closed, ``/healthz`` answers 503.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("forge.cli.serve")

#: Reconnect for ever: nats-py reads a negative ``max_reconnect_attempts`` as
#: "no limit".
RECONNECT_FOR_EVER: int = -1
#: Seconds between reconnect attempts (nats-py's own default).
RECONNECT_TIME_WAIT_SECONDS: float = 2.0
#: How often the serve loop also looks at the shared client's own
#: ``is_closed``, for a client whose ``closed_cb`` could not be wired.
CLOSED_POLL_SECONDS: float = 5.0


class BusConnectionLost(RuntimeError):
    """A bus connection reached *closed*: the coordinator exits non-zero."""


class BusWatch:
    """What every long-lived bus connection says about itself.

    One per serve run (:func:`reset`). ``callbacks(name)`` gives the nats-py
    lifecycle callbacks for one connection; the shared connection's are
    also written onto the readiness state, so ``/healthz`` answers 503 while
    it is disconnected or closed.
    """

    def __init__(self) -> None:
        self.state: Any = None
        self.lost = asyncio.Event()
        self.lost_names: list[str] = []

    def callbacks(self, name: str, *, shared: bool = False) -> dict[str, Callable[..., Awaitable[None]]]:
        async def disconnected() -> None:
            logger.warning("forge-serve: the %s bus connection is down; reconnecting", name)
            if shared:
                self._set_connected(False)

        async def reconnected() -> None:
            logger.info("forge-serve: the %s bus connection is back", name)
            if shared:
                self._set_connected(True)

        async def closed() -> None:
            logger.error(
                "forge-serve: the %s bus connection is closed for good; the "
                "coordinator exits so it is restarted",
                name,
            )
            if shared:
                self._set_connected(False)
            self.lost_names.append(name)
            self.lost.set()

        async def error(exc: Exception) -> None:
            # One line per failed attempt, not nats-py's default traceback:
            # an outage of an hour is 1800 attempts.
            logger.debug(
                "forge-serve: the %s bus connection: %s: %s", name, type(exc).__name__, exc
            )

        return {
            "disconnected_cb": disconnected,
            "reconnected_cb": reconnected,
            "closed_cb": closed,
            "error_cb": error,
        }

    def _set_connected(self, value: bool) -> None:
        if self.state is not None:
            self.state.bus_connected = value


#: The watch the production connect seams wire their callbacks to.
WATCH = BusWatch()


def reset(state: Any) -> BusWatch:
    """A fresh watch for one serve run, writing onto ``state``."""
    global WATCH
    WATCH = BusWatch()
    WATCH.state = state
    return WATCH


def nats_connect_kwargs(name: str, *, shared: bool = False) -> dict[str, Any]:
    """The keyword arguments every long-lived raw ``nats.connect`` gets."""
    return {
        "name": name,
        "max_reconnect_attempts": RECONNECT_FOR_EVER,
        "reconnect_time_wait": RECONNECT_TIME_WAIT_SECONDS,
        **WATCH.callbacks(name, shared=shared),
    }


def open_fleet_core_client(nats_url: str, name: str) -> Any:
    """A ``nats_core`` client that reconnects for ever and reports a close.

    ``NATSConfig`` refuses a negative ``max_reconnect_attempts`` (its field
    is ``ge=0``), though the nats-py client it configures reads -1 as "no
    limit". The validated config is therefore copied with the one value
    changed: every other field is validated as usual.
    """
    from nats_core.client import NATSClient
    from nats_core.config import NATSConfig

    config = NATSConfig(
        url=nats_url, name=name, reconnect_time_wait=RECONNECT_TIME_WAIT_SECONDS
    ).model_copy(update={"max_reconnect_attempts": RECONNECT_FOR_EVER})
    return NATSClient(config, **WATCH.callbacks(name))


async def watch_for_a_lost_bus(client: Any, watch: BusWatch | None = None) -> None:
    """Return never; raise :class:`BusConnectionLost` once any watched
    connection is closed, or the shared client says it is closed."""
    watch = watch or WATCH
    while True:
        if watch.lost.is_set():
            raise BusConnectionLost(
                "the bus connection is closed for good: "
                + ", ".join(watch.lost_names or ["shared"])
            )
        if getattr(client, "is_closed", False) is True:
            raise BusConnectionLost("the bus connection is closed for good: shared")
        try:
            await asyncio.wait_for(watch.lost.wait(), timeout=CLOSED_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


__all__ = [
    "BusConnectionLost",
    "BusWatch",
    "CLOSED_POLL_SECONDS",
    "RECONNECT_FOR_EVER",
    "RECONNECT_TIME_WAIT_SECONDS",
    "WATCH",
    "nats_connect_kwargs",
    "open_fleet_core_client",
    "reset",
    "watch_for_a_lost_bus",
]
