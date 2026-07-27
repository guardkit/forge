"""Tests for ``forge.cli._serve_deps`` (TASK-FW10-007).

Acceptance-criteria coverage map:

* AC: ``build_pipeline_consumer_deps(client, forge_config, sqlite_pool)``
  returns a :class:`PipelineConsumerDeps` with all four fields wired
  (``forge_config``, ``is_duplicate_terminal``, ``dispatch_build``,
  ``publish_build_failed``) — :class:`TestFactoryWiresAllFourFields`.
* AC: ``is_duplicate_terminal`` returns ``True`` for a known terminal
  ``(feature_id, correlation_id)`` and ``False`` for a novel pair —
  :class:`TestIsDuplicateTerminalAgainstSqlite`.
* AC: ``dispatch_build`` calls
  :func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`
  with the four collaborators (TASK-FW10-003/004/005 + the FW10-006
  emitter) — :class:`TestDispatchBuildWiresCollaborators`.
* AC: ``publish_build_failed`` delegates to
  :meth:`PipelinePublisher.publish_build_failed` —
  :class:`TestPublishBuildFailedDelegates`.
* AC: factory accepts the shared NATS client and never opens a second
  connection — :class:`TestSingleSharedClientInvariant`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps
from forge.cli._serve_deps import (
    build_pipeline_consumer_deps,
    is_terminal_status,
)
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubNatsClient:
    """Minimal pre-opened NATS client double — never re-dials."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        self.published.append((subject, body))
        return None


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Return a writer connection against a freshly-migrated db file."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection,
) -> SqliteLifecyclePersistence:
    """Return the persistence facade bound to a real writer connection."""
    return SqliteLifecyclePersistence(connection=writer_db)


@pytest.fixture()
def forge_config(tmp_path: Path) -> ForgeConfig:
    """Return a minimal :class:`ForgeConfig` with one allowlist entry."""
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[tmp_path]),
        ),
    )


@pytest.fixture()
def stub_client() -> _StubNatsClient:
    return _StubNatsClient()


# ---------------------------------------------------------------------------
# AC: factory wires all four PipelineConsumerDeps fields
# ---------------------------------------------------------------------------


class TestFactoryWiresAllFourFields:
    """``build_pipeline_consumer_deps`` returns a fully-wired deps object."""

    def test_factory_returns_pipeline_consumer_deps(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        assert isinstance(deps, PipelineConsumerDeps)

    def test_factory_threads_forge_config_through(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        assert deps.forge_config is forge_config, (
            "forge_config must be threaded through unchanged so the consumer "
            "reads the same approved_originators / allowlist as configured"
        )

    def test_factory_wires_callable_for_each_protocol_field(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        assert callable(deps.is_duplicate_terminal), (
            "is_duplicate_terminal must be a callable (IsDuplicateTerminal alias)"
        )
        assert callable(deps.dispatch_build), (
            "dispatch_build must be a callable (DispatchBuild alias)"
        )
        assert callable(deps.publish_build_failed), (
            "publish_build_failed must be a callable (PublishBuildFailed alias)"
        )

    def test_factory_rejects_none_client(
        self,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        with pytest.raises(ValueError, match="client"):
            build_pipeline_consumer_deps(None, forge_config, persistence)


# ---------------------------------------------------------------------------
# AC: is_duplicate_terminal correctness against a real SQLite pool
# ---------------------------------------------------------------------------


class TestIsDuplicateTerminalAgainstSqlite:
    """``is_duplicate_terminal`` reads the unique index per ASSUM-014."""

    @pytest.mark.asyncio
    async def test_returns_false_for_novel_pair(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        result = await deps.is_duplicate_terminal(
            "FEAT-NOVEL", "correlation-novel"
        )

        assert result is False, (
            "novel (feature_id, correlation_id) must report non-duplicate"
        )

    @pytest.mark.asyncio
    async def test_returns_true_for_terminal_pair(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        # Insert a terminal builds row directly so we can assert the
        # closure picks up the persisted status.
        cx = persistence.connection
        cx.execute(
            """
            INSERT INTO builds (
                build_id, feature_id, repo, branch, feature_yaml_path,
                status, triggered_by, originating_adapter,
                originating_user, correlation_id, parent_request_id,
                queued_at, max_turns, sdk_timeout_seconds, mode
            ) VALUES (
                'build-T1', 'FEAT-T', 'r', 'main', 'features/t.yaml',
                'COMPLETE', 'cli', 'cli', 'u', 'corr-T', NULL,
                ?, 5, 1800, 'mode-a'
            )
            """,
            (datetime(2026, 5, 2, tzinfo=UTC).isoformat(),),
        )

        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        result = await deps.is_duplicate_terminal("FEAT-T", "corr-T")

        assert result is True, (
            "a (feature_id, correlation_id) row in a terminal state must be "
            "reported as duplicate-terminal"
        )

    @pytest.mark.asyncio
    async def test_returns_false_for_in_flight_row(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        cx = persistence.connection
        cx.execute(
            """
            INSERT INTO builds (
                build_id, feature_id, repo, branch, feature_yaml_path,
                status, triggered_by, originating_adapter,
                originating_user, correlation_id, parent_request_id,
                queued_at, max_turns, sdk_timeout_seconds, mode
            ) VALUES (
                'build-R1', 'FEAT-R', 'r', 'main', 'features/r.yaml',
                'RUNNING', 'cli', 'cli', 'u', 'corr-R', NULL,
                ?, 5, 1800, 'mode-a'
            )
            """,
            (datetime(2026, 5, 2, tzinfo=UTC).isoformat(),),
        )

        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        result = await deps.is_duplicate_terminal("FEAT-R", "corr-R")

        assert result is False, (
            "an in-flight RUNNING row must NOT be reported as duplicate-terminal "
            "(reconciliation, not idempotency, owns redelivered in-flight builds)"
        )

    @pytest.mark.parametrize(
        "status,expected",
        [
            (BuildState.COMPLETE.value, True),
            (BuildState.FAILED.value, True),
            (BuildState.CANCELLED.value, True),
            (BuildState.SKIPPED.value, True),
            (BuildState.QUEUED.value, False),
            (BuildState.RUNNING.value, False),
            (BuildState.PAUSED.value, False),
            (None, False),
        ],
    )
    def test_is_terminal_status_membership(
        self, status: str | None, expected: bool
    ) -> None:
        assert is_terminal_status(status) is expected


# ---------------------------------------------------------------------------
# AC: dispatch_build wires the four Wave-2 collaborators
# ---------------------------------------------------------------------------


class TestDispatchBuildWiresCollaborators:
    """``dispatch_build`` calls ``dispatch_autobuild_async`` correctly."""

    @pytest.mark.asyncio
    async def test_dispatch_build_invokes_dispatch_autobuild_async(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        recorded_kwargs: dict[str, Any] = {}

        class _FakeStarter:
            def start_async_task(self, subagent_name: str, context: dict) -> str:
                return "task-A"

            async def astart_async_task(
                self, subagent_name: str, context: dict
            ) -> str:
                return "task-A"

        # TASK-FORGE-FRR-F010G: the production
        # ``dispatch_autobuild_async`` is now ``async def`` so the
        # patched stand-in must also be a coroutine function.
        async def _fake_dispatch(
            build_id: str,
            feature_id: str,
            correlation_id: str,
            **kwargs: Any,
        ) -> Any:
            recorded_kwargs.update(
                build_id=build_id,
                feature_id=feature_id,
                correlation_id=correlation_id,
                **kwargs,
            )
            return SimpleNamespace(task_id="task-A")

        deps = build_pipeline_consumer_deps(
            stub_client,
            forge_config,
            persistence,
            async_task_starter=_FakeStarter(),
        )

        payload = SimpleNamespace(
            feature_id="FEAT-D",
            repo="guardkit/forge",
            branch="main",
            feature_yaml_path="features/d.yaml",
            max_turns=5,
            sdk_timeout_seconds=1800,
            triggered_by="cli",
            originating_adapter="cli",
            originating_user="u",
            correlation_id="corr-D",
            parent_request_id=None,
            queued_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
        )

        async def _noop_ack() -> None:
            pass

        with patch.object(_serve_deps, "dispatch_autobuild_async", _fake_dispatch):
            await deps.dispatch_build(payload, _noop_ack)

        assert recorded_kwargs["feature_id"] == "FEAT-D"
        assert recorded_kwargs["correlation_id"] == "corr-D"
        # The four wave-2 collaborators must each be present and not None.
        for key in (
            "forward_context_builder",
            "stage_log_recorder",
            "state_channel",
            "async_task_starter",
            "lifecycle_emitter",
        ):
            assert recorded_kwargs.get(key) is not None, (
                f"dispatch_autobuild_async must be called with {key} bound; "
                f"got {recorded_kwargs.get(key)!r}"
            )

    @pytest.mark.asyncio
    async def test_dispatch_build_raises_when_async_task_starter_missing(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        # When async_task_starter is not provided (TASK-FW10-008 hasn't
        # wired the supervisor middleware yet), the closure must raise
        # rather than silently drop the build.
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        payload = SimpleNamespace(
            feature_id="FEAT-N",
            repo="r",
            branch="main",
            feature_yaml_path="features/n.yaml",
            max_turns=5,
            sdk_timeout_seconds=1800,
            triggered_by="cli",
            originating_adapter="cli",
            originating_user="u",
            correlation_id="corr-N",
            parent_request_id=None,
            queued_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
        )

        async def _noop_ack() -> None:
            pass

        with pytest.raises(RuntimeError, match="async_task_starter"):
            await deps.dispatch_build(payload, _noop_ack)


# ---------------------------------------------------------------------------
# AC: publish_build_failed delegates to the publisher
# ---------------------------------------------------------------------------


class TestPublishBuildFailedDelegates:
    """``publish_build_failed`` calls ``PipelinePublisher.publish_build_failed``."""

    @pytest.mark.asyncio
    async def test_publish_build_failed_delegates_to_publisher(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        from nats_core.events import BuildFailedPayload

        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        failure = BuildFailedPayload(
            feature_id="FEAT-F",
            build_id="FEAT-F",
            failure_reason="malformed BuildQueuedPayload",
            recoverable=False,
            failed_task_id=None,
        )
        # TASK-FORGE-FRR-F010C — the wrapper requires correlation_id as a
        # keyword-only argument (DDR-029).
        await deps.publish_build_failed(failure, "FEAT-F", correlation_id=None)

        assert len(stub_client.published) == 1, (
            "publish_build_failed must result in exactly one NATS publish"
        )
        subject, _body = stub_client.published[0]
        assert subject == "pipeline.build-failed.FEAT-F", (
            f"subject must be derived from feature_id; got {subject!r}"
        )

    @pytest.mark.asyncio
    async def test_publish_build_failed_threads_correlation_id_onto_envelope(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        """TASK-FORGE-FRR-F010C — the wrapper attaches the inbound
        ``correlation_id`` to the v1 ``BuildFailedPayload`` so the
        publisher's central envelope construction (which reads it back
        via ``getattr(payload, "correlation_id", None)``) writes it onto
        the outbound :class:`MessageEnvelope` (DDR-029).
        """
        import json

        from nats_core.events import BuildFailedPayload

        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        failure = BuildFailedPayload(
            feature_id="FEAT-43DE",
            build_id="FEAT-43DE",
            failure_reason="path outside allowlist",
            recoverable=False,
            failed_task_id=None,
        )
        await deps.publish_build_failed(
            failure,
            "FEAT-43DE",
            correlation_id="21df1258-63cb-4e8a-9bef-89234833b68e",
        )

        assert len(stub_client.published) == 1
        subject, body = stub_client.published[0]
        assert subject == "pipeline.build-failed.FEAT-43DE"
        envelope = json.loads(body)
        assert envelope["correlation_id"] == (
            "21df1258-63cb-4e8a-9bef-89234833b68e"
        ), (
            "DDR-029: outbound build-failed envelope must carry the "
            "inbound correlation_id"
        )


# ---------------------------------------------------------------------------
# AC: single shared NATS client (ASSUM-011)
# ---------------------------------------------------------------------------


class TestSingleSharedClientInvariant:
    """Factory binds the publisher to the supplied client without redialing."""

    def test_factory_does_not_open_second_nats_connection(
        self,
        stub_client: _StubNatsClient,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
    ) -> None:
        # The stub client never exposes ``.connect()`` or ``nats.connect``;
        # if the factory tried to dial a fresh connection the test would
        # raise AttributeError. Successful construction proves the
        # factory respected ASSUM-011.
        deps = build_pipeline_consumer_deps(
            stub_client, forge_config, persistence
        )

        assert deps is not None


# ---------------------------------------------------------------------------
# FEAT-UBS-002 (Option-B, stage 1) — per-build budget rides the launch payload
# ---------------------------------------------------------------------------
#
# The dispatch closure resolves ``builds.profile`` → budget caps and attaches a
# compact ``budget`` dict to the launch payload ONLY when the resolved profile
# carries a wall-clock cap. An attended / NULL-profile build attaches nothing,
# keeping the launch bytes byte-equivalent with the pre-budget shape (the
# caps-off no-op invariant). The runner MIN()s the cap against its env/default
# subprocess timeout (tested in test_autobuild_runner.py). Both launch branches
# — legacy no-gate and gate-approved — must carry the same resolved entry.


class _RecordingBudgetStarter:
    """Async-task starter that records each launch context verbatim."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    async def astart_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        self.contexts.append(dict(context))
        return "task-budget"


class _BudgetFakePool:
    """Minimal pool double: records the build then hands back a profiled row."""

    def __init__(self, *, profile: str | None) -> None:
        self._profile = profile

    def record_pending_build(self, payload: Any, profile: str | None = None) -> str:
        return f"build-{payload.feature_id}-budget"

    def get_build_row(self, build_id: str) -> Any:
        return SimpleNamespace(profile=self._profile)


class _NoopContextBuilder:
    def build_for(self, *, stage: Any, build_id: str, feature_id: str) -> list[Any]:
        return []


class _NoopStageLogRecorder:
    def record_running(self, **kwargs: Any) -> None:
        return None


class _NoopStateChannel:
    def initialise_autobuild_state(self, **kwargs: Any) -> None:
        return None


def _make_budget_dispatch_closure(
    *,
    pool: _BudgetFakePool,
    forge_config: ForgeConfig,
    starter: _RecordingBudgetStarter,
    gated: bool,
) -> Any:
    """Build the ``dispatch_build`` closure directly with fakes.

    ``gated=False`` leaves ``gate_repository``/``gate_state_machine`` unset so
    the closure takes the legacy no-gate launch branch. ``gated=True`` supplies
    truthy gate handles; the caller patches ``bound_gate_parts`` +
    ``maybe_gate_build`` so the gate-approved launch branch fires.
    """
    return _serve_deps._build_dispatch_build(
        sqlite_pool=pool,
        forward_context_builder=_NoopContextBuilder(),
        stage_log_recorder=_NoopStageLogRecorder(),
        state_channel=_NoopStateChannel(),
        lifecycle_emitter=SimpleNamespace(),
        async_task_starter=starter,
        forge_config=forge_config,
        gate_repository=object() if gated else None,
        gate_state_machine=object() if gated else None,
    )


def _budget_payload() -> SimpleNamespace:
    return SimpleNamespace(
        feature_id="FEAT-BUDGET",
        repo="guardkit/forge",
        branch="main",
        correlation_id="corr-budget",
        parent_request_id=None,
        queued_at=datetime(2026, 7, 26, 9, tzinfo=UTC),
    )


async def _noop_ack() -> None:
    return None


class TestBudgetRidesLaunchPayload:
    """The per-build wall-clock budget is attached to the launch payload."""

    @pytest.mark.asyncio
    async def test_null_profile_attaches_no_budget_legacy_branch(
        self, forge_config: ForgeConfig
    ) -> None:
        # NULL profile → default (attended, caps off) → NO budget key.
        starter = _RecordingBudgetStarter()
        pool = _BudgetFakePool(profile=None)
        dispatch = _make_budget_dispatch_closure(
            pool=pool, forge_config=forge_config, starter=starter, gated=False
        )

        await dispatch(_budget_payload(), _noop_ack)

        assert len(starter.contexts) == 1
        assert "budget" not in starter.contexts[0], (
            "an attended / NULL-profile launch must stay byte-equivalent — no "
            "budget key (ASSUM-010 caps-off no-op)"
        )

    @pytest.mark.asyncio
    async def test_capped_profile_attaches_budget_legacy_branch(
        self, forge_config: ForgeConfig
    ) -> None:
        # ``unattended`` profile carries the default wall-clock cap.
        from forge.config.models import (
            DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
        )

        starter = _RecordingBudgetStarter()
        pool = _BudgetFakePool(profile="unattended")
        dispatch = _make_budget_dispatch_closure(
            pool=pool, forge_config=forge_config, starter=starter, gated=False
        )

        await dispatch(_budget_payload(), _noop_ack)

        assert len(starter.contexts) == 1
        assert starter.contexts[0]["budget"] == {
            "max_wallclock_seconds": DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
            "profile_name": "unattended",
        }

    @pytest.mark.asyncio
    async def test_capped_profile_attaches_budget_gate_approved_branch(
        self, forge_config: ForgeConfig
    ) -> None:
        # Same resolution must fire on the gate-approved launch branch.
        from forge.cli import _serve_deps_gating, _serve_gate_activation
        from forge.config.models import (
            DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
        )
        from forge.gating.wrappers import GateOutcome

        starter = _RecordingBudgetStarter()
        pool = _BudgetFakePool(profile="unattended")
        dispatch = _make_budget_dispatch_closure(
            pool=pool, forge_config=forge_config, starter=starter, gated=True
        )

        async def _approve(**kwargs: Any) -> Any:
            return GateOutcome.AUTO_APPROVED

        with patch.object(
            _serve_deps_gating, "bound_gate_parts", lambda: object()
        ), patch.object(_serve_gate_activation, "maybe_gate_build", _approve):
            await dispatch(_budget_payload(), _noop_ack)

        assert len(starter.contexts) == 1
        assert starter.contexts[0]["budget"] == {
            "max_wallclock_seconds": DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
            "profile_name": "unattended",
        }


class TestResolveLaunchBudget:
    """Unit coverage for the pure ``_resolve_launch_budget`` helper."""

    def test_none_when_no_forge_config(self) -> None:
        pool = _BudgetFakePool(profile="unattended")
        assert _serve_deps._resolve_launch_budget(pool, None, "b1") is None

    def test_none_for_null_profile(self, forge_config: ForgeConfig) -> None:
        pool = _BudgetFakePool(profile=None)
        assert (
            _serve_deps._resolve_launch_budget(pool, forge_config, "b1") is None
        )

    def test_none_when_caps_enabled_but_no_wallclock(
        self, forge_config: ForgeConfig
    ) -> None:
        # A profile with a review-cycle cap but no wall-clock cap carries
        # nothing on this launch path (only the wall-clock cap is enforceable).
        from forge.config.models import BudgetConfig, BudgetGuards

        cfg = forge_config.model_copy(
            update={
                "budget": BudgetConfig(
                    default_profile="attended",
                    profiles={
                        "attended": BudgetGuards(),
                        "reviews-only": BudgetGuards(max_review_cycles=3),
                    },
                )
            }
        )
        pool = _BudgetFakePool(profile="reviews-only")
        assert _serve_deps._resolve_launch_budget(pool, cfg, "b1") is None

    def test_entry_for_wallclock_profile(self, forge_config: ForgeConfig) -> None:
        from forge.config.models import (
            DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
        )

        pool = _BudgetFakePool(profile="unattended")
        entry = _serve_deps._resolve_launch_budget(pool, forge_config, "b1")
        assert entry == {
            "max_wallclock_seconds": DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
            "profile_name": "unattended",
        }
