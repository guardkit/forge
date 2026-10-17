"""Finishing a mission: one merge card for the whole branch.

Missions design, 17 October 2026, section 7, and its tests in section 10
("Finishing"):

- READY with nothing landed is NOT_MERGED, one thread line, no card;
- the tip is checked against the last confirmed landing; anything else is
  NOT_MERGED;
- with the routing gate on, a missing receipt is NOT_MERGED and never
  retried; with it there, the final merge runs under that receipt;
- the mission-merge build row, its offer, counted as ONE card;
- the frozen source in a clean clone with no local mission branch: ``T``
  checked against the last landing, the local branch made, a stale one put
  back at ``T``, and the pin kept across a restart;
- the final merge after the last feature already landed under its ordinary
  integration names, with no collision;
- the card words and the risky-files rule, a test per word;
- the answer: MERGING then MERGED, a declined card NOT_MERGED.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import (
    SOURCE_BRANCH_PREFIX,
    list_tree_at_commit,
    make_source_branch_sync,
)
from forge.lifecycle.mission_builds import mission_merge_build_id, mission_of_build
from forge.lifecycle.missions import (
    MISSION_ROLE_MERGE,
    Mission,
    MissionAttempt,
    MissionFeature,
    MissionStore,
    attempt_correlation_id,
)
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline import mission_finish
from forge.pipeline.merge_executor import (
    MERGE_REPORT_STAGE_LABEL,
    MERGE_REPORT_TARGET_IDENTIFIER,
    MergeApprovalConsumer,
    _decision_entry,
)
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
)
from forge.pipeline.mission_finish import (
    RISKY_WORDS,
    MissionFinishDeps,
    finish_ready_mission,
    install_finisher,
    mission_card_words,
    risky_files,
    run_finishing_step,
)
from forge.planning.work_queue_loop import count_in_flight, unanswered_merge_cards
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    REPO,
    _drain,
    _envelope,
    _git,
    _receipts_env,
    pool,
    repo_root,
)
from tests.forge.pipeline.test_mission_landing import (  # noqa: F401 - fixtures
    B_BUILD,
    C_BUILD,
    MISSION_BRANCH,
    MISSION_CORRELATION,
    MISSION_ID,
    JoinsTheBranch,
    a_mission_feature_build,
    a_running_mission,
    config_on,
    press_deps,
    remote_tip,
    the_mission_branch_on_the_remote,
)
from tests.forge.recorded_fetch_address import origin_of


# ---------------------------------------------------------------------------
# The risky-files rule: one function, a test per word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", RISKY_WORDS)
def test_a_folder_named_for_the_word_is_risky(word: str) -> None:
    assert risky_files([f"internal/{word}/handler.go", "README.md"]) == [
        f"internal/{word}/handler.go"
    ]


@pytest.mark.parametrize("word", RISKY_WORDS)
def test_a_file_name_containing_the_word_is_risky(word: str) -> None:
    path = f"web/src/My{word.capitalize()}Panel.tsx"
    assert risky_files([path, "web/src/Notes.tsx"]) == [path]


def test_every_sql_file_is_risky_whatever_it_is_called() -> None:
    assert risky_files(["db/0004_tags.sql", "db/notes.go", "x/Y.SQL"]) == [
        "db/0004_tags.sql",
        "x/Y.SQL",
    ]


def test_the_words_are_the_designs_eleven() -> None:
    assert RISKY_WORDS == (
        "auth",
        "login",
        "session",
        "security",
        "secret",
        "token",
        "password",
        "keycloak",
        "oauth",
        "permission",
        "migration",
    )


def test_nothing_risky_is_an_empty_list() -> None:
    assert risky_files(["README.md", "web/src/TagCloud.tsx", ""]) == []


# ---------------------------------------------------------------------------
# The card words
# ---------------------------------------------------------------------------


def _mission(**over: Any) -> Mission:
    fields: dict[str, Any] = dict(
        id="m-3fa21c",
        title="Tags for notes",
        target_repo=REPO,
        branch="mission/m-3fa21c",
        status="READY",
        correlation_id="corr-tags",
        originating_user="rich",
        created_at="2026-10-17T00:00:00+00:00",
    )
    fields.update(over)
    return Mission(**fields)


def _attempt(key: str, status: str, reason: str | None = None) -> MissionAttempt:
    return MissionAttempt(
        mission_id="m-3fa21c",
        key=key,
        attempt=1,
        correlation_id=f"corr-tags-{key}-a1",
        status=status,
        reason=reason,
    )


FOUR = [
    MissionFeature(key="A", sentence="store tags"),
    MissionFeature(key="B", sentence="add/remove tags", needs=("A",)),
    MissionFeature(key="C", sentence="filter by tag", needs=("A",)),
    MissionFeature(key="D", sentence="tag cloud", needs=("B", "C")),
]


def test_the_card_says_what_landed_what_did_not_and_what_to_look_at() -> None:
    words = mission_card_words(
        mission=_mission(),
        features=FOUR,
        latest={
            "A": _attempt("A", "LANDED"),
            "B": _attempt("B", "LANDED"),
            "C": _attempt("C", "LANDED"),
            "D": _attempt("D", "FAILED", "its check failed: the cloud was empty. More."),
        },
        risky=["migrations/0004_tags.sql", "internal/auth/session.go"],
    )

    assert words.splitlines() == [
        'Mission "Tags for notes" — 3 of 4 features landed on mission/m-3fa21c',
        "Landed:  A store tags · B add/remove tags · C filter by tag",
        'Not landed:  D tag cloud — "its check failed: the cloud was empty."',
        "Look at these first (sign-in, security, database changes):",
        "  migrations/0004_tags.sql · internal/auth/session.go",
        "Approve = merge the mission into main and deploy. Reject = nothing "
        "reaches main; the branch is kept.",
    ]


def test_a_card_with_nothing_risky_and_everything_landed() -> None:
    words = mission_card_words(
        mission=_mission(),
        features=FOUR[:2],
        latest={"A": _attempt("A", "LANDED"), "B": _attempt("B", "LANDED")},
        risky=[],
    )
    lines = words.splitlines()
    assert lines[0].endswith("2 of 2 features landed on mission/m-3fa21c")
    assert not any(line.startswith("Not landed") for line in lines)
    assert (
        "Look at these first: no changed file touches sign-in, security or the "
        "database." in lines
    )


def test_a_card_whose_files_could_not_be_listed_says_so() -> None:
    words = mission_card_words(
        mission=_mission(),
        features=FOUR[:1],
        latest={"A": _attempt("A", "LANDED")},
        risky=None,
        risky_unread="the copy does not have the commit",
    )
    assert (
        "Look at these first: the changed files could not be listed (the copy "
        "does not have the commit)." in words
    )


# ---------------------------------------------------------------------------
# The finishing step against a real repository
# ---------------------------------------------------------------------------


class _Thread:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def __call__(self, mission: Mission, message: str) -> None:
        self.lines.append((mission.id, message))


class _Router:
    """The routing gate's committed receipts, by correlation id."""

    def __init__(self, committed: set[str]) -> None:
        self.committed = committed
        self.asked: list[str] = []

    def require_committed_success(self, correlation_id: str) -> Any:
        self.asked.append(correlation_id)
        if correlation_id not in self.committed:
            raise RuntimeError(f"no committed receipt for {correlation_id}")
        return SimpleNamespace(to_wire=lambda: {"feature_routing_id": correlation_id})


class _Trees:
    async def list_tree_at_commit(self, repo_path: str, commit: str) -> Any:
        return await list_tree_at_commit(repo_path, commit)


class _Wire:
    def __init__(self) -> None:
        self.approvals: list[Any] = []
        self.cards: list[Any] = []

    async def raw_publish(self, subject: str, body: bytes) -> None:
        self.approvals.append((subject, body))

    async def publish_build_paused(self, payload: Any) -> None:
        self.cards.append(payload)


def _offer_service(config: ForgeConfig, pool: SqliteLifecyclePersistence) -> tuple[MergeOfferService, _Wire]:  # noqa: F811
    wire = _Wire()
    return (
        MergeOfferService(
            config=config,
            pool=pool,
            pipeline_publisher=SimpleNamespace(publish_build_paused=wire.publish_build_paused),
            raw_publish=wire.raw_publish,
        ),
        wire,
    )


def _finish_deps(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    *,
    offer: Any,
    router: _Router | None = None,
) -> tuple[MissionFinishDeps, _Thread]:
    from forge.deploy.candidate_tree import InContainerCandidateGit

    thread = _Thread()
    addresses = dict(config.planning.fetch_addresses or {})
    return (
        MissionFinishDeps(
            pool=pool,
            config=config,
            offer=offer,
            git_for=lambda repo, root: InContainerCandidateGit(
                root, fetch_address=addresses.get(repo), repo=repo
            ),
            notify=thread,
            tree_reader_for=lambda _repo, _root: _Trees(),
            feature_routing_gate=router,
            feature_routing_required=router is not None,
        ),
        thread,
    )


async def _land(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    *,
    keys: tuple[str, ...] = ("B",),
    router: _Router | None = None,
) -> MissionStore:
    """B (and C) land on the mission branch for real; the mission is READY."""
    store = MissionStore(pool.connection)
    builds = {"B": (B_BUILD, "FEAT-MX1"), "C": (C_BUILD, "FEAT-MX2")}
    deps, *_ = press_deps(config, pool, repo_root, routing_gate=router)
    consumer = MergeApprovalConsumer(deps)
    for key in keys:
        build_id, feature_id = builds[key]
        a_mission_feature_build(pool, build_id=build_id, feature_id=feature_id, key=key)
        outcome = await consumer.land_mission_feature(build_id)
        assert outcome is not None and outcome.status == "PASSED", outcome
        store.mark_landed(attempt_correlation_id(MISSION_CORRELATION, key, 1))
    for attempt in store.attempts(MISSION_ID):
        if attempt.status in ("WAITING", "ADMITTED"):
            store.mark_failed(attempt.correlation_id, "its check failed: two of eight checks went red")
    assert store.set_status(MISSION_ID, "READY", expected=("RUNNING",))
    return store


def _offers(pool: SqliteLifecyclePersistence, build_id: str) -> list[Any]:  # noqa: F811
    return [
        s for s in pool.read_stages(build_id) if s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
    ]


def _local(repo: Path, ref: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() or None


class TestNothingLanded:
    @pytest.mark.asyncio
    async def test_no_card_one_line_and_not_merged(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        store = a_running_mission(pool, keys=("B", "C"))
        for attempt in store.attempts(MISSION_ID):
            store.mark_failed(attempt.correlation_id, "the build failed")
        store.set_status(MISSION_ID, "READY", expected=("RUNNING",))
        offered: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offered.append(kwargs)
            return True

        deps, thread = _finish_deps(config_on, pool, offer=offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        assert await finish_ready_mission(deps, MISSION_ID) == "nothing to do"

        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED" and mission.closed_reason == "nothing landed"
        assert offered == []
        assert mission_merge_build_id(pool, MISSION_ID) is None
        assert len(thread.lines) == 1
        assert "none of its 2 features landed" in thread.lines[0][1]


class TestTheTipIsChecked:
    @pytest.mark.asyncio
    async def test_a_branch_something_else_moved_is_not_offered(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B", "C"))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        # Somebody else pushes onto the mission branch after the last landing.
        _git(repo_root, "checkout", "-q", "-b", "elsewhere", f"origin/{MISSION_BRANCH}")
        (repo_root / "stray.txt").write_text("not the factory's\n", encoding="utf-8")
        _git(repo_root, "add", "stray.txt")
        _git(repo_root, "commit", "-q", "-m", "stray")
        _git(repo_root, "push", "-q", "origin", f"HEAD:refs/heads/{MISSION_BRANCH}")
        _git(repo_root, "checkout", "-q", "main")
        offered: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offered.append(kwargs)
            return True

        deps, thread = _finish_deps(config_on, pool, offer=offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert "something else changed the branch" in str(mission.closed_reason)
        assert mission.final_commit is None
        assert offered == [] and mission_merge_build_id(pool, MISSION_ID) is None
        assert len(thread.lines) == 1


class TestTheRoutingReceipt:
    @pytest.mark.asyncio
    async def test_a_missing_receipt_is_not_merged_and_never_retried(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B", "C"))
        the_mission_branch_on_the_remote(repo_root)
        attempts = {attempt_correlation_id(MISSION_CORRELATION, "B", 1)}
        store = await _land(config_on, pool, repo_root, router=_Router(attempts))
        router = _Router(attempts)  # the mission's own receipt is missing
        offered: list[Any] = []

        async def offer(**kwargs: Any) -> bool:
            offered.append(kwargs)
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer, router=router)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        assert await finish_ready_mission(deps, MISSION_ID) == "nothing to do"
        assert router.asked == [MISSION_CORRELATION]
        assert store.get(MISSION_ID).status == "NOT_MERGED"
        assert "routing" in str(store.get(MISSION_ID).closed_reason)
        assert offered == [] and mission_merge_build_id(pool, MISSION_ID) is None


class TestTheMissionsMergeCard:
    @pytest.mark.asyncio
    async def test_the_merge_build_its_frozen_source_and_one_card(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        main_tip = _git(repo_root, "rev-parse", "main")
        a_running_mission(pool, keys=("B", "C"), base_commit=main_tip)
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        landing = store.get(MISSION_ID)
        service, wire = _offer_service(config_on, pool)
        deps, thread = _finish_deps(config_on, pool, offer=service.offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "offered"

        mission = store.get(MISSION_ID)
        tip = remote_tip(repo_root, MISSION_BRANCH)
        assert mission.status == "READY" and mission.final_commit == tip
        build_id = mission_merge_build_id(pool, MISSION_ID)
        assert build_id is not None and build_id.startswith("build-FEAT-MX1-")
        row = pool.get_build_row(build_id)
        assert row.mission_id == MISSION_ID and row.mission_role == MISSION_ROLE_MERGE
        assert row.correlation_id == MISSION_CORRELATION
        assert row.branch == MISSION_BRANCH
        assert row.merge_branch == f"{SOURCE_BRANCH_PREFIX}{build_id}"
        assert row.status.value == "COMPLETE" and row.triggered_by == "forge-internal"
        assert row.start_commit == main_tip and row.target_branch == "main"
        assert row.feature_yaml_path == pool.get_build_row(B_BUILD).feature_yaml_path
        # THE FROZEN SOURCE, local, at T.
        assert _local(repo_root, f"refs/heads/{row.merge_branch}") == tip
        # ONE CARD, pinned at T, with the mission's own words.
        offers = _offers(pool, build_id)
        assert len(offers) == 1
        details = offers[0].details[MERGE_OFFER_DETAILS_KEY]
        assert details["candidate_sha"] == tip
        assert details["branch"] == row.merge_branch
        assert details["mission"]["landed"] == ["B"]
        assert details["mission"]["not_landed"] == ["C"]
        assert len(wire.approvals) == 1 and len(wire.cards) == 1
        words = wire.cards[0].rationale
        assert words.startswith('Mission "Tags for notes" — 1 of 2 features landed')
        assert 'C feature C of the mission — "its check failed' in words
        # COUNTED AS ONE CARD, under the mission's own correlation id.
        cards = unanswered_merge_cards(pool.connection)
        assert [(c.build_id, c.correlation_id) for c in cards] == [
            (build_id, MISSION_CORRELATION)
        ]
        assert count_in_flight(pool.connection, merge_offer_hold_seconds=3600) == 1
        assert thread.lines == []
        assert landing.final_commit is None

        # Asked again (every tick), nothing more is offered.
        assert await finish_ready_mission(deps, MISSION_ID) == "waiting for the card"
        assert len(_offers(pool, build_id)) == 1 and len(wire.approvals) == 1

    @pytest.mark.asyncio
    async def test_a_stale_source_is_put_back_and_the_pin_survives_a_restart(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        service, _wire = _offer_service(config_on, pool)
        deps, _thread = _finish_deps(config_on, pool, offer=service.offer)
        await finish_ready_mission(deps, MISSION_ID)
        build_id = mission_merge_build_id(pool, MISSION_ID)
        source = f"refs/heads/{SOURCE_BRANCH_PREFIX}{build_id}"
        tip = store.get(MISSION_ID).final_commit

        # A stale branch of the same name, pointing somewhere else.
        _git(repo_root, "branch", "-f", f"{SOURCE_BRANCH_PREFIX}{build_id}", "main")
        assert _local(repo_root, source) != tip
        await finish_ready_mission(deps, MISSION_ID)
        assert _local(repo_root, source) == tip

        # A restart into a clone that has lost it: it is made again at T.
        _git(repo_root, "branch", "-D", f"{SOURCE_BRANCH_PREFIX}{build_id}")
        await finish_ready_mission(deps, MISSION_ID)
        assert _local(repo_root, source) == tip
        offer = _offers(pool, build_id)[-1].details[MERGE_OFFER_DETAILS_KEY]
        assert offer["candidate_sha"] == tip

    @pytest.mark.asyncio
    async def test_a_clean_clone_with_no_local_mission_branch(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        # The clone the merge will run in is made BEFORE anything landed, so it
        # has neither the mission branch nor the landed commits.
        clean = tmp_path / "clean"
        subprocess.run(
            ["git", "clone", "-q", origin_of(repo_root), str(clean)],
            check=True,
            capture_output=True,
        )
        store = await _land(config_on, pool, repo_root)
        tip = remote_tip(repo_root, MISSION_BRANCH)
        assert _local(clean, f"{tip}^{{commit}}") is None
        assert _local(clean, f"refs/heads/{MISSION_BRANCH}") is None
        clean_config = config_on.model_copy(deep=True)
        clean_config.planning.target_repo_paths[REPO] = str(clean)
        service, _wire = _offer_service(clean_config, pool)
        deps, _thread = _finish_deps(clean_config, pool, offer=service.offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "offered"

        build_id = mission_merge_build_id(pool, MISSION_ID)
        assert store.get(MISSION_ID).final_commit == tip
        assert _local(clean, f"refs/heads/{SOURCE_BRANCH_PREFIX}{build_id}") == tip
        # Still no local mission branch: the frozen source is the only one.
        assert _local(clean, f"refs/heads/{MISSION_BRANCH}") is None
        assert len(_offers(pool, build_id)) == 1


class TestTheAnswer:
    async def _offered(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> tuple[MissionStore, MissionFinishDeps, _Thread, str]:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config, pool, repo_root)
        service, _wire = _offer_service(config, pool)
        deps, thread = _finish_deps(config, pool, offer=service.offer)
        await finish_ready_mission(deps, MISSION_ID)
        return store, deps, thread, str(mission_merge_build_id(pool, MISSION_ID))

    def _decide(self, pool: SqliteLifecyclePersistence, build_id: str, decision: str) -> None:  # noqa: F811
        pool.record_stage(
            _decision_entry(
                build_id=build_id,
                decision=decision,
                decided_by="rich",
                request_id=f"merge-{build_id}",
                now=datetime.now(UTC),
                attempt_id="a" * 32 if decision == "approve" else None,
            )
        )

    def _published(self, pool: SqliteLifecyclePersistence, build_id: str, result: str) -> None:  # noqa: F811
        from forge.pipeline.publication_record import PublicationRecordStore

        records = PublicationRecordStore(pool.connection)
        now = datetime.now(UTC)
        grant = records.take_lease(
            build_id=build_id, holder="press", now=now, target_branch="main"
        )
        assert grant is not None
        assert records.record(build_id=build_id, turn=grant.turn, now=now, result=result)
        records.release_lease(build_id=build_id, turn=grant.turn, holder="press", now=now)

    def _report(self, pool: SqliteLifecyclePersistence, build_id: str, *, status: str, result: str, detail: str) -> None:  # noqa: F811
        now = datetime.now(UTC)
        pool.record_stage(
            StageLogEntry(
                build_id=build_id,
                stage_label=MERGE_REPORT_STAGE_LABEL,
                target_kind="local_tool",
                target_identifier=MERGE_REPORT_TARGET_IDENTIFIER,
                status=status,
                gate_mode=None,
                started_at=now,
                completed_at=now,
                duration_secs=0.0,
                details={"result": result, "detail": detail},
            )
        )

    @pytest.mark.asyncio
    async def test_approve_is_merging_then_merged(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, thread, build_id = await self._offered(config_on, pool, repo_root)
        self._decide(pool, build_id, "approve")

        assert await finish_ready_mission(deps, MISSION_ID) == "merging"
        assert store.get(MISSION_ID).status == "MERGING"

        self._published(pool, build_id, "merged into the remote and running")
        self._report(
            pool,
            build_id,
            status="PASSED",
            result="merged-into-the-remote-and-running",
            detail="FEAT-MX1 was joined onto main. Merged into the remote and running.",
        )
        assert await finish_ready_mission(deps, MISSION_ID) == "merged"
        assert store.get(MISSION_ID).status == "MERGED"
        assert thread.lines[-1][1] == 'Mission "Tags for notes" is merged into main and deployed.'

    @pytest.mark.asyncio
    async def test_a_deploy_that_failed_after_the_send_is_merged_and_says_so(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, thread, build_id = await self._offered(config_on, pool, repo_root)
        self._decide(pool, build_id, "approve")
        await finish_ready_mission(deps, MISSION_ID)
        self._published(pool, build_id, "published, deployment pending")
        self._report(
            pool,
            build_id,
            status="FAILED",
            result="merged-deploy-failed",
            detail="The deploy step stopped with exit 1. The joined result stays on main.",
        )

        assert await finish_ready_mission(deps, MISSION_ID) == "merged"
        assert store.get(MISSION_ID).status == "MERGED"
        assert thread.lines[-1][1] == (
            'Mission "Tags for notes" is merged into main, but its deploy '
            "failed: The deploy step stopped with exit 1."
        )

    @pytest.mark.asyncio
    async def test_published_and_not_deployed_says_so(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, thread, build_id = await self._offered(config_on, pool, repo_root)
        self._decide(pool, build_id, "approve")
        await finish_ready_mission(deps, MISSION_ID)
        self._published(pool, build_id, "published, deployment pending")
        self._report(
            pool,
            build_id,
            status="PASSED",
            result="published-deployment-pending",
            detail="The project declares no deployment target. More words.",
        )

        assert await finish_ready_mission(deps, MISSION_ID) == "merged"
        assert thread.lines[-1][1] == (
            'Mission "Tags for notes" is merged into main; it is not deployed '
            "yet: The project declares no deployment target."
        )

    @pytest.mark.asyncio
    async def test_a_report_word_alone_does_not_make_it_merged(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, _thread, build_id = await self._offered(config_on, pool, repo_root)
        self._decide(pool, build_id, "approve")
        await finish_ready_mission(deps, MISSION_ID)
        self._report(
            pool,
            build_id,
            status="PASSED",
            result="merged-into-the-remote-and-running",
            detail="says so, but the record does not",
        )

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        assert store.get(MISSION_ID).status == "NOT_MERGED"

    @pytest.mark.asyncio
    async def test_reject_is_not_merged_and_the_branch_is_kept(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, thread, build_id = await self._offered(config_on, pool, repo_root)
        tip = remote_tip(repo_root, MISSION_BRANCH)
        self._decide(pool, build_id, "reject")

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED" and "declined" in str(mission.closed_reason)
        assert remote_tip(repo_root, MISSION_BRANCH) == tip
        assert "the branch mission/m-abc123 is kept" in thread.lines[-1][1]

    @pytest.mark.asyncio
    async def test_a_press_that_did_not_reach_main_is_not_merged_with_its_reason(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store, deps, thread, build_id = await self._offered(config_on, pool, repo_root)
        self._decide(pool, build_id, "approve")
        await finish_ready_mission(deps, MISSION_ID)
        self._report(
            pool,
            build_id,
            status="FAILED",
            result="merge-refused",
            detail="the merge stopped on a conflict in README.md; nothing was merged and the branch is kept",
        )

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert "conflict in README.md" in str(mission.closed_reason)


class TestTheFinalMerge:
    @pytest.mark.asyncio
    async def test_it_joins_under_the_missions_own_names_with_the_missions_receipt(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        attempt = attempt_correlation_id(MISSION_CORRELATION, "B", 1)
        router = _Router({attempt, MISSION_CORRELATION})
        store = await _land(config_on, pool, repo_root, router=router)
        landed_join = _git(repo_root, "rev-parse", "factory-integration/FEAT-MX1")
        service, _wire = _offer_service(config_on, pool)
        finish, thread = _finish_deps(config_on, pool, offer=service.offer, router=router)
        assert await finish_ready_mission(finish, MISSION_ID) == "offered"
        build_id = str(mission_merge_build_id(pool, MISSION_ID))
        tip = store.get(MISSION_ID).final_commit
        main_before = remote_tip(repo_root, "main")
        joins = JoinsTheBranch()
        deps, joins, _deploy, publisher, _bus = press_deps(
            config_on, pool, repo_root, joins=joins, routing_gate=router,
            declares_a_target=False,
        )
        consumer = MergeApprovalConsumer(deps)

        await consumer.handle_envelope(
            _envelope(
                request_id=f"merge-{build_id}",
                decision="approve",
                decided_by="rich",
                correlation_id=MISSION_CORRELATION,
            )
        )
        await _drain(consumer)

        merges = [c for c in joins.calls if c["args"][0] == "merge"]
        assert len(merges) == 1
        args = merges[0]["args"]
        assert args[1] == "FEAT-MX1"
        assert args[args.index("--target") + 1] == f"factory-integration/mission-{MISSION_ID}"
        assert args[args.index("--branch") + 1] == f"{SOURCE_BRANCH_PREFIX}{build_id}"
        assert args[args.index("--expect-main-sha") + 1] == main_before
        assert merges[0]["feature_routing_id"] == MISSION_CORRELATION
        # NO COLLISION: the feature's own join is exactly where its landing left it.
        assert _git(repo_root, "rev-parse", "factory-integration/FEAT-MX1") == landed_join
        assert _local(repo_root, f"refs/heads/factory-integration/mission-{MISSION_ID}")
        # The mission went to main, and main now contains the mission tip.
        assert [a["target_branch"] for a in publisher.asked] == ["main"]
        assert remote_tip(repo_root, "main") != main_before
        assert (
            subprocess.run(
                ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", tip, remote_tip(repo_root, "main")],
                capture_output=True,
            ).returncode
            == 0
        )

        assert await finish_ready_mission(finish, MISSION_ID) == "merged"
        assert store.get(MISSION_ID).status == "MERGED"
        assert mission_of_build(pool, build_id).is_merge


class TestTheTicksOneLine:
    @pytest.mark.asyncio
    async def test_nothing_runs_until_a_finisher_is_installed(self) -> None:
        ran: list[str] = []

        async def finisher() -> None:
            ran.append("ran")

        install_finisher(None)
        await run_finishing_step()
        install_finisher(finisher)
        try:
            await run_finishing_step()
        finally:
            install_finisher(None)
        assert ran == ["ran"]

    @pytest.mark.asyncio
    async def test_a_finisher_that_raises_never_stops_the_queue(self) -> None:
        async def finisher() -> None:
            raise RuntimeError("broken")

        install_finisher(finisher)
        try:
            await run_finishing_step()
        finally:
            install_finisher(None)

    @pytest.mark.asyncio
    async def test_ready_missions_are_finished_in_tasks_of_their_own(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        store = a_running_mission(pool, keys=("B",))
        for attempt in store.attempts(MISSION_ID):
            store.mark_failed(attempt.correlation_id, "the build failed")
        store.set_status(MISSION_ID, "READY", expected=("RUNNING",))

        async def offer(**_kwargs: Any) -> bool:
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)

        started = await mission_finish.finish_ready_missions(deps, wait=True)

        assert started == [MISSION_ID]
        assert store.get(MISSION_ID).status == "NOT_MERGED"


# ---------------------------------------------------------------------------
# The frozen source's own rule, and its row
# ---------------------------------------------------------------------------


class TestTheSourceBranchRule:
    def test_a_commit_not_in_the_clone_is_refused_never_fetched(self, repo_root: Path) -> None:  # noqa: F811
        made = make_source_branch_sync(repo_root, "build-FEAT-MX1-1", "f" * 40)
        assert not made.ok and "refusing to fetch" in str(made.refusal)

    def test_a_stale_branch_is_put_back(self, repo_root: Path) -> None:  # noqa: F811
        main = _git(repo_root, "rev-parse", "main")
        feature = _git(repo_root, "rev-parse", "autobuild/FEAT-MX1")
        _git(repo_root, "branch", "forge/source/build-FEAT-MX1-1", feature)
        made = make_source_branch_sync(repo_root, "build-FEAT-MX1-1", main)
        assert made.ok and made.commit == main
        assert _git(repo_root, "rev-parse", "forge/source/build-FEAT-MX1-1") == main

    def test_a_build_id_that_is_not_plain_is_refused(self, repo_root: Path) -> None:  # noqa: F811
        main = _git(repo_root, "rev-parse", "main")
        assert not make_source_branch_sync(repo_root, "../x", main).ok

    def test_the_prefix_is_the_runners(self) -> None:
        from forge.subagents.autobuild_runner import SOURCE_BRANCH_PREFIX as RUNNERS

        assert SOURCE_BRANCH_PREFIX == RUNNERS


class TestTheMergeBuildRow:
    def test_it_is_written_once_per_mission(self, pool: SqliteLifecyclePersistence) -> None:  # noqa: F811
        a_running_mission(pool, keys=("B",))
        kwargs = dict(
            mission_id=MISSION_ID,
            correlation_id=MISSION_CORRELATION,
            feature_id="FEAT-MX1",
            repo=REPO,
            branch=MISSION_BRANCH,
            feature_yaml_path="f.yaml",
            originating_user="rich",
            start_commit="1" * 40,
            target_branch="main",
            queued_at=datetime(2026, 10, 17, 9, 30, tzinfo=UTC),
        )
        first = pool.record_mission_merge_build(**kwargs)
        second = pool.record_mission_merge_build(**{**kwargs, "queued_at": datetime.now(UTC)})
        assert first == second == "build-FEAT-MX1-20261017093000"
        count = pool.connection.execute(
            "SELECT COUNT(*) FROM builds WHERE mission_role = 'mission-merge'"
        ).fetchone()[0]
        assert count == 1


class TestForgeStatusShowsTheMissionsMerge:
    def test_the_row_reads_as_the_missions_merge(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path  # noqa: F811
    ) -> None:
        import sqlite3

        from forge.cli.status import _build_table, _query_status_views

        a_running_mission(pool, keys=("B",))
        build_id = pool.record_mission_merge_build(
            mission_id=MISSION_ID,
            correlation_id=MISSION_CORRELATION,
            feature_id="FEAT-MX1",
            repo=REPO,
            branch=MISSION_BRANCH,
            feature_yaml_path="f.yaml",
            originating_user="rich",
            start_commit="1" * 40,
            target_branch="main",
            queued_at=datetime.now(UTC),
        )
        cx = sqlite3.connect(str(tmp_path / "forge.db"))
        cx.row_factory = sqlite3.Row
        views = _query_status_views(cx, None)
        view = next(v for v in views if v.build_id == build_id)
        assert view.mission_merge_of == MISSION_ID
        from rich.console import Console

        console = Console(width=200, record=True)
        console.print(_build_table([view]))
        assert f"mission {MISSION_ID}'s merge (last: FEAT-MX1)" in console.export_text()


class TestForgeHistoryShowsTheMissionsMerge:
    def test_the_table_and_the_markdown_name_the_mission(
        self, pool: SqliteLifecyclePersistence  # noqa: F811
    ) -> None:
        from forge.cli.history import render_markdown, render_table

        a_running_mission(pool, keys=("B",))
        build_id = pool.record_mission_merge_build(
            mission_id=MISSION_ID,
            correlation_id=MISSION_CORRELATION,
            feature_id="FEAT-MX1",
            repo=REPO,
            branch=MISSION_BRANCH,
            feature_yaml_path="f.yaml",
            originating_user="rich",
            start_commit="1" * 40,
            target_branch="main",
            queued_at=datetime.now(UTC),
        )
        row = pool.get_build_row(build_id)

        assert f"(mission {MISSION_ID}'s merge into main)" in render_table([(row, [])])
        assert f"Mission {MISSION_ID}'s merge into main." in render_markdown(
            [(row, [])], feature_id=None
        )


class TestTheLastLandingIsTheOneThatCounts:
    @pytest.mark.asyncio
    async def test_two_landings_reach_an_offer_at_the_second(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.pipeline.publication_record import PublicationRecordStore

        a_running_mission(pool, keys=("B", "C"))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root, keys=("B", "C"))
        records = PublicationRecordStore(pool.connection)
        first, second = records.read(B_BUILD).j_commit, records.read(C_BUILD).j_commit
        assert first != second
        service, _wire = _offer_service(config_on, pool)
        deps, _thread = _finish_deps(config_on, pool, offer=service.offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "offered"
        assert store.get(MISSION_ID).final_commit == second
        build_id = mission_merge_build_id(pool, MISSION_ID)
        # The last feature to land names the merge build.
        assert str(build_id).startswith("build-FEAT-MX2-")
        assert _offers(pool, str(build_id))[0].details[MERGE_OFFER_DETAILS_KEY]["candidate_sha"] == second


class _ReadsBackWrong:
    """The venue, except the frozen source reads back as another commit."""

    def __init__(self, inner: Any, wrong: str) -> None:
        self.inner, self.wrong = inner, wrong

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def rev_parse(self, ref: str) -> str | None:
        if ref.startswith(f"refs/heads/{SOURCE_BRANCH_PREFIX}"):
            return self.wrong
        return await self.inner.rev_parse(ref)


class TestAFrozenSourceThatDoesNotReadBack:
    @pytest.mark.asyncio
    async def test_no_card_is_offered(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        service, wire = _offer_service(config_on, pool)
        deps, thread = _finish_deps(config_on, pool, offer=service.offer)
        honest = deps.git_for
        main = _git(repo_root, "rev-parse", "main")
        deps.git_for = lambda repo, root: _ReadsBackWrong(honest(repo, root), main)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        build_id = str(mission_merge_build_id(pool, MISSION_ID))
        assert _offers(pool, build_id) == []
        assert wire.approvals == [] and wire.cards == []
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert "reads back as" in str(mission.closed_reason)
        assert len(thread.lines) == 1


class TestARefusedOfferIsRetriedWhenItMayPass:
    async def _ready(self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path) -> MissionStore:  # noqa: F811
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        return await _land(config, pool, repo_root)

    @pytest.mark.asyncio
    async def test_a_refusal_that_may_pass_waits_for_the_next_tick(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store = await self._ready(config_on, pool, repo_root)
        service, wire = _offer_service(config_on, pool)
        calls: list[int] = []

        async def offer(**kwargs: Any) -> bool:
            calls.append(1)
            if len(calls) == 1:
                kwargs["on_refusal"]("the target branch's commit could not be read", True)
                return False
            return await service.offer(**kwargs)

        deps, thread = _finish_deps(config_on, pool, offer=offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "waiting to offer"
        assert store.get(MISSION_ID).status == "READY" and thread.lines == []
        # Not offered again at once: once every OFFER_RETRY_SECONDS.
        assert await finish_ready_mission(deps, MISSION_ID) == "waiting to offer"
        assert len(calls) == 1
        later = datetime.now(UTC) + timedelta(seconds=mission_finish.OFFER_RETRY_SECONDS + 1)
        deps.clock = lambda: later
        assert await finish_ready_mission(deps, MISSION_ID) == "offered"
        assert len(wire.approvals) == 1

    @pytest.mark.asyncio
    async def test_a_card_refused_for_half_an_hour_closes_the_mission(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store = await self._ready(config_on, pool, repo_root)
        tip = remote_tip(repo_root, MISSION_BRANCH)
        calls: list[int] = []

        async def offer(**kwargs: Any) -> bool:
            calls.append(1)
            kwargs["on_refusal"]("the preview could not be prepared", True)
            return False

        deps, thread = _finish_deps(config_on, pool, offer=offer)
        start = datetime.now(UTC)
        answers = []
        for step in range(mission_finish.OFFER_ATTEMPTS + 1):
            moment = start + timedelta(seconds=step * (mission_finish.OFFER_RETRY_SECONDS + 1))
            deps.clock = lambda moment=moment: moment
            answers.append(await finish_ready_mission(deps, MISSION_ID))
            if answers[-1] != "waiting to offer":
                break

        # Six tries are only twenty-five minutes; the seventh is past thirty.
        assert answers == ["waiting to offer"] * mission_finish.OFFER_ATTEMPTS + ["not merged"]
        assert len(calls) == mission_finish.OFFER_ATTEMPTS + 1
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert "could not be offered after 7 tries" in str(mission.closed_reason)
        assert "the preview could not be prepared" in str(mission.closed_reason)
        assert remote_tip(repo_root, MISSION_BRANCH) == tip
        assert len(thread.lines) == 1

    @pytest.mark.asyncio
    async def test_the_count_of_failed_offers_survives_a_restart(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store = await self._ready(config_on, pool, repo_root)

        async def offer(**kwargs: Any) -> bool:
            kwargs["on_refusal"]("the preview could not be prepared", True)
            return False

        start = datetime.now(UTC)
        for step in range(mission_finish.OFFER_ATTEMPTS + 1):
            # A fresh set of collaborators each time: nothing kept in memory.
            deps, _thread = _finish_deps(config_on, pool, offer=offer)
            moment = start + timedelta(seconds=step * (mission_finish.OFFER_RETRY_SECONDS + 1))
            deps.clock = lambda moment=moment: moment
            await finish_ready_mission(deps, MISSION_ID)

        assert store.get(MISSION_ID).status == "NOT_MERGED"

    @pytest.mark.asyncio
    async def test_a_refusal_that_will_not_pass_closes_the_mission(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store = await self._ready(config_on, pool, repo_root)
        off = config_on.model_copy(deep=True)
        off.merge_executor.enabled = False
        service, wire = _offer_service(off, pool)
        deps, thread = _finish_deps(config_on, pool, offer=service.offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        mission = store.get(MISSION_ID)
        assert mission.status == "NOT_MERGED"
        assert "switched off" in str(mission.closed_reason)
        assert wire.approvals == []

    @pytest.mark.asyncio
    async def test_the_real_offer_says_an_unreadable_main_may_pass(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        store = await self._ready(config_on, pool, repo_root)
        wire = _Wire()

        async def no_head(_root: Path, _branch: str | None = None) -> str | None:
            return None

        service = MergeOfferService(
            config=config_on,
            pool=pool,
            pipeline_publisher=SimpleNamespace(publish_build_paused=wire.publish_build_paused),
            raw_publish=wire.raw_publish,
            git_head=no_head,
        )
        deps, _thread = _finish_deps(config_on, pool, offer=service.offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "waiting to offer"
        assert store.get(MISSION_ID).status == "READY"


class TestTheMissionBranch:
    @pytest.mark.asyncio
    async def test_a_branch_gone_from_the_remote_is_not_merged(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        _git(repo_root, "push", "-q", "origin", f":refs/heads/{MISSION_BRANCH}")

        async def offer(**_kwargs: Any) -> bool:
            raise AssertionError("no card for a branch that is gone")

        deps, thread = _finish_deps(config_on, pool, offer=offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "not merged"
        mission = store.get(MISSION_ID)
        assert "something else changed the mission branch" in str(mission.closed_reason)
        assert len(thread.lines) == 1

    @pytest.mark.asyncio
    async def test_a_remote_that_cannot_be_reached_waits(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        store = await _land(config_on, pool, repo_root)
        away = config_on.model_copy(deep=True)
        away.planning.fetch_addresses[REPO] = str(tmp_path / "nowhere.git")

        async def offer(**_kwargs: Any) -> bool:
            raise AssertionError("no card while the remote cannot be read")

        deps, thread = _finish_deps(away, pool, offer=offer)

        assert await finish_ready_mission(deps, MISSION_ID) == "waiting for the remote"
        assert store.get(MISSION_ID).status == "READY" and thread.lines == []


class TestALandingThatNeverReportedIsCarriedOn:
    def _decided(self, pool: SqliteLifecyclePersistence, build_id: str) -> None:  # noqa: F811
        from forge.lifecycle.mission_builds import landing_attempt_id

        pool.record_stage(
            _decision_entry(
                build_id=build_id,
                decision="approve",
                decided_by=f"mission {MISSION_ID} (approved by rich)",
                request_id=f"merge-{build_id}",
                now=datetime.now(UTC),
                attempt_id=landing_attempt_id(build_id),
            )
        )

    def _finished_at(self, pool: SqliteLifecyclePersistence, build_id: str, when: datetime) -> None:  # noqa: F811
        pool.connection.execute(
            "UPDATE builds SET completed_at = ? WHERE build_id = ?",
            (when.isoformat(), build_id),
        )
        pool.connection.commit()

    async def _deps(self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path, **press: Any) -> tuple[MissionFinishDeps, Any, MergeApprovalConsumer]:  # noqa: F811
        deps_, _joins, _deploy, publisher, _bus = press_deps(config, pool, repo_root, **press)
        consumer = MergeApprovalConsumer(deps_)

        async def offer(**_kwargs: Any) -> bool:
            return True

        deps, _thread = _finish_deps(config, pool, offer=offer)
        deps.land = consumer.land_mission_feature
        return deps, publisher, consumer

    @pytest.mark.asyncio
    async def test_after_a_restart_the_landing_is_pressed_once_more(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.pipeline.publication_record import (
            RESULT_PUBLISHED_TO_MISSION_BRANCH,
            PublicationRecordStore,
        )

        a_running_mission(pool, keys=("B", "C"))
        tip = the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        a_mission_feature_build(pool, build_id=C_BUILD, feature_id="FEAT-MX2", key="C")
        # B's landing was asked for and the coordinator stopped before it
        # pressed: a decision row and nothing else.
        self._decided(pool, B_BUILD)
        deps, publisher, consumer = await self._deps(config_on, pool, repo_root)
        # C finished with failed tasks: the offer hook gave it a FAILED report.
        assert await consumer.land_mission_feature(C_BUILD, tasks_failed=1) is None

        started = await mission_finish.finish_ready_missions(deps, wait=True)
        again = await mission_finish.finish_ready_missions(deps, wait=True)

        assert started == [B_BUILD] and again == []
        landed = PublicationRecordStore(pool.connection).read(B_BUILD)
        assert landed.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert landed.g_commit == tip
        assert remote_tip(repo_root, MISSION_BRANCH) == landed.j_commit
        assert len(publisher.asked) == 1
        decisions = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == "merge_deploy_decision"
        ]
        assert len(decisions) == 1
        # C was never pressed, and its report says why.
        assert not PublicationRecordStore(pool.connection).read(C_BUILD).recorded
        reports = [
            s for s in pool.read_stages(C_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert [r.status for r in reports] == ["FAILED"]
        assert reports[0].details["result"] == "mission-landing-failed"
        assert "1 failed task(s)" in reports[0].details["detail"]

    @pytest.mark.asyncio
    async def test_a_hook_that_never_ran_is_failed_after_the_grace_never_pressed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from datetime import timedelta

        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        deps, publisher, _consumer = await self._deps(config_on, pool, repo_root)

        self._finished_at(pool, B_BUILD, datetime.now(UTC))
        assert await mission_finish.finish_ready_missions(deps, wait=True) == []

        self._finished_at(
            pool,
            B_BUILD,
            datetime.now(UTC) - timedelta(seconds=mission_finish.LANDING_GRACE_SECONDS + 5),
        )
        assert await mission_finish.finish_ready_missions(deps, wait=True) == [B_BUILD]
        # NO DURABLE FAILED-TASK COUNT, SO UNKNOWN COUNTS AS FAILED: the build
        # may have finished with failed tasks, and it is not pressed.
        assert publisher.asked == []
        reports = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert [r.status for r in reports] == ["FAILED"]
        assert reports[0].details["result"] == "mission-landing-failed"
        assert "could tell whether every task passed" in reports[0].details["detail"]
        assert await mission_finish.finish_ready_missions(deps, wait=True) == []

    @pytest.mark.asyncio
    async def test_a_press_that_keeps_raising_gets_a_failed_report_and_stops(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.lifecycle.mission_builds import landing_attempt_id
        from forge.lifecycle.feature_routing import router_restart_blockers

        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        self._decided(pool, B_BUILD)
        # The routing gate is on and this attempt has no committed receipt:
        # the press raises before it does anything, every time.
        router = _Router(set())
        deps, publisher, _consumer = await self._deps(
            config_on, pool, repo_root, routing_gate=router
        )

        passes = [
            await mission_finish.finish_ready_missions(deps, wait=True) for _ in range(5)
        ]

        assert passes == [[B_BUILD], [B_BUILD], [B_BUILD], [], []]
        assert len(router.asked) == 3 and publisher.asked == []
        reports = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert len(reports) == 1 and reports[0].status == "FAILED"
        details = reports[0].details
        assert details["result"] == "mission-landing-failed"
        assert "RuntimeError on all 3 attempts" in details["detail"]
        assert details["merge_decision"]["execution_attempt_id"] == landing_attempt_id(B_BUILD)
        # The approved landing no longer holds the router.
        assert f"merge-approved-pending:{B_BUILD}" not in router_restart_blockers(pool.connection)


class TestTheLandingCountIsDurable:
    @pytest.mark.asyncio
    async def test_a_restart_between_raises_still_stops_at_the_bound(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        router = _Router(set())
        asked: list[str] = []
        for _ in range(5):
            # A new coordinator each pass: a fresh consumer, nothing in memory.
            press, _joins, _deploy, publisher, _bus = press_deps(
                config_on, pool, repo_root, routing_gate=router
            )
            consumer = MergeApprovalConsumer(press)
            await consumer.land_mission_feature(B_BUILD)
            asked.extend(a["target_branch"] for a in publisher.asked)

        assert len(router.asked) == 3 and asked == []
        attempts = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == "mission_landing_attempt"
        ]
        assert [a.details["mission_landing_attempt"]["attempt"] for a in attempts] == [1, 2, 3]
        reports = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert len(reports) == 1 and reports[0].status == "FAILED"

    @pytest.mark.asyncio
    async def test_a_press_that_took_the_coordinator_down_still_counts(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")

        from tests.forge.pipeline.test_merge_executor import _the_kept_hold_runs_out

        class _Down(BaseException):
            """The coordinator going down: nothing in the press catches it."""

        class _Crashes:
            calls = 0

            async def __call__(self, **_kwargs: Any) -> Any:
                type(self).calls += 1
                raise _Down("the coordinator went down mid-press")

        for _ in range(3):
            press, *_rest = press_deps(config_on, pool, repo_root, joins=_Crashes())
            with pytest.raises(_Down):
                await MergeApprovalConsumer(press).land_mission_feature(B_BUILD)
            # The hold the dead press kept runs out on its own.
            _the_kept_hold_runs_out(pool, B_BUILD)
        press, joins, *_rest = press_deps(config_on, pool, repo_root)

        assert await MergeApprovalConsumer(press).land_mission_feature(B_BUILD) is None
        assert _Crashes.calls == 3 and joins.calls == []
        reports = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert len(reports) == 1 and "started 3 times and never reported" in (
            reports[0].details["detail"]
        )


class TestNothingIsPressedTwice:
    @pytest.mark.asyncio
    async def test_a_resume_while_the_first_landing_waits_for_the_lock(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        import asyncio

        a_running_mission(pool, keys=("B",))
        the_mission_branch_on_the_remote(repo_root)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        press, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(press)

        async def offer(**_kwargs: Any) -> bool:
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        deps.land = consumer.land_mission_feature

        lock = consumer._lock_for(REPO)
        await lock.acquire()
        first = asyncio.create_task(consumer.land_mission_feature(B_BUILD))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(
                s.target_identifier == "merge_deploy_decision"
                for s in pool.read_stages(B_BUILD)
            ):
                break
        # The resume sees a decision and no report, and must come back at once
        # rather than queue a second press behind the first.
        assert await mission_finish.finish_ready_missions(deps) == [B_BUILD]
        await asyncio.wait_for(
            asyncio.gather(*list(mission_finish._tasks)), timeout=5
        )
        lock.release()
        await first

        assert len([c for c in joins.calls if c["args"][0] == "merge"]) == 1
        assert len(publisher.asked) == 1
        decisions = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == "merge_deploy_decision"
        ]
        assert len(decisions) == 1

    @pytest.mark.asyncio
    async def test_a_landing_held_by_a_live_lease_is_not_resumed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.pipeline.publication_record import PublicationRecordStore

        a_running_mission(pool, keys=("B",))
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        TestALandingThatNeverReportedIsCarriedOn()._decided(pool, B_BUILD)
        assert PublicationRecordStore(pool.connection).take_lease(
            build_id=B_BUILD, holder="another-worker", now=datetime.now(UTC)
        )
        landed: list[str] = []

        async def land(build_id: str, **_kwargs: Any) -> None:
            landed.append(build_id)

        async def offer(**_kwargs: Any) -> bool:
            return True

        deps, _thread = _finish_deps(config_on, pool, offer=offer)
        deps.land = land

        assert await mission_finish.finish_ready_missions(deps, wait=True) == []
        assert landed == []
