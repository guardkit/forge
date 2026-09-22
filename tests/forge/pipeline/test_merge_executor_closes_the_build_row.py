"""The merge press writes the build's ending, because only it knows it.

Observed on 2026-09-10 (build ``build-FEAT-39F6-20260910141815``): the press
merged the repair and promoted it — "publication-pending", merge commit
a1a4c51 — and the build's row in the ledger still said RUNNING hours later.
It said the same after every refusal that week: a dirty tree, a missing
branch, a main that had moved. Each one was cleared by hand with ``forge
cancel``, which writes CANCELLED into the ledger for a journey that had
merged or been honestly refused. The ledger is the estate's record of what
happened and it was recording a lie.

These tests drive the real executor against a REAL SQLite ledger in a
temporary directory and a REAL git repository, with guardkit and the deploy
stage faked at the same seams the executor's own suite fakes. They check the ending on the ``builds`` row:
COMPLETE for a merge that merged and was promoted, FAILED with the refusal's
own sentence for each of the four steps that can refuse, one ending when the
press runs twice, and no ending at all written over a row something else has
already closed.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.merge_executor import (
    MERGE_STEP_MERGE_TARGET_IDENTIFIER,
    execute_merge_deploy,
)

# The executor's own suite already owns the fakes at the two seams — guardkit
# and the deploy stage — and the words a green and a red check report. They
# are borrowed rather than copied so a change to either seam reaches both
# files at once. The fixtures below are this file's own, so nothing here can
# shadow that module's.
from tests.forge.pipeline.test_merge_executor import (
    RED_GATE,
    _deps,
    _FakeDeploy,
    _FakeGuardKit,
    _git,
    _stage_ids,
)

BUILD_ID = "build-FEAT-MX1-20260910141815"
FEATURE_ID = "FEAT-MX1"
REPO = "appmilla/api_test"
CORRELATION = "corr-mx-1"
MAIN_SHA = "a" * 40


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    """A real ledger in a temporary directory."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A real repository: main with one commit, the feature branch one ahead."""
    root = tmp_path / "api_test"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    (root / "README.md").write_text("first\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "checkout", "-q", "-b", f"autobuild/{FEATURE_ID}", "main")
    (root / f"{FEATURE_ID}.txt").write_text("the feature\n", encoding="utf-8")
    _git(root, "add", f"{FEATURE_ID}.txt")
    _git(root, "commit", "-q", "-m", "the feature")
    _git(root, "checkout", "-q", "main")
    # The merge word joins onto the branch of the remote the work was recorded
    # against, so the repository needs one: a bare repository on disk, which
    # is real git and nobody's account.
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", "-q", str(bare)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    return root


@pytest.fixture
def config(repo_root: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO: str(repo_root)}},
            "approval": {"expected_approver": "rich"},
            "merge_executor": {"enabled": True},
        }
    )


@pytest.fixture(autouse=True)
def receipts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "receipts"
    monkeypatch.setenv("FORGE_RECEIPTS_DIR", str(root))
    return root


# ---------------------------------------------------------------------------
# Helpers — a build row in the state the press really finds it in
# ---------------------------------------------------------------------------

#: What a fix journey's row says when the merge word arrives: the conductor
#: leaves the row alone for the card path on purpose, so it is still RUNNING.
RUNNING_ROW: str = "RUNNING"


def _seed_build(
    pool: SqliteLifecyclePersistence,
    *,
    status: str = RUNNING_ROW,
    build_id: str = BUILD_ID,
    feature_id: str = FEATURE_ID,
    mode: str = "mode-c",
    error: str | None = None,
) -> None:
    """One build row in the state named, with nothing else written on it."""
    pool.connection.execute(
        "INSERT OR REPLACE INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "mode, error, start_commit, target_branch) VALUES (?, ?, ?, ?, "
        "'f.yaml', ?, 'cli', ?, '2026-09-10T12:00:00Z', ?, ?, ?, 'main')",
        (
            build_id,
            feature_id,
            REPO,
            f"autobuild/{feature_id}",
            status,
            CORRELATION,
            mode,
            error,
            "0" * 40,
        ),
    )
    pool.connection.commit()


def _row(pool: SqliteLifecyclePersistence, build_id: str = BUILD_ID) -> Any:
    return pool.get_build_row(build_id)


async def _press(
    deps: Any,
    repo_root: Path,
    *,
    expect_main_sha: str = MAIN_SHA,
    dry_run: bool = False,
) -> Any:
    """Run the press over an already-seeded row (never seeds one itself)."""
    return await execute_merge_deploy(
        deps=deps,
        build_id=BUILD_ID,
        feature_id=FEATURE_ID,
        repo=REPO,
        repo_root=repo_root,
        expect_main_sha=expect_main_sha,
        correlation_id=CORRELATION,
        decided_by="rich",
        dry_run=dry_run,
    )


def _main_moves(repo_root: Path) -> str:
    """Another feature lands on main while this one was building."""
    _git(repo_root, "checkout", "-q", "main")
    (repo_root / "moved.txt").write_text("another landed\n", encoding="utf-8")
    _git(repo_root, "add", "moved.txt")
    _git(repo_root, "commit", "-q", "-m", "main moved during the build")
    return _git(repo_root, "rev-parse", "main")


# ---------------------------------------------------------------------------
# A press that merged and promoted
# ---------------------------------------------------------------------------


class TestAMergeThatMergedAndPromoted:
    @pytest.mark.asyncio
    async def test_the_row_is_closed_complete(self, config, pool, repo_root) -> None:
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        row = _row(pool)
        assert row.status is BuildState.COMPLETE
        assert row.completed_at is not None

    @pytest.mark.asyncio
    async def test_a_good_ending_writes_no_failure_text(
        self, config, pool, repo_root
    ) -> None:
        """``forge status`` renders ``builds.error`` as the failure text, so
        prose on a row that succeeded reads as a failure to every human and
        every dashboard."""
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        await _press(deps, repo_root)

        assert _row(pool).error is None


# ---------------------------------------------------------------------------
# The four refusals — each closes the row with its own sentence
# ---------------------------------------------------------------------------


class TestEveryRefusalClosesTheRowWithItsOwnSentence:
    """The reason on the row is the sentence the report and the card carry —
    not a word of this seam's own invention."""

    @pytest.mark.asyncio
    async def test_a_red_candidate_check(self, config, pool, repo_root) -> None:
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(
            config,
            pool,
            deploy=_FakeDeploy(candidate_outcome="failed", gate=dict(RED_GATE)),
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "candidate-refused"
        row = _row(pool)
        assert row.status is BuildState.FAILED
        assert row.error == outcome.detail
        assert row.error == publisher.reports[0].detail
        assert "users_count" in row.error

    @pytest.mark.asyncio
    async def test_a_branch_that_is_not_there(
        self, config, pool, repo_root
    ) -> None:
        _git(repo_root, "branch", "-D", f"autobuild/{FEATURE_ID}")
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "candidate-refused"
        row = _row(pool)
        assert row.status is BuildState.FAILED
        assert row.error == outcome.detail
        assert f"the branch autobuild/{FEATURE_ID} was not found" in row.error

    @pytest.mark.asyncio
    async def test_a_dirty_tree(self, config, pool, repo_root) -> None:
        sentence = (
            "the working tree at /srv/api_test has uncommitted changes; "
            "the merge was refused"
        )
        gk = _FakeGuardKit(
            status="failed",
            report={"outcome": "refused", "refusal_reason": sentence},
        )
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "merge-refused"
        row = _row(pool)
        assert row.status is BuildState.FAILED
        assert row.error == sentence

    @pytest.mark.asyncio
    async def test_a_main_that_moved_is_joined_onto_and_the_row_is_closed(
        self, config, pool, repo_root
    ) -> None:
        """A remote that moved is no longer a refusal: it is what is joined onto."""
        pin = _main_moves(repo_root)
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        outcome = await _press(deps, repo_root, expect_main_sha=pin)

        assert outcome.result == "publication-pending"
        row = _row(pool)
        assert row.status is BuildState.COMPLETE
        assert MERGE_STEP_MERGE_TARGET_IDENTIFIER in _stage_ids(pool, BUILD_ID)

    @pytest.mark.skip(
        reason=(
            "the publisher and the executor are the next two stages: while "
            "publication is switched off the press stops at \"checked and ready "
            "to publish\", so nothing is sent and nothing is deployed, and this "
            "test drives the deploy half. It comes back with that stage."
        )
    )
    @pytest.mark.asyncio
    async def test_a_merge_that_landed_and_then_went_red_is_also_closed(
        self, config, pool, repo_root
    ) -> None:
        """The merge landed and the live checks failed: the journey did not
        deliver, so the row says FAILED and carries the same words."""
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(
            config, pool, deploy=_FakeDeploy(outcome="reverted", verdict="fail")
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "merged-deploy-reverted"
        row = _row(pool)
        assert row.status is BuildState.FAILED
        assert row.error == outcome.detail

    @pytest.mark.asyncio
    async def test_the_reason_is_one_line_however_long_the_words_are(
        self, config, pool, repo_root
    ) -> None:
        """``builds.error`` is one line in a table cell. A refusal sentence is
        one sentence, but an advisory warning line can ride behind it."""
        gk = _FakeGuardKit(
            status="failed",
            report={
                "outcome": "refused",
                "refusal_reason": "the merge was refused\nbecause the tree is dirty",
            },
        )
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool, guardkit=gk)

        await _press(deps, repo_root)

        error = _row(pool).error
        assert "\n" not in error
        assert error == "the merge was refused because the tree is dirty"


# ---------------------------------------------------------------------------
# One ending, whatever happens twice
# ---------------------------------------------------------------------------


class TestOneEndingOnly:
    @pytest.mark.asyncio
    async def test_the_same_press_run_twice_leaves_one_ending(
        self, config, pool, repo_root
    ) -> None:
        """The second run picks the join up, and the ending the first run
        wrote stands: the row is not re-opened and not re-closed."""
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        first = await _press(deps, repo_root)
        assert first.result == "publication-pending"
        closed_at = _row(pool).completed_at

        second = await _press(deps, repo_root)

        assert second.result == "publication-pending"
        assert second.merged_sha == first.merged_sha
        row = _row(pool)
        assert row.status is BuildState.COMPLETE
        assert row.completed_at == closed_at
        assert row.error is None

    @pytest.mark.asyncio
    async def test_a_row_forge_cancel_already_closed_is_left_alone(
        self, config, pool, repo_root
    ) -> None:
        """What the estate has been doing by hand: a person ran ``forge
        cancel`` first. The press does not fight that row."""
        _seed_build(pool, status="CANCELLED", error="cancelled by the orchestrator")
        deps, publisher, gk, dp = _deps(config, pool)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        row = _row(pool)
        assert row.status is BuildState.CANCELLED
        assert row.error == "cancelled by the orchestrator"

    @pytest.mark.asyncio
    async def test_a_row_already_failed_keeps_its_own_reason(
        self, config, pool, repo_root
    ) -> None:
        _seed_build(pool, status="FAILED", error="the build ran out of turns")
        deps, publisher, gk, dp = _deps(
            config,
            pool,
            deploy=_FakeDeploy(candidate_outcome="failed", gate=dict(RED_GATE)),
        )

        outcome = await _press(deps, repo_root)

        assert outcome.result == "candidate-refused"
        row = _row(pool)
        assert row.status is BuildState.FAILED
        assert row.error == "the build ran out of turns"


# ---------------------------------------------------------------------------
# A routine feature build, and a dry run
# ---------------------------------------------------------------------------


class TestARoutineFeatureBuildIsUnchanged:
    """The honest answer to "does this change a routine feature build".

    It does not. The live build feed closes a routine build's row COMPLETE
    when the build finishes, and the merge card is offered only after that
    write lands — so the press finds a terminal row and leaves it exactly as
    it is. The executor's whole existing suite runs unchanged against rows
    seeded COMPLETE, which is the same proof from the other side.
    """

    @pytest.mark.asyncio
    async def test_the_row_the_build_feed_closed_is_untouched(
        self, config, pool, repo_root
    ) -> None:
        _seed_build(pool, status="COMPLETE", mode="mode-a")
        # What the build feed wrote when the build finished, an hour before
        # the card was answered.
        pool.connection.execute(
            "UPDATE builds SET completed_at = '2026-09-10T13:00:00+00:00' "
            "WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()
        before = _row(pool)
        assert before.completed_at is not None
        deps, publisher, gk, dp = _deps(config, pool)

        outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        after = _row(pool)
        assert after.status is BuildState.COMPLETE
        assert after.completed_at == before.completed_at
        assert after.error is None

    @pytest.mark.asyncio
    async def test_a_feature_build_that_was_never_closed_now_is(
        self, config, pool, repo_root
    ) -> None:
        """And when nothing closed it — no build feed this boot — the press
        does, rather than leaving the ledger saying RUNNING for ever."""
        _seed_build(pool, status=RUNNING_ROW, mode="mode-a")
        deps, publisher, gk, dp = _deps(config, pool)

        await _press(deps, repo_root)

        assert _row(pool).status is BuildState.COMPLETE


class TestADryRunWritesNoEnding:
    @pytest.mark.asyncio
    async def test_the_row_is_left_running(self, config, pool, repo_root) -> None:
        """A dry run leaves no durable rows on purpose — the ending is one."""
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)

        await _press(deps, repo_root, dry_run=True)

        assert _row(pool).status is BuildState.RUNNING


# ---------------------------------------------------------------------------
# The ending never costs the report
# ---------------------------------------------------------------------------


class _PoolThatCannotWriteTheEnding:
    """The real pool, except that the transition write raises."""

    def __init__(self, pool: SqliteLifecyclePersistence) -> None:
        self._pool = pool
        self.attempts = 0

    def apply_transition(self, transition: Any) -> None:
        self.attempts += 1
        raise sqlite3.OperationalError("database is locked")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


class TestAnEndingThatCannotBeWritten:
    @pytest.mark.asyncio
    async def test_never_costs_the_report_or_the_outcome(
        self, config, pool, repo_root, receipts_dir: Path, caplog
    ) -> None:
        _seed_build(pool)
        deps, publisher, gk, dp = _deps(config, pool)
        blocked = _PoolThatCannotWriteTheEnding(pool)
        deps.pool = blocked  # type: ignore[assignment]

        with caplog.at_level("ERROR"):
            outcome = await _press(deps, repo_root)

        assert outcome.result == "publication-pending"
        assert publisher.reports[0].result == "publication-pending"
        assert (
            receipts_dir / f"merge-{BUILD_ID}" / "merge_deploy_report.json"
        ).is_file()
        assert blocked.attempts >= 1
        # The row is untouched, and the log says plainly that it needs a hand
        # rather than leaving the failure silent.
        assert _row(pool).status is BuildState.RUNNING
        assert any("needs a hand" in r.getMessage() for r in caplog.records)
