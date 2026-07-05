"""Tests for ``forge.lifecycle.recovery`` (TASK-PSM-007).

Acceptance-criteria coverage map (one Group D scenario per AC):

* AC-001: full per-state matrix —
  :class:`TestPerStateRecoveryMatrix`.
* AC-002: PAUSED handling reads ``pending_approval_request_id`` and
  passes it verbatim to ``approval_publisher`` —
  :class:`TestPausedRequestIdVerbatim`.
* AC-003: PAUSED unit test with ``request_id="abc-123"`` —
  :class:`TestPausedRequestIdVerbatim.test_paused_recovery_preserves_request_id`.
* AC-004: PREPARING marks INTERRUPTED via ``state_machine.transition``,
  never raw status writes — :class:`TestStateMachineRouting` plus the
  static-grep check :class:`TestNoRawStatusWrites`.
* AC-005: RUNNING marks INTERRUPTED — :class:`TestPerStateRecoveryMatrix`.
* AC-006: FINALISING marks INTERRUPTED with PR-warning —
  :class:`TestFinalisingWarning`.
* AC-007: idempotent — :class:`TestIdempotency`.
* AC-008: per-handler failure isolation — :class:`TestFailureIsolation`.
* AC-009: ``RecoveryReport`` shape — :class:`TestRecoveryReport`.
* AC-010: every Group D scenario covered (5 scenarios) —
  :class:`TestPerStateRecoveryMatrix` + :class:`TestPausedRequestIdVerbatim`
  + :class:`TestFinalisingWarning` + :class:`TestPerStateRecoveryMatrix.test_terminal_no_op`.

Tests run against a real in-memory SQLite database — no mocking of the
storage layer. Publishers are duck-typed mocks that record their inputs
for assertion.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.recovery import (
    RecoveryReport,
    reconcile_on_boot,
)
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    feature_id: str = "FEAT-TEST-001",
    correlation_id: str = "corr-001",
    queued_at: datetime | None = None,
) -> SimpleNamespace:
    """Construct a duck-typed BuildQueuedPayload for record_pending_build."""
    if queued_at is None:
        queued_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
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
        queued_at=queued_at,
        requested_at=queued_at,
    )


@pytest.fixture()
def writer_db(tmp_path: Path) -> sqlite3.Connection:
    """Return a writer connection against a freshly-migrated db file."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    """Return a persistence facade bound to the migrated writer connection."""
    return SqliteLifecyclePersistence(connection=writer_db)


class _RecordingPipelinePublisher:
    """Duck-typed :class:`PipelineFailurePublisher` capturing calls."""

    def __init__(self) -> None:
        self.published_failed: list[Any] = []

    async def publish_build_failed(self, payload: Any) -> None:
        self.published_failed.append(payload)


class _RecordingApprovalPublisher:
    """Duck-typed :class:`ApprovalRepublisher` capturing envelopes."""

    def __init__(self) -> None:
        self.published_envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.published_envelopes.append(envelope)

    def last_published(self) -> Any:
        """Return the most recently published envelope (or ``None``)."""
        if not self.published_envelopes:
            return None
        return self.published_envelopes[-1]


class _FailingPipelinePublisher:
    """Pipeline publisher that always raises — exercises failure isolation."""

    async def publish_build_failed(self, payload: Any) -> None:
        raise RuntimeError("transport down")


def _seed_build_in_state(
    persistence: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
    target_state: BuildState,
    request_id: str | None = None,
    pr_url: str | None = None,
) -> str:
    """Seed a build and drive it to ``target_state`` via legitimate transitions.

    Mirrors the production state graph so the recovery pass sees realistic
    rows (e.g. ``PAUSED`` rows always have a non-null
    ``pending_approval_request_id`` because they were written by
    ``mark_paused``).
    """
    payload = _make_payload(feature_id=feature_id, correlation_id=correlation_id)
    build_id = persistence.record_pending_build(payload)

    if target_state is BuildState.QUEUED:
        return build_id

    # All other targets need at least PREPARING.
    persistence.apply_transition(
        compose_transition(
            Build(build_id=build_id, status=BuildState.QUEUED),
            BuildState.PREPARING,
        )
    )
    if target_state is BuildState.PREPARING:
        return build_id

    # Drive to RUNNING.
    persistence.apply_transition(
        compose_transition(
            Build(build_id=build_id, status=BuildState.PREPARING),
            BuildState.RUNNING,
        )
    )
    if target_state is BuildState.RUNNING:
        return build_id

    if target_state is BuildState.PAUSED:
        assert request_id is not None, "PAUSED seed requires a request_id"
        persistence.mark_paused(build_id, request_id)
        return build_id

    if target_state is BuildState.FINALISING:
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.RUNNING),
                BuildState.FINALISING,
            )
        )
        if pr_url is not None:
            persistence.connection.execute(
                "UPDATE builds SET pr_url = ? WHERE build_id = ?",
                (pr_url, build_id),
            )
            persistence.connection.commit()
        return build_id

    if target_state is BuildState.INTERRUPTED:
        persistence.apply_transition(
            compose_transition(
                Build(build_id=build_id, status=BuildState.RUNNING),
                BuildState.INTERRUPTED,
            )
        )
        return build_id

    raise ValueError(f"unsupported seed target state: {target_state}")


def _read_status(persistence: SqliteLifecyclePersistence, build_id: str) -> str:
    row = persistence.connection.execute(
        "SELECT status FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return row["status"]


def _read_error(persistence: SqliteLifecyclePersistence, build_id: str) -> str | None:
    row = persistence.connection.execute(
        "SELECT error FROM builds WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return row["error"]


# ---------------------------------------------------------------------------
# AC-001 + AC-005 + AC-010: full per-state recovery matrix
# ---------------------------------------------------------------------------


class TestPerStateRecoveryMatrix:
    """AC-001 / AC-005 / AC-010 — exercise every row of API-sqlite-schema.md §6."""

    def test_queued_is_no_op(self, persistence: SqliteLifecyclePersistence) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-Q-001",
            correlation_id="corr-q",
            target_state=BuildState.QUEUED,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        assert _read_status(persistence, build_id) == "QUEUED"
        assert publisher.published_failed == []
        assert approval.published_envelopes == []
        assert report.skipped_count == 1
        assert report.interrupted_count == 0

    def test_preparing_marks_interrupted_and_publishes_failed(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-P-001",
            correlation_id="corr-p",
            target_state=BuildState.PREPARING,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        assert _read_status(persistence, build_id) == "INTERRUPTED"
        assert len(publisher.published_failed) == 1
        emitted = publisher.published_failed[0]
        assert emitted.build_id == build_id
        assert emitted.recoverable is True
        assert "recoverable" in emitted.failure_reason
        assert report.interrupted_count == 1

    def test_running_marks_interrupted(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-R-001",
            correlation_id="corr-r",
            target_state=BuildState.RUNNING,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        assert _read_status(persistence, build_id) == "INTERRUPTED"
        # RUNNING does not emit build-failed — re-pickup is via NACK.
        assert publisher.published_failed == []
        assert report.interrupted_count == 1

    def test_terminal_no_op(self, persistence: SqliteLifecyclePersistence) -> None:
        # Seed a COMPLETE build so the terminal-filter is exercised.
        payload = _make_payload(feature_id="FEAT-T-001", correlation_id="corr-t")
        build_id = persistence.record_pending_build(payload)
        # QUEUED -> PREPARING -> RUNNING -> FINALISING -> COMPLETE
        for from_state, to_state in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
            (BuildState.RUNNING, BuildState.FINALISING),
            (BuildState.FINALISING, BuildState.COMPLETE),
        ):
            persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=from_state),
                    to_state,
                    pr_url=(
                        "https://github.com/x/y/pull/1"
                        if to_state is BuildState.COMPLETE
                        else None
                    ),
                )
            )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # Terminal builds are filtered out before the per-state matrix runs.
        assert _read_status(persistence, build_id) == "COMPLETE"
        assert publisher.published_failed == []
        assert approval.published_envelopes == []
        assert report.interrupted_count == 0
        assert report.paused_reissued_count == 0
        assert report.skipped_count == 0  # filtered, never visited

    def test_interrupted_is_no_op(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-I-001",
            correlation_id="corr-i",
            target_state=BuildState.INTERRUPTED,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        assert _read_status(persistence, build_id) == "INTERRUPTED"
        assert publisher.published_failed == []
        assert approval.published_envelopes == []
        assert report.skipped_count == 1


# ---------------------------------------------------------------------------
# AC-002 + AC-003: PAUSED recovery preserves the original request_id verbatim
# ---------------------------------------------------------------------------


class TestPausedRequestIdVerbatim:
    """sc_004 — the highest-stakes invariant in the feature."""

    def test_paused_recovery_preserves_request_id(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # AC-003 fixture: build paused with request_id="abc-123-original-uuid".
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-PA-001",
            correlation_id="corr-pa",
            target_state=BuildState.PAUSED,
            request_id="abc-123-original-uuid",
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # State stays PAUSED — re-publish is a wire action, not a transition.
        assert _read_status(persistence, build_id) == "PAUSED"
        assert report.paused_reissued_count == 1

        # The crucial assertion: the published envelope carries the
        # original request_id verbatim — NOT a fresh UUID.
        envelope = approval.last_published()
        assert envelope is not None
        # Pull request_id off the dumped payload (envelope.payload is a
        # dict produced by ApprovalRequestPayload.model_dump).
        assert envelope.payload["request_id"] == "abc-123-original-uuid"
        # Also assert the details dict carries the build/feature ids so
        # the publisher's subject resolver works downstream.
        assert envelope.payload["details"]["build_id"] == build_id
        assert envelope.payload["details"]["feature_id"] == "FEAT-PA-001"
        # TASK-GATE-D659 correlation landmine: the recovery/rearm envelope
        # now stamps BuildRow.correlation_id verbatim so the subscriber's
        # step-2b guard sees a matching value on the boot re-emit (pre-fix
        # this was None → the live responder echo would look mismatched).
        assert envelope.correlation_id == "corr-pa"

    def test_paused_without_request_id_records_failure(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # Manually construct a corrupt PAUSED row (no request_id) to
        # exercise the schema-invariant defensive branch. mark_paused
        # would refuse to create this row — we do it directly to
        # simulate the corrupt-database recovery path.
        payload = _make_payload(feature_id="FEAT-CORR-001", correlation_id="corr-c")
        build_id = persistence.record_pending_build(payload)
        for from_state, to_state in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ):
            persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=from_state),
                    to_state,
                )
            )
        # Force PAUSED with NULL pending_approval_request_id (corrupt row).
        persistence.connection.execute(
            "UPDATE builds SET status = 'PAUSED', "
            "pending_approval_request_id = NULL WHERE build_id = ?",
            (build_id,),
        )
        persistence.connection.commit()

        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # The handler raises — the failure is recorded, not propagated.
        assert report.paused_reissued_count == 0
        assert len(report.failures) == 1
        assert report.failures[0][0] == build_id
        assert "pending_approval_request_id" in str(report.failures[0][1])


# ---------------------------------------------------------------------------
# AC-004: state_machine.transition is the sole status writer
# ---------------------------------------------------------------------------


class TestStateMachineRouting:
    """All recovery transitions flow through state_machine.transition."""

    def test_preparing_transition_goes_through_state_machine(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-SM-001",
            correlation_id="corr-sm",
            target_state=BuildState.PREPARING,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # If the recovery pass had bypassed state_machine.transition the
        # error column would be empty; transition() composes Transition
        # with the error kwarg, apply_transition writes it.
        assert _read_status(persistence, build_id) == "INTERRUPTED"
        err = _read_error(persistence, build_id)
        assert err is not None and "recoverable" in err


class TestNoRawStatusWrites:
    """Static-grep check — recovery.py must NOT issue raw status updates.

    Mirrors the seam-test contract (STATE_TRANSITION_API): the only
    permitted writer of ``UPDATE builds SET status`` is
    ``persistence.apply_transition`` (TASK-PSM-005).
    """

    def test_recovery_module_has_no_raw_status_update(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        recovery_path = repo_root / "src" / "forge" / "lifecycle" / "recovery.py"
        text = recovery_path.read_text(encoding="utf-8")
        pattern = re.compile(r"UPDATE\s+builds\s+SET\s+status", re.IGNORECASE)
        assert pattern.search(text) is None, (
            "recovery.py must NOT issue raw `UPDATE builds SET status` "
            "SQL — every transition routes through state_machine.transition"
        )


# ---------------------------------------------------------------------------
# AC-006: FINALISING handling records the PR-warning
# ---------------------------------------------------------------------------


class TestFinalisingWarning:
    """FINALISING marks INTERRUPTED with the documented warning shape."""

    def test_finalising_with_pr_url_records_warning(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-F-001",
            correlation_id="corr-f",
            target_state=BuildState.FINALISING,
            pr_url="https://github.com/example/repo/pull/42",
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        assert _read_status(persistence, build_id) == "INTERRUPTED"
        err = _read_error(persistence, build_id)
        assert err == (
            "finalising-interrupted: PR may exist at "
            "https://github.com/example/repo/pull/42"
        )
        assert report.interrupted_count == 1
        assert len(report.finalising_warnings) == 1
        warning = report.finalising_warnings[0]
        assert warning.startswith(f"{build_id}: ")
        assert "https://github.com/example/repo/pull/42" in warning

    def test_finalising_without_pr_url_records_unknown_warning(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-F-002",
            correlation_id="corr-f2",
            target_state=BuildState.FINALISING,
            pr_url=None,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        err = _read_error(persistence, build_id)
        assert err == "finalising-interrupted: PR creation status unknown"
        assert any(
            "PR creation status unknown" in w for w in report.finalising_warnings
        )


# ---------------------------------------------------------------------------
# AC-007: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Running reconcile_on_boot twice produces no extra state changes."""

    def test_idempotent_across_full_matrix(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # Seed one build per non-terminal state.
        seeded = {
            BuildState.QUEUED: _seed_build_in_state(
                persistence,
                feature_id="FEAT-Q-002",
                correlation_id="c-q",
                target_state=BuildState.QUEUED,
            ),
            BuildState.PREPARING: _seed_build_in_state(
                persistence,
                feature_id="FEAT-P-002",
                correlation_id="c-p",
                target_state=BuildState.PREPARING,
            ),
            BuildState.RUNNING: _seed_build_in_state(
                persistence,
                feature_id="FEAT-R-002",
                correlation_id="c-r",
                target_state=BuildState.RUNNING,
            ),
            BuildState.PAUSED: _seed_build_in_state(
                persistence,
                feature_id="FEAT-PA-002",
                correlation_id="c-pa",
                target_state=BuildState.PAUSED,
                request_id="req-id-keep",
            ),
            BuildState.FINALISING: _seed_build_in_state(
                persistence,
                feature_id="FEAT-F-003",
                correlation_id="c-f",
                target_state=BuildState.FINALISING,
                pr_url="https://github.com/x/y/pull/9",
            ),
        }
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # Snapshot the post-first-run statuses.
        first_run_statuses = {
            seed: _read_status(persistence, build_id)
            for seed, build_id in seeded.items()
        }

        # Second run.
        report2 = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # Statuses are unchanged (the AC's "no additional state changes").
        for seed, build_id in seeded.items():
            assert _read_status(persistence, build_id) == first_run_statuses[seed]

        # Second-run interrupted_count is 0 — every PREPARING/RUNNING/
        # FINALISING from the first run is now INTERRUPTED, which is a
        # no-op handler.
        assert report2.interrupted_count == 0
        # PAUSED still re-publishes on the second run (wire-level action,
        # not a state change). The responder dedupes by request_id.
        assert report2.paused_reissued_count == 1


# ---------------------------------------------------------------------------
# AC-008: per-handler failure isolation
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """A failure in one handler must not block reconciliation of the rest."""

    def test_one_failing_handler_does_not_block_others(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # Seed both PREPARING (which calls the failing publisher) and
        # RUNNING (which does not). The RUNNING build must still be
        # reconciled even though PREPARING raises.
        preparing_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-FAIL-P",
            correlation_id="cf-p",
            target_state=BuildState.PREPARING,
        )
        running_id = _seed_build_in_state(
            persistence,
            feature_id="FEAT-FAIL-R",
            correlation_id="cf-r",
            target_state=BuildState.RUNNING,
        )
        publisher = _FailingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # PREPARING handler raised on the publish step — but the SQL
        # transition committed first, so the row is INTERRUPTED.
        assert _read_status(persistence, preparing_id) == "INTERRUPTED"
        # RUNNING handler succeeded.
        assert _read_status(persistence, running_id) == "INTERRUPTED"

        # Failure recorded but pass continued.
        assert any(
            bid == preparing_id for bid, _ in report.failures
        ), "PREPARING failure should be recorded on report.failures"
        # interrupted_count counts BOTH the PREPARING (transition committed
        # before the publish failure) and the RUNNING successful handler.
        assert report.interrupted_count == 2


# ---------------------------------------------------------------------------
# AC-009: RecoveryReport shape
# ---------------------------------------------------------------------------


class TestRecoveryReport:
    """RecoveryReport carries the documented counts + warnings + failures."""

    def test_default_report_is_empty(self) -> None:
        report = RecoveryReport()
        assert report.interrupted_count == 0
        assert report.paused_reissued_count == 0
        assert report.skipped_count == 0
        assert report.finalising_warnings == []
        assert report.failures == []

    def test_report_records_all_categories(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        # One of each interesting category.
        _seed_build_in_state(
            persistence,
            feature_id="FEAT-Q-003",
            correlation_id="c-q3",
            target_state=BuildState.QUEUED,
        )
        _seed_build_in_state(
            persistence,
            feature_id="FEAT-R-003",
            correlation_id="c-r3",
            target_state=BuildState.RUNNING,
        )
        _seed_build_in_state(
            persistence,
            feature_id="FEAT-PA-003",
            correlation_id="c-pa3",
            target_state=BuildState.PAUSED,
            request_id="req-final",
        )
        _seed_build_in_state(
            persistence,
            feature_id="FEAT-F-004",
            correlation_id="c-f4",
            target_state=BuildState.FINALISING,
            pr_url="https://github.com/x/y/pull/3",
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(reconcile_on_boot(persistence, publisher, approval))

        # RUNNING + FINALISING become INTERRUPTED → 2.
        assert report.interrupted_count == 2
        # PAUSED reissued once.
        assert report.paused_reissued_count == 1
        # QUEUED counted as skipped.
        assert report.skipped_count == 1
        # FINALISING warning recorded.
        assert len(report.finalising_warnings) == 1
        # No failures.
        assert report.failures == []
