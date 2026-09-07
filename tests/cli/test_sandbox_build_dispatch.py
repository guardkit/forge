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
