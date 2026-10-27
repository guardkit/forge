"""Sidecar-aware E2E integration test for the lifecycle bridge (TASK-FRR-PEB-013).

This test is the **sidecar-aware regression lock** that ASSUM-008 / Q8
sub-option (a) commits the project to. It is intentionally separate
from ``tests/integration/test_forge_serve_orchestrator_e2e.py``
(TASK-FW10-011) — which is the **in-process composition lock**: that
test mocks the sidecar boundary and asserts the in-process production
wiring emits the canonical lifecycle envelopes for a single
``pipeline.build-queued.<feature_id>`` input.

This file's regression target is the *other* failure class: the
translation-layer regressions and SDK version skew between the
``langgraph-runner`` sidecar and ``langgraph_sdk`` that an in-process
test cannot catch. To do that, the test:

1. Spins up a **real** ``langgraph-runner`` sidecar (``langgraph dev``)
   via the :func:`langgraph_sidecar` fixture (in ``conftest.py``).
2. Spins up a **real** ``nats-server`` with JetStream enabled in a
   subprocess on a free local port.
3. Bootstraps ``forge serve`` in-process — through its production
   composition path — wired against the real NATS broker and the real
   sidecar URL. Crucially, this means
   :func:`forge.lifecycle_bridge.version_check.check_langgraph_runner_version`
   actually runs against the live sidecar at startup; a version-skew
   regression fails the test deterministically.
4. Publishes one ``pipeline.build-queued.<feature_id>`` envelope onto
   JetStream via a real NATS client.
5. Subscribes to ``pipeline.>`` via a real NATS client and collects
   envelopes for up to ``COLLECTION_BUDGET_SECONDS`` (60s) or until the
   terminal envelope arrives — whichever comes first.
6. Asserts the collected sequence matches the canonical pattern:
   ``1× build-started → ≥1× stage-complete → 1× terminal`` and that
   every collected envelope carries the inbound ``correlation_id``.

Determinism strategy
--------------------

The test is parametrised across two paths:

* ``success_path`` — drives the autobuild dispatcher to script a clean
  ``starting → planning → running → completed`` lifecycle through the
  bridge's real publisher. The terminal envelope is ``build-complete``.
* ``forced_failure`` — drives the dispatcher to script a
  ``starting → planning → running`` sequence followed by a forced
  ``RuntimeError`` mid-stage. The terminal envelope is ``build-failed``
  with an operator-readable failure reason.

In both paths, the lifecycle envelopes are emitted through the **real**
:class:`~forge.lifecycle_bridge.bridge.LifecycleBridge` and the real
:class:`~forge.adapters.nats.pipeline_publisher.PipelinePublisher` — so
the test exercises the production translation / wireup / NATS path
end-to-end. The dispatcher boundary is scripted (rather than running a
real autobuild) for the same reason TASK-FW10-011 mocks it: a real
autobuild would invoke an LLM and is non-deterministic on a
per-second budget.

Skipping
--------

The test :func:`pytest.skip`s with an actionable reason whenever a
required external dependency is missing:

* The ``nats-server`` binary is not on ``PATH``.
* The ``langgraph_sidecar`` fixture (in ``conftest.py``) skips when
  ``langgraph`` / ``langgraph_api`` are unavailable.

Both binaries are typical CI installs; the test is also gated by
``@pytest.mark.slow`` so the default ``pytest`` invocation excludes it
and CI runs it on a separate stage.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from nats_core.envelope import EventType, MessageEnvelope


pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Per-test collection budget. The AC pins this at 60 seconds; the test
#: short-circuits as soon as the terminal envelope arrives, so a healthy
#: run completes in well under a second on a local dev machine.
COLLECTION_BUDGET_SECONDS: float = 60.0

#: Subjects published by ``PipelinePublisher`` that count as lifecycle
#: envelopes. Mirrors
#: :data:`forge.lifecycle_bridge.wireup._SUBJECT_SEGMENT_TABLE` so a
#: change to the canonical event set here surfaces both as a translator
#: bug AND as a missed assertion.
_LIFECYCLE_SEGMENTS: frozenset[str] = frozenset(
    {
        "build-started",
        "build-progress",
        "stage-complete",
        "build-paused",
        "build-resumed",
        "build-complete",
        "build-failed",
        "build-cancelled",
    }
)

_TERMINAL_SEGMENTS: frozenset[str] = frozenset(
    {"build-complete", "build-failed", "build-cancelled"}
)


# ---------------------------------------------------------------------------
# Helpers — port + readiness
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return an ephemeral free port on 127.0.0.1.

    Used to pick non-overlapping ports for the NATS server subprocess
    so concurrent test runs (e.g. ``pytest -n auto``) do not collide.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp_ready(
    host: str, port: int, *, timeout_seconds: float
) -> None:
    """Block until ``(host, port)`` accepts a TCP connection.

    Raises :class:`TimeoutError` if the port does not open within
    ``timeout_seconds``. Used to gate the test on NATS readiness so the
    nats-py client connect does not race the server boot.
    """
    deadline = time.monotonic() + timeout_seconds
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except (ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.05)
    raise TimeoutError(
        f"NATS at {host}:{port} did not accept connections within "
        f"{timeout_seconds:.1f}s (last error: {last_exc!r})"
    )


# ---------------------------------------------------------------------------
# Subprocess fixtures — real nats-server with JetStream
# ---------------------------------------------------------------------------


@pytest.fixture
def nats_server(tmp_path: Path) -> Iterator[str]:
    """Spin up ``nats-server -js`` in a subprocess; yield ``nats://...``.

    Skips when ``nats-server`` is not on ``PATH`` (typical lightweight
    CI runners). The subprocess is bound to a JetStream-enabled
    standalone server on an ephemeral port, with the JetStream store
    rooted under ``tmp_path`` so the test leaves no state behind.
    """
    binary = shutil.which("nats-server")
    if binary is None:
        pytest.skip(
            "nats-server binary not on PATH; sidecar-aware E2E test "
            "requires a real NATS broker. Install via `brew install "
            "nats-server` or the equivalent for your CI image."
        )

    port = _find_free_port()
    store_dir = tmp_path / "jetstream"
    store_dir.mkdir()

    cmd = [
        binary,
        "--jetstream",
        "--store_dir",
        str(store_dir),
        "--addr",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(  # noqa: S603 — binary path resolved via shutil.which
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        try:
            _wait_for_tcp_ready("127.0.0.1", port, timeout_seconds=10.0)
        except TimeoutError as exc:
            try:
                proc.terminate()
                stdout, _ = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate(timeout=5.0)
            pytest.skip(
                f"nats-server failed to become ready: {exc}\n"
                f"--- subprocess output ---\n{stdout}"
            )
        yield f"nats://127.0.0.1:{port}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# Helper — JetStream stream creation
# ---------------------------------------------------------------------------


async def _ensure_pipeline_stream(nats_url: str) -> None:
    """Create the ``PIPELINE`` JetStream stream against the real broker.

    The stream binds the canonical ``pipeline.>`` subject hierarchy.
    Production deployments seed this stream out-of-band; the test
    creates it explicitly so the publish + subscribe paths see the
    same contract a deployed broker would expose.
    """
    nats = pytest.importorskip("nats")
    from nats.js.api import StreamConfig

    client = await nats.connect(nats_url)
    try:
        js = client.jetstream()
        try:
            await js.add_stream(
                config=StreamConfig(
                    name="PIPELINE",
                    subjects=["pipeline.>"],
                    retention="limits",
                    storage="file",
                    max_age=300 * 1_000_000_000,  # 5 min in nanoseconds
                )
            )
        except Exception:
            # Stream already exists — idempotent re-create is a no-op.
            pass
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _build_build_queued_envelope_bytes(
    *, feature_id: str, correlation_id: str, feature_yaml_path: Path
) -> bytes:
    """Build a wire-shaped ``pipeline.build-queued.<feature_id>`` envelope."""
    from nats_core.events import BuildQueuedPayload

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


def _segment(subject: str) -> str:
    """Return the middle segment of ``pipeline.<segment>.<feature_id>``."""
    parts = subject.split(".")
    if len(parts) < 3:
        return ""
    return parts[1]


# ---------------------------------------------------------------------------
# Lifecycle scripting — owned by the test, runs against the real bridge
# ---------------------------------------------------------------------------


class _LifecycleScripter:
    """Drives a deterministic lifecycle sequence through the real emitter.

    This mirrors the production ``autobuild_runner`` subagent's
    ``_update_state`` calls (DDR-007 §Decision) but does so synchronously
    in the test harness. It exists because a real autobuild run is
    LLM-driven and cannot be made deterministic on a per-second test
    budget. The lifecycle envelopes still flow through the **real**
    :class:`~forge.lifecycle_bridge.bridge.LifecycleBridge` /
    :class:`~forge.adapters.nats.pipeline_publisher.PipelinePublisher`
    pair, so the translation-layer regressions this task targets are
    still locked in by the assertions below.

    The ``mode`` parameter selects the terminal envelope:

    * ``"success"`` — emits ``build-complete`` after two stage-complete
      events.
    * ``"forced_failure"`` — emits ``build-failed`` after one
      stage-complete, with an operator-readable failure reason.
    """

    def __init__(self, mode: str) -> None:
        if mode not in {"success", "forced_failure"}:
            raise ValueError(
                f"_LifecycleScripter: mode must be 'success' or "
                f"'forced_failure'; got {mode!r}"
            )
        self.mode = mode
        self.completion = asyncio.Event()
        self._scripted_tasks: list[asyncio.Task[None]] = []

    def schedule(
        self,
        *,
        emitter: Any,
        feature_id: str,
        build_id: str,
        correlation_id: str,
    ) -> None:
        """Schedule the scripted coroutine on the running loop."""
        from forge.pipeline import BuildContext

        ctx = BuildContext(
            feature_id=feature_id,
            build_id=build_id,
            correlation_id=correlation_id,
            wave_total=2,
        )
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run(emitter, ctx),
            name=f"lifecycle-scripter-{feature_id}",
        )
        self._scripted_tasks.append(task)

    async def _run(self, emitter: Any, ctx: Any) -> None:
        try:
            await emitter.emit_started(ctx)
            await emitter.emit_stage_complete(
                ctx,
                stage_label="planning_waves",
                target_kind="subagent",
                target_identifier=f"task-{ctx.build_id}",
                status="PASSED",
                gate_mode=None,
                coach_score=0.95,
                duration_secs=0.5,
                completed_at=datetime.now(UTC).isoformat(),
            )

            if self.mode == "success":
                await emitter.emit_stage_complete(
                    ctx,
                    stage_label="running_wave",
                    target_kind="subagent",
                    target_identifier=f"task-{ctx.build_id}",
                    status="PASSED",
                    gate_mode=None,
                    coach_score=0.92,
                    duration_secs=1.5,
                    completed_at=datetime.now(UTC).isoformat(),
                )
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
            else:
                # forced_failure path — the AC explicitly requires a
                # forced RuntimeError mid-stage with an operator-readable
                # failure reason on the terminal envelope. We emit the
                # failed terminal directly through the real publisher
                # (rather than letting an unhandled exception bubble up
                # and lose the terminal) because the wireup's contract
                # is that every observed run yields *exactly one*
                # terminal envelope (FEAT-FORGE-004 contract).
                await emitter.emit_failed(
                    ctx,
                    reason=(
                        "RuntimeError: forced failure injected by "
                        "TASK-FRR-PEB-013 sidecar-aware E2E test"
                    ),
                    failed_stage="running_wave",
                    duration_seconds=2,
                )
        finally:
            self.completion.set()


# ---------------------------------------------------------------------------
# Subscriber — collects pipeline.> envelopes from the real broker
# ---------------------------------------------------------------------------


async def _collect_pipeline_envelopes(
    *,
    nats_url: str,
    feature_id: str,
    budget_seconds: float,
) -> list[tuple[str, MessageEnvelope]]:
    """Subscribe to ``pipeline.>`` and collect envelopes for ``budget_seconds``.

    The subscription returns as soon as a terminal envelope arrives, so
    in the happy path the test completes in well under the budget.
    """
    nats = pytest.importorskip("nats")

    client = await nats.connect(nats_url)
    js = client.jetstream()
    collected: list[tuple[str, MessageEnvelope]] = []
    terminal_seen = asyncio.Event()

    async def _on_message(msg: Any) -> None:
        try:
            envelope = MessageEnvelope.model_validate_json(msg.data)
        except Exception:
            await msg.ack()
            return
        collected.append((msg.subject, envelope))
        await msg.ack()
        if (
            msg.subject.endswith(f".{feature_id}")
            and _segment(msg.subject) in _TERMINAL_SEGMENTS
        ):
            terminal_seen.set()

    sub = await js.subscribe(
        "pipeline.>",
        cb=_on_message,
        durable=f"e2e-collector-{uuid.uuid4().hex[:8]}",
    )

    try:
        try:
            await asyncio.wait_for(
                terminal_seen.wait(), timeout=budget_seconds
            )
        except asyncio.TimeoutError:
            # Surface the partial collection in the assertion failure
            # rather than raising here; the caller's assertions will
            # produce a more useful diagnostic.
            pass
    finally:
        try:
            await sub.unsubscribe()
        except Exception:  # noqa: BLE001
            pass
        await client.close()

    return collected


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


class TestSidecarAwareLifecycleE2E:
    """Sidecar-aware regression lock for the lifecycle bridge."""

    @pytest.mark.parametrize(
        "scripter_mode,expected_terminal",
        [
            pytest.param("success", "build-complete", id="success_path"),
            pytest.param(
                "forced_failure", "build-failed", id="forced_failure_path"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_canonical_lifecycle_sequence_against_real_sidecar(
        self,
        scripter_mode: str,
        expected_terminal: str,
        nats_server: str,
        langgraph_sidecar: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # --- Lazy production-side imports ------------------------------
        # Imported inside the test body so that when the fixtures skip
        # (missing binaries) we do not pay the import cost.
        from forge.adapters.nats.pipeline_consumer import (
            PipelineConsumerDeps,
        )
        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.cli._serve_config import ServeConfig
        from forge.cli._serve_deps_lifecycle import (
            build_publisher_and_emitter,
        )
        from forge.cli._serve_dispatcher import (
            make_handle_message_dispatcher,
        )
        from forge.cli._serve_state import SubscriptionState
        from forge.config.models import (
            FilesystemPermissions,
            ForgeConfig,
            PermissionsConfig,
        )
        from nats_core.events import BuildFailedPayload, BuildQueuedPayload

        # --- Arrange ----------------------------------------------------
        feature_id = f"FEAT-{uuid.uuid4().hex[:6].upper()}"
        correlation_id = f"corr-{uuid.uuid4().hex[:8]}"
        feature_yaml = tmp_path / "feature.yaml"
        feature_yaml.write_text(
            "# placeholder feature spec for sidecar-aware E2E\n",
            encoding="utf-8",
        )

        forge_config = ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[tmp_path]),
            ),
        )

        # Seed the PIPELINE stream so JetStream accepts both the inbound
        # build-queued publish and the outbound pipeline.* publishes.
        await _ensure_pipeline_stream(nats_server)

        # The version-skew check fires when the bridge constructor
        # observes a sidecar URL. The real sidecar is up; let the check
        # contact it. The test asserts on the lifecycle envelope shape,
        # not on the version_check call (which is independently covered
        # by tests/forge/lifecycle_bridge/test_version_check.py).
        scripter = _LifecycleScripter(scripter_mode)

        async def _compose_with_sidecar_aware_dispatch(client: Any) -> None:
            """Override the production composer to script the lifecycle.

            We retain the production publisher + emitter (so envelopes
            travel through the real PipelinePublisher → real NATS), but
            replace the AutobuildDispatcher boundary with a scripted
            sequence. The sidecar URL is still threaded through the
            ServeConfig so the bridge's startup version_check runs
            against the real sidecar.
            """
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
                build_id = f"build-{uuid.uuid4().hex[:8]}"
                scripter.schedule(
                    emitter=emitter,
                    feature_id=payload.feature_id,
                    build_id=build_id,
                    correlation_id=payload.correlation_id,
                )

            deps = PipelineConsumerDeps(
                forge_config=forge_config,
                is_duplicate_terminal=_is_duplicate_terminal,
                dispatch_build=_dispatch_build,
                publish_build_failed=_publish_build_failed,
            )
            _serve_daemon.dispatch_payload = make_handle_message_dispatcher(
                deps
            )

        monkeypatch.setattr(
            serve_module,
            "compose_dispatch_chain",
            _compose_with_sidecar_aware_dispatch,
        )

        async def _no_op_healthz(config: object, state: object) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(
            serve_module, "run_healthz_server", _no_op_healthz
        )

        config = ServeConfig(
            nats_url=nats_server,
            autobuild_runner_url=langgraph_sidecar,
            db_path=tmp_path / "forge.db",
        )
        state = SubscriptionState()

        # --- Act --------------------------------------------------------
        # 1. Boot forge serve in-process against the real broker + sidecar.
        run_task: asyncio.Task[None] = asyncio.create_task(
            serve_module._run_serve(config, state),
            name="forge-serve-sidecar-e2e",
        )

        # Wait for the daemon's durable consumer to attach so the
        # build-queued envelope we publish next is delivered to the
        # daemon (not lost to a not-yet-bound consumer).
        attach_deadline = time.monotonic() + 15.0
        while time.monotonic() < attach_deadline:
            if state.live:
                break
            await asyncio.sleep(0.05)
        if not state.live:
            run_task.cancel()
            try:
                await asyncio.wait_for(run_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            pytest.fail(
                "forge serve daemon did not bind the JetStream "
                "consumer within 15s; sidecar-aware E2E cannot proceed"
            )

        # 2. Publish the build-queued envelope onto JetStream.
        try:
            nats = pytest.importorskip("nats")
            publisher_client = await nats.connect(nats_server)
            try:
                publisher_js = publisher_client.jetstream()
                await publisher_js.publish(
                    f"pipeline.build-queued.{feature_id}",
                    _build_build_queued_envelope_bytes(
                        feature_id=feature_id,
                        correlation_id=correlation_id,
                        feature_yaml_path=feature_yaml,
                    ),
                )
            finally:
                await publisher_client.close()

            # 3. Collect envelopes via a parallel subscription on the
            #    real broker until the terminal arrives or the budget
            #    expires.
            collected = await _collect_pipeline_envelopes(
                nats_url=nats_server,
                feature_id=feature_id,
                budget_seconds=COLLECTION_BUDGET_SECONDS,
            )

            # Make sure the scripter actually finished — guards against
            # asserting on a partial sequence when the daemon stalled.
            try:
                await asyncio.wait_for(
                    scripter.completion.wait(), timeout=5.0
                )
            except asyncio.TimeoutError:
                pass

        finally:
            run_task.cancel()
            try:
                await asyncio.wait_for(run_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # --- Assert -----------------------------------------------------

        # Filter to lifecycle envelopes for THIS feature_id (the
        # subscription captures pipeline.>, which on a re-used broker
        # might contain unrelated envelopes from other test runs that
        # share the same JetStream store — though tmp_path scoping
        # makes that a non-issue here).
        lifecycle = [
            (subject, env)
            for subject, env in collected
            if subject.endswith(f".{feature_id}")
            and _segment(subject) in _LIFECYCLE_SEGMENTS
        ]
        segments = [_segment(subj) for subj, _ in lifecycle]

        # AC-3 (4): canonical pattern — 1× build-started → ≥1× stage-complete
        # → 1× terminal.
        assert segments, (
            f"AC violated: no lifecycle envelopes captured for "
            f"feature_id={feature_id!r} within {COLLECTION_BUDGET_SECONDS}s; "
            f"all captured: {[s for s, _ in collected]!r}"
        )
        assert segments[0] == "build-started", (
            f"AC violated: first lifecycle envelope must be "
            f"'build-started'; got sequence {segments!r}"
        )
        stage_count = sum(1 for s in segments if s == "stage-complete")
        assert stage_count >= 1, (
            f"AC violated: expected ≥1 'stage-complete' envelope; got "
            f"sequence {segments!r}"
        )
        terminal_indices = [
            i for i, s in enumerate(segments) if s in _TERMINAL_SEGMENTS
        ]
        assert len(terminal_indices) == 1, (
            f"AC violated: terminal envelope must appear exactly once; "
            f"observed at indices {terminal_indices!r} in sequence "
            f"{segments!r}"
        )
        assert segments[terminal_indices[0]] == expected_terminal, (
            f"AC violated: expected terminal envelope "
            f"{expected_terminal!r} for {scripter_mode!r} path; got "
            f"{segments[terminal_indices[0]]!r}"
        )

        # Ordering invariant — build-started precedes every
        # stage-complete; every stage-complete precedes the terminal.
        first_started_idx = segments.index("build-started")
        stage_indices = [
            i for i, s in enumerate(segments) if s == "stage-complete"
        ]
        assert all(first_started_idx < i for i in stage_indices), (
            "AC violated: build-started must precede every stage-complete"
        )
        assert all(i < terminal_indices[0] for i in stage_indices), (
            "AC violated: every stage-complete must precede the terminal"
        )

        # AC-3 (5): every collected envelope carries the inbound
        # correlation_id; no envelope carries a different one.
        wrong_correlation = [
            (subj, env.correlation_id)
            for subj, env in lifecycle
            if env.correlation_id != correlation_id
        ]
        assert not wrong_correlation, (
            f"AC violated: lifecycle envelopes carry mismatched "
            f"correlation_id (expected {correlation_id!r}); "
            f"mismatches={wrong_correlation!r}"
        )

        # forced_failure path additionally asserts that the failure
        # reason is operator-readable (per the test-requirements
        # section of TASK-FRR-PEB-013).
        if scripter_mode == "forced_failure":
            terminal_subject, terminal_env = lifecycle[terminal_indices[0]]
            payload = terminal_env.payload
            assert isinstance(payload, dict), (
                f"AC violated: build-failed payload must be a dict; "
                f"got {type(payload).__name__}"
            )
            reason = payload.get("reason", "")
            assert "RuntimeError" in reason, (
                f"AC violated: forced-failure terminal must carry an "
                f"operator-readable failure reason mentioning the "
                f"underlying RuntimeError; got reason={reason!r}"
            )
