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
        try:
            _wait_for_note(notes)
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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe(target: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._:-]", "-", target)


def _wait_for_note(notes: Path, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path in notes.glob("*.json"):
            try:
                written = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if written.get("group"):
                return written
        time.sleep(0.05)
    raise AssertionError("no note of a live command was written")


def _write_note(notes: Path, target: str, *, counter: int, build: str, group: int):
    (notes / f"{_safe(target)}.json").write_text(
        json.dumps(
            {
                "target": target,
                "counter": counter,
                "build": build,
                "marker": the_marker_for(target),
                "group": group,
                "started_at": None,
                "started_wall": time.time(),
                "limit": 60.0,
                "highest_counter": counter,
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
