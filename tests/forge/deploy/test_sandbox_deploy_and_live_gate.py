"""The deploy stage and the live gate for a repository that has a sandbox
(sandbox first, 2026-09-07, rule 85).

Rich's rule: nothing the factory runs on a repository runs on the host. When
the factory itself lives inside a repository's sandbox, three things follow
and each is proved here:

* the stage's scripts go to the deploy sidecar INSIDE that sandbox, not the
  host one;
* the deploy step runs the repository's own ``deploy/deploy.sh``, never the
  host wrapper ``deploy/sandbox-deploy.sh`` — the wrapper calls ``sbx``, and
  a sandbox cannot be made from inside one;
* the live gate's driver runs through that same sidecar, in the candidate's
  own tree, because the candidate's port is on the sandbox's own loopback.

The sidecar here is the REAL service on a real ephemeral loopback port,
serving the real routes against a real temporary repository. Nothing live is
touched: no ``sbx``, no docker, no sandbox, no service on the box. The one
stand-in is the repository's own driver script, which does what the real
drivers do at this boundary.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from forge.config.models import DeployStageConfig, ForgeConfig
from forge.deploy.candidate_tree import (
    ensure_candidate_trees_excluded,
    git_rev_parse,
    materialise_candidate_tree,
)
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    RepoDriverLiveGateInvoker,
    SidecarLiveGateInvoker,
)
from forge.deploy.profile import load_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.sidecar_runner import SidecarScriptRunner
from forge.deploy.stage import DeployStageRunner
from forge.deploy_sidecar.service import build_server
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
FEATURE_ID = "FEAT-SBX9"
REPO_KEY = "guardkit/api_test"
DRIVER = ["python3", "qa/gates/local_live_gate.py"]

SANDBOX = SimpleNamespace(
    name="api-test-factory",
    sidecar_url="http://127.0.0.1:9",
    runner_url="http://127.0.0.1:8924",
)

#: The stand-in driver: reads the registry relative to its working directory,
#: writes its evidence there, prints the results envelope, exits by verdict.
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
    "cwd": os.getcwd(), "base_url": os.environ.get("API_TEST_BASE_URL", ""),
    "registry": str(registry.resolve()), "gates": ids}))
print(json.dumps({"run_id": run_id, "verdict": verdict, "gates": gates,
                  "evidence_index_ref": "qa/gates/evidence/" + run_id + "/index.json"}))
sys.exit(0 if verdict == "pass" else 1)
'''

#: The repository's own deploy script, and the HOST wrapper beside it. Each
#: writes its own name so a test can say which one actually ran.
INNER_SCRIPT = '''#!/bin/sh
printf 'inner deploy.sh ran in %s with CANDIDATE=%s\\n' "$(pwd)" "${CANDIDATE:-}"
printf '%s\\n' "$(pwd)" >> "${MARKER_DIR}/deploy.sh.ran"
# Which sandbox settings this run was handed, if any. Inside the sandbox the
# answer must be none of them: they say how to MAKE the sandbox, and this
# script is already in it.
env | grep '^SANDBOX_' > "${MARKER_DIR}/deploy.sh.sandbox-env" 2>/dev/null || true
'''
WRAPPER_SCRIPT = '''#!/bin/sh
printf 'the HOST wrapper ran — it would have called sbx\\n'
printf '%s\\n' "$(pwd)" >> "${MARKER_DIR}/sandbox-deploy.sh.ran"
'''
HEALTHCHECK = '''#!/bin/sh
exit 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
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
    ).stdout.strip()


def _profile_yaml(root: Path) -> dict[str, Any]:
    return {
        "env_id": "apitest-sbx",
        "compose": {
            "file": "docker-compose.yml",
            "script": "deploy/sandbox-deploy.sh",
        },
        "sandbox": {"name": "api-test-deploy"},
        "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
        "cwd": str(root),
        "live_gate": {
            "driver": DRIVER,
            "gates": [],
            "timeout_seconds": 120,
            "env": {"API_TEST_BASE_URL": "http://localhost:8901"},
        },
        "candidate": {
            "env": {
                "CANDIDATE_PORT": "8902",
                "API_TEST_BASE_URL": "http://localhost:8902",
            },
            "keep": False,
        },
        "rollback_image_ref": "apitest:rollback",
    }


@pytest.fixture
def clone(tmp_path: Path, marker_dir: Path) -> Path:
    """Stands in for the factory's own clone inside the sandbox.

    THE TWO DEPLOY SCRIPTS ARE WRITTEN WITH THEIR MARKER FOLDER BAKED IN (23
    September 2026). They used to read ``$MARKER_DIR`` out of the environment
    they inherited, and that stopped working the day the environment door
    closed: a deploy script's environment is now BUILT from the factory's own
    named list plus what the project itself declared, so a setting a test
    happens to put in its own process no longer travels into it. Baking the
    path keeps exactly what these tests prove — which script ran, and in which
    working directory — without asking the door to stay open for them.
    """
    root = tmp_path / "api_test"
    (root / "qa" / "gates").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "qa" / "gates" / "local_live_gate.py").write_text(
        STUB_DRIVER, encoding="utf-8"
    )
    for name, body in (
        ("deploy.sh", INNER_SCRIPT),
        ("sandbox-deploy.sh", WRAPPER_SCRIPT),
        ("healthcheck.sh", HEALTHCHECK),
    ):
        path = root / "deploy" / name
        path.write_text(
            body.replace("${MARKER_DIR}", str(marker_dir)), encoding="utf-8"
        )
        path.chmod(0o755)
    (root / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(_profile_yaml(root)), encoding="utf-8"
    )
    (root / "qa" / "gates" / "registry.yaml").write_text(
        "gates:\n  - id: health\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text("qa/gates/evidence/\n", encoding="utf-8")
    _git(root, "init", "-b", "main", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}")
    (root / "qa" / "gates" / "registry.yaml").write_text(
        "gates:\n  - id: health\n  - id: users_count\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "the feature, with its gate registered")
    _git(root, "checkout", "-q", "main")
    return root


@pytest.fixture
def marker_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the two deploy scripts record that they ran."""
    where = tmp_path / "markers"
    where.mkdir()
    monkeypatch.setenv("MARKER_DIR", str(where))
    return where


@pytest.fixture
def sidecar(clone: Path, monkeypatch: pytest.MonkeyPatch):
    """The REAL sidecar on an ephemeral loopback port — the shape inside a
    sandbox, where the clone lives at the path the repository map names.

    It says it is inside a sandbox the way the real one does: the in-sandbox
    bootstrap sets ``FORGE_SIDECAR_IN_SANDBOX``, and only a sidecar carrying
    that value will run the repository's own ``deploy/deploy.sh`` rather than
    the host wrapper (L3b's coach, 2026-09-08).
    """
    from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

    monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    url = f"http://{host}:{port}"
    holder["config"] = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {
                "target_repo_paths": {REPO_KEY: str(clone)},
                "sandboxes": {
                    REPO_KEY: {
                        "name": "api-test-factory",
                        "sidecar_url": url,
                        "runner_url": "http://127.0.0.1:8924",
                    }
                },
            },
        }
    )
    try:
        yield SimpleNamespace(
            url=url,
            entry=SimpleNamespace(
                name="api-test-factory", sidecar_url=url, runner_url="http://x:1"
            ),
        )
    finally:
        srv.shutdown()
        srv.server_close()


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
        "publish_runbook_started",
        "publish_step_started",
        "publish_step_result",
        "publish_runbook_complete",
        "publish_escalated",
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


def _stage(
    *,
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    clone: Path,
    tmp_path: Path,
    sidecar_url: str,
    sandbox: Any | None,
    invoker: Any,
) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=_Publisher(),
        reservation=InProcessReservationLease(),
        live_gate_invoker=invoker,
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(
            execution_surface="sidecar", sidecar_url=sidecar_url
        ),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=False,
        clock=lambda: FIXED,
        target_repo=REPO_KEY,
        target_repo_root=str(clone),
        sandbox=sandbox,
        # These drives name no build — they are by-hand runs, and since 23
        # September 2026 the helper will not read a project's declarations for
        # a request that says nothing about whose work it is. So they say it.
        by_hand=True,
    )


async def _lay_out(clone: Path) -> Path:
    tip = await git_rev_parse(clone, f"autobuild/{FEATURE_ID}")
    assert tip
    await ensure_candidate_trees_excluded(clone)
    return await materialise_candidate_tree(clone, FEATURE_ID, tip)


# ---------------------------------------------------------------------------
# Which sidecar, and which script
# ---------------------------------------------------------------------------


class TestWhichSidecarTheStagesScriptsGoTo:
    def _runner(self, *, sandbox: Any | None) -> Any:
        return DeployStageRunner(
            repository=object(),  # type: ignore[arg-type] — the seam probe only
            runbook_publisher=object(),
            deploy_publisher=object(),
            reservation=object(),  # type: ignore[arg-type]
            live_gate_invoker=object(),  # type: ignore[arg-type]
            broker_inspector=object(),  # type: ignore[arg-type]
            config=DeployStageConfig(
                execution_surface="sidecar", sidecar_url="http://127.0.0.1:8125"
            ),
            deploy_record_root="docs/state",
            target_repo=REPO_KEY,
            sandbox=sandbox,
        )._resolve_script_runner()

    def test_without_a_sandbox_the_host_address_is_used_as_before(self) -> None:
        runner = self._runner(sandbox=None)
        assert isinstance(runner, SidecarScriptRunner)
        assert runner._base_url == "http://127.0.0.1:8125"

    def test_with_a_sandbox_the_sidecar_inside_it_is_used(self) -> None:
        runner = self._runner(sandbox=SANDBOX)
        assert isinstance(runner, SidecarScriptRunner)
        assert runner._base_url == "http://127.0.0.1:9"


class TestWhichDeployScriptIsSent:
    def _stage_for(self, sandbox: Any | None, clone: Path) -> DeployStageRunner:
        return DeployStageRunner(
            repository=object(),  # type: ignore[arg-type]
            runbook_publisher=object(),
            deploy_publisher=object(),
            reservation=object(),  # type: ignore[arg-type]
            live_gate_invoker=object(),  # type: ignore[arg-type]
            broker_inspector=object(),  # type: ignore[arg-type]
            config=DeployStageConfig(),
            deploy_record_root="docs/state",
            target_repo=REPO_KEY,
            sandbox=sandbox,
        )

    def test_without_a_sandbox_the_profile_is_returned_unchanged(
        self, clone: Path
    ) -> None:
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = self._stage_for(None, clone)

        assert stage._profile_for_run(profile) is profile
        assert profile.compose.script == "deploy/sandbox-deploy.sh"

    def test_with_a_sandbox_the_wrapper_is_replaced_by_its_inner_script(
        self, clone: Path
    ) -> None:
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = self._stage_for(SANDBOX, clone)

        swapped = stage._profile_for_run(profile)

        assert swapped.compose.script == "deploy/deploy.sh"
        # Nothing else about the profile moved.
        assert swapped.env_id == profile.env_id
        assert swapped.cwd == profile.cwd
        assert swapped.candidate == profile.candidate
        assert swapped.live_gate == profile.live_gate

    def test_a_profile_that_names_no_wrapper_is_left_alone(
        self, clone: Path
    ) -> None:
        from dataclasses import replace as dc_replace

        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        plain = dc_replace(
            profile, compose=dc_replace(profile.compose, script="deploy/deploy.sh")
        )
        stage = self._stage_for(SANDBOX, clone)

        assert stage._profile_for_run(plain) is plain


@pytest.mark.asyncio
class TestTheDeployStepRunsTheInnerScriptInTheSandbox:
    async def test_the_candidate_leg_runs_deploy_sh_and_never_the_wrapper(
        self,
        repository: RunbookRepository,
        runbook_publisher: AsyncMock,
        clone: Path,
        tmp_path: Path,
        sidecar: Any,
        marker_dir: Path,
    ) -> None:
        tree = await _lay_out(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = _stage(
            repository=repository,
            runbook_publisher=runbook_publisher,
            clone=clone,
            tmp_path=tmp_path,
            sidecar_url="http://127.0.0.1:9",  # the host one, deliberately dead
            sandbox=sidecar.entry,
            invoker=SidecarLiveGateInvoker(
                base_url=sidecar.url,
                repo=REPO_KEY,
                repo_path=clone,
                driver_argv=DRIVER,
                timeout_seconds=120,
                extra_env={"API_TEST_BASE_URL": "http://localhost:8901"},
                by_hand=True,
            ),
        )

        checked = await stage.candidate_check(
            profile,
            correlation_id="c",
            deploy_run_id="run-sbx-1",
            feature=FEATURE_ID,
            feat_id=FEATURE_ID,
            candidate_cwd=str(tree),
        )

        assert checked.outcome == "complete", checked
        # The repository's own deploy script ran, in the candidate's tree.
        ran = (marker_dir / "deploy.sh.ran").read_text(encoding="utf-8").split()
        assert [Path(p).resolve() for p in ran] == [tree.resolve()]
        # The host wrapper — the one that calls sbx — was never run.
        assert not (marker_dir / "sandbox-deploy.sh.ran").exists()

    async def test_a_sidecar_on_the_host_refuses_the_inner_script(
        self,
        repository: RunbookRepository,
        runbook_publisher: AsyncMock,
        clone: Path,
        tmp_path: Path,
        sidecar: Any,
        marker_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same request, to a sidecar that is NOT inside a sandbox.

        Nothing in the estate sends it — the deploy stage only sends the inner
        script when the repository has a sandbox, and then it dials that
        sandbox — but the wall has to be real, or the host sidecar could be
        asked to run a repository's deploy against the host's Docker engine.
        """
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        tree = await _lay_out(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = _stage(
            repository=repository,
            runbook_publisher=runbook_publisher,
            clone=clone,
            tmp_path=tmp_path,
            sidecar_url="http://127.0.0.1:9",
            sandbox=sidecar.entry,
            invoker=None,
        )

        checked = await stage.candidate_check(
            profile,
            correlation_id="c",
            deploy_run_id="run-sbx-host-wall",
            feature=FEATURE_ID,
            feat_id=FEATURE_ID,
            candidate_cwd=str(tree),
        )

        assert checked.outcome != "complete"
        assert not (marker_dir / "deploy.sh.ran").exists()
        assert not (marker_dir / "sandbox-deploy.sh.ran").exists()

    async def test_without_a_sandbox_the_wrapper_is_what_runs(
        self,
        repository: RunbookRepository,
        runbook_publisher: AsyncMock,
        clone: Path,
        tmp_path: Path,
        sidecar: Any,
        marker_dir: Path,
    ) -> None:
        tree = await _lay_out(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = _stage(
            repository=repository,
            runbook_publisher=runbook_publisher,
            clone=clone,
            tmp_path=tmp_path,
            sidecar_url=sidecar.url,
            sandbox=None,
            invoker=RepoDriverLiveGateInvoker(
                repo_path=clone,
                driver_argv=DRIVER,
                timeout_seconds=120,
                extra_env={"API_TEST_BASE_URL": "http://localhost:8901"},
            ),
        )

        checked = await stage.candidate_check(
            profile,
            correlation_id="c",
            deploy_run_id="run-plain-1",
            feature=FEATURE_ID,
            feat_id=FEATURE_ID,
            candidate_cwd=str(tree),
        )

        assert checked.outcome == "complete", checked
        assert (marker_dir / "sandbox-deploy.sh.ran").exists()
        assert not (marker_dir / "deploy.sh.ran").exists()


# ---------------------------------------------------------------------------
# The live gate through the sandbox's sidecar
# ---------------------------------------------------------------------------


class TestTheLiveGateRunsInsideTheSandbox:
    def test_the_driver_runs_in_the_candidate_tree_over_a_real_sidecar(
        self, clone: Path, sidecar: Any
    ) -> None:
        import asyncio

        tree = asyncio.run(_lay_out(clone))
        invoker = SidecarLiveGateInvoker(
            base_url=sidecar.url,
            repo=REPO_KEY,
            repo_path=clone,
            driver_argv=DRIVER,
            timeout_seconds=120,
            extra_env={"API_TEST_BASE_URL": "http://localhost:8901"},
            by_hand=True,
        ).with_repo_path(tree).with_extra_env(
            {"API_TEST_BASE_URL": "http://localhost:8902"}
        )

        answer = invoker.invoke(feature=FEATURE_ID, target="apitest-sbx")

        assert answer.verdict == "pass", answer.detail
        # The branch registered two checks; main knows one. The candidate saw two.
        assert answer.gate_ids == ("health", "users_count")
        assert answer.detail["cwd"] == str(tree)
        assert answer.detail["source"] == "results_envelope"
        assert answer.detail["sidecar"] == sidecar.url
        # It really ran there, and with the candidate's own address.
        index = json.loads(
            (tree / answer.evidence_index_ref).read_text(encoding="utf-8")
        )
        assert Path(index["cwd"]).resolve() == tree.resolve()
        assert index["base_url"] == "http://localhost:8902"
        assert index["gates"] == ["health", "users_count"]

    def test_the_argv_is_the_one_the_subprocess_backend_would_have_run(
        self, clone: Path, sidecar: Any
    ) -> None:
        """Byte for byte the same command, wherever it runs."""
        here = RepoDriverLiveGateInvoker(
            repo_path=clone, driver_argv=DRIVER, timeout_seconds=120
        )
        there = SidecarLiveGateInvoker(
            base_url=sidecar.url,
            repo=REPO_KEY,
            repo_path=clone,
            driver_argv=DRIVER,
            timeout_seconds=120,
        )

        mine = here.invoke(feature=FEATURE_ID, target="apitest-sbx", gates=("health",))
        theirs = there.invoke(
            feature=FEATURE_ID, target="apitest-sbx", gates=("health",)
        )

        assert theirs.detail["argv"] == mine.detail["argv"]
        assert theirs.detail["argv"] == [
            "python3",
            "qa/gates/local_live_gate.py",
            "--feature",
            FEATURE_ID,
            "--target",
            "apitest-sbx",
            "--gates",
            "health",
        ]
        assert theirs.verdict == mine.verdict == "pass"

    def test_a_sidecar_that_cannot_be_reached_is_an_instrument_failure(
        self, clone: Path
    ) -> None:
        invoker = SidecarLiveGateInvoker(
            base_url="http://127.0.0.1:9",
            repo=REPO_KEY,
            repo_path=clone,
            driver_argv=DRIVER,
            timeout_seconds=1,
        )

        answer = invoker.invoke(feature=FEATURE_ID, target="apitest-sbx")

        # Never a fail: the gate could not be run, which never indicts the
        # system under test.
        assert answer.verdict == "instrument_fail"
        assert "could not be reached" in answer.detail["error"]

    def test_a_driver_the_profile_does_not_declare_is_refused_not_run(
        self, clone: Path, sidecar: Any
    ) -> None:
        invoker = SidecarLiveGateInvoker(
            base_url=sidecar.url,
            repo=REPO_KEY,
            repo_path=clone,
            driver_argv=["python3", "qa/gates/something_else.py"],
            timeout_seconds=120,
        )

        answer = invoker.invoke(feature=FEATURE_ID, target="apitest-sbx")

        assert answer.verdict == "instrument_fail"
        assert "refused the live gate" in answer.detail["error"]
        assert "deploy/profile.yaml declares" in answer.detail["error"]


# ---------------------------------------------------------------------------
# The environment the venue can use (2026-09-09)
# ---------------------------------------------------------------------------

#: A sandbox that carries the factory's own services: every setting the
#: profile's sandbox block allows, which is the shape api_test had on the day
#: the first merge press was refused.
FULL_SANDBOX_BLOCK = {
    "name": "api-test-deploy",
    "memory": "6g",
    "cpus": 4,
    "publish": ["127.0.0.1:8901:8901", "127.0.0.1:8902:8902"],
    "allow_network": ["pypi.org", "*.debian.org"],
    "sidecar_publish": "127.0.0.1:8925:8125",
    "runner_publish": "127.0.0.1:8924:8124",
    "env_file": "/run/user/1000/forge-sandbox/api-test-deploy.env",
    "forge_path": "/home/rich/Projects/appmilla_github/forge",
    "guardkit_path": "/home/rich/Projects/appmilla_github/guardkit",
    "receipts_path": "/home/rich/forge-state/receipts",
}


def _carry_the_factory(clone: Path) -> None:
    """Give the clone's profile the full sandbox block, both sides of the wire.

    The stage reads the profile it is handed and the sidecar re-reads the same
    file itself, so the one edit serves both.
    """
    raw = _profile_yaml(clone)
    raw["sandbox"] = dict(FULL_SANDBOX_BLOCK)
    (clone / "deploy" / "profile.yaml").write_text(
        yaml.safe_dump(raw), encoding="utf-8"
    )
    _git(clone, "add", "deploy/profile.yaml")
    _git(clone, "commit", "-q", "-m", "the sandbox carries the factory")


@pytest.mark.asyncio
class TestTheDeployIsSentTheEnvironmentItsVenueCanUse:
    """E1: the first real merge press, and why it ended in 0.16 seconds.

    The candidate leg's first step never started. The deploy sidecar inside
    the sandbox refused the request because it carried SANDBOX_SIDECAR_PUBLISH
    — one of the six settings that say how to CREATE the sandbox, sent to a
    deploy that was already inside it. Nothing had ever exercised this path.
    """

    async def test_the_candidate_leg_runs_and_is_sent_none_of_them(
        self,
        repository: RunbookRepository,
        runbook_publisher: AsyncMock,
        clone: Path,
        tmp_path: Path,
        sidecar: Any,
        marker_dir: Path,
    ) -> None:
        _carry_the_factory(clone)
        tree = await _lay_out(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        assert profile.sandbox is not None
        assert profile.sandbox.sidecar_publish == "127.0.0.1:8925:8125"
        stage = _stage(
            repository=repository,
            runbook_publisher=runbook_publisher,
            clone=clone,
            tmp_path=tmp_path,
            sidecar_url="http://127.0.0.1:9",  # the host one, deliberately dead
            sandbox=sidecar.entry,
            invoker=SidecarLiveGateInvoker(
                base_url=sidecar.url,
                repo=REPO_KEY,
                repo_path=clone,
                driver_argv=DRIVER,
                timeout_seconds=120,
                extra_env={"API_TEST_BASE_URL": "http://localhost:8901"},
                by_hand=True,
            ),
        )

        checked = await stage.candidate_check(
            profile,
            correlation_id="c",
            deploy_run_id="run-sbx-env-1",
            feature=FEATURE_ID,
            feat_id=FEATURE_ID,
            candidate_cwd=str(tree),
        )

        # It ran at all — this is the whole of the defect, cured.
        assert checked.outcome == "complete", checked
        assert (marker_dir / "deploy.sh.ran").is_file()
        # And it was handed none of the settings that make a sandbox.
        handed = (marker_dir / "deploy.sh.sandbox-env").read_text(encoding="utf-8")
        assert handed.strip() == "", handed

    async def test_a_refused_step_says_what_the_sidecar_said(
        self,
        repository: RunbookRepository,
        runbook_publisher: AsyncMock,
        clone: Path,
        tmp_path: Path,
        sidecar: Any,
        marker_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refusal reaches the person, not only the ledger row.

        The refusal here is the host wall: a sidecar that is not inside a
        sandbox will not run a repository's own deploy script. Whatever the
        sidecar's reason, its sentence is what the candidate check reports as
        the place it stopped — which is the words the merge report reads.
        """
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.delenv(SIDECAR_IN_SANDBOX_ENV, raising=False)
        tree = await _lay_out(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        stage = _stage(
            repository=repository,
            runbook_publisher=runbook_publisher,
            clone=clone,
            tmp_path=tmp_path,
            sidecar_url="http://127.0.0.1:9",
            sandbox=sidecar.entry,
            invoker=None,
        )

        checked = await stage.candidate_check(
            profile,
            correlation_id="c",
            deploy_run_id="run-sbx-env-2",
            feature=FEATURE_ID,
            feat_id=FEATURE_ID,
            candidate_cwd=str(tree),
        )

        assert checked.outcome == "failed"
        assert checked.failed_step == "deploy_compose"
        stopped_at = checked.detail["gate_summary"]["failed_step"]
        assert stopped_at.startswith("deploy_compose — ")
        assert "sidecar refused (HTTP 400)" in stopped_at
        assert not (marker_dir / "deploy.sh.ran").exists()


class TestTheHostWrapperIsStillServed:
    """A repository whose sandbox is made by the host wrapper, over the wire.

    The wrapper is what reads the sandbox's settings, so all eleven ride on
    the request — and the sidecar accepts the seven that only name a sandbox,
    its size, its ports and its network rules. The other four are refused on
    purpose, and its allowlist writes down why: each of them would let a
    request choose what a sandbox being created reads or mounts.
    """

    #: The four the sidecar refuses on purpose. Kept here as plain names so
    #: this file says the same thing the allowlist does.
    REFUSED_ON_PURPOSE = (
        "SANDBOX_ENV_FILE",
        "SANDBOX_FORGE_PATH",
        "SANDBOX_GUARDKIT_PATH",
        "SANDBOX_RECEIPTS_PATH",
    )

    def _client(self, sidecar: Any) -> SidecarScriptRunner:
        # A by-hand drive: it names no build, and says so.
        return SidecarScriptRunner(
            base_url=sidecar.url, repo=REPO_KEY, by_hand=True
        )

    def test_the_settings_the_wrapper_reads_are_accepted(
        self, clone: Path, sidecar: Any, marker_dir: Path
    ) -> None:
        from forge.deploy.runbook_builder import sandbox_env

        _carry_the_factory(clone)
        profile = load_deploy_profile(clone / "deploy" / "profile.yaml")
        env = sandbox_env(profile)  # the host-wrapper venue: all eleven
        assert len(env) == 11
        for name in self.REFUSED_ON_PURPOSE:
            env.pop(name)  # deliberately not allowlisted
        exit_code, output = self._client(sidecar)(
            cwd=str(clone),
            script="deploy/sandbox-deploy.sh",
            env_file=None,
            timeout=60,
            extra_env={**env, "CANDIDATE": "1"},
        )
        assert exit_code == 0, output
        assert (marker_dir / "sandbox-deploy.sh.ran").is_file()

    @pytest.mark.parametrize("key", REFUSED_ON_PURPOSE)
    def test_a_setting_that_chooses_what_a_sandbox_gets_is_refused_and_says_so(
        self, clone: Path, sidecar: Any, key: str
    ) -> None:
        """These four name a file or a folder a NEW sandbox would take.

        The env file becomes its whole environment; the forge and guardkit
        folders are mounted into it (and forge's parent decides three more);
        the receipts folder is mounted read-write. The sidecar cannot check
        values, and the sandbox's name has always been the caller's to choose,
        so allowing them would widen what one request can do. Today nothing
        sends them over the wire: a deploy inside the sandbox is sent none of
        the eleven, and the wrapper is an attended host-side command.
        """
        _carry_the_factory(clone)
        exit_code, output = self._client(sidecar)(
            cwd=str(clone),
            script="deploy/sandbox-deploy.sh",
            env_file=None,
            timeout=60,
            extra_env={key: "/run/user/1000/anything"},
        )
        assert exit_code != 0
        assert "sidecar refused (HTTP 400)" in output
        assert key in output
