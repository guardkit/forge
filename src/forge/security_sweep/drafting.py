"""Filing a security repair request: at most once per finding (Codex R4, R10).

Public surface
==============

- :func:`security_rows` — the security repair rows already in the work queue,
  each with the fingerprints its filing event recorded.
- :func:`repair_correlation_id` — the row's identity, made from the project
  and the fingerprints it carries, never from the date.
- :func:`repair_sentence` — the request in plain English, at most ten items.
- :func:`file_repair` — files one row with ``WorkQueueStore.file_sentence``.

The queue, not the state file, is the record
--------------------------------------------

Every security row's filing event carries ``{"source": "security-sweep",
"fingerprints": [...]}`` in its details, written in the same transaction as
the row. Before drafting, the sweep reads those back: any fingerprint found
there is treated as drafted, whatever the state file says or whether it
exists. And the row's correlation id is ``security-<project>-<first 12 hex of
the sha256 of its sorted fingerprints>``, so a sweep that filed a row and was
stopped before it saved its state files the same id on the next run, which
the queue refuses as a duplicate. Either way nothing is filed twice.

One open repair per project; the ten most serious
-------------------------------------------------

While a project's earlier security row is still open in the queue, nothing
new is filed for it. Otherwise one row is filed with the ten most serious
eligible findings (malicious packages, then real secrets, then by score); only
those ten are recorded as drafted, and the rest are filed once that row
closes.

The row is an ordinary request (``kind="feature"``,
``triggered_by="forge-internal"``) from the configured approver, for the
project's ``forge.yaml`` key, so it reaches Rich as a normal spec card and the
factory's own local models do the repair.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from forge.planning.work_queue_store import OPEN_STATUSES, WorkQueueStore

from .findings import KIND_CODE, KIND_DEPENDENCY, KIND_SECRET, Finding

#: Written on the filing event; how a security row is recognised.
SOURCE = "security-sweep"

#: Every security row's correlation id starts with this.
CORRELATION_PREFIX = "security-"

#: Who files the row, on its filing event.
ACTOR = "forge-security-sweep"

#: The most findings one row carries (Codex R10).
MAX_ITEMS = 10


@dataclass(frozen=True)
class SecurityRow:
    """A security repair row already in the queue."""

    queue_id: int
    status: str
    target_repo: str
    fingerprints: tuple[str, ...]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


def security_rows(store: WorkQueueStore) -> list[SecurityRow]:
    """Every security repair row in the queue, open or closed, oldest first.

    A row counts only when its filing event says it came from the sweep;
    the fingerprints are the ones that event recorded.
    """
    try:
        rows = store.connection.execute(
            "SELECT id, status, target_repo FROM work_queue "
            "WHERE correlation_id LIKE ? ORDER BY id ASC",
            (CORRELATION_PREFIX + "%",),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    found: list[SecurityRow] = []
    for row in rows:
        queue_id = int(row[0])
        details = store.origin_details(queue_id)
        if details.get("source") != SOURCE:
            continue
        prints = details.get("fingerprints")
        found.append(
            SecurityRow(
                queue_id=queue_id,
                status=str(row[1]),
                target_repo=str(row[2] or ""),
                fingerprints=tuple(str(p) for p in prints if isinstance(p, str))
                if isinstance(prints, list)
                else (),
            )
        )
    return found


def repair_correlation_id(project: str, fingerprints: Iterable[str]) -> str:
    """``security-<project>-<12 hex>``: the same findings, the same id, any day."""
    digest = hashlib.sha256("\n".join(sorted(fingerprints)).encode("utf-8")).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", project).strip("-") or "project"
    return f"{CORRELATION_PREFIX}{safe}-{digest[:12]}"


def _listed(values: Sequence[str]) -> str:
    if len(values) <= 1:
        return "".join(values)
    return ", ".join(values[:-1]) + " and " + values[-1]


def _dependency_item(group: list[Finding]) -> str:
    first = group[0]
    parts = []
    for finding in group:
        details = ", ".join(
            bit
            for bit in (
                finding.severity,
                f"{finding.score:g}" if finding.score is not None else None,
            )
            if bit
        )
        parts.append(f"{finding.advisory}" + (f" ({details})" if details else ""))
    noun = "advisory" if len(group) == 1 else "advisories"
    fixed = sorted({v for f in group for v in f.fixed}, key=_display_order)
    where = f" in {first.file}" if first.file else ""
    text = f"{first.package} {first.version}{where} has {noun} {_listed(parts)}"
    if any(f.is_malicious for f in group):
        text += "; this is a known malicious package — remove it and use an established one"
    elif fixed:
        text += (
            f"; OSV lists fixes in {_listed(fixed)} — move to a version where every "
            "listed serious advisory is fixed"
        )
    else:
        text += "; OSV lists no fixed version — replace the package or remove it"
    return text


def _display_order(text: str) -> tuple[object, ...]:
    return tuple(
        (0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", text) if p
    )


def _secret_item(finding: Finding) -> str:
    return (
        f"{finding.file} line {finding.line} holds what looks like a real secret "
        f"(rule {finding.rule}) — remove it and read the value from the environment "
        "or a secret file; a test should build the value at run time instead of "
        "writing it down"
    )


def _code_item(finding: Finding) -> str:
    what = finding.message or "a pattern the security rules flag"
    what = what.rstrip(".")
    return (
        f"{finding.file} line {finding.line}: {what} (rule {finding.rule}) — change "
        "the code so the rule no longer matches"
    )


def repair_sentence(project: str, findings: Sequence[Finding]) -> str:
    """The request, in plain English, built from allowlisted fields only."""
    items: list[str] = []
    seen_groups: dict[tuple[str, str, str], list[Finding]] = {}
    order: list[object] = []
    for finding in findings:
        if finding.kind == KIND_DEPENDENCY:
            key = (finding.package or "", finding.version or "", finding.file)
            if key not in seen_groups:
                seen_groups[key] = []
                order.append(key)
            seen_groups[key].append(finding)
        else:
            order.append(finding)
    for entry in order:
        if isinstance(entry, tuple):
            items.append(_dependency_item(seen_groups[entry]))
        elif entry.kind == KIND_SECRET:
            items.append(_secret_item(entry))
        elif entry.kind == KIND_CODE:
            items.append(_code_item(entry))
    numbered = " ".join(f"({n}) {text}." for n, text in enumerate(items, start=1))
    noun = "problem" if len(findings) == 1 else "problems"
    return (
        f"Fix {len(findings)} security {noun} the nightly security check found in "
        f"{project}: {numbered} The factory's per-change security check confirms "
        "the result."
    )


def file_repair(
    store: WorkQueueStore,
    *,
    project: str,
    findings: Sequence[Finding],
    approver: str,
) -> tuple[int, bool, str]:
    """File one security repair row; ``(queue id, created, correlation id)``."""
    chosen = list(findings)[:MAX_ITEMS]
    prints = [f.fingerprint for f in chosen]
    correlation_id = repair_correlation_id(project, prints)
    filed = store.file_sentence(
        correlation_id=correlation_id,
        sentence=repair_sentence(project, chosen),
        originating_user=approver,
        target_repo=project,
        kind="feature",
        actor_identity=ACTOR,
        triggered_by="forge-internal",
        extra_details={"source": SOURCE, "fingerprints": prints},
    )
    return filed.queue_id, filed.created, correlation_id


__all__ = [
    "ACTOR",
    "CORRELATION_PREFIX",
    "MAX_ITEMS",
    "SOURCE",
    "SecurityRow",
    "file_repair",
    "repair_correlation_id",
    "repair_sentence",
    "security_rows",
]
