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
- **The task file rides a repair branch** (rewrite-on-refusal spec Part L,
  rules 48, 50 and 52). The admission commits a task file in the
  repository's own frontmatter shape, and the YAML beside it, on
  ``repair/<task id>`` cut from main; the build is queued on that branch;
  the shared checkout stays exactly as it was; a second admission adds no
  commit; the worktree the review leg actually gets — the conductor's
  writer run against the admitted build — is cut from the repair branch
  and carries the file, and guardkit's own loader finds it there; a write
  that fails refuses cleanly.

No broker: the publisher is a list. No live database: SQLite under
``tmp_path``. The fixture repository is a real temporary git checkout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from nats_core.events import BuildQueuedPayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._conductor_worktree import (
    WorktreeReady,
    journey_branch_name,
    prepare_journey_worktree,
)
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
    republish_build_queued,
    write_fix_task_yaml,
)
from forge.pipeline.fix_row_producer import (
    fix_correlation_id,
    make_failure_pack_source_reader,
)
from forge.pipeline.repair_branch import (
    branch_exists,
    find_task_file_on_branch,
    repair_worktree_path,
)
from forge.planning.work_queue_store import WorkQueueStore

from ._repair_repo import (
    branches,
    commit_count,
    git,
    head,
    isolate_git,
    make_feature_repo,
    porcelain_hash,
    show,
    worktrees,
)

FEATURE_ID = "FEAT-44A8"
REPO_KEY = "appmilla_github/api_test"
SOURCE_BUILD = "build-FEAT-44A8-20260904131328"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _git_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_git(monkeypatch)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A real checkout on ``main`` with FEAT-44A8 merged and its task folder."""
    return make_feature_repo(tmp_path / "api_test")


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
        assert admission.branch == "repair/TASK-FEAT44A8FIX1"
        # The YAML lives on the repair branch beside the task file, never in
        # the shared checkout's working tree (Part L, rules 48 and 50).
        written = show(
            repo_root, admission.branch, ".guardkit/features/TASK-FEAT44A8FIX1.yaml"
        )
        assert yaml.safe_load(written) == {
            "id": "TASK-FEAT44A8FIX1",
            "name": "The build of FEAT-44A8 in api_test failed: gates red",
            "parent_feature": FEATURE_ID,
        }
        assert not (features_dir(repo_root) / "TASK-FEAT44A8FIX1.yaml").exists()
        assert admission.fix_task_path == str(
            features_dir(repo_root) / "TASK-FEAT44A8FIX1.yaml"
        )
        assert publisher.payloads[0]["branch"] == "repair/TASK-FEAT44A8FIX1"
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
                "branch": "repair/TASK-FEAT44A8FIX1",
                "task_file_path": (
                    "tasks/backlog/add-the-thing/TASK-FEAT44A8FIX1-repair.md"
                ),
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


# ---------------------------------------------------------------------------
# Saying a written-but-never-announced build's event again
# ---------------------------------------------------------------------------


class TestSayingTheQueuedEventAgain:
    """A publish that failed left a real build nobody was told about.

    The row is deliberately kept, so the event has to be sayable again from
    the row itself. It must be the SAME event: same subject, same
    correlation id, same feature, same fix-task file, same task, mode-c.
    """

    def _write_the_row_and_lose_the_publish(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        repo_root: Path,
    ) -> sqlite3.Row:
        seed_failed_build(pool)
        with pytest.raises(FixPublishFailed):
            asyncio.run(
                admit_fix_build(
                    config=config,
                    persistence=pool,
                    task_id="TASK-44A8FIX1",
                    fix_task_yaml=write_fix_task(repo_root),
                    repo_path=repo_root,
                    correlation_id=fix_correlation_id(SOURCE_BUILD),
                    publish=Publisher(fail_with=RuntimeError("broker unreachable")),
                    profile=FIX_JOURNEY_PROFILE_NAME,
                )
            )
        written = [row for row in build_rows(pool) if row["build_id"] != SOURCE_BUILD]
        assert len(written) == 1
        assert written[0]["status"] == "QUEUED"
        return written[0]

    def test_the_event_said_again_is_the_event_that_was_lost(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        row = self._write_the_row_and_lose_the_publish(config, pool, repo_root)
        publisher = Publisher()

        subject = asyncio.run(republish_build_queued(row, publish=publisher))

        assert subject == f"pipeline.build-queued.{FEATURE_ID}"
        assert [s for s, _ in publisher.published] == [subject]
        payload = publisher.payloads[0]
        assert payload["feature_id"] == FEATURE_ID
        assert payload["correlation_id"] == fix_correlation_id(SOURCE_BUILD)
        assert payload["task_id"] == "TASK-44A8FIX1"
        assert payload["mode"] == BuildMode.MODE_C.value
        assert payload["repo"] == row["repo"]
        assert payload["feature_yaml_path"] == row["feature_yaml_path"]
        # It parses as the wire's own payload, which is the real assertion:
        # a mode-c event with no task id would be refused by the validator.
        assert BuildQueuedPayload.model_validate(payload).task_id == "TASK-44A8FIX1"

    def test_saying_it_again_writes_no_second_build(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        row = self._write_the_row_and_lose_the_publish(config, pool, repo_root)
        before = len(build_rows(pool))

        asyncio.run(republish_build_queued(row, publish=Publisher()))

        assert len(build_rows(pool)) == before

    def test_a_publish_that_fails_again_is_not_swallowed(
        self, config: ForgeConfig, pool: SqliteLifecyclePersistence, repo_root: Path
    ) -> None:
        row = self._write_the_row_and_lose_the_publish(config, pool, repo_root)

        with pytest.raises(RuntimeError, match="broker unreachable"):
            asyncio.run(
                republish_build_queued(
                    row, publish=Publisher(fail_with=RuntimeError("broker unreachable"))
                )
            )


# ---------------------------------------------------------------------------
# The task file on the repair branch (Part L, rules 48, 50 and 52)
# ---------------------------------------------------------------------------

REPAIR_BRANCH = "repair/TASK-FEAT44A8FIX1"
TASK_FILE = "tasks/backlog/add-the-thing/TASK-FEAT44A8FIX1-repair.md"
YAML_FILE = ".guardkit/features/TASK-FEAT44A8FIX1.yaml"
MERGE_SENTENCE = (
    "FEAT-44A8 was merged in appmilla_github/api_test but the checks after it "
    "went red: merged-deploy-failed — FEAT-44A8 merged, but the deploy ended "
    "failed"
)

EVIDENCE_TEXT = """format_version: '1.0'
entries:
- artifact: qa/gates/evidence/health_latest.json
  checkpoint_or_assertion_id: health::status
  description: 'health/health::status [pass]: expected ''200'', observed ''200'''
  inspected_by: null
  verdict: null
- artifact: qa/gates/evidence/stats_latest.json
  checkpoint_or_assertion_id: stats::status
  description: 'stats/stats::status [pass]: expected ''200'', observed ''200'''
  inspected_by: null
  verdict: null
- artifact: qa/gates/evidence/hurl-twins_latest.json
  checkpoint_or_assertion_id: hurl-twins::delete-existing-user::40
  description: 'hurl-twins/hurl-twins::delete-existing-user::40 [fail]: expected ''HTTP
    204'', observed ''actual value is <503>'''
  inspected_by: null
  verdict: null
- artifact: qa/gates/evidence/hurl-twins_latest.json
  checkpoint_or_assertion_id: hurl-twins::double-delete-honest-404::35
  description: 'hurl-twins/hurl-twins::double-delete-honest-404::35 [fail]: expected
    ''HTTP 204'', observed ''actual value is <503>'''
  inspected_by: null
  verdict: null
"""


def _frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    assert text.startswith("---\n"), text[:40]
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end]), text[end + 4 :]


def _write_merge_report(receipts_root: Path, *, source: str = SOURCE_BUILD) -> Path:
    where = receipts_root / f"merge-{source}"
    where.mkdir(parents=True)
    path = where / "merge_deploy_report.json"
    path.write_text(
        json.dumps(
            {
                "build_id": source,
                "feature_id": FEATURE_ID,
                "result": "merged-deploy-failed",
                "detail": (
                    "FEAT-44A8 merged, but the deploy ended failed — nothing "
                    "further was touched"
                ),
                "failed_step": "deploy",
                "merged_sha": "9131bc6b495a489921ab22aeabb71fa477a16cbd",
                "checks_passed": None,
                "checks_total": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_gate_evidence(repo_root: Path) -> Path:
    run = repo_root / "qa" / "gates" / "evidence" / f"{FEATURE_ID}-local-20260907T083219Z"
    run.mkdir(parents=True)
    path = run / "EVIDENCE.yaml"
    path.write_text(EVIDENCE_TEXT, encoding="utf-8")
    return path


class TestTheRepairTaskFile:
    def _file_merge_row(self, store: WorkQueueStore, *, pack: str | None = None) -> int:
        return store.file_sentence(
            correlation_id=fix_correlation_id(SOURCE_BUILD),
            sentence=MERGE_SENTENCE,
            originating_user="rich",
            target_repo=REPO_KEY,
            kind="fix",
            action="minted",
            extra_details={
                "source": "merge-report",
                "source_build_id": SOURCE_BUILD,
                "failure_pack_path": pack,
            },
        ).queue_id

    def _admit(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        queue_id: int,
        *,
        receipts_root: Path | None = None,
        publisher: Publisher | None = None,
    ) -> Any:
        return asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence=MERGE_SENTENCE,
                target_repo=REPO_KEY,
                publish=publisher or Publisher(),
                originating_user="rich",
                profile=FIX_JOURNEY_PROFILE_NAME,
                receipts_root=receipts_root,
            )
        )

    def test_the_file_is_committed_on_the_repair_branch_in_the_repository_s_shape(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
        tmp_path: Path,
    ) -> None:
        seed_failed_build(pool)
        receipts = tmp_path / "receipts"
        report = _write_merge_report(receipts)
        _write_gate_evidence(repo_root)
        queue_id = self._file_merge_row(store, pack=None)
        publisher = Publisher()

        admission = self._admit(
            config, pool, store, queue_id, receipts_root=receipts, publisher=publisher
        )

        assert admission.branch == REPAIR_BRANCH
        assert admission.task_file_path == TASK_FILE
        assert branch_exists(repo_root, REPAIR_BRANCH)
        assert admission.repair_commit == head(repo_root, REPAIR_BRANCH)
        assert commit_count(repo_root, REPAIR_BRANCH) == commit_count(repo_root, "main") + 1
        assert find_task_file_on_branch(repo_root, REPAIR_BRANCH, admission.task_id) == TASK_FILE
        assert git(repo_root, "log", "-1", "--format=%s", REPAIR_BRANCH).stdout.strip() == (
            f"repair task for {SOURCE_BUILD}: {MERGE_SENTENCE}"
        )

        front, body = _frontmatter_and_body(show(repo_root, REPAIR_BRANCH, TASK_FILE))
        assert front == {
            "id": "TASK-FEAT44A8FIX1",
            "title": f"Repair of {SOURCE_BUILD}",
            "task_type": "fix",
            "parent_review": "TASK-REV-44A8",
            "feature_id": FEATURE_ID,
            "wave": 1,
            "implementation_mode": "task-work",
            "complexity": 3,
            "dependencies": [],
        }
        assert f"# Repair of {SOURCE_BUILD}\n\n{MERGE_SENTENCE}\n" in body
        assert "## What was observed" in body
        assert (
            "- Result: merged-deploy-failed — FEAT-44A8 merged, but the deploy "
            "ended failed — nothing further was touched"
        ) in body
        assert (
            f"- Source build: {SOURCE_BUILD} (feature FEAT-44A8, merged commit "
            "9131bc6b495a)"
        ) in body
        assert "- Checks: 2 of 4 passed" in body
        assert (
            "- These checks failed:\n"
            "  - hurl-twins::delete-existing-user::40: expected HTTP 204, "
            "observed actual value is <503>\n"
            "  - hurl-twins::double-delete-honest-404::35: expected HTTP 204, "
            "observed actual value is <503>\n"
        ) in body
        assert "## Where the evidence is" in body
        assert f"- Merge report: {report}" in body
        assert (
            "- Gate evidence: qa/gates/evidence/FEAT-44A8-local-20260907T083219Z/"
            "EVIDENCE.yaml"
        ) in body
        assert "- Failure pack: none was recorded for this build" in body
        assert "## Acceptance Criteria" in body
        assert (
            "- [ ] The failed checks pass: hurl-twins::delete-existing-user::40, "
            "hurl-twins::double-delete-honest-404::35\n"
            "- [ ] The feature's existing tests stay green\n"
        ) in body
        assert "## Implementation Notes" in body
        assert (
            f"- This task and its YAML are committed on the branch {REPAIR_BRANCH}, "
            "cut from main; the fix journey's own branch is cut from there, so "
            "both files are in its worktree."
        ) in body

        # The YAML rides the branch too, in the drive-6 shape.
        assert yaml.safe_load(show(repo_root, REPAIR_BRANCH, YAML_FILE)) == {
            "id": "TASK-FEAT44A8FIX1",
            "name": MERGE_SENTENCE,
            "parent_feature": FEATURE_ID,
        }
        # And the build was queued on the branch.
        assert publisher.payloads[0]["branch"] == REPAIR_BRANCH
        assert build_rows(pool)[-1]["branch"] == REPAIR_BRANCH

    def test_the_shared_checkout_is_untouched_throughout(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        seed_failed_build(pool)
        (repo_root / "scratch.txt").write_text("an operator's own untracked file\n")
        queue_id = self._file_merge_row(store)
        status_before = porcelain_hash(repo_root)
        head_before = head(repo_root)
        trees_before = worktrees(repo_root)

        self._admit(config, pool, store, queue_id)

        assert porcelain_hash(repo_root) == status_before
        assert head(repo_root) == head_before
        assert git(repo_root, "diff", "--cached", "--quiet").returncode == 0
        assert worktrees(repo_root) == trees_before
        assert not repair_worktree_path(repo_root, "TASK-FEAT44A8FIX1").exists()
        assert not (features_dir(repo_root) / "TASK-FEAT44A8FIX1.yaml").exists()

    def test_a_second_admission_of_the_same_repair_reuses_the_branch_and_adds_no_commit(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        """The branch is made before the row; a refusal at the row leaves it
        for the next tick, which must find the same task id and the same file."""
        seed_failed_build(pool)
        queue_id = self._file_merge_row(store)
        # Queued a minute ago: a build id is the feature plus the queued
        # second, so a build queued in the same second as the repair's own
        # would collide on the id and read as a duplicate for the wrong reason.
        earlier = datetime.now(UTC) - timedelta(minutes=1)
        pool.record_pending_build(
            BuildQueuedPayload(
                feature_id=FEATURE_ID,
                repo=REPO_KEY,
                feature_yaml_path="f.yaml",
                triggered_by="cli",
                correlation_id="already-running",
                requested_at=earlier,
                queued_at=earlier,
            )
        )
        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, store, queue_id)
        assert caught.value.reason == "duplicate"
        tip = head(repo_root, REPAIR_BRANCH)
        pool.connection.execute(
            "UPDATE builds SET status = 'FAILED' WHERE correlation_id = 'already-running'"
        )
        pool.connection.commit()

        admission = self._admit(config, pool, store, queue_id)

        assert admission.task_id == "TASK-FEAT44A8FIX1"
        assert admission.branch == REPAIR_BRANCH
        assert head(repo_root, REPAIR_BRANCH) == tip
        assert commit_count(repo_root, REPAIR_BRANCH) == commit_count(repo_root, "main") + 1
        assert branches(repo_root) == ["main", REPAIR_BRANCH]
        recorded = [
            json.loads(str(row["details_json"]))["task_id"]
            for row in store.list_events(queue_id)
            if row["action"] == "repair_branch"
        ]
        assert recorded == ["TASK-FEAT44A8FIX1", "TASK-FEAT44A8FIX1"]

    def test_a_feature_without_a_task_folder_gets_its_id_lower_cased(
        self,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        tmp_path: Path,
    ) -> None:
        bare = make_feature_repo(tmp_path / "bare", folder=None)
        config = make_config(
            bare,
            profiles={"attended": {}, FIX_JOURNEY_PROFILE_NAME: {"max_review_cycles": 2}},
        )
        seed_failed_build(pool)
        queue_id = self._file_merge_row(store)

        admission = self._admit(config, pool, store, queue_id)

        assert admission.task_file_path == (
            "tasks/backlog/feat-44a8/TASK-FEAT44A8FIX1-repair.md"
        )
        front, body = _frontmatter_and_body(
            show(bare, REPAIR_BRANCH, admission.task_file_path)
        )
        assert "parent_review" not in front
        assert front["feature_id"] == FEATURE_ID
        assert "- Merge report: none was found under the receipts root" in body
        assert "- Gate evidence: not recorded" in body
        assert (
            "- Why the repair was filed: the merge landed and the checks after "
            "it went red"
        ) in body
        assert "- [ ] The checks that failed pass" in body

    def test_a_write_that_fails_refuses_cleanly_and_leaves_nothing(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        """``tasks`` is a file on main, so the task folder cannot be made."""
        (repo_root / "tasks").rename(repo_root / "tasks-was-here")
        (repo_root / "tasks").write_text("not a directory\n")
        git(repo_root, "add", "-A")
        git(repo_root, "commit", "-q", "-m", "tasks is a file now")
        seed_failed_build(pool)
        queue_id = self._file_merge_row(store)
        status_before = porcelain_hash(repo_root)

        with pytest.raises(FixAdmissionRefused) as caught:
            self._admit(config, pool, store, queue_id)

        assert caught.value.reason == "repair-task"
        assert caught.value.permanent is True
        assert caught.value.message.startswith(
            "Nothing was queued: the repair's task file could not be put on a "
            "repair branch ("
        )
        assert caught.value.message.endswith(
            "), and the review leg cannot find a repair task without its file."
        )
        assert branches(repo_root) == ["main"]
        assert not repair_worktree_path(repo_root, "TASK-FEAT44A8FIX1").exists()
        assert worktrees(repo_root) == [str(repo_root.resolve())]
        assert porcelain_hash(repo_root) == status_before
        assert [row for row in build_rows(pool) if row["build_id"] != SOURCE_BUILD] == []


# ---------------------------------------------------------------------------
# guardkit's own loader, against a detached worktree of the branch (rule 52)
# ---------------------------------------------------------------------------

_LOADER_SCRIPT = """
import json, sys
from pathlib import Path
from guardkit.tasks.task_loader import TaskLoader
task = TaskLoader.load_task(sys.argv[1], repo_root=Path(sys.argv[2]))
print("LOADED " + json.dumps({
    "frontmatter": task["frontmatter"],
    "acceptance_criteria": task["acceptance_criteria"],
    "file_path": str(task["file_path"]),
}, default=str))
"""


def _guardkit_loader_or_skip() -> tuple[Path, str]:
    """``(checkout, python)`` that can import guardkit's real TaskLoader, or skip."""
    from tests.forge.planning._live_guardkit import (
        find_sibling_checkout,
        live_guardkit_python,
    )

    start = Path(__file__)
    env = os.environ.get("FORGE_GUARDKIT_NORMALIZER_CHECKOUT")
    candidates = [Path(env)] if env else []
    candidates.append(find_sibling_checkout("guardkit", start))
    checkout = next(
        (c for c in candidates if (c / "guardkit" / "tasks" / "task_loader.py").is_file()),
        None,
    )
    if checkout is None:
        pytest.skip("no guardkit checkout with tasks/task_loader.py reachable")
    python = live_guardkit_python(checkout, start)
    probe = subprocess.run(
        [python, "-c", "import guardkit.tasks.task_loader"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(checkout)},
    )
    if probe.returncode != 0:
        pytest.skip(f"no interpreter can import guardkit's loader: {probe.stderr[-300:]!r}")
    return checkout, python


class TestGuardkitsLoaderFindsTheFile:
    """Rule 48's last clause and rule 52's loader proof, on the worktree the
    review leg ACTUALLY gets.

    A mode-C build's worktree is made by the conductor's writer
    (``forge.cli._conductor_worktree.prepare_journey_worktree``, serve.py's
    default ``worktree_writer``), which cuts ``fix/<task id>-<build8>`` for the
    build. Before this lane it cut that branch from ``main`` and never read
    the row's branch, so the repair's task file was not in the tree and
    journey one refused in four seconds. Both tests here drive that writer
    against the build the admission queued — never a hand-made worktree.
    """

    @pytest.fixture
    def repo_root(self, tmp_path: Path) -> Path:
        """The live estate's shape: the checkout's last two path parts ARE the
        key the config registers (``.../appmilla_github/api_test``), because the
        conductor's writer resolves the checkout from the build row's repo slug."""
        return make_feature_repo(tmp_path / "appmilla_github" / "api_test")

    def _admit_and_materialise(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
    ) -> tuple[Any, WorktreeReady]:
        seed_failed_build(pool)
        queue_id = store.file_sentence(
            correlation_id=fix_correlation_id(SOURCE_BUILD),
            sentence=MERGE_SENTENCE,
            originating_user="rich",
            target_repo=REPO_KEY,
            kind="fix",
            action="minted",
        ).queue_id
        admission = asyncio.run(
            admit_fix_row(
                config=config,
                persistence=pool,
                store=store,
                queue_id=queue_id,
                correlation_id=fix_correlation_id(SOURCE_BUILD),
                sentence=MERGE_SENTENCE,
                target_repo=REPO_KEY,
                publish=Publisher(),
                profile=FIX_JOURNEY_PROFILE_NAME,
            )
        )
        assert admission.branch == REPAIR_BRANCH
        outcome = asyncio.run(prepare_journey_worktree(pool, config, admission.build_id))
        assert isinstance(outcome, WorktreeReady), outcome
        return admission, outcome

    def test_the_review_legs_worktree_is_cut_from_the_repair_branch_and_carries_the_file(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        status_before = porcelain_hash(repo_root)

        admission, outcome = self._admit_and_materialise(config, pool, store)

        tree = Path(outcome.path)
        assert outcome.branch == journey_branch_name(admission.task_id, admission.build_id)
        assert outcome.base_ref == REPAIR_BRANCH
        # The tree is the repair branch's tip, not main's, ...
        assert head(tree) == head(repo_root, REPAIR_BRANCH)
        assert head(tree) != head(repo_root, "main")
        # ... so the task file and the YAML are in it, where the legs look.
        assert (tree / TASK_FILE).is_file()
        assert (tree / YAML_FILE).is_file()
        assert list((tree / "tasks").rglob(f"{admission.task_id}*.md")) == [tree / TASK_FILE]
        row = pool.get_build_row(admission.build_id)
        assert row is not None and row.worktree_path == str(tree)
        # The shared checkout is still untouched: the tree lives under .forge/.
        assert porcelain_hash(repo_root) == status_before

    def test_the_real_task_loader_reads_the_file_from_that_worktree(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        store: WorkQueueStore,
        repo_root: Path,
    ) -> None:
        """Exactly what the review leg does: guardkit loads the task by id in
        the worktree the conductor's writer made for the build."""
        checkout, python = _guardkit_loader_or_skip()
        admission, outcome = self._admit_and_materialise(config, pool, store)
        tree = Path(outcome.path)

        proc = subprocess.run(
            [python, "-c", _LOADER_SCRIPT, admission.task_id, str(tree)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(checkout)},
            timeout=120,
        )

        assert proc.returncode == 0, proc.stderr[-1200:]
        line = next(line for line in proc.stdout.splitlines() if line.startswith("LOADED "))
        loaded = json.loads(line[len("LOADED "):])
        assert loaded["frontmatter"]["id"] == "TASK-FEAT44A8FIX1"
        assert loaded["frontmatter"]["task_type"] == "fix"
        assert loaded["frontmatter"]["feature_id"] == FEATURE_ID
        assert loaded["frontmatter"]["parent_review"] == "TASK-REV-44A8"
        assert loaded["frontmatter"]["implementation_mode"] == "task-work"
        assert loaded["frontmatter"]["dependencies"] == []
        assert "The feature's existing tests stay green" in loaded["acceptance_criteria"]
        assert loaded["file_path"] == str(tree / TASK_FILE)
        # Before this lane the same loader had nothing to find on main — and
        # a tree cut from main, as the writer used to make, had nothing either.
        proc_main = subprocess.run(
            [python, "-c", _LOADER_SCRIPT, admission.task_id, str(repo_root)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(checkout)},
            timeout=120,
        )
        assert proc_main.returncode != 0
        assert "not found" in (proc_main.stderr + proc_main.stdout)
