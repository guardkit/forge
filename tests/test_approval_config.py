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
                "expected_approver": "rich",
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
                "expected_approver": "rich",
            },
            # ``queue`` was added by TASK-PSM-003 — the round-trip dump now
            # always includes it, so this "all sections" test must declare
            # the queue block explicitly to keep round-trip equality holding.
            "queue": {
                "default_max_turns": 5,
                "default_sdk_timeout_seconds": 1800,
                "default_history_limit": 50,
                "repo_allowlist": [],
            },
            # ``budget`` was added by FEAT-UBS-002 — like ``queue`` above, the
            # unfiltered round-trip dump now always includes it, so this "all
            # sections" test must declare it with default values.
            "budget": {
                "default_profile": "attended",
                "profiles": {
                    "attended": {
                        "max_review_cycles": None,
                        "max_build_wallclock_seconds": None,
                        "max_build_tokens": None,
                        "min_coach_score": None,
                        # The ``leg_*`` group (leg budgets become yaml knobs)
                        # is optional and defaults to None, so the dump now
                        # carries it too.
                        "leg_max_turns": None,
                        "leg_sdk_timeout_seconds": None,
                        "leg_budget_seconds": None,
                    },
                    "unattended": {
                        "max_review_cycles": 2,
                        "max_build_wallclock_seconds": 5400,
                        "max_build_tokens": None,
                        "min_coach_score": None,
                        "leg_max_turns": None,
                        "leg_sdk_timeout_seconds": None,
                        "leg_budget_seconds": None,
                    },
                },
            },
            # ``planning`` was added by FEAT-SPL-002 (Mode P) — like ``queue``
            # and ``budget`` above, the unfiltered round-trip dump now always
            # includes it, so this test must declare it with default values.
            "planning": {
                "enabled": False,
                "escalation_approver": None,
                "originator_wait_seconds": 300,
                "escalated_wait_seconds": 1800,
                "defer_cap": 3,
                "default_target_repo": None,
                "target_repo_paths": {},
                "terminal": "planned-handoff",
                "frontier_enabled": False,
                "frontier_timeout_seconds": 30,
                "model_resolution": {"model": None, "fallbacks": []},
                # Lane B / Phase E1 (B1) target-terminal flag — default-OFF; the
                # unfiltered round-trip dump always includes it.
                "target_terminal": {"enabled": False},
                # ``planning.digest_review`` — the machine chain's one pause.
                # Default always_ask=True; the unfiltered dump always includes it.
                "digest_review": {"always_ask": True, "skip_max_scenarios": 3},
            },
            # ``deploy`` was added by WS2-B8 (output-side stages); the unfiltered
            # round-trip dump always includes it with default values.
            "deploy": {
                "enabled": False,
                "run_live_gate": True,
                "reservation_backend": "none",
                "deploy_record_dir": "docs/state",
                # ``deploy.execution_surface`` — where the deploy stage runs.
                # Defaults to ``local``; always present in the dump.
                "execution_surface": "local",
                "sidecar_url": "http://127.0.0.1:8125",
            },
            # ``review_gate`` was added by WS3-S5 (adversarial merge gate); the
            # unfiltered round-trip dump always includes it, so declare defaults.
            "review_gate": {
                "enabled": False,
                "dimensions": [
                    "spec-fidelity",
                    "correctness",
                    "wire-topology",
                    "assumptions",
                    "tracker-consistency",
                ],
                "min_refuters": 2,
                "record_dir": "qa",
            },
            # ``resource_preflight`` was added by E2-S4 (run-entry memory /
            # disk headroom preflight); the unfiltered dump always includes it.
            "resource_preflight": {
                "enabled": True,
                "min_available_memory_gb": 8.0,
                "min_available_disk_gb": 20.0,
                "working_path": None,
            },
            # ``conductor`` — the fix journey's activation flag (the conductor
            # revival) plus its SEAT (the local model every fix-journey leg
            # runs on; conductor-activation design pass §2). Default-OFF with
            # no seat: the flag is what reserves activation, OFF is
            # byte-for-byte the routine path, and a DISABLED conductor needs
            # no seat (``enabled: true`` with no seat refuses at load). It is
            # a first-class config section, so the unfiltered round-trip dump
            # always includes both keys and this exhaustive fixture must
            # declare them.
            "conductor": {"enabled": False, "seat": None},
            # ``merge_executor`` — the merge word's activation switch
            # (make-merge-work build spec 2026-08-24). Default-OFF: no merge
            # card is offered and no approval response is consumed. A
            # first-class config section, so the unfiltered round-trip dump
            # always includes it and this exhaustive fixture must declare it.
            "merge_executor": {"enabled": False, "response_wait_seconds": 86400},
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
# TASK-JNB-101: expected_approver — the APPROVER_IDENTITY contract
# (config-alignment AC).
# ---------------------------------------------------------------------------


class TestExpectedApprover:
    """TASK-JNB-101 config-alignment AC.

    ``ApprovalConfig.expected_approver`` is the forge half of the
    APPROVER_IDENTITY contract: its value must string-equal (verbatim —
    no trimming, no case folding) the jarvis ``JARVIS_SLACK_DECIDED_BY``
    setting, or every phone approval is silently refused. The pinned
    shared value is ``"rich"`` (operator-chosen 2026-07-04); jarvis's
    ``.env.example`` documents the same pin on its side.
    """

    def test_model_defines_expected_approver(self) -> None:
        assert "expected_approver" in ApprovalConfig.model_fields

    def test_default_is_pinned_to_rich(self) -> None:
        # THE config-alignment assertion: the forge default and the
        # jarvis JARVIS_SLACK_DECIDED_BY value must both be exactly
        # "rich". Changing this default without changing the jarvis
        # side breaks every phone approval SILENTLY (WARNING-level
        # forge log only) — see TASK-JNB-107's troubleshooting notes.
        assert ApprovalConfig().expected_approver == "rich"

    def test_pin_is_exact_verbatim_string(self) -> None:
        # Guard the failure modes the wire contract cannot surface:
        # whitespace or case drift would silently refuse approvals
        # because forge compares with ``!=`` on the raw string.
        value = ApprovalConfig().expected_approver
        assert value is not None
        assert value == value.strip()
        assert value == value.lower()

    def test_yaml_override_is_respected(self) -> None:
        cfg = ApprovalConfig.model_validate({"expected_approver": "someone"})
        assert cfg.expected_approver == "someone"

    def test_explicit_none_enables_permissive_mode(self) -> None:
        # None = permissive dev mode (any decided_by accepted) — must
        # remain expressible for single-deployment dev loops.
        cfg = ApprovalConfig.model_validate({"expected_approver": None})
        assert cfg.expected_approver is None

    def test_forge_config_default_carries_the_pin(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert cfg.approval.expected_approver == "rich"


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
