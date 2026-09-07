"""What the merge word's deploy legs are pointed at when the repository has a
sandbox (sandbox first, 2026-09-07, rule 85).

The in-daemon deploy dispatcher is the one place that composes the deploy
stage for a merge. For a repository with a sandbox it must hand the stage that
repository's own sandbox entry — so the stage's scripts go to the sidecar
inside it and the deploy step runs the repository's own deploy script — and it
must build the live gate as the sandbox-backed one, pointed at the same
sidecar. A repository without a sandbox must get exactly what it got before:
no entry, and the subprocess live gate.

Nothing live is touched: the deploy stage itself is replaced by a recorder, so
no runbook, no database and no script is reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.live_gate import (
    RepoDriverLiveGateInvoker,
    SidecarLiveGateInvoker,
)
from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

REPO_WITH = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
SANDBOX_SIDECAR = "http://127.0.0.1:8925"
DRIVER = ["python3", "qa/gates/local_live_gate.py"]


def _profile(root: Path) -> dict[str, Any]:
    return {
        "env_id": "apitest",
        "compose": {"file": "docker-compose.yml", "script": "deploy/sandbox-deploy.sh"},
        "cwd": str(root),
        "live_gate": {"driver": DRIVER, "timeout_seconds": 120, "env": {}},
    }


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    made: dict[str, Path] = {}
    for key, name in ((REPO_WITH, "api_test"), (REPO_WITHOUT, "plain")):
        root = tmp_path / name
        (root / "deploy").mkdir(parents=True)
        (root / "deploy" / "profile.yaml").write_text(
            yaml.safe_dump(_profile(root)), encoding="utf-8"
        )
        made[key] = root
    return made


@pytest.fixture
def config(repos: dict[str, Path], tmp_path: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
            "planning": {
                "target_repo_paths": {k: str(v) for k, v in repos.items()},
                "sandboxes": {
                    REPO_WITH: {
                        "name": "api-test-factory",
                        "sidecar_url": SANDBOX_SIDECAR,
                        "runner_url": "http://127.0.0.1:8924",
                    }
                },
            },
        }
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _dispatch(*args: Any, **kwargs: Any) -> Any:
        calls.append({"profile": args[1] if len(args) > 1 else None, **kwargs})
        return None

    monkeypatch.setattr("forge.deploy.composition.dispatch_deploy_stage", _dispatch)
    return calls


async def _drive(
    config: ForgeConfig, repos: dict[str, Path], repo: str, tmp_path: Path
) -> None:
    dispatch = build_in_daemon_deploy_dispatcher(
        config=config, nats_client=object(), db_path=tmp_path / "forge.db"
    )
    await dispatch(
        repo=repo,
        repo_root=repos[repo],
        feature_id="FEAT-SBX7",
        build_id="build-1",
        correlation_id="c",
        decided_by="rich",
        dry_run=True,
        leg="candidate_check",
    )


@pytest.mark.asyncio
async def test_a_sandbox_repository_gets_its_entry_and_the_sandbox_live_gate(
    config: ForgeConfig,
    repos: dict[str, Path],
    recorded: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    await _drive(config, repos, REPO_WITH, tmp_path)

    (call,) = recorded
    entry = call["sandbox"]
    assert entry is not None
    assert entry.name == "api-test-factory"
    assert entry.sidecar_url == SANDBOX_SIDECAR
    invoker = call["live_gate_invoker"]
    assert isinstance(invoker, SidecarLiveGateInvoker)
    assert invoker.base_url == SANDBOX_SIDECAR
    assert invoker.repo_path == repos[REPO_WITH]


@pytest.mark.asyncio
async def test_a_repository_without_a_sandbox_is_composed_exactly_as_before(
    config: ForgeConfig,
    repos: dict[str, Path],
    recorded: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    await _drive(config, repos, REPO_WITHOUT, tmp_path)

    (call,) = recorded
    assert call["sandbox"] is None
    invoker = call["live_gate_invoker"]
    assert isinstance(invoker, RepoDriverLiveGateInvoker)
    assert invoker.repo_path == repos[REPO_WITHOUT]
