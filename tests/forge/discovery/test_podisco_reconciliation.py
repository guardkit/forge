"""M-08 / PODISCO reconciliation — the PO dispatch resolves non-degraded.

Binding amendment of Lane B / Phase E1 B2 (post-factory-2-three-lanes-handoff
§3 B2 + factory-close-out §3 E1): reconcile
``TASK-FWD-PLAN-PODISCO`` either by FIXING the discovery verb mismatch or by
EVIDENCING it stale-and-struck — no silent carry.

The fix (option (a) in the task file) is LIVE IN CODE: the specialist dispatcher
passes ``SPECIALIST_INTENT_BY_STAGE[stage]`` as ``intent_pattern`` to
``resolve()`` (``forge.pipeline.dispatchers.specialist.dispatch_specialist_stage``),
so forge's own exact-tool → intent-fallback algorithm reaches the live
``product-owner-agent`` via its advertised ``product.*`` intent even though no
agent advertises a *tool* named ``product_owner_specialist``.

This test reconstructs the live manifest shape recorded in the PODISCO task file
(tools ``po_idea`` / ``po_greenfield`` / … plus ``IntentCapability(pattern=
"product.*", confidence=0.95)``) and proves the mismatch NO LONGER FIRES: the PO
dispatch resolves to the agent (``match_source == "intent_pattern"``), NOT the
``unresolved`` / ``no_specialist_resolvable`` degrade the task reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability

from forge.discovery import DiscoveryCacheEntry, resolve
from forge.pipeline.dispatchers.specialist import (
    SPECIALIST_CAPABILITY_BY_STAGE,
    SPECIALIST_INTENT_BY_STAGE,
)
from forge.pipeline.stage_taxonomy import StageClass

_TS = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _po_tool(name: str) -> ToolCapability:
    return ToolCapability(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}},
        returns="dict",
        risk_level="read_only",
    )


def _live_product_owner_entry() -> DiscoveryCacheEntry:
    """The live ``product-owner-agent`` manifest shape (PODISCO task file)."""
    manifest = AgentManifest(
        agent_id="product-owner-agent",
        name="Product Owner Agent",
        version="0.1.0",
        template="test-template",
        trust_tier="specialist",
        status="ready",
        max_concurrent=1,
        # No tool named ``product_owner_specialist`` — exactly the mismatch.
        tools=[
            _po_tool("po_idea"),
            _po_tool("po_extract"),
            _po_tool("po_greenfield"),
            _po_tool("po_evolve"),
            _po_tool("po_impact"),
            _po_tool("po_scope"),
        ],
        intents=[
            IntentCapability(
                pattern="product.*",
                signals=["product"],
                confidence=0.95,
                description="product-owner intents",
            )
        ],
        required_permissions=[],
    )
    return DiscoveryCacheEntry(
        manifest=manifest,
        last_heartbeat_at=_TS,
        last_heartbeat_status="ready",
        last_queue_depth=0,
        last_active_tasks=0,
        cached_at=_TS,
    )


def test_po_capability_name_still_matches_no_advertised_tool() -> None:
    """Guard: the mismatch's premise still holds (no exact tool)."""
    entry = _live_product_owner_entry()
    tool_name = SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER]
    assert tool_name == "product_owner_specialist"
    assert not any(t.name == tool_name for t in entry.manifest.tools)


def test_product_owner_dispatch_resolves_via_intent_not_degraded() -> None:
    """The exact-tool → intent-fallback resolves the PO agent (M-08 fixed)."""
    snapshot = {"product-owner-agent": _live_product_owner_entry()}
    tool_name = SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER]
    intent_pattern = SPECIALIST_INTENT_BY_STAGE[StageClass.PRODUCT_OWNER]

    matched_agent, resolution = resolve(
        snapshot,
        tool_name,
        intent_pattern=intent_pattern,
        build_id="plan-corr-podisco",
        stage_label=StageClass.PRODUCT_OWNER.value,
    )

    # The reported failure was ``resolve.unresolved`` → ``no_specialist_resolvable``.
    # With the intent threaded, resolution succeeds via the intent fallback.
    assert matched_agent == "product-owner-agent"
    assert resolution.match_source == "intent_pattern"


def test_intent_fallback_only_needed_because_exact_tool_misses() -> None:
    """Without the intent, the exact-tool miss still degrades (the old path)."""
    snapshot = {"product-owner-agent": _live_product_owner_entry()}
    tool_name = SPECIALIST_CAPABILITY_BY_STAGE[StageClass.PRODUCT_OWNER]

    matched_agent, resolution = resolve(snapshot, tool_name, intent_pattern=None)

    # This is the exact regression the task recorded: tool_exact + intent=None
    # → unresolved. The specialist dispatcher NEVER calls it this way (it always
    # threads SPECIALIST_INTENT_BY_STAGE) — this test pins WHY the intent is
    # load-bearing, so a future refactor that drops it fails loudly here.
    assert matched_agent is None
    assert resolution.match_source == "unresolved"
