"""``forge status`` Click command — read-only SQLite path (TASK-PSM-009).

This module is the **CLI status reader** for FEAT-FORGE-001. It implements
``forge status`` per ``docs/design/contracts/API-cli.md §4`` and is
deliberately decoupled from the NATS messaging layer.

Critical constraint (review F6 / Group H): this module MUST NOT import
any module from :mod:`forge.adapters.nats`. The CLI read path is
SQLite-only — it must remain functional even when the NATS bus is
unreachable. The ``test_no_nats_imports_in_source`` static-analysis
check in :mod:`tests.forge.test_cli_status` is the AC-006 verifier.

Design rules baked into this module:

* **Per-poll read-only connection** — every status query opens a fresh
  :func:`forge.adapters.sqlite.read_only_connect` handle and closes it
  on exit. DDR-003 says reads use a per-CLI-invocation connection; for
  the watch loop we extend that to "per-poll", so a long-running watch
  never holds a reader against the writer.
* **Narrow projection** — :class:`~forge.lifecycle.persistence.BuildStatusView`
  carries only the columns the status table renders. The ``--full``
  view augments each row with up to :data:`_FULL_STAGE_LIMIT`
  ``stage_log`` entries (Group B "Full status view caps stage detail at 5").
* **Watch loop terminates on terminal-only** — the loop exits cleanly
  when every visible build is in a terminal state, so a CI invocation
  of ``forge status --watch`` will not hang the build.
* **No NATS imports** — see the static-analysis check.

References:
    - TASK-PSM-009 — this task brief.
    - TASK-PSM-005 — ``BuildStatusView`` projection + ``read_status``
      reference SQL (consumed via the ``PERSISTENCE_PROTOCOLS`` contract).
    - TASK-PSM-002 — schema + ``read_only_connect`` (consumed via the
      ``SCHEMA_INITIALIZED`` contract).
    - ``docs/design/contracts/API-cli.md §4`` — CLI contract.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterable

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table

from forge.adapters.sqlite.connect import read_only_connect
from forge.lifecycle.modes import BuildMode
from forge.lifecycle.persistence import (
    ACTIVE_STATES,
    BuildStatusView,
    StageLogEntry,
)
from forge.lifecycle.state_machine import BuildState
from forge.persistence.repositories.bridge_registry import (
    BridgeRegistry,
    BridgeRegistryEntry,
)

logger = logging.getLogger(__name__)


__all__ = [
    "status_cmd",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of recent terminal builds appended to the default view. Mirrors
#: ``forge.lifecycle.persistence._RECENT_TERMINAL_LIMIT`` — the AC keeps
#: this at 5 (per the task brief and ``API-cli.md §4.2``).
_RECENT_TERMINAL_LIMIT: Final[int] = 5

#: Group B "Full status view caps stage detail at 5" — ``--full`` shows at
#: most this many ``stage_log`` rows per build.
_FULL_STAGE_LIMIT: Final[int] = 5

#: Watch poll cadence (seconds). Per ``API-cli.md §4.2`` — every 2s.
_WATCH_INTERVAL_SECS: Final[float] = 2.0

#: Terminal lifecycle states (AC-003 — watch exits when all visible builds
#: are terminal).
_TERMINAL_STATES: Final[tuple[BuildState, ...]] = (
    BuildState.COMPLETE,
    BuildState.FAILED,
    BuildState.CANCELLED,
    BuildState.SKIPPED,
)

#: Environment variable used to point the CLI at a non-default db path.
_FORGE_DB_PATH_ENV: Final[str] = "FORGE_DB_PATH"

#: Default location of the forge database (relative to the cwd).
_DEFAULT_DB_PATH: Final[Path] = Path(".forge") / "forge.db"

#: Synthetic correlation-id used when the CLI reads the
#: ``lifecycle_bridge_registry`` table for the ``--in-flight`` view
#: (TASK-FRR-PEB-012). The registry's read API requires a non-empty
#: ``correlation_id`` for traceability — the CLI is not driven by a
#: build envelope, so we mint a stable label that operators can grep
#: for in the structured logs.
_IN_FLIGHT_CORRELATION_ID: Final[str] = "cli-status:in-flight"


# ---------------------------------------------------------------------------
# DB-path resolution
# ---------------------------------------------------------------------------


def _resolve_db_path(explicit: str | None) -> Path:
    """Resolve the SQLite database path.

    Resolution order:

    1. The ``--db-path`` Click option (``explicit``).
    2. The ``FORGE_DB_PATH`` environment variable.
    3. ``./.forge/forge.db`` relative to the cwd.

    The path is expanded (``~`` → home) and normalised but is NOT
    required to exist — callers are expected to surface a clean
    :class:`click.ClickException` when the path is missing.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(_FORGE_DB_PATH_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / _DEFAULT_DB_PATH).resolve()


# ---------------------------------------------------------------------------
# SQL — narrow status projection
# ---------------------------------------------------------------------------


def _row_to_status_view(row: sqlite3.Row) -> BuildStatusView:
    """Hydrate a narrow status-projection row into :class:`BuildStatusView`.

    Mirrors :func:`forge.lifecycle.persistence._row_to_status_view`. The
    duplication is intentional — the CLI read path is decoupled from the
    persistence facade so we don't need to instantiate a writer
    connection just to project one row.

    Tolerates legacy rows that pre-date the ``mode`` column (FEAT-FORGE-008
    additive migration in ``schema_v2.sql``) by defaulting to
    :attr:`BuildMode.MODE_A`. This keeps ``forge status`` working through
    the upgrade window and on databases that have not yet run the v2
    migration. ``terminal_class`` (``schema_v9.sql``) gets exactly the same
    tolerance: a database that has not run v9 has no such column, and "not
    classified" is the honest read.
    """
    try:
        raw_mode = row["mode"]
    except (IndexError, KeyError):
        raw_mode = None
    try:
        raw_terminal_class = row["terminal_class"]
    except (IndexError, KeyError):
        raw_terminal_class = None
    return BuildStatusView(
        build_id=row["build_id"],
        feature_id=row["feature_id"],
        status=BuildState(row["status"]),
        queued_at=datetime.fromisoformat(row["queued_at"]),
        started_at=(
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        ),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
        pr_url=row["pr_url"],
        error=row["error"],
        mode=BuildMode(raw_mode) if raw_mode else BuildMode.MODE_A,
        terminal_class=str(raw_terminal_class) if raw_terminal_class else None,
    )


#: Columns the projection always names.
_BASE_PROJECTION: Final[str] = (
    "build_id, feature_id, status, queued_at, started_at, "
    "completed_at, pr_url, error, mode"
)


def _status_projection(cx: sqlite3.Connection) -> str:
    """The SELECT column list, widened only where the database can serve it.

    THE UPGRADE WINDOW IS REAL. The CLI never migrates — the ``forge serve``
    daemon does, at boot. So there is a genuine interval in which a freshly
    installed forge reads a database that is still on the previous schema, and
    naming ``terminal_class`` unconditionally would turn ``forge status`` into
    a hard error ("no such column") for exactly the operator who is mid-
    upgrade and most wants to see what is running.

    One ``PRAGMA table_info`` per invocation buys the answer. An unreadable
    pragma degrades the same way a missing column does: to the narrow
    projection, which every version of the schema can serve.
    """
    try:
        columns = {row[1] for row in cx.execute("PRAGMA table_info(builds)")}
    except sqlite3.Error:  # pragma: no cover — a pragma failure means the
        # whole read is about to fail anyway; degrade rather than add a
        # second, more confusing error on the way there.
        return _BASE_PROJECTION
    if "terminal_class" in columns:
        return f"{_BASE_PROJECTION}, terminal_class"
    return _BASE_PROJECTION


def _query_status_views(
    cx: sqlite3.Connection,
    feature_id: str | None,
) -> list[BuildStatusView]:
    """Execute the status-projection SQL against ``cx``.

    Two branches:

    * ``feature_id`` provided — return all builds for that feature, most
      recent first (AC-002).
    * No filter — return active builds + the most recent
      :data:`_RECENT_TERMINAL_LIMIT` terminal builds, sorted globally
      newest-first by ``queued_at`` (AC-001).
    """
    projection = _status_projection(cx)
    if feature_id:
        rows = cx.execute(
            f"""
            SELECT {projection}
              FROM builds
             WHERE feature_id = ?
             ORDER BY queued_at DESC
            """,
            (feature_id,),
        ).fetchall()
        return [_row_to_status_view(r) for r in rows]

    active_values = tuple(s.value for s in ACTIVE_STATES)
    terminal_values = tuple(s.value for s in _TERMINAL_STATES)
    active_placeholders = ",".join(["?"] * len(active_values))
    terminal_placeholders = ",".join(["?"] * len(terminal_values))

    active_sql = f"""
        SELECT {projection}
          FROM builds
         WHERE status IN ({active_placeholders})
         ORDER BY queued_at DESC
    """
    terminal_sql = f"""
        SELECT {projection}
          FROM builds
         WHERE status IN ({terminal_placeholders})
         ORDER BY queued_at DESC
         LIMIT ?
    """

    active_rows = cx.execute(active_sql, active_values).fetchall()
    terminal_rows = cx.execute(
        terminal_sql, (*terminal_values, _RECENT_TERMINAL_LIMIT)
    ).fetchall()

    combined = [_row_to_status_view(r) for r in active_rows] + [
        _row_to_status_view(r) for r in terminal_rows
    ]
    combined.sort(key=lambda v: v.queued_at, reverse=True)
    return combined


def _query_recent_stages(
    cx: sqlite3.Connection,
    build_id: str,
    limit: int,
) -> list[StageLogEntry]:
    """Return the last ``limit`` stage_log rows for ``build_id``.

    Used by the ``--full`` view. The rows are ordered chronologically
    (oldest first) so the rendered tail reads top-to-bottom in the
    natural execution order.
    """
    if not build_id:
        return []
    rows = cx.execute(
        """
        SELECT build_id, stage_label, target_kind, target_identifier,
               status, gate_mode, coach_score, threshold_applied,
               started_at, completed_at, duration_secs, details_json
          FROM (
              SELECT *
                FROM stage_log
               WHERE build_id = ?
               ORDER BY started_at DESC, id DESC
               LIMIT ?
          )
         ORDER BY started_at ASC, id ASC
        """,
        (build_id, max(0, limit)),
    ).fetchall()
    entries: list[StageLogEntry] = []
    for r in rows:
        raw_details = r["details_json"]
        try:
            details = json.loads(raw_details) if raw_details else {}
        except json.JSONDecodeError:
            logger.warning(
                "stage_log row for build_id=%r has malformed details_json; "
                "rendering empty dict",
                r["build_id"],
            )
            details = {}
        entries.append(
            StageLogEntry(
                build_id=r["build_id"],
                stage_label=r["stage_label"],
                target_kind=r["target_kind"],
                target_identifier=r["target_identifier"],
                status=r["status"],
                gate_mode=r["gate_mode"],
                coach_score=r["coach_score"],
                threshold_applied=r["threshold_applied"],
                started_at=datetime.fromisoformat(r["started_at"]),
                completed_at=datetime.fromisoformat(r["completed_at"]),
                duration_secs=r["duration_secs"],
                details=details,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Public read helpers (also used by tests)
# ---------------------------------------------------------------------------


def _read_status_views(
    db_path: Path,
    feature_id: str | None,
) -> list[BuildStatusView]:
    """Open a fresh ro connection and run the status projection."""
    cx = read_only_connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        return _query_status_views(cx, feature_id)
    finally:
        cx.close()


def _read_full_payload(
    db_path: Path,
    feature_id: str | None,
) -> list[tuple[BuildStatusView, list[StageLogEntry]]]:
    """Read the status views plus the last 5 stages per build."""
    cx = read_only_connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        views = _query_status_views(cx, feature_id)
        return [
            (v, _query_recent_stages(cx, v.build_id, _FULL_STAGE_LIMIT)) for v in views
        ]
    finally:
        cx.close()


def _read_in_flight_entries(
    db_path: Path,
    feature_id_filter: str | None,
) -> list[BridgeRegistryEntry]:
    """Open a fresh ro connection and read the in-flight registry.

    AC-001 / AC-005 (TASK-FRR-PEB-012): the read uses a short-lived
    ``mode=ro`` URI handle so the CLI cannot mutate the registry. The
    bridge migration may not yet have been applied (e.g. brand-new
    install where ``forge serve`` has never run); in that case the
    table does not exist and we surface the result as an empty list,
    so the caller can render the "No in-flight builds." line.
    """
    cx = read_only_connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        try:
            registry = BridgeRegistry(connection=cx)
            entries = registry.list_active(correlation_id=_IN_FLIGHT_CORRELATION_ID)
        except sqlite3.OperationalError as exc:
            # Bridge migration has not run against this database yet —
            # treat as "no in-flight builds" rather than raising.
            if "no such table" in str(exc).lower():
                logger.debug(
                    "lifecycle_bridge_registry table missing; "
                    "rendering empty in-flight view (db_path=%s)",
                    db_path,
                )
                return []
            raise
        if feature_id_filter:
            entries = [e for e in entries if e.feature_id == feature_id_filter]
        return entries
    finally:
        cx.close()


def _all_terminal(views: Iterable[BuildStatusView]) -> bool:
    """Return True iff every view's status is terminal.

    An empty iterable is considered terminal so the watch loop exits
    cleanly once the queue drains.
    """
    materialised = list(views)
    if not materialised:
        return True
    return all(v.status in _TERMINAL_STATES for v in materialised)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_dt(value: datetime | None) -> str:
    """Render a UTC datetime as ``HH:MM:SS`` for the table view."""
    if value is None:
        return "—"
    return value.strftime("%H:%M:%S")


def _elapsed(view: BuildStatusView) -> str:
    """Compute a coarse elapsed string for the table view."""
    if view.started_at is None:
        return "—"
    end = view.completed_at or datetime.now(view.started_at.tzinfo)
    delta = end - view.started_at
    total_seconds = int(max(0, delta.total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _build_table(
    views: list[BuildStatusView],
    *,
    full_stages: dict[str, list[StageLogEntry]] | None = None,
) -> Table:
    """Build a Rich table for the status view.

    The columns mirror ``API-cli.md §4.3``. The STAGE cell is left as a
    placeholder (the most recent ``stage_label``, when known) — live
    autobuild progress (``API-cli.md §4.4``) is OUT OF SCOPE for this
    feature; it requires the ``async_tasks`` channel from
    FEAT-FORGE-007.

    CLASS (timeout truth, 2026-08-07) answers the question STATUS cannot.
    ``FAILED`` is one word for five different deaths — a monitor kill, a
    budget-cap kill, a wall-clock expiry, a guardkit in-band timeout, and an
    ordinary broken build — and "it ran out of time" versus "it is broken"
    are opposite verdicts with opposite next actions. STATUS is untouched:
    it still reads exactly ``FAILED``. CLASS renders ``—`` whenever the row
    carries no class, which is every healthy build, every historical row,
    and every ordinary failure.
    """
    table = Table(title="Forge build status")
    table.add_column("BUILD", overflow="fold")
    table.add_column("FEATURE", overflow="fold")
    # MODE column added by FEAT-FORGE-008 / TASK-MBC8-009 so operators
    # can disambiguate concurrent Mode A / B / C builds at a glance
    # (Group F scenarios). Legacy rows that pre-date the column render
    # as ``mode-a`` per the additive migration default.
    table.add_column("MODE")
    table.add_column("STATUS")
    # CLASS — WHICH death a FAILED build died (timeout truth, schema_v9).
    # Sits BESIDE status, never instead of it.
    table.add_column("CLASS")
    table.add_column("STAGE")
    table.add_column("STARTED")
    table.add_column("ELAPSED")

    for view in views:
        stage_cell = "—"
        if full_stages is not None:
            entries = full_stages.get(view.build_id, [])
            if entries:
                stage_cell = entries[-1].stage_label
        table.add_row(
            view.build_id,
            view.feature_id,
            view.mode.value,
            view.status.value,
            view.terminal_class or "—",
            stage_cell,
            _format_dt(view.started_at),
            _elapsed(view),
        )

    return table


def _build_in_flight_table(entries: list[BridgeRegistryEntry]) -> Table:
    """Build a Rich table for the ``--in-flight`` view (TASK-FRR-PEB-012).

    Mirrors the column layout of :func:`_build_table` so operators
    reading both views back-to-back see a consistent visual style.
    The columns are tailored to the registry's identifying fields —
    no STATE / ELAPSED / MODE because those live on the ``builds``
    projection, not the in-flight registry.
    """
    table = Table(title="Forge in-flight builds")
    table.add_column("FEATURE", overflow="fold")
    table.add_column("LIFECYCLE")
    table.add_column("THREAD", overflow="fold")
    table.add_column("RUN", overflow="fold")
    table.add_column("ATTACHED")
    table.add_column("DEADLINE")
    for entry in entries:
        table.add_row(
            entry.feature_id,
            entry.current_lifecycle,
            entry.thread_id,
            entry.run_id,
            _format_dt(entry.attached_at),
            _format_dt(entry.deadline_at),
        )
    return table


def _serialise_in_flight_entry(entry: BridgeRegistryEntry) -> dict[str, Any]:
    """Project a :class:`BridgeRegistryEntry` to a JSON-friendly dict.

    Excludes the opaque ``ack_handle_token`` because it is internal
    book-keeping for the bridge's in-memory ack callback map; surfacing
    it in CLI output would invite consumers to depend on what is
    deliberately a private contract.
    """
    return {
        "feature_id": entry.feature_id,
        "thread_id": entry.thread_id,
        "run_id": entry.run_id,
        "correlation_id": entry.correlation_id,
        "current_lifecycle": entry.current_lifecycle,
        "attached_at": entry.attached_at.isoformat(),
        "deadline_at": entry.deadline_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
        "last_event_id": entry.last_event_id,
        "published_lifecycles": sorted(entry.published_lifecycles),
    }


def _emit_in_flight_json(entries: list[BridgeRegistryEntry], *, out: Console) -> None:
    """Emit the in-flight registry payload as a JSON array to stdout."""
    rows = [_serialise_in_flight_entry(e) for e in entries]
    out.file.write(json.dumps(rows, indent=2, default=str))
    out.file.write("\n")
    out.file.flush()


def _serialise_view(view: BuildStatusView) -> dict[str, Any]:
    """Round-trip a :class:`BuildStatusView` through Pydantic JSON mode."""
    return json.loads(view.model_dump_json())


def _serialise_stage(entry: StageLogEntry) -> dict[str, Any]:
    """Round-trip a :class:`StageLogEntry` through Pydantic JSON mode."""
    return json.loads(entry.model_dump_json())


def _emit_json(
    payload: list[tuple[BuildStatusView, list[StageLogEntry]]] | list[BuildStatusView],
    *,
    full: bool,
    out: Console,
) -> None:
    """Emit the status payload as a JSON array to stdout."""
    if full:
        rows: list[dict[str, Any]] = []
        for view, entries in payload:  # type: ignore[misc]
            row = _serialise_view(view)
            row["stages"] = [_serialise_stage(e) for e in entries]
            rows.append(row)
    else:
        rows = [_serialise_view(v) for v in payload]  # type: ignore[arg-type]
    # Use ``print`` via the underlying file handle rather than rich's
    # rendering so the JSON stays clean for piping (``forge status --json
    # | jq``).
    out.file.write(json.dumps(rows, indent=2, default=str))
    out.file.write("\n")
    out.file.flush()


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


def _watch_loop(
    db_path: Path,
    feature_id: str | None,
    *,
    full: bool,
    interval_secs: float,
    console: Console,
) -> None:
    """Poll-and-render loop for ``--watch`` mode.

    Uses :class:`rich.live.Live` so re-renders happen in-place. Exits
    cleanly when every visible build is in a terminal state.
    """
    # Render once before entering the live context so an immediate
    # all-terminal result returns without flicker.
    if full:
        full_payload = _read_full_payload(db_path, feature_id)
        views = [v for v, _ in full_payload]
        full_stages = {v.build_id: entries for v, entries in full_payload}
    else:
        views = _read_status_views(db_path, feature_id)
        full_stages = None

    if _all_terminal(views):
        console.print(_build_table(views, full_stages=full_stages))
        return

    with Live(
        _build_table(views, full_stages=full_stages),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as live:
        while True:
            time.sleep(interval_secs)
            if full:
                full_payload = _read_full_payload(db_path, feature_id)
                views = [v for v, _ in full_payload]
                full_stages = {v.build_id: e for v, e in full_payload}
            else:
                views = _read_status_views(db_path, feature_id)
                full_stages = None
            live.update(_build_table(views, full_stages=full_stages))
            if _all_terminal(views):
                # One last render so the operator sees the final state.
                return


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(name="status")
@click.argument("feature_id", required=False)
@click.option(
    "--watch",
    "watch",
    is_flag=True,
    default=False,
    help=(
        "Poll every 2s and re-render via rich.live; exits when all "
        "visible builds are terminal."
    ),
)
@click.option(
    "--full",
    "full",
    is_flag=True,
    default=False,
    help=(
        "Include the last 5 stage_log entries per build "
        "(Group B 'Full status view caps stage detail at 5')."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit a JSON array suitable for piping into tooling. Each row "
        "matches the BuildStatusView Pydantic shape."
    ),
)
@click.option(
    "--db-path",
    "db_path_opt",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Override the SQLite database path. Defaults to the "
        f"{_FORGE_DB_PATH_ENV} env var or ./.forge/forge.db."
    ),
)
@click.option(
    "--in-flight",
    "in_flight",
    is_flag=True,
    default=False,
    help=(
        "Show the lifecycle bridge in-flight registry instead of the "
        "builds table. Read-only — no mutations to the registry. The "
        "view lists every build currently attached to the langgraph-"
        "runner sidecar. Combine with --json to pipe into tooling, "
        "or pass a positional FEATURE_ID to filter to one build."
    ),
)
def status_cmd(
    feature_id: str | None,
    watch: bool,
    full: bool,
    as_json: bool,
    db_path_opt: str | None,
    in_flight: bool,
) -> None:
    """Show current and recent Forge builds.

    Reads SQLite directly via ``read_only_connect()`` — does NOT touch
    the NATS bus. Per ``API-cli.md §4.2``:

    * ``forge status`` (no args): active builds + 5 most recent terminal,
      sorted newest-first.
    * ``forge status FEAT-XXX``: filter to that feature, all builds most
      recent first.
    * ``--watch``: poll every 2s and re-render via ``rich.live``.
    * ``--full``: include up to 5 ``stage_log`` entries per build.
    * ``--json``: emit a JSON array suitable for piping.
    * ``--in-flight`` (TASK-FRR-PEB-012): replace the builds-table view
      with the lifecycle bridge in-flight registry. Read-only; combines
      cleanly with ``--json``, ``--db-path`` and a positional
      ``feature_id`` filter; rejects ``--watch`` and ``--full``.
    """
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        raise click.ClickException(
            f"forge database not found at {db_path}. Set "
            f"{_FORGE_DB_PATH_ENV} or pass --db-path."
        )

    # Use a deterministically-wide console so the additional MODE column
    # (TASK-MBC8-009) does not squeeze the BUILD/FEATURE columns into
    # truncating width when running under non-tty contexts (CliRunner /
    # piped output). The interactive case still benefits because we are
    # well within typical terminal width.
    console = Console(width=160)

    try:
        if in_flight:
            # AC-001 (TASK-FRR-PEB-012): the --in-flight surface reads
            # from the lifecycle_bridge_registry rather than the builds
            # table. AC-004: combine with existing flags by rejecting
            # contradictory ones (--watch / --full do not apply to the
            # registry projection); --json and --db-path compose. The
            # positional feature_id still filters.
            if watch:
                raise click.UsageError(
                    "--in-flight is not compatible with --watch; the "
                    "registry view is a snapshot and does not require "
                    "polling."
                )
            if full:
                raise click.UsageError(
                    "--in-flight is not compatible with --full; --full "
                    "augments the builds table with stage_log entries, "
                    "which do not apply to the bridge registry."
                )
            entries = _read_in_flight_entries(db_path, feature_id)
            if as_json:
                _emit_in_flight_json(entries, out=console)
                return
            if not entries:
                # AC-003: when no builds are in-flight, emit the
                # canonical "No in-flight builds." line so operator
                # scripts can grep the output without parsing a table.
                console.print("No in-flight builds.")
                return
            console.print(_build_in_flight_table(entries))
            return

        if watch:
            if as_json:
                # JSON + watch is contradictory — refuse so we don't
                # emit a stream of JSON arrays a piping consumer would
                # have to parse line-wise. Click conventions: error.
                raise click.UsageError(
                    "--watch is not compatible with --json; pick one."
                )
            _watch_loop(
                db_path,
                feature_id,
                full=full,
                interval_secs=_WATCH_INTERVAL_SECS,
                console=console,
            )
            return

        if as_json:
            if full:
                full_payload = _read_full_payload(db_path, feature_id)
                _emit_json(full_payload, full=True, out=console)
            else:
                views = _read_status_views(db_path, feature_id)
                _emit_json(views, full=False, out=console)
            return

        # Default rendered table.
        if full:
            full_payload = _read_full_payload(db_path, feature_id)
            views = [v for v, _ in full_payload]
            full_stages = {v.build_id: e for v, e in full_payload}
        else:
            views = _read_status_views(db_path, feature_id)
            full_stages = None
        console.print(_build_table(views, full_stages=full_stages))
    except sqlite3.Error as exc:
        # Translate any low-level sqlite error into a clean Click error
        # so the user sees an actionable message rather than a stack
        # trace.
        raise click.ClickException(f"forge status: database error: {exc}") from exc
