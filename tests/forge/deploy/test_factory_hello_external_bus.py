from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ESTATE = Path(__file__).resolve().parents[3] / "deploy" / "estate"


def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "fake bin"
    bindir.mkdir()
    log = tmp_path / "docker argv.jsonl"
    docker = bindir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
with pathlib.Path(os.environ["FAKE_DOCKER_LOG"]).open("a") as f:
    f.write(json.dumps(args)+"\\n")
if args and args[0]=="compose":
    if os.environ.get("FAKE_COMPOSE_FAIL"):
        print("service bus-ready depends on undefined service nats-provision",file=sys.stderr)
        raise SystemExit(19)
    stop=args.index("ps")
    check=subprocess.run([os.environ["REAL_DOCKER"],*args[:stop],"config","--services"],capture_output=True,text=True,env=os.environ)
    if check.returncode:
        sys.stderr.write(check.stderr);raise SystemExit(check.returncode)
    if not os.environ.get("FAKE_NO_CID"): print("made-up-coordinator-id")
    raise SystemExit(0)
if args and args[0]=="exec":
    sys.stdin.read();raise SystemExit(0)
raise SystemExit(97)
"""
    )
    docker.chmod(0o755)
    return bindir, log


def estate_env(tmp_path: Path, mode: str, *, profiles: str = "") -> Path:
    env_file = tmp_path / "estate inputs with spaces.env"
    text = (ESTATE / ".env.example").read_text()
    text = text.replace("BUS_MODE=local", f"BUS_MODE={mode}")
    text = text.replace("COMPOSE_PROFILES=local-bus", f"COMPOSE_PROFILES={profiles}")
    if mode.strip("'\"") == "external":
        text = text.replace(
            "COMPOSE_FILE=compose.yaml\n",
            "COMPOSE_FILE=compose.yaml:compose.external-bus.yaml\n",
        )
    env_file.write_text(text)
    return env_file


def invoke(tmp_path: Path, env_file: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bindir, log = fake_docker(tmp_path)
    environment = {
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "DOCKER_HOST": "unix:///nonexistent",
        "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", ""),
        "REAL_DOCKER": shutil.which("docker") or "docker",
        "FAKE_DOCKER_LOG": str(log),
        **extra,
    }
    result = subprocess.run(
        ["bash", str(ESTATE / "factory-hello"), "--env-file", str(env_file), "--project", "hello-check"],
        cwd=ESTATE,
        env=environment,
        capture_output=True,
        text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


@pytest.mark.parametrize("mode", ["external", '"external"', "'external'"])
def test_env_only_external_mode_renders_with_the_overlay(tmp_path: Path, mode: str) -> None:
    result, calls = invoke(tmp_path, estate_env(tmp_path, mode))
    assert result.returncode == 0, result.stderr
    compose = calls[0]
    assert compose[:2] == ["compose", "--env-file"]
    assert compose[2].endswith("estate inputs with spaces.env")
    files = [compose[i + 1] for i, value in enumerate(compose) if value == "-f"]
    assert files == [str(ESTATE / "compose.yaml"), str(ESTATE / "compose.external-bus.yaml")]


def test_local_mode_renders_only_the_base_file(tmp_path: Path) -> None:
    result, calls = invoke(tmp_path, estate_env(tmp_path, "local", profiles="local-bus"))
    assert result.returncode == 0, result.stderr
    compose = calls[0]
    assert [compose[i + 1] for i, value in enumerate(compose) if value == "-f"] == [str(ESTATE / "compose.yaml")]


def test_inherited_nonempty_bus_mode_wins_over_the_file(tmp_path: Path) -> None:
    result, calls = invoke(tmp_path, estate_env(tmp_path, "local"), BUS_MODE="external")
    assert result.returncode == 0, result.stderr
    files = [calls[0][i + 1] for i, value in enumerate(calls[0]) if value == "-f"]
    assert files[-1] == str(ESTATE / "compose.external-bus.yaml")


def test_render_failure_is_not_reported_as_a_missing_container(tmp_path: Path) -> None:
    result, _ = invoke(tmp_path, estate_env(tmp_path, "external"), FAKE_COMPOSE_FAIL="1")
    assert result.returncode == 2
    assert "could not be read at all" in result.stderr
    assert "problem with the files or the env file" in result.stderr
    assert "coordinator is not running" not in result.stderr


def test_successful_render_with_no_container_keeps_the_distinct_exit(tmp_path: Path) -> None:
    result, _ = invoke(tmp_path, estate_env(tmp_path, "external"), FAKE_NO_CID="1")
    assert result.returncode == 1
    assert "estate reads fine" in result.stderr
    assert "coordinator is not running" in result.stderr
