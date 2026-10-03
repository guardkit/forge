"""What the merge word's deploy legs are pointed at when the repository has a
sandbox (sandbox first, 2026-09-07, rule 85).

The in-daemon deploy dispatcher is the one place that composes the deploy
stage for a merge. For a repository with a sandbox it must hand the stage that
repository's own sandbox entry — so the stage's scripts go to the sidecar
inside it and the deploy step runs the repository's own deploy script — and it
must build the live gate as the sandbox-backed one, pointed at the same
sidecar. A repository without a sandbox must get exactly what it got before:
no entry, and the subprocess live gate.

The PROFILE of a repository with a sandbox is read in there too (3 October
2026, FEAT-E592): its clone is the only copy, and the path this coordinator
was given for it holds nothing. Reading that path stopped the merge at the
candidate check with "deploy profile not found". Here that path is left
empty, and the sandbox's read is a recorder standing in for the sidecar.

Nothing live is touched: the deploy stage itself is replaced by a recorder, so
no runbook, no database and no script is reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import FileAtCommit
from forge.deploy.profile import DeployProfileError
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
    """The repository without a sandbox has its checkout here; the one with a
    sandbox has only a path that holds nothing, as in the coordinator."""
    plain = tmp_path / "plain"
    (plain / "deploy").mkdir(parents=True)
    (plain / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(_profile(plain)), encoding="utf-8"
    )
    return {REPO_WITH: tmp_path / "not-mounted" / "api_test", REPO_WITHOUT: plain}


@pytest.fixture
def read_in_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[dict[str, Any]]:
    """The sandbox's own read of a file at a commit, recorded and answered."""
    asked: list[dict[str, Any]] = []
    in_the_clone = yaml.safe_dump(_profile(Path("/sandbox/clone/api_test")))

    async def _read(self: Any, commit: str, file_path: str) -> FileAtCommit:
        asked.append(
            {
                "base_url": self.base_url,
                "repo": self.repo,
                "commit": commit,
                "file_path": file_path,
            }
        )
        return FileAtCommit(content=in_the_clone, found=True)

    monkeypatch.setattr(
        "forge.deploy.sidecar_git.SidecarCandidateGit.read_file_at_commit", _read
    )
    return asked


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
    read_in_the_sandbox: list[dict[str, Any]],
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
    # Its deploy records go to a folder of the coordinator's own, beside its
    # ledger, never under the path that holds nothing (3 October 2026).
    assert call["deploy_record_root"] == str(tmp_path / "deploy-records" / "api_test")


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
    assert call["deploy_record_root"] == str(
        repos[REPO_WITHOUT] / config.deploy.deploy_record_dir
    )


@pytest.mark.asyncio
async def test_a_sandbox_repositorys_profile_is_read_in_the_sandbox(
    config: ForgeConfig,
    repos: dict[str, Path],
    recorded: list[dict[str, Any]],
    read_in_the_sandbox: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """FEAT-E592: nothing on this side, and the candidate leg still composes."""
    assert not repos[REPO_WITH].exists()

    await _drive(config, repos, REPO_WITH, tmp_path)

    # Asked of the sandbox's sidecar, by the repository's key, for the
    # committed file. No build is recorded here, so it is the committed HEAD —
    # the same rule the press uses.
    assert read_in_the_sandbox == [
        {
            "base_url": SANDBOX_SIDECAR,
            "repo": REPO_WITH,
            "commit": "HEAD",
            "file_path": "deploy/profile.yaml",
        }
    ]
    (call,) = recorded
    assert call["profile"].env_id == "apitest"
    assert call["profile"].cwd == "/sandbox/clone/api_test"
    assert call["leg"] == "candidate_check"


@pytest.mark.asyncio
async def test_a_profile_the_sandbox_cannot_give_stops_the_leg_in_its_own_words(
    config: ForgeConfig,
    repos: dict[str, Path],
    recorded: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _refused(self: Any, commit: str, file_path: str) -> FileAtCommit:
        return FileAtCommit(refusal="the sandbox sidecar could not be reached")

    monkeypatch.setattr(
        "forge.deploy.sidecar_git.SidecarCandidateGit.read_file_at_commit", _refused
    )

    with pytest.raises(DeployProfileError) as raised:
        await _drive(config, repos, REPO_WITH, tmp_path)

    said = str(raised.value)
    assert "api-test-factory" in said
    assert "the sandbox sidecar could not be reached" in said
    # Never the path on this side, which is not where the repository lives.
    assert str(repos[REPO_WITH]) not in said
    assert recorded == []


@pytest.mark.asyncio
async def test_a_repository_without_a_sandbox_never_asks_a_sandbox(
    config: ForgeConfig,
    repos: dict[str, Path],
    recorded: list[dict[str, Any]],
    read_in_the_sandbox: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    await _drive(config, repos, REPO_WITHOUT, tmp_path)

    assert read_in_the_sandbox == []
    (call,) = recorded
    assert call["profile"].source_ref == str(
        repos[REPO_WITHOUT] / "deploy" / "profile.yaml"
    )
