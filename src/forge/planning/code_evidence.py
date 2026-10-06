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
  the request's own words start a word inside it. The windows travel to the
  planner, numbered, so it can say what is already done and cite the line.
  They are chosen across all the words at once: the request's own and rarest
  words first, one window per file before a second in any file, until the
  size budget is spent (:func:`choose_windows`);
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
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_FILES_PER_WORD",
    "MAX_EVIDENCE_WINDOWS",
    "MAX_SET_CANDIDATES_LISTED",
    "MAX_SET_FILES_READ",
    "MAX_SET_PHRASES",
    "candidate_windows",
    "choose_windows",
    "is_factory_record",
    "quantified_phrases",
    "request_words",
    "set_candidates",
    "trim_to_budget",
    "words_starting_in",
]

#: The window around one hit: this many lines before it and after it.
EVIDENCE_LINES_BEFORE = 3
EVIDENCE_LINES_AFTER = 12
#: At most this many windows in all (6 October 2026: was 3 per word and 12
#: in all). The size budget, :data:`MAX_EVIDENCE_CHARS`, normally stops them
#: first; see :func:`choose_windows`.
MAX_EVIDENCE_WINDOWS = 24
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
#: days" stops at "last" only because a word may not start with a digit, and
#: the noun ("days") comes after the number.
_NUMBER_THEN_NOUN = re.compile(r"\s+\d+\s+[A-Za-z]")

#: The factory's own records that neither the set search nor the evidence
#: windows ever show, beside the folders the driver already leaves out of
#: every repository read: the pass bars the planner itself writes.
_FACTORY_RECORD_PATTERNS = ("qa/pass-bar-*.yaml",)

#: What ``listed_all`` means, in the descriptor's own words.
LISTED_ALL_MEANS = (
    "listed_all is true only when every file that holds the looked_for word "
    "is in candidates: none was cut by the limit of "
    f"{MAX_SET_CANDIDATES_LISTED} listed files, no search answer was cut "
    "short and no candidate's lines were trimmed for size. It says nothing "
    "about which files are members of the set."
)

#: The most characters the evidence windows' text and the set candidates'
#: lines may take, together, across the whole descriptor. The plan-writer's
#: prompt (and, with the planner's own switch on, its checker's) carries
#: them. Each part is guaranteed half; what one part does not use goes to
#: the other. Past its share, a part loses its lowest-scoring windows, or
#: its candidate lines from the lowest-ranked candidate up, and what went is
#: counted. The windows are never emptied to make room for candidate lines.
MAX_EVIDENCE_CHARS = 16_000


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


def is_factory_record(path: str) -> bool:
    """True for the factory's own records that sit in a project's folders
    (the pass bars the planner writes): never the project's code, so never
    evidence and never a set candidate."""
    return any(fnmatch.fnmatchcase(str(path), pattern) for pattern in _FACTORY_RECORD_PATTERNS)


def candidate_windows(
    reader: Any,
    places: Sequence[str],
    *,
    words: Sequence[str],
    rank: Callable[[str], Any],
    texts: dict[str, str | None],
) -> tuple[list[dict[str, Any]], list[tuple[str, int]], list[str]]:
    """Every window round one word's hits, best first.

    ``places`` are every ``path:line`` the word's spellings were found at,
    the factory's own records already left out. The files read are the ones
    with the most hits, whatever their type; ``rank``, the driver's
    kind-of-file order, only breaks ties. ``texts`` is the run's cache of
    files already read, so a file is read once. Returns ``(candidates, hits,
    not_read)``: each candidate is a window ``{"path", "first_line",
    "last_line", "score", "text"}`` with a private ``_hit`` (the line it was
    cut round), best score first; ``hits`` is every ``(path, line)`` found,
    read or not, which :func:`choose_windows` counts against the windows it
    keeps; ``not_read`` says why each refused file was refused. Raises
    :class:`RepositoryUnreadable` when the repository itself stops answering.
    """
    hits = [hit for hit in (_hit(place) for place in places) if hit is not None]
    hits = list(dict.fromkeys(hits))
    per_file: dict[str, int] = {}
    for path, _ in hits:
        per_file[path] = per_file.get(path, 0) + 1
    # Which files to read: most hits first, never by file type; the
    # kind-of-file order only breaks ties.
    to_read = sorted(per_file, key=lambda path: (-per_file[path], rank(path), path))
    to_read = set(to_read[:MAX_EVIDENCE_FILES_PER_WORD])
    not_read: list[str] = []
    lines_of: dict[str, list[str]] = {}
    scored: list[tuple[int, Any, str, int, int, int]] = []
    for path, number in sorted(hits, key=lambda hit: (rank(f"{hit[0]}:{hit[1]}"), hit[0], hit[1])):
        if path not in to_read:
            continue
        if path not in texts:
            texts[path] = reader.read_text(path)
        text = texts[path]
        if text is None:
            sentence = _not_read_sentence(reader, path)
            if sentence not in not_read:
                not_read.append(sentence)
            continue
        if path not in lines_of:
            # Numbered as the search numbers them: by "\n" only, and the
            # empty piece after a final line break is not a line.
            split = text.split("\n")
            lines_of[path] = split[:-1] if split and split[-1] == "" else split
        lines = lines_of[path]
        if number < 1 or number > len(lines):
            continue
        first = max(1, number - EVIDENCE_LINES_BEFORE)
        last = min(len(lines), number + EVIDENCE_LINES_AFTER)
        score = words_starting_in(words, "\n".join(lines[first - 1 : last]))
        scored.append((-score, rank(f"{path}:{number}"), path, number, first, last))
    scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    candidates = [
        {
            "path": path,
            "first_line": first,
            "last_line": last,
            "score": -negative,
            "text": "\n".join(
                f"{n}: {lines_of[path][n - 1][:_WINDOW_LINE_CHARS]}" for n in range(first, last + 1)
            ),
            "_hit": number,
        }
        for negative, _rank, path, number, first, last in scored
    ]
    return candidates, hits, not_read


def _holds(window: Mapping[str, Any], path: str, number: int) -> bool:
    return window["path"] == path and window["first_line"] <= number <= window["last_line"]


def _mostly_shown(window: Mapping[str, Any], shown: Mapping[str, Any]) -> bool:
    """More than half of ``window``'s lines are already in ``shown``."""
    if window["path"] != shown["path"]:
        return False
    overlap = min(window["last_line"], shown["last_line"]) - max(
        window["first_line"], shown["first_line"]
    ) + 1
    return overlap * 2 > window["last_line"] - window["first_line"] + 1


def choose_windows(
    entries: Sequence[dict[str, Any]],
    candidates: Sequence[Sequence[dict[str, Any]]],
    hits: Sequence[Sequence[tuple[str, int]]],
    *,
    from_request: Sequence[bool],
    max_windows: int = MAX_EVIDENCE_WINDOWS,
    max_chars: int = MAX_EVIDENCE_CHARS,
) -> None:
    """Choose the windows the planner is shown, across all the words, in place.

    WHY (6 October 2026, evidence coverage). Up to 6 October each word kept
    its own 3 best windows. The best-scoring windows are the ones holding the
    most request words, which are usually the tests (they repeat the
    request's vocabulary), so three windows in one test file could stand in
    front of the code the request is about, and a request with one word
    found sent 3 windows and left most of the size budget unused.

    So, one list for the whole descriptor:

    * words the request itself names go before words only the specification
      names, and among those the word found in fewest places first: the
      rarer a word, the more it says about where to look;
    * round by round, each word in that order takes its best window in a
      file no window shows yet; only when no word has one left does a word
      take a second window in a file already shown;
    * a hit already inside a window shown (any word's) is not shown again,
      nor a window more than half of whose lines are already shown;
    * it stops at ``max_windows`` windows or ``max_chars`` characters of
      window text, whichever comes first, so the windows use what the set
      candidates leave of the budget.

    Each entry gets ``evidence`` (when any window was chosen for it) and
    ``more_hits``: every hit of its word not inside a window shown, read or
    not. Each window carries private ``_covers`` (its own word's hits it
    shows) and ``_covers_other`` (``{entry index: hits}`` of other words'
    hits it shows) for :func:`trim_to_budget`, which removes them.
    """
    order = sorted(
        range(len(entries)),
        key=lambda i: (not from_request[i], len(hits[i]), i),
    )
    chosen: list[tuple[int, dict[str, Any]]] = []
    shown_files: set[str] = set()
    used = 0

    def take(index: int, new_file_only: bool) -> bool:
        nonlocal used
        for window in candidates[index]:
            path = window["path"]
            if new_file_only and path in shown_files:
                continue
            if any(_holds(w, path, window["_hit"]) for _, w in chosen):
                continue
            if any(_mostly_shown(window, w) for _, w in chosen):
                continue
            if used + len(window["text"]) > max_chars:
                continue
            chosen.append((index, window))
            shown_files.add(path)
            used += len(window["text"])
            return True
        return False

    for new_file_only in (True, False):
        progress = True
        while progress and len(chosen) < max_windows:
            progress = False
            for index in order:
                if len(chosen) >= max_windows:
                    break
                if take(index, new_file_only):
                    progress = True
    for _index, window in chosen:
        window["_covers"] = 0
        window["_covers_other"] = {}
    for index, entry in enumerate(entries):
        not_shown = 0
        for path, number in hits[index]:
            holder = next(((i, w) for i, w in chosen if _holds(w, path, number)), None)
            if holder is None:
                not_shown += 1
            elif holder[0] == index:
                holder[1]["_covers"] += 1
            else:
                other = holder[1]["_covers_other"]
                other[index] = other.get(index, 0) + 1
        mine = [w for i, w in chosen if i == index]
        for window in mine:
            window.pop("_hit", None)
        if mine:
            entry["evidence"] = mine
        if not_shown:
            entry["more_hits"] = not_shown


# ---------------------------------------------------------------------------
# Item 3: the files that may hold "all the X"
# ---------------------------------------------------------------------------


def quantified_phrases(request_text: str) -> list[tuple[str, str, list[str]]]:
    """``[(phrase, search word, the phrase's own words)]``, at most two.

    The search word is the first word after the quantifier and determiner,
    a plural "s" removed, kept only when it is at least four letters long. A
    phrase cut short before its noun by a number ("each of the last 7 days")
    names no set of things in the repository and is left out.
    """
    found: list[tuple[str, str, list[str]]] = []
    for match in _QUANTIFIED.finditer(request_text or ""):
        # The noun comes after a number ("the last 7 days"): the phrase was
        # cut before its noun. A phrase that already ends in a plural noun
        # ("all active users 30 days after") or is followed by something
        # other than a number and a word ("2x faster") keeps its set.
        last_word = match.group(2).split()[-1].lower()
        ends_in_a_plural = _singular(last_word) != last_word
        if _NUMBER_THEN_NOUN.match(request_text[match.end() :]) and not ends_in_a_plural:
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
    return path.startswith(tuple(skip_prefixes)) or is_factory_record(path)


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
    own = {_singular(own_word) for own_word in phrase_words}
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
    lines_by_path = _matching_lines(word, listed, texts)
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
        if partial is not None:
            partial.append(
                f"the files that hold `{word}` could not all be read for ranking ({stopped})"
            )
    return entry


def _matching_lines(
    word: str, listed: Sequence[str], texts: Mapping[str, str]
) -> dict[str, list[tuple[int, str]]]:
    """Each listed file's lines holding ``word``, from the text already read
    for the ranking: no second search, so nothing a search cap cuts can go
    missing. A listed file that was not read has no lines (and the entry's
    ``not_read`` counts it)."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in listed:
        text = texts.get(path)
        if text is None:
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            if word in line.lower():
                found.setdefault(path, []).append((number, line))
    return found


# ---------------------------------------------------------------------------
# One size budget for what items 1 and 3 add to the plan-writer's prompt
# ---------------------------------------------------------------------------


def _evidence_chars(entries: Sequence[dict[str, Any]], sets: Sequence[dict[str, Any]]) -> int:
    windows = sum(len(w.get("text") or "") for e in entries for w in e.get("evidence") or [])
    lines = sum(len(line) for e in sets for c in e.get("candidates") or [] for line in c.get("lines") or [])
    return windows + lines


def trim_to_budget(
    entries: list[dict[str, Any]],
    sets: list[dict[str, Any]],
    *,
    budget: int = MAX_EVIDENCE_CHARS,
) -> dict[str, int]:
    """Keep the windows' text and the candidates' lines within ``budget``
    characters, in place, and say what went.

    Each part is guaranteed half of ``budget``, and what one part does not
    use goes to the other. Past its share the windows lose their
    lowest-scoring ones first (the later word's on a tie); each one's hits
    are added to its entry's ``more_hits``, and the other words' hits it
    alone showed to theirs. Past theirs the candidates lose
    lines, second lines before first ones, from the lowest-ranked candidate
    up; each entry counts them in ``lines_trimmed`` and is no longer
    ``listed_all``. The private ``_covers`` and ``_covers_other`` counts are
    always removed.
    Returns ``{"chars_before", "chars_after", "windows_trimmed",
    "lines_trimmed"}``.
    """
    before = _evidence_chars(entries, sets)
    windows_total = _evidence_chars(entries, [])
    lines_total = before - windows_total
    half = budget // 2
    # The windows' share: their half, and whatever the lines leave unused.
    windows_allowed = max(half, budget - lines_total)
    total = windows_total
    windows_trimmed = lines_trimmed = 0
    ranked = sorted(
        (
            (window.get("score", 0), -index, -position, index, window)
            for index, entry in enumerate(entries)
            for position, window in enumerate(entry.get("evidence") or [])
        ),
        key=lambda row: (row[0], row[1], row[2]),
    )
    for _score, _i, _p, index, window in ranked:
        if total <= windows_allowed:
            break
        entry = entries[index]
        entry["evidence"] = [w for w in entry["evidence"] if w is not window]
        entry["more_hits"] = int(entry.get("more_hits") or 0) + int(window.get("_covers") or 1)
        # Other words' hits it was the one window showing are no longer shown.
        for other_index, count in (window.get("_covers_other") or {}).items():
            other = entries[other_index]
            other["more_hits"] = int(other.get("more_hits") or 0) + int(count)
        if not entry["evidence"]:
            del entry["evidence"]
        total -= len(window.get("text") or "")
        windows_trimmed += 1
    # The lines' share: whatever the windows now leave, at least half.
    lines_allowed = budget - total
    windows_after = total
    total = lines_total
    for keep in (1, 0):
        for entry in sets:
            for candidate in reversed(entry.get("candidates") or []):
                if total <= lines_allowed:
                    break
                lines = candidate.get("lines") or []
                if len(lines) > keep:
                    total -= sum(len(line) for line in lines[keep:])
                    trimmed = len(lines) - keep
                    candidate["lines"] = lines[:keep]
                    entry["lines_trimmed"] = int(entry.get("lines_trimmed") or 0) + trimmed
                    entry["listed_all"] = False
                    lines_trimmed += trimmed
    for entry in entries:
        for window in entry.get("evidence") or []:
            window.pop("_covers", None)
            window.pop("_covers_other", None)
    return {
        "chars_before": before,
        "chars_after": windows_after + total,
        "windows_trimmed": windows_trimmed,
        "lines_trimmed": lines_trimmed,
    }
