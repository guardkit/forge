"""The routine build's merge card carries the scope line, and the receipt is
written before anyone is asked to say merge.

Driven, not mocked: the scope pass runs over a real git repository with a real
plan of record on main and a real build branch off it, and the sentence it is
held against is read out of a real planning row in a real database — the
routine build's own correlation id, and a repair build's parent row in one hop.

The design of record is
``ai-transition/docs/planner-fix-design-2026-09-15.md`` §2e and §2f.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.events import BuildCompletePayload

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_offer import (
    MERGE_OFFER_DETAILS_KEY,
    MERGE_OFFER_TARGET_IDENTIFIER,
    MergeOfferService,
    request_behind_the_build,
    run_the_scope_pass,
)
from forge.pipeline.scope_report import SCOPE_REPORT_NAME, ScopeReport

FEATURE_ID = "FEAT-SCP1"
BUILD_ID = "build-FEAT-SCP1-20260915"
REPO = "appmilla/api_test"
CORRELATION = "corr-scope-1"
BRANCH = f"autobuild/{FEATURE_ID}"

REQUEST = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


def _insert_build(
    pool: SqliteLifecyclePersistence,
    *,
    build_id: str = BUILD_ID,
    feature_id: str = FEATURE_ID,
    correlation_id: str = CORRELATION,
) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-09-15T00:00:00Z', 'mode-a')",
        (build_id, feature_id, REPO, f"autobuild/{feature_id}", correlation_id),
    )
    pool.connection.commit()


def _insert_planning_run(
    pool: SqliteLifecyclePersistence,
    *,
    correlation_id: str = CORRELATION,
    request_text: str = REQUEST,
) -> None:
    pool.connection.execute(
        "INSERT INTO planning_runs (correlation_id, state, originating_user, "
        "expected_approver, request_text, target_repo, triggered_by, "
        "originating_adapter, parent_request_id, queued_at) VALUES "
        "(?, 'QUEUED', 'rich', 'rich', ?, ?, 'jarvis', 'slack', NULL, "
        "'2026-09-15T00:00:00Z')",
        (correlation_id, request_text, REPO),
    )
    pool.connection.commit()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A repository whose plan names two files and whose build changed four."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    task = "tasks/backlog/daily-counts/TASK-SCP1-001.md"
    _write(
        root,
        f".guardkit/features/{FEATURE_ID}.yaml",
        f'id: {FEATURE_ID}\ntasks:\n  - id: TASK-SCP1-001\n    file_path: "{task}"\n',
    )
    _write(
        root,
        task,
        "---\nid: TASK-SCP1-001\n---\n\nAdd the endpoint.\n\n"
        "## Files to Create\n\n- _none_\n\n"
        "## Files to Modify\n\n- `src/users/router.py`\n",
    )
    _write(root, "src/users/router.py", "# the users router\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the plan of record")
    _git(root, "checkout", "-q", "-b", BRANCH)
    _write(
        root,
        "src/users/router.py",
        "# the users router\n@router.get('/stats/users-created-per-day')\n"
        "def counts():\n    return []\n",
    )
    _write(root, "src/analytics/service.py", "def build_counts():\n    return []\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "what the build wrote")
    return root


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "merge_executor": {"enabled": True},
        }
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def raw_publish(self, subject: str, body: bytes) -> None:
        self.events.append(("approval", (subject, body)))

    async def publish_build_paused(self, payload: Any) -> None:
        self.events.append(("paused", payload))

    @property
    def paused(self) -> Any:
        return [payload for kind, payload in self.events if kind == "paused"][0]


def _service(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    recorder: _Recorder,
    **kwargs: Any,
) -> MergeOfferService:
    async def _git_head(_repo_root: Path) -> str | None:
        return "mainsha1234"

    return MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(
            publish_build_paused=recorder.publish_build_paused
        ),
        raw_publish=recorder.raw_publish,
        git_head=_git_head,
        **kwargs,
    )


def _event(build_id: str = BUILD_ID) -> BuildCompletePayload:
    return BuildCompletePayload(
        feature_id=FEATURE_ID,
        build_id=build_id,
        tasks_completed=5,
        tasks_failed=0,
        tasks_total=5,
        duration_seconds=10,
        summary="done",
        status="COMPLETE",
    )


class TestTheSentenceBehindTheBuild:
    def test_a_routine_build_finds_it_through_its_own_correlation_id(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _insert_build(pool)
        _insert_planning_run(pool)
        row = pool.get_build_row(BUILD_ID)
        request, source, why_not = request_behind_the_build(pool, row)
        assert request == REQUEST
        assert source == "planning_runs.request_text via builds.correlation_id"
        assert why_not is None

    def test_a_repair_build_resolves_through_its_parent_in_one_hop(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _insert_build(pool)
        _insert_planning_run(pool)
        _insert_build(
            pool,
            build_id="build-FEAT-SCP1-repair",
            correlation_id=f"fix-{BUILD_ID}",
        )
        row = pool.get_build_row("build-FEAT-SCP1-repair")
        request, source, why_not = request_behind_the_build(pool, row)
        assert request == REQUEST
        assert source == (
            "planning_runs.request_text via the parent build's correlation_id"
        )
        assert why_not is None

    def test_no_planning_row_is_said_plainly_and_never_guessed(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _insert_build(pool)
        row = pool.get_build_row(BUILD_ID)
        request, source, why_not = request_behind_the_build(pool, row)
        assert request is None
        assert source is None
        assert why_not is not None
        assert CORRELATION in why_not


class TestTheScopePassOnARealBranch:
    def test_it_reads_the_branch_the_plan_and_the_sentence(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _insert_build(pool)
        _insert_planning_run(pool)
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID).mkdir(parents=True)
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))

        report = run_the_scope_pass(
            config=config,
            pool=pool,
            build_id=BUILD_ID,
            feature_id=FEATURE_ID,
            row=pool.get_build_row(BUILD_ID),
        )
        assert report is not None
        assert report.read is True
        assert report.plan_read is True
        assert report.request == REQUEST
        assert report.files_the_plan_named == ["src/users/router.py"]
        assert report.files_the_plan_did_not_name == ["src/analytics/service.py"]
        assert report.routes_the_request_did_not_name == [
            "/stats/users-created-per-day"
        ]

        kept = json.loads(
            (receipts / BUILD_ID / SCOPE_REPORT_NAME).read_text(encoding="utf-8")
        )
        assert kept["request"] == REQUEST
        assert kept["files_changed"] == 2
        assert kept["routes_the_request_did_not_name"] == [
            "/stats/users-created-per-day"
        ]

    def test_a_repository_forge_cannot_locate_counts_nothing(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        _insert_build(pool)
        empty = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {}},
                "merge_executor": {"enabled": True},
            }
        )
        assert (
            run_the_scope_pass(
                config=empty,
                pool=pool,
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                row=pool.get_build_row(BUILD_ID),
            )
            is None
        )


class TestTheCardAndTheDurableRow:
    def test_the_card_says_what_went_beyond_the_plan_and_the_request(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _insert_build(pool)
        _insert_planning_run(pool)
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID).mkdir(parents=True)
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))
        recorder = _Recorder()

        asyncio.run(_service(config, pool, recorder).maybe_offer(_event()))

        words = recorder.paused.rationale
        assert words.startswith(f"{FEATURE_ID} built clean — 5 of 5 tasks passed. ")
        assert (
            "This build also changed 1 file the plan did not name: "
            "src/analytics/service.py — worth a look before you merge." in words
        )
        assert (
            "It also answers at a web address the request did not name: "
            "/stats/users-created-per-day." in words
        )
        assert words.endswith(
            "Approve = merge into main, deploy to the sandbox and run the "
            "checks; the branch is kept either way. Reject = nothing changes."
        )

    def test_the_report_rides_on_the_durable_row(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _insert_build(pool)
        _insert_planning_run(pool)
        receipts = tmp_path / "receipts"
        (receipts / BUILD_ID).mkdir(parents=True)
        monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(receipts))

        asyncio.run(_service(config, pool, _Recorder()).maybe_offer(_event()))

        offered = [
            stage
            for stage in pool.read_stages(BUILD_ID)
            if stage.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
        ]
        assert len(offered) == 1
        kept = offered[0].details[MERGE_OFFER_DETAILS_KEY]["scope_report"]
        assert kept["read"] is True
        assert kept["files_the_plan_did_not_name"] == ["src/analytics/service.py"]

    def test_a_pass_nobody_could_take_leaves_the_card_as_it_was(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
    ) -> None:
        """The seam answering ``None`` is "nobody counted", and the card is
        then byte for byte the card that shipped before the scope pass."""
        _insert_build(pool)
        recorder = _Recorder()

        def _nobody_counted(**_kwargs: Any) -> None:
            return None

        asyncio.run(
            _service(config, pool, recorder, scope_pass=_nobody_counted).maybe_offer(
                _event()
            )
        )
        assert recorder.paused.rationale == (
            f"{FEATURE_ID} built clean — 5 of 5 tasks passed. Approve = merge "
            "into main, deploy to the sandbox and run the checks; the branch "
            "is kept either way. Reject = nothing changes."
        )

    def test_a_scope_pass_that_raises_never_costs_the_card(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
    ) -> None:
        _insert_build(pool)
        recorder = _Recorder()

        def _it_blows_up(**_kwargs: Any) -> ScopeReport:
            raise RuntimeError("the reader fell over")

        asyncio.run(
            _service(config, pool, recorder, scope_pass=_it_blows_up).maybe_offer(
                _event()
            )
        )
        assert "built clean" in recorder.paused.rationale
        assert "could not be read here" not in recorder.paused.rationale
