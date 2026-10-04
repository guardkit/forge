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
    "ExampleKind",
    "ExampleReview",
    "ExampleWords",
    "example_words_from",
    "read_example_words",
    "review_examples",
    "worked_examples_in",
]

#: The block in the project's ``.guardkit/config.yaml``, and the list in it.
DECLARATION_KEY = "spec_examples"
_KINDS_KEY = "not_asked_for"

#: A word that makes the next three words negative ("takes no parameters").
_NEGATION = re.compile(r"(no|not|without|never|nor|none)", re.IGNORECASE)

#: A sentence that tells the writer what NOT to do ("Do not write scenarios
#: about date ranges") licenses nothing it names.
_NEGATED_SENTENCE = re.compile(r"(do not|don't|never|no|drop|remove)\b", re.IGNORECASE)

#: How a sentence ends, for finding the sentence a phrase is in.
_SENTENCE_END = re.compile(r"[.!?]\s")

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
            if _NEGATED_SENTENCE.match(sentence):
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

    @property
    def flagged_titles(self) -> list[str]:
        return [finding.title for finding in self.findings]

    def note(self) -> str:
        """The machine's note to the spec writer. It never orders removal
        outright: the words can be wrong, so the writer may keep what the
        request needs, and says why in the example's own ``# Why:`` line."""
        lines = ["These worked examples look like things the request does not mention:"]
        lines += [f"- {finding.quoted}" for finding in self.findings]
        lines += [
            "",
            "Remove each one unless the request needs it. If you keep one, quote "
            "the words of the request that need it in its # Why: line. Remove any "
            "assumption written only for an example you remove. Do not add other "
            "examples of the same kind. Keep every other worked example exactly "
            "as it is.",
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
    request = " ".join(" ".join([request_text or "", *[str(n) for n in notes]]).split())
    review = ExampleReview()
    for title, text in worked_examples_in(feature_text):
        review.titles.append(title)
        found = tuple(kind.name for kind in kinds if kind.in_example(text) and not kind.in_request(request))
        if found:
            review.findings.append(ExampleFinding(title, found))
    return review
