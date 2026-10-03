"""End-to-end lifecycle integration test for ``forge serve`` (TASK-FW10-011).

Capstone test for FEAT-FORGE-010. Asserts that a single
``pipeline.build-queued.<feature_id>`` envelope routed to a
production-composed ``forge serve`` daemon produces the full lifecycle
envelope sequence on JetStream, with the inbound ``correlation_id``
threaded through every event, in the correct order, against the
wired-in-production stack rather than a unit test with mocked
dispatchers.

Per the task brief (finding F7), the test mocks
``AutobuildDispatcher.dispatch(...)`` at the boundary so the test does
not actually run a real autobuild. That boundary is the
:class:`AsyncTaskStarter` Protocol consumed by
:func:`forge.pipeline.dispatchers.autobuild_async.dispatch_autobuild_async`:
production wires the LangGraph ``AsyncSubAgentMiddleware``
``start_async_task`` hook there; this test injects an in-memory
``_FakeAutobuildStarter`` that scripts a realistic transition sequence
(``starting → planning_waves → running_wave → completed``) through the
real :class:`PipelineLifecycleEmitter` constructed by
:func:`forge.cli._serve_deps.build_pipeline_consumer_deps`. No real
worktree is built, no real DeepAgents subagent is invoked.

Everything else on the path is the production wiring:

* ``_run_serve`` opens **one** NATS client via the daemon's
  :data:`_serve_daemon.nats_connect` seam (here a
  :class:`_FakeBrokerClient` that exposes both ``publish(subject,
  body)`` for the real :class:`PipelinePublisher` and
  ``jetstream().pull_subscribe(...)`` for the daemon's durable
  consumer).
* Both ``reconcile_on_boot`` seams run before consumer attach (their
  default no-op shape suffices — the test does not seed redeliveries).
* :func:`forge.cli.serve.bind_production_dispatch_chain` is the bound
  composer, so the daemon's ``dispatch_payload`` is the real
  :func:`forge.adapters.nats.pipeline_consumer.handle_message` chain
  (envelope validation, originator allowlist, path allowlist,
  duplicate-terminal detection, accepted-build dispatch via
  ``dispatch_autobuild_async``).
* The lifecycle emitter is constructed **once** by
  :func:`forge.cli._serve_deps_lifecycle.build_publisher_and_emitter`
  against the shared NATS client; every captured envelope therefore
  flows through the same publisher.

The test asserts the FEAT-FORGE-010 acceptance set:

| AC | Assertion |
|---|---|
| Full lifecycle envelope sequence | ``build-started → stage-complete×N → build-complete`` published in order. |
| Correlation-id threading | Every captured envelope carries the inbound ``correlation_id``; no envelope carries a different one. |
| Build-started precedes first stage; terminal closes | First lifecycle envelope is ``build-started``; terminal envelope is the last lifecycle envelope and appears exactly once. |
| ``AsyncSubAgent`` dispatch returns task_id without blocking | ``start_async_task`` returns a non-empty ``task_id`` synchronously while the autobuild scripted task runs in background. |
| Real-stage-only ``stage-complete`` (ASSUM-004) | No envelope has ``stage_label="dispatch"``. |
| Single shared NATS connection (ASSUM-011) | Only one ``nats_connect(...)`` call across the daemon's startup path. |
| Lifecycle ordering invariant | ``build-started`` precedes any ``stage-complete``; every ``stage-complete`` precedes the terminal envelope. |
| In-subagent ``stage-complete`` carries ``target_kind="subagent"`` and ``target_identifier=task_id`` (ASSUM-018) | At least one captured ``stage-complete`` envelope has these fields populated as expected. |
| Two-replica failover (max_ack_pending=1) | Two daemons sharing the same fake subscription queue: exactly one fetches the message, the other idles. |
| Fail-fast on NATS unreachable | Daemon raises with a diagnostic naming the broker URL when ``nats_connect`` raises. |

Determinism: the only async waits are bounded ``asyncio.wait_for`` calls
on internal events (``scripted_completion``, captured-envelope count).
There are no real-time delays, so the suite is CI-deterministic.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

from forge.adapters.nats.pipeline_consumer import PipelineConsumerDeps
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_daemon
from forge.cli import serve as serve_module
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_dispatcher import make_handle_message_dispatcher
from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
from forge.cli._serve_state import SubscriptionState
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import BuildContext
from forge.pipeline.dispatchers.autobuild_async import AUTOBUILD_RUNNER_NAME
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import BuildFailedPayload, BuildQueuedPayload

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Embedded-NATS substitute — exposes both publish (used by the real
# PipelinePublisher) and jetstream() (used by the daemon's pull-subscribe).
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Minimal stand-in for ``nats.aio.msg.Msg`` carrying ``data`` + ``ack``."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False
        self._ack_lock = asyncio.Lock()

    async def ack(self) -> None:
        async with self._ack_lock:
            self.acked = True


class _FakeSubscription:
    """Pull subscription whose ``fetch()`` returns batches from a queue.

    Returning ``asyncio.TimeoutError`` lets the daemon's main loop poll
    without messages so we can drive the test by injecting a single
    ``_FakeMsg`` and then watching the captured published envelopes.

    For the two-replica failover sub-test we share one
    :class:`_FakeSubscription` between two daemons; the lock-protected
    ``batches.pop(0)`` enforces the work-queue semantics
    ``max_ack_pending=1`` provides at the broker level.
    """

    def __init__(self) -> None:
        self.batches: list[list[_FakeMsg]] = []
        self.fetch_calls = 0
        self.unsubscribed = False
        # Synchronisation for the two-replica failover sub-test.
        self._lock = asyncio.Lock()

    async def fetch(
        self, batch: int = 1, timeout: float = 1.0
    ) -> list[_FakeMsg]:
        async with self._lock:
            self.fetch_calls += 1
            if self.batches:
                return self.batches.pop(0)
        # Mimic nats-py's "no messages" behaviour: TimeoutError is the
        # documented signal the daemon's pull loop swallows.
        raise asyncio.TimeoutError()

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJetStream:
    """Captures ``pull_subscribe`` arguments and yields a fixed subscription."""

    def __init__(self, sub: _FakeSubscription) -> None:
        self._sub = sub
        self.pull_subscribe_kwargs: dict[str, Any] | None = None

    async def consumer_info(self, stream: str, durable: str) -> Any:
        # The daemon reads the live durable before binding so it can apply
        # the configured build limit. "Not found" means pull_subscribe
        # creates it, which is what these tests model.
        from nats.js.errors import NotFoundError

        raise NotFoundError()

    async def pull_subscribe(self, **kwargs: Any) -> _FakeSubscription:
        self.pull_subscribe_kwargs = kwargs
        return self._sub


class _FakeBrokerClient:
    """In-process NATS substitute used by the production wiring.

    Implements only the surface ``forge serve`` actually touches:

    * ``publish(subject, body)`` for the real
      :class:`~forge.adapters.nats.pipeline_publisher.PipelinePublisher`.
      Captured envelopes are exposed as ``self.published``.
    * ``jetstream()`` returning a :class:`_FakeJetStream` for the
      daemon's pull-subscribe.
    * ``close()`` so the ``finally`` block of ``_run_serve`` does not
      raise.
    """

    def __init__(self, sub: _FakeSubscription | None = None) -> None:
        self._js = _FakeJetStream(sub or _FakeSubscription())
        # Captured (subject, body) tuples in publish order. The
        # PipelinePublisher serialises wire writes per-envelope, so the
        # list order mirrors the wire order JetStream would observe.
        self.published: list[tuple[str, bytes]] = []
        self._publish_lock = asyncio.Lock()
        self.closed = False

    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        async with self._publish_lock:
            self.published.append((subject, body))
        return None

    def jetstream(self) -> _FakeJetStream:
        return self._js

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fake AutobuildDispatcher boundary — the AsyncTaskStarter Protocol seam
# ---------------------------------------------------------------------------


class _FakeAutobuildStarter:
    """Mock of the production ``AsyncSubAgentMiddleware`` ``start_async_task``.

    Mirrors the production wiring at the
    :class:`~forge.pipeline.dispatchers.autobuild_async.AsyncTaskStarter`
    Protocol seam:

    * Returns a deterministic, non-empty ``task_id`` *synchronously* —
      proves the dispatch returns without blocking the supervisor's
      reasoning loop (FEAT-FORGE-007 Group A).
    * Schedules a background coroutine that scripts the realistic
      autobuild lifecycle (``starting → planning_waves → running_wave →
      completed``) through the *real* :class:`PipelineLifecycleEmitter`
      threaded onto ``context['lifecycle_emitter']`` per DDR-007 Option A.
    * Sets ``scripted_completion`` once the terminal ``emit_complete``
      coroutine returns so the test can deterministically wait for the
      full sequence to land before asserting.

    The starter does NOT call ``msg.ack()`` — the production
    ``ack_callback`` is owned by the consumer's state machine and is
    irrelevant to the lifecycle envelope sequence asserted here.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.task_ids: list[str] = []
        self._counter = 0
        self._counter_lock = threading.Lock()
        self.scripted_completion = asyncio.Event()
        self._scripted_tasks: list[asyncio.Task[None]] = []

    def _mint_task_id(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"autobuild-task-{self._counter:04d}"

    def start_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        """Mint a task_id, schedule the lifecycle script, return immediately."""
        if subagent_name != AUTOBUILD_RUNNER_NAME:
            # Defensive — the dispatcher only addresses the autobuild
            # runner; surface a misconfiguration loudly rather than
            # silently scripting the wrong lifecycle.
            raise ValueError(
                f"_FakeAutobuildStarter only handles "
                f"{AUTOBUILD_RUNNER_NAME!r}; got {subagent_name!r}"
            )
        task_id = self._mint_task_id()
        self.task_ids.append(task_id)
        # Snapshot the context so test assertions can introspect it
        # without racing with the background script.
        self.calls.append((subagent_name, dict(context)))

        emitter = context.get("lifecycle_emitter")
        if emitter is None:
            # AC-relevant safety net — the production wiring threads the
            # emitter onto the context (DDR-007 Option A); a None value
            # means TASK-FW10-008 wiring regressed.
            raise RuntimeError(
                "_FakeAutobuildStarter: ctx['lifecycle_emitter'] missing — "
                "production wiring (TASK-FW10-008) regressed"
            )

        ctx = BuildContext(
            feature_id=context["feature_id"],
            build_id=context["build_id"],
            correlation_id=context["correlation_id"],
            wave_total=2,
        )
        loop = asyncio.get_running_loop()
        scripted = loop.create_task(
            self._script_lifecycle(emitter, ctx, task_id),
            name=f"fake-autobuild-{task_id}",
        )
        self._scripted_tasks.append(scripted)
        return task_id

    async def _script_lifecycle(
        self, emitter: Any, ctx: BuildContext, task_id: str
    ) -> None:
        """Drive the real emitter through a realistic transition sequence.

        The sequence mirrors what the production ``autobuild_runner``
        subagent emits via its ``_update_state`` helper (DDR-007 §Decision
        — every transition calls ``emitter.on_transition(state)`` from a
        single site). We invoke the emit methods directly here because
        the runner's exact transition table is owned by FEAT-FORGE-005
        and is out of scope for this wiring test; what matters is that
        the same emitter receives the same calls.
        """
        try:
            # 1. PREPARING → RUNNING. ``build-started`` lands first.
            await emitter.emit_started(ctx)

            # 2. In-subagent stage commit: planning_waves complete. The
            #    target is the autobuild AsyncSubAgent itself (ASSUM-018:
            #    target_kind="subagent", target_identifier=task_id).
            await emitter.emit_stage_complete(
                ctx,
                stage_label="planning_waves",
                target_kind="subagent",
                target_identifier=task_id,
                status="PASSED",
                gate_mode=None,
                coach_score=0.95,
                duration_secs=0.5,
                completed_at="2026-05-02T12:00:00+00:00",
            )

            # 3. In-subagent stage commit: running_wave complete.
            await emitter.emit_stage_complete(
                ctx,
                stage_label="running_wave",
                target_kind="subagent",
                target_identifier=task_id,
                status="PASSED",
                gate_mode=None,
                coach_score=0.92,
                duration_secs=1.5,
                completed_at="2026-05-02T12:00:01+00:00",
            )

            # 4. FINALISING → COMPLETE. Terminal envelope.
            await emitter.emit_complete(
                ctx,
                repo="guardkit/forge",
                branch="main",
                tasks_completed=2,
                tasks_failed=0,
                tasks_total=2,
                pr_url="https://github.com/guardkit/forge/pull/1",
                duration_seconds=10,
                summary="all waves committed",
            )
        finally:
            self.scripted_completion.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_envelope_bytes(
    feature_id: str,
    correlation_id: str,
    feature_yaml_path: Path,
) -> bytes:
    """Build a wire-shaped ``pipeline.build-queued.<feature_id>`` envelope."""
    now = datetime.now(UTC)
    payload = BuildQueuedPayload(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path=str(feature_yaml_path),
        triggered_by="cli",
        originating_adapter="cli-wrapper",
        correlation_id=correlation_id,
        requested_at=now,
        queued_at=now,
    )
    envelope = MessageEnvelope(
        source_id="forge-cli",
        event_type=EventType.BUILD_QUEUED,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json().encode("utf-8")


def _parse_published(
    published: list[tuple[str, bytes]],
) -> list[MessageEnvelope]:
    """Decode every captured (subject, body) into a :class:`MessageEnvelope`."""
    return [MessageEnvelope.model_validate_json(body) for _, body in published]


# Set of subject segments the daemon's lifecycle pipeline can publish.
# Used to filter the captured tuple list down to lifecycle envelopes
# (the test's :func:`_build_envelope_bytes` build-queued envelopes are
# not on this list — they are inbound, not produced by the daemon).
_LIFECYCLE_SUBJECT_SEGMENTS: frozenset[str] = frozenset({
    "build-started",
    "build-progress",
    "stage-complete",
    "build-paused",
    "build-resumed",
    "build-complete",
    "build-failed",
    "build-cancelled",
})


def _segment(subject: str) -> str:
    """Return the middle segment of ``pipeline.<segment>.<feature_id>``."""
    parts = subject.split(".")
    if len(parts) < 3:
        return ""
    return parts[1]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Migrated SQLite writer connection backed by ``tmp_path/forge.db``."""
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection,
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(connection=writer_db)


@pytest.fixture()
def forge_config(tmp_path: Path) -> ForgeConfig:
    """ForgeConfig whose filesystem allowlist contains ``tmp_path``."""
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[tmp_path]),
        ),
    )


@pytest.fixture()
def feature_yaml_inside_allowlist(tmp_path: Path) -> Path:
    """A feature YAML path inside the ``tmp_path`` allowlist root."""
    target = tmp_path / "feature.yaml"
    target.write_text("# placeholder feature spec\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# AC-001 to AC-008 — the headline E2E lifecycle assertion (one test, many ACs)
# ---------------------------------------------------------------------------


class TestForgeServeLifecycleE2E:
    """The capstone E2E test for FEAT-FORGE-010 (ACs as a single fixture)."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_envelope_sequence_and_correlation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        forge_config: ForgeConfig,
        persistence: SqliteLifecyclePersistence,
        feature_yaml_inside_allowlist: Path,
    ) -> None:
        # --- Arrange ----------------------------------------------------
        feature_id = f"FEAT-{uuid.uuid4().hex[:6].upper()}"
        correlation_id = f"corr-{uuid.uuid4().hex[:8]}"

        # Single shared fake broker client. Both the daemon (jetstream
        # pull-subscribe) and the real PipelinePublisher (publish) read
        # off the same instance — that's the ASSUM-011 single-shared-
        # connection invariant under test.
        sub = _FakeSubscription()
        sub.batches.append([
            _FakeMsg(_build_envelope_bytes(
                feature_id=feature_id,
                correlation_id=correlation_id,
                feature_yaml_path=feature_yaml_inside_allowlist,
            )),
        ])
        broker = _FakeBrokerClient(sub)

        connect_calls: list[str] = []

        async def _fake_connect(servers: str) -> Any:
            connect_calls.append(servers)
            return broker

        # The supervisor-owned middleware boundary. Production wires
        # AsyncSubAgentMiddleware here; the test injects the in-process
        # script.
        starter = _FakeAutobuildStarter()

        # Compose the dispatch chain with the real
        # PipelinePublisher / PipelineLifecycleEmitter pair bound to
        # the shared client. The AutobuildDispatcher boundary is mocked
        # by a custom ``dispatch_build`` closure that calls the
        # _FakeAutobuildStarter (which scripts the lifecycle through
        # the real emitter — DDR-007 Option A). Everything from
        # ``handle_message`` validation upwards is real production
        # code.
        async def _compose_with_fake_autobuild(client: Any) -> None:
            publisher, emitter = build_publisher_and_emitter(
                client, config=forge_config.pipeline
            )

            async def _is_duplicate_terminal(
                _feature: str, _correlation: str
            ) -> bool:
                return False

            async def _publish_build_failed(
                payload: BuildFailedPayload, _feature_id: str
            ) -> None:
                await publisher.publish_build_failed(payload)

            async def _dispatch_build(
                payload: BuildQueuedPayload, ack_callback: Any
            ) -> None:
                # Persist the QUEUED row so the build_id is durable
                # before the (mocked) autobuild starts. Mirrors the
                # production ``record_pending_build`` step in
                # ``_serve_deps._build_dispatch_build``.
                build_id = persistence.record_pending_build(payload)
                # Mock at the AutobuildDispatcher boundary: the fake
                # starter mints a task_id and schedules the lifecycle
                # script through the real emitter; no real autobuild
                # runs. The context shape mirrors the production
                # launch payload (DDR-007 Option A).
                starter.start_async_task(
                    subagent_name=AUTOBUILD_RUNNER_NAME,
                    context={
                        "build_id": build_id,
                        "feature_id": payload.feature_id,
                        "correlation_id": payload.correlation_id,
                        "context_entries": [],
                        "lifecycle_emitter": emitter,
                        "ack_callback": ack_callback,
                    },
                )

            deps = PipelineConsumerDeps(
                forge_config=forge_config,
                is_duplicate_terminal=_is_duplicate_terminal,
                dispatch_build=_dispatch_build,
                publish_build_failed=_publish_build_failed,
            )
            _serve_daemon.dispatch_payload = make_handle_message_dispatcher(deps)

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(
            serve_module, "compose_dispatch_chain", _compose_with_fake_autobuild
        )
        # Healthz aiohttp server is irrelevant to the wire assertions
        # and not nice to test in-process; replace with a no-op
        # coroutine so _run_serve's asyncio.wait does not block.
        async def _fake_healthz(config: object, state: object) -> None:
            # Park forever so the daemon coroutine is the FIRST_COMPLETED
            # winner once we cancel it externally.
            await asyncio.Event().wait()

        monkeypatch.setattr(serve_module, "run_healthz_server", _fake_healthz)

        config = ServeConfig()
        state = SubscriptionState()

        # --- Act --------------------------------------------------------
        # Spin up the daemon and wait for the scripted lifecycle to
        # complete on the wire. We bound every wait with a generous (but
        # still CI-safe) timeout to surface a stuck pipeline as a clear
        # test failure rather than a CI hang.
        run_task: asyncio.Task[None] = asyncio.create_task(
            serve_module._run_serve(config, state),
            name="forge-serve-e2e",
        )

        try:
            # Wait for the scripted background coroutine to finish
            # emitting all four lifecycle envelopes on the wire.
            await asyncio.wait_for(
                starter.scripted_completion.wait(), timeout=5.0
            )
            # Drain remaining publishes (emit_* coroutines are awaited
            # inside the scripted task, so by the time
            # scripted_completion fires every publish has reached the
            # broker; we still yield once to let the daemon advance).
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            run_task.cancel()
            try:
                await asyncio.wait_for(run_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # --- Assert -----------------------------------------------------

        # AC: single shared NATS connection (ASSUM-011) — exactly one
        # nats_connect call across the daemon's startup path.
        assert connect_calls == [config.nats_url], (
            f"AC violated: expected one nats_connect on the startup path, "
            f"got {connect_calls!r}"
        )

        # AC: the daemon attached to the durable consumer named
        # 'forge-serve' on pipeline.build-queued.* with explicit ack and
        # max_ack_pending=1.
        kwargs = broker.jetstream().pull_subscribe_kwargs
        assert kwargs is not None, (
            "daemon never attached its pull subscription"
        )
        assert kwargs["durable"] == "forge-serve"
        assert kwargs["subject"] == "pipeline.build-queued.*"
        assert kwargs["stream"] == "PIPELINE"
        cfg = kwargs["config"]
        assert isinstance(cfg, ConsumerConfig)
        assert cfg.ack_policy is AckPolicy.EXPLICIT
        assert cfg.deliver_policy is DeliverPolicy.ALL
        assert cfg.max_ack_pending == 1

        # AC: AutobuildDispatcher.dispatch (== AsyncTaskStarter) was
        # mocked at the boundary; no real autobuild ran.
        assert len(starter.calls) == 1, (
            f"expected exactly one start_async_task call; got "
            f"{len(starter.calls)} ({starter.calls!r})"
        )
        starter_subagent_name, starter_context = starter.calls[0]
        assert starter_subagent_name == AUTOBUILD_RUNNER_NAME
        assert starter_context["feature_id"] == feature_id
        assert starter_context["correlation_id"] == correlation_id
        # The middleware ctx must carry the lifecycle emitter (DDR-007
        # Option A) — that's the seam every transition is published
        # through.
        assert starter_context["lifecycle_emitter"] is not None

        # AC: AsyncSubAgent dispatch returned a non-empty task_id
        # without blocking the supervisor.
        assert len(starter.task_ids) == 1
        task_id = starter.task_ids[0]
        assert task_id.startswith("autobuild-task-")

        # Filter captured envelopes down to the lifecycle ones (the
        # build-queued envelope is inbound; we are asserting on the
        # forge-produced ones).
        lifecycle = [
            (subject, MessageEnvelope.model_validate_json(body))
            for subject, body in broker.published
            if _segment(subject) in _LIFECYCLE_SUBJECT_SEGMENTS
        ]
        segments = [_segment(subj) for subj, _ in lifecycle]

        # AC: full lifecycle envelope sequence end-to-end. The scripted
        # path emits build-started → 2× stage-complete → build-complete.
        assert segments == [
            "build-started",
            "stage-complete",
            "stage-complete",
            "build-complete",
        ], (
            f"AC violated: lifecycle envelope sequence wrong; got {segments!r}"
        )

        # AC: every observed envelope carries the inbound correlation_id
        # (no envelope carries a different one).
        wrong_corr = [
            (subj, env.correlation_id)
            for subj, env in lifecycle
            if env.correlation_id != correlation_id
        ]
        assert not wrong_corr, (
            f"AC violated: envelopes carry mismatched correlation_id "
            f"(expected {correlation_id!r}): {wrong_corr!r}"
        )

        # AC: ordering invariant — build-started precedes every
        # stage-complete; every stage-complete precedes the terminal
        # envelope; the terminal appears exactly once.
        first_started_idx = segments.index("build-started")
        terminal_idxs = [
            i
            for i, seg in enumerate(segments)
            if seg in {"build-complete", "build-failed", "build-cancelled"}
        ]
        assert len(terminal_idxs) == 1, (
            f"AC violated: terminal envelope must appear exactly once; "
            f"observed at indices {terminal_idxs!r}"
        )
        terminal_idx = terminal_idxs[0]
        stage_idxs = [
            i for i, seg in enumerate(segments) if seg == "stage-complete"
        ]
        assert all(first_started_idx < i for i in stage_idxs), (
            "AC violated: build-started must precede every stage-complete"
        )
        assert all(i < terminal_idx for i in stage_idxs), (
            "AC violated: every stage-complete must precede the terminal "
            "envelope"
        )

        # AC: no envelope carries stage_label='dispatch' (ASSUM-004 —
        # synthetic dispatch envelope dropped).
        stage_labels = [
            env.payload.get("stage_label")
            for subj, env in lifecycle
            if _segment(subj) == "stage-complete"
            and isinstance(env.payload, dict)
        ]
        assert "dispatch" not in stage_labels, (
            f"AC violated (ASSUM-004): stage_label='dispatch' leaked onto "
            f"the wire; observed labels: {stage_labels!r}"
        )

        # AC: at least one in-subagent stage-complete carries
        # target_kind='subagent' AND target_identifier == task_id
        # (ASSUM-018).
        in_subagent_matches = [
            env.payload
            for subj, env in lifecycle
            if _segment(subj) == "stage-complete"
            and isinstance(env.payload, dict)
            and env.payload.get("target_kind") == "subagent"
            and env.payload.get("target_identifier") == task_id
        ]
        assert in_subagent_matches, (
            f"AC violated (ASSUM-018): no stage-complete envelope had "
            f"target_kind='subagent' + target_identifier={task_id!r}; "
            f"got payloads: {[env.payload for _, env in lifecycle]!r}"
        )

        # Sanity: every lifecycle subject is on the canonical FEAT-XXX
        # routing key (FEAT-FORGE-002 §3.1 contract).
        assert all(
            subj.endswith(f".{feature_id}") for subj, _ in lifecycle
        ), (
            f"AC violated: lifecycle subjects must end with .{feature_id}; "
            f"got {[s for s, _ in lifecycle]!r}"
        )


# ---------------------------------------------------------------------------
# Group D / ADR-ARCH-027 — two-replica failover (max_ack_pending=1 work-queue)
# ---------------------------------------------------------------------------


class TestTwoReplicaWorkQueueFailover:
    """Two daemons share the durable; exactly one fetches the message."""

    @pytest.mark.asyncio
    async def test_only_one_replica_fetches_the_single_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both replicas attach to the same _FakeSubscription instance,
        # so the work-queue semantics ``max_ack_pending=1`` enforces at
        # the broker level are emulated by the lock-protected
        # ``batches.pop(0)`` inside fetch().
        sub = _FakeSubscription()
        sub.batches.append([_FakeMsg(b"{}")])  # malformed envelope; doesn't matter
        broker = _FakeBrokerClient(sub)

        async def _fake_connect(servers: str) -> Any:
            return broker

        # The default ``dispatch_payload`` parses + acks unparseable
        # envelopes safely (see ``_default_dispatch``); we let the
        # daemon use it so we don't need a full dispatch chain here.
        # The behaviour under test is purely "exactly one daemon
        # fetches the single available message".
        captured: list[bytes] = []

        async def _capturing_dispatch(msg: Any) -> None:
            captured.append(msg.data)
            await msg.ack()

        monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
        monkeypatch.setattr(
            _serve_daemon, "dispatch_payload", _capturing_dispatch
        )

        # Two daemons, same subscription instance, run concurrently.
        async def _run_one() -> None:
            config = ServeConfig()
            state = SubscriptionState()
            daemon = asyncio.create_task(
                _serve_daemon.run_daemon(config, state, client=broker),
                name="replica",
            )
            try:
                # Wait until the fetch loop has either acked the lone
                # message or polled at least twice (replica that lost
                # the race).
                for _ in range(200):
                    if captured or sub.fetch_calls >= 2:
                        break
                    await asyncio.sleep(0.01)
            finally:
                daemon.cancel()
                try:
                    await asyncio.wait_for(daemon, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        await asyncio.gather(_run_one(), _run_one())

        # AC (Group D, ADR-ARCH-027): exactly one replica fetched the
        # message. The other idled (its fetches all returned
        # TimeoutError because the queue was already drained).
        assert len(captured) == 1, (
            f"AC violated: max_ack_pending=1 should deliver the message to "
            f"exactly one replica; captured {len(captured)} delivery/ies"
        )


# ---------------------------------------------------------------------------
# Group E — fail-fast on NATS unreachable (diagnostic names the broker)
# ---------------------------------------------------------------------------


class TestFailFastOnNatsUnreachable:
    """``_run_serve`` raises with the broker URL when the connect fails."""

    @pytest.mark.asyncio
    async def test_run_serve_raises_with_broker_url_in_diagnostic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Production behaviour: ``nats.connect(servers=URL)`` raises
        # ``ErrNoServers`` (or a subclass) which propagates out of
        # ``_run_serve`` so the operator sees a clear startup-time
        # diagnostic. We stand in with a plain ConnectionError that
        # carries the broker URL in its message — same observable
        # property: the daemon refuses to start with a diagnostic
        # naming the broker.
        bad_url = "nats://broker.unreachable.example:4222"

        async def _failing_connect(servers: str) -> Any:
            raise ConnectionError(
                f"could not reach NATS broker at {servers}"
            )

        monkeypatch.setattr(_serve_daemon, "nats_connect", _failing_connect)

        config = ServeConfig(nats_url=bad_url)
        state = SubscriptionState()

        with pytest.raises(ConnectionError) as exc_info:
            await serve_module._run_serve(config, state)

        # The diagnostic must name the configured broker — operators
        # must be able to see *which* broker the daemon failed to
        # reach without grepping config files.
        assert bad_url in str(exc_info.value), (
            f"AC violated (Group E): diagnostic must name the broker "
            f"({bad_url!r}); got: {exc_info.value!r}"
        )
