"""``forge queue --mode c`` puts the task file on a repair branch when the branch has none.

Rewrite-on-refusal spec Part L, rule 49. The attended door takes the same
path the work queue takes: when ``--branch`` carries no
``tasks/**/<task id>*.md`` the fix-task YAML's own fields become a task file
on ``repair/<task id>``, cut from ``--branch``, and the build is queued
there; stdout says so in one sentence. A branch that already carries the
file is queued as given. A checkout the machine cannot even ask, or a write
that fails, refuses with one sentence and queues nothing.

No broker, no live database: the persistence facade and the publisher seam
are fakes, exactly as ``tests/forge/test_mode_c_cap_law.py`` fakes them; the
repository is a real temporary git checkout.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from forge.cli import queue as cli_queue

TASK_ID = "TASK-CAP1FIX1"
FEATURE_ID = "FEAT-CAP1"
REPAIR_BRANCH = f"repair/{TASK_ID}"
TASK_FILE = f"tasks/backlog/feat-cap1/{TASK_ID}-repair.md"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def porcelain_hash(root: Path) -> str:
    status = git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


class _RecordingPersistence:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, Any, Any]] = []

    def exists_active_build(self, feature_id: str) -> bool:
        return False

    def queue_build(self, payload: Any, *, mode: Any = None, profile: str | None = None) -> str:
        self.rows.append((payload, mode, profile))
        return "build-1"


@pytest.fixture(autouse=True)
def _git_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def persistence(monkeypatch: pytest.MonkeyPatch) -> _RecordingPersistence:
    fake = _RecordingPersistence()
    monkeypatch.setattr(cli_queue, "make_persistence", lambda config: fake)
    return fake


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    captured: list[tuple[str, bytes]] = []
    monkeypatch.setattr(cli_queue, "publish", lambda subject, body: captured.append((subject, body)))
    return captured


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "tests@example.com")
    git(repo, "config", "user.name", "the tests")
    (repo / "README.md").write_text("the feature, merged\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the feature, merged")
    return repo


@pytest.fixture
def fix_task_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "fix-task.yaml"
    path.write_text(
        f"id: {TASK_ID}\nname: the delete path answers 503 on Postgres\nparent_feature: {FEATURE_ID}\n",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, repo: Path) -> Path:
    body = {
        "queue": {"repo_allowlist": [str(repo)]},
        "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
        "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        "budget": {
            "default_profile": "attended",
            "profiles": {"attended": {}, "fix-journey": {"max_review_cycles": 2}},
        },
    }
    path = tmp_path / "forge.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _queue(config_path: Path, *, repo: Path, feature_yaml: Path, branch: str | None = None):
    from forge.cli.main import main

    argv = [
        "--config", str(config_path),
        "queue", TASK_ID,
        "--repo", str(repo),
        "--feature-yaml", str(feature_yaml),
        "--mode", "c",
        "--profile", "fix-journey",
        "--correlation-id", "fix-build-FEAT-CAP1-20260907055525",
    ]
    if branch is not None:
        argv += ["--branch", branch]
    return CliRunner().invoke(main, argv)


class TestABranchWithoutTheTaskFile:
    def test_the_file_is_written_on_a_repair_branch_and_the_build_queued_there(
        self,
        tmp_path: Path,
        checkout: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        status_before = porcelain_hash(checkout)

        result = _queue(_config(tmp_path, checkout), repo=checkout, feature_yaml=fix_task_yaml)

        assert result.exit_code == 0, result.output
        assert (
            f"{TASK_ID} had no task file on 'main', so the machine wrote one on the "
            f"branch '{REPAIR_BRANCH}' ({TASK_FILE}) and queued the build there."
        ) in result.output
        assert "Queued FEAT-CAP1 (build pending)" in result.output

        payload, mode, profile = persistence.rows[0]
        assert payload.branch == REPAIR_BRANCH
        assert payload.task_id == TASK_ID
        assert str(mode.value if hasattr(mode, "value") else mode) == "mode-c"
        assert len(published) == 1

        committed = git(checkout, "show", f"{REPAIR_BRANCH}:{TASK_FILE}").stdout
        front = yaml.safe_load(committed.split("---")[1])
        assert front["id"] == TASK_ID
        assert front["task_type"] == "fix"
        assert front["feature_id"] == FEATURE_ID
        assert "the delete path answers 503 on Postgres" in committed
        assert "- Source build: build-FEAT-CAP1-20260907055525" in committed
        copied = git(checkout, "show", f"{REPAIR_BRANCH}:.guardkit/features/{TASK_ID}.yaml").stdout
        assert yaml.safe_load(copied) == yaml.safe_load(fix_task_yaml.read_text())
        assert porcelain_hash(checkout) == status_before
        assert not (checkout / ".forge" / f"repair-{TASK_ID}").exists()

    def test_the_second_run_reuses_the_branch_without_a_second_commit(
        self,
        tmp_path: Path,
        checkout: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        config = _config(tmp_path, checkout)
        assert _queue(config, repo=checkout, feature_yaml=fix_task_yaml).exit_code == 0
        tip = git(checkout, "rev-parse", REPAIR_BRANCH).stdout.strip()

        result = _queue(config, repo=checkout, feature_yaml=fix_task_yaml)

        assert result.exit_code == 0, result.output
        assert git(checkout, "rev-parse", REPAIR_BRANCH).stdout.strip() == tip
        assert len(persistence.rows) == 2


class TestABranchThatAlreadyCarriesTheFile:
    def test_it_is_queued_as_given_and_nothing_is_written(
        self,
        tmp_path: Path,
        checkout: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        task = checkout / "tasks" / "backlog" / "by-hand" / f"{TASK_ID}-by-hand.md"
        task.parent.mkdir(parents=True)
        task.write_text(f"---\nid: {TASK_ID}\n---\n\n## Acceptance Criteria\n\n- [ ] it\n", encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "a task file by hand")

        result = _queue(_config(tmp_path, checkout), repo=checkout, feature_yaml=fix_task_yaml)

        assert result.exit_code == 0, result.output
        assert "had no task file" not in result.output
        payload, _, _ = persistence.rows[0]
        assert payload.branch == "main"
        heads = git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.split()
        assert heads == ["main"]


class TestRefusingInOneSentence:
    def test_a_directory_that_is_not_a_checkout_queues_nothing(
        self,
        tmp_path: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        result = _queue(_config(tmp_path, plain), repo=plain, feature_yaml=fix_task_yaml)

        assert result.exit_code == cli_queue.EXIT_PUBLISH_FAILED
        assert "Nothing was queued" in result.output
        assert "cannot find a repair task without its file" in result.output
        assert persistence.rows == []
        assert published == []

    def test_a_branch_the_checkout_does_not_have_queues_nothing(
        self,
        tmp_path: Path,
        checkout: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        result = _queue(_config(tmp_path, checkout), repo=checkout, feature_yaml=fix_task_yaml, branch="release")

        assert result.exit_code == cli_queue.EXIT_PUBLISH_FAILED
        assert "has no git branch 'release'" in result.output
        assert persistence.rows == []

    def test_a_write_that_fails_refuses_and_queues_nothing(
        self,
        tmp_path: Path,
        checkout: Path,
        fix_task_yaml: Path,
        persistence: _RecordingPersistence,
        published: list[tuple[str, bytes]],
    ) -> None:
        (checkout / "tasks").write_text("not a directory\n", encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "tasks is a file")

        result = _queue(_config(tmp_path, checkout), repo=checkout, feature_yaml=fix_task_yaml)

        assert result.exit_code == cli_queue.EXIT_PUBLISH_FAILED
        assert "Nothing was queued: the repair's task file could not be put on a repair branch" in result.output
        assert persistence.rows == []
        assert published == []
        heads = git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.split()
        assert heads == ["main"]
