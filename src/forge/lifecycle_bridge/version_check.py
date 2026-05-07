"""Startup-time version-skew diagnostic for the langgraph-runner sidecar.

TASK-FRR-PEB-010 mitigates the dominant Option C risk (SDK schema drift
across ``langgraph-api`` versions) by checking the running sidecar's
version at daemon boot and **failing the daemon with a clear diagnostic**
if the version is outside the bridge's declared support range.

Why fail fast?
--------------

The pipeline-emitter bridge translates SSE envelopes from the sidecar
into the in-flight registry's lifecycle vocabulary. A silently-mismatched
sidecar will emit envelopes the bridge cannot translate — producing
malformed registry rows that surface as opaque downstream errors hours
later. Surfacing the skew loudly at startup is strictly better than
discovering it at the first build attempt.

Why also defer to T8 on timeouts?
---------------------------------

A *slow-starting* sidecar is not a version-skew event — it's a timing
race the bridge already handles via T8's reconnect policy. Failing the
daemon on a 5s timeout would replace one operational hazard (silent
schema drift) with another (boot-time race). The check therefore raises
**only** when it gets a clean response that names an out-of-range
version; transport errors return silently.

Public surface
--------------

* :data:`LANGGRAPH_API_SUPPORTED_RANGE` — the source-of-truth specifier
  string. Mirrors the active range pinned in ``pyproject.toml``.
* :class:`LangGraphVersionMismatchError` — raised on confirmed skew.
* :func:`check_langgraph_runner_version` — the entry point invoked by
  :class:`forge.lifecycle_bridge.bridge.LifecycleBridge` during
  construction.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from typing import Callable, TextIO
from urllib.error import URLError
from urllib.request import Request, urlopen

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS",
    "LANGGRAPH_API_SUPPORTED_RANGE",
    "LangGraphVersionMismatchError",
    "check_langgraph_runner_version",
]


# AC-1: declared support range. Keep in sync with the
# ``langgraph``/``langgraph-api`` pin in ``pyproject.toml``. The active
# `langgraph` dep is ``>=1.1,<2`` but the *runner* (``langgraph-api``)
# tracks a separate cadence; ``>=0.8.5,<0.9`` reflects the sidecar
# version current at task design (TASK-FRR-PEB-010, 2026-05-06). When
# the sidecar moves to 0.9, update both this constant and the
# ``pyproject.toml`` extra in lock-step.
LANGGRAPH_API_SUPPORTED_RANGE: str = ">=0.8.5,<0.9"


# Test-requirement: the version check uses a 5s timeout so a
# slow-starting sidecar falls back to T8's reconnect rather than
# killing the daemon.
DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS: float = 5.0


# Type alias for the injectable HTTP fetch callable. The default
# implementation uses ``urllib.request`` (no third-party HTTP dep) but
# the parameter is exposed so tests can substitute a deterministic stub
# without monkey-patching network primitives.
VersionFetcher = Callable[[str, float], str]


class LangGraphVersionMismatchError(RuntimeError):
    """Raised when the sidecar's reported version is outside the support range.

    The error carries both the expected range and the observed version
    so log shippers and the boot-time diagnostic on stderr can name the
    drift precisely. ``RuntimeError`` is the base class because the skew
    is a configuration mismatch surfaced at runtime — not a programmer
    error (``ValueError``) and not a transport failure (``OSError``).

    Attributes:
        expected_range: The :class:`packaging.specifiers.SpecifierSet`
            string the bridge was built against.
        observed_version: The version string the sidecar's ``/version``
            endpoint returned.
    """

    def __init__(self, expected_range: str, observed_version: str) -> None:
        self.expected_range = expected_range
        self.observed_version = observed_version
        super().__init__(
            f"langgraph-runner version skew: expected {expected_range}, "
            f"observed {observed_version}. Bridge cannot start safely."
        )


# ---------------------------------------------------------------------------
# Default fetch implementation (stdlib only — no httpx/requests dep).
# ---------------------------------------------------------------------------


def _default_fetch(url: str, timeout_seconds: float) -> str:
    """Fetch the version string from ``url`` using ``urllib.request``.

    The endpoint is expected to return either a bare version string or a
    JSON object with a ``"version"`` key (the convention used by the
    langgraph-api ``/version`` endpoint). Any other shape is forwarded
    verbatim and the caller's :class:`packaging.version.Version` parser
    decides whether it is acceptable.

    Args:
        url: The fully-qualified ``/version`` URL.
        timeout_seconds: Per-request timeout. ``socket.timeout`` /
            :class:`TimeoutError` propagate out so the caller can
            decide whether to fail-fast or defer.

    Returns:
        The unwrapped version string, stripped of surrounding whitespace.

    Raises:
        OSError: Network / transport failures (incl. ``socket.timeout``,
            :class:`ConnectionRefusedError`, :class:`URLError`).
        ValueError: If the response body is not parseable as either a
            JSON object with a ``version`` key or a bare string.
    """
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        body_bytes = response.read()
    body = body_bytes.decode("utf-8", errors="replace").strip()
    # JSON object with a ``version`` key (the canonical shape).
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        # Fall through — treat as a bare version string.
        return body
    if isinstance(decoded, dict) and "version" in decoded:
        return str(decoded["version"]).strip()
    if isinstance(decoded, str):
        return decoded.strip()
    raise ValueError(
        f"Unexpected /version payload shape from {url!r}: {body!r}"
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def check_langgraph_runner_version(
    sidecar_url: str,
    *,
    supported_range: str = LANGGRAPH_API_SUPPORTED_RANGE,
    timeout_seconds: float = DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS,
    fetch: VersionFetcher = _default_fetch,
    stderr: TextIO | None = None,
) -> None:
    """Verify the sidecar's version is within the supported range.

    Behaviour matrix (what the caller sees):

    +----------------------------------+------------------------------------+
    | Sidecar response                 | Outcome                            |
    +==================================+====================================+
    | In-range version (e.g. 0.8.7)    | Returns ``None`` silently.         |
    +----------------------------------+------------------------------------+
    | Out-of-range version (e.g. 0.9.0)| Prints AC-4 diagnostic to stderr   |
    |                                  | **and** raises                     |
    |                                  | :class:`LangGraphVersionMismatchError`. |
    +----------------------------------+------------------------------------+
    | Unparseable version string       | Logs a WARNING and returns silently|
    |                                  | (defers to T8 — better to discover |
    |                                  | the schema drift downstream than   |
    |                                  | block boot on an unrecognised      |
    |                                  | format).                           |
    +----------------------------------+------------------------------------+
    | Timeout / connection error /     | Logs a WARNING and returns silently|
    | OSError                          | so a slow-starting sidecar falls   |
    |                                  | back to T8's reconnect policy.     |
    +----------------------------------+------------------------------------+

    Args:
        sidecar_url: Base URL of the langgraph-runner sidecar (without
            the ``/version`` suffix). Trailing slashes are normalised.
        supported_range: Override the module-level
            :data:`LANGGRAPH_API_SUPPORTED_RANGE` for tests / future
            callers that need a tighter window.
        timeout_seconds: Per-request timeout; defaults to 5s.
        fetch: Injectable HTTP callable. The default uses
            ``urllib.request`` (stdlib only).
        stderr: Override stream for the AC-4 diagnostic. Defaults to
            :data:`sys.stderr`. Passed in by tests to capture the output
            without writing to the real stderr.

    Raises:
        LangGraphVersionMismatchError: When the sidecar reports a
            version outside ``supported_range``.
    """
    if not sidecar_url:
        raise ValueError(
            "check_langgraph_runner_version: sidecar_url must be non-empty"
        )

    url = sidecar_url.rstrip("/") + "/version"
    err_stream = stderr if stderr is not None else sys.stderr

    # ---- Fetch ----------------------------------------------------------
    try:
        observed_raw = fetch(url, timeout_seconds)
    except (
        TimeoutError,
        socket.timeout,
        ConnectionError,
        URLError,
        OSError,
    ) as exc:
        # Slow-starting / unreachable sidecar — defer to T8 reconnect.
        # We deliberately do NOT raise here so a transient boot-time race
        # does not kill the daemon.
        logger.warning(
            "lifecycle_bridge.version_check.unreachable url=%s "
            "timeout_seconds=%s error=%s",
            url,
            timeout_seconds,
            exc,
        )
        return

    # ---- Parse ----------------------------------------------------------
    observed = str(observed_raw).strip()
    try:
        observed_version = Version(observed)
    except InvalidVersion as exc:
        # An unrecognised version format is suspicious but ambiguous —
        # treat it as a soft warning and let downstream translation
        # (T3) surface the problem on the first envelope rather than
        # blocking boot. This matches the "defer to T8" principle for
        # transport errors.
        logger.warning(
            "lifecycle_bridge.version_check.unparseable url=%s observed=%r "
            "error=%s",
            url,
            observed,
            exc,
        )
        return

    # ---- Compare --------------------------------------------------------
    spec = SpecifierSet(supported_range)
    if observed_version not in spec:
        diagnostic = (
            f"langgraph-runner version skew: expected {supported_range}, "
            f"observed {observed}. Bridge cannot start safely."
        )
        # AC-4: stderr carries the diagnostic so an operator sees it
        # without needing logs.
        print(diagnostic, file=err_stream)
        # Mirror to the structured logger so log shippers also see it.
        logger.error(
            "lifecycle_bridge.version_check.mismatch expected=%s observed=%s",
            supported_range,
            observed,
        )
        # AC-3: raise with both range and version in the message.
        raise LangGraphVersionMismatchError(supported_range, observed)

    # AC-5: in-range — silent success. Verbose-mode INFO is acceptable.
    logger.info(
        "lifecycle_bridge.version_check.ok expected=%s observed=%s",
        supported_range,
        observed,
    )
