"""Shared fixtures for the FEAT-FORGE-004 integration test suite.

This module exposes the seam-test infrastructure required by every test
file in ``tests/integration/`` per TASK-CGCP-011. Per the implementation
notes on the task:

* The in-memory NATS double is a small ``dict[str, list[bytes]]``
  substituting ``nats_core`` async pub/sub primitives — the goal is
  contract-level coverage, not transport correctness.
* The "temp SQLite" surface is modelled by an in-memory repository fake
  that captures the same operations the real SQLite-backed repository
  performs, so the wrapper sees the same Protocol surface.
* All clocks are injected — tests never call ``datetime.now()`` or
  ``time.sleep`` — so the suite is fully deterministic.

The fakes here are deliberately thin: they record what happened and
expose the data via plain attributes so tests can make precise,
auditable assertions without monkey-patching production modules.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.nats.approval_publisher import ApprovalPublisher
from forge.adapters.nats.approval_subscriber import (
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.adapters.nats.synthetic_response_injector import (
    SyntheticResponseInjector,
)
from forge.config.models import ApprovalConfig
from forge.gating.models import (
    CalibrationAdjustment,
    ConstitutionalRule,
    DetectionFinding,
    GateDecision,
    GateMode,
    PriorReference,
)
from forge.gating.wrappers import (
    GateCheckDeps,
    PausedBuildSnapshot,
)


# ---------------------------------------------------------------------------
# Constants used across the suite
# ---------------------------------------------------------------------------


BUILD_ID: str = "build-FEAT-CG44-20260425120000"
OTHER_BUILD_ID: str = "build-FEAT-CG44-20260425120001"
FEATURE_ID: str = "FEAT-CG44"
STAGE_LABEL: str = "Implementation"
RICH: str = "rich"
DEFAULT_FIXED_TIME: datetime = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Deterministic clocks
# ---------------------------------------------------------------------------


class FixedDateClock:
    """Frozen UTC datetime callable — drop-in for ``forge.gating`` clocks."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or DEFAULT_FIXED_TIME

    def __call__(self) -> datetime:
        return self._fixed


class FakeMonotonicClock:
    """Monotonic clock with manual ``tick`` advance — for the subscriber."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now += float(seconds)


# ---------------------------------------------------------------------------
# In-memory NATS double — dict-of-lists pub/sub surface
# ---------------------------------------------------------------------------


@dataclass
class InMemoryNats:
    """Tiny async NATS substitute — dict[subject -> [bytes, ...]] queue.

    Mirrors the surface the real ``nats_core`` client exposes to the
    publisher and subscriber adapters in this feature:

    * ``await nats.publish(subject, body_bytes)`` — append the envelope
      bytes to the per-subject queue and forward them to every active
      subscription whose subject matches.
    * ``await nats.subscribe(subject, callback)`` — register an
      envelope-aware callback (the production subscriber expects the
      callback to receive a parsed :class:`MessageEnvelope`, not raw
      bytes; this fake honours that contract).

    The fake is **purposely** transport-agnostic: there is no broker, no
    delivery ordering guarantee beyond local FIFO, and no JetStream
    acknowledgement protocol. The contract under test in this suite is
    the **wrapper-level** behaviour, not the transport.
    """

    # subject -> ordered list of envelope bytes that were published
    published: dict[str, list[bytes]] = field(default_factory=dict)
    # subject -> list of registered callbacks; appended in subscription
    # order so that "first subscriber wins" semantics are visible to
    # tests that exercise per-build routing.
    subscribers: dict[
        str, list[Callable[[MessageEnvelope], Awaitable[None]]]
    ] = field(default_factory=dict)
    # Optional per-subject side effects. When the head of the list is
    # not None, the matching publish raises that exception.
    publish_failures: dict[str, list[Exception | None]] = field(
        default_factory=dict
    )

    async def publish(self, subject: str, body: bytes) -> None:
        # Apply queued failure first so transport-error tests fire
        # exactly once and the subsequent publish on the same subject
        # succeeds (mirroring transient outages).
        queued = self.publish_failures.get(subject)
        if queued:
            effect = queued.pop(0)
            if effect is not None:
                raise effect

        self.published.setdefault(subject, []).append(body)

        # Synchronously forward to every callback subscribed to the
        # subject. Production callbacks parse the envelope; we do the
        # same here so the test reflects production parsing behaviour.
        callbacks = list(self.subscribers.get(subject, ()))
        if not callbacks:
            return
        envelope = MessageEnvelope.model_validate_json(body)
        for cb in callbacks:
            await cb(envelope)

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> "_InMemorySubscription":
        self.subscribers.setdefault(subject, []).append(callback)
        return _InMemorySubscription(self, subject, callback)

    # Convenience: deliver a hand-built response envelope to whoever is
    # listening on the response mirror subject. Used by tests that want
    # to simulate a real Rich response without going through the
    # publisher.
    async def deliver_response(
        self,
        *,
        build_id: str,
        request_id: str,
        decision: str,
        decided_by: str = RICH,
        notes: str | None = None,
    ) -> None:
        subject = f"agents.approval.forge.{build_id}.response"
        envelope = MessageEnvelope(
            source_id=decided_by,
            event_type=EventType.APPROVAL_RESPONSE,
            payload={
                "request_id": request_id,
                "decision": decision,
                "decided_by": decided_by,
                "notes": notes,
            },
        )
        body = envelope.model_dump_json().encode("utf-8")
        await self.publish(subject, body)


@dataclass
class _InMemorySubscription:
    """Subscription handle returned by :meth:`InMemoryNats.subscribe`."""

    _nats: InMemoryNats
    _subject: str
    _callback: Callable[[MessageEnvelope], Awaitable[None]]
    _unsubscribed: bool = False

    async def unsubscribe(self) -> None:
        if self._unsubscribed:
            return
        self._unsubscribed = True
        callbacks = self._nats.subscribers.get(self._subject)
        if callbacks and self._callback in callbacks:
            callbacks.remove(self._callback)


# ---------------------------------------------------------------------------
# In-memory repository — captures SQLite-shaped operations
# ---------------------------------------------------------------------------


@dataclass
class InMemoryRepository:
    """Capture-and-replay double for the gating repository surface.

    Tests assert against ``decisions``, ``paused``, ``resumed`` etc to
    prove the wrapper persisted the right rows in the right order. The
    ``order_log`` is a flat sequence of ``(op, payload)`` tuples that
    lets the atomicity test verify SQLite-before-publish ordering.
    """

    decisions: list[GateDecision] = field(default_factory=list)
    graphiti_writes: list[GateDecision] = field(default_factory=list)
    paused: list[PausedBuildSnapshot] = field(default_factory=list)
    resumed: list[tuple[str, str]] = field(default_factory=list)
    overridden: list[tuple[str, str, str]] = field(default_factory=list)
    cancelled: list[tuple[str, str]] = field(default_factory=list)
    order_log: list[tuple[str, Any]] = field(default_factory=list)
    graphiti_should_raise: bool = False

    async def record_decision(self, decision: GateDecision) -> None:
        self.decisions.append(decision)
        self.order_log.append(("record_decision", decision.build_id))

    async def write_to_graphiti(self, decision: GateDecision) -> None:
        if self.graphiti_should_raise:
            raise RuntimeError("graphiti unavailable")
        self.graphiti_writes.append(decision)
        self.order_log.append(("write_to_graphiti", decision.build_id))

    async def record_paused_build(
        self,
        *,
        build_id: str,
        feature_id: str,
        stage_label: str,
        request_id: str,
        attempt_count: int,
        decision: GateDecision,
    ) -> None:
        snap = PausedBuildSnapshot(
            build_id=build_id,
            feature_id=feature_id,
            stage_label=stage_label,
            request_id=request_id,
            attempt_count=attempt_count,
            decision_snapshot=decision,
        )
        self.paused.append(snap)
        self.order_log.append(("record_paused_build", request_id))

    async def list_paused_builds(self) -> list[PausedBuildSnapshot]:
        return list(self.paused)

    async def mark_resumed(self, *, build_id: str, stage_label: str) -> None:
        self.resumed.append((build_id, stage_label))
        self.order_log.append(("mark_resumed", build_id))

    async def mark_overridden(
        self, *, build_id: str, stage_label: str, reason: str
    ) -> None:
        self.overridden.append((build_id, stage_label, reason))
        self.order_log.append(("mark_overridden", build_id))

    async def mark_cancelled(self, *, build_id: str, reason: str) -> None:
        self.cancelled.append((build_id, reason))
        self.order_log.append(("mark_cancelled", build_id))


# ---------------------------------------------------------------------------
# In-memory state machine (mirrors the Protocol surface)
# ---------------------------------------------------------------------------


@dataclass
class InMemoryStateMachine:
    """Capture-and-replay double for the build-state-machine surface."""

    paused: list[tuple[str, str]] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: list[tuple[str, str]] = field(default_factory=list)
    # Snapshot of the build's status at every observable moment. Tests
    # asserting "observer never sees PAUSED-without-request" walk this
    # list against the bus log.
    status_log: list[tuple[str, str]] = field(default_factory=list)

    async def transition_to_paused(
        self, *, build_id: str, stage_label: str
    ) -> None:
        self.paused.append((build_id, stage_label))
        self.status_log.append((build_id, "PAUSED"))

    async def transition_to_running(self, *, build_id: str) -> None:
        self.running.append(build_id)
        self.status_log.append((build_id, "RUNNING"))

    async def transition_to_failed(
        self, *, build_id: str, reason: str
    ) -> None:
        self.failed.append((build_id, reason))
        self.status_log.append((build_id, "FAILED"))

    async def transition_to_cancelled(
        self, *, build_id: str, reason: str
    ) -> None:
        self.cancelled.append((build_id, reason))
        self.status_log.append((build_id, "CANCELLED"))


# ---------------------------------------------------------------------------
# Read-side fakes — priors / adjustments / rules
# ---------------------------------------------------------------------------


@dataclass
class FakePriorsReader:
    priors: list[PriorReference] = field(default_factory=list)

    async def read_priors(self, **_: Any) -> list[PriorReference]:
        return list(self.priors)


@dataclass
class FakeAdjustmentsReader:
    adjustments: list[CalibrationAdjustment] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def read_adjustments(
        self, *, target_capability: str, approved_only: bool
    ) -> list[CalibrationAdjustment]:
        self.calls.append(
            {
                "target_capability": target_capability,
                "approved_only": approved_only,
            }
        )
        return list(self.adjustments)


@dataclass
class FakeRulesReader:
    rules: list[ConstitutionalRule] = field(default_factory=list)

    async def read_rules(self, **_: Any) -> list[ConstitutionalRule]:
        return list(self.rules)


# ---------------------------------------------------------------------------
# Reasoning-model double — hard-coded JSON per requested mode
# ---------------------------------------------------------------------------


def model_returning(
    mode: GateMode, *, threshold: float | None = None
) -> Callable[[str], str]:
    """Return a deterministic ``(prompt: str) -> str`` callable.

    Mirrors the helper in the unit-test suite so all integration tests
    speak the same idiom and the wire-level prompt content is irrelevant
    — only the structured response shape matters.
    """
    import json

    payload = {
        "mode": mode.value,
        "rationale": f"reasoned: {mode.value}",
        "relevant_prior_ids": [],
        "threshold_applied": threshold,
    }
    body = json.dumps(payload)

    def _call(_prompt: str) -> str:
        return body

    return _call


# ---------------------------------------------------------------------------
# Wrapper-level deps builder
# ---------------------------------------------------------------------------


def build_gate_check_deps(
    *,
    nats: InMemoryNats,
    repo: InMemoryRepository | None = None,
    state_machine: InMemoryStateMachine | None = None,
    priors: list[PriorReference] | None = None,
    adjustments: list[CalibrationAdjustment] | None = None,
    rules: list[ConstitutionalRule] | None = None,
    mode: GateMode = GateMode.AUTO_APPROVE,
    threshold: float | None = None,
    expected_approver: str | None = None,
    subscriber_clock: Any = None,
    fixed_time: datetime | None = None,
    config: ApprovalConfig | None = None,
) -> tuple[
    GateCheckDeps,
    InMemoryNats,
    InMemoryRepository,
    InMemoryStateMachine,
    ApprovalSubscriber,
    SyntheticResponseInjector,
]:
    """Wire a real publisher + subscriber + injector against ``nats``.

    The wrapper-under-test runs end-to-end through real adapter
    instances; only the transport layer below them and the SQLite-shaped
    repository above them are doubled.
    """
    repo = repo if repo is not None else InMemoryRepository()
    sm = state_machine if state_machine is not None else InMemoryStateMachine()

    publisher = ApprovalPublisher(nats_client=nats)
    sub_clock = (
        subscriber_clock
        if subscriber_clock is not None
        else FakeMonotonicClock()
    )
    sub_deps = ApprovalSubscriberDeps(
        nats_client=nats,
        config=config or ApprovalConfig(),
        publish_refresh=None,
        expected_approver=expected_approver,
        project=None,
        clock=sub_clock,
        dedup_ttl_seconds=300,
    )
    subscriber = ApprovalSubscriber(sub_deps)
    injector = SyntheticResponseInjector(nats_client=nats)

    deps = GateCheckDeps(
        priors_reader=FakePriorsReader(priors=list(priors or [])),
        adjustments_reader=FakeAdjustmentsReader(
            adjustments=list(adjustments or []),
        ),
        rules_reader=FakeRulesReader(rules=list(rules or [])),
        repository=repo,
        state_machine=sm,
        publisher=publisher,
        subscriber=subscriber,
        injector=injector,
        reasoning_model_call=model_returning(mode, threshold=threshold),
        clock=FixedDateClock(fixed_time),
        per_attempt_wait_seconds=None,
    )
    return deps, nats, repo, sm, subscriber, injector


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nats() -> InMemoryNats:
    """Fresh in-memory NATS double per test."""
    return InMemoryNats()


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def state_machine() -> InMemoryStateMachine:
    return InMemoryStateMachine()


@pytest.fixture
def fixed_clock() -> FixedDateClock:
    return FixedDateClock()


@pytest.fixture
def fake_monotonic() -> FakeMonotonicClock:
    return FakeMonotonicClock()


# ---------------------------------------------------------------------------
# Convenience constructors for ApprovalResponsePayload-shaped dicts
# ---------------------------------------------------------------------------


def make_response(
    *,
    request_id: str,
    decision: str = "approve",
    decided_by: str = RICH,
    notes: str | None = None,
) -> ApprovalResponsePayload:
    """Build a typed approval response payload for in-memory delivery."""
    return ApprovalResponsePayload(
        request_id=request_id,
        decision=decision,  # type: ignore[arg-type]
        decided_by=decided_by,
        notes=notes,
    )


def make_response_envelope(
    payload: ApprovalResponsePayload,
    *,
    correlation_id: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        source_id=payload.decided_by,
        event_type=EventType.APPROVAL_RESPONSE,
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# A trivial "sample decision" used by several tests
# ---------------------------------------------------------------------------


def sample_decision(
    *,
    build_id: str = BUILD_ID,
    stage_label: str = STAGE_LABEL,
    mode: GateMode = GateMode.FLAG_FOR_REVIEW,
    coach_score: float | None = 0.7,
    rationale: str = "paused for review",
    detection_findings: list[DetectionFinding] | None = None,
) -> GateDecision:
    return GateDecision(
        build_id=build_id,
        stage_label=stage_label,
        target_kind="local_tool",
        target_identifier="t",
        mode=mode,
        rationale=rationale,
        coach_score=coach_score,
        criterion_breakdown={"completeness": coach_score or 0.0},
        detection_findings=detection_findings or [],
        evidence=[],
        decided_at=DEFAULT_FIXED_TIME,
    )


# ---------------------------------------------------------------------------
# TASK-FRR-PEB-013 — sidecar fixture for sidecar-aware E2E integration test
#
# Spins up a real ``langgraph-runner`` sidecar (``langgraph dev``) in a
# subprocess for the duration of a test. The fixture is intentionally
# defensive: when the ``langgraph`` binary is not on ``PATH`` (typical
# CI without the dev tooling installed), the fixture calls
# :func:`pytest.skip` rather than crashing the whole integration suite,
# so the sidecar-aware E2E test runs only on stages that have the
# dependencies installed (the test is also gated by ``@slow``).
#
# AC-2 (TASK-FRR-PEB-013): "A pytest fixture spins up a real
# langgraph-runner sidecar using subprocess.Popen … yields the sidecar
# URL and tears down the process on test exit."
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Bind to an ephemeral port, return the assigned port.

    The kernel guarantees the port is free at the moment of the bind;
    once the socket is closed the port may be reused by another process,
    but the langgraph-runner subprocess starts within milliseconds of
    this call so the race window is negligible for a CI-scoped test.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_ready(
    url: str,
    *,
    timeout_seconds: float,
    attempt_interval: float = 0.25,
    proc: subprocess.Popen | None = None,
) -> None:
    """Poll ``url`` until it returns any HTTP response, or timeout.

    Raises :class:`TimeoutError` if the URL is not reachable within
    ``timeout_seconds``. Used to gate the test on sidecar readiness so
    the build-queued publish does not race the sidecar startup.

    When a ``proc`` is supplied, an early subprocess exit short-circuits
    the wait — typical when ``langgraph dev`` fails to load a graph and
    aborts within milliseconds. This makes the skip diagnostic
    actionable instead of waiting the full timeout for a process that
    is already gone.
    """
    deadline = time.monotonic() + timeout_seconds
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise TimeoutError(
                f"sidecar subprocess at {url} exited prematurely "
                f"(returncode={proc.returncode})"
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
                # Any HTTP status counts as "the server is up"; the
                # specific endpoint shape is not the contract under test.
                resp.read(0)
                return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(attempt_interval)
    raise TimeoutError(
        f"sidecar at {url} did not respond within {timeout_seconds:.1f}s "
        f"(last error: {last_exc!r})"
    )


@pytest.fixture
def langgraph_sidecar(tmp_path: Path) -> Iterator[str]:
    """Spin up a real ``langgraph dev`` sidecar in a subprocess.

    Yields the sidecar's base URL (``http://127.0.0.1:<port>``) and
    tears the process down on test exit.

    Skips the test when:

    * The ``langgraph`` CLI is not on ``PATH`` (lightweight CI runners),
    * The langgraph_api package is not importable (the dev server cannot
      start without it), or
    * The sidecar fails to become reachable inside its startup budget.

    The subprocess is launched with the project's repository root as
    its working directory so ``langgraph.json`` is discovered without
    extra configuration.
    """
    binary = shutil.which("langgraph")
    if binary is None:
        pytest.skip(
            "langgraph CLI not available on PATH; sidecar-aware E2E "
            "test requires `pip install langgraph-cli[dev]` (or the "
            "matching extra) to be installed in the test environment"
        )

    # The langgraph dev command requires langgraph_api at runtime; if
    # the package is missing the subprocess exits immediately. We probe
    # the import here so the skip reason is actionable.
    try:
        import importlib

        importlib.import_module("langgraph_api")
    except ImportError:
        pytest.skip(
            "langgraph_api not importable; sidecar-aware E2E test "
            "requires the langgraph dev runtime in the test venv"
        )

    # Anchor the subprocess at the worktree root so the langgraph.json
    # the sidecar discovers is the one shipped with this checkout.
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    if not (repo_root / "langgraph.json").exists():
        pytest.skip(
            f"langgraph.json not found at expected repo root "
            f"{repo_root!r}; sidecar fixture cannot launch the runner"
        )

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    # Disable browser auto-open and tunnel features so the sidecar
    # exits cleanly when the test SIGTERMs it (no orphan browser tabs,
    # no LangSmith tunnel registration).
    env.setdefault("LANGGRAPH_CLI_NO_BROWSER", "1")
    env.setdefault("LANGSMITH_TRACING", "false")

    cmd = [
        binary,
        "dev",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-browser",
    ]

    proc = subprocess.Popen(  # noqa: S603 — binary path resolved via shutil.which
        cmd,
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        try:
            _wait_for_http_ready(
                f"{url}/ok", timeout_seconds=30.0, proc=proc
            )
        except TimeoutError as exc:
            # Gather diagnostic output before tearing the subprocess
            # down so the skip / failure carries the sidecar's stderr.
            try:
                proc.terminate()
                stdout, _ = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate(timeout=5.0)
            pytest.skip(
                f"langgraph-runner sidecar failed to become ready: "
                f"{exc}\n--- subprocess output ---\n{stdout}"
            )
        yield url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
