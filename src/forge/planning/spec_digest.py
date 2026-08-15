"""The spec digest and its deterministic consistency check (machine chain, stage 2).

The digest is the plain-language list a person actually reads before saying yes
to a spec: one sentence per worked example, plus the machine's assumptions. This
module is the reason that read can be trusted to be COMPLETE — it compares the
digest against the spec element by element, so a dropped example is a missing
key rather than a lowered count.

Pure, model-free, offline. One named error string per independent mismatch, each
carrying :data:`DIGEST_ERROR_PREFIX` so the caller can separate a digest failure
(which STOPS the run — nothing downstream of it can check a digest but a
person's eyes) from the advisory self-check errors, which real oracles follow.

WHY THIS CODE EXISTS HERE, HONESTLY
-----------------------------------
The spec-writer runs its OWN copy of this check inside the mode that writes the
digest (specialist-agent, ``roles/product_owner/modes/feature_spec.py``:
``check_digest_consistency``). This module is a deliberate MIRROR of it, not a
second design: the two repos share no package and forge declares no dependency
on specialist-agent, so "call the same function again" is not available across
the wire. The mirror earns its place because the specialist's copy runs on the
model's own text, and the ``.feature`` that is COMMITTED is not that text — the
normalizer rewrites it in place at pre-commit (collapsing wrapped steps,
commenting out box-drawing dividers). A digest proven only against the
pre-normalization spec is a digest about an artifact nobody builds from.

Drift between the two copies is the real risk and it is met head-on: the shared
behaviour is pinned by ``tests/forge/planning/test_spec_digest.py``, whose
fixtures are the same shapes the specialist's own suite drives, and every gate
below names its opposite number. A change to one copy without the other is a
test failure, not a silent divergence.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from typing import Any

__all__ = [
    "DIGEST_ERROR_PREFIX",
    "check_digest_consistency",
    "parse_scenarios",
]

#: Every digest error string starts with this. The spec leg separates these
#: from the other, advisory gate errors and STOPS the run on them: an unproven
#: digest must never be shown to a person as if it were proven.
DIGEST_ERROR_PREFIX = "spec digest: "

#: The drafted block of brief-stage assumptions, REMOVED from the contract: no
#: code can check it against the record it claims to quote (gate 9).
_EARLIER_ASSUMPTIONS_KEY = "earlier_assumptions"

#: A whole-line comment in a feature file. Stripped before the scenario scan —
#: see :func:`parse_scenarios`.
_GHERKIN_COMMENT_RE = re.compile(r"(?m)^[ \t]*#.*$")

#: Scenario block: captures tags (same or separate line) + the header + body.
_SCENARIO_BLOCK_RE = re.compile(
    r"^\s*((?:@(?:[^\s]+)\s+)*)"
    r"(Scenario(?:\s+Outline)?:\s+[^\n]+)"
    r"(.*?)(?=\n\s*(?:@(?:[^\s]+)\s+)*Scenario|\n\Z)",
    re.MULTILINE | re.DOTALL,
)

#: An independent count of scenario headers (gate 2's tripwire), deliberately a
#: different regex from the block scan so one parser defect cannot take both.
_SCENARIO_HEADER_COUNT_RE = re.compile(r"^\s*Scenario(?: Outline)?:", re.MULTILINE)

#: A full ISO 8601 stamp (gate 7).
_ISO8601_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

#: Gherkin step keywords. A digest sentence is plain English, so a step line
#: inside one is a defect (gate 5).
_STEP_KEYWORDS = ("Given", "When", "Then", "And", "But")

#: Step keywords that never open a plain-English sentence. "When" is missing on
#: purpose: "When the service restarts, the queue is drained." is ordinary
#: English, and refusing it would reject good digests.
_STEP_KEYWORDS_AT_START = ("Given", "Then", "And", "But")

#: Gate 5's anchor: a step keyword starting a line of its own INSIDE the
#: sentence — the shape of pasted Gherkin — OR one of the four keywords that
#: cannot open English opening the sentence itself.
_DIGEST_STEP_LINE_RE = re.compile(
    r"\n\s*(?:" + "|".join(_STEP_KEYWORDS) + r")\s"
    r"|\A(?:" + "|".join(_STEP_KEYWORDS_AT_START) + r")\s"
)

#: Gate 4's CANDIDATE sentence break: a full stop, question mark or exclamation
#: mark followed by whitespace. A candidate only — an abbreviation ends in a
#: full stop too, so every hit is re-checked against the tail pattern below.
_MID_SENTENCE_BREAK_RE = re.compile(r"[.?!]\s")

#: Abbreviations that end in a full stop and do NOT end a sentence. Named and
#: closed on purpose: a general "is this really a sentence end?" heuristic is
#: exactly the judgement this check is not allowed to make. "no." is
#: deliberately ABSENT — a sentence really can end on the word "no", and
#: swallowing that break would let a genuine two-sentence digest through.
_DIGEST_ABBREVIATIONS: tuple[str, ...] = (
    "e.g.",
    "i.e.",
    "etc.",
    "cf.",
    "vs.",
    "approx.",
    "a.m.",
    "p.m.",
    "U.S.",
    "U.K.",
    "E.U.",
    "Dr.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "Prof.",
    "St.",
    "Jr.",
    "Sr.",
    "Inc.",
    "Ltd.",
    "Co.",
)

#: One of the abbreviations above sitting at the END of the text preceding a
#: candidate break. The leading guard stops an ordinary word whose tail happens
#: to spell an abbreviation from suppressing a real break ("the bus." must not
#: read as "vs.").
_ABBREVIATION_TAIL_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(abbrev) for abbrev in _DIGEST_ABBREVIATIONS)
    + r")$",
    re.IGNORECASE,
)


def _has_mid_sentence_break(sentence: str) -> bool:
    """True iff ``sentence`` is more than one sentence (gate 4's engine)."""
    body = sentence[:-1]
    for match in _MID_SENTENCE_BREAK_RE.finditer(body):
        if _ABBREVIATION_TAIL_RE.search(body[: match.start() + 1]):
            continue
        return True
    return False


def _iso_text(value: Any) -> str:
    """A timestamp value as the text a person wrote, not the way Python prints it.

    YAML resolves an unquoted ``2026-07-09T14:32:00Z`` to a ``datetime``, whose
    ``str()`` is ``2026-07-09 14:32:00+00:00`` — a space where the ``T`` belongs.
    ``isoformat()`` gives the shape back; anything else is stringified unchanged
    so a genuinely malformed stamp still fails.
    """
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value or "")


def _with_final_newline(feature_text: str) -> str:
    """``feature_text`` guaranteed to end in a newline.

    The block regex closes a block on the next scenario header or on ``\\n\\Z``,
    so a feature whose text ends flush against its last step silently loses its
    final worked example. One newline restores it.
    """
    return feature_text if feature_text.endswith("\n") else feature_text + "\n"


def parse_scenarios(feature_text: str) -> list[tuple[str, list[str]]]:
    """``[(title, tags)]`` for every scenario in ``feature_text``, in file order.

    Comments are STRIPPED first because the block regex requires the tag line to
    sit immediately above its ``Scenario:`` header, and the spec-writer's own
    prompt asks for a ``# Why:`` comment "on the line above each scenario" —
    precisely that gap. With a comment in between the header still matches but
    the tag group comes back EMPTY, so gate 3 reported every scenario's labels as
    missing on a spec written exactly as instructed. Comments are non-executable
    prose, so nothing a scenario MEANS is lost with them. (The normalizer's
    divider repair comments out box-drawing lines, which makes this strip
    load-bearing on the committed artifact too.)
    """
    prepared = _with_final_newline(_GHERKIN_COMMENT_RE.sub("", feature_text))
    parsed: list[tuple[str, list[str]]] = []
    for match in _SCENARIO_BLOCK_RE.finditer(prepared):
        tags_part = match.group(1) or ""
        header = match.group(2) or ""
        title = header.split(":", 1)[1].strip() if ":" in header else header.strip()
        tags = [token for token in tags_part.split() if token.startswith("@")]
        parsed.append((title, tags))
    return parsed


def check_digest_consistency(
    digest_obj: Any,
    feature_text: str,
    manifest: dict[str, Any] | None,
    slug: str | None = None,
) -> list[str]:
    """Enumerate the digest/feature/manifest consistency gates.

    Nine gates, each independent, each yielding exactly one named error string
    per mismatch. What this guarantees is completeness, correspondence and
    plainness: every worked example is present, matched to the right title and
    labels, and written in English. What it deliberately does NOT judge is
    whether a sentence *describes* its example accurately — nothing
    deterministic can, and that is exactly what the human read is for.

    Omission is structurally impossible: gate 1 is a positional comparison
    against the titles parsed out of the ``.feature`` itself, so dropping an
    example means dropping its title, which gate 1 reports by name. The count in
    gate 2 is derived from the same list, so lowering it adds a second error
    rather than hiding the first.
    """
    errors: list[str] = []

    if not isinstance(digest_obj, dict):
        return [DIGEST_ERROR_PREFIX + "the digest is not a mapping"]

    raw_scenarios = digest_obj.get("scenarios")
    if raw_scenarios is None:
        raw_scenarios = []
    if not isinstance(raw_scenarios, list):
        return [DIGEST_ERROR_PREFIX + "the digest 'scenarios' field is not a list"]

    spec_scenarios = parse_scenarios(feature_text)
    spec_titles = [title for title, _ in spec_scenarios]
    spec_tags = [tags for _, tags in spec_scenarios]

    digest_titles: list[str] = []
    for index, entry in enumerate(raw_scenarios, start=1):
        if not isinstance(entry, dict):
            errors.append(
                DIGEST_ERROR_PREFIX + f"scenario entry #{index} is not a mapping"
            )
            digest_titles.append("")
            continue
        digest_titles.append(str(entry.get("title") or "").strip())

    # --- 1. Title bijection, order-sensitive -----------------------------
    missing = Counter(spec_titles) - Counter(digest_titles)
    extra = Counter(digest_titles) - Counter(spec_titles)
    for title in spec_titles:  # spec order, one report per missing copy
        if missing[title]:
            missing[title] -= 1
            errors.append(
                DIGEST_ERROR_PREFIX
                + f"the worked example {title!r} is in the spec but missing from "
                f"the digest"
            )
    for title in digest_titles:  # digest order, one report per extra copy
        if extra[title]:
            extra[title] -= 1
            errors.append(
                DIGEST_ERROR_PREFIX
                + f"the digest lists {title!r}, which is not a worked example in "
                f"the spec"
            )
    for position, (spec_title, digest_title) in enumerate(
        zip(spec_titles, digest_titles), start=1
    ):
        if spec_title != digest_title:
            errors.append(
                DIGEST_ERROR_PREFIX
                + f"digest entry {position} is {digest_title!r} but worked example "
                f"{position} is {spec_title!r} — the order must match the spec"
            )

    # --- 2. Count tripwire (independent of gate 1 on purpose) -------------
    scenario_total = len(_SCENARIO_HEADER_COUNT_RE.findall(feature_text))
    if len(raw_scenarios) != scenario_total:
        errors.append(
            DIGEST_ERROR_PREFIX
            + f"the digest describes {len(raw_scenarios)} worked examples but the "
            f"spec has {scenario_total}"
        )

    # --- 3–5. Per-entry gates --------------------------------------------
    for position, entry in enumerate(raw_scenarios, start=1):
        if not isinstance(entry, dict):
            continue
        label = digest_titles[position - 1] or f"entry {position}"

        # 3. Tag fidelity — verbatim and order-preserved.
        if position <= len(spec_tags):
            entry_tags = entry.get("tags")
            if entry_tags is None:
                entry_tags = []
            if not isinstance(entry_tags, list):
                errors.append(
                    DIGEST_ERROR_PREFIX + f"the labels for {label!r} are not a list"
                )
            else:
                as_strings = [str(tag) for tag in entry_tags]
                if as_strings != spec_tags[position - 1]:
                    errors.append(
                        DIGEST_ERROR_PREFIX
                        + f"the labels for {label!r} are {as_strings} but the spec "
                        f"has {spec_tags[position - 1]}"
                    )

        # 4. One sentence, non-empty.
        sentence = str(entry.get("sentence") or "")
        stripped = sentence.strip()
        if not stripped:
            errors.append(
                DIGEST_ERROR_PREFIX + f"{label!r} has no plain-English sentence"
            )
        else:
            if stripped[-1] not in ".?!":
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the sentence for {label!r} does not end in a full stop, "
                    f"question mark or exclamation mark"
                )
            if _has_mid_sentence_break(stripped):
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the sentence for {label!r} is more than one sentence"
                )

            # 5. No spec vocabulary — plain English or it is not a digest.
            if _DIGEST_STEP_LINE_RE.search(stripped):
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the sentence for {label!r} contains a specification step "
                    f"line rather than plain English"
                )
            for token in ("Scenario", "Feature:"):
                if token in stripped:
                    errors.append(
                        DIGEST_ERROR_PREFIX
                        + f"the sentence for {label!r} uses the specification word "
                        f"{token!r} — the digest is plain English"
                    )

    # --- 6. Assumption bijection ------------------------------------------
    manifest = manifest or {}
    manifest_assumptions = manifest.get("assumptions") or []
    if not isinstance(manifest_assumptions, list):
        manifest_assumptions = []
    manifest_by_id: dict[str, tuple[str, str]] = {}
    manifest_ids: list[str] = []
    for entry in manifest_assumptions:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        manifest_ids.append(entry_id)
        manifest_by_id[entry_id] = (
            str(entry.get("assumption") or ""),
            str(entry.get("basis") or ""),
        )

    raw_digest_assumptions = digest_obj.get("assumptions")
    if raw_digest_assumptions is None:
        raw_digest_assumptions = []
    if not isinstance(raw_digest_assumptions, list):
        errors.append(
            DIGEST_ERROR_PREFIX + "the digest 'assumptions' field is not a list"
        )
        raw_digest_assumptions = []

    digest_ids: list[str] = []
    for entry in raw_digest_assumptions:
        if not isinstance(entry, dict):
            errors.append(
                DIGEST_ERROR_PREFIX + "a digest assumption entry is not a mapping"
            )
            continue
        entry_id = str(entry.get("id") or "")
        digest_ids.append(entry_id)
        if entry_id in manifest_by_id:
            manifest_text, manifest_basis = manifest_by_id[entry_id]
            text = str(entry.get("text") or "")
            if text != manifest_text:
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the digest wording of assumption {entry_id} differs from "
                    f"the assumptions file"
                )
            # The BASIS is why the machine assumed it — the half that tells a
            # reader whether to trust it. Checking the assumption but not its
            # reason let a fabricated justification ride onto the card under a
            # verbatim assumption.
            basis = str(entry.get("basis") or "")
            if basis != manifest_basis:
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the digest reason for assumption {entry_id} differs from "
                    f"the assumptions file"
                )

    if Counter(digest_ids) != Counter(manifest_ids):
        for entry_id in manifest_ids:
            if entry_id not in digest_ids:
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"assumption {entry_id} is in the assumptions file but "
                    f"missing from the digest"
                )
        for entry_id in digest_ids:
            if entry_id not in manifest_ids:
                errors.append(
                    DIGEST_ERROR_PREFIX
                    + f"the digest lists assumption {entry_id}, which is not in "
                    f"the assumptions file"
                )

    # --- 7. Timestamp ------------------------------------------------------
    generated = _iso_text(digest_obj.get("generated"))
    if not _ISO8601_RE.match(generated):
        errors.append(
            DIGEST_ERROR_PREFIX
            + f"the digest 'generated' stamp {generated!r} is not a full ISO 8601 "
            f"timestamp"
        )

    # --- 8. Feature slug ---------------------------------------------------
    if slug is not None:
        digest_slug = str(digest_obj.get("feature") or "")
        if digest_slug != slug:
            errors.append(
                DIGEST_ERROR_PREFIX
                + f"the digest names feature {digest_slug!r} but the spec files are "
                f"{slug!r}"
            )

    # --- 9. No ungated record claims -------------------------------------
    # ``earlier_assumptions`` was a drafted block of brief-stage assumptions,
    # rendered on the card as something the machine "had already assumed".
    # Nothing can check it against the record it claims to quote, which made it
    # free model text shown to a person AS a record — the exact thing this check
    # exists to stop. Refused rather than quietly ignored, so a stale prompt
    # cannot put it back.
    if _EARLIER_ASSUMPTIONS_KEY in digest_obj:
        errors.append(
            DIGEST_ERROR_PREFIX
            + f"the digest carries {_EARLIER_ASSUMPTIONS_KEY!r}, which is not part "
            f"of the digest — nothing can check it against a record, so it must "
            f"not be shown as one"
        )

    return errors
