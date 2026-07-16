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
