"""A one-shot bus connect that really gives up (19 October 2026).

nats-py's first connect does NOT give up on its own unless
``max_reconnect_attempts`` is positive: its server-selection loop catches a
refused connection and tries again, for ever, with ``max_reconnect_attempts``
0 ("unlimited" in nats-py) or -1, and even with ``allow_reconnect=False``
(that flag only stops a failure that reaches ``connect``'s own loop). A
command meant to fail fast against a bus that is down therefore hung.

:func:`connect_once` gives a one-shot command a bounded connect: reconnects
off, a positive attempt limit (so the selection loop does give up), and the
whole connect bounded by the command's own timeout, whatever nats-py does.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: The smallest limit nats-py reads as a limit (0 and below mean "for ever").
ONE_SHOT_ATTEMPTS: int = 1
#: Seconds between the (at most two) tries inside the bound.
ONE_SHOT_RETRY_WAIT: float = 0.5


def one_shot_options(timeout: float) -> dict[str, Any]:
    """The nats-py options for a connect that gives up within ``timeout``."""
    return {
        "connect_timeout": timeout,
        "allow_reconnect": False,
        "max_reconnect_attempts": ONE_SHOT_ATTEMPTS,
        "reconnect_time_wait": ONE_SHOT_RETRY_WAIT,
    }


async def connect_once(servers: Any, *, timeout: float, connect: Any = None, **options: Any) -> Any:
    """Connect once, or raise within ``timeout`` seconds (``asyncio.TimeoutError``
    or nats-py's own error). ``connect`` is the connect callable (nats-py's
    by default); ``options`` are passed on, after the one-shot ones."""
    if connect is None:
        import nats  # type: ignore[import-not-found]

        connect = nats.connect
    merged = {**one_shot_options(timeout), **options}
    return await asyncio.wait_for(connect(servers, **merged), timeout=timeout)


__all__ = ["ONE_SHOT_ATTEMPTS", "ONE_SHOT_RETRY_WAIT", "connect_once", "one_shot_options"]
