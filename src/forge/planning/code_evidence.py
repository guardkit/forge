"""The code the planner is shown, and the files that may hold "all the X".

WHY (6 October 2026, planning improvements, items 1 and 3). Two plans for the
same delete-user sentence planned work the repository already had, and a
third missed two of the five count endpoints a request named as "all the count
endpoints". The planner was given file names and up to five ``path:line``
places per word, ordered by kind of file and then by path, and that order
never reached the route the repository already had.

Two pieces, both plain text search and counting, with no language's grammar
and no file type filtered out:

* **evidence windows** (item 1): for each place a word was found, the stretch
  of the file from 3 lines before to 12 lines after it, scored by how many of
  the request's own words start a word inside it. The best few travel to the
  planner, numbered, so it can say what is already done and cite the line;
* **the set search** (item 3): when the request says "all the X", "every X"
  or "each X", every tracked file that holds the X word anywhere is a
  candidate, ranked by how many of the request's other words it holds and how
  densely it holds the X word. At most 24 are listed, and the entry says
  plainly whether that list holds every matching file.

The only English this module knows is the quantifier grammar ("all", "every",
"each") and a short list of filler words left out of the scoring, of the same
kind as the negation words :mod:`forge.planning.example_review` knows.

Pure apart from the reader it is handed. A reader that cannot read raises
:class:`~forge.planning.repository_facts.RepositoryUnreadable`, which the
driver turns into its usual plain reason; anything else here never raises
past the driver's own guard.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from forge.planning.repository_facts import RepositoryUnreadable

__all__ = [
    "EVIDENCE_LINES_AFTER",
    "EVIDENCE_LINES_BEFORE",
    "LISTED_ALL_MEANS",
    "MAX_EVIDENCE_FILES_PER_WORD",
    "MAX_EVIDENCE_WINDOWS",
    "MAX_EVIDENCE_WINDOWS_PER_WORD",
    "MAX_SET_CANDIDATES_LISTED",
    "MAX_SET_FILES_READ",
    "MAX_SET_PHRASES",
    "evidence_for_word",
    "quantified_phrases",
    "request_words",
    "set_candidates",
    "words_starting_in",
]

#: The window around one hit: this many lines before it and after it.
EVIDENCE_LINES_BEFORE = 3
EVIDENCE_LINES_AFTER = 12
#: At most this many windows per word, and in all.
MAX_EVIDENCE_WINDOWS_PER_WORD = 3
MAX_EVIDENCE_WINDOWS = 12
#: At most this many files are read for one word's windows.
MAX_EVIDENCE_FILES_PER_WORD = 10
#: Each line of a window is cut to this many characters.
_WINDOW_LINE_CHARS = 200

#: The set search's bounds.
MAX_SET_PHRASES = 2
MAX_SET_FILES_READ = 120
MAX_SET_CANDIDATES_LISTED = 24
_SET_LINES_PER_CANDIDATE = 2
_SET_LINE_CHARS = 200

#: Words of three or more letters too common to say anything about a stretch
#: of code. English only, and short on purpose.
_FILLER_WORDS = frozenset(
    {"the", "and", "for", "from", "that", "with", "this", "all", "add", "make"}
)

_TOKEN = re.compile(r"[A-Za-z0-9]+")

#: "all the count endpoints", "every route", "each of its handlers". English
#: grammar only: the quantifier, an optional "of" and determiner, then up to
#: three words.
_QUANTIFIED = re.compile(
    r"\b(all|every|each)\s+(?:of\s+)?(?:the\s+|its\s+|our\s+)?"
    r"((?:[A-Za-z][\w-]*\s+){0,2}[A-Za-z][\w-]*)",
    re.IGNORECASE,
)

#: What a phrase stopped short by a number looks like: "each of the last 7
#: days" stops at "last" only because a word may not start with a digit.
_STOPPED_BY_A_NUMBER = re.compile(r"\s+\d")

#: The factory's own records the set search never lists, beside the folders
#: the driver already leaves out of every repository read: the pass bars the
#: planner itself writes.
_SET_SEARCH_SKIP_PATTERNS = ("qa/pass-bar-*.yaml",)

#: What ``listed_all`` means, in the descriptor's own words.
LISTED_ALL_MEANS = (
    "listed_all is true only when every file that holds the looked_for word "
    "is in candidates: none was cut by the limit of "
    f"{MAX_SET_CANDIDATES_LISTED} listed files and no search answer was cut "
    "short. It says nothing about which files are members of the set."
)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN.findall(text or "")}


def request_words(text: str, *, leave_out: Iterable[str] = ()) -> list[str]:
    """The request's own words of three or more characters, lower case, in
    order, without the filler words and without ``leave_out``."""
    skip = _FILLER_WORDS | {word.lower() for word in leave_out}
    words: list[str] = []
    for token in _TOKEN.findall(text or ""):
        lowered = token.lower()
        if len(lowered) >= 3 and lowered not in skip and lowered not in words:
            words.append(lowered)
    return words


def words_starting_in(words: Sequence[str], text: str) -> int:
    """How many of ``words`` start a word somewhere in ``text``, case ignored."""
    tokens = _tokens(text)
    return sum(1 for word in words if any(token.startswith(word) for token in tokens))


# ---------------------------------------------------------------------------
# Item 1: evidence windows
# ---------------------------------------------------------------------------


def _hit(place: str) -> tuple[str, int] | None:
    path, _, line = str(place).rpartition(":")
    if not path or not line.isdigit():
        return None
    return path, int(line)


def _not_read_sentence(reader: Any, path: str) -> str:
    why = (getattr(reader, "refused", None) or {}).get(path)
    return f"`{path}` could not be read ({why or 'it was not served'})"


def evidence_for_word(
    reader: Any,
    places: Sequence[str],
    *,
    words: Sequence[str],
    rank: Callable[[str], Any],
    texts: dict[str, str | None],
    windows_left: int,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """The best windows round one word's hits.

    ``places`` are every ``path:line`` the word's spellings were found at,
    the factory's own records already left out. ``rank`` is the driver's
    kind-of-file order, used only to choose which files to read first and to
    break ties. ``texts`` is the run's cache of files already read, so a file
    is read once. Returns ``(windows, more_hits, not_read)``: ``more_hits``
    counts the hits that were never scored (their file was past the read
    limit, could not be read, or the window limit was already spent), and
    ``not_read`` says why each refused file was refused. Raises
    :class:`RepositoryUnreadable` when the repository itself stops answering.
    """
    hits = [hit for hit in (_hit(place) for place in places) if hit is not None]
    hits = list(dict.fromkeys(hits))
    if windows_left <= 0:
        return [], len(hits), []
    by_rank = sorted(hits, key=lambda hit: (rank(f"{hit[0]}:{hit[1]}"), hit[0], hit[1]))
    to_read: list[str] = []
    for path, _ in by_rank:
        if path not in to_read:
            to_read.append(path)
    to_read = to_read[:MAX_EVIDENCE_FILES_PER_WORD]
    not_read: list[str] = []
    scored: list[tuple[int, Any, str, int, int, int]] = []
    lines_of: dict[str, list[str]] = {}
    unscored = 0
    for path, number in by_rank:
        if path not in to_read:
            unscored += 1
            continue
        if path not in texts:
            texts[path] = reader.read_text(path)
        text = texts[path]
        if text is None:
            sentence = _not_read_sentence(reader, path)
            if sentence not in not_read:
                not_read.append(sentence)
            unscored += 1
            continue
        if path not in lines_of:
            # Numbered as the search numbers them: by "\n" only, and the
            # empty piece after a final line break is not a line.
            split = text.split("\n")
            lines_of[path] = split[:-1] if split and split[-1] == "" else split
        lines = lines_of[path]
        if number < 1 or number > len(lines):
            unscored += 1
            continue
        first = max(1, number - EVIDENCE_LINES_BEFORE)
        last = min(len(lines), number + EVIDENCE_LINES_AFTER)
        score = words_starting_in(words, "\n".join(lines[first - 1 : last]))
        scored.append((-score, rank(f"{path}:{number}"), path, number, first, last))
    scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    chosen: list[dict[str, Any]] = []
    limit = min(MAX_EVIDENCE_WINDOWS_PER_WORD, windows_left)
    for negative, _rank, path, number, first, last in scored:
        if len(chosen) >= limit:
            break
        # A hit inside a window already chosen adds nothing the planner has
        # not been shown.
        if any(w["path"] == path and w["first_line"] <= number <= w["last_line"] for w in chosen):
            continue
        lines = lines_of[path]
        chosen.append(
            {
                "path": path,
                "first_line": first,
                "last_line": last,
                "score": -negative,
                "text": "\n".join(
                    f"{n}: {lines[n - 1][:_WINDOW_LINE_CHARS]}" for n in range(first, last + 1)
                ),
            }
        )
    return chosen, unscored, not_read


# ---------------------------------------------------------------------------
# Item 3: the files that may hold "all the X"
# ---------------------------------------------------------------------------


def quantified_phrases(request_text: str) -> list[tuple[str, str, list[str]]]:
    """``[(phrase, search word, the phrase's own words)]``, at most two.

    The search word is the first word after the quantifier and determiner,
    a plural "s" removed, kept only when it is at least four letters long. A
    phrase stopped short by a number ("each of the last 7 days") names no set
    of things in the repository and is left out.
    """
    found: list[tuple[str, str, list[str]]] = []
    for match in _QUANTIFIED.finditer(request_text or ""):
        if _STOPPED_BY_A_NUMBER.match(request_text[match.end() :]):
            continue
        phrase = " ".join(match.group(0).split())
        first = _singular(match.group(2).split()[0])
        if len(first) < 4:
            continue
        if any(existing[0].lower() == phrase.lower() for existing in found):
            continue
        found.append((phrase, first, [t.lower() for t in _TOKEN.findall(phrase)]))
        if len(found) >= MAX_SET_PHRASES:
            break
    return found


def _singular(word: str) -> str:
    """A plural "s" removed, as the search word's own is."""
    lowered = word.lower()
    return lowered[:-1] if lowered.endswith("s") and not lowered.endswith("ss") else lowered


def _set_skipped(path: str, skip_prefixes: Sequence[str]) -> bool:
    return path.startswith(tuple(skip_prefixes)) or any(
        fnmatch.fnmatchcase(path, pattern) for pattern in _SET_SEARCH_SKIP_PATTERNS
    )


def set_candidates(
    reader: Any,
    request_text: str,
    phrase: str,
    word: str,
    phrase_words: Sequence[str],
    *,
    skip_prefixes: Sequence[str],
    partial: list[str] | None = None,
) -> dict[str, Any]:
    """One ``sets_the_request_names`` entry. Raises
    :class:`RepositoryUnreadable` only when the first search cannot run; a
    reader that stops answering later keeps what was found and the entry
    says the list is not all of it."""
    # The phrase's own words, singular or plural, rank nothing: every
    # candidate holds the search word, and "endpoint" is the phrase's
    # "endpoints" again.
    own = {_singular(word) for word in phrase_words}
    others = [w for w in request_words(request_text) if _singular(w) not in own]
    relevant = lambda path: not _set_skipped(str(path), skip_prefixes)  # noqa: E731
    answer = reader.files_mentioning(word, ignore_case=True, relevant=relevant)
    cut = getattr(answer, "cut", None)
    search_cut = bool(cut)
    if cut and partial is not None:
        partial.append(f"the files that hold `{word}` were only partly searched ({cut})")
    candidates = sorted(dict.fromkeys(str(p) for p in answer if relevant(str(p))))
    entry: dict[str, Any] = {
        "phrase": phrase,
        "looked_for": word,
        "candidates": [],
        "listed": 0,
        "matched": len(candidates),
        "listed_all": False,
        "listed_all_means": LISTED_ALL_MEANS,
    }
    stopped: str | None = None
    ranked: list[tuple[int, float, str]] = []
    unread: list[str] = []
    texts: dict[str, str] = {}
    for index, path in enumerate(candidates):
        if index >= MAX_SET_FILES_READ or stopped:
            unread.append(path)
            continue
        try:
            text = reader.read_text(path)
        except RepositoryUnreadable as exc:
            stopped = str(exc)
            unread.append(path)
            continue
        if text is None:
            unread.append(path)
            continue
        texts[path] = text
        lines = text.split("\n")
        # How densely: the search word's matches per thousand lines.
        density = text.lower().count(word) * 1000.0 / max(len(lines), 1)
        ranked.append((words_starting_in(others, text), density, path))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    order = [row[2] for row in ranked] + sorted(unread)
    listed = order[:MAX_SET_CANDIDATES_LISTED]
    lines_by_path = _matching_lines(reader, word, listed, texts)
    rows: list[dict[str, Any]] = []
    for path in listed:
        matching = lines_by_path.get(path, [])
        best = sorted(matching, key=lambda row: (-words_starting_in(others, row[1]), row[0]))
        rows.append(
            {
                "path": path,
                "lines": [
                    f"{number}: {text.strip()[:_SET_LINE_CHARS]}"
                    for number, text in best[:_SET_LINES_PER_CANDIDATE]
                ],
            }
        )
    entry["candidates"] = rows
    entry["listed"] = len(rows)
    entry["listed_all"] = (
        not search_cut and stopped is None and len(candidates) <= MAX_SET_CANDIDATES_LISTED
    )
    if unread:
        entry["not_read"] = len(unread)
    if stopped is not None:
        entry["unavailable"] = stopped
    return entry


def _matching_lines(
    reader: Any, word: str, listed: Sequence[str], texts: Mapping[str, str]
) -> dict[str, list[tuple[int, str]]]:
    """Each listed file's lines holding ``word``: from the reader's own line
    search when it has one, else from the text already read."""
    wanted = set(listed)
    found: dict[str, list[tuple[int, str]]] = {}
    search = getattr(reader, "lines_mentioning", None)
    if callable(search):
        try:
            for path, number, text in search(word, ignore_case=True):
                if path in wanted:
                    found.setdefault(path, []).append((int(number), str(text)))
            return found
        except RepositoryUnreadable:
            found = {}
    for path in listed:
        text = texts.get(path)
        if text is None:
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            if word in line.lower():
                found.setdefault(path, []).append((number, line))
    return found
