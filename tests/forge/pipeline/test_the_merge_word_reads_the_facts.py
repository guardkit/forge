"""The merge press reads the machine's answers on EVERY merge word (TC8).

The press's deps are built once, when the coordinator starts. Release -3 gives
them a READER of the facts ``estate-check --publication-facts`` wrote, rather
than a value, and the press calls it at each merge word. So:

* facts that are missing or older than the coordinator's start keep a merge
  at "publication pending", and say why;
* facts written after the start turn publication on at the NEXT merge word,
  with the same deps object and no restart;
* both production callers — the coordinator's merge-word listener and the
  ``forge merge-deploy`` command — pass that reader.

The remote is a bare repository on disk; nothing real is contacted.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from forge.config.models import ForgeConfig
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_executor import MergeExecutorDeps, execute_merge_deploy
from forge.pipeline.publication_facts import (
    FACTS_FILE_ENV,
    read_publication_facts,
)
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    MAIN_SHA,
    REPO,
    _ensure_build,
    _FakeDeploy,
    _FakePublisher,
    _JoinsForReal,
    _receipts_env,
    pool,
    repo_root,
)
from tests.forge.pipeline.test_publication_facts import (
    ASKING,
    STARTED,
    facts,
)
from tests.forge.pipeline.test_the_merge_word_publishes import (  # noqa: F401
    _APublisherThatSays,
    _published,
    config_with_publication_on,
)


def _reader(path: Path) -> Any:
    return functools.partial(
        read_publication_facts,
        environ={FACTS_FILE_ENV: str(path)},
        who_is_asking=lambda: ASKING,
        now=lambda: STARTED + 300,
    )


async def _press_for(deps: MergeExecutorDeps, repo_root: Path, feature: str) -> Any:  # noqa: F811
    build_id = f"build-{feature}-20260824"
    _ensure_build(deps.pool, build_id=build_id, feature_id=feature)
    return await execute_merge_deploy(
        deps=deps,
        build_id=build_id,
        feature_id=feature,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=MAIN_SHA,
        correlation_id=f"corr-{build_id}",
        decided_by="rich",
    )


def _deps_reading(
    config: ForgeConfig, pool: SqliteLifecyclePersistence, path: Path, publisher: Any  # noqa: F811
) -> MergeExecutorDeps:
    return MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_FakePublisher(),
        guardkit_run=_JoinsForReal(),
        deploy_dispatcher=_FakeDeploy(),
        publisher=publisher,
        what_the_machine_says=_reader(path),
    )


class TestThePressReadsTheFactsAtEachMergeWord:
    @pytest.mark.asyncio
    async def test_facts_older_than_the_start_keep_it_pending(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "publication-facts.json"
        path.write_text(json.dumps(facts(written=STARTED - 60)))
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps = _deps_reading(config_with_publication_on, pool, path, publisher)

        outcome = await _press_for(deps, repo_root, "FEAT-MX1")

        assert outcome.result == "publication-pending"
        assert "at or before this coordinator started" in outcome.detail
        assert "nobody has looked" in outcome.detail
        assert publisher.asked == []

    @pytest.mark.asyncio
    async def test_a_missing_file_keeps_it_pending(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps = _deps_reading(
            config_with_publication_on, pool, tmp_path / "absent.json", publisher
        )

        outcome = await _press_for(deps, repo_root, "FEAT-MX1")

        assert outcome.result == "publication-pending"
        assert "there is no publication facts file" in outcome.detail
        assert publisher.asked == []

    @pytest.mark.asyncio
    async def test_fresh_facts_written_after_boot_count_at_the_next_merge_word(
        self,
        config_with_publication_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        tmp_path: Path,
    ) -> None:
        """ONE deps object, as the coordinator builds it once at boot; the file
        changes between two merge words, and so does the verdict."""
        path = tmp_path / "publication-facts.json"
        publisher = _APublisherThatSays([_published("c" * 40)])
        deps = _deps_reading(config_with_publication_on, pool, path, publisher)

        first = await _press_for(deps, repo_root, "FEAT-MX1")
        assert first.result == "publication-pending"
        assert publisher.asked == []

        path.write_text(json.dumps(facts(written=STARTED + 120)))
        second = await _press_for(deps, repo_root, "FEAT-MX2")

        assert second.result == "published-deployment-pending"
        assert len(publisher.asked) == 1
        assert publisher.asked[0]["build_id"] == "build-FEAT-MX2-20260824"


class TestBothProductionCallersPassTheReader:
    def test_the_coordinators_merge_word_listener(self) -> None:
        from forge.cli.serve import compose_merge_executor_deps

        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "merge_executor": {"enabled": True},
            }
        )
        deps = compose_merge_executor_deps(
            forge_config=config,
            sqlite_pool=object(),
            pipeline_publisher=object(),
            nats_client=object(),
            db_path=None,
        )
        assert deps.what_the_machine_says is read_publication_facts

    def test_the_merge_deploy_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tests.forge import test_cli_merge_deploy as cli_tests

        from forge.cli.merge_deploy import merge_deploy_cmd
        from forge.pipeline import merge_executor

        seen: list[MergeExecutorDeps] = []

        async def capture(*, deps: MergeExecutorDeps, **_kwargs: Any) -> Any:
            seen.append(deps)
            raise SystemExit(0)

        monkeypatch.setattr(merge_executor, "execute_merge_deploy", capture)
        root = tmp_path / "repo"
        root.mkdir()
        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.lifecycle import migrations

        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        pool = SqliteLifecyclePersistence(connection=cx)  # noqa: F811
        cli_tests._insert_build(pool)
        monkeypatch.setattr(cli_tests.merge_deploy_module, "_open_pool", lambda _p: pool)

        async def backends(_config: Any) -> Any:
            async def close() -> None:
                return None

            return object(), object(), object(), close

        monkeypatch.setattr(cli_tests.merge_deploy_module, "_aopen_backends", backends)
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {cli_tests.REPO: str(root)}},
                "approval": {"expected_approver": "rich"},
            }
        )
        CliRunner().invoke(merge_deploy_cmd, [cli_tests.FEATURE_ID], obj=config)

        assert len(seen) == 1
        assert seen[0].what_the_machine_says is read_publication_facts


class TestTheRunningCoordinatorsListenerGetsTheReader:
    @pytest.mark.asyncio
    async def test_the_composed_dispatch_chain_attaches_a_press_with_the_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Through the production composition itself — bind_production_dispatch_chain
        and its _compose — not the helper alone: the merge-word listener the
        running coordinator attaches must be given the facts reader."""
        from unittest.mock import MagicMock

        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli import _serve_daemon, _serve_deps_gating
        from forge.cli import serve as serve_module
        from forge.lifecycle import migrations
        from forge.pipeline import merge_executor

        attached: list[MergeExecutorDeps] = []

        class _Listener:
            def __init__(self, deps: MergeExecutorDeps) -> None:
                attached.append(deps)

            async def attach(self, _client: Any) -> None:
                return None

        monkeypatch.setattr(merge_executor, "MergeApprovalConsumer", _Listener)
        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        persistence = SqliteLifecyclePersistence(connection=cx)
        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "merge_executor": {"enabled": True},
            }
        )
        previous = _serve_daemon.dispatch_payload
        try:
            compose = serve_module.bind_production_dispatch_chain(
                forge_config=config, sqlite_pool=persistence
            )
            await compose(MagicMock(name="nats-client"))
        finally:
            _serve_daemon.dispatch_payload = previous
            _serve_deps_gating._reset_for_tests()
            cx.close()

        assert len(attached) == 1
        assert attached[0].what_the_machine_says is read_publication_facts
