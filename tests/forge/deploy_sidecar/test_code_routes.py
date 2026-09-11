"""The code door — the sidecar's four read-only routes into a repository's
own source (Rich's ruling, 2026-09-11: the seat can ASK).

Real code paths throughout. Every case builds a REAL git repository in a
temporary directory — tracked files, an untracked one, a binary one, a file
over the byte cap, and a symbolic link pointing out of the tree — and drives
the real request functions against it. Two cases go the whole way over a real
loopback HTTP server on an ephemeral port. Nothing is faked except the clock
the search's own wall is read from, and the one place a git that never answers
has to be simulated.

What these tests are really holding in place:

* the door serves only what git TRACKS, so it can never hand back a virtual
  environment, a build artefact or a stray secret file sitting in the tree;
* a path that leaves the repository is refused three ways — by its spelling
  ('..'), because it is absolute, and after resolution when a tracked symbolic
  link points outside;
* every cap actually caps, and the answer says so rather than quietly
  shortening;
* the one git command the door runs is asserted argument for argument, and no
  other program is ever started — in particular the search never reaches grep;
* the door writes nothing: the repository's commit and its clean status are
  the same after every route has run;
* the search's time limit is read before every file AND before every line, so
  one enormous file cannot run past it — and the one shape of pattern no clock
  in this process could stop is refused before a file is opened.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from forge.config.models import ForgeConfig
from forge.deploy_sidecar.service import (
    CODE_IMPORTS_ROUTE,
    CODE_LIST_FILES_ROUTE,
    CODE_READ_FILE_ROUTE,
    CODE_SEARCH_ROUTE,
    build_server,
    process_code_imports_request,
    process_code_list_files_request,
    process_code_read_file_request,
    process_code_search_request,
)
from forge.deploy_sidecar import service as sidecar

REPO_KEY = "guardkit/api_test"

#: What the repository tracks, and why each file is there.
USERS_PY = """import os
from pathlib import Path

from fastapi import APIRouter

from .db import get_session


def get_user(user_id: int) -> dict:
    \"\"\"The endpoint the plan seat kept proposing to create.\"\"\"
    return {"id": user_id, "created_at": os.environ.get("NOW"), "p": Path(".")}
"""

MODELS_PY = """from sqlalchemy import Column, DateTime


class User:
    created_at = Column(DateTime)
"""

BROKEN_PY = "def get_user(:\n    pass\n"

README = "# api_test\n\nA repository that already has a users router.\n"

NOTES_MD = "get_user is documented here, in a file that is not Python.\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file OUTSIDE the repository — the thing the fences exist to keep in."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.txt"
    secret.write_text("the key nobody asked this door for\n", encoding="utf-8")
    return secret


@pytest.fixture
def repo(tmp_path: Path, outside: Path) -> Path:
    """A real git repository with one commit and everything a fence needs."""
    path = tmp_path / "api_test"
    (path / "src" / "users").mkdir(parents=True)
    (path / "docs").mkdir()
    (path / ".venv" / "bin").mkdir(parents=True)

    (path / "src" / "users" / "router.py").write_text(USERS_PY, encoding="utf-8")
    (path / "src" / "users" / "models.py").write_text(MODELS_PY, encoding="utf-8")
    (path / "src" / "users" / "broken.py").write_text(BROKEN_PY, encoding="utf-8")
    (path / "README.md").write_text(README, encoding="utf-8")
    (path / "docs" / "notes.md").write_text(NOTES_MD, encoding="utf-8")
    # Tracked and binary: a null byte in the first block.
    (path / "docs" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00" + b"\xff" * 64)
    # Tracked and far over the byte cap.
    (path / "docs" / "huge.txt").write_text("x" * 300_000, encoding="utf-8")
    # Tracked, and a symbolic link that resolves OUT of the repository.
    (path / "escape.txt").symlink_to(outside)
    # Present on disk and deliberately NOT tracked — the virtual environment,
    # the build artefact, the stray secret file.
    (path / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "local-secrets.env").write_text("TOKEN=hunter2\n", encoding="utf-8")

    _git(path, "init", "-q")
    _git(path, "add", "README.md", "docs", "src", "escape.txt")
    _git(path, "commit", "-qm", "init")
    return path


def _config(paths: dict[str, str]) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": paths},
        }
    )


@pytest.fixture
def cfg(repo: Path) -> ForgeConfig:
    return _config({REPO_KEY: str(repo)})


TRACKED = [
    "README.md",
    "docs/huge.txt",
    "docs/logo.png",
    "docs/notes.md",
    "escape.txt",
    "src/users/broken.py",
    "src/users/models.py",
    "src/users/router.py",
]


# ---------------------------------------------------------------------------
# /code/list-files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_it_answers_the_tracked_files_sorted_and_nothing_else(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_list_files_request(
            {"repo": REPO_KEY}, config=cfg
        )

        assert status == 200, body
        assert body["files"] == TRACKED
        assert body["count"] == len(TRACKED) and body["total_tracked"] == len(TRACKED)
        assert body["capped"] is False and body["under"] is None
        # The two things sitting in the tree that git does not track.
        assert ".venv/bin/python" not in body["files"]
        assert "local-secrets.env" not in body["files"]

    def test_under_restricts_the_answer_to_one_directory(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_list_files_request(
            {"repo": REPO_KEY, "under": "src/users"}, config=cfg
        )

        assert status == 200, body
        assert body["files"] == [
            "src/users/broken.py",
            "src/users/models.py",
            "src/users/router.py",
        ]
        assert body["under"] == "src/users"
        assert body["total_tracked"] == len(TRACKED)

    def test_the_one_git_command_is_exactly_this_argument_list(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The door runs git for ONE thing, and this is the line it runs."""
        calls: list[list[str]] = []
        real = subprocess.run

        def _run(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            calls.append(list(argv))
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _run)

        status, _body = process_code_list_files_request(
            {"repo": REPO_KEY}, config=cfg
        )

        assert status == 200
        assert calls == [
            ["git", "-c", "safe.directory=*", "-C", str(repo), "ls-files", "-z"]
        ]

    def test_the_cap_caps_and_the_answer_says_it_was_reached(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sidecar, "CODE_MAX_TRACKED_PATHS", 3)

        status, body = process_code_list_files_request(
            {"repo": REPO_KEY}, config=cfg
        )

        assert status == 200, body
        assert body["files"] == TRACKED[:3]
        assert body["count"] == 3 and body["cap"] == 3
        assert body["capped"] is True
        assert body["total_tracked"] == len(TRACKED)

    def test_an_under_that_leaves_the_repository_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        for value in ("../elsewhere", "/etc", "src/../../elsewhere"):
            status, body = process_code_list_files_request(
                {"repo": REPO_KEY, "under": value}, config=cfg
            )
            assert status == 400, (value, body)
            assert "'under'" in body["error"]

    def test_an_under_that_is_not_a_directory_is_refused_plainly(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_list_files_request(
            {"repo": REPO_KEY, "under": "README.md"}, config=cfg
        )

        assert status == 400
        assert body["error"] == "'under' 'README.md' is not a directory in this repository"

    def test_a_directory_that_is_not_a_git_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        status, body = process_code_list_files_request(
            {"repo": REPO_KEY}, config=_config({REPO_KEY: str(plain)})
        )

        assert status == 400
        assert "is not a git repository" in body["error"]

    def test_a_git_that_never_answers_is_a_500_not_an_empty_repository(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _run(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=60.0)

        monkeypatch.setattr(subprocess, "run", _run)

        status, body = process_code_list_files_request(
            {"repo": REPO_KEY}, config=cfg
        )

        assert status == 500
        assert "sidecar git error" in body["error"]


# ---------------------------------------------------------------------------
# /code/read-file
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_it_answers_one_tracked_file_whole(self, cfg: ForgeConfig) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "src/users/router.py"}, config=cfg
        )

        assert status == 200, body
        assert body["content"] == USERS_PY
        assert body["path"] == "src/users/router.py"
        assert body["bytes"] == len(USERS_PY.encode("utf-8"))
        assert body["first_line"] == 1
        assert body["total_lines"] == len(USERS_PY.splitlines())
        assert body["last_line"] == body["total_lines"]

    def test_first_and_last_line_read_a_part_of_it(self, cfg: ForgeConfig) -> None:
        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "src/users/router.py",
                "first_line": 1,
                "last_line": 2,
            },
            config=cfg,
        )

        assert status == 200, body
        assert body["content"] == "import os\nfrom pathlib import Path\n"
        assert (body["first_line"], body["last_line"]) == (1, 2)
        assert body["total_lines"] == len(USERS_PY.splitlines())

    def test_a_line_range_the_wrong_way_round_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "src/users/router.py",
                "first_line": 9,
                "last_line": 2,
            },
            config=cfg,
        )

        assert status == 400
        assert "before 'first_line'" in body["error"]

    def test_a_first_line_past_the_end_is_refused_rather_than_answered_empty(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "src/users/router.py", "first_line": 500},
            config=cfg,
        )

        assert status == 400
        assert "is past the end of src/users/router.py" in body["error"]
        assert "which has 11 lines" in body["error"]

    def test_a_line_number_that_is_not_a_whole_number_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        for field in ("first_line", "last_line"):
            for value in (0, -3, "2", 1.5, True):
                status, body = process_code_read_file_request(
                    {"repo": REPO_KEY, "path": "README.md", field: value},
                    config=cfg,
                )
                assert status == 400, (field, value, body)
                assert body["error"] == (
                    f"'{field}' must be a whole number of at least 1"
                )

    def test_a_path_that_leaves_the_repository_by_spelling_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        for value in ("../elsewhere/secret.txt", "/etc/passwd", "src/../../x"):
            status, body = process_code_read_file_request(
                {"repo": REPO_KEY, "path": value}, config=cfg
            )
            assert status == 400, (value, body)
            assert "'path'" in body["error"]

    def test_a_tracked_symlink_that_resolves_outside_is_refused(
        self, cfg: ForgeConfig, outside: Path
    ) -> None:
        """The half a spelling check cannot do. ``escape.txt`` is tracked, is
        spelled like any other file in the tree, and points at a file outside
        it — so the refusal has to come after resolution."""
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "escape.txt"}, config=cfg
        )

        assert status == 400
        assert "outside the repository" in body["error"]
        assert str(outside.resolve()) in body["error"]

    def test_a_file_git_does_not_track_is_refused(self, cfg: ForgeConfig) -> None:
        for value in (".venv/bin/python", "local-secrets.env"):
            status, body = process_code_read_file_request(
                {"repo": REPO_KEY, "path": value}, config=cfg
            )
            assert status == 400, (value, body)
            assert f"git does not track {value}" in body["error"]

    def test_a_binary_file_is_refused_in_one_sentence(self, cfg: ForgeConfig) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "docs/logo.png"}, config=cfg
        )

        assert status == 400
        assert "is not a text file" in body["error"]

    def test_a_file_over_the_byte_cap_is_refused_not_truncated(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "docs/huge.txt"}, config=cfg
        )

        assert status == 400
        assert "over this door's limit" in body["error"]
        assert str(sidecar.CODE_MAX_FILE_BYTES) in body["error"]

    def test_the_byte_cap_is_the_one_named_in_the_module(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sidecar, "CODE_MAX_FILE_BYTES", 10)

        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "src/users/router.py"}, config=cfg
        )

        assert status == 400
        assert "over this door's limit of 10 bytes" in body["error"]

    def test_a_missing_path_is_refused_before_anything_is_read(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request({"repo": REPO_KEY}, config=cfg)

        assert status == 400
        assert "'path' must be a relative path inside the repository" in body["error"]


# ---------------------------------------------------------------------------
# /code/imports
# ---------------------------------------------------------------------------


class TestImports:
    def test_one_file_answers_its_imports_from_the_syntax_tree(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src/users/router.py"}, config=cfg
        )

        assert status == 200, body
        assert body["files_walked"] == 1 and body["capped"] is False
        entry = body["files"][0]
        assert entry["path"] == "src/users/router.py"
        assert entry["language"] == "python"
        assert [i["statement"] for i in entry["imports"]] == [
            "import os",
            "from pathlib import Path",
            "from fastapi import APIRouter",
            "from .db import get_session",
        ]
        kinds = {i["module"]: i["kind"] for i in entry["imports"]}
        assert kinds == {
            "os": "stdlib",
            "pathlib": "stdlib",
            "fastapi": "third-party",
            ".db": "local",
        }

    def test_the_reader_is_a_parser_not_a_text_search(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        """A line inside a string is not an import, and only a parser knows
        that. This is the whole reason the route says 'by parsing'."""
        (repo / "src" / "users" / "decoy.py").write_text(
            'DOCS = """\nimport nothing_at_all\n"""\n# import also_not_this\n'
            "import json\n",
            encoding="utf-8",
        )
        _git(repo, "add", "src/users/decoy.py")
        _git(repo, "commit", "-qm", "decoy")

        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src/users/decoy.py"}, config=cfg
        )

        assert status == 200, body
        assert [i["module"] for i in body["files"][0]["imports"]] == ["json"]

    def test_a_directory_walks_the_tracked_files_under_it(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src"}, config=cfg
        )

        assert status == 200, body
        assert [f["path"] for f in body["files"]] == [
            "src/users/broken.py",
            "src/users/models.py",
            "src/users/router.py",
        ]

    def test_a_file_that_does_not_parse_is_named_with_the_reason(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src/users/broken.py"}, config=cfg
        )

        assert status == 200, body
        entry = body["files"][0]
        assert entry["imports"] == []
        assert "does not parse as Python" in entry["note"]

    def test_a_file_that_is_not_python_says_so_rather_than_guessing(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "docs/notes.md"}, config=cfg
        )

        assert status == 200, body
        entry = body["files"][0]
        assert entry["language"] == "other" and entry["imports"] == []
        assert "is not a Python file" in entry["note"]
        assert "does not guess at other languages" in entry["note"]

    def test_the_walk_cap_caps_and_the_answer_says_it_was_reached(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sidecar, "CODE_IMPORTS_MAX_FILES", 2)

        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src"}, config=cfg
        )

        assert status == 200, body
        assert body["files_walked"] == 2
        assert body["capped"] is True and body["cap"] == 2

    def test_an_untracked_path_is_refused(self, cfg: ForgeConfig) -> None:
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": ".venv"}, config=cfg
        )

        assert status == 400
        assert "git does not track .venv" in body["error"]

    def test_a_path_that_leaves_the_repository_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        for value in ("../elsewhere", "/etc"):
            status, body = process_code_imports_request(
                {"repo": REPO_KEY, "path": value}, config=cfg
            )
            assert status == 400, (value, body)
            assert "'path'" in body["error"]


# ---------------------------------------------------------------------------
# /code/search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_it_finds_the_matching_lines_with_their_file_and_number(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": r"def get_user"}, config=cfg
        )

        assert status == 200, body
        # Both tracked Python files carry the phrase: the broken one on its
        # first line and the router on its ninth, in path order.
        assert [(m["path"], m["line"]) for m in body["matches"]] == [
            ("src/users/broken.py", 1),
            ("src/users/router.py", 9),
        ]
        match = body["matches"][1]
        assert match["text"] == "def get_user(user_id: int) -> dict:"
        assert match["line_truncated"] is False
        assert body["timed_out"] is False and body["capped"] is False

    def test_a_pattern_that_does_not_compile_is_refused_with_the_reason(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user("}, config=cfg
        )

        assert status == 400
        assert "is not a regular expression this door can use" in body["error"]
        assert "'fixed_string': true" in body["error"]

    def test_the_same_pattern_as_plain_text_finds_the_line(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user(", "fixed_string": True},
            config=cfg,
        )

        assert status == 200, body
        assert [(m["path"], m["line"]) for m in body["matches"]] == [
            ("src/users/broken.py", 1),
            ("src/users/router.py", 9),
        ]
        assert body["fixed_string"] is True

    def test_case_insensitive_is_asked_for_and_honoured(
        self, cfg: ForgeConfig
    ) -> None:
        plain = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "APIROUTER"}, config=cfg
        )[1]
        loose = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "APIROUTER", "case_insensitive": True},
            config=cfg,
        )[1]

        assert plain["count"] == 0
        assert loose["count"] == 1

    def test_under_restricts_the_search_to_one_directory(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user", "under": "docs"}, config=cfg
        )

        assert status == 200, body
        assert [m["path"] for m in body["matches"]] == ["docs/notes.md"]
        assert body["under"] == "docs"

    def test_the_match_cap_caps_and_the_answer_says_it_was_reached(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (repo / "src" / "many.py").write_text("x = 1\n" * 40, encoding="utf-8")
        _git(repo, "add", "src/many.py")
        _git(repo, "commit", "-qm", "many")
        monkeypatch.setattr(sidecar, "CODE_SEARCH_MAX_MATCHES", 5)

        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "x = 1"}, config=cfg
        )

        assert status == 200, body
        assert body["count"] == 5 and body["cap"] == 5
        assert body["capped"] is True

    def test_a_caller_may_ask_for_fewer_matches_but_never_more(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (repo / "src" / "many.py").write_text("x = 1\n" * 40, encoding="utf-8")
        _git(repo, "add", "src/many.py")
        _git(repo, "commit", "-qm", "many")
        monkeypatch.setattr(sidecar, "CODE_SEARCH_MAX_MATCHES", 5)

        fewer = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "x = 1", "max_results": 2}, config=cfg
        )[1]
        more = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "x = 1", "max_results": 500}, config=cfg
        )[1]

        assert fewer["count"] == 2 and fewer["cap"] == 2
        assert more["count"] == 5 and more["cap"] == 5

    def test_a_very_long_line_comes_back_cut_and_flagged(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (repo / "src" / "long.py").write_text(
            "MINIFIED = '" + "a" * 900 + "'  # needle\n", encoding="utf-8"
        )
        _git(repo, "add", "src/long.py")
        _git(repo, "commit", "-qm", "long")
        monkeypatch.setattr(sidecar, "CODE_SEARCH_MAX_LINE_CHARS", 40)

        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "MINIFIED"}, config=cfg
        )

        assert status == 200, body
        match = body["matches"][0]
        assert len(match["text"]) == 40
        assert match["line_truncated"] is True
        assert body["line_cap"] == 40

    def test_it_never_shells_out_to_grep_or_starts_any_other_program(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        real = subprocess.run

        def _run(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            calls.append(list(argv))
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _run)

        status, _body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user"}, config=cfg
        )

        assert status == 200
        assert calls == [
            ["git", "-c", "safe.directory=*", "-C", str(repo), "ls-files", "-z"]
        ]

    def test_binary_and_over_cap_files_are_passed_over(
        self, cfg: ForgeConfig
    ) -> None:
        """``docs/logo.png`` and ``docs/huge.txt`` are tracked; neither is
        searched, so the file count is the text files only."""
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "nothing-matches-this"}, config=cfg
        )

        assert status == 200, body
        # Eight tracked paths: the binary, the over-cap file and the symbolic
        # link out of the tree are all passed over.
        assert body["files_searched"] == 5

    def test_a_missing_pattern_is_refused(self, cfg: ForgeConfig) -> None:
        for payload in ({"repo": REPO_KEY}, {"repo": REPO_KEY, "pattern": "   "}):
            status, body = process_code_search_request(payload, config=cfg)
            assert status == 400, body
            assert body["error"] == "'pattern' is required (the text to search for)"

    def test_a_pattern_longer_than_the_cap_is_refused(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "a" * 2_000}, config=cfg
        )

        assert status == 400
        assert "over this door's limit" in body["error"]

    # --- the time limit, and the two limits that stand where it cannot -----

    def test_the_time_limit_stops_the_walk_between_files_and_says_so(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clock already past its wall stops the search before it opens
        anything, and the answer carries ``timed_out`` rather than looking
        like a search that found nothing."""
        monkeypatch.setattr(sidecar, "CODE_SEARCH_TIMEOUT_SECONDS", -1.0)

        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user"}, config=cfg
        )

        assert status == 200, body
        assert body["timed_out"] is True
        assert body["files_searched"] == 0 and body["count"] == 0

    def test_the_time_limit_also_stops_the_walk_inside_one_file(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The limit is read for every LINE, not only for every file.

        One tracked file of twenty thousand lines carries the only match on
        its last line. Read with a real clock, the search finds it. Read with
        a clock wound forward a thousandth of a second on every reading, the
        search stops partway down that same file — ``files_searched`` is 1, so
        the file was opened and entered, and the match at the end was never
        reached. Before this test the limit was read once per file and nothing
        stopped the work inside one.
        """
        (repo / "big").mkdir()
        (repo / "big" / "huge.py").write_text(
            "x = 1\n" * 20_000 + "needle = 1\n", encoding="utf-8"
        )
        _git(repo, "add", "big/huge.py")
        _git(repo, "commit", "-qm", "huge")

        found = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "needle", "under": "big"}, config=cfg
        )[1]
        assert found["count"] == 1 and found["timed_out"] is False

        ticks = itertools.count()
        monkeypatch.setattr(sidecar, "CODE_SEARCH_TIMEOUT_SECONDS", 3.0)
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks) * 0.001)

        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "needle", "under": "big"}, config=cfg
        )

        assert status == 200, body
        assert body["timed_out"] is True
        assert body["files_searched"] == 1
        assert body["count"] == 0

    def test_a_pattern_whose_work_explodes_is_refused_before_any_file_is_read(
        self, cfg: ForgeConfig, repo: Path
    ) -> None:
        """The shape no clock in this process could stop.

        Python's engine backtracks, one ``search()`` call cannot be
        interrupted, and while it runs it holds the interpreter — so this
        ordinary-looking source line and this ordinary-looking pattern would
        together freeze the whole sidecar, not just this request. The refusal
        comes back in one sentence, before any file is opened, and it says
        what to send instead. Ten seconds is a wall for the test itself: the
        answer is immediate, and the old code never returned at all.
        """
        (repo / "src" / "plain.py").write_text(
            "x = '" + "a" * 64 + "'\n", encoding="utf-8"
        )
        _git(repo, "add", "src/plain.py")
        _git(repo, "commit", "-qm", "plain")

        started = time.monotonic()
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": r"(a+)+$"}, config=cfg
        )
        elapsed = time.monotonic() - started

        assert status == 400, body
        assert elapsed < 10.0
        assert "repeats a group whose own content repeats" in body["error"]
        assert "'fixed_string': true" in body["error"]

    @pytest.mark.parametrize(
        "pattern",
        [r"a+a+a+a+a+$", r"\s*\w+\s*\w+\s*\w+$"],
    )
    def test_two_repeats_that_can_match_the_same_characters_are_refused_too(
        self, pattern: str, cfg: ForgeConfig, repo: Path
    ) -> None:
        """The second shape no clock could stop, and the reason the line cap
        does not cover it.

        Neither of these repeats a group — they repeat two pieces side by
        side that can both match the same character, so the engine can split
        the same line more ways with every character it holds. The file here
        carries two lines of exactly five hundred characters, which is this
        door's own line cap, so nothing is trimmed. Measured on this machine
        against one such line: the first pattern had not finished after
        twenty-five seconds, and the second took fifteen. The door's whole
        time limit is thirty seconds, and it cannot be read in the middle of
        either.
        """
        (repo / "src" / "plain.py").write_text(
            ("a" * 499 + "!\n") * 2, encoding="utf-8"
        )
        _git(repo, "add", "src/plain.py")
        _git(repo, "commit", "-qm", "plain")

        started = time.monotonic()
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": pattern}, config=cfg
        )
        elapsed = time.monotonic() - started

        assert status == 400, body
        assert elapsed < 10.0
        assert "repeats two pieces side by side" in body["error"]
        assert "'fixed_string': true" in body["error"]

    def test_that_same_pattern_is_searched_for_happily_as_plain_text(
        self, cfg: ForgeConfig
    ) -> None:
        """The refusal above names this way out, so it has to work: taken
        literally the pattern is text, and text cannot explode."""
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": r"(a+)+$", "fixed_string": True},
            config=cfg,
        )

        assert status == 200, body
        assert body["count"] == 0 and body["timed_out"] is False

    @pytest.mark.parametrize(
        "pattern",
        [
            r"def get_user",
            r"^\s*import\s+os",
            r"(self\.)?user_id",
            r"^(def|class) \w+",
            r"router\.(get|post)\(",
            r"[a-z]+_[a-z]+",
            r"def\s+\w+\s*\(",
            r"class \w+\(.*\):",
            r"^from \S+ import ",
            r"[A-Z]\w*\s*=\s*\d+",
            r"#\s*TODO:.*",
            r"\d{4}-\d{2}-\d{2}",
        ],
    )
    def test_the_shape_check_refuses_none_of_the_patterns_people_write(
        self, pattern: str, cfg: ForgeConfig
    ) -> None:
        """The check refuses a shape, not a feature: groups, alternatives,
        optional parts and repeats are all still searchable — including two
        repeats in one pattern when they cannot match the same characters,
        which is what ``def\s+\w+\s*\(`` is doing here."""
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": pattern}, config=cfg
        )

        assert status == 200, body

    def test_without_the_parser_the_shape_check_refuses_more_not_less(
        self, cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shape is normally read with the parser Python's own ``re``
        module uses. If that reading is ever unavailable, the door falls back
        to refusing any repeated group — more than it must, never less."""
        monkeypatch.setattr(sidecar, "_re_parser", None)

        refused = process_code_search_request(
            {"repo": REPO_KEY, "pattern": r"(get|set)+"}, config=cfg
        )
        allowed = process_code_search_request(
            {"repo": REPO_KEY, "pattern": r"get_user"}, config=cfg
        )

        assert refused[0] == 400
        assert "repeats a group whose own content repeats" in refused[1]["error"]
        assert allowed[0] == 200, allowed[1]

    def test_only_the_first_characters_of_a_long_line_are_searched(
        self, cfg: ForgeConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The line cap bounds what the engine is handed, which is the second
        thing standing where the clock cannot — so the answer has to say how
        many lines were only partly searched, rather than let a caller read a
        silent nought as 'not in this repository'."""
        (repo / "big").mkdir()
        (repo / "big" / "minified.py").write_text(
            "HEAD = '" + "a" * 900 + "needle'\n", encoding="utf-8"
        )
        _git(repo, "add", "big/minified.py")
        _git(repo, "commit", "-qm", "minified")
        monkeypatch.setattr(sidecar, "CODE_SEARCH_MAX_LINE_CHARS", 40)

        near = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "HEAD", "under": "big"}, config=cfg
        )[1]
        far = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "needle", "under": "big"}, config=cfg
        )[1]

        assert near["count"] == 1
        assert near["matches"][0]["line_truncated"] is True
        assert near["long_lines_partly_searched"] == 1
        # Past the cap the line is not searched at all, and the answer says
        # that one line was cut rather than pretending the word is absent.
        assert far["count"] == 0
        assert far["long_lines_partly_searched"] == 1

    def test_an_ordinary_line_is_searched_whole_and_counted_as_such(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "get_user"}, config=cfg
        )

        assert status == 200, body
        assert body["long_lines_partly_searched"] == 0


# ---------------------------------------------------------------------------
# The laws every route on this service keeps
# ---------------------------------------------------------------------------


ROUTES = (
    (process_code_list_files_request, {}),
    (process_code_read_file_request, {"path": "README.md"}),
    (process_code_imports_request, {"path": "src"}),
    (process_code_search_request, {"pattern": "get_user"}),
)


class TestTheSharedLaws:
    @pytest.mark.parametrize("handler,extra", ROUTES)
    def test_an_unknown_repository_key_is_refused_by_name(
        self, handler: Any, extra: dict[str, Any], cfg: ForgeConfig
    ) -> None:
        status, body = handler({"repo": "acme/ghost", **extra}, config=cfg)

        assert status == 400
        assert "unknown target repo 'acme/ghost'" in body["error"]
        assert REPO_KEY in body["error"]

    @pytest.mark.parametrize("handler,extra", ROUTES)
    def test_a_body_that_is_not_an_object_is_refused(
        self, handler: Any, extra: dict[str, Any], cfg: ForgeConfig
    ) -> None:
        status, body = handler(["not", "an", "object"], config=cfg)

        assert status == 400
        assert body["error"] == "request body must be a JSON object"

    @pytest.mark.parametrize("handler,extra", ROUTES)
    def test_no_route_here_writes_anything(
        self, handler: Any, extra: dict[str, Any], cfg: ForgeConfig, repo: Path
    ) -> None:
        """READ ONLY, proved on the repository itself: the same commit, the
        same clean status, and no worktree either side of the call."""
        before = _git(repo, "rev-parse", "HEAD").stdout
        handler({"repo": REPO_KEY, **extra}, config=cfg)

        assert _git(repo, "rev-parse", "HEAD").stdout == before
        assert _git(repo, "status", "--porcelain").stdout == (
            "?? .venv/\n?? local-secrets.env\n"
        )
        assert _git(repo, "worktree", "list", "--porcelain").stdout.count(
            "worktree "
        ) == 1


# ---------------------------------------------------------------------------
# End to end over loopback — a real server, a real socket
# ---------------------------------------------------------------------------


def _post(url: str, body: Any, *, timeout: float = 60.0) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def server(cfg: ForgeConfig):
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    assert host == "127.0.0.1"
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


class TestOverLoopback:
    def test_all_four_routes_answer_over_a_real_socket(self, server: str) -> None:
        status, body = _post(server + CODE_LIST_FILES_ROUTE, {"repo": REPO_KEY})
        assert status == 200 and body["files"] == TRACKED

        status, body = _post(
            server + CODE_READ_FILE_ROUTE,
            {"repo": REPO_KEY, "path": "src/users/models.py"},
        )
        assert status == 200 and body["content"] == MODELS_PY

        status, body = _post(
            server + CODE_IMPORTS_ROUTE,
            {"repo": REPO_KEY, "path": "src/users/models.py"},
        )
        assert status == 200
        assert body["files"][0]["imports"][0]["module"] == "sqlalchemy"

        status, body = _post(
            server + CODE_SEARCH_ROUTE, {"repo": REPO_KEY, "pattern": "created_at"}
        )
        assert status == 200 and body["count"] >= 1

    def test_a_refusal_over_the_socket_is_an_http_400_with_one_sentence(
        self, server: str
    ) -> None:
        status, body = _post(
            server + CODE_READ_FILE_ROUTE, {"repo": REPO_KEY, "path": "escape.txt"}
        )

        assert status == 400
        assert "outside the repository" in body["error"]

    def test_a_null_character_in_a_path_is_a_400_and_not_a_500(
        self, server: str
    ) -> None:
        """Over the wire, which is where this one used to hurt: the raise
        became an HTTP 500 reading "internal error: ValueError" with a stack
        trace in the sidecar's log for every such request."""
        status, body = _post(
            server + CODE_READ_FILE_ROUTE, {"repo": REPO_KEY, "path": "src/\x00.py"}
        )

        assert status == 400, body
        assert "null character" in body["error"]

    def test_a_code_path_that_is_not_one_of_the_four_is_not_a_route(
        self, server: str
    ) -> None:
        for path in (
            "/code/write-file",
            "/code/list-files/extra",
            "/code",
            "/code/run",
        ):
            status, body = _post(server + path, {"repo": REPO_KEY})
            assert status == 404, (path, body)
            assert body["error"] == f"no such path: {path}"


# ---------------------------------------------------------------------------
# A path no operating system can be asked about: the null character
# ---------------------------------------------------------------------------


NULL_PATHS = (
    (process_code_read_file_request, {"path": "src/\x00.py"}),
    (process_code_read_file_request, {"path": "\x00"}),
    (process_code_imports_request, {"path": "src/\x00"}),
    (process_code_list_files_request, {"under": "src\x00"}),
    (process_code_search_request, {"pattern": "get_user", "under": "src\x00"}),
)


class TestANullCharacterInAPath:
    """A null character is not whitespace, not a slash and not an empty
    segment, so the spelling fence lets it through — and looking such a path
    up raises ValueError, which is a different family from the OSError this
    door used to catch. Every route promises in its own docstring that it
    never raises, and a caller that gets a 500 and a stack trace has been told
    nothing it can act on."""

    @pytest.mark.parametrize("handler,extra", NULL_PATHS)
    def test_it_is_refused_in_one_sentence_rather_than_raised(
        self, handler: Any, extra: dict[str, Any], cfg: ForgeConfig
    ) -> None:
        status, body = handler({"repo": REPO_KEY, **extra}, config=cfg)

        assert status == 400, body
        assert "null character" in body["error"]
        assert "send the path exactly as git spells it" in body["error"]


# ---------------------------------------------------------------------------
# A file over the byte cap: served a line range at a time
# ---------------------------------------------------------------------------


BIG_LINES = 20_000
BIG_TEXT = "".join(
    f"line {number} of a file too big to hand back whole\n"
    for number in range(1, BIG_LINES + 1)
)
BIG_PY = "".join(f"value_{number} = {number}\n" for number in range(1, 40_000))


@pytest.fixture
def big_repo(tmp_path: Path) -> Path:
    """A real repository whose tracked files are all over the byte cap."""
    path = tmp_path / "big_repo"
    (path / "docs").mkdir(parents=True)
    (path / "src").mkdir()
    (path / "docs" / "big.txt").write_text(BIG_TEXT, encoding="utf-8")
    # One line of three hundred thousand characters: a real minified file.
    (path / "docs" / "one-line.txt").write_text("y" * 300_000, encoding="utf-8")
    # Over the cap AND binary.
    (path / "docs" / "big.bin").write_bytes(b"\x00" + b"z" * 300_000)
    (path / "src" / "big_module.py").write_text(BIG_PY, encoding="utf-8")

    _git(path, "init", "-q")
    _git(path, "add", "docs", "src")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def big_cfg(big_repo: Path) -> ForgeConfig:
    return _config({REPO_KEY: str(big_repo)})


class TestAFileOverTheByteCap:
    def test_the_refusal_names_a_way_out_and_the_way_out_works(
        self, big_cfg: ForgeConfig
    ) -> None:
        """The whole point of this pair. The refusal used to say "read a part
        of it with 'first_line' and 'last_line'" while the size check ran
        BEFORE any line slicing, so following the advice gave the identical
        refusal and a reader concluded the file could not be read."""
        status, refusal = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "docs/big.txt"}, config=big_cfg
        )

        assert status == 400
        assert "over this door's limit" in refusal["error"]
        assert "'first_line' and 'last_line'" in refusal["error"]

        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "docs/big.txt",
                "first_line": 1,
                "last_line": 10,
            },
            config=big_cfg,
        )

        assert status == 200, body
        assert body["content"] == "".join(
            f"line {number} of a file too big to hand back whole\n"
            for number in range(1, 11)
        )

    def test_a_window_late_in_the_file_is_read_and_says_it_is_a_part(
        self, big_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "docs/big.txt",
                "first_line": 19_998,
                "last_line": 20_000,
            },
            config=big_cfg,
        )

        assert status == 200, body
        assert body["content"] == "".join(
            f"line {number} of a file too big to hand back whole\n"
            for number in (19_998, 19_999, 20_000)
        )
        assert (body["first_line"], body["last_line"]) == (19_998, 20_000)
        assert body["partial"] is True
        assert body["total_lines"] is None, (
            "the rest of the file was never read, so its length is not known"
        )
        assert body["bytes"] == len(BIG_TEXT.encode("utf-8"))
        assert "only the lines you asked for were read" in body["note"]

    def test_a_first_line_alone_reads_to_the_end_of_the_file(
        self, big_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "docs/big.txt", "first_line": 19_999},
            config=big_cfg,
        )

        assert status == 200, body
        assert body["last_line"] == 20_000
        assert body["content"].endswith("line 20000 of a file too big to hand "
                                        "back whole\n")

    def test_a_window_past_the_end_is_refused_not_answered_empty(
        self, big_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "docs/big.txt",
                "first_line": 90_000,
                "last_line": 90_010,
            },
            config=big_cfg,
        )

        assert status == 400
        assert "is past the end of docs/big.txt" in body["error"]

    def test_a_window_bigger_than_the_byte_cap_is_refused_in_one_sentence(
        self, big_cfg: ForgeConfig
    ) -> None:
        """One line of three hundred thousand characters is a whole file's
        worth of text, and the window has to keep the same limit the whole
        file did."""
        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "docs/one-line.txt",
                "first_line": 1,
                "last_line": 1,
            },
            config=big_cfg,
        )

        assert status == 400
        assert "come to more than this door's limit" in body["error"]
        assert "ask for fewer lines" in body["error"]

    def test_an_over_size_binary_file_is_still_refused_as_not_text(
        self, big_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "docs/big.bin", "first_line": 1, "last_line": 2},
            config=big_cfg,
        )

        assert status == 400
        assert "is not a text file" in body["error"]

    def test_the_walk_into_a_big_file_stops_where_the_module_says(
        self, big_cfg: ForgeConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sidecar, "CODE_READ_SCAN_BYTES", 20_000)

        status, body = process_code_read_file_request(
            {
                "repo": REPO_KEY,
                "path": "docs/big.txt",
                "first_line": 19_000,
                "last_line": 19_001,
            },
            config=big_cfg,
        )

        assert status == 400
        assert "reads no further than 20000 bytes" in body["error"]
        assert "ask for earlier lines" in body["error"]

    def test_imports_says_something_true_about_an_over_size_file(
        self, big_cfg: ForgeConfig
    ) -> None:
        """/code/imports has no 'first_line' and no 'last_line', so the
        read-file sentence must never be copied into its answer."""
        status, body = process_code_imports_request(
            {"repo": REPO_KEY, "path": "src/big_module.py"}, config=big_cfg
        )

        assert status == 200, body
        note = body["files"][0]["note"]
        assert "over this door's limit" in note
        assert "its imports were not read" in note
        assert "first_line" not in note
        assert "last_line" not in note

    def test_a_file_inside_the_cap_still_answers_whole_and_says_so(
        self, cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "src/users/router.py"}, config=cfg
        )

        assert status == 200, body
        assert body["partial"] is False
        assert body["note"] is None
        assert body["total_lines"] == 11


# ---------------------------------------------------------------------------
# Line numbers mean what every other tool means by them
# ---------------------------------------------------------------------------


# Line 3 carries a FORM FEED. Python's splitlines() breaks on it; git, grep,
# every editor and every traceback do not.
FORM_FEED_PY = (
    "import os\n"
    "\n"
    "def one():\x0c pass\n"
    "\n"
    "needle_here = 1\n"
)
CRLF_TXT = "alpha\r\nbeta\r\nneedle_crlf\r\n"


@pytest.fixture
def odd_lines_repo(tmp_path: Path) -> Path:
    path = tmp_path / "odd_lines"
    path.mkdir()
    (path / "feed.py").write_text(FORM_FEED_PY, encoding="utf-8", newline="")
    (path / "windows.txt").write_text(CRLF_TXT, encoding="utf-8", newline="")
    _git(path, "init", "-q")
    _git(path, "add", "feed.py", "windows.txt")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def odd_cfg(odd_lines_repo: Path) -> ForgeConfig:
    return _config({REPO_KEY: str(odd_lines_repo)})


class TestLineNumbersAgreeWithEveryOtherTool:
    def test_the_file_really_does_divide_two_ways(self) -> None:
        """The premise, stated once so the rest of this class is readable:
        Python's own splitlines() finds six lines in this five-line file."""
        assert len(FORM_FEED_PY.splitlines()) == 6
        assert FORM_FEED_PY.count("\n") == 5

    def test_read_file_counts_the_lines_the_file_has(
        self, odd_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "feed.py"}, config=odd_cfg
        )

        assert status == 200, body
        assert body["total_lines"] == 5
        assert body["content"] == FORM_FEED_PY

    def test_a_line_range_returns_that_line_of_the_file(
        self, odd_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "feed.py", "first_line": 3, "last_line": 3},
            config=odd_cfg,
        )

        assert status == 200, body
        assert body["content"] == "def one():\x0c pass\n"

    def test_search_reports_the_line_number_grep_reports(
        self, odd_cfg: ForgeConfig, odd_lines_repo: Path
    ) -> None:
        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "needle_here"}, config=odd_cfg
        )

        assert status == 200, body
        assert [(m["path"], m["line"]) for m in body["matches"]] == [("feed.py", 5)]

        # And the same number a real grep gives for the same file — the door
        # never runs grep, but its answer has to agree with one.
        grep = subprocess.run(
            ["grep", "-n", "needle_here", "feed.py"],
            cwd=odd_lines_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert grep.stdout.split(":", 1)[0] == "5"

    def test_a_windows_file_reads_and_searches_as_three_lines(
        self, odd_cfg: ForgeConfig
    ) -> None:
        status, body = process_code_read_file_request(
            {"repo": REPO_KEY, "path": "windows.txt"}, config=odd_cfg
        )
        assert status == 200, body
        assert body["total_lines"] == 3

        status, body = process_code_search_request(
            {"repo": REPO_KEY, "pattern": "needle_crlf"}, config=odd_cfg
        )
        assert status == 200, body
        assert [(m["path"], m["line"], m["text"]) for m in body["matches"]] == [
            ("windows.txt", 3, "needle_crlf")
        ]
