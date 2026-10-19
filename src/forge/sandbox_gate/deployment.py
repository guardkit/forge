"""What the coordinator tells the starter about a sandbox's recorded deployment
(one sandbox per project, stage S2; Codex S2 implementation review 1, R1).

The starter keeps the last deployment it was told durably, and an omitted one
leaves it as it was, so an earlier "none" would outlive a first deployment.
So the coordinator always says it, on every start, stop, hold and ready:

- ``{}`` ("deployment=none") when the ledger records nothing running on any of
  the sandbox's targets;
- otherwise ``{"compose_project", "identity_label"}``: the sandbox entry's
  ``deployment_compose_project``, and its ``deployment_identity_label`` key
  joined to the identity the ledger records as running
  (``deployment_targets.running_identity``);
- there is no fallback (Codex S2 implementation review 3, R1): settings
  loading refuses ``idle_stop`` without both names, and a recorded deployment
  that still cannot be named (no identity in the ledger, or settings that
  slipped past validation) is a configuration problem — the gate refuses
  readiness, deployed holds and stops for the sandbox, and leaves it up.

When a sandbox has more than one target running (two environments), the most
recently confirmed one is the one named: the starter checks one deployment per
sandbox.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

__all__ = ["deployment_to_tell", "recorded_identity"]


def recorded_identity(connection: sqlite3.Connection | None, repos: Iterable[str]) -> str | None:
    """The identity most recently recorded as running on the repositories'
    targets, ``""`` for a deployment with no identity, ``None`` for none."""
    names = sorted({str(r) for r in repos if str(r or "").strip()})
    if connection is None or not names:
        return None
    clauses = " OR ".join("(target = ? OR substr(target, 1, ?) = ?)" for _ in names)
    params: list[object] = []
    for repo in names:
        params.extend([repo, len(repo) + 2, f"{repo}::"])
    try:
        row = connection.execute(
            f"SELECT running_identity FROM deployment_targets WHERE ({clauses}) "
            "AND running_commit IS NOT NULL AND trim(running_commit) <> '' "
            "ORDER BY running_at DESC, target LIMIT 1",
            tuple(params),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if row is None:
        return None
    return str(row[0] or "").strip()


def deployment_to_tell(
    connection: sqlite3.Connection | None,
    repos: Iterable[str],
    entry: Any,
) -> tuple[dict[str, str] | None, str | None]:
    """``(deployment, problem)``. ``({}, None)`` when the ledger records no
    deployment; ``(named, None)`` when it records one and the entry names its
    compose project and label key; otherwise ``(None, problem)`` — the
    deployment cannot be identified, and the caller refuses readiness, deployed
    holds and stops for the sandbox, leaving it up (Codex S2 implementation
    review 3, R1). There is no fallback."""
    identity = recorded_identity(connection, repos)
    if identity is None:
        return {}, None
    project = getattr(entry, "deployment_compose_project", None)
    key = getattr(entry, "deployment_identity_label", None)
    name = getattr(entry, "name", "the sandbox")
    if not (project and key):
        return None, (
            f"{name} has a recorded deployment, but its planning.sandboxes entry does not "
            "name deployment_compose_project and deployment_identity_label, so the "
            "deployment cannot be identified to the starter; the sandbox is left up and "
            "is not made ready until the settings name both"
        )
    if not identity:
        return None, (
            f"{name}'s recorded deployment has no identity in the ledger, so it cannot be "
            "identified to the starter; the sandbox is left up and is not made ready"
        )
    return {"compose_project": str(project), "identity_label": f"{key}={identity}"}, None
