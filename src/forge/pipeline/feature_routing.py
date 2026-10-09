"""Coordinator-owned HTTP gate for durable feature placement."""

from __future__ import annotations

import asyncio
import inspect
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable

from forge.lifecycle.feature_routing import (
    FeatureRoutingBlocked,
    FeatureRoutingReceipt,
    FeatureRoutingSeedStore,
    validate_feature_routing_id,
)

FEATURE_HEADER = "x-feature-id"
STORED_SERVER_HEADER = "x-feature-stored-server-id"
ACTUAL_SERVER_HEADER = "x-feature-actual-server-id"


class SeedResponseRejected(FeatureRoutingBlocked):
    """A complete seed response failed acknowledgement validation."""


class FeatureRoutingGate:
    """Ensure one committed placement before any model-bearing dispatch."""

    def __init__(
        self,
        store: FeatureRoutingSeedStore,
        router_url: str,
        *,
        timeout_seconds: float = 15.0,
        request: Callable[[str, str, float], tuple[int, object]] | None = None,
    ) -> None:
        self._store = store
        self._router_url = router_url.rstrip("/")
        self._timeout = timeout_seconds
        self._request = request or self._request_seed
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def ensure_seeded(
        self, feature_routing_id: str, *, origin_kind: str, origin_id: str
    ) -> FeatureRoutingReceipt:
        key = validate_feature_routing_id(feature_routing_id)
        lock = await self._lock_for(key)
        async with lock:
            try:
                return self._store.read_success_receipt(key)
            except FeatureRoutingBlocked:
                pass
            attempt_id = uuid.uuid4().hex
            claim = self._store.claim_initial_seed(
                key, attempt_id, origin_kind=origin_kind, origin_id=origin_id
            )
            if claim.receipt is not None:
                return claim.receipt
            try:
                if inspect.iscoroutinefunction(self._request):
                    pending = self._request(self._router_url, key, self._timeout)
                else:
                    pending = asyncio.to_thread(
                        self._request, self._router_url, key, self._timeout
                    )
                status, headers = await asyncio.wait_for(
                    pending, timeout=self._timeout
                )
                if status != 200:
                    raise SeedResponseRejected(f"seed-http-status-{status}")
                stored = self._header(headers, STORED_SERVER_HEADER)
                actual = self._header(headers, ACTUAL_SERVER_HEADER)
                try:
                    stored_id, actual_id = int(stored), int(actual)
                except (TypeError, ValueError) as exc:
                    raise SeedResponseRejected("seed-ack-invalid") from exc
                if stored_id not in (1, 2) or stored_id != actual_id:
                    raise SeedResponseRejected("seed-ack-mismatch")
                return self._store.complete_seed(key, attempt_id, stored_id)
            except asyncio.CancelledError:
                try:
                    self._store.fail_seed(
                        key, attempt_id, state="UNKNOWN", reason="seed-cancelled"
                    )
                except BaseException:
                    # Cancellation must still propagate.  A failed best-effort
                    # CAS leaves STARTED, which remains blocking and is
                    # poisoned to UNKNOWN at the next coordinator boot.
                    pass
                raise
            except SeedResponseRejected as exc:
                self._store.fail_seed(
                    key, attempt_id, state="FAILED", reason=str(exc)
                )
                raise
            except BaseException as exc:
                self._store.fail_seed(
                    key,
                    attempt_id,
                    state="UNKNOWN",
                    reason=f"seed-transport-ambiguous:{type(exc).__name__}",
                )
                raise FeatureRoutingBlocked(
                    "feature routing seed outcome is unknown"
                ) from exc

    def require_committed_success(
        self, feature_routing_id: str
    ) -> FeatureRoutingReceipt:
        """Read-only gate for recovery/card seams that may never bootstrap."""
        return self._store.read_success_receipt(feature_routing_id)

    @staticmethod
    def _header(headers: object, name: str) -> str | None:
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            values = get_all(name) or []
            if len(values) != 1:
                return None
            value = values[0]
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                return None
            return value
        getter = getattr(headers, "get", None)
        value = getter(name) if callable(getter) else None
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            return None
        return value

    @staticmethod
    def _request_seed(url: str, key: str, timeout: float) -> tuple[int, object]:
        request = urllib.request.Request(
            f"{url}/factory/seed", headers={FEATURE_HEADER: key}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
                return int(response.status), response.headers
        except urllib.error.HTTPError as exc:
            exc.read()
            return int(exc.code), exc.headers
