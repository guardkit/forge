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
  when the joined tree's copy of it differs from the target's, and a judge
  file only the joined tree has is listed as added;
* **what the merge does with each one** comes only from the target commit's
  whole-tree listing, the same thing GuardKit's merge reads (review R3):
  on the target means the merge uses main's copy (``on_target`` true), not
  on the target means the merge leaves it out (``on_target`` false), and an
  unreadable listing means the card cannot say (``on_target`` ``None``).
  What the build did to the file (``how``) is only why the line appears;
* **links** are found on their own, with GuardKit's exact merge rule on the
  same inputs (coach review 1, 15 October 2026): the whole tree of the target
  commit, the candidate commit and the preview's joined commit is listed
  (names, modes, types and object ids, never any file's content), and each
  listing is handed to GuardKit's ``links_on_judge_paths`` with the words
  ``pin_merge_environment`` pinned, exactly as the merge does. No path is
  looked up on its own. Link findings never depend on the build's changes. A
  listing that cannot be read is said in one plain sentence naming the commit.

Nothing here names a language or a stack: the paths are whatever git lists and
the declared commands name.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_PATH",
    "guardkit_rules",
    "read_the_merge_check",
    "EXACT_SUFFIX",
    "REPORT_NAME_LISTS",
    "card_body",
    "names_for_record",
    "names_from_record",
    "shown_path",
    "shown_report",
    "shown_sentence",
]

#: Where a project declares its commands, relative to its top folder.
CONFIG_PATH = ".guardkit/config.yaml"


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
            judge_words,
            links_on_judge_paths,
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
        links_on_judge_paths=links_on_judge_paths,
    )


#: The control characters with a short name of their own.
_NAMED_CONTROL = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def shown_path(path: Any) -> str:
    """A file name, or the build system's words, as a person reads them.

    The same rule as GuardKit's own ``shown_path`` (coach review 3), so the
    card, the record, the press and the build system's receipt show a name
    alike:

    * a byte that is not UTF-8 (kept by ``os.fsdecode`` as a lone surrogate,
      review R2) is shown as ``\\xNN``;
    * a line break, carriage return or tab as ``\\n``, ``\\r`` or ``\\t``,
      and any other control character (C0, C1, DEL) as ``\\xNN``;
    * a line or paragraph separator, an invisible format character (a
      right-to-left override, a zero-width space, a byte-order mark: Unicode
      category Cf) or any other lone surrogate as ``\\uNNNN``.

    So a name can never add a line, hide words, or make a card line read in
    another order. Every other character is unchanged, and showing shown text
    again changes nothing.
    """
    out: list[str] = []
    for char in str(path):
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif char in _NAMED_CONTROL:
            out.append(_NAMED_CONTROL[char])
        else:
            kind = unicodedata.category(char)
            if kind == "Cc":
                out.append(f"\\x{code:02x}")
            elif kind in ("Zl", "Zp", "Cs", "Cf"):
                out.append(f"\\u{code:04x}")
            else:
                out.append(char)
    return "".join(out)


#: Line breaks of every kind. In a sentence the build system wrote they are
#: read as the spaces they stand for, so its words stay one line.
_LINE_BREAKS = re.compile("[\n\r\t\x0b\x0c\x85\u2028\u2029]+")

#: The fields of the build system's reports that are lists of file names.
#: Their names keep the backslash form; every other string is a sentence.
REPORT_NAME_LISTS = frozenset(
    {"judged_with_main_copies", "judge_files_not_on_target", "conflict_files"}
)


def shown_sentence(text: Any) -> str:
    """A sentence the build system wrote, as one line a person reads: its
    line breaks become spaces, and everything else is shown as
    :func:`shown_path` shows it."""
    return shown_path(_LINE_BREAKS.sub(" ", str(text)))


def card_body(text: Any) -> str:
    """The last guard on a whole card or press text (review R7, 15 October
    2026). The text keeps its own line breaks, and every line of it is shown
    as :func:`shown_path` shows it, so nothing else of Unicode categories Cc,
    Cf, Zl or Zp is left anywhere in it. It cannot tell a line break a name
    smuggled in from one the text meant: every line is therefore first made
    one line where it is written (:func:`shown_sentence`), and every file name
    escaped where a sentence names it (:func:`shown_path`)."""
    return "\n".join(shown_path(part) for part in str(text).split("\n"))


def shown_report(value: Any, *, key: str | None = None) -> Any:
    """A report the build system printed, with every sentence in it shown as
    one line a person reads (:func:`shown_sentence`, coach review 3), so
    whatever it copied out of the project adds no line and no byte that
    cannot be written.

    The lists of file names in :data:`REPORT_NAME_LISTS` are structured data
    and are kept EXACTLY as reported (review R5): two different names may
    look alike once escaped, so they are compared and recorded as they are
    (:func:`names_for_record`) and escaped only when a sentence names them.
    """
    if key in REPORT_NAME_LISTS:
        return value
    if isinstance(value, str):
        return shown_sentence(value)
    if isinstance(value, dict):
        return {k: shown_report(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [shown_report(v, key=key) for v in value]
    return value


#: The suffix of a record field that keeps a list of names exactly.
EXACT_SUFFIX = "_exact"


def _name_bytes(name: str) -> bytes:
    try:
        return os.fsencode(name)
    except UnicodeError:
        return name.encode("utf-8", "surrogatepass")


def names_for_record(key: str, names: Any) -> dict[str, list[str]]:
    """The record fields for one list of file names (review R5): ``key`` holds
    the names as a person reads them, and ``key`` + ``_exact`` holds each
    name exactly, as base64 of its bytes, so a record written as strict UTF-8
    JSON gives back the same names, never two that look alike."""
    exact = [str(n) for n in (names if isinstance(names, (list, tuple)) else [])]
    return {
        key: [shown_path(n) for n in exact],
        key + EXACT_SUFFIX: [
            base64.b64encode(_name_bytes(n)).decode("ascii") for n in exact
        ],
    }


def names_from_record(detail: Any, key: str) -> list[str]:
    """The exact names :func:`names_for_record` wrote for ``key``. A record
    written before exact names were kept (no ``_exact`` field, or one that
    cannot be read) gives back its plain list as it is."""
    if not isinstance(detail, dict):
        return []
    carried = detail.get(key + EXACT_SUFFIX)
    if isinstance(carried, list) and all(isinstance(c, str) for c in carried):
        try:
            return [
                os.fsdecode(base64.b64decode(c.encode("ascii"), validate=True))
                for c in carried
            ]
        except (ValueError, UnicodeError):
            pass
    plain = detail.get(key)
    if not isinstance(plain, (list, tuple)):
        return []
    return [str(n) for n in plain]


def _not_read(declared_at: str | None, reason: str) -> dict[str, Any]:
    logger.info("merge card: the merge check could not be read (%s)", reason)
    return {
        "declared_at": declared_at,
        "read": False,
        "reason": shown_path(reason),
        "judge_files": [],
        "config_changed": False,
        "listing_unreadable": [],
    }


def _match_keys(word: str) -> list[str]:
    """The names one command word can stand for: the word, the part after its
    first ``=``, and the pieces of both split on ``,`` and ``:``, each without
    a leading ``./``. Used for the card's WORDING only (does a word name the
    file by its path, and is it a whole word); what is a judge file and what
    is a link is always GuardKit's answer."""
    whole = [str(word)]
    if "=" in whole[0]:
        whole.append(whole[0].split("=", 1)[1])
    keys = list(whole)
    for key in whole:
        keys.extend(piece for piece in key.replace(":", ",").split(",") if piece and piece != key)
    out: list[str] = []
    for key in keys:
        while key.startswith("./"):
            key = key[2:]
        if key and key not in out:
            out.append(key)
    return out


def _named_by_path(path: str, words: Sequence[str]) -> bool:
    """True when some word names ``path`` by its path, not only by its name."""
    return any(
        key == path or key.endswith("/" + path)
        for word in words
        for key in _match_keys(word)
    )


def _install_runs(path: str, argv: Sequence[str]) -> bool:
    """True when the install's argument list names ``path`` as a whole word
    that is the program or one of its plain arguments; False when the file is
    named only as an option's value (``-r requirements.txt``,
    ``--requirement=requirements.txt``) or inside a word."""
    for index, word in enumerate(argv):
        key = str(word)
        while key.startswith("./"):
            key = key[2:]
        if key != path and not key.endswith("/" + path):
            continue
        if index == 0 or not str(argv[index - 1]).startswith("-"):
            return True
    return False


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
            "pin_problem": shown_path(problem or ""),
            "judge_files": [],
            "config_changed": False,
            "listing_unreadable": [],
        }
    words = tuple(getattr(pinned, "judge_words", ()) or ())
    test_words: tuple[str, ...] = ()
    if test_command:
        said, _refusal = rules.judge_words(test_command, through_shell=True)
        test_words = tuple(said or ())
    install_text = str(
        getattr(getattr(declarations, "toolchain", None), "install", "") or ""
    )
    install_argv: tuple[str, ...] = ()
    if install_text:
        said, _refusal = rules.judge_words(install_text, through_shell=False)
        install_argv = tuple(said or ())

    def role_of(path: str) -> str:
        if test_words and rules.judge_files([path], test_words):
            return "check" if _named_by_path(path, test_words) else "check-named-like"
        if not _named_by_path(path, install_argv):
            return "install-named-like"
        return "install" if _install_runs(path, install_argv) else "install-list"

    # -- every tree, listed whole, judged by GuardKit's link rule ------------
    trees: list[tuple[str, str]] = [(declared_at, declared_at), (candidate, candidate)]
    if joined:
        trees.append((joined, "the preview's joined commit"))
    unlisted: list[str] = []
    if joined_expected and not joined:
        unlisted.append("the preview's joined commit")
    listed: dict[str, dict[str, Any]] = {}
    links: list[str] = []
    for commit, shown in trees:
        listing = await reader.list_tree_at_commit(repo_path, commit)
        if not listing.ok:
            logger.info("merge card: %s could not be listed (%s)", shown, listing.refusal)
            unlisted.append(shown)
            continue
        entries = {entry.path: entry for entry in listing.entries}
        listed[commit] = entries
        if words:
            modes = {path: entry.mode for path, entry in entries.items()}
            for path in rules.links_on_judge_paths(modes, words):
                if path not in links:
                    links.append(path)

    judges: list[dict[str, Any]] = []
    mains, joins = listed.get(declared_at), listed.get(joined or "")

    def judge(path: str, how: str) -> dict[str, Any]:
        # What the merge does with it comes only from the target's own tree,
        # as GuardKit's merge decides it (review R3): main's copy when the
        # target has the file, left out when it does not.
        return {
            "path": path,
            "how": how,
            "role": role_of(path),
            "on_target": (path in mains) if mains is not None else None,
        }

    # -- the build's changed judge files ------------------------------------
    paths = _paths_of(changes) if changes is not None else []
    for path in rules.judge_files(paths, words) if (paths and words) else []:
        judges.append(judge(path, _how(changes or (), path)))

    # -- the preview's joined tree, against the target's ---------------------
    # The merge restores every file the target's commands name; one whose
    # joined copy is not the target's carries the build's edits (round 4).
    # A judge file only the joined tree has is one the merge leaves out.
    if words and mains is not None and joins is not None:
        listed_paths = {j["path"] for j in judges}
        for path in rules.judge_files(sorted(mains), words):
            if path in listed_paths or path in links:
                continue
            here, there = mains[path], joins.get(path)
            if there is None:
                # On the target and gone from the joined tree: the build
                # deleted it (coach review 2).
                judges.append(judge(path, "deleted"))
            elif (here.mode, here.object) != (there.mode, there.object):
                judges.append(judge(path, "changed"))
        for path in rules.judge_files(sorted(set(joins) - set(mains)), words):
            if path not in listed_paths and path not in links:
                judges.append(judge(path, "added"))

    for path in links:
        judges = [j for j in judges if j["path"] != path]
        judges.append({"path": path, "how": "link", "role": "link"})

    # Every decision above was made on the exact names. The record and the
    # card carry them as a person reads them, since a name that is not UTF-8
    # cannot travel in a message as it is.
    for entry in judges:
        entry["path"] = shown_path(entry["path"])

    return {
        "declared_at": declared_at,
        "read": True,
        "reason": None,
        "changes_read": changes is not None,
        "judge_files": judges,
        "config_changed": CONFIG_PATH in paths,
        "listing_unreadable": unlisted,
    }
