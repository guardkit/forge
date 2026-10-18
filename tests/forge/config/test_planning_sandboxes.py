"""``planning.sandboxes`` — which repositories have a sandbox of their own.

Sandbox first (2026-09-07): nothing the factory runs on a repository runs on
the host. A repository named here has its planning commits made by the deploy
sidecar running inside its own sandbox, with the pre-commit checks run there
too; a repository not named here is handled exactly as before, in the forge
container. Empty by default, so a settings file that says nothing changes
nothing.

The surface is closed (``extra="forbid"``) on both the planning block and
each entry, so a misspelt key is refused rather than quietly ignored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.config.models import PlanningConfig, SandboxEntry

ENTRY = {
    "name": "api-test",
    "sidecar_url": "http://127.0.0.1:8225",
    "runner_url": "http://127.0.0.1:8224",
}
PATHS = {"guardkit/api_test": "/srv/repos/api_test"}


def test_no_repository_has_a_sandbox_by_default() -> None:
    assert PlanningConfig().sandboxes == {}


def test_a_repository_can_name_its_sandbox_and_its_two_addresses() -> None:
    planning = PlanningConfig.model_validate(
        {"target_repo_paths": PATHS, "sandboxes": {"guardkit/api_test": ENTRY}}
    )
    entry = planning.sandboxes["guardkit/api_test"]
    assert isinstance(entry, SandboxEntry)
    assert entry.name == "api-test"
    assert entry.sidecar_url == "http://127.0.0.1:8225"
    assert entry.runner_url == "http://127.0.0.1:8224"


def test_a_sandbox_for_a_repository_the_map_does_not_know_is_refused() -> None:
    """The runner routes by the repository's path and the sidecar resolves the
    same key, so an entry with no path could never be reached. Say so."""
    with pytest.raises(ValidationError) as caught:
        PlanningConfig.model_validate(
            {"target_repo_paths": PATHS, "sandboxes": {"acme/ghost": ENTRY}}
        )
    message = str(caught.value)
    assert "acme/ghost" in message
    assert "add each one to target_repo_paths first" in message


def test_both_addresses_are_required_and_must_be_web_addresses() -> None:
    for field in ("sidecar_url", "runner_url"):
        missing = {k: v for k, v in ENTRY.items() if k != field}
        with pytest.raises(ValidationError):
            SandboxEntry.model_validate(missing)
        with pytest.raises(ValidationError) as caught:
            SandboxEntry.model_validate({**ENTRY, field: "127.0.0.1:8225"})
        assert "http:// or https:// address" in str(caught.value)
    assert SandboxEntry.model_validate(
        {**ENTRY, "sidecar_url": " https://sandbox.local:8225 "}
    ).sidecar_url == "https://sandbox.local:8225"


def test_the_sandbox_needs_a_name_the_sandbox_tool_would_accept() -> None:
    for bad in ("", "-leading-dash", "has space", "slash/name"):
        with pytest.raises(ValidationError):
            SandboxEntry.model_validate({**ENTRY, "name": bad})


def test_both_surfaces_stay_closed() -> None:
    assert "sandboxes" in PlanningConfig.model_fields
    with pytest.raises(ValidationError):
        PlanningConfig.model_validate({"sandboxe": {}})
    with pytest.raises(ValidationError):
        SandboxEntry.model_validate({**ENTRY, "clone_path": "/srv/repos/api_test"})


def test_the_descriptions_are_plain_words() -> None:
    """The settings file is a surface a person reads: no house shorthand."""
    description = PlanningConfig.model_fields["sandboxes"].description or ""
    assert "deploy sidecar" in description and "build runner" in description
    assert "rule " not in description.lower()
    for field in SandboxEntry.model_fields.values():
        assert field.description and "rule " not in field.description.lower()


def test_a_sandbox_is_granted_no_memory_by_default() -> None:
    """Design section 2.4a (18 October 2026): a build's card reads memory only
    from a name its repository was granted; no list, no names."""
    assert SandboxEntry.model_validate(ENTRY).memory_projects == []


def test_memory_projects_are_names_the_memory_service_accepts() -> None:
    entry = SandboxEntry.model_validate({**ENTRY, "memory_projects": ["shop_a", "shop_a", "x9"]})
    assert entry.memory_projects == ["shop_a", "x9"], "a repeated name is kept once"
    for bad in ("Shop_A", "shop-a", "", "shop a", "a" * 129):
        with pytest.raises(ValidationError) as caught:
            SandboxEntry.model_validate({**ENTRY, "memory_projects": [bad]})
        assert "lower-case letters, digits and underscores" in str(caught.value)


def test_two_repositories_are_never_granted_the_same_memory_name() -> None:
    """A hand edit could otherwise give two projects one shelf (design 2.4a)."""
    paths = {"acme/shop-a": "/srv/repos/shop-a", "acme/shop-b": "/srv/repos/shop-b"}
    other = {**ENTRY, "name": "shop-b", "sidecar_url": "http://127.0.0.1:8235",
             "runner_url": "http://127.0.0.1:8234"}
    with pytest.raises(ValidationError) as caught:
        PlanningConfig.model_validate({
            "target_repo_paths": paths,
            "sandboxes": {
                "acme/shop-a": {**ENTRY, "memory_projects": ["shop_a"]},
                "acme/shop-b": {**other, "memory_projects": ["shop_b", "shop_a"]},
            },
        })
    message = str(caught.value)
    assert "shop_a" in message and "acme/shop-a" in message and "acme/shop-b" in message
    assert "belongs to one repository" in message

    planning = PlanningConfig.model_validate({
        "target_repo_paths": paths,
        "sandboxes": {
            "acme/shop-a": {**ENTRY, "memory_projects": ["shop_a", "shop_a"]},
            "acme/shop-b": {**other, "memory_projects": ["shop_b"]},
        },
    })
    assert planning.sandboxes["acme/shop-a"].memory_projects == ["shop_a"]
    assert planning.sandboxes["acme/shop-b"].memory_projects == ["shop_b"]


# ---------------------------------------------------------------------------
# A sandbox's own preview ports (18 October 2026)


def test_a_sandbox_names_no_preview_ports_by_default() -> None:
    assert SandboxEntry.model_validate(ENTRY).preview_ports == []


def test_preview_ports_are_port_numbers_each_named_once() -> None:
    entry = SandboxEntry.model_validate({**ENTRY, "preview_ports": [8960, 8961]})
    assert entry.preview_ports == [8960, 8961]
    for bad in ([0], [65536], [8960, 8960], ["not-a-port"]):
        with pytest.raises(ValidationError):
            SandboxEntry.model_validate({**ENTRY, "preview_ports": bad})


def test_two_sandboxes_never_name_the_same_preview_port() -> None:
    """Every sandbox publishes on one machine: one port is one sandbox's."""
    paths = {"acme/shop-a": "/srv/repos/shop-a", "acme/shop-b": "/srv/repos/shop-b"}
    other = {**ENTRY, "name": "shop-b", "sidecar_url": "http://127.0.0.1:8235",
             "runner_url": "http://127.0.0.1:8234"}
    with pytest.raises(ValidationError) as caught:
        PlanningConfig.model_validate({
            "target_repo_paths": paths,
            "sandboxes": {
                "acme/shop-a": {**ENTRY, "preview_ports": [8960, 8961]},
                "acme/shop-b": {**other, "preview_ports": [8961, 8970]},
            },
        })
    message = str(caught.value)
    assert "8961" in message and "api-test" in message and "shop-b" in message
    assert "preview ports of its own" in message

    planning = PlanningConfig.model_validate({
        "target_repo_paths": paths,
        "sandboxes": {
            "acme/shop-a": {**ENTRY, "preview_ports": [8960, 8961]},
            "acme/shop-b": {**other, "preview_ports": [8970, 8971]},
        },
    })
    assert planning.sandboxes["acme/shop-b"].preview_ports == [8970, 8971]
