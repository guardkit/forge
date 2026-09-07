"""Where a fix journey's legs, its tree and its receipts happen when the
repository has a sandbox (sandbox first, 2026-09-07, rules 75, 76 and 77).

Rich's rule: nothing the factory runs on a repository runs on the host. These
tests drive the three seams that decide where the work goes, and they drive
the whole way down where that is possible: the journey worktree is cut for
real, over a real loopback HTTP server running the real sidecar routes,
against a real git repository in ``tmp_path``; the receipts are copied out the
same way. Nothing live is touched — no sandbox, no ``sbx``, no docker, no
service.

The other half of every test is the repository that has NO sandbox: it must
behave exactly as it did before this lane, and each case says so.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._conductor_worktree import (
    WorktreeReady,
    WorktreeRefused,
    journey_branch_name,
    prepare_journey_worktree,
)
from forge.cli._serve_conductor import (
    make_conductor_guardkit_run_chooser,
    make_conductor_receipts_exporter,
)
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence

REPO_KEY = "guardkit/api_test"
PLAIN_KEY = "guardkit/no_sandbox"
BUILD_ID = "build-FEAT-SBX1-20260907170000"
TASK_ID = "TASK-SBX-001"

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
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
        ["git", *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


def _scratch_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    (path / "README").write_text("scratch\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path.resolve()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """Stands in for the factory's own clone inside the sandbox."""
    return _scratch_repo(tmp_path / "api_test")


@pytest.fixture
def plain_checkout(tmp_path: Path) -> Path:
    """A repository with no sandbox — today's path, in the container."""
    return _scratch_repo(tmp_path / "no_sandbox")


@pytest.fixture
def receipts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    root.mkdir()
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


def _raw_config(
    clone: Path, plain_checkout: Path, sidecar_url: str | None
) -> dict[str, Any]:
    planning: dict[str, Any] = {
        "target_repo_paths": {
            REPO_KEY: str(clone),
            PLAIN_KEY: str(plain_checkout),
        }
    }
    if sidecar_url is not None:
        planning["sandboxes"] = {
            REPO_KEY: {
                "name": "api-test-factory",
                "sidecar_url": sidecar_url,
                "runner_url": "http://127.0.0.1:8224",
            }
        }
    return {
        "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
        "planning": planning,
        "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
    }


@pytest.fixture
def sidecar(clone: Path, plain_checkout: Path):
    """A real sidecar on an ephemeral loopback port, over the real routes.

    It answers for the same paths the conductor sees, which is exactly the
    shape inside a sandbox: the factory's clone lives at the same path there
    as the repository map names.
    """
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    url = f"http://{host}:{port}"
    holder["config"] = ForgeConfig.model_validate(
        _raw_config(clone, plain_checkout, url)
    )
    try:
        yield SimpleNamespace(url=url, config=holder["config"])
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def plain_config(clone: Path, plain_checkout: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(_raw_config(clone, plain_checkout, None))


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx, db_path=tmp_path / "forge.db")


def _row(pool: SqliteLifecyclePersistence, repo: str, build_id: str = BUILD_ID) -> None:
    pool.connection.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "worktree_path, mode, task_id) VALUES (?, 'FEAT-SBX1', ?, 'main', "
        "'f.yaml', 'RUNNING', 'cli', ?, '2026-09-07T17:00:00Z', NULL, "
        "'mode-c', ?)",
        (build_id, repo, f"corr-{build_id}", TASK_ID),
    )
    pool.connection.commit()


# ---------------------------------------------------------------------------
# Rule 76 — the journey worktree is cut by the sidecar
# ---------------------------------------------------------------------------


class TestTheJourneyWorktreeIsCutInTheSandbox:
    def test_a_sandbox_repositorys_tree_is_cut_over_the_sidecar_and_recorded(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, clone: Path
    ) -> None:
        _row(pool, REPO_KEY)

        outcome = asyncio.run(
            prepare_journey_worktree(pool, sidecar.config, BUILD_ID)
        )

        assert isinstance(outcome, WorktreeReady), getattr(outcome, "reason", "")
        branch = journey_branch_name(TASK_ID, BUILD_ID)
        assert outcome.branch == branch and outcome.reused is False
        tree = Path(outcome.path)
        assert tree == clone / ".forge" / "worktrees" / BUILD_ID
        assert tree.is_dir() and (tree / ".git").exists()
        assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch
        row = pool.get_build_row(BUILD_ID)
        assert row.worktree_path == str(tree)
        assert row.merge_branch == branch

    def test_a_redelivery_of_the_same_build_reuses_its_tree(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        _row(pool, REPO_KEY)
        first = asyncio.run(prepare_journey_worktree(pool, sidecar.config, BUILD_ID))
        second = asyncio.run(prepare_journey_worktree(pool, sidecar.config, BUILD_ID))
        assert isinstance(first, WorktreeReady) and first.reused is False
        assert isinstance(second, WorktreeReady) and second.reused is True
        assert second.path == first.path

    def test_a_repository_without_a_sandbox_is_cut_in_the_container_as_before(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        plain_checkout: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same config, same boot — the repository with no sandbox entry never
        goes near the wire."""
        import forge.planning.sidecar_git_runner as wire

        def _never(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a repository without a sandbox used the wire")

        monkeypatch.setattr(wire, "_urllib_post", _never)
        _row(pool, PLAIN_KEY)

        outcome = asyncio.run(
            prepare_journey_worktree(pool, sidecar.config, BUILD_ID)
        )

        assert isinstance(outcome, WorktreeReady), getattr(outcome, "reason", "")
        assert Path(outcome.path) == (
            plain_checkout / ".forge" / "worktrees" / BUILD_ID
        )

    def test_a_sidecar_that_cannot_be_reached_is_a_refusal_that_names_it(
        self, pool: SqliteLifecyclePersistence, clone: Path, plain_checkout: Path
    ) -> None:
        config = ForgeConfig.model_validate(
            _raw_config(clone, plain_checkout, "http://127.0.0.1:1")
        )
        _row(pool, REPO_KEY)

        outcome = asyncio.run(prepare_journey_worktree(pool, config, BUILD_ID))

        assert isinstance(outcome, WorktreeRefused)
        assert "could not be reached" in outcome.reason
        assert "api-test-factory" in outcome.reason

    def test_a_sandbox_repositorys_checkout_is_never_read_on_this_host(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        clone: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """forge-prod does not mount a sandboxed repository's checkout any
        more, so the writer must not stat it. Proven by pointing the map at a
        path that does not exist here and letting the sidecar (which does have
        it) answer for the real one."""
        raw = _raw_config(clone, clone, sidecar.url)
        raw["planning"]["target_repo_paths"][REPO_KEY] = str(clone)
        config = ForgeConfig.model_validate(raw)
        seen: list[str] = []
        real_exists = Path.exists

        def _watch(self: Path) -> bool:
            if self.name == ".git":
                seen.append(str(self))
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", _watch)
        _row(pool, REPO_KEY)

        outcome = asyncio.run(prepare_journey_worktree(pool, config, BUILD_ID))

        assert isinstance(outcome, WorktreeReady)
        assert str(clone / ".git") not in seen


# ---------------------------------------------------------------------------
# Rule 77 — the receipts are exported inside the sandbox
# ---------------------------------------------------------------------------


class TestTheReceiptsAreExportedInTheSandbox:
    def test_a_sandbox_repositorys_receipts_are_exported_over_the_sidecar(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, receipts_root: Path
    ) -> None:
        _row(pool, REPO_KEY)
        ready = asyncio.run(prepare_journey_worktree(pool, sidecar.config, BUILD_ID))
        assert isinstance(ready, WorktreeReady)
        family = Path(ready.path) / ".guardkit" / "autobuild"
        family.mkdir(parents=True)
        (family / "review.json").write_text("{}", encoding="utf-8")

        export = make_conductor_receipts_exporter(pool=pool, config=sidecar.config)
        key = export(
            build_id=BUILD_ID,
            report=SimpleNamespace(
                chosen_stage=SimpleNamespace(value="task-review"),
                rationale="the review leg ran",
            ),
        )

        assert key == "001-task-review"
        dest = receipts_root / BUILD_ID / "stages" / key
        assert (dest / ".guardkit" / "autobuild" / "review.json").is_file()
        assert (dest / "turn-rationale.txt").read_text() == "the review leg ran"

    def test_a_repository_without_a_sandbox_copies_in_the_container_as_before(
        self,
        pool: SqliteLifecyclePersistence,
        sidecar: Any,
        plain_checkout: Path,
        receipts_root: Path,
        tmp_path: Path,
    ) -> None:
        _row(pool, PLAIN_KEY)
        tree = plain_checkout / ".forge" / "worktrees" / BUILD_ID
        (tree / ".guardkit" / "autobuild").mkdir(parents=True)
        (tree / ".guardkit" / "autobuild" / "review.json").write_text("{}")
        pool.record_worktree_path(BUILD_ID, str(tree))

        def _never(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a repository without a sandbox used the wire")

        export = make_conductor_receipts_exporter(
            pool=pool, config=sidecar.config, post=_never
        )
        key = export(
            build_id=BUILD_ID,
            report=SimpleNamespace(
                chosen_stage=SimpleNamespace(value="task-work"), rationale=""
            ),
        )

        assert key == "001-task-work"
        dest = receipts_root / BUILD_ID / "stages" / key
        assert (dest / ".guardkit" / "autobuild" / "review.json").is_file()

    def test_a_turn_that_dispatched_nothing_still_exports_nothing(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        _row(pool, REPO_KEY)
        export = make_conductor_receipts_exporter(pool=pool, config=sidecar.config)
        assert (
            export(
                build_id=BUILD_ID,
                report=SimpleNamespace(chosen_stage=None, rationale="x"),
            )
            is None
        )

    def test_an_unreachable_sidecar_does_not_break_the_turn(
        self,
        pool: SqliteLifecyclePersistence,
        clone: Path,
        plain_checkout: Path,
        receipts_root: Path,
    ) -> None:
        config = ForgeConfig.model_validate(
            _raw_config(clone, plain_checkout, "http://127.0.0.1:1")
        )
        _row(pool, REPO_KEY)
        pool.record_worktree_path(BUILD_ID, str(clone / ".forge" / "worktrees" / BUILD_ID))
        export = make_conductor_receipts_exporter(pool=pool, config=config)

        assert (
            export(
                build_id=BUILD_ID,
                report=SimpleNamespace(
                    chosen_stage=SimpleNamespace(value="task-work"), rationale=""
                ),
            )
            is None
        )


# ---------------------------------------------------------------------------
# Rule 75 — the legs run through the sandbox's sidecar
# ---------------------------------------------------------------------------


class _RefusingPool:
    """A pool that fails the test if anything reads a row from it."""

    def get_build_row(self, build_id: str) -> Any:
        raise AssertionError("no sandboxes configured — no row should be read")


class TestWhereTheLegsRun:
    def test_with_no_sandboxes_every_build_keeps_the_in_container_runner(
        self, plain_config: ForgeConfig
    ) -> None:
        in_container = object()
        choose = make_conductor_guardkit_run_chooser(
            pool=_RefusingPool(), config=plain_config, in_container_run=in_container
        )
        assert choose(BUILD_ID) is in_container
        assert choose("build-anything-else") is in_container

    def test_a_sandbox_repositorys_legs_go_to_that_sandboxs_sidecar(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        _row(pool, REPO_KEY)
        made: list[dict[str, Any]] = []
        sentinel = object()

        def _build(**kwargs: Any) -> Any:
            made.append(kwargs)
            return sentinel

        choose = make_conductor_guardkit_run_chooser(
            pool=pool,
            config=sidecar.config,
            in_container_run=object(),
            build_sidecar_run=_build,
        )

        assert choose(BUILD_ID) is sentinel
        assert made[0]["base_url"] == sidecar.url
        assert made[0]["repo_paths"] == dict(
            sidecar.config.planning.target_repo_paths
        )

    def test_a_repository_without_a_sandbox_keeps_the_in_container_runner(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        _row(pool, PLAIN_KEY)
        in_container = object()

        def _build(**kwargs: Any) -> Any:
            raise AssertionError("a repository without a sandbox was sent to a sidecar")

        choose = make_conductor_guardkit_run_chooser(
            pool=pool,
            config=sidecar.config,
            in_container_run=in_container,
            build_sidecar_run=_build,
        )
        assert choose(BUILD_ID) is in_container

    def test_one_runner_is_made_per_repository_not_per_build(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        _row(pool, REPO_KEY)
        _row(pool, REPO_KEY, build_id="build-FEAT-SBX1-20260907180000")
        made: list[Any] = []

        def _build(**kwargs: Any) -> Any:
            made.append(kwargs)
            return object()

        choose = make_conductor_guardkit_run_chooser(
            pool=pool,
            config=sidecar.config,
            in_container_run=object(),
            build_sidecar_run=_build,
        )
        first = choose(BUILD_ID)
        second = choose("build-FEAT-SBX1-20260907180000")
        assert first is second and len(made) == 1

    def test_a_row_that_cannot_be_read_falls_back_to_the_container(
        self, sidecar: Any
    ) -> None:
        class _Raising:
            def get_build_row(self, build_id: str) -> Any:
                raise RuntimeError("the ledger is unreadable")

        in_container = object()
        choose = make_conductor_guardkit_run_chooser(
            pool=_Raising(), config=sidecar.config, in_container_run=in_container
        )
        assert choose(BUILD_ID) is in_container

    def test_the_production_factory_is_the_sidecar_backed_runner(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        """With no factory injected, the real one is used."""
        _row(pool, REPO_KEY)
        in_container = object()
        choose = make_conductor_guardkit_run_chooser(
            pool=pool, config=sidecar.config, in_container_run=in_container
        )
        chosen = choose(BUILD_ID)
        assert chosen is not in_container
        assert callable(chosen)
        assert chosen.__name__ == "run_leg_via_sidecar"


class TestTheSupervisorFactoryUsesTheChooser:
    def test_the_dispatcher_gets_the_runner_the_chooser_names(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        from forge.cli._serve_conductor import build_conductor_supervisor_factory

        _row(pool, REPO_KEY)
        chosen = object()
        asked: list[str] = []

        def _for_build(build_id: str) -> Any:
            asked.append(build_id)
            return chosen

        made: list[dict[str, Any]] = []

        def _dispatcher_spy(**kwargs: Any) -> Any:
            made.append(kwargs)
            return object()

        import forge.pipeline.dispatchers.conductor_subprocess as mod

        real = mod.make_conductor_subprocess_dispatcher
        mod.make_conductor_subprocess_dispatcher = _dispatcher_spy  # type: ignore[assignment]
        try:
            factory = build_conductor_supervisor_factory(
                pool=pool,
                config=sidecar.config,
                forward_context_builder=object(),
                worktree_allowlist=object(),
                read_allowlist=[Path("/work")],
                subprocess_runner=object(),
                subprocess_runner_for_build=_for_build,
            )
            factory(BUILD_ID)
        finally:
            mod.make_conductor_subprocess_dispatcher = real  # type: ignore[assignment]

        assert asked == [BUILD_ID]
        assert made[0]["subprocess_runner"] is chosen


# ---------------------------------------------------------------------------
# Rule 75, driven the whole way: the chosen runner, a real socket, a real
# process, in the journey worktree
# ---------------------------------------------------------------------------


@pytest.fixture
def leg_guardkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``guardkit`` that records its arguments and its directory."""
    log = tmp_path / "leg-calls.jsonl"
    bin_dir = tmp_path / "legbin"
    bin_dir.mkdir()
    binary = bin_dir / "guardkit"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps("
        "{'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        "print('leg ran')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("FORGE_GUARDKIT_PATH", str(binary))
    return log


class TestALegRunsInTheSandbox:
    def test_the_chosen_runner_runs_the_leg_in_the_journey_worktree(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, leg_guardkit: Path
    ) -> None:
        import json

        _row(pool, REPO_KEY)
        ready = asyncio.run(prepare_journey_worktree(pool, sidecar.config, BUILD_ID))
        assert isinstance(ready, WorktreeReady), getattr(ready, "reason", "")
        choose = make_conductor_guardkit_run_chooser(
            pool=pool, config=sidecar.config, in_container_run=object()
        )
        run = choose(BUILD_ID)

        result = asyncio.run(
            run(
                subcommand="task-review",
                args=["--build-id", BUILD_ID, "--task-id", TASK_ID],
                repo_path=Path(ready.path),
                read_allowlist=None,
                timeout_seconds=60,
            )
        )

        assert result.status == "success" and result.exit_code == 0
        assert "leg ran" in result.stdout_tail
        call = json.loads(leg_guardkit.read_text().splitlines()[0])
        assert call["argv"][0] == "task-review"
        assert Path(call["cwd"]).resolve() == Path(ready.path).resolve()

    def test_the_door_refuses_a_command_that_is_not_one_of_the_two_legs(
        self, pool: SqliteLifecyclePersistence, sidecar: Any
    ) -> None:
        from forge.adapters.guardkit.run_via_sidecar import LegCallRefused

        _row(pool, REPO_KEY)
        run = make_conductor_guardkit_run_chooser(
            pool=pool, config=sidecar.config, in_container_run=object()
        )(BUILD_ID)

        with pytest.raises(LegCallRefused):
            asyncio.run(
                run(
                    subcommand="autobuild",
                    args=["merge", "FEAT-X"],
                    repo_path=Path("/tmp"),
                )
            )

    def test_a_worktree_of_no_known_repository_is_a_failed_result_not_a_raise(
        self, pool: SqliteLifecyclePersistence, sidecar: Any, tmp_path: Path
    ) -> None:
        _row(pool, REPO_KEY)
        run = make_conductor_guardkit_run_chooser(
            pool=pool, config=sidecar.config, in_container_run=object()
        )(BUILD_ID)

        result = asyncio.run(
            run(
                subcommand="task-work",
                args=[],
                repo_path=tmp_path / "nowhere",
                timeout_seconds=5,
            )
        )

        assert result.status == "failed"
        assert "does not know which repository" in (result.stderr or "")
