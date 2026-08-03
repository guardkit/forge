"""Tests for ``forge.config.models``.

Each test class mirrors one acceptance criterion of TASK-NFI-001 so the
mapping between criterion and verifier stays explicit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forge.config import (
    FilesystemPermissions,
    FleetConfig,
    ForgeConfig,
    PermissionsConfig,
    PipelineConfig,
)
from forge.config.models import (
    DEFAULT_APPROVED_ORIGINATORS,
    DEFAULT_BUILD_QUEUE_SUBJECT,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_INTENT_MIN_CONFIDENCE,
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    DEFAULT_STALE_HEARTBEAT_SECONDS,
)


# ---------------------------------------------------------------------------
# AC-001: required model classes exist with expected shape
# ---------------------------------------------------------------------------


class TestModelsExist:
    """AC-001: the four new models live in forge.config.models."""

    def test_fleet_config_is_a_model(self) -> None:
        assert issubclass(FleetConfig, object)
        assert "heartbeat_interval_seconds" in FleetConfig.model_fields
        assert "stale_heartbeat_seconds" in FleetConfig.model_fields
        assert "cache_ttl_seconds" in FleetConfig.model_fields
        assert "intent_min_confidence" in FleetConfig.model_fields

    def test_pipeline_config_is_a_model(self) -> None:
        assert "progress_interval_seconds" in PipelineConfig.model_fields
        assert "build_queue_subject" in PipelineConfig.model_fields
        assert "approved_originators" in PipelineConfig.model_fields

    def test_filesystem_permissions_has_allowlist(self) -> None:
        assert "allowlist" in FilesystemPermissions.model_fields

    def test_permissions_config_has_filesystem(self) -> None:
        assert "filesystem" in PermissionsConfig.model_fields


# ---------------------------------------------------------------------------
# AC-002: defaults match ASSUM-001..005 exactly
# ---------------------------------------------------------------------------


class TestDefaultsMatchAssumptions:
    """AC-002: the numeric defaults are pinned to ASSUM-001..005."""

    def test_assum_001_heartbeat_interval(self) -> None:
        assert FleetConfig().heartbeat_interval_seconds == 30
        assert DEFAULT_HEARTBEAT_INTERVAL_SECONDS == 30

    def test_assum_002_stale_heartbeat(self) -> None:
        assert FleetConfig().stale_heartbeat_seconds == 90
        assert DEFAULT_STALE_HEARTBEAT_SECONDS == 90

    def test_assum_003_cache_ttl(self) -> None:
        assert FleetConfig().cache_ttl_seconds == 30
        assert DEFAULT_CACHE_TTL_SECONDS == 30

    def test_assum_004_intent_min_confidence(self) -> None:
        assert FleetConfig().intent_min_confidence == 0.7
        assert DEFAULT_INTENT_MIN_CONFIDENCE == 0.7

    def test_assum_005_progress_interval(self) -> None:
        assert PipelineConfig().progress_interval_seconds == 60
        assert DEFAULT_PROGRESS_INTERVAL_SECONDS == 60

    def test_pipeline_subject_default(self) -> None:
        assert PipelineConfig().build_queue_subject == DEFAULT_BUILD_QUEUE_SUBJECT
        assert PipelineConfig().build_queue_subject == "pipeline.build-queued.>"

    def test_pipeline_originators_default(self) -> None:
        expected = list(DEFAULT_APPROVED_ORIGINATORS)
        assert PipelineConfig().approved_originators == expected
        # default_factory must hand out a fresh list to each instance
        assert (
            PipelineConfig().approved_originators
            is not PipelineConfig().approved_originators
        )


# ---------------------------------------------------------------------------
# AC-003: ForgeConfig.fleet and ForgeConfig.pipeline are optional
# ---------------------------------------------------------------------------


class TestForgeConfigOptionalSections:
    """AC-003: fleet/pipeline have working defaults when omitted."""

    def test_minimal_yaml_only_needs_permissions(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert isinstance(cfg.fleet, FleetConfig)
        assert isinstance(cfg.pipeline, PipelineConfig)
        assert cfg.fleet.heartbeat_interval_seconds == 30
        assert cfg.pipeline.progress_interval_seconds == 60

    def test_default_factory_isolates_pipeline_originators(self) -> None:
        a = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        b = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        a.pipeline.approved_originators.append("intruder")
        assert "intruder" not in b.pipeline.approved_originators


# ---------------------------------------------------------------------------
# AC-004: permissions.filesystem.allowlist is required
# ---------------------------------------------------------------------------


class TestAllowlistIsRequired:
    """AC-004: there is no default allowlist."""

    def test_allowlist_field_is_required(self) -> None:
        field = FilesystemPermissions.model_fields["allowlist"]
        assert field.is_required()

    def test_filesystem_permissions_rejects_missing_allowlist(self) -> None:
        with pytest.raises(ValidationError):
            FilesystemPermissions.model_validate({})

    def test_permissions_block_required_on_forge_config(self) -> None:
        with pytest.raises(ValidationError):
            ForgeConfig.model_validate({})


# ---------------------------------------------------------------------------
# AC-005: relative paths in allowlist are rejected
# ---------------------------------------------------------------------------


class TestAllowlistRejectsRelativePaths:
    """AC-005: a Pydantic validator refuses non-absolute paths."""

    def test_relative_path_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FilesystemPermissions.model_validate({"allowlist": ["./builds"]})
        msg = str(exc_info.value)
        assert "absolute" in msg
        assert "./builds" in msg

    def test_relative_path_in_mixed_list_raises(self) -> None:
        with pytest.raises(ValidationError):
            FilesystemPermissions.model_validate(
                {"allowlist": ["/srv/forge", "relative/path"]}
            )

    def test_absolute_path_accepted(self) -> None:
        cfg = FilesystemPermissions.model_validate(
            {"allowlist": ["/srv/forge", "/var/builds"]}
        )
        assert cfg.allowlist == [Path("/srv/forge"), Path("/var/builds")]


# ---------------------------------------------------------------------------
# AC-006: round-trip preserves field values
# ---------------------------------------------------------------------------


class TestYamlRoundTrip:
    """AC-006: YAML → model → dict preserves values."""

    def test_roundtrip_preserves_all_fields(self, tmp_path: Path) -> None:
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
            # always includes it, so the canonical input dict must declare it
            # explicitly to keep this test asserting full-fidelity round-trip.
            "queue": {
                "default_max_turns": 5,
                "default_sdk_timeout_seconds": 1800,
                "default_history_limit": 50,
                "repo_allowlist": [],
            },
            # ``budget`` was added by FEAT-UBS-002 — like ``queue`` above, the
            # round-trip dump now always includes it, so the canonical input
            # dict must declare it (with default values) to keep this test
            # asserting full-fidelity round-trip.
            "budget": {
                "default_profile": "attended",
                "profiles": {
                    "attended": {
                        "max_review_cycles": None,
                        "max_build_wallclock_seconds": None,
                        "max_build_tokens": None,
                        "min_coach_score": None,
                    },
                    "unattended": {
                        "max_review_cycles": 2,
                        "max_build_wallclock_seconds": 5400,
                        "max_build_tokens": None,
                        "min_coach_score": None,
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
                "target_terminal": {"enabled": False},
                # ``planning.dcl`` — the machine-authored DCL seat's
                # single-slot flag. Default-ON; always in the dump.
                "dcl": {"author_at_plan_commit": True},
            },
            # ``deploy`` was added by WS2-B8 (output-side stages) — like
            # ``planning`` above, the unfiltered round-trip dump always includes
            # it, so this test declares it with default values.
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
            # ``review_gate`` was added by WS3-S5 (adversarial merge gate) — like
            # ``deploy`` above, the unfiltered round-trip dump always includes
            # it, so this test declares it with default values.
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
            # ``resource_preflight`` was added by E2-S4 (O-27/O-29 run-entry
            # memory/disk headroom preflight) — like ``deploy`` above, the
            # unfiltered round-trip dump always includes it, so this test
            # declares it with default values.
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
        # ``mode='json'`` so Path objects come back as strings to match the
        # original YAML scalar form.
        roundtripped = cfg.model_dump(mode="json")

        assert roundtripped == original

    def test_roundtrip_with_defaults(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        dumped = cfg.model_dump(mode="json")
        re_parsed = ForgeConfig.model_validate(dumped)
        assert re_parsed.model_dump(mode="json") == dumped


# ---------------------------------------------------------------------------
# AC-007: missing allowlist yields a clear ValidationError
# ---------------------------------------------------------------------------


class TestMissingAllowlistMessage:
    """AC-007: the error message points the operator at the missing field."""

    def test_missing_allowlist_error_mentions_allowlist(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ForgeConfig.model_validate({"permissions": {"filesystem": {}}})

        errors = exc_info.value.errors()
        assert any(
            err["type"] == "missing"
            and err["loc"] == ("permissions", "filesystem", "allowlist")
            for err in errors
        ), (
            f"Expected a 'missing' error on permissions.filesystem.allowlist; got {errors!r}"
        )

    def test_missing_filesystem_block_error_is_clear(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ForgeConfig.model_validate({"permissions": {}})
        errors = exc_info.value.errors()
        assert any(
            err["type"] == "missing" and err["loc"] == ("permissions", "filesystem")
            for err in errors
        )


# ---------------------------------------------------------------------------
# Lane B / Phase E1 — planning.target_terminal flag (default-OFF, additive).
# ---------------------------------------------------------------------------


class TestTargetTerminalConfig:
    """The Lane B target-terminal flag defaults off and is byte-no-op safe."""

    def test_planning_config_has_target_terminal(self) -> None:
        from forge.config.models import PlanningConfig

        assert "target_terminal" in PlanningConfig.model_fields

    def test_target_terminal_defaults_disabled(self) -> None:
        from forge.config.models import PlanningConfig

        cfg = PlanningConfig()
        assert cfg.target_terminal.enabled is False

    def test_target_terminal_defaults_disabled_from_root(self) -> None:
        cfg = ForgeConfig.model_validate(
            {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}
        )
        assert cfg.planning.target_terminal.enabled is False

    def test_target_terminal_can_be_enabled(self) -> None:
        cfg = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
                "planning": {"target_terminal": {"enabled": True}},
            }
        )
        assert cfg.planning.target_terminal.enabled is True

    def test_target_terminal_rejects_unknown_keys(self) -> None:
        from forge.config.models import TargetTerminalConfig

        with pytest.raises(ValidationError):
            TargetTerminalConfig.model_validate({"enabled": False, "bogus": 1})
