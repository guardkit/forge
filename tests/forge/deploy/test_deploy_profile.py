"""Tests for the deploy-profile loader (WS2-B8, scope-design §4).

Covers the field-list parse, the minimal-profile shape, and the load-bearing
WS2-B8 guardrail: **secrets are register REFS only** — a value-bearing entry is
refused so a secret value can never enter a profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.deploy.profile import (
    DeployProfile,
    DeployProfileError,
    load_deploy_profile,
    parse_deploy_profile,
)

FLEET_MEMORY = Path("deploy/profile.yaml")


class TestLoadExemplar:
    """The committed fleet-memory exemplar profile parses to the §4 field list."""

    def test_exemplar_loads(self) -> None:
        p = load_deploy_profile(FLEET_MEMORY)
        assert isinstance(p, DeployProfile)
        assert p.env_id == "fleet-memory-nas"
        assert p.host_names == ["nas"]
        assert p.compose.file == "deploy/nas/docker-compose.yml"
        assert p.compose.script == "deploy.sh"
        assert p.secret_injection == ("FLEET_MEMORY_PG_DSN",)
        assert p.health_checks[0].cmd == "smoke.sh"
        assert p.source_ref is not None

    def test_reservation_resource_none_when_absent(self) -> None:
        p = load_deploy_profile(FLEET_MEMORY)
        assert p.reservation is None
        assert p.reservation_resource is None


class TestMinimalProfile:
    """A profile needs only env_id + compose.file."""

    def test_minimal(self) -> None:
        p = parse_deploy_profile(
            {"env_id": "demo", "compose": {"file": "docker-compose.yml"}}
        )
        assert p.env_id == "demo"
        assert p.compose.file == "docker-compose.yml"
        assert p.hosts == ()
        assert p.secret_injection == ()

    def test_missing_env_id_raises(self) -> None:
        with pytest.raises(DeployProfileError, match="env_id"):
            parse_deploy_profile({"compose": {"file": "c.yml"}})

    def test_missing_compose_raises(self) -> None:
        with pytest.raises(DeployProfileError, match="compose"):
            parse_deploy_profile({"env_id": "demo"})


class TestRichProfile:
    """A rich profile parameterizes every §4 section."""

    def test_all_sections(self) -> None:
        p = parse_deploy_profile(
            {
                "env_id": "study-tutor",
                "compose": {"file": "dc.yml", "profile": "prod"},
                "hosts": [
                    {"host": "nas", "role": "postgres"},
                    {"host": "gb10", "role": "backend"},
                ],
                "realm_import": "keycloak/realm.json",
                "secret_injection": ["MONEYHUB_SECRET", {"ref": "PG_DSN"}],
                "seed_fixture_contract": [
                    {"script": "restore.sh", "golden_state_ref": "golden.sql"}
                ],
                "models_required": [
                    {"model": "gemma", "warm_up_action": "warm.sh"},
                    "phi",
                ],
                "health_checks": [{"cmd": "health.sh", "expected": "ok"}],
                "broker_contract_ref": "contracts/manifest.yaml",
                "reservation": {"resource": "gb10-gpu", "quiet_window": "22:00-06:00"},
                "cwd": "/repo",
            }
        )
        assert [h.host for h in p.hosts] == ["nas", "gb10"]
        assert p.realm_import == "keycloak/realm.json"
        assert p.secret_injection == ("MONEYHUB_SECRET", "PG_DSN")
        assert p.seed_fixture_contract[0].script == "restore.sh"
        assert {m.model for m in p.models_required} == {"gemma", "phi"}
        assert p.reservation_resource == "gb10-gpu"
        assert p.broker_contract_ref == "contracts/manifest.yaml"


class TestSecretsAreRefsOnly:
    """WS2-B8 guardrail: secret_injection carries register REFS (names), never values."""

    def test_value_bearing_assignment_refused(self) -> None:
        with pytest.raises(DeployProfileError, match="REFS ONLY"):
            parse_deploy_profile(
                {
                    "env_id": "x",
                    "compose": {"file": "c.yml"},
                    "secret_injection": ["PG_DSN=postgres://user:pw@host/db"],
                }
            )

    def test_value_bearing_whitespace_refused(self) -> None:
        with pytest.raises(DeployProfileError, match="REFS ONLY"):
            parse_deploy_profile(
                {
                    "env_id": "x",
                    "compose": {"file": "c.yml"},
                    "secret_injection": ["postgres://user:pw@host"],
                }
            )

    def test_mapping_with_value_key_refused(self) -> None:
        with pytest.raises(DeployProfileError, match="value-bearing"):
            parse_deploy_profile(
                {
                    "env_id": "x",
                    "compose": {"file": "c.yml"},
                    "secret_injection": [{"ref": "PG_DSN", "value": "secret"}],
                }
            )

    def test_bare_name_accepted(self) -> None:
        p = parse_deploy_profile(
            {
                "env_id": "x",
                "compose": {"file": "c.yml"},
                "secret_injection": ["NATS_PASSWORD", "moneyhub.client_secret"],
            }
        )
        assert p.secret_injection == ("NATS_PASSWORD", "moneyhub.client_secret")


class TestLoadErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DeployProfileError, match="not found"):
            load_deploy_profile(tmp_path / "nope.yaml")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        with pytest.raises(DeployProfileError, match="empty"):
            load_deploy_profile(p)

    def test_non_mapping_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(DeployProfileError, match="mapping"):
            load_deploy_profile(p)


class TestRollbackImageRef:
    """O-32 — the profile carries the kept :rollback-* image ref."""

    def test_rollback_ref_parsed_and_exposed(self) -> None:
        p = parse_deploy_profile(
            {
                "env_id": "e",
                "compose": {"file": "dc.yml"},
                "rollback_image_ref": "app:rollback-20260713",
            }
        )
        assert p.rollback_image_ref == "app:rollback-20260713"
        assert p.rollback_ref == "app:rollback-20260713"

    def test_rollback_ref_absent_is_none(self) -> None:
        p = parse_deploy_profile({"env_id": "e", "compose": {"file": "dc.yml"}})
        assert p.rollback_ref is None

    def test_empty_rollback_ref_rejected(self) -> None:
        with pytest.raises(DeployProfileError, match="rollback_image_ref"):
            parse_deploy_profile(
                {"env_id": "e", "compose": {"file": "dc.yml"}, "rollback_image_ref": "  "}
            )


class TestLiveGateSection:
    """The optional per-target live_gate driver spec (the F16 real backend)."""

    def _base(self) -> dict:
        return {"env_id": "e", "compose": {"file": "dc.yml"}}

    def test_absent_is_none(self) -> None:
        p = parse_deploy_profile(self._base())
        assert p.live_gate is None

    def test_exemplar_has_no_live_gate(self) -> None:
        # The committed fleet-memory exemplar deliberately ships no live_gate
        # (headless Postgres, no live walk) — the seam stays Unconfigured.
        p = load_deploy_profile(FLEET_MEMORY)
        assert p.live_gate is None

    def test_minimal_live_gate_parses(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": ["python3", "qa/gates/local_live_gate.py"]}
        p = parse_deploy_profile(raw)
        assert p.live_gate is not None
        assert p.live_gate.driver == ("python3", "qa/gates/local_live_gate.py")
        assert p.live_gate.gates == ()
        assert p.live_gate.timeout_seconds == 600
        assert p.live_gate.env == {}

    def test_full_live_gate_parses(self) -> None:
        raw = self._base()
        raw["live_gate"] = {
            "driver": ["python3", "qa/gates/local_live_gate.py"],
            "gates": ["health", "api"],
            "timeout_seconds": 120,
            "env": {"API_TEST_BASE_URL": "http://localhost:8901"},
        }
        p = parse_deploy_profile(raw)
        lg = p.live_gate
        assert lg is not None
        assert lg.gates == ("health", "api")
        assert lg.timeout_seconds == 120
        assert lg.env == {"API_TEST_BASE_URL": "http://localhost:8901"}

    def test_empty_driver_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": []}
        with pytest.raises(DeployProfileError, match="live_gate.driver"):
            parse_deploy_profile(raw)

    def test_missing_driver_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"gates": ["health"]}
        with pytest.raises(DeployProfileError, match="live_gate.driver"):
            parse_deploy_profile(raw)

    def test_non_string_driver_element_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": ["python3", 3]}
        with pytest.raises(DeployProfileError, match=r"live_gate.driver\[1\]"):
            parse_deploy_profile(raw)

    def test_non_positive_timeout_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": ["x"], "timeout_seconds": 0}
        with pytest.raises(DeployProfileError, match="timeout_seconds"):
            parse_deploy_profile(raw)

    def test_lowercase_env_name_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": ["x"], "env": {"base_url": "http://x"}}
        with pytest.raises(DeployProfileError, match="UPPER_SNAKE"):
            parse_deploy_profile(raw)

    def test_non_string_env_value_rejected(self) -> None:
        raw = self._base()
        raw["live_gate"] = {"driver": ["x"], "env": {"PORT": 8080}}
        with pytest.raises(DeployProfileError, match="string value"):
            parse_deploy_profile(raw)


class TestPreviewSection:
    """The optional ``preview`` section (web readiness W7, 12 October 2026)."""

    BASE = {"env_id": "notes", "compose": {"file": "compose.yml"}}
    SLOTS = [
        {
            "port": 8950,
            "internal_port": 18950,
            "public_url": "https://preview.example.test:8950",
        },
        {
            "port": 8951,
            "internal_port": 18951,
            "public_url": "https://preview.example.test:8951/",
        },
    ]

    def test_absent_is_none(self) -> None:
        assert parse_deploy_profile(dict(self.BASE)).preview is None

    def test_full_section_parses_with_defaults(self) -> None:
        p = parse_deploy_profile(
            {**self.BASE, "preview": {"enabled": True, "slots": self.SLOTS}}
        )
        assert p.preview is not None
        assert p.preview.enabled is True
        assert [s.port for s in p.preview.slots] == [8950, 8951]
        assert p.preview.slots[1].public_url == "https://preview.example.test:8951"
        assert (p.preview.app_ttl_hours, p.preview.evidence_days, p.preview.slot_wait_minutes) == (
            24,
            14,
            30,
        )
        assert "preview" not in p.extra

    def test_enabled_defaults_to_off(self) -> None:
        p = parse_deploy_profile({**self.BASE, "preview": {"slots": self.SLOTS}})
        assert p.preview is not None and p.preview.enabled is False

    def test_explicit_lifetimes(self) -> None:
        p = parse_deploy_profile(
            {
                **self.BASE,
                "preview": {
                    "enabled": True,
                    "slots": self.SLOTS,
                    "app_ttl_hours": 2,
                    "evidence_days": 3,
                    "slot_wait_minutes": 4,
                },
            }
        )
        assert (p.preview.app_ttl_hours, p.preview.evidence_days, p.preview.slot_wait_minutes) == (
            2,
            3,
            4,
        )

    @pytest.mark.parametrize(
        "preview, match",
        [
            ({"enabled": "yes", "slots": SLOTS}, "preview.enabled"),
            ({"enabled": True}, "at least one slot"),
            ({"enabled": True, "slots": "8950"}, "preview.slots must be a list"),
            (
                {"enabled": True, "slots": [{**SLOTS[0], "port": 0}]},
                r"preview.slots\[0\].port",
            ),
            (
                {"enabled": True, "slots": [{**SLOTS[0], "internal_port": "x"}]},
                "internal_port",
            ),
            (
                {"enabled": True, "slots": [{**SLOTS[0], "public_url": "http://x:1"}]},
                "https",
            ),
            (
                {"enabled": True, "slots": [SLOTS[0], {**SLOTS[1], "port": 8950}]},
                "ports of its own",
            ),
            (
                {"enabled": True, "slots": [{**SLOTS[0], "internal_port": 8950}]},
                "ports of its own",
            ),
            ({"enabled": True, "slots": SLOTS, "app_ttl_hours": 0}, "app_ttl_hours"),
            ({"enabled": True, "slots": SLOTS, "evidence_days": True}, "evidence_days"),
            ({"enabled": True, "slots": SLOTS, "slot_wait_minutes": -1}, "slot_wait_minutes"),
            ("on", "preview must be a mapping"),
        ],
    )
    def test_bad_sections_are_refused_in_words(self, preview, match) -> None:
        with pytest.raises(DeployProfileError, match=match):
            parse_deploy_profile({**self.BASE, "preview": preview})

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pw@preview.example.test:8950",
            "https://preview.example.test:8950\\evil",
            "https://preview.example.test :8950",
            "https://preview.example.test:8950/\x07",
            "https://preview.example.test:8950/?x=1",
            "https://preview.example.test:8950/#frag",
            "https://",
            "https://preview.example.test:port",
        ],
    )
    def test_a_slot_address_that_could_be_read_two_ways_is_refused(self, url) -> None:
        with pytest.raises(DeployProfileError, match="plain https"):
            parse_deploy_profile(
                {
                    **self.BASE,
                    "preview": {"enabled": True, "slots": [{**self.SLOTS[0], "public_url": url}]},
                }
            )


class TestTheEvidenceBaseAddress:
    @pytest.mark.parametrize(
        "url",
        [
            "https://user@evidence.example.test/factory-evidence",
            "https://evidence.example.test/factory\\evidence",
            "https://evidence.example.test/factory evidence",
            "http://evidence.example.test/factory-evidence",
            "https://evidence.example.test/factory-evidence?x=1",
        ],
    )
    def test_an_address_that_could_be_read_two_ways_is_refused(self, url) -> None:
        from pydantic import ValidationError

        from forge.config.models import ForgeConfig

        with pytest.raises(ValidationError, match="plain https"):
            ForgeConfig.model_validate(
                {
                    "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                    "review_preview": {"evidence_base_url": url},
                }
            )

    def test_a_plain_address_is_kept_without_its_trailing_slash(self) -> None:
        from forge.config.models import ForgeConfig

        config = ForgeConfig.model_validate(
            {
                "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
                "review_preview": {
                    "evidence_base_url": "https://evidence.example.test/factory-evidence/"
                },
            }
        )
        assert config.review_preview.evidence_base_url == (
            "https://evidence.example.test/factory-evidence"
        )
        assert config.review_preview.enabled is False
        assert config.review_preview.evidence_root is None
