"""``estate-check --publication-facts`` LOOKS, then writes what it found (TC8).

Release -3, upgrade runbook section 3, TC8 (b). The check answers the questions
publication needs that only the machine can answer, by asking Docker and the
sandbox tool, and writes one record into the publication-facts volume through
a network-less, read-only-root helper container. The coordinator reads that
record at every merge word (:mod:`forge.pipeline.publication_facts`).

Docker and the sandbox tool are stand-ins on PATH here: nothing real is
inspected, started or written. The helper container's writes land in a
temporary folder that plays the volume.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

import pytest

from forge.config.models import ForgeConfig
from forge.pipeline.publication_facts import (
    FACTS_FILE_ENV,
    ThisCoordinator,
    process_start,
    read_publication_facts,
)
from forge.pipeline.publication_switch import (
    publication_is_switched_on,
    why_publication_is_off,
)

ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / "deploy" / "estate" / "estate-check"
PROJECT = "codex-review"
COORDINATOR = "c" * 64
PUBLISHER = "d" * 64
OTHER = "e" * 64


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
if args[:1] == ["--host"]:
    args = args[2:]
case = os.environ.get("CASE", "good")
facts_dir = pathlib.Path(os.environ["FACTS_DIR"])
log = pathlib.Path(os.environ["DOCKER_LOG"])
with log.open("a") as f:
    f.write(json.dumps(args) + "\n")
P = os.environ["PROJECT"]
C, D, E = "c" * 64, "d" * 64, "e" * 64
cred = os.environ["CRED_FILE"]
started = os.environ["STARTED_AT"]

def mount(src, dst, rw=True, name=None, kind="volume"):
    m = {"Type": kind, "Source": src, "Destination": dst, "RW": rw}
    if name:
        m["Name"] = name
    return m

def coordinator():
    mounts = [
        mount(os.environ["LEDGER_SOURCE"], "/var/lib/forge", True, P + "_forge-ledger"),
        mount(os.environ["SETTINGS_SOURCE"], "/etc/forge", False, P + "_forge-settings"),
    ]
    if case != "facts_not_mounted":
        mounts.append(mount(os.environ["FACTS_SOURCE"], "/var/lib/forge-publication-facts",
                            case == "facts_mounted_rw", P + "_publication-facts"))
    seen = pathlib.Path(os.environ["DOCKER_LOG"]).with_suffix(".coordinator-inspections")
    times = int(seen.read_text()) + 1 if seen.exists() else 1
    seen.write_text(str(times))
    pid, when = int(os.environ["COORD_PID"]), started
    if case == "restart_while_looking" and times > 1:
        pid, when = int(os.environ["COORD_PID_AFTER"]), os.environ["STARTED_AT_AFTER"]
    state = "garbage" if case == "coordinator_state_garbage" else {"Running": True, "StartedAt": when, "Pid": pid}
    return {"Id": C, "Name": "/" + P + "-coordinator-1", "Image": "sha256:" + "a" * 64,
            "State": state, "Mounts": mounts}

def publisher():
    ports = {"8711/tcp": [{"HostPort": "8711"}]} if case == "publisher_publishes" else {}
    networks = {P + "_forge-publisher-net": {"IPAddress": "192.0.2.9"}}
    if case == "second_network_reachable":
        networks["somebody-elses-net"] = {"IPAddress": "198.51.100.7"}
    mode = "host" if case == "publisher_host_mode" else P + "_forge-publisher-net"
    return {"Id": D, "Name": "/" + P + "-forge-publisher-1",
            "HostConfig": {"PortBindings": ports, "NetworkMode": mode},
            "NetworkSettings": {"Networks": networks},
            "Mounts": [mount(cred, "/etc/forge-publisher/credential", False, kind="bind"),
                       mount(os.environ["LEDGER_SOURCE"], "/var/lib/forge", False, P + "_forge-ledger")]}

def other():
    mounts = [mount("/srv/other", "/data", True, kind="bind")]
    if case == "credential_mounted_elsewhere":
        mounts.append(mount(str(pathlib.Path(cred).parent), "/secrets", False, kind="bind"))
    if case == "facts_held_elsewhere":
        mounts.append(mount(os.environ["FACTS_SOURCE"], "/facts", True, P + "_publication-facts"))
    return {"Id": E, "Name": "/" + P + "-answer-service-1", "Mounts": mounts}

BY_ID = {C: coordinator, "coordinator": coordinator, D: publisher, "forge-publisher": publisher, E: other}

if args[0] == "info":
    print("fixture")
elif args[0] == "compose":
    if "config" in args:
        bridge = "fpb" + hashlib.sha256(P.encode()).hexdigest()[:12]
        volumes = {"forge-ledger": {"name": P + "_forge-ledger"}}
        if case != "release_two_compose":
            volumes["publication-facts"] = {"name": P + "_publication-facts"}
        print(json.dumps({"name": P, "volumes": volumes,
            "networks": {"forge-publisher-net": {"name": P + "_forge-publisher-net", "driver": "bridge",
                "enable_ipv6": False, "driver_opts": {"com.docker.network.bridge.name": bridge}}},
            "services": {"coordinator": {"networks": {"factory": {}, "forge-publisher-net": {}},
                                         "environment": {"FORGE_PUBLISHER_URL": "http://forge-publisher:8711"}},
                         "forge-publisher": {"volumes": [{"type": "bind", "source": os.environ["REVIEW_SETTINGS_FILE"],
                             "target": "/etc/forge-publisher/settings.json", "read_only": True}],
                             "networks": {"forge-publisher-net": {}},
                             "healthcheck": {"test": ["CMD", "curl", "http://localhost:8711/healthz"]}}}}))
        raise SystemExit(0)
    service = args[-1]
    if service in ("coordinator", "forge-publisher"):
        print(service)
elif args[:2] == ["volume", "inspect"]:
    raise SystemExit(0 if case != "no_volume" else 1)
elif args[:2] == ["network", "inspect"]:
    members = {C: {"Name": P + "-coordinator-1"}, D: {"Name": P + "-forge-publisher-1"}}
    if case == "extra_member":
        members[E] = {"Name": P + "-answer-service-1"}
    print(json.dumps([{"Containers": members}]))
elif args[0] == "inspect":
    print(json.dumps([BY_ID[i]() for i in args[1:]]))
elif args[:2] == ["ps", "-aq"]:
    print("\n".join([C, D, E]))
elif args[0] == "exec":
    print("200")
elif args[0] == "run":
    script = args[-1]
    stdin = sys.stdin.read()
    assert "--network" in args and args[args.index("--network") + 1] == "none", args
    assert "--read-only" in args, args
    assert args[args.index("--cap-drop") + 1] == "ALL", args
    assert args[args.index("--security-opt") + 1] == "no-new-privileges", args
    assert "-v" in args and args[args.index("-v") + 1] == P + "_publication-facts:/facts", args
    if case == "helper_fails":
        raise SystemExit(1)
    target = facts_dir / "publication-facts.json"
    if "cat >" in script:
        target.write_text(stdin)
        target.chmod(0o644)
    elif "invalidated" in script and target.exists():
        target.rename(facts_dir / "publication-facts.json.invalidated")
else:
    raise SystemExit(1)
'''

FAKE_SBX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
case = os.environ.get("CASE", "good")
if args[:2] == ["ls", "--json"]:
    if case == "sbx_unreadable":
        raise SystemExit(1)
    workspaces = ["/home/someone/project", "/home/someone/forge:ro"]
    if case == "workspace_reaches_settings":
        workspaces.append(os.path.dirname(os.environ["SETTINGS_SOURCE"]) + ":ro")
    if case == "workspace_reaches_credential":
        workspaces.append(os.path.dirname(os.environ["CRED_FILE"]))
    if case == "workspace_reaches_facts":
        workspaces.append(os.path.dirname(os.environ["FACTS_SOURCE"]) + ":ro")
    if case == "workspace_links_to_settings":
        workspaces.append(os.environ["LINK_TO_SETTINGS"])
    status = "stopped" if case == "nothing_running" else "running"
    print(json.dumps({"sandboxes": [
        {"name": "project-deploy", "status": status, "workspaces": workspaces},
        {"name": "old-one", "status": "stopped", "workspaces": ["/home/someone/old"]},
    ]}))
elif args[0] == "exec":
    rest = args[args.index("--") + 1:]
    if rest[:3] == ["docker", "ps", "-aq"]:
        print("inner1")
    elif rest[:2] == ["docker", "inspect"]:
        mounts = [{"Source": "/home/someone/project", "Destination": "/app"}]
        if case == "inner_container_sees_ledger":
            mounts.append({"Source": os.environ["LEDGER_SOURCE"], "Destination": "/l"})
        print(json.dumps([{"Name": "/inner1", "Mounts": mounts}]))
    elif rest[0] == "curl":
        url = rest[-1]
        if "/recorded?" in url:
            print("000" if case == "sandbox_cannot_make_requests" else "200")
        elif ":8711/" in url:
            direct = "--noproxy" in rest
            if case == "sandbox_reaches" and direct:
                print("200")
            elif case.startswith("direct_") and direct and "192.0.2.9" in url:
                print(case.split("_")[1])
            elif case == "proxy_answers_for_itself" and not direct:
                print("403")
            elif case == "second_network_reachable" and direct and "198.51.100.7" in url:
                print("200")
            else:
                print("000")
        else:
            print("000")
    else:
        raise SystemExit(1)
else:
    raise SystemExit(1)
'''

FAKE_SUDO = r'''#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
if a[:1] == ["-n"]:
    os.execvp(a[1], a[1:])
assert a[0] == "--" and a[1] == "/usr/local/libexec/forge-publisher-host-policy" and a[2] == "verify"
if os.environ.get("CASE") == "policy_missing":
    print("publisher-host-policy: REFUSED current kernel policy missing")
    raise SystemExit(1)
print("VERIFIED daemon=fixture bridge=fixture drop_packets=1 drop_bytes=60")
'''


@pytest.fixture(scope="module")
def pid_ones():
    """The coordinator's PID 1, stood in for by a real process so its kernel
    start is read exactly as the check and the coordinator read it; and a
    second one, standing in for the coordinator after a restart. Started a
    little over a second before any check runs, because the record's time is
    in whole seconds and must be later than the start."""
    first = subprocess.Popen(["sleep", "600"])
    time.sleep(0.05)
    second = subprocess.Popen(["sleep", "600"])
    time.sleep(1.1)
    yield first, second
    first.kill()
    second.kill()


@pytest.fixture()
def look(tmp_path: Path, pid_ones):
    if shutil.which("python3") is None:
        pytest.skip("the check needs python3")
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(tools / "docker", FAKE_DOCKER)
    _executable(tools / "sbx", FAKE_SBX)
    _executable(tools / "sudo", FAKE_SUDO)
    facts_dir = tmp_path / "the-facts-volume"
    facts_dir.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "publisher-credential"
    credential.write_text("not-a-real-credential")
    credential.chmod(0o600)
    settings_file = tmp_path / "publisher.json"
    settings_file.write_text('{"host":"0.0.0.0","port":8711}')
    env_file = tmp_path / "probe.env"
    bridge = "fpb" + hashlib.sha256(PROJECT.encode()).hexdigest()[:12]
    env_file.write_text(
        f"""BUS_MODE=external
COMPOSE_PROFILES=
COMPOSE_FILE=compose.yaml:compose.external-bus.yaml
FORGE_PUBLISHER_URL=http://forge-publisher:8711
FORGE_PUBLISHER_BRIDGE={bridge}
FORGE_PUBLISHER_SETTINGS_FILE={settings_file}
FORGE_PUBLISHER_UID={os.getuid()}
FACTORY_GATEWAY_ADDRESS=127.0.0.1
FORGE_ANSWER_PORT=8126
"""
    )
    settings_dir = tmp_path / "volumes" / "settings" / "_data"
    ledger_dir = tmp_path / "volumes" / "ledger" / "_data"
    facts_source = tmp_path / "volumes" / "facts" / "_data"
    for folder in (settings_dir, ledger_dir, facts_source):
        folder.mkdir(parents=True)
    link = tmp_path / "an-innocent-looking-link"
    link.symlink_to(settings_dir)
    first, second = pid_ones
    started_epoch = int(time.time()) - 3600
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started_epoch)) + ".123456789Z"
    log = tmp_path / "docker.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tools}:{env['PATH']}",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "FACTS_DIR": str(facts_dir),
            "DOCKER_LOG": str(log),
            "PROJECT": PROJECT,
            "CRED_FILE": str(credential),
            "STARTED_AT": started_at,
            "STARTED_AT_AFTER": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".5Z",
            "COORD_PID": str(first.pid),
            "COORD_PID_AFTER": str(second.pid),
            "SETTINGS_SOURCE": str(settings_dir),
            "LEDGER_SOURCE": str(ledger_dir),
            "FACTS_SOURCE": str(facts_source),
            "LINK_TO_SETTINGS": str(link),
            "REVIEW_SETTINGS_FILE": str(settings_file),
        }
    )
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)

    def run(case: str = "good", **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(CHECK), "--env-file", str(env_file), "--project", PROJECT,
             "--publication-facts"],
            text=True, capture_output=True, env=env | {"CASE": case} | extra, timeout=60,
        )

    def record() -> dict:
        return json.loads((facts_dir / "publication-facts.json").read_text())

    run.coordinator_pid = first.pid  # type: ignore[attr-defined]
    run.restarted_pid = second.pid  # type: ignore[attr-defined]
    return run, record, facts_dir, started_epoch, credential


def test_a_clean_machine_writes_every_answer_publication_needs(look) -> None:
    run, record, facts_dir, started_epoch, _ = look
    done = run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASSED" in done.stdout
    facts = record()
    assert facts["format"] == "forge-publication-facts/1"
    assert facts["coordinator_container_id"] == COORDINATOR
    assert facts["compose_project"] == PROJECT
    assert facts["written_at_epoch"] > started_epoch
    assert facts["coordinator_started_at_epoch"] == pytest.approx(started_epoch + 0.123456)
    assert facts["machine"] == {
        "a_sandbox_can_write_the_coordinators_settings_file": False,
        "a_sandbox_can_see_the_ledger": False,
        "a_sandbox_can_reach_the_publisher": False,
        "only_the_coordinator_is_on_the_publishers_network": True,
        "the_credential_file_can_be_read_by_them": False,
    }
    assert "not-a-real-credential" not in json.dumps(facts) + done.stdout + done.stderr


def test_what_the_check_writes_is_what_the_coordinator_acts_on(look, tmp_path: Path) -> None:
    """The connected result: the writer's record turns publication on through
    the coordinator's own reader, for that coordinator, after its start."""
    run, _, facts_dir, started_epoch, _ = look
    assert run().returncode == 0
    config = ForgeConfig.model_validate(
        {
            "permissions": {"filesystem": {"allowlist": ["/tmp"]}},
            "publication": {
                "enabled": True,
                "publisher_url": "http://forge-publisher:8711",
                "builds_may_run_inside_the_coordinator": False,
                "publisher_credential_file": "/etc/forge-publisher/credential",
            },
        }
    )
    environ = {FACTS_FILE_ENV: str(facts_dir / "publication-facts.json")}

    def as_seen_by(pid: int) -> ThisCoordinator:
        boot, ticks, per_second = process_start(Path("/proc"), str(pid))
        return ThisCoordinator(COORDINATOR, boot + ticks / per_second, boot, ticks, per_second)

    this_one = functools.partial(
        read_publication_facts, environ=environ,
        who_is_asking=lambda: as_seen_by(run.coordinator_pid),
    )
    assert publication_is_switched_on(config, this_one)
    recorded = json.loads((facts_dir / "publication-facts.json").read_text())
    assert recorded["coordinator_pid1_start"]["start_ticks"] == as_seen_by(run.coordinator_pid).start_ticks

    restarted = functools.partial(
        read_publication_facts, environ=environ,
        who_is_asking=lambda: as_seen_by(run.restarted_pid),
    )
    assert not publication_is_switched_on(config, restarted)
    assert "not the one running now" in why_publication_is_off(config, restarted)


def test_a_restart_while_looking_writes_nothing(look) -> None:
    """Codex R1: the start is re-read immediately before the write; a
    coordinator that restarted during the look gets no record at all."""
    run, _, facts_dir, _, _ = look
    done = run("restart_while_looking")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "restarted (or stopped) while this check was looking" in done.stderr
    assert not (facts_dir / "publication-facts.json").exists()


def test_an_earlier_record_is_invalidated_before_looking(look) -> None:
    run, _, facts_dir, _, _ = look
    (facts_dir / "publication-facts.json").write_text('{"an": "older pass"}')
    done = run("helper_fails")
    assert done.returncode == 2
    # the helper refused, so nothing moved; and with a working helper:
    done = run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads((facts_dir / "publication-facts.json.invalidated").read_text()) == {
        "an": "older pass"
    }


@pytest.mark.parametrize(
    "case, name, value",
    [
        ("workspace_reaches_settings", "a_sandbox_can_write_the_coordinators_settings_file", True),
        ("inner_container_sees_ledger", "a_sandbox_can_see_the_ledger", True),
        ("sandbox_reaches", "a_sandbox_can_reach_the_publisher", True),
        ("nothing_running", "a_sandbox_can_reach_the_publisher", None),
        ("sandbox_cannot_make_requests", "a_sandbox_can_reach_the_publisher", None),
        ("policy_missing", "a_sandbox_can_reach_the_publisher", None),
        ("extra_member", "only_the_coordinator_is_on_the_publishers_network", False),
        ("publisher_publishes", "only_the_coordinator_is_on_the_publishers_network", False),
        ("credential_mounted_elsewhere", "the_credential_file_can_be_read_by_them", True),
        ("workspace_reaches_credential", "the_credential_file_can_be_read_by_them", True),
        ("workspace_links_to_settings", "a_sandbox_can_write_the_coordinators_settings_file", True),
        ("second_network_reachable", "a_sandbox_can_reach_the_publisher", True),
        ("second_network_reachable", "only_the_coordinator_is_on_the_publishers_network", False),
        ("direct_401", "a_sandbox_can_reach_the_publisher", True),
        ("direct_404", "a_sandbox_can_reach_the_publisher", True),
        ("direct_503", "a_sandbox_can_reach_the_publisher", True),
        ("publisher_host_mode", "a_sandbox_can_reach_the_publisher", True),
        ("publisher_host_mode", "only_the_coordinator_is_on_the_publishers_network", False),
    ],
)
def test_each_answer_comes_from_looking(look, case: str, name: str, value) -> None:
    run, record, _, _, _ = look
    done = run(case)
    assert done.returncode == 1, done.stdout + done.stderr
    assert record()["machine"][name] is value
    assert "NOT PASSED" in done.stdout


def test_a_credential_others_can_read_is_found(look) -> None:
    run, record, _, _, credential = look
    credential.chmod(0o644)
    done = run()
    assert done.returncode == 1
    assert record()["machine"]["the_credential_file_can_be_read_by_them"] is True


@pytest.mark.parametrize(
    "case, words",
    [
        ("release_two_compose", "declare no publication-facts volume"),
        ("no_volume", "does not exist yet"),
        ("facts_not_mounted", "does not mount"),
        ("facts_mounted_rw", "does not mount"),
        ("facts_held_elsewhere", "also mount(s)"),
        ("workspace_reaches_facts", "reaches the facts volume"),
        ("sbx_unreadable", "did not list its sandboxes"),
        ("coordinator_state_garbage", "before any record was written"),
    ],
)
def test_nothing_is_written_when_there_is_nowhere_to_write(look, case: str, words: str) -> None:
    run, _, facts_dir, _, _ = look
    done = run(case)
    assert done.returncode == 2, done.stdout + done.stderr
    assert words in done.stderr
    assert not (facts_dir / "publication-facts.json").exists()


def test_a_proxy_answering_for_itself_is_not_the_publisher(look) -> None:
    """By the default route only the publisher's own 200 counts: a proxy that
    refuses the request (403) has not put the sandbox through to it."""
    run, record, _, _, _ = look
    done = run("proxy_answers_for_itself")
    assert done.returncode == 0, done.stdout + done.stderr
    assert record()["machine"]["a_sandbox_can_reach_the_publisher"] is False


def test_a_credential_owned_by_another_user_is_found(look) -> None:
    run, record, _, _, _ = look
    done = run(FORGE_PUBLISHER_UID=str(os.getuid() + 1))
    assert done.returncode == 1, done.stdout + done.stderr
    assert record()["machine"]["the_credential_file_can_be_read_by_them"] is True
    assert "is owned by uid" in done.stdout

