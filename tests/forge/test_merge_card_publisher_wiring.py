"""The merge card composes the SAME approve-click machinery — no duplicate.

Revival design pass §c.2, Stage 1c.

The ruling's delivery shape is "the same approve-click merge card the
consumer path already delivers" (DF-021). So
:func:`make_merge_card_publisher` must go through the machinery
``_serve_gate_activation`` already owns — ``gate_check`` over
``make_gate_check_deps``, with the publisher swapped for the mirrored
approval publisher so the AGENTS request goes out first and the
``build-paused`` envelope (the one that renders the card) second.

What this file pins:

* the card runs through ``gate_check``, not a second envelope builder;
* the stage label is the phrase-book PLAIN NAME — that string is the
  card's stage copy, and user surfaces speak human;
* the mirrored publisher is installed, so the two-envelope contract
  holds and **jarvis is untouched** (the card rides existing seams);
* identity (feature_id / correlation_id) is read off the build row.

No broker is contacted anywhere: ``gate_check`` itself is replaced with
a recorder.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps_gating, _serve_gate_activation
from forge.cli._serve_gate_activation import make_merge_card_publisher
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_ready_checkpoint import (
    MERGE_READY_CHECKPOINT_LABEL,
    GatesReport,
    GateStatus,
)


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
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="card-test",
        correlation_id="dddd4444-eeee-5555-ffff-666666666666",
        parent_request_id=None,
        queued_at=datetime(2026, 7, 31, 11, 0, 0, tzinfo=UTC),
    )


class _Deps:
    """Stand-in for the assembled ``GateCheckDeps``."""

    def __init__(self) -> None:
        self.publisher: Any = "the-raw-approval-publisher"


class _Parts:
    def __init__(self, emitter: Any) -> None:
        self.publisher = "inner-approval-publisher"
        self.emitter = emitter
        self.subscriber = None
        self.injector = None
        self.expected_approver = "rich"
        # Sentinel double — the activation paths read parts.priors_reader
        # (never a real reader in the unit tier).
        self.priors_reader = "the-priors-reader"


@pytest.mark.asyncio
async def test_the_card_goes_through_gate_check_with_the_plain_name(
    monkeypatch: pytest.MonkeyPatch,
    persistence: SqliteLifecyclePersistence,
) -> None:
    build_id = persistence.record_pending_build(_payload())
    made: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    deps = _Deps()

    def fake_make_deps(parts: Any, **kwargs: Any) -> Any:
        made.append(dict(kwargs))
        return deps

    async def fake_gate_check(**kwargs: Any) -> Any:
        checked.append(dict(kwargs))
        return ("RESUMED", None)

    monkeypatch.setattr(_serve_deps_gating, "make_gate_check_deps", fake_make_deps)
    monkeypatch.setattr(_serve_gate_activation, "gate_check", fake_gate_check)

    publish_card = make_merge_card_publisher(
        parts=_Parts(emitter=object()),
        sqlite_pool=persistence,
        gate_repository=object(),
        gate_state_machine=object(),
        clock=lambda: datetime(2026, 7, 31, 11, 5, tzinfo=UTC),
    )

    outcome = await publish_card(
        build_id=build_id,
        feature_id="FEAT-CARD",
        rationale="mode-c-commits-present",
        branch="fix/FEAT-CARD",
        gates=GatesReport(status=GateStatus.GREEN),
    )

    assert outcome == "RESUMED"
    assert len(checked) == 1
    # THE CARD COPY: the plain name, not the pre-ruling codename.
    assert checked[0]["stage_label"] == MERGE_READY_CHECKPOINT_LABEL
    assert "pull-request" not in checked[0]["stage_label"]
    assert checked[0]["build_id"] == build_id
    assert checked[0]["feature_id"] == "FEAT-CARD"
    # Identity comes off the build row.
    ctx = made[0]["ctx"]
    assert ctx.correlation_id == "dddd4444-eeee-5555-ffff-666666666666"
    assert ctx.build_id == build_id
    # The mirrored publisher is installed — the AGENTS request first,
    # the build-paused second (the two-envelope jarvis contract).
    assert deps.publisher != "inner-approval-publisher"
    assert type(deps.publisher).__name__ == "_MirroredApprovalPublisher"


@pytest.mark.asyncio
async def test_without_an_emitter_the_raw_publisher_is_kept(
    monkeypatch: pytest.MonkeyPatch,
    persistence: SqliteLifecyclePersistence,
) -> None:
    """Unit tiers with no emitter still publish the approval request."""
    build_id = persistence.record_pending_build(_payload())
    deps = _Deps()

    monkeypatch.setattr(
        _serve_deps_gating, "make_gate_check_deps", lambda parts, **kw: deps
    )

    async def fake_gate_check(**kwargs: Any) -> Any:
        return ("RESUMED", None)

    monkeypatch.setattr(_serve_gate_activation, "gate_check", fake_gate_check)

    publish_card = make_merge_card_publisher(
        parts=_Parts(emitter=None),
        sqlite_pool=persistence,
        gate_repository=object(),
        gate_state_machine=object(),
        clock=lambda: datetime(2026, 7, 31, 11, 5, tzinfo=UTC),
    )
    await publish_card(build_id=build_id, feature_id="FEAT-CARD")

    assert deps.publisher == "the-raw-approval-publisher"


@pytest.mark.asyncio
async def test_the_feature_id_falls_back_to_the_build_row(
    monkeypatch: pytest.MonkeyPatch,
    persistence: SqliteLifecyclePersistence,
) -> None:
    """The Mode C call sites pass ``feature_id=""`` — resolve it, don't guess."""
    build_id = persistence.record_pending_build(_payload())
    checked: list[dict[str, Any]] = []

    monkeypatch.setattr(
        _serve_deps_gating, "make_gate_check_deps", lambda parts, **kw: _Deps()
    )

    async def fake_gate_check(**kwargs: Any) -> Any:
        checked.append(dict(kwargs))
        return ("RESUMED", None)

    monkeypatch.setattr(_serve_gate_activation, "gate_check", fake_gate_check)

    publish_card = make_merge_card_publisher(
        parts=_Parts(emitter=None),
        sqlite_pool=persistence,
        gate_repository=object(),
        gate_state_machine=object(),
        clock=lambda: datetime(2026, 7, 31, 11, 5, tzinfo=UTC),
    )
    await publish_card(build_id=build_id, feature_id="")

    assert checked[0]["feature_id"] == "FEAT-CARD"
