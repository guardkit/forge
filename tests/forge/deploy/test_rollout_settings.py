"""Behavioral boundary tests for the standalone rollout-settings command.

The fake Docker boundary executes the candidate's transformer with this release image's
real ``forge.config.loader``. A separate operational receipt exercises real Compose.
"""

from __future__ import annotations

import json
import os
import subprocess
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
        "seed/seed-two": str(old_two),
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
        "ROLLOUT_TEST_COMPOSE_JSON": str(tmp_path / "compose.json"),
        "ROLLOUT_TEST_DOCKER_LOG": str(tmp_path / "docker.log"),
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
                "environment": {"FORGE_NATS_URL": "nats://forge:not-a-real-password@bus:14222"},
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
                "networks": {"factory": None},
                "ports": [{"host_ip": "192.0.2.44", "published": "18005", "target": 8005}],
            },
            "memory-relay": {
                "image": IMAGE,
                "environment": {"FLEET_MEMORY_BUS_ADDRESS": "nats://bus:14222"},
                "networks": {"factory": None},
            },
            "front-door": {
                "image": IMAGE,
                "environment": {"JARVIS_NATS_URL": "nats://bus:14222"},
                "networks": {"factory": None},
            },
            "bus-gateway": {
                "image": IMAGE,
                "environment": {"JARVIS_NATS_URL": "nats://bus:14222"},
                "networks": {"factory": None},
            },
            "bus-ready": {
                "image": IMAGE,
                "environment": {"BUS_MONITORING_ADDRESS": "bus:18222"},
                "networks": {"factory": None},
            },
        }
    }
    Path(public_values["ROLLOUT_TEST_COMPOSE_JSON"]).write_text(json.dumps(compose))
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
with open(os.environ["ROLLOUT_TEST_DOCKER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["image", "inspect"]:
    print(args[-1])
    raise SystemExit(0)
if args and args[0] == "compose":
    print(Path(os.environ["ROLLOUT_TEST_COMPOSE_JSON"]).read_text())
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
        "--ack-registration-drift", "seed/seed-two",
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
        "compose_json": Path(public_values["ROLLOUT_TEST_COMPOSE_JSON"]),
        "docker_log": Path(public_values["ROLLOUT_TEST_DOCKER_LOG"]),
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


def test_real_loader_preserves_choices_rewrites_only_contract_fields_and_is_idempotent(scenario):
    first = run(scenario)
    assert first.returncode == 0, first.stderr
    original = yaml.safe_load(scenario["settings_input"].read_text())
    rendered = yaml.safe_load(scenario["outputs"]["settings"].read_text())
    assert rendered["planning"]["enabled"] is False
    assert rendered["planning"]["target_repo_paths"] == {
        "example/project-one": "/var/lib/forge/projects/project-one",
        "seed/seed-two": "/var/lib/forge/projects/seed-two",
    }
    assert rendered["planning"]["sandboxes"]["example/project-one"]["sidecar_url"] == "${FORGE_SANDBOX_SIDECAR_URL}"
    assert rendered["deploy"]["enabled"] == original["deploy"]["enabled"]
    assert rendered["publication"]["enabled"] == original["publication"]["enabled"]
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
    assert "seed/seed-two" in result.stderr and "acknowledgements" in result.stderr
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
