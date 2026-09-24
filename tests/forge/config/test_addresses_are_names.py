"""Addresses in the settings file may be NAMES (24 September 2026).

The containerisation pass moved the coordinator off this machine's network.
Every address its settings file carried was one of this machine's own, and
every one of them silently stopped reaching anything the moment the
coordinator became a container on a declared network.

So the loader fills in ``${NAME}`` in an address field from the environment
the service was started with, and REFUSES — by name — when nothing set it.
These tests hold the three properties that make that safe:

* a name that is set is filled in, wherever the address lives in the file;
* a name that is not set is a refusal that says which setting and which line,
  and no configuration comes back at all;
* nothing outside an address field is touched, and a bare ``$NAME`` is left
  alone, because a real address may legitimately contain one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.config.loader import (
    AnAddressNameIsNotSet,
    fill_in_address_names,
    is_an_address_key,
    load_config,
)

# The smallest settings document the root model accepts: ``permissions`` is
# required because there is no safe default filesystem allowlist.
_PERMISSIONS = {"permissions": {"filesystem": {"allowlist": ["/var/lib/forge-evidence"]}}}


def _write(tmp_path: Path, document: dict) -> Path:
    where = tmp_path / "forge.yaml"
    where.write_text(yaml.safe_dump(document), encoding="utf-8")
    return where


class TestAnAddressWrittenAsAName:
    """A ``${NAME}`` in an address field is filled in from the environment."""

    def test_the_publishers_address_is_filled_in(self, tmp_path: Path) -> None:
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "publication": {"publisher_url": "${FORGE_PUBLISHER_URL}"},
            },
        )

        config = load_config(
            where, environ={"FORGE_PUBLISHER_URL": "http://forge-publisher:8711"}
        )

        assert config.publication.publisher_url == "http://forge-publisher:8711"

    def test_a_sandboxs_two_addresses_are_filled_in(self, tmp_path: Path) -> None:
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "planning": {
                    "target_repo_paths": {"the-org/the-project": "/var/lib/forge/p"},
                    "sandboxes": {
                        "the-org/the-project": {
                            "name": "the-sandbox",
                            "sidecar_url": "${FORGE_SANDBOX_SIDECAR_URL}",
                            "runner_url": "${FORGE_SANDBOX_RUNNER_URL}",
                        }
                    },
                },
            },
        )

        config = load_config(
            where,
            environ={
                "FORGE_SANDBOX_SIDECAR_URL": "http://a-gateway.invalid:8125",
                "FORGE_SANDBOX_RUNNER_URL": "http://a-gateway.invalid:8124",
            },
        )

        entry = config.planning.sandboxes["the-org/the-project"]
        assert entry.sidecar_url == "http://a-gateway.invalid:8125"
        assert entry.runner_url == "http://a-gateway.invalid:8124"

    def test_a_name_may_be_part_of_an_address(self, tmp_path: Path) -> None:
        """The machine names the host; the file keeps the port and the route."""
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "deploy": {
                    "execution_surface": "sidecar",
                    "sidecar_url": "http://${FACTORY_GATEWAY_ADDRESS}:8125",
                },
            },
        )

        config = load_config(
            where, environ={"FACTORY_GATEWAY_ADDRESS": "a-gateway.invalid"}
        )

        assert config.deploy.sidecar_url == "http://a-gateway.invalid:8125"


class TestAnUnsetNameIsRefusedByName:
    """Never a default address: a name nothing set stops the load."""

    def test_the_refusal_names_the_setting_and_the_field(self, tmp_path: Path) -> None:
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "publication": {"publisher_url": "${FORGE_PUBLISHER_URL}"},
            },
        )

        with pytest.raises(AnAddressNameIsNotSet) as refusal:
            load_config(where, environ={})

        said = str(refusal.value)
        assert "FORGE_PUBLISHER_URL" in said
        assert "publication.publisher_url" in said
        assert str(where) in said

    def test_an_empty_value_is_as_good_as_unset(self, tmp_path: Path) -> None:
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "publication": {"publisher_url": "${FORGE_PUBLISHER_URL}"},
            },
        )

        with pytest.raises(AnAddressNameIsNotSet):
            load_config(where, environ={"FORGE_PUBLISHER_URL": "   "})

    def test_no_default_address_is_ever_supplied(self, tmp_path: Path) -> None:
        """The whole point: nothing comes back, rather than something wrong."""
        where = _write(
            tmp_path,
            {
                **_PERMISSIONS,
                "deploy": {"sidecar_url": "${FORGE_SANDBOX_SIDECAR_URL}"},
            },
        )

        with pytest.raises(AnAddressNameIsNotSet):
            load_config(where, environ={})


class TestNothingElseIsTouched:
    """Only address fields, and only the braced form."""

    def test_a_value_outside_an_address_field_is_left_alone(self) -> None:
        raw = {"planning": {"terminal": "${NOT_A_NAME_ANYTHING_SET}"}}

        filled = fill_in_address_names(raw, where="forge.yaml", environ={})

        assert filled == raw

    def test_a_bare_dollar_name_is_left_alone(self) -> None:
        raw = {"publication": {"publisher_url": "http://$HOST:8711"}}

        filled = fill_in_address_names(raw, where="forge.yaml", environ={})

        assert filled["publication"]["publisher_url"] == "http://$HOST:8711"

    def test_an_ordinary_address_passes_through_unchanged(self) -> None:
        raw = {"publication": {"publisher_url": "http://forge-publisher:8711"}}

        filled = fill_in_address_names(raw, where="forge.yaml", environ={})

        assert filled == raw

    @pytest.mark.parametrize(
        "key", ["url", "host", "address", "sidecar_url", "runner_url", "publisher_url"]
    )
    def test_these_are_address_keys(self, key: str) -> None:
        assert is_an_address_key(key)

    @pytest.mark.parametrize("key", ["terminal", "name", "allowlist", "curl", "", None])
    def test_these_are_not(self, key: object) -> None:
        assert not is_an_address_key(key)


class TestTheExampleSettingsFileThatShipsWithTheBundle:
    """``deploy/compose/settings.example.yaml`` is the real thing, not prose."""

    @property
    def _where(self) -> Path:
        return (
            Path(__file__).resolve().parents[3]
            / "deploy"
            / "compose"
            / "settings.example.yaml"
        )

    def test_it_loads_when_every_name_is_set(self) -> None:
        config = load_config(
            self._where,
            environ={
                "FORGE_SANDBOX_SIDECAR_URL": "http://a-gateway.invalid:8125",
                "FORGE_SANDBOX_RUNNER_URL": "http://a-gateway.invalid:8124",
                "FORGE_PUBLISHER_URL": "http://forge-publisher:8711",
            },
        )

        assert config.deploy.execution_surface == "sidecar"
        assert config.publication.enabled is False

    def test_it_refuses_when_a_name_is_not_set(self) -> None:
        with pytest.raises(AnAddressNameIsNotSet):
            load_config(self._where, environ={})

    def test_it_carries_no_machine_address(self) -> None:
        written = self._where.read_text(encoding="utf-8")

        assert "127.0.0.1" not in written
        assert "172.30." not in written
        assert "/home/" not in written
