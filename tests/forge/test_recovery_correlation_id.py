"""TASK-FORGE-FRR-F010D — recovery surface threads ``correlation_id``.

DDR-029's notification-thread contract requires every outbound
``pipeline.build-failed.*`` envelope to carry the **same**
``correlation_id`` as the build it relates to, so jarvis's
``forge_subscriber`` can route the notification back to the originating
chat session.

TASK-FORGE-FRR-F010C closed the gap on the consumer-side rejection
paths in :mod:`forge.adapters.nats.pipeline_consumer`. This module is
the **symmetrical** closure for the boot-recovery surface in
:mod:`forge.lifecycle.recovery`: PREPARING-recovery's
``publisher.publish_build_failed(...)`` MUST thread
``BuildRow.correlation_id`` onto the v1 ``BuildFailedPayload`` via
:func:`forge.pipeline.attach_correlation_id` before publishing, so the
publisher's central ``_publish_envelope`` lookup
(``getattr(payload, "correlation_id", None)``) sees a non-null value and
the outbound envelope carries the originating correlation context.

Test classes map 1:1 to the task's acceptance criteria:

* ``TestPreparingRecoveryThreadsCorrelationId`` — AC-3: the happy-path
  threading assertion. Drives a real PREPARING build through
  :func:`reconcile_on_boot` and inspects the payload that the duck-typed
  publisher captured. Pre-fix this assertion read ``None``; post-fix it
  reads the seeded ``BuildRow.correlation_id``.
* ``TestRecoveryPublishSitesThreadCorrelationId`` — AC-4: AST-based lint
  guard over :mod:`forge.lifecycle.recovery`. Walks every
  ``publisher.publish_build_failed(...)`` call site and asserts each one
  is preceded (in the same ``async def`` body) by an
  ``attach_correlation_id`` call. A future PR that adds a new
  recovery-emit branch without threading the field MUST fail this test.

The audit conclusion (AC-1) is recorded in the task's completion notes:
``_handle_preparing`` is the only outbound publish site in
``recovery.py``; ``_handle_running`` and ``_handle_finalising`` issue
no wire publish, and ``_handle_paused`` re-issues an
``ApprovalRequestPayload`` whose envelope is constructed in a different
module (:mod:`forge.adapters.nats.approval_publisher`) — out of scope
per F010D's "don't widen the fix beyond the audit" boundary.
"""

from __future__ import annotations

import ast
import asyncio
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
from forge.lifecycle.recovery import reconcile_on_boot
from forge.lifecycle.state_machine import (
    BuildState,
    transition as compose_transition,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/forge/test_lifecycle_recovery.py shape)
# ---------------------------------------------------------------------------


SEEDED_CORRELATION_ID = "f010d-corr-3a7e1d2c-recovery-thread"
"""A distinctive correlation_id so a regression-bug ``None`` is unmistakeable."""


def _make_payload(
    *,
    feature_id: str = "FEAT-F010D-001",
    correlation_id: str = SEEDED_CORRELATION_ID,
) -> SimpleNamespace:
    """Construct a duck-typed BuildQueuedPayload for record_pending_build."""
    queued_at = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
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
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(writer_db: sqlite3.Connection) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db)


class _RecordingPipelinePublisher:
    """Duck-typed :class:`PipelineFailurePublisher` capturing payloads."""

    def __init__(self) -> None:
        self.published_failed: list[Any] = []

    async def publish_build_failed(self, payload: Any) -> None:
        self.published_failed.append(payload)


class _RecordingApprovalPublisher:
    """Duck-typed :class:`ApprovalRepublisher` (unused by the PREPARING path)."""

    def __init__(self) -> None:
        self.published_envelopes: list[Any] = []

    async def publish_request(self, envelope: Any) -> None:
        self.published_envelopes.append(envelope)


def _seed_preparing(
    persistence: SqliteLifecyclePersistence,
    *,
    feature_id: str,
    correlation_id: str,
) -> str:
    """Seed a build and drive it to PREPARING via legitimate transitions."""
    payload = _make_payload(feature_id=feature_id, correlation_id=correlation_id)
    build_id = persistence.record_pending_build(payload)
    persistence.apply_transition(
        compose_transition(
            Build(build_id=build_id, status=BuildState.QUEUED),
            BuildState.PREPARING,
        )
    )
    return build_id


# ---------------------------------------------------------------------------
# AC-3: PREPARING-recovery emit threads BuildRow.correlation_id
# ---------------------------------------------------------------------------


class TestPreparingRecoveryThreadsCorrelationId:
    """AC-3 — the happy-path regression for the F010D fix.

    The publisher's central ``_publish_envelope`` reads ``correlation_id``
    off the payload via ``getattr(payload, "correlation_id", None)``. The
    F010D fix attaches the seeded ``BuildRow.correlation_id`` to the v1
    ``BuildFailedPayload`` via :func:`attach_correlation_id` before the
    publish call, so the assertion below reads the seeded value rather
    than ``None``. Pre-fix: ``getattr(...) is None`` → outbound envelope
    carries ``correlation_id: null`` → jarvis cannot route the recovery
    notification back to the originating chat session.
    """

    def test_preparing_recovery_payload_carries_buildrow_correlation_id(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        build_id = _seed_preparing(
            persistence,
            feature_id="FEAT-F010D-PREP",
            correlation_id=SEEDED_CORRELATION_ID,
        )
        publisher = _RecordingPipelinePublisher()
        approval = _RecordingApprovalPublisher()

        report = asyncio.run(
            reconcile_on_boot(persistence, publisher, approval)
        )

        # Sanity: the recovery pass actually reached the PREPARING handler.
        assert report.interrupted_count == 1
        assert len(publisher.published_failed) == 1

        emitted = publisher.published_failed[0]
        assert emitted.build_id == build_id
        assert emitted.recoverable is True

        # The actual DDR-029 invariant: the publisher reads correlation_id
        # off the payload via ``getattr(payload, "correlation_id", None)``.
        # The F010D fix attaches it via attach_correlation_id; pre-fix this
        # returned None.
        assert getattr(emitted, "correlation_id", None) == SEEDED_CORRELATION_ID, (
            "PREPARING-recovery build-failed payload missing correlation_id "
            "(DDR-029) — F010D regression. The publisher's _publish_envelope "
            "reads ``getattr(payload, 'correlation_id', None)`` and would "
            "write ``correlation_id: null`` onto the outbound envelope, "
            "leaving the recovery notification unrouteable by jarvis."
        )


# ---------------------------------------------------------------------------
# AC-4: cross-cut lint guard — every recovery publish site threads correlation_id
# ---------------------------------------------------------------------------


class TestRecoveryPublishSitesThreadCorrelationId:
    """AC-4 — the recovery contract MUST NOT be lost again.

    AST-walk over ``forge/lifecycle/recovery.py``: every
    ``publisher.publish_build_failed(...)`` call site must be preceded
    (in the same enclosing function body) by an ``attach_correlation_id``
    call. A future PR that adds a new recovery-emit branch without
    threading the field MUST fail this test, even before any new
    behaviour test is written.

    Mirrors the shape of
    :class:`tests.forge.test_pipeline_consumer_correlation_id.TestAllRejectionPathsThreadCorrelationId.test_every_safe_publish_failure_call_passes_correlation_id_kwarg`.
    """

    @staticmethod
    def _publish_call_attr_name(call: ast.Call) -> str | None:
        """Return ``"publish_build_failed"`` for ``X.publish_build_failed(...)``."""
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    @staticmethod
    def _enclosing_function(
        tree: ast.Module, target: ast.Call
    ) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
        """Find the (Async)FunctionDef whose body transitively contains ``target``."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                for descendant in ast.walk(node):
                    if descendant is target:
                        return node
        return None

    @staticmethod
    def _function_calls_attach(
        func: ast.AsyncFunctionDef | ast.FunctionDef,
    ) -> bool:
        """True if ``func``'s body contains a call to ``attach_correlation_id``."""
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == "attach_correlation_id":
                return True
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr == "attach_correlation_id"
            ):
                return True
        return False

    def test_every_publish_build_failed_call_in_recovery_threads_correlation_id(
        self,
    ) -> None:
        source_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "forge"
            / "lifecycle"
            / "recovery.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        publish_calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if self._publish_call_attr_name(node) == "publish_build_failed":
                publish_calls.append(node)

        # Sanity: confirm the AST walk found the existing call site
        # (otherwise the test is silently a no-op).
        assert publish_calls, (
            "expected ≥1 publisher.publish_build_failed(...) call site in "
            "forge/lifecycle/recovery.py, found 0 — module restructured? "
            "Update this guard if the recovery emit surface legitimately "
            "moved out of recovery.py."
        )

        offenders: list[tuple[int, str]] = []
        for call in publish_calls:
            enclosing = self._enclosing_function(tree, call)
            if enclosing is None or not self._function_calls_attach(enclosing):
                offenders.append((call.lineno, ast.unparse(call)))

        assert not offenders, (
            "publisher.publish_build_failed(...) call site(s) in "
            "forge/lifecycle/recovery.py not preceded by an "
            "attach_correlation_id(...) call in the same function body "
            "(DDR-029 — every outbound recovery build-failed envelope MUST "
            "carry the BuildRow.correlation_id of the recovered row). If "
            "this is a new recovery branch, thread build.correlation_id via "
            "attach_correlation_id before the publish call (mirror the "
            "F010D pattern in _handle_preparing).\n\nOffending call sites:\n"
            + "\n".join(f"  line {lineno}: {snippet}" for lineno, snippet in offenders)
        )
