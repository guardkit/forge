"""The finding anchor — the fix journey's stable dedup key.

Leg-invocation stage-2 design §5 (`leg-invocation-stage2-design-2026-08-02`),
FB3.

Why an anchor at all
--------------------

The runaway crossing ran ~200 legs because nothing could tell the pipeline
that two review cycles had found *the same defect*. Two measured facts killed
the obvious keys:

* **The fix-task id is not an identity.** It is prose+position-derived
  (``TASK-{prefix}-{ordinal}-{slugified-title[:50]}``) — 88 distinct ids for
  ~5 actual defects.
* **The turn fingerprint never repeats adjacently on a fix journey.** Review
  rows 347 / 355 / 363 / 371 emitted BYTE-IDENTICAL fix-task lists four
  cycles running and the turn-level nothing-changed streak never reached its
  limit, because a ``/task-work`` turn sits between every pair of reviews.

The measured stable key is the finding's LOCATION: 162 findings collapsed to
31 ``(file, line)`` pairs and 8 files, and the line drifted (14 / null / 0 /
36) across cycles for a single defect while the file survived. So the anchor
is ``<repo-relative file>|<severity>``, and ``line`` stays a data field on the
finding, never part of its identity.

The coarseness is NAMED and accepted (design §5): file+severity collapses
distinct same-file defects. At ``max_review_cycles: 2`` the cost is at most
one early stop on a legitimately-progressing same-file journey — and the stop
NAMES its anchors, so the reader can see exactly why it fired.

Who mints the anchor
--------------------

**The producer does.** The review leg attaches an explicit ``anchor`` field to
every finding in its ``## Detection Findings`` block
(``guardkit/orchestrator/review_runner.py`` — ``finding_anchor`` /
``anchored_findings``). That field is authoritative and is used verbatim
whenever it is present: the rule is stated once, on the side that knows the
repo root.

:func:`derive_finding_anchors` therefore does two jobs, in this order:

1. **Read** the producer's ``anchor`` field.
2. **Fall back**, tolerantly, for a findings block minted before the producer
   emitted anchors (or by a hand-written fixture / a replay), deriving
   ``<file>|<severity>`` from the finding's own fields.

The fallback deliberately mirrors the producer's observable output shape —
same two-part string, same ``(no file)`` / ``unspecified`` sentinels, same
trailing-``:line`` strip — so an anchored review and a legacy one are
comparable across a cycle boundary. It cannot share the producer's *code*:
that lives in the sibling checkout, which forge does not import. What it can
do is state the difference honestly, which is what this docstring is for. The
one behaviour it does not replicate is absolute-path relativization: this
seam is handed findings, not a repo root, so an absolute path is kept
verbatim rather than guessed at.

The module is stdlib-only and I/O-free on purpose: the conductor's turn loop
(:mod:`forge.pipeline.conductor_driver`) is a domain module with no import
edge to persistence, and it needs this vocabulary.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

__all__ = [
    "ANCHOR_FIELD",
    "ANCHOR_NO_FILE",
    "ANCHOR_NO_SEVERITY",
    "FINDING_ANCHORS_DETAILS_KEY",
    "derive_finding_anchors",
    "finding_anchor",
]


#: ``details_json`` key carrying a ``task-review`` row's finding anchors.
#: A JSON array of anchor strings, in the order the review reported them.
#: An ABSENT key means "this row predates the anchor thread" — a legacy row,
#: which the no-progress rule treats as *no baseline*, never as "no findings".
FINDING_ANCHORS_DETAILS_KEY: str = "finding_anchors"

#: The field the producer stamps on each finding. Read first, always.
ANCHOR_FIELD: str = "anchor"

#: What the anchor says when a finding names no file at all. A constant rather
#: than a bare ``""`` so an anchor is always a readable two-part string.
#: Mirrors ``review_runner.ANCHOR_NO_FILE`` in the builder checkout.
ANCHOR_NO_FILE: str = "(no file)"

#: What the anchor says when a finding names no severity. The review protocol
#: requires one; the fallback never assumes compliance. Mirrors
#: ``review_runner.ANCHOR_NO_SEVERITY``.
ANCHOR_NO_SEVERITY: str = "unspecified"

#: A trailing ``:88`` / ``:88:4`` on a file value. The model is asked for
#: ``file`` and ``line`` separately but often writes ``src/parser.py:88`` —
#: the line must not smuggle itself into the identity through the file half.
_TRAILING_LINE_RE = re.compile(r":\d+(?::\d+)?$")


def _normalize_file(raw: Any) -> str:
    """Normalize a finding's ``file`` value into the anchor's first half."""
    if not isinstance(raw, str):
        return ANCHOR_NO_FILE
    text = raw.strip().replace("\\", "/")
    if not text:
        return ANCHOR_NO_FILE
    text = _TRAILING_LINE_RE.sub("", text)
    while text.startswith("./"):
        text = text[2:]
    return text or ANCHOR_NO_FILE


def _normalize_severity(raw: Any) -> str:
    """Normalize a finding's ``severity`` into the anchor's second half."""
    if not isinstance(raw, str):
        return ANCHOR_NO_SEVERITY
    return raw.strip().lower() or ANCHOR_NO_SEVERITY


def finding_anchor(finding: Mapping[str, Any]) -> str:
    """Return the anchor for one finding.

    The producer's own ``anchor`` field wins whenever it is a non-empty
    string. Otherwise the two-part fallback is derived from ``file`` and
    ``severity`` — see the module docstring for why that division of labour
    is deliberate.
    """
    existing = finding.get(ANCHOR_FIELD)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return (
        f"{_normalize_file(finding.get('file'))}"
        f"|{_normalize_severity(finding.get('severity'))}"
    )


def derive_finding_anchors(findings: Iterable[Any] | None) -> tuple[str, ...]:
    """Return the anchors of ``findings``, order-preserving and de-duplicated.

    Never raises: a findings block is model output that crossed a process
    boundary, so anything that is not a mapping is skipped rather than
    allowed to take a turn down with it. ``None`` and an empty iterable both
    answer ``()`` — the CALLER decides what an empty anchor set means (the
    no-progress rule reads it as "no readable findings block" and fails
    closed; see :mod:`forge.pipeline.conductor_driver`).
    """
    anchors: list[str] = []
    for finding in findings or ():
        if not isinstance(finding, Mapping):
            continue
        anchor = finding_anchor(finding)
        if anchor not in anchors:
            anchors.append(anchor)
    return tuple(anchors)
