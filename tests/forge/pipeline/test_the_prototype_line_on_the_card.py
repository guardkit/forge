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
from tests.forge.pipeline.test_merge_offer import (  # noqa: F401 - fixtures
    BUILD_ID,
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


@pytest.mark.parametrize(
    "reader",
    [Trees({}), _Raising(), object()],
    ids=["refused", "raised", "no-reader"],
)
@pytest.mark.asyncio
async def test_settings_that_cannot_be_read_are_said_not_left_out(
    reader: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused read (the commit is not in the copy, the helper cannot be
    reached), a read that raises, or no reader at all: nothing says the
    project declares no record, so the card says it could not be read."""
    with caplog.at_level("WARNING", logger="forge.planning.reference_material"):
        line = await prototype_card_line_at_commit(reader, repo_path="/r", commit=START)
    assert line == PROTOTYPE_NOT_READ_LINE
    assert any("could not be read" in r.getMessage() for r in caplog.records)


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


async def _routine(config: Any, pool: Any, trees: Trees | None) -> str:
    _insert_build(pool)
    _record_start(pool)
    recorder = _Recorder()
    await _service(config, pool, recorder, trees=trees, rules=StandInGuardKit()).maybe_offer(
        _event()
    )
    return _approval_words(recorder)


async def _repair(config: Any, pool: Any, trees: Trees | None) -> str:
    _insert_build(pool)
    _record_start(pool)
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
