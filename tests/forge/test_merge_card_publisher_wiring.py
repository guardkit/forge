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
