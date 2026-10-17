"""The merge card names the signed-off prototype version a build was made from.

Prototype-to-factory design (17 October 2026), section 2.6 and Codex R4. When
the project's settings at the build's START commit declare
``reference.signed_off``, both merge cards carry one line: "Built from the
signed-off prototype <name> at <short id> (signed off <date>)." When the
record is declared and cannot be read, the line says so instead. A project
without the setting gets no line and no read of a record.

Trees are dictionaries; the repository names, commits and dates are invented.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from forge.cli._serve_gate_activation import make_merge_card_publisher, merge_card_words
from forge.planning.reference_material import (
    PROTOTYPE_NOT_READ_LINE,
    SignedOffRecord,
    card_line_about_the_prototype,
    prototype_card_line_at_commit,
)
from forge.pipeline.merge_offer import signed_off_declared_at_start
from forge.planning.run_store import SqlitePlanningRunStore
from tests.forge.pipeline.test_merge_offer import (  # noqa: F401 - fixtures
    BUILD_ID,
    CORRELATION,
    FEATURE_ID,
    _event,
    _insert_build,
    _Recorder,
    config,
    pool,
    repo_root,
)
from tests.forge.pipeline.test_the_card_says_main_checks_the_merge import (
    CANDIDATE_SHA,
    LOCAL_MAIN,
    START,
    TARGET,
    StandInGuardKit,
    Trees,
    _approval_words,
    _record_start,
    _service,
)

RECORD_PATH = "reference/signed-off.yaml"
RECORDED_COMMIT = "4" * 40
DECLARES = (
    "reference:\n"
    '  paths: ["reference/"]\n'
    f'  signed_off: "{RECORD_PATH}"\n'
)
RECORD = (
    "repository: https://example.invalid/acme/widget-mockup.git\n"
    f"commit: {RECORDED_COMMIT}\n"
    "signed_off: 2026-10-01\n"
)
LINE = "Built from the signed-off prototype widget-mockup at 4444444 (signed off 2026-10-01)."


def _files(config_text: str | None, record: str | None = RECORD) -> dict[str, tuple[str, str]]:
    files: dict[str, tuple[str, str]] = {}
    if config_text is not None:
        files[".guardkit/config.yaml"] = ("100644", config_text)
    if record is not None:
        files[RECORD_PATH] = ("100644", record)
    return files


# ---------------------------------------------------------------------------
# The line, read at one commit
# ---------------------------------------------------------------------------


def test_the_line_names_the_repository_the_short_id_and_the_date() -> None:
    record = SignedOffRecord(
        repository="https://example.invalid/acme/widget-mockup.git",
        commit=RECORDED_COMMIT,
        signed_off="2026-10-01",
    )
    assert card_line_about_the_prototype(record) == LINE
    undated = SignedOffRecord(repository="acme/widget-mockup", commit=RECORDED_COMMIT)
    assert card_line_about_the_prototype(undated) == (
        "Built from the signed-off prototype widget-mockup at 4444444."
    )
    assert card_line_about_the_prototype(None) == PROTOTYPE_NOT_READ_LINE


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (_files(None, record=None), ""),
        (_files("memory:\n  project: p\n"), ""),
        (_files('reference:\n  paths: ["reference/"]\n'), ""),
        (_files(DECLARES), LINE),
        (_files(DECLARES, record=None), PROTOTYPE_NOT_READ_LINE),
        (_files(DECLARES, record="repository: x\ncommit: short\n"), PROTOTYPE_NOT_READ_LINE),
    ],
    ids=["no-settings", "no-reference", "no-record-declared", "good", "missing", "malformed"],
)
@pytest.mark.asyncio
async def test_what_the_line_says_at_a_commit(
    files: dict[str, tuple[str, str]], expected: str
) -> None:
    trees = Trees({START: files})
    assert await prototype_card_line_at_commit(trees, repo_path="/r", commit=START) == expected


@pytest.mark.asyncio
async def test_without_the_setting_no_record_is_read() -> None:
    trees = Trees({START: _files("memory:\n  project: p\n")})
    assert await prototype_card_line_at_commit(trees, repo_path="/r", commit=START) == ""
    assert [path for _commit, path, _m in trees.calls] == [".guardkit/config.yaml"]


class _Raising:
    async def read_file_at_commit(self, *_a: Any, **_k: Any) -> Any:
        raise OSError("the helper went away")


UNREADABLE = [Trees({}), _Raising(), object()]
UNREADABLE_IDS = ["refused", "raised", "no-reader"]


@pytest.mark.parametrize("reader", UNREADABLE, ids=UNREADABLE_IDS)
@pytest.mark.asyncio
async def test_unreadable_settings_with_a_record_declared_at_start_are_said(
    reader: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The run recorded a signed-off record at its start, and the settings
    cannot be read now: the card says so rather than leaving it out."""
    with caplog.at_level("WARNING", logger="forge.planning.reference_material"):
        line = await prototype_card_line_at_commit(
            reader, repo_path="/r", commit=START, declared_at_start=True
        )
    assert line == PROTOTYPE_NOT_READ_LINE
    assert any("could not be read" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("reader", UNREADABLE, ids=UNREADABLE_IDS)
@pytest.mark.asyncio
async def test_unreadable_settings_never_name_a_prototype_otherwise(
    reader: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Declared nothing at its start: no line. No start record to ask: no
    line, and a warning."""
    assert (
        await prototype_card_line_at_commit(
            reader, repo_path="/r", commit=START, declared_at_start=False
        )
        == ""
    )
    with caplog.at_level("WARNING", logger="forge.planning.reference_material"):
        line = await prototype_card_line_at_commit(reader, repo_path="/r", commit=START)
    assert line == ""
    assert any("no start record" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Both cards
# ---------------------------------------------------------------------------


def _trees(at_start: dict[str, tuple[str, str]]) -> Trees:
    """The start commit carries ``at_start``; every later commit has a newer
    record, which the card must never name."""
    newer = dict(at_start)
    if RECORD_PATH in newer:
        newer[RECORD_PATH] = ("100644", RECORD.replace(RECORDED_COMMIT, "5" * 40))
    return Trees({START: at_start, TARGET: newer, CANDIDATE_SHA: newer, LOCAL_MAIN: newer})


async def _routine(config: Any, pool: Any, trees: Trees | None, prepare: Any = None) -> str:
    _insert_build(pool)
    _record_start(pool)
    if prepare is not None:
        prepare(pool)
    recorder = _Recorder()
    await _service(config, pool, recorder, trees=trees, rules=StandInGuardKit()).maybe_offer(
        _event()
    )
    return _approval_words(recorder)


async def _repair(config: Any, pool: Any, trees: Trees | None, prepare: Any = None) -> str:
    _insert_build(pool)
    _record_start(pool)
    if prepare is not None:
        prepare(pool)
    recorder = _Recorder()
    service = _service(config, pool, recorder, trees=trees, rules=StandInGuardKit())
    publish_card = make_merge_card_publisher(
        offer_service=service,
        sqlite_pool=pool,
        clock=lambda: datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
    )
    await publish_card(build_id=BUILD_ID, feature_id=FEATURE_ID, gates=None)
    return _approval_words(recorder)


CARDS = {"routine card": _routine, "repair card": _repair}


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_both_cards_name_the_version_read_at_the_start_commit(
    config: Any, pool: Any, card: str
) -> None:
    words = await CARDS[card](config, pool, _trees(_files(DECLARES)))
    assert LINE in words
    assert "5555555" not in words


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_a_declared_record_that_cannot_be_read_is_said(
    config: Any, pool: Any, card: str
) -> None:
    words = await CARDS[card](config, pool, _trees(_files(DECLARES, record=None)))
    assert PROTOTYPE_NOT_READ_LINE in words


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.asyncio
async def test_without_the_setting_the_card_has_no_line(
    config: Any, pool: Any, card: str
) -> None:
    words = await CARDS[card](config, pool, _trees(_files("memory:\n  project: p\n")))
    assert "prototype" not in words


def test_the_repair_cards_words_are_unchanged_without_the_line() -> None:
    before = merge_card_words(feature_id=FEATURE_ID, branch="autobuild/x", supplied_plan="S.")
    assert merge_card_words(
        feature_id=FEATURE_ID, branch="autobuild/x", supplied_plan="S.", prototype=""
    ) == before
    with_line = merge_card_words(
        feature_id=FEATURE_ID, branch="autobuild/x", supplied_plan="S.", prototype=LINE
    )
    assert with_line == before.replace("S. ", f"S. {LINE} ", 1)


# ---------------------------------------------------------------------------
# What the planning run recorded at its start decides an unreadable case
# ---------------------------------------------------------------------------


def _planning_run(pool: Any, *, signed_off: str | None, recorded: bool = True) -> None:
    """The planning run behind the build, as its door records it."""
    store = SqlitePlanningRunStore(pool.connection, target_terminal_enabled=True)
    store.record_queued(
        correlation_id=CORRELATION,
        originating_user="someone",
        expected_approver="someone",
        request_text="build the gadgets page",
        triggered_by="jarvis",
        target_repo="acme/gadgets",
    )
    if recorded:
        store._record_event(
            correlation_id=CORRELATION,
            stage_label="reference-material",
            status="recorded",
            actor_identity="planning-driver",
            details_json=json.dumps(
                {"reference_material": {"paths": ["reference/"], "files": [], "signed_off": signed_off}}
            ),
        )
    store.record_memory_project(CORRELATION, memory_project="gadgets")


def test_the_start_record_says_whether_a_record_was_declared(pool: Any) -> None:
    _insert_build(pool)
    row = pool.get_build_row(BUILD_ID)
    assert signed_off_declared_at_start(pool, row) is None  # no planning run
    _planning_run(pool, signed_off=RECORD_PATH)
    assert signed_off_declared_at_start(pool, row) is True


def test_a_run_that_declared_no_record_says_false(pool: Any) -> None:
    _insert_build(pool)
    _planning_run(pool, signed_off=None, recorded=False)
    assert signed_off_declared_at_start(pool, pool.get_build_row(BUILD_ID)) is False


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.parametrize(
    ("signed_off", "said"),
    [(RECORD_PATH, PROTOTYPE_NOT_READ_LINE), (None, None)],
    ids=["declared-at-start", "not-declared-at-start"],
)
@pytest.mark.asyncio
async def test_an_unreadable_start_commit_follows_the_start_record(
    config: Any, pool: Any, card: str, signed_off: str | None, said: str | None
) -> None:
    _planning_run(pool, signed_off=signed_off, recorded=signed_off is not None)
    # The copy cannot read the start commit at all.
    trees = Trees({TARGET: _files(None), CANDIDATE_SHA: _files(None), LOCAL_MAIN: _files(None)})
    words = await CARDS[card](config, pool, trees)
    if said:
        assert said in words
    else:
        assert "prototype" not in words


# ---------------------------------------------------------------------------
# A prepared build: what its admission found is kept beside its row (R4)
# ---------------------------------------------------------------------------


def _admission(signed_off: str | None) -> Any:
    from forge.pipeline.prepared_admission import AdmittedBuild

    return AdmittedBuild(
        start_commit=START,
        target_branch="main",
        source_commit=START,
        memory_project="gadgets",
        reference_signed_off=signed_off,
    )


def _admitted_payload() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        feature_id="FEAT-PREP",
        repo="acme/gadgets",
        branch="feature/prepared",
        feature_yaml_path=".guardkit/features/FEAT-PREP.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="jarvis",
        originating_adapter="slack",
        originating_user="someone",
        correlation_id="corr-prepared-1",
        parent_request_id=None,
        queued_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(("signed_off", "expected"), [(RECORD_PATH, True), (None, False)])
def test_the_admission_keeps_what_it_found_beside_the_row(
    pool: Any, signed_off: str | None, expected: bool
) -> None:
    build_id = pool.record_pending_build(_admitted_payload(), admitted=_admission(signed_off))
    assert signed_off_declared_at_start(pool, pool.get_build_row(build_id)) is expected


def _as_admitted(signed_off: str | None) -> Any:
    """The record a prepared admission writes beside the build row, for the
    harness's build (whose row the harness writes itself)."""
    from forge.lifecycle.persistence import PREPARED_ADMISSION_STAGE

    def write(pool: Any) -> None:
        pool.connection.execute(
            "INSERT INTO stage_log (build_id, stage_label, target_kind, "
            "target_identifier, status, started_at, completed_at, duration_secs, "
            "details_json) VALUES (?, ?, 'local_tool', ?, 'PASSED', ?, ?, 0.0, ?)",
            (
                BUILD_ID,
                PREPARED_ADMISSION_STAGE,
                PREPARED_ADMISSION_STAGE,
                "2026-10-17T09:00:00+00:00",
                "2026-10-17T09:00:00+00:00",
                json.dumps({"reference_signed_off": signed_off}),
            ),
        )
        pool.connection.commit()

    return write


@pytest.mark.parametrize("card", sorted(CARDS))
@pytest.mark.parametrize(
    ("signed_off", "said"),
    [(RECORD_PATH, PROTOTYPE_NOT_READ_LINE), (None, None)],
    ids=["prepared-declared", "prepared-undeclared"],
)
@pytest.mark.asyncio
async def test_a_prepared_build_with_an_unreadable_start_follows_its_admission(
    config: Any, pool: Any, card: str, signed_off: str | None, said: str | None
) -> None:
    trees = Trees({TARGET: _files(None), CANDIDATE_SHA: _files(None), LOCAL_MAIN: _files(None)})
    words = await CARDS[card](config, pool, trees, prepare=_as_admitted(signed_off))
    if said:
        assert said in words
    else:
        assert "prototype" not in words


@pytest.mark.parametrize(
    ("signed_off", "expected"),
    [(RECORD_PATH, PROTOTYPE_NOT_READ_LINE), (None, "")],
    ids=["declared", "undeclared"],
)
@pytest.mark.asyncio
async def test_the_outer_failure_path_follows_the_start_record(
    config: Any, pool: Any, signed_off: str | None, expected: str
) -> None:
    """A failure outside the settings read itself (here the commit reader
    cannot even be made) still follows what was recorded at the start."""
    from forge.pipeline.merge_offer import MergeOfferService

    _insert_build(pool)
    _record_start(pool)
    _as_admitted(signed_off)(pool)

    def broken_reader(_repo: str, _root: Any) -> Any:
        raise RuntimeError("no reader for this project")

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=None,
        raw_publish=None,  # type: ignore[arg-type]
        commit_reader=broken_reader,
    )
    assert await service.card_line_about_the_prototype(BUILD_ID) == expected


@pytest.mark.parametrize(
    ("how", "signed_off", "expected"),
    [
        ("prepared", RECORD_PATH, PROTOTYPE_NOT_READ_LINE),
        ("prepared", None, ""),
        ("planned", RECORD_PATH, PROTOTYPE_NOT_READ_LINE),
        ("planned", None, ""),
        ("nothing recorded", None, ""),
    ],
)
@pytest.mark.asyncio
async def test_with_no_commit_reader_the_start_record_still_decides(
    config: Any, pool: Any, how: str, signed_off: str | None, expected: str
) -> None:
    """Codex implementation review R4, round 2: a service with no commit
    reader (its default) consults the start record before anything else."""
    from forge.pipeline.merge_offer import MergeOfferService

    if how == "planned":
        _planning_run(pool, signed_off=signed_off, recorded=signed_off is not None)
    _insert_build(pool)
    _record_start(pool)
    if how == "prepared":
        _as_admitted(signed_off)(pool)
    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=None,
        raw_publish=None,  # type: ignore[arg-type]
    )
    assert await service.card_line_about_the_prototype(BUILD_ID) == expected
