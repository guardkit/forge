"""The executor is the gate on every deploy command (H, I and J).

REAL CHILD PROCESSES throughout, in a process group of their own, with one
deliberately slow command for the pause and takeover cases. The kernel's own
process table is what survivors are found in; no process's environment is ever
opened, here or in the code under test.

What is pinned:

a. ownership — a lower counter refused, an EQUAL counter from a different
   build refused, a higher one accepted;
b. one command per target, the slot held for the whole life of the process
   GROUP and not for the length of a request;
c. a higher counter while a command is alive stops the old one, WAITS until
   every process in its group is gone and confirms it; unconfirmed ⇒ refuse
   and nothing new starts;
d. the note is durable BEFORE the command starts, and cleared only after every
   process is confirmed gone; the highest counter never goes down;
e. on its own start the executor reconciles: alive ⇒ occupied, none ⇒ cleared;
f. notes gone is not an empty slot — a live marker ⇒ occupied, nothing alive ⇒
   the counter and owning build are CONFIRMED WITH THE COORDINATOR;
g. a hard time limit;
h. the environment door — the named list, never a copy of this process's own.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forge.deploy_sidecar.deploy_executor import (
    DEPLOY_MARKER_PREFIX,
    DeployExecutor,
    DeployRequest,
    ProcessTable,
    request_from,
    the_marker_for,
)

#: Each test gets a deployment target of its OWN, and the reason is worth
#: writing down: rule (f) looks for a live deploy marker for a target across
#: the WHOLE process table, which is exactly right in the running system and
#: means two tests sharing one target name can see each other's children. The
#: fixture below makes the name unique per test.
_TARGET_STEM = "bench/widget-shop"


def _script(where: Path, name: str, body: str) -> str:
    path = where / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return name


@pytest.fixture
def TARGET(tmp_path: Path) -> str:
    """This test's own deployment target, so no test can see another's."""
    return f"{_TARGET_STEM}-{tmp_path.name}::live"


@pytest.fixture
def workshop(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    return tmp_path / "work"


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    where = tmp_path / "notes"
    where.mkdir()
    return where


def _quick(workshop: Path) -> str:
    """A stand-in deploy step: writes what it was handed, reports an identity."""
    return _script(
        workshop,
        "deploy.sh",
        "#!/bin/sh\n"
        'printf "handed=%s\\n" "${DEPLOY_IDENTITY:-nothing}"\n'
        'printf "DEPLOYED_IDENTITY=%s\\n" "${DEPLOY_IDENTITY:-nothing}"\n'
        "exit 0\n",
    )


def _slow(workshop: Path, seconds: int = 120) -> str:
    """One real long-running child, so process-group control is exercised."""
    return _script(
        workshop,
        "slow-deploy.sh",
        "#!/bin/sh\n"
        'printf "starting\\n"\n'
        f"sleep {seconds} &\n"
        f"sleep {seconds}\n",
    )


def _writes_at_the_end(
    workshop: Path, name: str, landed: Path, *, seconds: int
) -> str:
    """A stand-in deploy step that puts its own name on "the target" at the END.

    The path is baked into the script's own text rather than read out of the
    environment, because the environment the child gets is the factory's named
    list and nothing else — which is the door this stage closed.
    """
    return _script(
        workshop,
        f"deploy-{name}.sh",
        "#!/bin/sh\n"
        f'printf "starting {name}\\n"\n'
        + (f"sleep {seconds}\n" if seconds else "")
        + f'printf "{name}\\n" > "{landed}"\n'
        'printf "DEPLOYED_IDENTITY=%s\\n" "${DEPLOY_IDENTITY:-nothing}"\n'
        "exit 0\n",
    )


def _live_groups(table: ProcessTable, target: str) -> list[int]:
    """The process groups of every live deploy command for this target."""
    found = table.carrying(f"{DEPLOY_MARKER_PREFIX}:{_safe(target)}:") or []
    groups = {table.group_of(pid) for pid in found}
    return sorted(group for group in groups if group)


def _ask(
    target: str, script: str, counter: int, build: str, workshop: Path, **kw
) -> DeployRequest:
    return DeployRequest(
        target=target,
        target_counter=counter,
        build=build,
        cwd=str(workshop),
        script=script,
        identity=kw.pop("identity", "j-abcdef@1234"),
        identity_setting=kw.pop("identity_setting", "DEPLOY_IDENTITY"),
        **kw,
    )


def _executor(notes: Path, **kw) -> DeployExecutor:
    return DeployExecutor(notes_root=notes, **kw)


# ---------------------------------------------------------------------------
# (a) ownership
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_a_request_with_no_target_is_refused(self, notes, workshop) -> None:
        answer = _executor(notes).run(
            _ask("", _quick(workshop), 1, "build-a", workshop)
        )
        assert answer.accepted is False
        assert answer.word == "the-request-names-no-target"

    def test_a_request_with_no_build_is_refused(self, notes, workshop, TARGET) -> None:
        answer = _executor(notes).run(
            _ask(TARGET, _quick(workshop), 1, "", workshop)
        )
        assert answer.accepted is False
        assert answer.word == "the-request-names-no-build"

    def test_a_request_with_no_counter_is_refused(self, notes, workshop, TARGET) -> None:
        answer = _executor(notes).run(
            _ask(TARGET, _quick(workshop), 0, "build-a", workshop)
        )
        assert answer.accepted is False
        assert answer.word == "the-request-carries-no-counter"

    def test_a_lower_counter_is_refused(self, notes, workshop, TARGET) -> None:
        executor = _executor(notes)
        script = _quick(workshop)
        assert executor.run(_ask(TARGET, script, 5, "build-a", workshop)).accepted
        answer = executor.run(_ask(TARGET, script, 4, "build-b", workshop))
        assert answer.accepted is False
        assert answer.word == "the-counter-has-moved-on"
        assert "5 has already been accepted" in answer.sentence

    def test_an_equal_counter_from_another_build_is_refused(
        self, notes, workshop, TARGET
    ) -> None:
        """Case 29: one counter belongs to one build."""
        executor = _executor(notes)
        script = _quick(workshop)
        assert executor.run(_ask(TARGET, script, 5, "build-a", workshop)).accepted
        answer = executor.run(_ask(TARGET, script, 5, "build-b", workshop))
        assert answer.accepted is False
        assert answer.word == "that-counter-belongs-to-another-build"
        assert "build-a" in answer.sentence

    def test_the_same_counter_from_the_same_build_still_runs(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes)
        script = _quick(workshop)
        assert executor.run(_ask(TARGET, script, 5, "build-a", workshop)).accepted
        assert executor.run(_ask(TARGET, script, 5, "build-a", workshop)).accepted

    def test_a_higher_counter_is_accepted(self, notes, workshop, TARGET) -> None:
        executor = _executor(notes)
        script = _quick(workshop)
        assert executor.run(_ask(TARGET, script, 5, "build-a", workshop)).accepted
        assert executor.run(_ask(TARGET, script, 6, "build-b", workshop)).accepted

    def test_another_targets_counter_is_its_own(self, notes, workshop, TARGET) -> None:
        executor = _executor(notes)
        script = _quick(workshop)
        assert executor.run(_ask(TARGET, script, 9, "build-a", workshop)).accepted
        other = executor.run(_ask("other::live", script, 1, "build-b", workshop))
        assert other.accepted is True


# ---------------------------------------------------------------------------
# (b, d) one command per target; the note is durable before anything starts
# ---------------------------------------------------------------------------


class TestTheSlotAndTheNote:
    def test_the_note_names_the_target_the_counter_the_build_and_the_command(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes)
        executor.run(_ask(TARGET, _quick(workshop), 3, "build-a", workshop))
        written = json.loads(
            next(notes.glob("*.json")).read_text(encoding="utf-8")
        )
        assert written["highest_counter"] == 3
        # The active command is cleared once it is confirmed gone; the highest
        # counter is what survives, and it is what refuses a delayed request.
        assert written["counter"] == 0

    def test_the_highest_counter_survives_a_new_executor(
        self, notes, workshop, TARGET
    ) -> None:
        script = _quick(workshop)
        _executor(notes).run(_ask(TARGET, script, 7, "build-a", workshop))
        answer = _executor(notes).run(_ask(TARGET, script, 6, "build-b", workshop))
        assert answer.accepted is False
        assert answer.word == "the-counter-has-moved-on"

    def test_nothing_starts_beside_a_live_command_from_the_same_build(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes, stop_confirm_seconds=10.0)
        slow = _slow(workshop)
        started = _Background(executor, _ask(TARGET, slow, 1, "build-a", workshop))
        started.begin()
        try:
            _wait_for_note(notes)
            answer = executor.run(_ask(TARGET, slow, 1, "build-a", workshop))
            assert answer.accepted is False
            assert answer.word == "a-command-is-already-running"
        finally:
            started.stop()


# ---------------------------------------------------------------------------
# (c) a takeover stops the old command and CONFIRMS it is gone
# ---------------------------------------------------------------------------


class TestATakeoverStopsAndConfirms:
    def test_the_old_process_group_is_stopped_and_confirmed_before_anything_new(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes, stop_confirm_seconds=20.0)
        slow = _slow(workshop)
        running = _Background(executor, _ask(TARGET, slow, 1, "build-a", workshop))
        running.begin()
        try:
            note = _wait_for_note(notes)
            group = int(note["group"])
            table = ProcessTable()
            assert table.members_of(group), "the slow command never started"

            answer = executor.run(
                _ask(TARGET, _quick(workshop), 2, "build-b", workshop)
            )
            assert answer.accepted is True, answer.sentence
            # Every process of the OLD command is gone — confirmed, not assumed.
            assert table.members_of(group) == []
            assert "DEPLOYED_IDENTITY=j-abcdef@1234" in answer.output
        finally:
            running.stop()

    def test_a_taken_over_command_never_destroys_its_successors_note(
        self, notes, workshop, TARGET
    ) -> None:
        """THE BLOCKER the stage's reviewer drove, step by step.

        A (counter 5) starts a slow command; B (counter 6) takes over, so A's
        group is stopped and confirmed gone and B's command starts and is
        alive; A's request then returns. A's waiter used to clear the note it
        was holding IN MEMORY — which by then was B's note — leaving
        ``{counter 0, highest 5, highest_build A}`` behind. A third request, C
        (counter 7), then read an EMPTY slot, was accepted, and its command ran
        beside B's live one; C finished and wrote its result, and B's OLDER
        command finished afterwards and overwrote it. Three things went wrong
        at once: two commands on one target, the target's highest counter
        LOWERED from 6 to 5, and the older result left running.

        The note is inspected after every step, ONE live command is asserted at
        every moment, and the NEWEST result is what is on the target at the
        end. The stage's existing takeover test used a QUICK successor and
        never looked at the note or made a third request, which is why it
        passed while this was broken; it is kept, and this stands beside it.
        """
        executor = _executor(notes, stop_confirm_seconds=20.0)
        # WHAT IS "ON THE TARGET": one file each command writes its own name
        # into, at the END of its work. A command that is stopped part-way
        # never writes, which is exactly what a stopped deploy means here.
        landed = workshop / "what-is-on-the-target"
        a_slow = _writes_at_the_end(workshop, "a", landed, seconds=120)
        b_slow = _writes_at_the_end(workshop, "b", landed, seconds=120)
        c_quick = _writes_at_the_end(workshop, "c", landed, seconds=0)
        table = ProcessTable()
        a = _Background(executor, _ask(TARGET, a_slow, 5, "build-a", workshop))
        b = _Background(executor, _ask(TARGET, b_slow, 6, "build-b", workshop))
        a_group = b_group = 0
        try:
            # --- A is running, and the note says so ------------------------
            a.begin()
            note = _wait_for_note(notes, until=lambda n: n["counter"] == 5)
            a_group = int(note["group"])
            assert note["build"] == "build-a"
            assert note["highest_counter"] == 5
            assert table.members_of(a_group), "A's command never started"
            assert _live_groups(table, TARGET) == [a_group]

            # --- B takes over: A is stopped and confirmed gone -------------
            b.begin()
            note = _wait_for_note(notes, until=lambda n: n["counter"] == 6)
            b_group = int(note["group"])
            assert note["build"] == "build-b"
            assert note["highest_counter"] == 6
            assert table.members_of(a_group) == [], "A's group was not confirmed gone"
            # ONE live command, and it is B's.
            assert _live_groups(table, TARGET) == [b_group]

            # --- A's request comes back. IT WRITES NOTHING -----------------
            a.until_answered()
            assert a.answer is not None
            assert a.answer.accepted is False, a.answer.sentence
            assert a.answer.word == "the-deploy-command-was-stopped-by-a-takeover"
            assert "nothing was deployed by this request" in a.answer.sentence
            # B's note is untouched: the counter is still 6 and the group is
            # still B's, so the slot is not empty and the counter has not gone
            # backwards.
            after = _read_note(notes)
            assert after["counter"] == 6
            assert after["build"] == "build-b"
            assert after["group"] == b_group
            assert after["highest_counter"] == 6
            assert after["highest_build"] == "build-b"

            # --- C arrives. It takes over B; it does NOT run beside it -----
            answered = executor.run(_ask(TARGET, c_quick, 7, "build-c", workshop))
            assert answered.accepted is True, answered.sentence
            assert table.members_of(b_group) == [], "B's group was not confirmed gone"
            assert _live_groups(table, TARGET) == []

            # --- B's request comes back. IT WRITES NOTHING EITHER ----------
            b.until_answered()
            assert b.answer is not None
            assert b.answer.accepted is False, b.answer.sentence
            assert b.answer.word == "the-deploy-command-was-stopped-by-a-takeover"

            # --- the note at the end ---------------------------------------
            ended = _read_note(notes)
            assert ended["group"] == 0, "C's command is over, so the slot is free"
            assert ended["highest_counter"] == 7, "the counter never goes down"
            assert ended["highest_build"] == "build-c"

            # --- and the NEWEST result is what is on the target ------------
            assert landed.read_text(encoding="utf-8").strip() == "c"
            # A delayed request from A cannot deploy anything now.
            delayed = executor.run(_ask(TARGET, c_quick, 5, "build-a", workshop))
            assert delayed.accepted is False
            assert delayed.word == "the-counter-has-moved-on"
        finally:
            _kill(a_group)
            _kill(b_group)
            a.stop()
            b.stop()

    def test_a_command_that_cannot_be_confirmed_stopped_refuses(
        self, notes, workshop, TARGET
    ) -> None:
        """Case 27: nothing new is deployed and the reason is said.

        The stop is made impossible by giving the executor a view of the
        process table in which the command never goes away.
        """
        executor = _executor(
            notes,
            stop_confirm_seconds=0.4,
            process_table=_AlwaysAlive(),
        )
        # A note of a live command, written by hand: this test is about the
        # refusal, not about starting anything.
        _write_note(notes, TARGET, counter=1, build="build-a", group=os.getpid())
        answer = executor.run(_ask(TARGET, _quick(workshop), 2, "build-b", workshop))
        assert answer.accepted is False
        assert answer.word == "the-old-command-could-not-be-confirmed-stopped"
        assert "published, deployment pending" in answer.sentence
        assert "not confirmed gone" in answer.sentence

    def test_a_stop_on_one_target_does_not_hold_up_another_target(
        self, notes, workshop, tmp_path
    ) -> None:
        """The slot is per TARGET, and so is the lock that decides it.

        Stopping a command can take the whole stop-and-confirm limit. Under one
        lock across every target — which is what the code did, while its own
        comment promised the opposite — a deploy to a target nobody is stopping
        anything on waited for it.
        """
        stubborn = 2**22 - 1  # a group the stub below never lets go of
        x = f"bench/x-{tmp_path.name}::live"
        y = f"bench/y-{tmp_path.name}::live"
        executor = _executor(
            notes,
            stop_confirm_seconds=6.0,
            process_table=_AliveOnly({stubborn}),
        )
        _write_note(notes, x, counter=1, build="build-a", group=stubborn)
        stopping = _Background(executor, _ask(x, _quick(workshop), 2, "build-b", workshop))
        stopping.begin()
        try:
            # X's stop is under way and will not be confirmed for six seconds.
            time.sleep(0.5)
            began = time.monotonic()
            answer = executor.run(_ask(y, _quick(workshop), 1, "build-c", workshop))
            took = time.monotonic() - began
            assert answer.accepted is True, answer.sentence
            assert took < 3.0, f"Y waited {took:.1f}s on a stop for X"
        finally:
            stopping.stop()


# ---------------------------------------------------------------------------
# (d) the note is written in two parts, and the window is the marker's to cover
# ---------------------------------------------------------------------------


class TestTheNoteIsWrittenInTwoParts:
    def test_a_note_is_on_disk_before_the_command_is_started(
        self, notes, workshop, TARGET
    ) -> None:
        """The provisional note — target, counter, build, marker, "starting"."""
        seen: list[dict] = []

        def _watching_spawn(**kwargs):
            seen.append(_read_note(notes))
            return subprocess.Popen(**kwargs)  # noqa: S603 — the executor's own argv

        answer = _executor(notes, spawn=_watching_spawn).run(
            _ask(TARGET, _quick(workshop), 1, "build-a", workshop)
        )
        assert answer.accepted is True, answer.sentence
        assert len(seen) == 1
        before = seen[0]
        assert before["phase"] == "starting"
        assert before["counter"] == 1
        assert before["build"] == "build-a"
        assert before["group"] == 0, "the group does not exist until it exists"
        assert before["marker"].startswith(f"{DEPLOY_MARKER_PREFIX}:")
        assert before["highest_counter"] == 1

    def test_the_note_is_completed_with_the_group_after_the_command_starts(
        self, notes, workshop, TARGET
    ) -> None:
        answer = _executor(notes).run(
            _ask(TARGET, _quick(workshop), 1, "build-a", workshop)
        )
        assert answer.accepted is True, answer.sentence
        ended = _read_note(notes)
        # The command is over by now, so the slot is free and the counter kept.
        assert ended["group"] == 0
        assert ended["highest_counter"] == 1
        assert ended["highest_build"] == "build-a"

    def test_a_starting_note_is_occupied_while_its_marker_is_alive(
        self, notes, workshop, TARGET
    ) -> None:
        """The window the two writes leave is covered by the MARKER.

        A helper that stopped between writing the note and knowing the
        command's group has no group to look for. The reconciler must not read
        that as an empty slot: it looks for the note's own marker.
        """
        marker = the_marker_for(TARGET)
        _write_note(
            notes,
            TARGET,
            counter=1,
            build="build-a",
            group=0,
            marker=marker,
            phase="starting",
        )
        executor = _executor(notes, process_table=_Carrying({marker}))
        settled = executor.reconcile()
        assert "occupied" in settled[TARGET], settled
        answer = executor.run(_ask(TARGET, _quick(workshop), 2, "build-b", workshop))
        assert answer.accepted is False, answer.sentence
        assert answer.word == "the-slot-is-occupied"
        assert "was being started" in answer.sentence

    def test_a_starting_note_with_nothing_alive_is_cleared_and_the_next_runs(
        self, notes, workshop, TARGET
    ) -> None:
        _write_note(
            notes,
            TARGET,
            counter=1,
            build="build-a",
            group=0,
            marker=the_marker_for(TARGET),
            phase="starting",
        )
        executor = _executor(notes)
        settled = executor.reconcile()
        assert settled[TARGET] == "cleared"
        answer = executor.run(_ask(TARGET, _quick(workshop), 2, "build-b", workshop))
        assert answer.accepted is True, answer.sentence
        assert _read_note(notes)["highest_counter"] == 2


# ---------------------------------------------------------------------------
# (e, f) reconciling on the executor's own start
# ---------------------------------------------------------------------------


class TestReconcilingOnStart:
    def test_a_live_command_makes_the_slot_occupied(self, notes, workshop, TARGET) -> None:
        """Case 30, first half: restarted with a live command."""
        first = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(first, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        try:
            _wait_for_note(notes)
            # A NEW executor — the helper service restarted.
            second = _executor(notes, stop_confirm_seconds=20.0)
            settled = second.reconcile()
            assert settled[TARGET] == "occupied (adopted)"
        finally:
            running.stop()

    def test_a_takeover_after_a_restart_still_stops_and_confirms(
        self, notes, workshop, TARGET
    ) -> None:
        """Case 30, and the rule that a takeover cannot skip the confirmation."""
        first = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(first, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        try:
            note = _wait_for_note(notes)
            group = int(note["group"])
            second = _executor(notes, stop_confirm_seconds=20.0)
            second.reconcile()
            answer = second.run(_ask(TARGET, _quick(workshop), 2, "b", workshop))
            assert answer.accepted is True, answer.sentence
            assert ProcessTable().members_of(group) == []
        finally:
            running.stop()

    def test_a_note_of_a_command_that_has_ended_is_cleared(self, notes, TARGET) -> None:
        """Case 31: restarted after the command ended, before the note cleared."""
        _write_note(notes, TARGET, counter=1, build="build-a", group=_a_dead_group())
        settled = _executor(notes).reconcile()
        assert settled[TARGET] == "cleared"
        written = json.loads(next(notes.glob("*.json")).read_text(encoding="utf-8"))
        assert written["group"] == 0
        assert written["highest_counter"] == 1

    def test_notes_that_cannot_be_read_with_something_alive_is_occupied(
        self, notes, workshop, TARGET
    ) -> None:
        """Case 30, second half: the notes deleted, the command still alive."""
        first = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(first, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        group = 0
        try:
            # THE GROUP IS REMEMBERED BEFORE THE NOTES ARE SPOILED, because the
            # tidy-up below finds what to stop by READING the notes — and this
            # test is about notes that cannot be read. Without it the slow
            # command outlives the test, and the next run of this same test
            # meets a live marker for its own target name and refuses.
            group = int(_wait_for_note(notes)["group"])
            # The notes are made unreadable, as if they had been lost.
            for path in notes.glob("*.json"):
                path.write_text("this is not a note", encoding="utf-8")
            second = _executor(notes, stop_confirm_seconds=20.0)
            settled = second.reconcile()
            assert "occupied" in settled[_safe(TARGET)]
            # ...and the request is refused for the same reason, asked again
            # against the target's own name rather than the note file's.
            answer = second.run(_ask(TARGET, _quick(workshop), 9, "b", workshop))
            assert answer.accepted is False
            assert answer.word == "the-slot-is-occupied"
            assert "still alive" in answer.sentence
        finally:
            _kill(group)
            running.stop()

    def test_notes_gone_and_nothing_alive_asks_the_coordinator(
        self, notes, workshop, TARGET
    ) -> None:
        """Rule (f)'s last clause, and the point the sign-off note watches.

        A delayed request presenting an old counter cannot establish who owns
        the target, so the answer comes from the COORDINATOR, not the request.
        """
        asked: list[str] = []

        def _coordinator(target: str):
            asked.append(target)
            return {"counter": 4, "build": "build-b"}

        executor = _executor(notes, ask_the_coordinator=_coordinator)
        (notes / (_safe(TARGET) + ".json")).write_text("rubbish", encoding="utf-8")
        executor._reconciled = True  # the unreadable note is met on the request

        # The delayed request carries a counter the coordinator does not agree
        # with: refused, and the sentence names both.
        refused = executor.run(_ask(TARGET, _quick(workshop), 2, "build-a", workshop))
        assert refused.accepted is False
        assert refused.word == "the-coordinator-says-somebody-else-owns-it"
        assert "counter 4" in refused.sentence and "build-b" in refused.sentence

        # The request the coordinator DOES agree with is accepted.
        accepted = executor.run(_ask(TARGET, _quick(workshop), 4, "build-b", workshop))
        assert accepted.accepted is True, accepted.sentence
        assert asked == [TARGET, TARGET]

    def test_with_nobody_to_ask_it_refuses_rather_than_believing_the_request(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes)
        (notes / (_safe(TARGET) + ".json")).write_text("rubbish", encoding="utf-8")
        executor._reconciled = True
        answer = executor.run(_ask(TARGET, _quick(workshop), 2, "build-a", workshop))
        assert answer.accepted is False
        assert answer.word == "nobody-can-be-asked-who-owns-it"

    def test_a_process_table_that_cannot_be_read_refuses(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes, process_table=ProcessTable("/nowhere-at-all"))
        _write_note(notes, TARGET, counter=1, build="build-a", group=999999)
        settled = executor.reconcile()
        assert settled[TARGET] == "cannot tell"
        answer = executor.run(_ask(TARGET, _quick(workshop), 2, "b", workshop))
        assert answer.accepted is False
        assert answer.word == "the-slot-cannot-be-settled"


# ---------------------------------------------------------------------------
# (g) the hard time limit
# ---------------------------------------------------------------------------


class TestTheTimeLimit:
    def test_a_command_that_runs_too_long_is_stopped(self, notes, workshop, TARGET) -> None:
        executor = _executor(notes, command_seconds=1.0, stop_confirm_seconds=20.0)
        answer = executor.run(
            _ask(TARGET, _slow(workshop), 1, "build-a", workshop, timeout=1.0)
        )
        assert answer.accepted is True
        assert answer.word == "the-deploy-command-ran-out-of-time"
        assert answer.exit_code == 124

    def test_a_request_cannot_ask_for_longer_than_the_executor_allows(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes, command_seconds=1.0, stop_confirm_seconds=20.0)
        answer = executor.run(
            _ask(TARGET, _slow(workshop), 1, "build-a", workshop, timeout=9999.0)
        )
        assert answer.word == "the-deploy-command-ran-out-of-time"


# ---------------------------------------------------------------------------
# (h) the environment door
# ---------------------------------------------------------------------------


class TestTheEnvironmentDoor:
    def test_the_child_gets_the_named_list_and_not_a_copy_of_this_process(
        self, notes, workshop, monkeypatch, TARGET
    ) -> None:
        monkeypatch.setenv("SOMETHING_ON_NO_LIST", "must-not-travel")
        monkeypatch.setenv("GH_TOKEN", "must-not-travel-either")
        monkeypatch.setenv("SOME_TOOL_CACHE", "the project asked for this")
        script = _script(
            workshop,
            "say-env.sh",
            "#!/bin/sh\n"
            "env | sort\n"
            'printf "DEPLOYED_IDENTITY=%s\\n" "${DEPLOY_IDENTITY:-nothing}"\n',
        )
        answer = _executor(notes).run(
            DeployRequest(
                target=TARGET,
                target_counter=1,
                build="build-a",
                cwd=str(workshop),
                script=script,
                memory_project="widget_shop",
                launch_settings=("SOME_TOOL_CACHE",),
                identity="j-abcdef@1234",
                identity_setting="DEPLOY_IDENTITY",
            )
        )
        assert answer.accepted is True
        said = answer.output
        assert "SOMETHING_ON_NO_LIST" not in said
        assert "GH_TOKEN" not in said
        assert "SOME_TOOL_CACHE=the project asked for this" in said
        assert "GUARDKIT_MEMORY_PROJECT=widget_shop" in said
        assert "GUARDKIT_FACTORY_LAUNCH=1" in said
        assert "DEPLOY_IDENTITY=j-abcdef@1234" in said

    def test_the_identity_travels_under_the_name_the_project_declared(
        self, notes, workshop, TARGET
    ) -> None:
        script = _script(
            workshop,
            "say-ours.sh",
            "#!/bin/sh\n"
            'printf "OURS=%s\\n" "${WIDGET_SHOP_RELEASE:-nothing}"\n',
        )
        answer = _executor(notes).run(
            _ask(
                TARGET,
                script,
                1,
                "build-a",
                workshop,
                identity="j-abcdef@1234",
                identity_setting="WIDGET_SHOP_RELEASE",
            )
        )
        assert "OURS=j-abcdef@1234" in answer.output


# ---------------------------------------------------------------------------
# The marker, and what a request has to carry
# ---------------------------------------------------------------------------


class TestTheMarker:
    def test_it_names_the_target_so_it_can_be_found_with_no_notes(self, TARGET) -> None:
        marker = the_marker_for(TARGET)
        assert marker.startswith(f"{DEPLOY_MARKER_PREFIX}:")
        # The target is written PLAINLY into the marker — a slash becomes a
        # dash because a slash is not something a name may carry, and nothing
        # else is changed — so "is anything deploying to this target?" can be
        # asked with no notes at all.
        assert TARGET.replace("/", "-") in marker

    def test_a_live_command_carries_it_in_its_argument_list(
        self, notes, workshop, TARGET
    ) -> None:
        """Not in its environment — a process's environment is never opened."""
        executor = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(executor, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        try:
            note = _wait_for_note(notes)
            table = ProcessTable()
            carrying = table.carrying(note["marker"])
            assert carrying, "no live process carries the marker"
        finally:
            running.stop()


class TestWhatARequestHasToCarry:
    def test_a_body_with_no_deploy_block_is_refused_in_words(self) -> None:
        assert isinstance(request_from(None), str)
        assert "that target's own counter" in request_from(None)

    def test_each_missing_field_is_named(self, TARGET) -> None:
        assert "name the deployment target" in request_from({})
        assert "name the build" in request_from({"target": TARGET})
        assert "that target's counter" in request_from(
            {"target": TARGET, "build": "b"}
        )

    def test_a_whole_one_becomes_a_request(self, workshop, TARGET) -> None:
        built = request_from(
            {
                "target": TARGET,
                "build": "build-a",
                "target_counter": 3,
                "identity": "j-abcdef@1234",
                "identity_setting": "DEPLOY_IDENTITY",
            },
            cwd=str(workshop),
            script="deploy.sh",
            memory_project="widget_shop",
            launch_settings=("SOME_TOOL_CACHE",),
            timeout=42.0,
        )
        assert isinstance(built, DeployRequest)
        assert built.target_counter == 3
        assert built.identity == "j-abcdef@1234"
        assert built.launch_settings == ("SOME_TOOL_CACHE",)
        # The coordinator said nothing about what is running, so nothing is
        # assumed: the safe reading of an absent field is "this request cannot
        # tell me a deploy has happened before".
        assert built.something_is_running is False

    def test_what_the_coordinator_says_is_running_travels_on_the_request(
        self, workshop, TARGET
    ) -> None:
        """It is read off the ownership block the press built under the lock."""
        built = request_from(
            {
                "target": TARGET,
                "build": "build-a",
                "target_counter": 3,
                "something_is_running": True,
            },
            cwd=str(workshop),
            script="deploy.sh",
        )
        assert isinstance(built, DeployRequest)
        assert built.something_is_running is True


# ---------------------------------------------------------------------------
# (f) A NOTE THAT IS NOT THERE IS NOT AN EMPTY SLOT
#
# The stage's reviewer drove this hole and it was the blocker: MISSING was
# handled as "fine, nothing here", and only UNREADABLE took rule (f)'s branch.
# So a note removed under a live command — or a helper that came back onto an
# empty notes folder, which is what a sandbox restart onto a fresh
# FORGE_DEPLOY_NOTES_DIR does — had a second deploy command started beside the
# first. These are the cases that hole is now closed against.
# ---------------------------------------------------------------------------


class TestANoteThatIsNotThere:
    def test_the_note_is_removed_under_a_live_command_and_the_slot_is_occupied(
        self, notes, workshop, TARGET
    ) -> None:
        """THE BLOCKER, driven: nothing new starts beside a live command."""
        executor = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(executor, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        group = 0
        try:
            group = int(_wait_for_note(notes)["group"])
            # The note is REMOVED, not corrupted. This is the shape a lost
            # folder has, and it used to read as an empty slot.
            for path in notes.glob("*.json"):
                path.unlink()
            answer = executor.run(_ask(TARGET, _quick(workshop), 2, "b", workshop))
            assert answer.accepted is False, answer.sentence
            assert answer.word == "the-slot-is-occupied"
            assert "no note of its own" in answer.sentence
            # ...and the first command is still the only one alive.
            assert ProcessTable().members_of(group)
        finally:
            _kill(group)
            running.stop()

    def test_a_restarted_helper_with_no_notes_at_all_finds_the_live_command(
        self, notes, workshop, TARGET
    ) -> None:
        """Rule (e) and (f) together: reconcile looks in the process table.

        The walk of the notes folder can only see targets it has a file for,
        so a helper whose notes did not survive would have settled NOTHING.
        The command carries its marker in its own argument list for exactly
        this question.
        """
        first = _executor(notes, stop_confirm_seconds=20.0)
        running = _Background(first, _ask(TARGET, _slow(workshop), 1, "a", workshop))
        running.begin()
        group = 0
        try:
            group = int(_wait_for_note(notes)["group"])
            for path in notes.glob("*.json"):
                path.unlink()
            second = _executor(notes, stop_confirm_seconds=20.0)
            settled = second.reconcile()
            assert "occupied" in settled.get(_safe(TARGET), ""), settled
            answer = second.run(_ask(TARGET, _quick(workshop), 3, "c", workshop))
            assert answer.accepted is False, answer.sentence
            assert answer.word == "the-slot-is-occupied"
        finally:
            _kill(group)
            running.stop()

    def test_nothing_alive_and_nothing_ever_run_is_the_first_deploy(
        self, notes, workshop, TARGET
    ) -> None:
        """The one path where a missing note is not a loss, and why.

        Requiring the coordinator here would mean the first deploy of every
        target needs an answer about a target nobody has ever heard of, and
        the deploy path would be dead on the day it is installed.
        """
        answer = _executor(notes).run(_ask(TARGET, _quick(workshop), 1, "a", workshop))
        assert answer.accepted is True, answer.sentence

    def test_nothing_alive_but_something_is_running_there_is_a_lost_note(
        self, notes, workshop, TARGET
    ) -> None:
        """The coordinator read R under the lock and says something is running.

        Then a deploy has happened before, so a note SHOULD exist, so its
        absence is a loss — and with nobody to ask, that is refused.
        """
        answer = _executor(notes).run(
            _ask(
                TARGET,
                _quick(workshop),
                4,
                "build-b",
                workshop,
                something_is_running=True,
            )
        )
        assert answer.accepted is False, answer.sentence
        assert answer.word == "nobody-can-be-asked-who-owns-it"
        assert "something is already running" in answer.sentence

    def test_with_a_coordinator_the_missing_note_is_settled_by_asking_it(
        self, notes, workshop, TARGET
    ) -> None:
        """Rule (f)'s last clause on the MISSING half, not just the unreadable one."""
        asked: list[str] = []

        def _coordinator(target: str):
            asked.append(target)
            return {"counter": 7, "build": "build-c"}

        executor = _executor(notes, ask_the_coordinator=_coordinator)
        refused = executor.run(
            _ask(TARGET, _quick(workshop), 6, "build-b", workshop,
                 something_is_running=True)
        )
        assert refused.accepted is False
        assert refused.word == "the-coordinator-says-somebody-else-owns-it"
        accepted = executor.run(
            _ask(TARGET, _quick(workshop), 7, "build-c", workshop,
                 something_is_running=True)
        )
        assert accepted.accepted is True, accepted.sentence
        assert asked == [TARGET, TARGET]

    def test_a_process_table_that_cannot_be_read_refuses_a_missing_note_too(
        self, notes, workshop, TARGET
    ) -> None:
        executor = _executor(notes, process_table=ProcessTable("/nowhere-at-all"))
        answer = executor.run(_ask(TARGET, _quick(workshop), 1, "a", workshop))
        assert answer.accepted is False, answer.sentence
        assert answer.word == "the-slot-cannot-be-settled"
        assert "could not be told" in answer.sentence


class TestTheNotesFolderItself:
    def test_a_folder_that_cannot_be_written_is_said_at_the_start(
        self, tmp_path, workshop, TARGET
    ) -> None:
        """The setting is named, once, rather than found in the middle of a merge."""
        where = tmp_path / "read-only-notes"
        where.mkdir()
        where.chmod(0o500)
        try:
            executor = _executor(where)
            executor.reconcile()
            answer = executor.run(_ask(TARGET, _quick(workshop), 1, "a", workshop))
            assert answer.accepted is False, answer.sentence
            assert answer.word == "the-executors-notes-folder-cannot-be-used"
            assert "FORGE_DEPLOY_NOTES_DIR" in answer.sentence
        finally:
            where.chmod(0o700)


class TestTheRunningServiceCanAskTheCoordinator:
    """Rule (f)'s question is reachable in the service, not only in a test.

    The helper used to build its executor with no way to ask, so in the
    running system that branch could only ever answer "nobody can be asked".
    It is an operator's setting now: an address for the coordinator's own
    read-only answer. Unset, the refusal is exactly what it was.

    The stand-in below is a server this test starts on a loopback port the
    kernel picks. Nothing real is contacted.
    """

    def test_with_no_address_there_is_nobody_to_ask(self) -> None:
        from forge.deploy_sidecar.service import coordinator_owner_asker

        assert coordinator_owner_asker({}) is None

    def test_with_an_address_it_asks_and_reads_the_two_fields_back(self) -> None:
        import http.server
        import threading

        from forge.deploy_sidecar.service import (
            COORDINATOR_OWNER_ENV,
            coordinator_owner_asker,
        )

        asked: list[str] = []

        class _Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — the library's own name
                asked.append(self.path)
                body = json.dumps({"counter": 4, "build": "build-b"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # noqa: D102 — quiet
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Answer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            host, port = server.server_address[:2]
            ask = coordinator_owner_asker(
                {COORDINATOR_OWNER_ENV: f"http://{host}:{port}/who-owns"}
            )
            assert ask is not None
            assert ask("shop::live") == {"counter": 4, "build": "build-b"}
            assert asked and "target=shop%3A%3Alive" in asked[0]
        finally:
            server.shutdown()

    def test_a_coordinator_that_cannot_be_reached_is_a_refusal_not_a_crash(
        self,
    ) -> None:
        from forge.deploy_sidecar.service import (
            COORDINATOR_OWNER_ENV,
            coordinator_owner_asker,
        )

        # Port 1 on loopback: nothing listens, and nothing outside this machine
        # is contacted.
        ask = coordinator_owner_asker({COORDINATOR_OWNER_ENV: "http://127.0.0.1:1/x"})
        assert ask is not None
        assert ask("shop::live") is None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _kill(group: int) -> None:
    """Stop a process group this test started, whatever the notes say."""
    if not group:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except OSError:
            return


def _safe(target: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._:-]", "-", target)


def _wait_for_note(notes: Path, seconds: float = 20.0, until=None) -> dict:
    """The note of a live command, optionally one that answers ``until``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path in notes.glob("*.json"):
            try:
                written = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if written.get("group") and (until is None or until(written)):
                return written
        time.sleep(0.05)
    raise AssertionError("no note of a live command was written")


def _read_note(notes: Path) -> dict:
    """The one note in the folder, as it stands right now."""
    paths = sorted(notes.glob("*.json"))
    assert len(paths) == 1, f"expected one note, found {[p.name for p in paths]}"
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _write_note(
    notes: Path,
    target: str,
    *,
    counter: int,
    build: str,
    group: int,
    marker: str | None = None,
    phase: str = "running",
):
    (notes / f"{_safe(target)}.json").write_text(
        json.dumps(
            {
                "target": target,
                "counter": counter,
                "build": build,
                "marker": marker or the_marker_for(target),
                "group": group,
                "started_at": None,
                "started_wall": time.time(),
                "limit": 60.0,
                "highest_counter": counter,
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )


def _a_dead_group() -> int:
    """A process group id that certainly has nothing in it."""
    done = subprocess.run(
        [sys.executable, "-c", "pass"], start_new_session=True, check=False
    )
    # The child has been reaped; its number's group is empty. If the number is
    # reused before the test looks, the start-time half of the identity
    # (recorded as None here) still makes the answer "nothing of ours".
    return done.returncode if False else 2 ** 22 - 1


class _AlwaysAlive:
    """A process table in which the command never goes away."""

    available = True

    def members_of(self, group: int):
        return [group]

    def started_at(self, pid: int):
        return None

    def carrying(self, fragment: str):
        return []

    def group_of(self, pid: int):
        return pid

    def command_of(self, pid: int):
        return ""


class _Carrying:
    """A process table in which one pretend process carries each named marker."""

    available = True

    def __init__(self, markers: set[str]) -> None:
        self._markers = set(markers)

    def members_of(self, group: int):
        return []

    def started_at(self, pid: int):
        return None

    def carrying(self, fragment: str):
        return [4242] if any(fragment in m for m in self._markers) else []

    def group_of(self, pid: int):
        return pid

    def command_of(self, pid: int):
        return " ".join(sorted(self._markers))


class _AliveOnly:
    """A process table in which only the named groups are alive."""

    available = True

    def __init__(self, groups: set[int]) -> None:
        self._groups = set(groups)

    def members_of(self, group: int):
        return [group] if group in self._groups else []

    def started_at(self, pid: int):
        return None

    def carrying(self, fragment: str):
        return []

    def group_of(self, pid: int):
        return pid

    def command_of(self, pid: int):
        return ""


class _Background:
    """Run one executor request on a thread, so the test can look while it runs."""

    def __init__(self, executor: DeployExecutor, request: DeployRequest) -> None:
        self._executor = executor
        self._request = request
        self._thread = None
        self.answer = None

    def begin(self) -> None:
        import threading

        def _go() -> None:
            self.answer = self._executor.run(self._request)

        self._thread = threading.Thread(target=_go, daemon=True)
        self._thread.start()

    def until_answered(self, seconds: float = 30.0) -> None:
        """Wait for this request's own answer, without touching anything."""
        if self._thread is not None:
            self._thread.join(timeout=seconds)
        assert self.answer is not None, "the request never answered"

    def stop(self) -> None:
        # Best effort: stop anything of ours still alive, so no test leaves a
        # process behind.
        for path in Path(self._executor._root).glob("*.json"):
            try:
                written = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            group = int(written.get("group") or 0)
            if group:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(group, sig)
                    except OSError:
                        break
        if self._thread is not None:
            self._thread.join(timeout=30)
