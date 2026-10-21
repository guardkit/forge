"""First-profile refusals through the real helper and recorded authority.

Only command execution is recorded. Commits, archives, approval/publication
writes and the coordinator's read-only SQLite answer are real and temporary.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import materialise_candidate_tree
from forge.deploy.profile_selection import FirstDeploymentProfile
from forge.deploy_sidecar import service
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence, StageLogEntry
from forge.pipeline.publication_record import (
    STEP_CANDIDATE_CHECK,
    STEP_JOIN,
    PublicationRecordStore,
)
from forge.record_answer.service import TheCoordinatorsRecord

REPO = "example/service"
BUILD = "build-first-profile"
SCRIPT = "deploy/deploy.sh"
DRIVER = ["python3", "qa/gate.py"]
PROFILE = """\
env_id: service-live
compose:
  file: compose.yaml
  script: deploy/deploy.sh
live_gate:
  driver: [python3, qa/gate.py]
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-c", "user.email=tests@example.invalid",
            "-c", "user.name=tests", "-c", "commit.gpgsign=false", *args,
        ],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class _SelectedCase:
    def __init__(self, repo: Path, record: SqliteLifecyclePersistence) -> None:
        self.repo = repo
        self.record = record
        self.start = _git(repo, "rev-parse", "main")
        self.candidate = _git(repo, "rev-parse", "approved")
        self.joined = _git(repo, "rev-parse", "joined")
        self.tree = _git(repo, "rev-parse", "joined^{tree}")
        self.archive = asyncio.run(materialise_candidate_tree(repo, "selected", self.joined))
        self.config = ForgeConfig.model_validate({
            "permissions": {"filesystem": {"allowlist": [str(repo.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(repo)}},
        })
        self.script_calls: list[dict[str, Any]] = []
        self.driver_calls: list[dict[str, Any]] = []
        self.answer_calls: list[str] = []

    def answer(self, build: str) -> dict[str, Any]:
        self.answer_calls.append(build)
        return TheCoordinatorsRecord(self.record.db_path).what_build_starts_from(build)

    def payload(self, route: str = "script", **overrides: Any) -> dict[str, Any]:
        """Record coherent real authority, including deliberately bad Git claims."""
        selected = FirstDeploymentProfile(
            **{
                "version": 1, "repo": REPO, "build": BUILD,
                "offer_id": "offer-first-profile", "decision_id": "approved-first-profile",
                "candidate_commit": self.candidate, "candidate_tree": self.tree,
                "start_commit": self.start, "target_commit": self.start,
                "joined_commit": self.joined, "joined_tree": self.tree,
                "profile_sha256": hashlib.sha256(PROFILE.encode()).hexdigest(),
                "attempt": 1, "candidate_cwd": str(self.archive), **overrides,
            }
        )
        now = datetime.now(UTC)
        self.record.connection.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, "
            "status, triggered_by, correlation_id, queued_at, mode, start_commit, "
            "target_branch) VALUES (?, 'first-profile-feature', ?, 'approved', "
            "'feature.yaml', 'COMPLETE', 'cli', 'corr-first-profile', ?, 'mode-a', ?, 'main')",
            (BUILD, REPO, now.isoformat(), self.start),
        )
        common = dict(
            build_id=BUILD, stage_label="merge", target_kind="local_tool",
            started_at=now, completed_at=now, duration_secs=0.0,
        )
        self.record.record_stage(StageLogEntry(
            **common, target_identifier="merge_deploy_offer", status="GATED",
            details={"merge_offer": {
                "request_id": selected.offer_id, "repo": REPO,
                "candidate_sha": selected.candidate_commit,
                "candidate_tree": selected.candidate_tree,
            }},
        ))
        self.record.record_stage(StageLogEntry(
            **common, target_identifier="merge_deploy_decision", status="PASSED",
            details={"merge_decision": {
                "request_id": selected.offer_id, "decision": "approve",
                "execution_attempt_id": selected.decision_id,
            }},
        ))
        store = PublicationRecordStore(self.record.connection)
        lease = store.take_lease(
            build_id=BUILD, holder="helper-test", now=now,
            feature_id="first-profile-feature", repo=REPO, decided_by="owner",
            target_branch="main", starts_a_press=True, attempt_id=selected.decision_id,
        )
        assert lease is not None
        assert store.record(
            build_id=BUILD, turn=lease.turn, now=now, attempt=1,
            g_commit=selected.target_commit, build_tip=selected.candidate_commit,
        )
        assert store.done(
            build_id=BUILD, turn=lease.turn, now=now, attempt=1,
            step=STEP_JOIN, j_commit=selected.joined_commit,
            result={"j_commit": selected.joined_commit, "verify_ok": True},
        )
        assert store.about_to(
            build_id=BUILD, turn=lease.turn, now=now, attempt=1,
            step=STEP_CANDIDATE_CHECK, inputs={
                "j_commit": selected.joined_commit, "j_tree": selected.joined_tree,
                "candidate_cwd": str(self.archive),
                "first_deployment_profile": selected.to_wire(),
            },
        )
        reference = {"selection": selected.to_wire(), "turn": lease.turn}
        # The production reader must actually confirm every canonical test selection.
        assert self.answer(BUILD)["first_deployment_profile"] == reference
        self.answer_calls.clear()
        return {
            "repo": REPO, "build": BUILD, "declared_at": self.start,
            "cwd": str(self.archive), "env": {},
            "helper_instance": service.HELPER_INSTANCE,
            "first_deployment_profile": reference,
            **({"script": SCRIPT} if route == "script" else {"driver": list(DRIVER)}),
        }

    def run(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        def script(**kwargs: Any) -> tuple[int, str]:
            self.script_calls.append(kwargs)
            return 0, "recorded script"

        def driver(**kwargs: Any) -> tuple[int, str, str]:
            self.driver_calls.append(kwargs)
            return 0, "recorded driver", ""

        return service.process_run_request(
            payload, config=self.config, inside_sandbox=True,
            script_runner=script, command_runner=driver,
        )

    def assert_refused(self, payload: dict[str, Any], reason: str) -> None:
        status, body = self.run(payload)
        assert status == 400, body
        assert reason in body["error"], body
        assert body["execution_started"] is False
        assert body["helper_instance"] == service.HELPER_INSTANCE
        assert body["requested_first_deployment_profile"] == payload["first_deployment_profile"]
        assert "first_deployment_profile" not in body
        assert self.script_calls == self.driver_calls == []


@pytest.fixture
def selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_SelectedCase]:
    repo = tmp_path / "registered"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("start without deployment\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "start")
    _git(repo, "checkout", "-q", "-b", "approved")
    (repo / "deploy").mkdir()
    (repo / "qa").mkdir()
    (repo / ".guardkit").mkdir()
    (repo / "deploy/profile.yaml").write_text(PROFILE, encoding="utf-8")
    (repo / SCRIPT).write_text("#!/bin/sh\necho selected\n", encoding="utf-8")
    (repo / "qa/gate.py").write_text("print('selected')\n", encoding="utf-8")
    (repo / ".guardkit/config.yaml").write_text(
        "launch:\n  settings: [CANDIDATE_LAUNCH]\n", encoding="utf-8",
    )
    _git(repo, "add", "deploy", "qa", ".guardkit")
    _git(repo, "commit", "-q", "-m", "introduce deployment")
    _git(repo, "checkout", "-q", "-b", "joined")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "joined candidate")
    _git(repo, "checkout", "-q", "main")
    db = tmp_path / "record.db"
    connection = sqlite_connect.connect_writer(db)
    connection.row_factory = sqlite3.Row
    migrations.apply_at_boot(connection)
    try:
        case = _SelectedCase(repo, SqliteLifecyclePersistence(connection=connection, db_path=db))
        monkeypatch.setattr(service, "coordinator_recorded_build", lambda: case.answer)
        yield case
    finally:
        connection.close()


@pytest.mark.parametrize("route", ["script", "driver"])
def test_recorded_selection_runs_only_the_approved_archive(selected: _SelectedCase, route: str) -> None:
    payload = selected.payload(route)
    status, body = selected.run(payload)
    assert status == 200, body
    assert body["first_deployment_profile"] == payload["first_deployment_profile"]
    assert body["helper_instance"] == service.HELPER_INSTANCE
    assert body["exit_code"] == 0
    assert body["cwd"] == str(selected.archive)
    assert selected.answer_calls == [BUILD]
    calls = selected.script_calls if route == "script" else selected.driver_calls
    assert len(calls) == 1
    assert calls[0]["cwd"] == str(selected.archive)
    assert calls[0]["repository"] == str(selected.repo)
    if route == "script":
        assert calls[0]["script"] == SCRIPT
        assert selected.driver_calls == []
    else:
        assert calls[0]["argv"] == DRIVER
        assert selected.script_calls == []
    assert not (selected.repo / "deploy/profile.yaml").exists()
    assert not (selected.archive / ".git").exists()


@pytest.mark.parametrize("route,change,reason", [
    ("script", "profile", "archive profile differs"),
    ("driver", "profile", "archive profile differs"),
    ("script", "source", "executable source 'deploy/deploy.sh' differs"),
    ("driver", "source", "executable source 'qa/gate.py' differs"),
    ("script", "archive", "not the selected candidate archive"),
    ("driver", "archive", "not the selected candidate archive"),
])
def test_selected_archive_substitution_starts_nothing(
    selected: _SelectedCase, route: str, change: str, reason: str,
) -> None:
    payload = selected.payload(route)
    if change == "archive":
        other = asyncio.run(materialise_candidate_tree(selected.repo, "other", selected.joined))
        payload["cwd"] = str(other)
    else:
        source = "deploy/profile.yaml" if change == "profile" else SCRIPT if route == "script" else "qa/gate.py"
        (selected.archive / source).write_text("substituted\n", encoding="utf-8")
    selected.assert_refused(payload, reason)


@pytest.mark.parametrize("change,reason", [
    ("candidate", "selected commit and tree do not match"),
    ("candidate-tree", "selected commit and tree do not match"),
    ("joined-tree", "selected commit and tree do not match"),
    ("digest", "differs from the approved candidate or digest"),
    ("target-profile", "not provably absent at build start and target"),
    ("candidate-profile", "differs from the approved candidate or digest"),
])
def test_confirmed_selection_still_requires_exact_git_identity_and_true_absence(
    selected: _SelectedCase, change: str, reason: str,
) -> None:
    start_tree = _git(selected.repo, "rev-parse", "main^{tree}")
    overrides = {
        "candidate": {"candidate_commit": selected.start},
        "candidate-tree": {"candidate_tree": start_tree},
        "joined-tree": {"joined_tree": start_tree},
        "digest": {"profile_sha256": "0" * 64},
        "target-profile": {"target_commit": selected.candidate},
    }
    if change == "candidate-profile":
        _git(selected.repo, "checkout", "-q", "approved")
        (selected.repo / "deploy/profile.yaml").write_text(
            PROFILE.replace("service-live", "different-live"), encoding="utf-8",
        )
        _git(selected.repo, "add", "deploy/profile.yaml")
        _git(selected.repo, "commit", "-q", "-m", "changed candidate profile")
        overrides[change] = {
            "candidate_commit": _git(selected.repo, "rev-parse", "HEAD"),
            "candidate_tree": _git(selected.repo, "rev-parse", "HEAD^{tree}"),
        }
        _git(selected.repo, "checkout", "-q", "main")
    payload = selected.payload(**overrides[change])
    selected.assert_refused(payload, reason)
    assert selected.answer_calls == [BUILD]


@pytest.mark.parametrize("change,reason", [
    ("null-reference", "reference is malformed"),
    ("missing-digest", "reference is malformed"),
    ("short-candidate", "needs a full candidate_commit"),
    ("short-joined-tree", "needs a full joined_tree"),
    ("changed-candidate", "did not confirm this exact deployment profile"),
])
def test_malformed_or_altered_reference_cannot_run(selected: _SelectedCase, change: str, reason: str) -> None:
    payload = selected.payload()
    wire = payload["first_deployment_profile"]["selection"]
    if change == "null-reference":
        payload["first_deployment_profile"] = None
    elif change == "missing-digest":
        del wire["profile_sha256"]
    elif change == "short-candidate":
        wire["candidate_commit"] = selected.candidate[:12]
    elif change == "short-joined-tree":
        wire["joined_tree"] = selected.tree[:12]
    else:
        wire["candidate_commit"] = selected.start
    selected.assert_refused(payload, reason)


@pytest.mark.parametrize("route", ["script", "driver"])
@pytest.mark.parametrize("misuse,reason", [
    ("declared-test", "only for factory deployment"),
    ("by-hand", "only for factory deployment"),
    ("launch-name", "does not declare"),
])
def test_selected_profile_never_authorizes_other_work_or_candidate_launch_names(
    selected: _SelectedCase, route: str, misuse: str, reason: str,
) -> None:
    payload = selected.payload(route)
    if misuse == "declared-test":
        payload["declared_test"] = ["python3", "qa/tests.py"]
    elif misuse == "by-hand":
        payload["by_hand"] = True
    else:
        payload["launch_settings"] = ["CANDIDATE_LAUNCH"]
    selected.assert_refused(payload, reason)


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_present_clone_profile_failure_never_uses_missing_profile_fallback(
    selected: _SelectedCase, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    payload = selected.payload()
    payload.pop("first_deployment_profile")
    profile = selected.repo / "deploy/profile.yaml"
    profile.parent.mkdir(exist_ok=True)
    profile.write_text("[]\n" if failure == "malformed" else PROFILE)
    if failure == "unreadable":
        read_text = Path.read_text

        def denied(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == profile:
                raise PermissionError("profile cannot be read")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.setattr(
        service, "coordinator_recorded_build",
        lambda: pytest.fail("a present unusable profile entered the absence fallback"),
    )
    status, body = selected.run(payload)
    assert status == 400, body
    assert selected.script_calls == selected.driver_calls == []


@pytest.mark.parametrize("start_kind", [
    "ordinary", "changed_candidate", "malformed", "nonordinary", "missing_commit", "refused_read",
])
def test_stale_clone_uses_only_the_ordinary_profile_at_the_recorded_start(
    selected: _SelectedCase, monkeypatch: pytest.MonkeyPatch, start_kind: str,
) -> None:
    from forge.deploy.candidate_tree import FileAtCommit

    recorded_start = selected.candidate
    if start_kind == "changed_candidate":
        _git(selected.repo, "checkout", "-q", "approved")
        (selected.repo / "deploy/profile.yaml").write_text(
            PROFILE.replace("script: deploy/deploy.sh", "script: deploy/other.sh"),
        )
        _git(selected.repo, "add", "deploy/profile.yaml")
        _git(selected.repo, "commit", "-q", "-m", "candidate changes deployment profile")
        selected.candidate = selected.joined = _git(selected.repo, "rev-parse", "HEAD")
        selected.tree = _git(selected.repo, "rev-parse", "HEAD^{tree}")
        _git(selected.repo, "checkout", "-q", "main")
        selected.archive = asyncio.run(materialise_candidate_tree(selected.repo, "selected", selected.joined))
    elif start_kind in ("malformed", "nonordinary"):
        _git(selected.repo, "checkout", "-q", "-b", "unusable-start")
        (selected.repo / "deploy").mkdir()
        profile = selected.repo / "deploy/profile.yaml"
        if start_kind == "malformed":
            profile.write_text("[]\n")
        else:
            profile.symlink_to("missing-profile")
        _git(selected.repo, "add", "deploy/profile.yaml")
        _git(selected.repo, "commit", "-q", "-m", "unusable start profile")
        recorded_start = _git(selected.repo, "rev-parse", "HEAD")
        _git(selected.repo, "checkout", "-q", "main")
    elif start_kind == "missing_commit":
        recorded_start = "0" * 40
    payload = selected.payload()
    payload.pop("first_deployment_profile")
    payload["declared_at"] = recorded_start
    selected.record.connection.execute(
        "UPDATE builds SET start_commit = ? WHERE build_id = ?", (recorded_start, BUILD),
    )
    selected.record.connection.commit()
    if start_kind == "refused_read":
        read_file = service.read_file_at_commit_sync

        def refused(repo: Path, commit: str, name: str, **options: Any) -> FileAtCommit:
            if commit == recorded_start and name == "deploy/profile.yaml":
                return FileAtCommit(refusal="committed profile read unavailable")
            return read_file(repo, commit, name, **options)

        monkeypatch.setattr(service, "read_file_at_commit_sync", refused)
    status, body = selected.run(payload)
    assert selected.answer_calls == [BUILD]
    if start_kind == "ordinary":
        assert status == 200, body
        assert len(selected.script_calls) == 1
    else:
        assert status == 400, body
        assert selected.script_calls == selected.driver_calls == []
