"""Tests for ``forge.config.models.ApprovalConfig`` (TASK-CGCP-002).

Each test class mirrors one acceptance criterion of TASK-CGCP-002 so the
mapping between criterion and verifier stays explicit. The defaults exercised
here are pinned to the Confidence-Gated Checkpoint Protocol assumptions
manifest (FEAT-FORGE-004 ASSUM-001 / ASSUM-002).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forge.config import ApprovalConfig, ForgeConfig
from forge.config.models import (
    DEFAULT_APPROVAL_MAX_WAIT_SECONDS,
    DEFAULT_APPROVAL_WAIT_SECONDS,
)


# ---------------------------------------------------------------------------
# AC-001 / AC-002 / AC-003: model exists with the two non-negative integer
# fields and the documented defaults.
# ---------------------------------------------------------------------------


class TestApprovalConfigShape:
    """AC-001..003: the model has the right field names and defaults."""

    def test_model_defines_default_wait_seconds(self) -> None:
        assert "default_wait_seconds" in ApprovalConfig.model_fields

    def test_model_defines_max_wait_seconds(self) -> None:
        assert "max_wait_seconds" in ApprovalConfig.model_fields

    def test_default_wait_seconds_default_is_300(self) -> None:
        # AC-002 / ASSUM-001
        assert ApprovalConfig().default_wait_seconds == 300
        assert DEFAULT_APPROVAL_WAIT_SECONDS == 300

    def test_max_wait_seconds_default_is_3600(self) -> None:
        # AC-003 / ASSUM-002
        assert ApprovalConfig().max_wait_seconds == 3600
        assert DEFAULT_APPROVAL_MAX_WAIT_SECONDS == 3600

    def test_default_wait_seconds_is_int_typed(self) -> None:
        annotation = ApprovalConfig.model_fields["default_wait_seconds"].annotation
        assert annotation is int

    def test_max_wait_seconds_is_int_typed(self) -> None:
        annotation = ApprovalConfig.model_fields["max_wait_seconds"].annotation
        assert annotation is int


# ---------------------------------------------------------------------------
# AC-004: validators reject negative values
# ---------------------------------------------------------------------------


class TestNegativeValuesRejected:
    """AC-004: a negative value on either field raises ValidationError."""

    def test_negative_default_wait_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalConfig(default_wait_seconds=-1, max_wait_seconds=10)

    def test_negative_max_wait_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalConfig(default_wait_seconds=0, max_wait_seconds=-1)

    def test_zero_default_wait_accepted(self) -> None:
        # ge=0, not gt=0 — zero is a meaningful "publish-and-immediately-
        # refresh" configuration and must remain accepted.
        cfg = ApprovalConfig(default_wait_seconds=0, max_wait_seconds=0)
        assert cfg.default_wait_seconds == 0
        assert cfg.max_wait_seconds == 0


# ---------------------------------------------------------------------------
# AC-005: validator rejects default_wait_seconds > max_wait_seconds
# ---------------------------------------------------------------------------


class TestDefaultNotAboveMax:
    """AC-005: cross-field invariant on the wait-time settings."""

    def test_default_above_max_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ApprovalConfig(default_wait_seconds=600, max_wait_seconds=300)
        msg = str(exc_info.value)
        assert "default_wait_seconds" in msg
        assert "max_wait_seconds" in msg

    def test_default_equal_to_max_accepted(self) -> None:
        cfg = ApprovalConfig(default_wait_seconds=300, max_wait_seconds=300)
        assert cfg.default_wait_seconds == cfg.max_wait_seconds == 300

    def test_canonical_defaults_are_consistent(self) -> None:
        # Sanity: the manifest defaults must satisfy the cross-field invariant
        # by construction (300 <= 3600).
        cfg = ApprovalConfig()
        assert cfg.default_wait_seconds <= cfg.max_wait_seconds


# ---------------------------------------------------------------------------
# AC-006: ForgeConfig.approval is wired with default_factory=ApprovalConfig
# ---------------------------------------------------------------------------


class TestForgeConfigWiresApproval:
    """AC-006: ApprovalConfig hangs off ForgeConfig with a default factory."""

    def test_forge_config_has_approval_field(self) -> None:
        assert "approval" in ForgeConfig.model_fields

    def test_minimal_yaml_gets_default_approval(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert isinstance(cfg.approval, ApprovalConfig)
        assert cfg.approval.default_wait_seconds == 300
        assert cfg.approval.max_wait_seconds == 3600

    def test_default_factory_isolates_instances(self) -> None:
        # default_factory must hand out a fresh ApprovalConfig per instance
        a = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        b = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert a.approval is not b.approval


# ---------------------------------------------------------------------------
# AC-007: forge.yaml round-trips through ForgeConfig.model_validate(...)
# with the new section.
# ---------------------------------------------------------------------------


class TestApprovalYamlRoundTrip:
    """AC-007: YAML → model → dict preserves the approval section."""

    def test_roundtrip_preserves_approval_section(self, tmp_path: Path) -> None:
        original = {
            "approval": {
                "default_wait_seconds": 120,
                "max_wait_seconds": 1800,
            },
            "permissions": {
                "filesystem": {
                    "allowlist": ["/srv/forge"],
                }
            },
        }

        yaml_path = tmp_path / "forge.yaml"
        yaml_path.write_text(yaml.safe_dump(original))

        loaded = yaml.safe_load(yaml_path.read_text())
        cfg = ForgeConfig.model_validate(loaded)
        roundtripped = cfg.model_dump(mode="json", include={"approval", "permissions"})

        assert roundtripped == original

    def test_full_roundtrip_with_all_sections(self, tmp_path: Path) -> None:
        original = {
            "fleet": {
                "heartbeat_interval_seconds": 45,
                "stale_heartbeat_seconds": 120,
                "cache_ttl_seconds": 15,
                "intent_min_confidence": 0.85,
            },
            "pipeline": {
                "progress_interval_seconds": 90,
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal", "slack"],
            },
            "approval": {
                "default_wait_seconds": 300,
                "max_wait_seconds": 3600,
            },
            "permissions": {
                "filesystem": {
                    "allowlist": ["/srv/forge", "/var/data"],
                }
            },
        }

        yaml_path = tmp_path / "forge.yaml"
        yaml_path.write_text(yaml.safe_dump(original))

        loaded = yaml.safe_load(yaml_path.read_text())
        cfg = ForgeConfig.model_validate(loaded)
        roundtripped = cfg.model_dump(mode="json")

        assert roundtripped == original


# ---------------------------------------------------------------------------
# AC-008: inline comment documents ASSUM-003 (ceiling fallback semantics)
# is deferred to forge-pipeline-config.
# ---------------------------------------------------------------------------


class TestAssum003DeferralDocumented:
    """AC-008: the source of ApprovalConfig must say so explicitly."""

    def test_docstring_mentions_assum_003_deferral(self) -> None:
        doc = ApprovalConfig.__doc__ or ""
        assert "ASSUM-003" in doc
        assert "forge-pipeline-config" in doc


# ---------------------------------------------------------------------------
# AC-009: module imports nothing from nats_core, nats-py, or langgraph.
# ---------------------------------------------------------------------------


class TestNoForbiddenImports:
    """AC-009: forge.config.models is a pure declarative schema layer."""

    def test_module_source_does_not_import_forbidden_packages(self) -> None:
        from forge.config import models

        source = Path(models.__file__).read_text()
        for forbidden in ("nats_core", "nats_py", "nats-py", "langgraph"):
            assert f"import {forbidden}" not in source, (
                f"forge.config.models must not import {forbidden}"
            )
            assert f"from {forbidden}" not in source, (
                f"forge.config.models must not import from {forbidden}"
            )
