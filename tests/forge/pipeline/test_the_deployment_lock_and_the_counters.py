"""The deployment lock, the target's own counter, and only forwards.

The design's third revision F, fifth revision I, and second revision B. What
is pinned here:

* one row per deployment target, the counter up by one on EVERY grant or
  takeover by any build, and never down;
* a live lock is left alone; an expired one may be taken over, and the
  takeover cancels the previous holder — its next write changes no row;
* the two counters are separate: a build on turn 3 and a build on turn 1 are
  both ordinary, and the target's counter is what decides who owns the target;
* what is running (R) survives a new connection to the same file, which is a
  restart;
* only forwards: nothing running, an ancestor, a descendant, a divergence, and
  git that cannot say — five answers, and only one of them deploys.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from forge.lifecycle.migrations import apply_at_boot
from forge.pipeline.deployment_lock import (
    DeploymentLockStore,
    default_deploy_lease_seconds,
    deployment_target_name,
)
from forge.pipeline.only_forwards import what_to_do_about_j

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
TARGET = "bench/widget-shop::live"


@pytest.fixture
def ledger(tmp_path):
    connection = sqlite3.connect(tmp_path / "forge.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    apply_at_boot(connection)
    yield connection
    connection.close()


@pytest.fixture
def store(ledger) -> DeploymentLockStore:
    return DeploymentLockStore(ledger)


class TestTheTargetsName:
    def test_it_is_the_project_and_what_the_project_declared(self) -> None:
        assert deployment_target_name("org/thing", "live") == "org/thing::live"

    def test_a_project_that_declares_no_environment_is_still_a_target(self) -> None:
        assert deployment_target_name("org/thing", None) == "org/thing"
        assert deployment_target_name("org/thing", "  ") == "org/thing"

    def test_nothing_is_guessed_when_there_is_no_project_either(self) -> None:
        assert deployment_target_name("", "live") == "unnamed-project::live"


class TestTheCounter:
    def test_nothing_deployed_reads_as_not_recorded(self, store) -> None:
        read = store.read(TARGET)
        assert read.recorded is False
        assert read.counter == 0
        assert read.nothing_is_running is True

    def test_the_first_grant_is_counter_one(self, store) -> None:
        grant = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        assert grant is not None
        assert grant.counter == 1
        assert grant.build_id == "build-a"

    def test_a_live_lock_is_left_alone(self, store) -> None:
        store.grant(target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW)
        assert (
            store.grant(
                target=TARGET, build_id="build-b", turn=1, holder="w2", now=NOW
            )
            is None
        )

    def test_an_expired_lock_is_taken_over_and_the_counter_goes_up(
        self, store
    ) -> None:
        store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW, seconds=1
        )
        later = NOW + timedelta(seconds=30)
        grant = store.grant(
            target=TARGET, build_id="build-b", turn=1, holder="w2", now=later
        )
        assert grant is not None
        assert grant.counter == 2
        assert grant.took_over_from == "build-a"

    def test_the_previous_holders_next_write_changes_no_row(self, store) -> None:
        first = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW, seconds=1
        )
        later = NOW + timedelta(seconds=30)
        store.grant(target=TARGET, build_id="build-b", turn=1, holder="w2", now=later)
        assert first is not None
        assert (
            store.record_running(
                target=TARGET,
                counter=first.counter,
                now=later,
                commit="c" * 40,
                identity="j-aaaa@ffff",
                build_id="build-a",
            )
            is False
        )
        assert store.renew(target=TARGET, counter=first.counter, now=later) is False
        assert store.release(target=TARGET, counter=first.counter, now=later) is False

    def test_a_release_does_not_lower_the_counter(self, store) -> None:
        grant = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        assert grant is not None
        assert store.release(target=TARGET, counter=grant.counter, now=NOW) is True
        read = store.read(TARGET)
        assert read.counter == 1
        assert read.holder_build is None

    def test_a_release_lets_the_next_build_take_it_at_once(self, store) -> None:
        grant = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        assert grant is not None
        store.release(target=TARGET, counter=grant.counter, now=NOW)
        again = store.grant(
            target=TARGET, build_id="build-b", turn=1, holder="w2", now=NOW
        )
        assert again is not None
        assert again.counter == 2

    def test_the_same_build_asking_again_gets_a_new_counter_not_a_refusal(
        self, store
    ) -> None:
        """A press that lost its own answer can ask again; two builds cannot."""
        first = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        again = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        assert first is not None and again is not None
        assert again.counter == first.counter + 1


class TestTheTwoCountersCountDifferentThings:
    def test_a_build_on_turn_three_then_a_build_on_turn_one(self, store) -> None:
        """Case 28. The target's counter decides, never the build's turn.

        Build A is taken over twice, so its OWN turn number is 3, and it
        finishes its deploy. Build B then starts normally, at turn 1 of its own
        record, and is granted the target's lock with the NEXT target counter.
        Its deploy must be accepted: comparing 1 against 3 would refuse the
        build that legitimately owns the target.
        """
        a = store.grant(
            target=TARGET, build_id="build-a", turn=3, holder="wa", now=NOW
        )
        assert a is not None
        store.record_running(
            target=TARGET,
            counter=a.counter,
            now=NOW,
            commit="a" * 40,
            identity="j-aaaaaaaa@1111",
            build_id="build-a",
        )
        store.release(target=TARGET, counter=a.counter, now=NOW)

        b = store.grant(
            target=TARGET, build_id="build-b", turn=1, holder="wb", now=NOW
        )
        assert b is not None
        # The BUILD's turn went 3 -> 1. The TARGET's counter went 1 -> 2.
        assert b.turn == 1
        assert b.counter == a.counter + 1
        assert b.running_commit == "a" * 40
        assert b.running_identity == "j-aaaaaaaa@1111"


class TestWhatIsRunning:
    def test_it_survives_a_new_connection_to_the_same_file(self, tmp_path) -> None:
        path = tmp_path / "forge.db"
        first = sqlite3.connect(path, isolation_level=None)
        apply_at_boot(first)
        store = DeploymentLockStore(first)
        grant = store.grant(
            target=TARGET, build_id="build-a", turn=1, holder="w1", now=NOW
        )
        assert grant is not None
        store.record_running(
            target=TARGET,
            counter=grant.counter,
            now=NOW,
            commit="d" * 40,
            identity="j-dddddddd@2222",
            build_id="build-a",
        )
        first.close()

        second = sqlite3.connect(path, isolation_level=None)
        read = DeploymentLockStore(second).read(TARGET)
        second.close()
        assert read.running_commit == "d" * 40
        assert read.running_identity == "j-dddddddd@2222"
        assert read.running_build == "build-a"

    def test_the_lease_is_long_enough_to_outlast_a_deploy_command(self) -> None:
        from forge.deploy_sidecar.deploy_executor import DEFAULT_COMMAND_SECONDS

        assert default_deploy_lease_seconds() > DEFAULT_COMMAND_SECONDS


class _Git:
    """Answers the one question only-forwards asks, from a written-down map."""

    def __init__(self, answers: dict[tuple[str, str], bool | None]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, str]] = []

    async def is_ancestor(self, ancestor: str, descendant: str):
        self.asked.append((ancestor, descendant))
        return self.answers.get((ancestor, descendant), False)


J = "j" * 40
R = "r" * 40


class TestOnlyForwards:
    @pytest.mark.asyncio
    async def test_nothing_running_deploys(self) -> None:
        answer = await what_to_do_about_j(
            _Git({}), j_commit=J, running_commit=None, target=TARGET
        )
        assert answer.word == "deploy"
        assert answer.go is True
        assert "nothing is recorded as running" in answer.sentence

    @pytest.mark.asyncio
    async def test_what_is_running_is_part_of_j_deploys(self) -> None:
        answer = await what_to_do_about_j(
            _Git({(R, J): True}), j_commit=J, running_commit=R, target=TARGET
        )
        assert answer.word == "deploy"

    @pytest.mark.asyncio
    async def test_j_is_part_of_what_is_running_does_not(self) -> None:
        answer = await what_to_do_about_j(
            _Git({(R, J): False, (J, R): True}),
            j_commit=J,
            running_commit=R,
            target=TARGET,
        )
        assert answer.word == "already-running"
        assert answer.go is False
        assert "a later result that includes it is already running" in answer.sentence

    @pytest.mark.asyncio
    async def test_the_same_commit_is_already_running(self) -> None:
        answer = await what_to_do_about_j(
            _Git({}), j_commit=J, running_commit=J, target=TARGET
        )
        assert answer.word == "already-running"

    @pytest.mark.asyncio
    async def test_neither_contains_the_other_stops_for_a_person(self) -> None:
        answer = await what_to_do_about_j(
            _Git({(R, J): False, (J, R): False}),
            j_commit=J,
            running_commit=R,
            target=TARGET,
        )
        assert answer.word == "neither-contains-the-other"
        assert "left for a person" in answer.sentence

    @pytest.mark.asyncio
    async def test_git_that_cannot_say_is_its_own_ending(self) -> None:
        answer = await what_to_do_about_j(
            _Git({(R, J): None, (J, R): None}),
            j_commit=J,
            running_commit=R,
            target=TARGET,
        )
        assert answer.word == "cannot-tell"
        assert answer.go is False

    @pytest.mark.asyncio
    async def test_git_that_raises_is_cannot_tell_and_never_a_crash(self) -> None:
        class _Explodes:
            async def is_ancestor(self, ancestor: str, descendant: str):
                raise RuntimeError("git went away")

        answer = await what_to_do_about_j(
            _Explodes(), j_commit=J, running_commit=R, target=TARGET
        )
        assert answer.word == "cannot-tell"

    @pytest.mark.asyncio
    async def test_no_joined_commit_deploys_nothing(self) -> None:
        answer = await what_to_do_about_j(
            _Git({}), j_commit="", running_commit=None, target=TARGET
        )
        assert answer.go is False
