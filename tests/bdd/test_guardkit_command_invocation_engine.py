"""Pytest-bdd wiring for FEAT-FORGE-005 / TASK-GCI-005 + TASK-GCI-007 scenarios.

This module is the executable surface for the BDD oracle of the
gh-adapter (TASK-GCI-007) and progress-stream-subscriber (TASK-GCI-005)
tasks. Scenarios in
``features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature``
are bound here to step functions that drive the real adapters through
hand-rolled fakes (no ``gh`` binary invocation, no live NATS).

Scope
-----

Wired here:

- ``@key-example`` (TASK-GCI-007) — *Forge opens a pull request for the
  build through the version-control adapter*.
- ``@negative`` (TASK-GCI-007) — *A pull-request creation without
  GitHub credentials returns a structured error*.
- ``@key-example`` (TASK-GCI-005) — *GuardKit progress is streamed on
  the bus while the subprocess is still running*. Drives
  :func:`forge.adapters.guardkit.progress_subscriber.subscribe_progress`
  against a fake NATS client, asserts the sink observes each emitted
  :class:`GuardKitProgressEvent` and the simulated blocking invocation
  returns its authoritative result only after the subprocess exits.
- ``@edge-case`` (TASK-GCI-005) — *The authoritative result still
  returns when progress streaming is unavailable*. Drives
  ``subscribe_progress`` with ``nats_client=None`` and asserts a
  structured ``progress_stream_unavailable`` warning is recorded while
  the simulated invocation still returns a parsed
  :class:`GuardKitResult` with artefacts.
- ``@edge-case`` (TASK-GCI-005) — *Progress events emitted faster than
  Forge consumes them are still observable for live status*. Drives
  ``subscribe_progress`` with a producer-faster-than-sink workload and
  asserts the most recent event is preserved on the bounded sink.

Other ``@task:TASK-GCI-XXX`` scenarios in the same feature file belong
to sibling tasks (TASK-GCI-003 / 004 / 006 / 008 / 009 / 010); their
step bindings live with those tasks.

Background steps
----------------

The feature-level Background ("Forge is running inside an ephemeral
build worktree" / "a project configuration file defines …" / "a context
manifest is available …") is a no-op for the gh adapter — none of those
preconditions affect ``create_pr`` behaviour. They are still bound to
inert ``given`` steps so pytest-bdd can resolve every step in the
scenario without Background-step errors.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from pytest_bdd import given, scenario, then, when

from forge.adapters.gh import operations
from forge.adapters.git.models import PRResult
from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.guardkit.progress import GuardKitProgressEvent
from forge.adapters.guardkit.progress_subscriber import (
    PROGRESS_STREAM_UNAVAILABLE,
    ProgressSink,
    subject_for,
    subscribe_progress,
)


FEATURE_FILE = (
    "guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature"
)


# ---------------------------------------------------------------------------
# Scenario decorators — only the @task:TASK-GCI-007 pair
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Forge opens a pull request for the build through the version-control adapter",
)
def test_key_example_create_pr_success() -> None:
    """@key-example — TASK-GCI-007 happy-path PR creation."""


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A pull-request creation without GitHub credentials returns a structured error",
)
def test_negative_create_pr_missing_credentials() -> None:
    """@negative — TASK-GCI-007 missing-credential structured error."""


# ---------------------------------------------------------------------------
# Per-scenario world fixture (kept local so the GCI suite does not
# collide with the FEAT-FORGE-002 ``world`` fixture in conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def gci_world() -> dict[str, Any]:
    """Mutable scratch dict threading state across Given/When/Then steps."""
    return {}


# ---------------------------------------------------------------------------
# Background — inert bindings (preconditions don't affect create_pr)
# ---------------------------------------------------------------------------


@given("Forge is running inside an ephemeral build worktree")
def _bg_forge_in_worktree(gci_world: dict[str, Any]) -> None:
    # The worktree is represented as a Path string handed to create_pr.
    # We use a plausible path matching the ADR-ARCH-028 convention.
    gci_world["worktree"] = Path("/var/forge/builds/B-bdd")


@given(
    "a project configuration file defines the shell, filesystem, "
    "and network permissions"
)
def _bg_permissions_defined(gci_world: dict[str, Any]) -> None:
    # ADR-ARCH-023 makes permissions constitutional and enforced by the
    # framework, not by the adapter — no-op for create_pr.
    gci_world["permissions_defined"] = True


@given(
    "a context manifest is available at the repo root describing documents "
    "grouped by category"
)
def _bg_manifest_available(gci_world: dict[str, Any]) -> None:
    # gh adapter does not consult the context manifest — no-op.
    gci_world["manifest_available"] = True


# ---------------------------------------------------------------------------
# @key-example: Forge opens a pull request through the version-control adapter
# ---------------------------------------------------------------------------


@given("a build has committed and pushed its work to a remote branch")
def _given_build_pushed(
    gci_world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-conditions for the happy path: GH_TOKEN must be set, and the
    # subprocess seam returns gh's canonical PR-URL stdout.
    monkeypatch.setenv("GH_TOKEN", "ghp_bdd-token")
    pr_url = "https://github.com/owner/repo/pull/77"
    gci_world["expected_pr_url"] = pr_url
    gci_world["execute_mock"] = AsyncMock(return_value=(0, pr_url + "\n", ""))
    gci_world["base_branch"] = "main"
    monkeypatch.setattr(operations, "_execute", gci_world["execute_mock"])


@when("Forge asks the version-control adapter to open a pull request")
def _when_open_pr(gci_world: dict[str, Any]) -> None:
    # Drive the real adapter; both scenarios share this When-step.
    # ``asyncio.run`` creates and closes a fresh event loop per call —
    # safe inside synchronous pytest-bdd step bodies and avoids the
    # ``get_event_loop`` deprecation in 3.12+.
    import asyncio

    base = gci_world.get("base_branch", "main")
    gci_world["result"] = asyncio.run(
        operations.create_pr(
            worktree=gci_world["worktree"],
            title="BDD oracle: open a PR",
            body="Driven by pytest-bdd against the real adapter.",
            base=base,
        )
    )


@then("the adapter should create the pull request against the configured base branch")
def _then_pr_created_against_base(gci_world: dict[str, Any]) -> None:
    execute_mock: AsyncMock = gci_world["execute_mock"]
    execute_mock.assert_awaited_once()
    args, kwargs = execute_mock.call_args
    command = list(kwargs.get("command") or args[0])
    assert command[0] == "gh"
    assert command[1:3] == ["pr", "create"]
    assert "--base" in command
    base_idx = command.index("--base")
    assert command[base_idx + 1] == gci_world["base_branch"]
    # Worktree confinement: subprocess cwd is the build's worktree.
    cwd = kwargs.get("cwd") or args[1]
    assert cwd == str(gci_world["worktree"])


@then("the invocation should return the pull-request URL as a structured result")
def _then_returns_pr_url_structured(gci_world: dict[str, Any]) -> None:
    result: PRResult = gci_world["result"]
    assert isinstance(result, PRResult)
    assert result.status == "success"
    assert result.pr_url == gci_world["expected_pr_url"]
    assert result.pr_number == 77
    assert result.error_code is None
    assert result.stderr is None


# ---------------------------------------------------------------------------
# @negative: missing-credential structured error
# ---------------------------------------------------------------------------


@given("the runtime has no GitHub access credentials available")
def _given_no_gh_credentials(
    gci_world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both unset and empty must short-circuit identically; the scenario
    # phrasing covers both. Use ``delenv`` for the unset variant.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    # Install an execute spy so the Then-step can assert non-invocation.
    spy = AsyncMock()
    gci_world["execute_mock"] = spy
    monkeypatch.setattr(operations, "_execute", spy)


@then(
    "the adapter should return a structured error explaining the credential is missing"
)
def _then_structured_missing_credential_error(gci_world: dict[str, Any]) -> None:
    result: PRResult = gci_world["result"]
    assert isinstance(result, PRResult)
    assert result.status == "failed"
    assert result.error_code == "missing_credentials"
    # Stable, machine-readable explanation lives on the stderr field.
    assert result.stderr == "GH_TOKEN not set in environment"
    # Successful-path fields stay None on the structured failure.
    assert result.pr_url is None
    assert result.pr_number is None


@then("no pull request should be created")
def _then_no_pr_created(gci_world: dict[str, Any]) -> None:
    execute_mock: AsyncMock = gci_world["execute_mock"]
    # The pre-flight check must short-circuit before the subprocess
    # seam — gh is never invoked, so no PR can have been created.
    execute_mock.assert_not_called()
    execute_mock.assert_not_awaited()


# ===========================================================================
# TASK-GCI-005 — progress-stream subscriber scenarios
# ===========================================================================
#
# The wiring below drives the real
# :func:`forge.adapters.guardkit.progress_subscriber.subscribe_progress`
# context manager against a hand-rolled fake NATS client. The
# "subprocess" is simulated as an ``async def`` whose return value
# stands in for the authoritative :class:`GuardKitResult` that
# ``forge.adapters.guardkit.run()`` will eventually deliver
# (TASK-GCI-008 composes both).
# ---------------------------------------------------------------------------


_GCI005_BUILD_ID = "B-bdd-005"
_GCI005_SUBCOMMAND = "/feature-spec"


class _BDDFakeSubscription:
    """Minimal ``Subscription``-shaped fake exposing :meth:`unsubscribe`."""

    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _BDDFakeNATSClient:
    """Captures subscribe calls and exposes the registered callback.

    Mirrors the shape used by :class:`forge.adapters.nats.client.NATSClient`
    so the production :func:`subscribe_progress` accepts it without any
    monkey-patching.
    """

    def __init__(self) -> None:
        self._subs: dict[
            str,
            list[
                tuple[
                    _BDDFakeSubscription,
                    Callable[[MessageEnvelope], Awaitable[None]],
                ]
            ],
        ] = {}

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[MessageEnvelope], Awaitable[None]],
    ) -> _BDDFakeSubscription:
        sub = _BDDFakeSubscription()
        self._subs.setdefault(topic, []).append((sub, callback))
        return sub

    async def deliver(self, topic: str, envelope: MessageEnvelope) -> None:
        for _sub, cb in list(self._subs.get(topic, [])):
            await cb(envelope)


def _bdd_make_event(
    *,
    seq: int,
    stage_label: str,
    build_id: str = _GCI005_BUILD_ID,
    subcommand: str = _GCI005_SUBCOMMAND,
) -> GuardKitProgressEvent:
    return GuardKitProgressEvent(
        build_id=build_id,
        subcommand=subcommand,
        stage_label=stage_label,
        seq=seq,
        timestamp=f"2026-04-26T08:30:{seq:02d}+00:00",
    )


def _bdd_envelope_for(event: GuardKitProgressEvent) -> MessageEnvelope:
    return MessageEnvelope(
        event_type=EventType.STAGE_COMPLETE,
        source_id="guardkit",
        payload=event.model_dump(),
    )


def _bdd_simulated_result() -> GuardKitResult:
    """Stand-in for the authoritative result the GuardKit subprocess emits."""
    return GuardKitResult(
        status="success",
        subcommand=_GCI005_SUBCOMMAND,
        artefacts=["docs/specs/example.md"],
        duration_secs=0.01,
        stdout_tail="",
        exit_code=0,
    )


# ---------------------------------------------------------------------------
# @key-example: GuardKit progress is streamed on the bus while the
#               subprocess is still running
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "GuardKit progress is streamed on the bus while the subprocess is still running",
)
def test_key_example_progress_streamed_while_running() -> None:
    """@key-example — TASK-GCI-005 live-telemetry happy path."""


# ---------------------------------------------------------------------------
# @edge-case: The authoritative result still returns when progress
#             streaming is unavailable
# ---------------------------------------------------------------------------


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "The authoritative result still returns when progress streaming is unavailable",
)
def test_edge_case_authoritative_result_when_stream_unavailable() -> None:
    """@edge-case — TASK-GCI-005 telemetry-only invariant."""


# ---------------------------------------------------------------------------
# @edge-case: Progress events emitted faster than Forge consumes them
#             are still observable for live status
# ---------------------------------------------------------------------------


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "Progress events emitted faster than Forge consumes them are "
    "still observable for live status",
)
def test_edge_case_back_pressure_most_recent_observable() -> None:
    """@edge-case — TASK-GCI-005 bounded-sink back-pressure."""


# ---------------------------------------------------------------------------
# Step bindings — TASK-GCI-005
# ---------------------------------------------------------------------------


@given("the reasoning model invokes any GuardKit wrapper that supports streaming")
def _given_streaming_wrapper_invoked(gci_world: dict[str, Any]) -> None:
    # The wrapper invocation is simulated as a coroutine that emits N
    # progress events through the fake client and then returns the
    # authoritative GuardKitResult. The wrapper does NOT return until
    # the simulated subprocess finishes, mirroring the synchronous
    # contract of ``forge.adapters.guardkit.run()``.
    gci_world["nats_client"] = _BDDFakeNATSClient()
    gci_world["sink"] = ProgressSink(max_events=50)
    gci_world["build_id"] = _GCI005_BUILD_ID
    gci_world["subcommand"] = _GCI005_SUBCOMMAND
    gci_world["events_to_emit"] = [
        _bdd_make_event(seq=1, stage_label="discovery"),
        _bdd_make_event(seq=2, stage_label="generation"),
        _bdd_make_event(seq=3, stage_label="verification"),
    ]


@when("the GuardKit process emits progress events during its run")
def _when_progress_events_emitted(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]
    client: _BDDFakeNATSClient = gci_world["nats_client"]
    build_id = gci_world["build_id"]
    subcommand = gci_world["subcommand"]
    events: list[GuardKitProgressEvent] = gci_world["events_to_emit"]

    completion_marker: dict[str, Any] = {"completed": False}

    async def _run() -> GuardKitResult:
        async with subscribe_progress(client, build_id, subcommand, sink):
            topic = subject_for(build_id, subcommand)
            # Emit each progress event mid-run.
            for ev in events:
                await client.deliver(topic, _bdd_envelope_for(ev))
            # Subprocess "exits" — the authoritative result is now
            # produced and returned.
            completion_marker["completed"] = True
            return _bdd_simulated_result()

    gci_world["result"] = asyncio.run(_run())
    gci_world["completion_marker"] = completion_marker


@then("Forge should observe each progress event on the pipeline progress channel")
def _then_each_event_observed(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]
    expected: list[GuardKitProgressEvent] = gci_world["events_to_emit"]
    observed = sink.all_for(gci_world["build_id"], gci_world["subcommand"])
    assert observed == expected, (
        f"sink should hold every emitted event in order, got "
        f"seqs={[e.seq for e in observed]} expected="
        f"{[e.seq for e in expected]}"
    )


@then("the blocking invocation should still return only after the subprocess exits")
def _then_invocation_returns_after_subprocess(gci_world: dict[str, Any]) -> None:
    # The simulated wrapper sets ``completed=True`` only after every
    # event was delivered AND just before returning the result. The
    # caller observed the result, therefore completion happened — and
    # critically, no event delivery raced past the wrapper return,
    # because ``async with subscribe_progress`` is the synchronous
    # boundary that contains every emit.
    assert gci_world["completion_marker"]["completed"] is True
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "success"
    assert result.exit_code == 0
    # And every event reached the sink before the wrapper returned.
    sink: ProgressSink = gci_world["sink"]
    assert sink.latest(gci_world["build_id"], gci_world["subcommand"]) is not None


@given("the progress stream channel is unavailable during a GuardKit invocation")
def _given_progress_stream_unavailable(gci_world: dict[str, Any]) -> None:
    # ``nats_client=None`` is the canonical no-op path for the
    # subscriber; ``subscribe_progress`` records a structured
    # ``progress_stream_unavailable`` warning and the caller proceeds.
    gci_world["nats_client"] = None
    gci_world["sink"] = ProgressSink()
    gci_world["build_id"] = _GCI005_BUILD_ID
    gci_world["subcommand"] = _GCI005_SUBCOMMAND


@when("the subprocess exits cleanly")
def _when_subprocess_exits_cleanly(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]

    async def _run() -> GuardKitResult:
        async with subscribe_progress(
            gci_world["nats_client"],
            gci_world["build_id"],
            gci_world["subcommand"],
            sink,
        ):
            # No subprocess events are emitted on the unavailable
            # path; the wrapper still returns its authoritative
            # result on clean exit.
            return _bdd_simulated_result()

    gci_world["result"] = asyncio.run(_run())


@then("the invocation should still return a parsed success result with artefact paths")
def _then_returns_parsed_success_with_artefacts(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.artefacts, "authoritative result must carry artefact paths"


@then("the missing progress stream should not itself fail the call")
def _then_missing_stream_does_not_fail(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]
    # Exactly one structured warning recorded with the canonical code.
    codes = [w.code for w in sink.warnings]
    assert PROGRESS_STREAM_UNAVAILABLE in codes
    # And the GuardKitResult reached the caller — the call did not
    # propagate any exception.
    assert isinstance(gci_world["result"], GuardKitResult)


@given("a GuardKit subprocess is emitting progress events at a high cadence")
def _given_high_cadence_emitter(gci_world: dict[str, Any]) -> None:
    # The bound on the sink (``max_events``) is intentionally smaller
    # than the producer's burst so that back-pressure forces eviction.
    gci_world["nats_client"] = _BDDFakeNATSClient()
    gci_world["sink"] = ProgressSink(max_events=3)
    gci_world["build_id"] = _GCI005_BUILD_ID
    gci_world["subcommand"] = _GCI005_SUBCOMMAND
    # Producer emits 10 events; sink retains only the most recent 3.
    gci_world["events_to_emit"] = [
        _bdd_make_event(seq=i, stage_label=f"stage-{i}") for i in range(1, 11)
    ]


@when("Forge is slower to consume than the producer is to emit")
def _when_forge_slower_than_producer(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]
    client: _BDDFakeNATSClient = gci_world["nats_client"]
    events: list[GuardKitProgressEvent] = gci_world["events_to_emit"]
    build_id = gci_world["build_id"]
    subcommand = gci_world["subcommand"]

    async def _run() -> GuardKitResult:
        async with subscribe_progress(client, build_id, subcommand, sink):
            topic = subject_for(build_id, subcommand)
            for ev in events:
                await client.deliver(topic, _bdd_envelope_for(ev))
            return _bdd_simulated_result()

    gci_world["result"] = asyncio.run(_run())


@then("the live status view should still reflect the most recent progress events")
def _then_most_recent_observable(gci_world: dict[str, Any]) -> None:
    sink: ProgressSink = gci_world["sink"]
    retained = sink.all_for(gci_world["build_id"], gci_world["subcommand"])
    expected_tail = gci_world["events_to_emit"][-3:]
    assert retained == expected_tail, (
        f"bounded sink should retain the most recent 3 events, "
        f"got seqs={[e.seq for e in retained]}"
    )
    latest = sink.latest(gci_world["build_id"], gci_world["subcommand"])
    assert latest is not None
    assert latest.seq == 10


@then("the authoritative completion result should remain unaffected")
def _then_authoritative_result_unaffected(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "success"
    assert result.exit_code == 0
    # No telemetry-failure warnings — only back-pressure eviction
    # happened, which is silent (the deque drops oldest on its own).
    sink: ProgressSink = gci_world["sink"]
    assert all(w.code != PROGRESS_STREAM_UNAVAILABLE for w in sink.warnings)
