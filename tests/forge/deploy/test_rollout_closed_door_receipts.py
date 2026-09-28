from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / "deploy" / "estate" / "estate-check"
IMAGE = "sha256:1eaa3360b280cadebb308363aa2bfae06ddb852b25b1b8c7cd603cfa62ec0316"


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def probe(tmp_path: Path):
    if shutil.which("jq") is None:
        pytest.skip("receipt parsing requires jq or the release provisioning image")
    tools = tmp_path / "tools"
    tools.mkdir()
    _executable(
        tools / "docker",
        r"""#!/usr/bin/env python3
import os, sys, json, hashlib
args=sys.argv[1:]
if args[:1]==['--host']: args=args[2:]
text=' '.join(args); mode=os.environ.get('PROBE_CASE','good')
if args[0]=='info': print('fixture')
elif args[:2]==['context','inspect']:print(json.dumps([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}]))
elif args[0]=='compose':
    if 'config' in args:
        project='codex-review'; bridge='fpb'+hashlib.sha256(project.encode()).hexdigest()[:12]
        print(json.dumps({'name':project,'networks':{'forge-publisher-net':{'name':project+'_forge-publisher-net','driver':'bridge','enable_ipv6':False,'driver_opts':{'com.docker.network.bridge.name':bridge}}},'services':{'coordinator':{'networks':{'factory':{},'forge-publisher-net':{}},'environment':{'FORGE_PUBLISHER_URL':'http://forge-publisher:8711'}},'forge-publisher':{'volumes':[{'type':'bind','source':os.environ['REVIEW_SETTINGS_FILE'],'target':'/etc/forge-publisher/settings.json','read_only':True}],'networks':{'forge-publisher-net':{}},'healthcheck':{'test':['CMD','curl','http://localhost:8711/healthz']}}}}))
        raise SystemExit(0)
    service=args[-1]
    if service in ('coordinator','answer-service','memory-relay','forge-publisher'): print(service)
    if service=='front-door' and mode=='producer': print('unexpected-front-door')
elif args[:2]==['image','inspect']: print(os.environ['REVIEW_IMAGE'])
elif args[0]=='inspect':
    if '.Image' in text: print(os.environ['REVIEW_IMAGE'])
    elif 'StartedAt' in text: print(os.environ.get('MOCK_STARTED_AT','2026-09-27T00:00:00Z'))
    elif 'com.docker.compose.project' in text: print('codex-review')
    elif 'IPAddress' in text: print('192.0.2.2')
    elif 'NetworkSettings' in text: print('codex-review_factory')
elif args[0]=='network': pass
elif args[0]=='logs': print('memory: ON')
elif args[0]=='run':
    if 'WANTED=' in text: print('UNREADABLE malformed response' if mode=='counts_unreadable' else 'QUIET in account own-test')
    else: print('jq: parse error: Invalid numeric literal' if mode=='storage_unreadable' else 'ALL 8 streams and 4 key-value buckets')
elif args[0]=='exec':
    if 'test -f' in text: pass
    elif '/recorded?' in text: print('{"recorded": false} HTTP 200')
    elif 'publisher' in text and args[1]=='answer-service': print('000')
    else: print('200')
else: raise SystemExit(1)
""",
    )
    _executable(tools / "sbx", "#!/bin/sh\ncase \"$1\" in ls) echo codex-owned-fake-sandbox;; exec) echo 200;; *) exit 1;; esac\n")
    _executable(tools / "sudo", r"""#!/usr/bin/env python3
import os, sys, pathlib
assert os.getuid()==1000, 'must exercise ordinary operator sudo caller'
a=sys.argv[1:]; assert a[0]=='--' and a[1]=='/usr/local/libexec/forge-publisher-host-policy' and a[2]=='verify'
assert a[a.index('--docker-host')+1]=='unix:///var/run/docker.sock'
assert a[a.index('--project')+1]=='codex-review'
assert '--require-members' in a
if os.environ.get('POLICY_CASE') in ('missing','changed'):
    print('publisher-host-policy: REFUSED current kernel policy '+os.environ['POLICY_CASE'])
    raise SystemExit(1)
p=pathlib.Path(__file__).parent/'counter';n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
print('VERIFIED daemon=fixture bridge=fixture drop_packets='+str(n)+' drop_bytes=240')
""")
    _executable(tools / "curl", "#!/bin/sh\nprintf '000'\nexit 28\n")
    state = tmp_path / "state"
    state.mkdir()
    settings_file = tmp_path / "publisher.json"
    settings_file.write_text('{"host":"0.0.0.0","port":8711}')
    env_file = tmp_path / "probe.env"
    env_file.write_text(
        f"""BUS_MODE=external
FORGE_IMAGE=review-release
BUS_EXTERNAL_NETWORK=codex-own-network
BUS_MONITORING_ADDRESS=127.0.0.1:44583
FORGE_NATS_URL=nats://127.0.0.1:37883
ROLLOUT_STATE_DIR={state}
ROLLOUT_BUS_STREAM=PIPELINE
ROLLOUT_BUS_CONSUMERS=forge-serve forge-serve-planning
NATS_PROVISION_IMAGE=mocked-not-executed
BUS_SOURCE_VOLUME=mocked-not-mounted
FLEET_MEMORY_URL=http://mock-memory
MODEL_SEAT_URL=http://mock-model
FORGE_PUBLISHER_URL=http://forge-publisher:8711
FORGE_PUBLISHER_BRIDGE=fpb{__import__('hashlib').sha256(b'codex-review').hexdigest()[:12]}
FORGE_PUBLISHER_SETTINGS_FILE={settings_file}
SANDBOX_NAME=codex-owned-fake-sandbox
FACTORY_GATEWAY_ADDRESS=127.0.0.1
FORGE_ANSWER_PORT=8126
COMPOSE_PROFILES=
COMPOSE_FILE=compose.yaml:compose.external-bus.yaml
"""
    )
    env = os.environ.copy()
    env["PATH"] = f"{tools}:{env['PATH']}"
    env["REVIEW_IMAGE"] = IMAGE
    env["REVIEW_SETTINGS_FILE"] = str(settings_file)
    env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"): env.pop(key, None)

    def run(mode: str = "good", read: bool = False, **extra: str):
        call_env = env | {"PROBE_CASE": mode} | extra
        return subprocess.run(
            ["bash", str(CHECK), "--env-file", str(env_file), "--project", "codex-review",
             "--read-pre-resume" if read else "--pre-resume"],
            text=True, capture_output=True, env=call_env, timeout=30,
        )

    return run, state, env_file, env


def test_positive_receipt_and_strict_reader(probe) -> None:
    run, state, _, _ = probe
    made = run()
    assert made.returncode == 0, made.stdout + made.stderr
    assert not list(state.glob(".pre-resume.json.*")), "atomic temp receipt was left behind"
    read = run(read=True)
    assert read.returncode == 0, read.stdout + read.stderr

    receipt = state / "pre-resume.json"
    receipt.write_text(receipt.read_text() + "\nmalformed trailing JSON\n")
    malformed = run(read=True)
    assert malformed.returncode == 1
    assert "not one complete, valid receipt" in malformed.stdout

    assert run().returncode == 0
    data = json.loads(receipt.read_text())
    data["compose_project"] = ""
    receipt.write_text(json.dumps(data))
    assert run(read=True).returncode == 1

    assert run().returncode == 0
    data = json.loads(receipt.read_text())
    data["items"].pop()
    receipt.write_text(json.dumps(data))
    assert run(read=True).returncode == 1


def test_unknowns_are_recorded_as_unknown_and_refused(probe) -> None:
    run, state, _, _ = probe
    for mode, item in (("storage_unreadable", "8b"), ("counts_unreadable", "10")):
        result = run(mode)
        assert result.returncode == 1, result.stdout + result.stderr
        receipt = json.loads((state / "pre-resume.json").read_text())
        assert receipt["verdict"] == "passed-with-items-not-checked"
        row = next(row for row in receipt["items"] if row["item"] == item)
        assert row["verdict"] == "not-checked"
        refused = run(read=True)
        assert refused.returncode == 1
        assert "unknown is not a pass" in refused.stdout


def test_failed_recheck_and_unknown_start_time_cannot_reuse_a_pass(probe) -> None:
    run, state, _, _ = probe
    assert run().returncode == 0
    state.chmod(0o555)
    try:
        failed = run("producer")
        assert failed.returncode == 1, failed.stdout + failed.stderr
        refused = run(read=True)
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert "MAY BE ACTED ON" not in refused.stdout
    finally:
        state.chmod(0o755)

    assert run().returncode == 0
    for started_at in ("", " ", "unreadable"):
        unreadable = run(read=True, MOCK_STARTED_AT=started_at)
        assert unreadable.returncode == 1
        assert "start time could not be read" in unreadable.stdout


def test_wholly_unwritable_store_refuses_without_claiming_a_failed_check(probe) -> None:
    run, state, _, _ = probe
    assert run().returncode == 0
    assert run(read=True).returncode == 0
    receipt = state / "pre-resume.json"
    saved = receipt.read_bytes()
    receipt.chmod(0o444)
    state.chmod(0o555)
    try:
        blocked = run("producer")
        assert blocked.returncode == 2, blocked.stdout + blocked.stderr
        assert "DID NOT START" in blocked.stderr
        assert "no service item was checked" in blocked.stderr
        assert receipt.read_bytes() == saved
        refused = run(read=True)
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert "store is unusable" in refused.stdout
        assert "not itself a later failed check" in refused.stdout
    finally:
        state.chmod(0o755)
        receipt.chmod(0o644)

    # The blocked attempt never reached an item, so restoring the store makes
    # the still-current pass readable again; a subsequent real check supersedes it.
    assert run(read=True).returncode == 0
    assert run().returncode == 0


def test_host_bus_mode_success_does_not_abort_on_a_helper_local(probe, tmp_path: Path) -> None:
    _, _, _, base_env = probe
    for mode, profiles, files in (
        ("external", "", "compose.yaml:compose.external-bus.yaml"),
        ("local", "local-bus", "compose.yaml"),
    ):
        env_file = tmp_path / f"host-{mode}.env"
        env_file.write_text(
            f"""BUS_MODE={mode}
COMPOSE_PROFILES={profiles}
COMPOSE_FILE={files}
BUS_EXTERNAL_NETWORK=codex-own-network
BUS_MONITORING_ADDRESS=127.0.0.1:44583
FORGE_NATS_URL=nats://127.0.0.1:37883
FACTORY_GATEWAY_ADDRESS=127.0.0.1
"""
        )
        done = subprocess.run(
            ["bash", str(CHECK), "--env-file", str(env_file), "host"],
            text=True, capture_output=True, env=base_env, timeout=30,
        )
        assert done.returncode == 1, done.stdout + done.stderr
        assert "unbound variable" not in done.stderr
        assert "ok            7b" in done.stdout, done.stdout


@pytest.mark.parametrize("policy_case", ["missing", "changed"])
def test_consumed_passing_receipt_refuses_current_policy_loss(probe, policy_case):
    run, state, _, _ = probe
    assert run().returncode == 0
    saved = (state / "pre-resume.json").read_bytes()
    result = run(read=True, POLICY_CASE=policy_case)
    assert result.returncode == 1
    assert "current kernel policy " + policy_case in result.stdout
    assert (state / "pre-resume.json").read_bytes() == saved


@pytest.mark.parametrize("name,value", [("DOCKER_CONTEXT","foreign"),("DOCKER_HOST","tcp://remote:2375"),("DOCKER_TLS_VERIFY","1")])
def test_privilege_boundary_does_not_hide_client_endpoint_conflicts(probe, name, value):
    run, state, _, _ = probe
    assert run().returncode == 0
    assert run(read=True, **{name:value}).returncode == 1
