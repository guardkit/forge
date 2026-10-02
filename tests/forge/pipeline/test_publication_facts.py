"""The machine's answers for publication, read from the check's record (TC8).

Release -3 of the factory: ``estate-check --publication-facts`` LOOKS at the
machine and writes a record; the coordinator reads it on every merge word
through :func:`forge.pipeline.publication_facts.read_publication_facts`. These
tests hold the reader to the runbook's TC8 (c):

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
STARTED = 1_790_000_000.0

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
        "machine": dict(EVERY_WALL_STANDS if machine is None else machine),
        "said": {},
    }
    record.update(over)
    return record


def a_reader(
    path: Path,
    *,
    now: float = STARTED + 120,
    asking: ThisCoordinator = ThisCoordinator(CONTAINER, STARTED),
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
                ThisCoordinator(None, STARTED),
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
