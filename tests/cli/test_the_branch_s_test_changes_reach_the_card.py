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
    """The factory's own clone: two tests, the code they cover, and an
    acceptance twin sitting where the specification fence protects it."""
    path = tmp_path / "api_test"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _write(path / TESTS, THE_TESTS)
    _write(path / "src" / "users" / "router.py", "def lookup(email):\n    ...\n")
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
