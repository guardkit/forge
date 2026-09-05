"""Opening a fix journey — one statement of the rules, two doors.

Conductor rewire spec 2026-09-05, rules 2 and 8 (the admission half).

What these pin:

- **THE CAP LAW applies in process exactly as it applies at the CLI.** The
  same refusals ``tests/forge/test_mode_c_cap_law.py`` pins for
  ``forge queue --mode c`` fire here, and nothing is written when they do.
- **The task id and the fix-task file.** ``TASK-<feature8>FIX<n>``, inside
  the wire's twelve characters, next number when one is taken; the file is
  the three-field drive-6 shape and it lands beside the target repository's
  features.
- **The source build reaches the pack reader.** The build a repair opens
  carries the correlation id ``fix-<the failed build's id>``, which is what
  the conductor's composed reader reads back — so the journey reviews the
  right failure instead of reviewing blind.
- **Write, then publish.** A publish that fails leaves the row alone.

No broker: the publisher is a list. No live database: SQLite under
``tmp_path``. No ``.guardkit`` outside the fixture repository.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import FIX_JOURNEY_PROFILE_NAME, ForgeConfig
from forge.lifecycle import migrations as lifecycle_migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.fix_admission import (
    FixAdmissionRefused,
    FixPublishFailed,
    admit_fix_build,
    admit_fix_row,
    existing_fix_task_ids,
    features_dir,
    mint_fix_task_id,
    read_parent_feature,
    write_fix_task_yaml,
)
from forge.pipeline.fix_row_producer import (
    fix_correlation_id,
    make_failure_pack_source_reader,
)
from forge.planning.work_queue_store import WorkQueueStore

FEATURE_ID = "FEAT-44A8"
REPO_KEY = "appmilla_github/api_test"
SOURCE_BUILD = "build-FEAT-44A8-20260904131328"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    (root / ".guardkit" / "features").mkdir(parents=True)
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


@pytest.fixture
def store(pool: SqliteLifecyclePersistence) -> WorkQueueStore:
    return WorkQueueStore(pool.connection)


def make_config(
    repo_root: Path,
    *,
    profiles: dict[str, Any] | None = None,
    default_profile: str = "attended",
) -> ForgeConfig:
    body: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": [str(repo_root.parent)]}},
        "queue": {"repo_allowlist": [str(repo_root)]},
        "planning": {"target_repo_paths": {REPO_KEY: str(repo_root)}},
        "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
    }
    if profiles is not None:
        body["budget"] = {
            "default_profile": default_profile,
            "profiles": profiles,
        }
    return ForgeConfig.model_validate(body)


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    """A config whose ``fix-journey`` profile is capped — the law is a gate."""
    return make_config(
        repo_root,
        profiles={
            "attended": {},
            FIX_JOURNEY_PROFILE_NAME: {"max_review_cycles": 2},
        },
    )


class Publisher:
    """Stands in for the wire: every envelope, in order, and never a socket."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._fail_with = fail_with

    async def __call__(self, subject: str, body: bytes) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.published.append((subject, body))

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(body)["payload"] for _, body in self.published]


def seed_failed_build(
    pool: SqliteLifecyclePersistence,
    *,
    build_id: str = SOURCE_BUILD,
    feature_id: str = FEATURE_ID,
) -> str:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, originating_user, "
        "correlation_id, queued_at, mode) VALUES (?, ?, ?, 'main', 'f.yaml', "
        "'FAILED', 'cli', 'rich', ?, '2026-09-04T13:13:28Z', 'mode-a')",
        (build_id, feature_id, REPO_KEY, f"corr-{build_id}"),
    )
    pool.connection.commit()
    return build_id


def write_fix_task(repo_root: Path, *, parent: str = FEATURE_ID) -> Path:
    return write_fix_task_yaml(
        repo_path=repo_root,
        task_id="TASK-FEAT44A8FIX1",
        parent_feature=parent,
        name="repair the build",
    )


def queue_rows(pool: SqliteLifecyclePersistence) -> list[sqlite3.Row]:
    return list(
        pool.connection.execute("SELECT * FROM work_queue ORDER BY id").fetchall()
    )


def build_rows(pool: SqliteLifecyclePersistence) -> list[sqlite3.Row]:
    return list(
        pool.connection.execute("SELECT * FROM builds ORDER BY rowid").fetchall()
    )


# ---------------------------------------------------------------------------
# The task id
# ---------------------------------------------------------------------------


class TestTheTaskId:
    def test_it_names_the_feature_and_the_repair(self) -> None:
        assert mint_fix_task_id("FEAT-44A8") == "TASK-FEAT44A8FIX1"

    def test_it_takes_the_next_free_number(self) -> None:
        assert (
            mint_fix_task_id("FEAT-44A8", existing={"TASK-FEAT44A8FIX1"})
            == "TASK-FEAT44A8FIX2"
        )

    def test_it_stays_inside_the_wire_s_twelve_characters(self) -> None:
        minted = mint_fix_task_id("FEAT-LONGNAME-THAT-GOES-ON")
        assert len(minted) <= len("TASK-") + 12
        from forge.pipeline.fix_admission import TASK_ID_REGEX

        assert TASK_ID_REGEX.match(minted)

    def test_every_minted_id_is_one_the_wire_accepts(self) -> None:
        from forge.pipeline.fix_admission import TASK_ID_REGEX

        taken: set[str] = set()
        for _ in range(9):
            minted = mint_fix_task_id("FEAT-44A8", existing=taken)
            assert TASK_ID_REGEX.match(minted), minted
            taken.add(minted)
        assert len(taken) == 9

    def test_a_feature_with_no_letters_still_mints_something_readable(self) -> None:
        assert mint_fix_task_id("----").startswith("TASK-FIX")


# ---------------------------------------------------------------------------
# The fix-task file
# ---------------------------------------------------------------------------


class TestTheFixTaskFile:
    def test_it_is_three_fields_and_lands_beside_the_features(
        self, repo_root: Path
    ) -> None:
        path = write_fix_task_yaml(
            repo_path=repo_root,
            task_id="TASK-44A8FIX1",
            parent_feature=FEATURE_ID,
            name="the build of FEAT-44A8 failed",
        )

        assert path == features_dir(repo_root) / "TASK-44A8FIX1.yaml"
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
            "id": "TASK-44A8FIX1",
            "name": "the build of FEAT-44A8 failed",
            "parent_feature": FEATURE_ID,
        }

    def test_the_features_directory_is_made_when_it_is_missing(
        self, tmp_path: Path
    ) -> None:
        fresh = tmp_path / "fresh_repo"
        fresh.mkdir()

        path = write_fix_task_yaml(
            repo_path=fresh,
            task_id="TASK-AAAAFIX1",
            parent_feature="FEAT-AAAA",
            name="x",
        )

        assert path.is_file()

    def test_what_is_already_there_is_read_back(self, repo_root: Path) -> None:
        write_fix_task(repo_root)

        assert existing_fix_task_ids(repo_root) == {"TASK-FEAT44A8FIX1"}

    def test_the_parent_feature_reads_back(self, repo_root: Path) -> None:
        path = write_fix_task(repo_root)

        assert read_parent_feature(path) == FEATURE_ID

    @pytest.mark.parametrize(
        "body, fragment",
        [
            ("name: x\n", "parent_feature"),
            ("- not a mapping\n", "must be a YAML mapping"),
            ("parent_feature: ''\n", "parent_feature"),
        ],
    )
    def test_a_spec_that_names_no_parent_is_refused_in_words(
        self, tmp_path: Path, body: str, fragment: str
    ) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(body, encoding="utf-8")

        with pytest.raises(FixAdmissionRefused) as caught:
            read_parent_feature(path)

        assert fragment in caught.value.message
        assert caught.value.reason == "fix-task-yaml"
        assert caught.value.permanent is True

    def test_a_file_that_is_not_there_is_refused_in_words(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FixAdmissionRefused) as caught:
            read_parent_feature(tmp_path / "nope.yaml")

        assert "Cannot read fix-task YAML" in caught.value.message


# ---------------------------------------------------------------------------
# THE CAP LAW on the in-process path
# ---------------------------------------------------------------------------


class TestTheCapLawInProcess:
    """Exactly the refusals ``test_mode_c_cap_law.py`` pins for the CLI."""

    def _admit(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        repo_root: Path,
        *,
        profile: str | None,
        uncapped_acknowledged: bool = False,
    ) -> Any:
        return asyncio.run(
            admit_fix_build(
                config=config,
                persistence=pool,
                task_id="TASK-44A8FIX1",
                fix_task_yaml=write_fix_task(repo_root),
                repo_path=repo_root,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                publish=Publisher(),
                profile=profile,
                uncapped_acknowledged=uncapped_acknowledged,
            )
        )

    def test_an_uncapped_profile_refuses_and_writes_nothing(
        self, repo_root: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        config = make_config(repo_root)  # in-code defaults; 'attended' = no caps

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, profile="attended")

        assert caught.value.reason == "cap"
        assert caught.value.permanent is True
        assert build_rows(pool) == []

    def test_the_production_shape_an_absent_fix_journey_block(
        self, repo_root: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        """The deployed forge.yaml spells out profiles and omits fix-journey."""
        config = make_config(
            repo_root,
            profiles={"attended": {}, "unattended": {"max_review_cycles": 2}},
        )

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, profile=FIX_JOURNEY_PROFILE_NAME)

        assert caught.value.reason == "cap"
        assert caught.value.permanent is True
        assert "unknown budget profile" in caught.value.message
        assert build_rows(pool) == []

    def test_a_cap_of_one_is_refused_as_the_trap_it_is(
        self, repo_root: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        config = make_config(
            repo_root,
            profiles={"attended": {}, "too-tight": {"max_review_cycles": 1}},
        )

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, profile="too-tight")

        assert caught.value.reason == "cap"
        assert caught.value.permanent is True

    def test_a_capped_profile_opens_the_journey(
        self, config: ForgeConfig, repo_root: Path, pool: SqliteLifecyclePersistence
    ) -> None:
        """The law is a gate, not a wall."""
        admission = self._admit(
            config, pool, repo_root, profile=FIX_JOURNEY_PROFILE_NAME
        )

        assert admission.feature_id == FEATURE_ID
        rows = build_rows(pool)
        assert len(rows) == 1
        assert rows[0]["mode"] == BuildMode.MODE_C.value
        assert rows[0]["profile"] == FIX_JOURNEY_PROFILE_NAME
        assert rows[0]["task_id"] == "TASK-44A8FIX1"


# ---------------------------------------------------------------------------
# The other refusals, and write-then-publish
# ---------------------------------------------------------------------------


class TestTheOtherRefusals:
    def _admit(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        repo_root: Path,
        *,
        task_id: str = "TASK-44A8FIX1",
        repo_path: Path | None = None,
        publisher: Publisher | None = None,
        fix_task_yaml: Path | None = None,
    ) -> Any:
        return asyncio.run(
            admit_fix_build(
                config=config,
                persistence=pool,
                task_id=task_id,
                fix_task_yaml=fix_task_yaml or write_fix_task(repo_root),
                repo_path=repo_path or repo_root,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                publish=publisher or Publisher(),
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

    def test_a_subject_that_is_not_a_task_id_is_refused(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, task_id="FEAT-44A8")

        assert caught.value.reason == "task-id"
        assert caught.value.permanent is True
        assert "Mode C requires positional argument to match" in caught.value.message
        assert build_rows(pool) == []

    def test_a_parent_feature_with_traversal_is_refused(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        bad = write_fix_task_yaml(
            repo_path=repo_root,
            task_id="TASK-44A8FIX9",
            parent_feature="../etc/passwd",
            name="x",
        )

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, fix_task_yaml=bad)

        assert caught.value.reason == "parent-feature"
        assert caught.value.permanent is True
        assert "Invalid parent_feature" in caught.value.message
        assert build_rows(pool) == []

    def test_a_repository_outside_the_allowlist_is_refused(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path,
        tmp_path: Path,
    ) -> None:
        elsewhere = tmp_path / "somewhere_else"
        elsewhere.mkdir()

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root, repo_path=elsewhere)

        assert caught.value.reason == "repo-not-allowed"
        assert caught.value.permanent is True
        assert build_rows(pool) == []

    def test_an_active_build_for_the_same_feature_is_refused(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        now = datetime.now(UTC)
        pool.record_pending_build(
            BuildQueuedPayload(
                feature_id=FEATURE_ID,
                repo=REPO_KEY,
                feature_yaml_path="f.yaml",
                triggered_by="cli",
                correlation_id="already-running",
                requested_at=now,
                queued_at=now,
            )
        )

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, repo_root)

        assert caught.value.reason == "duplicate"
        assert caught.value.permanent is False

    def test_a_publish_that_fails_leaves_the_row_alone(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        """SQLite is the pipeline's truth; the reconciler redrives the row."""
        publisher = Publisher(fail_with=RuntimeError("broker unreachable"))

        with pytest.raises(FixPublishFailed) as caught:
            self._admit(config, pool, repo_root, publisher=publisher)

        assert "NOT NOTIFIED" in caught.value.message
        assert len(build_rows(pool)) == 1
        assert caught.value.admission.build_id


# ---------------------------------------------------------------------------
# What rides on the wire, and the link back to the failed build
# ---------------------------------------------------------------------------


class TestTheSourceBuildReachesThePackReader:
    def test_the_build_carries_the_failed_build_in_its_correlation_id(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        seed_failed_build(pool, feature_id="FEAT-9999")  # a different feature
        publisher = Publisher()

        admission = asyncio.run(
            admit_fix_build(
                config=config,
                persistence=pool,
                task_id="TASK-44A8FIX1",
                fix_task_yaml=write_fix_task(repo_root),
                repo_path=repo_root,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                publish=publisher,
                profile=FIX_JOURNEY_PROFILE_NAME,
                source_build_id=SOURCE_BUILD,
            )
        )

        # THE SEAM THE CONDUCTOR COMPOSITION WAS MISSING: the reader wired at
        # ``cli/serve.py`` answers with the build whose pack the journey must
        # read, and it answers it for THIS build.
        read = make_failure_pack_source_reader(pool)
        assert read(admission.build_id) == SOURCE_BUILD

    def test_the_wire_carries_the_task_and_the_parent_feature(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        publisher = Publisher()

        asyncio.run(
            admit_fix_build(
                config=config,
                persistence=pool,
                task_id="TASK-44A8FIX1",
                fix_task_yaml=write_fix_task(repo_root),
                repo_path=repo_root,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                publish=publisher,
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

        assert len(publisher.published) == 1
        subject, _ = publisher.published[0]
        assert subject == f"pipeline.build-queued.{FEATURE_ID}"
        payload = publisher.payloads[0]
        assert payload["task_id"] == "TASK-44A8FIX1"
        assert payload["feature_id"] == FEATURE_ID
        assert payload["mode"] == BuildMode.MODE_C.value
        assert payload["correlation_id"] == fix_correlation_id(SOURCE_BUILD)


# ---------------------------------------------------------------------------
# The queue's door: a row in, a journey out
# ---------------------------------------------------------------------------


class TestAdmittingAQueueRow:
    def _file_fix_row(
        self, store: WorkQueueStore, *, repo: str | None = REPO_KEY
    ) -> int:
        return store.file_sentence(
            correlation_id=fix_correlation_id(SOURCE_BUILD),
            sentence="The build of FEAT-44A8 in api_test failed: gates red",
            originating_user="rich",
            target_repo=repo,
            kind="fix",
            action="minted",
        ).queue_id

    def test_a_row_becomes_a_task_a_file_and_a_build(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        seed_failed_build(pool)
        queue_id = self._file_fix_row(store)
        publisher = Publisher()

        admission = asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence="The build of FEAT-44A8 in api_test failed: gates red",
                target_repo=REPO_KEY,
                publish=publisher,
                originating_user="rich",
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

        assert admission.task_id == "TASK-FEAT44A8FIX1"
        assert admission.source_build_id == SOURCE_BUILD
        written = features_dir(repo_root) / "TASK-FEAT44A8FIX1.yaml"
        assert yaml.safe_load(written.read_text(encoding="utf-8")) == {
            "id": "TASK-FEAT44A8FIX1",
            "name": "The build of FEAT-44A8 in api_test failed: gates red",
            "parent_feature": FEATURE_ID,
        }
        assert len(publisher.published) == 1
        assert make_failure_pack_source_reader(pool)(admission.build_id) == (
            SOURCE_BUILD
        )

    def test_the_row_records_which_build_it_opened(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        seed_failed_build(pool)
        queue_id = self._file_fix_row(store)

        admission = asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence="repair it",
                target_repo=REPO_KEY,
                publish=Publisher(),
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

        recorded = [
            json.loads(str(row["details_json"]))
            for row in store.list_events(queue_id)
            if row["action"] == "admitted_build"
        ]
        assert recorded == [
            {
                "build_id": admission.build_id,
                "task_id": "TASK-FEAT44A8FIX1",
                "feature_id": FEATURE_ID,
                "source_build_id": SOURCE_BUILD,
                "fix_task_path": admission.fix_task_path,
            }
        ]

    def test_a_second_repair_of_the_same_feature_gets_the_next_number(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        seed_failed_build(pool)
        write_fix_task(repo_root)  # TASK-FEAT44A8FIX1 already exists
        queue_id = self._file_fix_row(store)

        admission = asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence="repair it",
                target_repo=REPO_KEY,
                publish=Publisher(),
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

        assert admission.task_id == "TASK-FEAT44A8FIX2"

    def test_a_repository_the_forge_does_not_know_is_refused_in_words(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
    ) -> None:
        seed_failed_build(pool)
        queue_id = self._file_fix_row(store, repo="nowhere/at-all")

        with pytest.raises(FixAdmissionRefused) as caught:
            asyncio.run(
                admit_fix_row(
                    config=config,
                    persistence=pool,
                    store=store,
                    queue_id=queue_id,
                    correlation_id=fix_correlation_id(SOURCE_BUILD),
                    sentence="repair it",
                    target_repo="nowhere/at-all",
                    publish=Publisher(),
                    profile=FIX_JOURNEY_PROFILE_NAME,
                )
            )

        assert caught.value.reason == "repo-unknown"
        assert caught.value.permanent is True
        assert "I don't know a repository called" in caught.value.message

    def test_a_row_whose_failed_build_is_gone_is_refused_in_words(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
    ) -> None:
        queue_id = self._file_fix_row(store)  # no builds row seeded

        with pytest.raises(FixAdmissionRefused) as caught:
            asyncio.run(
                admit_fix_row(
                    config=config,
                    persistence=pool,
                    store=store,
                    queue_id=queue_id,
                    correlation_id=fix_correlation_id(SOURCE_BUILD),
                    sentence="repair it",
                    target_repo=REPO_KEY,
                    publish=Publisher(),
                    profile=FIX_JOURNEY_PROFILE_NAME,
                )
            )

        assert caught.value.reason == "no-source-build"
        assert caught.value.permanent is True
        assert build_rows(pool) == []

    def test_a_row_that_names_no_failed_build_is_refused_in_words(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
    ) -> None:
        queue_id = store.file_sentence(
            correlation_id="typed-by-hand",
            sentence="repair something",
            originating_user="rich",
            target_repo=REPO_KEY,
            kind="fix",
        ).queue_id

        with pytest.raises(FixAdmissionRefused) as caught:
            asyncio.run(
                admit_fix_row(
                    config=config,
                    persistence=pool,
                    store=store,
                    queue_id=queue_id,
                    correlation_id="typed-by-hand",
                    sentence="repair something",
                    target_repo=REPO_KEY,
                    publish=Publisher(),
                    profile=FIX_JOURNEY_PROFILE_NAME,
                )
            )

        assert caught.value.reason == "no-source-build"
        assert caught.value.permanent is True


# ---------------------------------------------------------------------------
# Never a planning run
# ---------------------------------------------------------------------------


class TestARepairIsNeverAPlanningRun:
    def test_admitting_a_repair_creates_no_planning_run(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        seed_failed_build(pool)
        queue_id = store.file_sentence(
            correlation_id=fix_correlation_id(SOURCE_BUILD),
            sentence="repair it",
            originating_user="rich",
            target_repo=REPO_KEY,
            kind="fix",
        ).queue_id

        asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence="repair it",
                target_repo=REPO_KEY,
                publish=Publisher(),
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )

        runs = pool.connection.execute(
            "SELECT COUNT(*) FROM planning_runs"
        ).fetchone()
        assert runs[0] == 0
