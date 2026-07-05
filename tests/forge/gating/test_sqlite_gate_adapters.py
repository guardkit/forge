"""Tests for :mod:`forge.gating.sqlite_adapters` (TASK-GATE-D659, Wave 1).

The SQLite gate adapters compose the tested ``forge.lifecycle.persistence``
facades so the live ``gate_check`` path runs unchanged against a real
database. These tests prove **semantic parity** with the in-memory fakes
(``tests/integration/conftest.py`` ``InMemoryRepository`` /
``InMemoryStateMachine``) plus the invariants the design leans on:

* single-transition-owner — the state machine owns every ``builds.status``
  write; the repository's ``mark_resumed`` / ``mark_cancelled`` are no-ops,
  so each logical cancel produces exactly one ``apply_transition``
  (arch-review M1);
* ``StaleTransitionError`` on already-terminal rows (raised *before* any
  publish);
* ``parse_request_id`` round-trips ``derive_request_id``;
* ``refresh_pending_approval_request_id`` is status-preserving and raises on
  a missing row.

Async adapter methods are exercised via ``asyncio.run`` inside sync test
methods (mirroring ``tests/forge/test_lifecycle_recovery.py``); the suite
never touches wall-clock time — a frozen ``FixedClock`` is injected.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.gating.identity import derive_request_id, parse_request_id
from forge.gating.models import (
    DetectionFinding,
    GateDecision,
    GateMode,
)
from forge.gating.sqlite_adapters import (
    StaleTransitionError,
    build_sqlite_gate_adapters,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)

# In-memory fakes the SQLite adapters must be substitutable for.
from tests.integration.conftest import (
    InMemoryRepository,
    InMemoryStateMachine,
)

STAGE_LABEL = "autobuild"
FROZEN = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    """Frozen ``() -> datetime`` (UTC) — clock hygiene, never wall-clock."""

    def __init__(self, fixed: datetime = FROZEN) -> None:
        self._fixed = fixed

    def __call__(self) -> datetime:
        return self._fixed


# ---------------------------------------------------------------------------
# Fixtures / seeding helpers (mirror test_lifecycle_recovery.py shape)
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str,
    correlation_id: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/test/test.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=datetime(2026, 7, 5, 11, 0, 0, tzinfo=UTC),
        requested_at=datetime(2026, 7, 5, 11, 0, 0, tzinfo=UTC),
    )


@pytest.fixture()
def writer_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db)


@pytest.fixture()
def adapters(persistence: SqliteLifecyclePersistence):
    return build_sqlite_gate_adapters(persistence, clock=FixedClock())


def _seed_running(
    persistence: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> str:
    """Seed a build and drive QUEUED → PREPARING → RUNNING."""
    build_id = persistence.record_pending_build(
        _make_payload(feature_id=feature_id, correlation_id=correlation_id)
    )
    for frm, to in (
        (BuildState.QUEUED, BuildState.PREPARING),
        (BuildState.PREPARING, BuildState.RUNNING),
    ):
        persistence.apply_transition(
            compose_transition(Build(build_id=build_id, status=frm), to)
        )
    return build_id


def _status(persistence: SqliteLifecyclePersistence, build_id: str) -> str:
    row = persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    return row["status"]


def _pending(persistence: SqliteLifecyclePersistence, build_id: str) -> str | None:
    row = persistence.connection.execute(
        "SELECT pending_approval_request_id FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return row["pending_approval_request_id"]


def _error(persistence: SqliteLifecyclePersistence, build_id: str) -> str | None:
    row = persistence.connection.execute(
        "SELECT error FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    return row["error"]


def _decision(
    build_id: str,
    *,
    stage_label: str = STAGE_LABEL,
    mode: GateMode = GateMode.FLAG_FOR_REVIEW,
    coach_score: float | None = 0.7,
    findings: list[DetectionFinding] | None = None,
) -> GateDecision:
    return GateDecision(
        build_id=build_id,
        stage_label=stage_label,
        target_kind="subagent",
        target_identifier="autobuild_runner",
        mode=mode,
        rationale="paused for review",
        coach_score=coach_score,
        criterion_breakdown={"completeness": coach_score or 0.0},
        detection_findings=findings or [],
        evidence=[],
        decided_at=FROZEN,
    )


async def _pause_via_adapters(
    repo: Any,
    sm: Any,
    *,
    build_id: str,
    feature_id: str,
    decision: GateDecision,
    request_id: str,
    attempt_count: int = 0,
) -> None:
    """Run the pause sequence the way ``gate_check`` does."""
    await repo.record_decision(decision)
    await repo.record_paused_build(
        build_id=build_id,
        feature_id=feature_id,
        stage_label=decision.stage_label,
        request_id=request_id,
        attempt_count=attempt_count,
        decision=decision,
    )
    await sm.transition_to_paused(build_id=build_id, stage_label=decision.stage_label)


# ---------------------------------------------------------------------------
# parse_request_id (inverse of derive_request_id)
# ---------------------------------------------------------------------------


class TestParseRequestId:
    """``parse_request_id`` is the exact inverse of ``derive_request_id``."""

    @pytest.mark.parametrize(
        ("build_id", "stage_label", "attempt_count"),
        [
            ("build-1", "autobuild", 0),
            ("build-1", "Architecture Review", 7),
            ("b:c", "s:d", 3),
            ("build with spaces", "stage.with.dots", 2),
            ("build.with.dots~x", "stage~with~tilde", 5),
            ("ünïcode", "läbel", 12),
            ("b/c?d#e", "p.q*r>s", 1),
        ],
    )
    def test_round_trips_derive(
        self, build_id: str, stage_label: str, attempt_count: int
    ) -> None:
        rid = derive_request_id(
            build_id=build_id,
            stage_label=stage_label,
            attempt_count=attempt_count,
        )
        assert parse_request_id(rid) == (build_id, stage_label, attempt_count)

    @pytest.mark.parametrize(
        "bad",
        ["", "nocolons", "only:one", "a:b:c:d", "a:b:notanint", "a:b:-1"],
    )
    def test_malformed_raises_value_error(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_request_id(bad)


# ---------------------------------------------------------------------------
# refresh_pending_approval_request_id (persistence facade)
# ---------------------------------------------------------------------------


class TestRefreshPendingApprovalRequestId:
    """Status-preserving UPDATE; raises on a missing row."""

    def test_preserves_status_and_updates_id(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_running(
            persistence, feature_id="FEAT-RF-001", correlation_id="c-rf"
        )
        persistence.mark_paused(build_id, "req-original")
        assert _status(persistence, build_id) == "PAUSED"

        persistence.refresh_pending_approval_request_id(build_id, "req-refreshed")

        # Status is untouched; only the pending id changed.
        assert _status(persistence, build_id) == "PAUSED"
        assert _pending(persistence, build_id) == "req-refreshed"

    def test_missing_row_raises_runtime_error(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(RuntimeError):
            persistence.refresh_pending_approval_request_id("nope", "req")

    def test_terminal_row_is_soft_noop_not_error(
        self,
        persistence: SqliteLifecyclePersistence,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A concurrent terminal transition landing between the caller's PAUSED
        # precheck and this UPDATE must NOT stamp a fresh request_id onto the
        # already-terminal row (single-owner discipline). The status predicate
        # makes the UPDATE a 0-row no-op; the row-exists re-read softens it to
        # a logged skip rather than a RuntimeError.
        build_id = _seed_running(
            persistence, feature_id="FEAT-RF-002", correlation_id="c-rf2"
        )
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.RUNNING),
                BuildState.CANCELLED,
            )
        )

        with caplog.at_level(logging.WARNING):
            # No raise — soft no-op.
            persistence.refresh_pending_approval_request_id(build_id, "req-late")

        assert _status(persistence, build_id) == "CANCELLED"
        # The terminal row's pending id was NOT stamped.
        assert _pending(persistence, build_id) is None
        assert any(
            "refresh skipped" in rec.getMessage() and rec.levelno >= logging.WARNING
            for rec in caplog.records
        )

    @pytest.mark.parametrize(("bid", "rid"), [("", "r"), ("b", "")])
    def test_empty_args_raise_value_error(
        self, persistence: SqliteLifecyclePersistence, bid: str, rid: str
    ) -> None:
        with pytest.raises(ValueError):
            persistence.refresh_pending_approval_request_id(bid, rid)


# ---------------------------------------------------------------------------
# record_decision — first-ever writer of details_json["gate"]
# ---------------------------------------------------------------------------


class TestRecordDecision:
    def test_writes_gated_stage_log_row_with_snapshot(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, _sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-RD-001", correlation_id="c-rd"
        )
        decision = _decision(build_id)

        asyncio.run(repo.record_decision(decision))

        stages = persistence.read_stages(build_id)
        gated = [s for s in stages if s.details.get("gate")]
        assert len(gated) == 1
        assert gated[0].status == "GATED"
        assert gated[0].gate_mode == GateMode.FLAG_FOR_REVIEW.value
        assert gated[0].details["gate"]["build_id"] == build_id
        assert gated[0].details["gate"]["mode"] == GateMode.FLAG_FOR_REVIEW.value

    def test_write_to_graphiti_is_noop(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, _sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-RD-002", correlation_id="c-rd2"
        )
        # Must not raise and must not touch the DB.
        asyncio.run(repo.write_to_graphiti(_decision(build_id)))
        assert persistence.read_stages(build_id) == []


# ---------------------------------------------------------------------------
# Pause round-trip parity with the in-memory fakes
# ---------------------------------------------------------------------------


class TestPauseParity:
    def test_pause_records_paused_and_request_id(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-PP-001", correlation_id="c-pp"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        decision = _decision(build_id)

        # --- SQLite adapters ---
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-PP-001",
                decision=decision,
                request_id=request_id,
            )
        )
        assert _status(persistence, build_id) == "PAUSED"
        assert _pending(persistence, build_id) == request_id

        # --- in-memory fakes, same call sequence ---
        mem_repo = InMemoryRepository()
        mem_sm = InMemoryStateMachine()
        asyncio.run(
            _pause_via_adapters(
                mem_repo,
                mem_sm,
                build_id=build_id,
                feature_id="FEAT-PP-001",
                decision=decision,
                request_id=request_id,
            )
        )
        # Parity: both record the pause + the same request_id, both leave
        # the build observably PAUSED.
        assert mem_sm.status_log[-1] == (build_id, "PAUSED")
        assert mem_repo.paused[0].request_id == request_id
        assert mem_repo.paused[0].request_id == _pending(persistence, build_id)

    def test_list_paused_builds_reconstructs_snapshot(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-PP-002", correlation_id="corr-xyz"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        decision = _decision(build_id)
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-PP-002",
                decision=decision,
                request_id=request_id,
            )
        )

        snaps = asyncio.run(repo.list_paused_builds())
        assert len(snaps) == 1
        snap = snaps[0]
        assert snap.build_id == build_id
        assert snap.feature_id == "FEAT-PP-002"
        assert snap.stage_label == STAGE_LABEL
        assert snap.request_id == request_id
        assert snap.attempt_count == 0
        assert snap.correlation_id == "corr-xyz"
        # Decision rehydrated verbatim from details_json["gate"].
        assert snap.decision_snapshot.mode is GateMode.FLAG_FOR_REVIEW
        assert snap.decision_snapshot.build_id == build_id

    def test_list_paused_builds_skips_non_paused(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, _sm = adapters
        # A RUNNING build is non-terminal but not PAUSED — must be skipped.
        _seed_running(persistence, feature_id="FEAT-PP-003", correlation_id="c-pp3")
        assert asyncio.run(repo.list_paused_builds()) == []

    def test_list_paused_builds_degraded_fallback(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, _sm = adapters
        # Pause a build directly (no record_decision) so no gate snapshot
        # exists in stage_log — list_paused_builds must fall back to a
        # degraded decision rather than crash.
        build_id = _seed_running(
            persistence, feature_id="FEAT-PP-004", correlation_id="c-pp4"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        persistence.mark_paused(build_id, request_id)

        snaps = asyncio.run(repo.list_paused_builds())
        assert len(snaps) == 1
        assert snaps[0].decision_snapshot.mode is GateMode.MANDATORY_HUMAN_APPROVAL
        assert snaps[0].decision_snapshot.degraded_mode is True
        assert snaps[0].stage_label == STAGE_LABEL

    def test_list_paused_builds_skips_unparseable_request_id(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, _sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-PP-005", correlation_id="c-pp5"
        )
        # Legacy / corrupt pending id that parse_request_id cannot parse.
        persistence.mark_paused(build_id, "legacy-unparseable-id")
        # Skipped with an ERROR log, not raised.
        assert asyncio.run(repo.list_paused_builds()) == []


# ---------------------------------------------------------------------------
# Defer refresh — record_paused_build on an already-PAUSED row
# ---------------------------------------------------------------------------


class TestDeferRefresh:
    def test_second_record_paused_build_refreshes_without_transition(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-DF-001", correlation_id="c-df"
        )
        decision = _decision(build_id)
        r0 = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-DF-001",
                decision=decision,
                request_id=r0,
            )
        )

        # Count status writes across the defer refresh — there must be zero
        # (the refresh is status-preserving; single-transition-owner holds).
        writes = _CountingApplyTransition(persistence)
        r1 = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=1
        )
        with writes:
            asyncio.run(
                repo.record_paused_build(
                    build_id=build_id,
                    feature_id="FEAT-DF-001",
                    stage_label=STAGE_LABEL,
                    request_id=r1,
                    attempt_count=1,
                    decision=decision,
                )
            )

        assert writes.count == 0
        assert _status(persistence, build_id) == "PAUSED"
        assert _pending(persistence, build_id) == r1


# ---------------------------------------------------------------------------
# Resume / cancel / fail transitions + single-transition-owner
# ---------------------------------------------------------------------------


class _CountingApplyTransition:
    """Context manager that counts ``apply_transition`` invocations."""

    def __init__(self, persistence: SqliteLifecyclePersistence) -> None:
        self._persistence = persistence
        self._orig = persistence.apply_transition
        self.count = 0

    def __enter__(self) -> "_CountingApplyTransition":
        def _wrapped(transition: Any) -> None:
            self.count += 1
            return self._orig(transition)

        self._persistence.apply_transition = _wrapped  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: Any) -> None:
        self._persistence.apply_transition = self._orig  # type: ignore[method-assign]


class TestTransitions:
    def test_transition_to_running_clears_pending(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-TR-001", correlation_id="c-tr"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-TR-001",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )

        asyncio.run(sm.transition_to_running(build_id=build_id))

        assert _status(persistence, build_id) == "RUNNING"
        # PAUSED -> RUNNING clears the pending approval id in the same UPDATE.
        assert _pending(persistence, build_id) is None

    def test_transition_to_failed_records_reason(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        _repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-TF-001", correlation_id="c-tf"
        )
        asyncio.run(sm.transition_to_failed(build_id=build_id, reason="gate hard stop"))
        assert _status(persistence, build_id) == "FAILED"
        assert _error(persistence, build_id) == "gate hard stop"

    def test_transition_to_cancelled_is_sole_writer_reject_order(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        """Reject order: repo.mark_cancelled (no-op) then SM cancel = 1 write."""
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-TC-001", correlation_id="c-tc"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-TC-001",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )

        with _CountingApplyTransition(persistence) as writes:
            asyncio.run(repo.mark_cancelled(build_id=build_id, reason="rejected"))
            asyncio.run(
                sm.transition_to_cancelled(build_id=build_id, reason="rejected")
            )

        assert writes.count == 1  # exactly one status write — the SM's
        assert _status(persistence, build_id) == "CANCELLED"
        assert _error(persistence, build_id) == "rejected"

    def test_transition_to_cancelled_is_sole_writer_maxwait_order(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        """Max-wait order: SM cancel then repo.mark_cancelled (no-op) = 1 write."""
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-TC-002", correlation_id="c-tc2"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-TC-002",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )

        with _CountingApplyTransition(persistence) as writes:
            asyncio.run(
                sm.transition_to_cancelled(build_id=build_id, reason="max wait")
            )
            asyncio.run(repo.mark_cancelled(build_id=build_id, reason="max wait"))

        assert writes.count == 1
        assert _status(persistence, build_id) == "CANCELLED"

    def test_mark_resumed_is_noop(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-MR-001", correlation_id="c-mr"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-MR-001",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )
        with _CountingApplyTransition(persistence) as writes:
            asyncio.run(repo.mark_resumed(build_id=build_id, stage_label=STAGE_LABEL))
        assert writes.count == 0
        # Still PAUSED — mark_resumed does not transition (the SM owns it).
        assert _status(persistence, build_id) == "PAUSED"

    def test_mark_overridden_records_skipped_stage(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-OV-001", correlation_id="c-ov"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-OV-001",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )
        asyncio.run(
            repo.mark_overridden(
                build_id=build_id, stage_label=STAGE_LABEL, reason="operator override"
            )
        )
        skipped = [
            s
            for s in persistence.read_stages(build_id)
            if s.status == "SKIPPED" and s.stage_label == STAGE_LABEL
        ]
        assert len(skipped) == 1
        assert skipped[0].details["rationale"] == "operator override"


# ---------------------------------------------------------------------------
# StaleTransitionError — cancel of an already-terminal row
# ---------------------------------------------------------------------------


class TestStaleTransition:
    def test_cancel_of_cancelled_row_raises_stale(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-ST-001", correlation_id="c-st"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-ST-001",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )
        # First cancel wins.
        asyncio.run(sm.transition_to_cancelled(build_id=build_id, reason="first"))
        assert _status(persistence, build_id) == "CANCELLED"

        # Second cancel targets a terminal row → StaleTransitionError, and
        # critically it raises BEFORE any further write (error unchanged).
        with _CountingApplyTransition(persistence) as writes:
            with pytest.raises(StaleTransitionError):
                asyncio.run(
                    sm.transition_to_cancelled(build_id=build_id, reason="second")
                )
        assert writes.count == 0
        assert _error(persistence, build_id) == "first"

    def test_cancel_of_completed_row_raises_stale(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        _repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-ST-002", correlation_id="c-st2"
        )
        # Drive RUNNING -> FINALISING -> COMPLETE.
        for frm, to in (
            (BuildState.RUNNING, BuildState.FINALISING),
            (BuildState.FINALISING, BuildState.COMPLETE),
        ):
            persistence.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        with pytest.raises(StaleTransitionError):
            asyncio.run(sm.transition_to_cancelled(build_id=build_id, reason="late"))
        assert _status(persistence, build_id) == "COMPLETE"

    def test_transition_to_running_softens_on_concurrent_terminal(
        self, persistence: SqliteLifecyclePersistence, adapters
    ) -> None:
        repo, sm = adapters
        build_id = _seed_running(
            persistence, feature_id="FEAT-ST-003", correlation_id="c-st3"
        )
        request_id = derive_request_id(
            build_id=build_id, stage_label=STAGE_LABEL, attempt_count=0
        )
        asyncio.run(
            _pause_via_adapters(
                repo,
                sm,
                build_id=build_id,
                feature_id="FEAT-ST-003",
                decision=_decision(build_id),
                request_id=request_id,
            )
        )
        # A concurrent CLI-cancel terminalised the row before approve.
        asyncio.run(sm.transition_to_cancelled(build_id=build_id, reason="cli"))
        # transition_to_running must soften to a no-op, not raise.
        asyncio.run(sm.transition_to_running(build_id=build_id))
        assert _status(persistence, build_id) == "CANCELLED"
