"""The specification fence, reading a real branch in a real git repository.

Rich's ruling, 2026-09-09. The rules themselves are pinned beside the
checkpoint (``tests/forge/pipeline/test_specification_fence.py``); this file
pins the READING — where the branch's own changes come from, where the
repository says which of its files are the specification, and that both
answers are the same whether the repository has a sandbox or not.

Real code paths throughout: real temporary git repositories with real
commits, real ``git diff`` runs, and — for the sandbox half — the REAL deploy
sidecar on an ephemeral loopback port answering the real route. Nothing live
is touched: no sandbox, no docker, no service, every path under ``tmp_path``.

What is pinned:

* the incident itself — a branch that renames the acceptance twin is refused
  and the sentence names the old name and the new one;
* a branch that edits a twin's body is refused;
* a branch that rewrites an ``APPROVED … by <name>`` line in a file that is
  not a twin at all is refused, and says which file and whose approval;
* a branch that touches neither is CLEAR, and its card is published;
* a repository that declares its own specification is honoured, and one that
  declares none gets the default;
* the declaration is read from the CANONICAL tree, so a branch cannot free
  itself by deleting it;
* the sandbox form: the same answers, through the real sidecar, and the
  request the sidecar is sent is the one the route's laws are written for;
* the refusal reaches the journey's own history (the receipts the planner and
  the close-out read) by the same red-gate route every other red gate takes.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_conductor as conductor
from forge.cli._serve_deps_stage_log import (
    CHECKPOINT_TARGET_IDENTIFIER,
    build_fix_journey_stage_log_writer,
)
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_ready_checkpoint import (
    MergeCardOutcome,
    SpecificationFenceStatus,
)

REPO_WITH = "guardkit/api_test"
REPO_WITHOUT = "guardkit/plain"
BUILD_ID = "build-FEAT-39F6-20260909195749"
JOURNEY_BRANCH = "fix/TASK-FEAT39F6FIX1-20260909"
TWIN = "qa/twins/users-delete-by-email/double-delete-honest-404.hurl"
RENAMED_TWIN = "qa/twins/users-delete-by-email/double-delete-honest-410.hurl"
THE_RULING = (
    "# APPROVED AS PROPOSED by Rich 2026-07-28 (interactive sit; all 4 "
    "assumptions confirmed, ASSUM-003 = 404 honest absence)"
)

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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — scratch fixture, list tokens, no shell
        ["git", *args],
        cwd=cwd,
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """The factory's own clone: one acceptance twin carrying the owner's
    ruling, and a decision document carrying his approval too."""
    path = tmp_path / "api_test"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _write(
        path / TWIN,
        THE_RULING + "\nDELETE http://localhost/users?email=a@b\nHTTP 204\n",
    )
    _write(path / "docs" / "decisions" / "delete.md", THE_RULING + "\n")
    _write(path / "src" / "crud.py", "def delete():\n    ...\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path.resolve()


@pytest.fixture
def worktree(clone: Path) -> Path:
    """The fix journey's own tree, where its branch is."""
    path = clone / ".forge" / "worktrees" / BUILD_ID
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(clone, "worktree", "add", "-b", JOURNEY_BRANCH, str(path), "main")
    return path.resolve()


@pytest.fixture
def pool(tmp_path: Path, worktree: Path) -> SqliteLifecyclePersistence:
    """A real ledger with the journey's row: queued on ``main``, so ``main``
    is what its branch is measured against."""
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "worktree_path, mode, task_id, profile) VALUES (?, 'FEAT-39F6', ?, "
        "'main', 'f.yaml', 'RUNNING', 'cli', 'corr-39f6', "
        "'2026-09-09T19:57:49+00:00', ?, 'mode-c', 'TASK-FEAT39F6FIX1', "
        "'fix-journey')",
        (BUILD_ID, REPO_WITH, str(worktree)),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _config(clone: Path, *, sidecar_url: str | None = None) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {REPO_WITH: str(clone), REPO_WITHOUT: str(clone)}
    }
    if sidecar_url is not None:
        planning["sandboxes"] = {
            REPO_WITH: {
                "name": "api-test-factory",
                "sidecar_url": sidecar_url,
                "runner_url": "http://127.0.0.1:8924",
            }
        }
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": [str(clone.parent)]}},
            "planning": planning,
        }
    )


def _commit(worktree: Path, message: str) -> None:
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)


def _fence(pool: Any, clone: Path, **kwargs: Any) -> Any:
    return conductor.make_specification_fence(
        pool=pool, config=_config(clone), **kwargs
    )


# ---------------------------------------------------------------------------
# The branch's own changes, read in this container
# ---------------------------------------------------------------------------


class TestReadingTheBranchWhereItIs:
    def test_the_incident_itself_a_renamed_twin_is_refused(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.REFUSED
        assert TWIN in report.detail
        assert RENAMED_TWIN in report.detail
        assert "renamed to" in report.detail
        assert "a specification change is the owner's to make" in report.detail
        assert "no card was published" in report.detail

    def test_a_twins_body_being_edited_is_refused(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(
            worktree / TWIN,
            THE_RULING + "\nDELETE http://localhost/users?email=a@b\nHTTP 410\n",
        )
        _commit(worktree, "410 not 204")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.REFUSED
        assert f"{TWIN} (changed)" in report.detail

    def test_an_approval_line_edited_in_any_file_is_refused_and_says_which(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """Not a twin, not under any declared path — the rule reads the
        CHANGE, and this change puts new words under his name."""
        _write(
            worktree / "docs" / "decisions" / "delete.md",
            THE_RULING.replace("404 honest absence", "410 Gone for soft-deleted")
            + "\n",
        )
        _commit(worktree, "tidy the decision")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.REFUSED
        assert "docs/decisions/delete.md" in report.detail
        assert "Rich's approval" in report.detail
        assert report.specification_files == ()

    def test_a_branch_that_touches_neither_is_clear(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(worktree / "src" / "crud.py", "def delete():\n    return None\n")
        _commit(worktree, "the fix")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.CLEAR
        assert report.refuses is False

    def test_a_branch_with_no_commits_is_clear(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.CLEAR

    def test_a_worktree_that_is_not_there_is_a_refusal_never_a_pass(
        self, pool: Any, clone: Path, worktree: Path, tmp_path: Path
    ) -> None:
        pool.connection.execute(
            "UPDATE builds SET worktree_path = '' WHERE build_id = ?", (BUILD_ID,)
        )
        pool.connection.commit()

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert report.refuses is True
        assert "no recorded worktree_path" in report.detail

    def test_a_worktree_directory_that_is_gone_is_a_refusal_never_a_pass(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """The branch is still in the clone carrying every one of its commits;
        only the tree it was written in has gone. "There is nothing to read
        here" is not the same statement as "this branch changed nothing", and
        only the second one may lead to a card."""
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")
        shutil.rmtree(worktree)

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert report.refuses is True
        assert "there is nothing at" in report.detail

    def test_a_path_that_is_not_a_git_tree_is_a_refusal_never_a_pass(
        self, pool: Any, clone: Path, worktree: Path, tmp_path: Path
    ) -> None:
        """A plain directory where the journey's tree should be: the reading
        did not happen, so the fence refuses like every other reading that
        did not happen."""
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")
        shutil.rmtree(worktree)
        worktree.mkdir(parents=True)

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert report.refuses is True
        assert "is not the root of a git tree" in report.detail

    def test_the_reader_itself_names_both_of_those(self, tmp_path: Path) -> None:
        gone = tmp_path / "nothing-here"
        plain = tmp_path / "not-a-repo"
        plain.mkdir()

        _, _, missing_error = conductor.read_branch_changes(worktree=gone, base="main")
        _, _, plain_error = conductor.read_branch_changes(worktree=plain, base="main")

        assert missing_error and "there is nothing at" in missing_error
        assert plain_error and "is not the root of a git tree" in plain_error

    def test_git_failing_to_answer_is_a_refusal_never_a_pass(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """A base nobody made: not "nothing changed", which is what a quiet
        empty answer would be read as."""
        pool.connection.execute(
            "UPDATE builds SET branch = 'repair/nobody-made-this' "
            "WHERE build_id = ?",
            (BUILD_ID,),
        )
        pool.connection.commit()

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert "could not be read" in report.detail


# ---------------------------------------------------------------------------
# Where a repository says which of its files are the specification
# ---------------------------------------------------------------------------


class TestTheRepositorySaysWhatItsSpecificationIs:
    def test_a_repository_that_declares_none_gets_the_default(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        assert conductor.load_declared_specification_paths(clone) is None

        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")

        assert (
            _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH).status
            is SpecificationFenceStatus.REFUSED
        )

    def test_a_repository_declares_its_own_beside_its_toolchain(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """The declaration lives where the repository already declares things
        about its gates — beside ``toolchain:`` in ``.guardkit/config.yaml``."""
        _write(
            clone / ".guardkit" / "config.yaml",
            'toolchain:\n  test: "qa/run-suite.sh"\n'
            "specification:\n  paths:\n    - \"contracts/**\"\n",
        )
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "declare the specification")

        assert conductor.load_declared_specification_paths(clone) == ("contracts/**",)

        # The twins are no longer what this repository calls its specification.
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")
        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)
        assert report.specification_files == ()

        # What it does call its specification is fenced.
        _write(worktree / "contracts" / "delete.yaml", "gone: 410\n")
        _commit(worktree, "rewrite the contract")
        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)
        assert report.status is SpecificationFenceStatus.REFUSED
        assert "contracts/delete.yaml (added)" in report.detail

    def test_the_declaration_is_read_from_the_canonical_tree_not_the_branch(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """A branch that deletes the declaration cannot free itself: the file
        is read from ``main`` in the clone, exactly as the toolchain is."""
        _write(
            clone / ".guardkit" / "config.yaml",
            "specification:\n  paths:\n    - \"qa/twins/**\"\n",
        )
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "declare the specification")

        _write(worktree / ".guardkit" / "config.yaml", "specification:\n  paths: []\n")
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "free myself and update the twin")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.REFUSED
        assert TWIN in report.detail

    def test_a_file_with_no_such_key_declares_nothing(self, clone: Path) -> None:
        _write(clone / ".guardkit" / "config.yaml", 'toolchain:\n  test: "true"\n')

        assert conductor.load_declared_specification_paths(clone) is None

    def test_no_file_at_all_declares_nothing(self, clone: Path) -> None:
        assert not (clone / ".guardkit" / "config.yaml").exists()

        assert conductor.load_declared_specification_paths(clone) is None


class TestADeclarationNobodyCanHearIsNotADeclarationOfNothing:
    """Saying nothing and saying something nobody can hear are different
    things. The first takes the default; the second refuses, because falling
    back to the default would fence the DEFAULT paths in place of the ones
    this repository meant to name — less protection, not more."""

    def test_a_declaration_that_will_not_parse_is_unreadable(
        self, clone: Path
    ) -> None:
        _write(clone / ".guardkit" / "config.yaml", "specification: [: not yaml\n")

        answer = conductor.load_declared_specification_paths(clone)

        assert isinstance(answer, conductor.UnreadableDeclaration)
        assert "could not be parsed" in answer.reason

    def test_a_specification_block_with_no_paths_is_unreadable(
        self, clone: Path
    ) -> None:
        _write(clone / ".guardkit" / "config.yaml", "specification:\n  files: []\n")

        answer = conductor.load_declared_specification_paths(clone)

        assert isinstance(answer, conductor.UnreadableDeclaration)
        assert "no paths: list" in answer.reason

    def test_a_file_that_is_there_and_will_not_open_is_unreadable(
        self, clone: Path
    ) -> None:
        """A directory where the file should be: it IS there, and it cannot
        be read."""
        (clone / ".guardkit" / "config.yaml").mkdir(parents=True)

        answer = conductor.load_declared_specification_paths(clone)

        assert isinstance(answer, conductor.UnreadableDeclaration)
        assert "could not be read" in answer.reason

    def test_the_fence_refuses_rather_than_falling_back_to_the_default(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """The whole point: this repository's specification is
        ``contracts/**``, NOT the twins. Read as "declares nothing" the
        default would stand and a branch rewriting a contract would come back
        CLEAR."""
        _write(worktree / "contracts" / "delete.yaml", "gone: 410\n")
        _commit(worktree, "rewrite the contract")

        def _raises(_repo_root: Any) -> Any:
            raise OSError("the declaration could not be opened")

        report = _fence(pool, clone, declaration_loader=_raises)(
            build_id=BUILD_ID, branch=JOURNEY_BRANCH
        )

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert report.refuses is True
        assert "which files this repository calls its specification" in report.detail

    def test_an_unparseable_declaration_stops_the_card_through_the_real_loader(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(clone / ".guardkit" / "config.yaml", "specification: [: not yaml\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "a declaration nobody can parse")
        _write(worktree / "src" / "crud.py", "def delete():\n    return None\n")
        _commit(worktree, "the fix")

        report = _fence(pool, clone)(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert "could not be parsed" in report.detail


# ---------------------------------------------------------------------------
# The same fence for a repository whose factory lives in its sandbox (rule 88)
# ---------------------------------------------------------------------------


@pytest.fixture
def sidecar(clone: Path):
    """The REAL sidecar service on an ephemeral loopback port."""
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    url = f"http://{host}:{port}"
    holder["config"] = _config(clone, sidecar_url=url)
    try:
        yield SimpleNamespace(url=url, config=holder["config"])
    finally:
        srv.shutdown()
        srv.server_close()


class TestTheSandboxFormIsTheSameFence:
    def test_a_renamed_twin_is_refused_through_the_real_sidecar(
        self, pool: Any, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")

        fence = conductor.make_specification_fence(
            pool=pool, config=sidecar.config
        )
        report = fence(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.REFUSED
        assert TWIN in report.detail and RENAMED_TWIN in report.detail

    def test_a_clean_branch_is_clear_through_the_real_sidecar(
        self, pool: Any, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _write(worktree / "src" / "crud.py", "def delete():\n    return None\n")
        _commit(worktree, "the fix")

        fence = conductor.make_specification_fence(
            pool=pool, config=sidecar.config
        )

        assert (
            fence(build_id=BUILD_ID, branch=JOURNEY_BRANCH).status
            is SpecificationFenceStatus.CLEAR
        )

    def test_the_request_the_sidecar_is_sent_names_the_tree_and_the_base(
        self, worktree: Path, sidecar: Any
    ) -> None:
        sent: list[tuple[str, dict[str, Any], float]] = []

        def _post(url: str, body: dict[str, Any], timeout: float):
            sent.append((url, dict(body), timeout))
            from forge.planning.sidecar_git_runner import _urllib_post

            return _urllib_post(url, body, timeout)

        names, patch, error = conductor.read_branch_changes_in_sandbox(
            worktree=worktree,
            base="main",
            sandbox=sidecar.config.planning.sandboxes[REPO_WITH],
            repo=REPO_WITH,
            post=_post,
        )

        assert error is None, error
        assert len(sent) == 1
        url, body, timeout = sent[0]
        assert url.endswith("/git/worktree-changed-files")
        assert body == {"repo": REPO_WITH, "path": str(worktree), "base": "main"}
        assert timeout > conductor.BRANCH_DIFF_TIMEOUT_SECONDS

    def test_an_unreachable_sidecar_is_a_refusal_never_a_pass(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        config = _config(clone, sidecar_url="http://127.0.0.1:9")
        fence = conductor.make_specification_fence(pool=pool, config=config)

        report = fence(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.UNREADABLE
        assert "could not be reached" in report.detail

    def test_the_declaration_comes_out_of_the_clone_over_the_sidecar(
        self, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _write(
            clone / ".guardkit" / "config.yaml",
            "specification:\n  paths:\n    - \"contracts/**\"\n",
        )
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "declare the specification")

        declared = conductor.load_declared_specification_paths_from_sandbox(
            clone,
            sandbox=sidecar.config.planning.sandboxes[REPO_WITH],
            repo=REPO_WITH,
        )

        assert declared == ("contracts/**",)

    def test_no_declaration_on_the_branch_declares_nothing(
        self, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        """The route answers ``content: null`` when the file is not there,
        and THAT is the one "declares nothing": the default applies."""
        declared = conductor.load_declared_specification_paths_from_sandbox(
            clone,
            sandbox=sidecar.config.planning.sandboxes[REPO_WITH],
            repo=REPO_WITH,
        )

        assert declared is None

    def test_a_sidecar_that_cannot_be_reached_is_unreadable_not_nothing(
        self, clone: Path
    ) -> None:
        config = _config(clone, sidecar_url="http://127.0.0.1:9")

        answer = conductor.load_declared_specification_paths_from_sandbox(
            clone,
            sandbox=config.planning.sandboxes[REPO_WITH],
            repo=REPO_WITH,
        )

        assert isinstance(answer, conductor.UnreadableDeclaration)
        assert "could not be reached" in answer.reason


# ---------------------------------------------------------------------------
# A repository with no sandbox behaves the same as one with a sandbox
# ---------------------------------------------------------------------------


class TestBothVenuesAgree:
    def test_the_same_branch_gets_the_same_answer_either_way(
        self, pool: Any, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")

        here = conductor.make_specification_fence(
            pool=pool, config=_config(clone)
        )(build_id=BUILD_ID, branch=JOURNEY_BRANCH)
        there = conductor.make_specification_fence(
            pool=pool, config=sidecar.config
        )(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert here == there


# ---------------------------------------------------------------------------
# The refusal reaches the journey's own history, by the red-gate route
# ---------------------------------------------------------------------------


class TestTheRefusalReachesTheReceipts:
    def test_the_checkpoint_writes_the_refusal_into_the_journeys_history(
        self, pool: Any, clone: Path, worktree: Path, tmp_path: Path
    ) -> None:
        _git(worktree, "mv", TWIN, RENAMED_TWIN)
        _commit(worktree, "update the twin")

        published: list[dict[str, Any]] = []
        checkpoint = conductor.make_merge_ready_checkpoint(
            pool=pool,
            publish_card=lambda **kw: published.append(kw) or "RESUMED",
            gates_green_reader=lambda **_: True,
            published_probe=lambda _bid: False,
            receipts_root=tmp_path / "receipts",
            stage_log_writer=build_fix_journey_stage_log_writer(pool),
            review_cycle_cap=None,
            specification_fence=_fence(pool, clone),
        )

        decision = asyncio.run(
            checkpoint.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-39F6",
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )

        assert decision.outcome is MergeCardOutcome.RED_GATE_LOOP_BACK
        assert published == []

        rows = [
            row
            for row in pool.read_stages(BUILD_ID)
            if getattr(row, "target_identifier", None) == CHECKPOINT_TARGET_IDENTIFIER
        ]
        assert len(rows) == 1
        assert rows[0].status == "FAILED"
        written = str(rows[0].details.get("rationale") or "")
        assert TWIN in written and RENAMED_TWIN in written
        assert "a specification change is the owner's to make" in written
        # The rule names are on the row too, so the close-out's own sentence
        # names the files when there is no review cycle left to loop into.
        gates = rows[0].details.get("failed_gates") or []
        assert any("specification" in gate for gate in gates)
        assert any("recorded approval" in gate for gate in gates)
