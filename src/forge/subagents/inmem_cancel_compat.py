"""Narrow compatibility guard for long in-memory LangGraph runs.

``langgraph-runtime-inmem==0.34.1`` stops its run-control listener after one
240-second idle interval.  The run row can still be marked ``interrupted`` by
the HTTP handler, but the worker no longer receives the control message.  A
long-running autobuild then keeps its owned subprocess alive.

The upstream package has no newer release in the API 0.14.x compatibility
range as of 2026-09-18.  This module wraps the exact affected function instead
of copying it: after the original listener returns, it is awaited again only
when its ``done`` event remains unset.  The original continues to own stored
control-key handling, queue authentication, interrupt/rollback types and task
cancellation.

The patch is inert for any other runtime version. The known-affected version
refuses graph startup unless the exact function source hash matches the
reviewed build. Remove it when an upstream release keeps the listener alive
itself.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

logger = logging.getLogger(__name__)

_AFFECTED_VERSION = "0.34.1"
_AFFECTED_SOURCE_SHA256 = (
    "a107b6c12a31edda00ce131771e396bc9d51123b639a56d3c5d882df943eea33"
)
_PATCH_MARKER = "__forge_persistent_inmem_cancel_listener__"

InstallResult = Literal[
    "installed",
    "already-installed",
    "runtime-unavailable",
    "version-mismatch",
]


def _persistent_listener(
    original: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Repeat the reviewed listener only after its idle return."""

    @functools.wraps(original)
    async def _wrapped(queue: Any, run_id: Any, thread_id: Any, done: Any) -> None:
        while not done.is_set():
            await original(queue, run_id, thread_id, done)
            # The reviewed original waits up to 240 seconds before an unset
            # return. Yield once before re-arming as a defence against event-
            # loop churn even though the exact-source guard already proves the
            # original cannot return immediately on its affected timeout path.
            if not done.is_set():
                import asyncio

                await asyncio.sleep(0)

    setattr(_wrapped, _PATCH_MARKER, True)
    return _wrapped


def install_inmem_cancel_listener_compat(
    *,
    _ops: Any = None,
    _runtime_version: str | None = None,
    _source_text: str | None = None,
) -> InstallResult:
    """Install the exact-version listener wrapper.

    A different runtime version remains untouched. The known-affected 0.34.1
    version fails graph startup when its listener is absent or its source does
    not match the reviewed function; running that version unpatched would
    advertise cancellation while silently orphaning children after 240 seconds.

    Private override arguments are test seams. Production calls this with no
    arguments and derives all identity from the installed distribution.
    """
    if _ops is None:
        try:
            from langgraph_runtime_inmem import ops as runtime_ops
        except ImportError:
            logger.info(
                "in-memory LangGraph cancellation compatibility not installed: "
                "langgraph_runtime_inmem is unavailable"
            )
            return "runtime-unavailable"
        _ops = runtime_ops

    if _runtime_version is None:
        try:
            _runtime_version = version("langgraph-runtime-inmem")
        except PackageNotFoundError:
            return "runtime-unavailable"
    if _runtime_version != _AFFECTED_VERSION:
        logger.info(
            "in-memory LangGraph cancellation compatibility not installed: "
            "runtime version %s is not reviewed affected version %s",
            _runtime_version,
            _AFFECTED_VERSION,
        )
        return "version-mismatch"

    original = getattr(_ops, "listen_for_cancellation", None)
    if original is None:
        raise RuntimeError(
            "refusing autobuild graph startup: langgraph-runtime-inmem "
            "cancellation listener is absent"
        )
    if getattr(original, _PATCH_MARKER, False):
        return "already-installed"

    if _source_text is None:
        try:
            _source_text = inspect.getsource(original)
        except (OSError, TypeError):
            _source_text = ""
    source_hash = hashlib.sha256(_source_text.encode("utf-8")).hexdigest()
    if source_hash != _AFFECTED_SOURCE_SHA256:
        raise RuntimeError(
            "refusing autobuild graph startup: langgraph-runtime-inmem 0.34.1 "
            f"listener source hash {source_hash} does not match reviewed "
            f"{_AFFECTED_SOURCE_SHA256}"
        )

    _ops.listen_for_cancellation = _persistent_listener(original)
    logger.warning(
        "installed Forge compatibility for langgraph-runtime-inmem 0.34.1 "
        "long-run cancellation listener (reviewed source %s)",
        _AFFECTED_SOURCE_SHA256,
    )
    return "installed"
