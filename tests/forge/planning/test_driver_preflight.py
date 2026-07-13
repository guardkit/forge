"""O-27 / O-29 (E2-S4) — the driver's run-start resource preflight wiring.

Proves the seam, not the readings (those are covered purely in
``tests/forge/test_preflight.py``):

- a BREACH at the fresh QUEUED→RUNNING boundary refuses the run into a loud
  FAILED terminal + an error notification (route-and-notify), and NO
  seat-holding PO dispatch ever fires;
- an OK verdict (and an unwired ``None`` preflight) lets control through to the
  normal chain (PO dispatch is reached).

The preflight is injected as a zero-arg callable, so the driver never touches
``/proc`` or ``shutil`` here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from forge.preflight import ResourceBreach, ResourcePreflightResult

CID = "pf-run-0001"
ORIGINATOR = "U0RIGINATOR"


class _POReached(Exception):
    """Raised by the PO-dispatch spy to prove control passed the preflight."""


def _store(tmp_path: Path) -> SqlitePlanningRunStore:
    cx = sqlite_connect.connect_writer(tmp_path / "pf.db")
    migrations.apply_at_boot(cx)
    return SqlitePlanningRunStore(cx, target_terminal_enabled=False)


def _queue(store: SqlitePlanningRunStore) -> None:
    store.record_queued(
        correlation_id=CID,
        originating_user=ORIGINATOR,
        expected_approver=ORIGINATOR,
        request_text="add a GET /stats endpoint",
        triggered_by="jarvis",
    )


def _driver(
    store: SqlitePlanningRunStore,
    *,
    preflight: Any,
    notifications: list[tuple[str, str, str]],
    dispatch_po: Any,
) -> PlanningRunDriver:
    async def publish_notification(cid: str, message: str, level: str) -> None:
        notifications.append((cid, message, level))

    deps = PlanningDriverDeps(
        store=store,
        # Unreached on the tested paths — trivial stand-ins.
        repository=SimpleNamespace(),
        state_machine=SimpleNamespace(),
        approval_publisher=SimpleNamespace(),
        subscriber_factory=lambda *_a, **_k: SimpleNamespace(),
        dispatch_product_owner=dispatch_po,
        second_opinion_provider=SimpleNamespace(),
        git_runner=SimpleNamespace(),
        planning_config=SimpleNamespace(),
        clock=lambda: datetime.now(timezone.utc),
        publish_notification=publish_notification,
        resource_preflight=preflight,
    )
    return PlanningRunDriver(deps)


def _breach_result() -> ResourcePreflightResult:
    return ResourcePreflightResult(
        ok=False,
        breaches=(
            ResourceBreach(resource="memory", available_gb=3.2, floor_gb=8.0),
        ),
        checked=("memory", "disk"),
    )


@pytest.mark.asyncio
async def test_breach_refuses_run_before_any_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _queue(store)
    notifications: list[tuple[str, str, str]] = []
    po_calls: list[dict[str, Any]] = []

    async def dispatch_po(**kwargs: Any) -> Any:  # pragma: no cover - must not run
        po_calls.append(kwargs)
        raise AssertionError("PO dispatch must not fire when the box is starved")

    driver = _driver(
        store,
        preflight=lambda: _breach_result(),
        notifications=notifications,
        dispatch_po=dispatch_po,
    )

    await driver.drive(CID)

    # Loud terminal: FAILED, never a running zombie.
    assert store.get_run(CID)["state"] == PlanningState.FAILED.value
    # No seat-holding dispatch happened.
    assert po_calls == []
    # Route-and-notify: exactly one error notification carrying the specific
    # breach detail.
    assert len(notifications) == 1
    cid, message, level = notifications[0]
    assert cid == CID
    assert level == "error"
    assert "resource-preflight" in message.lower() or "preflight FAILED" in message
    assert "memory 3.2 GB < 8.0 GB floor" in message
    # The failure was tagged as a resource-preflight refusal in the durable log.
    labels = {e["stage_label"] for e in store.list_events(CID)}
    assert "resource-preflight" in labels


@pytest.mark.asyncio
async def test_ok_verdict_lets_control_through_to_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _queue(store)
    notifications: list[tuple[str, str, str]] = []

    async def dispatch_po(**kwargs: Any) -> Any:
        raise _POReached

    driver = _driver(
        store,
        preflight=lambda: ResourcePreflightResult(ok=True, checked=("memory", "disk")),
        notifications=notifications,
        dispatch_po=dispatch_po,
    )

    # Reaching the PO dispatch proves the preflight passed control through.
    await driver.drive(CID)
    # PO dispatch raised (caught by the driver → run FAILED at product_owner,
    # NOT at resource-preflight) — the seam let the run start.
    labels = {e["stage_label"] for e in store.list_events(CID)}
    assert "product_owner" in labels
    assert "resource-preflight" not in labels


@pytest.mark.asyncio
async def test_unwired_preflight_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _queue(store)
    notifications: list[tuple[str, str, str]] = []

    async def dispatch_po(**kwargs: Any) -> Any:
        raise _POReached

    driver = _driver(
        store,
        preflight=None,  # unwired = byte-for-byte no-op
        notifications=notifications,
        dispatch_po=dispatch_po,
    )

    await driver.drive(CID)
    labels = {e["stage_label"] for e in store.list_events(CID)}
    assert "product_owner" in labels
    assert "resource-preflight" not in labels
