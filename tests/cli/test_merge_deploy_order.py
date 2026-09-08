"""The attended ``forge merge-deploy`` command follows the merge card's order.

Protect-main (Part J, rule 39): the candidate is checked in the sandbox
FIRST; only if every check passes does the merge land and that exact build
get promoted. The command drives the same executor as the card press, so it
cannot keep the old order — this proves it, with the NATS, guardkit and
deploy seams faked and a real git repository for the branch.
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

FEATURE_ID = "FEAT-ORD1"
BUILD_ID = "build-FEAT-ORD1-20260907"
REPO = "appmilla/api_test"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _receipts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


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
    return root


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    pool = SqliteLifecyclePersistence(connection=cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, "
        "status, triggered_by, correlation_id, queued_at, mode) VALUES "
        "(?, ?, ?, ?, 'f.yaml', 'COMPLETE', 'cli', ?, '2026-09-07T00:00:00Z', 'mode-a')",
        (BUILD_ID, FEATURE_ID, REPO, f"autobuild/{FEATURE_ID}", f"corr-{BUILD_ID}"),
    )
    cx.commit()
    return pool


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
        }
    )


class _Publisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    pool: SqliteLifecyclePersistence,
    repo_root: Path,
    *,
    candidate_verdict: str = "pass",
) -> dict[str, Any]:
    order: list[str] = []
    publisher = _Publisher()
    merged = _git(repo_root, "rev-parse", f"autobuild/{FEATURE_ID}")

    async def _guardkit(**kwargs: Any) -> Any:
        order.append("merge")
        return SimpleNamespace(
            status="success",
            stdout_tail=json.dumps({"status": "merged", "merged_sha": merged}),
            stderr=None, exit_code=0, artefacts=[],
        )

    async def _deploy(**kwargs: Any) -> Any:
        leg = kwargs.get("leg", "deploy")
        order.append(leg)
        if leg == "candidate_check":
            if candidate_verdict == "pass":
                return SimpleNamespace(
                    outcome="complete", verdict="pass", failed_step=None,
                    events=("DeployQueued",),
                    detail={"gate_summary": {"verdict": "pass", "checks_total": 5,
                                             "checks_passed": 5, "failed_checks": []},
                            "candidate": "standing"},
                )
            return SimpleNamespace(
                outcome="failed", verdict="fail", failed_step="candidate_gate",
                events=("DeployQueued", "DeployFailed"),
                detail={"reason": "candidate_failed",
                        "gate_summary": {"verdict": "fail", "checks_total": 5,
                                         "checks_passed": 4, "failed_checks": ["etag"]}},
            )
        if leg == "candidate_down":
            return SimpleNamespace(outcome="complete", detail={"candidate": "torn-down"})
        return SimpleNamespace(
            outcome="complete", verdict="pass", deploy_record_ref="r",
            detail={"candidate": "torn-down"},
        )

    async def _backends(_config: ForgeConfig):
        async def _close() -> None:
            return None

        return publisher, _guardkit, _deploy, _close

    async def _main_sha(_repo_root: Path) -> str | None:
        return _git(repo_root, "rev-parse", "main")

    monkeypatch.setattr(merge_deploy_module, "_open_pool", lambda _p: pool)
    monkeypatch.setattr(merge_deploy_module, "_aopen_backends", _backends)
    monkeypatch.setattr(merge_offer_module, "git_rev_parse_main", _main_sha)
    return {"order": order, "publisher": publisher, "merged": merged}


def test_the_attended_command_checks_then_merges_then_promotes(
    config, pool, repo_root, monkeypatch
) -> None:
    wired = _wire(monkeypatch, pool, repo_root)
    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
    assert result.exit_code == 0, result.output
    assert wired["order"] == ["candidate_check", "merge", "promote"]
    assert "result=merged-and-running" in result.output
    assert "checked in the sandbox before merging: pass (5 of 5 checks passed)" in result.output
    assert f"merged_sha={wired['merged']}" in result.output
    report = wired["publisher"].reports[0]
    assert report.gate_before_merge["verdict"] == "pass"
    assert report.gate_before_merge["trees_match"] is True
    # The tree it laid out is gone again.
    assert not (repo_root / ".forge-candidates" / FEATURE_ID).exists()


def test_the_attended_command_never_merges_a_branch_that_failed_its_check(
    config, pool, repo_root, monkeypatch
) -> None:
    wired = _wire(monkeypatch, pool, repo_root, candidate_verdict="fail")
    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)
    assert result.exit_code == 1
    assert wired["order"] == ["candidate_check"]
    assert "result=candidate-refused" in result.output
    assert "failed_step=candidate" in result.output
    assert (
        f"{FEATURE_ID} was checked in the sandbox before merging and failed 1 of 5 "
        "checks (etag); nothing was merged and the branch is kept."
    ) in result.output
    assert "checked in the sandbox before merging: fail (4 of 5 checks passed)" in result.output
    # main did not move.
    assert _git(repo_root, "rev-parse", "main") != wired["merged"]


# ---------------------------------------------------------------------------
# The attended word merges the branch the build made (Part M, 2026-09-07)
# ---------------------------------------------------------------------------

REPAIR_BUILD_ID = "build-FEAT-MD1-20260907120000"
REPAIR_BRANCH = "fix/TASK-MD1FIX1-07120000"


def _wire_recording_argv(
    monkeypatch: pytest.MonkeyPatch,
    pool: SqliteLifecyclePersistence,
    repo_root: Path,
    *,
    merged: str,
) -> dict[str, Any]:
    """The green wiring, but the merge fake keeps the argv it was given."""
    argv: list[list[str]] = []
    publisher = _Publisher()

    async def _guardkit(**kwargs: Any) -> Any:
        argv.append(list(kwargs["args"]))
        return SimpleNamespace(
            status="success",
            stdout_tail=json.dumps({"status": "merged", "merged_sha": merged}),
            stderr=None, exit_code=0, artefacts=[],
        )

    async def _deploy(**kwargs: Any) -> Any:
        leg = kwargs.get("leg", "deploy")
        if leg == "candidate_check":
            return SimpleNamespace(
                outcome="complete", verdict="pass", failed_step=None,
                events=("DeployQueued",),
                detail={"gate_summary": {"verdict": "pass", "checks_total": 5,
                                         "checks_passed": 5, "failed_checks": []},
                        "candidate": "standing"},
            )
        if leg == "candidate_down":
            return SimpleNamespace(outcome="complete", detail={"candidate": "torn-down"})
        return SimpleNamespace(
            outcome="complete", verdict="pass", deploy_record_ref="r",
            detail={"candidate": "torn-down"},
        )

    async def _backends(_config: ForgeConfig):
        async def _close() -> None:
            return None

        return publisher, _guardkit, _deploy, _close

    async def _main_sha(_repo_root: Path) -> str | None:
        return _git(repo_root, "rev-parse", "main")

    monkeypatch.setattr(merge_deploy_module, "_open_pool", lambda _p: pool)
    monkeypatch.setattr(merge_deploy_module, "_aopen_backends", _backends)
    monkeypatch.setattr(merge_offer_module, "git_rev_parse_main", _main_sha)
    return {"argv": argv, "publisher": publisher}


def test_a_repair_named_by_build_id_merges_its_recorded_branch(
    config, pool, repo_root, monkeypatch
) -> None:
    """Rule 54 on the attended path: the row's ``merge_branch`` is what is checked
    and merged, ``--branch`` reaches the merge command, and the printed line
    names the branch because it is not the feature's own (rule 55)."""
    _git(repo_root, "checkout", "-q", "-b", REPAIR_BRANCH, "main")
    (repo_root / "the-repair.txt").write_text("the repair\n", encoding="utf-8")
    _git(repo_root, "add", "the-repair.txt")
    _git(repo_root, "commit", "-q", "-m", "the repair")
    _git(repo_root, "checkout", "-q", "main")
    repair_tip = _git(repo_root, "rev-parse", REPAIR_BRANCH)
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, "
        "status, triggered_by, correlation_id, queued_at, mode, task_id) VALUES "
        "(?, ?, ?, 'repair/TASK-MD1FIX1', 'f.yaml', 'COMPLETE', 'cli', ?, "
        "'2026-09-07T12:00:00Z', 'mode-c', 'TASK-MD1FIX1')",
        (REPAIR_BUILD_ID, FEATURE_ID, REPO, f"corr-{REPAIR_BUILD_ID}"),
    )
    pool.connection.commit()
    pool.record_merge_branch(REPAIR_BUILD_ID, REPAIR_BRANCH)
    wired = _wire_recording_argv(monkeypatch, pool, repo_root, merged=repair_tip)

    result = CliRunner().invoke(
        merge_deploy_cmd, [FEATURE_ID, "--build-id", REPAIR_BUILD_ID], obj=config
    )

    assert result.exit_code == 0, result.output
    assert wired["argv"][0][-2:] == ["--branch", REPAIR_BRANCH]
    assert f"merge-deploy {FEATURE_ID} (branch {REPAIR_BRANCH}) @ {REPO}: result=merged-and-running" in result.output
    report = wired["publisher"].reports[0]
    assert report.branch == REPAIR_BRANCH
    assert report.gate_before_merge["candidate_sha"] == repair_tip
    assert report.gate_before_merge["trees_match"] is True
    assert not (repo_root / ".forge-candidates" / FEATURE_ID).exists()


def test_a_feature_build_is_merged_without_a_branch_flag_and_named_as_before(
    config, pool, repo_root, monkeypatch
) -> None:
    merged = _git(repo_root, "rev-parse", f"autobuild/{FEATURE_ID}")
    wired = _wire_recording_argv(monkeypatch, pool, repo_root, merged=merged)

    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=config)

    assert result.exit_code == 0, result.output
    assert "--branch" not in wired["argv"][0]
    assert wired["argv"][0][-1] == "--json"
    assert f"merge-deploy {FEATURE_ID} @ {REPO}: result=merged-and-running" in result.output
    assert "(branch " not in result.output
    assert wired["publisher"].reports[0].branch == f"autobuild/{FEATURE_ID}"


# ---------------------------------------------------------------------------
# The attended word presses where the repository lives (sandbox first, rule 89)
# ---------------------------------------------------------------------------
#
# Rich's ruling of 2026-09-07 23:31Z: there is no merge-by-hand shape. The
# attended command drives the same executor as the card, so it too has to reach
# a sandboxed repository's git inside its sandbox — starting with the pin,
# which is the first git the press needs and which it reads itself.


SANDBOX_REPO = "appmilla/api_test_sandboxed"


@pytest.fixture
def sandbox_clone(tmp_path: Path) -> Path:
    """The factory's own clone, as it is inside the repository's sandbox."""
    root = tmp_path / "sandbox" / "api_test"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    return root.resolve()


@pytest.fixture
def sandbox_sidecar(sandbox_clone: Path, monkeypatch: pytest.MonkeyPatch):
    """The real deploy sidecar, on a real loopback port, inside the sandbox."""
    import threading

    from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV, build_server

    monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
    inside = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(sandbox_clone.parent)]}},
            "planning": {"target_repo_paths": {SANDBOX_REPO: str(sandbox_clone)}},
        }
    )
    srv = build_server(port=0, config_loader=lambda: inside)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_attended_command_presses_a_sandboxed_repository_in_its_sandbox(
    pool, tmp_path, sandbox_clone, sandbox_sidecar, monkeypatch
) -> None:
    # forge-prod has no checkout of this repository at all — rule 63.
    on_this_side = tmp_path / "not-mounted" / "api_test"
    settings = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {SANDBOX_REPO: str(on_this_side)},
                "sandboxes": {
                    SANDBOX_REPO: {
                        "name": "api-test-factory",
                        "sidecar_url": sandbox_sidecar,
                        "runner_url": "http://127.0.0.1:8924",
                    }
                },
            },
            "approval": {"expected_approver": "rich"},
        }
    )
    pool.connection.execute(
        "UPDATE builds SET repo = ? WHERE build_id = ?", (SANDBOX_REPO, BUILD_ID)
    )
    pool.connection.commit()
    wired = _wire(monkeypatch, pool, sandbox_clone)

    # The pin is NOT faked here: it is read wherever the press reads it, and
    # this side has no repository for it to be read from.
    monkeypatch.setattr(
        merge_offer_module,
        "git_rev_parse_main",
        _refuse_main_sha_on_this_side,
    )

    result = CliRunner().invoke(merge_deploy_cmd, [FEATURE_ID], obj=settings)

    assert result.exit_code == 0, result.output
    assert wired["order"] == ["candidate_check", "merge", "promote"]
    assert "result=merged-and-running" in result.output
    # The candidate was laid out in the clone, and taken away again.
    assert not (on_this_side / ".forge-candidates").exists()
    assert not (sandbox_clone / ".forge-candidates" / FEATURE_ID).exists()


async def _refuse_main_sha_on_this_side(_repo_root: Path) -> str | None:
    raise AssertionError(
        "a sandboxed repository's main was read on this side; it lives in the "
        "sandbox's clone"
    )
