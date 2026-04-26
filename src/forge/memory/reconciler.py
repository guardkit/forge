"""Reconcile-backfill at build start (TASK-IC-004).

This module is the **durability safety net** for the fire-and-forget
Graphiti writes shipped by :mod:`forge.memory.writer` (TASK-IC-002). At
the start of every build — *before* priors retrieval (TASK-IC-006), so
priors see a consistent Graphiti state — the orchestrator invokes
:func:`reconcile_pipeline_history` to:

1. Read the SQLite ledger via the FEAT-FORGE-001
   :class:`PipelineHistoryRepository` interface for the last
   ``horizon_days`` (default 30) of pipeline-history rows.
2. Query Graphiti for the set of ``entity_id`` values that already exist
   in the ``forge_pipeline_history`` group within the same horizon.
3. Compute the set difference — UUIDs present in SQLite but absent from
   Graphiti — and replay each missing entity through
   :func:`forge.memory.writer.write_entity` (the
   *synchronous-failure* variant, NOT fire-and-forget — the caller wants
   to know whether the backfill actually succeeded).
4. Aggregate the outcome into a :class:`ReconcileReport` carrying the
   counts and a per-entity diagnostic for any backfill failure.

Risk-3 mitigation (no SQLite schema coupling)
---------------------------------------------

Per the task review (``parent_review: TASK-REV-IC8B``), this module
**must NOT** know anything about the SQLite schema, table names, or row
shape. The reconcile contract is exclusively the
:class:`PipelineHistoryRepository` Protocol below — it produces
already-validated :data:`PipelineHistoryEntity` instances. If a future
schema migration adds a column or splits a table, the change is absorbed
in the FEAT-FORGE-001 repository implementation without touching this
module. The Protocol approach mirrors the
:mod:`forge.memory.priors`/``_BuildContextLike`` pattern: structural
typing keeps ``forge.memory.*`` independent of the heavy repository
package.

Failure-handling contract
-------------------------

* **Per-entity backfill failures DO NOT raise.** Every exception from
  :func:`write_entity` is caught, recorded as a :class:`FailedEntity`
  in the report, and emitted to the structured log (matching the
  failure-log shape in :mod:`forge.memory.writer`). This is the
  durability invariant: a single bad row must not abort reconcile of
  the other hundreds.
* **Graphiti-query failures degrade gracefully.** The query path
  catches transport errors and falls back to ``set()`` so reconcile
  treats *every* SQLite row as missing — at worst we re-write rows
  that already exist, at best we recover from a transient outage. The
  Graphiti writer is itself idempotent on ``entity_id`` per ASSUM-007.
* :func:`reconcile_pipeline_history` itself never raises for
  individual row outcomes. It only raises :class:`ValueError` for an
  invalid ``horizon_days``, mirroring the validation in
  :mod:`forge.memory.priors`.

Performance
-----------

For a 30-day horizon the expected entity count is "low hundreds, not
thousands" per the task implementation notes. A simple per-entity write
loop is well within budget; we deliberately do not pre-optimise with
bulk APIs. The SQLite read happens once; the Graphiti query happens
once; backfill writes are sequential to keep failure attribution
unambiguous in the report.

Testing
-------

Tests inject custom callables via the ``write_fn=`` and ``query_fn=``
kwargs on :func:`reconcile_pipeline_history` rather than monkeypatching
module-level helpers. This mirrors the pattern in :mod:`forge.memory.priors`
where ``query_fn=`` lets a test record concurrent invocations
deterministically. Production callers omit both kwargs and the module
falls back to its 3-tier dispatcher (MCP → CLI → empty-set), again
mirroring writer/priors.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Optional,
    Protocol,
    runtime_checkable,
)

from .writer import PipelineHistoryEntity, write_entity

#: Module logger. Failure log lines are emitted via ``extra=...`` so
#: aggregators can index on ``entity_id`` / ``entity_type`` /
#: ``error_class`` without parsing free-text. Mirrors the convention in
#: :mod:`forge.memory.writer` and :mod:`forge.memory.priors`.
logger = logging.getLogger("forge.memory.reconciler")

#: Graphiti group_id holding pipeline-history entities. Kept in sync
#: with :data:`forge.memory.priors.PIPELINE_HISTORY_GROUP`. Centralised
#: so a future "rename a group" refactor is one edit.
PIPELINE_HISTORY_GROUP = "forge_pipeline_history"

#: Default reconcile horizon in days. The task spec sets this at 30 and
#: makes it overridable via ``forge.yaml`` (the orchestrator wires the
#: config value through ``horizon_days=`` at the call site).
DEFAULT_RECONCILE_HORIZON_DAYS = 30


# ---------------------------------------------------------------------------
# Repository Protocol — Risk-3 mitigation: no SQLite schema knowledge here.
# ---------------------------------------------------------------------------


@runtime_checkable
class PipelineHistoryRepository(Protocol):
    """Structural contract for the FEAT-FORGE-001 SQLite repository.

    The reconcile pass reads only via this Protocol — never via direct
    SQLite SQL or schema knowledge (Risk 3 mitigation from the parent
    review TASK-REV-IC8B). Implementations are responsible for their own
    transaction semantics; reconcile treats the returned iterable as a
    point-in-time snapshot of the SQLite ledger.

    The single method, :meth:`list_entities_since`, returns an iterable
    of already-validated :data:`PipelineHistoryEntity` instances
    (pydantic models). Returning *entities*, not raw rows, is the
    boundary contract: schema concerns stay inside FEAT-FORGE-001.
    """

    def list_entities_since(
        self, since: datetime
    ) -> Iterable[PipelineHistoryEntity]:
        """Return every pipeline-history entity written at or after ``since``.

        Args:
            since: Lower bound for the recency filter, expressed as a
                timezone-aware datetime. Implementations MUST treat
                naive datetimes as UTC for parity with the writer.

        Returns:
            An iterable of validated pipeline-history entities. May be
            empty. Order is unspecified — reconcile treats the result
            as a set keyed on ``entity_id``.
        """
        ...  # pragma: no cover — Protocol method body


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedEntity:
    """Diagnostic record for a single backfill failure.

    A :class:`FailedEntity` is appended to :attr:`ReconcileReport.failed_entities`
    whenever :func:`forge.memory.writer.write_entity` raises during the
    backfill replay. It carries enough metadata for the operator to
    correlate the failure with the underlying pipeline run without
    digging through stack traces.

    Attributes:
        entity_id: String form of the offending entity's ``entity_id``
            (UUIDs are stringified so the dataclass is JSON-serialisable
            for downstream telemetry).
        entity_type: Class name of the entity (e.g. ``"GateDecision"``)
            so log aggregators can pivot by type.
        error_class: ``type(exc).__name__`` of the underlying exception.
        error_message: ``str(exc)`` — the human-readable failure text.
    """

    entity_id: str
    entity_type: str
    error_class: str
    error_message: str


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of a single reconcile pass.

    Returned by :func:`reconcile_pipeline_history`. The report is
    deliberately a small immutable dataclass so the orchestrator can
    log it, compare two passes, or feed it into a metrics collector
    without copying.

    Attributes:
        total_sqlite: Number of entities the SQLite repository returned
            for the horizon window — the reconcile baseline.
        total_graphiti: Number of distinct ``entity_id`` values the
            Graphiti query returned for the same horizon. May be zero
            when the backend was unavailable; in that case every SQLite
            row is treated as missing and replayed.
        backfilled_count: Number of entities the reconcile pass
            successfully wrote into Graphiti. Equals the size of the
            set difference minus :attr:`failed_count`.
        failed_count: Number of entities whose backfill replay raised.
            Equals ``len(failed_entities)``.
        failed_entities: Per-entity diagnostics for every backfill
            failure. Empty when no failures occurred.
    """

    total_sqlite: int = 0
    total_graphiti: int = 0
    backfilled_count: int = 0
    failed_count: int = 0
    failed_entities: list[FailedEntity] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Type aliases for injected callables
# ---------------------------------------------------------------------------

#: Signature of the Graphiti-side "give me the entity_ids in this group
#: since this timestamp" query. Tests inject a custom callable to avoid
#: monkeypatching the module-level dispatcher.
GraphitiIdQueryFn = Callable[..., Awaitable[set[str]]]

#: Signature of the per-entity write callable. Production callers use
#: :func:`forge.memory.writer.write_entity`; tests inject a recorder so
#: backfill calls can be asserted without going near a real backend.
WriteFn = Callable[[PipelineHistoryEntity, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reconcile_pipeline_history(
    sqlite_repo: PipelineHistoryRepository,
    *,
    horizon_days: int = DEFAULT_RECONCILE_HORIZON_DAYS,
    now: Optional[datetime] = None,
    write_fn: Optional[WriteFn] = None,
    query_fn: Optional[GraphitiIdQueryFn] = None,
) -> ReconcileReport:
    """Diff SQLite vs Graphiti for the horizon window; backfill missing entities.

    The reconcile pass is invoked once per build start, before priors
    retrieval, so priors see a consistent Graphiti state. The function
    is async because the Graphiti query and the per-entity backfill
    writes are I/O bound; the SQLite read is performed synchronously
    inside this coroutine because the FEAT-FORGE-001 repository is a
    sync interface and the call returns in tens of milliseconds for
    the expected horizon size.

    Args:
        sqlite_repo: Object satisfying the :class:`PipelineHistoryRepository`
            Protocol — typically the FEAT-FORGE-001 SQLite repository
            implementation. Must NOT be ``None``.
        horizon_days: Recency horizon applied to both the SQLite read
            and the Graphiti query. Defaults to
            :data:`DEFAULT_RECONCILE_HORIZON_DAYS` (30). Must be a
            positive integer.
        now: Reference timestamp for computing the lower bound. Defaults
            to ``datetime.now(tz=UTC)``. Tests pin this to make the
            horizon boundary deterministic.
        write_fn: Optional override for the per-entity write call. When
            ``None`` (production), :func:`forge.memory.writer.write_entity`
            is used. Tests pass a recording coroutine to assert which
            entities were replayed.
        query_fn: Optional override for the Graphiti entity_id query.
            When ``None`` (production), the module's 3-tier dispatcher
            (:func:`_dispatch_id_query`) is used.

    Returns:
        A populated :class:`ReconcileReport`. Per-entity backfill
        failures appear in :attr:`ReconcileReport.failed_entities` and
        are simultaneously emitted as a structured log line. The
        function itself does NOT raise on backfill failure — a single
        bad row must not poison the rest of the pass.

    Raises:
        ValueError: ``horizon_days`` is not a positive integer, or
            ``sqlite_repo`` is ``None``.
    """
    if sqlite_repo is None:
        raise ValueError("sqlite_repo must not be None")
    if not isinstance(horizon_days, int) or horizon_days <= 0:
        raise ValueError(
            f"horizon_days must be a positive int, got {horizon_days!r}"
        )

    now_utc = now if now is not None else datetime.now(tz=timezone.utc)
    horizon_start = now_utc - timedelta(days=horizon_days)

    # 1. Read SQLite — strictly via the repository Protocol. We
    #    materialise the iterable into a list so we can both count it
    #    and iterate twice (the diff and the backfill loop).
    try:
        sqlite_entities: list[PipelineHistoryEntity] = list(
            sqlite_repo.list_entities_since(horizon_start)
        )
    except Exception as exc:  # noqa: BLE001 — boundary swallow
        # A repository read failure is unrecoverable for *this* reconcile
        # pass — we have no baseline to diff against. Log and return an
        # empty report so the build can proceed (reconcile is a safety
        # net; if it cannot run, the next build will retry).
        logger.error(
            "reconcile_sqlite_read_failed",
            extra={
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "horizon_days": horizon_days,
            },
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return ReconcileReport()

    # 2. Query Graphiti — degrades to an empty set when the backend is
    #    unavailable so reconcile treats every SQLite row as missing
    #    (the writer is idempotent on entity_id per ASSUM-007).
    fetch_ids = query_fn if query_fn is not None else _dispatch_id_query
    try:
        graphiti_ids: set[str] = await fetch_ids(
            group_id=PIPELINE_HISTORY_GROUP, since=horizon_start
        )
    except Exception as exc:  # noqa: BLE001 — boundary swallow
        logger.warning(
            "reconcile_graphiti_query_failed",
            extra={
                "group_id": PIPELINE_HISTORY_GROUP,
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        graphiti_ids = set()

    if not isinstance(graphiti_ids, set):
        # Defensive — a custom query_fn could return a list or tuple. We
        # need set membership semantics for the diff, so coerce.
        graphiti_ids = set(graphiti_ids)

    # 3. Diff and backfill. We iterate sqlite_entities (not the difference)
    #    so the report's totals reflect every SQLite row we considered;
    #    rows already present in Graphiti are skipped silently.
    write = write_fn if write_fn is not None else write_entity

    backfilled = 0
    failed: list[FailedEntity] = []
    for entity in sqlite_entities:
        # ``entity_id`` may be a UUID or a deterministic string
        # (CalibrationEvent uses (source_file, line_range_hash)); both
        # serialise to ``str()`` cleanly and the Graphiti id set is
        # populated with strings, so direct membership works.
        entity_id_str = str(getattr(entity, "entity_id", ""))
        if entity_id_str and entity_id_str in graphiti_ids:
            continue

        entity_type = type(entity).__name__
        try:
            await write(entity, PIPELINE_HISTORY_GROUP)
        except Exception as exc:  # noqa: BLE001 — boundary swallow
            failed.append(
                FailedEntity(
                    entity_id=entity_id_str,
                    entity_type=entity_type,
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            logger.error(
                "reconcile_backfill_failed",
                extra={
                    "entity_id": entity_id_str,
                    "entity_type": entity_type,
                    "group_id": PIPELINE_HISTORY_GROUP,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            backfilled += 1

    return ReconcileReport(
        total_sqlite=len(sqlite_entities),
        total_graphiti=len(graphiti_ids),
        backfilled_count=backfilled,
        failed_count=len(failed),
        failed_entities=failed,
    )


# ---------------------------------------------------------------------------
# Backend dispatch — 3-tier MCP/CLI/none, mirrors writer.py + priors.py
# ---------------------------------------------------------------------------


def _mcp_backend_available() -> bool:
    """Return ``True`` when ``graphiti_core`` can be imported.

    Mirrors :func:`forge.memory.writer._mcp_backend_available` —
    duplicated rather than imported so each module owns its tier-check
    contract independently (writer and priors do the same).
    """
    return importlib.util.find_spec("graphiti_core") is not None


def _cli_backend_available() -> bool:
    """Return ``True`` when ``guardkit`` is on the current PATH."""
    return shutil.which("guardkit") is not None


async def _dispatch_id_query(
    *, group_id: str, since: datetime
) -> set[str]:
    """Choose the highest-tier available backend and fetch entity_ids.

    Tests typically pass ``query_fn=`` to
    :func:`reconcile_pipeline_history` rather than monkeypatching this
    function, but the patch path is also supported.

    Args:
        group_id: Graphiti group_id, e.g. ``"forge_pipeline_history"``.
        since: Lower bound — only entities written at or after this
            timestamp are returned.

    Returns:
        A set of stringified ``entity_id`` values. Empty when no
        backend is available; reconcile then treats every SQLite row
        as missing (idempotent re-write per ASSUM-007).
    """
    if _mcp_backend_available():
        return await _query_ids_via_mcp(group_id, since)
    if _cli_backend_available():
        return await _query_ids_via_cli(group_id, since)
    logger.warning(
        "reconcile_backend_unavailable",
        extra={"group_id": group_id},
    )
    return set()


async def _query_ids_via_mcp(group_id: str, since: datetime) -> set[str]:
    """Tier 0 — query entity_ids via the in-process ``graphiti_core`` client.

    Lazily imports ``graphiti_core`` so this module imports cleanly when
    the optional dependency is absent. We extract ``entity_id`` from
    each returned record's ``episode_body`` (string-or-dict, mirroring
    :func:`forge.memory.priors._normalise_episode_bodies`) so the
    reconcile diff sees exactly the same identity that the writer used.
    """
    import importlib

    graphiti_core = importlib.import_module("graphiti_core")
    client = graphiti_core.Graphiti()  # type: ignore[attr-defined]
    search = (
        getattr(client, "search_nodes", None)
        or getattr(client, "search", None)
    )
    if search is None:  # pragma: no cover — API drift guard
        logger.warning(
            "reconcile_mcp_search_method_missing",
            extra={"group_id": group_id},
        )
        return set()
    query = (
        f"All pipeline-history entity_ids in {group_id} since "
        f"{since.isoformat()}"
    )
    raw = await search(query=query, group_id=group_id)
    return _extract_entity_ids(raw)


async def _query_ids_via_cli(group_id: str, since: datetime) -> set[str]:
    """Tier 1 — query entity_ids via ``guardkit graphiti query`` subprocess.

    The natural-language query is identical to the MCP tier so both
    backends scope to the same horizon. On non-zero exit we log and
    return an empty set — reconcile then re-writes every SQLite row,
    relying on writer-side idempotency.
    """
    query = (
        f"All pipeline-history entity_ids in {group_id} since "
        f"{since.isoformat()}"
    )
    proc = await asyncio.create_subprocess_exec(
        "guardkit",
        "graphiti",
        "query",
        "--group",
        group_id,
        "--query",
        query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "reconcile_cli_failed",
            extra={
                "group_id": group_id,
                "returncode": proc.returncode,
                "stderr": stderr_bytes.decode(errors="replace").strip(),
            },
        )
        return set()
    try:
        payload: Any = json.loads(stdout_bytes.decode(errors="replace") or "[]")
    except json.JSONDecodeError as exc:
        logger.warning(
            "reconcile_cli_json_decode_failed",
            extra={
                "group_id": group_id,
                "error_message": str(exc),
            },
        )
        return set()
    return _extract_entity_ids(payload)


def _extract_entity_ids(raw: Any) -> set[str]:
    """Walk a Graphiti backend response and collect every ``entity_id``.

    Backends may return any of:

    * a flat list of episode-body dicts (each with ``entity_id``);
    * a list of envelopes wrapping ``episode_body`` as a JSON string;
    * a dict with ``"results"`` or ``"nodes"`` holding either of the above.

    We normalise all three shapes into a single ``set[str]`` so the
    reconcile diff is shape-independent. Anything we cannot parse (a
    bad envelope, a missing ``entity_id``) is silently skipped — those
    rows simply look "missing" and reconcile re-writes them; better an
    extra idempotent write than a crash.
    """
    out: set[str] = set()
    if isinstance(raw, dict):
        raw = raw.get("results", raw.get("nodes", []))
    if not isinstance(raw, list):
        return out
    for entry in raw:
        body: Any
        if isinstance(entry, dict) and "episode_body" in entry:
            body = entry["episode_body"]
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    continue
        elif isinstance(entry, dict):
            body = entry
        else:
            continue
        if not isinstance(body, dict):
            continue
        entity_id = body.get("entity_id")
        if entity_id is None:
            continue
        out.add(str(entity_id))
    return out


__all__ = [
    "DEFAULT_RECONCILE_HORIZON_DAYS",
    "FailedEntity",
    "PIPELINE_HISTORY_GROUP",
    "PipelineHistoryRepository",
    "ReconcileReport",
    "reconcile_pipeline_history",
]
