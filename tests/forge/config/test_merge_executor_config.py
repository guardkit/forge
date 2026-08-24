"""MergeExecutorConfig — the merge word's flag defaults OFF, every angle.

Make-merge-work build spec (2026-08-24) piece 1. The prime invariant is the
ConductorConfig posture applied to the merge word: ``enabled`` defaults to
False and with it off the tree is byte-for-byte today's behaviour. "Defaults
OFF" is a property to test from every shape a config can arrive in — absent
section, absent field, explicit section — plus the deploy-order law's teeth
(``extra="forbid"`` refuses an unknown key loudly at load).
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from forge.config.models import ForgeConfig, MergeExecutorConfig

_MINIMAL = """
permissions:
  filesystem:
    allowlist: ["/tmp"]
"""


def _config(text: str) -> ForgeConfig:
    return ForgeConfig.model_validate(yaml.safe_load(text))


class TestDefaultsOff:
    def test_absent_section_defaults_off(self) -> None:
        cfg = _config(_MINIMAL)
        assert cfg.merge_executor.enabled is False
        assert cfg.merge_executor.response_wait_seconds == 86400

    def test_bare_model_defaults_off(self) -> None:
        cfg = MergeExecutorConfig()
        assert cfg.enabled is False
        assert cfg.response_wait_seconds == 86400

    def test_empty_section_defaults_off(self) -> None:
        cfg = _config(_MINIMAL + "\nmerge_executor: {}\n")
        assert cfg.merge_executor.enabled is False


class TestExplicitLoad:
    def test_enabled_loads(self) -> None:
        cfg = _config(_MINIMAL + "\nmerge_executor:\n  enabled: true\n")
        assert cfg.merge_executor.enabled is True
        # The wait default rides untouched.
        assert cfg.merge_executor.response_wait_seconds == 86400

    def test_response_wait_loads(self) -> None:
        cfg = _config(
            _MINIMAL
            + "\nmerge_executor:\n  enabled: true\n  response_wait_seconds: 60\n"
        )
        assert cfg.merge_executor.response_wait_seconds == 60

    def test_negative_wait_refused(self) -> None:
        with pytest.raises(ValidationError):
            _config(
                _MINIMAL + "\nmerge_executor:\n  response_wait_seconds: -1\n"
            )


class TestDeployOrderLaw:
    """extra="forbid" — an unknown key refuses the WHOLE config at load."""

    def test_unknown_key_in_section_refused(self) -> None:
        with pytest.raises(ValidationError):
            _config(_MINIMAL + "\nmerge_executor:\n  turbo: true\n")

    def test_section_field_shape(self) -> None:
        # The field sits on ForgeConfig beside conductor with a factory
        # default — a minimal config always carries a usable section.
        cfg = _config(_MINIMAL)
        assert isinstance(cfg.merge_executor, MergeExecutorConfig)
