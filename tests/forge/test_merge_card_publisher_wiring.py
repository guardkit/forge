"""The checkpoint's card IS the card the merge press consumes.

2026-09-09, the fix journey's twentieth attempt — the first to reach the
end, and the one that proved there were TWO merge cards in this estate and
only one of them had a listener. The merge-ready checkpoint published its
own card through the ordinary approval gate, so the card carried the gate's
request id (``<build id>:<stage label>:0``) and no merge-offer row. The
merge press requires both: a request id beginning ``merge-``, and a durable
``merge_deploy_offer`` row whose recorded request id matches. Rich answered
approve in Slack eleven seconds after the card went out, forge wrote "merge
card published", the journey reported delivered — and nothing checked the
candidate, nothing merged, nothing promoted.

What this file pins:

* the card goes out through the routine build's OWN publisher
  (``MergeOfferService.offer``), not a second envelope builder;
* the request id, the durable row and the synthetic Slack join key are
  therefore the press's, byte for byte;
* the words are the checkpoint's own, in plain English: what was checked,
  what is left to the merge press, which branch, and what the merge word
  does;
* the checkpoint's own durable row is still written, because the one-card
  latch and the self-closed-defect measure both count it;
* an offer that refuses publishes nothing and says so by raising, so the
  journey can never report a delivery that did not happen.

No broker is contacted anywhere: the offer's wire is a pair of recording
fakes, and the ledger is a real SQLite file in a temp directory.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_gate_activation import (
    _MERGE_CARD_TARGET_IDENTIFIER,
    MergeCardNotPublished,
    make_merge_card_publisher,
    merge_card_words,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
    merge_request_id,
)
from forge.pipeline.merge_ready_checkpoint import (
    MERGE_READY_CHECKPOINT_LABEL,
    GatesReport,
    GateStatus,
)

CORRELATION = "dddd4444-eeee-5555-ffff-666666666666"
REPO = "guardkit/forge"


class _CandidatePins:
    async def rev_parse(self, ref: str) -> str:
        return "c" * 40 if ref.endswith("^{tree}") else "b" * 40


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


def _payload() -> SimpleNamespace:
    return SimpleNamespace(
        feature_id="FEAT-CARD",
        repo=REPO,
        branch="repair/TASK-CARDFIX1",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="card-test",
        correlation_id=CORRELATION,
        parent_request_id=None,
        queued_at=datetime(2026, 7, 31, 11, 0, 0, tzinfo=UTC),
    )


class _RecordingPublisher:
    """The pipeline publisher, recording ``build-paused`` instead of sending."""

    def __init__(self) -> None:
        self.paused: list[Any] = []

    async def publish_build_paused(self, payload: Any) -> None:
        self.paused.append(payload)


class _Config:
    def __init__(self, repo_root: Path) -> None:
        self.merge_executor = SimpleNamespace(
            enabled=True, response_wait_seconds=3600
        )
        self.planning = SimpleNamespace(target_repo_paths={REPO: str(repo_root)})
        self.approval = SimpleNamespace(expected_approver="rich")


def _offer_service(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> tuple[MergeOfferService, _RecordingPublisher, list[tuple[str, bytes]]]:
    publisher = _RecordingPublisher()
    raw: list[tuple[str, bytes]] = []

    async def _raw_publish(subject: str, body: bytes) -> None:
        raw.append((subject, body))

    async def _git_head(_repo_root: Path) -> str:
        return "a" * 40

    service = MergeOfferService(
        config=_Config(tmp_path / "repo"),
        pool=pool,
        pipeline_publisher=publisher,
        raw_publish=_raw_publish,
        git_head=_git_head,
        git_surface=lambda _repo, _root: _CandidatePins(),
        baseline_reader=lambda _build_id: None,
        clock=lambda: datetime(2026, 9, 9, 9, 8, tzinfo=UTC),
    )
    return service, publisher, raw


def _gates() -> GatesReport:
    return GatesReport(
        status=GateStatus.GREEN,
        detail="declared suite GREEN (817 passed, 2 deselected)",
        deferred_detail=(
            "5 stamped checks (probe:bus, probe:process) have no live-gate "
            "evidence yet: the merge press stands the candidate up in the "
            "sandbox and runs this repository's live gate on it before "
            "anything lands."
        ),
    )


def _publisher_for(
    pool: SqliteLifecyclePersistence, tmp_path: Path
) -> tuple[Any, _RecordingPublisher, list[tuple[str, bytes]]]:
    service, publisher, raw = _offer_service(pool, tmp_path)
    publish_card = make_merge_card_publisher(
        offer_service=service,
        sqlite_pool=pool,
        clock=lambda: datetime(2026, 9, 9, 9, 8, tzinfo=UTC),
    )
    return publish_card, publisher, raw


def _offer_row(pool: SqliteLifecyclePersistence, build_id: str) -> dict[str, Any]:
    rows = [
        s
        for s in pool.read_stages(build_id)
        if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ]
    assert len(rows) == 1, "exactly one durable merge offer row"
    return dict(rows[-1].details.get(MERGE_OFFER_DETAILS_KEY) or {})


@pytest.mark.asyncio
async def test_the_card_is_the_press_s_card(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """Request id, durable row and Slack join key all belong to the press."""
    build_id = persistence.record_pending_build(_payload())
    publish_card, publisher, raw = _publisher_for(persistence, tmp_path)

    result = await publish_card(
        build_id=build_id,
        feature_id="FEAT-CARD",
        rationale="mode-c-commits-present",
        branch="repair/TASK-CARDFIX1",
        gates=_gates(),
    )

    # Nothing comes back: the owner's answer goes to the merge press.
    assert result is None

    # The durable row the press matches on, with the press's request id.
    offer = _offer_row(persistence, build_id)
    assert offer["request_id"] == merge_request_id(build_id)
    assert offer["request_id"].startswith("merge-")
    assert offer["correlation_id"] == CORRELATION
    assert offer["approval_subject"] == "agents.approval.forge.merge-FEAT-CARD"

    # The approval request on the wire, on the press's own subject.
    assert len(raw) == 1
    subject, body = raw[0]
    assert subject == "agents.approval.forge.merge-FEAT-CARD"
    envelope = json.loads(body.decode("utf-8"))
    assert envelope["payload"]["request_id"] == merge_request_id(build_id)

    # The card jarvis renders, with the synthetic join key that lets the tap
    # work on a build the terminal registry has already seen.
    assert len(publisher.paused) == 1
    paused = publisher.paused[0]
    assert paused.build_id == "merge-FEAT-CARD"
    assert paused.feature_id == "FEAT-CARD"
    assert paused.approval_subject == "agents.approval.forge.merge-FEAT-CARD"
    assert paused.correlation_id == CORRELATION


@pytest.mark.asyncio
async def test_the_words_are_the_checkpoint_s_own(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """What was checked, what is deferred, which branch, what approve does."""
    build_id = persistence.record_pending_build(_payload())
    persistence.record_merge_branch(build_id, "repair/TASK-CARDFIX1")
    publish_card, publisher, _raw = _publisher_for(persistence, tmp_path)

    await publish_card(
        build_id=build_id,
        feature_id="FEAT-CARD",
        branch="repair/TASK-CARDFIX1",
        gates=_gates(),
    )

    words = publisher.paused[0].rationale
    assert "declared suite GREEN (817 passed, 2 deselected)" in words
    assert "repair/TASK-CARDFIX1" in words
    # The checks that could not be proved here, said in ordinary words.
    assert (
        "Some of the checks this repository asks for could not be proved on "
        "this branch here; they are run against the candidate in the sandbox "
        "before anything is merged." in words
    )
    assert (
        "Approve = check the candidate in the sandbox, merge the branch into "
        "main and promote it." in words
    )
    assert "Reject = nothing changes" in words
    # No house words and no internal ids anywhere near a card Rich reads —
    # the gate report's own deferred sentence carries all of these, and it
    # stays on the decision and in the log where it belongs.
    for shorthand in (
        "gate_check",
        "merge_deploy_offer",
        "DF-021",
        "§c.3",
        "stamped check",
        "live-gate",
        "live gate",
        "merge press",
        "probe:bus",
        "probe:process",
    ):
        assert shorthand not in words


@pytest.mark.asyncio
async def test_a_card_with_nothing_deferred_leaves_that_sentence_out(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """Every gate set that defers nothing gets a three-sentence card."""
    build_id = persistence.record_pending_build(_payload())
    publish_card, publisher, _raw = _publisher_for(persistence, tmp_path)

    await publish_card(
        build_id=build_id,
        feature_id="FEAT-CARD",
        branch="repair/TASK-CARDFIX1",
        gates=GatesReport(status=GateStatus.GREEN, detail="12 of 12 green"),
    )

    words = publisher.paused[0].rationale
    assert "12 of 12 green" in words
    assert "could not be proved" not in words


def test_the_words_stand_alone_without_a_gate_report() -> None:
    """A card never claims a check it cannot name."""
    words = merge_card_words(feature_id="FEAT-CARD", branch="autobuild/FEAT-CARD")
    assert words.startswith("FEAT-CARD is ready to merge on branch")
    assert "came back green" in words


@pytest.mark.asyncio
async def test_the_checkpoint_s_own_row_is_written_after_the_card(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """The one-card latch and the self-closed measure both read this row."""
    build_id = persistence.record_pending_build(_payload())
    publish_card, _publisher, _raw = _publisher_for(persistence, tmp_path)

    await publish_card(build_id=build_id, feature_id="FEAT-CARD", gates=_gates())

    rows = [
        s
        for s in persistence.read_stages(build_id)
        if s.target_identifier == _MERGE_CARD_TARGET_IDENTIFIER
    ]
    assert len(rows) == 1
    assert rows[0].stage_label == MERGE_READY_CHECKPOINT_LABEL
    assert rows[0].status == "GATED"


@pytest.mark.asyncio
async def test_a_second_card_for_the_same_build_is_refused_loudly(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """One card per merge word — and a refusal is never a silent delivery."""
    build_id = persistence.record_pending_build(_payload())
    publish_card, publisher, raw = _publisher_for(persistence, tmp_path)

    await publish_card(build_id=build_id, feature_id="FEAT-CARD", gates=_gates())
    with pytest.raises(MergeCardNotPublished):
        await publish_card(build_id=build_id, feature_id="FEAT-CARD", gates=_gates())

    assert len(raw) == 1
    assert len(publisher.paused) == 1
    assert (
        len(
            [
                s
                for s in persistence.read_stages(build_id)
                if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_an_offer_that_cannot_be_made_publishes_nothing(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """A repository the daemon has no path for cannot be merged — say so."""
    build_id = persistence.record_pending_build(_payload())
    service, publisher, raw = _offer_service(persistence, tmp_path)
    service._config.planning.target_repo_paths.clear()  # noqa: SLF001 — the seam
    publish_card = make_merge_card_publisher(
        offer_service=service,
        sqlite_pool=persistence,
        clock=lambda: datetime(2026, 9, 9, 9, 8, tzinfo=UTC),
    )

    with pytest.raises(MergeCardNotPublished):
        await publish_card(build_id=build_id, feature_id="FEAT-CARD", gates=_gates())

    assert raw == []
    assert publisher.paused == []
    assert persistence.read_stages(build_id) == []


@pytest.mark.asyncio
async def test_a_refusal_says_nothing_reached_the_wire(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """The journey's record must not hedge about a card that never existed.

    Every refusal happens before the offer touches the wire, so the raise
    carries ``card_reached_the_wire = False`` and the checkpoint writes "no
    card was published" rather than "the card may be on the wire".
    """
    build_id = persistence.record_pending_build(_payload())
    service, _publisher, _raw = _offer_service(persistence, tmp_path)
    service._config.planning.target_repo_paths.clear()  # noqa: SLF001 — the seam
    publish_card = make_merge_card_publisher(
        offer_service=service,
        sqlite_pool=persistence,
        clock=lambda: datetime(2026, 9, 9, 9, 8, tzinfo=UTC),
    )

    with pytest.raises(MergeCardNotPublished) as raised:
        await publish_card(build_id=build_id, feature_id="FEAT-CARD", gates=_gates())

    assert raised.value.card_reached_the_wire is False


@pytest.mark.asyncio
async def test_the_feature_id_falls_back_to_the_build_row(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """The Mode C call sites pass ``feature_id=""`` — resolve it, don't guess."""
    build_id = persistence.record_pending_build(_payload())
    publish_card, publisher, _raw = _publisher_for(persistence, tmp_path)

    await publish_card(build_id=build_id, feature_id="", gates=_gates())

    assert publisher.paused[0].feature_id == "FEAT-CARD"
    assert publisher.paused[0].build_id == "merge-FEAT-CARD"


# ---------------------------------------------------------------------------
# When planning registered no after-deploy check (4 October 2026)
#
# The checkpoint card reads the same record, through the same reader, as the
# routine card, and says the same sentence. A registered check, no record,
# or a reader that falls over leaves the card byte for byte as it was.
# ---------------------------------------------------------------------------

_PATCH_SENTENCE = (
    "Planning did not register an after-deploy check for this feature "
    "automatically (reason: only GET addresses are supported, and this one "
    "is PATCH)."
)
_APPROVE_SENTENCE = (
    "Approve = check the candidate in the sandbox, merge the branch into "
    "main and promote it."
)


def _record_planning_run(pool: SqliteLifecyclePersistence, correlation_id: str) -> None:
    pool.connection.execute(
        "INSERT INTO planning_runs (correlation_id, state, originating_user, "
        "expected_approver, request_text, target_repo, triggered_by, "
        "originating_adapter, parent_request_id, queued_at) VALUES "
        "(?, 'BUILD_QUEUED', 'rich', 'rich', 'a sentence', ?, 'jarvis', "
        "'slack', NULL, '2026-10-04T00:00:00Z')",
        (correlation_id, REPO),
    )
    pool.connection.commit()


def _record_gate_step(
    pool: SqliteLifecyclePersistence, correlation_id: str, details: dict[str, Any]
) -> None:
    pool.connection.execute(
        "INSERT INTO planning_run_events (correlation_id, stage_label, status, "
        "actor_identity, details_json, recorded_at) VALUES "
        "(?, 'qa-feature-gate', 'approved', 'planning-driver', ?, "
        "'2026-10-04T00:00:00Z')",
        (correlation_id, json.dumps(details)),
    )
    pool.connection.commit()


_PATCH_SKIP = {
    "skipped": True,
    "reason": "the spec names PATCH /users/{user_id}/deactivate; only a GET "
    "address can be checked automatically — no gate registered",
    "reason_code": "unsupported_method",
    "feature_id": "FEAT-CARD",
    "address": {"method": "PATCH", "path": "/users/{user_id}/deactivate"},
}


def _repair_payload(parent_build_id: str) -> SimpleNamespace:
    payload = _payload()
    payload.correlation_id = f"fix-{parent_build_id}"
    payload.queued_at = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    return payload


async def _checkpoint_card(
    pool: SqliteLifecyclePersistence, tmp_path: Path, build_id: str, **kwargs: Any
) -> str:
    pool.record_merge_branch(build_id, "repair/TASK-CARDFIX1")
    service, publisher, _raw = _offer_service(pool, tmp_path)
    publish_card = make_merge_card_publisher(
        offer_service=service,
        sqlite_pool=pool,
        clock=lambda: datetime(2026, 9, 9, 9, 8, tzinfo=UTC),
        **kwargs,
    )
    await publish_card(
        build_id=build_id,
        feature_id="FEAT-CARD",
        branch="repair/TASK-CARDFIX1",
        gates=_gates(),
    )
    assert len(publisher.paused) == 1
    return publisher.paused[0].rationale


def _card_as_it_was() -> str:
    """The checkpoint card before this sentence existed."""
    return merge_card_words(
        feature_id="FEAT-CARD", branch="repair/TASK-CARDFIX1", gates=_gates()
    )


@pytest.mark.asyncio
async def test_a_skip_puts_the_sentence_on_the_checkpoint_card(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    build_id = persistence.record_pending_build(_payload())
    _record_planning_run(persistence, CORRELATION)
    _record_gate_step(persistence, CORRELATION, _PATCH_SKIP)

    words = await _checkpoint_card(persistence, tmp_path, build_id)

    assert f"{_PATCH_SENTENCE} {_APPROVE_SENTENCE}" in words
    # Only the sentence was added; the rest is the card as it was.
    assert words.replace(f"{_PATCH_SENTENCE} ", "", 1) == _card_as_it_was()


@pytest.mark.asyncio
async def test_a_repair_build_s_checkpoint_card_finds_its_parent_s_planning_run(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    """The checkpoint card is the repair journey's card, so this is its usual
    case: ``fix-<parent build id>`` goes one hop to the parent's row."""
    parent = persistence.record_pending_build(_payload())
    build_id = persistence.record_pending_build(_repair_payload(parent))
    assert build_id != parent
    _record_planning_run(persistence, CORRELATION)
    _record_gate_step(persistence, CORRELATION, _PATCH_SKIP)

    words = await _checkpoint_card(persistence, tmp_path, build_id)

    assert _PATCH_SENTENCE in words


@pytest.mark.asyncio
async def test_an_older_free_text_record_gives_the_checkpoint_card_a_sentence(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    build_id = persistence.record_pending_build(_payload())
    _record_planning_run(persistence, CORRELATION)
    _record_gate_step(
        persistence,
        CORRELATION,
        {"skipped": True, "reason": "no derivable endpoint — no gate registered"},
    )

    words = await _checkpoint_card(persistence, tmp_path, build_id)

    assert (
        "Planning did not register an after-deploy check for this feature "
        "automatically (reason: no address it could check was found)." in words
    )


@pytest.mark.asyncio
async def test_a_registered_gate_leaves_the_checkpoint_card_byte_for_byte(
    persistence: SqliteLifecyclePersistence, tmp_path: Path
) -> None:
    build_id = persistence.record_pending_build(_payload())
    _record_planning_run(persistence, CORRELATION)
    _record_gate_step(
        persistence,
        CORRELATION,
        {
            "feature_id": "FEAT-CARD",
            "gate_file": "qa/gates/active_count_gate.py",
            "endpoint": {"method": "GET", "path": "/users/active-count"},
        },
    )

    assert await _checkpoint_card(persistence, tmp_path, build_id) == _card_as_it_was()


@pytest.mark.asyncio
async def test_no_planning_record_leaves_the_checkpoint_card_and_is_logged(
    persistence: SqliteLifecyclePersistence,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    build_id = persistence.record_pending_build(_payload())

    with caplog.at_level("INFO", logger="forge.pipeline.merge_offer"):
        words = await _checkpoint_card(persistence, tmp_path, build_id)

    assert words == _card_as_it_was()
    assert any("has no record of the step" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_reader_error_leaves_the_checkpoint_card_and_is_logged(
    persistence: SqliteLifecyclePersistence,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    build_id = persistence.record_pending_build(_payload())
    _record_planning_run(persistence, CORRELATION)
    _record_gate_step(persistence, CORRELATION, _PATCH_SKIP)

    def _falls_over(_pool: Any, _row: Any) -> Any:
        raise RuntimeError("the ledger is locked")

    with caplog.at_level("WARNING", logger="forge.pipeline.merge_offer"):
        words = await _checkpoint_card(
            persistence, tmp_path, build_id, after_deploy_check_reader=_falls_over
        )

    assert words == _card_as_it_was()
    assert any(
        r.levelname == "WARNING" and "could not be read" in r.getMessage()
        for r in caplog.records
    )


def test_the_checkpoint_words_carry_the_shared_sentence() -> None:
    """``merge_card_words`` puts the reader's sentence just before what the
    merge word does, and nothing at all when there is no skip."""
    from forge.pipeline.merge_offer import AfterDeployCheckSkip

    skip = AfterDeployCheckSkip(
        planning_run=CORRELATION,
        reason="the address has a placeholder, {user_id}",
        reason_code="placeholder_in_address",
    )
    words = merge_card_words(
        feature_id="FEAT-CARD",
        branch="autobuild/FEAT-CARD",
        after_deploy_check_skip=skip,
    )
    assert (
        "Planning did not register an after-deploy check for this feature "
        "automatically (reason: the address has a placeholder, {user_id}). "
        f"{_APPROVE_SENTENCE}" in words
    )
    assert merge_card_words(
        feature_id="FEAT-CARD", branch="autobuild/FEAT-CARD", after_deploy_check_skip=None
    ) == merge_card_words(feature_id="FEAT-CARD", branch="autobuild/FEAT-CARD")
