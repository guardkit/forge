"""The press's ending goes through the lifecycle's own transition seam.

The merge press is now the writer of a build's ending, and this is the half
of that change the state machine cares about: the ending is composed as
legal hops and applied through ``apply_transition``, which stays the sole
writer of ``builds.status``. Nothing about the machine was widened to make
it fit — every state a row can be in when its merge word arrives already
reaches both endings under today's table — and an illegal move is still
refused, so a terminal row can never be re-opened by a second press.

Real SQLite ledgers in temporary directories throughout.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations
from forge.lifecycle.persistence import (
    Build,
    SqliteLifecyclePersistence,
)
from forge.lifecycle.state_machine import (
    TERMINAL_STATES,
    TRANSITION_TABLE,
    BuildState,
    InvalidTransitionError,
    Transition,
    transition,
    transition_chain,
)
from forge.pipeline.merge_executor import MergeDeployOutcome, close_build_row

BUILD_ID = "build-FEAT-39F6-20260910141815"
FEATURE_ID = "FEAT-39F6"
REPO = "appmilla/api_test"


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


def _seed(pool: SqliteLifecyclePersistence, status: str = "RUNNING") -> None:
    pool.connection.execute(
        "INSERT OR REPLACE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', ?, 'cli', 'corr-1', "
        "'2026-09-10T14:18:15Z', 'mode-c')",
        (BUILD_ID, FEATURE_ID, REPO, f"autobuild/{FEATURE_ID}", status),
    )
    pool.connection.commit()


class _SpyPool:
    """The real ledger, with every transition it is asked to apply recorded."""

    def __init__(self, pool: SqliteLifecyclePersistence) -> None:
        self._pool = pool
        self.applied: list[Transition] = []

    def apply_transition(self, transition_value: Transition) -> None:
        self.applied.append(transition_value)
        self._pool.apply_transition(transition_value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def _green() -> MergeDeployOutcome:
    return MergeDeployOutcome(
        result="merged-and-running",
        status="PASSED",
        merged_sha="a1a4c51a1aaa4590ab0e05c9af7818669fe818c9",
        detail=f"{FEATURE_ID} checked in the sandbox (8 of 8), merged and running.",
    )


def _refused(sentence: str) -> MergeDeployOutcome:
    return MergeDeployOutcome(
        result="merge-refused", status="FAILED", failed_step="merge", detail=sentence
    )


# ---------------------------------------------------------------------------
# The write goes through the seam
# ---------------------------------------------------------------------------


class TestTheEndingIsAppliedAsLegalHops:
    def test_a_green_press_walks_the_machine_s_own_path_to_complete(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _seed(pool)
        spy = _SpyPool(pool)

        close_build_row(spy, BUILD_ID, _green())

        assert [(t.from_state, t.to_state) for t in spy.applied] == [
            (BuildState.RUNNING, BuildState.FINALISING),
            (BuildState.FINALISING, BuildState.COMPLETE),
        ]
        assert all(isinstance(t, Transition) for t in spy.applied)
        # The terminal hop records when it ended, and writes no failure text.
        assert spy.applied[-1].completed_at is not None
        assert spy.applied[-1].error is None
        assert pool.get_build_row(BUILD_ID).status is BuildState.COMPLETE

    def test_a_refusal_is_one_hop_carrying_the_refusal_s_own_sentence(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _seed(pool)
        spy = _SpyPool(pool)
        sentence = (
            f"{FEATURE_ID} passed its sandbox check, but main had moved since "
            "this was built; nothing was merged and the branch is kept."
        )

        close_build_row(spy, BUILD_ID, _refused(sentence))

        assert [(t.from_state, t.to_state) for t in spy.applied] == [
            (BuildState.RUNNING, BuildState.FAILED)
        ]
        assert spy.applied[0].error == sentence
        assert pool.get_build_row(BUILD_ID).error == sentence

    def test_a_row_already_closed_composes_no_transition_at_all(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """Not "refused on the way in" — never composed. The press does not
        fight a writer that got there first."""
        _seed(pool, status="CANCELLED")
        spy = _SpyPool(pool)

        reason = close_build_row(spy, BUILD_ID, _green())

        assert spy.applied == []
        assert "already terminal" in (reason or "")
        assert pool.get_build_row(BUILD_ID).status is BuildState.CANCELLED

    def test_an_ending_this_seam_does_not_write_stays_unwritten(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """SKIPPED is the reject shape, and the press never produces it — an
        outcome word this seam has not met is not grounds for an ending."""
        _seed(pool)
        spy = _SpyPool(pool)

        assert (
            close_build_row(
                spy,
                BUILD_ID,
                MergeDeployOutcome(
                    result="rejected", status="SKIPPED", detail="rich said no"
                ),
            )
            is None
        )
        assert spy.applied == []
        assert pool.get_build_row(BUILD_ID).status is BuildState.RUNNING


# ---------------------------------------------------------------------------
# The machine is unchanged
# ---------------------------------------------------------------------------


class TestTheStateMachineStillRules:
    def test_an_illegal_transition_is_still_refused(self) -> None:
        with pytest.raises(InvalidTransitionError):
            transition(
                Build(build_id=BUILD_ID, status=BuildState.COMPLETE),
                BuildState.RUNNING,
            )

    def test_a_terminal_row_can_never_be_re_opened_by_a_second_press(self) -> None:
        for terminal in sorted(TERMINAL_STATES):
            for ending in (BuildState.COMPLETE, BuildState.FAILED):
                if ending is terminal:
                    # Asking for the state it is already in is the empty
                    # answer, never a write.
                    assert (
                        transition_chain(
                            Build(build_id=BUILD_ID, status=terminal), ending
                        )
                        == []
                    )
                    continue
                with pytest.raises(InvalidTransitionError):
                    transition_chain(
                        Build(build_id=BUILD_ID, status=terminal), ending
                    )

    def test_a_press_that_asks_the_impossible_is_refused_not_obeyed(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        """The seam never writes the column itself, so a refusal by the
        machine is a refusal in the ledger too."""
        _seed(pool, status="COMPLETE")
        row_before = pool.get_build_row(BUILD_ID)

        with pytest.raises(RuntimeError):
            pool.apply_transition(
                Transition(
                    build_id=BUILD_ID,
                    from_state=BuildState.RUNNING,
                    to_state=BuildState.FAILED,
                    occurred_at=row_before.queued_at,
                    completed_at=row_before.queued_at,
                    error="a writer that did not read the row first",
                )
            )

        assert pool.get_build_row(BUILD_ID).status is BuildState.COMPLETE

    def test_no_transition_had_to_be_added_for_the_press(self) -> None:
        """Every state a row can be in when its merge word arrives already
        reaches both endings, so nothing was widened to make this fit."""
        for state in sorted(set(TRANSITION_TABLE) - TERMINAL_STATES):
            for ending in (BuildState.COMPLETE, BuildState.FAILED):
                hops = transition_chain(
                    Build(build_id=BUILD_ID, status=state), ending
                )
                assert hops and hops[-1].to_state is ending

    def test_the_table_the_press_walks_is_the_table_that_was_there(self) -> None:
        """A pin, so a later change that widens the machine to make an ending
        fit has to say so here first."""
        assert TRANSITION_TABLE[BuildState.RUNNING] == frozenset(
            {
                BuildState.PAUSED,
                BuildState.FINALISING,
                BuildState.FAILED,
                BuildState.INTERRUPTED,
                BuildState.CANCELLED,
                BuildState.SKIPPED,
            }
        )
        assert TRANSITION_TABLE[BuildState.FINALISING] == frozenset(
            {BuildState.COMPLETE, BuildState.FAILED, BuildState.INTERRUPTED}
        )
        assert all(TRANSITION_TABLE[state] == frozenset() for state in TERMINAL_STATES)


# ---------------------------------------------------------------------------
# The other writer of an ending is untouched
# ---------------------------------------------------------------------------


class TestTheConductorsCloseOutIsUntouched:
    """The conductor's close-out (2026-09-08) still closes a FAILED journey
    and still steps aside for the merge-card path — which is what makes the
    press the only writer there, and why nothing wrote that row at all until
    now. The two never race for the same row."""

    @staticmethod
    def _close_out(pool: SqliteLifecyclePersistence) -> Any:
        from forge.cli._serve_conductor import make_conductor_close_out

        return make_conductor_close_out(pool=pool)

    class _Report:
        def __init__(self, outcome: str, rationale: str, dispatch_result: Any) -> None:
            self.outcome = outcome
            self.rationale = rationale
            self.dispatch_result = dispatch_result

    class _Decision:
        def __init__(self, outcome: str) -> None:
            self.outcome = outcome

    class _Card:
        card_published = True

    def test_a_failed_journey_is_still_closed_failed_exactly_once(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _seed(pool)
        spy = _SpyPool(pool)
        close_out = self._close_out(spy)

        close_out(
            build_id=BUILD_ID,
            report=self._Report(
                "terminal", "failed: the review cycle changed nothing",
                self._Decision("failed"),
            ),
        )

        assert [(t.from_state, t.to_state) for t in spy.applied] == [
            (BuildState.RUNNING, BuildState.FAILED)
        ]
        assert pool.get_build_row(BUILD_ID).status is BuildState.FAILED

        # A second close-out writes nothing more: one ending, as before.
        close_out(
            build_id=BUILD_ID,
            report=self._Report(
                "terminal", "failed: again", self._Decision("failed")
            ),
        )
        assert len(spy.applied) == 1

    def test_the_merge_card_path_is_still_left_to_the_press(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _seed(pool)
        spy = _SpyPool(pool)

        self._close_out(spy)(
            build_id=BUILD_ID,
            report=self._Report(
                "dispatched", "the merge card was published", self._Card()
            ),
        )

        assert spy.applied == []
        assert pool.get_build_row(BUILD_ID).status is BuildState.RUNNING

        # And the press, when its answer comes back, is what closes it.
        close_build_row(spy, BUILD_ID, _green())
        assert pool.get_build_row(BUILD_ID).status is BuildState.COMPLETE
