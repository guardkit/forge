"""F7 deploy-record writer (WS2-B8, scope-design §2 F7 / LPA-17).

Every deploy run leaves a **deploy record** — the MP-012 addenda pattern
(`docs/state/<task>/deploy-verification-*.md`). F7 schema (scope §2):

    header:  {env, date, deployer (session id), runbook_ref, deploy_profile_ref}
    claims:  [{runtime_claim, evidence_artifact, committed_at}]
    addenda: dated incident sections accreting in place

Enforcement (F7 refusing gate): the deploy stage **refuses to report complete
without a record**, and **a runtime claim with no evidence artifact is
unverified by definition** — :func:`render_deploy_record` raises if there are no
claims or any claim lacks an evidence artifact. Dry-run records are honestly
labelled ``dry_run: true`` in the header and their claims cite the persisted
dry-run step results as the artifact (an explicit non-verification, never a
fabricated runtime claim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "DeployClaim",
    "DeployAddendum",
    "DeployRecord",
    "DeployRecordError",
    "render_deploy_record",
    "write_deploy_record",
]


class DeployRecordError(ValueError):
    """Raised when a deploy record is incomplete (missing claim or artifact)."""


@dataclass(frozen=True, slots=True)
class DeployClaim:
    """One runtime claim + its same-day evidence artifact.

    Attributes:
        runtime_claim: The claim (e.g. "fleet-memory Postgres reachable over LAN").
        evidence_artifact: The artifact backing it (consumer-info JSON, boot-log
            lines, image digest, stream-info output, or — for a dry run — the
            persisted runbook step-result ref). MUST be non-empty: a claim with
            no artifact is unverified by definition (F7 enforcement).
        committed_at: When the evidence was committed (same day as the claim).
    """

    runtime_claim: str
    evidence_artifact: str
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class DeployAddendum:
    """A dated addendum section (the MP-012 addenda-1..N pattern)."""

    title: str
    date: datetime
    body: str


@dataclass(frozen=True, slots=True)
class DeployRecord:
    """A full F7 deploy record.

    Attributes:
        env: The deploy environment id.
        date: The deploy date/time.
        deployer: The session/run id that performed the deploy.
        runbook_ref: The rendered runbook ref (runbook_id).
        deploy_profile_ref: The deploy/profile.yaml ref consumed.
        claims: Runtime claims, each with a same-day evidence artifact.
        addenda: Dated incident sections.
        status: Deploy outcome ("complete" | "failed").
        dry_run: True when this record is for a dry-run deploy.
        image_digests: service -> image digest map (evidence), or None.
        artifact_digest: Merged-artifact digest, or None.
        task_id: The state-dir task id the record is filed under, or None.
    """

    env: str
    date: datetime
    deployer: str
    runbook_ref: str
    deploy_profile_ref: str | None
    claims: tuple[DeployClaim, ...]
    status: str = "complete"
    dry_run: bool = False
    image_digests: dict[str, str] | None = None
    artifact_digest: str | None = None
    task_id: str | None = None
    addenda: tuple[DeployAddendum, ...] = ()
    extra_header: dict[str, str] = field(default_factory=dict)


def _validate(record: DeployRecord) -> None:
    if not record.claims:
        raise DeployRecordError(
            "deploy record has no claims; a deploy stage refuses to report "
            "complete without at least one evidenced runtime claim (F7)"
        )
    for i, claim in enumerate(record.claims):
        if not claim.runtime_claim or not claim.runtime_claim.strip():
            raise DeployRecordError(f"claim[{i}] has an empty runtime_claim")
        if not claim.evidence_artifact or not claim.evidence_artifact.strip():
            raise DeployRecordError(
                f"claim[{i}]={claim.runtime_claim!r} has no evidence artifact; a "
                "runtime claim with no artifact is unverified by definition (F7)"
            )


def render_deploy_record(record: DeployRecord) -> str:
    """Render an F7 deploy record to markdown.

    Raises:
        DeployRecordError: If the record has no claims, or any claim lacks an
            evidence artifact (F7 enforcement — an unverified claim is refused).
    """
    _validate(record)

    date_str = record.date.strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = []
    dry = " (DRY RUN)" if record.dry_run else ""
    lines.append(f"# Deploy record — {record.env}{dry}")
    lines.append("")
    lines.append("## Header")
    lines.append("")
    lines.append(f"- **env**: {record.env}")
    lines.append(f"- **date**: {date_str}")
    lines.append(f"- **deployer**: {record.deployer}")
    lines.append(f"- **status**: {record.status}")
    lines.append(f"- **dry_run**: {str(record.dry_run).lower()}")
    lines.append(f"- **runbook_ref**: {record.runbook_ref}")
    lines.append(f"- **deploy_profile_ref**: {record.deploy_profile_ref or '(none)'}")
    if record.artifact_digest:
        lines.append(f"- **artifact_digest**: {record.artifact_digest}")
    if record.image_digests:
        digests = ", ".join(
            f"{svc}={dig}" for svc, dig in sorted(record.image_digests.items())
        )
        lines.append(f"- **image_digests**: {digests}")
    for k, v in record.extra_header.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## Claims")
    lines.append("")
    lines.append("| # | runtime_claim | evidence_artifact | committed_at |")
    lines.append("|---|---|---|---|")
    for i, claim in enumerate(record.claims, start=1):
        committed = claim.committed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        # Escape pipes so a claim/artifact with a '|' cannot break the table.
        rc = claim.runtime_claim.replace("|", "\\|")
        ev = claim.evidence_artifact.replace("|", "\\|")
        lines.append(f"| {i} | {rc} | {ev} | {committed} |")
    lines.append("")

    if record.addenda:
        lines.append("## Addenda")
        lines.append("")
        for n, add in enumerate(record.addenda, start=1):
            add_date = add.date.strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"### Addendum {n} — {add.title} ({add_date})")
            lines.append("")
            lines.append(add.body.rstrip())
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _record_dir_name(record: DeployRecord) -> str:
    """The per-record subdirectory name under the deploy-record root."""
    if record.task_id:
        return record.task_id
    return f"deploy-{record.env}"


def write_deploy_record(
    record: DeployRecord,
    *,
    root: str | Path,
    filename: str | None = None,
) -> str:
    """Write an F7 deploy record to ``<root>/<task-or-env>/deploy-record-<date>.md``.

    Args:
        record: The record to write (validated first — an incomplete record is
            never written).
        root: The deploy-record root directory (config ``deploy_record_dir``).
        filename: Override the default ``deploy-record-<YYYY-MM-DD>.md`` name.

    Returns:
        The path the record was written to (the ``deploy_record_ref``).

    Raises:
        DeployRecordError: If the record is incomplete (see
            :func:`render_deploy_record`).
    """
    rendered = render_deploy_record(record)  # validates before any I/O
    day = record.date.strftime("%Y-%m-%d")
    name = filename or f"deploy-record-{day}.md"
    out_dir = Path(root) / _record_dir_name(record)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    out_path.write_text(rendered, encoding="utf-8")
    return str(out_path)
