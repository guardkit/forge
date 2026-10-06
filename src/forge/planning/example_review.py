"""Check the spec's worked examples against the request before a person reads them.

WHY (4 October 2026). The spec writer pads its worked examples with things the
request never mentioned: a POST to a GET endpoint, a database outage, a
thousand requests at once. Changing its prompt did not stop it. So, before
the spec card goes out, each example is checked against the request and the
owner's earlier notes. An example about something neither mentions goes back
to the writer once, in the same machine rewrite the assumption review already
asks for, and the card says what was removed and what was kept.

WHAT COUNTS AS PADDING BELONGS TO THE PROJECT. This module holds no project
words. A project lists its own, in an optional ``spec_examples:`` block in
its ``.guardkit/config.yaml`` (the file the factory already reads for
``repository_facts:`` and the rest)::

    spec_examples:
      not_asked_for:
        - name: a dependency being down
          example_words: ["database * unavailab*", "outage*"]
          request_words: ["unavailab*", "outage*", "ready"]

An example is flagged for a kind when it uses one of the kind's
``example_words`` and neither the request nor the owner's notes use one of
its ``request_words`` (the ``example_words`` themselves when there are
none). Each phrase is matched as whole words, case ignored; a ``*`` straight
after letters means any ending, and a ``*`` on its own stands for up to two
words. A project with no block is not checked, and nothing changes for it.

"Uses" means uses positively. The only words this module knows are the
English words that make a phrase negative: a phrase within three words after
"no", "not", "without", "never", "nor" or "none", or in a sentence that
starts "Do not", "Don't", "Never", "No", "Drop" or "Remove", is not counted.
So "takes no parameters" in a request licenses nothing, and "does not
require authentication" in an example only restates the request.

TWO MORE CHECKS, FOR EVERY PROJECT (6 October 2026, planning improvements
item 4). Neither uses any project's words:

* **the quote is real** (:func:`untraced_examples`): each worked example's
  ``# Why:`` line must hold a double-quoted span of at least three words that
  appears, case, spacing and punctuation aside, in the request, an owner's
  note or a project document the writer was given. This decides only that
  the quote exists, never that it supports the example. When more than half
  of a draft's examples fail it, the draft came from a writer that does not
  quote yet, and these findings are dropped for that draft
  (:class:`QuoteCheck` ``guard_fired``);
* **the example asks for no more than its quote** is a judgement the spec
  writer's own checker makes (its ``example_support.json``); this module
  only reads its verdicts (:func:`reading_of`).

All three are merged into one :class:`ExampleReview` (:func:`merge_reviews`),
so the note, the one rewrite and the card stay one. The driver runs the two
new checks only for a draft that carries the checker's
``example_support.json``, which the spec writer writes only once its own
switch is on; without it the 4 October check runs exactly as it did.

Pure: no I/O except through the reader it is handed, no model, never raises.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

__all__ = [
    "DECLARATION_KEY",
    "QUOTE_KIND",
    "READING_KIND",
    "ExampleKind",
    "ExampleReview",
    "ExampleWords",
    "QuoteCheck",
    "check_quotes",
    "example_words_from",
    "merge_reviews",
    "read_example_words",
    "reading_of",
    "review_examples",
    "untraced_examples",
    "why_lines_in",
    "worked_examples_in",
]

#: The block in the project's ``.guardkit/config.yaml``, and the list in it.
DECLARATION_KEY = "spec_examples"
_KINDS_KEY = "not_asked_for"

#: A word that makes the next three words negative ("takes no parameters"),
#: any word ending in n't ("doesn't") among them.
_NEGATION = re.compile(r"(no|not|without|never|nor|none|\w+n['\u2019]t)", re.IGNORECASE)

#: A sentence that tells the writer what NOT to do ("Do not write scenarios
#: about date ranges") licenses nothing it names.
_NEGATED_SENTENCE = re.compile(r"(do not|don't|never|no|drop|remove)\b", re.IGNORECASE)

#: How a sentence ends, for finding the sentence a phrase is in: a full
#: stop, question or exclamation mark before a space, a blank line, or a line
#: break before a list item. Not every line break: a request typed with hard
#: line breaks ("Do not write scenarios about rejecting / unauthenticated
#: requests, ... or about date ranges.") is still one sentence.
_SENTENCE_END = re.compile(r"[.!?]\s|\n\s*\n|\n(?=\s*(?:[-*\u2022]|\d+[.)])\s)")

#: A list marker at the start of a sentence ("- Drop the POST example").
_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s*")

#: The punctuation stripped from a word before asking if it is a negation.
_EDGE_PUNCTUATION = ",.;:()\"“”"

_SCENARIO_LINE = re.compile(r"(Scenario(?: Outline)?|Example|Background|Rule|Feature)\s*:\s*(.*)")


# ---------------------------------------------------------------------------
# The project's words
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1024)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """One declared phrase as a whole-words pattern, case ignored."""
    parts = []
    for token in phrase.split():
        if token == "*":
            parts.append(r"(?:[\w-]+\s+){0,2}")
        else:
            parts.append(re.escape(token).replace(r"\*", r"[\w-]*") + r"\s+")
    body = "".join(parts)
    if body.endswith(r"\s+"):
        body = body[: -len(r"\s+")]
    return re.compile(r"(?<![\w-])" + body + r"(?![\w])", re.IGNORECASE)


@dataclass(frozen=True)
class ExampleKind:
    """One kind of example the project does not want unless it is asked for."""

    name: str
    example_words: tuple[str, ...]
    request_words: tuple[str, ...]

    def in_example(self, text: str) -> bool:
        return _uses(self.example_words, text)

    def in_request(self, text: str) -> bool:
        return _uses(self.request_words or self.example_words, text)


@dataclass(frozen=True)
class ExampleWords:
    """What the project declared, or why there is nothing to check against.

    ``kinds`` is ``None`` when there is no check. ``not_checked`` then says
    why for the run's record: no block, or a file that could not be read.
    ``unreadable`` is set instead when the block is there and could not be
    read, which the card says, because silence there would hide a mistake.
    """

    kinds: tuple[ExampleKind, ...] | None
    not_checked: str | None = None
    unreadable: str | None = None

    def receipt(self) -> dict[str, Any]:
        return {
            "kinds": [kind.name for kind in self.kinds] if self.kinds is not None else None,
            "not_checked": self.not_checked,
            "unreadable": self.unreadable,
        }


def _phrases(value: Any, where: str) -> tuple[tuple[str, ...] | None, str | None]:
    """A list of text phrases, or the reason it is not one."""
    if not isinstance(value, list):
        return None, f"{where} is not a list"
    phrases: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None, f"{where} has a phrase that is not text ({item!r})"
        if not item.strip():
            return None, f"{where} has an empty phrase"
        phrases.append(" ".join(item.split()))
    return tuple(phrases), None


def example_words_from(settings: Mapping[str, Any]) -> ExampleWords:
    """The project's kinds, read from its parsed ``.guardkit/config.yaml``."""
    block = settings.get(DECLARATION_KEY)
    if block is None:
        return ExampleWords(None, not_checked=f"the project declares no `{DECLARATION_KEY}` block")

    def unreadable(why: str) -> ExampleWords:
        return ExampleWords(None, not_checked=f"its `{DECLARATION_KEY}` block {why}", unreadable=why)

    if not isinstance(block, Mapping):
        return unreadable("is not a set of settings")
    entries = block.get(_KINDS_KEY)
    if not isinstance(entries, list):
        return unreadable(f"has no `{_KINDS_KEY}` list")
    kinds: list[ExampleKind] = []
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            return unreadable(f"has entry {number} that is not a set of settings")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return unreadable(f"has entry {number} with no name")
        example_words, why = _phrases(entry.get("example_words"), f"the example_words of {name!r}")
        if why is not None:
            return unreadable(why)
        if not example_words:
            return unreadable(f"the example_words of {name!r} are empty")
        request_words: tuple[str, ...] | None = ()
        if entry.get("request_words") is not None:
            request_words, why = _phrases(entry.get("request_words"), f"the request_words of {name!r}")
            if why is not None:
                return unreadable(why)
        kinds.append(ExampleKind(" ".join(name.split()), tuple(example_words or ()), tuple(request_words or ())))
    return ExampleWords(tuple(kinds))


def read_example_words(reader: Any) -> ExampleWords:
    """Read the project's kinds through the planner's repository reader.

    The same reader, the same file and the same bounded parse the fact
    sheet's ``repository_facts:`` block is read with. Never raises: a file
    that cannot be read is no check, and the record says why.
    """
    from forge.planning.declared_memory import DECLARATION_PATH, _parse

    try:
        # A sandbox reader's time allowance is per pass: the fact sheet's
        # pass may have started minutes ago, before the spec writer ran.
        begin = getattr(reader, "begin", None)
        if callable(begin):
            begin()
        text = reader.read_text(DECLARATION_PATH)
    except Exception as exc:  # noqa: BLE001 — a reader that fails is no check, said
        why = (str(exc) or type(exc).__name__)[:200]
        return ExampleWords(None, not_checked=f"`{DECLARATION_PATH}` could not be read ({why})")
    if text is None:
        why = (getattr(reader, "refused", None) or {}).get(DECLARATION_PATH) or "it was not served"
        return ExampleWords(None, not_checked=f"`{DECLARATION_PATH}` was not read ({why})")
    data, why = _parse(text)
    if data is None:
        return ExampleWords(None, not_checked=f"`{DECLARATION_PATH}` could not be parsed ({why})")
    return example_words_from(data)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _uses(phrases: Sequence[str], text: str) -> bool:
    """Whether ``text`` uses one of ``phrases`` positively (not negated)."""
    for phrase in phrases:
        for match in _phrase_pattern(phrase).finditer(text):
            before = text[: match.start()].split()[-3:]
            if any(_NEGATION.fullmatch(word.strip(_EDGE_PUNCTUATION)) for word in before):
                continue
            sentence = _SENTENCE_END.split(text[: match.start()])[-1].strip().lower()
            if _NEGATED_SENTENCE.match(_BULLET.sub("", sentence)):
                continue
            return True
    return False


def worked_examples_in(feature_text: str) -> list[tuple[str, str]]:
    """``[(title, text)]`` for each scenario: its title and steps. Comments
    (the ``# Why:`` lines among them), tags and the Background are left out."""
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in (feature_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        heading = _SCENARIO_LINE.match(stripped)
        if heading:
            if current is not None:
                blocks.append(current)
            kind, title = heading.groups()
            current = None if kind in ("Background", "Rule", "Feature") else (title, [title])
            continue
        if current is not None and stripped and not stripped.startswith("@"):
            current[1].append(stripped)
    if current is not None:
        blocks.append(current)
    return [(title, "\n".join(lines)) for title, lines in blocks]


@dataclass(frozen=True)
class ExampleFinding:
    """One example, word for word, and the kinds it is about."""

    title: str
    kinds: tuple[str, ...]

    @property
    def quoted(self) -> str:
        return f'"{self.title}" ({"; ".join(self.kinds)})'


@dataclass
class ExampleReview:
    """Every example as read, and the ones about something not asked for."""

    titles: list[str] = field(default_factory=list)
    findings: list[ExampleFinding] = field(default_factory=list)
    #: Set only when the quote check and the checker's reading ran (6 October
    #: 2026): the note then asks for the quote copied exactly. Off, the note
    #: is word for word the 4 October note.
    asks_for_exact_quotes: bool = False

    @property
    def flagged_titles(self) -> list[str]:
        return [finding.title for finding in self.findings]

    def listed(self) -> list[str]:
        """The note's list: each flagged example, word for word, and its kinds."""
        lines = ["These worked examples look like things the request does not mention:"]
        return lines + [f"- {finding.quoted}" for finding in self.findings]

    def note(self) -> str:
        """The machine's note to the spec writer. It never orders removal
        outright: the words can be wrong, so the writer may keep what the
        request needs, and says why in the example's own ``# Why:`` line."""
        lines = self.listed()
        quote = (
            "the words of the request that need it in its # Why: line, copied "
            "exactly, in double quotes, and the example asks for nothing more "
            "than those words do."
            if self.asks_for_exact_quotes
            else "the words of the request that need it in its # Why: line."
        )
        lines += [
            "",
            "Remove each one unless the request needs it. If you keep one, quote "
            f"{quote} Remove any assumption written only for an example you "
            "remove. Do not add other examples of the same kind. Keep every "
            "other worked example exactly as it is.",
        ]
        return "\n".join(lines)

    def card_lines(self, first: "ExampleReview | None") -> list[str]:
        """The card's lines for the final draft, measured against the first.

        Removed: flagged in the first draft and gone from this one. Kept:
        flagged in this one, whatever reason the writer gave. Either way the
        examples are named, so a possible loss shows as well as what stays.
        """
        lines: list[str] = []
        removed = [t for t in (first.flagged_titles if first else []) if t not in self.titles]
        if removed:
            lines.append(
                "Removed as not asked for: "
                + "; ".join(f'"{title}"' for title in removed)
                + ". If one of them was needed, send a note."
            )
        if self.findings:
            lines.append(
                "Not asked for, but kept: "
                + "; ".join(finding.quoted for finding in self.findings)
                + ". If you approve, it will be built; to drop it, send a note."
            )
        return lines

    def receipt(self) -> dict[str, Any]:
        return {
            "examples": len(self.titles),
            "flagged": [{"title": f.title, "kinds": list(f.kinds)} for f in self.findings],
        }


def review_examples(
    feature_text: str,
    *,
    request_text: str,
    notes: Sequence[str] = (),
    kinds: Sequence[ExampleKind],
) -> ExampleReview:
    """Hold each worked example against the request and the owner's notes.

    An example is flagged for a kind when it uses the kind's example words
    and neither the request nor any note uses its request words. So an
    example the owner asked for in a note is never flagged.
    """
    # The request and each note are read separately, so one cannot end or
    # negate a sentence of the other.
    asked = [str(text) for text in (request_text, *notes) if text]
    review = ExampleReview()
    for title, text in worked_examples_in(feature_text):
        review.titles.append(title)
        found = tuple(
            kind.name
            for kind in kinds
            if kind.in_example(text) and not any(kind.in_request(said) for said in asked)
        )
        if found:
            review.findings.append(ExampleFinding(title, found))
    return review


# ---------------------------------------------------------------------------
# The quote is real, and the example asks for no more than it (6 October 2026)
# ---------------------------------------------------------------------------

#: The plain words the card and the note use for each new finding.
QUOTE_KIND = "it quotes no words of the request"
READING_KIND = "it asks for more than the words it quotes"

#: A double-quoted span, straight or curly.
_QUOTED = re.compile(r'"([^"\n]+)"|\u201c([^\u201d\n]+)\u201d')
_WHY = re.compile(r"#\s*why\s*:", re.IGNORECASE)
_NOT_A_WORD = re.compile(r"[^0-9a-z]+")
_MIN_QUOTE_WORDS = 3


def _normal(text: str) -> str:
    """Case, spacing and punctuation aside: lower-case words, one space apart."""
    return " ".join(_NOT_A_WORD.sub(" ", str(text or "").lower()).split())


def why_lines_in(feature_text: str) -> list[tuple[str, str]]:
    """``[(title, why)]`` for each scenario, in the order
    :func:`worked_examples_in` gives them: the ``# Why:`` comment (and any
    comment lines after it) in the block of comments and tags directly above
    the scenario's heading; ``""`` when there is none."""
    found: list[tuple[str, str]] = []
    block: list[str] = []
    for line in (feature_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            block.append(stripped)
            continue
        if stripped.startswith("@") or not stripped:
            continue
        heading = _SCENARIO_LINE.match(stripped)
        if heading:
            kind, title = heading.groups()
            if kind not in ("Background", "Rule", "Feature"):
                why: list[str] = []
                for comment in block:
                    if why or _WHY.match(comment):
                        why.append(comment.lstrip("#").strip())
                found.append((title, " ".join(why)))
        block = []
    return found


def _is_traced(why: str, sources: Sequence[str]) -> bool:
    normal_sources = [f" {_normal(source)} " for source in sources if source]
    for match in _QUOTED.finditer(why or ""):
        span = _normal(match.group(1) or match.group(2) or "")
        if len(span.split()) < _MIN_QUOTE_WORDS:
            continue
        if any(f" {span} " in source for source in normal_sources):
            return True
    return False


def untraced_examples(feature_text: str, *, sources: Sequence[str]) -> list[str]:
    """The titles of the worked examples whose ``# Why:`` line quotes nothing
    found in ``sources`` (the request, the owner's notes and the project
    documents the writer was given). Decides only that a quote exists."""
    return [title for title, why in why_lines_in(feature_text) if not _is_traced(why, sources)]


@dataclass(frozen=True)
class QuoteCheck:
    """One draft's quote check. ``untraced`` is empty when the guard fired."""

    examples: int
    untraced: tuple[str, ...]
    guard_fired: bool = False
    #: How many failed before the guard dropped them.
    failed: int = 0

    @property
    def checked(self) -> bool:
        return self.examples > 0

    def receipt(self) -> dict[str, Any]:
        return {
            "examples": self.examples,
            "failed": self.failed,
            "guard_fired": self.guard_fired,
            "untraced": list(self.untraced),
        }


def check_quotes(feature_text: str, *, sources: Sequence[str]) -> QuoteCheck:
    """The quote check with its guard: when more than half of a draft's
    examples fail, that draft came from a writer that does not quote yet, so
    its quote findings are dropped and the guard says so. Never raises."""
    try:
        examples = len(why_lines_in(feature_text))
        untraced = untraced_examples(feature_text, sources=sources)
    except Exception:  # noqa: BLE001 — a reviewer must never stop a run
        return QuoteCheck(examples=0, untraced=())
    if examples and len(untraced) * 2 > examples:
        return QuoteCheck(examples, (), guard_fired=True, failed=len(untraced))
    return QuoteCheck(examples, tuple(untraced), failed=len(untraced))


def reading_of(example_support: Any, titles: Sequence[str]) -> tuple[str, list[str]]:
    """The checker's reading, as ``(status, titles that ask for more)``.

    ``example_support`` is what the driver kept from the spec writer's
    ``example_support.json``. Only a ``checked`` status counts; a title that
    is not one of this draft's examples is dropped. Nothing there is ``""``.
    """
    if not isinstance(example_support, Mapping):
        return "", []
    status = str(example_support.get("status") or "")
    if status != "checked":
        return status, []
    beyond: list[str] = []
    for entry in example_support.get("goes_beyond") or []:
        title = str(entry.get("title") or "").strip() if isinstance(entry, Mapping) else ""
        if title and title in titles and title not in beyond:
            beyond.append(title)
    return status, beyond


def merge_reviews(
    titles: Sequence[str],
    project: "ExampleReview | None",
    *,
    untraced: Sequence[str] = (),
    goes_beyond: Sequence[str] = (),
) -> ExampleReview:
    """One review from the project's words, the quote check and the
    checker's reading: an example flagged more than once carries every kind,
    in that order, and the examples keep the draft's own order."""
    kinds: dict[str, list[str]] = {}
    for finding in project.findings if project is not None else []:
        kinds.setdefault(finding.title, []).extend(finding.kinds)
    for title in untraced:
        kinds.setdefault(title, []).append(QUOTE_KIND)
    for title in goes_beyond:
        kinds.setdefault(title, []).append(READING_KIND)
    merged = ExampleReview(titles=list(titles), asks_for_exact_quotes=True)
    for title in dict.fromkeys(titles):
        if title in kinds:
            merged.findings.append(ExampleFinding(title, tuple(dict.fromkeys(kinds[title]))))
    return merged
