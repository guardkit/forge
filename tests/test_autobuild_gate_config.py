"""Tests for ``forge.config.models.AutobuildGateConfig`` (2026-08-26).

The build gate's human-approval wait: 0 (the default) = wait indefinitely
for an answer, like the spec digest pause; a positive value restores a hard
ceiling. This surface is deliberately separate from ``ApprovalConfig`` —
that model keeps the wire protocol's per-window and refresh numbers used by
the planning doors and the conductor's wait windows.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.config.models import (
    DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS,
    AutobuildGateConfig,
    ForgeConfig,
)


class TestAutobuildGateConfigShape:
    def test_default_is_zero_wait_forever(self) -> None:
        assert DEFAULT_AUTOBUILD_GATE_APPROVAL_MAX_WAIT_SECONDS == 0
        assert AutobuildGateConfig().approval_max_wait_seconds == 0

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AutobuildGateConfig(approval_max_wait_seconds=-1)

    def test_positive_value_accepted(self) -> None:
        cfg = AutobuildGateConfig(approval_max_wait_seconds=3600)
        assert cfg.approval_max_wait_seconds == 3600

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AutobuildGateConfig(no_such_key=1)


class TestForgeConfigCarriesAutobuildGate:
    def test_minimal_config_defaults_to_wait_forever(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert cfg.autobuild_gate.approval_max_wait_seconds == 0

    def test_yaml_section_parses(self) -> None:
        cfg = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
                "autobuild_gate": {"approval_max_wait_seconds": 1800},
            }
        )
        assert cfg.autobuild_gate.approval_max_wait_seconds == 1800
