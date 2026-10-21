"""Connected proof for the authority that introduces a deployment profile.

The merge caller, dispatcher, deploy stage, helper protocol, record answer,
deployment executor, and publisher checks are production code. Temporary Git
repositories provide the commits. Only process execution, event transport,
and the remote write are recording boundaries; no broker is contacted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import compose_merge_git_surface
from forge.config.models import ForgeConfig
from forge.deploy.profile_selection import selection_from_record
from forge.deploy_sidecar.deploy_executor import DeployExecutor
from forge.deploy_sidecar.service import (
    COORDINATOR_OWNER_ENV,
    SIDECAR_IN_SANDBOX_ENV,
    build_server,
    coordinator_owner_asker,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline.deployment_lock import DeploymentLockStore
from forge.pipeline.merge_executor import (
    MergeExecutorDeps,
    build_in_daemon_deploy_dispatcher,
    execute_merge_deploy,
)
from forge.pipeline.publication_activation import WhatTheMachineSays
from forge.pipeline.publication_record import PublicationRecordStore
from forge.publisher.git_work import TheProjectsCommits
from forge.publisher.service import Publisher
from forge.publisher.settings import ProjectRoute, PublisherSettings
from forge.record_answer.service import (
    ANSWER_ROUTE,
    TheCoordinatorsRecord,
    serve as serve_record_answer,
)
from tests.forge.pipeline.test_merge_executor import FEATURE_ID as FEATURE
from tests.forge.pipeline.test_merge_executor import _JoinsForReal
from tests.forge.recorded_fetch_address import allow_local_remotes, origin_of

REPO = "example/service"
BUILD = "build-first-profile"
ATTEMPT = "approved-attempt-one"
OFFER = "offer-first-profile"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _profile() -> str:
    return """\
env_id: service-live
compose:
  file: compose.yaml
  script: deploy/deploy.sh
health_checks:
  - cmd: qa/health.sh
candidate:
  env:
    CANDIDATE_PORT: "8902"
live_gate:
  driver: [python3, qa/live_gate.py]
rollback_image_ref: service:rollback
identity:
  setting: RELEASE_IDENTITY
  artifact_setting: RELEASE_ARTIFACT
  asked_with: RUNNING_RELEASE
"""


def _project(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    repo = tmp_path / "registered-clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "start without deployment")
    start = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", f"autobuild/{FEATURE}")
    (repo / "deploy").mkdir()
    (repo / "qa").mkdir()
    (repo / "deploy" / "profile.yaml").write_text(_profile(), encoding="utf-8")
    (repo / "deploy" / "deploy.sh").write_text(
        "#!/bin/sh\n"
        "if [ \"${REVERT:-}\" = 1 ]; then echo reverted; exit 0; fi\n"
        "printf 'DEPLOYED_IDENTITY=%s\\n' \"${RELEASE_IDENTITY:-none}\"\n",
        encoding="utf-8",
    )
    (repo / "deploy" / "deploy.sh").chmod(0o755)
    (repo / "qa" / "health.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "qa" / "health.sh").chmod(0o755)
    (repo / "qa" / "live_gate.py").write_text("print('gate')\n", encoding="utf-8")
    (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "introduce deployment")
    candidate = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "checkout", "-q", "main")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    return repo, bare, start, candidate, tree


def _record_offer(
    pool: SqliteLifecyclePersistence,
    *,
    build: str,
    feature: str,
    start: str,
    candidate: str,
    tree: str,
    attempt_id: str,
    offer_id: str,
) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, "
        "status, triggered_by, correlation_id, queued_at, mode, start_commit, "
        "target_branch) VALUES (?, ?, ?, ?, 'feature.yaml', 'COMPLETE', 'cli', "
        "?, ?, 'mode-a', ?, 'main')",
        (
            build,
            feature,
            REPO,
            f"autobuild/{feature}",
            f"corr-{build}",
            datetime.now(UTC).isoformat(),
            start,
        ),
    )
    now = datetime.now(UTC)
    common = {
        "build_id": build,
        "stage_label": "merge",
        "target_kind": "local_tool",
        "started_at": now,
        "completed_at": now,
        "duration_secs": 0.0,
    }
    pool.record_stage(
        StageLogEntry(
            **common,
            target_identifier="merge_deploy_offer",
            status="GATED",
            details={
                "merge_offer": {
                    "request_id": offer_id,
                    "repo": REPO,
                    "candidate_sha": candidate,
                    "candidate_tree": tree,
                }
            },
        )
    )
    pool.record_stage(
        StageLogEntry(
            **common,
            target_identifier="merge_deploy_decision",
            status="PASSED",
            details={
                "merge_decision": {
                    "request_id": offer_id,
                    "decision": "approve",
                    "execution_attempt_id": attempt_id,
                }
            },
        )
    )


class _PipelineEvents:
    async def publish_stage_complete(self, _payload: Any) -> None:
        return None


class _Bus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.messages.append((subject, payload))


class _Runtime:
    """The only external-command boundary used by the real helper."""

    def __init__(self, *, fail_live_gate: bool = False) -> None:
        self.fail_live_gate = fail_live_gate
        self.running_identity: str | None = None
        self.running_answers: list[str] = []
        self.script_calls: list[dict[str, Any]] = []
        self.gate_calls: list[dict[str, Any]] = []
        self.gate_verdicts: list[str] = []

    def script(self, **kwargs: Any) -> tuple[int, str]:
        self.script_calls.append(kwargs)
        env = dict(kwargs.get("extra_env") or {})
        if env.get("RUNNING_RELEASE") == "1":
            answer = self.running_identity or "none"
            self.running_answers.append(answer)
            return 0, f"RUNNING_IDENTITY={answer}\n"
        if env.get("CANDIDATE") == "1":
            identity = env.get("RELEASE_IDENTITY", "unknown")
            return 0, f"CHECKED_ARTIFACT=artifact-{identity}\n"
        return 0, "completed\n"

    def project(self, **kwargs: Any) -> tuple[int, str, str]:
        self.gate_calls.append(kwargs)
        candidate = bool(dict(kwargs.get("extra_env") or {}).get("CANDIDATE_PORT"))
        verdict = "fail" if self.fail_live_gate and not candidate else "pass"
        self.gate_verdicts.append(verdict)
        body = {
            "verdict": verdict,
            "run_id": f"gate-{len(self.gate_calls)}",
            "gates": [{"gate_id": "ready", "status": verdict}],
        }
        return 0, json.dumps(body), ""


class _SpawnRecorder:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> subprocess.Popen[bytes]:
        self.calls.append(dict(kwargs))
        env = dict(kwargs.get("env") or {})
        if env.get("PROMOTE") == "1":
            self.runtime.running_identity = str(env["RELEASE_IDENTITY"])
        return subprocess.Popen(**kwargs)


class _CheckedGitWithRecordedSend(TheProjectsCommits):
    """Production commit inspection with only remote I/O recorded."""

    def __init__(
        self, route: ProjectRoute, state: Path, start: str, bare_remote: Path
    ) -> None:
        super().__init__(route, state_dir=state, credential=None)
        self.remote = start
        self.bare_remote = bare_remote
        self.sends: list[str] = []

    def where_the_remotes_branch_is(self, _branch: str) -> Any:
        return SimpleNamespace(ok=True, out=self.remote, said="recorded remote")

    def send(self, commit: str, _branch: str) -> Any:
        self.sends.append(commit)
        self.remote = commit
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.where),
                "push",
                "--quiet",
                str(self.bare_remote),
                f"{commit}:refs/heads/main",
            ],
            check=True,
            capture_output=True,
        )
        return SimpleNamespace(ok=True, out=commit, said="recorded remote write")


def _config(repo: Path, db: Path, helper_url: str) -> ForgeConfig:
    coordinator_copy = repo.parent / "coordinator-copy"
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {
                "target_repo_paths": {REPO: str(coordinator_copy)},
                "fetch_addresses": {REPO: origin_of(repo)},
                "sandboxes": {
                    REPO: {
                        "name": "service-factory",
                        "sidecar_url": helper_url,
                        "runner_url": "http://127.0.0.1:1",
                    }
                },
            },
            "approval": {"expected_approver": "owner"},
            "merge_executor": {"enabled": True},
            "deploy": {"enabled": True, "run_live_gate": True},
            "publication": {
                "enabled": True,
                "publisher_url": "http://127.0.0.1:1",
                "builds_may_run_inside_the_coordinator": False,
                "publisher_credential_file": str(db.with_suffix(".credential")),
            },
        }
    )


def _services(
    *,
    repo: Path,
    db: Path,
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Any, Any, ForgeConfig, _SpawnRecorder]:
    answer, _thread = serve_record_answer(ledger=db, host="127.0.0.1", port=0)
    answer_host, answer_port = answer.server_address[:2]
    monkeypatch.setenv(
        COORDINATOR_OWNER_ENV,
        f"http://{answer_host}:{answer_port}{ANSWER_ROUTE}",
    )
    monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
    helper_config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(repo)}},
        }
    )
    spawn = _SpawnRecorder(runtime)
    executor = DeployExecutor(
        notes_root=tmp_path / "executor-notes",
        ask_the_coordinator=coordinator_owner_asker(),
        spawn=spawn,
    )
    executor.reconcile()
    helper = build_server(
        port=0,
        config_loader=lambda: helper_config,
        script_runner=runtime.script,
        project_runner=runtime.project,
        deploy_executor=executor,
    )
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    helper_host, helper_port = helper.server_address[:2]
    config = _config(repo, db, f"http://{helper_host}:{helper_port}")
    return answer, helper, config, spawn


def _publisher(
    db: Path, repo: Path, tmp_path: Path, remote: str, bare_remote: Path
) -> tuple[Publisher, _CheckedGitWithRecordedSend]:
    credential = db.with_suffix(".credential")
    credential.write_text("test-only credential\n", encoding="utf-8")
    route = ProjectRoute(
        name=REPO,
        source=str(repo),
        remote=str(tmp_path / "unused-remote"),
    )
    checked = _CheckedGitWithRecordedSend(
        route, tmp_path / f"publisher-{db.stem}", remote, bare_remote
    )
    publisher = Publisher(
        PublisherSettings(
            credential_file=str(credential),
            ledger=str(db),
            state_dir=str(tmp_path / f"publisher-{db.stem}"),
            projects={REPO: route},
            private_path_prefixes=(str(tmp_path / "unpublished"),),
        ),
        commits_for=lambda _route: checked,
    )
    return publisher, checked


def _runbook_ids(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT runbook_id FROM runbooks").fetchall()
    }


def _script_calls_at(runtime: _Runtime, candidate_cwd: str) -> list[dict[str, Any]]:
    return [
        call
        for call in runtime.script_calls
        if str(call.get("cwd") or "") == candidate_cwd
    ]


def _assert_project_legs_ran_from_selected_archive(
    *,
    runtime: _Runtime,
    spawn: _SpawnRecorder,
    candidate_cwd: str,
    checked: dict[str, Any],
    reverted: bool,
    candidate_checks: int = 1,
    candidate_cleanups: int | None = None,
) -> None:
    calls = _script_calls_at(runtime, candidate_cwd)

    candidate_deploy = [
        call
        for call in calls
        if call.get("script") == "deploy/deploy.sh"
        and (call.get("extra_env") or {}).get("CANDIDATE") == "1"
    ]
    candidate_health = [
        call
        for call in calls
        if call.get("script") == "qa/health.sh"
        and (call.get("extra_env") or {}).get("CANDIDATE_PORT") == "8902"
    ]
    running_queries = [
        call
        for call in calls
        if call.get("script") == "deploy/deploy.sh"
        and (call.get("extra_env") or {}).get("RUNNING_RELEASE") == "1"
    ]
    live_health = [
        call
        for call in calls
        if call.get("script") == "qa/health.sh"
        and "CANDIDATE_PORT" not in (call.get("extra_env") or {})
    ]
    candidate_down = [
        call
        for call in calls
        if call.get("script") == "deploy/deploy.sh"
        and (call.get("extra_env") or {}).get("CANDIDATE_DOWN") == "1"
    ]
    reverts = [
        call
        for call in calls
        if call.get("script") == "deploy/deploy.sh"
        and (call.get("extra_env") or {}).get("REVERT") == "1"
    ]
    assert len(candidate_deploy) == candidate_checks
    assert len(candidate_health) == candidate_checks
    assert len(running_queries) == 1
    assert len(live_health) == 1
    assert len(candidate_down) == (
        candidate_checks if candidate_cleanups is None else candidate_cleanups
    )
    assert len(reverts) == int(reverted)
    if reverted:
        assert reverts[0]["extra_env"]["ROLLBACK_IMAGE_REF"] == "service:rollback"

    gates = [
        call
        for call in runtime.gate_calls
        if str(call.get("cwd") or "") == candidate_cwd
    ]
    assert len(gates) == candidate_checks + 1
    for gate in gates[:candidate_checks]:
        assert "qa/live_gate.py" in gate["argv"]
        assert gate["extra_env"]["CANDIDATE_PORT"] == "8902"
    assert "qa/live_gate.py" in gates[-1]["argv"]
    assert "CANDIDATE_PORT" not in (gates[-1].get("extra_env") or {})

    promotes = [
        call
        for call in spawn.calls
        if str(call.get("cwd") or "") == candidate_cwd
        and call.get("env", {}).get("PROMOTE") == "1"
    ]
    assert len(promotes) == 1
    promote_env = promotes[0]["env"]
    assert (
        promote_env["RELEASE_IDENTITY"]
        == checked["identity"]["handed_to_the_check"]
    )
    assert promote_env["RELEASE_ARTIFACT"] == checked["artifact"]


@pytest.mark.asyncio
async def test_first_profile_survives_rejoin_and_landed_send_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bare, start, candidate, candidate_tree = _project(tmp_path)
    _git(repo, "checkout", "-q", "-b", "moved-target", start)
    (repo / "target-note.txt").write_text("landed while publishing\n", encoding="utf-8")
    _git(repo, "add", "target-note.txt")
    _git(repo, "commit", "-q", "-m", "move target without a profile")
    moved_target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    allow_local_remotes(monkeypatch, tmp_path / "fetch-copies")
    db = tmp_path / "first.db"
    connection: sqlite3.Connection = sqlite_connect.connect_writer(db)
    migrations.apply_at_boot(connection)
    pool = SqliteLifecyclePersistence(connection=connection, db_path=db)
    _record_offer(
        pool,
        build=BUILD,
        feature=FEATURE,
        start=start,
        candidate=candidate,
        tree=candidate_tree,
        attempt_id=ATTEMPT,
        offer_id=OFFER,
    )
    runtime = _Runtime()
    answer, helper, config, spawn = _services(
        repo=repo, db=db, runtime=runtime, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    bus = _Bus()
    real_publisher, checked = _publisher(db, repo, tmp_path, start, bare)
    coordinator_copy = Path(config.planning.target_repo_paths[REPO])
    assert not coordinator_copy.exists()
    publish_calls: list[dict[str, Any]] = []

    async def publish(_config: Any, request: dict[str, Any]) -> dict[str, Any]:
        publish_calls.append(dict(request))
        if len(publish_calls) == 1:
            _git(repo, "push", "-q", str(bare), f"{moved_target}:refs/heads/main")
            checked.remote = moved_target
            return {
                "published": False,
                "remote_now": moved_target,
                "contains_j": False,
                "refusal": "the target gained work; make a new joined result",
                "refusal_kind": "the-remote-moved",
            }
        landed = real_publisher.publish(request)
        assert landed.published is True, landed.refusal
        if len(publish_calls) == 2:
            raise RuntimeError("the remote write landed but its response was lost")
        return landed.to_wire()

    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_PipelineEvents(),
        guardkit_run=_JoinsForReal(feature_id=FEATURE),
        deploy_dispatcher=build_in_daemon_deploy_dispatcher(
            config=config, nats_client=bus, db_path=db
        ),
        git_surface=compose_merge_git_surface(config),
        publisher=publish,
        what_the_machine_says=WhatTheMachineSays(
            the_publisher_passed_its_self_check=True
        ),
        deployment_lock=lambda: DeploymentLockStore(connection),
    )
    try:
        interrupted = await execute_merge_deploy(
            deps=deps,
            build_id=BUILD,
            feature_id=FEATURE,
            repo=REPO,
            repo_root=coordinator_copy,
            expect_main_sha=start,
            correlation_id=f"corr-{BUILD}",
            decided_by="owner",
            expected_candidate_sha=candidate,
            expected_candidate_tree=candidate_tree,
            merge_execution_attempt_id=ATTEMPT,
        )
        assert interrupted.result == "publication-pending", interrupted.detail
        store = PublicationRecordStore(connection)
        before = store.read(BUILD)
        before_selection = selection_from_record(before)
        assert before_selection is not None and before_selection.attempt == 2
        candidate_checks = [
            line
            for line in before.lines
            if line.kind == "about to" and line.step == "candidate-check"
        ]
        attempt_runs = {
            line.attempt: str(line.detail["deploy_run_id"])
            for line in candidate_checks
        }
        assert set(attempt_runs) == {1, 2}
        assert attempt_runs[1] != attempt_runs[2]
        assert before.checked["deploy_run_id"] == attempt_runs[2]
        connection.execute(
            "UPDATE publication_records SET lease_expires_at = ? WHERE build_id = ?",
            ("2000-01-01T00:00:00+00:00", BUILD),
        )
        connection.commit()
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=BUILD,
            feature_id=FEATURE,
            repo=REPO,
            repo_root=coordinator_copy,
            expect_main_sha=start,
            correlation_id=f"corr-{BUILD}",
            decided_by="owner",
            expected_candidate_sha=candidate,
            expected_candidate_tree=candidate_tree,
            merge_execution_attempt_id=ATTEMPT,
        )
    finally:
        helper.shutdown()
        helper.server_close()
        answer.shutdown()
        answer.server_close()

    assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
    assert len(publish_calls) == 2
    assert checked.sends == [outcome.merged_sha]
    assert runtime.gate_verdicts == ["pass", "pass", "pass"]
    _assert_project_legs_ran_from_selected_archive(
        runtime=runtime,
        spawn=spawn,
        candidate_cwd=before_selection.candidate_cwd,
        checked=before.checked,
        reverted=False,
        candidate_checks=2,
        candidate_cleanups=3,
    )
    assert bus.messages
    after = PublicationRecordStore(connection).read(BUILD)
    selected = selection_from_record(after)
    assert selected is not None
    assert selected.attempt == 2
    assert selected.target_commit == moved_target
    assert after.turn == before.turn + 1
    assert after.lease_holder is None
    assert after.checked["deploy_run_id"] == attempt_runs[2]
    assert [
        line
        for line in after.lines
        if line.kind == "about to" and line.step == "candidate-check"
    ] == candidate_checks
    runbooks = _runbook_ids(connection)
    takeover_runs = {
        runbook_id.removeprefix("ask-")
        for runbook_id in runbooks
        if runbook_id.startswith("ask-")
    }
    assert len(takeover_runs) == 1
    takeover_run = takeover_runs.pop()
    assert takeover_run not in set(attempt_runs.values())
    assert {
        f"deploy-cand-{attempt_runs[1]}",
        f"live-gate-cand-{attempt_runs[1]}",
        f"teardown-cand-{attempt_runs[1]}",
        f"deploy-cand-{attempt_runs[2]}",
        f"live-gate-cand-{attempt_runs[2]}",
        f"teardown-cand-{attempt_runs[2]}",
        f"ask-{takeover_run}",
        f"deploy-{takeover_run}",
        f"teardown-cand-{takeover_run}",
        f"live-gate-{takeover_run}",
    } <= runbooks
    ending = PublicationRecordStore(connection).read_outcome(BUILD)
    assert ending is not None
    assert ending.outcome["cleanup_done"] is True
    assert ending.outcome["candidate_may_stand"] is False
    assert ending.outcome["leg_unanswered"] is False
    assert ending.finished_at is not None
    recorded = TheCoordinatorsRecord(db).what_build_starts_from(BUILD)
    assert "first_deployment_profile" not in recorded
    assert "deployment_layout" not in recorded
    assert not (repo / "deploy" / "profile.yaml").exists()


@pytest.mark.asyncio
async def test_takeover_rechecks_identity_after_promote_response_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bare, start, candidate, candidate_tree = _project(tmp_path)
    allow_local_remotes(monkeypatch, tmp_path / "fetch-copies")
    build = "build-promote-takeover"
    attempt_id = "approved-attempt-promote-takeover"
    db = tmp_path / "promote-takeover.db"
    connection: sqlite3.Connection = sqlite_connect.connect_writer(db)
    migrations.apply_at_boot(connection)
    pool = SqliteLifecyclePersistence(connection=connection, db_path=db)
    _record_offer(
        pool,
        build=build,
        feature=FEATURE,
        start=start,
        candidate=candidate,
        tree=candidate_tree,
        attempt_id=attempt_id,
        offer_id="offer-promote-takeover",
    )
    runtime = _Runtime()
    answer, helper, config, spawn = _services(
        repo=repo, db=db, runtime=runtime, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    bus = _Bus()
    real_publisher, checked = _publisher(db, repo, tmp_path, start, bare)
    coordinator_copy = Path(config.planning.target_repo_paths[REPO])
    publish_calls: list[dict[str, Any]] = []

    async def publish(_config: Any, request: dict[str, Any]) -> dict[str, Any]:
        publish_calls.append(dict(request))
        return real_publisher.publish(request).to_wire()

    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_PipelineEvents(),
        guardkit_run=_JoinsForReal(feature_id=FEATURE),
        deploy_dispatcher=build_in_daemon_deploy_dispatcher(
            config=config, nats_client=bus, db_path=db
        ),
        git_surface=compose_merge_git_surface(config),
        publisher=publish,
        what_the_machine_says=WhatTheMachineSays(
            the_publisher_passed_its_self_check=True
        ),
        deployment_lock=lambda: DeploymentLockStore(connection),
    )
    real_urlopen = urllib.request.urlopen
    lost_responses: list[dict[str, Any]] = []

    def lose_the_first_promote_response(
        request: Any, *args: Any, **kwargs: Any
    ) -> Any:
        raw = getattr(request, "data", None)
        body: dict[str, Any] = {}
        if isinstance(raw, bytes):
            try:
                decoded = json.loads(raw.decode("utf-8"))
                body = decoded if isinstance(decoded, dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
        is_promote = (
            str(getattr(request, "full_url", "")).endswith("/run")
            and isinstance(body.get("deploy"), dict)
            and dict(body.get("env") or {}).get("PROMOTE") == "1"
        )
        if is_promote and not lost_responses:
            with real_urlopen(request, *args, **kwargs) as response:
                response.read()
            lost_responses.append(body)
            raise asyncio.CancelledError(
                "the promote ran, but its HTTP response was lost with the press"
            )
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", lose_the_first_promote_response)
    try:
        with pytest.raises(asyncio.CancelledError):
            await execute_merge_deploy(
                deps=deps,
                build_id=build,
                feature_id=FEATURE,
                repo=REPO,
                repo_root=coordinator_copy,
                expect_main_sha=start,
                correlation_id=f"corr-{build}",
                decided_by="owner",
                expected_candidate_sha=candidate,
                expected_candidate_tree=candidate_tree,
                merge_execution_attempt_id=attempt_id,
            )
        store = PublicationRecordStore(connection)
        before = store.read(build)
        before_selection = selection_from_record(before)
        assert before_selection is not None
        before_checked = dict(before.checked)
        candidate_checks = [
            line
            for line in before.lines
            if line.kind == "about to" and line.step == "candidate-check"
        ]
        assert len(candidate_checks) == 1
        checked_run = str(candidate_checks[0].detail["deploy_run_id"])
        assert before_checked["deploy_run_id"] == checked_run
        assert len(lost_responses) == 1
        assert len(spawn.calls) == 1
        checked_identity = str(
            before_checked["identity"]["handed_to_the_check"]
        )
        assert runtime.running_identity == checked_identity
        assert runtime.running_answers == ["none"]
        connection.execute(
            "UPDATE publication_records SET lease_expires_at = ? WHERE build_id = ?",
            ("2000-01-01T00:00:00+00:00", build),
        )
        connection.commit()
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=build,
            feature_id=FEATURE,
            repo=REPO,
            repo_root=coordinator_copy,
            expect_main_sha=start,
            correlation_id=f"corr-{build}",
            decided_by="owner",
            expected_candidate_sha=candidate,
            expected_candidate_tree=candidate_tree,
            merge_execution_attempt_id=attempt_id,
        )
    finally:
        helper.shutdown()
        helper.server_close()
        answer.shutdown()
        answer.server_close()

    assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
    assert len(publish_calls) == 1
    assert checked.sends == [outcome.merged_sha]
    assert runtime.running_answers == ["none", checked_identity]
    assert len(spawn.calls) == 1
    after = PublicationRecordStore(connection).read(build)
    assert after.turn == before.turn + 1
    assert after.lease_holder is None
    assert selection_from_record(after) == before_selection
    assert after.checked == before_checked
    assert [
        line
        for line in after.lines
        if line.kind == "about to" and line.step == "candidate-check"
    ] == candidate_checks
    recovered_deploy = next(
        line
        for line in after.lines
        if line.kind == "done" and line.step == "deploy"
    )
    assert recovered_deploy.detail["found_by_looking"] is True
    assert recovered_deploy.detail["identity"]["identity"] == checked_identity
    runbooks = _runbook_ids(connection)
    ask_runs = {
        runbook_id.removeprefix("ask-")
        for runbook_id in runbooks
        if runbook_id.startswith("ask-")
    }
    assert checked_run in ask_runs
    assert len(ask_runs) == 2
    takeover_run = (ask_runs - {checked_run}).pop()
    assert takeover_run != checked_run
    assert f"deploy-{checked_run}" in runbooks
    assert f"deploy-{takeover_run}" not in runbooks
    assert f"ask-{takeover_run}" in runbooks
    assert f"teardown-cand-{takeover_run}" in runbooks
    first_questions = connection.execute(
        "SELECT status FROM runbook_steps WHERE runbook_id = ?",
        (f"ask-{checked_run}",),
    ).fetchall()
    interrupted_promotes = connection.execute(
        "SELECT status, result FROM runbook_steps WHERE runbook_id = ?",
        (f"deploy-{checked_run}",),
    ).fetchall()
    recovery_questions = connection.execute(
        "SELECT status FROM runbook_steps WHERE runbook_id = ?",
        (f"ask-{takeover_run}",),
    ).fetchall()
    assert [tuple(row) for row in first_questions] == [("passed",)]
    assert [tuple(row) for row in interrupted_promotes] == [("running", None)]
    assert [tuple(row) for row in recovery_questions] == [("passed",)]
    ending = PublicationRecordStore(connection).read_outcome(build)
    assert ending is not None
    assert ending.outcome["cleanup_done"] is True
    assert ending.outcome["candidate_may_stand"] is False
    assert ending.outcome["leg_unanswered"] is False
    assert ending.finished_at is not None
    recorded = TheCoordinatorsRecord(db).what_build_starts_from(build)
    assert "first_deployment_profile" not in recorded
    assert "deployment_layout" not in recorded


@pytest.mark.asyncio
async def test_second_deployment_uses_committed_profile_and_reverts_failed_live_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, bare, start, first_candidate, _first_tree = _project(tmp_path)
    first_folder = tmp_path / "first-join"
    _git(repo, "worktree", "add", "-q", "-b", "joined-first", str(first_folder), start)
    _git(first_folder, "merge", "--no-ff", "-q", "-m", "first join", first_candidate)
    first_join = _git(first_folder, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", f"{first_join}:refs/heads/main")
    feature = FEATURE
    build = "build-second-profile"
    attempt_id = "approved-attempt-two"
    _git(repo, "branch", "-D", f"autobuild/{feature}")
    _git(repo, "checkout", "-q", "-b", f"autobuild/{feature}", first_join)
    (repo / "ordinary-change.txt").write_text("second deployment\n", encoding="utf-8")
    _git(repo, "add", "ordinary-change.txt")
    _git(repo, "commit", "-q", "-m", "ordinary later change")
    candidate = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "checkout", "-q", "main")
    allow_local_remotes(monkeypatch, tmp_path / "fetch-copies")
    db = tmp_path / "second.db"
    connection = sqlite_connect.connect_writer(db)
    migrations.apply_at_boot(connection)
    pool = SqliteLifecyclePersistence(connection=connection, db_path=db)
    _record_offer(
        pool,
        build=build,
        feature=feature,
        start=first_join,
        candidate=candidate,
        tree=candidate_tree,
        attempt_id=attempt_id,
        offer_id="offer-second-profile",
    )
    runtime = _Runtime(fail_live_gate=True)
    answer, helper, config, spawn = _services(
        repo=repo, db=db, runtime=runtime, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    bus = _Bus()
    real_publisher, checked = _publisher(db, repo, tmp_path, first_join, bare)
    coordinator_copy = Path(config.planning.target_repo_paths[REPO])
    assert not coordinator_copy.exists()

    async def publish(_config: Any, request: dict[str, Any]) -> dict[str, Any]:
        return real_publisher.publish(request).to_wire()

    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=_PipelineEvents(),
        guardkit_run=_JoinsForReal(feature_id=feature),
        deploy_dispatcher=build_in_daemon_deploy_dispatcher(
            config=config, nats_client=bus, db_path=db
        ),
        git_surface=compose_merge_git_surface(config),
        publisher=publish,
        what_the_machine_says=WhatTheMachineSays(
            the_publisher_passed_its_self_check=True
        ),
        deployment_lock=lambda: DeploymentLockStore(connection),
    )
    try:
        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=build,
            feature_id=feature,
            repo=REPO,
            repo_root=coordinator_copy,
            expect_main_sha=first_join,
            correlation_id=f"corr-{build}",
            decided_by="owner",
            expected_candidate_sha=candidate,
            expected_candidate_tree=candidate_tree,
            merge_execution_attempt_id=attempt_id,
        )
    finally:
        helper.shutdown()
        helper.server_close()
        answer.shutdown()
        answer.server_close()

    assert outcome.result == "merged-deploy-failed", outcome.detail
    assert checked.sends == [outcome.merged_sha]
    assert runtime.gate_verdicts == ["pass", "fail"]
    after = PublicationRecordStore(connection).read(build)
    candidate_line = next(
        line
        for line in after.lines
        if line.kind == "about to" and line.step == "candidate-check"
    )
    assert "first_deployment_profile" not in candidate_line.detail
    deploy_run_id = str(candidate_line.detail["deploy_run_id"])
    assert after.checked["deploy_run_id"] == deploy_run_id
    candidate_cwd = str(candidate_line.detail["candidate_cwd"])
    _assert_project_legs_ran_from_selected_archive(
        runtime=runtime,
        spawn=spawn,
        candidate_cwd=candidate_cwd,
        checked=after.checked,
        reverted=True,
    )
    assert {
        f"deploy-cand-{deploy_run_id}",
        f"live-gate-cand-{deploy_run_id}",
        f"ask-{deploy_run_id}",
        f"deploy-{deploy_run_id}",
        f"teardown-cand-{deploy_run_id}",
        f"live-gate-{deploy_run_id}",
        f"revert-{deploy_run_id}",
    } <= _runbook_ids(connection)
    reverted_events = [
        json.loads(payload)
        for subject, payload in bus.messages
        if subject == f"deploy.reverted.corr-{build}"
    ]
    assert len(reverted_events) == 1
    assert reverted_events[0]["event_type"] == "deploy_reverted"
    assert reverted_events[0]["payload"]["deploy_run_id"] == deploy_run_id
    assert reverted_events[0]["payload"]["reverted_to_image_ref"] == "service:rollback"
    assert selection_from_record(after) is None
    assert bus.messages
    recorded = TheCoordinatorsRecord(db).what_build_starts_from(build)
    assert "first_deployment_profile" not in recorded
    assert "deployment_layout" not in recorded
    assert not (repo / "deploy" / "profile.yaml").exists()
