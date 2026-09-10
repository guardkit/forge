"""What the branch did to the tests, read off a REAL branch and put on the card.

Rich's ruling, 2026-09-10: do not fence ordinary unit tests — report on them.
The counting rules and the card's words are pinned beside the checkpoint
(``tests/forge/pipeline/test_the_tests_line_on_the_card.py``); this file pins
the reading and the whole way through, from a real commit to the sentence a
person reads in Slack.

Real code paths throughout: real temporary git repositories with real
commits, real ``git diff`` runs, the REAL deploy sidecar on an ephemeral
loopback port for the sandbox half, and the real merge-ready checkpoint
publishing through a card seam that keeps what it was handed. Nothing live is
touched: no sandbox, no docker, no service, every path under ``tmp_path``.

What is pinned:

* a branch that deletes a test function and removes assertions produces the
  counts, names the file, and the card invites the look;
* a branch that only adds tests says nothing alarming;
* a branch that touches no test at all still gets its card, with one short
  reassuring clause;
* the count is right when a test file is renamed;
* a repository with no sandbox and one with a sandbox take the same path and
  get the same numbers;
* which files a repository calls its tests is read from where that repository
  lives — through its own sandbox when it has one, on this side when it does
  not — so a repository that keeps its tests somewhere the plain default does
  not look still gets them counted;
* the fence's own verdict is untouched by any of it — a branch that deletes
  every test it has is still CLEAR, because this reports and never refuses.
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
from forge.cli import _serve_conductor as conductor
from forge.cli._serve_gate_activation import merge_card_words
from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import build_server
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.merge_ready_checkpoint import (
    MergeCardOutcome,
    MergeReadyCheckpointPublisher,
    SpecificationFenceStatus,
)

REPO = "guardkit/api_test"
BUILD_ID = "build-FEAT-39F6-20260910104500"
JOURNEY_BRANCH = "fix/TASK-FEAT39F6FIX1-10141815"
TESTS = "tests/users/test_router.py"
RENAMED_TESTS = "tests/users/test_lookup.py"
TWIN = "qa/twins/users-delete-by-email/double-delete-honest-404.hurl"

THE_TESTS = '''\
def test_lookup_by_email():
    response = client.get("/users?email=a@b")
    assert response.status_code == 200
    assert response.json()["email"] == "a@b"


def test_deleted_user_is_absent():
    response = client.get("/users?email=gone@b")
    assert response.status_code == 404
'''

THE_TESTS_WITH_LOSSES = '''\
def test_lookup_by_email():
    response = client.get("/users?email=a@b")
    assert response.status_code == 200
'''

#: The same repository's javascript tests, written the plain way: ``test(...)``
#: with a helper doing the checking, so not one line of the second block holds
#: the word "assert", the word "expect" or the ``it(`` form. A branch that
#: deletes that block has lost a test and the card must say so.
JS_TESTS = "tests/router.test.ts"
THE_JS_TESTS = '''\
import { lookup } from "../src/users/router";

test("finds the user", () => {
  checkUser(lookup("a@b"));
});

test("another", () => {
  checkUser(lookup("gone@b"));
});
'''

THE_JS_TESTS_WITH_A_LOSS = '''\
import { lookup } from "../src/users/router";

test("finds the user", () => {
  checkUser(lookup("a@b"));
});
'''

#: The same repository's OTHER tests, kept somewhere the plain default does
#: not look. Only the repository's own declared test command says these are
#: tests — and for a repository with a sandbox that declaration lives in the
#: sandbox, not on this side.
DECLARED_TESTS = "spec/checks_users.py"

THE_DECLARED_TESTS = '''\
def test_delete_is_honest():
    assert delete("a@b") == 204
'''

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
    """The factory's own clone: two tests, the code they cover, a check kept
    where only the repository's own declared command says to look, and an
    acceptance twin sitting where the specification fence protects it."""
    path = tmp_path / "api_test"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _write(path / TESTS, THE_TESTS)
    _write(path / "src" / "users" / "router.py", "def lookup(email):\n    ...\n")
    _write(path / JS_TESTS, THE_JS_TESTS)
    _write(path / DECLARED_TESTS, THE_DECLARED_TESTS)
    _write(path / TWIN, "DELETE http://localhost/users?email=a@b\nHTTP 204\n")
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
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "worktree_path, mode, task_id, profile) VALUES (?, 'FEAT-39F6', ?, "
        "'main', 'f.yaml', 'RUNNING', 'cli', 'corr-39f6', "
        "'2026-09-10T10:45:00+00:00', ?, 'mode-c', 'TASK-FEAT39F6FIX1', "
        "'fix-journey')",
        (BUILD_ID, REPO, str(worktree)),
    )
    cx.commit()
    return SqliteLifecyclePersistence(connection=cx)


def _config(clone: Path, *, sidecar_url: str | None = None) -> ForgeConfig:
    planning: dict[str, Any] = {"target_repo_paths": {REPO: str(clone)}}
    if sidecar_url is not None:
        planning["sandboxes"] = {
            REPO: {
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


def _counts(pool: Any, config: ForgeConfig) -> Any:
    report = conductor.make_specification_fence(pool=pool, config=config)(
        build_id=BUILD_ID, branch=JOURNEY_BRANCH
    )
    assert report.status is SpecificationFenceStatus.CLEAR, report.detail
    return report.test_changes


def _gates(report: Any) -> Any:
    """The gates report the card is written from, carrying today's count."""
    from forge.pipeline.merge_ready_checkpoint import GatesReport, GateStatus

    return GatesReport(
        status=GateStatus.GREEN,
        detail="the tests this repository declares came back green",
        test_changes=report.test_changes,
    )


@pytest.fixture
def sidecar(clone: Path):
    """The REAL sidecar service on an ephemeral loopback port."""
    holder: dict[str, ForgeConfig] = {}
    srv = build_server(port=0, config_loader=lambda: holder["config"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    holder["config"] = _config(clone, sidecar_url=f"http://{host}:{port}")
    try:
        yield SimpleNamespace(config=holder["config"])
    finally:
        srv.shutdown()
        srv.server_close()


class TestTheCountsComeOffARealBranch:
    def test_a_deleted_test_and_removed_assertions_are_counted_and_named(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 1
        assert counted.tests_deleted == 1
        assert counted.assertions_removed == 2
        assert counted.files == (TESTS,)
        assert counted.read_whole is True

    def test_a_branch_that_only_adds_tests_says_nothing_alarming(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(
            worktree / TESTS,
            THE_TESTS
            + "\n\ndef test_absent_user_is_404():\n    assert lookup('x') is None\n",
        )
        _commit(worktree, "cover the absent case")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 1
        assert counted.lost_something is False

    def test_a_branch_that_touches_no_test_counts_no_test_file(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(
            worktree / "src" / "users" / "router.py",
            "def lookup(email):\n    return None\n",
        )
        _commit(worktree, "the fix")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 0
        assert counted.lost_something is False

    def test_a_renamed_test_file_is_one_file_and_nothing_lost(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """git detects the rename, so the count says what really happened:
        one test file moved, and no test and no assertion went away."""
        _git(worktree, "mv", TESTS, RENAMED_TESTS)
        _commit(worktree, "rename the file")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 1
        assert counted.tests_deleted == 0
        assert counted.assertions_removed == 0

    def test_a_renamed_file_that_also_loses_an_assertion_counts_the_loss_once(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """Renamed AND changed: one file, and the assertion that really went
        away, named at the file's new name."""
        _git(worktree, "mv", TESTS, RENAMED_TESTS)
        _write(
            worktree / RENAMED_TESTS,
            THE_TESTS.replace("    assert response.status_code == 404\n", ""),
        )
        _commit(worktree, "rename and trim")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 1
        assert counted.tests_deleted == 0
        assert counted.assertions_removed == 1
        assert counted.files == (RENAMED_TESTS,)

    def test_a_javascript_test_with_no_assert_in_it_is_still_counted(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """The plain ``test("...")`` form, with a helper doing the checking.

        Nothing the branch removes here holds "assert", "expect" or ``it(``,
        so the words git filters the diff by are the only thing standing
        between this loss and a card that says "removed no tests" about a
        branch that removed one.
        """
        _write(worktree / JS_TESTS, THE_JS_TESTS_WITH_A_LOSS)
        _commit(worktree, "drop a javascript test")

        counted = _counts(pool, _config(clone))

        assert counted.files_changed == 1
        assert counted.tests_deleted == 1
        assert counted.files == (JS_TESTS,)
        assert counted.lost_something is True

    def test_deleting_tests_is_never_a_refusal(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """His ruling, made structural: a fence here would refuse honest
        work, so the branch is CLEAR and the card carries the sentence."""
        _write(worktree / TESTS, "")
        _commit(worktree, "empty the tests")

        report = conductor.make_specification_fence(
            pool=pool, config=_config(clone)
        )(build_id=BUILD_ID, branch=JOURNEY_BRANCH)

        assert report.status is SpecificationFenceStatus.CLEAR
        assert report.refuses is False
        assert report.test_changes.tests_deleted == 2


class TestTheSandboxFormCountsTheSame:
    def test_the_same_branch_gets_the_same_numbers_through_the_real_sidecar(
        self, pool: Any, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")

        here = _counts(pool, _config(clone))
        there = _counts(pool, sidecar.config)

        assert there == here
        assert there.tests_deleted == 1 and there.assertions_removed == 2

    def test_a_renamed_test_file_reads_the_same_in_both_venues(
        self, pool: Any, clone: Path, worktree: Path, sidecar: Any
    ) -> None:
        _git(worktree, "mv", TESTS, RENAMED_TESTS)
        _commit(worktree, "rename the file")

        assert _counts(pool, sidecar.config) == _counts(pool, _config(clone))

    @staticmethod
    def _declared_in_the_sandbox(
        monkeypatch: pytest.MonkeyPatch, command: str | None
    ) -> None:
        """This repository's declaration, readable only through its sandbox.

        guardkit is not importable in this interpreter, so the loader itself
        is the seam: what is pinned here is WHICH SIDE is asked, and that the
        answer reaches the count.
        """

        def _here(root: Any, **_: Any) -> Any:
            raise AssertionError("a sandboxed repository must not be read here")

        monkeypatch.setattr(conductor, "load_declared_toolchain", _here)
        monkeypatch.setattr(
            conductor,
            "load_declared_toolchain_from_sandbox",
            lambda root, *, sandbox, repo, **_: (
                SimpleNamespace(test=command) if command else None
            ),
        )

    def test_the_sandboxed_repositorys_own_declared_paths_reach_the_count(
        self,
        pool: Any,
        clone: Path,
        worktree: Path,
        sidecar: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole way, for a repository that keeps tests where the plain
        default does not look: its declaration is read in its sandbox, so the
        deletion is counted and the file is named."""
        (worktree / DECLARED_TESTS).unlink()
        _commit(worktree, "drop the declared check")
        self._declared_in_the_sandbox(monkeypatch, "uv run pytest spec/")

        counted = _counts(pool, sidecar.config)

        assert counted.files_changed == 1
        assert counted.tests_deleted == 1
        assert counted.assertions_removed == 1
        assert counted.files == (DECLARED_TESTS,)
        assert counted.lost_something is True

    def test_without_that_declaration_the_same_loss_is_invisible(
        self,
        pool: Any,
        clone: Path,
        worktree: Path,
        sidecar: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Why the side that is asked matters: with no declaration to read,
        the same branch counts nothing and the card reassures him."""
        (worktree / DECLARED_TESTS).unlink()
        _commit(worktree, "drop the declared check")
        self._declared_in_the_sandbox(monkeypatch, None)

        counted = _counts(pool, sidecar.config)

        assert counted.files_changed == 0
        assert counted.lost_something is False

    def test_a_sidecar_that_carries_no_count_is_said_plainly_not_as_nothing(
        self, worktree: Path, sidecar: Any
    ) -> None:
        """A sidecar older than this lane answers without a test diff at all.
        That must read as "nobody read it", never as "nothing changed"."""

        def _post(url: str, body: dict[str, Any], timeout: float):
            from forge.planning.sidecar_git_runner import _urllib_post

            status, decoded = _urllib_post(url, body, timeout)
            decoded.pop("test_patch", None)
            decoded.pop("test_patch_truncated", None)
            return status, decoded

        reading = conductor.read_branch_changes_in_sandbox(
            worktree=worktree,
            base="main",
            sandbox=sidecar.config.planning.sandboxes[REPO],
            repo=REPO,
            post=_post,
        )

        assert reading.error is None
        assert reading.test_patch_read_whole is False


class TestNothingAboutTheTestDiffCanRefuseTheBranch:
    """The third reading is a line on a card, so no way of failing it may
    reach the sentence that refuses a branch.

    Failing it three ways: git exits non-zero, the answer is too long to read
    whole, and — the one that got past the first cut — git never answers at
    all, because it timed out or could not be run. All three leave the two
    readings the fence really does refuse on exactly as they were, and all
    three say "this could not be read" rather than "nothing changed".
    """

    @staticmethod
    def _make_the_test_diff_raise(monkeypatch: pytest.MonkeyPatch) -> None:
        from forge.pipeline.merge_ready_checkpoint import TEST_CHANGE_MARKER

        marker = f"-G{TEST_CHANGE_MARKER}"
        real = subprocess.run

        def _run(argv: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(argv, (list, tuple)) and marker in argv:
                raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120.0)
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _run)

    def test_read_here_a_test_diff_that_never_answers_is_not_an_error(
        self, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")
        self._make_the_test_diff_raise(monkeypatch)

        reading = conductor.read_branch_changes(worktree=worktree, base="main")

        assert reading.error is None
        assert reading.test_patch == ""
        assert reading.test_patch_read_whole is False
        assert TESTS in reading.name_status

    def test_read_here_the_branch_is_still_carded(
        self, pool: Any, clone: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")
        self._make_the_test_diff_raise(monkeypatch)

        report = conductor.make_specification_fence(pool=pool, config=_config(clone))(
            build_id=BUILD_ID, branch=JOURNEY_BRANCH
        )

        assert report.status is SpecificationFenceStatus.CLEAR, report.detail
        assert report.refuses is False
        assert report.test_changes.read_whole is False
        card = merge_card_words(
            feature_id="FEAT-39F6", branch=JOURNEY_BRANCH, gates=_gates(report)
        )
        assert "could not be read here" in card

    def test_in_the_sandbox_a_test_diff_that_never_answers_is_not_an_error(
        self, worktree: Path, sidecar: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same again through the REAL sidecar over the loopback port: the
        route answers 200 with nothing counted, not the 500 the caller would
        have to read as a refusal."""
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")
        self._make_the_test_diff_raise(monkeypatch)

        reading = conductor.read_branch_changes_in_sandbox(
            worktree=worktree,
            base="main",
            sandbox=sidecar.config.planning.sandboxes[REPO],
            repo=REPO,
            post=None,
        )

        assert reading.error is None
        assert reading.test_patch_read_whole is False
        assert TESTS in reading.name_status

    def test_the_sandbox_is_waited_for_as_long_as_it_may_spend(self) -> None:
        """Three git commands now run inside one request, each with the same
        wall. If the caller gave up before the sidecar did, a slow repository
        would come back as an error sentence — and an error sentence refuses
        the branch."""
        waited = (
            conductor.BRANCH_DIFF_TIMEOUT_SECONDS
            * conductor.SANDBOX_BRANCH_DIFF_GIT_CALLS
            + conductor.SANDBOX_BRANCH_DIFF_HTTP_MARGIN_S
        )

        assert waited > conductor.BRANCH_DIFF_TIMEOUT_SECONDS * 3


class TestWhichFilesThisRepositoryCallsItsTests:
    def test_with_no_declaration_the_plain_default_stands(self, clone: Path) -> None:
        from forge.pipeline.merge_ready_checkpoint import DEFAULT_TEST_PATHS

        assert conductor.test_paths_for(clone, REPO) == DEFAULT_TEST_PATHS

    def test_the_repositorys_own_test_command_names_paths_of_its_own(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            conductor,
            "load_declared_toolchain",
            lambda root: SimpleNamespace(test="uv run pytest qa/acceptance/"),
        )

        paths = conductor.test_paths_for(clone, REPO)

        assert "qa/acceptance" in paths

    def test_a_declaration_that_cannot_be_read_leaves_the_default_standing(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.pipeline.merge_ready_checkpoint import DEFAULT_TEST_PATHS

        def _boom(root: Any) -> Any:
            raise RuntimeError("guardkit is not importable here")

        monkeypatch.setattr(conductor, "load_declared_toolchain", _boom)

        assert conductor.test_paths_for(clone, REPO) == DEFAULT_TEST_PATHS

    def test_a_repository_with_a_sandbox_is_read_where_it_lives(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 88, and the whole point of reading a declaration at all.

        A repository with a sandbox keeps its clone in there and none of it
        on this side, so asking here finds no file, says nothing about it,
        and quietly leaves the plain default standing — which would make the
        "read it from where the repository already says so" half of the
        ruling do nothing at all for exactly those repositories.
        """
        asked: list[dict[str, Any]] = []

        def _here(root: Any, **_: Any) -> Any:
            raise AssertionError("a sandboxed repository must not be read here")

        def _there(root: Any, *, sandbox: Any, repo: str, **_: Any) -> Any:
            asked.append({"root": str(root), "sandbox": sandbox, "repo": repo})
            return SimpleNamespace(test="uv run pytest spec/")

        monkeypatch.setattr(conductor, "load_declared_toolchain", _here)
        monkeypatch.setattr(conductor, "load_declared_toolchain_from_sandbox", _there)
        entry = SimpleNamespace(name="api-test-factory", sidecar_url="http://127.0.0.1:1")

        paths = conductor.test_paths_for(clone, REPO, sandbox=entry)

        assert "spec" in paths
        assert asked == [{"root": str(clone), "sandbox": entry, "repo": REPO}]

    def test_a_repository_with_no_sandbox_is_still_read_here(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _there(*_: Any, **__: Any) -> Any:
            raise AssertionError("there is no sandbox to read through")

        monkeypatch.setattr(conductor, "load_declared_toolchain_from_sandbox", _there)
        monkeypatch.setattr(
            conductor,
            "load_declared_toolchain",
            lambda root: SimpleNamespace(test="uv run pytest qa/acceptance/"),
        )

        assert "qa/acceptance" in conductor.test_paths_for(clone, REPO)

    def test_a_sandbox_that_cannot_be_read_leaves_the_default_standing(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same law on both sides: a count nobody could take is the plain
        default and a line in the log, never a stopped journey."""
        from forge.pipeline.merge_ready_checkpoint import DEFAULT_TEST_PATHS

        def _boom(root: Any, **_: Any) -> Any:
            raise RuntimeError("the sidecar refused")

        monkeypatch.setattr(conductor, "load_declared_toolchain_from_sandbox", _boom)
        entry = SimpleNamespace(name="api-test-factory", sidecar_url="http://127.0.0.1:1")

        paths = conductor.test_paths_for(clone, REPO, sandbox=entry)

        assert paths == DEFAULT_TEST_PATHS


class TestTheWholeWayToTheCard:
    def _publish(self, pool: Any, config: ForgeConfig) -> dict[str, Any]:
        handed: list[dict[str, Any]] = []
        publisher = MergeReadyCheckpointPublisher(
            publish_card=lambda **kw: handed.append(kw) or "PUBLISHED",
            gates_green_reader=lambda **_: True,
            branch_reader=lambda _bid: JOURNEY_BRANCH,
            published_probe=lambda _bid: False,
            specification_fence=conductor.make_specification_fence(
                pool=pool, config=config
            ),
        )
        decision = asyncio.run(
            publisher.submit_decision(
                build_id=BUILD_ID,
                feature_id="FEAT-39F6",
                auto_approve=False,
                rationale="mode-c-commits-present",
            )
        )
        assert decision.outcome is MergeCardOutcome.CARD_PUBLISHED
        assert len(handed) == 1
        return handed[0]

    def test_the_card_names_what_went_away_and_invites_the_look(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")

        card = merge_card_words(
            feature_id="FEAT-39F6",
            branch=JOURNEY_BRANCH,
            gates=self._publish(pool, _config(clone))["gates"],
        )

        assert (
            "This branch deleted 1 test and removed 2 assertions in "
            "tests/users/test_router.py — worth a look before you merge."
        ) in card

    def test_a_branch_that_touched_no_test_still_gets_its_card(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        _write(
            worktree / "src" / "users" / "router.py",
            "def lookup(email):\n    return None\n",
        )
        _commit(worktree, "the fix")

        card = merge_card_words(
            feature_id="FEAT-39F6",
            branch=JOURNEY_BRANCH,
            gates=self._publish(pool, _config(clone))["gates"],
        )

        assert "This branch changed no test files." in card
        assert "worth a look" not in card

    def test_the_journeys_own_record_keeps_the_count_too(
        self, pool: Any, clone: Path, worktree: Path
    ) -> None:
        """The decision the receipts are written from carries it, so what the
        card said can be read back without running anything again."""
        _write(worktree / TESTS, THE_TESTS_WITH_LOSSES)
        _commit(worktree, "the fix")

        gates = self._publish(pool, _config(clone))["gates"]

        assert gates.test_changes.tests_deleted == 1
        assert gates.test_changes.assertions_removed == 2
