"""Capability resolution algorithm for the Forge discovery layer.

Implements the algorithm in DM-discovery §3:

1. **Exact tool-name match**: any cached, non-degraded agent whose
   manifest advertises a ``ToolCapability`` with ``name == tool_name``.
2. **Intent-pattern fallback**: when no tool match is found *and* the
   caller supplied an ``intent_pattern``, match agents whose
   ``IntentCapability.pattern`` matches and whose
   ``IntentCapability.confidence >= min_confidence``.
3. **Tie-break**: ``trust_tier`` rank (core(0) > specialist(1) >
   extension(2)), then capability confidence (higher wins), then
   ``last_queue_depth`` (lower wins).

Returns ``(None, CapabilityResolution(match_source="unresolved"))`` on
miss so the caller can persist the unresolved attempt — that history
is exactly what the learning layer needs.

Pattern matching is intentionally simple: a pattern matches when its
left-hand glob prefix (the part before ``*``) is a prefix of the
target. This mirrors the conventions used by manifest authors —
``"build.*"`` matches ``"build.run"``, ``"build.greenfield"``, and so
on. A pattern with no ``*`` requires exact equality. This keeps the
domain layer free of regex-engine choices; richer matching can be
layered in later without breaking callers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from nats_core.manifest import AgentManifest, IntentCapability

from forge.discovery.models import (
    CapabilityResolution,
    DiscoveryCacheEntry,
    TrustTier,
)

logger = logging.getLogger(__name__)

# Trust-tier rank — lower = higher priority (DM-discovery §3).
_TRUST_TIER_RANK: dict[str, int] = {
    "core": 0,
    "specialist": 1,
    "extension": 2,
}


def _trust_tier_rank(tier: TrustTier) -> int:
    """Return the integer rank for a trust tier (core=0 ... extension=2)."""
    return _TRUST_TIER_RANK[tier]


def _pattern_matches(manifest_pattern: str, requested_pattern: str) -> bool:
    """Return ``True`` when ``requested_pattern`` is covered by the manifest.

    Matching rules (kept deliberately simple — see module docstring):

    * Exact equality always matches.
    * A manifest pattern of the form ``"prefix.*"`` matches any
      requested pattern starting with ``"prefix."`` (or equal to
      ``"prefix"``). The leading ``*`` (``"*.suffix"``) is treated as
      a suffix match.
    * Anything else falls through to the equality check.
    """
    if manifest_pattern == requested_pattern:
        return True
    if "*" not in manifest_pattern:
        return False
    if manifest_pattern.endswith("*"):
        prefix = manifest_pattern[:-1]
        return requested_pattern.startswith(prefix)
    if manifest_pattern.startswith("*"):
        suffix = manifest_pattern[1:]
        return requested_pattern.endswith(suffix)
    # Middle wildcard: split on the *first* '*' and require both halves.
    head, _, tail = manifest_pattern.partition("*")
    return requested_pattern.startswith(head) and requested_pattern.endswith(tail)


def _matching_intent(
    manifest: AgentManifest,
    intent_pattern: str,
    min_confidence: float,
) -> IntentCapability | None:
    """Return the first :class:`IntentCapability` matching the request, if any.

    Args:
        manifest: The agent's manifest.
        intent_pattern: Pattern requested by the caller.
        min_confidence: Confidence floor — lower-confidence matches are
            ignored.
    """
    for intent in manifest.intents:
        if (
            _pattern_matches(intent.pattern, intent_pattern)
            and intent.confidence >= min_confidence
        ):
            return intent
    return None


def _best_confidence(
    manifest: AgentManifest,
    tool_name: str,
    intent_pattern: str | None,
    min_confidence: float,
) -> float:
    """Return the confidence used for tie-break / persistence.

    For ``tool_exact`` matches we treat tool capabilities as
    fully-specified (confidence ``1.0``). For ``intent_pattern``
    matches we use the highest matching intent's ``confidence``.
    Falls back to ``0.0`` if nothing matches — defensive only;
    the caller is expected to have already filtered.
    """
    if any(t.name == tool_name for t in manifest.tools):
        return 1.0
    if intent_pattern is None:
        return 0.0
    best = 0.0
    for intent in manifest.intents:
        if (
            _pattern_matches(intent.pattern, intent_pattern)
            and intent.confidence >= min_confidence
            and intent.confidence > best
        ):
            best = intent.confidence
    return best


def _filter_tool_candidates(
    snapshot: dict[str, DiscoveryCacheEntry],
    tool_name: str,
) -> list[DiscoveryCacheEntry]:
    """Return non-degraded entries advertising ``tool_name`` exactly."""
    return [
        e
        for e in snapshot.values()
        if e.last_heartbeat_status != "degraded"
        and any(t.name == tool_name for t in e.manifest.tools)
    ]


def _filter_intent_candidates(
    snapshot: dict[str, DiscoveryCacheEntry],
    intent_pattern: str,
    min_confidence: float,
) -> list[DiscoveryCacheEntry]:
    """Return non-degraded entries with a matching intent above the floor."""
    return [
        e
        for e in snapshot.values()
        if e.last_heartbeat_status != "degraded"
        and _matching_intent(e.manifest, intent_pattern, min_confidence) is not None
    ]


def resolve(
    snapshot: dict[str, DiscoveryCacheEntry],
    tool_name: str,
    intent_pattern: str | None = None,
    min_confidence: float = 0.7,
    *,
    build_id: str = "unknown",
    stage_label: str = "unknown",
    resolution_id: str | None = None,
    now: datetime | None = None,
) -> tuple[str | None, CapabilityResolution]:
    """Resolve a tool/intent request to an agent against a cache snapshot.

    The resolver is a **pure function** of the snapshot — it never
    reaches into the cache itself, so callers (the dispatch tool) can
    test resolution with hand-built dicts and still exercise every
    branch.

    The algorithm is exact-tool → intent-fallback → tie-break, as
    specified in DM-discovery §3. A miss returns
    ``(None, CapabilityResolution(match_source="unresolved", ...))`` so
    the caller can persist the failed attempt.

    Args:
        snapshot: Mapping of ``agent_id`` to :class:`DiscoveryCacheEntry`,
            typically produced by
            :meth:`DiscoveryCache.snapshot <forge.discovery.cache.DiscoveryCache.snapshot>`.
        tool_name: Tool name to match exactly against
            ``ToolCapability.name``.
        intent_pattern: Optional intent pattern for fallback matching.
            ``None`` disables the intent-fallback step.
        min_confidence: Confidence floor for intent matches. Defaults
            to ``0.7`` per DM-discovery §3.
        build_id: Build identifier embedded in the resolution record.
        stage_label: Stage label embedded in the resolution record.
        resolution_id: Optional UUID for the resolution. Generated
            with :func:`uuid.uuid4` if not supplied.
        now: Optional UTC timestamp for ``resolved_at``. Defaults to
            ``datetime.now(UTC)`` — exposed so tests can pin time.

    Returns:
        A two-tuple ``(matched_agent_id, resolution)``. The agent id
        is ``None`` for the unresolved path, in which case
        ``resolution.match_source`` is ``"unresolved"``.
    """
    resolution_id = resolution_id or str(uuid.uuid4())
    resolved_at = now if now is not None else datetime.now(UTC)

    # 1. Exact tool-name match.
    candidates = _filter_tool_candidates(snapshot, tool_name)
    match_source: str = "tool_exact"

    # 2. Intent fallback.
    if not candidates and intent_pattern:
        candidates = _filter_intent_candidates(
            snapshot, intent_pattern, min_confidence,
        )
        match_source = "intent_pattern"

    # 3. Unresolved.
    if not candidates:
        logger.info(
            "discovery.resolve.unresolved tool=%s intent=%s",
            tool_name,
            intent_pattern,
        )
        return None, CapabilityResolution(
            resolution_id=resolution_id,
            build_id=build_id,
            stage_label=stage_label,
            requested_tool=tool_name,
            requested_intent=intent_pattern,
            matched_agent_id=None,
            match_source="unresolved",
            competing_agents=[],
            chosen_trust_tier=None,
            chosen_confidence=None,
            chosen_queue_depth=None,
            resolved_at=resolved_at,
        )

    # 4. Tie-break: trust_tier (rank asc) -> -confidence -> queue_depth.
    candidates.sort(
        key=lambda e: (
            _trust_tier_rank(e.manifest.trust_tier),
            -_best_confidence(
                e.manifest, tool_name, intent_pattern, min_confidence,
            ),
            e.last_queue_depth,
        ),
    )
    chosen = candidates[0]
    chosen_confidence = _best_confidence(
        chosen.manifest, tool_name, intent_pattern, min_confidence,
    )
    competing = [c.manifest.agent_id for c in candidates[1:]]

    logger.info(
        "discovery.resolve.matched tool=%s agent=%s source=%s competitors=%d",
        tool_name,
        chosen.manifest.agent_id,
        match_source,
        len(competing),
    )

    return chosen.manifest.agent_id, CapabilityResolution(
        resolution_id=resolution_id,
        build_id=build_id,
        stage_label=stage_label,
        requested_tool=tool_name,
        requested_intent=intent_pattern,
        matched_agent_id=chosen.manifest.agent_id,
        match_source=match_source,  # type: ignore[arg-type]
        competing_agents=competing,
        chosen_trust_tier=chosen.manifest.trust_tier,
        chosen_confidence=chosen_confidence,
        chosen_queue_depth=chosen.last_queue_depth,
        resolved_at=resolved_at,
    )


__all__ = ["resolve"]
