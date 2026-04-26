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


# ===========================================================================
# TASK-GCI-006 — git adapter scenarios
# ===========================================================================
#
# The two @task:TASK-GCI-006 scenarios in
# ``features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature``
# bind here. They drive the real
# :mod:`forge.adapters.git.operations` module against the same
# ``FakeExecute`` shape used by the unit tests, but with the BDD oracle's
# Given/When/Then ordering — so the Gherkin specification is executable
# end-to-end rather than only spot-checked by unit tests.
#
# Scenarios wired:
#
# - "A failed worktree cleanup is logged but does not prevent build
#    completion" — drives :func:`cleanup_worktree` against a fake
#    ``execute`` that exits non-zero, asserts the result is a structured
#    failure (not an exception) and that the WARNING-level log line was
#    emitted by the adapter so operators can see the cleanup miss.
#
# - "Shell metacharacters in subprocess arguments are passed as literal
#    tokens" — drives :func:`commit_all` with a commit message containing
#    ``;``, ``&&`` and quotes, asserts the recorded argv slot is the
#    *exact* same string identity-equal to the input (no shell expansion,
#    no splitting, no escaping).
# ---------------------------------------------------------------------------

import logging  # noqa: E402 — placed near step bindings that need it.

from forge.adapters.git import operations as git_operations  # noqa: E402
from forge.adapters.git.models import GitOpResult  # noqa: E402
from forge.adapters.git.operations import (  # noqa: E402
    ExecuteResult as GitExecuteResult,
)


class _GCI006FakeExecute:
    """Recording fake matching the production ``ExecuteCallable`` shape.

    Identical in spirit to ``tests/forge/adapters/git/test_operations.py``'s
    ``FakeExecute``, restated here so this BDD module stays self-contained
    (the unit-test helper is private to the unit-test module by
    convention; cross-importing it would couple two suites that are
    intentionally independent).
    """

    def __init__(
        self,
        *,
        responses: list[GitExecuteResult] | None = None,
    ) -> None:
        self.responses: list[GitExecuteResult] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        command: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> GitExecuteResult:
        self.calls.append({"command": list(command), "cwd": cwd, "timeout": timeout})
        if not self.responses:
            return GitExecuteResult(exit_code=0, stdout="", stderr="")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# @edge-case (TASK-GCI-006): A failed worktree cleanup is logged but does
#                            not prevent build completion
# ---------------------------------------------------------------------------


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "A failed worktree cleanup is logged but does not prevent build completion",
)
def test_edge_case_failed_cleanup_does_not_block_completion() -> None:
    """@edge-case — TASK-GCI-006 best-effort cleanup contract."""


@given("a build has reached a terminal state")
def _given_build_at_terminal_state(gci_world: dict[str, Any]) -> None:
    # The "terminal state" is represented as the build_id + worktree path
    # the state machine would hand to the adapter on the cleanup edge.
    # The state machine itself lives elsewhere; the contract under test
    # is: regardless of cleanup outcome, the function returns a
    # GitOpResult and never raises.
    gci_world["build_id"] = "build-cleanup-bdd"
    gci_world["worktree"] = Path("/var/forge/builds/build-cleanup-bdd")
    gci_world["terminal_marked"] = False  # state machine flips after cleanup returns


@when("the adapter attempts to delete the build's worktree")
def _when_adapter_attempts_delete(gci_world: dict[str, Any]) -> None:
    # Snapshot the FakeExecute the And-step will provide; storing the
    # exec on world keeps the When/And-steps decoupled.
    gci_world["execute"] = _GCI006FakeExecute(
        responses=[
            GitExecuteResult(
                exit_code=128,
                stdout="",
                stderr="fatal: worktree locked by another process\n",
            )
        ]
    )


@when("the deletion fails")
def _when_deletion_fails(
    gci_world: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # Capture the WARNING the adapter emits on best-effort failure.
    caplog.set_level(logging.WARNING, logger=git_operations.logger.name)

    async def _drive() -> GitOpResult:
        return await git_operations.cleanup_worktree(
            gci_world["build_id"],
            gci_world["worktree"],
            execute=gci_world["execute"],
        )

    gci_world["cleanup_result"] = asyncio.run(_drive())
    gci_world["caplog_records"] = list(caplog.records)
    # The state machine (caller) only flips the build to terminal-in-
    # history *after* cleanup_worktree returns — the contract under
    # test is that this assignment is reachable regardless of the
    # cleanup outcome (i.e. the adapter never raised).
    gci_world["terminal_marked"] = True


@then("the build should still be marked as terminal in history")
def _then_build_marked_terminal(gci_world: dict[str, Any]) -> None:
    # The flip happened — meaning cleanup_worktree returned normally
    # and did not raise past the adapter boundary (ADR-ARCH-025).
    assert gci_world["terminal_marked"] is True
    result: GitOpResult = gci_world["cleanup_result"]
    assert isinstance(result, GitOpResult)
    # The structured failure stays a failure — the adapter does not
    # silently rewrite it as success — but it is observable, not
    # raised.
    assert result.status == "failed"
    assert result.operation == "cleanup_worktree"
    assert result.exit_code == 128
    assert result.stderr is not None and "locked" in result.stderr


@then("a structured warning should be logged about the failed cleanup")
def _then_warning_logged_about_cleanup(gci_world: dict[str, Any]) -> None:
    records: list[logging.LogRecord] = gci_world["caplog_records"]
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert any("cleanup_worktree non-zero exit" in r.getMessage() for r in warnings), [
        r.getMessage() for r in warnings
    ]
    # The log carries the build_id so operators can correlate it.
    assert any(gci_world["build_id"] in r.getMessage() for r in warnings), [
        r.getMessage() for r in warnings
    ]


# ---------------------------------------------------------------------------
# @edge-case @negative (TASK-GCI-006): Shell metacharacters in subprocess
#                                       arguments are passed as literal
#                                       tokens
# ---------------------------------------------------------------------------


@pytest.mark.edge_case
@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "Shell metacharacters in subprocess arguments are passed as literal tokens",
)
def test_edge_case_shell_metacharacters_pass_as_literal_tokens() -> None:
    """@edge-case @negative — TASK-GCI-006 list-token contract."""


@given(
    "the reasoning model builds subprocess arguments that contain shell metacharacters"
)
def _given_args_with_shell_metacharacters(gci_world: dict[str, Any]) -> None:
    # Canonical injection probe: ``;`` would chain a second shell
    # command, ``&&`` would short-circuit, the embedded double-quotes
    # would terminate a shell-quoted string. None of these can have any
    # effect when delivered as a single argv slot.
    gci_world["nasty_message"] = 'feat: pwn; rm -rf / && echo "owned" `whoami`'
    gci_world["worktree"] = Path("/var/forge/builds/build-meta-bdd")
    gci_world["execute"] = _GCI006FakeExecute(
        responses=[
            GitExecuteResult(exit_code=0, stdout="", stderr=""),  # git add -A
            GitExecuteResult(  # git commit -m
                exit_code=0,
                stdout="[main deadbee] " + gci_world["nasty_message"] + "\n",
                stderr="",
            ),
            GitExecuteResult(  # git rev-parse HEAD
                exit_code=0, stdout="deadbeefcafebabe\n", stderr=""
            ),
        ]
    )


@when("the adapter launches the subprocess")
def _when_adapter_launches_subprocess(gci_world: dict[str, Any]) -> None:
    async def _drive() -> GitOpResult:
        return await git_operations.commit_all(
            gci_world["worktree"],
            gci_world["nasty_message"],
            execute=gci_world["execute"],
        )

    gci_world["commit_result"] = asyncio.run(_drive())


@then("each argument should be delivered to the binary as a single literal token")
def _then_each_arg_literal_token(gci_world: dict[str, Any]) -> None:
    fake: _GCI006FakeExecute = gci_world["execute"]
    nasty: str = gci_world["nasty_message"]
    # The ``commit`` invocation is the second of the three; isolate it
    # and assert the message lives in a single argv slot identity-equal
    # to the input — no shell-expansion, no splitting, no escaping.
    commit_call = fake.calls[1]
    assert commit_call["command"][:3] == ["git", "commit", "-m"]
    assert commit_call["command"][3] is nasty
    # The total argv length is exactly 4 — there is no extra token from
    # accidental ``;`` or ``&&`` splitting.
    assert len(commit_call["command"]) == 4
    # And the result is a structured success carrying the commit SHA —
    # proving the literal-token path does not mangle the workflow.
    result: GitOpResult = gci_world["commit_result"]
    assert isinstance(result, GitOpResult)
    assert result.status == "success"
    assert result.sha == "deadbeefcafebabe"


@then("no shell expansion or secondary command should be evaluated")
def _then_no_shell_expansion(gci_world: dict[str, Any]) -> None:
    # The default executor is ``asyncio.create_subprocess_exec`` (no
    # shell). The injected fake doesn't shell out either. We verify the
    # contract by source inspection — ``shell=True`` must never appear
    # in the operations module — and by call-shape: every recorded
    # command was delivered as a list of discrete argv tokens, not a
    # single shell string.
    source = Path(git_operations.__file__).read_text(encoding="utf-8")
    assert "shell=True," not in source
    assert "shell=True)" not in source
    fake: _GCI006FakeExecute = gci_world["execute"]
    for call in fake.calls:
        # Each command is a list[str] — no element is itself a
        # shell-string with ``;`` between subcommands.
        assert isinstance(call["command"], list)
        assert all(isinstance(token, str) for token in call["command"])
        # The first token is always the binary, never a shell wrapper.
        assert call["command"][0] == "git"


# ===========================================================================
# TASK-GCI-004 — tolerant output parser scenarios
# ===========================================================================
#
# The five @task:TASK-GCI-004 scenarios in
# ``features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature``
# all bind to ``forge.adapters.guardkit.parser.parse_guardkit_output``.
# The parser is a pure function over (stdout, stderr, exit_code,
# duration_secs, timed_out) that returns a canonical
# :class:`GuardKitResult` and **never raises** past its own boundary
# (ADR-ARCH-025). The Given-steps assemble the subprocess outcome as
# the wrapper would observe it; the When-step calls the parser; the
# Then-steps assert on the structured result.
#
# Scenarios wired:
#
# - "A failing GuardKit subprocess is reported as a structured error,
#    not an exception" (@key-example @smoke).
# - "A non-zero exit is reported as a failure with the subprocess error
#    output" (@negative).
# - "An unknown GuardKit output shape degrades to success with no
#    artefacts" (@negative @edge-case).
# - "A compact stdout is preserved verbatim in the returned result"
#    (@boundary).
# - "A large stdout is truncated to the most recent tail in the
#    returned result" (@boundary).
# ---------------------------------------------------------------------------


from forge.adapters.guardkit.parser import (  # noqa: E402
    _STDOUT_TAIL_BYTES,
    parse_guardkit_output,
)


_GCI004_SUBCOMMAND = "feature-spec"


def _gci004_invoke_parser(gci_world: dict[str, Any]) -> GuardKitResult:
    """Drive ``parse_guardkit_output`` with the inputs assembled in ``gci_world``.

    Wraps the call in a try/except so the BDD oracle can verify the
    "tool layer should not propagate an exception" contract directly:
    if the parser ever raised, ``gci_world['parser_raised']`` would be
    set and the Then-step would fail loudly. In practice the parser is
    designed to never raise (AC-008).
    """
    try:
        result = parse_guardkit_output(
            subcommand=gci_world.get("subcommand", _GCI004_SUBCOMMAND),
            stdout=gci_world.get("stdout", ""),
            stderr=gci_world.get("stderr", ""),
            exit_code=gci_world.get("exit_code", 0),
            duration_secs=gci_world.get("duration_secs", 1.0),
            timed_out=gci_world.get("timed_out", False),
        )
    except Exception as exc:  # pragma: no cover — guarded by AC-008
        gci_world["parser_raised"] = exc
        raise
    gci_world["parser_raised"] = None
    gci_world["result"] = result
    return result


# ---------------------------------------------------------------------------
# @key-example @smoke (TASK-GCI-004): A failing GuardKit subprocess is
#                                     reported as a structured error,
#                                     not an exception
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@pytest.mark.smoke
@scenario(
    FEATURE_FILE,
    "A failing GuardKit subprocess is reported as a structured error, not an exception",
)
def test_key_example_failing_subprocess_structured_error() -> None:
    """@key-example @smoke — TASK-GCI-004 structured-failure happy path."""


# ---------------------------------------------------------------------------
# @negative (TASK-GCI-004): A non-zero exit is reported as a failure
#                           with the subprocess error output
# ---------------------------------------------------------------------------


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A non-zero exit is reported as a failure with the subprocess error output",
)
def test_negative_non_zero_exit_failure_with_stderr() -> None:
    """@negative — TASK-GCI-004 non-zero exit preserves stderr."""


# ---------------------------------------------------------------------------
# @negative @edge-case (TASK-GCI-004): An unknown GuardKit output shape
#                                       degrades to success with no
#                                       artefacts
# ---------------------------------------------------------------------------


@pytest.mark.negative
@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "An unknown GuardKit output shape degrades to success with no artefacts",
)
def test_negative_unknown_shape_success_empty() -> None:
    """@negative @edge-case — TASK-GCI-004 tolerant unknown-shape contract."""


# ---------------------------------------------------------------------------
# @boundary (TASK-GCI-004): A compact stdout is preserved verbatim in
#                           the returned result
# ---------------------------------------------------------------------------


@pytest.mark.boundary
@scenario(
    FEATURE_FILE,
    "A compact stdout is preserved verbatim in the returned result",
)
def test_boundary_compact_stdout_preserved_verbatim() -> None:
    """@boundary — TASK-GCI-004 just-inside stdout-tail boundary."""


# ---------------------------------------------------------------------------
# @boundary (TASK-GCI-004): A large stdout is truncated to the most
#                           recent tail in the returned result
# ---------------------------------------------------------------------------


@pytest.mark.boundary
@scenario(
    FEATURE_FILE,
    "A large stdout is truncated to the most recent tail in the returned result",
)
def test_boundary_large_stdout_truncated_to_tail() -> None:
    """@boundary — TASK-GCI-004 just-outside stdout-tail boundary."""


# ---------------------------------------------------------------------------
# Step bindings — TASK-GCI-004
# ---------------------------------------------------------------------------


@given("the reasoning model invokes a GuardKit wrapper")
def _given_reasoning_invokes_guardkit_wrapper(gci_world: dict[str, Any]) -> None:
    # Establish the canonical "wrapper invocation" inputs the parser
    # would normally receive from the subprocess wrapper. Per-scenario
    # When-steps mutate exit_code / stdout / stderr / timed_out before
    # the parser is driven.
    gci_world["subcommand"] = _GCI004_SUBCOMMAND
    gci_world["stdout"] = ""
    gci_world["stderr"] = ""
    gci_world["exit_code"] = 0
    gci_world["duration_secs"] = 0.42
    gci_world["timed_out"] = False


@when("the subprocess exits with a non-zero status")
def _when_subprocess_exits_non_zero(gci_world: dict[str, Any]) -> None:
    # Canonical failing-process probe: non-zero exit + non-empty stderr.
    # The parser must produce status="failed" and surface the captured
    # error stream verbatim — no exception ever escapes the boundary.
    gci_world["exit_code"] = 2
    gci_world["stderr"] = (
        "guardkit: feature-spec stage refused — "
        "manifest missing required key 'specs'\n"
    )
    _gci004_invoke_parser(gci_world)


@when(
    "the subprocess exits with a non-zero status and writes diagnostics "
    "to its error stream"
)
def _when_non_zero_with_diagnostics(gci_world: dict[str, Any]) -> None:
    # Slightly richer probe than the @key-example variant: include a
    # multi-line stderr with diagnostic detail so the Then-step can
    # assert *exit_code AND error output* both reached the result.
    gci_world["exit_code"] = 64
    gci_world["stderr"] = (
        "Traceback (most recent call last):\n"
        '  File "guardkit/cli.py", line 117, in <module>\n'
        '    raise ManifestError("required key missing")\n'
        "guardkit.errors.ManifestError: required key missing\n"
    )
    _gci004_invoke_parser(gci_world)


@when(
    "the subprocess exits cleanly but its output does not match "
    "the expected artefact shape"
)
def _when_clean_exit_unknown_shape(gci_world: dict[str, Any]) -> None:
    # Clean exit (exit_code=0, timed_out=False) with stdout that looks
    # nothing like the documented GuardKit shape. The parser must NOT
    # raise on the unknown shape — instead it must degrade to
    # status="success" with empty artefacts so the reasoning model
    # decides whether the stage produced useful work.
    gci_world["exit_code"] = 0
    gci_world["stdout"] = (
        "??? not a guardkit output ???\n"
        "<<< binary noise + freeform prose >>>\n"
    )
    _gci004_invoke_parser(gci_world)


@given(
    "a GuardKit subprocess that prints fewer than four kilobytes "
    "to standard output"
)
def _given_compact_stdout(gci_world: dict[str, Any]) -> None:
    # Ten lines of recognisable output, well under the 4 KB cap. The
    # tail-truncation branch must NOT fire on this input — the parser
    # should preserve the stdout verbatim on the ``stdout_tail`` field.
    gci_world["subcommand"] = _GCI004_SUBCOMMAND
    gci_world["stdout"] = "".join(f"line {i}: compact output\n" for i in range(10))
    gci_world["stderr"] = ""
    gci_world["exit_code"] = 0
    gci_world["duration_secs"] = 0.05
    gci_world["timed_out"] = False
    # Sanity check the fixture: confirm the precondition matches the
    # Gherkin phrasing ("fewer than four kilobytes") so the assertion
    # actually exercises the small-stdout branch.
    assert len(gci_world["stdout"].encode("utf-8")) < _STDOUT_TAIL_BYTES


@given(
    "a GuardKit subprocess that prints far more than the captured tail size"
)
def _given_oversize_stdout(gci_world: dict[str, Any]) -> None:
    # 10_000 ASCII bytes is comfortably above the 4 KB tail cap. We use
    # distinguishable head/tail markers so the Then-step can assert we
    # kept the END (truncation is "last N bytes", not "first N").
    gci_world["subcommand"] = _GCI004_SUBCOMMAND
    head_marker = "GCI004-HEAD-MARKER"
    tail_marker = "GCI004-TAIL-MARKER"
    filler = "X" * 10_000
    gci_world["stdout"] = head_marker + filler + tail_marker
    gci_world["stderr"] = ""
    gci_world["exit_code"] = 0
    gci_world["duration_secs"] = 0.05
    gci_world["timed_out"] = False
    gci_world["head_marker"] = head_marker
    gci_world["tail_marker"] = tail_marker
    # Sanity: precondition really does exceed the tail cap.
    assert len(gci_world["stdout"].encode("utf-8")) > _STDOUT_TAIL_BYTES


@when("the invocation completes")
def _when_invocation_completes(gci_world: dict[str, Any]) -> None:
    # Both compact and oversize scenarios share this When-step; the
    # parser is driven once with whatever the Given-step assembled.
    _gci004_invoke_parser(gci_world)


@then("the invocation should return a structured failure result")
def _then_structured_failure_result(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "failed"
    assert result.subcommand == _GCI004_SUBCOMMAND


@then("the failure result should carry the captured error output")
def _then_failure_carries_error_output(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    # The wrapper's stderr capture is preserved verbatim on the
    # structured result — no log-only side-channel.
    assert result.stderr == gci_world["stderr"]


@then("the tool layer should not propagate an exception to the reasoning model")
def _then_no_exception_propagates(gci_world: dict[str, Any]) -> None:
    # ``_gci004_invoke_parser`` records any escaping exception on
    # ``gci_world['parser_raised']``. AC-008 says it must always be
    # ``None`` — the parser folds internal failures into structured
    # warnings, never re-raises.
    assert gci_world["parser_raised"] is None


@then("the failure result should include the subprocess exit status and error output")
def _then_failure_has_exit_and_stderr(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert result.exit_code == gci_world["exit_code"]
    assert result.stderr == gci_world["stderr"]
    # And the failure status was reached — not silently rewritten as
    # success on a non-zero exit.
    assert result.status == "failed"


@then("the invocation should still report success")
def _then_invocation_still_reports_success(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    # Tolerant by design: unknown-shape stdout still yields success on
    # a clean exit (AC-005). The reasoning model evaluates whether the
    # stage produced useful work — not the parser.
    assert result.status == "success"


@then("the returned artefact list should be empty")
def _then_artefact_list_empty(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert result.artefacts == []
    # And no exception escaped.
    assert gci_world["parser_raised"] is None


@then(
    "the reasoning model should be responsible for deciding whether "
    "the stage produced useful work"
)
def _then_reasoning_model_decides(gci_world: dict[str, Any]) -> None:
    # The parser surfaces enough context (status, artefacts,
    # stdout_tail, warnings) for the reasoning model to make the call,
    # but NEVER pre-decides the stage's verdict by raising or by
    # silently rewriting status. Verify the fields the reasoning model
    # consults are intact and observable.
    result: GuardKitResult = gci_world["result"]
    assert result.status == "success"
    assert result.artefacts == []
    # stdout_tail is byte-truncated on the success path; assert the
    # field is at least observable so the reasoning model can inspect
    # it. None is *not* the contract — the parser always produces a
    # string (possibly empty).
    assert isinstance(result.stdout_tail, str)


@then("the returned result should include the full standard output")
def _then_full_stdout_in_result(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    # ``stdout_tail`` mirrors the input verbatim when the input is
    # below the tail cap. Compare bytes-for-bytes — no normalisation.
    assert result.stdout_tail == gci_world["stdout"]
    # And we did not slip into a failure / timeout path on a clean
    # exit just because the output was small.
    assert result.status == "success"


@then("the returned result should include only the most recent slice of standard output")
def _then_only_most_recent_slice(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    # The truncation must keep the END of stdout, not the start.
    assert gci_world["tail_marker"] in result.stdout_tail
    assert gci_world["head_marker"] not in result.stdout_tail
    assert result.stdout_tail.endswith(gci_world["tail_marker"])


@then("the slice size should match the configured tail limit")
def _then_slice_size_matches_tail_limit(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    # ``_STDOUT_TAIL_BYTES`` is the documented 4 KB cap (ASSUM-003).
    # The tail is byte-based, so re-encoding must come in at-or-below
    # the cap regardless of how many *characters* that represents.
    assert len(result.stdout_tail.encode("utf-8")) <= _STDOUT_TAIL_BYTES
    # And on this all-ASCII input the tail is exactly the cap.
    assert len(result.stdout_tail.encode("utf-8")) == _STDOUT_TAIL_BYTES
