"""Previews: the posted card kept true, Rebuild preview, and the slots (16 October 2026).

The design (ai-transition ``docs/designs/previews-expiry-rebuild-slots-2026-10-16.md``,
accepted by Codex in its fourth review round) in tests, Forge's side:

* each previewed card is described from the ledger — latest offer row, newest
  preview lease, decision row — with ONE revision counter per card raised in
  the same transaction as every change, at once on a change and again on
  every sweep;
* Rebuild preview (``defer`` + ``rebuild-preview``) prepares the SAME
  candidate again, and its amended offer row is written in one transaction
  with the decision check, after its merge-check read;
* a card never waits for a slot; a Rebuild press takes the oldest ready,
  unanswered preview's slot, serialised with Approve in the ledger.

Everything is real except what the preview service already fakes in its own
tests (the sandbox venue, the deploy dispatch and the clock) and the merge
press's run after an approval (recorded, not run).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nats_core.envelope import EventType, MessageEnvelope
from nats_core.events import ApprovalResponsePayload

from forge.adapters.sqlite.connect import connect_writer
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import RemoteStartPoint
from forge.deploy.review_preview import (
    PreviewLease,
    PreviewLeaseStore,
    ReviewPreviewService,
)
from forge.lifecycle import card_revisions, migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import merge_card
from forge.pipeline.merge_executor import (
    MERGE_DECISION_DETAILS_KEY,
    MERGE_DECISION_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
    MergeExecutorDeps,
)
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
)
from forge.planning.work_queue_loop import (
    MERGE_CARD_GRACE_SECONDS,
    NO_MERGE_CARD,
    NOT_LANDED_YET,
    work_landing,
)
from tests.forge.deploy.test_review_preview import (
    BASE_URL,
    PROJECT,
    SHA_CAND,
    SHA_FETCHED,
    SHA_MAIN,
    START,
    TREE_CAND,
    FakeClock,
    FakeDispatch,
    FakeVenue,
    _profile,
)

APPROVER = "rich"
WORDS = "built — 3 of 3 tasks passed."


class FakeGit:
    """The project's git, as the offer and the rebuild ask it."""

    async def rev_parse(self, ref: str) -> str | None:
        return TREE_CAND if ref.endswith("^{tree}") else SHA_CAND

    async def fetch_remote_start_point(self) -> RemoteStartPoint:
        return RemoteStartPoint(branch="main", commit=SHA_FETCHED)


class CardWire:
    """Where descriptions go; can be told to fail."""

    def __init__(self) -> None:
        self.paused: list[Any] = []
        self.failures = 0

    async def publish_build_paused(self, payload: Any) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the bus is down")
        self.paused.append(payload)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.cx = connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(self.cx)
        self.pool = SqliteLifecyclePersistence(connection=self.cx)
        self.clock = FakeClock()
        self.venue = FakeVenue()
        self.dispatch = FakeDispatch()
        self.profile = _profile()
        self.store = PreviewLeaseStore(self.cx)
        counter = iter(range(1, 1000))
        self.service = ReviewPreviewService(
            store=self.store,
            profile_reader=lambda project: self.profile,
            venue_for=lambda project: self.venue,
            deploy_dispatcher=self.dispatch,
            repo_paths={PROJECT: "/clone"},
            evidence_root=tmp_path / "evidence",
            evidence_base_url=BASE_URL,
            clock=self.clock,
            sleep=self.clock.sleep,
            token_factory=lambda: "the-right-token",
            lease_id_factory=lambda: f"pv{next(counter):012x}",
            run_id_factory=lambda: f"pvrun-{next(counter):012x}",
            blocking_sleep=lambda _s: None,
        )
        self.config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {PROJECT: str(tmp_path / "clone")}},
                "approval": {"expected_approver": APPROVER},
                "merge_executor": {"enabled": True},
            }
        )
        self.wire = CardWire()
        # No expiry timers unless a test asks for them (they would sleep a day).
        self.expiry_sleep: Any = None
        self.approvals: list[tuple[str, bytes]] = []
        self.offers = self.new_offer_service()
        self.reports: list[Any] = []
        self.approved: list[dict[str, Any]] = []
        self.consumer = self.new_consumer(self.offers)

    def new_offer_service(self) -> MergeOfferService:
        async def raw_publish(subject: str, body: bytes) -> None:
            self.approvals.append((subject, body))

        async def git_head(_root: Path, _branch: str | None = None) -> str:
            return SHA_MAIN

        return MergeOfferService(
            config=self.config,
            pool=self.pool,
            pipeline_publisher=self.wire,
            raw_publish=raw_publish,
            git_head=git_head,
            git_surface=lambda _repo, _root: FakeGit(),
            baseline_reader=lambda _b: None,
            finished_feature_reader=lambda *_a, **_k: (None, None, "nothing"),
            scope_pass=lambda **_k: None,
            after_deploy_check_reader=lambda _p, _r: None,
            clock=self.clock,
            review_preview=self.service,
            expiry_sleep=self.expiry_sleep,
        )

    def new_consumer(self, offers: MergeOfferService) -> MergeApprovalConsumer:
        async def publish_stage_complete(report: Any) -> None:
            self.reports.append(report)

        deps = MergeExecutorDeps(
            config=self.config,
            pool=self.pool,
            pipeline_publisher=SimpleNamespace(
                publish_stage_complete=publish_stage_complete
            ),
            guardkit_run=AsyncMock(),
            deploy_dispatcher=AsyncMock(),
            clock=self.clock,
            review_preview=self.service,
            describe_card=offers.describe_card,
            rebuild_preview=offers.rebuild_preview,
        )
        consumer = MergeApprovalConsumer(deps)

        async def run_approved(**kwargs: Any) -> None:
            self.approved.append(kwargs)

        consumer._run_approved = run_approved  # type: ignore[method-assign]
        return consumer

    # -- builds, offers and presses -----------------------------------------

    def add_build(self, build_id: str, feature_id: str) -> None:
        self.cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
            "mode, start_commit, target_branch) VALUES (?, ?, ?, ?, 'f.yaml', "
            "'COMPLETE', 'cli', ?, '2026-10-12T08:00:00+00:00', 'mode-a', ?, 'main')",
            (
                build_id,
                feature_id,
                PROJECT,
                f"autobuild/{feature_id}",
                f"corr-{build_id}",
                "0" * 40,
            ),
        )

    async def offer(self, build_id: str, feature_id: str) -> None:
        self.add_build(build_id, feature_id)
        await self.offers.offer(
            build_id=build_id,
            feature_id=feature_id,
            card_words=lambda _b, _m: f"{feature_id} {WORDS}",
        )
        await self.drain()

    def envelope(
        self, build_id: str, decision: str, *, notes: str | None = None
    ) -> MessageEnvelope:
        return MessageEnvelope(
            source_id="jarvis",
            event_type=EventType.APPROVAL_RESPONSE,
            correlation_id=f"corr-{build_id}",
            payload=ApprovalResponsePayload(
                request_id=f"merge-{build_id}",
                decision=decision,
                decided_by=APPROVER,
                notes=notes,
            ).model_dump(mode="json"),
        )

    async def press(
        self,
        build_id: str,
        decision: str,
        *,
        notes: str | None = None,
        drain: bool = True,
    ) -> None:
        await self.consumer.handle_envelope(
            self.envelope(build_id, decision, notes=notes)
        )
        if drain:
            await self.drain()

    async def rebuild(self, build_id: str, *, drain: bool = True) -> None:
        await self.press(build_id, "defer", notes=merge_card.REBUILD_NOTE, drain=drain)

    async def drain(self) -> None:
        for _ in range(50):
            pending = [*self.consumer._tasks, *self.offers._card_tasks]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def expire(self) -> None:
        """Move past every preview's time and run the sweep."""
        self.clock.now += timedelta(hours=25)
        await self.service.sweep(now=self.clock.now)
        await self.offers.sweep_cards(now=self.clock.now)
        await self.drain()

    # -- reading --------------------------------------------------------------

    def cards(self, build_id: str) -> list[Any]:
        return [
            p.merge_card
            for p in self.wire.paused
            if p.merge_card is not None and p.merge_card.build_id == build_id
        ]

    def card(self, build_id: str) -> Any:
        cards = self.cards(build_id)
        assert cards, f"no description of {build_id} went out"
        return cards[-1]

    def paused(self, build_id: str) -> Any:
        return [
            p
            for p in self.wire.paused
            if p.merge_card is not None and p.merge_card.build_id == build_id
        ][-1]

    def offer_rows(self, build_id: str) -> list[dict[str, Any]]:
        return [
            s.details[MERGE_OFFER_DETAILS_KEY]
            for s in self.pool.read_stages(build_id)
            if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        ]

    def decisions(self, build_id: str) -> list[Any]:
        return [
            s
            for s in self.pool.read_stages(build_id)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ]

    def revision(self, build_id: str) -> int:
        return card_revisions.card_revision(self.cx, build_id)

    def lease_of(self, build_id: str) -> PreviewLease:
        lease = self.store.newest_for_build(build_id)
        assert lease is not None
        return lease

    def named_lease(self, build_id: str) -> PreviewLease:
        run = self.offer_rows(build_id)[-1]["review_evidence"]["run_id"]
        lease = self.store.for_run(run)
        assert lease is not None
        return lease

    def hold_a_slot(
        self, port: int, *, build_id: str, feature_id: str, state: str
    ) -> str:
        """A lease another preparation holds (written straight into the ledger)."""
        lease_id = f"pvhold{port}"
        self.store.insert(
            PreviewLease(
                lease_id=lease_id,
                slot=0 if port == 8950 else 1,
                port=port,
                token="t",
                project=PROJECT,
                feature_id=feature_id,
                build_id=build_id,
                candidate_sha=SHA_CAND,
                candidate_tree=TREE_CAND,
                preview_tree=None,
                run_id=f"run-{lease_id}",
                compose_project=None,
                evidence_dir=None,
                state=state,
                reason=None,
                created_at=self.clock.now.isoformat(),
                app_expires_at=(self.clock.now + timedelta(hours=24)).isoformat(),
                evidence_expires_at=(self.clock.now + timedelta(days=14)).isoformat(),
            ),
            now=self.clock.now,
        )
        return lease_id


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _pending_offers(cx: sqlite3.Connection) -> list[str]:
    """The idle check's question (feature_routing / merge_retirement_cleanup)."""
    return [
        str(row[0])
        for row in cx.execute(
            "SELECT DISTINCT offered.build_id FROM stage_log AS offered "
            "WHERE offered.target_identifier = 'merge_deploy_offer' "
            "AND NOT EXISTS (SELECT 1 FROM stage_log AS decided "
            "WHERE decided.build_id = offered.build_id "
            "AND decided.target_identifier = 'merge_deploy_decision' "
            "AND decided.id > offered.id)"
        )
    ]


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _tables(cx: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_the_table_is_outside_the_ladder_and_made_when_needed(h: Harness) -> None:
    """Schema stays 19 (coordinator's decision, 16 October 2026); a ledger
    without the table works, and the first counter write makes it."""
    assert h.cx.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 19
    assert migrations.target_schema_version() == 19
    assert "merge_card_revisions" not in _tables(h.cx)
    assert h.revision("nobody") == 0
    with card_revisions.immediate_transaction(h.cx):
        assert card_revisions.bump_card_revision(h.cx, "b1", START) == 1
    assert "merge_card_revisions" in _tables(h.cx)
    card_revisions.ensure_table(h.cx)  # idempotent
    assert h.revision("b1") == 1


@pytest.mark.asyncio
async def test_a_card_works_on_a_ledger_without_the_table(h: Harness) -> None:
    assert "merge_card_revisions" not in _tables(h.cx)
    await h.offer("b1", "FEAT-AB12")
    assert h.card("b1").status == "ready" and h.card("b1").revision >= 1


def test_release_2026_10_16_1_accepts_a_ledger_with_the_extra_table(
    tmp_path: Path,
) -> None:
    """Rolling back to 2026.10.16-1 stays an image swap.

    These are the checks that release (Forge 5c45fef7) makes of a ledger:
    the migrations, the planning-handoff preflight and reader, and the estate
    rollout helper's ledger_state. The first three files on this branch are
    byte for byte that release's, and the rollout helper differs from it only
    by the two boot-step lines that make this table (checked with git diff
    against 5c45fef7 when this was written), so running them here is running
    that release's checks.
    """
    import importlib.util

    from forge.lifecycle.planning_handoff_preflight import (
        preflight_retired_planning_handoff_correlations,
    )
    from forge.lifecycle.planning_handoff_retirement import (
        retired_planning_handoff_correlations,
    )

    db = tmp_path / "forge.db"
    cx = connect_writer(db)
    migrations.apply_at_boot(cx)
    card_revisions.ensure_table(cx)
    with card_revisions.immediate_transaction(cx):
        card_revisions.bump_card_revision(cx, "b1", START)
    assert migrations.apply_at_boot(cx) == 19  # nothing to apply, nothing refused
    retired_planning_handoff_correlations(cx)
    cx.close()
    preflight_retired_planning_handoff_correlations(db)

    bundle = Path(__file__).resolve().parents[3] / "deploy" / "estate"
    spec = importlib.util.spec_from_file_location(
        "rollout_support_1016", bundle / "rollout_support.py"
    )
    rollout = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(rollout)
    assert rollout.LEDGER_SCHEMA_VERSION == 19
    state = rollout.ledger_state(db)
    assert state["schema_version"] == 19
    # The helper's own copy of the boot steps makes the same table, so a
    # started ledger and its loaded copy still match.
    assert "card_revisions.ensure_table(c)" in rollout.BOOT_SQLITE_CODE


def test_the_coordinator_boot_makes_the_table() -> None:
    import inspect

    from forge.cli import _serve_production

    source = inspect.getsource(_serve_production.bind_production_serve)
    assert source.index("_bridge_registry_migration.apply(connection)") < source.index(
        "_ensure_card_revisions(connection)"
    )


def test_the_lifecycle_names_match_the_pipelines() -> None:
    from forge.pipeline.merge_executor import MERGE_DECISION_DETAILS_KEY as key

    assert card_revisions.OFFER_ROW == MERGE_OFFER_TARGET_IDENTIFIER
    assert card_revisions.OFFER_DETAILS_KEY == MERGE_OFFER_DETAILS_KEY
    assert card_revisions.DECISION_ROW == MERGE_DECISION_TARGET_IDENTIFIER
    assert card_revisions.DECISION_DETAILS_KEY == key == MERGE_DECISION_DETAILS_KEY


def test_a_bump_outside_a_transaction_is_refused(h: Harness) -> None:
    with pytest.raises(RuntimeError):
        card_revisions.bump_card_revision(h.cx, "b1", START)


# ---------------------------------------------------------------------------
# Part 1: describing the card
# ---------------------------------------------------------------------------


class TestTheFirstCard:
    @pytest.mark.asyncio
    async def test_it_carries_the_block_and_reads_as_before(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        paused = h.paused("b1")
        card = paused.merge_card
        # The buttons keep the synthetic id; the real one is for identity only.
        assert paused.build_id == "merge-FEAT-AB12"
        assert card.build_id == "b1"
        assert card.request_id == "merge-b1"
        assert card.correlation_id == "corr-b1" == paused.correlation_id
        assert card.approval_subject == "agents.approval.forge.merge-FEAT-AB12"
        assert card.candidate_sha == SHA_CAND
        assert (card.status, card.rebuild_offered, card.status_text) == (
            "ready",
            False,
            None,
        )
        assert card.revision == h.revision("b1") >= 1
        assert card.words == f"FEAT-AB12 {WORDS}"
        # The rationale is laid out as before: words, then the preview line.
        assert paused.rationale == (
            f"FEAT-AB12 {WORDS}\nPreview ready — see the buttons below."
        )
        assert paused.review_evidence.state == "ready"
        # The offer row keeps the card's sentences, so it can be described again.
        assert h.offer_rows("b1")[0][merge_card.CARD_WORDS_KEY] == f"FEAT-AB12 {WORDS}"
        # One approval request, ever.
        assert len(h.approvals) == 1

    @pytest.mark.asyncio
    async def test_two_offers_for_one_feature_and_commit_are_two_cards(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.offer("b2", "FEAT-AB12")
        one, two = h.card("b1"), h.card("b2")
        assert one.candidate_sha == two.candidate_sha
        assert (one.request_id, one.build_id) == ("merge-b1", "b1")
        assert (two.request_id, two.build_id) == ("merge-b2", "b2")
        assert one.correlation_id != two.correlation_id


class TestRevisions:
    @pytest.mark.asyncio
    async def test_a_lease_only_change_moves_the_counter(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        before = h.revision("b1")
        lease = h.lease_of("b1")
        h.store.update(lease.lease_id, now=h.clock.now, evidence_dir=lease.evidence_dir)
        assert h.revision("b1") == before + 1
        # ...and the card is described at once with the new counter.
        await h.drain()
        assert h.card("b1").revision == before + 1

    @pytest.mark.asyncio
    async def test_expiry_shows_on_the_same_card_with_rebuild_and_a_newer_revision(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        ready = h.card("b1")
        await h.expire()
        ended = h.card("b1")
        assert ended.request_id == ready.request_id
        assert ended.revision > ready.revision
        assert (ended.status, ended.rebuild_offered) == ("ended", True)
        expiry = h.lease_of("b1").app_expires_at[11:16]
        assert ended.status_text == f"Preview expired at {expiry} UTC."
        evidence = h.paused("b1").review_evidence
        assert evidence.state == "ended" and evidence.app_url is None
        assert evidence.report_url is not None  # the report link stays
        # Every "ready" description is older than the "ended" one, so Jarvis,
        # which never applies an older revision, can never go back to ready.
        assert all(
            c.revision < ended.revision for c in h.cards("b1") if c.status == "ready"
        )

    @pytest.mark.asyncio
    async def test_the_sweep_describes_every_live_card_again(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        sent = len(h.cards("b1"))
        await h.offers.sweep_cards(now=h.clock.now)
        assert len(h.cards("b1")) == sent + 1
        assert h.cards("b1")[-1] == h.cards("b1")[-2]  # the same description again

    @pytest.mark.asyncio
    async def test_a_failed_send_is_sent_again_by_the_next_sweep(
        self, h: Harness
    ) -> None:
        """Covers a failed Slack edit and an update published while Jarvis was
        down too: Forge's answer to all three is the same next sweep."""
        await h.offer("b1", "FEAT-AB12")
        h.clock.now += timedelta(hours=25)
        h.wire.failures = 10
        await h.service.sweep(now=h.clock.now)
        await h.drain()
        assert h.card("b1").status == "ready"  # the "ended" sends all failed
        h.wire.failures = 0
        await h.offers.sweep_cards(now=h.clock.now)
        assert h.card("b1").status == "ended"

    @pytest.mark.asyncio
    async def test_cards_from_before_this_release_are_not_described(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        h.cx.execute(
            "UPDATE stage_log SET details_json = replace(details_json, "
            "'\"card_words\"', '\"old_words\"')"
        )
        sent = len(h.wire.paused)
        await h.offers.sweep_cards(now=h.clock.now)
        assert len(h.wire.paused) == sent

    @pytest.mark.asyncio
    async def test_decided_cards_stop_being_described_after_a_day(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.press("b1", "reject")
        assert h.card("b1").status == "decided"
        assert merge_card.cards_to_describe(h.cx, h.clock.now) == ["b1"]
        assert (
            merge_card.cards_to_describe(h.cx, h.clock.now + timedelta(hours=24)) == []
        )


# ---------------------------------------------------------------------------
# Decisions and the click latch
# ---------------------------------------------------------------------------


class TestDecisions:
    @pytest.mark.asyncio
    async def test_reject_is_described_as_rejected_by(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        before = h.revision("b1")
        await h.press("b1", "reject")
        card = h.card("b1")
        assert (card.status, card.decision, card.decided_by) == (
            "decided",
            "reject",
            APPROVER,
        )
        assert card.status_text == f"Rejected by {APPROVER}."
        assert card.revision == before + 2  # the decision, then the release
        assert h.lease_of("b1").state == "ended"
        assert [r.result for r in h.reports] == ["rejected"]

    @pytest.mark.asyncio
    async def test_approve_is_described_as_approved_by(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.press("b1", "approve")
        card = h.card("b1")
        assert (card.status, card.status_text) == (
            "decided",
            f"Approved by {APPROVER}.",
        )
        assert len(h.approved) == 1
        assert (
            h.approved[0]["expected_preview_tree"]
            == h.offer_rows("b1")[-1]["preview_tree"]
        )
        decision = h.decisions("b1")[0].details[MERGE_DECISION_DETAILS_KEY]
        assert decision["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_a_refused_approve_then_reject_reaches_forge(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        ended = h.card("b1")
        await h.press("b1", "approve")
        refused = h.card("b1")
        # Refused: nothing recorded, the counter moved on, the card undecided.
        assert h.decisions("b1") == [] and h.approved == []
        assert refused.revision == ended.revision + 1
        assert refused.status == "ended" and refused.decision is None
        await h.press("b1", "reject")
        assert [d.status for d in h.decisions("b1")] == ["SKIPPED"]
        assert h.card("b1").status == "decided"

    @pytest.mark.asyncio
    async def test_a_late_description_after_a_decision_still_says_decided(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.press("b1", "approve")
        decided = h.card("b1")
        await h.offers.describe_card("b1")
        await h.offers.sweep_cards(now=h.clock.now)
        assert {c.status for c in h.cards("b1")[-2:]} == {"decided"}
        assert h.card("b1").revision >= decided.revision

    @pytest.mark.asyncio
    async def test_a_finished_build_s_card_takes_every_press(self, h: Harness) -> None:
        """Approve, Reject and Rebuild all reach Forge for a COMPLETE build,
        from the first card and from a replacement card (same block)."""
        await h.offer("b1", "FEAT-AB12")
        first = h.card("b1")
        await h.offers.sweep_cards(now=h.clock.now)
        replacement = h.paused("b1")
        assert replacement.build_id == "merge-FEAT-AB12"
        assert replacement.merge_card.request_id == first.request_id
        assert replacement.approval_subject == first.approval_subject
        await h.expire()
        await h.rebuild("b1")
        assert h.card("b1").status == "ready"
        await h.press("b1", "approve")
        assert h.card("b1").status == "decided"
        await h.offer("b2", "FEAT-CD34")
        await h.press("b2", "reject")
        assert h.card("b2").decision == "reject"
        # Still one approval request per offer: nothing new was published.
        assert len(h.approvals) == 2


# ---------------------------------------------------------------------------
# Part 2: Rebuild preview
# ---------------------------------------------------------------------------


class TestRebuild:
    @pytest.mark.asyncio
    async def test_rebuild_after_expiry_previews_the_same_candidate(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        first = h.offer_rows("b1")[0]
        await h.expire()
        await h.rebuild("b1")
        rows = h.offer_rows("b1")
        assert len(rows) == 2
        amended = rows[-1]
        assert amended["request_id"] == first["request_id"]
        for pin in ("candidate_sha", "candidate_tree", "branch", "expect_main_sha"):
            assert amended[pin] == first[pin]
        assert amended[merge_card.REBUILDS_KEY] == 1
        assert (
            amended["review_evidence"]["run_id"] != first["review_evidence"]["run_id"]
        )
        assert amended["review_evidence"]["candidate_sha"] == SHA_CAND
        card = h.card("b1")
        assert (card.status, card.rebuild_offered) == ("ready", False)
        # It went through "rebuilding" on the way.
        assert "rebuilding" in [c.status for c in h.cards("b1")]
        # No new approval request; Approve now works on the new preview.
        assert len(h.approvals) == 1
        await h.press("b1", "approve")
        assert len(h.approved) == 1

    @pytest.mark.asyncio
    async def test_a_running_named_preview_is_left_alone(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        before = (len(h.offer_rows("b1")), len(h.dispatch.calls))
        await h.rebuild("b1")
        assert (len(h.offer_rows("b1")), len(h.dispatch.calls)) == before
        assert h.card("b1").status == "ready"

    @pytest.mark.asyncio
    async def test_a_decided_card_refuses_rebuild(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.press("b1", "reject")
        calls = len(h.dispatch.calls)
        assert await h.offers.rebuild_preview(build_id="b1") == "decided"
        # The press itself never reaches the rebuild once a decision is there.
        await h.rebuild("b1")
        assert len(h.dispatch.calls) == calls
        assert h.card("b1").status == "decided"

    @pytest.mark.asyncio
    async def test_three_rebuilds_then_the_card_says_so(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        for n in range(1, 4):
            await h.expire()
            await h.rebuild("b1")
            assert h.offer_rows("b1")[-1][merge_card.REBUILDS_KEY] == n
        await h.expire()
        card = h.card("b1")
        assert card.status == "ended" and card.rebuild_offered is False
        assert card.status_text.endswith(
            "Rebuilt three times; reject and run the feature again."
        )
        assert await h.offers.rebuild_preview(build_id="b1") == "capped"

    @pytest.mark.asyncio
    async def test_two_rebuild_presses_at_once_prepare_once(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        before = h.dispatch.legs().count("preview_up")
        await asyncio.gather(
            h.offers.rebuild_preview(build_id="b1"),
            h.offers.rebuild_preview(build_id="b1"),
        )
        await h.drain()
        assert h.dispatch.legs().count("preview_up") == before + 1
        assert len(h.offer_rows("b1")) == 2
        assert h.card("b1").status == "ready"

    @pytest.mark.asyncio
    async def test_another_defer_is_still_refused(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        await h.press("b1", "defer", notes="later please")
        assert len(h.offer_rows("b1")) == 1 and h.decisions("b1") == []


class _SlowMergeCheck:
    """Holds a rebuild inside its merge-check read until released."""

    def __init__(self, h: Harness) -> None:
        self.entered = asyncio.Event()
        self.go = asyncio.Event()

        async def read(**_kwargs: Any) -> None:
            self.entered.set()
            await self.go.wait()
            return None

        h.offers._read_the_merge_check = read  # type: ignore[method-assign]


class TestDuringTheMergeCheckRead:
    @pytest.mark.asyncio
    async def test_a_sweep_shows_rebuilding_and_ends_nothing(self, h: Harness) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        slow = _SlowMergeCheck(h)
        await h.rebuild("b1", drain=False)
        await slow.entered.wait()
        rebuilt = h.lease_of("b1")
        assert rebuilt.state == "ready"
        await h.offers.sweep_cards(now=h.clock.now)
        assert h.card("b1").status == "rebuilding"
        assert h.lease_of("b1").state == "ready"  # not taken for unfinished
        slow.go.set()
        await h.drain()
        assert h.card("b1").status == "ready"

    @pytest.mark.asyncio
    async def test_an_approve_is_refused_and_leaves_the_rebuild_standing(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        slow = _SlowMergeCheck(h)
        await h.rebuild("b1", drain=False)
        await slow.entered.wait()
        rebuilt = h.lease_of("b1")
        await h.press("b1", "approve", drain=False)
        assert h.decisions("b1") == [] and h.approved == []
        assert h.store.get(rebuilt.lease_id).state == "ready"
        slow.go.set()
        await h.drain()
        assert h.card("b1").status == "ready"
        await h.press("b1", "approve")
        assert len(h.approved) == 1

    @pytest.mark.asyncio
    async def test_reject_during_the_read_writes_no_amended_row(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        slow = _SlowMergeCheck(h)
        await h.rebuild("b1", drain=False)
        await slow.entered.wait()
        await h.press("b1", "reject", drain=False)
        slow.go.set()
        await h.drain()
        assert len(h.offer_rows("b1")) == 1
        assert [d.status for d in h.decisions("b1")] == ["SKIPPED"]
        assert _pending_offers(h.cx) == []
        assert h.store.live_for_build("b1") == []
        assert h.card("b1").status == "decided"

    @pytest.mark.asyncio
    async def test_a_restart_mid_read_leaves_a_stopped_card_and_rebuild_works(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        slow = _SlowMergeCheck(h)
        await h.rebuild("b1", drain=False)
        await slow.entered.wait()
        for task in list(h.consumer._tasks):
            task.cancel()
        await asyncio.gather(*h.consumer._tasks, return_exceptions=True)
        assert h.lease_of("b1").state == "ready"  # left behind by the "crash"

        # The coordinator starts again: a fresh offer service and press.
        h.offers = h.new_offer_service()
        h.consumer = h.new_consumer(h.offers)
        # Before the start-up sweep: a ready preview no committed offer names
        # is never shown as ready, even with no preparation running here.
        await h.offers.describe_card("b1")
        assert h.card("b1").status == "rebuilding"
        await h.offers.on_boot()
        await h.drain()
        card = h.card("b1")
        assert (card.status, card.rebuild_offered) == ("ended", True)
        assert card.status_text == "Preview stopped: the rebuild did not finish."
        assert h.lease_of("b1").state == "ended"

        await h.rebuild("b1")
        assert h.card("b1").status == "ready"
        # The unfinished rebuild did not count.
        assert h.offer_rows("b1")[-1][merge_card.REBUILDS_KEY] == 1

    @pytest.mark.asyncio
    async def test_a_crash_after_the_amended_row_is_repaired_by_the_sweep(
        self, h: Harness
    ) -> None:
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        h.wire.failures = 100  # nothing the rebuild sends gets out
        await h.rebuild("b1")
        assert len(h.offer_rows("b1")) == 2
        assert h.card("b1").status == "ended"  # Slack still shows the old truth
        h.wire.failures = 0
        await h.offers.sweep_cards(now=h.clock.now)
        card = h.card("b1")
        assert card.status == "ready" and card.revision == h.revision("b1")


# ---------------------------------------------------------------------------
# Part 3: the two slots
# ---------------------------------------------------------------------------


class TestSlots:
    @pytest.mark.asyncio
    async def test_both_slots_busy_the_card_goes_out_at_once(self, h: Harness) -> None:
        await h.offer("bA", "FEAT-AAAA")
        await h.offer("bB", "FEAT-BBBB")
        await h.offer("bC", "FEAT-CCCC")
        assert h.clock.sleeps == []  # nothing waited
        card = h.card("bC")
        assert (card.status, card.rebuild_offered) == ("failed", True)
        assert card.status_text == (
            "Preview not started: both preview slots are in use by FEAT-AAAA and "
            "FEAT-BBBB. Press Rebuild preview when you want to look."
        )
        assert h.paused("bC").rationale.endswith(card.status_text)

    @pytest.mark.asyncio
    async def test_rebuild_takes_the_ready_slot_not_the_preparing_one(
        self, h: Harness
    ) -> None:
        await h.offer("bA", "FEAT-AAAA")
        h.hold_a_slot(8951, build_id="bB", feature_id="FEAT-BBBB", state="preparing")
        await h.offer("bC", "FEAT-CCCC")
        assert h.card("bC").status == "failed"
        a_slot = h.named_lease("bA").port
        await h.rebuild("bC")
        assert h.card("bC").status == "ready"
        assert h.named_lease("bC").port == a_slot
        a_card = h.card("bA")
        assert (a_card.status, a_card.rebuild_offered) == ("ended", True)
        assert a_card.status_text == "Preview stopped to make room for FEAT-CCCC."
        assert h.store.get("pvhold8951").state == "preparing"

    @pytest.mark.asyncio
    async def test_rebuild_takes_the_unanswered_slot_not_the_decided_one(
        self, h: Harness
    ) -> None:
        await h.offer("bA", "FEAT-AAAA")
        await h.offer("bB", "FEAT-BBBB")
        await h.press("bA", "approve")  # committed before any slot is taken
        await h.offer("bC", "FEAT-CCCC")
        await h.rebuild("bC")
        assert h.card("bC").status == "ready"
        assert h.named_lease("bA").state == "ready"  # the approved one is kept
        assert h.named_lease("bB").state == "ended"
        assert h.card("bB").status_text == "Preview stopped to make room for FEAT-CCCC."

    @pytest.mark.asyncio
    async def test_both_preparing_the_card_waits_and_the_sweep_retries(
        self, h: Harness
    ) -> None:
        h.hold_a_slot(8950, build_id="bA", feature_id="FEAT-AAAA", state="preparing")
        h.hold_a_slot(8951, build_id="bB", feature_id="FEAT-BBBB", state="preparing")
        await h.offer("bC", "FEAT-CCCC")
        await h.rebuild("bC")
        card = h.card("bC")
        assert card.status == "rebuilding" and card.rebuild_offered is False
        assert (
            card.status_text
            == "Waiting for a preview slot; it will start when one frees."
        )
        assert h.offer_rows("bC")[-1].get(merge_card.REBUILD_WANTED_KEY)
        # Nothing frees: the sweep tries and the card keeps waiting.
        await h.offers.sweep_cards(now=h.clock.now)
        await h.drain()
        assert h.card("bC").status == "rebuilding"
        # A slot frees: the next sweep starts the rebuild.
        h.store.update(
            "pvhold8950",
            now=h.clock.now,
            state="ended",
            ended_at=h.clock.now.isoformat(),
        )
        await h.offers.sweep_cards(now=h.clock.now)
        await h.drain()
        assert h.card("bC").status == "ready"
        assert (
            h.offer_rows("bC")[-1][merge_card.REBUILDS_KEY] == 1
        )  # waiting did not count
        assert merge_card.REBUILD_WANTED_KEY not in h.offer_rows("bC")[-1]

    @pytest.mark.asyncio
    async def test_an_approve_during_the_teardown_is_refused(self, h: Harness) -> None:
        await h.offer("bA", "FEAT-AAAA")
        h.hold_a_slot(8951, build_id="bB", feature_id="FEAT-BBBB", state="preparing")
        await h.offer("bC", "FEAT-CCCC")
        tearing = asyncio.Event()
        go = asyncio.Event()
        removed = h.venue.remove_project

        async def slow_remove(lease_id: str) -> bool:
            tearing.set()
            await go.wait()
            return await removed(lease_id)

        h.venue.remove_project = slow_remove  # type: ignore[method-assign]
        await h.rebuild("bC", drain=False)
        await tearing.wait()
        assert h.named_lease("bA").state == "ending"
        await h.press("bA", "approve", drain=False)
        assert h.decisions("bA") == [] and h.approved == []
        go.set()
        await h.drain()
        assert h.card("bC").status == "ready"
        assert h.card("bA").status == "ended"
        assert h.card("bA").status_text == "Preview stopped to make room for FEAT-CCCC."

    @pytest.mark.asyncio
    async def test_an_approve_that_commits_first_keeps_its_preview(
        self, h: Harness
    ) -> None:
        await h.offer("bA", "FEAT-AAAA")
        h.hold_a_slot(8951, build_id="bB", feature_id="FEAT-BBBB", state="preparing")
        await h.offer("bC", "FEAT-CCCC")
        await h.press("bA", "approve")
        await h.rebuild("bC")
        assert h.named_lease("bA").state == "ready"
        assert h.card("bC").status == "rebuilding"  # waiting: nothing eligible
        assert h.card("bA").status == "decided"


# ---------------------------------------------------------------------------
# Small related pieces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unfinished_first_offer_s_preview_is_ended_by_the_sweep(
    h: Harness,
) -> None:
    from forge.deploy.review_preview import PreviewRequest

    h.add_build("b1", "FEAT-AB12")
    await h.service.prepare(
        PreviewRequest(
            project=PROJECT,
            feature_id="FEAT-AB12",
            build_id="b1",
            branch="autobuild/FEAT-AB12",
            candidate_sha=SHA_CAND,
            candidate_tree=TREE_CAND,
            expect_main_sha=SHA_MAIN,
            target_branch="main",
            preview_base_sha=SHA_FETCHED,
        )
    )
    assert h.lease_of("b1").state == "ready"
    assert await h.offers.end_unfinished_previews() == 1
    lease = h.lease_of("b1")
    assert (lease.state, lease.reason) == ("ended", "its merge card was never offered")


@pytest.mark.asyncio
async def test_max_app_ttl_minutes_caps_the_preview_s_life(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.service._max_app_ttl = timedelta(minutes=15)
    await h.offer("b1", "FEAT-AB12")
    lease = h.lease_of("b1")
    assert lease.app_expires_at == (START + timedelta(minutes=15)).isoformat()
    evidence = h.offer_rows("b1")[0]["review_evidence"]
    assert evidence["app_expires_at"].startswith("2026-10-12T09:15:00")
    h.clock.now += timedelta(minutes=16)
    await h.service.sweep(now=h.clock.now)
    await h.drain()
    assert h.card("b1").status_text == "Preview expired at 09:15 UTC."


def test_the_setting_is_optional_and_whole_minutes() -> None:
    from forge.config.models import ReviewPreviewConfig

    assert ReviewPreviewConfig().max_app_ttl_minutes is None
    assert ReviewPreviewConfig(max_app_ttl_minutes=15).max_app_ttl_minutes == 15
    with pytest.raises(ValueError):
        ReviewPreviewConfig(max_app_ttl_minutes=0)


def test_the_queue_counts_a_preparing_preview_as_a_card_on_its_way(h: Harness) -> None:
    h.add_build("b1", "FEAT-AB12")
    h.cx.execute(
        "UPDATE builds SET completed_at = ? WHERE build_id = 'b1'", (START.isoformat(),)
    )
    later = START + timedelta(seconds=MERGE_CARD_GRACE_SECONDS + 60)
    assert work_landing(h.cx, "corr-b1", now=later) == NO_MERGE_CARD
    h.hold_a_slot(8950, build_id="b1", feature_id="FEAT-AB12", state="preparing")
    assert work_landing(h.cx, "corr-b1", now=later) == NOT_LANDED_YET


# ---------------------------------------------------------------------------
# The coach's review (16 October 2026): Reject during a rebuild, and expiry
# ---------------------------------------------------------------------------


def _record_states(h: Harness) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    original = h.store.update

    def update(lease_id: str, *, now: Any, **fields: Any) -> bool:
        applied = original(lease_id, now=now, **fields)
        if applied and "state" in fields:
            seen.append((lease_id, fields["state"]))
        return applied

    h.store.update = update  # type: ignore[method-assign]
    return seen


def _hold_preview_up(
    h: Harness,
) -> tuple[asyncio.Event, asyncio.Event, list[tuple[str, str]]]:
    entered, go = asyncio.Event(), asyncio.Event()
    calls: list[tuple[str, str]] = []
    original = h.service._dispatch

    async def held(**kwargs: Any) -> Any:
        calls.append((kwargs["leg"], str(kwargs.get("build_id"))))
        if kwargs["leg"] == "preview_up" and not entered.is_set():
            entered.set()
            await go.wait()
        return await original(**kwargs)

    h.service._dispatch = held
    return entered, go, calls


class TestRejectDuringARebuild:
    @pytest.mark.asyncio
    async def test_an_ended_lease_never_comes_back(self, h: Harness) -> None:
        """S1a: Reject while the rebuild's preview is being started."""
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        entered, go, calls = _hold_preview_up(h)
        seen = _record_states(h)
        await h.rebuild("b1", drain=False)
        await asyncio.wait_for(entered.wait(), 30)
        lease = h.lease_of("b1")
        assert lease.state == "preparing"
        await h.press("b1", "reject", drain=False)
        # Released, but it keeps its slot until its own preparation stops.
        assert h.store.get(lease.lease_id).state == "ending"
        go.set()
        await asyncio.wait_for(h.drain(), 30)
        mine = [state for lease_id, state in seen if lease_id == lease.lease_id]
        assert "ready" not in mine, mine
        assert h.store.get(lease.lease_id).state == "ended"
        assert h.store.get(lease.lease_id).reason == "rejected"
        assert ("preview_journey", "b1") not in calls
        assert lease.lease_id in h.venue.named("remove_project")
        # An ended lease stays ended, whatever writes it.
        assert not h.store.update(lease.lease_id, now=h.clock.now, state="ready")
        assert h.store.get(lease.lease_id).state == "ended"
        assert h.card("b1").status == "decided"

    @pytest.mark.asyncio
    async def test_no_other_card_gets_the_slot_while_it_stops(self, h: Harness) -> None:
        """S1b: the rejected rebuild's slot is not handed to another card
        while its preparation may still be starting containers."""
        await h.offer("b1", "FEAT-AB12")
        await h.expire()
        await h.offer("b0", "FEAT-0000")  # holds the other slot
        entered, go, calls = _hold_preview_up(h)
        await h.rebuild("b1", drain=False)
        await asyncio.wait_for(entered.wait(), 30)
        b1_lease = h.lease_of("b1")
        await h.press("b1", "reject", drain=False)
        h.add_build("b2", "FEAT-CD34")
        await asyncio.wait_for(
            h.offers.offer(
                build_id="b2", feature_id="FEAT-CD34", card_words=lambda _b, _m: "w"
            ),
            30,
        )
        await asyncio.sleep(0)
        assert h.card("b2").status == "failed"  # both slots still held
        assert h.lease_of("b2").port is None
        go.set()
        await asyncio.wait_for(h.drain(), 30)
        assert ("preview_journey", "b1") not in calls
        assert h.store.get(b1_lease.lease_id).state == "ended"
        # Once it has stopped, the slot is free for b2's own Rebuild.
        await h.rebuild("b2")
        assert h.card("b2").status == "ready"
        assert h.named_lease("b2").port == b1_lease.port
        assert h.named_lease("b0").state == "ready"

    @pytest.mark.asyncio
    async def test_a_rejected_card_never_takes_another_card_s_slot(
        self, h: Harness
    ) -> None:
        """S2: Reject while the rebuild is joining; it must not then evict."""
        await h.offer("bA", "FEAT-AAAA")
        await h.offer("bB", "FEAT-BBBB")
        await h.offer("bC", "FEAT-CCCC")
        entered, go = asyncio.Event(), asyncio.Event()
        original = h.venue.lay_out

        async def held(lease_id: str, *, onto: str, candidate: str) -> Any:
            entered.set()
            await go.wait()
            return await original(lease_id, onto=onto, candidate=candidate)

        h.venue.lay_out = held  # type: ignore[method-assign]
        await h.rebuild("bC", drain=False)
        await asyncio.wait_for(entered.wait(), 30)
        await h.press("bC", "reject", drain=False)
        go.set()
        await asyncio.wait_for(h.drain(), 30)
        assert h.named_lease("bA").state == "ready", h.named_lease("bA").reason
        assert h.named_lease("bB").state == "ready", h.named_lease("bB").reason
        assert h.card("bC").status == "decided"
        assert len(h.offer_rows("bC")) == 1


class TestExpiryShowsAtOnce:
    @pytest.mark.asyncio
    async def test_past_its_time_the_card_has_no_approve_before_the_sweep(
        self, h: Harness
    ) -> None:
        """S3: between expiry and the next sweep the card no longer says ready."""
        await h.offer("b1", "FEAT-AB12")
        h.clock.now += timedelta(hours=25)
        await h.offers.describe_card("b1")
        card = h.card("b1")
        expiry = h.lease_of("b1").app_expires_at[11:16]
        assert (card.status, card.rebuild_offered) == ("ended", True)
        assert card.status_text == f"Preview expired at {expiry} UTC."
        assert h.paused("b1").review_evidence.app_url is None
        # An Approve pressed now is refused, and the card says the same thing,
        # without telling anyone to reject.
        await h.press("b1", "approve")
        card = h.card("b1")
        assert card.status == "ended" and card.rebuild_offered
        assert card.status_text == f"Preview expired at {expiry} UTC."
        assert "reject" not in card.status_text.lower()

    @pytest.mark.asyncio
    async def test_the_preview_is_ended_and_described_at_its_expiry(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        slept: list[float] = []

        async def sleep_until(seconds: float) -> None:
            slept.append(seconds)
            h.clock.now += timedelta(seconds=seconds)

        h.expiry_sleep = sleep_until
        h.offers = h.new_offer_service()
        h.consumer = h.new_consumer(h.offers)
        await h.offer("b1", "FEAT-AB12")
        for _ in range(5):
            await asyncio.sleep(0)
        await h.drain()
        assert slept == [24 * 3600.0]
        lease = h.lease_of("b1")
        assert (lease.state, lease.reason) == ("ended", "the preview's time was up")
        assert h.card("b1").status == "ended"
        assert h.offers._expiry_timers == {}
