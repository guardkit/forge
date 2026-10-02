"""The machine's answers for publication, read from the check's record (the GitHub publishing gate, 2 October 2026).

2 October 2026 of the factory: ``estate-check --publication-facts`` LOOKS at the
machine and writes a record; the coordinator reads it on every merge word
through :func:`forge.pipeline.publication_facts.read_publication_facts`. These
tests hold the reader to the publishing gate:

* with ``FORGE_PUBLICATION_FACTS_FILE`` unset, behaviour is exactly today's;
* a missing, malformed, foreign, stale, too-old or future record reads as
  "nobody has looked", which keeps publication off, with the reason said;
* a fresh record for this coordinator, with every wall standing, and the
  settings on, turns publication on;
* each machine answer found wrong keeps it off by name;
* the reader is asked each time, never cached;
* ``publication_status`` and the press give the same verdict for one file;
* the boot line still says off before any facts exist.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.pipeline import publication_status
from forge.pipeline.publication_activation import WhatTheMachineSays
from forge.pipeline.publication_facts import (
    DEFAULT_MAX_AGE_SECONDS,
    FACTS_FILE_ENV,
    FACTS_FORMAT,
    MACHINE_ANSWER_NAMES,
    ThisCoordinator,
    read_publication_facts,
    the_machine_now,
    this_coordinator,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    say_where_publication_stands_at_boot,
    why_publication_is_off,
)

CONTAINER = "c0ffee" + "0" * 58
ANOTHER = "beef" + "1" * 60
#: The coordinator's PID 1 start, as the kernel counts it: boot time, start
#: ticks since boot, ticks per second. STARTED is the same moment in seconds.
TPS = int(os.sysconf("SC_CLK_TCK"))
BOOT = 1_789_000_000
TICKS = 1_000_000 * TPS
STARTED = BOOT + TICKS / TPS
PID1_START = {"boot_time_epoch": BOOT, "start_ticks": TICKS, "ticks_per_second": TPS}
ASKING = ThisCoordinator(CONTAINER, STARTED, BOOT, TICKS, TPS)

EVERY_WALL_STANDS = {
    "a_sandbox_can_write_the_coordinators_settings_file": False,
    "a_sandbox_can_see_the_ledger": False,
    "a_sandbox_can_reach_the_publisher": False,
    "only_the_coordinator_is_on_the_publishers_network": True,
    "the_credential_file_can_be_read_by_them": False,
}


def facts(
    *,
    written: float = STARTED + 60,
    container: str = CONTAINER,
    machine: dict[str, Any] | None = None,
    **over: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "format": FACTS_FORMAT,
        "what_this_is": "a test record",
        "written_at": "2026-09-21T00:00:00Z",
        "written_at_epoch": int(written),
        "compose_project": "forge-estate",
        "coordinator_container_id": container,
        "coordinator_image_id": "sha256:" + "a" * 64,
        "coordinator_started_at": "2026-09-21T00:00:00Z",
        "coordinator_started_at_epoch": STARTED,
        "coordinator_pid1_start": dict(PID1_START),
        "machine": dict(EVERY_WALL_STANDS if machine is None else machine),
        "said": {},
    }
    record.update(over)
    return record


def a_reader(
    path: Path,
    *,
    now: float = STARTED + 120,
    asking: ThisCoordinator = ASKING,
    **kwargs: Any,
) -> Any:
    """The production reader, told where the file is, who asks, and when."""
    return functools.partial(
        read_publication_facts,
        environ={FACTS_FILE_ENV: str(path)},
        who_is_asking=lambda: asking,
        now=lambda: now,
        **kwargs,
    )


def write(path: Path, record: Any) -> Path:
    path.write_text(record if isinstance(record, str) else json.dumps(record))
    return path


@pytest.fixture
def config_on() -> ForgeConfig:
    """Publication's own settings all on, and a publisher route named."""
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "publication": {
                "enabled": True,
                "publisher_url": "http://forge-publisher:8711",
                "builds_may_run_inside_the_coordinator": False,
                "publisher_credential_file": "/etc/forge-publisher/credential",
            },
        }
    )


# ---------------------------------------------------------------------------
# The reader on its own
# ---------------------------------------------------------------------------


class TestWithNoFactsFileNamedNothingChanges:
    def test_unset_is_none_which_is_what_production_passed_before(self) -> None:
        assert read_publication_facts(environ={}) is None
        assert read_publication_facts(environ={FACTS_FILE_ENV: "  "}) is None

    def test_unset_keeps_the_old_sentence_exactly(self, config_on: ForgeConfig) -> None:
        before = why_publication_is_off(config_on, None)
        after = why_publication_is_off(
            config_on, functools.partial(read_publication_facts, environ={})
        )
        assert after == before
        assert not publication_is_switched_on(
            config_on, functools.partial(read_publication_facts, environ={})
        )


class TestAFreshRecordForThisCoordinatorIsRead:
    def test_the_answers_come_back_as_written(self, tmp_path: Path) -> None:
        said = a_reader(write(tmp_path / "f.json", facts()))()
        assert isinstance(said, WhatTheMachineSays)
        for name in MACHINE_ANSWER_NAMES:
            assert getattr(said, name) == EVERY_WALL_STANDS[name]
        assert said.why_nobody_has_looked is None
        assert "estate-check --publication-facts" in (said.looked_at_by or "")

    def test_with_the_settings_on_publication_is_on(
        self, tmp_path: Path, config_on: ForgeConfig
    ) -> None:
        reader = a_reader(write(tmp_path / "f.json", facts()))
        assert publication_is_switched_on(config_on, reader)
        assert publication_is_switched_on(config_on, the_machine_now(reader))

    def test_with_the_setting_off_it_stays_off(self, tmp_path: Path) -> None:
        reader = a_reader(write(tmp_path / "f.json", facts()))
        off = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/tmp"]}}}
        )
        assert not publication_is_switched_on(off, reader)


class TestAnythingElseIsNobodyHasLooked:
    """Each refusal keeps publication off, and says why in the sentence."""

    @pytest.mark.parametrize(
        "record, asking, now, words",
        [
            pytest.param(
                None, None, None, "there is no publication facts file", id="missing"
            ),
            pytest.param(
                "{not json", None, None, "not one complete JSON record", id="malformed"
            ),
            pytest.param(
                facts(format="something-else/1"), None, None, "format", id="wrong-format"
            ),
            pytest.param(
                facts(container=ANOTHER),
                None,
                None,
                "written for the coordinator container",
                id="another-coordinator",
            ),
            pytest.param(
                facts(written=STARTED - 3600),
                None,
                None,
                "at or before this coordinator started",
                id="before-the-start",
            ),
            pytest.param(
                facts(written=STARTED),
                None,
                None,
                "at or before this coordinator started",
                id="at-the-start",
            ),
            pytest.param(
                facts(),
                None,
                STARTED + 60 + DEFAULT_MAX_AGE_SECONDS + 1,
                "seconds at most",
                id="too-old",
            ),
            pytest.param(
                facts(), None, STARTED - 600, "in the future", id="from-the-future"
            ),
            pytest.param(
                facts(machine={"a_sandbox_can_see_the_ledger": False}),
                None,
                None,
                "not exactly the five",
                id="answers-missing",
            ),
            pytest.param(
                facts(machine=dict(EVERY_WALL_STANDS, a_sandbox_can_see_the_ledger="no")),
                None,
                None,
                "not true, false or unknown",
                id="answer-not-a-boolean",
            ),
            pytest.param(
                facts(),
                ThisCoordinator(CONTAINER, STARTED + 30, BOOT, TICKS + 30 * TPS, TPS),
                None,
                "not the one running now",
                id="restarted-after-the-look",
            ),
            pytest.param(
                facts(written=STARTED + 90),
                ThisCoordinator(CONTAINER, STARTED + 30, BOOT, TICKS + 30 * TPS, TPS),
                None,
                "not the one running now",
                id="old-start-restart-then-write",
            ),
            pytest.param(
                facts(),
                ThisCoordinator(CONTAINER, STARTED, BOOT + 1, TICKS, TPS),
                None,
                "not the one running now",
                id="clock-stepped",
            ),
            pytest.param(
                facts(coordinator_pid1_start=None),
                None,
                None,
                "which start of the coordinator",
                id="no-start-identity",
            ),
            pytest.param(
                facts(),
                ThisCoordinator(None, STARTED, BOOT, TICKS, TPS),
                None,
                "could not tell which container",
                id="not-in-a-container",
            ),
            pytest.param(
                facts(),
                ThisCoordinator(CONTAINER, None),
                None,
                "start time could not be read",
                id="start-unreadable",
            ),
        ],
    )
    def test_it_refuses_and_says_why(
        self,
        tmp_path: Path,
        config_on: ForgeConfig,
        record: Any,
        asking: ThisCoordinator | None,
        now: float | None,
        words: str,
    ) -> None:
        path = tmp_path / "publication-facts.json"
        if record is not None:
            write(path, record)
        kwargs: dict[str, Any] = {}
        if asking is not None:
            kwargs["asking"] = asking
        if now is not None:
            kwargs["now"] = now
        reader = a_reader(path, **kwargs)

        said = reader()
        assert isinstance(said, WhatTheMachineSays)
        assert all(getattr(said, name) is None for name in MACHINE_ANSWER_NAMES)
        assert words in (said.why_nobody_has_looked or "")

        assert not publication_is_switched_on(config_on, reader)
        sentence = why_publication_is_off(config_on, reader)
        assert words in sentence
        assert "nobody has looked" in sentence

    def test_a_reader_that_raises_has_not_looked(self, config_on: ForgeConfig) -> None:
        def broken() -> WhatTheMachineSays:
            raise RuntimeError("boom")

        assert not publication_is_switched_on(config_on, broken)
        assert "reading the machine's answers failed" in why_publication_is_off(
            config_on, broken
        )


class TestEachWallFoundDownKeepsItOff:
    @pytest.mark.parametrize(
        "name, wrong, words",
        [
            ("a_sandbox_can_write_the_coordinators_settings_file", True,
             "a sandbox can write the coordinator's settings file"),
            ("a_sandbox_can_see_the_ledger", True, "a sandbox can see the ledger"),
            ("a_sandbox_can_reach_the_publisher", True, "a sandbox can reach the publisher"),
            ("only_the_coordinator_is_on_the_publishers_network", False,
             "something other than the coordinator is on the publisher's network"),
            ("the_credential_file_can_be_read_by_them", True,
             "credential file can be read"),
            ("a_sandbox_can_reach_the_publisher", None,
             "nobody has looked at whether a sandbox can reach the publisher"),
        ],
    )
    def test_the_wall_is_named(
        self, tmp_path: Path, config_on: ForgeConfig, name: str, wrong: Any, words: str
    ) -> None:
        machine = dict(EVERY_WALL_STANDS)
        machine[name] = wrong
        reader = a_reader(write(tmp_path / "f.json", facts(machine=machine)))
        assert not publication_is_switched_on(config_on, reader)
        assert words in why_publication_is_off(config_on, reader)


class TestItIsReadEachTimeNeverOnce:
    def test_changing_the_file_changes_the_next_answer(
        self, tmp_path: Path, config_on: ForgeConfig
    ) -> None:
        path = tmp_path / "f.json"
        reader = a_reader(path)
        assert not publication_is_switched_on(config_on, reader)
        write(path, facts())
        assert publication_is_switched_on(config_on, reader)
        write(path, facts(machine=dict(EVERY_WALL_STANDS, a_sandbox_can_see_the_ledger=True)))
        assert not publication_is_switched_on(config_on, reader)
        path.unlink()
        assert not publication_is_switched_on(config_on, reader)

    def test_the_machine_now_calls_a_reader_every_time(self) -> None:
        calls: list[int] = []

        def reader() -> WhatTheMachineSays:
            calls.append(1)
            return WhatTheMachineSays()

        the_machine_now(reader)
        the_machine_now(reader)
        assert len(calls) == 2

    def test_plain_values_pass_through(self) -> None:
        stand_in = WhatTheMachineSays(a_sandbox_can_see_the_ledger=False)
        assert the_machine_now(stand_in) is stand_in
        assert the_machine_now(None) is None


class TestWhoIsAsking:
    def test_the_container_and_pid1_start_are_read_from_proc(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "1").mkdir()
        (proc / "self" / "mountinfo").write_text(
            f"1 2 0:1 /docker/containers/{CONTAINER}/hostname /etc/hostname rw - ext4 /dev/x rw\n"
            f"3 2 0:1 /docker/containers/{CONTAINER}/hosts /etc/hosts rw - ext4 /dev/x rw\n"
        )
        ticks = 12345 * os.sysconf("SC_CLK_TCK")
        fields = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 10
        (proc / "1" / "stat").write_text(f"1 (forge (serve)) {' '.join(fields)}\n")
        (proc / "stat").write_text("cpu 1 2 3\nbtime 1790000000\n")

        asking = this_coordinator(proc)

        assert asking.container_id == CONTAINER
        assert asking.started_at_epoch == pytest.approx(1_790_000_000 + 12345)
        assert asking.start_identity == (1_790_000_000, ticks, os.sysconf("SC_CLK_TCK"))

    def test_outside_a_container_nothing_is_claimed(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "mountinfo").write_text("1 2 0:1 / / rw - ext4 /dev/x rw\n")
        asking = this_coordinator(proc)
        assert asking.container_id is None
        assert asking.started_at_epoch is None


class TestTheStatusCommandAndThePressAgree:
    def test_the_same_file_gives_the_same_verdict(
        self, tmp_path: Path, config_on: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "f.json"
        reader = a_reader(path)
        # publication_status uses the production reader with no arguments;
        # point it at the same file, coordinator and clock.
        monkeypatch.setattr(publication_status, "read_publication_facts", reader)

        for record in (None, facts(), facts(written=STARTED - 1)):
            if record is None:
                path.unlink(missing_ok=True)
            else:
                write(path, record)
            press_says = publication_is_switched_on(config_on, the_machine_now(reader))
            on, sentence = publication_status.where_publication_stands(config_on)
            assert on is press_says
            if not on:
                assert sentence == "publication is OFF: " + why_publication_is_off(
                    config_on, the_machine_now(reader)
                )

    def test_main_prints_and_exits_by_the_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        settings = tmp_path / "forge.yaml"
        settings.write_text(
            "permissions:\n  filesystem:\n    allowlist: ['/tmp']\n"
            "publication:\n  enabled: true\n"
            "  publisher_url: http://forge-publisher:8711\n"
            "  builds_may_run_inside_the_coordinator: false\n"
            "  publisher_credential_file: /etc/forge-publisher/credential\n"
        )
        path = tmp_path / "f.json"
        monkeypatch.setattr(publication_status, "read_publication_facts", a_reader(path))

        assert publication_status.main(["--config", str(settings)]) == 1
        assert "publication is OFF" in capsys.readouterr().out
        write(path, facts())
        assert publication_status.main(["--config", str(settings)]) == 0
        assert "publication is ON" in capsys.readouterr().out
        assert publication_status.main(["--config", str(tmp_path / "nope.yaml")]) == 2


class TestTheBootLineStillSaysOff:
    def test_before_any_facts_exist_boot_says_off(
        self, tmp_path: Path, config_on: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boot line is said once and decides nothing; it is asked with no
        machine answers, as ``bind_production_serve`` asks it, so at boot it
        says OFF, nobody has looked — even with the facts file named."""
        monkeypatch.setenv(FACTS_FILE_ENV, str(tmp_path / "not-yet.json"))
        line = say_where_publication_stands_at_boot(config_on)
        assert line.startswith("publication is OFF")
        assert "nobody has looked" in line


class TestItSaysPlainlyWhatToRun:
    """Nothing in the estate re-runs the check, so a stale or missing record
    must say so in the merge word's sentence, with the command that refreshes it."""

    @pytest.mark.parametrize(
        "record, now",
        [
            (None, None),
            (facts(written=STARTED - 1), None),
            (facts(), STARTED + 60 + DEFAULT_MAX_AGE_SECONDS + 1),
        ],
        ids=["missing", "before-the-start", "too-old"],
    )
    def test_the_sentence_names_the_refresh_command(
        self, tmp_path: Path, config_on: ForgeConfig, record: Any, now: float | None
    ) -> None:
        from forge.pipeline.publication_facts import REFRESH_THE_CHECK

        path = tmp_path / "f.json"
        if record is not None:
            write(path, record)
        reader = a_reader(path, **({"now": now} if now is not None else {}))
        sentence = why_publication_is_off(config_on, reader)
        assert "The machine check is out of date" in sentence
        assert "estate-check --env-file <the estate's env file> --publication-facts" in sentence
        assert sentence.count(REFRESH_THE_CHECK) == 1
        # said once, not once per machine question
        assert sentence.count("nobody has looked") == 1


class TestWhereTheFactsStandAtBoot:
    def test_unset_says_none_are_configured(self) -> None:
        from forge.pipeline.publication_facts import where_the_facts_stand

        assert "none are configured" in where_the_facts_stand(environ={})

    def test_missing_and_stale_and_fresh_are_told_apart(self, tmp_path: Path) -> None:
        from forge.pipeline.publication_facts import where_the_facts_stand

        path = tmp_path / "f.json"
        ask = {
            "environ": {FACTS_FILE_ENV: str(path)},
            "who_is_asking": lambda: ASKING,
            "now": lambda: STARTED + 120,
        }
        assert "there is no publication facts file" in where_the_facts_stand(**ask)
        write(path, facts(written=STARTED - 5))
        assert "at or before this coordinator started" in where_the_facts_stand(**ask)
        write(path, facts())
        assert where_the_facts_stand(**ask).startswith("publication facts: present and fresh")




class TestOnlyTheWritersWholeNumbersAreTimes:
    """Codex R3: NaN, infinity, floats, bools and strings are not times."""

    WRITTEN = f'"written_at_epoch": {int(STARTED) + 60}'
    TICKED = f'"start_ticks": {TICKS}'

    @pytest.mark.parametrize(
        "replace, by",
        [
            pytest.param(WRITTEN, '"written_at_epoch": NaN', id="NaN"),
            pytest.param(WRITTEN, '"written_at_epoch": Infinity', id="Infinity"),
            pytest.param(WRITTEN, '"written_at_epoch": -Infinity', id="minus-Infinity"),
            pytest.param(WRITTEN, '"written_at_epoch": 1e400', id="overflow-to-inf"),
            pytest.param(WRITTEN, f'"written_at_epoch": {STARTED + 60.5}', id="float"),
            pytest.param(WRITTEN, '"written_at_epoch": true', id="bool"),
            pytest.param(WRITTEN, f'"written_at_epoch": "{int(STARTED) + 60}"', id="string"),
            pytest.param(WRITTEN, WRITTEN + ', "x": NaN', id="NaN-elsewhere"),
            pytest.param(TICKED, '"start_ticks": NaN', id="NaN-start"),
            pytest.param(TICKED, f'"start_ticks": {TICKS}.0', id="float-start"),
        ],
    )
    def test_it_is_refused_through_the_activation_gate(
        self, tmp_path: Path, config_on: ForgeConfig, replace: str, by: str
    ) -> None:
        raw = json.dumps(facts())
        assert raw.count(replace) == 1
        path = tmp_path / "f.json"
        path.write_text(raw.replace(replace, by))
        reader = a_reader(path)

        assert not publication_is_switched_on(config_on, reader)
        assert "nobody has looked" in why_publication_is_off(config_on, reader)

    def test_the_valid_record_beside_them_is_on(
        self, tmp_path: Path, config_on: ForgeConfig
    ) -> None:
        reader = a_reader(write(tmp_path / "f.json", facts()))
        assert publication_is_switched_on(config_on, reader)
