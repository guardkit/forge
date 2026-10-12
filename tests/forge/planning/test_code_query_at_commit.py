"""Ranked code search for the planner, at the run's recorded commit (12 October 2026).

The real helper server over a real clone whose branch and working tree have
moved on; the real ``SidecarCodeReader``; the real descriptor builder. Skipped
unless GuardKitFactory's runner and its pinned programs are installed, because
a stand-in would prove nothing about the programs.

Set ``FORGE_CODE_SEARCH_FIXTURE_OUT`` to a folder to keep the descriptor this
produces; specialist-agent's consumer tests read that file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

code_search = pytest.importorskip("guardkitfactory.code_search")

from forge.config.models import ForgeConfig  # noqa: E402
from forge.deploy_sidecar.service import (  # noqa: E402
    CODE_QUERY_ROUTE,
    build_server,
    process_code_query_request,
)
from forge.planning.code_evidence import ranked_source_field  # noqa: E402
from forge.planning.driver import PlanningRunDriver  # noqa: E402
from forge.planning.repository_facts import RepositoryUnreadable  # noqa: E402
from forge.planning.sidecar_git_runner import SidecarCodeReader  # noqa: E402

_READY, _WHY = code_search.available()
pytestmark = pytest.mark.skipif(not _READY, reason=f"pinned programs not installed: {_WHY}")

REPO_KEY = "guardkit/sample"

ROUTER = (
    "from store import Store\n"
    "\n"
    "\n"
    "def count_active_users(store: Store) -> int:\n"
    "    return sum(1 for user in store.users if user.active)\n"
    "\n"
    "\n"
    "def deactivate_user(store: Store, user_id: int) -> None:\n"
    "    store.users[user_id].active = False\n"
)
TEST = (
    "from router import count_active_users\n"
    "\n"
    "\n"
    "def test_count_active_users_skips_inactive_ones():\n"
    "    assert count_active_users(FakeStore()) == 0\n"
)


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _private_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "snapshots"
    monkeypatch.setenv("FORGE_CODE_SNAPSHOT_ROOT", str(root))
    return root


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str]:
    """A clone whose recorded commit has the router and its test, and whose
    branch and working tree have since moved on."""
    seed = tmp_path / "seed"
    (seed / "app").mkdir(parents=True)
    (seed / "tests" / "users").mkdir(parents=True)
    (seed / "app" / "router.py").write_text(ROUTER)
    (seed / "tests" / "users" / "test_router.py").write_text(TEST)
    # Tracked, would match, and too large for the snapshot.
    (seed / "app" / "huge_report.py").write_text(
        "# count active users report\n" + "x = 1\n" * 60_000
    )
    (seed / "app" / "alias.py").symlink_to("router.py")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "recorded")
    recorded = _git(seed, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(seed), str(clone))
    # Later: the branch moves on and the working tree is edited.
    (clone / "app" / "router.py").write_text("def later_rewrite():\n    pass\n")
    _git(clone, "commit", "-qam", "later")
    (clone / "app" / "local_only.py").write_text("def count_active_users_local():\n    pass\n")
    return clone, recorded


def _cfg(clone: Path) -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "planning": {"target_repo_paths": {REPO_KEY: str(clone)}},
        }
    )


@contextlib.contextmanager
def _serving(clone: Path):
    cfg = _cfg(clone)
    srv = build_server(port=0, config_loader=lambda: cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_query_needs_a_recorded_commit(clone: tuple[Path, str]) -> None:
    path, _ = clone
    status, body = process_code_query_request(
        {"repo": REPO_KEY, "operation": "search", "query": "count"}, config=_cfg(path)
    )
    assert status == 400 and "commit" in body["error"]


def test_unknown_operation_is_refused(clone: tuple[Path, str]) -> None:
    path, recorded = clone
    status, body = process_code_query_request(
        {"repo": REPO_KEY, "commit": recorded, "operation": "rewrite"}, config=_cfg(path)
    )
    assert status == 400


def test_answers_come_from_the_recorded_commit_only(clone: tuple[Path, str]) -> None:
    path, recorded = clone
    status, body = process_code_query_request(
        {"repo": REPO_KEY, "commit": recorded, "operation": "search", "mode": "ranked",
         "query": "count active users"},
        config=_cfg(path),
    )
    assert status == 200, body
    assert body["commit"] == recorded and body["source"]["commit"] == recorded
    paths = {r["path"] for r in body["results"]}
    assert "app/router.py" in paths
    assert "tests/users/test_router.py" in paths  # tests included
    assert "app/local_only.py" not in paths  # the working tree never answers
    texts = " ".join(r["text"] for r in body["results"])
    assert "later_rewrite" not in texts  # the branch moved on; the commit did not
    # The oversized tracked file and the link were left out, and said so.
    omitted = {o["path"]: o["reason"] for o in body["source"]["omitted"]}
    assert omitted["app/huge_report.py"].startswith("larger than")
    assert omitted["app/alias.py"] == "symbolic link"
    assert body["status"] == "partial"
    assert any("left out of the commit snapshot" in r for r in body["truncation"])


def test_no_match_with_an_omission_in_scope_is_partial(clone: tuple[Path, str]) -> None:
    path, recorded = clone
    status, body = process_code_query_request(
        {"repo": REPO_KEY, "commit": recorded, "operation": "search", "mode": "literal",
         "query": "zzqq_nowhere", "path": "app"},
        config=_cfg(path),
    )
    assert status == 200 and body["status"] == "partial" and body["results"] == []
    status, body = process_code_query_request(
        {"repo": REPO_KEY, "commit": recorded, "operation": "search", "mode": "literal",
         "query": "zzqq_nowhere", "path": "tests"},
        config=_cfg(path),
    )
    assert status == 200 and body["status"] == "no_matches"


def test_snapshot_is_reused_and_read_only(clone: tuple[Path, str],
                                          _private_snapshots: Path) -> None:
    path, recorded = clone
    for _ in range(2):
        process_code_query_request(
            {"repo": REPO_KEY, "commit": recorded, "operation": "outline",
             "path": "app/router.py"},
            config=_cfg(path),
        )
    folders = [p for p in _private_snapshots.iterdir() if p.is_dir()]
    assert [p.name for p in folders] == [recorded]
    assert not os.access(folders[0] / "app" / "router.py", os.W_OK) or os.geteuid() == 0
    # Nothing was written into the clone.
    assert _git(path, "status", "--porcelain") == "?? app/local_only.py"


def test_reader_and_descriptor_over_the_real_server(clone: tuple[Path, str]) -> None:
    path, recorded = clone
    with _serving(path) as url:
        reader = SidecarCodeReader(url, repo=REPO_KEY, commit=recorded)
        answer = reader.ranked_source("count active users")
        assert answer["commit"] == recorded and answer["status"] == "partial"
        descriptor = PlanningRunDriver._build_target_repo_descriptor(
            REPO_KEY, str(path), "Feature: count active users",
            reader=SidecarCodeReader(url, repo=REPO_KEY, commit=recorded),
            request_text="Add an endpoint that counts the active users",
            ranked_source=True,
        )
        without = PlanningRunDriver._build_target_repo_descriptor(
            REPO_KEY, str(path), "Feature: count active users",
            reader=SidecarCodeReader(url, repo=REPO_KEY, commit=recorded),
            request_text="Add an endpoint that counts the active users",
        )
    field = descriptor["ranked_source"]
    assert field["commit"] == recorded
    assert field["status"] == "partial"
    blocks = {b["path"]: b for b in field["blocks"]}
    router = blocks["app/router.py"]
    assert router["text"].splitlines()[0].startswith(f"{router['first_line']}: ")
    assert "count_active_users" in router["text"] and router["test"] is False
    assert blocks["tests/users/test_router.py"]["test"] is True
    # Off, the descriptor is exactly what it was before the switch existed.
    assert "ranked_source" not in without
    assert {k: v for k, v in descriptor.items() if k != "ranked_source"} == without
    out = os.environ.get("FORGE_CODE_SEARCH_FIXTURE_OUT")
    if out:
        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        data = json.dumps(descriptor, indent=2, sort_keys=True)
        (folder / "ranked-source-descriptor.json").write_text(data)
        (folder / "ranked-source-descriptor.json.sha256").write_text(
            hashlib.sha256(data.encode()).hexdigest() + "\n"
        )


def test_reader_without_a_commit_never_searches_the_working_tree(
    clone: tuple[Path, str]
) -> None:
    path, _ = clone
    with _serving(path) as url:
        with pytest.raises(RepositoryUnreadable, match="recorded starting commit"):
            SidecarCodeReader(url, repo=REPO_KEY).ranked_source("count")


def test_an_older_helper_without_the_route_is_said_plainly() -> None:
    def post(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        return 404, {"error": f"no such path: {CODE_QUERY_ROUTE}"}

    reader = SidecarCodeReader("http://helper", repo=REPO_KEY, commit="a" * 40, post=post)
    field = PlanningRunDriver._ranked_source_for(reader, "count", test_roots=[], shown=[])
    assert field is not None and field["status"] == "tool_error" and field["blocks"] == []
    assert "could not run" in field["note"]


class _Lines:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


def test_field_numbering_dedupe_tests_and_budget() -> None:
    files = {"a.py": "".join(f"line {n}\n" for n in range(1, 101)),
             "tests/t.py": "def test():\n    pass\n"}
    answer = {
        "status": "ok", "truncation": [], "coverage": {}, "note": "",
        "tool": {"name": "probe", "version": "0.6.0-rc341"},
        "query": {"words": ["line"]},
        "source": {"commit": "c" * 40, "files": 2, "omitted_count": 0},
        "results": [
            {"path": "a.py", "first_line": 10, "last_line": 12, "score": 3.0},
            {"path": "a.py", "first_line": 20, "last_line": 90, "score": 2.0},
            {"path": "tests/t.py", "first_line": 1, "last_line": 2, "score": 1.0},
            {"path": "gone.py", "first_line": 1, "last_line": 2, "score": 0.5},
        ],
    }
    field = ranked_source_field(
        answer, reader=_Lines(files), test_roots=["tests"],
        shown_windows=[{"path": "a.py", "first_line": 5, "last_line": 15}],
    )
    assert [b["path"] for b in field["blocks"]] == ["a.py", "tests/t.py"]
    long_block = field["blocks"][0]
    assert long_block["first_line"] == 20 and long_block["last_line"] == 59
    assert long_block["block_continues_to"] == 90
    assert long_block["text"].splitlines()[0] == "20: line 20"
    assert field["blocks"][1]["test"] is True
    assert field["coverage"]["blocks_already_shown_as_windows"] == 1
    assert field["status"] == "partial"  # gone.py could not be read at the commit
    tight = ranked_source_field(answer, reader=_Lines(files), test_roots=[], budget=50)
    assert tight["status"] == "partial"
    assert any("to fit 50 characters" in reason for reason in tight["truncation"])
