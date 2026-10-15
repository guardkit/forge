"""What the merge card says about the files that judge the merge.

Design: "The merge check uses main's copy of a check script the build changed"
(15 October 2026), Forge item 6, with review rounds 1 to 4.

A judge file is a file a declared install or check command names directly
(``scripts/check.sh`` in ``bash scripts/check.sh``). The build system's merge
reads the declaration on the commit it joins onto and checks the joined code
with that commit's copies of the judge files, never the build's. This module
reads the same declaration on the same commit, so the merge card can say so
before anyone approves:

* **the commit** is the one the merge and the preview join onto: the preview's
  base commit, or the target branch fetched afresh. The clone's local branch,
  which nothing moves forward, is never read;
* **the declaration** is read at that commit through the commit-based reader
  and handed to GuardKit's own ``read_merge_declarations`` and
  ``pin_merge_environment``. Forge forms no second opinion of what the
  project declared;
* **the changed judge files** are GuardKit's ``judge_files`` applied to every
  path the build's ``name_status`` names, added, deleted and renamed ones
  included. With a preview, a judge file the target renamed is also listed
  when the joined tree's copy of it differs from the target's;
* **links** are found on their own: GuardKit's ``judge_link_paths`` names every
  file the declared commands name and every folder on its path, and each is
  looked up, component by component, with the reader's mode answer in the
  target tree, the candidate tree and the preview's joined tree. A link
  anywhere there means the merge will not verify; a tree that cannot be read
  is said plainly.

Nothing here names a language or a stack: the paths are whatever git lists and
the declared commands name.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_PATH",
    "guardkit_rules",
    "read_the_merge_check",
]

#: Where a project declares its commands, relative to its top folder.
CONFIG_PATH = ".guardkit/config.yaml"

#: The modes a commit's tree gives a file whose content can be compared.
_BLOB_MODES = frozenset({"100644", "100755"})
_LINK_MODE = "120000"
_EXECUTABLE_MODE = "100755"

#: The check time limit handed to GuardKit's pin. It only caps the install's
#: own time limit, which the card never reads.
_PIN_TIMEOUT_S = 3600


def guardkit_rules() -> Any | None:
    """GuardKit's own rules, or ``None`` when GuardKit is not importable here.

    Imported by name, never as a module object, so nothing here can call
    anything else of GuardKit's.
    """
    try:
        from guardkit.orchestrator.merge_environment import (  # type: ignore[import-not-found]
            judge_files,
            judge_link_paths,
            judge_words,
            pin_merge_environment,
            read_merge_declarations,
        )
    except Exception as exc:  # noqa: BLE001 — a missing rule is said on the card
        logger.warning(
            "merge card: GuardKit's merge rules are not importable here (%s: %s); "
            "the card says which files judge the merge could not be read",
            type(exc).__name__,
            exc,
        )
        return None
    return SimpleNamespace(
        read_merge_declarations=read_merge_declarations,
        pin_merge_environment=pin_merge_environment,
        judge_words=judge_words,
        judge_files=judge_files,
        judge_link_paths=judge_link_paths,
    )


def _not_read(declared_at: str | None, reason: str) -> dict[str, Any]:
    logger.info("merge card: the merge check could not be read (%s)", reason)
    return {
        "declared_at": declared_at,
        "read": False,
        "reason": reason,
        "judge_files": [],
        "config_changed": False,
        "link_unreadable": [],
    }


def _paths_of(changes: Iterable[Any]) -> list[str]:
    """Every path the build's changes name, a rename's old path included."""
    paths: list[str] = []
    for change in changes:
        for path in (getattr(change, "old_path", None), getattr(change, "path", None)):
            text = str(path or "")
            if text and text not in paths:
                paths.append(text)
    return paths


def _how(changes: Sequence[Any], path: str) -> str:
    """``added``, ``deleted`` or ``changed`` for one path of the build's changes."""
    for change in changes:
        status = str(getattr(change, "status", "") or "").upper()[:1]
        if getattr(change, "old_path", None) == path and status == "R":
            return "deleted"
        if getattr(change, "path", None) == path:
            if status in ("A", "C") or (status == "R"):
                return "added"
            if status == "D":
                return "deleted"
            return "changed"
    return "changed"


async def _mode(reader: Any, repo_path: str, commit: str, path: str) -> tuple[str | None, str | None]:
    """``(mode, refusal)`` of one path at one commit. A path that is not
    there answers ``(None, None)``."""
    answer = await reader.read_file_at_commit(repo_path, commit, path, mode_only=True)
    if not answer.ok:
        return None, str(answer.refusal or "the read was refused")
    return (str(answer.mode) if answer.mode else None), None


async def read_the_merge_check(
    *,
    reader: Any,
    rules: Any | None,
    repo_path: str,
    declared_at: str | None,
    declared_at_refusal: str | None = None,
    candidate: str,
    joined: str | None = None,
    joined_expected: bool = False,
    changes: Sequence[Any] | None,
) -> dict[str, Any]:
    """The ``merge_check_reading`` record for one offer. Never raises.

    ``joined`` is the preview's joined commit (a ref) when there is one;
    ``joined_expected`` says a preview was joined, so a missing ``joined`` is
    unreadable evidence rather than "no joined tree". ``changes`` are the
    build's changes as the scope pass read them (``None`` when they could not
    be read).
    """
    try:
        return await _read(
            reader=reader,
            rules=rules,
            repo_path=repo_path,
            declared_at=declared_at,
            declared_at_refusal=declared_at_refusal,
            candidate=candidate,
            joined=joined,
            joined_expected=joined_expected,
            changes=changes,
        )
    except Exception as exc:  # noqa: BLE001 — a reading never stops a card
        logger.warning(
            "merge card: reading the merge check raised (%s: %s)", type(exc).__name__, exc
        )
        return _not_read(declared_at, f"the reading raised {type(exc).__name__}")


async def _read(
    *,
    reader: Any,
    rules: Any | None,
    repo_path: str,
    declared_at: str | None,
    declared_at_refusal: str | None,
    candidate: str,
    joined: str | None,
    joined_expected: bool,
    changes: Sequence[Any] | None,
) -> dict[str, Any]:
    if not declared_at:
        return _not_read(
            None, declared_at_refusal or "there is no target commit to read the declaration at"
        )
    if rules is None:
        return _not_read(declared_at, "GuardKit's merge rules are not importable here")
    if reader is None:
        return _not_read(declared_at, "there is no reader for this project's commits")

    # -- the declaration, at exactly that commit, judged by GuardKit --------
    declared = await reader.read_file_at_commit(
        repo_path, declared_at, CONFIG_PATH, ordinary_file_only=True
    )
    if not declared.ok:
        return _not_read(declared_at, str(declared.refusal or "the read was refused"))
    with tempfile.TemporaryDirectory(prefix="forge-merge-check-") as folder:
        if declared.found:
            where = Path(folder) / CONFIG_PATH
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_text(str(declared.content or ""), encoding="utf-8")
        declarations, problem = rules.read_merge_declarations(Path(folder))
    if problem is not None or declarations is None:
        return _not_read(declared_at, str(problem or "the declaration could not be read"))
    test_command = getattr(declarations, "declared_test", None)
    pinned, problem = rules.pin_merge_environment(declarations, test_command, _PIN_TIMEOUT_S)
    if problem is not None or pinned is None:
        # The merge will not run anything either; it says so itself, and the
        # card's existing reporting is unchanged.
        return {
            "declared_at": declared_at,
            "read": True,
            "reason": None,
            "pinned": False,
            "pin_problem": str(problem or ""),
            "judge_files": [],
            "config_changed": False,
            "link_unreadable": [],
        }
    words = tuple(getattr(pinned, "judge_words", ()) or ())
    test_words: tuple[str, ...] = ()
    if test_command:
        said, _refusal = rules.judge_words(test_command, through_shell=True)
        test_words = tuple(said or ())
    if changes is None:
        return _not_read(declared_at, "the files this build changed could not be read")

    judges: list[dict[str, Any]] = []

    async def role_of(path: str, how: str) -> str:
        if test_words and rules.judge_files([path], test_words):
            return "check"
        at = candidate if how == "added" else declared_at
        mode, _refusal = await _mode(reader, repo_path, at, path)
        return "install" if mode == _EXECUTABLE_MODE else "install-list"

    # -- the build's changed judge files ------------------------------------
    paths = _paths_of(changes)
    for path in rules.judge_files(paths, words) if (paths and words) else []:
        how = _how(changes, path)
        judges.append({"path": path, "how": how, "role": await role_of(path, how)})

    # -- links, found on their own, in every tree ----------------------------
    unreadable: list[dict[str, str]] = []
    trees: list[tuple[str, str]] = [(declared_at, declared_at), (candidate, candidate)]
    if joined:
        trees.append((joined, "the preview's joined commit"))
    link_paths = list(rules.judge_link_paths(words)) if words else []
    if joined_expected and not joined and link_paths:
        unreadable.append({"path": link_paths[-1], "at": "the preview's joined commit"})
    links: list[str] = []
    for commit, shown in trees:
        for path in link_paths:
            mode, refusal = await _mode(reader, repo_path, commit, path)
            if refusal is not None:
                if not any(u["path"] == path for u in unreadable):
                    unreadable.append({"path": path, "at": shown})
                continue
            if mode == _LINK_MODE and path not in links:
                links.append(path)
    for path in links:
        judges = [j for j in judges if j["path"] != path]
        judges.append({"path": path, "how": "link", "role": "link"})

    # -- a judge the target renamed, whose joined copy differs ---------------
    if joined:
        listed = {j["path"] for j in judges}
        for path in link_paths:
            if path in listed or path in links:
                continue
            mode, _refusal = await _mode(reader, repo_path, declared_at, path)
            if mode not in _BLOB_MODES or not rules.judge_files([path], words):
                continue
            mains = await reader.read_file_at_commit(repo_path, declared_at, path, raw=True)
            joins = await reader.read_file_at_commit(repo_path, joined, path, raw=True)
            if not (mains.ok and joins.ok):
                unreadable.append({"path": path, "at": "the preview's joined commit"})
                continue
            if (mains.found, mains.content) != (joins.found, joins.content):
                judges.append(
                    {"path": path, "how": "changed", "role": await role_of(path, "changed")}
                )

    # The card names the first of these: a file's own path before a bare
    # word (a program's name such as ``bash``) or a folder on the way to it.
    every = [u["path"] for u in unreadable]

    def specific_first(item: tuple[int, dict[str, str]]) -> tuple[int, int]:
        index, entry = item
        path = entry["path"]
        leaf = "/" in path and not any(o.startswith(path + "/") for o in every)
        return (0 if leaf else 1, index)

    unreadable = [entry for _i, entry in sorted(enumerate(unreadable), key=specific_first)]
    return {
        "declared_at": declared_at,
        "read": True,
        "reason": None,
        "judge_files": judges,
        "config_changed": CONFIG_PATH in paths,
        "link_unreadable": unreadable,
    }
