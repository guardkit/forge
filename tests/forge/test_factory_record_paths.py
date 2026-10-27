"""The factory's own record paths: the one list, pinned (design test 27).

Design: "Factory records never enter a project's commits" (14 October 2026).
GuardKit writes records about a build inside the build's working folder. The
list of where they live is written once in the design and copied literally
into GuardKit and into Forge, because Forge does not import GuardKit. This
file pins Forge's copy, and checks the small matcher the planning commit uses
(``is_factory_record``) against git's own pathspec matching for a fixed set of
paths, so the two can never disagree about what a record is.

Everything here is real git in a temporary folder. Nothing names a language.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge.factory_files import (
    FACTORY_RECORD_PATHS,
    factory_record_content_pathspecs,
    factory_record_exclusions,
    factory_record_pathspecs,
    is_factory_record,
)
from forge.project_git import PatternPathspec

#: The eight paths, exactly as the design writes them.
THE_EIGHT = (
    "**/.guardkit/autobuild/**",
    "**/.guardkit/autobuild-private/**",
    "**/.guardkit/qav-shadow/**",
    "**/.guardkit/memory-query-log.jsonl*",
    "**/.guardkit/worktrees/**",
    "**/.guardkit/bootstrap_state.json",
    "conversation_history/**",
    "**/large_tool_results/**",
)

#: Each folder path's own name (``x/**`` gives ``x``): a symbolic link of that
#: name is a record too (GuardKit e6bd14d2).
BARE_NAMES = tuple(p[:-3] for p in THE_EIGHT if p.endswith("/**"))

#: GuardKit's lists and its matcher's answers, pinned from the commit named in
#: the file (Forge does not import GuardKit).
PINNED = Path(__file__).with_name("guardkit_factory_records.json")

#: Paths a project could hold. Some are records, most are not.
PATHS = (
    ".guardkit/autobuild/x.json",
    ".guardkit/autobuild/baseline.json",
    ".guardkit/autobuild/TASK-1/player_turn_1.json",
    ".guardkit/autobuild/TASK-1/_rollback_archive/turn_2/a.json",
    "services/api/.guardkit/autobuild/x.json",
    ".guardkit/autobuild-private/judge.json",
    ".guardkit/qav-shadow/queue.jsonl",
    ".guardkit/memory-query-log.jsonl",
    ".guardkit/memory-query-log.jsonl.1",
    "pkg/.guardkit/memory-query-log.jsonl",
    ".guardkit/worktrees/FEAT-1/inner.txt",
    ".guardkit/bootstrap_state.json",
    "deep/er/.guardkit/bootstrap_state.json",
    "conversation_history/a.md",
    "conversation_history/sub/b.md",
    "large_tool_results/r.txt",
    "a/b/large_tool_results/r.txt",
    # Not records.
    "src/conversation_history/k.go",
    ".guardkit/features/F.yaml",
    ".guardkit/config.yaml",
    ".guardkit/seam-checks.yaml",
    ".guardkit/bdd/a.feature",
    ".guardkit/autobuildx/y.json",
    ".guardkit/memory-query-log.json",
    "x.guardkit/autobuild/z.json",
    ".guardkitx/autobuild/z.json",
    "docs/state/notes.md",
    "tasks/backlog/TASK-1.md",
    "notes/old.json",
    "README",
    "large_tool_results.txt",
    "conversation_history.md",
    # Folder names on their own (as a file or a link), at some depth.
    "b1/.guardkit/autobuild",
    "b2/.guardkit/autobuild-private",
    "b3/.guardkit/qav-shadow",
    "b4/.guardkit/worktrees",
    "b5/large_tool_results",
    "src/conversation_history.txt",
    # A name with a line break in it (coach review 1, F2).
    "we\nird/.guardkit/autobuild/x.json",
    "line\nbreak/notes.md",
)


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(where),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_the_list_is_the_designs_eight_paths_literally() -> None:
    assert tuple(FACTORY_RECORD_PATHS) == THE_EIGHT


def test_the_pathspec_helpers_are_marked_as_patterns() -> None:
    exclusions = factory_record_exclusions()
    pathspecs = factory_record_pathspecs()
    # Paths 1 to 6 and 8 are left out of the add; path 7 never is, because
    # naming it there makes git refuse the add in a project that ignores it.
    assert list(exclusions) == [
        f":(exclude,glob){p}"
        for p in (*THE_EIGHT, *BARE_NAMES)
        if not p.startswith("conversation_history")
    ]
    assert list(pathspecs) == [f":(glob){p}" for p in (*THE_EIGHT, *BARE_NAMES)]
    assert all(isinstance(p, PatternPathspec) for p in (*exclusions, *pathspecs))


def test_the_matcher_agrees_with_gits_own_pathspec_matching(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for path in PATHS:
        where = repo / path
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(f"{path}\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    # A nested build folder recorded as an embedded repository (a gitlink).
    blob = "0" * 39 + "1"
    _git(
        repo, "update-index", "--add", "--cacheinfo",
        f"160000,{blob},.guardkit/worktrees/FEAT-2",
    )
    every = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    assert ".guardkit/worktrees/FEAT-2" in every

    matched_by_git: set[str] = set()
    for pattern in (*THE_EIGHT, *BARE_NAMES):
        listed = _git(repo, "ls-files", "-z", "--", f":(glob){pattern}")
        matched_by_git.update(p for p in listed.split("\0") if p)

    matched_here = {p for p in every if is_factory_record(p)}
    assert matched_here == matched_by_git
    # And the fixed set really exercises both sides.
    assert "src/conversation_history/k.go" not in matched_here
    assert ".guardkit/worktrees/FEAT-2" in matched_here
    assert ".guardkit/features/F.yaml" not in matched_here
    assert "we\nird/.guardkit/autobuild/x.json" in matched_here
    assert "b5/large_tool_results" in matched_here


def test_the_list_matches_guardkits_pinned_copy() -> None:
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))
    assert pinned["source"] == "guardkit/orchestrator/worktree_checkpoints.py"
    assert len(pinned["guardkit_commit"]) == 40
    assert list(FACTORY_RECORD_PATHS) == pinned["FACTORY_RECORD_PATHS"]
    # GuardKit's add-time exclusions of its records are Forge's, in any order.
    assert sorted(factory_record_exclusions()) == pinned["record_exclusions"]
    # And so is where record content is looked for.
    assert sorted(factory_record_content_pathspecs()) == pinned["record_content_pathspecs"]


def test_the_matcher_gives_guardkits_answers() -> None:
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))["is_factory_record"]
    assert len(pinned) > 30
    differ = {p: want for p, want in pinned.items() if is_factory_record(p) is not want}
    assert differ == {}


def test_against_guardkit_itself_when_it_is_installed() -> None:
    checkpoints = pytest.importorskip("guardkit.orchestrator.worktree_checkpoints")
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))["is_factory_record"]
    assert tuple(checkpoints.FACTORY_RECORD_PATHS) == FACTORY_RECORD_PATHS
    for path in pinned:
        assert checkpoints.is_factory_record(path) is is_factory_record(path), path


@pytest.mark.parametrize(
    "path, expected",
    [
        (".guardkit/autobuild/x.json", True),
        ("conversation_history/x.md", True),
        ("src/conversation_history/k.go", False),
        (".guardkit/features/G.yaml", False),
        (".guardkit/autobuild", True),
        ("conversation_history", True),
        ("a/large_tool_results", True),
        ("we\nird/.guardkit/autobuild/x.json", True),
        ("", False),
    ],
)
def test_a_few_answers_spelled_out(path: str, expected: bool) -> None:
    assert is_factory_record(path) is expected
