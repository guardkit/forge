"""``forge merge-deploy`` CLI smoke — the attended merge word, seams faked.

Covers: config-required refusal, build-row resolution (newest COMPLETE
routine build; --build-id mismatch refused; nothing-to-merge refused), and
the happy path through the real executor with the NATS/guardkit/deploy seams
faked — asserting the printed receipt lines and the exit-code mapping.
"""

from __future__ import annotations

import json
import sqlite3
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
        "mode) VALUES (?, ?, ?, ?, 'f.yaml', ?, 'cli', ?, ?, ?)",
        (
            build_id,
            feature_id,
            REPO,
            f"autobuild/{feature_id}",
            status,
            f"corr-{build_id}",
            queued_at,
            mode,
        ),
    )
    pool.connection.commit()


class _FakePublisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


@pytest.fixture
def config(tmp_path: Path) -> ForgeConfig:
    repo_root = tmp_path / "api_test"
    repo_root.mkdir()
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
        }
    )


@pytest.fixture
def fakes(
    pool: SqliteLifecyclePersistence, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Rebind the CLI's module seams to offline fakes."""
    publisher = _FakePublisher()
    gk_calls: list[dict[str, Any]] = []
    dp_calls: list[dict[str, Any]] = []

    async def _fake_guardkit(**kwargs: Any) -> Any:
        gk_calls.append(kwargs)
        return SimpleNamespace(
            status="success",
            stdout_tail=json.dumps({"status": "merged", "merged_sha": "d" * 40}),
            stderr=None,
            exit_code=0,
            artefacts=[],
        )

    async def _fake_deploy(**kwargs: Any) -> Any:
        dp_calls.append(kwargs)
        return SimpleNamespace(
            outcome="complete", verdict="pass", deploy_record_ref="r"
        )

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
    return {"publisher": publisher, "gk_calls": gk_calls, "dp_calls": dp_calls}


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
        assert "result=merged-and-running" in result.output
        assert "status=PASSED" in result.output
        assert f"merged_sha={'d' * 40}" in result.output
        assert "merged and running" in result.output
        assert f"merge-{BUILD_ID}/" in result.output
        # The executor really ran: one merge, one deploy, one report.
        assert len(fakes["gk_calls"]) == 1
        assert len(fakes["dp_calls"]) == 1
        assert len(fakes["publisher"].reports) == 1
        # expect-main-sha was computed NOW (the fake pin).
        args = fakes["gk_calls"][0]["args"]
        assert args[args.index("--expect-main-sha") + 1] == "e" * 40

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

        async def _fake_backends(_config: ForgeConfig):
            async def _close() -> None:
                return None

            async def _no_deploy(**kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("deploy must not run after a merge refusal")

            return publisher, _refusing_guardkit, _no_deploy, _close

        monkeypatch.setattr(merge_deploy_module, "_aopen_backends", _fake_backends)
        result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
        assert result.exit_code == 1
        assert "result=merge-refused" in result.output
        assert "failed_step=merge" in result.output
