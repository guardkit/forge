"""The merge card shows the build reviewer's notes as advice (29 October 2026).

The build reviewer design of 29 October 2026: GuardKit writes what each task's
final review left (at most ten findings, each with its pattern, place and one
sentence) to ``build_review_notes.json`` beside the code-checks summary, and
the merge card shows them under the build, the way the build card shows the
planner's notes. Nothing waits or refuses on them.

Through the real ``MergeOfferService.maybe_offer`` with the record exported
under the receipts folder as the runner exports it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge.pipeline.merge_offer import (
    BUILD_REVIEW_NOTES_NAME,
    card_line_about_the_build_review,
)

from .test_merge_offer_what_was_checked import (  # noqa: F401 — fixtures
    BUILD_ID,
    FEATURE_ID,
    _card,
    _event,
    _Recorder,
    _service,
    a_code_checks,
    a_record,
    config,
    pool,
)

_NOTES = {
    "feature": FEATURE_ID,
    "notes": [
        {
            "task_id": "TASK-WC1-002",
            "pattern": "NOT_TESTED",
            "location": "apps/api/src/count.ts:14",
            "description": "No test the project's check runs covers the empty list.",
        },
        {
            "task_id": "TASK-WC1-003",
            "pattern": "CRITERION_CONFLICT",
            "location": "docs/design/contracts/API.md:43",
            "description": "The criterion says null; the contract allows a timestamp",
        },
    ],
    "total": 2,
}


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notes: Any) -> None:
    folder = (
        tmp_path / "receipts" / BUILD_ID / ".guardkit" / "autobuild-private"
    )
    folder.mkdir(parents=True)
    if notes is not None:
        (folder / BUILD_REVIEW_NOTES_NAME).write_text(json.dumps(notes), "utf-8")
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(tmp_path / "receipts"))


@pytest.mark.asyncio
async def test_the_card_shows_the_reviewer_notes_as_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, pool
) -> None:
    _export(tmp_path, monkeypatch, _NOTES)
    recorder = _Recorder()
    await _service(
        config, pool, recorder, lambda *_a, **_k: (a_record(), a_code_checks(), None)
    ).maybe_offer(_event())
    words, _details = _card(recorder)
    (line,) = [w for w in words.splitlines() if "build reviewer's notes" in w]
    assert line.startswith("The build reviewer's notes for you: ")
    assert "TASK-WC1-002 NOT_TESTED apps/api/src/count.ts:14: No test" in line
    assert "TASK-WC1-003 CRITERION_CONFLICT docs/design/contracts/API.md:43" in line
    # Advice under the build: before the closing Approve/Reject sentence.
    lines = words.splitlines()
    assert lines.index(line) < len(lines) - 1
    assert lines[-1].startswith("Approve = merge into main")


@pytest.mark.asyncio
@pytest.mark.parametrize("notes", [None, {"feature": FEATURE_ID, "notes": [], "total": 0}])
async def test_no_notes_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config, pool, notes: Any
) -> None:
    _export(tmp_path, monkeypatch, notes)
    recorder = _Recorder()
    await _service(
        config, pool, recorder, lambda *_a, **_k: (a_record(), a_code_checks(), None)
    ).maybe_offer(_event())
    words, _details = _card(recorder)
    assert "build reviewer's notes" not in words


def test_another_features_notes_are_not_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.pipeline.merge_offer import read_build_review_notes

    _export(tmp_path, monkeypatch, {**_NOTES, "feature": "FEAT-OTHER"})
    assert read_build_review_notes(BUILD_ID, FEATURE_ID) == ""
    assert read_build_review_notes(BUILD_ID, "FEAT-OTHER").startswith(
        "The build reviewer's notes for you: "
    )


def test_more_than_shown_is_counted_and_nonsense_is_dropped() -> None:
    line = card_line_about_the_build_review(
        {"notes": [_NOTES["notes"][0], "nonsense", 3], "total": 7}
    )
    assert "NOT_TESTED" in line
    assert "(6 more in the build's records.)" in line
    assert card_line_about_the_build_review({"notes": "x"}) == ""
    assert card_line_about_the_build_review(None) == ""
