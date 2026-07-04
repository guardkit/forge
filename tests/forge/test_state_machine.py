"""Tests for the lifecycle state machine (TASK-PSM-004).

These tests are the property-test arm of the state-machine acceptance
criteria. They exercise the transition table, the terminal-state
invariant ("no transition out of a terminal state"), the
``completed_at`` auto-population invariant, the PAUSED-recovery
``pending_approval_request_id`` carry-forward, and the static-analysis
"single SQL writer" check that FEAT-FORGE-001 sc_001 demands.

The static-analysis test runs ``grep`` over the ``src/`` tree and is
deliberately permissive at this point in the wave: TASK-PSM-005 will
introduce exactly one ``UPDATE builds SET status`` site inside
``persistence.apply_transition``. Until that task lands, the count is
zero. The assertion accepts ``0`` or ``1`` so the test is meaningful in
both pre- and post-PSM-005 worlds without becoming a tripwire across
wave boundaries.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.lifecycle import state_machine as sm
from forge.lifecycle.state_machine import (
    InvalidTransitionError,
    TERMINAL_STATES,
    TRANSITION_TABLE,
    Transition,
    transition,
)
from forge.pipeline.supervisor import BuildState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeBuild:
    """Stand-in for the TASK-PSM-003 ``Build`` Pydantic model.

    The state machine only needs ``status`` + ``build_id`` from the
    build instance. Using a tiny dataclass here keeps the test isolated
    from the TASK-PSM-003 producer — these tests must pass before that
    task lands as well as after it.
    """

    build_id: str
    status: BuildState


def _build(state: BuildState, build_id: str = "build-test-1") -> _FakeBuild:
    return _FakeBuild(build_id=build_id, status=state)


# Expected transition rows mirrored from the task brief. Kept as a plain
# dict so the property tests can compare structurally without importing
# the production constant (the AC explicitly requires "every state has a
# documented transition row").
_EXPECTED_TABLE: dict[BuildState, set[BuildState]] = {
    BuildState.QUEUED: {
        BuildState.PREPARING,
        BuildState.INTERRUPTED,
        BuildState.CANCELLED,
    },
    BuildState.PREPARING: {
        BuildState.RUNNING,
        BuildState.FAILED,
        BuildState.INTERRUPTED,
        BuildState.CANCELLED,
    },
    BuildState.RUNNING: {
        BuildState.PAUSED,
        BuildState.FINALISING,
        BuildState.FAILED,
        BuildState.INTERRUPTED,
        BuildState.CANCELLED,
        BuildState.SKIPPED,
    },
    BuildState.PAUSED: {
        BuildState.RUNNING,
        BuildState.FINALISING,
        BuildState.FAILED,
        BuildState.CANCELLED,
        BuildState.SKIPPED,
    },
    BuildState.FINALISING: {
        BuildState.COMPLETE,
        BuildState.FAILED,
        BuildState.INTERRUPTED,
    },
    BuildState.INTERRUPTED: {
        BuildState.QUEUED,
        BuildState.PREPARING,
    },
    BuildState.COMPLETE: set(),
    BuildState.FAILED: set(),
    BuildState.CANCELLED: set(),
    BuildState.SKIPPED: set(),
}


# ---------------------------------------------------------------------------
# AC: BuildState enum re-exported (single source of truth)
# ---------------------------------------------------------------------------


class TestBuildStateReExport:
    """``BuildState`` must be re-exported from the supervisor enum."""

    def test_enum_is_re_exported_from_supervisor(self) -> None:
        from forge.pipeline import supervisor

        assert sm.BuildState is supervisor.BuildState

    def test_buildstate_includes_all_lifecycle_states(self) -> None:
        # The schema CHECK constraint (TASK-PSM-002) is the source of
        # truth — every state listed there must be enumerated here.
        expected = {
            "QUEUED",
            "PREPARING",
            "RUNNING",
            "PAUSED",
            "FINALISING",
            "COMPLETE",
            "FAILED",
            "INTERRUPTED",
            "CANCELLED",
            "SKIPPED",
        }
        assert {s.value for s in BuildState} == expected


# ---------------------------------------------------------------------------
# AC: TRANSITION_TABLE — frozen mapping, immutable at runtime
# ---------------------------------------------------------------------------


class TestTransitionTable:
    """The transition table is the authoritative graph."""

    def test_every_state_has_a_row(self) -> None:
        # AC: "every state has a documented transition row".
        assert set(TRANSITION_TABLE.keys()) == set(BuildState)

    def test_table_matches_specification(self) -> None:
        for state, expected_targets in _EXPECTED_TABLE.items():
            assert TRANSITION_TABLE[state] == frozenset(expected_targets), (
                f"transition row for {state} does not match the spec"
            )

    def test_target_sets_are_frozen(self) -> None:
        # Per-state target sets are ``frozenset`` so callers cannot
        # accidentally mutate the graph at runtime (Group C invariant).
        for state, targets in TRANSITION_TABLE.items():
            assert isinstance(targets, frozenset), (
                f"transition row for {state} must be a frozenset"
            )

    def test_terminal_rows_are_empty(self) -> None:
        for state in TERMINAL_STATES:
            assert TRANSITION_TABLE[state] == frozenset(), (
                f"{state} is terminal — its transition row must be empty"
            )

    def test_terminal_states_match_buildstate_is_terminal(self) -> None:
        # Terminal-set drift between the state machine and the
        # ``BuildState.is_terminal`` property is the F-class bug class
        # this assertion guards against.
        terminal_via_property = {s for s in BuildState if s.is_terminal}
        assert TERMINAL_STATES == terminal_via_property


# ---------------------------------------------------------------------------
# AC: transition() — happy path returns a Transition value object
# ---------------------------------------------------------------------------


class TestTransitionHappyPath:
    """Every cell in the table must be reachable via ``transition()``."""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (s, t)
            for s, targets in _EXPECTED_TABLE.items()
            for t in targets
        ],
    )
    def test_every_allowed_transition_succeeds(
        self,
        from_state: BuildState,
        to_state: BuildState,
    ) -> None:
        before = datetime.now(UTC) - timedelta(seconds=1)
        result = transition(_build(from_state), to_state)
        after = datetime.now(UTC) + timedelta(seconds=1)

        assert isinstance(result, Transition)
        assert result.build_id == "build-test-1"
        assert result.from_state == from_state
        assert result.to_state == to_state
        assert before <= result.occurred_at <= after

    def test_returns_frozen_value_object(self) -> None:
        result = transition(_build(BuildState.QUEUED), BuildState.PREPARING)
        # Pydantic's ``frozen=True`` raises ValidationError on
        # post-construction mutation.
        with pytest.raises(ValidationError):
            result.build_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC: out-of-table jump raises InvalidTransitionError
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Group C invariant — illegal jumps are refused."""

    def test_invalid_jump_raises(self) -> None:
        # QUEUED → COMPLETE is not in the table.
        with pytest.raises(InvalidTransitionError) as exc:
            transition(_build(BuildState.QUEUED), BuildState.COMPLETE)
        assert exc.value.build_id == "build-test-1"
        assert exc.value.from_state == BuildState.QUEUED
        assert exc.value.to_state == BuildState.COMPLETE

    def test_error_is_a_valueerror(self) -> None:
        # InvalidTransitionError subclasses ValueError so callers using
        # broad ``except ValueError`` still catch it (boundary-error
        # handling rule).
        assert issubclass(InvalidTransitionError, ValueError)

    def test_error_message_includes_state_names(self) -> None:
        with pytest.raises(InvalidTransitionError) as exc:
            transition(_build(BuildState.RUNNING), BuildState.QUEUED)
        message = str(exc.value)
        assert "RUNNING" in message
        assert "QUEUED" in message
        assert "build-test-1" in message


# ---------------------------------------------------------------------------
# AC: no transition out of a terminal state succeeds
# ---------------------------------------------------------------------------


class TestTerminalNoEscape:
    """Property test — no transition out of a terminal state succeeds."""

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
    @pytest.mark.parametrize("target", list(BuildState))
    def test_no_transition_out_of_terminal(
        self,
        terminal: BuildState,
        target: BuildState,
    ) -> None:
        with pytest.raises(InvalidTransitionError):
            transition(_build(terminal), target)


# ---------------------------------------------------------------------------
# AC: terminal transitions auto-populate completed_at
# ---------------------------------------------------------------------------


class TestCompletedAtInvariant:
    """Group G — completion time is always recorded for terminal states."""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (BuildState.FINALISING, BuildState.COMPLETE),
            (BuildState.RUNNING, BuildState.FAILED),
            (BuildState.QUEUED, BuildState.CANCELLED),
            (BuildState.RUNNING, BuildState.SKIPPED),
        ],
    )
    def test_completed_at_auto_populated(
        self,
        from_state: BuildState,
        to_state: BuildState,
    ) -> None:
        result = transition(_build(from_state), to_state)
        assert result.completed_at is not None
        # ``completed_at`` mirrors ``occurred_at`` when the caller does
        # not provide one — same UTC instant covers the full record.
        assert result.completed_at == result.occurred_at

    def test_explicit_completed_at_is_preserved(self) -> None:
        explicit = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        result = transition(
            _build(BuildState.FINALISING),
            BuildState.COMPLETE,
            completed_at=explicit,
        )
        assert result.completed_at == explicit
        # The caller-provided timestamp is recorded verbatim — not
        # overwritten by the auto-population logic.
        assert result.completed_at != result.occurred_at

    def test_non_terminal_transition_does_not_set_completed_at(self) -> None:
        result = transition(_build(BuildState.QUEUED), BuildState.PREPARING)
        assert result.completed_at is None


# ---------------------------------------------------------------------------
# AC: PAUSED carries pending_approval_request_id (review finding F4)
# ---------------------------------------------------------------------------


class TestPausedApprovalCarryForward:
    """F4 — PAUSED-recovery idempotency requires the request id."""

    def test_paused_transition_carries_request_id(self) -> None:
        result = transition(
            _build(BuildState.RUNNING),
            BuildState.PAUSED,
            pending_approval_request_id="req-abc-123",
        )
        assert result.pending_approval_request_id == "req-abc-123"

    def test_resume_from_paused_can_clear_the_id(self) -> None:
        # Resuming back into RUNNING omits the field; default is None
        # so the persistence layer clears the column on the UPDATE.
        result = transition(_build(BuildState.PAUSED), BuildState.RUNNING)
        assert result.pending_approval_request_id is None

    def test_failed_transition_can_carry_error(self) -> None:
        result = transition(
            _build(BuildState.RUNNING),
            BuildState.FAILED,
            error="orchestrator timeout after 1800s",
        )
        assert result.error == "orchestrator timeout after 1800s"

    def test_complete_transition_can_carry_pr_url(self) -> None:
        result = transition(
            _build(BuildState.FINALISING),
            BuildState.COMPLETE,
            pr_url="https://github.com/example/repo/pull/42",
        )
        assert result.pr_url == "https://github.com/example/repo/pull/42"

    def test_unknown_field_is_rejected(self) -> None:
        # ``extra="forbid"`` on the Transition model surfaces typos at
        # the boundary rather than silently dropping the field.
        with pytest.raises(ValidationError):
            transition(
                _build(BuildState.RUNNING),
                BuildState.PAUSED,
                bogus_field="oops",
            )


# ---------------------------------------------------------------------------
# Static-analysis AC: only one writer of ``UPDATE builds SET status``
# ---------------------------------------------------------------------------


class TestSingleStatusWriter:
    """sc_001 — only ``persistence.apply_transition`` writes status SQL."""

    def test_single_status_writer_in_src(self) -> None:
        # Walk the src tree and count occurrences of ``UPDATE builds
        # SET status``. TASK-PSM-005 introduces exactly one such site
        # (in ``persistence.apply_transition``); until that task lands
        # the count is zero. The assertion accepts 0 or 1 so the test
        # is meaningful at both wave checkpoints.
        repo_root = Path(__file__).resolve().parents[2]
        src_root = repo_root / "src"
        pattern = re.compile(r"UPDATE\s+builds\s+SET\s+status", re.IGNORECASE)
        hits: list[Path] = []
        for path in src_root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(path)
        # If the persistence layer exists, the single site must live
        # inside it. If not, no other module is allowed to write status.
        assert len(hits) <= 1, (
            f"expected at most one ``UPDATE builds SET status`` writer, "
            f"found {len(hits)}: {hits}"
        )
        if hits:
            assert "persistence" in hits[0].as_posix(), (
                f"the sole status-writer must live in the persistence layer, "
                f"not {hits[0]}"
            )

    def test_grep_is_consistent_with_python_walk(self) -> None:
        # Belt-and-braces: shell out to grep so the static-analysis
        # check matches the AC's literal phrasing ("grep -r 'UPDATE
        # builds SET status' src/ returns exactly the one location").
        repo_root = Path(__file__).resolve().parents[2]
        src_root = repo_root / "src"
        try:
            result = subprocess.run(
                [
                    "grep",
                    "-r",
                    "-l",
                    "--include=*.py",
                    "UPDATE builds SET status",
                    str(src_root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("grep not available on this platform")
        # grep returns 1 when no matches are found; treat that as zero
        # hits rather than an error.
        if result.returncode not in (0, 1):
            pytest.skip(f"grep failed: {result.stderr.strip()}")
        matched = [
            line for line in result.stdout.splitlines() if line.strip()
        ]
        assert len(matched) <= 1, (
            f"grep found multiple status writers: {matched}"
        )


class TestTransitionChain:
    """``transition_chain`` — multi-hop composition (bridge write-back).

    The lifecycle bridge observes builds in coarser steps than the
    transition graph permits in one hop (a fast run's first envelope
    can be ``build-complete`` against a ``QUEUED`` row). The chain
    helper walks the shortest legal path so callers never hand-roll
    intermediate hops.
    """

    def test_chain_to_same_state_returns_empty_list(self) -> None:
        assert sm.transition_chain(_build(BuildState.QUEUED), BuildState.QUEUED) == []

    def test_single_hop_chain_matches_transition(self) -> None:
        chain = sm.transition_chain(_build(BuildState.QUEUED), BuildState.CANCELLED)
        assert [(t.from_state, t.to_state) for t in chain] == [
            (BuildState.QUEUED, BuildState.CANCELLED)
        ]

    def test_queued_to_complete_walks_the_full_legal_path(self) -> None:
        chain = sm.transition_chain(_build(BuildState.QUEUED), BuildState.COMPLETE)
        assert [(t.from_state, t.to_state) for t in chain] == [
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
            (BuildState.RUNNING, BuildState.FINALISING),
            (BuildState.FINALISING, BuildState.COMPLETE),
        ]

    def test_every_hop_is_table_legal(self) -> None:
        for from_state in BuildState:
            for to_state in BuildState:
                try:
                    chain = sm.transition_chain(_build(from_state), to_state)
                except InvalidTransitionError:
                    continue
                for hop in chain:
                    assert hop.to_state in TRANSITION_TABLE[hop.from_state]

    def test_hops_compose_from_state_to_state(self) -> None:
        chain = sm.transition_chain(_build(BuildState.QUEUED), BuildState.FAILED)
        for earlier, later in zip(chain, chain[1:]):
            assert earlier.to_state is later.from_state

    def test_fields_land_on_final_hop_only(self) -> None:
        chain = sm.transition_chain(
            _build(BuildState.QUEUED),
            BuildState.COMPLETE,
            pr_url="https://example.test/pr/1",
        )
        assert chain[-1].pr_url == "https://example.test/pr/1"
        assert all(t.pr_url is None for t in chain[:-1])

    def test_completed_at_set_on_terminal_hop_only(self) -> None:
        chain = sm.transition_chain(_build(BuildState.QUEUED), BuildState.COMPLETE)
        assert chain[-1].completed_at is not None
        assert all(t.completed_at is None for t in chain[:-1])

    def test_no_path_out_of_terminal_raises(self) -> None:
        with pytest.raises(InvalidTransitionError):
            sm.transition_chain(_build(BuildState.COMPLETE), BuildState.RUNNING)

    def test_error_field_recorded_on_failed_target(self) -> None:
        chain = sm.transition_chain(
            _build(BuildState.RUNNING), BuildState.FAILED, error="boom"
        )
        assert chain[-1].to_state is BuildState.FAILED
        assert chain[-1].error == "boom"
