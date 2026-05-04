"""Integration test for ``dispatch_build`` against the real persistence facade.

Regression lock for TASK-FORGE-FRR-F010B. The bug: the production
composer in ``forge.cli._serve_deps`` was handing the bare
:class:`SqliteLifecyclePersistence` to
:func:`build_forward_context_builder`, but the facade does not expose
the :class:`StageLogReader` Protocol surface
(``get_approved_stage_entry`` / ``get_all_approved_stage_entries``).
The first dispatch raised
``AttributeError: 'SqliteLifecyclePersistence' object has no attribute
'get_approved_stage_entry'`` from inside
``forward_context_builder.build_for(...)`` — the exact failure mode
observed on the GB10 in run 4 of the post-FIX-F010 jarvis runbook
rerun (correlation_id ``f876fd47-5e3c-4851-8f89-a7b7bcab8464``).

The fix: wrap ``sqlite_pool`` in a narrow
:class:`StageLogReader`-shaped adapter at the composition seam (see
:func:`forge.cli._serve_deps_forward_context.build_stage_log_reader`),
symmetric with the existing Wave-2 wrappers
(:func:`build_stage_log_recorder`,
:func:`build_autobuild_state_initialiser`).

This test drives ``deps.dispatch_build(payload, ack)`` end-to-end
against a real :class:`SqliteLifecyclePersistence` over a tmp-path
SQLite database (with ``apply_at_boot`` already run, mirroring the
F010.A fix), with only ``AsyncTaskStarter`` mocked at the boundary so
the in-process LangGraph middleware does not need a runtime. Before
this fix, the call would raise AttributeError before reaching the
``start_async_task`` call; with the fix in place, the dispatcher
proceeds through ``forward_context_builder.build_for(...)`` (returning
empty context for the empty stage_log), records the pre-dispatch
``stage_log`` row, and reaches the ``start_async_task`` boundary.

The outer ``pipeline_consumer.handle_message`` try/except is NOT
exercised here — we test the inner dispatch closure directly so a
regression surfaces as a raised exception rather than being silently
swallowed (which is exactly what hid the bug from the unit suite at
``tests/cli/test_serve_deps.py`` — those tests mock ``dispatch_autobuild_async``
itself, so the forward-context-builder call site never fired).

References:
    * TASK-FORGE-FRR-F010B — this regression lock.
    * TASK-FIX-F010 — the production composer wiring whose first
      end-to-end exercise surfaced the AttributeError.
    * TASK-FW10-011 — the full end-to-end integration test that AC-12
      of the post-merge follow-ups asks to be resurrected.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence


class _StubNatsClient:
    """Pre-opened NATS client double — never re-dials."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None


class _RecordingAsyncTaskStarter:
    """Records the single ``start_async_task`` call the dispatcher makes.

    Returns a deterministic ``task_id`` so the dispatcher's downstream
    state-channel write succeeds. The recorded payload is asserted
    on after dispatch to confirm the dispatcher reached this seam
    without raising on the upstream
    :meth:`StageLogReader.get_approved_stage_entry` call (which is
    where the AttributeError used to fire).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-regression-lock"


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Writer connection against a freshly-migrated tmp-path SQLite DB.

    ``apply_at_boot`` provides the ``builds`` / ``stage_log`` /
    ``schema_version`` tables that the dispatcher writes to. This
    mirrors the post-F010.A boot sequence — without it, the
    ``record_pending_build`` write would fail with ``no such table:
    builds`` before the AttributeError under test ever had a chance
    to fire.
    """
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    """Real :class:`SqliteLifecyclePersistence` over the migrated DB."""
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


@pytest.fixture()
def forge_config(tmp_path: Path) -> ForgeConfig:
    """Minimal :class:`ForgeConfig` whose allowlist includes ``tmp_path``."""
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[tmp_path]),
        ),
    )


@pytest.fixture()
def stub_client() -> _StubNatsClient:
    return _StubNatsClient()


@pytest.fixture()
def queued_payload() -> SimpleNamespace:
    """Minimal :class:`BuildQueuedPayload` shape the dispatcher consumes."""
    return SimpleNamespace(
        feature_id="FEAT-43DE",
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/regression.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="regression-test",
        correlation_id="f876fd47-5e3c-4851-8f89-a7b7bcab8464",
        parent_request_id=None,
        queued_at=datetime(2026, 5, 4, 19, 36, 35, tzinfo=UTC),
    )


class TestDispatchBuildAgainstRealPersistence:
    """``dispatch_build`` reaches ``start_async_task`` against a real pool.

    The regression lock for TASK-FORGE-FRR-F010B: before the fix, the
    dispatcher would raise
    ``AttributeError: 'SqliteLifecyclePersistence' object has no
    attribute 'get_approved_stage_entry'`` from inside
    :meth:`ForwardContextBuilder.build_for` — *after* the QUEUED row
    had been persisted, leaving partial state in the database with no
    outbound lifecycle envelope. With the fix, the same call path
    completes cleanly through the empty-stage_log branch.
    """

    @pytest.mark.asyncio
    async def test_empty_stage_log_does_not_raise(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
        queued_payload: SimpleNamespace,
    ) -> None:
        """The dispatcher reaches ``start_async_task`` on an empty stage_log.

        This is the canonical run-4 case: a fresh build whose
        ``stage_log`` is empty. The forward-context-builder must
        return an empty context list without raising, the dispatcher
        must record its pre-dispatch ``stage_log`` row, and
        ``start_async_task`` must be called exactly once.
        """
        starter = _RecordingAsyncTaskStarter()

        deps = build_pipeline_consumer_deps(
            stub_client,
            forge_config,
            persistence,
            async_task_starter=starter,
        )

        async def _noop_ack() -> None:
            pass

        # Before the fix, this call raised AttributeError inside
        # forward_context_builder.build_for -> StageLogReader.get_approved_stage_entry.
        await deps.dispatch_build(queued_payload, _noop_ack)

        # The dispatcher reached start_async_task — i.e. it threaded
        # past the forward-context-builder call without raising.
        assert len(starter.calls) == 1, (
            "dispatch_build did not reach start_async_task — the "
            "forward-context-builder seam may still be raising "
            "AttributeError. Run-4 regression has resurfaced."
        )

        subagent_name, context = starter.calls[0]
        assert context["build_id"].startswith("build-FEAT-43DE-")
        assert context["feature_id"] == "FEAT-43DE"
        assert context["correlation_id"] == queued_payload.correlation_id

    @pytest.mark.asyncio
    async def test_queued_row_is_persisted_before_dispatch(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
        queued_payload: SimpleNamespace,
    ) -> None:
        """The QUEUED ``builds`` row is persisted (covers the run-4 partial-state shape).

        The empirical evidence for the bug was a QUEUED row in
        ``builds`` with no ``stage_log`` follow-on (the AttributeError
        fired between the two writes). This test asserts the QUEUED
        row IS written and the ``stage_log`` pre-dispatch row IS
        written — the two-step shape the dispatcher promises, with
        the fix landed.
        """
        starter = _RecordingAsyncTaskStarter()

        deps = build_pipeline_consumer_deps(
            stub_client,
            forge_config,
            persistence,
            async_task_starter=starter,
        )

        async def _noop_ack() -> None:
            pass

        await deps.dispatch_build(queued_payload, _noop_ack)

        # The builds row exists.
        with persistence._reader() as cx:  # noqa: SLF001
            cx.row_factory = sqlite3.Row
            builds_row = cx.execute(
                "SELECT build_id, status, feature_id, correlation_id "
                "FROM builds WHERE feature_id = ? AND correlation_id = ?",
                (queued_payload.feature_id, queued_payload.correlation_id),
            ).fetchone()
            stage_rows = cx.execute(
                "SELECT stage_label, status, details_json "
                "FROM stage_log WHERE build_id = ?",
                (builds_row["build_id"],),
            ).fetchall()

        assert builds_row is not None, (
            "dispatch_build did not persist the QUEUED row — the "
            "regression may be earlier than the AttributeError site."
        )
        assert builds_row["status"] == "QUEUED"

        # The pre-dispatch stage_log row exists — proof the dispatcher
        # threaded past the forward-context-builder call site and
        # reached ``stage_log_recorder.record_running``. The dispatcher
        # writes the recorder twice (pre- and post-``start_async_task``,
        # see ``autobuild_async.py:443`` and ``:497``); we assert at
        # least one row was written, which is the bug-fix shape — the
        # regression had ZERO stage_log rows because the call raised
        # AttributeError before the recorder was ever invoked.
        assert len(stage_rows) >= 1, (
            "dispatch_build did not write any stage_log rows — the "
            "forward-context-builder call may still be raising before "
            "the recorder is invoked."
        )
        assert all(row["stage_label"] == "autobuild" for row in stage_rows)
