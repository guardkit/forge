"""The fix journey's worktree writer — every arm (conductor activation §1).

Everything here is driven against a **scratch git checkout** built in
``tmp_path`` with a real ``git`` binary. No registered checkout is ever
touched, and nothing in this file reads the live config or the live DB.

What is pinned:

* the happy path — a tree materialised under the registered checkout on
  ``fix/<task_id>-<build8>`` off ``main``, recorded on
  ``builds.worktree_path``, with the three consumers that refused on a
  NULL column now satisfied (the dispatcher's pre-spawn check, the commit
  probe, the gates reader's directory check);
* the embedded-gitlink hazard — a repo-root ``git add -A`` stages nothing
  from ``.forge/``, because the writer plants the guard file itself;
* the reuse arm and its three collision refusals;
* two journeys for the SAME task not colliding (the branch suffix);
* the allowlist trap, at write time, before anything lands on disk;
* materialise failure, an unregistered repo, a missing task id, a missing
  row, and a record-write that will not land — all refusals, never raises;
* the router seam: a real materialise on the way to TAKEN_RUNNING, and a
  refusing writer becoming a TakenTerminal with the row FAILED.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess  # noqa: S404 — scratch-repo fixtures only, never in src.
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.git.operations import ExecuteResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._conductor_outcome import TakenTerminal
from forge.cli._conductor_worktree import (
    JOURNEY_BASE_REF,
    WorktreeReady,
    WorktreeRefused,
    journey_branch_name,
    prepare_journey_worktree,
    short_build_id,
)
from forge.cli.serve import build_conductor_router
from forge.config.models import (
    FIX_JOURNEY_PROFILE_NAME,
    ConductorConfig,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    PlanningConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.mode_c_commit_probe import make_mode_c_commit_probe

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git binary not available")

REPO_KEY = "appmilla_github/scratch-repo"
TASK_ID = "TASK-WTW-001"

_GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo``, hermetically. Fixture helper only."""
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell.
        [_GIT, *args],
        cwd=repo,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


# --------------------------------------------------------------------------- #
# Fixtures — a scratch registered checkout, a real sqlite pool.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A throwaway 'registered checkout': one commit on ``main``."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README").write_text("scratch\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo.resolve()


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def pool(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


def _payload(
    feature_id: str,
    *,
    repo: str = REPO_KEY,
    task_id: str | None = TASK_ID,
    queued_at: datetime | None = None,
) -> SimpleNamespace:
    ns = SimpleNamespace(
        feature_id=feature_id,
        repo=repo,
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="worktree-writer-test",
        correlation_id=f"cid-{feature_id}",
        parent_request_id=None,
        queued_at=queued_at or datetime(2026, 8, 3, 14, 25, 30, tzinfo=UTC),
    )
    if task_id is not None:
        ns.task_id = task_id
    return ns


def _config(
    checkout: Path,
    *,
    allowlist: "list[Path] | None" = None,
    repo_paths: "dict[str, str] | None" = None,
) -> ForgeConfig:
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(
                allowlist=allowlist if allowlist is not None else [checkout]
            ),
        ),
        planning=PlanningConfig(
            target_repo_paths=(
                repo_paths if repo_paths is not None else {REPO_KEY: str(checkout)}
            )
        ),
        conductor=ConductorConfig(enabled=True),
    )


def _queue_mode_c(
    pool: SqliteLifecyclePersistence,
    feature_id: str,
    **payload_kwargs: Any,
) -> str:
    return pool.record_pending_build(
        _payload(feature_id, **payload_kwargs),
        mode=BuildMode.MODE_C,
        profile=FIX_JOURNEY_PROFILE_NAME,
    )


# --------------------------------------------------------------------------- #
# Branch naming.
# --------------------------------------------------------------------------- #


class TestBranchNaming:
    def test_the_branch_carries_the_task_and_a_per_build_suffix(self) -> None:
        name = journey_branch_name("TASK-X", "build-FEAT-A-20260803142530")
        assert name == "fix/TASK-X-03142530"

    def test_two_journeys_for_one_task_get_different_branches(self) -> None:
        """The verifiers' catch: a bare ``fix/<task_id>`` collides FOREVER."""
        first = journey_branch_name("TASK-X", "build-FEAT-A-20260803142530")
        second = journey_branch_name("TASK-X", "build-FEAT-A-20260803150000")
        assert first != second

    def test_the_short_form_is_deterministic(self) -> None:
        """The reuse arm can only recognise its own tree if this is stable."""
        assert short_build_id("build-FEAT-A-20260803142530") == short_build_id(
            "build-FEAT-A-20260803142530"
        )

    def test_ref_hostile_characters_never_reach_the_branch_name(
        self, checkout: Path
    ) -> None:
        """A hostile-looking subject must not become an illegal ref.

        ``..``, whitespace and ``~`` are all refused by git outright, so
        an unsanitised name would turn every journey for that task into a
        materialise failure — and a stray ``/`` would smuggle in a second
        ref path component. Proven by asking git itself.
        """
        name = journey_branch_name("TASK ../evil~1", "build-x y-20260803142530")

        assert " " not in name and ".." not in name and "~" not in name
        # Exactly one path component under ``fix/``.
        assert name.count("/") == 1
        _git(checkout, "check-ref-format", f"refs/heads/{name}")


# --------------------------------------------------------------------------- #
# The happy path, and the consumers it unblocks.
# --------------------------------------------------------------------------- #


class TestMaterialise:
    @pytest.mark.asyncio
    async def test_a_tree_is_made_recorded_and_on_a_named_branch_off_main(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTW1")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeReady), outcome
        assert outcome.reused is False
        expected = checkout / ".forge" / "worktrees" / build_id
        assert Path(outcome.path) == expected
        assert expected.is_dir()
        # Invariant 4: a NAMED branch, never a detached HEAD (the work
        # leg's branch detection degrades to a 'main' fallback on detach).
        assert (
            _git(expected, "rev-parse", "--abbrev-ref", "HEAD").strip()
            == journey_branch_name(TASK_ID, build_id)
        )
        # Cut from the trunk, so the probe's ``main..HEAD`` range is real.
        assert _git(expected, "rev-parse", "HEAD").strip() == _git(
            checkout, "rev-parse", JOURNEY_BASE_REF
        ).strip()
        # Invariant 1: RECORDED — the column three consumers refuse on.
        row = pool.get_build_row(build_id)
        assert row is not None and row.worktree_path == str(expected)
        # Invariant 2: absolute, and inside the declared allowlist.
        assert Path(row.worktree_path).is_absolute()

    @pytest.mark.asyncio
    async def test_the_recorded_path_satisfies_the_dispatchers_pre_spawn_check(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """The refusal this whole lane exists to remove.

        ``conductor_subprocess`` refuses to dispatch a stage when the row
        has no worktree path ("refusing rather than run the build system
        from an inferred directory"). That check is exactly
        "non-empty ``worktree_path`` naming a real directory".
        """
        build_id = _queue_mode_c(pool, "FEAT-WTW2")

        assert isinstance(
            await prepare_journey_worktree(pool, _config(checkout), build_id),
            WorktreeReady,
        )

        row = pool.get_build_row(build_id)
        assert row is not None
        assert row.worktree_path
        assert Path(row.worktree_path).is_dir()

    @pytest.mark.asyncio
    async def test_the_commit_probe_counts_a_leg_commit_in_the_new_tree(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """The journey's ONLY commit evidence, end to end.

        ``git rev-list --count main..HEAD`` in the materialised worktree —
        zero before a leg commits, one after. This is the split between
        "hand back a gates-green branch" and "the journey changed nothing".
        """
        from forge.lifecycle.persistence import Build

        build_id = _queue_mode_c(pool, "FEAT-WTW3")
        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )
        assert isinstance(outcome, WorktreeReady)
        worktree = Path(outcome.path)

        probe = make_mode_c_commit_probe(pool)
        build = Build(build_id=build_id, status=BuildState.RUNNING)

        before = await probe(build)
        assert before.failed is False and before.count == 0

        (worktree / "fix.txt").write_text("the leg's work\n", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", "leg: a fix")

        after = await probe(build)
        assert after.failed is False, after
        assert after.count == 1


# --------------------------------------------------------------------------- #
# The embedded-gitlink hazard — closed by the writer, in the writer's own dir.
# --------------------------------------------------------------------------- #


class TestTheGitignoreGuard:
    @pytest.mark.asyncio
    async def test_a_repo_root_git_add_dash_a_stages_nothing_from_dot_forge(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """THE hazard, proven: a live nested worktree is an embedded gitlink.

        Without the guard, ``git add -A`` at the checkout root stages the
        journey's private tree into somebody's commit. The writer plants
        ``.forge/.gitignore`` containing ``*`` in the directory it creates,
        so the cure travels with the code rather than needing an edit in
        each target repository.
        """
        build_id = _queue_mode_c(pool, "FEAT-WTW4")
        assert isinstance(
            await prepare_journey_worktree(pool, _config(checkout), build_id),
            WorktreeReady,
        )

        guard = checkout / ".forge" / ".gitignore"
        assert guard.is_file()
        assert "*" in guard.read_text(encoding="utf-8").split()

        (checkout / "unrelated.txt").write_text("work\n", encoding="utf-8")
        _git(checkout, "add", "-A")
        staged = _git(checkout, "diff", "--cached", "--name-only").split()

        assert staged == ["unrelated.txt"], staged
        assert not any(name.startswith(".forge") for name in staged)

    @pytest.mark.asyncio
    async def test_an_existing_guard_that_ignores_nothing_is_a_refusal(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """Never silently overwrite the operator's file — and never proceed."""
        build_id = _queue_mode_c(pool, "FEAT-WTW5")
        (checkout / ".forge").mkdir()
        (checkout / ".forge" / ".gitignore").write_text(
            "# operator's own notes\nsomething-else\n", encoding="utf-8"
        )

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "embedded gitlink" in outcome.reason
        assert not (checkout / ".forge" / "worktrees").exists()


# --------------------------------------------------------------------------- #
# The reuse arm and its collisions.
# --------------------------------------------------------------------------- #


class TestReuseAndCollisions:
    @pytest.mark.asyncio
    async def test_a_redelivery_reuses_its_own_tree_rather_than_failing(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """The redelivery arms must not fail on their own earlier work."""
        build_id = _queue_mode_c(pool, "FEAT-WTW6")
        config = _config(checkout)

        first = await prepare_journey_worktree(pool, config, build_id)
        assert isinstance(first, WorktreeReady) and first.reused is False

        # A leg's work already lives there; reuse must not disturb it.
        (Path(first.path) / "in-progress.txt").write_text("x\n", encoding="utf-8")

        second = await prepare_journey_worktree(pool, config, build_id)

        assert isinstance(second, WorktreeReady), second
        assert second.reused is True
        assert second.path == first.path
        assert second.branch == first.branch
        assert (Path(first.path) / "in-progress.txt").is_file()
        # Still exactly one registration for this build.
        listing = _git(checkout, "worktree", "list", "--porcelain")
        assert listing.count(f"worktree {first.path}\n") == 1

    @pytest.mark.asyncio
    async def test_the_path_taken_on_another_branch_refuses_loudly(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """ANY other collision refuses — never reuse somebody else's tree."""
        build_id = _queue_mode_c(pool, "FEAT-WTW7")
        target = checkout / ".forge" / "worktrees" / build_id
        _git(checkout, "worktree", "add", "-b", "someone-else", str(target), "main")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "someone-else" in outcome.reason
        assert "refusing" in outcome.reason.lower()

    @pytest.mark.asyncio
    async def test_the_branch_busy_at_another_path_refuses_loudly(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTW8")
        branch = journey_branch_name(TASK_ID, build_id)
        elsewhere = checkout / ".forge" / "worktrees" / "somewhere-else"
        _git(checkout, "worktree", "add", "-b", branch, str(elsewhere), "main")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert branch in outcome.reason
        assert str(elsewhere) in outcome.reason

    @pytest.mark.asyncio
    async def test_a_second_journey_for_the_same_task_does_not_collide(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """Why the branch carries a per-build suffix at all.

        git refuses a branch another worktree already has checked out, so
        a bare ``fix/<task_id>`` would make the SECOND journey for a task
        impossible forever.
        """
        config = _config(checkout)
        first_id = _queue_mode_c(
            pool,
            "FEAT-WTW9",
            queued_at=datetime(2026, 8, 3, 14, 25, 30, tzinfo=UTC),
        )
        second_id = _queue_mode_c(
            pool,
            "FEAT-WTWA",
            queued_at=datetime(2026, 8, 3, 16, 40, 10, tzinfo=UTC),
        )
        assert first_id != second_id

        first = await prepare_journey_worktree(pool, config, first_id)
        second = await prepare_journey_worktree(pool, config, second_id)

        assert isinstance(first, WorktreeReady), first
        assert isinstance(second, WorktreeReady), second
        assert first.branch != second.branch
        assert first.path != second.path
        assert Path(first.path).is_dir() and Path(second.path).is_dir()


# --------------------------------------------------------------------------- #
# Refusals: every one loud, none a raise.
# --------------------------------------------------------------------------- #


class TestRefusals:
    @pytest.mark.asyncio
    async def test_the_allowlist_trap_refuses_before_anything_lands(
        self, pool: SqliteLifecyclePersistence, checkout: Path, tmp_path: Path
    ) -> None:
        """Invariant 2, checked at WRITE time.

        The pinned trap: autobuild's ``/tmp/forge-autobuild-worktrees`` and
        ``prepare_worktree``'s designed ``/var/forge/builds`` are both
        OUTSIDE the live allowlist, so a tree made there is one the leg's
        own cwd check then refuses. Checking before materialising means no
        orphan directory is left behind by the discovery.
        """
        build_id = _queue_mode_c(pool, "FEAT-WTWB")
        elsewhere = tmp_path / "not-allowlisted"
        elsewhere.mkdir()

        outcome = await prepare_journey_worktree(
            pool, _config(checkout, allowlist=[elsewhere]), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "allowlist" in outcome.reason
        assert not (checkout / ".forge").exists()
        row = pool.get_build_row(build_id)
        assert row is not None and row.worktree_path is None

    @pytest.mark.asyncio
    async def test_an_unregistered_repo_refuses_and_names_the_registered_ones(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTWC", repo="somebody/unregistered")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "somebody/unregistered" in outcome.reason
        assert REPO_KEY in outcome.reason, "the refusal did not say what IS registered"

    @pytest.mark.asyncio
    async def test_a_mode_c_row_with_no_task_id_cannot_name_a_branch(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTWD", task_id=None)

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "task_id" in outcome.reason

    @pytest.mark.asyncio
    async def test_a_missing_row_refuses(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        outcome = await prepare_journey_worktree(
            pool, _config(checkout), "build-NOPE-20260803142530"
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "no builds row" in outcome.reason

    @pytest.mark.asyncio
    async def test_a_checkout_that_is_not_a_git_repo_refuses(
        self, pool: SqliteLifecyclePersistence, checkout: Path, tmp_path: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTWE")
        bare = tmp_path / "not-a-checkout"
        bare.mkdir()

        outcome = await prepare_journey_worktree(
            pool,
            _config(checkout, allowlist=[bare], repo_paths={REPO_KEY: str(bare)}),
            build_id,
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "not a git checkout" in outcome.reason

    @pytest.mark.asyncio
    async def test_a_materialise_failure_is_a_refusal_carrying_gits_diagnostic(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """Missing trunk = git exit 128. The writer says so; it never raises."""
        build_id = _queue_mode_c(pool, "FEAT-WTWF")
        # Rename the trunk out from under the writer so ``main`` is gone.
        _git(checkout, "branch", "-m", "main", "trunk")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "FAILED" in outcome.reason
        assert not (checkout / ".forge" / "worktrees" / build_id).exists()
        row = pool.get_build_row(build_id)
        assert row is not None and row.worktree_path is None

    @pytest.mark.asyncio
    async def test_a_listing_failure_refuses_rather_than_risking_a_collision(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        build_id = _queue_mode_c(pool, "FEAT-WTWG")

        async def _broken_execute(**_kwargs: Any) -> ExecuteResult:
            return ExecuteResult(exit_code=1, stdout="", stderr="git is unwell")

        outcome = await prepare_journey_worktree(
            pool, _config(checkout), build_id, execute=_broken_execute
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "git is unwell" in outcome.reason
        assert not (checkout / ".forge").exists()

    @pytest.mark.asyncio
    async def test_a_record_write_that_will_not_land_is_a_refusal(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """A path nobody recorded is a path the dispatch refuses anyway.

        Reporting success on an unrecorded tree would hand the journey a
        worktree the dispatcher then refuses pre-spawn — a failure two
        stages away from its cause.
        """
        build_id = _queue_mode_c(pool, "FEAT-WTWH")

        class _WriteRefusingPool:
            def __init__(self, real: Any) -> None:
                self._real = real

            def get_build_row(self, bid: str) -> Any:
                return self._real.get_build_row(bid)

            def record_worktree_path(self, bid: str, path: str) -> None:
                raise sqlite3.OperationalError("database is locked")

        outcome = await prepare_journey_worktree(
            _WriteRefusingPool(pool), _config(checkout), build_id
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "OperationalError" in outcome.reason
        assert "builds.worktree_path" in outcome.reason

    @pytest.mark.asyncio
    async def test_an_unreadable_pool_refuses_and_never_raises(
        self, checkout: Path
    ) -> None:
        class _Boom:
            def get_build_row(self, _bid: str) -> Any:
                raise RuntimeError("the pool is gone")

        outcome = await prepare_journey_worktree(
            _Boom(), _config(checkout), "build-X-20260803142530"
        )

        assert isinstance(outcome, WorktreeRefused)
        assert "RuntimeError" in outcome.reason


# --------------------------------------------------------------------------- #
# The narrow persistence write.
# --------------------------------------------------------------------------- #


class TestRecordWorktreePath:
    def test_it_writes_the_column_without_touching_status(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """``apply_transition``'s column set stays closed, on purpose."""
        build_id = _queue_mode_c(pool, "FEAT-WTWI")
        before = pool.get_build_row(build_id)
        assert before is not None and before.worktree_path is None

        pool.record_worktree_path(build_id, "/srv/forge/tree")

        after = pool.get_build_row(build_id)
        assert after is not None
        assert after.worktree_path == "/srv/forge/tree"
        assert after.status is before.status

    def test_a_blank_path_is_refused(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError, match="non-blank"):
            pool.record_worktree_path("build-x", "   ")

    def test_a_blank_build_id_is_refused(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        with pytest.raises(ValueError, match="build_id"):
            pool.record_worktree_path("", "/srv/forge/tree")

    def test_an_unknown_build_id_is_a_quiet_no_op(
        self, pool: SqliteLifecyclePersistence
    ) -> None:
        pool.record_worktree_path("build-never-existed", "/srv/forge/tree")


# --------------------------------------------------------------------------- #
# The router seam (activation design §1, ruled placement (c)).
# --------------------------------------------------------------------------- #


class TestTheRouterSeam:
    @pytest.mark.asyncio
    async def test_the_router_materialises_the_tree_before_it_spawns(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """The whole point: the DAEMON makes the tree, at the moment of need."""
        build_id = _queue_mode_c(pool, "FEAT-WTWJ")
        spawned: list[Any] = []
        seen_at_spawn: list[Any] = []

        def spawn(coro: Any) -> Any:
            # The row must already carry its worktree by the time the
            # journey is handed off — the dispatcher refuses otherwise.
            row = pool.get_build_row(build_id)
            seen_at_spawn.append(row.worktree_path if row else None)
            spawned.append(coro)
            coro.close()
            return None

        router = build_conductor_router(
            pool=pool,
            config=_config(checkout),
            supervisor_factory=lambda _bid: object(),
            spawn=spawn,
        )
        assert router is not None

        from forge.cli._conductor_outcome import TAKEN_RUNNING

        assert await router(build_id=build_id) is TAKEN_RUNNING
        assert len(spawned) == 1
        expected = str(checkout / ".forge" / "worktrees" / build_id)
        assert seen_at_spawn == [expected]
        assert Path(expected).is_dir()

    @pytest.mark.asyncio
    async def test_a_materialise_failure_is_taken_and_terminal_never_routine(
        self, pool: SqliteLifecyclePersistence, checkout: Path
    ) -> None:
        """The §3 vocabulary, on the §1 arm.

        DECLINED here would run a fix task through the routine autobuild
        launch — the silent downgrade the cap law's own comment forbids.
        """
        from forge.cli._conductor_outcome import DECLINED

        build_id = _queue_mode_c(pool, "FEAT-WTWK", repo="somebody/unregistered")

        router = build_conductor_router(
            pool=pool,
            config=_config(checkout),
            supervisor_factory=lambda _bid: pytest.fail(
                "a supervisor was built for a build with no worktree"
            ),
            spawn=lambda _coro: pytest.fail("a journey was spawned with no worktree"),
        )
        assert router is not None

        outcome = await router(build_id=build_id)

        assert isinstance(outcome, TakenTerminal), outcome
        assert outcome is not DECLINED
        assert "target_repo_paths" in outcome.reason
        row = pool.get_build_row(build_id)
        assert row is not None
        assert row.status is BuildState.FAILED
        assert row.error and "\n" not in row.error
        assert outcome.reason == row.error
