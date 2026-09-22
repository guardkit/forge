"""``forge merge-deploy`` CLI smoke — the attended merge word, seams faked.

Covers: config-required refusal, build-row resolution (newest COMPLETE
routine build; --build-id mismatch refused; nothing-to-merge refused), and
the happy path through the real executor with the NATS/guardkit/deploy seams
faked — asserting the printed receipt lines and the exit-code mapping.

The target repository is a real git repository (main plus the feature
branch) because the executor lays the branch's tree out and compares tree
ids with git before the promote (protect-main).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import merge_deploy as merge_deploy_module
from forge.cli.merge_deploy import merge_deploy_cmd
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline import merge_offer as merge_offer_module

FEATURE_ID = "FEAT-CLI1"
BUILD_ID = "build-FEAT-CLI1-20260824"
REPO = "appmilla/api_test"


@pytest.fixture(autouse=True)
def _receipts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


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
    status: str = "COMPLETE",
    mode: str = "mode-a",
    queued_at: str = "2026-08-24T00:00:00Z",
) -> None:
    # correlation is unique per build — the builds table has a UNIQUE
    # (feature_id, correlation_id) index.
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode, start_commit, target_branch) VALUES (?, ?, ?, ?, 'f.yaml', ?, "
        "'cli', ?, ?, ?, ?, 'main')",
        (
            build_id,
            feature_id,
            REPO,
            f"autobuild/{feature_id}",
            status,
            f"corr-{build_id}",
            queued_at,
            mode,
            "0" * 40,
        ),
    )
    pool.connection.commit()


class _FakePublisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    # The merge word joins onto the branch of the remote this work was
    # recorded against: a bare repository beside it stands in for one.
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    return root


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
        }
    )


def _leg_aware_deploy(dp_calls: list[dict[str, Any]], *, promote: Any = None):
    """A deploy fake that answers every leg green, unless ``promote`` is
    given — then the promote leg calls it (to raise, for example)."""

    async def _fake_deploy(**kwargs: Any) -> Any:
        dp_calls.append(kwargs)
        leg = kwargs.get("leg", "deploy")
        if leg == "candidate_check":
            return SimpleNamespace(
                outcome="complete",
                verdict="pass",
                failed_step=None,
                events=("DeployQueued",),
                detail={
                    "gate_summary": {
                        "verdict": "pass", "checks_total": 3,
                        "checks_passed": 3, "failed_checks": [],
                    },
                    "candidate": "standing",
                },
            )
        if leg == "candidate_down":
            return SimpleNamespace(outcome="complete", detail={"candidate": "torn-down"})
        if promote is not None:
            return promote(**kwargs)
        return SimpleNamespace(
            outcome="complete", verdict="pass", deploy_record_ref="r",
            detail={"candidate": "torn-down"},
        )

    return _fake_deploy


@pytest.fixture
def fakes(
    pool: SqliteLifecyclePersistence, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Rebind the CLI's module seams to offline fakes."""
    publisher = _FakePublisher()
    gk_calls: list[dict[str, Any]] = []
    dp_calls: list[dict[str, Any]] = []
    merged = _git(repo_root, "rev-parse", f"autobuild/{FEATURE_ID}")

    async def _fake_guardkit(**kwargs: Any) -> Any:
        gk_calls.append(kwargs)
        return SimpleNamespace(
            status="success",
            stdout_tail=json.dumps({"status": "merged", "merged_sha": merged}),
            stderr=None,
            exit_code=0,
            artefacts=[],
        )

    _fake_deploy = _leg_aware_deploy(dp_calls)

    async def _fake_backends(_config: ForgeConfig):
        async def _close() -> None:
            return None

        return publisher, _fake_guardkit, _fake_deploy, _close

    async def _fake_git_head(_repo_root: Path) -> str | None:
        return "e" * 40

    monkeypatch.setattr(merge_deploy_module, "_open_pool", lambda _p: pool)
    monkeypatch.setattr(merge_deploy_module, "_aopen_backends", _fake_backends)
    monkeypatch.setattr(
        merge_offer_module, "git_rev_parse_main", _fake_git_head
    )
    return {
        "publisher": publisher,
        "gk_calls": gk_calls,
        "dp_calls": dp_calls,
        "merged": merged,
    }


class TestRefusals:
    def test_no_config_refused(self) -> None:
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=None)
        assert result.exit_code != 0
        assert "needs a forge.yaml" in result.output

    def test_nothing_to_merge_refused(self, config, pool, fakes) -> None:
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code != 0
        assert "no COMPLETE routine build" in result.output

    def test_build_id_feature_mismatch_refused(
        self, config, pool, fakes
    ) -> None:
        _insert_build(pool, feature_id="FEAT-OTHER", build_id="build-OTHER")
        result = CliRunner().invoke(
            merge_deploy_cmd,
            [FEATURE_ID, "--build-id", "build-OTHER"],
            obj=config,
        )
        assert result.exit_code != 0
        assert "belongs to FEAT-OTHER" in result.output

    def test_failed_build_not_picked(self, config, pool, fakes) -> None:
        _insert_build(pool, status="FAILED")
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code != 0

    def test_fix_journey_build_not_picked(self, config, pool, fakes) -> None:
        _insert_build(pool, mode="mode-c")
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code != 0


class TestHappyPath:
    def test_merges_and_prints_receipt_lines(self, config, pool, fakes) -> None:
        _insert_build(pool)
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code == 0, result.output
        assert "result=publication-pending" in result.output
        assert "status=PASSED" in result.output
        assert f"merged_sha={fakes['merged']}" in result.output
        assert "checked and ready to publish" in result.output
        assert "live check of the joined result in the sandbox: pass (3 of 3 checks passed)" in result.output
        assert f"merge-{BUILD_ID}/" in result.output
        # The executor really ran: the candidate check, one merge, the
        # promote, one report — in that order (protect-main).
        assert len(fakes["gk_calls"]) == 1
        assert [c["leg"] for c in fakes["dp_calls"]] == ["candidate_check", "candidate_down"]
        assert len(fakes["publisher"].reports) == 1
        # The join is pinned to the commit the REMOTE'S recorded branch is at,
        # which the press fetched for itself — not the local pin the card
        # carried.
        args = fakes["gk_calls"][0]["args"]
        assert args[args.index("--target") + 1] == f"factory-integration/{FEATURE_ID}"
        assert len(args[args.index("--expect-main-sha") + 1]) == 40
        assert "--in-worktree" in args

    def test_newest_complete_routine_build_wins(
        self, config, pool, fakes
    ) -> None:
        _insert_build(
            pool, build_id="build-OLD", queued_at="2026-08-01T00:00:00Z"
        )
        _insert_build(pool, queued_at="2026-08-24T00:00:00Z")
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code == 0, result.output
        assert f"merge-{BUILD_ID}/" in result.output

    def test_a_project_whose_recorded_branch_is_not_main_can_be_pressed(
        self, config, pool, fakes, repo_root: Path, _receipts_env: Path
    ) -> None:
        """The one plain pick-up command, for a project that has no "main".

        It used to read a branch literally named ``main`` for its pin and
        refuse when there was none, so a project whose recorded branch is
        "trunk" — or a release line, or anything else — could not be pressed
        by this command at all. The recorded name is now what is read.
        """
        # The remote and the copy know only "trunk". There is no "main"
        # anywhere: the old code would have refused before the press ran.
        _git(repo_root, "branch", "-m", "main", "trunk")
        bare = _git(repo_root, "remote", "get-url", "origin")
        _git(repo_root, "push", "-q", "origin", "trunk")
        subprocess.run(
            ["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/trunk"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", bare, "branch", "-D", "main"], check=True, capture_output=True
        )
        assert _git(repo_root, "branch", "--list", "main") == ""
        assert (
            subprocess.run(
                ["git", "-C", bare, "branch", "--list", "main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == ""
        )

        _insert_build(pool)
        pool.connection.execute(
            "UPDATE builds SET target_branch = 'trunk' WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()

        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)

        assert result.exit_code == 0, result.output
        assert "result=publication-pending" in result.output
        assert "joined onto trunk" in result.output
        # The pin it computed is the RECORDED branch's commit, and it rode
        # into the press's own receipt rather than deciding anything.
        args = fakes["gk_calls"][0]["args"]
        assert args[args.index("--target") + 1] == f"factory-integration/{FEATURE_ID}"
        receipt = json.loads(
            (
                _receipts_env / f"merge-{BUILD_ID}" / "merge_deploy_merge.json"
            ).read_text(encoding="utf-8")
        )
        assert receipt["target_branch"] == "trunk"

    def test_a_build_with_no_recorded_branch_is_not_refused_by_the_command(
        self, config, pool, fakes
    ) -> None:
        """The press says it, and says it better than this command could."""
        _insert_build(pool)
        pool.connection.execute(
            "UPDATE builds SET target_branch = NULL WHERE build_id = ?", (BUILD_ID,)
        )
        pool.connection.commit()

        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)

        assert result.exit_code == 1, result.output
        assert "refusing an unpinned merge" not in result.output
        assert "no target branch on its record" in result.output

    def test_dry_run_threads_through(self, config, pool, fakes) -> None:
        _insert_build(pool)
        result = CliRunner().invoke(
            merge_deploy_cmd, [FEATURE_ID, "--dry-run"], obj=config
        )
        assert result.exit_code == 0, result.output
        assert fakes["dp_calls"][0]["dry_run"] is True

    def test_merge_refusal_maps_to_exit_1(
        self, config, pool, fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _insert_build(pool)

        async def _refusing_guardkit(**kwargs: Any) -> Any:
            return SimpleNamespace(
                status="failed",
                stdout_tail="",
                stderr="main moved",
                exit_code=1,
                artefacts=[],
            )

        publisher = fakes["publisher"]

        def _no_promote(**kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("the promote must not run after a merge refusal")

        async def _fake_backends(_config: ForgeConfig):
            async def _close() -> None:
                return None

            return (
                publisher,
                _refusing_guardkit,
                _leg_aware_deploy(fakes["dp_calls"], promote=_no_promote),
                _close,
            )

        monkeypatch.setattr(merge_deploy_module, "_aopen_backends", _fake_backends)
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code == 1
        assert "result=merge-refused" in result.output
        assert "failed_step=merge" in result.output
        # The join comes first, so nothing was stood up to come down.
        assert [c["leg"] for c in fakes["dp_calls"]] == []
