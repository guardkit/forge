"""F14 review-findings record emission + cross-seam validation.

Forge consumes guardkit as a subprocess black box (seam v1 frozen, DF-001), so
the F14 record is rendered to YAML *natively here* — matching the guardkit F14
schema (``review-findings`` kind, ``guardkit/qa/formats/review_findings.py``)
field-for-field — and validated by shelling ``guardkit qa validate`` across the
frozen CLI boundary. The guardkit pydantic model is never imported.

``render_review_findings`` produces the canonical YAML; ``write_review_findings``
writes it under the configured record dir; ``validate_review_findings`` runs the
guardkit validator and returns a structured result (it never raises for a
validation *failure* — it reports it; it raises only if no guardkit CLI is
resolvable, so an absent validator is loud, not a silent green).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from forge.review_gate.models import Finding, ReviewFindingsRecord

__all__ = [
    "render_review_findings",
    "write_review_findings",
    "validate_review_findings",
    "ValidationResult",
    "GuardkitValidatorUnavailable",
]

#: The guardkit QA format kind for the F14 record.
F14_KIND = "review-findings"

#: Candidate guardkit CLI binaries, in resolution order. The ``guardkit``
#: wrapper delegates unknown subcommands (``qa …``) to ``guardkit-py`` (guardkit
#: `0049ed4d`); ``guardkit-py`` is the direct Python CLI.
_GUARDKIT_BINARIES = ("guardkit", "guardkit-py")


class GuardkitValidatorUnavailable(Exception):
    """No guardkit CLI is resolvable — an absent validator is loud, not green."""


def _finding_to_dict(f: Finding) -> dict:
    """One F14 ``findings[]`` entry — optional keys omitted when unset."""
    d: dict = {
        "id": f.id,
        "dimension": f.dimension,
        "severity": f.severity,
        "status": f.status,
        "summary": f.summary,
    }
    if f.executed_reproduction and f.executed_reproduction.strip():
        d["executed_reproduction"] = f.executed_reproduction
    if f.refuters:
        d["refuters"] = [
            (
                {"who": r.who, "verdict": r.verdict}
                | ({"note": r.note} if r.note else {})
            )
            for r in f.refuters
        ]
    return d


def _record_to_dict(record: ReviewFindingsRecord) -> dict:
    """Serialise the record to the F14 dict shape (``extra='forbid'`` clean)."""
    return {
        "format_version": record.format_version,
        "review_id": record.review_id,
        "subject": {"kind": record.subject.kind, "ref": record.subject.ref},
        "dimensions": list(record.dimensions),
        "findings": [_finding_to_dict(f) for f in record.findings],
        "stats": {
            "findings_total": record.stats.findings_total,
            "confirmed": record.stats.confirmed,
            "refuted": record.stats.refuted,
            "refutations_attempted": record.stats.refutations_attempted,
        },
    }


def render_review_findings(record: ReviewFindingsRecord) -> str:
    """Render the F14 record to canonical YAML (human-readable, diffable)."""
    header = (
        f"# F14 review-findings / refutation record (WS3-S5 adversarial merge "
        f"gate)\n# review_id: {record.review_id}\n"
    )
    body = yaml.safe_dump(
        _record_to_dict(record),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return header + body


def write_review_findings(record: ReviewFindingsRecord, *, root: str | Path) -> str:
    """Write the F14 record under ``root`` and return the written path.

    Filename: ``review-<review_id>.yaml`` (the structured sibling of the prose
    review block in ``docs/reviews/<id>.md``).
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    out = root_path / f"review-{record.review_id}.yaml"
    out.write_text(render_review_findings(record), encoding="utf-8")
    return str(out)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of ``guardkit qa validate review-findings <path>``."""

    ok: bool
    binary: str
    returncode: int
    stdout: str
    stderr: str


def _resolve_guardkit() -> str | None:
    for name in _GUARDKIT_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def validate_review_findings(
    path: str | Path, *, timeout: float = 60.0
) -> ValidationResult:
    """Validate an emitted F14 record via the guardkit CLI (frozen seam).

    Shells ``guardkit qa validate review-findings <path>``. Returns a
    :class:`ValidationResult` (``ok`` reflects exit 0) — a validation *failure*
    is reported, not raised.

    Raises:
        GuardkitValidatorUnavailable: if no guardkit CLI is resolvable — an
            absent validator must be loud, never a silent pass.
    """
    binary = _resolve_guardkit()
    if binary is None:
        raise GuardkitValidatorUnavailable(
            "no guardkit CLI on PATH (looked for "
            f"{', '.join(_GUARDKIT_BINARIES)}) — cannot validate the F14 record; "
            "an absent validator is not a pass."
        )
    proc = subprocess.run(  # noqa: S603 — binary resolved via shutil.which
        [binary, "qa", "validate", F14_KIND, str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return ValidationResult(
        ok=proc.returncode == 0,
        binary=binary,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
