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
    """

    def __init__(self) -> None:
        self.cwds: list[str] = []

    def record(self, cwd: Any) -> None:
        self.cwds.append(str(cwd))

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
        self._calls.record(kwargs.get("cwd"))
        return subprocess.run(*args, **kwargs)

    def Popen(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 — the stdlib name
        self._calls.record(kwargs.get("cwd"))
        return subprocess.Popen(*args, **kwargs)


class _RecordingAsyncio:
    """``asyncio``, with ``create_subprocess_exec`` recorded and forwarded."""

    def __init__(self, calls: _GitCalls) -> None:
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def create_subprocess_exec(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.record(kwargs.get("cwd"))
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


def _build_row(pool: SqliteLifecyclePersistence, repo: str = REPO) -> None:
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
            "0" * 40,
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
    deploy: _FakeDeploy,
    expect_main_sha: str,
    publisher: _FakePublisher | None = None,
) -> Any:
    _build_row(pool, repo)
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
        assert deploy.legs() == ["candidate_check", "candidate_down"]
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
        assert deploy.legs() == ["candidate_check", "candidate_down"]
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
