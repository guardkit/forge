"""Tests for Mode P planning configuration and audit (TASK-MP-001).

Covers:
- PlanningConfig Pydantic validation
- ForgeConfig.planning integration
- ApprovalConfig immutability verification
- audit_planning_model_resolution pure function

All tests are offline unit tests (no I/O, no external services).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.config.models import ApprovalConfig, ForgeConfig, PlanningConfig
from forge.planning.audit import (
    PlanningAuditResult,
    audit_planning_model_resolution,
)


class TestPlanningConfigValidation:
    """Test PlanningConfig Pydantic validation rules."""

    def test_extra_fields_rejected(self):
        """Extra keys are rejected (extra='forbid')."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PlanningConfig(unknown_field="value")

    def test_negative_originator_wait_rejected(self):
        """originator_wait_seconds must be non-negative."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            PlanningConfig(originator_wait_seconds=-1)

    def test_negative_escalated_wait_rejected(self):
        """escalated_wait_seconds must be non-negative."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            PlanningConfig(escalated_wait_seconds=-1)

    def test_defer_cap_minimum(self):
        """defer_cap must be >= 1."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            PlanningConfig(defer_cap=0)

    def test_invalid_target_repo_format_rejected(self):
        """default_target_repo must match org/name pattern."""
        with pytest.raises(ValidationError, match="must match pattern"):
            PlanningConfig(default_target_repo="invalid")

        with pytest.raises(ValidationError, match="must match pattern"):
            PlanningConfig(default_target_repo="owner/")

    def test_valid_target_repo_formats(self):
        """Valid org/name formats are accepted."""
        valid_formats = [
            "owner/repo",
            "Org123/Repo_Name",
            "org.name/repo-name",
            "org_name/repo.name",
        ]
        for repo in valid_formats:
            config = PlanningConfig(default_target_repo=repo)
            assert config.default_target_repo == repo

    def test_defaults(self):
        """Verify default values per acceptance criteria."""
        config = PlanningConfig()
        assert config.enabled is False
        assert config.frontier_enabled is False
        assert config.defer_cap == 3
        assert config.escalation_approver is None
        assert config.default_target_repo is None
        assert config.target_repo_paths == {}
        assert config.terminal == "planned-handoff"
        assert config.model_resolution.model is None
        assert config.model_resolution.fallbacks == []


class TestForgeConfigPlanningIntegration:
    """Test ForgeConfig.planning field integration."""

    def test_planning_field_has_default_factory(self):
        """ForgeConfig.planning uses default_factory per house shape."""
        # Create minimal ForgeConfig with only required permissions field
        config = ForgeConfig(
            permissions={
                "filesystem": {"allowlist": ["/tmp/test"]},
            }
        )
        assert config.planning is not None
        assert isinstance(config.planning, PlanningConfig)
        assert config.planning.enabled is False

    def test_planning_field_can_be_overridden(self):
        """ForgeConfig accepts explicit planning configuration."""
        config = ForgeConfig(
            planning=PlanningConfig(
                enabled=True,
                defer_cap=5,
                default_target_repo="org/repo",
            ),
            permissions={
                "filesystem": {"allowlist": ["/tmp/test"]},
            },
        )
        assert config.planning.enabled is True
        assert config.planning.defer_cap == 5
        assert config.planning.default_target_repo == "org/repo"


class TestApprovalConfigImmutability:
    """Verify ApprovalConfig is byte-identical to main (no new fields)."""

    def test_approval_config_field_set_unchanged(self):
        """ApprovalConfig has exactly the expected fields, no additions."""
        expected_fields = {
            "default_wait_seconds",
            "max_wait_seconds",
            "expected_approver",
        }

        # Get actual fields from the model
        actual_fields = set(ApprovalConfig.model_fields.keys())

        assert actual_fields == expected_fields, (
            f"ApprovalConfig field set changed. "
            f"Expected: {expected_fields}, Got: {actual_fields}"
        )


class TestPlanningModelResolutionAudit:
    """Test audit_planning_model_resolution pure function."""

    def test_empty_fallbacks_pass(self):
        """Empty fallbacks list passes audit (DF-004 compliant)."""
        config = PlanningConfig()  # Default has empty fallbacks
        result = audit_planning_model_resolution(config)

        assert result.passed is True
        assert result.violation is None
        assert "no fallbacks" in result.reason.lower()

    def test_nonempty_fallbacks_fail(self):
        """Non-empty fallbacks violate DF-004 (cloud escalation forbidden)."""
        config = PlanningConfig()
        config.model_resolution.fallbacks = ["claude-opus-4.6"]

        result = audit_planning_model_resolution(config)

        assert result.passed is False
        assert result.violation == "DF-004"
        assert "fallbacks" in result.reason.lower()
        assert "DF-004" in result.reason

    def test_multiple_fallbacks_fail(self):
        """Multiple fallbacks also violate DF-004."""
        config = PlanningConfig()
        config.model_resolution.fallbacks = ["model1", "model2", "model3"]

        result = audit_planning_model_resolution(config)

        assert result.passed is False
        assert result.violation == "DF-004"

    def test_audit_is_pure_function(self):
        """Audit function is pure (no I/O, no raising, deterministic)."""
        config = PlanningConfig()

        # Call multiple times with same input
        result1 = audit_planning_model_resolution(config)
        result2 = audit_planning_model_resolution(config)

        # Results should be identical (deterministic)
        assert result1.passed == result2.passed
        assert result1.violation == result2.violation
        assert result1.reason == result2.reason

    def test_audit_never_raises(self):
        """Audit function never raises (soft-fail posture, ASSUM-011)."""
        # Even with invalid config, should return result, not raise
        config = PlanningConfig()

        # This should not raise, regardless of state
        result = audit_planning_model_resolution(config)

        assert isinstance(result, PlanningAuditResult)
        assert isinstance(result.passed, bool)
        assert isinstance(result.reason, str)

    def test_disabled_mode_p_still_audits(self):
        """Audit runs even when planning.enabled=False."""
        config = PlanningConfig(enabled=False)
        config.model_resolution.fallbacks = ["some-model"]

        result = audit_planning_model_resolution(config)

        # Audit should still fail for fallbacks, regardless of enabled state
        assert result.passed is False
        assert result.violation == "DF-004"
