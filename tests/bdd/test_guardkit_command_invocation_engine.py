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
from pytest_bdd import given, parsers, scenario, then, when

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
        "??? not a guardkit output ???\n" "<<< binary noise + freeform prose >>>\n"
    )
    _gci004_invoke_parser(gci_world)


@given(
    "a GuardKit subprocess that prints fewer than four kilobytes " "to standard output"
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


@given("a GuardKit subprocess that prints far more than the captured tail size")
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


@then(
    "the returned result should include only the most recent slice of standard output"
)
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


# ===========================================================================
# TASK-GCI-008 — subprocess wrapper scenarios (10 scenarios)
# TASK-GCI-003 — context resolver scenarios (8 scenarios)
# TASK-GCI-009 — generic wrapper error contract (1 scenario)
# TASK-GCI-010 — Graphiti resolver-bypass (1 scenario)
# ---------------------------------------------------------------------------
#
# The bindings below activate the remaining 20 of the 32 BDD scenarios
# in the GCI feature file (R2 oracle activation per TASK-GCI-011).
#
# Step bindings exercise the **public** tool layer surface
# (``forge.tools.guardkit.guardkit_*`` and
# ``forge.tools.graphiti.guardkit_graphiti_*``) plus the run() seam
# directly when a tool wrapper would obscure the contract under test
# (e.g. the cwd-confinement and parallel scenarios assert directly on
# ``run()``'s post-conditions). DeepAgents' subprocess seam
# (``forge.adapters.guardkit.run._execute_subprocess``) is monkey-patched
# to deliver canned outcomes — no real ``guardkit`` binary is invoked.
# ---------------------------------------------------------------------------

import json  # noqa: E402

import yaml  # noqa: E402

from forge.adapters.guardkit import run as gk_run_module  # noqa: E402
from forge.adapters.guardkit.context_resolver import (  # noqa: E402
    resolve_context_flags,
)
from forge.adapters.guardkit.run import run as gk_run  # noqa: E402
from forge.tools.graphiti import guardkit_graphiti_query  # noqa: E402
from forge.tools.guardkit import (  # noqa: E402
    _invoke as guardkit_tools_invoke,
    guardkit_feature_spec,
)


def _gci_canned_success_stdout(*, artefacts: list[str], coach_score: float | None = None) -> str:
    """Build a canned GuardKit-shaped stdout for the parser to consume."""
    lines: list[str] = ["## Artefacts"]
    for artefact in artefacts:
        lines.append(f"- {artefact}")
    if coach_score is not None:
        lines.extend(["", f"coach_score: {coach_score:.2f}"])
    return "\n".join(lines) + "\n"


def _gci_seed_manifest(
    repo_root: Path,
    *,
    always_include: list[str] | None = None,
    key_docs: list[dict[str, str]] | None = None,
    dependencies: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write a ``.guardkit/context-manifest.yaml`` under ``repo_root``.

    Returns the manifest path. Files referenced by the manifest are
    created as empty placeholders so the resolver's allowlist check
    passes on a real on-disk path.
    """
    manifest_dir = repo_root / ".guardkit"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "context-manifest.yaml"

    data: dict[str, Any] = {}
    if always_include:
        data["internal_docs"] = {"always_include": always_include}
        for rel in always_include:
            doc_path = repo_root / rel
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            if not doc_path.exists():
                doc_path.write_text("", encoding="utf-8")
    if dependencies:
        data["dependencies"] = dependencies
        # Materialise the dependency's key_docs even though the manifest
        # itself lives in a sibling repo (the resolver re-validates each
        # path against its owning repo root).
        for _name, dep_info in dependencies.items():
            dep_root = (repo_root / dep_info["path"]).resolve()
            for kd in dep_info.get("key_docs", []) or []:
                kd_path = dep_root / kd["path"]
                kd_path.parent.mkdir(parents=True, exist_ok=True)
                if not kd_path.exists():
                    kd_path.write_text("", encoding="utf-8")
    if key_docs:
        # Persist key_docs under origin's "self" pseudo-dependency so the
        # resolver bucket-fills from the origin manifest itself.
        data.setdefault("dependencies", {})["self"] = {
            "path": ".",
            "key_docs": key_docs,
        }
        for kd in key_docs:
            kd_path = repo_root / kd["path"]
            kd_path.parent.mkdir(parents=True, exist_ok=True)
            if not kd_path.exists():
                kd_path.write_text("", encoding="utf-8")

    manifest_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# TASK-GCI-008 — Scenario decorators
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@pytest.mark.smoke
@scenario(
    FEATURE_FILE,
    "A GuardKit subcommand completes successfully and its artefacts are captured",
)
def test_key_example_subcommand_completes_with_artefacts() -> None:
    """@key-example @smoke — TASK-GCI-008 happy-path subprocess success."""


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Subprocesses are executed inside the current build's worktree",
)
def test_key_example_subprocess_in_worktree() -> None:
    """@key-example — TASK-GCI-008 cwd confinement contract."""


@pytest.mark.boundary
@scenario(
    FEATURE_FILE,
    "A subprocess that finishes within the timeout is reported as successful",
)
def test_boundary_finish_within_timeout_outline() -> None:
    """@boundary — TASK-GCI-008 just-inside timeout (Outline 1/300/599 s)."""


@pytest.mark.boundary
@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A subprocess that exceeds the timeout is reported as timed-out",
)
def test_boundary_negative_subprocess_timed_out() -> None:
    """@boundary @negative — TASK-GCI-008 timeout-status contract."""


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A subprocess whose binary is not in the shell allowlist is refused",
)
def test_negative_binary_not_in_allowlist() -> None:
    """@negative — TASK-GCI-008 binary-allowlist refusal."""


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A subprocess targeting a working directory outside the allowlist is refused",
)
def test_negative_cwd_outside_allowlist() -> None:
    """@negative — TASK-GCI-008 cwd-allowlist refusal."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "A failed invocation can be retried with additional explicit context",
)
def test_edge_case_retry_with_extra_context() -> None:
    """@edge-case — TASK-GCI-008 retry-with-explicit-context contract."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "Parallel GuardKit invocations in the same build do not corrupt "
    "each other's results",
)
def test_edge_case_parallel_invocations_isolated() -> None:
    """@edge-case — TASK-GCI-008 parallel-invocation isolation."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "A cancelled build terminates its in-flight subprocess cleanly",
)
def test_edge_case_cancelled_build_terminates_subprocess() -> None:
    """@edge-case — TASK-GCI-008 cancellation propagation."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "A silent stalled subprocess is terminated by the configured timeout",
)
def test_edge_case_silent_stalled_subprocess_timeout() -> None:
    """@edge-case — TASK-GCI-008 silent-stalled-process termination."""


# ---------------------------------------------------------------------------
# TASK-GCI-008 — Step bindings
# ---------------------------------------------------------------------------


@given("the reasoning model invokes a GuardKit subcommand wrapper for the current build")
def _given_reasoning_invokes_subcommand_wrapper(
    gci_world: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build a tmp worktree with no manifest — the resolver will skip
    # safely and the parser will still produce a structured success.
    worktree = tmp_path / "builds" / "B-bdd-008-1"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree
    gci_world["expected_artefacts"] = [
        "docs/specs/example.md",
        "docs/contracts/example.md",
    ]
    gci_world["expected_coach_score"] = 0.91
    canned_stdout = _gci_canned_success_stdout(
        artefacts=gci_world["expected_artefacts"],
        coach_score=gci_world["expected_coach_score"],
    )
    captured: dict[str, Any] = {}

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        captured["command"] = list(command)
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        # Simulated wall-clock duration as observed by the wrapper.
        return (canned_stdout, "", 0, 1.25, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)
    gci_world["captured_call"] = captured


@when("the GuardKit process exits cleanly")
def _when_guardkit_process_exits_cleanly(gci_world: dict[str, Any]) -> None:
    async def _drive() -> str:
        return await guardkit_feature_spec.ainvoke(
            {
                "repo": str(gci_world["worktree"]),
                "feature_description": "BDD oracle: subcommand success",
            }
        )

    raw_json = asyncio.run(_drive())
    gci_world["raw_json"] = raw_json
    gci_world["result"] = GuardKitResult.model_validate_json(raw_json)


@then("the invocation should report success")
def _then_invocation_reports_success(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "success"
    assert result.exit_code == 0


@then("the returned result should list every artefact path emitted by GuardKit")
def _then_returned_lists_all_artefacts(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert result.artefacts == gci_world["expected_artefacts"]


@then("the returned result should include the command's wall-clock duration")
def _then_returned_includes_duration(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    # Wall-clock duration must be a non-negative number — the parser
    # propagates whatever the wrapper measured.
    assert isinstance(result.duration_secs, float)
    assert result.duration_secs >= 0.0


@then("the returned result should include the Coach score when GuardKit produced one")
def _then_returned_includes_coach_score(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert result.coach_score == pytest.approx(gci_world["expected_coach_score"])


# Worktree-confinement scenario


@given("a build's worktree has been prepared")
def _given_build_worktree_prepared(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-2"
    worktree.mkdir(parents=True)
    # forge.yaml stand-in: the read_allowlist passed into ``run()``.
    gci_world["worktree"] = worktree
    gci_world["read_allowlist"] = [worktree]
    captured: dict[str, Any] = {}

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        captured["command"] = list(command)
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return (_gci_canned_success_stdout(artefacts=["docs/specs/x.md"]), "", 0, 0.1, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)
    gci_world["captured_call"] = captured


@when("any GuardKit, git, or shell subprocess is launched through the adapter")
def _when_any_subprocess_launched(gci_world: dict[str, Any]) -> None:
    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=["--description", "BDD oracle: cwd confinement"],
            repo_path=gci_world["worktree"],
            read_allowlist=gci_world["read_allowlist"],
        )

    gci_world["result"] = asyncio.run(_drive())


@then("the subprocess working directory should be the build's worktree")
def _then_subprocess_cwd_is_worktree(gci_world: dict[str, Any]) -> None:
    captured = gci_world["captured_call"]
    expected = str(gci_world["worktree"].resolve())
    assert captured["cwd"] == expected


@then(
    "the subprocess should not be able to read or write outside the allowed worktree paths"
)
def _then_subprocess_cannot_escape(gci_world: dict[str, Any]) -> None:
    # Defence-in-depth: the ``read_allowlist`` is what ``run()`` enforces.
    # Any path outside that allowlist would have been refused before the
    # subprocess seam was even called. We exercise the negative case in
    # the @negative cwd-outside scenario; here we assert the positive
    # invariant: the resolved cwd is genuinely under one allowlist entry.
    captured = gci_world["captured_call"]
    cwd_path = Path(captured["cwd"]).resolve()
    allowlist_resolved = [p.resolve() for p in gci_world["read_allowlist"]]
    assert any(
        cwd_path == allowed or cwd_path.is_relative_to(allowed)
        for allowed in allowlist_resolved
    )


# Outline timeout-success scenario


@given("the subprocess timeout has been configured")
def _given_timeout_configured(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-timeout"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree
    # Keep cap below the canonical 600-second default so the @boundary
    # values 1/300/599 stay well inside it.
    gci_world["timeout_seconds"] = 600


@when(
    parsers.parse("a GuardKit wrapper completes after {seconds:d} seconds of runtime"),
)
def _when_wrapper_completes_after_seconds(
    gci_world: dict[str, Any], seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned_stdout = _gci_canned_success_stdout(artefacts=["docs/specs/timing.md"])
    duration = float(seconds)

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        return (canned_stdout, "", 0, duration, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)
    gci_world["expected_duration"] = duration

    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["worktree"],
            read_allowlist=[gci_world["worktree"]],
            timeout_seconds=gci_world["timeout_seconds"],
        )

    gci_world["result"] = asyncio.run(_drive())


@then("the returned duration should match the observed runtime")
def _then_duration_matches_observed(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert result.duration_secs == pytest.approx(gci_world["expected_duration"])


# Timeout-exceeded scenario


@when("a GuardKit wrapper has been running longer than the configured timeout")
def _when_wrapper_running_longer_than_timeout(
    gci_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        # Simulate the SIGTERM-after-timeout outcome the real seam
        # produces: empty/partial stdout, exit_code=124 (canonical
        # GNU-coreutils timeout signal), timed_out=True.
        return ("", "", 124, float(timeout), True)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)

    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["worktree"],
            read_allowlist=[gci_world["worktree"]],
            timeout_seconds=gci_world["timeout_seconds"],
        )

    gci_world["result"] = asyncio.run(_drive())


@then("the invocation should return a timeout result")
def _then_invocation_returns_timeout(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "timeout"


@then("the reasoning model should be free to decide whether to retry or escalate")
def _then_reasoning_decides_retry_or_escalate(gci_world: dict[str, Any]) -> None:
    # The contract under test is: timeout is a structured outcome, NOT
    # an exception. The reasoning model receives a parseable result and
    # can choose retry vs. escalate. Verify the result is observable
    # and carries enough context.
    result: GuardKitResult = gci_world["result"]
    assert result.status == "timeout"
    assert isinstance(result.duration_secs, float)
    # And no exception ever propagated: ``asyncio.run`` returned a
    # ``GuardKitResult`` rather than raising.


# Binary-not-in-allowlist scenario


@given("the project configuration lists the permitted shell binaries")
def _given_shell_binaries_allowlisted(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-allowlist"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree
    # forge.yaml stand-in: an allowlist of permitted binaries the
    # subprocess seam would consult. We simulate the refusal by having
    # ``_execute_subprocess`` raise PermissionError — that is the
    # canonical signal ``run()`` converts into a structured permissions
    # error (per ADR-ARCH-023 / DDR / TASK-GCI-008).
    gci_world["permitted_binaries"] = ["/usr/local/bin/guardkit"]


@when("the reasoning model attempts to execute a binary that is not on the allowlist")
def _when_attempts_unallowed_binary(
    gci_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {"called": False}

    async def _refusing_execute(*, command: list[str], cwd: str, timeout: int):
        captured["called"] = True
        # The DeepAgents permission layer raises PermissionError when
        # the binary is not on the shell allowlist; ``run()`` converts
        # that into a structured failure with code "permissions_refused".
        raise PermissionError(
            f"binary {command[0]!r} not on shell allowlist"
        )

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _refusing_execute)
    gci_world["execute_called"] = captured

    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["worktree"],
            read_allowlist=[gci_world["worktree"]],
        )

    gci_world["result"] = asyncio.run(_drive())


@then("the subprocess should be refused before launch")
def _then_subprocess_refused_before_launch(gci_world: dict[str, Any]) -> None:
    # Either the seam was never reached (cwd-allowlist check fired in
    # ``run()``), or it was reached and raised PermissionError. Both
    # forms count as "refused before the user's process actually ran".
    result: GuardKitResult = gci_world["result"]
    assert isinstance(result, GuardKitResult)
    assert result.status == "failed"


@then("the invocation should return a structured permissions error")
def _then_returns_structured_permissions_error(gci_world: dict[str, Any]) -> None:
    result: GuardKitResult = gci_world["result"]
    codes = [w.code for w in result.warnings]
    assert any(
        c in {"permissions_refused", "cwd_outside_allowlist"} for c in codes
    ), codes


@then("no side effects should occur on the worktree")
def _then_no_side_effects(gci_world: dict[str, Any]) -> None:
    # The fake _execute either raised before doing any work, or was
    # never invoked. The worktree path remains an empty directory.
    worktree: Path = gci_world["worktree"]
    children = [p for p in worktree.iterdir() if p.is_file()]
    assert children == []


# CWD-outside-allowlist scenario


@given("the project configuration allows subprocesses only within the build worktrees path")
def _given_subprocesses_only_in_worktrees(
    gci_world: dict[str, Any], tmp_path: Path
) -> None:
    allowed_root = tmp_path / "builds"
    allowed_root.mkdir()
    gci_world["allowed_root"] = allowed_root
    gci_world["read_allowlist"] = [allowed_root]
    # An outside-allowlist directory we will hand to ``run()``.
    outside = tmp_path / "outside-allowlist-dir"
    outside.mkdir()
    gci_world["outside_path"] = outside


@when("a subprocess is requested with a working directory outside that path")
def _when_subprocess_cwd_outside(
    gci_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    not_called: dict[str, bool] = {"called": False}

    async def _should_not_be_called(*, command: list[str], cwd: str, timeout: int):
        not_called["called"] = True
        return ("", "", 0, 0.0, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _should_not_be_called)
    gci_world["execute_called"] = not_called

    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["outside_path"],
            read_allowlist=gci_world["read_allowlist"],
        )

    gci_world["result"] = asyncio.run(_drive())
    # Confirm the seam was never reached — the cwd check in ``run()``
    # short-circuited the launch.
    assert gci_world["execute_called"]["called"] is False


# Retry-with-extra-context scenario


@given("a GuardKit wrapper has returned a failure result")
def _given_wrapper_returned_failure(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-retry"
    worktree.mkdir(parents=True)
    # Seed a manifest so the resolver has manifest-derived flags to
    # merge with the explicit retry paths.
    _gci_seed_manifest(
        worktree,
        always_include=["docs/always.md"],
        key_docs=[{"path": "docs/specs/auto.md", "category": "specs"}],
    )
    gci_world["worktree"] = worktree

    captured_calls: list[dict[str, Any]] = []

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        captured_calls.append({"command": list(command), "cwd": cwd})
        if len(captured_calls) == 1:
            # First call: structured failure.
            return ("", "guardkit: failure\n", 2, 0.05, False)
        # Retry: success with merged context flags.
        return (_gci_canned_success_stdout(artefacts=["docs/specs/auto.md"]), "", 0, 0.06, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)
    gci_world["captured_calls"] = captured_calls

    # Drive the first call (the failed attempt).
    async def _drive_first() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=worktree,
            read_allowlist=[worktree],
        )

    gci_world["first_result"] = asyncio.run(_drive_first())
    assert gci_world["first_result"].status == "failed"


@when(
    "the reasoning model retries the same wrapper with additional explicit context paths"
)
def _when_retries_with_extra_context(gci_world: dict[str, Any]) -> None:
    # Use an absolute path that is allowlist-eligible (under worktree)
    # so the resolver's path validation passes.
    extra_doc = gci_world["worktree"] / "docs" / "explicit-extra.md"
    extra_doc.parent.mkdir(parents=True, exist_ok=True)
    extra_doc.write_text("", encoding="utf-8")
    gci_world["extra_paths"] = [str(extra_doc)]

    async def _drive_retry() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["worktree"],
            read_allowlist=[gci_world["worktree"]],
            extra_context_paths=gci_world["extra_paths"],
        )

    gci_world["result"] = asyncio.run(_drive_retry())


@then("the retry should merge the explicit paths with the manifest-derived ones")
def _then_retry_merges_explicit_with_manifest(gci_world: dict[str, Any]) -> None:
    # The retry call (second entry) carries both the manifest-derived
    # and the caller-supplied paths in its --context flags.
    retry_call = gci_world["captured_calls"][1]
    cmd = retry_call["command"]
    context_indices = [i for i, tok in enumerate(cmd) if tok == "--context"]
    context_paths = [cmd[i + 1] for i in context_indices]
    extra_path = gci_world["extra_paths"][0]
    assert extra_path in context_paths
    # Manifest-derived path also present (resolver included always_include).
    assert any("always.md" in p for p in context_paths)


@then("the retry should be a fresh subprocess launch, not a continuation")
def _then_retry_is_fresh_launch(gci_world: dict[str, Any]) -> None:
    # Two distinct invocations of the seam. The wrapper holds no shared
    # state between them — ASSUM-005 / ASSUM-007.
    assert len(gci_world["captured_calls"]) == 2
    # Each call had its own command list (not the same Python list).
    first, second = gci_world["captured_calls"]
    assert first["command"] is not second["command"]


# Parallel-invocations scenario


@given("two GuardKit wrappers are invoked in parallel within the same build")
def _given_two_wrappers_in_parallel(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-parallel"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree

    artefacts_a = ["docs/a/spec-a.md"]
    artefacts_b = ["docs/b/spec-b.md"]
    gci_world["artefacts_a"] = artefacts_a
    gci_world["artefacts_b"] = artefacts_b

    call_count = {"n": 0}

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        call_count["n"] += 1
        # Distinguish by subcommand argument so the per-task artefacts
        # do not bleed into the wrong result.
        if "task-a" in command:
            return (_gci_canned_success_stdout(artefacts=artefacts_a), "", 0, 0.1, False)
        if "task-b" in command:
            return (_gci_canned_success_stdout(artefacts=artefacts_b), "", 0, 0.1, False)
        return ("", "", 0, 0.0, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)
    gci_world["call_count"] = call_count


@when("both subprocesses complete")
def _when_both_subprocesses_complete(gci_world: dict[str, Any]) -> None:
    async def _run_both() -> tuple[GuardKitResult, GuardKitResult]:
        ra, rb = await asyncio.gather(
            gk_run(
                subcommand="feature-spec",
                args=["task-a"],
                repo_path=gci_world["worktree"],
                read_allowlist=[gci_world["worktree"]],
            ),
            gk_run(
                subcommand="feature-spec",
                args=["task-b"],
                repo_path=gci_world["worktree"],
                read_allowlist=[gci_world["worktree"]],
            ),
        )
        return ra, rb

    a, b = asyncio.run(_run_both())
    gci_world["result_a"] = a
    gci_world["result_b"] = b


@then("each invocation should receive its own independent structured result")
def _then_each_independent_result(gci_world: dict[str, Any]) -> None:
    a: GuardKitResult = gci_world["result_a"]
    b: GuardKitResult = gci_world["result_b"]
    assert isinstance(a, GuardKitResult) and isinstance(b, GuardKitResult)
    assert a is not b
    assert a.status == "success" and b.status == "success"


@then(
    "neither result should contain artefacts or progress events belonging to the other"
)
def _then_no_cross_contamination(gci_world: dict[str, Any]) -> None:
    a: GuardKitResult = gci_world["result_a"]
    b: GuardKitResult = gci_world["result_b"]
    assert a.artefacts == gci_world["artefacts_a"]
    assert b.artefacts == gci_world["artefacts_b"]
    # Cross-check: each result has none of the other's artefacts.
    assert not set(a.artefacts) & set(b.artefacts)


# Cancellation scenario


@given("a GuardKit subprocess is currently running inside a build")
def _given_subprocess_running(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-cancel"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree

    cancellation_observed = {"hit": False}

    async def _slow_execute(*, command: list[str], cwd: str, timeout: int):
        try:
            # A long sleep that cancellation will interrupt.
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            cancellation_observed["hit"] = True
            raise
        return ("", "", 0, 60.0, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _slow_execute)
    gci_world["cancellation_observed"] = cancellation_observed


@when("the build is cancelled")
def _when_build_cancelled(gci_world: dict[str, Any]) -> None:
    async def _drive_with_cancel() -> str:
        task = asyncio.create_task(
            gk_run(
                subcommand="feature-spec",
                args=[],
                repo_path=gci_world["worktree"],
                read_allowlist=[gci_world["worktree"]],
            )
        )
        # Yield once so the task starts and enters the slow sleep.
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    gci_world["cancel_outcome"] = asyncio.run(_drive_with_cancel())


@then("the subprocess should be terminated")
def _then_subprocess_terminated(gci_world: dict[str, Any]) -> None:
    # ``asyncio.CancelledError`` reached the seam (cancellation_observed
    # flipped) and propagated past ``run()`` per ADR-ARCH-025 footnote.
    assert gci_world["cancel_outcome"] == "cancelled"
    assert gci_world["cancellation_observed"]["hit"] is True


@then(
    "any partial artefacts produced by the terminated subprocess should not be "
    "reported as completed work"
)
def _then_no_partial_artefacts_reported(gci_world: dict[str, Any]) -> None:
    # The wrapper's outer Task was cancelled BEFORE the seam returned
    # any stdout to the parser. There is no GuardKitResult to inspect —
    # the cancellation propagated, which itself is the contract.
    assert gci_world["cancel_outcome"] == "cancelled"


# Silent-stalled scenario


@given(
    "a GuardKit subprocess has produced no standard output and no progress events"
)
def _given_silent_subprocess(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-008-silent"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree
    gci_world["timeout_seconds"] = 30

    seam_called = {"called": False}

    async def _silent_execute(*, command: list[str], cwd: str, timeout: int):
        seam_called["called"] = True
        # Empty stdout AND empty stderr; timed_out=True, exit_code=124.
        return ("", "", 124, float(timeout), True)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _silent_execute)
    gci_world["seam_called"] = seam_called


@when("the subprocess has been running longer than the configured timeout")
def _when_subprocess_running_longer_than_timeout(
    gci_world: dict[str, Any],
) -> None:
    async def _drive() -> GuardKitResult:
        return await gk_run(
            subcommand="feature-spec",
            args=[],
            repo_path=gci_world["worktree"],
            read_allowlist=[gci_world["worktree"]],
            timeout_seconds=gci_world["timeout_seconds"],
        )

    gci_world["result"] = asyncio.run(_drive())


@then(
    "the subprocess should be terminated before its process handle is released"
)
def _then_subprocess_terminated_before_handle_released(
    gci_world: dict[str, Any],
) -> None:
    # The seam was driven, returned timed_out=True, and the wrapper
    # produced a structured timeout result rather than hanging or
    # raising. That is the "terminated before handle released"
    # invariant under test in this BDD layer.
    assert gci_world["seam_called"]["called"] is True
    result: GuardKitResult = gci_world["result"]
    assert result.status == "timeout"


# ---------------------------------------------------------------------------
# TASK-GCI-003 — Scenario decorators
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@pytest.mark.smoke
@scenario(
    FEATURE_FILE,
    "Context flags are assembled automatically from the manifest "
    "for the invoked subcommand",
)
def test_key_example_context_flags_auto_assembled() -> None:
    """@key-example @smoke — TASK-GCI-003 manifest-driven context."""


@pytest.mark.boundary
@scenario(
    FEATURE_FILE,
    "Context resolution follows dependency references up to the depth cap",
)
def test_boundary_context_depth_chase() -> None:
    """@boundary — TASK-GCI-003 depth-2 dependency chase."""


@pytest.mark.boundary
@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "Context resolution stops at the depth cap and warns instead of "
    "recursing further",
)
def test_boundary_edge_case_depth_cap_warn() -> None:
    """@boundary @edge-case — TASK-GCI-003 depth-cap structured warning."""


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A missing context manifest degrades gracefully to no context flags",
)
def test_negative_missing_manifest_degrades_gracefully() -> None:
    """@negative — TASK-GCI-003 missing-manifest contract."""


@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "Context documents that fall outside the read allowlist are omitted "
    "with a warning",
)
def test_negative_outside_allowlist_omitted_with_warning() -> None:
    """@negative — TASK-GCI-003 read-allowlist enforcement."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "A circular dependency chain is detected and resolved safely",
)
def test_edge_case_circular_dependency_safe() -> None:
    """@edge-case — TASK-GCI-003 cycle-detection invariant."""


@pytest.mark.edge_case
@pytest.mark.negative
@scenario(
    FEATURE_FILE,
    "A context manifest entry that would escape the repository root is rejected",
)
def test_edge_case_negative_path_traversal_rejected() -> None:
    """@edge-case @negative — TASK-GCI-003 path-traversal refusal."""


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "Two concurrent builds against the same repository resolve context "
    "independently",
)
def test_edge_case_concurrent_builds_isolated_resolution() -> None:
    """@edge-case — TASK-GCI-003 stateless-resolver invariant."""


# ---------------------------------------------------------------------------
# TASK-GCI-003 — Step bindings
# ---------------------------------------------------------------------------


@given("the manifest lists documents tagged with multiple categories")
def _given_manifest_multi_category(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "ms-1"
    repo.mkdir(parents=True)
    _gci_seed_manifest(
        repo,
        always_include=["docs/internal/AGENTS.md"],
        key_docs=[
            {"path": "docs/specs/spec-1.md", "category": "specs"},
            {"path": "docs/contracts/contract-1.md", "category": "contracts"},
            {"path": "docs/source/source-1.md", "category": "source"},
            {"path": "docs/decisions/decision-1.md", "category": "decisions"},
            # An "architecture" doc that feature-spec MUST exclude.
            {"path": "docs/architecture/arch-1.md", "category": "architecture"},
        ],
    )
    gci_world["repo"] = repo


@when("Forge invokes the feature-spec wrapper")
def _when_invokes_feature_spec(gci_world: dict[str, Any]) -> None:
    repo = gci_world["repo"]
    gci_world["resolved"] = resolve_context_flags(repo, "feature-spec", [repo])


@then(
    "the resolver should select only documents whose category is relevant to feature-spec"
)
def _then_only_feature_spec_categories(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    # feature-spec categories (DDR-005): specs, contracts, source, decisions.
    # Architecture must be excluded.
    assert not any("architecture/arch-1.md" in p for p in paths)
    assert any("specs/spec-1.md" in p for p in paths)
    assert any("contracts/contract-1.md" in p for p in paths)
    assert any("source/source-1.md" in p for p in paths)
    assert any("decisions/decision-1.md" in p for p in paths)


@then("every always-included document should be prepended to the context list")
def _then_always_included_prepended(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    assert paths, "resolver returned an empty path list"
    assert "internal/AGENTS.md" in paths[0]


@then("the subprocess command line should carry one context flag per selected document")
def _then_one_context_flag_per_doc(gci_world: dict[str, Any]) -> None:
    flags: list[str] = gci_world["resolved"].flags
    paths: list[str] = gci_world["resolved"].paths
    assert flags.count("--context") == len(paths)
    # Pairing invariant: --context P --context Q --context R
    assert len(flags) == 2 * len(paths)


# Depth-2 chase scenario


@given("the manifest declares a dependency on a sibling repository")
def _given_manifest_with_dependency(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "depth-origin"
    sibling = tmp_path / "repos" / "depth-sibling"
    repo.mkdir(parents=True)
    sibling.mkdir(parents=True)
    gci_world["repo"] = repo
    gci_world["sibling"] = sibling
    # Origin manifest references the sibling and lists one of the
    # sibling's docs in key_docs.
    sibling_rel = "../depth-sibling"
    _gci_seed_manifest(
        repo,
        dependencies={
            "sibling": {
                "path": sibling_rel,
                "key_docs": [
                    {"path": "docs/specs/sibling-spec.md", "category": "specs"},
                ],
            }
        },
    )


@given("the sibling repository declares its own manifest with key documents")
def _given_sibling_manifest(gci_world: dict[str, Any]) -> None:
    sibling: Path = gci_world["sibling"]
    _gci_seed_manifest(
        sibling,
        key_docs=[
            {"path": "docs/contracts/sibling-contract.md", "category": "contracts"},
        ],
    )


@when("Forge invokes a subcommand that consumes both manifests' categories")
def _when_invokes_for_both_manifests(gci_world: dict[str, Any]) -> None:
    # feature-spec consumes specs+contracts+source+decisions — both
    # docs in the chain qualify.
    repo: Path = gci_world["repo"]
    sibling: Path = gci_world["sibling"]
    gci_world["resolved"] = resolve_context_flags(
        repo, "feature-spec", [repo, sibling]
    )


@then("the resolver should include selected documents from both manifests")
def _then_includes_both_manifests(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    assert any("sibling-spec.md" in p for p in paths)
    assert any("sibling-contract.md" in p for p in paths)


@then("the final context list should preserve a stable order")
def _then_stable_order(gci_world: dict[str, Any]) -> None:
    # Run the resolution again — the resolver is stateless, so the
    # order must be byte-for-byte identical.
    repo: Path = gci_world["repo"]
    sibling: Path = gci_world["sibling"]
    second = resolve_context_flags(repo, "feature-spec", [repo, sibling])
    assert gci_world["resolved"].paths == second.paths
    assert gci_world["resolved"].flags == second.flags


# Depth-cap scenario


@given("the manifest chain would require traversing beyond the resolver's depth cap")
def _given_chain_beyond_depth_cap(gci_world: dict[str, Any], tmp_path: Path) -> None:
    # A → B → C → D. Depth cap is 2 (manifest hops). The resolver
    # collects key_docs declared inline in each parent's dependency
    # entry, then chases the dep's own manifest only when ``depth+1
    # ≤ depth_cap``.
    #
    # To make D's docs genuinely unreachable, we declare them ONLY in
    # D's own manifest — not inline as key_docs of C's dependency
    # entry. That way the resolver must LOAD D's manifest to see
    # them, and the depth-cap check stops it from doing so.
    a = tmp_path / "repos" / "chain-a"
    b = tmp_path / "repos" / "chain-b"
    c = tmp_path / "repos" / "chain-c"
    d = tmp_path / "repos" / "chain-d"
    for repo in (a, b, c, d):
        repo.mkdir(parents=True)
    _gci_seed_manifest(
        a,
        dependencies={
            "b": {
                "path": "../chain-b",
                "key_docs": [
                    {"path": "docs/specs/b-spec.md", "category": "specs"},
                ],
            }
        },
    )
    _gci_seed_manifest(
        b,
        dependencies={
            "c": {
                "path": "../chain-c",
                "key_docs": [
                    {"path": "docs/specs/c-spec.md", "category": "specs"},
                ],
            }
        },
    )
    # C declares only the dep path, NO inline key_docs — d-spec.md is
    # only discoverable by loading D's manifest at depth=3.
    _gci_seed_manifest(
        c,
        dependencies={
            "d": {
                "path": "../chain-d",
                "key_docs": [],
            }
        },
    )
    # D's own manifest declares d-spec via the "self" pseudo-dependency
    # convention used by ``_gci_seed_manifest``; only reachable if the
    # resolver loads D's manifest (which the depth cap prevents).
    _gci_seed_manifest(
        d,
        key_docs=[{"path": "docs/specs/d-spec.md", "category": "specs"}],
    )
    gci_world["chain_a"] = a
    gci_world["chain_repos"] = [a, b, c, d]


@when("Forge resolves context flags for a subcommand")
def _when_resolves_context(gci_world: dict[str, Any]) -> None:
    # Multi-purpose When-step driving resolve_context_flags directly.
    # Some scenarios pre-populate gci_world["repo"]; the chain scenario
    # uses gci_world["chain_a"]; the missing-manifest case sets
    # gci_world["target_repo"].
    if "chain_a" in gci_world:
        repo = gci_world["chain_a"]
        allowlist = gci_world["chain_repos"]
    elif "target_repo" in gci_world:
        repo = gci_world["target_repo"]
        allowlist = gci_world.get("read_allowlist", [repo])
    else:
        repo = gci_world["repo"]
        allowlist = gci_world.get("read_allowlist", [repo])
    gci_world["resolved"] = resolve_context_flags(repo, "feature-spec", allowlist)


@then("the resolver should stop at the depth cap")
def _then_stops_at_depth_cap(gci_world: dict[str, Any]) -> None:
    # Multi-scenario assertion. Both the @boundary depth-cap scenario
    # AND the @edge-case circular scenario use this step, but their
    # path assertions differ. We branch on the per-scenario state set
    # by the matching Given step: ``chain_a`` for the depth-cap
    # scenario, otherwise the cycle scenario.
    resolved = gci_world["resolved"]
    paths: list[str] = resolved.paths
    if "chain_a" in gci_world:
        # Chain A → B → C → D: B (depth-1) and C (depth-2) docs are
        # included; D (depth-3) docs declared in D's own manifest are
        # excluded because the resolver never loaded D's manifest.
        assert any("b-spec.md" in p for p in paths)
        assert any("c-spec.md" in p for p in paths)
        assert not any("d-spec.md" in p for p in paths)
    else:
        # Cycle A ↔ B: the resolver returned a structured result
        # (didn't infinite-loop) and at least one cycle-detection
        # warning was emitted. The exact path inclusion depends on
        # BFS order; what matters is termination + warning.
        codes = [w.code for w in resolved.warnings]
        assert "context_manifest_cycle_detected" in codes


@then("the resolver should emit a structured cycle-detected warning")
def _then_emits_cycle_detected_warning(gci_world: dict[str, Any]) -> None:
    codes = [w.code for w in gci_world["resolved"].warnings]
    assert "context_manifest_cycle_detected" in codes


@then("the invocation should still proceed with the documents resolved so far")
def _then_proceeds_with_resolved(gci_world: dict[str, Any]) -> None:
    # A non-empty path list means the resolver continued past the
    # depth-cap warning rather than aborting.
    paths: list[str] = gci_world["resolved"].paths
    assert paths


# Missing-manifest scenario


@given("the target repository has no context manifest")
def _given_no_manifest(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "no-manifest"
    repo.mkdir(parents=True)
    # Deliberately do NOT call _gci_seed_manifest.
    gci_world["target_repo"] = repo
    gci_world["read_allowlist"] = [repo]


@when("Forge invokes any GuardKit wrapper for that repository")
def _when_invokes_wrapper_for_no_manifest(gci_world: dict[str, Any]) -> None:
    repo = gci_world["target_repo"]
    gci_world["resolved"] = resolve_context_flags(
        repo, "feature-spec", gci_world["read_allowlist"]
    )


@then("the invocation should proceed with no context flags")
def _then_no_context_flags(gci_world: dict[str, Any]) -> None:
    assert gci_world["resolved"].flags == []
    assert gci_world["resolved"].paths == []


@then(
    "a structured warning should be recorded indicating the manifest was missing"
)
def _then_manifest_missing_warning(gci_world: dict[str, Any]) -> None:
    codes = [w.code for w in gci_world["resolved"].warnings]
    assert "context_manifest_missing" in codes


@then(
    "the reasoning model should be able to see the warning in the returned result"
)
def _then_warning_visible(gci_world: dict[str, Any]) -> None:
    warnings = gci_world["resolved"].warnings
    # Each warning carries a stable code and a non-empty message.
    assert warnings
    for warning in warnings:
        assert warning.code
        assert warning.message


# Outside-allowlist scenario


@given(
    "the manifest references a document whose path is outside the filesystem "
    "read allowlist"
)
def _given_doc_outside_allowlist(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "allowlist-test"
    repo.mkdir(parents=True)
    sibling = tmp_path / "outside-allowlist"
    sibling.mkdir()
    # The doc the resolver would try to include lives inside the repo
    # (so the path-outside-repo check passes) but the allowlist
    # restricts reads to a sibling root, so the path-outside-allowlist
    # check fires.
    _gci_seed_manifest(
        repo,
        key_docs=[{"path": "docs/specs/blocked.md", "category": "specs"}],
    )
    gci_world["repo"] = repo
    gci_world["read_allowlist"] = [sibling]


@then("the out-of-allowlist document should be omitted from the context list")
def _then_outside_allowlist_omitted(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    assert not any("blocked.md" in p for p in paths)


@then("a structured warning should be emitted identifying the omitted document")
def _then_warning_identifies_omitted(gci_world: dict[str, Any]) -> None:
    warnings = gci_world["resolved"].warnings
    assert any(
        w.code == "context_manifest_path_outside_allowlist"
        and "blocked.md" in (w.details.get("path") or "")
        for w in warnings
    )


# Circular-dependency scenario


@given("two repositories' manifests reference one another as dependencies")
def _given_two_repos_circular(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo_a = tmp_path / "repos" / "circ-a"
    repo_b = tmp_path / "repos" / "circ-b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    _gci_seed_manifest(
        repo_a,
        dependencies={
            "b": {
                "path": "../circ-b",
                "key_docs": [
                    {"path": "docs/specs/b-spec.md", "category": "specs"},
                ],
            }
        },
    )
    _gci_seed_manifest(
        repo_b,
        dependencies={
            "a": {
                "path": "../circ-a",
                "key_docs": [
                    {"path": "docs/specs/a-spec.md", "category": "specs"},
                ],
            }
        },
    )
    gci_world["repo"] = repo_a
    gci_world["read_allowlist"] = [repo_a, repo_b]


@then("the resolver should detect the cycle before repeating a manifest")
def _then_cycle_before_repeat(gci_world: dict[str, Any]) -> None:
    # The resolver returns successfully (no exception). The "cycle
    # before repeat" assertion is that resolution terminated and a
    # cycle warning is present.
    codes = [w.code for w in gci_world["resolved"].warnings]
    assert "context_manifest_cycle_detected" in codes


@then("a structured cycle-detected warning should be recorded")
def _then_cycle_detected_warning(gci_world: dict[str, Any]) -> None:
    warnings = gci_world["resolved"].warnings
    cycle_warnings = [w for w in warnings if w.code == "context_manifest_cycle_detected"]
    assert cycle_warnings
    for warning in cycle_warnings:
        assert warning.message
        assert warning.details


# Path-traversal scenario


@given(
    "the context manifest lists a document path that resolves outside the "
    "target repository"
)
def _given_path_traversal_doc(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "traversal"
    repo.mkdir(parents=True)
    # Materialise an "outside" file the manifest will reference via "../".
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leaked.md").write_text("", encoding="utf-8")
    # Plus a legitimate inside doc so the "remaining valid documents"
    # assertion has something to validate.
    _gci_seed_manifest(
        repo,
        key_docs=[
            {"path": "../outside/leaked.md", "category": "specs"},
            {"path": "docs/specs/legit.md", "category": "specs"},
        ],
    )
    gci_world["repo"] = repo
    gci_world["read_allowlist"] = [repo, outside_dir]


@then("the out-of-repository path should be omitted from the context list")
def _then_path_outside_repo_omitted(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    assert not any("leaked.md" in p for p in paths)


@then("a structured warning should be emitted identifying the rejected path")
def _then_path_traversal_warning(gci_world: dict[str, Any]) -> None:
    warnings = gci_world["resolved"].warnings
    assert any(
        w.code == "context_manifest_path_outside_repo"
        and "leaked.md" in (w.details.get("path") or "")
        for w in warnings
    )


@then("the invocation should proceed with the remaining valid documents")
def _then_proceeds_with_valid(gci_world: dict[str, Any]) -> None:
    paths: list[str] = gci_world["resolved"].paths
    assert any("legit.md" in p for p in paths)


# Concurrent-builds scenario


@given("two builds are running at the same time against the same target repository")
def _given_two_builds_same_repo(gci_world: dict[str, Any], tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "concurrent-target"
    repo.mkdir(parents=True)
    # Manifest with an ambiguous doc — different allowlists per build
    # will produce different inclusion outcomes (proves resolver is
    # stateless and sees per-call allowlist).
    _gci_seed_manifest(
        repo,
        key_docs=[
            {"path": "docs/specs/shared.md", "category": "specs"},
        ],
    )
    sibling = tmp_path / "isolated-allowlist"
    sibling.mkdir()
    gci_world["repo"] = repo
    # Build 1 has full allowlist; build 2's allowlist excludes the repo.
    gci_world["build1_allowlist"] = [repo]
    gci_world["build2_allowlist"] = [sibling]


@when("each build invokes a GuardKit wrapper requiring context resolution")
def _when_each_build_invokes(gci_world: dict[str, Any]) -> None:
    repo = gci_world["repo"]
    gci_world["build1_resolved"] = resolve_context_flags(
        repo, "feature-spec", gci_world["build1_allowlist"]
    )
    gci_world["build2_resolved"] = resolve_context_flags(
        repo, "feature-spec", gci_world["build2_allowlist"]
    )


@then("each build should receive its own resolved context flag list")
def _then_each_build_own_resolution(gci_world: dict[str, Any]) -> None:
    r1 = gci_world["build1_resolved"]
    r2 = gci_world["build2_resolved"]
    # Build 1 sees the doc; build 2's allowlist excludes it.
    assert any("shared.md" in p for p in r1.paths)
    assert not any("shared.md" in p for p in r2.paths)
    # Distinct list identities (resolver returns fresh lists).
    assert r1.paths is not r2.paths


@then(
    "one build's resolver warnings should not appear in the other build's result"
)
def _then_warnings_isolated(gci_world: dict[str, Any]) -> None:
    r1 = gci_world["build1_resolved"]
    r2 = gci_world["build2_resolved"]
    # Build 2 emitted the path-outside-allowlist warning for shared.md;
    # build 1 did not.
    r2_codes = {w.code for w in r2.warnings}
    r1_codes = {w.code for w in r1.warnings}
    assert "context_manifest_path_outside_allowlist" in r2_codes
    assert "context_manifest_path_outside_allowlist" not in r1_codes


# ---------------------------------------------------------------------------
# TASK-GCI-009 — Generic wrapper error contract scenario
# ---------------------------------------------------------------------------


@pytest.mark.edge_case
@scenario(
    FEATURE_FILE,
    "An unexpected error inside a wrapper is returned as a structured error, "
    "not raised",
)
def test_edge_case_unexpected_error_returned_structured() -> None:
    """@edge-case — TASK-GCI-009 universal error contract."""


@given("any GuardKit wrapper is invoked")
def _given_any_wrapper_invoked(gci_world: dict[str, Any], tmp_path: Path) -> None:
    worktree = tmp_path / "builds" / "B-bdd-009"
    worktree.mkdir(parents=True)
    gci_world["worktree"] = worktree


@when("an unexpected internal error occurs inside the wrapper")
def _when_unexpected_internal_error(
    gci_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch the shared `_invoke` helper to raise a synthetic exception
    # outside the inner ``run()`` body — this exercises the wrapper's
    # outer try/except (the ADR-ARCH-025 boundary swallow).
    async def _exploding_invoke(**kwargs: Any) -> str:
        raise RuntimeError("synthetic internal error: telemetry seam blew up")

    monkeypatch.setattr(
        "forge.tools.guardkit._invoke", _exploding_invoke
    )

    async def _drive() -> str:
        return await guardkit_feature_spec.ainvoke(
            {
                "repo": str(gci_world["worktree"]),
                "feature_description": "BDD oracle: unexpected error",
            }
        )

    gci_world["raw_json"] = asyncio.run(_drive())


@then("the wrapper should catch the error")
def _then_wrapper_catches_error(gci_world: dict[str, Any]) -> None:
    raw = gci_world["raw_json"]
    # If the wrapper had not caught the error, ``asyncio.run`` would
    # have propagated the exception. Reaching this point at all proves
    # the catch path fired.
    assert isinstance(raw, str)


@then(
    "the wrapper should return a structured error result describing the error "
    "type and message"
)
def _then_returns_structured_error(gci_world: dict[str, Any]) -> None:
    payload = json.loads(gci_world["raw_json"])
    assert payload["status"] == "error"
    assert "RuntimeError" in payload["error"]
    assert "synthetic internal error" in payload["error"]
    assert payload["tool"] == "guardkit_feature_spec"


@then("no exception should propagate to the reasoning model")
def _then_no_exception_propagates_to_model(gci_world: dict[str, Any]) -> None:
    # Same invariant restated: the JSON is observable, so no exception
    # propagated past the wrapper boundary.
    assert isinstance(gci_world["raw_json"], str)


# ---------------------------------------------------------------------------
# TASK-GCI-010 — Graphiti resolver-bypass scenario
# ---------------------------------------------------------------------------


@pytest.mark.key_example
@scenario(
    FEATURE_FILE,
    "Graphiti GuardKit subcommands skip context-manifest resolution entirely",
)
def test_key_example_graphiti_skips_resolver() -> None:
    """@key-example — TASK-GCI-010 Graphiti resolver-bypass."""


@when("Forge invokes a Graphiti GuardKit wrapper")
def _when_invokes_graphiti_wrapper(
    gci_world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "builds" / "B-bdd-010"
    worktree.mkdir(parents=True)
    # Seed a manifest in the repo to PROVE the resolver would have
    # picked up flags if it had been invoked. The Graphiti wrapper
    # must NOT consult it.
    _gci_seed_manifest(
        worktree,
        always_include=["docs/internal/AGENTS.md"],
        key_docs=[{"path": "docs/specs/manifest.md", "category": "specs"}],
    )
    gci_world["worktree"] = worktree

    captured: dict[str, Any] = {}

    async def _fake_execute(*, command: list[str], cwd: str, timeout: int):
        captured["command"] = list(command)
        captured["cwd"] = cwd
        return ("", "", 0, 0.05, False)

    monkeypatch.setattr(gk_run_module, "_execute_subprocess", _fake_execute)

    # Resolver must NEVER be reached for Graphiti subcommands. Spy via
    # monkeypatch — the test fails loudly if the wrapper ever calls it.
    resolver_called = {"called": False}

    def _spy_resolver(*args: Any, **kwargs: Any) -> Any:
        resolver_called["called"] = True
        raise AssertionError(
            "Graphiti wrapper must not call resolve_context_flags "
            "(DDR-005 / TASK-GCI-010)"
        )

    monkeypatch.setattr(
        "forge.adapters.guardkit.run.resolve_context_flags", _spy_resolver
    )

    gci_world["captured_call"] = captured
    gci_world["resolver_called"] = resolver_called

    async def _drive() -> str:
        return await guardkit_graphiti_query.ainvoke(
            {
                "query": "BDD oracle: bypass test",
                "group": "guardkit__feature_specs",
                "repo": str(worktree),
            }
        )

    gci_world["raw_json"] = asyncio.run(_drive())


@then("no context flags should be assembled")
def _then_no_context_flags_assembled(gci_world: dict[str, Any]) -> None:
    cmd: list[str] = gci_world["captured_call"]["command"]
    assert "--context" not in cmd, cmd


@then("no manifest lookup should be performed for this invocation")
def _then_no_manifest_lookup(gci_world: dict[str, Any]) -> None:
    assert gci_world["resolver_called"]["called"] is False
    # Sanity: the wrapper still produced an observable structured
    # result (success path), not an exception.
    payload = json.loads(gci_world["raw_json"])
    assert payload["status"] in {"success", "failed", "timeout"}


# Re-export for static analysis: the ``guardkit_tools_invoke`` symbol is
# imported solely to keep the monkeypatch target stable across the
# TASK-GCI-009 scenario; reference it here so tooling does not flag the
# import as unused.
assert callable(guardkit_tools_invoke)
