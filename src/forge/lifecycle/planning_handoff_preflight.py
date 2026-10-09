"""Read-only compatibility preflight for planning-handoff retirements."""

from __future__ import annotations

from pathlib import Path

from forge.adapters.sqlite.connect import read_only_connect
from forge.lifecycle.migrations import observed_schema_version
from forge.lifecycle.planning_handoff_retirement import (
    PlanningHandoffRetirementError,
    SUPPORTED_RECEIPT_SCHEMA_VERSIONS,
    has_retirement_looking_history,
    retired_planning_handoff_correlations,
)


def preflight_retired_planning_handoff_correlations(
    db_path: Path,
) -> frozenset[str]:
    """Validate retirement history without creating or migrating a database.

    A missing database retains Forge's ordinary fresh-start path. Receipt-free
    legacy databases may likewise proceed to the existing migration runner.
    Once the canonical selector sees retirement-looking history, or the ledger
    is new enough to support a receipt, the full shared reader is authoritative.
    """

    path = Path(db_path).expanduser()
    if not path.exists():
        return frozenset()
    if not path.is_file():
        raise PlanningHandoffRetirementError(
            f"planning handoff retirement preflight requires a database file: {path}"
        )

    connection = None
    started = False
    try:
        connection = read_only_connect(path)
        connection.execute("BEGIN")
        started = True
        version = observed_schema_version(connection)
        has_history = has_retirement_looking_history(connection)
        if has_history or version >= min(SUPPORTED_RECEIPT_SCHEMA_VERSIONS):
            retired = retired_planning_handoff_correlations(connection)
        else:
            retired = frozenset()
        connection.commit()
        started = False
        return retired
    except PlanningHandoffRetirementError:
        if connection is not None and started:
            connection.rollback()
        raise
    except Exception as exc:
        if connection is not None and started:
            connection.rollback()
        raise PlanningHandoffRetirementError(
            f"planning handoff retirement preflight could not read {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


__all__ = ["preflight_retired_planning_handoff_correlations"]
