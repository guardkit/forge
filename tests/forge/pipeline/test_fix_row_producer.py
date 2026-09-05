"""A failure becomes a repair job — once, and only with evidence behind it.

Conductor rewire spec 2026-09-05, rules 1 and 8 (the producer half).

What these pin, in the words of the thing they protect:

- **One row per failure, however many times the news arrives.** Redelivery
  of the same terminal envelope, and a second boot replaying it, both file
  the SAME row — the queue's UNIQUE correlation id is the whole rule.
- **No row without evidence.** A build that failed and left no failure pack
  files nothing: a repair journey with no receipts would review blind.
- **A merge that did not stay green files one too**, through the merge
  executor's report — proved with an in-process recorder standing in for the
  publisher, the ``tests/bdd/conftest.py`` pattern, never a live broker.
- **A defect in the producer never costs a build its ending.** The build
  state recorder is the sole writer of ``builds.status``; a producer that
  blew up must leave the FAILED row exactly as it was.

Nothing here opens a socket and nothing reads the live database: every test
gets its own SQLite file under ``tmp_path`` and its own receipts root.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from nats_core.events import BuildFailedPayload, BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.pipeline import fix_row_producer
from forge.pipeline.fix_row_producer import (
    SOURCE_BUILD_FAILED,
    SOURCE_MERGE_REPORT,
    fix_correlation_id,
    make_failure_pack_source_reader,
    maybe_mint_fix_row,
    source_build_id_from_correlation_id,
)
from forge.subagents.autobuild_runner import (
    _RECEIPT_FAMILIES,
    FAILURE_MANIFEST_NAME,
    STDOUT_LOG_NAME,
)

FEATURE_ID = "FEAT-44A8"
CORRELATION_ID = "corr-44a8"


# ---------------------------------------------------------------------------
# A database, a build, and a pack on disk
# ---------------------------------------------------------------------------


@pytest.fixture
def receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A receipts root of our own — the routine path's own knob, pointed here."""
    root = tmp_path / "receipts"
    root.mkdir()
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[SqliteLifecyclePersistence]:
    db_path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(db_path)
    lifecycle_migrations.apply_at_boot(cx)
    try:
        yield SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    finally:
        cx.close()


def queue_a_build(
    pool: SqliteLifecyclePersistence,
    *,
    feature_id: str = FEATURE_ID,
    correlation_id: str = CORRELATION_ID,
    repo: str = "appmilla_github/api_test",
    queued_at: datetime | None = None,
) -> str:
    now = queued_at or datetime.now(UTC)
    return pool.record_pending_build(
        BuildQueuedPayload(
            feature_id=feature_id,
            repo=repo,
            feature_yaml_path=f".guardkit/features/{feature_id}.yaml",
            triggered_by="cli",
            originating_user="rich",
            correlation_id=correlation_id,
            requested_at=now,
            queued_at=now,
        )
    )


def write_pack(receipts: Path, build_id: str) -> Path:
    """Lay down the receipts a failed build leaves behind."""
    pack = receipts / build_id
    for family in _RECEIPT_FAMILIES:
        (pack / family).mkdir(parents=True, exist_ok=True)
        (pack / family / "verdict.json").write_text("{}", encoding="utf-8")
    (pack / STDOUT_LOG_NAME).write_text("===== autobuild run\n", encoding="utf-8")
    (pack / FAILURE_MANIFEST_NAME).write_text(
        json.dumps({"build_id": build_id, "feature_id": FEATURE_ID}),
        encoding="utf-8",
    )
    return pack


def queue_rows(pool: SqliteLifecyclePersistence) -> list[sqlite3.Row]:
    return list(
        pool.connection.execute(
            "SELECT * FROM work_queue ORDER BY id ASC"
        ).fetchall()
    )


def events(pool: SqliteLifecyclePersistence, queue_id: int) -> list[sqlite3.Row]:
    return list(
        pool.connection.execute(
            "SELECT * FROM work_queue_events WHERE queue_id = ? ORDER BY id ASC",
            (queue_id,),
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# The correlation id IS the link back to the failed build
# ---------------------------------------------------------------------------


class TestTheCorrelationIdCarriesTheFailedBuild:
    def test_it_spells_out_the_build_it_repairs(self) -> None:
        assert fix_correlation_id("build-FEAT-1-2026") == "fix-build-FEAT-1-2026"

    def test_and_reads_back(self) -> None:
        assert (
            source_build_id_from_correlation_id("fix-build-FEAT-1-2026")
            == "build-FEAT-1-2026"
        )

    @pytest.mark.parametrize("value", [None, "", "fix-", "corr-123", "  "])
    def test_anything_else_names_no_build(self, value: str | None) -> None:
        assert source_build_id_from_correlation_id(value) is None


# ---------------------------------------------------------------------------
# One row per failure
# ---------------------------------------------------------------------------


class TestOneRowPerFailure:
    def test_a_failed_build_with_a_pack_files_one_row(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        queue_id = maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_BUILD_FAILED,
            detail="gates red: pytest",
        )

        rows = queue_rows(pool)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == queue_id
        assert row["kind"] == "fix"
        assert row["status"] == "QUEUED"
        assert row["correlation_id"] == fix_correlation_id(build_id)
        assert row["target_repo"] == "appmilla_github/api_test"
        assert row["originating_user"] == "rich"

    def test_the_sentence_names_the_feature_and_what_failed(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_BUILD_FAILED,
            detail="gates red: pytest",
        )

        sentence = str(queue_rows(pool)[0]["sentence"])
        assert sentence == (
            "The build of FEAT-44A8 in appmilla_github/api_test failed: "
            "gates red: pytest"
        )

    def test_a_stack_trace_of_a_reason_becomes_one_readable_line(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_BUILD_FAILED,
            detail="line one\n  line two\n\nline three " + ("x" * 800),
        )

        sentence = str(queue_rows(pool)[0]["sentence"])
        assert "\n" not in sentence
        assert len(sentence) <= fix_row_producer.MAX_SENTENCE_CHARS
        assert sentence.endswith("…")

    def test_redelivery_files_the_same_row(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        """The same terminal envelope arriving twice is one repair, not two."""
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        first = maybe_mint_fix_row(
            pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED, detail="red"
        )
        second = maybe_mint_fix_row(
            pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED, detail="red"
        )

        assert first == second
        assert len(queue_rows(pool)) == 1

    def test_a_reboot_that_replays_the_terminal_files_the_same_row(
        self, tmp_path: Path, receipts: Path
    ) -> None:
        """A fresh connection — a second boot — still finds the row filed."""
        db_path = tmp_path / "forge.db"
        first_cx = sqlite_connect.connect_writer(db_path)
        lifecycle_migrations.apply_at_boot(first_cx)
        first = SqliteLifecyclePersistence(connection=first_cx, db_path=db_path)
        build_id = queue_a_build(first)
        write_pack(receipts, build_id)
        one = maybe_mint_fix_row(
            pool=first, build_id=build_id, source=SOURCE_BUILD_FAILED, detail="red"
        )
        first_cx.close()

        second_cx = sqlite_connect.connect_writer(db_path)
        second = SqliteLifecyclePersistence(connection=second_cx, db_path=db_path)
        try:
            two = maybe_mint_fix_row(
                pool=second,
                build_id=build_id,
                source=SOURCE_BUILD_FAILED,
                detail="red",
            )
            assert one == two
            assert len(queue_rows(second)) == 1
        finally:
            second_cx.close()

    def test_two_different_failures_file_two_rows(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        one = queue_a_build(pool, correlation_id="corr-1")
        two = queue_a_build(
            pool, feature_id="FEAT-BBBB", correlation_id="corr-2"
        )
        write_pack(receipts, one)
        write_pack(receipts, two)

        maybe_mint_fix_row(pool=pool, build_id=one, source=SOURCE_BUILD_FAILED)
        maybe_mint_fix_row(pool=pool, build_id=two, source=SOURCE_BUILD_FAILED)

        assert len(queue_rows(pool)) == 2


# ---------------------------------------------------------------------------
# No row without evidence
# ---------------------------------------------------------------------------


class TestNoRowWithoutEvidence:
    def test_a_failed_build_with_no_pack_files_nothing(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)  # no pack written

        assert (
            maybe_mint_fix_row(
                pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED
            )
            is None
        )
        assert queue_rows(pool) == []

    def test_a_red_merge_needs_no_pack_because_the_build_succeeded(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)  # no pack: it did not fail, it merged

        queue_id = maybe_mint_fix_row(
            pool=pool,
            build_id=build_id,
            source=SOURCE_MERGE_REPORT,
            detail="merged-deploy-reverted — the live checks went red",
        )

        assert queue_id is not None
        sentence = str(queue_rows(pool)[0]["sentence"])
        assert sentence.startswith(
            "FEAT-44A8 was merged in appmilla_github/api_test but the checks "
            "after it went red:"
        )

    def test_a_build_that_is_not_on_record_files_nothing(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        assert (
            maybe_mint_fix_row(
                pool=pool, build_id="build-NOPE-1", source=SOURCE_BUILD_FAILED
            )
            is None
        )
        assert queue_rows(pool) == []


# ---------------------------------------------------------------------------
# The pack path and the source build ride on the row's filing event
# ---------------------------------------------------------------------------


class TestWhatIsWrittenDown:
    def test_the_filing_event_carries_the_pack_and_the_failed_build(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        pack = write_pack(receipts, build_id)

        queue_id = maybe_mint_fix_row(
            pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED, detail="red"
        )
        assert queue_id is not None

        rows = events(pool, queue_id)
        minted = [row for row in rows if row["action"] == "minted"]
        assert len(minted) == 1
        details = json.loads(str(minted[0]["details_json"]))
        assert details["source_build_id"] == build_id
        assert details["failure_pack_path"] == str(pack)
        assert details["source"] == SOURCE_BUILD_FAILED
        assert minted[0]["actor_identity"] == fix_row_producer.PRODUCER_ACTOR

    def test_no_new_columns_were_needed(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        """The row is the queue's own shape — the evidence rides an event."""
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)
        maybe_mint_fix_row(
            pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED
        )

        columns = {
            str(row[1])
            for row in pool.connection.execute(
                "PRAGMA table_info(work_queue)"
            ).fetchall()
        }
        assert "source_build_id" not in columns
        assert "failure_pack_path" not in columns


# ---------------------------------------------------------------------------
# The pack reader the conductor composition was missing
# ---------------------------------------------------------------------------


class TestThePackSourceReader:
    def test_it_names_the_failed_build_a_repair_came_from(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        failed = queue_a_build(
            pool,
            correlation_id="corr-source",
            queued_at=datetime(2026, 9, 4, 14, 13, 28, tzinfo=UTC),
        )
        repair = queue_a_build(
            pool,
            correlation_id=fix_correlation_id(failed),
            queued_at=datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC),
        )

        read = make_failure_pack_source_reader(pool)

        assert read(repair) == failed

    def test_a_build_queued_some_other_way_names_none(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        build_id = queue_a_build(pool, correlation_id="typed-by-hand")

        assert make_failure_pack_source_reader(pool)(build_id) is None

    def test_a_reader_over_a_broken_pool_answers_none_and_does_not_raise(
        self,
    ) -> None:
        class Angry:
            def get_build_row(self, build_id: str) -> Any:
                raise RuntimeError("the database is on fire")

        assert make_failure_pack_source_reader(Angry())("anything") is None


# ---------------------------------------------------------------------------
# The build-state recorder hook
# ---------------------------------------------------------------------------


class TestTheTerminalHook:
    def test_a_build_landing_failed_files_a_repair_row(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        asyncio.run(
            build_build_state_recorder(pool)(
                BuildFailedPayload(
                    feature_id=FEATURE_ID,
                    build_id=build_id,
                    failure_reason="task 004 unfinished at wave three",
                    failed_stage="autobuild",
                    recoverable=False,
                )
            )
        )

        rows = queue_rows(pool)
        assert len(rows) == 1
        assert rows[0]["kind"] == "fix"
        assert "task 004 unfinished at wave three" in str(rows[0]["sentence"])

    def test_a_build_landing_complete_files_nothing(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        from nats_core.events import BuildCompletePayload

        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        asyncio.run(
            build_build_state_recorder(pool)(
                BuildCompletePayload(
                    feature_id=FEATURE_ID,
                    build_id=build_id,
                    tasks_completed=3,
                    tasks_failed=0,
                    tasks_total=3,
                    duration_seconds=1.0,
                    summary="all green",
                )
            )
        )

        assert queue_rows(pool) == []

    def test_the_same_failed_envelope_twice_still_files_one_row(
        self, pool: SqliteLifecyclePersistence, receipts: Path
    ) -> None:
        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)
        recorder = build_build_state_recorder(pool)
        event = BuildFailedPayload(
            feature_id=FEATURE_ID,
            build_id=build_id,
            failure_reason="gates red",
            failed_stage="autobuild",
            recoverable=False,
        )

        asyncio.run(recorder(event))
        asyncio.run(recorder(event))

        assert len(queue_rows(pool)) == 1

    def test_a_producer_that_blows_up_never_costs_the_build_its_ending(
        self,
        pool: SqliteLifecyclePersistence,
        receipts: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The recorder is the sole writer of builds.status. Nothing it calls
        may be allowed to break that — including this producer."""
        import forge.lifecycle_bridge.build_state_recorder as recorder_module

        build_id = queue_a_build(pool)
        write_pack(receipts, build_id)

        def explode(**_: Any) -> int:
            raise RuntimeError("the producer is broken")

        monkeypatch.setattr(recorder_module, "maybe_mint_fix_row", explode)

        asyncio.run(
            build_build_state_recorder(pool)(
                BuildFailedPayload(
                    feature_id=FEATURE_ID,
                    build_id=build_id,
                    failure_reason="gates red",
                    failed_stage="autobuild",
                    recoverable=False,
                )
            )
        )

        row = pool.connection.execute(
            "SELECT status FROM builds WHERE build_id = ?", (build_id,)
        ).fetchone()
        assert row["status"] == BuildState.FAILED.value
        assert queue_rows(pool) == []


# ---------------------------------------------------------------------------
# The merge executor's hook — through the real executor, no broker
# ---------------------------------------------------------------------------


class TestTheRedMergeHook:
    """A merge that landed and then went red files a repair row.

    The executor is driven for real; only the things that would touch the
    world are stood in for — an in-process recorder in place of the pipeline
    publisher (the ``tests/bdd/conftest.py`` pattern), a fake guardkit and a
    fake deploy dispatcher. No socket is opened.
    """

    MERGE_BUILD = "build-FEAT-MRG1-20260905120000"
    MERGE_FEATURE = "FEAT-MRG1"
    MERGE_REPO = "appmilla_github/api_test"

    @staticmethod
    def _config(repo_root: Path) -> Any:
        from forge.config.models import ForgeConfig

        return ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": [str(repo_root)]}},
                "planning": {
                    "target_repo_paths": {
                        TestTheRedMergeHook.MERGE_REPO: str(repo_root)
                    }
                },
                "approval": {"expected_approver": "rich"},
                "merge_executor": {"enabled": True},
            }
        )

    def _seed_build(self, pool: SqliteLifecyclePersistence) -> None:
        pool.connection.execute(
            "INSERT OR IGNORE INTO builds (build_id, feature_id, repo, branch, "
            "feature_yaml_path, status, triggered_by, originating_user, "
            "correlation_id, queued_at, mode) VALUES (?, ?, ?, 'main', "
            "'f.yaml', 'COMPLETE', 'cli', 'rich', ?, "
            "'2026-09-05T12:00:00Z', 'mode-a')",
            (self.MERGE_BUILD, self.MERGE_FEATURE, self.MERGE_REPO, "corr-mrg-1"),
        )
        pool.connection.commit()

    async def _run(
        self,
        pool: SqliteLifecyclePersistence,
        tmp_path: Path,
        *,
        deploy_outcome: str | None = "reverted",
        dry_run: bool = False,
    ) -> Any:
        from forge.adapters.guardkit.models import GuardKitResult
        from forge.pipeline.merge_executor import (
            MergeExecutorDeps,
            execute_merge_deploy,
        )

        repo_root = tmp_path / "api_test"
        repo_root.mkdir(exist_ok=True)
        self._seed_build(pool)

        published: list[Any] = []

        class Recorder:
            async def publish_stage_complete(self, payload: Any) -> None:
                published.append(payload)

        async def guardkit(**kwargs: Any) -> GuardKitResult:
            return GuardKitResult(
                status="success",
                subcommand=kwargs.get("subcommand", "merge"),
                duration_secs=0.1,
                stdout_tail=json.dumps(
                    {"outcome": "merged", "post_sha": "c" * 40, "verify_ok": True}
                ),
                stderr=None,
                exit_code=0,
            )

        async def deploy(**_: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(
                outcome=deploy_outcome,
                verdict="fail",
                deploy_record_ref="docs/state/x.md",
            )

        deps = MergeExecutorDeps(
            config=self._config(repo_root),
            pool=pool,
            pipeline_publisher=Recorder(),
            guardkit_run=guardkit,
            deploy_dispatcher=deploy,
            receipts_root_fn=lambda: tmp_path / "merge-receipts",
        )
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=self.MERGE_BUILD,
            feature_id=self.MERGE_FEATURE,
            repo=self.MERGE_REPO,
            repo_root=repo_root,
            expect_main_sha="a" * 40,
            correlation_id="corr-mrg-1",
            decided_by="rich",
            dry_run=dry_run,
        )
        return outcome, published

    def test_a_reverted_deploy_files_a_repair_row(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path, receipts: Path
    ) -> None:
        outcome, published = asyncio.run(self._run(pool, tmp_path))

        assert outcome.result == "merged-deploy-reverted"
        assert published and published[0].status == "FAILED"
        rows = queue_rows(pool)
        assert len(rows) == 1
        assert rows[0]["kind"] == "fix"
        assert rows[0]["correlation_id"] == fix_correlation_id(self.MERGE_BUILD)
        assert "merged-deploy-reverted" in str(rows[0]["sentence"])

    def test_a_green_merge_files_nothing(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path, receipts: Path
    ) -> None:
        outcome, _ = asyncio.run(
            self._run(pool, tmp_path, deploy_outcome="complete")
        )

        assert outcome.result == "merged-and-running"
        assert queue_rows(pool) == []

    def test_a_dry_run_files_nothing(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path, receipts: Path
    ) -> None:
        """A dry run changed nothing on purpose; there is nothing to repair."""
        asyncio.run(self._run(pool, tmp_path, dry_run=True))

        assert queue_rows(pool) == []

    def test_a_refused_merge_files_nothing(
        self, pool: SqliteLifecyclePersistence, tmp_path: Path, receipts: Path
    ) -> None:
        """Nothing landed, so nothing is broken — a second press is not a bug."""
        asyncio.run(self._run(pool, tmp_path))
        first = len(queue_rows(pool))

        outcome, _ = asyncio.run(self._run(pool, tmp_path))

        assert outcome.result == "merge-refused"
        assert len(queue_rows(pool)) == first
