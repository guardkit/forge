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
  the request's own words start a word inside it. A window starts higher
  when the place sits in a block of lines with no blank line between them
  that begins at most 24 lines above it: it then starts at the block's first
  line (:func:`window_start`). The windows travel to the
  planner, numbered, so it can say what is already done and cite the line.
  They are chosen across all the words at once: the request's own and rarest
  words first, one window per file before a second in any file, until the
  size budget is spent (:func:`choose_windows`). Then each approved
  scenario no window shown is a test of is given one, from the project's
  declared test folders, but only on strong evidence (:class:`_ScenarioFit`):
  no window rather than a wrong one;
* **the set search** (item 3): when the request says "all the X", "every X"
  or "each X", every tracked file that holds the X word anywhere is a
  candidate, ranked by how many of the request's other words it holds and how
  densely it holds the X word. At most 24 are listed, and the entry says
  plainly whether that list holds every matching file.

Neither ever shows the factory's own files in a project (its records, and
the sandbox scripts it ships where it puts them, :func:`is_factory_file`).
A window is never made round a hit past the 200 characters it shows of the
hit's line, and a candidate holding its word only on lines written by a
program (:data:`MACHINE_WRITTEN_LINE_CHARS`) is ranked last.

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

from forge.factory_files import is_shipped_script, shipped_script_path
from forge.planning.repository_facts import RepositoryUnreadable

__all__ = [
    "EVIDENCE_BLOCK_LINES_ABOVE",
    "EVIDENCE_LINES_AFTER",
    "EVIDENCE_LINES_BEFORE",
    "LISTED_ALL_MEANS",
    "MACHINE_WRITTEN_LINE_CHARS",
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_FILES_PER_WORD",
    "MAX_EVIDENCE_WINDOWS",
    "MAX_SET_CANDIDATES_LISTED",
    "MAX_SET_FILES_READ",
    "MAX_SET_PHRASES",
    "candidate_windows",
    "choose_windows",
    "is_documentation",
    "is_factory_record",
    "quantified_phrases",
    "request_method",
    "request_words",
    "scenario_words",
    "set_candidates",
    "trim_to_budget",
    "window_start",
    "words_starting_in",
]

#: The window around one hit: this many lines before it and after it.
EVIDENCE_LINES_BEFORE = 3
EVIDENCE_LINES_AFTER = 12
#: How far above its hit a window may start to take in the whole block of
#: lines the hit sits in (:func:`window_start`).
EVIDENCE_BLOCK_LINES_ABOVE = 24
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

#: A line longer than this was written by a program, not a person: a
#: one-line report, a minified bundle, a data dump. A file holding the set's
#: word only on such lines stays a candidate, and in ``matched``, but is
#: ranked after every other. (7 October 2026: a committed one-line test
#: coverage report holds every source file's name, so it held most of the
#: request's words and ranked above the code.) The evidence windows have
#: their own rule: a hit is no window only when it lies past the 200
#: characters a window shows of its line, and it is still counted.
MACHINE_WRITTEN_LINE_CHARS = 1_000

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
# A test window for each approved scenario
# ---------------------------------------------------------------------------

#: A scenario's header in the specification (the factory's own Gherkin
#: specification format, not the project's): its words start here.
_SCENARIO_HEADER = re.compile(r"(?:Scenario(?:\s+Outline)?|Example)\s*:\s*(.*)")
#: Any other header ends the scenario before it.
_OTHER_HEADER = re.compile(r"(?:Feature|Background|Rule)\s*:")
#: A quoted value in a step.
_QUOTED = re.compile(r"\"([^\"\n]+)\"|'([^'\n]+)'")
#: A value written without quotes that is a name, not a word: it has a
#: dot, slash, at sign, colon, hyphen or underscore inside it (a path, an
#: address, a dotted or joined-up name).
_PUNCTUATED = re.compile(r"/?[A-Za-z0-9{][A-Za-z0-9{}]*(?:[./@:_-][A-Za-z0-9{}]+)+")
#: Words that say nothing about which scenario a test is for: the
#: specification format's own, common English, and the keywords and
#: built-in names of common programming languages (so ``None`` in a test's
#: code is never taken for a scenario's "none"). A modest list on purpose.
_NOT_SCENARIO_WORDS = frozenset(
    """
    given when then and but scenario outline example examples background
    feature rule should must shall will would could can may might does done
    have has had having been being were was are its it's they them their
    there these those this that what which while where who whom whose why
    how either neither nor not none nobody nothing some any all each every
    both other others another same such only also just very more most less
    than then once again still even ever never always here into onto from
    with without within about above below after before over under through
    upon until between against among because since though although whether
    rather instead else true false null nil none undefined self this super
    class def function func fun async await return returns yield import
    export from package module public private protected static final const
    var let void typeof instanceof interface struct enum type
    types trait impl extends implements override abstract throw throws
    raise raises try catch except finally assert expect expected test tests
    match case switch break range select record records using namespace
    internal readonly lambda elif pass equal equals unsafe
    describe context string int integer float bool boolean list dict map
    array object value values result results
    """.split()
)
#: Words are compared by their first five letters (a plural "s" dropped
#: first), so "succeeds" and "successfully", or "users" and "user", are
#: one word.
_SCENARIO_STEM_LETTERS = 5
#: How many of its own distinctive words a window must hold to count as a
#: scenario's test, when it holds none of its distinctive values.
_SCENARIO_MIN_WORDS = 2
#: The scenario windows take at most one part in this many of the windows,
#: and of the characters.
_SCENARIO_SHARE = 2


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:_SCENARIO_STEM_LETTERS]


def _scenario_tokens(text: str) -> set[str]:
    """The words of ``text``, lower case, with a capital inside a word
    starting a new one (``TestDeactivatingAnActiveUser`` holds ``active``,
    ``HTTPStatus`` holds ``status``): a test is often named after its
    scenario in one joined-up word."""
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", split)
    return {token.lower() for token in _TOKEN.findall(split)}


def _is_a_value(text: str) -> bool:
    """A value worth looking for as written: five or more characters with
    punctuation or a space inside (``domain.co.uk``, ``/users/x``, ``No
    users found``), not one plain word such as ``active``."""
    text = text.strip()
    return len(text) >= 5 and bool(re.search(r"[^A-Za-z0-9]", text))


def scenario_words(spec_feature: str) -> list[tuple[str, list[str], list[str]]]:
    """``[(title, words, values)]`` for each scenario of the specification,
    in order.

    ``values`` are what the scenario's steps give as written: quoted values,
    the cells of its tables, and unquoted names with punctuation inside
    (paths, addresses, dotted names), lower case, those of five or more
    characters with punctuation or a space inside (:func:`_is_a_value`).

    ``words`` are the scenario's other words (its title and the prose of its
    steps), cut to their first five letters: words of four or more letters
    and numbers of three or more digits, without the words in
    :data:`_NOT_SCENARIO_WORDS`. A value with a digit or punctuation inside
    is data (``user-123``, ``alice@example.com``), so its pieces are not
    words; a quoted plain word (``"inactive"``) is. Comments and tags are
    neither. Plain text only: nothing about the project's language or test
    tools.
    """
    found: list[tuple[str, list[str]]] = []
    lines: list[str] | None = None
    for raw in (spec_feature or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "@")):
            continue
        header = _SCENARIO_HEADER.match(line)
        if header:
            lines = [header.group(1)]
            found.append((header.group(1).strip(), lines))
            continue
        if _OTHER_HEADER.match(line):
            lines = None
            continue
        if lines is not None:
            lines.append(line)
    result: list[tuple[str, list[str], list[str]]] = []
    for title, text_lines in found:
        values: list[str] = []
        prose: list[str] = []
        for line in text_lines:
            if line.startswith("|"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                values.extend(cell for cell in cells if _is_a_value(cell))
                continue
            for match in _QUOTED.finditer(line):
                quoted = match.group(1) or match.group(2) or ""
                if _is_a_value(quoted):
                    values.append(quoted)
            values.extend(m.group(0) for m in _PUNCTUATED.finditer(_QUOTED.sub(" ", line)))
            # Data is not prose: a quoted value with a digit or punctuation
            # inside leaves no words behind.
            prose.append(
                _QUOTED.sub(
                    lambda m: " " if re.search(r"[^A-Za-z ]", m.group(1) or m.group(2) or "") else m.group(0),
                    line,
                )
            )
        words: list[str] = []
        for token in _scenario_tokens(" ".join(prose)):
            keep = len(token) >= 3 if token.isdigit() else len(token) >= 4
            if keep and token not in _NOT_SCENARIO_WORDS and token not in _FILLER_WORDS:
                if _stem(token) not in words:
                    words.append(_stem(token))
        kept = sorted({value.lower().strip(".,;:") for value in values if _is_a_value(value)})
        result.append((title, sorted(words), kept))
    return result


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path.startswith(root.rstrip("/") + "/") for root in roots if root.strip("/"))


def _window_body(window: Mapping[str, Any]) -> str:
    """A window's text with its line numbers left out."""
    return re.sub(r"(?m)^\d+: ", "", window.get("text") or "")


#: A quoted stretch that is a call's argument or a list's item: its quote
#: (any of the three most languages use for strings, after at most two
#: prefix letters such as a format or raw marker) opens right after "(",
#: "[" or ",", with only spaces or line breaks between. A one-line
#: description in quotes, or a comment whose apostrophes happen to pair
#: up, is not one.
_QUOTED_ARGUMENT = re.compile(
    r"[(\[,]\s*[A-Za-z$@]{0,2}(\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`)"
)

#: The method a request names before its route: an all-capitals word
#: followed by a path ("PATCH /users/{user_id}/deactivate").
_METHOD_BEFORE_A_ROUTE = re.compile(r"\b([A-Z]{3,7})\s+/")


def request_method(request_text: str) -> str:
    """The method word the request writes right before its route, lower
    case, or ``""`` when it names none."""
    match = _METHOD_BEFORE_A_ROUTE.search(request_text or "")
    return match.group(1).lower() if match else ""


#: What a line starts with when the whole line is a comment, in the
#: common languages and file formats.
_COMMENT_LINE_STARTS = ("#", "//", "--", ";", "*", "/*", "<!--", "%")


#: Comments that run until a closing mark, possibly over several lines.
_BLOCK_COMMENTS = (("/*", "*/"), ("<!--", "-->"))
#: A line starting with one of these opens a description block (a
#: docstring) that runs until the same mark closes it.
_DESCRIPTION_MARKS = ('"""', "'''")


def _code_only(body: str) -> str:
    """``body`` with its comments and descriptions blanked out, so a call
    written in one is never taken for one the test makes. Line breaks are
    kept, so lines keep their places.

    * a ``/* ... */`` or ``<!-- ... -->`` comment, over as many lines as
      it runs; one that never closes is blanked to the end;
    * a block opened by a line starting with three quotes, until the same
      three quotes close it (a one-line one is blanked too); one that never
      closes is blanked to the end;
    * a line starting with a common comment marker
      (:data:`_COMMENT_LINE_STARTS`);
    * a ``#`` or ``//`` comment after code, from where it starts outside
      quotes (not the ``//`` after a ``:`` in an address).
    """
    kept: list[str] = []
    closer = ""
    for line in body.split("\n"):
        chars = list(line)
        at = 0
        if closer:
            end = line.find(closer)
            if end == -1:
                kept.append("")
                continue
            at = end + len(closer)
            chars[:at] = " " * at
            closer = ""
        stripped = line[at:].lstrip()
        if at == 0 and stripped.startswith(_DESCRIPTION_MARKS):
            if stripped[3:].find(stripped[:3]) == -1:
                closer = stripped[:3]
            kept.append("")
            continue
        if (
            at == 0
            and stripped.startswith(_COMMENT_LINE_STARTS)
            and not stripped.startswith(tuple(o for o, _c in _BLOCK_COMMENTS))
        ):
            kept.append("")
            continue
        quote = ""
        i = at
        while i < len(line):
            char = line[i]
            if quote:
                if char == quote:
                    quote = ""
                i += 1
                continue
            if char in "\"'`":
                quote = char
                i += 1
                continue
            block = next(((o, c) for o, c in _BLOCK_COMMENTS if line.startswith(o, i)), None)
            if block is not None:
                end = line.find(block[1], i + len(block[0]))
                if end == -1:
                    chars[i:] = " " * (len(line) - i)
                    closer = block[1]
                    break
                chars[i : end + len(block[1])] = " " * (end + len(block[1]) - i)
                i = end + len(block[1])
                continue
            if char == "#" or (line.startswith("//", i) and not line[:i].endswith(":")):
                chars[i:] = " " * (len(line) - i)
                break
            i += 1
        kept.append("".join(chars).rstrip())
    return "\n".join(kept)


def _route_pattern(route: str) -> re.Pattern[str]:
    """The whole of ``route`` as a quoted call argument may write it: each
    ``{placeholder}`` segment stands for any one segment (``{thing_id}``
    for ``t-1``), every other segment is itself, nothing may be missing
    before it but a scheme and host (``http://test``), and only a closing
    slash, a query or a fragment may follow it. Its fixed segments are
    matched with their case, as a router matches them."""
    segment = r"[^/\s\"'`?#]+"
    parts = [segment if "{" in part else re.escape(part) for part in route.strip("/").split("/")]
    return re.compile(
        r"(?:[A-Za-z][A-Za-z0-9+.-]*://[^/\s]+)?/" + "/".join(parts) + r"/?(?:[?#]\S*)?"
    )


def _enclosing_call(code: str, at: int) -> int:
    """Where the opening parenthesis of the call holding position ``at``
    is, through any lists or brackets on the way, or -1."""
    depth = 0
    for i in range(at - 1, -1, -1):
        char = code[i]
        if char in ")]}":
            depth += 1
        elif char in "([{":
            if depth:
                depth -= 1
            elif char == "(":
                return i
    return -1


def _call_uses(code: str, paren: int, method: str) -> bool:
    """The call opening at ``paren`` is made with ``method``: the name just
    before the parenthesis is that word (``patch(``, ``client.patch(``), or
    one of the call's own arguments is that word quoted (``"PATCH"``).
    Case is ignored."""
    name = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", code[:paren])
    if name and name.group(1).lower() == method:
        return True
    depth = 0
    for i in range(paren, len(code)):
        if code[i] in "([{":
            depth += 1
        elif code[i] in ")]}":
            depth -= 1
            if depth == 0:
                break
    arguments = code[paren : i + 1]
    return (
        re.search(r"[\"'`]" + re.escape(method) + r"[\"'`]", arguments, re.IGNORECASE)
        is not None
    )


def _calls(body: str, anchor: str, method: str) -> bool:
    """``body`` passes ``anchor`` as a call's quoted argument or a list's
    quoted item, outside comments and descriptions (:func:`_code_only`),
    and, when ``method`` is given, in a call made with that method
    (:func:`_call_uses`). A route (``anchor`` starting with a slash) must
    be the whole route, its fixed segments with their case
    (:func:`_route_pattern`); another name is looked for inside the quoted
    text, case ignored."""
    code = _code_only(body)
    route = _route_pattern(anchor) if anchor.startswith("/") else None
    for match in _QUOTED_ARGUMENT.finditer(code):
        quoted = match.group(1)[1:-1]
        if route is not None:
            if not route.fullmatch(quoted):
                continue
        elif anchor.lower() not in quoted.lower():
            continue
        if not method:
            return True
        paren = _enclosing_call(code, match.start(1))
        if paren != -1 and _call_uses(code, paren, method):
            return True
    return False


#: Prose formats: a window in one of these, outside the test and source
#: folders, is documentation.
_PROSE_SUFFIXES = (".md", ".rst", ".adoc", ".txt")
#: Folders that commonly hold a project's own code.
_SOURCE_FOLDERS = frozenset({"src", "lib", "app", "pkg", "cmd", "internal", "source"})


def is_documentation(path: str, test_roots: Sequence[str]) -> bool:
    """True only for a file that is positively documentation: prose (by its
    suffix) outside the declared test folders and outside the common
    source folders. Anything else, a data or settings file included, is
    not, because it may be a test's cases or the project's code."""
    path = str(path)
    if _under(path, test_roots) or path.split("/", 1)[0] in _SOURCE_FOLDERS:
        return False
    return path.lower().endswith(_PROSE_SUFFIXES)


def _is_data(value: str) -> bool:
    """A value that looks like example data (``user-123``, an address, a
    number): it cannot make a window a scenario's test on its own."""
    return bool(re.search(r"[0-9@]", value))


class _ScenarioFit:
    """Whether a window is good evidence of a scenario's test, and how good.

    A window is a scenario's test only on strong evidence, because a wrong
    test shown as a scenario's proof is worse than none (the planner may
    then say "already done" when it is not):

    * it is in the project's declared test folders;
    * it names what the request names, as the request writes it with its
      punctuation: ``anchors``, as a call's quoted argument or a list's
      quoted item (:data:`_QUOTED_ARGUMENT`) outside comments
      (:func:`_code_only`), where a test writes what it calls; a mention in
      a comment or a one-line description does not count. A route must be
      called whole (:func:`_route_pattern`): ``/widgets/t-1/archive`` is
      not ``/things/{thing_id}/archive``, nor is ``/users/{user_id}`` found
      in ``/users/{user_id}/deactivate``. When the request names a method
      before its route (``PATCH``), that word must be on the same line or
      the line before (``client.patch(``), outside comments, so a test of
      another method on the same route is not this request's test;
    * it holds at least half of the words most of the scenarios say (the
      feature's own vocabulary, such as ``deactivate``, ``patch``,
      ``request``), so it is a test of this feature;
    * it holds no word that only the other scenarios say;
    * and either it holds at least half of the values only this scenario
      gives (``domain.co.uk``), not only example data (``user-123``, an
      address, a number) with nothing else of the scenario's beside it, or
      at least :data:`_SCENARIO_MIN_WORDS` of the words that tell this
      scenario apart.

    When several windows are, the one holding more of the scenario's own
    values and words is preferred (:meth:`score`).

    KNOWN LIMITS (8 October 2026). This is word matching on plain text, not
    an understanding of the test. A test written to look like this
    scenario's (one that calls the requested route with the requested
    method and plants the scenario's own words and values) can still be
    taken for its test, and a real test written in another way (a route
    built from pieces or held in a constant named elsewhere, a call in a
    form not recognised, a window starting inside a comment or description)
    can be missed. Nothing knows a language's grammar. The backstop is the
    planner's proof question: the model is asked whether a cited test
    proves the scenario before "already done" is accepted.

    Only words at most half of the scenarios say tell them apart; the
    others (``user``, ``request``) are the feature's, and are left out of
    both counts. A word the request itself says (``already``, ``inactive``)
    is not counted for a scenario, but still counts against it when only
    the other scenarios say it.
    """

    def __init__(
        self,
        scenarios: Sequence[tuple[Sequence[str], Sequence[str]]],
        test_roots: Sequence[str],
        anchors: Sequence[str],
        request_text: str = "",
    ) -> None:
        self.test_roots = list(test_roots)
        self.anchors = [a for a in anchors if a]
        self.method = request_method(request_text)
        stems = [set(words) for words, _values in scenarios]
        values = [set(vals) for _words, vals in scenarios]
        # A word tells scenarios apart only when at most half of them say it
        # (and, with one scenario, always).
        said_by: dict[str, int] = {}
        for own in stems:
            for word in own:
                said_by[word] = said_by.get(word, 0) + 1
        most = max(1, -(-len(stems) // 2))
        telling = {w for w, n in said_by.items() if n <= most and (n < len(stems) or len(stems) == 1)}
        # A word the request itself says is the feature's: it is no
        # evidence for one scenario, though it still marks a window as
        # another scenario's.
        requested = {_stem(token) for token in _scenario_tokens(request_text)}
        # The words most scenarios say are the feature's own vocabulary.
        self.shared = {w for w, n in said_by.items() if n > most} if len(stems) > 1 else set()
        self.mine: list[set[str]] = []
        self.theirs: list[set[str]] = []
        self.values: list[set[str]] = []
        for position, own in enumerate(stems):
            others = [s for i, s in enumerate(stems) if i != position]
            other_values = [v for i, v in enumerate(values) if i != position]
            self.mine.append((own & telling) - requested)
            self.theirs.append((set().union(*others) - own) & telling)
            self.values.append(values[position] - set().union(*other_values))
        # Each window read once, kept beside its reading, so a window made
        # later at the same address is never given another's reading.
        self._cache: dict[int, tuple[Mapping[str, Any], tuple[str, set[str]] | None]] = {}

    def _read(self, window: Mapping[str, Any]) -> tuple[str, set[str]] | None:
        cached = self._cache.get(id(window))
        if cached is not None and cached[0] is window:
            return cached[1]
        reading: tuple[str, set[str]] | None = None
        if _under(str(window.get("path", "")), self.test_roots):
            text = _window_body(window)
            body = text.lower()
            if not self.anchors or any(
                _calls(text, anchor, self.method) for anchor in self.anchors
            ):
                stems = {_stem(token) for token in _scenario_tokens(_window_body(window))}
                reading = (body, stems)
        self._cache[id(window)] = (window, reading)
        return reading

    def score(self, position: int, window: Mapping[str, Any]) -> tuple[int, int] | None:
        """How strongly ``window`` is scenario ``position``'s test, when it
        is strong evidence of it, else ``None``: ``(the scenario's own
        values that are not example data and its own words held, its
        example data held)``; the larger, the better."""
        read = self._read(window)
        if read is None:
            return None
        body, stems = read
        if len(self.shared & stems) * 2 < len(self.shared):
            return None  # not a test of this feature
        if self.theirs[position] & stems:
            return None  # it says what only another scenario says
        values = self.values[position]
        held = [value for value in values if value in body]
        held_data = sum(1 for value in held if _is_data(value))
        strength = (len(held) - held_data + len(self.mine[position] & stems), held_data)
        if values and len(held) * 2 >= len(values) and strength[0] >= 1:
            return strength  # its values, and not only its example data
        if len(self.mine[position] & stems) >= _SCENARIO_MIN_WORDS:
            return strength
        return None


def _scenario_windows(
    candidates: Sequence[Sequence[dict[str, Any]]],
    order: Sequence[int],
    from_request: Sequence[bool],
    fit: _ScenarioFit,
    count: int,
) -> list[list[tuple[int, dict[str, Any], tuple[int, int]]]]:
    """For each of ``count`` scenarios, the windows that are strong evidence
    of its test (:class:`_ScenarioFit`), best first, as ``(entry index,
    window)``: those holding more of its values, then more of its words, then
    the higher request score. Only windows round the request's own words
    when there are any. Each comes with its strength
    (:meth:`_ScenarioFit.score`)."""
    pool: list[tuple[int, dict[str, Any]]] = []
    seen: set[tuple[str, int, int]] = set()
    asked = [i for i in order if from_request[i]] or list(order)
    for index in asked:
        for window in candidates[index]:
            key = (window["path"], window["first_line"], window["last_line"])
            if key not in seen:
                seen.add(key)
                pool.append((index, window))
    fits: list[list[tuple[int, dict[str, Any], tuple[int, int]]]] = []
    for position in range(count):
        rows = []
        for index, window in pool:
            score = fit.score(position, window)
            if score is not None:
                rows.append((score, window.get("score", 0), index, window))
        rows.sort(
            key=lambda row: (-row[0][0], -row[0][1], -row[1], row[3]["path"], row[3]["first_line"])
        )
        fits.append([(index, window, score) for score, _s, index, window in rows])
    return fits


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
    (the pass bars the planner writes), by path alone."""
    return any(fnmatch.fnmatchcase(str(path), pattern) for pattern in _FACTORY_RECORD_PATTERNS)


def is_factory_file(reader: Any, path: str, texts: dict[str, str | None]) -> bool:
    """True for the factory's own files in a project's tree: its records
    (:func:`is_factory_record`), and a script the factory ships for a
    project's sandbox, at the place the factory puts it (``deploy/<name>``)
    and reading as that script (its header,
    :func:`forge.factory_files.is_shipped_script`). Never the project's code,
    so never evidence and never a set candidate. A project's own file of the
    same name elsewhere, or with other content, stays the project's. Only a
    file at a shipped script's place is read, once, into ``texts``; a file
    that cannot be read is not taken to be the factory's."""
    path = str(path)
    if is_factory_record(path):
        return True
    if shipped_script_path(path) is None:
        return False
    if path not in texts:
        try:
            texts[path] = reader.read_text(path)
        except RepositoryUnreadable:
            return False
    return is_shipped_script(path, texts[path])


def candidate_windows(
    reader: Any,
    places: Sequence[str],
    *,
    words: Sequence[str],
    rank: Callable[[str], Any],
    texts: dict[str, str | None],
    spellings: Sequence[str] = (),
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
        if _hit_past_what_is_shown(lines[number - 1], spellings):
            # The window shows the first 200 characters of each line, so it
            # would show nothing of this hit (a one-line report, a minified
            # file). The hit is still counted.
            continue
        first = window_start(lines, number)
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


def window_start(lines: Sequence[str], number: int) -> int:
    """The first line of the window round the hit on line ``number``.

    Normally :data:`EVIDENCE_LINES_BEFORE` lines above the hit. But when the
    hit sits in a block of lines with no blank line between them (or the
    start of the file) that begins higher, and no more than
    :data:`EVIDENCE_BLOCK_LINES_ABOVE` lines above the hit, the window starts
    at the block's first line. A block whose start is further up than that
    leaves the window where it was.

    WHY (7 October 2026). A hit on a function's own line showed the
    function, but not the long block of route options written directly above
    it, which is where the route's method and path were (the planner and its
    checks were shown the handler of PATCH .../deactivate, not the line
    declaring PATCH). Only blank lines are looked at, which every language
    and text format uses to set blocks apart, so nothing here knows any
    language's decorators, annotations, attributes or route tables.
    """
    first = max(1, number - EVIDENCE_LINES_BEFORE)
    top = number
    while top > 1 and lines[top - 2].strip():
        if number - (top - 1) > EVIDENCE_BLOCK_LINES_ABOVE:
            return first
        top -= 1
    return min(first, top)


def _hit_past_what_is_shown(line: str, spellings: Sequence[str]) -> bool:
    """True when no spelling's first occurrence on ``line`` ends within the
    characters a window shows of it. A line short enough to be shown whole
    is never past; without spellings, nothing is known to be past."""
    if len(line) <= _WINDOW_LINE_CHARS or not spellings:
        return False
    ends = [line.find(s) + len(s) for s in spellings if s and s in line]
    return bool(ends) and min(ends) > _WINDOW_LINE_CHARS


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
    scenarios: Sequence[tuple[Sequence[str], Sequence[str]]] = (),
    test_roots: Sequence[str] = (),
    anchors: Sequence[str] = (),
    may_give_way: Callable[[str], bool] = lambda _path: False,
    request_text: str = "",
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

    A TEST FOR EACH SCENARIO (8 October 2026). The planner may answer "already
    done" only when every approved scenario has a test it was shown and can
    cite. On the deactivate request the tests for the two error scenarios
    were shown and the one for "A user is successfully deactivated" was not,
    though the repository had two: their windows scored below the
    documentation's, sat in files already shown, and the budget was spent on
    one-per-file windows of a word only the error scenario said. So the
    planner planned "verify" tasks instead.

    So, once the windows above are chosen, each scenario in ``scenarios``
    (each one's words and values, from :func:`scenario_words`) that no
    window shown is as strong a test of as the best one there is, is given
    that best one. A
    wrong test shown as a scenario's proof is worse than none (the planner
    may then say "already done" when it is not), so only strong evidence
    counts (:class:`_ScenarioFit`): a window in ``test_roots`` (the
    project's declared test folders) that names what the request names
    (``anchors``, a route whole) as a call's quoted argument outside
    comments, with the request's method
    beside it when it names one, speaks the feature's own words,
    says nothing only another scenario says, and holds this scenario's own
    values (not only example data) or at least two of the words that tell
    it apart. When nothing fits, the
    scenario gets no window. Room is made only by taking out windows for
    which ``may_give_way`` is true (the driver passes
    :func:`is_documentation`: prose outside the test and source folders),
    never a word's first window and never a window in the test folders,
    so no scenario loses the test it was shown. These windows count against the same limits and
    together take at most half of the windows and half of the characters.
    Without ``scenarios``, ``test_roots`` or ``anchors`` nothing changes.

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

    def shown(window: Mapping[str, Any]) -> bool:
        return any(
            _holds(w, window["path"], window["_hit"]) or _mostly_shown(window, w)
            for _, w in chosen
        )

    def add(index: int, window: dict[str, Any]) -> None:
        nonlocal used
        chosen.append((index, window))
        shown_files.add(window["path"])
        used += len(window["text"])

    def take(index: int, new_file_only: bool) -> bool:
        for window in candidates[index]:
            if new_file_only and window["path"] in shown_files:
                continue
            if shown(window):
                continue
            if used + len(window["text"]) > max_chars:
                continue
            add(index, window)
            return True
        return False

    def one_round(new_file_only: bool) -> bool:
        progress = False
        for index in order:
            if len(chosen) >= max_windows:
                break
            if take(index, new_file_only):
                progress = True
        return progress

    one_round(True)
    first_round = [window for _index, window in chosen]
    for new_file_only in (True, False):
        while len(chosen) < max_windows and one_round(new_file_only):
            pass
    if scenarios and test_roots and anchors:
        used = _add_scenario_tests(
            chosen,
            candidates,
            order,
            from_request,
            _ScenarioFit(scenarios, test_roots, anchors, request_text),
            len(scenarios),
            keep=first_round,
            may_give_way=may_give_way,
            used=used,
            max_windows=max_windows,
            max_chars=max_chars,
        )
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


def _add_scenario_tests(
    chosen: list[tuple[int, dict[str, Any]]],
    candidates: Sequence[Sequence[dict[str, Any]]],
    order: Sequence[int],
    from_request: Sequence[bool],
    fit: _ScenarioFit,
    count: int,
    *,
    keep: Sequence[dict[str, Any]],
    may_give_way: Callable[[str], bool],
    used: int,
    max_windows: int,
    max_chars: int,
) -> int:
    """Give each scenario that no window shown is a test of the best window
    that is, in place, and return the characters now used. See
    :func:`choose_windows`."""

    def overlaps(window: Mapping[str, Any]) -> bool:
        return any(
            _holds(w, window["path"], window["_hit"]) or _mostly_shown(window, w)
            for _, w in chosen
        )

    taken = taken_chars = 0
    for position, fitting in enumerate(
        _scenario_windows(candidates, order, from_request, fit, count)
    ):
        shown = [fit.score(position, w) for _, w in chosen]
        best_shown = max((s for s in shown if s is not None), default=None)
        if best_shown is not None and (not fitting or best_shown >= fitting[0][2]):
            continue  # a window already shown is this scenario's test, as strong as any
        if taken >= max_windows // _SCENARIO_SHARE:
            break
        for index, window, strength in fitting:
            if best_shown is not None and strength <= best_shown:
                break  # no stronger test than the one already shown
            if overlaps(window):
                continue
            size = len(window["text"])
            if taken_chars + size > max_chars // _SCENARIO_SHARE:
                continue
            # Room comes only from windows that may give way (the
            # documentation), never from a word's first window: the
            # lowest request score first, the latest chosen on a tie.
            out: list[int] = []
            free_chars, free_windows = max_chars - used, max_windows - len(chosen)
            for i in sorted(
                (
                    i
                    for i, (_, w) in enumerate(chosen)
                    if may_give_way(w["path"])
                    and not _under(w["path"], fit.test_roots)
                    and not w.get("_scenario")
                    and not any(w is k for k in keep)
                ),
                key=lambda i: (chosen[i][1].get("score", 0), -i),
            ):
                if size <= free_chars and free_windows >= 1:
                    break
                out.append(i)
                free_chars += len(chosen[i][1]["text"])
                free_windows += 1
            if size > free_chars or free_windows < 1:
                continue
            for i in sorted(out, reverse=True):
                used -= len(chosen[i][1]["text"])
                del chosen[i]
            window["_scenario"] = True
            chosen.append((index, window))
            used += size
            taken, taken_chars = taken + 1, taken_chars + size
            break
    return used


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
    # The factory's own scripts, where it puts them and reading as them.
    looked_at: dict[str, str | None] = {}
    candidates = [
        p
        for p in candidates
        if shipped_script_path(p) is None or not is_factory_file(reader, p, looked_at)
    ]
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
        by_hand = any(
            word in line.lower() and len(line) <= MACHINE_WRITTEN_LINE_CHARS for line in lines
        )
        ranked.append((by_hand, words_starting_in(others, text), density, path))
    # Files holding the word only on machine-written lines go last.
    ranked.sort(key=lambda row: (not row[0], -row[1], -row[2], row[3]))
    order = [row[3] for row in ranked] + sorted(unread)
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
    lowest-scoring ones first (the later word's on a tie), and a scenario's
    own test window (:func:`choose_windows`) only after every other; each one's hits
    are added to its entry's ``more_hits``, and the other words' hits it
    alone showed to theirs. Past theirs the candidates lose
    lines, second lines before first ones, from the lowest-ranked candidate
    up; each entry counts them in ``lines_trimmed`` and is no longer
    ``listed_all``. The private ``_covers``, ``_covers_other`` and
    ``_scenario`` marks are always removed.
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
            (bool(window.get("_scenario")), window.get("score", 0), -index, -position, index, window)
            for index, entry in enumerate(entries)
            for position, window in enumerate(entry.get("evidence") or [])
        ),
        key=lambda row: (row[0], row[1], row[2], row[3]),
    )
    for _scenario, _score, _i, _p, index, window in ranked:
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
            window.pop("_scenario", None)
    return {
        "chars_before": before,
        "chars_after": windows_after + total,
        "windows_trimmed": windows_trimmed,
        "lines_trimmed": lines_trimmed,
    }
