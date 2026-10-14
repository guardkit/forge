"""The files the factory itself places in a project's tree.

Neutral ground (7 October 2026) for the two places that need the list: the
command-line package that ships the scripts (``forge.cli.deploy_templates``)
and the planner, whose code evidence and "all the X" candidates must never
offer the factory's own files as the project's code. A live run offered the
factory's sandbox runner, which the upgrade procedure copies into the
sandbox's clone and which mentions a "counter", as a member of "all the
count endpoints".

A file counts as the factory's only where the factory puts it AND when it
reads as the factory's script: a project's own ``tools/sandbox-runner.sh``,
or a ``deploy/sandbox-runner.sh`` the project wrote itself, stays the
project's file.

THE FACTORY'S RECORDS (14 October 2026, design "Factory records never enter a
project's commits"). GuardKit, and the coding model's own libraries, write
records about a build inside the build's working folder: what the model said
on each turn, what the judge decided, the test baseline, the query log, inner
build folders. Nothing reads them back from git; they reach the receipts
folder from disk. :data:`FACTORY_RECORD_PATHS` is where they live. Forge's
planning commit leaves them out (:func:`forge.adapters.git.operations.prepare_index`)
and the publisher refuses to send a commit that brings one in
(:mod:`forge.publisher.git_work`). GuardKit holds a literal copy of the same
list (``guardkit/orchestrator/worktree_checkpoints.py``), because Forge does
not import GuardKit; a test in each repository pins its copy. A new GuardKit
writer inside a working folder has to be added to both lists.

This module stays standard-library only, so the publisher can import it.
"""

from __future__ import annotations

import re

__all__ = [
    "FACTORY_RECORD_PATHS",
    "SHIPPED_SCRIPTS",
    "factory_record_exclusions",
    "factory_record_pathspecs",
    "is_factory_record",
    "is_shipped_script",
    "shipped_script_path",
]

#: The factory's own record paths, as git glob patterns relative to the top
#: of the repository. Written once in the design; GuardKit holds the same
#: eight, in the same order.
FACTORY_RECORD_PATHS: tuple[str, ...] = (
    # 1. Every per-task and per-feature record, including head-identity.jsonl.
    "**/.guardkit/autobuild/**",
    # 2. Judge evidence and the feature-check record.
    "**/.guardkit/autobuild-private/**",
    # 3. The shadow judge's queue.
    "**/.guardkit/qav-shadow/**",
    # 4. GuardKit's memory query log and its rotated copy.
    "**/.guardkit/memory-query-log.jsonl*",
    # 5. GuardKit's inner build folders (embedded repositories if committed).
    "**/.guardkit/worktrees/**",
    # 6. Setup state holding a machine-local interpreter path.
    "**/.guardkit/bootstrap_state.json",
    # 7. The coding model's transcripts, at the top of the folder only.
    "conversation_history/**",
    # 8. The coding model's oversized tool output.
    "**/large_tool_results/**",
)

#: The one path never left out of ``git add`` itself: naming a top-level
#: folder there makes git refuse the whole add in a project whose ignore file
#: covers it. It is taken out of what is staged afterwards, like the rest.
_NOT_AT_ADD_TIME = "conversation_history/**"


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """git's glob pathspec rules (wildmatch with ``WM_PATHNAME``) for the
    shapes the list uses: ``**/`` at the start matches any number of leading
    folders (none included), ``/**`` at the end matches everything inside,
    ``/**/`` in the middle matches any number of folders, ``*`` and ``?``
    never cross a ``/``."""
    parts = pattern.split("/")
    out: list[str] = []
    last = len(parts) - 1
    for index, part in enumerate(parts):
        if part == "**":
            if index == last:
                out.append(".+")
            else:
                out.append("(?:.*/)?")
            continue
        piece = "".join(
            "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
            for ch in part
        )
        out.append(piece if index == last else piece + "/")
    return re.compile("".join(out))


_RECORD_MATCHERS: tuple["re.Pattern[str]", ...] = tuple(
    _glob_to_regex(p) for p in FACTORY_RECORD_PATHS
)


def is_factory_record(path: str) -> bool:
    """True when ``path``, as git prints it (relative to the top of the
    repository, ``/``-separated), lies under one of the record paths.

    The same answer git's own ``:(glob)`` pathspec matching gives for the
    list; a test pins the two together."""
    text = str(path or "")
    return bool(text) and any(m.fullmatch(text) for m in _RECORD_MATCHERS)


def factory_record_exclusions() -> tuple[str, ...]:
    """Record paths 1 to 6 and 8 as ``:(exclude,glob)`` pathspecs for
    ``git add``, marked as patterns for the factory's git helper (which
    otherwise takes every name literally)."""
    from forge.project_git import pattern_pathspec

    return tuple(
        pattern_pathspec(f":(exclude,glob){p}")
        for p in FACTORY_RECORD_PATHS
        if p != _NOT_AT_ADD_TIME
    )


def factory_record_pathspecs() -> tuple[str, ...]:
    """All eight record paths as ``:(glob)`` pathspecs, marked as patterns."""
    from forge.project_git import pattern_pathspec

    return tuple(pattern_pathspec(f":(glob){p}") for p in FACTORY_RECORD_PATHS)

#: Each script the factory ships for a project's sandbox, by file name, and
#: the line its header carries in every version shipped so far (its third
#: line; the first two are the interpreter line and a bare ``#``). The
#: scripts are placed at ``deploy/<name>`` (the one-page upgrade procedure,
#: and :mod:`forge.launch_environment`).
SHIPPED_SCRIPTS: dict[str, str] = {
    "sandbox-deploy.sh": "# The sandbox deploy wrapper — the one script the factory's deploy step runs for",
    "sandbox-runner.sh": "# The sandbox runner bootstrap — the one script that runs INSIDE a repository's",
}

#: The folder in a project the scripts are placed in.
SHIPPED_SCRIPTS_FOLDER = "deploy"

#: How many of a file's first lines are searched for the header.
_HEADER_LINES = 5


def shipped_script_path(path: str) -> str | None:
    """The shipped script's name when ``path`` is where the factory places
    one (``deploy/<name>``), else ``None``."""
    folder, _, name = str(path).rpartition("/")
    if folder == SHIPPED_SCRIPTS_FOLDER and name in SHIPPED_SCRIPTS:
        return name
    return None


def is_shipped_script(path: str, text: str | None) -> bool:
    """True when ``path`` is where the factory places a script AND ``text``
    (the file's content) carries that script's header in its first lines.
    Unread text (``None``) is never taken to be the factory's."""
    name = shipped_script_path(path)
    if name is None or text is None:
        return False
    header = SHIPPED_SCRIPTS[name]
    return any(line.rstrip() == header for line in text.split("\n")[:_HEADER_LINES])
