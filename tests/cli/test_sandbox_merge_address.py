"""Which deploy sidecar the merge word's command is sent to (rule 85).

Rich's rule: nothing the factory runs on a repository runs on the host. A
merge checks out, merges and re-runs a repository's own tests, so for a
repository that has a sandbox it happens inside that sandbox, through the
deploy sidecar running in there — not through the host one. Every other
repository keeps today's path exactly, and with ``planning.sandboxes`` empty
nothing routes at all.

Nothing live is touched: the addresses are never dialled — the sidecar client
builder is replaced by a recorder, so these tests assert WHICH address the
composition chose, without a socket.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from forge.cli.serve import compose_merge_guardkit_run
from forge.config.models import ForgeConfig

REPO_WITH = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
HOST_SIDECAR = "http://127.0.0.1:8125"
SANDBOX_SIDECAR = "http://127.0.0.1:8925"


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    with_sandbox = tmp_path / "api_test"
    without = tmp_path / "plain"
    with_sandbox.mkdir()
    without.mkdir()
    return {REPO_WITH: with_sandbox, REPO_WITHOUT: without}


def _config(
    repos: dict[str, Path], *, sandbox: bool, host_sidecar: str = HOST_SIDECAR
) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {key: str(path) for key, path in repos.items()}
    }
    if sandbox:
        planning["sandboxes"] = {
            REPO_WITH: {
                "name": "api-test-factory",
                "sidecar_url": SANDBOX_SIDECAR,
                "runner_url": "http://127.0.0.1:8924",
            }
        }
    raw: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": [str(next(iter(repos.values())).parent)]}},
        "planning": planning,
    }
    # The deploy block always carries a sidecar address (its default is the
    # host one), so "no sidecar configured" is spelled with an empty value.
    raw["deploy"] = {"sidecar_url": host_sidecar}
    return ForgeConfig.model_validate(raw)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every base_url a sidecar-backed merge runner was built against."""
    urls: list[str] = []

    def _build(*, base_url: str, repo_paths: Any) -> Any:
        urls.append(base_url)

        async def _run(**kwargs: Any) -> str:
            return f"ran through {base_url}"

        return _run

    monkeypatch.setattr(
        "forge.adapters.guardkit.run_via_sidecar.build_sidecar_guardkit_run", _build
    )
    return urls


class TestNoSandboxesMeansTodaysComposition:
    def test_the_merge_word_gets_one_runner_on_the_host_address(
        self, repos: dict[str, Path], recorded: list[str]
    ) -> None:
        run = compose_merge_guardkit_run(_config(repos, sandbox=False))

        assert recorded == [HOST_SIDECAR]
        answer = asyncio.run(run(repo_path=repos[REPO_WITH], subcommand="autobuild"))
        assert answer == f"ran through {HOST_SIDECAR}"

    def test_without_a_sidecar_address_it_runs_in_the_container_as_before(
        self, repos: dict[str, Path], recorded: list[str]
    ) -> None:
        from forge.adapters.guardkit.run import run as in_container_run

        run = compose_merge_guardkit_run(_config(repos, sandbox=False, host_sidecar=""))

        assert run is in_container_run
        assert recorded == []


class TestASandboxRepositoryIsMergedInsideItsSandbox:
    def test_the_address_is_chosen_per_repository(
        self, repos: dict[str, Path], recorded: list[str]
    ) -> None:
        run = compose_merge_guardkit_run(_config(repos, sandbox=True))

        # Only the default is built at boot; a repository's own runner is
        # built the first time a merge for it arrives.
        assert recorded == [HOST_SIDECAR]

        sandboxed = asyncio.run(run(repo_path=repos[REPO_WITH], subcommand="autobuild"))
        plain = asyncio.run(run(repo_path=repos[REPO_WITHOUT], subcommand="autobuild"))

        assert sandboxed == f"ran through {SANDBOX_SIDECAR}"
        assert plain == f"ran through {HOST_SIDECAR}"
        assert recorded == [HOST_SIDECAR, SANDBOX_SIDECAR]

    def test_the_runner_for_one_repository_is_built_once_and_reused(
        self, repos: dict[str, Path], recorded: list[str]
    ) -> None:
        run = compose_merge_guardkit_run(_config(repos, sandbox=True))

        for _ in range(3):
            asyncio.run(run(repo_path=repos[REPO_WITH], subcommand="autobuild"))

        assert recorded == [HOST_SIDECAR, SANDBOX_SIDECAR]

    def test_a_path_the_map_does_not_know_falls_back_to_the_default(
        self, repos: dict[str, Path], recorded: list[str], tmp_path: Path
    ) -> None:
        run = compose_merge_guardkit_run(_config(repos, sandbox=True))
        stranger = tmp_path / "stranger"
        stranger.mkdir()

        answer = asyncio.run(run(repo_path=stranger, subcommand="autobuild"))

        assert answer == f"ran through {HOST_SIDECAR}"
        assert recorded == [HOST_SIDECAR]
