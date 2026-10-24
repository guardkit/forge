"""Coordinator-owned HTTP gate for durable feature placement."""

from __future__ import annotations

import asyncio
import inspect
import logging
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
SEED_SERVER_HEADER = "x-feature-seed-server"

logger = logging.getLogger(__name__)


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
        request: Callable[[str, str, float, int], tuple[int, object]] | None = None,
    ) -> None:
        self._store = store
        self._router_url = router_url.rstrip("/")
        self._timeout = timeout_seconds
        self._request = request or self._request_seed
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        # One placement at a time across all keys, so each admission counts
        # every earlier placement before choosing a seat.
        self._placement_lock = asyncio.Lock()

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
            async with self._placement_lock:
                preference = self._preferred_server()
                attempt_id = uuid.uuid4().hex
                claim = self._store.claim_initial_seed(
                    key, attempt_id, origin_kind=origin_kind, origin_id=origin_id
                )
                if claim.receipt is not None:
                    return claim.receipt
                try:
                    stored_id = await self._seed_once(key, attempt_id, preference)
                except SeedResponseRejected as first:
                    # RETRY ONCE (step 1, 24 October 2026): a completed,
                    # rejected response is asked once more under the same
                    # routing id. An unknown outcome (timeout, partial
                    # response, cancellation, restart) is never replayed.
                    attempt_id = uuid.uuid4().hex
                    if not self._store.retry_failed_seed(key, attempt_id):
                        raise first
                    stored_id = await self._seed_once(key, attempt_id, preference)
                return self._record_success(key, attempt_id, stored_id)

    async def _seed_once(self, key: str, attempt_id: str, preference: int) -> int:
        """One seed request for the claimed attempt; the acknowledged server.

        A completed rejection is recorded FAILED and raised as
        :class:`SeedResponseRejected`; anything else that stops the exchange
        is recorded UNKNOWN. The success record is written by the caller,
        outside this exchange, so a failed write is never mistaken for an
        unknown HTTP result.
        """
        try:
            if inspect.iscoroutinefunction(self._request):
                pending = self._request(self._router_url, key, self._timeout, preference)
            else:
                pending = asyncio.to_thread(
                    self._request,
                    self._router_url,
                    key,
                    self._timeout,
                    preference,
                )
            status, headers = await asyncio.wait_for(pending, timeout=self._timeout)
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
            return stored_id
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
            self._store.fail_seed(key, attempt_id, state="FAILED", reason=str(exc))
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

    def _record_success(
        self, key: str, attempt_id: str, stored_id: int
    ) -> FeatureRoutingReceipt:
        """Write a valid acknowledgement's record, retrying the write once.

        The router has answered; only the record failed. The request is never
        sent again for it.
        """
        try:
            return self._store.complete_seed(key, attempt_id, stored_id)
        except Exception as first:  # noqa: BLE001 — the write is retried once
            logger.warning(
                "feature routing: %s was acknowledged but its record was not "
                "written (%s: %s); writing it once more",
                key,
                type(first).__name__,
                first,
            )
        try:
            return self._store.complete_seed(key, attempt_id, stored_id)
        except Exception as exc:
            try:
                self._store.fail_seed(
                    key,
                    attempt_id,
                    state="UNKNOWN",
                    reason="seed-acknowledged-record-not-written",
                )
            except Exception:  # noqa: BLE001 — the raise below says what happened
                pass
            raise FeatureRoutingBlocked(
                "feature routing seed was acknowledged but its record could not "
                "be written"
            ) from exc

    def _preferred_server(self) -> int:
        """Prefer the server with fewer live pins; ties go to server 1."""
        counts = self._store.live_pin_counts()
        return 2 if counts.get(2, 0) < counts.get(1, 0) else 1

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
    def _request_seed(
        url: str, key: str, timeout: float, server_preference: int
    ) -> tuple[int, object]:
        request = urllib.request.Request(
            f"{url}/factory/seed",
            headers={
                FEATURE_HEADER: key,
                SEED_SERVER_HEADER: str(server_preference),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
                return int(response.status), response.headers
        except urllib.error.HTTPError as exc:
            exc.read()
            return int(exc.code), exc.headers
