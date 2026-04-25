"""Forge agent manifest published to the NATS fleet registry.

This module exports :data:`FORGE_MANIFEST` — the immutable, declarative
:class:`nats_core.manifest.AgentManifest` describing Forge's intents,
tools, trust tier, and required permissions. The manifest is published
on startup to the ``fleet.register`` topic and stored in the
``agent-registry`` NATS KV bucket per
``docs/design/contracts/API-nats-fleet-lifecycle.md §2.1``.

The manifest is a pure module-level constant — no runtime I/O, no
environment reads, no secrets. The contents are copied verbatim from
the API contract specification. Re-registration is idempotent and a
version-bumped manifest supersedes the previous entry.
"""

from __future__ import annotations

from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability

FORGE_MANIFEST: AgentManifest = AgentManifest(
    agent_id="forge",
    name="Forge",
    version="0.1.0",
    template="deepagents-pipeline-orchestrator",
    trust_tier="core",
    status="ready",
    max_concurrent=1,  # ADR-SP-012 — sequential builds
    intents=[
        IntentCapability(
            pattern="build.*",
            signals=["build", "develop", "implement", "create", "make", "ship"],
            confidence=0.90,
            description="Run a feature through the software factory pipeline to PR",
        ),
        IntentCapability(
            pattern="pipeline.*",
            signals=["pipeline", "stages", "progress", "status", "deploy"],
            confidence=0.85,
            description="Operate the build pipeline — queue, inspect, cancel, resume",
        ),
        IntentCapability(
            pattern="feature.*",
            signals=["feature", "add feature", "new capability", "requirement"],
            confidence=0.80,
            description="Add a new feature to an existing project",
        ),
    ],
    tools=[
        ToolCapability(
            name="forge_greenfield",
            description=(
                "Start a full greenfield pipeline run. Returns pipeline_id "
                "immediately (fire-and-forget). Poll forge_status, cancel "
                "with forge_cancel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "feature_yaml_path": {"type": "string"},
                    "branch": {"type": "string", "default": "main"},
                },
                "required": ["repo", "feature_yaml_path"],
            },
            returns="{pipeline_id: str, queued_at: datetime}",
            risk_level="mutating",
            async_mode=True,
            requires_approval=False,
        ),
        ToolCapability(
            name="forge_feature",
            description="Add a feature to an existing project. Returns pipeline_id immediately.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "feature_id": {"type": "string"},
                },
                "required": ["repo", "feature_id"],
            },
            returns="{pipeline_id: str, queued_at: datetime}",
            risk_level="mutating",
            async_mode=True,
        ),
        ToolCapability(
            name="forge_review_fix",
            description="Run a review-and-fix cycle on existing code. Returns pipeline_id immediately.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["repo", "subject"],
            },
            returns="{pipeline_id: str, queued_at: datetime}",
            risk_level="mutating",
            async_mode=True,
        ),
        ToolCapability(
            name="forge_status",
            description="Read current pipeline status (all running/paused builds, or a specific pipeline_id).",
            parameters={
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "string"},
                },
            },
            returns="list[BuildStatus]",
            risk_level="read_only",
        ),
        ToolCapability(
            name="forge_cancel",
            description="Cancel an in-flight pipeline run.",
            parameters={
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["pipeline_id"],
            },
            returns="{cancelled: bool, at: datetime}",
            risk_level="mutating",
        ),
    ],
    required_permissions=[
        "graphiti:read",
        "graphiti:write",
        "filesystem:read",
        "filesystem:write",
        "shell:execute",
        "nats:publish",
        "nats:subscribe",
        "network:github.com",
    ],
)

__all__ = ["FORGE_MANIFEST"]
