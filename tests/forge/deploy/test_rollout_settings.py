"""Behavioral boundary tests for the standalone rollout-settings command.

The fake Docker boundary executes the candidate's transformer with this release image's
real ``forge.config.loader``. A separate operational receipt exercises real Compose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy/estate/rollout-settings"
IMAGE = "sha256:" + "f" * 64
IDS = {
    "FORGE_IMAGE",
    "FORGE_PUBLISHER_IMAGE",
    "FLEET_MEMORY_MCP_IMAGE",
    "FLEET_MEMORY_RELAY_IMAGE",
    "JARVIS_IMAGE",
    "NATS_IMAGE",
    "NATS_PROVISION_IMAGE",
}


@pytest.fixture
def scenario(tmp_path: Path):
    old_evidence = tmp_path / "old-evidence"
    old_one = tmp_path / "project-one"
    old_two = tmp_path / "seed-two"
    settings = yaml.safe_load((REPO / "deploy/compose/settings.example.yaml").read_text())
    settings["permissions"]["filesystem"]["allowlist"] = [
        str(old_evidence),
        str(old_one),
        "/var/lib/deliberate-extra",
    ]
    settings["planning"]["target_repo_paths"] = {
        "example/project-one": str(old_one),
        "synthetic/project-two": str(tmp_path / "project-two"),
        "synthetic/project-three": str(tmp_path / "project-three"),
        "synthetic/project-four": str(tmp_path / "project-four"),
        "synthetic/project-five": str(tmp_path / "project-five"),
        "synthetic-seed/seed-one": str(tmp_path / "seed-one"),
        "synthetic-seed/seed-two": str(old_two),
        "synthetic-seed/seed-three": str(tmp_path / "seed-three"),
    }
    settings["planning"]["sandboxes"] = {
        "example/project-one": {
            "name": "project-one-sandbox",
            "sidecar_url": "http://127.0.0.1:8125",
            "runner_url": "http://127.0.0.1:8124",
        }
    }
    settings["deploy"]["sidecar_url"] = "http://127.0.0.1:8125"
    settings["publication"]["publisher_url"] = "http://127.0.0.1:8711"
    settings_input = tmp_path / "input-forge.yaml"
    settings_input.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")

    public_values = {
        **{name: IMAGE for name in IDS},
        "RELEASE_SWEEP_TERMS": "old-machine",
        "FACTORY_GATEWAY_ADDRESS": "192.0.2.44",
        "FORGE_SANDBOX_SIDECAR_PORT": "18125",
        "FORGE_SANDBOX_RUNNER_PORT": "18124",
        "FORGE_SANDBOX_SIDECAR_URL": "http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_SANDBOX_SIDECAR_PORT}",
        "FORGE_SANDBOX_RUNNER_URL": "http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_SANDBOX_RUNNER_PORT}",
        "FORGE_AUTOBUILD_RUNNER_URL": "${FORGE_SANDBOX_RUNNER_URL}",
        "FORGE_PUBLISHER_URL": "http://forge-publisher:8711",
        "FORGE_ANSWER_PORT": "18126",
        "FORGE_TARGET_OWNER_URL": "http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_ANSWER_PORT}/recorded",
        "FORGE_NATS_URL": "nats://forge:${FORGE_NATS_PASSWORD}@bus:14222",
        "FLEET_MEMORY_BUS_ADDRESS": "nats://bus:14222",
        "JARVIS_NATS_URL": "nats://bus:14222",
        "BUS_MONITORING_ADDRESS": "bus:18222",
        "NATS_PROVISION_ADDRESS": "nats://bus:14222",
        "NATS_CLIENT_PORT": "14222",
        "FLEET_MEMORY_URL": "http://memory:8005/mcp/",
        "FLEET_MEMORY_PORT": "8005",
        "FLEET_MEMORY_ALLOWED_HOSTS": "memory:8005,${FACTORY_GATEWAY_ADDRESS}:8005",
        "FLEET_MEMORY_ENABLED": "true",
        "FLEET_MEMORY_EMBED_URL": "http://${FACTORY_GATEWAY_ADDRESS}:18080",
        "FLEET_MEMORY_EMBED_MODEL": "example-embed",
        "FLEET_MEMORY_EMBED_DIMS": "768",
        "JARVIS_MODEL_SEAT_URL": "http://${FACTORY_GATEWAY_ADDRESS}:18080",
        "OPENAI_BASE_URL": "http://${FACTORY_GATEWAY_ADDRESS}:18080",
    }
    env_file = tmp_path / "estate.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in sorted(public_values.items())) + "\n",
        encoding="utf-8",
    )
    secret = tmp_path / "private.env"
    secret.write_text(
        "FORGE_NATS_PASSWORD=not-a-real-password\n"
        "FLEET_MEMORY_NATS_URL=nats://memory:not-a-real-password@192.0.2.44:14222\n"
        "FLEET_MEMORY_PG_DSN=postgresql://memory:not-a-real-password@pg:15432/memory\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "previous-runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "format_version": 1,
                "image_id": IMAGE,
                "release_image_id": IMAGE,
                "repo_tags": [],
                "mounts": [{"Type": "bind", "Source": str(old_one), "Destination": str(old_one)}],
                "networks": {},
                "ports": {},
                "port_bindings": {},
                "restart_policy": {"Name": "unless-stopped"},
                "network_mode": "host",
                "env_names": ["FORGE_IMAGE", "FORGE_NATS_URL"],
                "service_identity": {
                    "name": "made-up-coordinator",
                    "container_id": "1" * 64,
                    "hostname": "made-up",
                    "user": "forge",
                    "working_dir": "/home/forge",
                    "entrypoint": ["forge"],
                    "command": ["serve"],
                },
            }
        ),
        encoding="utf-8",
    )
    compose = {
        "services": {
            "coordinator": {
                "image": IMAGE,
                "environment": {
                    "FORGE_NATS_URL": "nats://forge:not-a-real-password@bus:14222",
                    "FORGE_AUTOBUILD_RUNNER_URL": "http://192.0.2.44:18124",
                    "FORGE_SANDBOX_SIDECAR_URL": "http://192.0.2.44:18125",
                    "FORGE_SANDBOX_RUNNER_URL": "http://192.0.2.44:18124",
                    "FORGE_PUBLISHER_URL": "http://forge-publisher:8711",
                    "FLEET_MEMORY_EMBED_URL": "http://192.0.2.44:18080",
                },
                "networks": {"factory": None, "forge-publisher-net": None},
                "volumes": [{"type": "volume", "source": "ledger", "target": "/var/lib/forge"}],
            },
            "answer-service": {
                "image": IMAGE,
                "networks": {"factory": None},
                "ports": [{"host_ip": "192.0.2.44", "published": "18126", "target": 8126}],
            },
            "forge-publisher": {
                "image": IMAGE,
                "networks": {"forge-publisher-net": None},
                "volumes": [{"type": "volume", "source": "ledger", "target": "/var/lib/forge"}],
            },
            "memory": {
                "image": IMAGE,
                "environment": {
                    "FLEET_MEMORY_MCP_ALLOWED_HOSTS": "memory:8005,192.0.2.44:8005",
                    "FLEET_MEMORY_EMBED_URL": "http://192.0.2.44:18080",
                },
                "networks": {"factory": None},
                "ports": [{"host_ip": "192.0.2.44", "published": "18005", "target": 8005}],
            },
            "memory-relay": {
                "image": IMAGE,
                "environment": {
                    "FLEET_MEMORY_BUS_ADDRESS": "nats://bus:14222",
                    "FLEET_MEMORY_EMBED_URL": "http://192.0.2.44:18080",
                },
                "networks": {"factory": None},
            },
            "front-door": {
                "image": IMAGE,
                "environment": {
                    "JARVIS_NATS_URL": "nats://bus:14222",
                    "JARVIS_LLAMA_SWAP_BASE_URL": "http://192.0.2.44:18080",
                },
                "networks": {"factory": None},
            },
            "bus-gateway": {
                "image": IMAGE,
                "environment": {
                    "JARVIS_NATS_URL": "nats://bus:14222",
                    "JARVIS_LLAMA_SWAP_BASE_URL": "http://192.0.2.44:18080",
                },
                "networks": {"factory": None},
            },
            "bus-ready": {
                "image": IMAGE,
                "environment": {"BUS_MONITORING_ADDRESS": "bus:18222"},
                "networks": {"factory": None},
            },
        }
    }
    compose_json = tmp_path / "compose.json"
    compose_json.write_text(json.dumps(compose))
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(__file__).resolve().parent.parent
with (root / "docker.log").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["image", "inspect"]:
    print(args[-1])
    raise SystemExit(0)
if args and args[0] == "compose":
    print((root / "compose.json").read_text())
    raise SystemExit(0)
if args and args[0] == "run":
    mounts, child_env = {}, os.environ.copy()
    index = 0
    while index < len(args):
        if args[index] == "-v":
            source, target, *_ = args[index + 1].split(":")
            mounts[target] = source
            index += 2
        elif args[index] == "-e":
            key, value = args[index + 1].split("=", 1)
            child_env[key] = value
            index += 2
        else:
            index += 1
    code_at = args.index("-c")
    code = args[code_at + 1]
    child_args = []
    for value in args[code_at + 2:]:
        replaced = value
        for target, source in mounts.items():
            if value == target or value.startswith(target + "/"):
                replaced = source + value[len(target):]
                break
        child_args.append(replaced)
    raise SystemExit(subprocess.run([sys.executable, "-c", code, *child_args], env=child_env).returncode)
raise SystemExit(93)
''',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    outputs = {
        "env": tmp_path / "rendered.env",
        "settings": tmp_path / "rendered-forge.yaml",
        "receipt": tmp_path / "settings-receipt.json",
    }
    command = [
        str(SCRIPT),
        "--env-file", str(env_file),
        "--env-output", str(outputs["env"]),
        "--settings-input", str(settings_input),
        "--settings-output", str(outputs["settings"]),
        "--compose-file", str(compose_file),
        "--project", "codex-settings-build-20260927",
        "--previous-runtime", str(runtime),
        "--receipt", str(outputs["receipt"]),
        "--evidence-source-path", str(old_evidence),
        "--secret-env-file", str(secret),
        "--ack-registration-drift",
        "synthetic/project-two",
        "--ack-registration-drift",
        "synthetic/project-three",
        "--ack-registration-drift",
        "synthetic/project-four",
        "--ack-registration-drift",
        "synthetic/project-five",
        "--ack-registration-drift",
        "synthetic-seed/seed-one",
        "--ack-registration-drift",
        "synthetic-seed/seed-two",
        "--ack-registration-drift",
        "synthetic-seed/seed-three",
        "--machine-term", "old-machine",
    ]
    process_env = os.environ.copy()
    process_env["PATH"] = f"{fake_bin}:{process_env['PATH']}"
    return {
        "command": command,
        "env": process_env,
        "outputs": outputs,
        "settings_input": settings_input,
        "env_file": env_file,
        "secret": secret,
        "compose_file": compose_file,
        "runtime": runtime,
        "compose_json": compose_json,
        "docker_log": tmp_path / "docker.log",
        "old_one": old_one,
        "old_two": old_two,
    }


def run(scenario, *extra):
    return subprocess.run(
        [*scenario["command"], *extra],
        env=scenario["env"],
        text=True,
        capture_output=True,
        check=False,
    )


def configure_legacy_alias_shape(scenario):
    """Write the synthetic shape of the pre-publication live settings."""
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings.pop("publication")

    registrations = {}
    alias_pairs = []
    for number in range(1, 7):
        shared_path = scenario["settings_input"].parent / f"legacy-repo-{number}"
        left = f"owner-a/repo-{number}"
        right = f"owner-b/repo-{number}"
        registrations[left] = str(shared_path)
        registrations[right] = str(shared_path)
        alias_pairs.append((left, right, shared_path))
    for number in range(1, 8):
        project = f"owner-c/unique-{number}"
        registrations[project] = str(
            scenario["settings_input"].parent / f"legacy-unique-{number}"
        )

    sandbox_projects = tuple(registrations)[:9]
    settings["planning"]["target_repo_paths"] = registrations
    settings["planning"]["sandboxes"] = {
        project: {
            "name": f"synthetic-sandbox-{number}",
            "sidecar_url": "http://127.0.0.1:8125",
            "runner_url": "http://127.0.0.1:8124",
        }
        for number, project in enumerate(sandbox_projects, 1)
    }
    old_evidence = settings["permissions"]["filesystem"]["allowlist"][0]
    settings["permissions"]["filesystem"]["allowlist"] = [
        old_evidence,
        str(alias_pairs[0][2]),
        "/var/lib/deliberate-extra",
    ]
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"] = [
        {"Type": "bind", "Source": path, "Destination": path}
        for path in dict.fromkeys(registrations.values())
    ]
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    command = scenario["command"]
    while "--ack-registration-drift" in command:
        index = command.index("--ack-registration-drift")
        del command[index : index + 2]
    return registrations, alias_pairs, sandbox_projects


def configure_parent_mapped_evidence(scenario):
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    old_evidence = settings["permissions"]["filesystem"]["allowlist"][0]
    settings["permissions"]["filesystem"]["allowlist"] = [
        "/var/forge" if item == old_evidence else item
        for item in settings["permissions"]["filesystem"]["allowlist"]
    ]
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    host_parent = scenario["settings_input"].parent / "synthetic-forge-state"
    evidence_source = host_parent / "receipts"
    evidence_source.mkdir(parents=True)
    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"].append(
        {"Type": "bind", "Source": str(host_parent), "Destination": "/var/forge"}
    )
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    command = scenario["command"]
    evidence_index = command.index("--evidence-source-path") + 1
    command[evidence_index] = str(evidence_source)
    if "--add-evidence-permission" not in command:
        command.append("--add-evidence-permission")
    return host_parent, evidence_source


def configure_existing_exact_mapped_evidence(scenario):
    host_parent, evidence_source = configure_parent_mapped_evidence(scenario)
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["permissions"]["filesystem"]["allowlist"] = [
        str(evidence_source) if item == "/var/forge" else item
        for item in settings["permissions"]["filesystem"]["allowlist"]
    ]
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )
    scenario["command"].remove("--add-evidence-permission")
    return host_parent, evidence_source


def configure_complete_actual_shape(scenario):
    """Model the complete sanitized 19/6/9 legacy settings and permission shape."""
    registrations, alias_pairs, sandbox_projects = configure_legacy_alias_shape(scenario)
    smoke_parent = scenario["settings_input"].parent / "smoke-preparer" / "repos"
    smoke_project = smoke_parent / "synthetic-project"
    registrations["owner-c/unique-1"] = str(smoke_project)

    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["planning"]["target_repo_paths"] = registrations
    unique_paths = list(dict.fromkeys(registrations.values()))
    obsolete_home = "/home/synthetic-forge-runtime"
    obsolete_checkout = "/home/synthetic-owner/Projects/synthetic-forge"
    settings["permissions"]["filesystem"]["allowlist"] = [
        obsolete_home,
        unique_paths[0],
        obsolete_checkout,
        *unique_paths[1:5],
        str(smoke_parent),
        *unique_paths[7:10],
        "/var/lib/deliberate-extra",
    ]
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    evidence_root = scenario["settings_input"].parent / "legacy-state"
    evidence_source = evidence_root / "receipts"
    evidence_source.mkdir(parents=True)
    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"] = [
        {"Type": "bind", "Source": path, "Destination": path}
        for path in dict.fromkeys(registrations.values())
    ]
    runtime["mounts"].append(
        {"Type": "bind", "Source": str(evidence_root), "Destination": "/var/forge"}
    )
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    command = scenario["command"]
    command[command.index("--evidence-source-path") + 1] = str(evidence_source)
    command.extend(
        [
            "--add-evidence-permission",
            "--retire-permission",
            obsolete_home,
            "--retire-permission",
            obsolete_checkout,
        ]
    )
    return {
        "registrations": registrations,
        "alias_pairs": alias_pairs,
        "sandbox_projects": sandbox_projects,
        "evidence_source": evidence_source,
        "obsolete": [obsolete_home, obsolete_checkout],
        "smoke_parent": str(smoke_parent),
    }


def transformer_run_arguments(scenario):
    calls = [json.loads(line) for line in scenario["docker_log"].read_text().splitlines()]
    return next(call for call in calls if call and call[0] == "run")


def test_legacy_settings_add_safe_publication_and_preserve_alias_identity(scenario):
    registrations, alias_pairs, sandbox_projects = configure_legacy_alias_shape(scenario)

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    rendered_paths = rendered["planning"]["target_repo_paths"]
    assert len(rendered_paths) == 19
    assert set(rendered_paths) == set(registrations)
    assert len(set(rendered_paths.values())) == 13
    for left, right, _ in alias_pairs:
        assert rendered_paths[left] == rendered_paths[right]
    assert set(rendered["planning"]["sandboxes"]) == set(sandbox_projects)
    assert rendered["publication"] == {
        "enabled": False,
        "publisher_url": "${FORGE_PUBLISHER_URL}",
        "builds_may_run_inside_the_coordinator": False,
    }
    assert rendered["permissions"]["filesystem"]["allowlist"][1] == (
        "/var/lib/forge/projects/repo-1"
    )

    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert set(receipt["registrations"]) == set(registrations)
    assert {
        project
        for project, details in receipt["registrations"].items()
        if details["sandbox_configured"]
    } == set(sandbox_projects)
    for left, right, _ in alias_pairs:
        assert receipt["registrations"][left]["new_path"] == (
            receipt["registrations"][right]["new_path"]
        )


def test_parent_mapped_evidence_adds_only_canonical_permission(scenario):
    _, evidence_source = configure_parent_mapped_evidence(scenario)
    input_before = scenario["settings_input"].read_bytes()

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    assert scenario["settings_input"].read_bytes() == input_before
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    allowlist = rendered["permissions"]["filesystem"]["allowlist"]
    assert "/var/forge" in allowlist
    assert "/var/lib/deliberate-extra" in allowlist
    assert "/var/lib/forge-evidence" in allowlist
    assert allowlist.count("/var/lib/forge-evidence") == 1
    assert str(evidence_source) not in allowlist

    docker_args = transformer_run_arguments(scenario)
    assert any(
        item.endswith(":/input/previous-runtime.json:ro") for item in docker_args
    )
    assert "/input/previous-runtime.json" in docker_args


def test_parent_mapped_evidence_requires_a_matching_bind(scenario):
    host_parent, _ = configure_parent_mapped_evidence(scenario)
    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"] = [
        mount for mount in runtime["mounts"] if mount.get("Source") != str(host_parent)
    ]
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_mapped_evidence_requires_explicit_add_disposition(scenario):
    configure_parent_mapped_evidence(scenario)
    scenario["command"].remove("--add-evidence-permission")

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_unrelated_ancestor_permission_does_not_authorize_evidence(scenario):
    configure_parent_mapped_evidence(scenario)
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["permissions"]["filesystem"]["allowlist"] = [
        "/var" if item == "/var/forge" else item
        for item in settings["permissions"]["filesystem"]["allowlist"]
    ]
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_bind_source_prefix_is_not_path_ancestry(scenario):
    host_parent, _ = configure_parent_mapped_evidence(scenario)
    runtime = json.loads(scenario["runtime"].read_text())
    for mount in runtime["mounts"]:
        if mount.get("Source") == str(host_parent):
            mount["Source"] = str(host_parent).removesuffix("-state")
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_nested_bind_mapping_is_ambiguous_and_refuses(scenario):
    _, evidence_source = configure_parent_mapped_evidence(scenario)
    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"].append(
        {
            "Type": "bind",
            "Source": str(evidence_source),
            "Destination": "/var/other-receipts",
        }
    )
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_noncanonical_bind_path_refuses(scenario):
    host_parent, _ = configure_parent_mapped_evidence(scenario)
    runtime = json.loads(scenario["runtime"].read_text())
    for mount in runtime["mounts"]:
        if mount.get("Source") == str(host_parent):
            mount["Destination"] = "/var/forge/../forge"
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())




@pytest.mark.parametrize(
    "alias", ["/var/forge/.", "/var/forge//receipts", "/var/forge/receipts/.."]
)
def test_noncanonical_covering_permission_refuses_without_outputs(scenario, alias):
    configure_parent_mapped_evidence(scenario)
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["permissions"]["filesystem"]["allowlist"].append(alias)
    scenario["settings_input"].write_text(yaml.safe_dump(settings, sort_keys=False))

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


@pytest.mark.parametrize("kind", ["bind", "volume", "tmpfs"])
def test_mount_shadowing_mapped_evidence_refuses_without_outputs(scenario, kind):
    host_parent, _ = configure_parent_mapped_evidence(scenario)
    runtime = json.loads(scenario["runtime"].read_text())
    runtime["mounts"].append(
        {
            "Type": kind,
            "Source": str(host_parent.parent / "different-source"),
            "Destination": "/var/forge/receipts",
        }
    )
    scenario["runtime"].write_text(json.dumps(runtime), encoding="utf-8")

    result = run(scenario)

    assert result.returncode == 2
    assert "shadows" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


@pytest.mark.parametrize("where", ["evidence", "bind-source"])
def test_host_symlink_in_evidence_mapping_refuses_without_outputs(scenario, where):
    host_parent, evidence_source = configure_parent_mapped_evidence(scenario)
    outside = host_parent.parent / "outside"
    outside.mkdir()
    if where == "evidence":
        evidence_source.rmdir()
        evidence_source.symlink_to(outside, target_is_directory=True)
    else:
        evidence_source.rmdir()
        host_parent.rmdir()
        host_parent.symlink_to(outside, target_is_directory=True)
        (outside / "receipts").mkdir()

    result = run(scenario)

    assert result.returncode == 2
    assert "symbolic-link" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_complete_actual_shape_preserves_registration_permission_coverage(scenario):
    shape = configure_complete_actual_shape(scenario)
    input_bytes = scenario["settings_input"].read_bytes()
    runtime_bytes = scenario["runtime"].read_bytes()

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    assert scenario["settings_input"].read_bytes() == input_bytes
    assert scenario["runtime"].read_bytes() == runtime_bytes
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert len(rendered["planning"]["target_repo_paths"]) == 19
    assert len(rendered["planning"]["sandboxes"]) == 9
    assert len(set(rendered["planning"]["target_repo_paths"].values())) == 13
    allowlist = rendered["permissions"]["filesystem"]["allowlist"]
    assert all(item not in allowlist for item in shape["obsolete"])
    assert shape["smoke_parent"] not in allowlist
    assert "/var/lib/forge/projects" not in allowlist
    assert allowlist.count("/var/lib/forge-evidence") == 1
    assert receipt["permission_choices"]["add_evidence_permission"] is True
    assert receipt["permission_choices"]["retired_permissions"] == shape["obsolete"]
    coverage = receipt["registration_permission_coverage"]
    assert set(coverage) == set(shape["registrations"])
    assert all(item["before_authorized"] == item["after_authorized"] for item in coverage.values())
    for left, right, _path in shape["alias_pairs"]:
        assert coverage[left]["exact_aliases"] == [left, right]
        assert coverage[right]["exact_aliases"] == [left, right]
        assert coverage[left]["after_permissions"] == coverage[right]["after_permissions"]


@pytest.mark.parametrize(
    "disposition", ["omit-one", "unknown", "duplicate", "ambiguous", "malformed"]
)
def test_incorrect_retirement_dispositions_refuse_and_preserve_inputs(scenario, disposition):
    shape = configure_complete_actual_shape(scenario)
    command = scenario["command"]
    if disposition == "omit-one":
        index = command.index("--retire-permission")
        del command[index:index + 2]
    elif disposition == "unknown":
        command.extend(["--retire-permission", "/home/synthetic-owner/unknown"])
    elif disposition == "duplicate":
        command.extend(["--retire-permission", shape["obsolete"][0]])
    elif disposition == "ambiguous":
        child = shape["obsolete"][0] + "/child"
        settings = yaml.safe_load(scenario["settings_input"].read_text())
        settings["permissions"]["filesystem"]["allowlist"].append(child)
        scenario["settings_input"].write_text(yaml.safe_dump(settings, sort_keys=False))
        command.extend(["--retire-permission", child])
    else:
        command.extend(["--retire-permission", "relative/not-canonical"])
    before = {
        key: scenario[key].read_bytes()
        for key in ("settings_input", "env_file", "secret", "compose_file", "runtime")
    }

    result = run(scenario)

    assert result.returncode == 2
    assert all(scenario[key].read_bytes() == value for key, value in before.items())
    assert not any(path.exists() for path in scenario["outputs"].values())


@pytest.mark.parametrize("protected", ["registration", "evidence"])
def test_cannot_retire_permission_covering_registered_path_or_evidence(scenario, protected):
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    permission = (
        str(scenario["old_one"])
        if protected == "registration"
        else settings["permissions"]["filesystem"]["allowlist"][0]
    )
    assert permission in settings["permissions"]["filesystem"]["allowlist"]
    scenario["command"].extend(["--retire-permission", permission])

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


@pytest.mark.parametrize("permission", ["/var/forge", "/var/forge/receipts"])
def test_cannot_retire_proved_mapped_evidence_with_explicit_add(scenario, permission):
    configure_parent_mapped_evidence(scenario)
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    allowlist = settings["permissions"]["filesystem"]["allowlist"]
    if permission not in allowlist:
        allowlist.append(permission)
    scenario["settings_input"].write_text(yaml.safe_dump(settings, sort_keys=False))
    scenario["command"].extend(["--retire-permission", permission])
    previous = {name: f"previous-{name}\n".encode() for name in scenario["outputs"]}
    for name, path in scenario["outputs"].items():
        path.write_bytes(previous[name])

    result = run(scenario)

    assert result.returncode == 2
    assert all(path.read_bytes() == previous[name] for name, path in scenario["outputs"].items())


@pytest.mark.parametrize("permission", ["/var/forge", "/var/forge/receipts"])
def test_existing_exact_evidence_cannot_retire_runtime_mapped_path(scenario, permission):
    configure_existing_exact_mapped_evidence(scenario)
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["permissions"]["filesystem"]["allowlist"].append(permission)
    scenario["settings_input"].write_text(yaml.safe_dump(settings, sort_keys=False))
    scenario["command"].extend(["--retire-permission", permission])
    previous = {name: f"previous-{name}\n".encode() for name in scenario["outputs"]}
    for name, path in scenario["outputs"].items():
        path.write_bytes(previous[name])

    result = run(scenario)

    assert result.returncode == 2
    assert all(path.read_bytes() == previous[name] for name, path in scenario["outputs"].items())


def test_existing_exact_evidence_still_allows_unrelated_retirement(scenario):
    _, evidence_source = configure_existing_exact_mapped_evidence(scenario)
    obsolete = "/home/synthetic-obsolete-permission"
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["permissions"]["filesystem"]["allowlist"].append(obsolete)
    scenario["settings_input"].write_text(yaml.safe_dump(settings, sort_keys=False))
    scenario["command"].extend(["--retire-permission", obsolete])

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    allowlist = rendered["permissions"]["filesystem"]["allowlist"]
    assert obsolete not in allowlist
    assert str(evidence_source) not in allowlist
    assert allowlist.count("/var/lib/forge-evidence") == 1
    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert receipt["permission_choices"]["add_evidence_permission"] is False
    assert receipt["permission_choices"]["retired_permissions"] == [obsolete]


def test_existing_publication_enabled_policy_is_preserved(scenario):
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["publication"]["enabled"] = True
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    assert rendered["publication"]["enabled"] is True


@pytest.mark.parametrize(
    "publication",
    [None, [], {"enabled": False, "unknown_policy": True}],
    ids=("null", "list", "unknown-field"),
)
def test_explicit_malformed_or_unknown_publication_still_refuses(scenario, publication):
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["publication"] = publication
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_same_basename_from_different_source_paths_refuses(scenario):
    settings = yaml.safe_load(scenario["settings_input"].read_text())
    settings["planning"]["target_repo_paths"]["owner-a/repo"] = str(
        scenario["settings_input"].parent / "first-repo"
    )
    settings["planning"]["target_repo_paths"]["owner-b/repo"] = str(
        scenario["settings_input"].parent / "second-repo"
    )
    scenario["settings_input"].write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )

    result = run(scenario)

    assert result.returncode == 2
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_real_loader_preserves_choices_rewrites_only_contract_fields_and_is_idempotent(scenario):
    first = run(scenario)
    assert first.returncode == 0, first.stderr
    original = yaml.safe_load(scenario["settings_input"].read_text())
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    assert rendered["planning"]["enabled"] is False
    assert rendered["planning"]["target_repo_paths"] == {
        "example/project-one": "/var/lib/forge/projects/project-one",
        "synthetic/project-two": "/var/lib/forge/projects/project-two",
        "synthetic/project-three": "/var/lib/forge/projects/project-three",
        "synthetic/project-four": "/var/lib/forge/projects/project-four",
        "synthetic/project-five": "/var/lib/forge/projects/project-five",
        "synthetic-seed/seed-one": "/var/lib/forge/projects/seed-one",
        "synthetic-seed/seed-two": "/var/lib/forge/projects/seed-two",
        "synthetic-seed/seed-three": "/var/lib/forge/projects/seed-three",
    }
    assert set(rendered["planning"]["sandboxes"]) == {"example/project-one"}
    assert rendered["planning"]["sandboxes"]["example/project-one"]["sidecar_url"] == "${FORGE_SANDBOX_SIDECAR_URL}"
    assert rendered["deploy"]["enabled"] == original["deploy"]["enabled"]
    assert rendered["publication"]["enabled"] == original["publication"]["enabled"]
    assert rendered["publication"]["builds_may_run_inside_the_coordinator"] is False
    assert "/var/lib/deliberate-extra" in rendered["permissions"]["filesystem"]["allowlist"]
    assert scenario["settings_input"].read_text() == yaml.safe_dump(original, sort_keys=False)
    before = {name: path.read_bytes() for name, path in scenario["outputs"].items()}
    second = run(scenario)
    assert second.returncode == 0, second.stderr
    assert all(path.read_bytes() == before[name] for name, path in scenario["outputs"].items())
    assert "env unchanged; settings unchanged; receipt unchanged" in second.stdout


def test_plan_has_zero_effects_and_does_not_invoke_docker(scenario):
    result = run(scenario, "--plan")
    assert result.returncode == 0
    assert "PLAN 1" in result.stdout and "PLAN 5" in result.stdout
    assert not any(path.exists() for path in scenario["outputs"].values())
    assert not scenario["docker_log"].exists()


def test_empty_env_name_refuses_plainly_without_outputs(scenario):
    text = scenario["env_file"].read_text().replace("FLEET_MEMORY_ENABLED=true", "FLEET_MEMORY_ENABLED=")
    scenario["env_file"].write_text(text)
    result = run(scenario)
    assert result.returncode == 2
    assert result.stderr.startswith("REFUSED:") and "FLEET_MEMORY_ENABLED" in result.stderr
    assert "Traceback" not in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_host_environment_cannot_override_explicit_env_file_route(scenario):
    scenario["env"]["FACTORY_GATEWAY_ADDRESS"] = "203.0.113.99"
    result = run(scenario)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert receipt["routes"]["gateway_routes_match"] is True
    assert "203.0.113.99" not in scenario["outputs"]["settings"].read_text()


def test_secret_values_never_reach_outputs_or_diagnostics(scenario):
    result = run(scenario)
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    for path in scenario["outputs"].values():
        combined += path.read_text()
    assert "not-a-real-password" not in combined


def test_unacknowledged_registration_drift_refuses_and_names_exact_difference(scenario):
    index = scenario["command"].index("--ack-registration-drift")
    del scenario["command"][index:index + 2]
    result = run(scenario)
    assert result.returncode == 2
    assert "synthetic/project-two" in result.stderr and "acknowledgements" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_wrong_section_seven_route_refuses_before_writing(scenario):
    text = scenario["env_file"].read_text().replace(
        "FORGE_PUBLISHER_URL=http://forge-publisher:8711",
        "FORGE_PUBLISHER_URL=http://192.0.2.44:8711",
    )
    scenario["env_file"].write_text(text)
    result = run(scenario)
    assert result.returncode == 2
    assert "publisher service on its private network" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_invalid_settings_shape_refuses_without_child_traceback(scenario):
    payload = yaml.safe_load(scenario["settings_input"].read_text())
    payload["planning"] = []
    scenario["settings_input"].write_text(yaml.safe_dump(payload), encoding="utf-8")
    result = run(scenario)
    assert result.returncode == 2
    assert result.stderr.startswith("REFUSED:")
    assert "Traceback" not in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_rendered_compose_must_not_bind_an_old_registered_project(scenario):
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["coordinator"]["volumes"].append(
        {"type": "bind", "source": str(scenario["old_one"]), "target": "/old-project"}
    )
    scenario["compose_json"].write_text(json.dumps(compose))
    result = run(scenario)
    assert result.returncode == 2
    assert "binds an old registered project path" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


@pytest.mark.parametrize(
    ("output_flag", "alias_kind", "input_key"),
    [
        ("--receipt", "direct", "settings_input"),
        ("--env-output", "hardlink", "secret"),
        ("--settings-output", "symlink", "compose_file"),
        ("--receipt", "output-pair", "env"),
    ],
)
def test_outputs_cannot_alias_any_input_or_each_other_before_effects(
    scenario, output_flag, alias_kind, input_key
):
    flag_index = scenario["command"].index(output_flag) + 1
    original_inputs = {
        key: scenario[key].read_bytes()
        for key in ("settings_input", "env_file", "secret", "compose_file", "runtime")
    }
    target = scenario["outputs"][input_key] if input_key in scenario["outputs"] else scenario[input_key]
    output = Path(scenario["command"][flag_index])
    if alias_kind == "direct":
        scenario["command"][flag_index] = str(target)
    elif alias_kind == "hardlink":
        os.link(target, output)
    elif alias_kind == "symlink":
        output.symlink_to(target)
    else:
        scenario["command"][flag_index] = str(target)

    result = run(scenario)

    assert result.returncode == 2
    assert result.stderr.startswith("REFUSED:") and "alias" in result.stderr
    assert "Traceback" not in result.stderr
    assert not scenario["docker_log"].exists()
    assert all(scenario[key].read_bytes() == before for key, before in original_inputs.items())


def test_bus_client_port_drift_refuses_before_writing(scenario):
    scenario["env_file"].write_text(
        scenario["env_file"].read_text().replace(
            "JARVIS_NATS_URL=nats://bus:14222",
            "JARVIS_NATS_URL=nats://bus:14223",
        )
    )
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["front-door"]["environment"]["JARVIS_NATS_URL"] = "nats://bus:14223"
    compose["services"]["bus-gateway"]["environment"]["JARVIS_NATS_URL"] = "nats://bus:14223"
    scenario["compose_json"].write_text(json.dumps(compose))

    result = run(scenario)

    assert result.returncode == 2
    assert "declared internal bus host and client port" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_rendered_consumer_override_cannot_bypass_validated_route(scenario):
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["front-door"]["environment"]["JARVIS_NATS_URL"] = "nats://bus:14223"
    scenario["compose_json"].write_text(json.dumps(compose))

    result = run(scenario)

    assert result.returncode == 2
    assert "front-door does not consume the validated JARVIS_NATS_URL" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_ordered_compose_overlays_are_passed_to_actual_render(scenario):
    overlay = scenario["compose_file"].with_name("safety-overlay.yaml")
    overlay.write_text("services: {}\n")
    insert_at = scenario["command"].index("--project")
    scenario["command"][insert_at:insert_at] = ["--compose-file", str(overlay)]

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in scenario["docker_log"].read_text().splitlines()]
    compose_call = next(call for call in calls if call and call[0] == "compose")
    first = compose_call.index(str(scenario["compose_file"].resolve()))
    second = compose_call.index(str(overlay.resolve()))
    assert first < second
    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert [item["path"] for item in receipt["project_files"]] == [
        str(scenario["compose_file"].resolve()),
        str(overlay.resolve()),
    ]


def test_compose_file_env_authority_must_match_explicit_order(scenario):
    overlay = scenario["compose_file"].with_name("external.yaml")
    overlay.write_text("services: {}\n")
    with scenario["env_file"].open("a") as stream:
        stream.write("COMPOSE_FILE=compose.yaml:external.yaml\n")

    result = run(scenario)

    assert result.returncode == 2
    assert "differ from COMPOSE_FILE" in result.stderr
    assert not scenario["docker_log"].exists()
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_rendered_coordinator_helper_override_refuses_before_outputs(scenario):
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["coordinator"]["environment"]["FORGE_SANDBOX_SIDECAR_URL"] = (
        "http://192.0.2.99:19999"
    )
    scenario["compose_json"].write_text(json.dumps(compose))

    result = run(scenario)

    assert result.returncode == 2
    assert "coordinator does not consume the validated FORGE_SANDBOX_SIDECAR_URL" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_single_quoted_env_reference_refuses_before_docker_or_outputs(scenario):
    scenario["env_file"].write_text(
        scenario["env_file"].read_text().replace(
            "FORGE_SANDBOX_SIDECAR_URL=http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_SANDBOX_SIDECAR_PORT}",
            "FORGE_SANDBOX_SIDECAR_URL='http://${FACTORY_GATEWAY_ADDRESS}:${FORGE_SANDBOX_SIDECAR_PORT}'",
        )
    )

    result = run(scenario)

    assert result.returncode == 2
    assert "single-quotes an env reference" in result.stderr
    assert not scenario["docker_log"].exists()
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_existing_regular_file_output_parent_refuses_before_effects(scenario):
    blocker = scenario["outputs"]["env"].parent / "blocker"
    blocker.write_text("keep-me\n")
    index = scenario["command"].index("--settings-output") + 1
    scenario["command"][index] = str(blocker / "forge.yaml")

    result = run(scenario)

    assert result.returncode == 2
    assert "output parent" in result.stderr and "not a directory" in result.stderr
    assert blocker.read_text() == "keep-me\n"
    assert not scenario["docker_log"].exists()
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_output_ancestor_collision_refuses_before_effects(scenario):
    env_output = scenario["outputs"]["env"]
    index = scenario["command"].index("--settings-output") + 1
    scenario["command"][index] = str(env_output / "forge.yaml")

    result = run(scenario)

    assert result.returncode == 2
    assert "ancestor or child" in result.stderr
    assert not scenario["docker_log"].exists()
    assert not any(path.exists() for path in scenario["outputs"].values())



def test_answer_publication_must_match_declared_callback_port(scenario):
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["answer-service"]["ports"][0]["published"] = "18199"
    scenario["compose_json"].write_text(json.dumps(compose))

    result = run(scenario)

    assert result.returncode == 2
    assert "TCP publication does not match" in result.stderr
    assert not any(path.exists() for path in scenario["outputs"].values())


def test_explicit_matching_nondefault_answer_publication_is_valid(scenario):
    scenario["env_file"].write_text(
        scenario["env_file"].read_text().replace(
            "FORGE_ANSWER_PORT=18126", "FORGE_ANSWER_PORT=18199"
        )
    )
    compose = json.loads(scenario["compose_json"].read_text())
    compose["services"]["answer-service"]["ports"][0]["published"] = "18199"
    scenario["compose_json"].write_text(json.dumps(compose))

    result = run(scenario)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(scenario["outputs"]["receipt"].read_text())
    assert receipt["routes"]["answer_port"] == 18199


def test_interrupt_during_replacement_reports_possible_partial_output(scenario):
    wrapper = r"""
import os, runpy, signal, sys
script, *arguments = sys.argv[1:]
original = os.replace
calls = 0
def interrupted_replace(source, destination):
    global calls
    calls += 1
    if calls == 2:
        signal.raise_signal(signal.SIGINT)
    return original(source, destination)
os.replace = interrupted_replace
sys.argv = [script, *arguments]
runpy.run_path(script, run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", wrapper, *scenario["command"]],
        env=scenario["env"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "may already have been replaced" in result.stderr
    assert "no partial output" not in result.stderr
    assert scenario["outputs"]["env"].is_file()
    assert not scenario["outputs"]["settings"].exists()
    assert not scenario["outputs"]["receipt"].exists()
