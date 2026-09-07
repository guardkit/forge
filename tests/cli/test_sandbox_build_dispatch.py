"""Where a feature build is dispatched when its repository has a sandbox
(sandbox first, 2026-09-07, rule 78).

Rich's rule: nothing the factory runs on a repository runs on the host. A
build installs and runs the repository's own code, so a repository that has a
sandbox has its builds dispatched to the build runner inside it. Every other
repository — which is every repository until an operator fills
``planning.sandboxes`` in — is dispatched to the one global runner, and these
tests say so for each case.

Nothing live is touched: no sandbox, no ``sbx``, no docker, no service. The
middleware is the real composition's own factory, driven with a stand-in tool
so no LangGraph client is ever constructed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from forge.cli._serve_production import (
    _RepoRoutedAsyncTaskStarter,
    build_repo_routed_async_task_starter,
)
from forge.config.models import ForgeConfig

REPO_WITH = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
GLOBAL_RUNNER = "http://127.0.0.1:8124"
SANDBOX_RUNNER = "http://127.0.0.1:8924"


class _Command:
    def __init__(self, task_id: str) -> None:
        self.update = {"async_tasks": {task_id: {}}}


class _Tool:
    """Stands in for the middleware's ``start_async_task`` StructuredTool."""

    name = "start_async_task"

    def __init__(self, url: str, seen: list[tuple[str, str]]) -> None:
        self._url = url
        self._seen = seen

    def func(self, *, description: str, subagent_type: str, runtime: Any) -> Any:
        self._seen.append((self._url, "sync"))
        return _Command(f"thread-for-{self._url}")

    async def coroutine(
        self, *, description: str, subagent_type: str, runtime: Any
    ) -> Any:
        self._seen.append((self._url, "async"))
        return _Command(f"thread-for-{self._url}")


class _FakeServeModule:
    """The real composition's seam, recording every runner address it is
    asked to register a middleware against."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.launches: list[tuple[str, str]] = []

    def _build_async_subagent_middleware(
        self, *, autobuild_runner_url: str | None = None
    ) -> Any:
        self.urls.append(str(autobuild_runner_url))
        return SimpleNamespace(
            tools=[_Tool(str(autobuild_runner_url), self.launches)]
        )


def _config(*, with_sandbox: bool, runner_url: str = SANDBOX_RUNNER) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {REPO_WITH: "/repos/api_test", REPO_WITHOUT: "/repos/plain"}
    }
    if with_sandbox:
        planning["sandboxes"] = {
            REPO_WITH: {
                "name": "api-test-factory",
                "sidecar_url": "http://127.0.0.1:8925",
                "runner_url": runner_url,
            }
        }
    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/repos"]}}, "planning": planning}
    )


def _default_starter(seen: list[tuple[str, str]]) -> Any:
    from forge.cli._serve_async_task_starter import build_async_task_starter

    return build_async_task_starter(_Tool(GLOBAL_RUNNER, seen))


class TestARepositoryWithNoSandboxIsDispatchedExactlyAsBefore:
    def test_an_empty_map_returns_the_very_same_starter_object(self) -> None:
        seen: list[tuple[str, str]] = []
        default = _default_starter(seen)
        module = _FakeServeModule()

        routed = build_repo_routed_async_task_starter(
            serve_module=module,
            forge_config=_config(with_sandbox=False),
            default_starter=default,
        )

        # Not merely equivalent: the same object, so the composition is byte
        # for byte what it was before this lane.
        assert routed is default
        assert module.urls == []

    def test_a_repository_outside_the_map_still_goes_to_the_global_runner(
        self,
    ) -> None:
        seen: list[tuple[str, str]] = []
        module = _FakeServeModule()
        routed = build_repo_routed_async_task_starter(
            serve_module=module,
            forge_config=_config(with_sandbox=True),
            default_starter=_default_starter(seen),
        )

        task_id = asyncio.run(
            routed.astart_async_task(
                "autobuild_runner", {"repo": REPO_WITHOUT, "correlation_id": "c1"}
            )
        )

        assert seen == [(GLOBAL_RUNNER, "async")]
        assert task_id == f"thread-for-{GLOBAL_RUNNER}"

    def test_a_dispatch_that_names_no_repository_goes_to_the_global_runner(
        self,
    ) -> None:
        seen: list[tuple[str, str]] = []
        module = _FakeServeModule()
        routed = build_repo_routed_async_task_starter(
            serve_module=module,
            forge_config=_config(with_sandbox=True),
            default_starter=_default_starter(seen),
        )

        asyncio.run(routed.astart_async_task("autobuild_runner", {"correlation_id": "c"}))

        assert seen == [(GLOBAL_RUNNER, "async")]


class TestARepositoryWithASandboxIsDispatchedIntoIt:
    def test_the_build_goes_to_the_runner_inside_the_sandbox(self) -> None:
        seen: list[tuple[str, str]] = []
        module = _FakeServeModule()
        routed = build_repo_routed_async_task_starter(
            serve_module=module,
            forge_config=_config(with_sandbox=True),
            default_starter=_default_starter(seen),
        )

        assert isinstance(routed, _RepoRoutedAsyncTaskStarter)
        # One middleware was registered, against the sandbox's own address.
        assert module.urls == [SANDBOX_RUNNER]

        task_id = asyncio.run(
            routed.astart_async_task(
                "autobuild_runner", {"repo": REPO_WITH, "correlation_id": "c2"}
            )
        )

        assert module.launches == [(SANDBOX_RUNNER, "async")]
        assert seen == []
        assert task_id == f"thread-for-{SANDBOX_RUNNER}"

    def test_the_sync_launch_path_routes_the_same_way(self) -> None:
        seen: list[tuple[str, str]] = []
        module = _FakeServeModule()
        routed = build_repo_routed_async_task_starter(
            serve_module=module,
            forge_config=_config(with_sandbox=True),
            default_starter=_default_starter(seen),
        )

        routed.start_async_task("autobuild_runner", {"repo": REPO_WITH})
        routed.start_async_task("autobuild_runner", {"repo": REPO_WITHOUT})

        assert module.launches == [(SANDBOX_RUNNER, "sync")]
        assert seen == [(GLOBAL_RUNNER, "sync")]

    def test_a_sandbox_entry_needs_an_address_or_it_is_said_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A blank runner address cannot be validated by the model (it refuses
        # one), so this drives the composition with an entry-shaped stand-in
        # whose address is empty — the shape a hand-edited settings file could
        # produce if the field were ever made optional.
        config = SimpleNamespace(
            planning=SimpleNamespace(
                sandboxes={REPO_WITH: SimpleNamespace(name="s", runner_url="")},
                target_repo_paths={},
            )
        )
        seen: list[tuple[str, str]] = []
        default = _default_starter(seen)
        module = _FakeServeModule()

        with caplog.at_level("WARNING"):
            routed = build_repo_routed_async_task_starter(
                serve_module=module, forge_config=config, default_starter=default
            )

        assert routed is default
        assert "no runner address" in caplog.text


# ---------------------------------------------------------------------------
# Where the dispatched build is WATCHED (rule 78, repaired 2026-09-08)
# ---------------------------------------------------------------------------
#
# Dispatching a build into the sandbox is only half of rule 78. Everything
# that then watches it — the event stream forge-prod joins, the run id it
# resolves, the final state it reads back when the stream closes empty — has
# to speak to the same runner. Watching the host runner for a build that ran
# in a sandbox would mean no lifecycle event ever arrives: the build sits in
# its stage for ever and the queue counts it as in flight, which is the shape
# of the 2026-09-05 stuck-count defect.


def _pool(tmp_path: Any) -> Any:
    """A real ledger on disk, with the real schema and the real facade."""
    import sqlite3 as _sqlite3

    from forge.cli._serve_deps_state_channel import ASYNC_TASKS_SCHEMA_DDL
    from forge.lifecycle.migrations import apply_at_boot
    from forge.lifecycle.persistence import SqliteLifecyclePersistence

    db_path = str(tmp_path / "forge.db")
    connection = _sqlite3.connect(db_path)
    apply_at_boot(connection)
    connection.execute(ASYNC_TASKS_SCHEMA_DDL)
    connection.commit()
    return SqliteLifecyclePersistence(connection=connection, db_path=db_path)


def _record_build(pool: Any, *, feature_id: str, repo: str) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at) "
        "VALUES (?, ?, ?, ?, ?, 'QUEUED', 'cli', ?, '2026-09-08T00:00:00Z')",
        (
            f"build-{feature_id}-20260908000000",
            feature_id,
            repo,
            f"feat/{feature_id}",
            f"features/{feature_id}.yaml",
            f"corr-{feature_id}",
        ),
    )
    pool.connection.commit()


class TestWhichRunnerIsWatchedForADispatchedBuild:
    def test_with_no_sandbox_there_is_no_routing_at_all(self, tmp_path: Any) -> None:
        from forge.cli._serve_production import build_feature_runner_url_resolver

        resolver = build_feature_runner_url_resolver(
            sqlite_pool=_pool(tmp_path),
            forge_config=_config(with_sandbox=False),
            default_url=GLOBAL_RUNNER,
        )

        # None means "do not wrap anything": the stream source, the identity
        # provider and the state fetcher stay the objects they always were.
        assert resolver is None

    def test_a_features_own_build_row_says_which_runner_holds_it(
        self, tmp_path: Any
    ) -> None:
        from forge.cli._serve_production import build_feature_runner_url_resolver

        pool = _pool(tmp_path)
        _record_build(pool, feature_id="FEAT-AAAA", repo=REPO_WITH)
        _record_build(pool, feature_id="FEAT-BBBB", repo=REPO_WITHOUT)
        resolver = build_feature_runner_url_resolver(
            sqlite_pool=pool,
            forge_config=_config(with_sandbox=True),
            default_url=GLOBAL_RUNNER,
        )
        assert resolver is not None

        assert resolver("FEAT-AAAA") == SANDBOX_RUNNER
        assert resolver("FEAT-BBBB") == GLOBAL_RUNNER
        # A feature nothing has queued yet, and a blank one: the honest
        # answer is the global runner, never a guess.
        assert resolver("FEAT-NONE") == GLOBAL_RUNNER
        assert resolver("") == GLOBAL_RUNNER

    def test_the_answer_is_remembered_so_watching_is_not_a_query_per_event(
        self, tmp_path: Any
    ) -> None:
        from forge.cli._serve_production import build_feature_runner_url_resolver

        pool = _pool(tmp_path)
        _record_build(pool, feature_id="FEAT-AAAA", repo=REPO_WITH)
        resolver = build_feature_runner_url_resolver(
            sqlite_pool=pool,
            forge_config=_config(with_sandbox=True),
            default_url=GLOBAL_RUNNER,
        )
        assert resolver is not None
        assert resolver("FEAT-AAAA") == SANDBOX_RUNNER

        # The row is gone; the answer already given does not change.
        pool.connection.execute("DELETE FROM builds")
        pool.connection.commit()
        assert resolver("FEAT-AAAA") == SANDBOX_RUNNER

    def test_a_ledger_that_cannot_be_read_watches_the_global_runner(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from forge.cli._serve_production import build_feature_runner_url_resolver

        pool = _pool(tmp_path)
        pool.connection.execute("DROP TABLE builds")
        pool.connection.commit()
        resolver = build_feature_runner_url_resolver(
            sqlite_pool=pool,
            forge_config=_config(with_sandbox=True),
            default_url=GLOBAL_RUNNER,
        )
        assert resolver is not None

        with caplog.at_level("WARNING"):
            assert resolver("FEAT-AAAA") == GLOBAL_RUNNER

        assert "could not read which repository" in caplog.text


class TestTheWatchingSeamsTakeTheRoutedAddress:
    """The three seams that speak to a runner, driven for both repositories.

    Each factory is replaced by a recorder so no LangGraph client is ever
    constructed; what is under test is which address each seam is built
    against, which is the whole of the defect.
    """

    @staticmethod
    def _recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        from forge.cli import _serve_production as production

        joined: list[str] = []
        fetched: list[str] = []

        def _stream(*, runner_url: str) -> Any:
            def _source(*, feature_id: str, thread_id: Any, run_id: Any) -> Any:
                joined.append(runner_url)
                return iter(())

            return _source

        def _fetcher(*, runner_url: str) -> Any:
            async def _fetch(*, feature_id: str, thread_id: Any, run_id: Any) -> Any:
                fetched.append(runner_url)
                return None

            return _fetch

        monkeypatch.setattr(production, "langgraph_stream_source", _stream)
        monkeypatch.setattr(production, "langgraph_run_state_fetcher", _fetcher)
        return {"joined": joined, "fetched": fetched}

    def test_the_stream_and_the_state_are_read_where_the_build_ran(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli._serve_production import _build_lifecycle_bridge_wireup_parts

        seen = self._recorders(monkeypatch)
        pool = _pool(tmp_path)
        _record_build(pool, feature_id="FEAT-AAAA", repo=REPO_WITH)
        _record_build(pool, feature_id="FEAT-BBBB", repo=REPO_WITHOUT)

        parts = _build_lifecycle_bridge_wireup_parts(
            sqlite_pool=pool,
            autobuild_runner_url=GLOBAL_RUNNER,
            forge_config=_config(with_sandbox=True),
        )

        parts.stream_source(feature_id="FEAT-AAAA", thread_id="t", run_id="r")
        parts.stream_source(feature_id="FEAT-BBBB", thread_id="t", run_id="r")
        asyncio.run(
            parts.run_state_fetcher(feature_id="FEAT-AAAA", thread_id="t", run_id="r")
        )
        asyncio.run(
            parts.run_state_fetcher(feature_id="FEAT-BBBB", thread_id="t", run_id="r")
        )

        assert seen["joined"] == [SANDBOX_RUNNER, GLOBAL_RUNNER]
        assert seen["fetched"] == [SANDBOX_RUNNER, GLOBAL_RUNNER]

    def test_with_no_sandbox_the_seams_are_the_very_objects_as_before(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli import _serve_production as production

        made: dict[str, Any] = {}

        def _stream(*, runner_url: str) -> Any:
            made["stream"] = object()
            made["stream_url"] = runner_url
            return made["stream"]

        def _fetcher(*, runner_url: str) -> Any:
            made["fetch"] = object()
            return made["fetch"]

        monkeypatch.setattr(production, "langgraph_stream_source", _stream)
        monkeypatch.setattr(production, "langgraph_run_state_fetcher", _fetcher)

        parts = production._build_lifecycle_bridge_wireup_parts(
            sqlite_pool=_pool(tmp_path),
            autobuild_runner_url=GLOBAL_RUNNER,
            forge_config=_config(with_sandbox=False),
        )

        # Not merely equivalent — the same objects the factories returned,
        # with no wrapper of this lane's in between.
        assert parts.stream_source is made["stream"]
        assert parts.run_state_fetcher is made["fetch"]
        assert made["stream_url"] == GLOBAL_RUNNER

    def test_the_run_id_is_asked_for_at_the_same_runner(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity provider is the third seam that speaks to a runner:
        it asks which run belongs to a thread. A thread made inside a sandbox
        is unknown to the host runner, so it must be asked there too."""
        import sys
        from types import ModuleType

        from forge.cli._serve_production import (
            _build_async_tasks_identity_provider,
            build_feature_runner_url_resolver,
        )

        asked: list[str] = []

        class _Runs:
            def __init__(self, url: str) -> None:
                self._url = url

            async def list(self, thread_id: str, limit: int = 1) -> Any:
                asked.append(self._url)
                return [{"run_id": f"run-in-{self._url}"}]

        fake_sdk = ModuleType("langgraph_sdk")
        fake_sdk.get_client = lambda *, url: SimpleNamespace(runs=_Runs(url))  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "langgraph_sdk", fake_sdk)

        pool = _pool(tmp_path)
        _record_build(pool, feature_id="FEAT-AAAA", repo=REPO_WITH)
        _record_build(pool, feature_id="FEAT-BBBB", repo=REPO_WITHOUT)
        for feature_id in ("FEAT-AAAA", "FEAT-BBBB"):
            pool.connection.execute(
                "INSERT INTO async_tasks (task_id, build_id, feature_id, "
                "correlation_id, lifecycle, wave_index, task_index, "
                "started_at, last_activity_at) VALUES (?, ?, ?, ?, 'run', 0, 0, "
                "'2026-09-08T00:00:00Z', '2026-09-08T00:00:00Z')",
                (
                    f"thread-{feature_id}",
                    f"build-{feature_id}-20260908000000",
                    feature_id,
                    f"corr-{feature_id}",
                ),
            )
        pool.connection.commit()

        provider = _build_async_tasks_identity_provider(
            sqlite_pool=pool,
            autobuild_runner_url=GLOBAL_RUNNER,
            runner_url_for_feature=build_feature_runner_url_resolver(
                sqlite_pool=pool,
                forge_config=_config(with_sandbox=True),
                default_url=GLOBAL_RUNNER,
            ),
        )

        assert asyncio.run(provider("FEAT-AAAA", "corr-FEAT-AAAA")) == (
            "thread-FEAT-AAAA",
            f"run-in-{SANDBOX_RUNNER}",
        )
        assert asyncio.run(provider("FEAT-BBBB", "corr-FEAT-BBBB")) == (
            "thread-FEAT-BBBB",
            f"run-in-{GLOBAL_RUNNER}",
        )
        assert asked == [SANDBOX_RUNNER, GLOBAL_RUNNER]
