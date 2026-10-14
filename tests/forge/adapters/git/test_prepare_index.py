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


class TestCoachFixPass1:
    """Coach review 1 of the Forge half, and parity with GuardKit e6bd14d2."""

    @pytest.mark.asyncio
    async def test_f1_an_empty_product_file_beside_a_tracked_empty_record_is_committed(
        self, project: Path
    ) -> None:
        # git pairs empty files with each other as copies; an empty product
        # file is not a copy of a record.
        _write(project, {".guardkit/autobuild/events.jsonl": ""})
        _git(project, "add", "-f", ".guardkit/autobuild/events.jsonl")
        _git(project, "commit", "-q", "-m", "an older build committed an empty record")
        _write(project, {"src/__init__.py": "", "src/app.txt": "two\n"})

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/__init__.py", "src/app.txt"}

    @pytest.mark.asyncio
    async def test_f3_a_rename_inside_the_record_folders_keeps_the_tracked_record(
        self, project: Path
    ) -> None:
        _write(project, {".guardkit/autobuild/a.json": OLD_RECORD})
        _git(project, "add", "-f", ".guardkit/autobuild/a.json")
        _git(project, "commit", "-q", "-m", "an older build committed a record")
        _git(project, "mv", ".guardkit/autobuild/a.json", ".guardkit/autobuild/b.json")
        _write(project, {"src/app.txt": "two\n"})

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
        assert ".guardkit/autobuild/a.json" in _tracked(project)
        assert ".guardkit/autobuild/b.json" not in _tracked(project)

    @pytest.mark.parametrize("ignored", [False, True], ids=["not ignored", "ignored"])
    @pytest.mark.asyncio
    async def test_an_exact_copy_of_an_uncommitted_record_is_left_out(
        self, project: Path, ignored: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        if ignored:
            _write(project, {".gitignore": ".guardkit/autobuild/\nlarge_tool_results/\n"})
            _git(project, "add", ".gitignore")
            _git(project, "commit", "-q", "-m", "ignore the records")
        _write(
            project,
            {
                ".guardkit/autobuild/TASK-1/player_turn_1.json": OLD_RECORD,
                # At least 64 bytes: tinier records are not compared.
                "large_tool_results/out.txt": "a lot of tool output\n" * 4,
                "docs/turn.json": OLD_RECORD,
                "docs/out.txt": "a lot of tool output\n" * 4,
                "src/app.txt": "two\n",
            },
        )

        with caplog.at_level(logging.WARNING):
            result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
        assert (project / "docs/turn.json").is_file()
        assert "docs/" in caplog.text
        assert "moves or copies of GuardKit's own records" in caplog.text

    @pytest.mark.asyncio
    async def test_copies_controls_empty_files_and_inner_build_folders_are_not_compared(
        self, project: Path
    ) -> None:
        inner = project / ".guardkit" / "worktrees" / "FEAT-1"
        inner.mkdir(parents=True)
        _git(inner, "init", "-q", "-b", "main")
        _write(inner, {"src/feature.txt": "the product's own code\n"})
        _write(
            project,
            {
                ".guardkit/autobuild/empty.json": "",
                "src/empty.txt": "",
                "src/feature.txt": "the product's own code\n",
            },
        )

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/empty.txt", "src/feature.txt"}

    @pytest.mark.parametrize(
        "link", [".guardkit/autobuild", ".guardkit/qav-shadow", "large_tool_results"]
    )
    @pytest.mark.asyncio
    async def test_a_link_named_like_a_record_folder_is_left_out(
        self, project: Path, link: str
    ) -> None:
        where = project / link
        where.parent.mkdir(parents=True, exist_ok=True)
        where.symlink_to("/home/example/forge-state/receipts")
        _write(project, {"src/app.txt": "two\n"})

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}


class TestCoachFixPass2:
    """Parity with GuardKit e0b72800: blob ids computed in-process, inner
    build records read, and a bound on how long the reads may take."""

    @pytest.mark.parametrize("object_format", ["sha1", "sha256"])
    def test_git_blob_id_is_what_git_hash_object_says(
        self, tmp_path: Path, object_format: str
    ) -> None:
        from forge.adapters.git.operations import git_blob_id

        repo = tmp_path / object_format
        repo.mkdir()
        _git(repo, "init", "-q", f"--object-format={object_format}")
        for name, data in (
            ("plain", b"hello\n"),
            ("crlf", b"one\r\ntwo\r\n"),
            ("binary", bytes(range(256)) * 3),
            ("no-newline", b"x"),
        ):
            (repo / name).write_bytes(data)
            said = _git(repo, "hash-object", "--no-filters", "--", name).strip()
            assert git_blob_id(data, object_format) == said, name

    def test_a_file_that_cannot_be_read_as_a_regular_file_is_skipped(
        self, tmp_path: Path
    ) -> None:
        import os

        from forge.adapters.git.operations import _read_regular_file

        (tmp_path / "file").write_bytes(b"data")
        (tmp_path / "empty").write_bytes(b"")
        (tmp_path / "link").symlink_to(tmp_path / "file")
        os.mkfifo(tmp_path / "fifo")
        assert _read_regular_file(tmp_path / "file") == b"data"
        assert _read_regular_file(tmp_path / "empty") is None
        assert _read_regular_file(tmp_path / "link") is None
        assert _read_regular_file(tmp_path / "fifo") is None
        assert _read_regular_file(tmp_path / "gone") is None

    @pytest.mark.parametrize(
        "record",
        [
            ".guardkit/autobuild/TASK-1/player_turn_1.json",
            ".guardkit/autobuild-private/judge.json",
            ".guardkit/qav-shadow/queue.jsonl",
            "conversation_history/session.md",
            "large_tool_results/out.txt",
            ".guardkit/memory-query-log.jsonl.1",
            ".guardkit/bootstrap_state.json",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_copy_of_an_inner_builds_record_is_left_out(
        self, project: Path, record: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        inner = project / ".guardkit" / "worktrees" / "FEAT-1"
        inner.mkdir(parents=True)
        _git(inner, "init", "-q", "-b", "main")
        _write(inner, {record: OLD_RECORD, "src/feature.txt": "the product's own code\n"})
        _write(
            project,
            {
                "notes/copied.json": OLD_RECORD,
                "src/feature.txt": "the product's own code\n",
            },
        )

        with caplog.at_level(logging.WARNING):
            result = await commit_all(project, "the work")

        assert result.status == "success", result
        # The inner build's product file is never read, so the outer copy of
        # it is ordinary work.
        assert _committed(project) == {"src/feature.txt"}
        assert "notes/copied.json" in caplog.text

    @pytest.mark.asyncio
    async def test_reads_that_take_too_long_are_left_out_and_the_commit_goes_ahead(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import time

        from forge.adapters.git import operations

        real = operations._read_regular_file

        def slow(path: Path) -> bytes | None:
            time.sleep(0.5)
            return real(path)

        monkeypatch.setattr(operations, "_read_regular_file", slow)
        monkeypatch.setattr(operations, "RECORD_READ_BOUND_S", 0.2)
        _write(
            project,
            {".guardkit/autobuild/x.json": OLD_RECORD, "notes/copy.json": OLD_RECORD},
        )

        with caplog.at_level(logging.WARNING):
            result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert "did not finish within" in caplog.text
        assert _committed(project) == {"notes/copy.json"}


class TestCoachFixPass3:
    """Coordinator decision: records under 64 bytes are not compared."""

    @pytest.mark.asyncio
    async def test_a_tiny_product_file_beside_a_tiny_record_is_committed(
        self, project: Path
    ) -> None:
        _write(
            project,
            {".guardkit/autobuild/TASK-1/list.json": "[]", "data/seed.json": "[]"},
        )

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"data/seed.json"}

    @pytest.mark.asyncio
    async def test_a_copy_of_a_record_of_64_bytes_or_more_is_still_left_out(
        self, project: Path
    ) -> None:
        sixty_four = '{"turn": 1, "said": "' + "x" * 41 + '"}'
        assert len(sixty_four.encode()) == 64
        _write(
            project,
            {
                ".guardkit/autobuild/TASK-1/turn.json": sixty_four,
                "notes/turn.json": sixty_four,
                "src/app.txt": "two\n",
            },
        )

        result = await commit_all(project, "the work")

        assert result.status == "success", result
        assert _committed(project) == {"src/app.txt"}
