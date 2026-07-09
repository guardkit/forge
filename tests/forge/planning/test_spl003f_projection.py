"""Checkpoint detail projection + J04 contract-fixture gate (TASK-SPL003F-001).

The **GATE**: forge's real ``build_planning_approval_envelope`` projection must
satisfy jarvis's J04 contract fixture
(``tests/fixtures/spl003_forge_details.json`` — a byte-copy of jarvis
``tests/fixtures/spl003_forge_details.json``, the consumer contract). The
producer is a superset of the fixture — jarvis reads a subset of ``details``
(``checkpoint_type`` / ``build_id`` / ``feature_id`` / ``expected_approver`` /
``summary.assumptions`` / ``parent_request_id`` / ``cycle`` / ``attempt_count``)
and ignores forge-internal routing keys — so satisfaction is a **deep-contains**
of every fixture key path, with the shared ``summary`` sub-tree byte-identical.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nats_core.events import ApprovalRequestPayload, NotificationPayload

from forge.gating.identity import derive_request_id
from forge.planning.checkpoint import (
    build_planning_approval_envelope,
    checkpoint_product_docs,
)
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.notifications import build_planning_notification_envelope
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "spl003_forge_details.json"
_JARVIS_FIXTURE = (
    Path(__file__).parents[4]
    / "jarvis"
    / "tests"
    / "fixtures"
    / "spl003_forge_details.json"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _assert_deep_contains(expected: Any, actual: Any, path: str = "details") -> None:
    """Assert every key path / value in ``expected`` appears (deep) in ``actual``."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected a dict, got {type(actual)}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key}: missing from forge projection"
            _assert_deep_contains(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list"
        assert len(expected) == len(actual), f"{path}: list length differs"
        for i, (exp, act) in enumerate(zip(expected, actual)):
            _assert_deep_contains(exp, act, f"{path}[{i}]")
    else:
        assert expected == actual, f"{path}: {expected!r} != {actual!r}"


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# GATE — the projection satisfies J04's contract fixture byte-for-byte
# ---------------------------------------------------------------------------


class TestForgeSatisfiesJ04Fixture:
    def test_projection_deep_contains_the_fixture(self):
        """Generate the details payload for the fixture scenario and diff it
        against jarvis's J04 fixture (deep-contains on every contract key)."""
        fixture = _load_fixture()
        envelope = build_planning_approval_envelope(
            request_id=derive_request_id(
                build_id=fixture["build_id"],
                stage_label="product_docs",
                attempt_count=fixture["attempt_count"],
            ),
            plan_run_id=fixture["build_id"],
            feature_id=fixture["feature_id"],
            stage_label="product_docs",
            summary_data={},
            expected_approver=fixture["expected_approver"],
            attempt_count=fixture["attempt_count"],
            checkpoint_type=fixture["checkpoint_type"],
            parent_request_id=fixture["parent_request_id"],
            cycle=fixture["cycle"],
            assumptions=fixture["summary"]["assumptions"],
        )
        # The payload is a wire-valid ApprovalRequestPayload (jarvis JNB-103).
        details = ApprovalRequestPayload.model_validate(envelope.payload).details
        _assert_deep_contains(fixture, details)

    def test_summary_subtree_is_byte_identical(self):
        """The shared ``summary`` sub-tree (checkpoint + assumptions) matches
        the fixture exactly — the shape jarvis's J02 renders per-item."""
        fixture = _load_fixture()
        envelope = build_planning_approval_envelope(
            request_id="req-1",
            plan_run_id=fixture["build_id"],
            feature_id=fixture["feature_id"],
            stage_label="product_docs",
            summary_data={},
            expected_approver=fixture["expected_approver"],
            attempt_count=fixture["attempt_count"],
            parent_request_id=fixture["parent_request_id"],
            cycle=fixture["cycle"],
            assumptions=fixture["summary"]["assumptions"],
        )
        details = ApprovalRequestPayload.model_validate(envelope.payload).details
        assert details["summary"] == fixture["summary"]

    def test_forge_fixture_copy_matches_jarvis_source(self):
        """Drift guard: the vendored fixture is byte-identical to jarvis's
        J04 fixture when the sibling repo is present."""
        if not _JARVIS_FIXTURE.exists():
            pytest.skip("jarvis sibling repo not present")
        assert (
            _FIXTURE.read_bytes() == _JARVIS_FIXTURE.read_bytes()
        ), "forge fixture copy has drifted from jarvis's J04 contract fixture"

    def test_producer_is_a_superset(self):
        """Documented non-divergence: forge carries additional internal routing
        keys the jarvis consumer ignores by design (never a full-equality
        contract — the committed fixture already omits them)."""
        fixture = _load_fixture()
        envelope = build_planning_approval_envelope(
            request_id="req-1",
            plan_run_id=fixture["build_id"],
            feature_id=fixture["feature_id"],
            stage_label="product_docs",
            summary_data={},
            expected_approver=fixture["expected_approver"],
            attempt_count=fixture["attempt_count"],
            parent_request_id=fixture["parent_request_id"],
            cycle=fixture["cycle"],
            assumptions=fixture["summary"]["assumptions"],
        )
        details = ApprovalRequestPayload.model_validate(envelope.payload).details
        # forge-internal keys jarvis ignores:
        for internal_key in ("stage_label", "gate_mode", "rationale"):
            assert internal_key in details
            assert internal_key not in fixture


# ---------------------------------------------------------------------------
# Non-dialogue path: the summary rides through unchanged (no regression)
# ---------------------------------------------------------------------------


def test_no_assumptions_leaves_summary_unchanged():
    summary_data = {"title": "Docs", "sections": ["A", "B"]}
    envelope = build_planning_approval_envelope(
        request_id="req-1",
        plan_run_id="plan-x",
        feature_id="FEAT-X",
        stage_label="product_docs",
        summary_data=summary_data,
    )
    details = ApprovalRequestPayload.model_validate(envelope.payload).details
    assert details["summary"] == summary_data
    # anchors present but None (jarvis degrades to a top-level post)
    assert details["parent_request_id"] is None
    assert details["cycle"] is None


# ---------------------------------------------------------------------------
# Real checkpoint: parent_request_id read from the row, cycle computed durably
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    from forge.lifecycle import migrations as lifecycle_migrations

    db_path = tmp_path / "planning.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    lifecycle_migrations.apply_at_boot(conn)
    return conn


class _FakePublisher:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


class _AssumptionsProvider:
    def __init__(self, assumptions: list[dict[str, Any]]) -> None:
        self._assumptions = assumptions

    async def get_summary_for_approval(
        self, *, plan_run_id: str, stage_label: str
    ) -> dict[str, Any]:
        return {"checkpoint": stage_label, "assumptions": list(self._assumptions)}


@pytest.mark.asyncio
async def test_real_checkpoint_projects_durable_anchor_and_cycle(tmp_db):
    store = SqlitePlanningRunStore(tmp_db)
    repository, state_machine = build_planning_gate_adapters(store, clock=_fixed_clock)

    correlation_id = "4d5e205f"
    store.record_queued(
        correlation_id=correlation_id,
        originating_user="U03QR8WKT29",
        expected_approver="U03QR8WKT29",
        request_text="plan the thing",
        triggered_by="jarvis",
        parent_request_id="1700000000.000100",
    )
    store.transition(
        correlation_id=correlation_id,
        to_state=PlanningState.RUNNING,
        actor_identity="test",
    )

    assumptions = _load_fixture()["summary"]["assumptions"]
    publisher = _FakePublisher()
    await checkpoint_product_docs(
        plan_run_id=f"plan-{correlation_id}",
        feature_id="FEAT-PLANNING",
        repository=repository,
        state_machine=state_machine,
        publisher=publisher,
        second_opinion_provider=_AssumptionsProvider(assumptions),
        clock=_fixed_clock,
    )

    assert len(publisher.envelopes) == 1
    details = ApprovalRequestPayload.model_validate(
        publisher.envelopes[0].payload
    ).details
    # parent_request_id read from the durable row (DD-SPL003-1, never re-derived)
    assert details["parent_request_id"] == "1700000000.000100"
    # cycle 1 on the initial checkpoint (zero recorded revisions)
    assert details["cycle"] == 1
    assert details["checkpoint_type"] == "product_docs"
    assert details["summary"]["assumptions"] == assumptions
    assert details["expected_approver"] == "U03QR8WKT29"


# ---------------------------------------------------------------------------
# Part 4: outbound notification projection
# ---------------------------------------------------------------------------


class TestNotificationProjection:
    def test_anchor_projected_when_present(self):
        envelope = build_planning_notification_envelope(
            correlation_id="cid1",
            message="Planning handoff reached",
            parent_request_id="1700000000.000100",
            target_user="U03QR8WKT29",
        )
        payload = NotificationPayload.model_validate(envelope.payload)
        assert payload.parent_request_id == "1700000000.000100"
        assert payload.thread_ts == "1700000000.000100"
        assert payload.target_user == "U03QR8WKT29"
        assert payload.adapter == "slack"

    def test_degrades_to_top_level_when_anchor_absent(self):
        envelope = build_planning_notification_envelope(
            correlation_id="cid1", message="thin update"
        )
        payload = NotificationPayload.model_validate(envelope.payload)
        assert payload.parent_request_id is None
        assert payload.thread_ts is None
        assert payload.target_user is None
        assert payload.message == "thin update"
