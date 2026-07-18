"""MG-5 demotion-event emission — the forge deploy leg of the DF-021 ledger.

Stage 3 of H-A RETIRE-THE-COORDINATOR (see
``docs/ways-of-working/retire-the-coordinator-build-handoff-2026-07-17.md`` §3,
ORDERING / MG-5). When a post-merge live-gate FAILS in the deploy stage — the
existing O-32 revert path — an auto-merged lane must be DEMOTED back to attended.
The DF-021 trust ledger (guardkit ``qa/trust_ledger.py``) keys that demotion on a
**file-based demotion event** it reads from the TARGET repo's ``qa/`` tree, beside
its gates. This module writes that event.

**Shape (binding — matches guardkit ``load_demotion_event`` byte-for-byte).** A
YAML mapping with the required keys ``feature_id / lane / source / verdict /
timestamp`` and an optional ``receipt_ref``::

    feature_id: FEAT-XYZ
    lane: appmilla/api_test
    source: live_gate
    verdict: fail
    timestamp: 2026-07-20T09:00:00+00:00   # caller-provided (the deploy clock)
    receipt_ref: qa/live-gate-FEAT-XYZ.yaml   # optional (the failing-gate evidence)

**Transport (binding, v1).** FILE-BASED. No NATS payloads (that is WS4); the
disposition surface is the YAML record itself.

**Inert-by-design.** A demotion event written with no ledger consuming it is just
data — safe. The emission therefore rides the existing deploy revert path
UNCONDITIONALLY; it never gates, reads, or alters the revert behaviour — O-32
stays byte-for-byte untouched. ``source`` mirrors guardkit's ``SOURCE_LIVE_GATE``
as a PATTERN (never an import — forge is self-contained, DF-001).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

__all__ = ["SOURCE_LIVE_GATE", "write_demotion_event"]

#: The demotion ``source`` the ledger records for a live-gate failure — mirrors
#: guardkit ``trust_ledger.SOURCE_LIVE_GATE`` as a PATTERN, not an import.
SOURCE_LIVE_GATE = "live_gate"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    """Filesystem-safe token for a demotion-event filename."""
    cleaned = _SAFE.sub("-", value.strip()).strip("-")
    return cleaned or "unknown"


def write_demotion_event(
    qa_root: str | Path,
    *,
    feature_id: str,
    lane: str,
    verdict: str,
    timestamp: str,
    receipt_ref: str | None = None,
    run_id: str | None = None,
) -> str:
    """Write one MG-5 live-gate demotion event under the target repo's ``qa/`` tree.

    Args:
        qa_root: The target repo's ``qa/`` directory (created if absent) — where
            the ledger reads demotion events from, beside its gates.
        feature_id: The feature whose auto-merge is being demoted.
        lane: The lane identifier the ledger demotes (the deploy target/repo).
        verdict: The failing live-gate verdict (``fail`` / ``environment_fail`` /
            ``instrument_fail``) — recorded verbatim, honest about *why* the gate
            did not pass.
        timestamp: The caller-provided event time (the deploy clock; the ledger
            never invents wall time for a record it did not observe first-hand).
        receipt_ref: Optional evidence ref for the failing gate (F5 index / run id).
        run_id: Optional deploy-run id, folded into the filename so repeated
            demotions of one feature never collide.

    Returns:
        The written file path (str).
    """
    root = Path(qa_root)
    root.mkdir(parents=True, exist_ok=True)

    stem = f"demotion-{_slug(feature_id)}"
    if run_id:
        stem = f"{stem}-{_slug(run_id)}"
    out = root / f"{stem}.yaml"

    # Ordered exactly as guardkit's load_demotion_event documents; receipt_ref is
    # emitted only when present (a falsy value reads as None on the ledger side).
    doc: dict[str, str] = {
        "feature_id": feature_id,
        "lane": lane,
        "source": SOURCE_LIVE_GATE,
        "verdict": verdict,
        "timestamp": timestamp,
    }
    if receipt_ref:
        doc["receipt_ref"] = receipt_ref

    header = (
        "# MG-5 live-gate demotion event (H-A Stage 3 · DF-021 trust ledger)\n"
        f"# feature_id: {feature_id}\n"
    )
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    out.write_text(header + body, encoding="utf-8")
    return str(out)
