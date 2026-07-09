"""Data model for the WS3-S5 adversarial merge gate.

This module holds two layers of data:

- The **raw fan-out inputs** — what a reviewer / refuter seat produces before
  the gate has adjudicated anything (:class:`RawFinding`, :class:`RefuterVote`).
  These are *proposals*: a reviewer proposes a finding, a refuter votes on it.
  Neither can set the confirmed/refuted verdict — that is the assembler's job.

- The **F14 emission targets** — the adjudicated record the gate writes and
  ``guardkit qa validate review-findings`` checks (:class:`Finding`,
  :class:`Refuter`, :class:`ReviewStats`, :class:`ReviewFindingsRecord`). Field
  names and value ranges mirror the guardkit F14 schema
  (``guardkit/qa/formats/review_findings.py``, session B10 ``6b4dde0d``)
  EXACTLY — forge consumes guardkit as a subprocess black box (seam v1 frozen,
  DF-001), so the record is emitted natively here and validated across the
  frozen CLI boundary, never by importing the guardkit model.

The two load-bearing rules of the F14 record are enforced by the assembler that
builds these objects, not by these dataclasses (see
:mod:`forge.review_gate.assembler`):

- **Reading is not a verdict (LPA-15).** A ``confirmed`` finding MUST carry an
  ``executed_reproduction``.
- **Adversarial by default (LPA-14).** Every critical/high finding carries ≥2
  independent refuters, each defaulting to *refuted*; a finding survives only
  by surviving refutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# --- Vocabulary (mirrors the guardkit F14 Literals verbatim) ----------------

#: What was reviewed. A working tree (uncommitted) is reviewable (LPA-18).
SubjectKind = Literal["tree", "commit", "merge"]

#: A finding's severity.
Severity = Literal["critical", "high", "medium", "low"]

#: Did the finding survive verification?
FindingStatus = Literal["confirmed", "refuted"]

#: A single refuter's verdict (default refuted — a finding survives refutation).
RefuterVerdict = Literal["refuted", "not_refuted"]

#: Severities that require the adversarial ≥2-refuter treatment (LPA-14).
SERIOUS: frozenset[Severity] = frozenset({"critical", "high"})

#: Runtime value sets for the F14 Literals (used for loud validation of
#: externally-supplied values — the assembler-derived ones are always in range).
_SUBJECT_KINDS: frozenset[str] = frozenset({"tree", "commit", "merge"})
_SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})
_VERDICTS: frozenset[str] = frozenset({"refuted", "not_refuted"})


class ReviewInputError(ValueError):
    """A review-input document / subject is malformed (fail loud, never guess)."""


# --- Raw fan-out inputs (proposals, pre-adjudication) -----------------------


@dataclass(frozen=True, slots=True)
class RefuterVote:
    """One independent refuter's attempt to refute a proposed finding.

    ``verdict`` defaults to ``"refuted"`` — refuted-by-default (LPA-14). A
    refuter promotes a finding towards survival only by returning
    ``"not_refuted"`` (it tried to refute and could not).
    """

    who: str
    verdict: RefuterVerdict = "refuted"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RawFinding:
    """A finding as *proposed* by a dimension reviewer, before adjudication.

    A reviewer proposes a finding and, critically, attaches an
    ``executed_reproduction`` iff it actually ran one. It CANNOT set the
    confirmed/refuted status — the assembler derives that from the executed
    reproduction and the refuter votes.

    Attributes:
        id: Stable finding id within the review (e.g. ``"F-01"``).
        dimension: The review dimension that raised it (must be a dispatched
            dimension).
        severity: One of critical/high/medium/low.
        summary: One-line statement of the defect.
        executed_reproduction: The concrete reproduction that was *executed*
            (command + observed result). Empty/None ⇒ reading-only ⇒ the
            finding cannot reach confirmed (LPA-15).
        refuter_votes: The independent refuter votes dispatched against this
            finding (≥2 required for critical/high — LPA-14).
    """

    id: str
    dimension: str
    severity: Severity
    summary: str
    executed_reproduction: str | None = None
    refuter_votes: tuple[RefuterVote, ...] = ()


# --- F14 emission targets (adjudicated record) ------------------------------


@dataclass(frozen=True, slots=True)
class Refuter:
    """One independent refutation attempt, as recorded in the F14 record."""

    who: str
    verdict: RefuterVerdict = "refuted"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One adjudicated review finding (F14 ``findings[]`` entry)."""

    id: str
    dimension: str
    severity: Severity
    status: FindingStatus
    summary: str
    executed_reproduction: str | None = None
    refuters: tuple[Refuter, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewSubject:
    """What was reviewed and its reference (F14 ``subject``).

    Validated at construction so an out-of-vocabulary ``kind`` or an empty
    ``ref`` fails LOUD on EVERY path (CLI, runner, direct) — not only when the
    optional ``guardkit qa validate`` step runs. ``kind`` must be one of the F14
    Literal values; ``ref`` a non-empty string.
    """

    kind: SubjectKind
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in _SUBJECT_KINDS:
            raise ReviewInputError(
                f"subject.kind must be one of {sorted(_SUBJECT_KINDS)}, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ReviewInputError("subject.ref must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ReviewStats:
    """The refutation tally — the review's honesty signal (F14 ``stats``)."""

    findings_total: int
    confirmed: int
    refuted: int
    refutations_attempted: int


@dataclass(frozen=True, slots=True)
class ReviewFindingsRecord:
    """The full F14 review-findings / refutation record (emission target)."""

    review_id: str
    subject: ReviewSubject
    dimensions: tuple[str, ...]
    findings: tuple[Finding, ...]
    stats: ReviewStats
    format_version: str = "1.0"

    @property
    def confirmed_serious(self) -> tuple[Finding, ...]:
        """Confirmed critical/high findings — the disposition trigger."""
        return tuple(
            f
            for f in self.findings
            if f.status == "confirmed" and f.severity in SERIOUS
        )


_ALLOWED_FINDING_KEYS = {
    "id",
    "dimension",
    "severity",
    "summary",
    "executed_reproduction",
    "refuter_votes",
}
_ALLOWED_VOTE_KEYS = {"who", "verdict", "note"}


def _refuter_vote_from_dict(data: dict) -> RefuterVote:
    unknown = set(data) - _ALLOWED_VOTE_KEYS
    if unknown:
        raise ReviewInputError(f"unknown refuter_vote key(s): {sorted(unknown)}")
    who = data.get("who")
    if not isinstance(who, str) or not who.strip():
        raise ReviewInputError("refuter_vote.who must be a non-empty string")
    verdict = data.get("verdict", "refuted")
    if verdict not in _VERDICTS:
        raise ReviewInputError(
            f"refuter_vote.verdict must be one of {sorted(_VERDICTS)}, got {verdict!r}"
        )
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise ReviewInputError("refuter_vote.note must be a string or absent")
    return RefuterVote(who=who, verdict=verdict, note=note)


def raw_finding_from_dict(data: dict) -> RawFinding:
    """Parse one raw finding proposal from a review-input dict (loud on error).

    A reviewer proposal deliberately CANNOT carry a ``status`` — the verdict is
    the gate's to derive, never the reviewer's to assert (that is exactly the
    honesty the gate exists to enforce). ``status`` in the input is rejected.
    """
    if not isinstance(data, dict):
        raise ReviewInputError(f"finding must be a mapping, got {type(data).__name__}")
    if "status" in data:
        raise ReviewInputError(
            "a review-input finding must not carry 'status' — the confirmed/"
            "refuted verdict is derived by the gate, never asserted by a reviewer"
        )
    unknown = set(data) - _ALLOWED_FINDING_KEYS
    if unknown:
        raise ReviewInputError(f"unknown finding key(s): {sorted(unknown)}")
    fid = data.get("id")
    if not isinstance(fid, str) or not fid.strip():
        raise ReviewInputError("finding.id must be a non-empty string")
    dimension = data.get("dimension")
    if not isinstance(dimension, str) or not dimension.strip():
        raise ReviewInputError(f"finding {fid!r}: dimension must be a non-empty string")
    severity = data.get("severity")
    if severity not in _SEVERITIES:
        raise ReviewInputError(
            f"finding {fid!r}: severity must be one of {sorted(_SEVERITIES)}, "
            f"got {severity!r}"
        )
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ReviewInputError(f"finding {fid!r}: summary must be a non-empty string")
    repro = data.get("executed_reproduction")
    if repro is not None and not isinstance(repro, str):
        raise ReviewInputError(
            f"finding {fid!r}: executed_reproduction must be a string or absent"
        )
    votes_raw = data.get("refuter_votes", [])
    if not isinstance(votes_raw, list):
        raise ReviewInputError(f"finding {fid!r}: refuter_votes must be a list")
    votes = tuple(_refuter_vote_from_dict(v) for v in votes_raw)
    return RawFinding(
        id=fid,
        dimension=dimension,
        severity=severity,
        summary=summary,
        executed_reproduction=repro,
        refuter_votes=votes,
    )


def raw_findings_from_input(data: dict) -> tuple[RawFinding, ...]:
    """Parse the ``findings`` list of a review-input document."""
    if not isinstance(data, dict):
        raise ReviewInputError("review-input must be a mapping")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ReviewInputError("review-input.findings must be a list")
    return tuple(raw_finding_from_dict(f) for f in findings)


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    """The assembled review packet dispatched to the reviewer fan-out.

    Attributes:
        review_id: The id assigned to this review (drives the record filename).
        subject: What is under review (tree/commit/merge + ref).
        feature: The agent-built feature under review (label only).
        dimensions: The dimensions to dispatch one reviewer each.
        diff_ref: Optional pointer to the diff/artifact the reviewers read
            (a path or range string — the packet does not embed bytes).
        context: Free-form extra context strings handed to every reviewer.
    """

    review_id: str
    subject: ReviewSubject
    feature: str
    dimensions: tuple[str, ...]
    diff_ref: str | None = None
    context: tuple[str, ...] = field(default_factory=tuple)
