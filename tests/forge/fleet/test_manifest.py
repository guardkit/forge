"""Unit tests for the FORGE_MANIFEST constant.

Validates the manifest matches docs/design/contracts/API-nats-fleet-lifecycle.md §2.1
verbatim and is free of any secret material.
"""

from __future__ import annotations

import re

import pytest
from nats_core.manifest import AgentManifest, IntentCapability, ToolCapability


class TestForgeManifestExport:
    """AC-001 / AC-008 — module-level constant and import path resolution."""

    def test_module_exports_constant(self) -> None:
        """AC-001 — ``src/forge/fleet/manifest.py`` exposes ``FORGE_MANIFEST``."""
        from forge.fleet import manifest as manifest_module

        assert hasattr(manifest_module, "FORGE_MANIFEST")

    def test_import_path_resolves(self) -> None:
        """AC-008 — ``from forge.fleet.manifest import FORGE_MANIFEST`` resolves."""
        from forge.fleet.manifest import FORGE_MANIFEST  # noqa: F401

    def test_package_reexport(self) -> None:
        """AC-008 supplemental — package-level re-export resolves too."""
        from forge.fleet import FORGE_MANIFEST  # noqa: F401


class TestForgeManifestType:
    """AC-002 — typed as ``nats_core.manifest.AgentManifest``."""

    def test_is_agent_manifest_instance(self) -> None:
        """AC-002 — ``FORGE_MANIFEST`` is an instance of ``AgentManifest``."""
        from forge.fleet.manifest import FORGE_MANIFEST

        assert isinstance(FORGE_MANIFEST, AgentManifest)

    def test_imported_not_redeclared(self) -> None:
        """AC-002 — the type lives in ``nats_core.manifest``, not redeclared in forge."""
        from forge.fleet.manifest import FORGE_MANIFEST

        # The class object must be the same one exported by nats_core.
        assert type(FORGE_MANIFEST) is AgentManifest


class TestForgeManifestIdentity:
    """AC-003 — agent_id, trust_tier, max_concurrent."""

    def test_agent_id(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert FORGE_MANIFEST.agent_id == "forge"

    def test_trust_tier(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert FORGE_MANIFEST.trust_tier == "core"

    def test_max_concurrent(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert FORGE_MANIFEST.max_concurrent == 1

    def test_name_and_template_and_version(self) -> None:
        """AC-003 supplemental — anchor name/template/version too."""
        from forge.fleet.manifest import FORGE_MANIFEST

        assert FORGE_MANIFEST.name == "Forge"
        assert FORGE_MANIFEST.template == "deepagents-pipeline-orchestrator"
        assert FORGE_MANIFEST.version == "0.1.0"
        assert FORGE_MANIFEST.status == "ready"


class TestForgeManifestIntents:
    """AC-004 — three IntentCapability entries verbatim."""

    def test_three_intents(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert len(FORGE_MANIFEST.intents) == 3
        for intent in FORGE_MANIFEST.intents:
            assert isinstance(intent, IntentCapability)

    def test_build_intent(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        build = next(i for i in FORGE_MANIFEST.intents if i.pattern == "build.*")
        assert build.signals == ["build", "develop", "implement", "create", "make", "ship"]
        assert build.confidence == pytest.approx(0.90)
        assert build.description == "Run a feature through the software factory pipeline to PR"

    def test_pipeline_intent(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        pipeline = next(i for i in FORGE_MANIFEST.intents if i.pattern == "pipeline.*")
        assert pipeline.signals == ["pipeline", "stages", "progress", "status", "deploy"]
        assert pipeline.confidence == pytest.approx(0.85)
        assert (
            pipeline.description
            == "Operate the build pipeline — queue, inspect, cancel, resume"
        )

    def test_feature_intent(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        feature = next(i for i in FORGE_MANIFEST.intents if i.pattern == "feature.*")
        assert feature.signals == ["feature", "add feature", "new capability", "requirement"]
        assert feature.confidence == pytest.approx(0.80)
        assert feature.description == "Add a new feature to an existing project"


class TestForgeManifestTools:
    """AC-005 — five ToolCapability entries verbatim."""

    def test_five_tools(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert len(FORGE_MANIFEST.tools) == 5
        for tool in FORGE_MANIFEST.tools:
            assert isinstance(tool, ToolCapability)

    def test_tool_names(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert {t.name for t in FORGE_MANIFEST.tools} == {
            "forge_greenfield",
            "forge_feature",
            "forge_review_fix",
            "forge_status",
            "forge_cancel",
        }

    def test_forge_greenfield(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        tool = next(t for t in FORGE_MANIFEST.tools if t.name == "forge_greenfield")
        assert tool.risk_level == "mutating"
        assert tool.async_mode is True
        assert tool.requires_approval is False
        assert tool.returns == "{pipeline_id: str, queued_at: datetime}"
        assert tool.parameters == {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "feature_yaml_path": {"type": "string"},
                "branch": {"type": "string", "default": "main"},
            },
            "required": ["repo", "feature_yaml_path"],
        }

    def test_forge_feature(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        tool = next(t for t in FORGE_MANIFEST.tools if t.name == "forge_feature")
        assert tool.risk_level == "mutating"
        assert tool.async_mode is True
        assert tool.returns == "{pipeline_id: str, queued_at: datetime}"
        assert tool.parameters == {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "feature_id": {"type": "string"},
            },
            "required": ["repo", "feature_id"],
        }
        assert (
            tool.description
            == "Add a feature to an existing project. Returns pipeline_id immediately."
        )

    def test_forge_review_fix(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        tool = next(t for t in FORGE_MANIFEST.tools if t.name == "forge_review_fix")
        assert tool.risk_level == "mutating"
        assert tool.async_mode is True
        assert tool.returns == "{pipeline_id: str, queued_at: datetime}"
        assert tool.parameters == {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["repo", "subject"],
        }
        assert (
            tool.description
            == "Run a review-and-fix cycle on existing code. Returns pipeline_id immediately."
        )

    def test_forge_status(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        tool = next(t for t in FORGE_MANIFEST.tools if t.name == "forge_status")
        assert tool.risk_level == "read_only"
        assert tool.returns == "list[BuildStatus]"
        assert tool.parameters == {
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "string"},
            },
        }
        assert tool.description == (
            "Read current pipeline status (all running/paused builds, "
            "or a specific pipeline_id)."
        )

    def test_forge_cancel(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        tool = next(t for t in FORGE_MANIFEST.tools if t.name == "forge_cancel")
        assert tool.risk_level == "mutating"
        assert tool.returns == "{cancelled: bool, at: datetime}"
        assert tool.parameters == {
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["pipeline_id"],
        }
        assert tool.description == "Cancel an in-flight pipeline run."


class TestForgeManifestPermissions:
    """AC-006 — required_permissions verbatim."""

    def test_required_permissions(self) -> None:
        from forge.fleet.manifest import FORGE_MANIFEST

        assert FORGE_MANIFEST.required_permissions == [
            "graphiti:read",
            "graphiti:write",
            "filesystem:read",
            "filesystem:write",
            "shell:execute",
            "nats:publish",
            "nats:subscribe",
            "network:github.com",
        ]


class TestForgeManifestSecretFree:
    """AC-007 — manifest serialisation contains no secret-shaped material."""

    @pytest.mark.parametrize(
        "needle",
        ["api_key", "token", "password", "secret", "credential"],
    )
    def test_no_secret_substrings(self, needle: str) -> None:
        """AC-007 — case-insensitive scan of ``model_dump_json()`` output."""
        from forge.fleet.manifest import FORGE_MANIFEST

        dumped = FORGE_MANIFEST.model_dump_json()
        # Case-insensitive search via regex.
        assert re.search(needle, dumped, flags=re.IGNORECASE) is None, (
            f"FORGE_MANIFEST.model_dump_json() unexpectedly contains '{needle}'"
        )
