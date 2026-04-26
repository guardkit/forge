"""Tolerant parser for GuardKit subprocess output (TASK-GCI-004).

Converts a raw GuardKit subprocess outcome — ``(stdout, stderr,
exit_code, duration_secs, timed_out)`` — into the canonical
:class:`forge.adapters.guardkit.models.GuardKitResult` shape consumed by
the tool wrappers, the reasoning model, and the dispatch layer.

The parser is **tolerant by design** (per
``docs/design/contracts/API-subprocess.md`` §3.4 and ADR-ARCH-025):

- Unknown stdout shapes degrade to ``status="success"`` with empty
  ``artefacts`` rather than failing the whole call. The reasoning model
  decides whether a stage produced useful work — not the parser.
- Internal exceptions (malformed JSON, regex failures, …) are caught
  and surfaced as a structured
  :class:`forge.adapters.guardkit.models.GuardKitWarning` with code
  ``"parser_unrecognised_shape"``. The function **never raises** past
  its own boundary.

The parser is a pure function over its inputs — it must not import the
subprocess wrapper, file I/O, or any transport package.
"""

from __future__ import annotations

import json
import re
from typing import Any

from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning

_STDOUT_TAIL_BYTES = 4096  # ASSUM-003

# Recognised shapes — kept small and regex-based on purpose. The
# Implementation Notes in TASK-GCI-004 prefer a "simple regex pass +
# JSON-block detection" over a full parser.
_ARTEFACTS_SECTION_RE = re.compile(
    r"^##\s+Artefacts\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE,
)
_ARTEFACT_LINE_RE = re.compile(r"^\s*-\s+(\S.*?)\s*$", re.MULTILINE)
_COACH_SCORE_RE = re.compile(
    r"^\s*coach_score\s*:\s*([+-]?\d+(?:\.\d+)?)\s*$",
    re.MULTILINE,
)
_COACH_BREAKDOWN_SECTION_RE = re.compile(
    r"^##\s+Coach\s+Breakdown\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE,
)
_BREAKDOWN_ROW_RE = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*([+-]?\d+(?:\.\d+)?)\s*\|\s*$",
    re.MULTILINE,
)
_DETECTION_FINDINGS_SECTION_RE = re.compile(
    r"^##\s+Detection\s+Findings\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE,
)
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```",
    re.MULTILINE,
)


def parse_guardkit_output(
    *,
    subcommand: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    duration_secs: float,
    timed_out: bool = False,
) -> GuardKitResult:
    """Parse a GuardKit subprocess outcome into the canonical result shape.

    Tolerant: unknown stdout shapes still return ``status="success"`` with
    empty artefacts (the reasoning model decides whether the stage produced
    useful work). Never raises — internal exceptions are caught and folded
    into ``GuardKitResult.warnings``.

    Parameters
    ----------
    subcommand:
        GuardKit subcommand label (e.g. ``"feature-spec"``).
    stdout:
        Captured standard output of the subprocess.
    stderr:
        Captured standard error of the subprocess.
    exit_code:
        Process exit code (``0`` = success, non-zero = failure).
    duration_secs:
        Wall-clock duration of the subprocess invocation.
    timed_out:
        ``True`` when the subprocess wrapper killed the process for
        exceeding its timeout. Takes precedence over ``exit_code``.

    Returns
    -------
    GuardKitResult
        Canonical result shape. Status is determined as follows:

        - ``timed_out=True`` → ``"timeout"``
        - ``exit_code != 0`` → ``"failed"``
        - otherwise → ``"success"`` (recognised or unknown shape)
    """
    warnings: list[GuardKitWarning] = []
    stdout_tail = _tail_bytes(stdout, _STDOUT_TAIL_BYTES)

    # Status arbitration — timeout outranks exit_code per AC-002.
    if timed_out:
        status: str = "timeout"
    elif exit_code != 0:
        status = "failed"
    else:
        status = "success"

    artefacts: list[str] = []
    coach_score: float | None = None
    criterion_breakdown: dict[str, float] | None = None
    detection_findings: list[dict[str, Any]] | None = None

    # Only attempt structured extraction on the success path. On failure
    # / timeout, callers care about exit_code + stderr — not artefacts.
    if status == "success":
        try:
            artefacts = _extract_artefacts(stdout)
        except Exception as exc:  # pragma: no cover — defensive
            warnings.append(_unrecognised_shape_warning(exc, "artefacts"))
            artefacts = []

        try:
            coach_score = _extract_coach_score(stdout)
        except Exception as exc:  # pragma: no cover — defensive
            warnings.append(_unrecognised_shape_warning(exc, "coach_score"))
            coach_score = None

        try:
            criterion_breakdown = _extract_criterion_breakdown(stdout)
        except Exception as exc:  # pragma: no cover — defensive
            warnings.append(_unrecognised_shape_warning(exc, "criterion_breakdown"))
            criterion_breakdown = None

        try:
            detection_findings = _extract_detection_findings(stdout)
        except Exception as exc:
            # Malformed JSON is the canonical case here — surface it as a
            # warning rather than a raise. AC-008.
            warnings.append(_unrecognised_shape_warning(exc, "detection_findings"))
            detection_findings = None

    return GuardKitResult(
        status=status,  # type: ignore[arg-type]
        subcommand=subcommand,
        artefacts=artefacts,
        coach_score=coach_score,
        criterion_breakdown=criterion_breakdown,
        detection_findings=detection_findings,
        duration_secs=duration_secs,
        stdout_tail=stdout_tail,
        stderr=stderr if stderr else None,
        exit_code=exit_code,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tail_bytes(text: str, max_bytes: int) -> str:
    """Return the last ``max_bytes`` bytes of ``text`` decoded as UTF-8.

    Byte-based slicing keeps the truncation logic compatible with the
    documented 4 KB ASSUM-003 budget regardless of how many characters
    that represents in a multi-byte encoding. The leading remainder is
    decoded with ``errors="ignore"`` so a slice landing inside a UTF-8
    code point cannot raise.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _extract_artefacts(stdout: str) -> list[str]:
    """Extract absolute paths listed under a ``## Artefacts`` section."""
    section = _ARTEFACTS_SECTION_RE.search(stdout)
    if section is None:
        return []
    body = section.group(1)
    return [match.group(1) for match in _ARTEFACT_LINE_RE.finditer(body)]


def _extract_coach_score(stdout: str) -> float | None:
    """Extract a ``coach_score: <float>`` line from stdout, if present."""
    match = _COACH_SCORE_RE.search(stdout)
    if match is None:
        return None
    return float(match.group(1))


def _extract_criterion_breakdown(stdout: str) -> dict[str, float] | None:
    """Extract the ``## Coach Breakdown`` markdown table, if present."""
    section = _COACH_BREAKDOWN_SECTION_RE.search(stdout)
    if section is None:
        return None
    body = section.group(1)
    breakdown: dict[str, float] = {}
    for row in _BREAKDOWN_ROW_RE.finditer(body):
        criterion = row.group(1).strip()
        # Skip the header row ("| Criterion | Score |") and separator
        # rows ("|-----------|-------|") which would otherwise survive
        # if the score column happened to look numeric.
        if criterion.lower() == "criterion":
            continue
        try:
            score = float(row.group(2))
        except ValueError:
            continue
        breakdown[criterion] = score
    return breakdown or None


def _extract_detection_findings(stdout: str) -> list[dict[str, Any]] | None:
    """Extract a JSON-fenced block beneath ``## Detection Findings``."""
    section = _DETECTION_FINDINGS_SECTION_RE.search(stdout)
    if section is None:
        return None
    body = section.group(1)
    fence = _JSON_FENCE_RE.search(body)
    if fence is None:
        return None
    payload = fence.group(1).strip()
    if not payload:
        return None
    # json.loads will raise JSONDecodeError on malformed payloads — the
    # caller catches it and folds it into a warning per AC-008.
    parsed = json.loads(payload)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return None


def _unrecognised_shape_warning(exc: BaseException, context: str) -> GuardKitWarning:
    """Build the canonical warning for an internal parse failure."""
    return GuardKitWarning(
        code="parser_unrecognised_shape",
        message=(
            f"failed to parse {context} from GuardKit stdout: "
            f"{type(exc).__name__}: {exc}"
        ),
        details={
            "context": context,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        },
    )


__all__ = [
    "parse_guardkit_output",
]
