"""Forge's commits leave the factory's own records out (design tests 12, 13, 20).

Design: "Factory records never enter a project's commits" (14 October 2026).
Forge's planning commit stages a project's working folder, and so does every
commit made through :func:`forge.adapters.git.operations.commit_all`. Both now
go through one step, :func:`forge.adapters.git.operations.prepare_index`,
which stages the folder with the record paths left out, takes out any record
that got staged anyway (and any move or copy of a record to another path),
and only then answers whether anything is left to commit.

Every case runs real git, through Forge's production git helper, in a
temporary folder. Nothing names a language: the "product" is text files.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from forge.adapters.git.operations import commit_all, prepare_index

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

#: Twenty lines of ordinary JSON, with no private folder name in it.
OLD_RECORD = "{\n" + "".join(f'  "field_{n}": "value number {n}",\n' for n in range(19)) + '  "last": true\n}\n'


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        where = root / path
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A project that ignores nothing, with one commit."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _write(root, {"README": "the widget shop\n", "src/app.txt": "one\n"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "the beginning")
    return root


def _committed(root: Path) -> set[str]:
    return {p for p in _git(root, "show", "--name-only", "--format=", "HEAD").splitlines() if p}


def _tracked(root: Path) -> set[str]:
    return {p for p in _git(root, "ls-files").splitlines() if p}


RECORDS = {
    ".guardkit/autobuild/x.json": '{"turn": 1}\n',
    ".guardkit/autobuild/TASK-1/player_turn_1.json": '{"said": "hello"}\n',
    ".guardkit/qav-shadow/queue.jsonl": '{"q": 1}\n',
    ".guardkit/memory-query-log.jsonl": '{"query": "a"}\n',
    ".guardkit/memory-query-log.jsonl.1": '{"query": "b"}\n',
    "conversation_history/session.md": "what the model said\n",
    "large_tool_results/out.txt": "a lot of output\n",
}

STILL_ARRIVE = {
    ".guardkit/features/F.yaml": "id: F\n",
    "tasks/backlog/TASK-1.md": "# task\n",
    ".guardkit/config.yaml": "toolchain: {}\n",
    ".guardkit/seam-checks.yaml": "checks: []\n",
    "docs/state/notes.md": "notes a person wrote\n",
    "src/conversation_history/k.txt": "a product's own folder of that name\n",
}


class TestCommitAllLeavesRecordsOut:
    @pytest.mark.asyncio
    async def test_new_records_are_not_committed_and_stay_on_disk(
        self, project: Path
    ) -> None:
        _write(project, {**RECORDS, **STILL_ARRIVE, "src/app.txt": "two\n"})

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        committed = _committed(project)
        assert committed == {"src/app.txt", *STILL_ARRIVE}
        for path in RECORDS:
            assert (project / path).is_file(), path

    @pytest.mark.asyncio
    async def test_an_already_staged_record_is_taken_out_with_a_warning(
        self, project: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(project, {".guardkit/autobuild/x.json": "{}\n", "src/app.txt": "two\n"})
        _git(project, "add", "-f", ".guardkit/autobuild/x.json")

        with caplog.at_level(logging.WARNING):
            result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
        assert (project / ".guardkit/autobuild/x.json").is_file()
        assert ".guardkit/autobuild/x.json" in caplog.text

    @pytest.mark.asyncio
    async def test_a_change_to_a_tracked_record_is_not_committed(
        self, project: Path
    ) -> None:
        _write(project, {".guardkit/autobuild/old.json": OLD_RECORD})
        _git(project, "add", "-f", ".guardkit/autobuild/old.json")
        _git(project, "commit", "-q", "-m", "an older build committed a record")
        _write(
            project,
            {".guardkit/autobuild/old.json": OLD_RECORD + "\n", "src/app.txt": "two\n"},
        )

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
        assert _git(project, "show", "HEAD:.guardkit/autobuild/old.json") == OLD_RECORD

    @pytest.mark.asyncio
    async def test_an_untracked_nested_build_folder_is_not_committed(
        self, project: Path
    ) -> None:
        inner = project / ".guardkit" / "worktrees" / "FEAT-1"
        inner.mkdir(parents=True)
        _git(inner, "init", "-q", "-b", "main")
        _write(inner, {"inner.txt": "inner\n"})
        _write(project, {"src/app.txt": "two\n"})

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
        assert not any(p.startswith(".guardkit/worktrees") for p in _tracked(project))


class TestARecordMovedOut:
    """Design test 20, the checkpoint half, through Forge's ``prepare_index``."""

    @pytest.fixture()
    def tracking(self, project: Path) -> Path:
        _write(project, {".guardkit/autobuild/old.json": OLD_RECORD})
        _git(project, "add", "-f", ".guardkit/autobuild/old.json")
        _git(project, "commit", "-q", "-m", "an older build committed a record")
        return project

    @pytest.mark.parametrize("shape", ["git mv", "plain move"])
    @pytest.mark.asyncio
    async def test_a_lightly_edited_record_moved_out_is_left_out(
        self, tracking: Path, shape: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tracking / "notes").mkdir()
        if shape == "git mv":
            _git(tracking, "mv", ".guardkit/autobuild/old.json", "notes/old.json")
        else:
            (tracking / ".guardkit/autobuild/old.json").rename(tracking / "notes/old.json")
        with (tracking / "notes/old.json").open("a", encoding="utf-8") as handle:
            handle.write('{"one more": "line"}\n')
        _write(tracking, {"src/app.txt": "two\n"})

        with caplog.at_level(logging.WARNING):
            prepared = await prepare_index(tracking)
        assert prepared.failure is None, prepared
        assert prepared.staged is True
        result = await commit_all(tracking, "the work")

        assert result.status == "success", result
        assert _committed(tracking) == {"src/app.txt"}
        assert (tracking / "notes/old.json").is_file()
        assert "notes/old.json" in caplog.text
        assert "moves or copies of GuardKit's own records" in caplog.text

    @pytest.mark.asyncio
    async def test_control_only_deleting_the_record_keeps_the_product_work(
        self, tracking: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tracking / ".guardkit/autobuild/old.json").unlink()
        _write(tracking, {"src/app.txt": "two\n", "src/more.txt": "more\n"})

        with caplog.at_level(logging.WARNING):
            result = await commit_all(tracking, "the work")

        assert result.status == "success", result
        assert {"src/app.txt", "src/more.txt"} <= _committed(tracking)
        assert "moves or copies" not in caplog.text

    @pytest.mark.asyncio
    async def test_control_a_product_file_copied_from_another_is_kept(
        self, tracking: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tracking, {"src/copy.txt": (tracking / "src/app.txt").read_text()})

        with caplog.at_level(logging.WARNING):
            result = await commit_all(tracking, "the work")

        assert result.status == "success", result
        assert _committed(tracking) == {"src/copy.txt"}
        assert "moves or copies" not in caplog.text


class TestTheAnswerComesAfterTheRecordsAreOut:
    @pytest.mark.asyncio
    async def test_only_records_means_nothing_is_staged(self, project: Path) -> None:
        _write(project, RECORDS)
        _write(project, {".guardkit/autobuild/pre.json": "{}\n"})
        _git(project, "add", "-f", ".guardkit/autobuild/pre.json")

        prepared = await prepare_index(project)

        assert prepared.failure is None, prepared
        assert prepared.staged is False
        assert _git(project, "diff", "--cached", "--name-only") == ""

    @pytest.mark.asyncio
    async def test_it_is_safe_to_run_twice(self, project: Path) -> None:
        _write(project, {**RECORDS, "src/app.txt": "two\n"})

        first = await prepare_index(project)
        second = await prepare_index(project)

        assert first.staged is True and second.staged is True
        assert _git(project, "diff", "--cached", "--name-only").split() == ["src/app.txt"]
