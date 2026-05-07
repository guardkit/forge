"""Tests for ``forge.lifecycle_bridge.version_check`` (TASK-FRR-PEB-010).

Acceptance-criteria coverage map:

* AC-1: ``LANGGRAPH_API_SUPPORTED_RANGE`` is declared and parses cleanly
  with :class:`packaging.specifiers.SpecifierSet` —
  :class:`TestSupportedRangeConstant`.
* AC-2: :class:`LifecycleBridge` calls ``/version`` at construction
  (before ``recover_in_flight``) when ``sidecar_url`` is supplied —
  :class:`TestBridgeIntegration`.
* AC-3: An out-of-range observed version raises
  :class:`LangGraphVersionMismatchError` whose message names both the
  expected range and the observed version, and the error propagates to
  the bridge constructor — :class:`TestVersionMismatch` and
  :class:`TestBridgeIntegration`.
* AC-4: The diagnostic is also printed to stderr —
  :class:`TestVersionMismatch.test_diagnostic_printed_to_stderr`.
* AC-5: An in-range observed version returns silently with no stderr —
  :class:`TestVersionInRange`.
* Test-requirement: a slow-starting / unreachable sidecar must NOT
  fail the daemon — :class:`TestSidecarUnreachable`.
"""

from __future__ import annotations

import io
from typing import Callable

import pytest
from packaging.specifiers import SpecifierSet

from forge.lifecycle_bridge.bridge import LifecycleBridge
from forge.lifecycle_bridge.version_check import (
    DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS,
    LANGGRAPH_API_SUPPORTED_RANGE,
    LangGraphVersionMismatchError,
    check_langgraph_runner_version,
)
from forge.persistence.repositories.bridge_registry import BridgeRegistry


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_fetch(
    *,
    version: str | None = None,
    raise_exc: BaseException | None = None,
) -> Callable[[str, float], str]:
    """Return a stub ``fetch`` callable for ``check_langgraph_runner_version``.

    Either ``version`` (return value) or ``raise_exc`` (exception to raise)
    must be supplied. The returned callable records the URL it was called
    with on the ``calls`` list attribute so tests can assert the
    ``/version`` URL was contacted.
    """
    calls: list[tuple[str, float]] = []

    def _fetch(url: str, timeout_seconds: float) -> str:
        calls.append((url, timeout_seconds))
        if raise_exc is not None:
            raise raise_exc
        assert version is not None
        return version

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


# ---------------------------------------------------------------------------
# AC-1: supported-range constant.
# ---------------------------------------------------------------------------


class TestSupportedRangeConstant:
    """The module declares ``LANGGRAPH_API_SUPPORTED_RANGE`` and it parses."""

    def test_constant_is_declared(self) -> None:
        assert isinstance(LANGGRAPH_API_SUPPORTED_RANGE, str)
        assert LANGGRAPH_API_SUPPORTED_RANGE.strip() != ""

    def test_constant_parses_as_specifier_set(self) -> None:
        # Smoke check — the constant is a valid SpecifierSet expression.
        spec = SpecifierSet(LANGGRAPH_API_SUPPORTED_RANGE)
        # The active range used at design time is ``>=0.8.5,<0.9``;
        # update both the constant and this canary together if the
        # supported range changes.
        assert "0.8.7" in spec
        assert "0.9.0" not in spec
        assert "0.8.4" not in spec

    def test_default_timeout_is_five_seconds(self) -> None:
        # AC + test-requirement: the version check uses a 5s timeout so a
        # slow-starting sidecar can fall back to T8's reconnect rather than
        # killing the daemon.
        assert DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS == 5.0


# ---------------------------------------------------------------------------
# AC-5: in-range version → silent success.
# ---------------------------------------------------------------------------


class TestVersionInRange:
    """An in-range version returns silently with no stderr noise."""

    def test_in_range_returns_silently(self) -> None:
        stderr = io.StringIO()
        fetch = _make_fetch(version="0.8.7")
        # No exception, no stderr writes.
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=stderr,
        )
        assert stderr.getvalue() == ""

    def test_fetch_is_called_with_version_endpoint(self) -> None:
        fetch = _make_fetch(version="0.8.5")
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]
        url, timeout = fetch.calls[0]  # type: ignore[attr-defined]
        # The /version path is appended; trailing slashes are normalised.
        assert url.endswith("/version")
        assert timeout == DEFAULT_VERSION_CHECK_TIMEOUT_SECONDS

    def test_trailing_slash_in_sidecar_url_is_normalised(self) -> None:
        fetch = _make_fetch(version="0.8.5")
        check_langgraph_runner_version(
            "http://localhost:2024/",
            fetch=fetch,
            stderr=io.StringIO(),
        )
        url, _ = fetch.calls[0]  # type: ignore[attr-defined]
        # No double slash before "version".
        assert "//version" not in url
        assert url.endswith("/version")

    def test_accepts_dict_payload_with_version_key(self) -> None:
        # The convention for /version endpoints is ``{"version": "X.Y.Z"}``
        # — but our fetch contract is to return the bare version string,
        # so the JSON-decoding lives in the default fetch implementation
        # and the unit test here only needs to verify that a stripped
        # bare version string is accepted.
        fetch = _make_fetch(version="0.8.6")
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )
        # No exception is the assertion — fall-through reaches here.


# ---------------------------------------------------------------------------
# AC-3 + AC-4: out-of-range version → raise + stderr diagnostic.
# ---------------------------------------------------------------------------


class TestVersionMismatch:
    """An out-of-range version raises :class:`LangGraphVersionMismatchError`."""

    def test_above_range_raises(self) -> None:
        fetch = _make_fetch(version="0.9.0")
        with pytest.raises(LangGraphVersionMismatchError) as excinfo:
            check_langgraph_runner_version(
                "http://localhost:2024",
                fetch=fetch,
                stderr=io.StringIO(),
            )
        # AC-3: message names both the expected range and the observed version.
        assert LANGGRAPH_API_SUPPORTED_RANGE in str(excinfo.value)
        assert "0.9.0" in str(excinfo.value)
        # Structured attributes are exposed for log-shipping.
        assert excinfo.value.expected_range == LANGGRAPH_API_SUPPORTED_RANGE
        assert excinfo.value.observed_version == "0.9.0"

    def test_below_range_raises(self) -> None:
        fetch = _make_fetch(version="0.8.4")
        with pytest.raises(LangGraphVersionMismatchError) as excinfo:
            check_langgraph_runner_version(
                "http://localhost:2024",
                fetch=fetch,
                stderr=io.StringIO(),
            )
        assert "0.8.4" in str(excinfo.value)

    def test_diagnostic_printed_to_stderr(self) -> None:
        # AC-4: stderr carries the diagnostic so an operator sees it
        # without needing logs.
        stderr = io.StringIO()
        fetch = _make_fetch(version="0.9.5")
        with pytest.raises(LangGraphVersionMismatchError):
            check_langgraph_runner_version(
                "http://localhost:2024",
                fetch=fetch,
                stderr=stderr,
            )
        emitted = stderr.getvalue()
        assert "langgraph-runner version skew" in emitted
        assert LANGGRAPH_API_SUPPORTED_RANGE in emitted
        assert "0.9.5" in emitted
        assert "Bridge cannot start safely" in emitted

    def test_unparseable_version_does_not_fail_daemon(self) -> None:
        # If the sidecar returns a payload we can't parse as a version,
        # we must not fail-fast — that would replicate the very runtime
        # surprise this task is trying to mitigate. Defer to T8 reconnect.
        fetch = _make_fetch(version="not-a-version")
        # No exception.
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )

    def test_supported_range_override_is_honoured(self) -> None:
        # The default range is the module constant, but tests / future
        # callers can pass ``supported_range=...`` to lock to a tighter
        # window without monkeypatching the module constant.
        fetch = _make_fetch(version="0.8.7")
        with pytest.raises(LangGraphVersionMismatchError):
            check_langgraph_runner_version(
                "http://localhost:2024",
                fetch=fetch,
                stderr=io.StringIO(),
                supported_range=">=0.9,<1.0",
            )


# ---------------------------------------------------------------------------
# Test-requirement: sidecar unreachable / slow → defer to T8 reconnect.
# ---------------------------------------------------------------------------


class TestSidecarUnreachable:
    """Timeout / connection errors must NOT fail the daemon."""

    def test_timeout_returns_silently(self) -> None:
        # ``socket.timeout`` is a subclass of ``OSError`` — the check
        # must catch it and return without raising so a slow-starting
        # sidecar falls back to T8's reconnect policy.
        import socket

        fetch = _make_fetch(raise_exc=socket.timeout("read timed out"))
        # No exception.
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )

    def test_timeout_error_returns_silently(self) -> None:
        fetch = _make_fetch(raise_exc=TimeoutError("read timed out"))
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )

    def test_connection_refused_returns_silently(self) -> None:
        fetch = _make_fetch(raise_exc=ConnectionRefusedError())
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )

    def test_generic_oserror_returns_silently(self) -> None:
        fetch = _make_fetch(raise_exc=OSError("network down"))
        check_langgraph_runner_version(
            "http://localhost:2024",
            fetch=fetch,
            stderr=io.StringIO(),
        )


# ---------------------------------------------------------------------------
# AC-2 + AC-3: bridge integration — failed check propagates from __init__.
# ---------------------------------------------------------------------------


class TestBridgeIntegration:
    """The :class:`LifecycleBridge` constructor wires the version check.

    Existing T2 tests construct ``LifecycleBridge(registry=registry)``
    without a sidecar URL — backwards compatibility is preserved by
    making ``sidecar_url`` optional. When supplied, the check runs
    during construction (before any later ``recover_in_flight`` call).
    """

    def test_no_sidecar_url_skips_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Backward compatibility — existing T2 fixtures must keep working.
        called = {"hit": False}

        def _spy_check(*args: object, **kwargs: object) -> None:
            called["hit"] = True

        monkeypatch.setattr(
            "forge.lifecycle_bridge.bridge.check_langgraph_runner_version",
            _spy_check,
        )
        registry = _StubRegistry()
        LifecycleBridge(registry=registry)
        assert called["hit"] is False

    def test_in_range_sidecar_constructs_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = _make_fetch(version="0.8.7")
        # Patch the bridge's view of check_langgraph_runner_version to
        # inject the stub fetch — keeps the bridge call site honest.
        from forge.lifecycle_bridge import bridge as bridge_mod
        from forge.lifecycle_bridge import version_check as vc_mod

        def _wrapped(sidecar_url: str, **kwargs: object) -> None:
            return vc_mod.check_langgraph_runner_version(
                sidecar_url, fetch=fetch, stderr=io.StringIO()
            )

        monkeypatch.setattr(
            bridge_mod, "check_langgraph_runner_version", _wrapped
        )
        registry = _StubRegistry()
        LifecycleBridge(
            registry=registry, sidecar_url="http://localhost:2024"
        )
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

    def test_out_of_range_sidecar_fails_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = _make_fetch(version="0.9.5")
        from forge.lifecycle_bridge import bridge as bridge_mod
        from forge.lifecycle_bridge import version_check as vc_mod

        def _wrapped(sidecar_url: str, **kwargs: object) -> None:
            return vc_mod.check_langgraph_runner_version(
                sidecar_url, fetch=fetch, stderr=io.StringIO()
            )

        monkeypatch.setattr(
            bridge_mod, "check_langgraph_runner_version", _wrapped
        )
        registry = _StubRegistry()
        with pytest.raises(LangGraphVersionMismatchError):
            LifecycleBridge(
                registry=registry, sidecar_url="http://localhost:2024"
            )

    def test_unreachable_sidecar_does_not_fail_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = _make_fetch(raise_exc=ConnectionRefusedError())
        from forge.lifecycle_bridge import bridge as bridge_mod
        from forge.lifecycle_bridge import version_check as vc_mod

        def _wrapped(sidecar_url: str, **kwargs: object) -> None:
            return vc_mod.check_langgraph_runner_version(
                sidecar_url, fetch=fetch, stderr=io.StringIO()
            )

        monkeypatch.setattr(
            bridge_mod, "check_langgraph_runner_version", _wrapped
        )
        registry = _StubRegistry()
        # Slow / unreachable sidecar must NOT kill construction —
        # the daemon falls back to T8's reconnect policy.
        LifecycleBridge(
            registry=registry, sidecar_url="http://localhost:2024"
        )


# ---------------------------------------------------------------------------
# Local stub registry (lighter than spinning up sqlite for these tests).
# ---------------------------------------------------------------------------


class _StubRegistry(BridgeRegistry):
    """A no-op subclass of :class:`BridgeRegistry` used only to satisfy
    the bridge's ``isinstance(registry, BridgeRegistry)`` guard.

    The constructor of :class:`BridgeRegistry` is bypassed deliberately —
    the version-check tests never exercise the registry, so paying the
    cost of opening a real SQLite connection per test is unnecessary.
    """

    def __init__(self) -> None:  # noqa: D401 — intentional override
        # Intentionally do not call super().__init__ — the registry is
        # never invoked in these tests.
        pass
