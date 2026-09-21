"""What the merge card says about the finished feature — the four wordings.

Parts 1, 2 and 3a of the way-forward plan (21 September 2026). The card gains
one reading of the two records a finished build leaves behind, and this file
holds it to the three things the design fixed:

* **four wordings, never confusable** — the check ran; it could not run (with
  the reason); the project declares no check; no record could be read. A
  check that ran and found nothing wrong is never worded like the other
  three, and "not checked" is never worded like a pass.
* **one budget of 900 characters** for everything this reading adds, cut with
  a visible mark, and the not-checked list shortened to a bare count BEFORE
  any observation is dropped.
* **a reading fault never stops a card.** Whatever the reader does — raise,
  find nothing, find nonsense — the card is still offered, and it says which
  of those happened.

Everything here is offline: no wire, no git beyond the injected pins, no
project and no stack. The records are the shape the two kept builds of
19 September really wrote.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.envelope import MessageEnvelope
from nats_core.events import BuildCompletePayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import merge_offer as merge_offer_module
from forge.pipeline.merge_offer import (
    CHECK_COULD_NOT_RUN,
    CHECK_RAN,
    CUT_MARK,
    EVIDENCE_UNAVAILABLE,
    FINISHED_FEATURE_BUDGET,
    FINISHED_FEATURE_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    NO_CHECK_DECLARED,
    MergeOfferService,
    read_what_was_checked,
    what_was_checked,
)

BUILD_ID = "build-FEAT-WC1-20260921"
FEATURE_ID = "FEAT-WC1"
REPO = "appmilla/somewhere"
CORRELATION = "corr-wc-1"

#: The four openings, as a set, so "never confusable" can be asserted rather
#: than described.
ALL_FOUR = (CHECK_RAN, CHECK_COULD_NOT_RUN, NO_CHECK_DECLARED, EVIDENCE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Records, shaped as the build really writes them
# ---------------------------------------------------------------------------


def a_record(**over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "feature": FEATURE_ID,
        "status": "passed",
        "declared": True,
        "scenarios_covered": [],
        "not_checked": [
            {"name": "the first promise", "reason": "no check file of its own"},
            {"name": "the second promise", "reason": "no check file of its own"},
        ],
        # The record's own total double-counts (found while driving stage B):
        # the card must count the LIST, and this number is here so the test
        # can prove the card ignores it.
        "not_checked_total": 99,
        "observations": [
            {"asked": "the plainest question, with nothing stored", "answered": "nothing"},
            {"asked": "the same question again", "answered": "one thing"},
        ],
        "observations_total": 2,
        "could_not_run_reason": None,
    }
    record.update(over)
    return record


def a_code_checks(**over: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "feature": FEATURE_ID,
        "finding_count": 1,
        "tasks_total": 5,
        "tasks_with_something_not_checked": 2,
        "tasks_not_checked": ["TASK-WC1-002", "TASK-WC1-004"],
        "shell_command_count": 37,
        "tasks": [
            {
                "task": "TASK-WC1-004",
                "checks": {
                    "wiring": {
                        "state": "found_something",
                        "findings": [
                            {
                                "file": "somewhere/service",
                                "name": "the_thing_nothing_calls",
                                "kind": "UNWIRED_PATH",
                            }
                        ],
                        "finding_count": 1,
                    }
                },
            }
        ],
        "groups": [],
    }
    summary.update(over)
    return summary


# ---------------------------------------------------------------------------
# The four wordings
# ---------------------------------------------------------------------------


class TestTheFourWordings:
    def test_the_check_ran(self) -> None:
        said = what_was_checked(a_record(), a_code_checks())
        assert said.state == "ran"
        assert said.text.startswith(CHECK_RAN)
        for other in (CHECK_COULD_NOT_RUN, NO_CHECK_DECLARED, EVIDENCE_UNAVAILABLE):
            assert other not in said.text

    def test_the_check_could_not_run_says_why(self) -> None:
        said = what_was_checked(
            a_record(
                status="could_not_run",
                could_not_run_reason="there is no container runtime here",
            )
        )
        assert said.state == "could_not_run"
        assert said.text.startswith(CHECK_COULD_NOT_RUN)
        assert "there is no container runtime here" in said.text
        for other in (CHECK_RAN, NO_CHECK_DECLARED, EVIDENCE_UNAVAILABLE):
            assert other not in said.text

    def test_could_not_run_without_a_reason_still_says_so(self) -> None:
        said = what_was_checked(a_record(status="could_not_run", reason=None))
        assert said.state == "could_not_run"
        assert said.text.startswith(CHECK_COULD_NOT_RUN)
        assert "the project did not say why" in said.text

    def test_the_project_declares_no_check(self) -> None:
        said = what_was_checked(
            {"feature": FEATURE_ID, "status": "not_declared", "declared": False}
        )
        assert said.state == "not_declared"
        assert said.text == NO_CHECK_DECLARED
        for other in (CHECK_RAN, CHECK_COULD_NOT_RUN, EVIDENCE_UNAVAILABLE):
            assert other not in said.text

    def test_no_record_could_be_read(self) -> None:
        said = what_was_checked(None, None, "no feature_check.json was exported")
        assert said.state == "unavailable"
        assert said.text.startswith(EVIDENCE_UNAVAILABLE)
        assert "no feature_check.json was exported" in said.text
        for other in (CHECK_RAN, CHECK_COULD_NOT_RUN, NO_CHECK_DECLARED):
            assert other not in said.text

    def test_no_record_and_nobody_said_why_is_still_said(self) -> None:
        said = what_was_checked(None, None, None)
        assert said.state == "unavailable"
        assert said.text.startswith(EVIDENCE_UNAVAILABLE)

    def test_exactly_one_of_the_four_opens_every_block(self) -> None:
        blocks = [
            what_was_checked(a_record()),
            what_was_checked(a_record(status="could_not_run")),
            what_was_checked({"status": "not_declared", "declared": False}),
            what_was_checked(None, None, "nothing was exported"),
        ]
        for said in blocks:
            opens = [word for word in ALL_FOUR if said.text.startswith(word)]
            assert len(opens) == 1, said.text
        assert len({said.state for said in blocks}) == 4


class TestNotCheckedIsNeverAPass:
    def test_the_count_comes_from_the_list_not_the_records_own_total(self) -> None:
        # The record says 99; the list holds 2. The card says 2.
        said = what_was_checked(a_record())
        assert "Not checked: 2" in said.text
        assert "99" not in said.text
        assert said.details["not_checked_count"] == 2

    def test_a_check_that_could_not_run_still_names_what_was_not_checked(self) -> None:
        said = what_was_checked(a_record(status="could_not_run"))
        assert "Not checked: 2" in said.text

    def test_nothing_unchecked_is_said_plainly_and_claims_nothing(self) -> None:
        said = what_was_checked(a_record(not_checked=[], not_checked_total=0))
        assert "It left nothing on its not-checked list." in said.text
        assert "Not checked:" not in said.text

    def test_a_check_that_ran_and_did_not_pass_is_not_worded_as_a_pass(self) -> None:
        said = what_was_checked(a_record(status="failed"))
        assert said.state == "ran"
        assert "did not pass" in said.text
        assert not said.text.startswith(CHECK_RAN)


class TestObservations:
    def test_both_sides_of_every_observation_reach_the_card(self) -> None:
        said = what_was_checked(a_record())
        assert "Asked: the plainest question, with nothing stored" in said.text
        assert "Answered: nothing" in said.text

    def test_at_most_six_are_carried(self) -> None:
        many = [
            {"asked": f"question {n}", "answered": f"answer {n}"} for n in range(12)
        ]
        said = what_was_checked(a_record(observations=many, not_checked=[]))
        assert said.details["observations_count"] == 6

    def test_nonsense_entries_are_dropped_and_the_rest_survive(self) -> None:
        said = what_was_checked(
            a_record(
                observations=[
                    "not an entry",
                    {"asked": "a real question", "answered": "a real answer"},
                    None,
                    {},
                ]
            )
        )
        assert said.details["observations_count"] == 1
        assert "Asked: a real question" in said.text

    def test_one_empty_side_is_said_rather_than_guessed(self) -> None:
        said = what_was_checked(
            a_record(observations=[{"asked": "a question", "answered": ""}])
        )
        assert "Answered: (not said)" in said.text


class TestCodeChecksLine:
    def test_a_finding_is_named(self) -> None:
        said = what_was_checked(a_record(), a_code_checks())
        assert "1 finding (the_thing_nothing_calls)" in said.text

    def test_tasks_nothing_looked_at_are_counted(self) -> None:
        said = what_was_checked(a_record(), a_code_checks())
        assert "2 of 5 tasks not fully checked" in said.text

    def test_commands_the_build_ran_directly_are_noted(self) -> None:
        said = what_was_checked(a_record(not_checked=[], observations=[]), a_code_checks())
        assert "37 commands the build ran directly" in said.text

    def test_no_summary_means_no_line_rather_than_a_claim(self) -> None:
        said = what_was_checked(a_record(), None)
        assert "Code checks:" not in said.text

    def test_a_summary_with_nothing_found_says_no_findings(self) -> None:
        said = what_was_checked(
            a_record(),
            a_code_checks(finding_count=0, tasks=[], tasks_with_something_not_checked=0),
        )
        assert "Code checks: no findings" in said.text


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def _longest_allowed() -> dict[str, Any]:
    """Six observations at the record's own caps, and fifty unchecked names."""
    return a_record(
        not_checked=[
            {"name": f"promise number {n} " + "w" * 120, "reason": "r" * 200}
            for n in range(50)
        ],
        not_checked_total=50,
        observations=[
            {"asked": "q" * 400, "answered": "a" * 400} for _ in range(6)
        ],
        observations_total=6,
    )


class TestTheBudget:
    def test_the_longest_allowed_content_fits(self) -> None:
        said = what_was_checked(_longest_allowed(), a_code_checks())
        assert len(said.text) <= FINISHED_FEATURE_BUDGET

    def test_the_not_checked_list_shortens_to_a_count_first(self) -> None:
        said = what_was_checked(_longest_allowed(), a_code_checks())
        assert "Not checked: 50." in said.text
        assert said.details["not_checked_shortened_to_a_count"] is True

    def test_and_only_then_are_observations_dropped(self) -> None:
        said = what_was_checked(_longest_allowed(), a_code_checks())
        assert said.details["observations_not_shown"] > 0
        assert "not shown here" in said.text
        # The shortening happened; dropping is the second resort, never the
        # first, and the first observation is always the one kept.
        assert said.details["not_checked_shortened_to_a_count"] is True
        assert said.text.count("Asked: ") >= 1

    def test_a_cut_always_leaves_a_visible_mark(self) -> None:
        one_enormous = a_record(
            not_checked=[],
            observations=[{"asked": "q" * 400, "answered": "a" * 400}],
        )
        # One observation that cannot be dropped without losing everything:
        # what is left is cut, and the mark says so.
        said = what_was_checked(
            one_enormous,
            a_code_checks(
                finding_count=3,
                tasks=[
                    {
                        "task": "T",
                        "checks": {
                            "wiring": {
                                "findings": [
                                    {"name": "n" * 300},
                                    {"name": "m" * 300},
                                ],
                                "finding_count": 3,
                            }
                        },
                    }
                ],
            ),
        )
        assert len(said.text) <= FINISHED_FEATURE_BUDGET
        if said.details["cut_to_fit"]:
            assert CUT_MARK.strip() in said.text

    def test_an_over_long_name_is_cut_with_a_mark_and_the_count_stays_true(
        self,
    ) -> None:
        said = what_was_checked(
            a_record(
                not_checked=[{"name": "w" * 400, "reason": "r"} for _ in range(3)],
                observations=[],
            )
        )
        assert said.text.startswith(CHECK_RAN)
        assert "Not checked: 3" in said.text
        assert CUT_MARK.strip() in said.text
        assert "and 2 more." in said.text
        assert len(said.text) <= FINISHED_FEATURE_BUDGET

    def test_the_not_checked_line_keeps_its_own_cap_whatever_the_names(
        self,
    ) -> None:
        said = what_was_checked(
            a_record(
                not_checked=[
                    {"name": "x" * 290, "reason": "r"} for _ in range(3)
                ],
                observations=[],
            )
        )
        line = [ln for ln in said.lines if ln.startswith("Not checked:")][0]
        assert len(line) <= 300
        assert line.endswith("more.")

    def test_the_details_say_what_the_card_said(self) -> None:
        said = what_was_checked(_longest_allowed(), a_code_checks())
        assert said.details["card_characters"] == len(said.text)
        assert said.details["budget_characters"] == FINISHED_FEATURE_BUDGET
        assert said.details["card_lines"] == list(said.lines)


# ---------------------------------------------------------------------------
# The reader off disk
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_the_two_records_are_found_wherever_they_landed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deep = (
            tmp_path
            / "receipts"
            / BUILD_ID
            / "worktrees"
            / FEATURE_ID
            / ".guardkit"
            / "autobuild-private"
        )
        deep.mkdir(parents=True)
        (deep / "feature_check.json").write_text(json.dumps(a_record()), "utf-8")
        (deep / "code_checks.json").write_text(json.dumps(a_code_checks()), "utf-8")
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))
        record, code_checks, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert why_not is None
        assert record is not None and record["feature"] == FEATURE_ID
        assert code_checks is not None and code_checks["finding_count"] == 1

    def test_this_features_own_record_wins_when_several_were_exported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "receipts" / BUILD_ID
        (root / "outer").mkdir(parents=True)
        (root / "worktrees" / FEATURE_ID).mkdir(parents=True)
        (root / "outer" / "feature_check.json").write_text(
            json.dumps(a_record(feature="FEAT-OTHER", status="could_not_run")), "utf-8"
        )
        (root / "worktrees" / FEATURE_ID / "feature_check.json").write_text(
            json.dumps(a_record()), "utf-8"
        )
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))
        record, _, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert why_not is None
        assert record is not None and record["feature"] == FEATURE_ID

    def test_nothing_exported_says_so_in_a_short_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "nowhere"))
        record, code_checks, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert record is None and code_checks is None
        assert why_not and "exported" in why_not

    def test_nonsense_on_disk_is_a_reason_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "receipts" / BUILD_ID
        root.mkdir(parents=True)
        (root / "feature_check.json").write_text("not json at all", "utf-8")
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))
        record, _, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert record is None
        assert why_not and "could not be read" in why_not

    def test_a_record_that_is_not_a_record_is_a_reason_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "receipts" / BUILD_ID
        root.mkdir(parents=True)
        (root / "feature_check.json").write_text(json.dumps([1, 2, 3]), "utf-8")
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))
        record, _, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert record is None
        assert why_not and "not a record" in why_not

    def test_a_missing_code_checks_summary_costs_the_record_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "receipts" / BUILD_ID
        root.mkdir(parents=True)
        (root / "feature_check.json").write_text(json.dumps(a_record()), "utf-8")
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))
        record, code_checks, why_not = read_what_was_checked(BUILD_ID, FEATURE_ID)
        assert record is not None and code_checks is None and why_not is None


# ---------------------------------------------------------------------------
# The card itself — a reading fault never stops one
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    facade = SqliteLifecyclePersistence(connection=cx)
    facade.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-09-21T00:00:00Z', 'mode-a')",
        (BUILD_ID, FEATURE_ID, REPO, f"autobuild/{FEATURE_ID}", CORRELATION),
    )
    facade.connection.commit()
    return facade


@pytest.fixture
def config(tmp_path: Path) -> ForgeConfig:
    root = tmp_path / "repo"
    root.mkdir()
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(root)}},
            "merge_executor": {"enabled": True},
        }
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def raw_publish(self, subject: str, body: bytes) -> None:
        self.events.append(("approval", (subject, body)))

    async def publish_build_paused(self, payload: Any) -> None:
        self.events.append(("paused", payload))


class _Pins:
    async def rev_parse(self, ref: str) -> str:
        return "d" * 40 if ref.endswith("^{tree}") else "c" * 40


def _service(config: ForgeConfig, pool: Any, recorder: _Recorder, reader: Any) -> Any:
    async def _git_head(_root: Path) -> str:
        return "mainsha"

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        finished_feature_reader=reader,
        scope_pass=lambda **_kw: None,
        git_surface=lambda _repo, _root: _Pins(),
    )


def _event() -> BuildCompletePayload:
    return BuildCompletePayload(
        feature_id=FEATURE_ID,
        build_id=BUILD_ID,
        tasks_completed=5,
        tasks_failed=0,
        tasks_total=5,
        duration_seconds=10,
        summary="done",
    )


def _card(recorder: _Recorder) -> tuple[str, dict[str, Any]]:
    kind, (_subject, body) = recorder.events[0]
    assert kind == "approval"
    payload = MessageEnvelope.model_validate_json(body).payload
    return payload["action_description"], payload["details"]


class TestAReadingFaultNeverStopsACard:
    @pytest.mark.asyncio
    async def test_a_reader_that_raises_still_leaves_a_card(
        self, config, pool
    ) -> None:
        def _explodes(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the disk went away")

        recorder = _Recorder()
        await _service(config, pool, recorder, _explodes).maybe_offer(_event())
        words, details = _card(recorder)
        assert EVIDENCE_UNAVAILABLE in words
        assert "RuntimeError" in words
        assert details[FINISHED_FEATURE_DETAILS_KEY]["state"] == "unavailable"
        # And the card is a real, latched offer, not a half one.
        assert [
            s
            for s in pool.read_stages(BUILD_ID)
            if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        ]

    @pytest.mark.asyncio
    async def test_a_reader_that_never_comes_back_still_leaves_a_card(
        self, config, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            merge_offer_module, "FINISHED_FEATURE_READ_SECONDS", 0.05
        )

        def _hangs(*_a: Any, **_k: Any) -> Any:
            time.sleep(5)
            raise AssertionError("the card should not have waited for this")

        recorder = _Recorder()
        await _service(config, pool, recorder, _hangs).maybe_offer(_event())
        words, details = _card(recorder)
        assert EVIDENCE_UNAVAILABLE in words
        assert "did not come back within" in words
        assert details[FINISHED_FEATURE_DETAILS_KEY]["state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_a_reader_that_finds_nothing_still_leaves_a_card(
        self, config, pool
    ) -> None:
        recorder = _Recorder()
        await _service(
            config,
            pool,
            recorder,
            lambda *_a, **_k: (None, None, "nothing was exported for this build"),
        ).maybe_offer(_event())
        words, details = _card(recorder)
        assert EVIDENCE_UNAVAILABLE in words
        assert "nothing was exported for this build" in words
        assert details[FINISHED_FEATURE_DETAILS_KEY]["state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_the_words_and_the_structured_data_say_the_same_thing(
        self, config, pool
    ) -> None:
        recorder = _Recorder()
        await _service(
            config,
            pool,
            recorder,
            lambda *_a, **_k: (a_record(), a_code_checks(), None),
        ).maybe_offer(_event())
        words, details = _card(recorder)
        block = details[FINISHED_FEATURE_DETAILS_KEY]
        assert block["state"] == "ran"
        assert block["not_checked_count"] == 2
        assert block["observations_count"] == 2
        assert block["code_checks"]["finding_count"] == 1
        for line in block["card_lines"]:
            assert line in words

    @pytest.mark.asyncio
    async def test_what_the_reading_adds_stays_inside_the_budget(
        self, config, pool
    ) -> None:
        recorder = _Recorder()
        await _service(
            config,
            pool,
            recorder,
            lambda *_a, **_k: (_longest_allowed(), a_code_checks(), None),
        ).maybe_offer(_event())
        words, details = _card(recorder)
        block = details[FINISHED_FEATURE_DETAILS_KEY]
        assert block["card_characters"] <= FINISHED_FEATURE_BUDGET
        assert " ".join(block["card_lines"]) in words

    @pytest.mark.asyncio
    async def test_the_card_still_says_everything_it_said_before(
        self, config, pool
    ) -> None:
        recorder = _Recorder()
        await _service(
            config,
            pool,
            recorder,
            lambda *_a, **_k: (a_record(), None, None),
        ).maybe_offer(_event())
        words, _ = _card(recorder)
        assert words.startswith(f"{FEATURE_ID} built clean — 5 of 5 tasks passed.")
        assert words.endswith(
            "Approve = merge into main, deploy to the sandbox and run the "
            "checks; the branch is kept either way. Reject = nothing changes."
        )
