"""The merge word, end to end, for a repository whose factory lives in its
sandbox (sandbox first, 2026-09-07, rules 62, 85 and 89).

Rich's ruling of 2026-09-07 23:31Z, on L3b's third coach: there is no
merge-by-hand shape. The merge word is one of his three touches and must work
for a repository that has a sandbox, so the press's own git moves in there
beside the merge command, the deploy leg and the live gate that L3b already
routed.

This drives the whole press against a REAL deploy sidecar on a real ephemeral
loopback port, standing in for the one inside the sandbox, with a REAL git
repository standing in for the factory's clone. The path forge-prod is given
for that repository is a path that holds no repository at all — which is the
truth for a sandboxed repository once its bind mount is dropped (rule 63) —
so a press that reached for git on this side could not have found the branch,
and a recording seam over every way this process can start git proves that it
never tried.

Nothing live is touched: no ``sbx``, no docker, no systemctl, no service of
the estate. The deploy stage and the merge command are the fakes, at the
boundaries the executor already has.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import compose_merge_git_surface
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_executor import MergeExecutorDeps, execute_merge_deploy

REPO = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
FEATURE_ID = "FEAT-L3E"
BUILD_ID = "build-FEAT-L3E-20260908"
CORRELATION = "corr-l3e"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo(root: Path) -> Path:
    """A repository, with a bare "remote" for the merge word to join onto.

    The remote is a bare repository beside it: real git, nobody's account, and
    nothing here contacts anything.
    """
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / "feature.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", "feature.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    bare = root.parent / f"{root.name}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    return root.resolve()


# ---------------------------------------------------------------------------
# The recording seam: every way this process can start git, watched
# ---------------------------------------------------------------------------


class _GitCalls:
    """Records the working directory of every git command this process starts.

    The sidecar under test runs in this same process, so "in the container" and
    "in the sandbox" cannot be told apart by the calling process — but they CAN
    be told apart by the directory the command is run in. Every call whose
    working directory is the path forge-prod was given is a call the press made
    on this side, and there must be none of those.

    A ``git -C <path>`` names its directory in the command rather than as the
    working directory, so that path is recorded too (3 October 2026): the
    press's read of ``deploy/profile.yaml`` went to the path on this side that
    way, and this recorder, watching the working directory only, missed it.
    """

    def __init__(self) -> None:
        self.cwds: list[str] = []

    def record(self, cwd: Any, argv: Any = None) -> None:
        self.cwds.append(str(cwd))
        words = [str(word) for word in argv] if isinstance(argv, (list, tuple)) else []
        for at, word in enumerate(words[:-1]):
            if word == "-C":
                self.cwds.append(words[at + 1])

    def any_in(self, where: Path) -> list[str]:
        root = str(where)
        return [cwd for cwd in self.cwds if cwd == root or cwd.startswith(root + "/")]


class _RecordingSubprocess:
    """``subprocess``, with ``run`` and ``Popen`` recorded and forwarded."""

    def __init__(self, calls: _GitCalls) -> None:
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.record(kwargs.get("cwd"), args[0] if args else kwargs.get("args"))
        return subprocess.run(*args, **kwargs)

    def Popen(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 — the stdlib name
        self._calls.record(kwargs.get("cwd"), args[0] if args else kwargs.get("args"))
        return subprocess.Popen(*args, **kwargs)


class _RecordingAsyncio:
    """``asyncio``, with ``create_subprocess_exec`` recorded and forwarded."""

    def __init__(self, calls: _GitCalls) -> None:
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def create_subprocess_exec(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.record(kwargs.get("cwd"), args)
        return asyncio.create_subprocess_exec(*args, **kwargs)


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> _GitCalls:
    from forge.deploy import candidate_tree
    from forge.pipeline import merge_offer

    calls = _GitCalls()
    monkeypatch.setattr(candidate_tree, "subprocess", _RecordingSubprocess(calls))
    monkeypatch.setattr(candidate_tree, "asyncio", _RecordingAsyncio(calls))
    monkeypatch.setattr(merge_offer, "asyncio", _RecordingAsyncio(calls))
    return calls


# ---------------------------------------------------------------------------
# The estate: a clone in the sandbox, a real sidecar, and nothing on this side
# ---------------------------------------------------------------------------


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """The factory's own clone of the repository, inside its sandbox."""
    return _make_repo(tmp_path / "sandbox" / "api_test")


@pytest.fixture
def plain_checkout(tmp_path: Path) -> Path:
    """A repository with no sandbox, whose press must be exactly as before."""
    return _make_repo(tmp_path / "host" / "plain")


@pytest.fixture
def on_this_side(tmp_path: Path) -> Path:
    """What forge-prod has for a sandboxed repository: no checkout at all."""
    return tmp_path / "not-mounted" / "api_test"


@pytest.fixture
def sidecar(clone: Path, plain_checkout: Path, monkeypatch: pytest.MonkeyPatch):
    """The real deploy sidecar, standing where the sandbox's own stands."""
    from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

    monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    holder["config"] = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": {"target_repo_paths": {REPO: str(clone)}},
        }
    )
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def config(sidecar: str, on_this_side: Path, plain_checkout: Path) -> ForgeConfig:
    """forge-prod's own settings: the sandboxed repository names its sandbox."""
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {
                "target_repo_paths": {
                    REPO: str(on_this_side),
                    REPO_WITHOUT: str(plain_checkout),
                },
                "sandboxes": {
                    REPO: {
                        "name": "api-test-factory",
                        "sidecar_url": sidecar,
                        "runner_url": "http://127.0.0.1:8924",
                    }
                },
            },
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture(autouse=True)
def _receipts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


def _build_row(
    pool: SqliteLifecyclePersistence, repo: str = REPO, start_commit: str = "0" * 40
) -> None:
    pool.connection.execute(
        "INSERT OR IGNORE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode, start_commit, target_branch) VALUES (?, ?, ?, ?, 'f.yaml', "
        "'COMPLETE', 'cli', ?, '2026-09-08T00:00:00Z', 'mode-a', ?, 'main')",
        (
            BUILD_ID,
            FEATURE_ID,
            repo,
            f"autobuild/{FEATURE_ID}",
            CORRELATION,
            start_commit,
        ),
    )
    pool.connection.commit()


class _FakePublisher:
    def __init__(self) -> None:
        self.reports: list[Any] = []

    async def publish_stage_complete(self, payload: Any) -> None:
        self.reports.append(payload)


class _FakeDeploy:
    """The deploy stage's legs, recording what tree they were pointed at.

    The candidate leg is where the live gate runs (L3b routed both into the
    sandbox), so what it is handed as its working directory is what the gate
    would be driven in.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.seen: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        leg = kwargs.get("leg", "deploy")
        if leg == "candidate_check":
            cwd = kwargs.get("candidate_cwd")
            where = Path(cwd) if cwd else None
            self.seen = {
                "cwd": cwd,
                "the branch's file is in it": bool(
                    where and (where / "feature.txt").is_file()
                ),
            }
            return SimpleNamespace(
                outcome="complete",
                verdict="pass",
                failed_step=None,
                events=("DeployQueued",),
                detail={
                    "gate_summary": {
                        "verdict": "pass",
                        "checks_total": 8,
                        "checks_passed": 8,
                        "failed_checks": [],
                    },
                    "candidate": "standing",
                },
            )
        return SimpleNamespace(
            outcome="complete",
            verdict="pass",
            failed_step=None,
            events=("DeployComplete",),
            detail={"candidate": "torn-down"},
        )

    def legs(self) -> list[str]:
        return [call.get("leg", "deploy") for call in self.calls]


class _FakeMergeCommand:
    """``guardkit autobuild merge``, which L3b already runs in the sandbox."""

    def __init__(self, merged_sha: str) -> None:
        self.merged_sha = merged_sha
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        return GuardKitResult(
            status="success",
            subcommand=kwargs.get("subcommand", "autobuild"),
            duration_secs=0.1,
            stdout_tail=json.dumps(
                {"status": "merged", "merged_sha": self.merged_sha}
            ),
            exit_code=0,
        )


async def _press(
    *,
    config: ForgeConfig,
    pool: SqliteLifecyclePersistence,
    repo: str,
    repo_root: Path,
    merge: _FakeMergeCommand,
    deploy: Any,
    expect_main_sha: str,
    publisher: _FakePublisher | None = None,
    start_commit: str = "0" * 40,
) -> Any:
    _build_row(pool, repo, start_commit)
    deps = MergeExecutorDeps(
        config=config,
        pool=pool,
        pipeline_publisher=publisher or _FakePublisher(),
        guardkit_run=merge,
        deploy_dispatcher=deploy,
        git_surface=compose_merge_git_surface(config),
    )
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=repo,
        repo_root=repo_root,
        expect_main_sha=expect_main_sha,
        correlation_id=CORRELATION,
        decided_by="rich",
    )


class TestTheWholePressRunsWhereTheRepositoryLives:
    @pytest.mark.asyncio
    async def test_candidate_merge_and_promote_all_happen_in_the_sandbox(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        deploy = _FakeDeploy()

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=deploy,
            expect_main_sha=_git(clone, "rev-parse", "main"),
        )

        assert outcome.result == "publication-pending", outcome.detail
        # The join first, then the check on the joined result; nothing is
        # published and nothing is deployed, so the candidate comes down.
        # A CLEANUP THAT CANNOT NAME THE CANDIDATE DOES NOT RUN (26 September
        # 2026). This stand-in project declares no identity, so nothing was
        # handed to the check and nothing recorded says which candidate this
        # build stood up — the teardown is not dispatched, the candidate is
        # left standing, and the report says so in plain words.
        assert deploy.legs() == ["candidate_check"]
        # The candidate was laid out in the clone, and that is the tree the
        # deploy leg and the live gate were pointed at.
        assert deploy.seen["cwd"] == str(clone / ".forge-candidates" / FEATURE_ID)
        assert deploy.seen["the branch's file is in it"] is True
        # The tree that landed is the tree that was checked (rule 37).
        gate = outcome.gate_before_merge
        assert gate["candidate_sha"] == tip
        assert gate["candidate_tree"] == _git(clone, "rev-parse", f"{tip}^{{tree}}")
        assert gate["trees_match"] is True
        # And the tree is gone when the run ends.
        assert not (clone / ".forge-candidates" / FEATURE_ID).exists()

    @pytest.mark.asyncio
    async def test_not_one_git_command_ran_on_this_side_for_that_repository(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        """Rich's rule, proved: nothing the factory runs on a repository runs
        on the host."""
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=_FakeDeploy(),
            expect_main_sha=_git(clone, "rev-parse", "main"),
        )

        assert outcome.result == "publication-pending", outcome.detail
        assert git_calls.any_in(on_this_side) == []
        # Every git command this press caused ran in the clone.
        assert git_calls.any_in(clone), "the press asked git nothing at all"

    @pytest.mark.asyncio
    async def test_a_main_that_moved_during_the_build_is_refused_before_the_merge(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        """A remote that moved is joined onto, in the clone, not refused."""
        # Somebody else landed work on main after this branch was cut, in the
        # clone — which is the only copy that knows.
        (clone / "someone-else.txt").write_text("moved\n", encoding="utf-8")
        _git(clone, "add", "someone-else.txt")
        _git(clone, "commit", "-q", "-m", "somebody else landed work")
        moved_main = _git(clone, "rev-parse", "main")
        merge = _FakeMergeCommand(_git(clone, "rev-parse", f"autobuild/{FEATURE_ID}"))
        deploy = _FakeDeploy()

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=merge,
            deploy=deploy,
            # The pin the card carries is a commit the branch does not have.
            expect_main_sha=moved_main,
        )

        # A remote that moved during the build is no longer a refusal: the
        # work is JOINED onto where the remote is now, and the joined result
        # is what gets checked. What the card pinned no longer decides it.
        assert outcome.result == "publication-pending", outcome.detail
        assert merge.calls, "the join must be made onto the commit the remote has"
        # A CLEANUP THAT CANNOT NAME THE CANDIDATE DOES NOT RUN (26 September
        # 2026). This stand-in project declares no identity, so nothing was
        # handed to the check and nothing recorded says which candidate this
        # build stood up — the teardown is not dispatched, the candidate is
        # left standing, and the report says so in plain words.
        assert deploy.legs() == ["candidate_check"]
        assert git_calls.any_in(on_this_side) == []

    @pytest.mark.asyncio
    async def test_a_repository_without_a_sandbox_is_pressed_here_as_before(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        plain_checkout: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        """The same settings, the other repository: this side, exactly as before."""
        tip = _git(plain_checkout, "rev-parse", f"autobuild/{FEATURE_ID}")
        deploy = _FakeDeploy()

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO_WITHOUT,
            repo_root=plain_checkout,
            merge=_FakeMergeCommand(tip),
            deploy=deploy,
            expect_main_sha=_git(plain_checkout, "rev-parse", "main"),
        )

        assert outcome.result == "publication-pending", outcome.detail
        assert deploy.seen["cwd"] == str(
            plain_checkout / ".forge-candidates" / FEATURE_ID
        )
        # Its git ran here, in its own checkout, which is where it lives.
        assert git_calls.any_in(plain_checkout)


class TestTheProjectsDeployProfileIsReadWhereTheRepositoryLives:
    """FEAT-E592, 3 October 2026: the merge word stopped at the candidate check
    with "deploy profile not found: <the path this side was given>", because
    the press and the deploy legs read ``deploy/profile.yaml`` out of a copy
    this side does not have. Both now read the committed file in the sandbox.

    The press here is given the REAL in-daemon deploy dispatcher, so the
    candidate leg reads the profile exactly as production does; only the deploy
    stage beneath it is a recorder, so no script and no driver is run.
    """

    _PROFILE = {
        "env_id": "apitest",
        "compose": {"file": "docker-compose.yml", "script": "deploy/sandbox-deploy.sh"},
        "identity": {"setting": "DEPLOY_IDENTITY", "reported_as": "DEPLOYED_IDENTITY"},
    }

    @staticmethod
    def _commit_the_profile(clone: Path, profile: dict[str, Any], message: str) -> str:
        """Commit ``profile`` as the project's deploy profile on the clone's main."""
        import yaml

        (clone / "deploy").mkdir(exist_ok=True)
        (clone / "deploy" / "profile.yaml").write_text(
            yaml.safe_dump(profile), encoding="utf-8"
        )
        _git(clone, "add", "deploy/profile.yaml")
        _git(clone, "commit", "-q", "-m", message)
        return _git(clone, "rev-parse", "HEAD")

    @staticmethod
    def _the_real_dispatcher(
        config: ForgeConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Any, list[dict[str, Any]]]:
        """The production deploy dispatcher, with only the stage beneath it a recorder."""
        from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher

        legs: list[dict[str, Any]] = []
        fake_leg = _FakeDeploy()

        async def _stage(deploy_cfg: Any, profile: Any, **kwargs: Any) -> Any:
            legs.append({"profile": profile, **kwargs})
            return await fake_leg(**kwargs)

        monkeypatch.setattr("forge.deploy.composition.dispatch_deploy_stage", _stage)
        dispatcher = build_in_daemon_deploy_dispatcher(
            config=config, nats_client=object(), db_path=tmp_path / "forge.db"
        )
        return dispatcher, legs

    @pytest.mark.asyncio
    async def test_the_candidate_check_passes_with_nothing_on_this_side(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        # The project's own profile, committed in the clone in the sandbox and
        # nowhere else; the build is recorded as starting from that commit.
        self._commit_the_profile(clone, self._PROFILE, "the project's deploy profile")
        _git(clone, "rebase", "-q", "main", f"autobuild/{FEATURE_ID}")
        _git(clone, "checkout", "-q", "main")
        _git(clone, "push", "-q", "origin", "main")
        started_at = _git(clone, "rev-parse", "main")
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        assert not on_this_side.exists()

        dispatcher, legs = self._the_real_dispatcher(config, monkeypatch, tmp_path)

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=dispatcher,
            expect_main_sha=started_at,
            start_commit=started_at,
        )

        # Past the candidate check: nothing published, so publication waits.
        assert outcome.result == "publication-pending", outcome.detail
        assert outcome.gate_before_merge["checks_passed"] == 8
        check = legs[0]
        assert check["leg"] == "candidate_check"
        # The profile the leg was composed from is the one in the clone ...
        assert check["profile"].env_id == "apitest"
        # ... and the press read it there too: the identity the project
        # declares was handed to the check under the project's own setting.
        assert set(check["identity_env"] or {}) == {"DEPLOY_IDENTITY"}
        assert check["candidate_cwd"] == str(clone / ".forge-candidates" / FEATURE_ID)
        # With an identity on record, the cleanup names the candidate it removes,
        # and that leg's profile came from the sandbox as well.
        assert [leg["leg"] for leg in legs] == ["candidate_check", "candidate_down"]
        assert legs[1]["profile"].env_id == "apitest"
        # Not one git command, and no profile read, on this side.
        assert git_calls.any_in(on_this_side) == []
        assert not on_this_side.exists()

    @pytest.mark.asyncio
    async def test_the_profile_is_read_at_the_recorded_start_not_at_the_clones_head(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        """The clone's HEAD has moved past the commit this build is recorded as
        starting from, and the profile differs between the two. The press and
        every deploy leg read it at the recorded start, never at the HEAD."""
        started_at = self._commit_the_profile(
            clone, self._PROFILE, "the project's deploy profile"
        )
        _git(clone, "rebase", "-q", "main", f"autobuild/{FEATURE_ID}")
        _git(clone, "checkout", "-q", "main")
        _git(clone, "push", "-q", "origin", "main")
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        # Afterwards, in the clone only: a different environment and a
        # different identity setting, at what is now its HEAD.
        moved = self._commit_the_profile(
            clone,
            {
                **self._PROFILE,
                "env_id": "moved-on",
                "identity": {"setting": "SOME_LATER_SETTING", "reported_as": "LATER"},
            },
            "a later change to the deploy profile",
        )
        assert _git(clone, "rev-parse", "HEAD") == moved != started_at
        dispatcher, legs = self._the_real_dispatcher(config, monkeypatch, tmp_path)

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=dispatcher,
            expect_main_sha=started_at,
            start_commit=started_at,
        )

        assert outcome.result == "publication-pending", outcome.detail
        assert [leg["leg"] for leg in legs] == ["candidate_check", "candidate_down"]
        # The dispatcher composed both legs from the profile at the start ...
        assert [leg["profile"].env_id for leg in legs] == ["apitest", "apitest"]
        # ... and the press handed the identity under the setting declared at
        # the start, not the one the clone's HEAD now names.
        assert set(legs[0]["identity_env"] or {}) == {"DEPLOY_IDENTITY"}
        assert set(legs[1]["identity_env"] or {}) == {"DEPLOY_IDENTITY"}
        assert git_calls.any_in(on_this_side) == []


class TestTheCompositionOnlyRoutesWhatItShould:
    def test_an_estate_with_no_sandboxes_composes_nothing_at_all(
        self, plain_checkout: Path
    ) -> None:
        settings = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO_WITHOUT: str(plain_checkout)}},
            }
        )

        assert compose_merge_git_surface(settings) is None

    def test_a_repository_without_a_sandbox_gets_no_surface(
        self, config: ForgeConfig, plain_checkout: Path, on_this_side: Path
    ) -> None:
        from forge.deploy.sidecar_git import SidecarCandidateGit

        surface_for = compose_merge_git_surface(config)
        assert surface_for is not None

        assert surface_for(REPO_WITHOUT, plain_checkout) is None
        sandboxed = surface_for(REPO, on_this_side)
        assert isinstance(sandboxed, SidecarCandidateGit)
        assert sandboxed.repo == REPO
        # The same repository asked twice is the same client, not a new one.
        assert surface_for(REPO, on_this_side) is sandboxed

    @pytest.mark.asyncio
    async def test_the_merge_cards_pin_is_read_where_the_repository_lives(
        self,
        config: ForgeConfig,
        clone: Path,
        plain_checkout: Path,
        on_this_side: Path,
    ) -> None:
        """The card pins the commit the merge is held to (rule 89).

        Read on this side for a sandboxed repository it would be a commit the
        merge inside the sandbox has never heard of, and every press would
        refuse on the pin. So the offer reads main where the merge will.
        """
        from forge.cli.serve import compose_merge_offer_git_head

        read_main = compose_merge_offer_git_head(config)
        assert read_main is not None

        assert await read_main(on_this_side) == _git(clone, "rev-parse", "main")
        assert await read_main(plain_checkout) == _git(
            plain_checkout, "rev-parse", "main"
        )

    def test_an_estate_with_no_sandboxes_leaves_the_offers_own_reader_alone(
        self, plain_checkout: Path
    ) -> None:
        from forge.cli.serve import compose_merge_offer_git_head

        settings = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {"target_repo_paths": {REPO_WITHOUT: str(plain_checkout)}},
            }
        )

        assert compose_merge_offer_git_head(settings) is None


# ---------------------------------------------------------------------------
# L3c (rule 79): the words after a green merge that landed in the sandbox
# ---------------------------------------------------------------------------


def _receipt(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / f"merge-{BUILD_ID}" / name).read_text(encoding="utf-8"))


class TestTheWordsAfterAGreenMergeInTheSandbox:
    """A merge that landed in the factory's clone says where it landed and how
    to bring it over (sandbox first, 2026-09-07, rule 79)."""

    @pytest.mark.skip(
        reason=(
            "the sentence this pins told an operator to fast-forward their own "
            "checkout onto the sandbox's main. The merge word now joins onto a "
            "branch of the factory's own and publication is switched off, so "
            "there is nothing on anybody's main to fetch and saying so would be "
            "false. A true version of it belongs to the publisher stage."
        )
    )
    @pytest.mark.asyncio
    async def test_the_report_carries_the_plain_sentence_and_the_exact_command(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")
        publisher = _FakePublisher()

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=_FakeDeploy(),
            expect_main_sha=_git(clone, "rev-parse", "main"),
            publisher=publisher,
        )

        assert outcome.result == "publication-pending", outcome.detail
        checkout = str(on_this_side)
        command = (
            f"git -C {checkout} fetch sandbox-api-test-factory main && "
            f"git -C {checkout} merge --ff-only sandbox-api-test-factory/main"
        )
        # The sentence a person reads: where it landed, and the one command.
        assert outcome.detail.endswith(
            "This merge landed in the factory's own copy of the repository, "
            "inside the sandbox api-test-factory — not in your checkout at "
            f"{checkout}. To bring it to your checkout, run: {command}"
        ), outcome.detail
        # The green line's own words are still in front of it.
        assert "checked in the sandbox (8 of 8), merged and running" in outcome.detail
        # And the same, in parts, on the report the thread is written from —
        # so the words are forge's, never invented downstream.
        said = outcome.sandbox_merge
        assert said == {
            "sandbox": "api-test-factory",
            "remote": "sandbox-api-test-factory",
            "checkout": checkout,
            "fetch_command": command,
            "sentence": said["sentence"],
        }
        raw = publisher.reports[0].model_dump(mode="json")
        assert raw["sandbox_merge"]["fetch_command"] == command
        assert raw["detail"].endswith(command)

    @pytest.mark.skip(
        reason=(
            "the sentence this pins told an operator to fast-forward their own "
            "checkout onto the sandbox's main. The merge word now joins onto a "
            "branch of the factory's own and publication is switched off, so "
            "there is nothing on anybody's main to fetch and saying so would be "
            "false. A true version of it belongs to the publisher stage."
        )
    )
    @pytest.mark.asyncio
    async def test_the_merge_receipt_records_where_the_merge_landed(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        tip = _git(clone, "rev-parse", f"autobuild/{FEATURE_ID}")

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO,
            repo_root=on_this_side,
            merge=_FakeMergeCommand(tip),
            deploy=_FakeDeploy(),
            expect_main_sha=_git(clone, "rev-parse", "main"),
        )

        assert outcome.result == "publication-pending", outcome.detail
        landed = _receipt(_receipts_env, "merge_deploy_merge.json")[
            "landed_in_the_sandbox"
        ]
        assert landed == outcome.sandbox_merge
        # The report's own receipt says it too, and so does the build's record.
        report = _receipt(_receipts_env, "merge_deploy_report.json")
        assert report["sandbox_merge"] == outcome.sandbox_merge
        rows = [
            row
            for row in pool.read_stages(BUILD_ID)
            if row.target_identifier == "merge_deploy_executor"
        ]
        assert rows[-1].details["sandbox_merge"]["fetch_command"] == landed[
            "fetch_command"
        ]

    @pytest.mark.skip(
        reason=(
            "the sentence this pins told an operator to fast-forward their own "
            "checkout onto the sandbox's main. The merge word now joins onto a "
            "branch of the factory's own and publication is switched off, so "
            "there is nothing on anybody's main to fetch and saying so would be "
            "false. A true version of it belongs to the publisher stage."
        )
    )
    @pytest.mark.asyncio
    async def test_a_repository_without_a_sandbox_says_and_records_nothing_extra(
        self,
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        plain_checkout: Path,
        git_calls: _GitCalls,
        _receipts_env: Path,
    ) -> None:
        """The same settings, the other repository: the words it always had."""
        tip = _git(plain_checkout, "rev-parse", f"autobuild/{FEATURE_ID}")
        publisher = _FakePublisher()

        outcome = await _press(
            config=config,
            pool=pool,
            repo=REPO_WITHOUT,
            repo_root=plain_checkout,
            merge=_FakeMergeCommand(tip),
            deploy=_FakeDeploy(),
            expect_main_sha=_git(plain_checkout, "rev-parse", "main"),
            publisher=publisher,
        )

        assert outcome.result == "merged-and-running"
        assert outcome.detail == (
            f"{FEATURE_ID} checked in the sandbox (8 of 8), merged and running. "
            "Rollback is one command; the branch is kept."
        )
        assert outcome.sandbox_merge is None
        assert "sandbox_merge" not in publisher.reports[0].model_dump(mode="json")
        assert "landed_in_the_sandbox" not in _receipt(
            _receipts_env, "merge_deploy_merge.json"
        )
        assert "sandbox_merge" not in _receipt(
            _receipts_env, "merge_deploy_report.json"
        )


class TestTheFetchWordsThemselves:
    """The words are made from the settings, and only for a repository that
    has a sandbox."""

    def test_the_command_names_the_checkout_and_the_sandbox_remote(
        self, config: ForgeConfig, on_this_side: Path
    ) -> None:
        from forge.pipeline.merge_executor import sandbox_merge_words

        said = sandbox_merge_words(config, REPO, on_this_side)

        assert said is not None
        assert said["fetch_command"] == (
            f"git -C {on_this_side} fetch sandbox-api-test-factory main && "
            f"git -C {on_this_side} merge --ff-only sandbox-api-test-factory/main"
        )
        assert said["sentence"].endswith(said["fetch_command"])
        assert "api-test-factory" in said["sentence"]

    def test_a_repository_with_no_sandbox_has_no_words(
        self, config: ForgeConfig, plain_checkout: Path
    ) -> None:
        from forge.pipeline.merge_executor import sandbox_merge_words

        assert sandbox_merge_words(config, REPO_WITHOUT, plain_checkout) is None

    def test_settings_of_another_shape_are_answered_with_silence(
        self, plain_checkout: Path
    ) -> None:
        """A report's words must never be the thing that fails a merge."""
        from forge.pipeline.merge_executor import sandbox_merge_words

        assert sandbox_merge_words(object(), REPO, plain_checkout) is None
        assert sandbox_merge_words(None, REPO, plain_checkout) is None


# ---------------------------------------------------------------------------
# Every deploy leg runs the project's own steps at the commit it is about
# ---------------------------------------------------------------------------

#: The project's deploy step, as it ships with a commit. It writes one line per
#: run to a log beside the test — which version ran, in which mode, and where —
#: and answers each mode the way the project's identity block says it will.
_NEW_DEPLOY_STEP = """#!/bin/sh
mode=deploy
[ "${{CANDIDATE:-}}" = 1 ] && mode=candidate
[ "${{PROMOTE:-}}" = 1 ] && mode=promote
[ "${{RUNNING_IDENTITY:-}}" = 1 ] && mode=ask
[ "${{CANDIDATE_DOWN:-}}" = 1 ] && mode=down
[ "${{REVERT:-}}" = 1 ] && mode=revert
echo "new $mode $(pwd)" >> {log}
case "$mode" in
  candidate) echo "CHECKED_ARTIFACT=artifact-of-${{DEPLOY_IDENTITY}}" ;;
  ask) echo "RUNNING_IDENTITY=none" ;;
  promote) echo "DEPLOYED_IDENTITY=${{DEPLOY_IDENTITY}}" ;;
esac
exit 0
"""

#: The same step months earlier, as the clone's working copy still has it. It
#: knows none of the modes — like api_test's July script on 3 October 2026,
#: which took the read-only question for a plain deploy.
_OLD_DEPLOY_STEP = """#!/bin/sh
echo "old deploy $(pwd)" >> {log}
exit 0
"""

_NEW_HEALTH_CHECK = """#!/bin/sh
echo "new health $(pwd)" >> {log}
exit 0
"""

_OLD_HEALTH_CHECK = """#!/bin/sh
echo "old health $(pwd)" >> {log}
exit 0
"""

#: The project's own live checks, as they ship with a commit and as the working
#: copy still has them. Both pass; each writes down which version ran, where.
_NEW_LIVE_GATE = """#!/usr/bin/env python3
import json, os
with open("LOGFILE", "a") as log:
    log.write("new gate " + os.getcwd() + "\\n")
print(json.dumps({"run_id": "run-gate", "verdict": "pass", "evidence_index_ref": "",
                  "gates": [{"gate_id": "health", "exit_code": 0, "assertions": [
                      {"id": "health::status", "status": "pass"}]}]}))
"""

_OLD_LIVE_GATE = _NEW_LIVE_GATE.replace('"new gate "', '"old gate "')

_A_DEPLOYABLE_PROFILE = {
    "env_id": "live",
    "compose": {"file": "docker-compose.yml", "script": "deploy/deploy.sh"},
    "identity": {
        "setting": "DEPLOY_IDENTITY",
        "reported_as": "DEPLOYED_IDENTITY",
        "checked_as": "CHECKED_ARTIFACT",
        "artifact_setting": "DEPLOY_ARTIFACT",
        "asked_with": "RUNNING_IDENTITY",
        "running_as": "RUNNING_IDENTITY",
    },
    "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
    "live_gate": {"driver": ["python3", "qa/gate.py"], "timeout_seconds": 60},
    "candidate": {"env": {"CANDIDATE_PORT": "8902"}, "keep": False},
    "rollback_image_ref": "the-project:rollback",
}


class TestEveryDeployLegRunsTheStepsOfItsOwnCommit:
    """3 October 2026: the live app went down because the press asked the
    project what it was running with a deploy script from July.

    A repository with a sandbox is deployed out of the sandbox's own clone, and
    nothing keeps that clone's working copy up to date. The candidate check
    already ran from a tree laid out at the commit being checked; every other
    leg ran the project's scripts out of the working copy. So here the working
    copy is left at an old commit whose deploy step knows none of the modes,
    and the whole press is driven through the REAL deploy dispatcher, the REAL
    deploy stage, and a REAL deploy helper with its real executor — so every
    one of the project's steps is really run, and writes down which version of
    itself ran and where. Not one line may come from the working copy.
    """

    @staticmethod
    def _write(path: Path, text: str, *, executable: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if executable:
            path.chmod(0o755)

    def _commit_the_steps(
        self,
        clone: Path,
        *,
        deploy: str,
        health: str,
        gate: str,
        log: Path,
        message: str,
    ) -> str:
        import yaml

        self._write(
            clone / "deploy" / "profile.yaml", yaml.safe_dump(_A_DEPLOYABLE_PROFILE)
        )
        self._write(
            clone / "deploy" / "deploy.sh", deploy.format(log=log), executable=True
        )
        self._write(
            clone / "deploy" / "healthcheck.sh",
            health.format(log=log),
            executable=True,
        )
        self._write(clone / "qa" / "gate.py", gate.replace("LOGFILE", str(log)))
        _git(clone, "add", "deploy", "qa")
        _git(clone, "commit", "-q", "-m", message)
        return _git(clone, "rev-parse", "HEAD")

    def _a_clone_left_behind(
        self, clone: Path, log: Path, *, deploy: str = _NEW_DEPLOY_STEP
    ) -> tuple[str, str]:
        """``(old, started_at)``: main carries the steps as they ship now, the
        build starts there, and the clone's working copy is left months back."""
        old = self._commit_the_steps(
            clone,
            deploy=_OLD_DEPLOY_STEP,
            health=_OLD_HEALTH_CHECK,
            gate=_OLD_LIVE_GATE,
            log=log,
            message="the deploy steps, in July",
        )
        started_at = self._commit_the_steps(
            clone,
            deploy=deploy,
            health=_NEW_HEALTH_CHECK,
            gate=_NEW_LIVE_GATE,
            log=log,
            message="the deploy steps, as they ship now",
        )
        _git(clone, "push", "-q", "origin", "main")
        _git(clone, "rebase", "-q", "main", f"autobuild/{FEATURE_ID}")
        # THE STALE WORKING COPY: the clone is left where it was months ago.
        _git(clone, "checkout", "-q", "--detach", old)
        assert "old deploy" in (clone / "deploy" / "deploy.sh").read_text()
        return old, started_at

    @staticmethod
    def _config(helper_url: str, on_this_side: Path) -> ForgeConfig:
        return ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "planning": {
                    "target_repo_paths": {REPO: str(on_this_side)},
                    "sandboxes": {
                        REPO: {
                            "name": "api-test-factory",
                            "sidecar_url": helper_url,
                            "runner_url": "http://127.0.0.1:8924",
                        }
                    },
                },
                "approval": {"expected_approver": "rich"},
                "merge_executor": {"enabled": True},
                "deploy": {"enabled": True, "run_live_gate": True},
                "publication": {
                    "enabled": True,
                    "publisher_url": "http://127.0.0.1:1",
                    "builds_may_run_inside_the_coordinator": False,
                    "publisher_credential_file": "/etc/forge-publisher/credential",
                },
            }
        )

    @staticmethod
    async def _press_it(
        config: ForgeConfig,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        tmp_path: Path,
        started_at: str,
        monkeypatch: pytest.MonkeyPatch,
        publisher: Any = None,
    ) -> Any:
        """The whole press, with the real dispatcher and stage beneath it.

        The publisher really sends: the joined commit goes to the clone's bare
        "remote", so a later press that reads the remote finds it there."""
        from forge.pipeline.merge_executor import build_in_daemon_deploy_dispatcher
        from forge.pipeline.publication_activation import WhatTheMachineSays
        from tests.forge._a_stand_in_coordinator import a_coordinator_that_recorded
        from tests.forge.pipeline.test_merge_executor import _JoinsForReal

        class _Bus:
            async def publish(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _the_publisher(_config: Any, request: dict[str, Any]) -> dict[str, Any]:
            _git(clone, "push", "-q", "origin", f"{request['j_commit']}:refs/heads/main")
            return {
                "published": True,
                "remote_now": request["j_commit"],
                "contains_j": True,
                "refusal": None,
                "refusal_kind": None,
            }

        _build_row(pool, REPO, started_at)
        deps = MergeExecutorDeps(
            config=config,
            pool=pool,
            pipeline_publisher=_FakePublisher(),
            guardkit_run=_JoinsForReal(feature_id=FEATURE_ID),
            deploy_dispatcher=build_in_daemon_deploy_dispatcher(
                config=config, nats_client=_Bus(), db_path=tmp_path / "forge.db"
            ),
            git_surface=compose_merge_git_surface(config),
            publisher=publisher or _the_publisher,
            what_the_machine_says=WhatTheMachineSays(
                the_publisher_passed_its_self_check=True
            ),
        )
        with a_coordinator_that_recorded({BUILD_ID: started_at}, monkeypatch):
            return await execute_merge_deploy(
                deps=deps,
                build_id=BUILD_ID,
                feature_id=FEATURE_ID,
                repo=REPO,
                repo_root=on_this_side,
                expect_main_sha=started_at,
                correlation_id=CORRELATION,
                decided_by="rich",
            )

    @staticmethod
    def _which_ran(log: Path, clone: Path) -> list[str]:
        """The modes the project's own steps ran in, each checked to be the
        version that ships with the commit, run in that commit's tree."""
        ran = log.read_text(encoding="utf-8").splitlines()
        tree = str(clone / ".forge-candidates" / FEATURE_ID)
        # NOT ONE STEP CAME FROM THE WORKING COPY ...
        assert not [line for line in ran if line.startswith("old ")], ran
        # ... and every one ran in the tree of the joined commit.
        assert ran and all(line.endswith(f" {tree}") for line in ran), ran
        return [line.split(" ")[1] for line in ran]

    @pytest.fixture
    def a_helper_that_deploys(
        self, clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The helper in the sandbox, WITH the executor a real deploy goes through.

        Yields its address and the coordinator's answer to "who owns this
        target", which a test can change when the target's counter moves.
        """
        from forge.deploy_sidecar.deploy_executor import DeployExecutor
        from forge.deploy_sidecar.service import SIDECAR_IN_SANDBOX_ENV

        monkeypatch.setenv(SIDECAR_IN_SANDBOX_ENV, "1")
        sidecar_config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
                "planning": {"target_repo_paths": {REPO: str(clone)}},
            }
        )
        # The counter the press's lock grants a fresh target, and this build.
        owner = {"counter": 1, "build": BUILD_ID}
        executor = DeployExecutor(
            notes_root=tmp_path / "executor-notes",
            ask_the_coordinator=lambda target: dict(owner),
        )
        executor.reconcile()
        srv = build_server(
            port=0, config_loader=lambda: sidecar_config, deploy_executor=executor
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host, port = srv.server_address[:2]
        try:
            yield f"http://{host}:{port}", owner
        finally:
            srv.shutdown()
            srv.server_close()

    @pytest.mark.asyncio
    async def test_every_step_runs_at_the_commit_and_a_passing_gate_is_kept(
        self,
        a_helper_that_deploys: tuple[str, dict[str, Any]],
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        from forge.pipeline.publication_record import (
            RESULT_MERGED_AND_RUNNING,
            PublicationRecordStore,
        )

        log = tmp_path / "which-steps-ran.log"
        old, started_at = self._a_clone_left_behind(clone, log)
        config = self._config(a_helper_that_deploys[0], on_this_side)
        # The coordinator's own path for the repository holds nothing.
        assert not on_this_side.exists()

        outcome = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )

        assert outcome.result == "merged-into-the-remote-and-running", outcome.detail
        assert (
            PublicationRecordStore(pool.connection).read(BUILD_ID).result
            == RESULT_MERGED_AND_RUNNING
        )
        # Every leg is there: the check with its health check and live gate,
        # the question, the promote with its health check, the candidate's
        # teardown and the live gate on what is now live — which PASSED and
        # was read as a pass, so nothing was rolled back. (The press's own
        # cleanup asks for the teardown once more under the same run, and the
        # stage turns that repeat away before any step runs.)
        assert self._which_ran(log, clone) == [
            "candidate",
            "health",
            "gate",
            "ask",
            "promote",
            "health",
            "down",
            "gate",
        ]
        # The tree goes when the press ends, as it always did.
        assert not (clone / ".forge-candidates" / FEATURE_ID).exists()
        # And the clone's working copy was left exactly where it was.
        assert _git(clone, "rev-parse", "HEAD") == old
        # THE CARD SAYS WHERE IT RAN, AND NOTHING WAS WRITTEN ON THIS SIDE
        # (3 October 2026). The words come from the repository having a
        # sandbox, not from a profile at the coordinator's path, which holds
        # nothing; and the deploy records went to a folder of the
        # coordinator's own beside its ledger, not under that path.
        assert outcome.deployed_in == "docker-sandbox"
        assert not on_this_side.exists()
        records = tmp_path / "deploy-records" / "api_test"
        assert list(records.rglob("deploy-record-*.md")), (
            "the deploy record was not written to the coordinator's own folder"
        )

    @pytest.mark.asyncio
    async def test_a_press_that_picks_up_a_published_join_lays_its_tree_out_again(
        self,
        a_helper_that_deploys: tuple[str, dict[str, Any]],
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        """The pick-up runs no check, so no tree is standing when it deploys.
        It lays the joined commit's tree out for itself rather than running
        the project's steps from the working copy."""
        from forge.pipeline.deployment_lock import DeploymentLockStore

        log = tmp_path / "which-steps-ran.log"
        _, started_at = self._a_clone_left_behind(clone, log)
        helper_url, owner = a_helper_that_deploys
        config = self._config(helper_url, on_this_side)
        # Another build holds the target, so the first press publishes and
        # deploys nothing.
        lock = DeploymentLockStore(pool.connection)
        target = f"{REPO}::live"
        held = lock.grant(
            target=target,
            build_id="another-build",
            turn=1,
            holder="somebody-else",
            now=datetime.now(UTC),
        )
        assert held is not None

        first = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )

        assert first.result == "published-deployment-pending", first.detail
        assert "holds the deployment lock" in first.detail
        assert not (clone / ".forge-candidates" / FEATURE_ID).exists()
        log.write_text("", encoding="utf-8")
        # The other build lets go; this build's grant is the target's second.
        assert lock.release(target=target, counter=held.counter, now=datetime.now(UTC))
        owner["counter"] = held.counter + 1

        again = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )

        assert again.result == "merged-into-the-remote-and-running", again.detail
        assert self._which_ran(log, clone) == [
            "ask",
            "promote",
            "health",
            "down",
            "gate",
        ]
        assert not (clone / ".forge-candidates" / FEATURE_ID).exists()

    @staticmethod
    def _the_clone_can_no_longer_lay_a_tree_out(
        monkeypatch: pytest.MonkeyPatch, *, after: int = 0
    ) -> list[str]:
        """From the ``after``-th lay-out on, the helper cannot lay a tree out.

        The helper's own route is left real; only the lay-out it calls refuses,
        in the words a real failure carries. Returns the commits it was asked
        to lay out, refused or not.
        """
        from forge.deploy import candidate_tree

        real = candidate_tree.materialise_candidate_tree
        asked: list[str] = []

        async def _lay_out(repo_root: Any, feature_id: str, sha: str) -> Any:
            asked.append(str(sha))
            if len(asked) > after:
                raise candidate_tree.CandidateTreeError(
                    f"git archive {sha} failed: the clone could not be read"
                )
            return await real(repo_root, feature_id, sha)

        monkeypatch.setattr(candidate_tree, "materialise_candidate_tree", _lay_out)
        return asked

    @pytest.mark.asyncio
    async def test_a_deploy_whose_tree_cannot_be_laid_out_asks_and_deploys_nothing(
        self,
        a_helper_that_deploys: tuple[str, dict[str, Any]],
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        """No tree of the joined commit, no question and no promote — and
        never the working copy's steps instead."""
        from forge.pipeline.deployment_lock import DeploymentLockStore

        log = tmp_path / "which-steps-ran.log"
        _, started_at = self._a_clone_left_behind(clone, log)
        config = self._config(a_helper_that_deploys[0], on_this_side)
        # The first press publishes and deploys nothing: the target is held.
        lock = DeploymentLockStore(pool.connection)
        target = f"{REPO}::live"
        held = lock.grant(
            target=target,
            build_id="another-build",
            turn=1,
            holder="somebody-else",
            now=datetime.now(UTC),
        )
        assert held is not None
        first = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )
        assert first.result == "published-deployment-pending", first.detail
        assert lock.release(target=target, counter=held.counter, now=datetime.now(UTC))
        log.write_text("", encoding="utf-8")
        # The pick-up has to lay the joined commit's tree out, and cannot.
        asked = self._the_clone_can_no_longer_lay_a_tree_out(monkeypatch)

        again = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )

        assert again.result == "published-deployment-pending", again.detail
        assert "could not be laid out" in again.detail
        assert "was not asked what it is running and nothing was deployed" in (
            again.detail
        )
        # It was asked for the joined commit's tree, once ...
        assert len(asked) == 1
        # ... and not one of the project's steps ran: no question, no promote,
        # and nothing out of the working copy instead.
        assert log.read_text(encoding="utf-8") == ""
        # The target was never taken for this build.
        assert lock.read(target).counter == held.counter

    @pytest.mark.asyncio
    async def test_a_pick_up_whose_lay_out_answer_was_lost_keeps_the_hold(
        self,
        a_helper_that_deploys: tuple[str, dict[str, Any]],
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        """The coach's G4, 4 October 2026: the pick-up's request to lay the
        joined commit's tree out is sent and the connection drops. The sandbox
        may still be writing that tree, so although the press ends "published,
        deployment pending" the hold is kept and the next press is refused."""
        from forge.deploy.sidecar_git import SidecarCandidateGit
        from forge.pipeline.deployment_lock import DeploymentLockStore

        log = tmp_path / "which-steps-ran.log"
        _, started_at = self._a_clone_left_behind(clone, log)
        config = self._config(a_helper_that_deploys[0], on_this_side)
        lock = DeploymentLockStore(pool.connection)
        target = f"{REPO}::live"
        held = lock.grant(
            target=target,
            build_id="another-build",
            turn=1,
            holder="somebody-else",
            now=datetime.now(UTC),
        )
        first = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )
        assert first.result == "published-deployment-pending", first.detail
        assert lock.release(target=target, counter=held.counter, now=datetime.now(UTC))
        # From here, the sandbox takes the lay-out request and the connection
        # drops before any answer comes back.
        real_call = SidecarCandidateGit._call
        dropped: list[str] = []

        async def _call(self: Any, route: str, body: dict[str, Any], *, timeout: float) -> Any:
            if route == "/git/candidate-tree":
                dropped.append(route)
                return ConnectionResetError("the connection dropped mid-request")
            return await real_call(self, route, body, timeout=timeout)

        monkeypatch.setattr(SidecarCandidateGit, "_call", _call)

        again = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )

        assert again.result == "published-deployment-pending", again.detail
        assert "could not be laid out" in again.detail
        assert dropped == ["/git/candidate-tree"]
        from forge.pipeline.publication_record import PublicationRecordStore

        assert PublicationRecordStore(pool.connection).read(BUILD_ID).lease_holder
        third = await self._press_it(
            config, pool, clone, on_this_side, tmp_path, started_at, monkeypatch
        )
        assert "another worker" in third.detail

    @pytest.mark.asyncio
    async def test_a_candidate_whose_tree_cannot_be_laid_out_is_left_standing_and_said(
        self,
        a_helper_that_deploys: tuple[str, dict[str, Any]],
        pool: SqliteLifecyclePersistence,
        clone: Path,
        on_this_side: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _receipts_env: Path,
    ) -> None:
        """The cleanup that cannot have the candidate's own tree leaves the
        candidate where it is and says so, rather than run the working copy's
        teardown step — which may not know the teardown at all.

        Driven the one way the press reaches it: the remote moves under the
        send, the first attempt's candidate does not come down (its teardown
        step fails) and its tree is removed, the next attempt's tree cannot be
        laid out, and at the end the cleanup cannot lay out the first
        candidate's tree again either.
        """
        log = tmp_path / "which-steps-ran.log"
        a_teardown_that_fails = _NEW_DEPLOY_STEP.replace(
            "exit 0\n", '[ "$mode" = down ] && exit 1\nexit 0\n'
        )
        _, started_at = self._a_clone_left_behind(
            clone, log, deploy=a_teardown_that_fails
        )
        config = self._config(a_helper_that_deploys[0], on_this_side)
        bare = _git(clone, "remote", "get-url", "origin")
        asked = self._the_clone_can_no_longer_lay_a_tree_out(monkeypatch, after=1)

        async def _the_remote_moves(_config: Any, request: dict[str, Any]) -> dict[str, Any]:
            # Somebody else lands work on the remote under the send.
            other = tmp_path / "somebody-else"
            subprocess.run(
                ["git", "clone", "-q", bare, str(other)],
                check=True,
                capture_output=True,
                env=_GIT_ENV,
            )
            (other / "elsewhere.txt").write_text("their work\n", encoding="utf-8")
            _git(other, "add", "elsewhere.txt")
            _git(other, "commit", "-q", "-m", "somebody else's work")
            _git(other, "push", "-q", "origin", "main")
            return {
                "published": False,
                "remote_now": _git(other, "rev-parse", "main"),
                "contains_j": False,
                "refusal": "the remote moved under the send",
                "refusal_kind": "the-remote-moved",
            }

        outcome = await self._press_it(
            config,
            pool,
            clone,
            on_this_side,
            tmp_path,
            started_at,
            monkeypatch,
            publisher=_the_remote_moves,
        )

        assert outcome.result == "candidate-refused", outcome.detail
        # The first attempt's tree, the second attempt's (refused), and the
        # first candidate's tree again at the cleanup (refused).
        assert len(asked) == 3 and asked[2] == asked[0] != asked[1], asked
        # The check ran from the first tree, and its teardown was tried there
        # and failed; nothing at all ran from the working copy, and nothing
        # ran after the cleanup could not lay the candidate's tree out.
        assert self._which_ran(log, clone) == ["candidate", "health", "gate", "down"]
        left = _receipt(_receipts_env, "merge_deploy_cleanup.json")
        assert left["candidate_torn_down"] is False
        sentence = left["candidate_left_standing"]
        assert "was left in place because the tree of" in sentence
        assert "could not be laid out" in sentence
        assert "remove it by hand" in sentence
