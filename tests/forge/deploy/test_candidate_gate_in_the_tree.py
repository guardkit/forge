"""The candidate's live gate reads the candidate's tree (protect-main, 2026-09-07).

The coach's finding on the lane: the compose and health steps ran in the
feature branch's laid-out tree, but the live gate ran in the checkout — at
main — so the branch's build was checked against main's gate registry and
main's Hurl twins. The per-feature gate is registered ON the branch and only
reaches main with the merge, so the check was blind to it.

Here a real temporary git checkout is laid out through the real
:mod:`forge.deploy.candidate_tree` code and gated through the real
:class:`RepoDriverLiveGateInvoker`. The one stand-in is the repository's
driver script itself: a stub that does what the real drivers do at this
boundary — reads ``qa/gates/registry.yaml`` relative to ``--repo`` (default
``.``, i.e. its working directory), writes its evidence under
``<repo>/qa/gates/evidence/<run_id>/``, and prints the results envelope. The
stage runs dry, so no deploy script is executed; the gate is not a dry-run
step and runs for real.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.config.models import DeployStageConfig
from forge.deploy.candidate_tree import (
    ensure_candidate_trees_excluded,
    git_rev_parse,
    materialise_candidate_tree,
    remove_candidate_tree,
)
from forge.deploy.live_gate import DryRunBrokerInspector, RepoDriverLiveGateInvoker
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.stage import DeployStageRunner
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
FEATURE_ID = "FEAT-TR33"
DRIVER = ["python3", "qa/gates/local_live_gate.py"]

#: The stand-in driver. A gate whose id starts with ``red_`` fails.
STUB_DRIVER = '''#!/usr/bin/env python3
import argparse, json, os, re, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--feature", required=True)
ap.add_argument("--target", required=True)
ap.add_argument("--gates", default=None)
ap.add_argument("--repo", default=".")
args = ap.parse_args()
repo = Path(args.repo)
registry = repo / "qa" / "gates" / "registry.yaml"
run_id = f"run-{args.feature}"
if not registry.is_file():
    print(json.dumps({"run_id": run_id, "verdict": "instrument_fail", "gates": [],
                      "evidence_index_ref": ""}))
    sys.exit(3)
ids = re.findall(r"^\\s*-\\s*id:\\s*(\\S+)", registry.read_text(), flags=re.M)
gates = []
for gid in ids:
    ok = not gid.startswith("red_")
    gates.append({"gate_id": gid, "exit_code": 0 if ok else 1,
                  "assertions": [{"id": gid + "::status", "status": "pass" if ok else "fail"}]})
verdict = "pass" if all(g["exit_code"] == 0 for g in gates) else "fail"
evidence = repo / "qa" / "gates" / "evidence" / run_id
evidence.mkdir(parents=True, exist_ok=True)
(evidence / "index.json").write_text(json.dumps({
    "cwd": os.getcwd(), "registry": str(registry.resolve()), "gates": ids}))
print(json.dumps({"run_id": run_id, "verdict": verdict, "gates": gates,
                  "evidence_index_ref": "qa/gates/evidence/" + run_id + "/index.json"}))
sys.exit(0 if verdict == "pass" else 1)
'''


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _registry(*gate_ids: str) -> str:
    return "gates:\n" + "".join(f"  - id: {g}\n" for g in gate_ids)


def _checkout(tmp_path: Path, *, main_registry: str | None, branch_registry: str) -> Path:
    """main carries the driver (and ``main_registry`` when given); the branch
    ``autobuild/FEAT-TR33`` carries ``branch_registry`` — registered on the
    branch, as the per-feature gate is."""
    root = tmp_path / "api_test"
    (root / "qa" / "gates").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "qa" / "gates" / "local_live_gate.py").write_text(STUB_DRIVER, encoding="utf-8")
    (root / "deploy" / "deploy.sh").write_text("#!/bin/sh\necho main\n", encoding="utf-8")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    # The checkout's own evidence directory is ignored, as api_test's is.
    (root / ".gitignore").write_text("qa/gates/evidence/\n", encoding="utf-8")
    if main_registry is not None:
        (root / "qa" / "gates" / "registry.yaml").write_text(main_registry, encoding="utf-8")
    _git(root, "init", "-b", "main", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "qa" / "gates" / "registry.yaml").write_text(branch_registry, encoding="utf-8")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "the feature, with its gate registered")
    _git(root, "checkout", "-q", "main")
    return root


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(conn)
    return RunbookRepository(connection=conn)


@pytest.fixture
def runbook_publisher() -> AsyncMock:
    pub = AsyncMock()
    for name in (
        "publish_runbook_started", "publish_step_started", "publish_step_result",
        "publish_runbook_complete", "publish_escalated",
    ):
        setattr(pub, name, AsyncMock())
    return pub


class _Publisher:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("publish_"):
            async def _record(payload: Any) -> None:
                self.events.append(name.removeprefix("publish_"))
            return _record
        raise AttributeError(name)


def _profile(checkout: Path):
    return parse_deploy_profile(
        {
            "env_id": "apitest-f2",
            "compose": {"file": "docker-compose.yml", "script": "deploy/deploy.sh"},
            "health_checks": [{"cmd": "deploy/deploy.sh"}],
            "cwd": str(checkout),
            "candidate": {"env": {"API_TEST_BASE_URL": "http://localhost:8902"}},
            "rollback_image_ref": "apitest:rollback",
        }
    )


def _runner(repository, runbook_publisher, checkout: Path, tmp_path: Path) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=_Publisher(),
        reservation=InProcessReservationLease(),
        # Composed as production composes it: pointed at the checkout.
        live_gate_invoker=RepoDriverLiveGateInvoker(repo_path=checkout, driver_argv=DRIVER),
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=True,
        clock=lambda: FIXED,
    )


async def _lay_out(checkout: Path) -> Path:
    tip = await git_rev_parse(checkout, f"autobuild/{FEATURE_ID}")
    assert tip
    await ensure_candidate_trees_excluded(checkout)
    return await materialise_candidate_tree(checkout, FEATURE_ID, tip)


def _gate_payload(repository: RunbookRepository, runbook_id: str, corr: str) -> dict[str, Any]:
    rb = repository.load_runbook(runbook_id, correlation_id=corr)
    assert rb is not None, runbook_id
    return dict(rb.steps[0].result.payload)


@pytest.mark.asyncio
async def test_the_candidate_gate_reads_the_trees_registry_and_the_promote_reads_the_checkouts(
    repository, runbook_publisher, tmp_path
) -> None:
    checkout = _checkout(
        tmp_path,
        main_registry=_registry("health"),
        branch_registry=_registry("health", "users_count", "etag"),
    )
    tree = await _lay_out(checkout)
    runner = _runner(repository, runbook_publisher, checkout, tmp_path)

    checked = await runner.candidate_check(
        _profile(checkout),
        correlation_id="c",
        deploy_run_id="run-1",
        feature=FEATURE_ID,
        feat_id=FEATURE_ID,
        candidate_cwd=str(tree),
    )
    assert checked.outcome == "complete", checked
    summary = checked.detail["gate_summary"]
    # The branch registered three checks; main knows one. The candidate saw three.
    assert summary["gate_ids"] == ["health", "users_count", "etag"]
    assert (summary["checks_total"], summary["checks_passed"]) == (3, 3)
    assert summary["failed_checks"] == []
    assert summary["candidate_cwd"] == str(tree)
    assert summary["evidence_index_ref"] == f"qa/gates/evidence/run-{FEATURE_ID}/index.json"
    # The driver ran in the tree and read the tree's registry.
    index = json.loads((tree / summary["evidence_index_ref"]).read_text(encoding="utf-8"))
    assert Path(index["cwd"]).resolve() == tree.resolve()
    assert Path(index["registry"]) == (tree / "qa" / "gates" / "registry.yaml").resolve()
    assert index["gates"] == ["health", "users_count", "etag"]
    assert not (checkout / "qa" / "gates" / "evidence").exists()

    promoted = await runner.promote(
        _profile(checkout),
        correlation_id="c",
        deploy_run_id="run-1",
        feature=FEATURE_ID,
        feat_id=FEATURE_ID,
        prior_events=checked.events,
    )
    assert promoted.outcome == "complete", promoted
    # The promote's gate ran in the checkout, as before, and saw main's one check.
    live_gate = _gate_payload(repository, "live-gate-run-1", "c")
    assert live_gate["gate_ids"] == ["health"]
    live_index = json.loads(
        (checkout / live_gate["evidence_index_ref"]).read_text(encoding="utf-8")
    )
    assert Path(live_index["cwd"]).resolve() == checkout.resolve()
    assert live_index["gates"] == ["health"]


@pytest.mark.asyncio
async def test_a_red_check_registered_on_the_branch_is_named_and_the_candidate_torn_down(
    repository, runbook_publisher, tmp_path
) -> None:
    checkout = _checkout(
        tmp_path,
        main_registry=_registry("health"),
        branch_registry=_registry("health", "users_count", "red_etag"),
    )
    tree = await _lay_out(checkout)
    runner = _runner(repository, runbook_publisher, checkout, tmp_path)

    checked = await runner.candidate_check(
        _profile(checkout),
        correlation_id="r",
        deploy_run_id="run-2",
        feature=FEATURE_ID,
        candidate_cwd=str(tree),
    )
    assert checked.outcome == "failed"
    assert checked.failed_step == "candidate_gate"
    summary = checked.detail["gate_summary"]
    # Main's registry alone would have passed this build: its one check is green.
    assert summary["verdict"] == "fail"
    assert (summary["checks_total"], summary["checks_passed"]) == (3, 2)
    assert summary["failed_checks"] == ["red_etag"]
    assert repository.load_runbook("teardown-cand-run-2", correlation_id="r") is not None
    assert repository.load_runbook("deploy-run-2", correlation_id="r") is None
    assert (tree / summary["evidence_index_ref"]).is_file()


@pytest.mark.asyncio
async def test_the_evidence_goes_with_the_tree_and_the_numbers_do_not(
    repository, runbook_publisher, tmp_path
) -> None:
    """Rule 38: the tree is removed when the run ends. The driver's evidence
    lives under it and goes with it; the checkout is never written to before
    the merge (the merge refuses a dirty checkout); and the verdict, the
    counts and the names are on the runbook record and in the summary."""
    checkout = _checkout(
        tmp_path,
        main_registry=None,
        branch_registry=_registry("health", "users_count", "etag"),
    )
    tree = await _lay_out(checkout)
    runner = _runner(repository, runbook_publisher, checkout, tmp_path)

    checked = await runner.candidate_check(
        _profile(checkout),
        correlation_id="e",
        deploy_run_id="run-3",
        feature=FEATURE_ID,
        candidate_cwd=str(tree),
    )
    assert checked.outcome == "complete"
    summary = checked.detail["gate_summary"]
    evidence = tree / "qa" / "gates" / "evidence"
    assert evidence.is_dir()
    assert not (checkout / "qa" / "gates" / "evidence").exists()
    # The shared checkout is as clean as the merge command needs it.
    assert _git(checkout, "status", "--porcelain") == ""

    assert await remove_candidate_tree(tree) is True
    assert not tree.exists()
    assert not evidence.exists()
    # What the report reads was copied out before the teardown: the gate
    # step's own record in the runbook DB, and the summary the leg returned.
    on_record = _gate_payload(repository, "live-gate-cand-run-3", "e")
    assert on_record["verdict"] == "pass"
    assert on_record["gate_ids"] == ["health", "users_count", "etag"]
    assert on_record["evidence_index_ref"] == summary["evidence_index_ref"]
    assert (summary["checks_total"], summary["checks_passed"]) == (3, 3)
    assert _git(checkout, "status", "--porcelain") == ""

    # And with no tree given, the same invoker gates the checkout — which has
    # no registry on main: an instrument problem, honestly reported.
    plain = await runner.candidate_check(
        _profile(checkout), correlation_id="p", deploy_run_id="run-4", feature=FEATURE_ID
    )
    assert plain.outcome == "failed"
    assert plain.detail["gate_summary"]["verdict"] == "instrument_fail"
