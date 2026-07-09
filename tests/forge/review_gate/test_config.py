"""ReviewGateConfig defaults — inert in production (WS3-S5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    ReviewGateConfig,
)


class TestDefaults:
    def test_disabled_by_default(self):
        assert ReviewGateConfig().enabled is False

    def test_default_dimensions_mirror_dd4f(self):
        cfg = ReviewGateConfig()
        assert cfg.dimensions == [
            "spec-fidelity",
            "correctness",
            "wire-topology",
            "assumptions",
            "tracker-consistency",
        ]

    def test_min_refuters_floor_is_two(self):
        assert ReviewGateConfig().min_refuters == 2
        with pytest.raises(ValidationError):
            ReviewGateConfig(min_refuters=1)

    def test_forge_config_review_gate_default_off(self):
        cfg = ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=["/tmp"])
            )
        )
        assert cfg.review_gate.enabled is False

    def test_extra_key_forbidden(self):
        with pytest.raises(ValidationError):
            ReviewGateConfig(bogus=1)
