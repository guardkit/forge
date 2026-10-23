"""A finished mission feature is merged into its mission's branch, with no card.

Missions design, 17 October 2026, section 6, and its tests in section 10
("Landing"):

- no merge card for a feature built inside a mission; the offer hook hands it
  to the press's ``land_mission_feature`` instead;
- the ordinary press, onto the mission branch (``target_branch_now`` accepts
  only the recorded mission branch), with ``deploy=False``: it stops after a
  confirmed publish and records "published to the mission branch";
- the decision row says ``mission <id> (approved by <who>)``, and its attempt
  id comes from the build;
- no deploy and no repair row, after a build failure and after a refused
  candidate check;
- two features finishing at the same moment press one after the other;
- a conflict leaves ``"conflict": True`` on the join's "done" line.

The repository is real git with a bare "remote" on disk; the build system,
the deploy stage and the publisher are stand-ins at the boundaries the press
already has. Nothing contacts anything real.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nats_core.events import BuildCompletePayload, BuildFailedPayload

from forge.adapters.guardkit.models import GuardKitResult
from forge.config.models import ForgeConfig
from forge.deploy.candidate_tree import FileAtCommit, RemoteStartPoint
from forge.deploy_sidecar import service as deploy_sidecar
from forge.lifecycle.mission_builds import (
    landing_attempt_id,
    mission_join_name,
    mission_of_build,
)
from forge.lifecycle.missions import (
    MissionFeature,
    MissionStore,
    attempt_correlation_id,
)
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle_bridge.build_state_recorder import build_build_state_recorder
from forge.pipeline.fix_row_producer import SOURCE_BUILD_FAILED, maybe_mint_fix_row
from forge.pipeline.merge_executor import (
    MERGE_DECISION_DETAILS_KEY,
    MERGE_DECISION_TARGET_IDENTIFIER,
    RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH,
    MergeApprovalConsumer,
    MergeExecutorDeps,
    execute_merge_deploy,
)
from forge.pipeline.merge_join import target_branch_now
from forge.pipeline.merge_offer import MERGE_OFFER_TARGET_IDENTIFIER, MergeOfferService
from forge.pipeline.publication_activation import WhatTheMachineSays
from forge.pipeline.publication_record import (
    RESULT_PUBLICATION_PENDING,
    RESULT_PUBLISHED_TO_MISSION_BRANCH,
    STEP_JOIN,
    PublicationRecordStore,
)
from forge.record_answer.service import TheCoordinatorsRecord
from tests.forge.pipeline.test_fix_row_producer import write_pack
from tests.forge.pipeline.test_merge_executor import (  # noqa: F401 - fixtures
    REPO,
    _a_project_that_declares_its_identity,
    _ensure_build,
    _FakeDeploy,
    _FakeGuardKit,
    _FakePublisher,
    _git,
    _legs,
    _receipts_env,
    pool,
    repo_root,
)
from tests.forge.recorded_fetch_address import origin_of

MISSION_ID = "m-abc123"
MISSION_BRANCH = f"mission/{MISSION_ID}"
MISSION_CORRELATION = "corr-mission-tags"
B_BUILD = "build-FEAT-MX1-20261017090000"
C_BUILD = "build-FEAT-MX2-20261017090001"
EVERY_WALL_STANDS = WhatTheMachineSays(the_publisher_passed_its_self_check=True)


# ---------------------------------------------------------------------------
# The mission, its branch, its feature builds
# ---------------------------------------------------------------------------


def a_running_mission(
    pool: SqliteLifecyclePersistence,  # noqa: F811
    *,
    keys: tuple[str, ...] = ("B", "C"),
    base_commit: str | None = None,
) -> MissionStore:
    """A mission approved by rich and RUNNING, every attempt 1 ADMITTED."""
    store = MissionStore(pool.connection)
    store.create_mission(
        title="Tags for notes",
        target_repo=REPO,
        correlation_id=MISSION_CORRELATION,
        originating_user="rich",
        features=[MissionFeature(key=k, sentence=f"feature {k} of the mission") for k in keys],
        mission_id=MISSION_ID,
    )
    store.record_approval(MISSION_ID, approved_by="rich", plan_request_id="mission-m-abc123")
    if base_commit:
        store.set_base_commit(MISSION_ID, base_commit)
    store.set_status(MISSION_ID, "STARTING", expected=("PROPOSED",))
    store.set_status(MISSION_ID, "RUNNING", expected=("STARTING",))
    store.file_first_attempts(MISSION_ID)
    for key in keys:
        store.admit_attempt(attempt_correlation_id(MISSION_CORRELATION, key, 1))
    return store


def a_mission_feature_build(
    pool: SqliteLifecyclePersistence,  # noqa: F811
    *,
    build_id: str,
    feature_id: str,
    key: str,
) -> None:
    """A COMPLETE feature build of the mission, aimed at the mission branch."""
    _ensure_build(pool, build_id=build_id, feature_id=feature_id, target_branch=MISSION_BRANCH)
    pool.connection.execute(
        "UPDATE builds SET mission_id = ?, mission_role = 'feature', "
        "correlation_id = ? WHERE build_id = ?",
        (MISSION_ID, attempt_correlation_id(MISSION_CORRELATION, key, 1), build_id),
    )
    pool.connection.commit()


def the_mission_branch_on_the_remote(repo_root: Path) -> str:  # noqa: F811
    """Make ``mission/m-abc123`` on the bare remote at main, with no local branch."""
    _git(repo_root, "push", "-q", "origin", f"main:refs/heads/{MISSION_BRANCH}")
    return remote_tip(repo_root, MISSION_BRANCH)


def remote_tip(repo_root: Path, branch: str) -> str:  # noqa: F811
    listed = _git(repo_root, "ls-remote", "origin", f"refs/heads/{branch}")
    return listed.split()[0] if listed else ""


class APublisherThatPushes:
    """The publisher, doing what the real one does to a bare remote: a
    fast-forward push of exactly J to the requested branch, never forced."""

    def __init__(self, repo_root: Path) -> None:  # noqa: F811
        self.repo_root = repo_root
        self.asked: list[dict[str, Any]] = []

    async def __call__(self, _config: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(dict(request))
        target = request["target_branch"]
        done = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "push",
                "-q",
                "origin",
                f"{request['j_commit']}:refs/heads/{target}",
            ],
            capture_output=True,
            text=True,
        )
        now = remote_tip(self.repo_root, target)
        if done.returncode != 0:
            return {
                "published": False,
                "remote_now": now,
                "contains_j": False,
                "refusal": "the branch moved; nothing was sent",
                "refusal_kind": "the-remote-moved",
            }
        return {
            "published": True,
            "remote_now": now,
            "contains_j": True,
            "refusal": None,
            "refusal_kind": None,
        }


class JoinsTheBranch:
    """The build system's merge, for real: ``--branch`` when given, else the
    feature's own ``autobuild/<FEAT>``, merged ``--no-ff`` in the working
    folder it was given. Counts how many merges run at once."""

    def __init__(self, *, conflict_for: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.most_at_once = 0
        self.conflict_for = conflict_for

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        args = list(kwargs["args"])
        if args and args[0] == "check-join":
            return GuardKitResult(
                confirmed=True,
                status="failed",
                subcommand="autobuild",
                duration_secs=0.0,
                stdout_tail="",
                stderr="Error: No such command 'check-join'.",
                exit_code=2,
            )
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        try:
            await asyncio.sleep(0.05)
            feature = args[1]
            if feature == self.conflict_for:
                report = {
                    "outcome": "conflict",
                    "status": "conflict",
                    "conflict_files": ["README.md"],
                }
                return GuardKitResult(
                    confirmed=True,
                    status="failed",
                    subcommand="autobuild",
                    duration_secs=0.1,
                    stdout_tail=json.dumps(report),
                    stderr=None,
                    exit_code=1,
                )
            branch = (
                args[args.index("--branch") + 1]
                if "--branch" in args
                else f"autobuild/{feature}"
            )
            folder = Path(args[args.index("--in-worktree") + 1])
            _git(folder, "merge", "--no-ff", "-q", "-m", f"join {feature}", branch)
            joined = _git(folder, "rev-parse", "HEAD")
            report = {
                "outcome": "merged",
                "post_sha": joined,
                "verify_ran": True,
                "verify_ok": True,
                "verify_status": "passed",
                "verify_detail": "",
                "charged_failures": [],
                "checks_passed": 3,
                "checks_total": 3,
            }
            return GuardKitResult(
                confirmed=True,
                status="success",
                subcommand="autobuild",
                duration_secs=0.1,
                stdout_tail=json.dumps(report),
                stderr=None,
                exit_code=0,
            )
        finally:
            self.active -= 1


@pytest.fixture
def config_on(repo_root: Path) -> ForgeConfig:  # noqa: F811
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {REPO: str(repo_root)},
                "fetch_addresses": {REPO: origin_of(repo_root)},
            },
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
            "publication": {
                "enabled": True,
                "publisher_url": "http://127.0.0.1:1",
                "builds_may_run_inside_the_coordinator": False,
                "publisher_credential_file": "/etc/forge-publisher/credential",
            },
        }
    )


def press_deps(
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,  # noqa: F811
    repo_root: Path,  # noqa: F811
    *,
    joins: Any = None,
    deploy: _FakeDeploy | None = None,
    routing_gate: Any = None,
    declares_a_target: bool = True,
) -> tuple[MergeExecutorDeps, Any, _FakeDeploy, APublisherThatPushes, _FakePublisher]:
    joins = joins or JoinsTheBranch()
    deploy = deploy or _FakeDeploy()
    publisher = APublisherThatPushes(repo_root)
    bus = _FakePublisher()
    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=bus,
        guardkit_run=joins,
        deploy_dispatcher=deploy,
        publisher=publisher,
        what_the_machine_says=EVERY_WALL_STANDS,
        **(
            {
                "deployment_target": lambda repo, root: (
                    f"{repo}::live",
                    _a_project_that_declares_its_identity(),
                )
            }
            if declares_a_target
            else {}
        ),
        **(
            {"feature_routing_gate": routing_gate, "feature_routing_required": True}
            if routing_gate is not None
            else {}
        ),
    )
    return deps, joins, deploy, publisher, bus


def repair_rows(pool: SqliteLifecyclePersistence) -> list[Any]:  # noqa: F811
    return list(
        pool.connection.execute("SELECT id FROM work_queue WHERE kind = 'fix'").fetchall()
    )


def record(pool: SqliteLifecyclePersistence, build_id: str) -> Any:  # noqa: F811
    return PublicationRecordStore(pool.connection).read(build_id)


class RecordingSidecarRunner:
    def __init__(self) -> None:
        self.runner_calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[int, str]:
        self.runner_calls.append(kwargs)
        return 0, "candidate archive accepted"


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


class TestAMissionFeatureLandsOnTheMissionBranch:
    @pytest.mark.parametrize(
        "variant",
        [
            "accepted",
            "ordinary-without-offer",
            "nonfeature",
            "undeclared-attempt",
            "unapproved",
            "wrong-mission",
            "repo-mismatch",
            "target-mismatch",
            "feature-mismatch",
            "missing-decision",
            "rejected-decision",
            "wrong-execution",
            "expired-lease",
            "durable-turn-mismatch",
            "ended-outcome",
            "changed-joined",
            "changed-start",
            "changed-cwd",
            "changed-joined-tree",
            "contradictory-input",
        ],
    )
    def test_offerless_mission_archive_reaches_the_real_missing_profile_guard(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        variant: str,
    ) -> None:
        profile = (
            "env_id: generic-live\n"
            "compose:\n"
            "  file: compose.yaml\n"
            "  script: deploy/check.sh\n"
        )
        (repo_root / "deploy").mkdir()
        (repo_root / "deploy/profile.yaml").write_text(profile, encoding="utf-8")
        (repo_root / "deploy/check.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        _git(repo_root, "add", "deploy")
        _git(repo_root, "commit", "-q", "-m", "declare generic deployment")
        start = _git(repo_root, "rev-parse", "HEAD")
        _git(repo_root, "checkout", "-q", "autobuild/FEAT-MX1")
        _git(repo_root, "cherry-pick", start)
        _git(repo_root, "checkout", "-q", "main")
        _git(repo_root, "push", "-q", "origin", "main")

        a_running_mission(pool, keys=("B",), base_commit=start)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        pool.connection.execute(
            "UPDATE builds SET start_commit = ? WHERE build_id = ?", (start, B_BUILD)
        )
        pool.connection.commit()
        target = the_mission_branch_on_the_remote(repo_root)
        runner = RecordingSidecarRunner()
        deps, _joins, _deploy, _publisher, _bus = press_deps(config_on, pool, repo_root)
        attempt_id = MergeApprovalConsumer(deps)._decide_landing(
            B_BUILD,
            mission_id=MISSION_ID,
            decided_by=f"mission {MISSION_ID} (approved by rich)",
            request_id=f"merge-{B_BUILD}",
        )
        assert attempt_id == landing_attempt_id(B_BUILD)
        candidate = _git(repo_root, "rev-parse", "autobuild/FEAT-MX1")
        joined = candidate
        joined_tree = _git(repo_root, "rev-parse", f"{joined}^{{tree}}")
        archive = repo_root / ".forge-candidates" / "mission-archive"
        (archive / "deploy").mkdir(parents=True)
        (archive / "deploy/profile.yaml").write_text(profile, encoding="utf-8")
        (archive / "deploy/check.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
        now = datetime.now(UTC)
        store = PublicationRecordStore(pool.connection)
        lease = store.take_lease(
            build_id=B_BUILD,
            holder="mission-archive-test",
            now=now,
            feature_id="FEAT-MX1",
            repo=REPO,
            decided_by="mission landing",
            target_branch=MISSION_BRANCH,
            starts_a_press=True,
            attempt_id=attempt_id,
        )
        assert lease is not None
        assert store.record(
            build_id=B_BUILD,
            turn=lease.turn,
            now=now,
            g_commit=target,
            build_tip=candidate,
            attempt=1,
        )
        assert store.done(
            build_id=B_BUILD,
            turn=lease.turn,
            now=now,
            step=STEP_JOIN,
            attempt=1,
            result={"j_commit": joined, "verify_ok": True},
            j_commit=joined,
        )
        assert store.about_to(
            build_id=B_BUILD,
            turn=lease.turn,
            now=now,
            step="candidate-check",
            attempt=1,
            inputs={
                "j_commit": joined,
                "j_tree": joined_tree,
                "feature_id": "FEAT-MX1",
                "candidate_cwd": str(archive),
            },
        )
        cx = pool.connection
        if variant == "ordinary-without-offer":
            cx.execute(
                "UPDATE builds SET mission_id = NULL, mission_role = NULL "
                "WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "nonfeature":
            cx.execute(
                "UPDATE builds SET mission_role = 'mission-merge' WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "undeclared-attempt":
            cx.execute(
                "UPDATE builds SET correlation_id = 'corr-unrelated' WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "unapproved":
            cx.execute("UPDATE missions SET approved_at = NULL WHERE id = ?", (MISSION_ID,))
        elif variant == "wrong-mission":
            cx.execute(
                "UPDATE builds SET mission_id = 'm-other' WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "repo-mismatch":
            cx.execute(
                "UPDATE publication_records SET repo = 'example/other' "
                "WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "target-mismatch":
            cx.execute(
                "UPDATE publication_records SET target_branch = 'main' "
                "WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "feature-mismatch":
            cx.execute(
                "UPDATE publication_records SET feature_id = 'FEAT-OTHER' "
                "WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "missing-decision":
            cx.execute(
                "DELETE FROM stage_log WHERE build_id = ? "
                "AND target_identifier = 'merge_deploy_decision'",
                (B_BUILD,),
            )
        elif variant == "rejected-decision":
            cx.execute(
                "UPDATE stage_log SET status = 'FAILED' WHERE build_id = ? "
                "AND target_identifier = 'merge_deploy_decision'",
                (B_BUILD,),
            )
        elif variant == "wrong-execution":
            row = cx.execute(
                "SELECT details_json FROM stage_log WHERE build_id = ? "
                "AND target_identifier = 'merge_deploy_decision'",
                (B_BUILD,),
            ).fetchone()
            details = json.loads(row[0])
            details["merge_decision"]["execution_attempt_id"] = "0" * 32
            cx.execute(
                "UPDATE stage_log SET details_json = ? WHERE build_id = ? "
                "AND target_identifier = 'merge_deploy_decision'",
                (json.dumps(details), B_BUILD),
            )
        elif variant == "expired-lease":
            cx.execute(
                "UPDATE publication_records SET lease_expires_at = "
                "'2000-01-01T00:00:00+00:00' WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "durable-turn-mismatch":
            cx.execute(
                "UPDATE publication_records SET turn = turn + 1 WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "ended-outcome":
            cx.execute(
                "UPDATE publication_outcomes SET finished_at = "
                "'2026-01-01T00:00:00+00:00' WHERE build_id = ?",
                (B_BUILD,),
            )
        elif variant == "changed-joined":
            cx.execute(
                "UPDATE publication_records SET j_commit = ? WHERE build_id = ?",
                ("7" * 40, B_BUILD),
            )
        elif variant == "contradictory-input":
            row = cx.execute(
                "SELECT lines_json FROM publication_records WHERE build_id = ?",
                (B_BUILD,),
            ).fetchone()
            lines = json.loads(row[0])
            lines.append(
                {
                    **lines[-1],
                    "detail": {
                        **lines[-1]["detail"],
                        "candidate_cwd": str(
                            repo_root / ".forge-candidates" / "contradiction"
                        ),
                    },
                }
            )
            cx.execute(
                "UPDATE publication_records SET lines_json = ? WHERE build_id = ?",
                (json.dumps(lines), B_BUILD),
            )
        cx.commit()
        recorded_answers: list[dict[str, Any]] = []

        def answer(build: str) -> dict[str, Any]:
            recorded = TheCoordinatorsRecord(pool.db_path).what_build_starts_from(build)
            recorded_answers.append(recorded)
            return recorded

        monkeypatch.setattr(deploy_sidecar, "coordinator_recorded_build", lambda: answer)

        async def local_tree(_repo: Path, ref: str) -> str | None:
            if ref != f"{joined}^{{tree}}":
                return None
            return "f" * 40 if variant == "changed-joined-tree" else joined_tree

        def local_file(
            repo: Path, commit: str, name: str, **_options: Any
        ) -> FileAtCommit:
            entry = subprocess.run(
                ["git", "-C", str(repo), "ls-tree", commit, "--", name],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if not entry:
                return FileAtCommit(found=False)
            mode, kind, _object, _path = entry.split(maxsplit=3)
            if kind != "blob":
                return FileAtCommit(found=False, mode=mode)
            content = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}:{name}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            return FileAtCommit(content=content, found=True, ordinary=True, mode=mode)

        async def local_file_async(
            repo: Path, commit: str, name: str, **options: Any
        ) -> FileAtCommit:
            return local_file(repo, commit, name, **options)

        monkeypatch.setattr(deploy_sidecar, "read_file_at_commit_sync", local_file)
        monkeypatch.setattr("forge.deploy.candidate_tree.git_rev_parse", local_tree)
        monkeypatch.setattr(
            "forge.deploy.candidate_tree.read_file_at_commit", local_file_async
        )
        (repo_root / "deploy/profile.yaml").unlink()
        requested_archive = archive
        if variant == "changed-cwd":
            requested_archive = repo_root / ".forge-candidates" / "other"
            requested_archive.mkdir()
        payload_start = "e" * 40 if variant == "changed-start" else start
        status, body = deploy_sidecar.process_run_request(
            {
                "repo": REPO,
                "build": B_BUILD,
                "declared_at": payload_start,
                "cwd": str(requested_archive),
                "env": {},
                "script": "deploy/check.sh",
            },
            config=config_on,
            script_runner=runner,
        )

        assert len(recorded_answers) == 1
        assert "first_deployment_profile" not in recorded_answers[0]
        assert pool.connection.execute(
            "SELECT COUNT(*) FROM stage_log WHERE build_id = ? "
            "AND target_identifier = 'merge_deploy_offer'",
            (B_BUILD,),
        ).fetchone()[0] == 0
        if variant == "accepted":
            layout = recorded_answers[0]["deployment_layout"]
            assert layout["candidate_cwd"] == str(archive)
            assert status == 200, body
            assert len(runner.runner_calls) == 1
            assert runner.runner_calls[0]["cwd"] == str(archive)
        else:
            assert status == 400, body
            assert body["error"].endswith("nothing was started")
            assert runner.runner_calls == []
            if variant in {"changed-start", "changed-cwd", "changed-joined-tree"}:
                assert recorded_answers[0]["deployment_layout"]["candidate_cwd"] == str(
                    archive
                )
            else:
                assert "deployment_layout" not in recorded_answers[0]

    @pytest.mark.asyncio
    async def test_it_is_published_to_the_mission_branch_and_nothing_is_deployed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        tip_before = the_mission_branch_on_the_remote(repo_root)
        main_before = remote_tip(repo_root, "main")
        deps, joins, deploy, publisher, bus = press_deps(config_on, pool, repo_root)

        outcome = await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)

        assert outcome is not None
        assert outcome.result == RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH
        assert outcome.status == "PASSED"
        assert "Nothing was deployed" in outcome.detail
        landed = record(pool, B_BUILD)
        assert landed.result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert landed.target_branch == MISSION_BRANCH
        assert landed.g_commit == tip_before
        # J is on the mission branch, and main never moved.
        assert remote_tip(repo_root, MISSION_BRANCH) == landed.j_commit
        assert remote_tip(repo_root, "main") == main_before
        assert [a["target_branch"] for a in publisher.asked] == [MISSION_BRANCH]
        # NO DEPLOY: the candidate was checked and taken down, nothing promoted.
        assert "promote" not in _legs(deploy)
        assert _legs(deploy) == ["candidate_check", "candidate_down"]
        # The join happened under the feature's own integration name.
        args = joins.calls[0]["args"]
        assert args[args.index("--target") + 1] == "factory-integration/FEAT-MX1"
        assert args[args.index("--expect-main-sha") + 1] == tip_before
        assert repair_rows(pool) == []
        assert bus.reports[-1].result == RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH

    @pytest.mark.asyncio
    async def test_the_decision_row_names_the_mission_and_its_approver(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, _joins, _deploy, _publisher, bus = press_deps(config_on, pool, repo_root)

        await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)

        decisions = [
            s
            for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_DECISION_TARGET_IDENTIFIER
        ]
        assert len(decisions) == 1
        said = decisions[0].details[MERGE_DECISION_DETAILS_KEY]
        assert said["decided_by"] == f"mission {MISSION_ID} (approved by rich)"
        assert said["decision"] == "approve"
        assert said["request_id"] == f"merge-{B_BUILD}"
        assert said["execution_attempt_id"] == landing_attempt_id(B_BUILD)
        assert len(said["execution_attempt_id"]) == 32
        # The report carries the same attempt, so the router's blocker clears.
        report = bus.reports[-1].model_dump(mode="json")
        assert report[MERGE_DECISION_DETAILS_KEY]["execution_attempt_id"] == (
            landing_attempt_id(B_BUILD)
        )

    @pytest.mark.asyncio
    async def test_a_landing_already_reported_is_not_pressed_again(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)

        await consumer.land_mission_feature(B_BUILD)
        again = await consumer.land_mission_feature(B_BUILD)

        assert again is None
        assert len(publisher.asked) == 1
        assert len([c for c in joins.calls if c["args"][0] == "merge"]) == 1

    @pytest.mark.asyncio
    async def test_a_build_that_is_no_missions_is_not_landed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        _ensure_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1")
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)

        assert await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD) is None
        assert joins.calls == [] and publisher.asked == []


class TestTwoFeaturesFinishingTogether:
    @pytest.mark.asyncio
    async def test_b_and_c_press_one_after_the_other(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        a_mission_feature_build(pool, build_id=C_BUILD, feature_id="FEAT-MX2", key="C")
        start = the_mission_branch_on_the_remote(repo_root)
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)

        both = await asyncio.gather(
            consumer.land_mission_feature(B_BUILD),
            consumer.land_mission_feature(C_BUILD),
        )

        assert [o.result for o in both] == [RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH] * 2
        # ONE AT A TIME, under the repository's lock.
        assert joins.most_at_once == 1
        first, second = record(pool, B_BUILD), record(pool, C_BUILD)
        # The second joined onto the first's landing, so the tip holds both.
        assert first.g_commit == start
        assert second.g_commit == first.j_commit
        assert remote_tip(repo_root, MISSION_BRANCH) == second.j_commit
        files = _git(repo_root, "ls-tree", "--name-only", second.j_commit).split()
        assert {"FEAT-MX1.txt", "FEAT-MX2.txt"} <= set(files)
        assert len(publisher.asked) == 2


class TestAConflictSaysSo:
    @pytest.mark.asyncio
    async def test_the_join_line_carries_the_conflict_flag(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        tip = the_mission_branch_on_the_remote(repo_root)
        deps, _joins, deploy, publisher, _bus = press_deps(
            config_on, pool, repo_root, joins=JoinsTheBranch(conflict_for="FEAT-MX1")
        )

        outcome = await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)

        assert outcome is not None and outcome.result == "merge-refused"
        refused = record(pool, B_BUILD)
        join_done = [
            line for line in refused.lines if line.step == STEP_JOIN and line.kind == "done"
        ]
        assert join_done[-1].detail["joined"] is False
        assert "conflict" in join_done[-1].detail["refusal"]
        assert join_done[-1].detail["conflict"] is True
        assert refused.result == RESULT_PUBLICATION_PENDING
        assert remote_tip(repo_root, MISSION_BRANCH) == tip
        assert publisher.asked == [] and deploy.calls == []
        assert repair_rows(pool) == []

    @pytest.mark.asyncio
    async def test_an_ordinary_press_line_is_unchanged(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        _ensure_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1")
        deps, _joins, _deploy, _publisher, _bus = press_deps(
            config_on, pool, repo_root, joins=JoinsTheBranch(conflict_for="FEAT-MX1")
        )

        await execute_merge_deploy(
            deps=deps,
            build_id=B_BUILD,
            feature_id="FEAT-MX1",
            repo=REPO,
            repo_root=repo_root,
            expect_main_sha="a" * 40,
            correlation_id="corr-plain",
            decided_by="rich",
        )

        join_done = [
            line
            for line in record(pool, B_BUILD).lines
            if line.step == STEP_JOIN and line.kind == "done"
        ]
        assert set(join_done[-1].detail) == {"joined", "refusal"}


class TestNoRepairRowAndNoDeployForAMissionFeature:
    @pytest.mark.asyncio
    async def test_a_refused_candidate_check_files_no_repair_and_deploys_nothing(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        tip = the_mission_branch_on_the_remote(repo_root)
        deps, _joins, deploy, publisher, _bus = press_deps(
            config_on, pool, repo_root, deploy=_FakeDeploy(candidate_outcome="failed")
        )

        outcome = await MergeApprovalConsumer(deps).land_mission_feature(B_BUILD)

        assert outcome is not None and outcome.result == "candidate-refused"
        assert outcome.gate_before_merge is not None and outcome.gate_before_merge["ran"]
        assert repair_rows(pool) == []
        assert "promote" not in _legs(deploy)
        assert publisher.asked == []
        assert remote_tip(repo_root, MISSION_BRANCH) == tip

    def test_a_failed_mission_build_files_no_repair(
        self,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        _receipts_env: Path,  # noqa: F811
    ) -> None:
        from datetime import UTC, datetime

        from nats_core.events import BuildQueuedPayload

        a_running_mission(pool)
        now = datetime.now(UTC)
        build_id = pool.record_pending_build(
            BuildQueuedPayload(
                feature_id="FEAT-MX1",
                repo=REPO,
                feature_yaml_path=".guardkit/features/FEAT-MX1.yaml",
                triggered_by="cli",
                originating_user="rich",
                correlation_id=attempt_correlation_id(MISSION_CORRELATION, "B", 1),
                requested_at=now,
                queued_at=now,
            )
        )
        write_pack(_receipts_env, build_id)
        belongs = mission_of_build(pool, build_id)
        assert belongs is not None and belongs.is_feature

        asyncio.run(
            build_build_state_recorder(pool)(
                BuildFailedPayload(
                    feature_id="FEAT-MX1",
                    build_id=build_id,
                    failure_reason="task 004 unfinished",
                    failed_stage="autobuild",
                    recoverable=False,
                )
            )
        )

        assert pool.get_build_row(build_id).status.value == "FAILED"
        assert repair_rows(pool) == []
        assert (
            maybe_mint_fix_row(pool=pool, build_id=build_id, source=SOURCE_BUILD_FAILED)
            is None
        )


# ---------------------------------------------------------------------------
# No card: the offer hook hands a mission feature to the press
# ---------------------------------------------------------------------------


class _Lander:
    def __init__(self) -> None:
        self.landed: list[str] = []
        self.failed_tasks: list[int] = []

    async def __call__(self, build_id: str, *, tasks_failed: int = 0) -> None:
        self.landed.append(build_id)
        self.failed_tasks.append(tasks_failed)


def _offer_service(config: ForgeConfig, pool: SqliteLifecyclePersistence) -> tuple[MergeOfferService, list[Any]]:  # noqa: F811
    wire: list[Any] = []

    async def _raw(subject: str, body: bytes) -> None:
        wire.append((subject, body))

    async def _paused(payload: Any) -> None:
        wire.append(payload)

    service = MergeOfferService(
        config=config,
        pool=pool,
        pipeline_publisher=SimpleNamespace(publish_build_paused=_paused),
        raw_publish=_raw,
    )
    return service, wire


def _complete(build_id: str, feature_id: str, *, failed: int = 0) -> BuildCompletePayload:
    return BuildCompletePayload(
        feature_id=feature_id,
        build_id=build_id,
        tasks_completed=3,
        tasks_failed=failed,
        tasks_total=3 + failed,
        duration_seconds=10,
        summary="done",
    )


class TestNoCardForAMissionFeature:
    @pytest.mark.asyncio
    async def test_a_clean_mission_feature_is_landed_and_offered_no_card(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        service, wire = _offer_service(config_on, pool)
        lander = _Lander()
        service.set_mission_lander(lander)

        await service.maybe_offer(_complete(B_BUILD, "FEAT-MX1"))

        assert lander.landed == [B_BUILD] and lander.failed_tasks == [0]
        assert wire == []
        assert not any(
            s.target_identifier == MERGE_OFFER_TARGET_IDENTIFIER
            for s in pool.read_stages(B_BUILD)
        )

    @pytest.mark.asyncio
    async def test_a_mission_feature_with_failed_tasks_is_handed_over_as_failed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        service, wire = _offer_service(config_on, pool)
        lander = _Lander()
        service.set_mission_lander(lander)

        await service.maybe_offer(_complete(B_BUILD, "FEAT-MX1", failed=1))

        assert lander.landed == [B_BUILD] and lander.failed_tasks == [1]
        assert wire == []

    @pytest.mark.asyncio
    async def test_with_no_press_composed_nothing_is_offered_or_landed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        service, wire = _offer_service(config_on, pool)

        await service.maybe_offer(_complete(B_BUILD, "FEAT-MX1"))

        assert wire == []
        assert pool.read_stages(B_BUILD) == []


# ---------------------------------------------------------------------------
# target_branch_now accepts only the recorded mission branch
# ---------------------------------------------------------------------------


class _Remote:
    def __init__(self, *, has_mission_branch: bool = True) -> None:
        self.has = has_mission_branch
        self.asked: list[str | None] = []

    async def fetch_remote_start_point(self, branch: str | None = None) -> RemoteStartPoint:
        self.asked.append(branch)
        if branch is None:
            return RemoteStartPoint(branch="main", commit="1" * 40)
        if not self.has:
            return RemoteStartPoint(
                refusal=f"The project's remote has no branch called '{branch}'."
            )
        return RemoteStartPoint(branch="main", commit="1" * 40, branch_commit="2" * 40)


class TestTheTargetIsTheRecordedMissionBranch:
    @pytest.mark.asyncio
    async def test_the_recorded_mission_branch_is_accepted_at_its_tip(self) -> None:
        where = await target_branch_now(
            _Remote(), recorded_branch=MISSION_BRANCH, mission_branch=MISSION_BRANCH
        )
        assert where.ok and where.branch == MISSION_BRANCH and where.commit == "2" * 40

    @pytest.mark.asyncio
    async def test_a_mission_branch_with_no_mission_behind_it_is_refused(self) -> None:
        where = await target_branch_now(_Remote(), recorded_branch=MISSION_BRANCH)
        assert not where.ok and "default branch" in str(where.refusal)

    @pytest.mark.asyncio
    async def test_another_missions_branch_is_refused(self) -> None:
        where = await target_branch_now(
            _Remote(), recorded_branch=MISSION_BRANCH, mission_branch="mission/m-ffffff"
        )
        assert not where.ok

    @pytest.mark.asyncio
    async def test_a_mission_branch_missing_on_the_remote_is_refused(self) -> None:
        where = await target_branch_now(
            _Remote(has_mission_branch=False),
            recorded_branch=MISSION_BRANCH,
            mission_branch=MISSION_BRANCH,
        )
        assert not where.ok and "no branch called" in str(where.refusal)

    @pytest.mark.asyncio
    async def test_the_default_branch_is_read_as_before(self) -> None:
        remote = _Remote()
        where = await target_branch_now(
            remote, recorded_branch="main", mission_branch=MISSION_BRANCH
        )
        assert where.ok and where.commit == "1" * 40 and remote.asked == [None]


def test_the_mission_join_name() -> None:
    assert mission_join_name(MISSION_ID) == "mission-m-abc123"


# ---------------------------------------------------------------------------
# The build decides, not the caller
# ---------------------------------------------------------------------------


class TestAMissionFeatureIsNeverDeployedWhoeverPressesIt:
    @pytest.mark.asyncio
    async def test_a_press_asked_to_deploy_lands_it_on_the_mission_branch_only(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        main_before = remote_tip(repo_root, "main")
        deps, _joins, deploy, publisher, _bus = press_deps(config_on, pool, repo_root)

        outcome = await execute_merge_deploy(
            deps=deps,
            build_id=B_BUILD,
            feature_id="FEAT-MX1",
            repo=REPO,
            repo_root=repo_root,
            expect_main_sha="a" * 40,
            correlation_id=attempt_correlation_id(MISSION_CORRELATION, "B", 1),
            decided_by="rich",
            deploy=True,
        )

        assert outcome.result == RESULT_WORD_PUBLISHED_TO_MISSION_BRANCH
        assert record(pool, B_BUILD).result == RESULT_PUBLISHED_TO_MISSION_BRANCH
        assert "promote" not in _legs(deploy)
        assert [a["target_branch"] for a in publisher.asked] == [MISSION_BRANCH]
        assert remote_tip(repo_root, "main") == main_before


class TestALandingAndAnOrdinaryMergeShareTheLock:
    @pytest.mark.asyncio
    async def test_they_press_one_after_the_other(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from tests.forge.pipeline.test_merge_executor import _drain, _envelope, _write_offer

        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        _write_offer(pool, build_id=C_BUILD, feature_id="FEAT-MX2", correlation_id="corr-plain")
        deps, joins, _deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)

        await consumer.handle_envelope(
            _envelope(request_id=f"merge-{C_BUILD}", correlation_id="corr-plain")
        )
        await consumer.land_mission_feature(B_BUILD)
        await _drain(consumer)

        merges = [c for c in joins.calls if c["args"][0] == "merge"]
        assert len(merges) == 2
        assert joins.most_at_once == 1
        assert sorted(a["target_branch"] for a in publisher.asked) == ["main", MISSION_BRANCH]


class TestAFeatureThatFinishedWithFailedTasks:
    @pytest.mark.asyncio
    async def test_it_gets_a_failed_landing_report_and_is_never_pressed(
        self,
        config_on: ForgeConfig,
        pool: SqliteLifecyclePersistence,  # noqa: F811
        repo_root: Path,  # noqa: F811
    ) -> None:
        from forge.pipeline.merge_executor import MERGE_REPORT_TARGET_IDENTIFIER

        a_running_mission(pool)
        a_mission_feature_build(pool, build_id=B_BUILD, feature_id="FEAT-MX1", key="B")
        the_mission_branch_on_the_remote(repo_root)
        deps, joins, deploy, publisher, _bus = press_deps(config_on, pool, repo_root)
        consumer = MergeApprovalConsumer(deps)

        assert await consumer.land_mission_feature(B_BUILD, tasks_failed=2) is None
        assert await consumer.land_mission_feature(B_BUILD) is None

        reports = [
            s for s in pool.read_stages(B_BUILD)
            if s.target_identifier == MERGE_REPORT_TARGET_IDENTIFIER
        ]
        assert len(reports) == 1 and reports[0].status == "FAILED"
        assert reports[0].details["result"] == "mission-landing-failed"
        assert joins.calls == [] and publisher.asked == [] and deploy.calls == []
        assert repair_rows(pool) == []
