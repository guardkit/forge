"""Priors retrieval and prose injection for the reasoning-model system prompt.

This module is the **read side** of FEAT-FORGE-006 — it complements
:mod:`forge.memory.writer` (TASK-IC-002) by retrieving previously-written
pipeline-history and calibration-history entities and assembling them into
a *prose block* that the reasoning model consumes via the
``{domain_prompt}`` placeholder in its system prompt template (mirroring the
``domain-context-injection-specialist`` pattern used by
:mod:`agents.agents`).

Four categories of priors are queried, **concurrently** via
:func:`asyncio.gather`, so the wall-clock cost of priors retrieval is one
round-trip latency rather than four. Each category has a stable section
name in the rendered prose:

* ``recent_similar_builds``           — :class:`SessionOutcome` rows from
  ``forge_pipeline_history``.
* ``recent_override_behaviour``       — :class:`OverrideEvent` rows from
  ``forge_pipeline_history``.
* ``approved_calibration_adjustments`` — :class:`CalibrationAdjustment` rows
  from ``forge_pipeline_history``, filtered to ``approved=True`` AND
  ``expires_at > now()`` per the
  ``@boundary boundary-expired-adjustments`` rule.
* ``qa_priors``                       — :class:`CalibrationEvent` rows
  from ``forge_calibration_history``.

Empty sections render as the literal ``(none)`` marker — a section is
**never** omitted from the prose block. This is the
``@edge-case empty-priors-representation`` rule and is the load-bearing
contract verified by the seam test in TASK-IC-006.

Security boundary
-----------------

Priors carry operator-history detail (overrides, rationales, Q&A snippets)
that is sensitive in two ways: it can leak operator behaviour to anyone who
reads a process listing, and it can balloon argv past the kernel limit and
crash the spawn. The
``@edge-case @security priors-as-argument-refusal`` rule therefore states:
**priors are NEVER passed as subprocess arguments.** They reach the
reasoning model only via the in-memory ``{domain_prompt}`` placeholder
substitution performed by :func:`inject_into_system_prompt`.

:func:`assert_not_in_argv` is the runtime defence-in-depth helper that
walks ``sys.argv`` and raises :class:`PriorsLeakError` if a priors chunk
appears verbatim. It is called automatically inside
:func:`render_priors_prose` so any caller that renders priors gets the
check for free.

Backend selection (mirrors :mod:`forge.memory.writer`)
------------------------------------------------------

The same 3-tier backend pattern as the writer:

* **Tier 0 — MCP/in-process** (``graphiti_core`` is importable): use it
  directly via :func:`_query_via_mcp`.
* **Tier 1 — CLI fallback** (``guardkit`` on PATH): shell out to
  ``guardkit graphiti query`` via
  :func:`asyncio.create_subprocess_exec`.
* **Tier 2 — unavailable**: log a structured warning and return an empty
  list for that category. Priors retrieval is best-effort — a missing
  backend degrades the priors block to ``(none)`` markers, it does not
  abort the build.

Tests patch :func:`_dispatch_query` directly to inject deterministic
results without going through the availability checks.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from pydantic import ValidationError

from .models import (
    CalibrationAdjustment,
    CalibrationEvent,
    OverrideEvent,
    SessionOutcome,
)

#: Module logger — structured via ``extra=...`` so log aggregators can
#: index on ``group_id`` / ``entity_type`` / ``error_class`` without
#: parsing free-text. Mirrors :mod:`forge.memory.writer`.
logger = logging.getLogger("forge.memory.priors")

#: Graphiti group_id constants — kept in sync with
#: :mod:`forge.memory.writer`. Centralised so a future "rename a group"
#: refactor only needs one edit.
PIPELINE_HISTORY_GROUP = "forge_pipeline_history"
CALIBRATION_HISTORY_GROUP = "forge_calibration_history"

#: Default recency horizon (days) reused across all four queries. The
#: same value bounds similar-builds, override behaviour, adjustments,
#: and Q&A priors so the reasoning model sees a single coherent window.
DEFAULT_HORIZON_DAYS = 30

#: Stable section ordering for :func:`render_priors_prose`. The seam
#: test in TASK-IC-006 asserts each name appears in the prose; the
#: order here is also the rendered order, so changing it is a
#: behaviour-visible change for the reasoning model.
SECTION_ORDER: tuple[str, ...] = (
    "recent_similar_builds",
    "recent_override_behaviour",
    "approved_calibration_adjustments",
    "qa_priors",
)

#: Marker rendered when a section has zero rows. Per the
#: ``@edge-case empty-priors-representation`` rule, an empty section is
#: rendered with this exact literal — never omitted, never collapsed.
EMPTY_MARKER = "(none)"

#: Default placeholder name expected in the reasoning-model system
#: prompt template, mirroring the ``domain-context-injection-specialist``
#: pattern in :mod:`agents.agents`. Kept as a constant so callers can
#: import the symbolic name rather than the literal string.
DOMAIN_PROMPT_PLACEHOLDER = "domain_prompt"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PriorsLeakError(RuntimeError):
    """Raised when priors content is detected in ``sys.argv``.

    Surfaces the
    ``@edge-case @security priors-as-argument-refusal`` rule. The
    exception message names the offending argv element so the
    operator can locate and excise the leak.
    """


# ---------------------------------------------------------------------------
# BuildContext protocol — avoid importing ``forge.pipeline`` (heavy
# transitive import of ``nats_core``).
# ---------------------------------------------------------------------------


@runtime_checkable
class _BuildContextLike(Protocol):
    """Structural shape of :class:`forge.pipeline.BuildContext`.

    Only the fields actually consumed here are required. Using a
    Protocol keeps :mod:`forge.memory.priors` independent of the
    pipeline package and its NATS dependencies — important because
    ``forge.memory.*`` is a pure schema/IO layer per the project
    boundary rules.
    """

    feature_id: str
    build_id: str


# ---------------------------------------------------------------------------
# Priors dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Priors:
    """Assembled priors for a single build.

    Each list is in retrieval order — typically newest-first as returned
    by the underlying Graphiti query, but the prose renderer does not
    rely on ordering, only on stable section *names*. Empty lists are
    valid and render as the ``(none)`` marker.

    Attributes:
        recent_similar_builds: :class:`SessionOutcome` rows for builds
            that have completed within the configured horizon and whose
            outcome (success / failure / aborted) is informative for the
            current build.
        recent_override_behaviour: :class:`OverrideEvent` rows captured
            within the horizon — what the operator chose to override
            recently and why.
        approved_calibration_adjustments: :class:`CalibrationAdjustment`
            rows that are both ``approved=True`` and not yet expired
            (``expires_at > now()``) at retrieval time.
        qa_priors: :class:`CalibrationEvent` rows from the calibration
            log within the horizon.
    """

    recent_similar_builds: list[SessionOutcome] = field(default_factory=list)
    recent_override_behaviour: list[OverrideEvent] = field(default_factory=list)
    approved_calibration_adjustments: list[CalibrationAdjustment] = field(
        default_factory=list
    )
    qa_priors: list[CalibrationEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API — retrieval
# ---------------------------------------------------------------------------


#: Type alias for the dispatcher callable. Tests pass a custom callable
#: via the ``query_fn=`` kwarg on :func:`retrieve_priors` to inject
#: deterministic results without monkeypatching the module.
QueryFn = Callable[..., Awaitable[list[dict[str, Any]]]]


async def retrieve_priors(
    build_context: _BuildContextLike,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    now: Optional[datetime] = None,
    query_fn: Optional[QueryFn] = None,
) -> Priors:
    """Issue four concurrent Graphiti queries and assemble :class:`Priors`.

    All four queries fan out via a single :func:`asyncio.gather` so the
    wall-clock cost is one round-trip latency, not four. The
    ``approved_calibration_adjustments`` query result is then filtered
    in-process to ``approved=True`` AND ``expires_at > now()`` — both
    so the boundary contract holds even when the underlying backend
    cannot express the filter, and so a clock injected via ``now=``
    can deterministically test the expiry boundary.

    Args:
        build_context: The current build's identity. Only ``feature_id``
            and ``build_id`` are read; declared as a Protocol to avoid
            importing the full pipeline module.
        horizon_days: Recency horizon applied uniformly to all four
            queries. Defaults to :data:`DEFAULT_HORIZON_DAYS`. Must be
            a positive integer.
        now: Reference timestamp for the expiry filter. Defaults to
            ``datetime.now(tz=UTC)``. Tests pin this to make the
            ``expires_at > now()`` boundary deterministic.
        query_fn: Optional dispatch override. When supplied, replaces
            the production :func:`_dispatch_query` for all four
            categories — the unit test for "four parallel queries" uses
            this to record concurrent invocations without monkeypatching.

    Returns:
        A populated :class:`Priors` dataclass. Categories whose backend
        query failed or returned an empty result render as empty lists
        (which the prose renderer maps to ``(none)``).

    Raises:
        ValueError: ``horizon_days`` is not a positive integer.
    """
    if not isinstance(horizon_days, int) or horizon_days <= 0:
        raise ValueError(
            f"horizon_days must be a positive int, got {horizon_days!r}"
        )

    now_utc = now if now is not None else datetime.now(tz=timezone.utc)
    horizon_start = now_utc - timedelta(days=horizon_days)
    invoke = query_fn if query_fn is not None else _dispatch_query

    # The four queries fan out concurrently. Each await is a coroutine
    # object handed to gather — gather schedules them on the event loop
    # so the actual round-trips overlap. ``return_exceptions=True``
    # keeps a single category failure from cancelling the other three.
    builds_raw, overrides_raw, adjustments_raw, qa_raw = await asyncio.gather(
        invoke(
            group_id=PIPELINE_HISTORY_GROUP,
            entity_type="SessionOutcome",
            since=horizon_start,
            build_context=build_context,
        ),
        invoke(
            group_id=PIPELINE_HISTORY_GROUP,
            entity_type="OverrideEvent",
            since=horizon_start,
            build_context=build_context,
        ),
        invoke(
            group_id=PIPELINE_HISTORY_GROUP,
            entity_type="CalibrationAdjustment",
            since=horizon_start,
            build_context=build_context,
        ),
        invoke(
            group_id=CALIBRATION_HISTORY_GROUP,
            entity_type="CalibrationEvent",
            since=horizon_start,
            build_context=build_context,
        ),
        return_exceptions=True,
    )

    builds = _coerce_list(SessionOutcome, builds_raw, category="recent_similar_builds")
    overrides = _coerce_list(
        OverrideEvent, overrides_raw, category="recent_override_behaviour"
    )
    adjustments_all = _coerce_list(
        CalibrationAdjustment,
        adjustments_raw,
        category="approved_calibration_adjustments",
    )
    qa = _coerce_list(CalibrationEvent, qa_raw, category="qa_priors")

    # Defence-in-depth: enforce ``approved=True AND expires_at > now()``
    # in code regardless of what the backend returned. This is the
    # ``@boundary boundary-expired-adjustments`` rule.
    adjustments_live = [
        adj
        for adj in adjustments_all
        if adj.approved and _ensure_aware(adj.expires_at) > now_utc
    ]

    return Priors(
        recent_similar_builds=builds,
        recent_override_behaviour=overrides,
        approved_calibration_adjustments=adjustments_live,
        qa_priors=qa,
    )


# ---------------------------------------------------------------------------
# Public API — prose rendering
# ---------------------------------------------------------------------------


def render_priors_prose(priors: Priors) -> str:
    """Render :class:`Priors` to a structured prose block.

    The output has exactly four named sections in :data:`SECTION_ORDER`.
    A section with zero rows renders the literal :data:`EMPTY_MARKER`
    on its own line — the section is **never omitted** and **never
    collapsed**. This is the load-bearing contract for the seam test
    in TASK-IC-006: downstream consumers can pattern-match on the
    section name AND on the ``(none)`` token to disambiguate
    "no priors known" from "priors absent due to error".

    The rendered string is also passed through :func:`assert_not_in_argv`
    before return — defence-in-depth so a caller who renders priors
    cannot then concatenate them onto a subprocess command line without
    the leak detector firing first.

    Args:
        priors: The assembled priors block.

    Returns:
        A single string ready for substitution into the
        ``{domain_prompt}`` placeholder of the reasoning model's system
        prompt template.

    Raises:
        PriorsLeakError: any non-trivial line of the rendered prose
            also appears verbatim in ``sys.argv`` — surfaces the
            ``@edge-case @security priors-as-argument-refusal`` rule.
    """
    lines: list[str] = []
    for section in SECTION_ORDER:
        items = getattr(priors, section)
        lines.append(f"## {section}")
        lines.append("")
        if not items:
            lines.append(EMPTY_MARKER)
        else:
            for item in items:
                lines.append(f"- {_format_item(item)}")
        lines.append("")

    prose = "\n".join(lines).rstrip() + "\n"
    assert_not_in_argv(prose)
    return prose


def inject_into_system_prompt(
    template: str, priors: Priors, **extras: Any
) -> str:
    """Render priors and inject via the ``{domain_prompt}`` placeholder.

    Mirrors the ``domain-context-injection-specialist`` pattern used by
    :mod:`agents.agents` — the reasoning-model system prompt is a Python
    template containing ``{domain_prompt}`` (and possibly other
    placeholders), which is filled with :meth:`str.format` at agent-
    construction time.

    Args:
        template: The raw template string. MUST contain the
            ``{domain_prompt}`` placeholder; other placeholders may be
            supplied via ``**extras``.
        priors: The priors to render and inject.
        **extras: Additional ``str.format`` kwargs forwarded verbatim.

    Returns:
        The fully-substituted system prompt string.

    Raises:
        KeyError: ``template`` references a placeholder not satisfied
            by ``domain_prompt`` or ``**extras``. Re-raised verbatim
            from :meth:`str.format`.
        PriorsLeakError: priors content is currently in ``sys.argv``
            (see :func:`render_priors_prose`).
    """
    prose = render_priors_prose(priors)
    return template.format(**{DOMAIN_PROMPT_PLACEHOLDER: prose}, **extras)


# ---------------------------------------------------------------------------
# Public API — argv leak detector
# ---------------------------------------------------------------------------


def assert_not_in_argv(text: str) -> None:
    """Verify that no chunk of ``text`` is currently in ``sys.argv``.

    Walks ``sys.argv`` element-by-element and raises
    :class:`PriorsLeakError` if any non-trivial line of ``text`` is
    contained verbatim in any argv element.

    Why this matters: priors carry operator history (override
    rationales, Q&A snippets, calibration parameters). If they leak
    into a subprocess argv they become visible to anyone who can read
    ``/proc/<pid>/cmdline`` or the process table — and on long priors
    blocks they can blow past the kernel ``ARG_MAX`` and crash the
    spawn. They MUST reach the reasoning model only via the in-memory
    ``{domain_prompt}`` substitution performed by
    :func:`inject_into_system_prompt`.

    The check splits on newlines and ignores blank/heading-only lines
    (so the section headers and the ``(none)`` marker do not produce
    false positives — those are safe even if echoed). Non-trivial lines
    are matched as substrings of each argv element so that a priors
    chunk concatenated with other text still trips the detector.

    Args:
        text: The rendered priors prose (or any text whose presence in
            ``sys.argv`` would constitute a leak).

    Raises:
        PriorsLeakError: at least one non-trivial line of ``text``
            appears in ``sys.argv``. The message names the offending
            argv element (truncated) so it can be located.
    """
    needles = _meaningful_lines(text)
    if not needles:
        return
    for arg in sys.argv:
        if not isinstance(arg, str):
            # Defensive — sys.argv is documented as list[str], but a
            # third-party hook could conceivably mutate it.
            continue
        for needle in needles:
            if needle in arg:
                truncated = arg if len(arg) <= 80 else arg[:77] + "..."
                raise PriorsLeakError(
                    "Priors leak detected in sys.argv: argument "
                    f"{truncated!r} contains priors content "
                    f"(matched line: {needle!r})"
                )


# ---------------------------------------------------------------------------
# Helpers — coercion / formatting
# ---------------------------------------------------------------------------


def _meaningful_lines(text: str) -> list[str]:
    """Return the lines of ``text`` worth checking for argv leaks.

    Skips:

    * Blank / whitespace-only lines.
    * Section-header lines (start with ``##``) — those are stable,
      non-sensitive structural tokens.
    * The standalone :data:`EMPTY_MARKER` token — also non-sensitive.
    * Lines under 12 characters — too short to confidently flag as a
      priors leak versus an unrelated argv coincidence.
    """
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            continue
        if stripped == EMPTY_MARKER:
            continue
        if len(stripped) < 12:
            continue
        out.append(stripped)
    return out


def _coerce_list(
    model_cls: type, raw_results: Any, *, category: str
) -> list[Any]:
    """Coerce a raw query result into a list of validated model rows.

    The dispatcher returns ``list[dict]`` on success or an
    :class:`Exception` instance when ``return_exceptions=True`` in
    :func:`asyncio.gather` swallowed a per-category failure. Either
    way, this helper produces a clean ``list`` of validated entities
    so the caller never sees an exception value embedded in a
    "list" position.

    A pydantic :class:`ValidationError` on an individual row is logged
    and the row is dropped — one bad row from Graphiti must not
    poison the whole category.

    Args:
        model_cls: The pydantic model class to validate each row
            against (e.g. :class:`SessionOutcome`).
        raw_results: Either a list of dicts, or an Exception captured
            by gather's ``return_exceptions=True``.
        category: Section name for log enrichment.

    Returns:
        A list of validated ``model_cls`` instances. Empty when the
        backend errored or no rows matched.
    """
    if isinstance(raw_results, BaseException):
        logger.warning(
            "graphiti_priors_query_failed",
            extra={
                "category": category,
                "error_class": type(raw_results).__name__,
                "error_message": str(raw_results),
            },
        )
        return []
    if not isinstance(raw_results, list):
        logger.warning(
            "graphiti_priors_query_unexpected_shape",
            extra={
                "category": category,
                "actual_type": type(raw_results).__name__,
            },
        )
        return []

    coerced: list[Any] = []
    for row in raw_results:
        if isinstance(row, model_cls):
            coerced.append(row)
            continue
        if not isinstance(row, dict):
            logger.warning(
                "graphiti_priors_row_unexpected_shape",
                extra={"category": category, "actual_type": type(row).__name__},
            )
            continue
        try:
            coerced.append(model_cls.model_validate(row))
        except ValidationError as exc:
            logger.warning(
                "graphiti_priors_row_validation_failed",
                extra={
                    "category": category,
                    "model": model_cls.__name__,
                    "error_message": str(exc),
                },
            )
    return coerced


def _ensure_aware(dt: datetime) -> datetime:
    """Return ``dt`` as a timezone-aware UTC datetime.

    pydantic accepts both naive and aware datetimes; the priors
    expiry boundary requires a deterministic comparison against
    an aware ``now``. A naive datetime is assumed to be in UTC —
    callers writing into Graphiti via :mod:`forge.memory.writer`
    use ``datetime.now(tz=UTC)`` so this assumption holds end-to-end.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_item(item: Any) -> str:
    """One-line compact representation of a priors entity for the prose.

    Tailored per entity type so the reasoning model gets the most
    decision-relevant fields up front (outcome, decision, parameter)
    rather than a dump of every field. A best-effort ``repr`` is the
    fallback so an unexpected entity type never crashes the renderer.
    """
    if isinstance(item, SessionOutcome):
        return (
            f"build={item.build_id} outcome={item.outcome} "
            f"closed_at={item.closed_at.isoformat()}"
        )
    if isinstance(item, OverrideEvent):
        return (
            f"override decided_at={item.decided_at.isoformat()} "
            f"decision={item.operator_decision!r} "
            f"rationale={item.operator_rationale!r}"
        )
    if isinstance(item, CalibrationAdjustment):
        return (
            f"adjustment param={item.parameter!r} "
            f"old={item.old_value!r} new={item.new_value!r} "
            f"expires_at={item.expires_at.isoformat()}"
        )
    if isinstance(item, CalibrationEvent):
        return (
            f"qa source={item.source_file} "
            f"captured_at={item.captured_at.isoformat()} "
            f"question={item.question!r}"
        )
    return repr(item)  # pragma: no cover — defensive fallback


# ---------------------------------------------------------------------------
# Backend dispatch (mirrors writer.py 3-tier pattern)
# ---------------------------------------------------------------------------


def _mcp_backend_available() -> bool:
    """Return ``True`` when ``graphiti_core`` can be imported."""
    return importlib.util.find_spec("graphiti_core") is not None


def _cli_backend_available() -> bool:
    """Return ``True`` when ``guardkit`` is on the current PATH."""
    return shutil.which("guardkit") is not None


async def _dispatch_query(
    *,
    group_id: str,
    entity_type: str,
    since: datetime,
    build_context: _BuildContextLike,
) -> list[dict[str, Any]]:
    """Choose the highest-tier available backend and await the query.

    Tests patch this function directly (or pass ``query_fn=`` to
    :func:`retrieve_priors`) to inject deterministic results without
    going through the availability checks.

    Args:
        group_id: Graphiti group_id, e.g. ``"forge_pipeline_history"``.
        entity_type: The pipeline-history entity class name to filter
            on (e.g. ``"SessionOutcome"``).
        since: Lower bound for the recency filter — only entities
            written at or after this timestamp are returned.
        build_context: Used by future production backends to scope
            "similar builds" by ``feature_id``. Forwarded to both
            backends for parity but currently unused at the wire.

    Returns:
        A list of episode-body dicts ready for pydantic validation by
        :func:`_coerce_list`. Empty list when no backend is available
        (priors retrieval is best-effort — see module docstring).
    """
    if _mcp_backend_available():
        return await _query_via_mcp(group_id, entity_type, since, build_context)
    if _cli_backend_available():
        return await _query_via_cli(group_id, entity_type, since, build_context)
    logger.warning(
        "graphiti_priors_backend_unavailable",
        extra={"group_id": group_id, "entity_type": entity_type},
    )
    return []


def _build_query_string(
    entity_type: str, since: datetime, build_context: _BuildContextLike
) -> str:
    """Compose the natural-language query passed to the Graphiti backend.

    The query asks for ``entity_type`` rows from ``since`` onward,
    optionally scoped to the current ``feature_id`` for the
    similar-builds category. Both backends consume the same string so
    the recency horizon and feature scoping are uniform across MCP and
    CLI.
    """
    iso_since = since.isoformat()
    feature_id = getattr(build_context, "feature_id", "")
    return (
        f"Recent {entity_type} entries since {iso_since}"
        + (f" for feature {feature_id}" if feature_id else "")
    )


async def _query_via_mcp(
    group_id: str,
    entity_type: str,
    since: datetime,
    build_context: _BuildContextLike,
) -> list[dict[str, Any]]:
    """Tier 0 — query via the in-process ``graphiti_core`` client.

    Lazily imports ``graphiti_core`` so this module imports cleanly
    in environments where the optional dependency is absent. The
    canonical client method is ``search`` / ``search_nodes`` per the
    ``graphiti-core`` README; we look up whichever entry point the
    installed version exposes so a minor API rename does not break
    the priors path.
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
            "graphiti_priors_mcp_search_method_missing",
            extra={"group_id": group_id, "entity_type": entity_type},
        )
        return []
    query = _build_query_string(entity_type, since, build_context)
    raw = await search(query=query, group_id=group_id)
    return _normalise_episode_bodies(raw)


async def _query_via_cli(
    group_id: str,
    entity_type: str,
    since: datetime,
    build_context: _BuildContextLike,
) -> list[dict[str, Any]]:
    """Tier 1 — query via ``guardkit graphiti query`` subprocess.

    The natural-language query is passed via ``--query``. The
    subcommand starts with ``"graphiti "``, so the resolver-skip
    branch in :mod:`forge.adapters.guardkit.run` fires and no
    ``--context`` flag is threaded (DDR-005).

    On non-zero exit we log and return an empty list — priors
    retrieval is best-effort and a backend failure must not abort
    the pipeline.
    """
    query = _build_query_string(entity_type, since, build_context)
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
            "graphiti_priors_cli_failed",
            extra={
                "group_id": group_id,
                "entity_type": entity_type,
                "returncode": proc.returncode,
                "stderr": stderr_bytes.decode(errors="replace").strip(),
            },
        )
        return []
    try:
        payload = json.loads(stdout_bytes.decode(errors="replace") or "[]")
    except json.JSONDecodeError as exc:
        logger.warning(
            "graphiti_priors_cli_json_decode_failed",
            extra={
                "group_id": group_id,
                "entity_type": entity_type,
                "error_message": str(exc),
            },
        )
        return []
    return _normalise_episode_bodies(payload)


def _normalise_episode_bodies(raw: Any) -> list[dict[str, Any]]:
    """Coerce a backend response into a flat list of episode-body dicts.

    Both backends may return either a bare list of episode bodies, a
    list of episode envelopes (where the body lives under
    ``"episode_body"`` as a JSON string), or a dict wrapping the list
    under ``"results"``. Normalising in one place keeps
    :func:`_coerce_list` simple.
    """
    if isinstance(raw, dict):
        raw = raw.get("results", raw.get("nodes", []))
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and "episode_body" in entry:
            body = entry["episode_body"]
            if isinstance(body, str):
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    out.append(parsed)
            elif isinstance(body, dict):
                out.append(body)
        elif isinstance(entry, dict):
            out.append(entry)
    return out


__all__ = [
    "CALIBRATION_HISTORY_GROUP",
    "DEFAULT_HORIZON_DAYS",
    "DOMAIN_PROMPT_PLACEHOLDER",
    "EMPTY_MARKER",
    "PIPELINE_HISTORY_GROUP",
    "Priors",
    "PriorsLeakError",
    "SECTION_ORDER",
    "assert_not_in_argv",
    "inject_into_system_prompt",
    "render_priors_prose",
    "retrieve_priors",
]
