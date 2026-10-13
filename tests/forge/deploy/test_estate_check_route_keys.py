"""estate-check item 6b: one route-keys folder, named the same in both places.

13 October 2026, the publisher's per-route credentials. The publisher mounts
every route's own key from FORGE_PUBLISHER_ROUTE_KEYS_DIR; the coordinator's
isolation check protects the folder its settings record as
publication.publisher_route_keys_dir. Item 6b compares the two, and refuses a
publisher whose routes name keys while the coordinator records no folder.

NOTHING REAL IS RUN. ``docker``, ``sbx`` and ``sudo`` are stand-ins first on
PATH: docker renders a small bundle and answers the one read of the
coordinator's settings with what the test says the settings hold. No
container, volume, image, sandbox or kernel policy is touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[3] / "deploy" / "estate" / "estate-check"

_STAND_IN_DOCKER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
text = " ".join(args)
if args[:1] == ["version"]:
    print("27.0.0"); sys.exit(0)
if args[:1] == ["compose"]:
    if "version" in args:
        print("2.30.0"); sys.exit(0)
    if args[-1] == "config" and "--format" not in args:
        print("name: probe\nservices: {}\nvolumes:\n  forge-ledger:\n    name: probe_forge-ledger\n"
              "  forge-settings:\n    name: probe_forge-settings\n")
        sys.exit(0)
    sys.exit(1)
if args[:2] == ["system", "info"]:
    print(""); sys.exit(0)
if args[:1] in (["image"], ["manifest"], ["volume"]):
    sys.exit(0)
if args[:1] == ["run"] and "publisher_route_keys_dir" in text:
    assert "--network none" in text, text
    assert "probe_forge-settings:/etc/forge:ro" in text, text
    assert "the-coordinator-image" in text, text
    print(os.environ["STAND_IN_SETTINGS_SAY"])
    sys.exit(0)
sys.exit(1)
'''


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def check(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(tools / "docker", _STAND_IN_DOCKER)
    _executable(tools / "sbx", "#!/bin/sh\nexit 1\n")
    _executable(tools / "sudo", "#!/bin/sh\nexit 1\n")
    folder = tmp_path / "route-keys"
    folder.mkdir(mode=0o700)

    def run(*, coordinator_says: str, routes: dict | None = None, env_dir: str | None = str(folder)):
        publisher = tmp_path / "publisher-settings.json"
        publisher.write_text(json.dumps({"projects": routes or {}}), encoding="utf-8")
        env_file = tmp_path / "probe.env"
        lines = [
            "BUS_MODE=local",
            "COMPOSE_PROFILES=local-bus",
            "COMPOSE_FILE=compose.yaml",
            "FORGE_IMAGE=the-coordinator-image",
            f"FORGE_PUBLISHER_SETTINGS_FILE={publisher}",
        ]
        if env_dir is not None:
            lines.append(f"FORGE_PUBLISHER_ROUTE_KEYS_DIR={env_dir}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("DOCKER_", "FORGE_", "COMPOSE_", "BUS_"))
        }
        env["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
        env["STAND_IN_SETTINGS_SAY"] = coordinator_says
        done = subprocess.run(
            ["bash", str(CHECK), "--env-file", str(env_file), "host"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        line = [row for row in done.stdout.splitlines() if " 6b " in row]
        assert len(line) == 1, done.stdout + done.stderr
        return line[0]

    return run, folder


def test_the_same_folder_in_both_places_passes(check) -> None:
    run, folder = check
    said = run(coordinator_says=f"SET={folder}", routes={"o/p": {"credential_name": "k"}})
    assert said.lstrip().startswith("ok"), said
    assert str(folder) in said


def test_a_trailing_slash_is_the_same_folder(check) -> None:
    run, folder = check
    assert run(coordinator_says=f"SET={folder}/").lstrip().startswith("ok")


def test_two_different_folders_fail(check) -> None:
    run, folder = check
    said = run(coordinator_says=f"SET={folder}2")
    assert said.lstrip().startswith("NOT PASSED"), said
    assert f"{folder}2" in said and "same folder" in said


def test_routes_with_keys_and_no_coordinator_setting_fail(check) -> None:
    run, _folder = check
    said = run(coordinator_says="SET=", routes={"o/p": {"credential_name": "k"}, "o/q": {}})
    assert said.lstrip().startswith("NOT PASSED"), said
    assert "1 of the publisher's routes" in said


def test_no_route_keys_and_no_coordinator_setting_pass_with_a_note(check) -> None:
    run, _folder = check
    said = run(coordinator_says="SET=", routes={"o/q": {}})
    assert said.lstrip().startswith("ok"), said
    assert "before any route names one" in said


def test_unreadable_coordinator_settings_are_not_a_pass(check) -> None:
    run, _folder = check
    said = run(coordinator_says="NO-FILE")
    assert said.lstrip().startswith("not checked"), said


def test_an_env_file_without_the_folder_fails(check) -> None:
    run, _folder = check
    said = run(coordinator_says="SET=/x", env_dir=None)
    assert said.lstrip().startswith("NOT PASSED"), said
    assert "FORGE_PUBLISHER_ROUTE_KEYS_DIR" in said
