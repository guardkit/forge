"""Whole-document JSON ingress, with file-only transports and an inert socket."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WATCH = ROOT / "deploy/estate/provisioner/gateway-watch.sh"
FIXTURES = Path(__file__).parent / "gateway-watch-fixtures"
HEARTBEAT = b'{"state":"connected","last_event_at":"2026-09-26T15:59:55Z"}'
BUS = b'{"connections":[{"authorized_user":"jarvis","name":"bus-gateway-factory","subscriptions_list":["agents.command.jarvis"]}]}'
PROJECT = b'{"Config":{"Labels":{"com.docker.compose.project":"owned"}}}'
CONTAINERS = b'[{"Id":"fakeid"}]'
VALID = {"bus-file": BUS, "bus-http": BUS, "project": PROJECT, "containers": CONTAINERS}


def run_ingress(tmp_path: Path, target: str, body: bytes, *, read_status: int = 0,
                parser_failure: bool = False, extractor_failure: bool = False) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, value in (("bus", BUS), ("project", PROJECT), ("containers", CONTAINERS)):
        (tmp_path / name).write_bytes(value)
    (tmp_path / ("bus" if target.startswith("bus-") else target)).write_bytes(body)
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_bytes(HEARTBEAT)
    alerts = tmp_path / "alerts"
    trace = tmp_path / "trace"
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}",
               JARVIS_NATS_USER="jarvis", GATEWAY_WATCH_CLIENT_NAME="bus-gateway-factory",
               GATEWAY_WATCH_HEARTBEAT_PATH=str(heartbeat), GATEWAY_WATCH_NOTIFIER="file",
               GATEWAY_WATCH_NOTIFIER_FILE=str(alerts), BUS_MONITORING_ADDRESS="stub.invalid",
               GATEWAY_WATCH_DOCKER_SOCKET=str(tmp_path / "fake.sock"),
               GATEWAY_WATCH_CONTAINER="", TEST_ROOT=str(tmp_path), TEST_TARGET=target,
               TEST_READ_STATUS=str(read_status), TEST_FIXTURES=str(FIXTURES))
    # No HTTP request is made. The socket only satisfies the watch's -S guard.
    curl = fake_bin / "curl"
    curl.write_text(r'''#!/bin/sh
case "$*" in
  */logs\?*) printf 'logs\n' >> "$TEST_ROOT/trace"; cat "$TEST_FIXTURES/log-fresh.txt"; exit ;;
  *http://localhost/containers/json*) kind=containers ;;
  *http://localhost/containers/*/json*) kind=project ;;
  *http://stub.invalid/connz\?*) kind=bus ;;
  *) exit 99 ;;
esac
printf '%s\n' "$kind" >> "$TEST_ROOT/trace"
cat "$TEST_ROOT/$kind"
if [ "$kind" = "$TEST_TARGET" ] || [ "$TEST_TARGET" = bus-http ]; then exit "$TEST_READ_STATUS"; fi
''')
    curl.chmod(0o755)
    if target == "bus-file" and read_status:
        real_cat = shutil.which("cat")
        cat = fake_bin / "cat"
        cat.write_text(f'''#!/bin/sh
"{real_cat}" "$@"
status=$?
if [ "$1" = "$TEST_ROOT/bus" ]; then exit "$TEST_READ_STATUS"; fi
exit "$status"
''')
        cat.chmod(0o755)
    if parser_failure or extractor_failure:
        real_jq = shutil.which("jq")
        jq = fake_bin / "jq"
        jq.write_text(f'''#!/bin/sh
"{real_jq}" "$@"
status=$?
if [ "$1" = {"-er" if extractor_failure else "-Rse"} ]; then exit 1; fi
exit "$status"
''')
        jq.chmod(0o755)
    args = ["bash", str(WATCH), "--once", "--now", "1790438400"]
    if target != "bus-http":
        args += ["--connz-file", str(tmp_path / "bus")]
    if target.startswith("bus-"):
        args += ["--log-file", str(FIXTURES / "log-fresh.txt")]
    with socket.socket(socket.AF_UNIX) as inert:
        inert.bind(str(tmp_path / "fake.sock"))
        done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
    messages = [json.loads(line)["text"] for line in alerts.read_text().splitlines()] if alerts.exists() else []
    calls = trace.read_text().splitlines() if trace.exists() else []
    return done, messages, calls


@pytest.mark.parametrize("target", VALID)
@pytest.mark.parametrize("case", ["positive", "whitespace", "embedded-nul", "terminal-nul",
                                  "trailing-junk", "second-document", "truncated-second",
                                  "empty", "read-failure", "parser-failure", "extractor-failure"])
def test_json_ingress_requires_complete_bytes_and_success(tmp_path: Path, target: str, case: str) -> None:
    body = VALID[target]
    if case == "whitespace":
        body = b" \n\t" + body + b"\r\n"
    elif case == "embedded-nul":
        marker = b"gateway-factory" if target.startswith("bus-") else b"owned" if target == "project" else b"fakeid"
        body = body.replace(marker, marker[:2] + b"\0" + marker[2:])
    elif case == "terminal-nul":
        body += b"\0"
    elif case == "trailing-junk":
        body += b"junk"
    elif case == "second-document":
        body += b"\n" + body
    elif case == "truncated-second":
        body += b'\n{"unfinished":'
    elif case == "empty":
        body = b""
    done, messages, calls = run_ingress(tmp_path, target, body,
        read_status=1 if case == "read-failure" else 0,
        parser_failure=case == "parser-failure",
        extractor_failure=case == "extractor-failure")
    healthy = case in {"positive", "whitespace"}
    component = "bus connection   " if target.startswith("bus-") else "recent activity  "
    assert done.returncode == (0 if healthy else 10), done.stdout + done.stderr
    assert component + ("ok" if healthy else "unknown") in done.stdout
    assert len(messages) == (0 if healthy else 1)
    assert "ignored null byte" not in done.stderr
    if not healthy and not target.startswith("bus-"):
        assert "logs" not in calls, "failed discovery must not fetch another container's log"
        if target == "project":
            assert "containers" not in calls


@pytest.mark.parametrize("target,body", [
    ("bus-file", b'{"connections":{}}'),
    ("bus-http", b'{"connections":"not-an-array"}'),
    ("project", b'{"Config":{"Labels":{"com.docker.compose.project":7}}}'),
    ("project", PROJECT.replace(b"owned", b"owned\\u0000")),
    ("project", PROJECT.replace(b"owned", b"owned\\n")),
    ("containers", b'[{"Id":7}]'),
    ("containers", CONTAINERS.replace(b"fakeid", b"fakeid\\u0000")),
    ("containers", CONTAINERS.replace(b"fakeid", b"fakeid\\n")),
])
def test_json_ingress_rejects_wrong_types_and_unsafe_identifiers(tmp_path: Path, target: str, body: bytes) -> None:
    done, messages, calls = run_ingress(tmp_path, target, body)
    assert done.returncode == 10, done.stdout + done.stderr
    assert len(messages) == 1
    if not target.startswith("bus-"):
        assert "recent activity  unknown" in done.stdout
        assert "logs" not in calls


@pytest.mark.parametrize("subscriptions", [
    {"subscriptions_list": "agents.command.jarvis"},
    {"subscriptions_list": [7, "agents.command.jarvis"]},
    {"subscriptions_list": False},
    {"subscriptions_list_detail": {"subject": "agents.command.jarvis"}},
    {"subscriptions_list_detail": [{"subject": 7}, {"subject": "agents.command.jarvis"}]},
    {"subscriptions_list_detail": ["agents.command.jarvis"]},
])
def test_bus_identity_requires_typed_subscription_lists(tmp_path: Path, subscriptions: dict) -> None:
    connection = {"authorized_user": "jarvis", "name": "bus-gateway-factory", **subscriptions}
    done, messages, _ = run_ingress(tmp_path, "bus-file", json.dumps({"connections": [connection]}).encode())
    assert done.returncode == 10, done.stdout + done.stderr
    assert "bus connection   unknown" in done.stdout
    assert len(messages) == 1


@pytest.mark.parametrize("subscriptions,verdict", [
    ({"subscriptions_list": ["agents.command.jarvis"]}, "ok"),
    ({"subscriptions_list_detail": [{"subject": "agents.command.jarvis"}]}, "ok"),
    ({"subscriptions_list": []}, "lost"),
    ({"subscriptions_list_detail": []}, "lost"),
    ({}, "lost"),
    ({"subscriptions_list": ["some.other.subject"]}, "lost"),
])
def test_bus_typed_subscription_controls(tmp_path: Path, subscriptions: dict, verdict: str) -> None:
    connection = {"authorized_user": "jarvis", "name": "bus-gateway-factory", **subscriptions}
    done, messages, _ = run_ingress(tmp_path, "bus-file", json.dumps({"connections": [connection]}).encode())
    assert done.returncode == (0 if verdict == "ok" else 10), done.stdout + done.stderr
    assert "bus connection   " + verdict in done.stdout
    assert len(messages) == (0 if verdict == "ok" else 1)
