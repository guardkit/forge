"""Which git the planning chain uses, per repository (sandbox first, 2026-09-07).

Rich's rule: nothing the factory runs on a repository runs on the host. A
repository that has a sandbox of its own has its planning commits made by the
deploy sidecar running inside that sandbox, on the factory's own clone, with
the pre-commit checks run there. Every other repository is handled exactly as
before, by the git runner inside the forge container.

These tests pin both halves at the place the choice is actually made: the
composition that boots the planning chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect_writer
from forge.cli._serve_planning import (
    compose_planning_consumer_and_dispatch,
    compose_planning_git_runner,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations as lifecycle_migrations
from forge.planning.sidecar_git_runner import RepoRoutedGitRunner, SidecarGitRunner

from tests.cli.test_serve_planning import FixedClock
from tests.integration.conftest import InMemoryNats

REPO = "guardkit/api_test"
OTHER = "appmilla/office"
PATHS = {REPO: "/srv/repos/api_test", OTHER: "/srv/repos/office"}
SANDBOX = {
    "name": "api-test",
    "sidecar_url": "http://127.0.0.1:8225",
    "runner_url": "http://127.0.0.1:8224",
}


def _config(*, sandboxes: dict[str, Any] | None = None) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
            "planning": {
                "enabled": True,
                "escalation_approver": "U_OWNER",
                "target_repo_paths": PATHS,
                **({"sandboxes": sandboxes} if sandboxes is not None else {}),
            },
        }
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "planning.db"
    lifecycle_migrations.apply_at_boot(connect_writer(db_path))
    return db_path


# ---------------------------------------------------------------------------
# The choice itself
# ---------------------------------------------------------------------------


def test_with_no_sandbox_anywhere_the_chain_gets_the_in_container_runner_alone() -> None:
    """The composition before this lane, byte for byte: one runner, no
    per-repository resolver, so the driver's every leg behaves as it did."""
    planning = _config().planning
    runner, resolver = compose_planning_git_runner(planning)
    assert isinstance(runner, WorktreeGitRunner)
    assert resolver is None
    assert not hasattr(runner, "supports_declared_checks")


def test_a_repository_with_a_sandbox_gets_that_sandboxs_sidecar() -> None:
    planning = _config(sandboxes={REPO: SANDBOX}).planning
    runner, resolver = compose_planning_git_runner(planning)
    assert isinstance(runner, RepoRoutedGitRunner)
    assert resolver is not None

    sandboxed = resolver(REPO)
    assert isinstance(sandboxed, SidecarGitRunner)
    assert sandboxed.base_url == "http://127.0.0.1:8225"
    assert sandboxed.repo == REPO
    assert sandboxed.supports_declared_checks() is True

    # Every other repository keeps the in-container runner, closures and all.
    assert isinstance(resolver(OTHER), WorktreeGitRunner)
    assert isinstance(resolver("nobody/knows"), WorktreeGitRunner)
    assert resolver(OTHER) is runner.default


def test_the_boot_log_no_longer_says_some_legs_are_not_declared(caplog) -> None:
    """Rule 87 landed: every planning leg declares its checks, so the boot no
    longer warns that giving a repository a sandbox would stop its spec leg.
    It says plainly where the checks run instead."""
    import logging

    planning = _config(sandboxes={REPO: SANDBOX}).planning
    with caplog.at_level(logging.INFO, logger="forge.cli._serve_planning"):
        compose_planning_git_runner(planning)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "api-test" in said and "http://127.0.0.1:8225" in said
    assert "every planning leg's checks run there" in said
    for stale in (
        "Only the plan leg's checks are declared",
        "hand a Python function",
        "only once those legs are moved",
    ):
        assert stale not in said


def test_the_calls_of_a_repository_without_a_sandbox_never_leave_the_container() -> None:
    """Routing is by the repository's own path, so a leg that writes for the
    unsandboxed repository reaches the in-container runner untouched."""
    planning = _config(sandboxes={REPO: SANDBOX}).planning
    routed, _ = compose_planning_git_runner(
        planning, worktree_runner_factory=lambda: _Recording()
    )
    assert routed.runner_for_path(PATHS[OTHER]) is routed.default
    assert routed.runner_for_path(PATHS[REPO]) is not routed.default


class _Recording:
    """A stand-in for the in-container runner, to prove identity routing."""


# ---------------------------------------------------------------------------
# The call site: what the booted chain actually holds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_booted_chain_holds_the_resolver_only_when_a_sandbox_is_configured(
    tmp_db: Path,
) -> None:
    with_sandbox = await compose_planning_consumer_and_dispatch(
        db_path=tmp_db,
        nats_client=InMemoryNats(),
        config=_config(sandboxes={REPO: SANDBOX}),
        clock=FixedClock(),
    )
    assert with_sandbox is not None and with_sandbox.driver is not None
    deps = with_sandbox.driver._deps
    assert isinstance(deps.git_runner, RepoRoutedGitRunner)
    assert deps.git_runner_for_repo is not None
    assert isinstance(deps.git_runner_for_repo(REPO), SidecarGitRunner)
    assert isinstance(deps.git_runner_for_repo(OTHER), WorktreeGitRunner)


@pytest.mark.asyncio
async def test_without_a_sandbox_the_booted_chain_is_what_it_was_before(
    tmp_db: Path,
) -> None:
    plain = await compose_planning_consumer_and_dispatch(
        db_path=tmp_db,
        nats_client=InMemoryNats(),
        config=_config(),
        clock=FixedClock(),
    )
    assert plain is not None and plain.driver is not None
    deps = plain.driver._deps
    assert isinstance(deps.git_runner, WorktreeGitRunner)
    assert deps.git_runner_for_repo is None
