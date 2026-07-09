"""``forge review-gate`` — the WS3-S5 attended adversarial merge gate.

Attended-v1 (Q2, Rich 2026-07-09): an operator dispatches the reviewer fan-out
(one reviewer per dimension + ≥2 independent refuters per critical/high finding,
per the DD4F practice), collects the proposals + refuter votes into a
review-input JSON, and runs this command. The gate:

    adjudicate (refuted-by-default — a finding without an executed reproduction
        cannot reach confirmed; LPA-15) → emit the guardkit F14 record →
        [--validate] check it via `guardkit qa validate` → report the
        disposition (CLEAN, or BLOCKED for the human checkpoint).

Gated on ``review_gate.enabled`` (default False — inert in production until
reviewer-seat SLMs land in WS4; same rollout pattern as ``deploy.enabled``). The
command refuses to run when the gate is disabled — flag OFF is a no-op.

Review-input JSON shape::

    {
      "review_id": "FEAT-DD4F-postmerge",   # optional; default derived
      "subject": {"kind": "merge", "ref": "d13d88f..1fcb72c"},  # optional
      "findings": [
        {"id": "F-01", "dimension": "correctness", "severity": "critical",
         "summary": "...", "executed_reproduction": "ran X, got TypeError",
         "refuter_votes": [{"who": "r1", "verdict": "not_refuted"},
                           {"who": "r2", "verdict": "not_refuted"}]}
      ]
    }

Exit codes: 0 = clean; 1 = a hard error (bad input / gate disabled / validation
failed); 4 = the review ran and BLOCKED on a confirmed critical/high finding
(the disposition is the human checkpoint's — a blocked merge is not an error,
but it is a distinct, non-zero outcome so a caller can branch on it).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from forge.config.models import ForgeConfig, ReviewGateConfig
from forge.review_gate.assembler import ReviewAssemblyError, assemble_review_findings
from forge.review_gate.models import (
    ReviewInputError,
    ReviewSubject,
    raw_findings_from_input,
)
from forge.review_gate.record import (
    GuardkitValidatorUnavailable,
    validate_review_findings,
    write_review_findings,
)

__all__ = ["review_gate_cmd"]

_EXIT_BLOCKED = 4


def _resolve_config(ctx: click.Context) -> ReviewGateConfig:
    """Pull the review_gate config from ctx.obj (a ForgeConfig), else defaults."""
    obj = ctx.obj
    if isinstance(obj, ForgeConfig):
        return obj.review_gate
    return ReviewGateConfig()


@click.command(name="review-gate")
@click.option("--feature", required=True, help="The agent-built feature under review.")
@click.option(
    "--input",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Review-input JSON: the collected reviewer/refuter fan-out.",
)
@click.option(
    "--subject-kind",
    type=click.Choice(["tree", "commit", "merge"]),
    default="merge",
    show_default=True,
    help="What was reviewed (overridden by input.subject.kind if present).",
)
@click.option(
    "--subject-ref",
    default=None,
    help="Subject reference (tree path / commit sha / merge range).",
)
@click.option(
    "--record-dir",
    "record_dir",
    default=None,
    help="Where to write the F14 record (default: review_gate.record_dir).",
)
@click.option(
    "--validate/--no-validate",
    default=True,
    show_default=True,
    help="Validate the emitted F14 record via `guardkit qa validate`.",
)
@click.pass_context
def review_gate_cmd(
    ctx: click.Context,
    feature: str,
    input_path: Path,
    subject_kind: str,
    subject_ref: str | None,
    record_dir: str | None,
    validate: bool,
) -> None:
    """Run the attended adversarial merge gate for a feature."""
    config = _resolve_config(ctx)
    if not config.enabled:
        click.echo(
            "forge review-gate: the merge gate is disabled "
            "(review_gate.enabled=False). It is inert in production until "
            "reviewer-seat SLMs land in WS4 — set review_gate.enabled=true in "
            "forge.yaml to run the attended checkpoint.",
            err=True,
        )
        sys.exit(1)

    try:
        doc = json.loads(input_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"forge review-gate: cannot read --input: {exc}", err=True)
        sys.exit(1)

    review_id = doc.get("review_id") if isinstance(doc, dict) else None
    review_id = review_id or f"{feature}-merge-review"
    subj = doc.get("subject") if isinstance(doc, dict) else None

    try:
        # ReviewSubject validates kind/ref at construction — a bad subject from
        # the input fails loud here, never a silently schema-invalid record.
        if isinstance(subj, dict) and subj.get("kind") and subj.get("ref"):
            subject = ReviewSubject(kind=subj["kind"], ref=subj["ref"])
        else:
            subject = ReviewSubject(kind=subject_kind, ref=subject_ref or feature)
        raw_findings = raw_findings_from_input(doc)
        dims = tuple(dict.fromkeys(f.dimension for f in raw_findings)) or tuple(
            config.dimensions
        )
        record = assemble_review_findings(
            review_id=review_id,
            subject=subject,
            dimensions=dims,
            raw_findings=raw_findings,
            min_refuters=config.min_refuters,
        )
    except (ReviewInputError, ReviewAssemblyError) as exc:
        click.echo(f"forge review-gate: {exc}", err=True)
        sys.exit(1)

    root = record_dir or config.record_dir
    record_ref = write_review_findings(record, root=root)

    if validate:
        try:
            result = validate_review_findings(record_ref)
        except GuardkitValidatorUnavailable as exc:
            click.echo(f"forge review-gate: {exc}", err=True)
            sys.exit(1)
        if not result.ok:
            click.echo(
                f"forge review-gate: emitted F14 record FAILED guardkit "
                f"validation (rc={result.returncode}):\n"
                f"{result.stderr.strip() or result.stdout.strip()}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"✓ F14 record validated ({result.binary} qa validate)")

    s = record.stats
    confirmed_serious = record.confirmed_serious
    click.echo(f"review {review_id}: {record_ref}")
    click.echo(
        f"  findings: {s.findings_total} "
        f"({s.confirmed} confirmed / {s.refuted} refuted; "
        f"{s.refutations_attempted} refutation attempts)"
    )
    if confirmed_serious:
        click.echo(
            f"  BLOCKED — {len(confirmed_serious)} confirmed critical/high "
            f"finding(s) require the checkpoint's disposition:"
        )
        for f in confirmed_serious:
            click.echo(f"    [{f.severity}] {f.id} ({f.dimension}): {f.summary}")
        click.echo(
            "  The merge does not proceed on a confirmed critical/high finding "
            "without the human checkpoint's approval (DF-009)."
        )
        sys.exit(_EXIT_BLOCKED)
    click.echo("  CLEAN — no confirmed critical/high findings; merge may proceed.")
