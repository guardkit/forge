"""Review the spec writer's assumptions before a person is asked about them.

WHY (2026-09-13, arm B of the coder comparison). The same sentence, through
the same planning seats, raised two assumptions in one run and four in the
other. The fourth was *"the endpoint requires authentication"* — ``basis:
"Not stated in input; common security practice for analytics endpoints"``.
The coder built exactly that, and the live gate refused it three hours later
with ``expected 200, saw 403``. The spec seat's own coach had scored that
manifest perfectly, because every row of its rubric is about the FORM of an
assumption — six fields present, confidence low, response deferred — and no
row asks whether the assumption should exist at all.

THE RULE this module applies, in one sentence: **an assumption may choose
between readings of what the person said; it may not add something the
person did not say.** "Seven days ending today or yesterday?" is a reading.
"Requires authentication" is an addition.

This is the deterministic half of the planning coach (design
``planning-coach-design-2026-09-13.md`` §4c). It does not trust a model to
read correctly — the 8-bit coach seat misread an absent signal eleven times
in twenty-four on 2026-09-04 — so it decides the edge cases itself, by rule,
and the model's verdict is advisory beside it. Its findings travel two ways:
as the machine's note back to the spec writer (one round, exactly as the
plan leg's note already does), and as a warning under the assumption on the
card when the writer would not remove it.

The keyword list is blunt on purpose. A false positive costs one machine
round; a false negative costs what 2026-09-13 cost. It grows from evidence,
not in advance.

Pure: no I/O, no model, never raises on bad input (an unreadable manifest is
a review with no assumptions and a receipt that says why).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

__all__ = [
    "CONVENTION_CONTRADICTED",
    "INVENTED_REQUIREMENT",
    "AssumptionFinding",
    "AssumptionReview",
    "parse_manifest",
    "review_assumptions",
]

#: The assumption introduces a capability, constraint or behaviour the request
#: does not mention. Critical: it is the class of finding that refused arm B.
INVENTED_REQUIREMENT = "INVENTED_REQUIREMENT"

#: The default the assumption chose contradicts what the repository already
#: does for sibling behaviour (the fact sheet says the siblings take no token;
#: this one demands one).
CONVENTION_CONTRADICTED = "CONVENTION_CONTRADICTED"

#: The classes of thing an assumption may not introduce, each with the words
#: that name it. Order matters only for which class is reported first.
_CAPABILITIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "authentication",
        re.compile(
            r"authenticat|authori[sz]|auth[ -]?token|x-auth|bearer|api[ -]?key|"
            r"\blogin\b|logged[- ]in|credential|\bsign(ed)?[- ]in\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permissions",
        re.compile(r"\bpermission|\brole[- ]based|\badmin[- ]only|access[- ]control", re.IGNORECASE),
    ),
    ("rate limiting", re.compile(r"rate[- ]?limit|throttl", re.IGNORECASE)),
    # "offset" alone would catch "UTC offset" in a perfectly good assumption
    # about day boundaries, and "metadata" alone caught "the build metadata"
    # in a fixture on the first run of the driver's own tests. Each class is
    # named by the phrases that mean the CAPABILITY, not by a word that
    # happens to appear in it.
    (
        "pagination",
        re.compile(r"paginat|page[- ]size|limit and offset|offset (parameter|query|and limit)|next[- ]page cursor", re.IGNORECASE),
    ),
    ("caching", re.compile(r"\bcach(e|ed|ing)\b|\betag\b|max-age", re.IGNORECASE)),
    (
        "a required header",
        re.compile(r"(required|custom|auth\w*|extra|additional|new|request) header|header (is|be|must be) (required|present|sent|supplied)|x-[a-z]+(-[a-z]+)* header", re.IGNORECASE),
    ),
    (
        "a response wrapper",
        re.compile(r"wrapped in|wrapper object|response envelope|\benvelope(d)?\b|(with|carries|includes?) (a |an )?(total|summary|meta)[a-z]* (count|field|object|block)", re.IGNORECASE),
    ),
)

#: A ``basis`` that admits the thing was not asked for. The seat writes these
#: words itself; this only reads them.
_NOT_ASKED = re.compile(
    r"not (explicitly |expressly )?(stated|mentioned|specified|given|asked|"
    r"in the (input|request|description))|absent from the (input|request)|"
    r"common (security |industry |api )?practice|best practice|\btypically\b|"
    r"\busually\b|by convention|standard practice|conventional",
    re.IGNORECASE,
)

#: What the fact sheet says when the repository's siblings take no token, and
#: when they return their data bare. Written by ``repository_facts``; read here.
_FACTS_NO_AUTH = re.compile(
    r"(none of them|neither|no route|does not|do not) (declare|require)s? (an? )?authenticat|"
    r"no authentication dependency|without authentication|take[s]? no token",
    re.IGNORECASE,
)
_FACTS_UNWRAPPED = re.compile(r"unwrapped|bare (list|object)|without a wrapper|plain list", re.IGNORECASE)


@dataclass(frozen=True)
class AssumptionFinding:
    """One thing wrong with one assumption, said plainly."""

    assumption_id: str
    pattern: str
    capability: str
    sentence: str


@dataclass
class AssumptionReview:
    """The manifest as read, and everything found wrong with it."""

    assumptions: list[dict[str, Any]] = field(default_factory=list)
    findings: list[AssumptionFinding] = field(default_factory=list)
    unreadable: str | None = None

    @property
    def flagged_ids(self) -> list[str]:
        seen: list[str] = []
        for finding in self.findings:
            if finding.assumption_id not in seen:
                seen.append(finding.assumption_id)
        return seen

    def note(self) -> str:
        """The machine's note to the spec writer: every flagged assumption,
        word for word, and the one ask."""
        lines = [
            f"The reviewer found {len(self.flagged_ids)} assumption(s) that add "
            "something the request did not ask for:"
        ]
        for finding in self.findings:
            lines.append(f"- {finding.assumption_id}: {finding.sentence}")
        lines.append(
            "Remove these assumptions and every worked example that depends on "
            "them. The specification may choose between readings of what was "
            "asked; it may not add a capability the request does not mention "
            "and the repository does not already use. Change nothing else."
        )
        return "\n".join(lines)

    def card_warning(self, assumption_id: str) -> str | None:
        """The warning a person reads under an assumption the writer kept."""
        for finding in self.findings:
            if finding.assumption_id == assumption_id:
                return f"⚠ not asked for — {finding.sentence}"
        return None

    def receipt(self) -> dict[str, Any]:
        return {
            "assumptions": len(self.assumptions),
            "flagged": list(self.flagged_ids),
            "findings": [
                {
                    "assumption_id": f.assumption_id,
                    "pattern": f.pattern,
                    "capability": f.capability,
                    "sentence": f.sentence,
                }
                for f in self.findings
            ],
            "unreadable": self.unreadable,
        }


def parse_manifest(text: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """The manifest's assumption entries, tolerant of an empty or broken file.

    Returns ``(entries, why_not)``: ``why_not`` is a plain sentence when the
    text could not be read as a manifest, and ``None`` when it could (an
    empty manifest is a manifest).
    """
    if not text or not text.strip():
        return [], None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [], f"the assumptions manifest could not be read as YAML ({type(exc).__name__})"
    if loaded is None:
        return [], None
    if isinstance(loaded, list):
        raw = loaded
    elif isinstance(loaded, dict):
        raw = loaded.get("assumptions") or []
    else:
        return [], "the assumptions manifest is not a list or a mapping"
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            entries.append(item)
    return entries, None


def _text_of(entry: dict[str, Any]) -> str:
    return " ".join(
        str(entry.get(key) or "")
        for key in ("assumption", "text", "scenario", "title")
    )


def _capability_in(text: str) -> tuple[str, re.Pattern[str]] | None:
    for name, pattern in _CAPABILITIES:
        if pattern.search(text):
            return name, pattern
    return None


def review_assumptions(
    manifest_text: str | None,
    *,
    request_text: str,
    repository_facts: str | None = None,
) -> AssumptionReview:
    """Hold every assumption against the request and the repository's facts.

    An assumption is flagged as an INVENTED_REQUIREMENT when it names a
    capability class (authentication, pagination, …) that the request does
    not name, and its own ``basis`` admits the thing was not asked for (or
    gives no basis at all). It is flagged as CONVENTION_CONTRADICTED when the
    fact sheet says the repository's siblings do the opposite — and then the
    contradiction is what the sentence says, because it is the stronger fact.
    An assumption the request itself asks for is never flagged, whatever its
    words.
    """
    entries, why_not = parse_manifest(manifest_text)
    review = AssumptionReview(assumptions=entries, unreadable=why_not)
    request = request_text or ""
    facts = repository_facts or ""
    for entry in entries:
        assumption_id = str(entry.get("id") or entry.get("assumption_id") or "").strip()
        if not assumption_id:
            continue
        text = _text_of(entry)
        found = _capability_in(text)
        if found is None:
            continue
        capability, pattern = found
        if pattern.search(request):
            continue  # the person asked for it; a reading, not an addition
        basis = str(entry.get("basis") or "").strip()
        contradicted = (
            (capability == "authentication" and _FACTS_NO_AUTH.search(facts))
            or (capability == "a response wrapper" and _FACTS_UNWRAPPED.search(facts))
        )
        admitted = not basis or bool(_NOT_ASKED.search(basis))
        quoted = str(entry.get("assumption") or entry.get("text") or "").strip()
        if contradicted:
            sentence = (
                f'"{quoted}" — {capability} is not in the request, and the '
                f"repository's sibling endpoints do the opposite"
            )
            review.findings.append(
                AssumptionFinding(assumption_id, CONVENTION_CONTRADICTED, capability, sentence)
            )
        elif admitted:
            why = f' (the basis says so: "{basis}")' if basis else " (and no basis is given)"
            sentence = f'"{quoted}" — {capability} was not asked for{why}'
            review.findings.append(
                AssumptionFinding(assumption_id, INVENTED_REQUIREMENT, capability, sentence)
            )
    return review
