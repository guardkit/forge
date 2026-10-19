"""One sandbox per project, stage S2: the sandbox starter.

Design: one-sandbox-per-project (17 October 2026), sections 2.3 and 2.3a.

``deploy/compose/sandbox-starter/starter.py`` starts, stops and holds a
project's supervisor container and reports its state. These tests drive it
with fake ``docker`` and ``sbx`` programs (first on PATH, and named to the
starter by their full paths, so the real sandbox client is never reached) and
a fake helper and runner served from this process. No real sandbox, engine or
daemon is touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
STARTER_PY = ROOT / "deploy" / "compose" / "sandbox-starter" / "starter.py"
COMPOSE = ROOT / "deploy" / "compose" / "compose.sandbox-starter.yaml"

_spec = importlib.util.spec_from_file_location("forge_sandbox_starter_under_test", STARTER_PY)
starter_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = starter_mod
_spec.loader.exec_module(starter_mod)

NAME = "a-private-project"
SUPERVISOR = f"forge-sbx-{NAME}-sandbox-runner-1"
DEPLOYMENT = {"compose_project": "notes-live", "identity_label": "org.example.identity=abc123"}

FAKE_DOCKER = r"""#!/usr/bin/env bash
S="$FAKE_STATE"
printf '%s\n' "$*" >> "$S/docker.log"
case "$1" in
  inspect)
    c="${@: -1}"
    started=$(cat "$S/started_at" 2>/dev/null || echo 2026-10-19T08:00:00.000000001Z)
    if [ -f "$S/inspect_running_for" ]; then
      n=$(cat "$S/inspect_running_for")
      if [ "$n" -gt 0 ]; then echo $((n-1)) > "$S/inspect_running_for"; echo "running|$started"; exit 0; fi
    fi
    [ -f "$S/container" ] || { echo "Error: No such object: $c" >&2; exit 1; }
    state=$(cat "$S/container")
    [ -n "$state" ] && echo "$state|$started" ;;
  start)
    # Started while a stop is still under way: the thing that must never happen.
    if [ "$(cat "$S/inspect_running_for" 2>/dev/null || echo 0)" -gt 0 ] ||
       [ "$(cat "$S/vm_stops_after" 2>/dev/null || echo 0)" -gt 0 ]; then
      echo "started while the stop was still under way" >> "$S/midstop"
    fi
    [ -f "$S/start_sleep" ] && sleep "$(cat "$S/start_sleep")"
    [ -f "$S/start_fails" ] && { echo "Error response from daemon: it would not start" >&2; exit 1; }
    # A start of a stopped container is a new run of it; of a running one, nothing.
    if [ "$(cat "$S/container" 2>/dev/null)" != running ]; then
      date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$S/started_at"
    fi
    echo running > "$S/container" ;;
  stop)
    [ -f "$S/stop_sleep" ] && sleep "$(cat "$S/stop_sleep")"
    if [ -f "$S/stop_fails" ]; then
      # The answer is lost; the shutdown it started goes on (or never ends).
      [ -f "$S/stop_never_exits" ] || echo exited > "$S/container"
      echo "Error: context deadline exceeded" >&2; exit 1
    fi
    echo exited > "$S/container" ;;
  wait) echo 0 ;;
  *) echo "unexpected docker $*" >&2; exit 3 ;;
esac
"""

FAKE_SBX = r"""#!/usr/bin/env bash
S="$FAKE_STATE"
printf '%s\n' "$*" >> "$S/sbx.log"
case "$1" in
  ls)
    [ -f "$S/signed_out" ] && { echo "no valid user session: run sbx login" >&2; exit 1; }
    c=$(cat "$S/container" 2>/dev/null || echo missing)
    if [ -f "$S/vm_override" ]; then state=$(cat "$S/vm_override")
    elif [ "$c" = running ] || [ "$c" = paused ]; then state=running
    else
      n=$(cat "$S/vm_stops_after" 2>/dev/null || echo 0)
      if [ "$n" -gt 0 ]; then echo $((n-1)) > "$S/vm_stops_after"; state=running; else state=stopped; fi
    fi
    printf 'SANDBOX              AGENT   STATUS    PORTS\n'
    printf 'api-test-deploy      shell   running   127.0.0.1:8924->8124/tcp\n'
    printf '%s    shell   %s\n' "$FAKE_SANDBOX" "$state" ;;
  exec)
    shift 2
    if [ "$1 $2" = "docker ps" ]; then
      [ -f "$S/exec_fails" ] && { echo "the engine did not answer" >&2; exit 1; }
      [ -f "$S/ps" ] && cat "$S/ps"
    fi
    exit 0 ;;
  *) echo "unexpected sbx $*" >&2; exit 3 ;;
esac
"""


class FakeServices:
    """The helper's /healthz and /busy and the runner's /ok, on one port."""

    def __init__(self):
        self.answers = {
            "/healthz": (200, '{"status": "healthy", "instance": "helper-run-1"}'),
            "/ok": (200, '{"ok":true,"name":"runner"}'),
            "/busy": (200, '{"executing": []}'),
        }
        services = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                status, body = services.answers.get(self.path, (404, "not here"))
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeClock:
    """A clock that moves only when the starter sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return 1_790_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds
            self.slept.append(seconds)
        time.sleep(0.001)

    def wait(self, event, seconds: float) -> bool:
        if event.wait(0.001):
            return True
        with self._lock:
            self.now += seconds
        return False


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(FAKE_DOCKER)
    (fake_bin / "sbx").write_text(FAKE_SBX)
    for name in ("docker", "sbx"):
        (fake_bin / name).chmod(0o755)
    state = tmp_path / "fake-state"
    state.mkdir()
    (state / "container").write_text("exited\n")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_SANDBOX", NAME)
    services = FakeServices()

    class World:
        pass

    w = World()
    w.tmp = tmp_path
    w.bin = fake_bin
    w.state = state
    w.services = services
    w.starters = []
    w.servers = []

    def settings(deployment=None, **env):
        table = {NAME: {"helper_url": services.url, "runner_url": services.url}}
        if deployment:
            table[NAME]["deployment"] = deployment
        settings_file = tmp_path / "sandboxes.json"
        settings_file.write_text(json.dumps(table))
        base = {
            "STARTER_LISTEN_ADDRESS": "127.0.0.1",
            "STARTER_LISTEN_PORT": "0",
            "STARTER_SANDBOXES": NAME,
            "STARTER_SETTINGS_FILE": str(settings_file),
            "STARTER_STATE_DIR": str(tmp_path / "starter-state"),
            "STARTER_DOCKER": str(fake_bin / "docker"),
            "STARTER_SBX": str(fake_bin / "sbx"),
            "STARTER_READY_LIMIT_SECONDS": "3",
            "STARTER_READY_POLL_SECONDS": "0.05",
            "STARTER_RECOVERY_POLL_SECONDS": "0.05",
            "STARTER_CLIENT_TIMEOUT_SECONDS": "20",
        }
        base.update(env)
        return starter_mod.Settings(base)

    def make(deployment=None, clock=None, **env):
        kwargs = {"say": lambda line: None}
        if clock is not None:
            kwargs.update(clock=clock, wall=clock.wall, sleep=clock.sleep, wait=clock.wait)
        s = starter_mod.Starter(settings(deployment, **env), **kwargs)
        s.boot()
        w.starters.append(s)
        return s

    def http(starter):
        server = starter_mod.serve(starter, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        w.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def lines(name):
        path = state / name
        return [line for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    w.settings = settings
    w.make = make
    w.http = http
    w.lines = lines
    yield w
    for server in w.servers:
        server.shutdown()
        server.server_close()
    for s in w.starters:
        s.close()
    services.close()


def call(base: str, method: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(base + path, method=method, data=b"" if method != "GET" else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def in_background(fn, *args, **kwargs):
    box: dict = {}

    def run():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    box["thread"] = thread
    return box


def wait_for(predicate, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def log_entries(world) -> list[dict]:
    path = world.tmp / "starter-state" / f"{NAME}.jsonl"
    entries = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line still being written
    return entries


# ---------------------------------------------------------------------------


class TestSettings:
    def test_api_test_deploy_in_the_list_is_refused_at_boot(self, world):
        with pytest.raises(ValueError, match="api-test-deploy"):
            world.settings(STARTER_SANDBOXES=f"{NAME} api-test-deploy")

    def test_a_listed_name_without_addresses_is_refused(self, world):
        with pytest.raises(ValueError, match="no entry"):
            world.settings(STARTER_SANDBOXES=f"{NAME},another-project")

    def test_the_vm_poll_is_never_below_five_seconds(self, world):
        assert world.settings(STARTER_VM_POLL_SECONDS="1").vm_poll == 5.0

    def test_the_compose_file_never_composes_up_and_holds_what_it_declares(self):
        text = COMPOSE.read_text()
        assert "/opt/forge/sandbox-starter/starter.py" in text
        assert "/var/run/docker.sock:/var/run/docker.sock" in text
        assert "/usr/bin/sbx:ro" in text
        assert "sandboxd.sock" in text
        assert 'network_mode: "host"' in text
        source = STARTER_PY.read_text()
        assert '"compose"' not in source and "'compose'" not in source

    def test_the_release_image_carries_the_program(self):
        assert (
            "COPY deploy/compose/sandbox-starter/starter.py /opt/forge/sandbox-starter/starter.py"
            in (ROOT / "Dockerfile").read_text()
        )


class TestNamesItActsFor:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/status/api-test-deploy"),
            ("POST", "/start/api-test-deploy"),
            ("POST", "/stop/api-test-deploy"),
            ("POST", "/hold/api-test-deploy?owner=estate-check&until=services"),
            ("POST", "/ready/api-test-deploy"),
            ("POST", "/suspend/api-test-deploy?reason=x"),
            ("POST", "/clear/api-test-deploy"),
        ],
    )
    def test_api_test_deploy_is_refused_by_name(self, world, method, path):
        base = world.http(world.make())
        status, body = call(base, method, path)
        assert status == 403
        assert "api-test-deploy is the shared sandbox" in body["message"]
        assert world.lines("docker.log") == [] and world.lines("sbx.log") == []

    def test_a_name_not_in_the_list_is_not_found(self, world):
        base = world.http(world.make())
        status, body = call(base, "POST", "/start/someone-else")
        assert status == 404 and "not in this starter's list" in body["message"]
        assert world.lines("docker.log") == []


class TestStart:
    def test_start_checks_the_sign_in_then_docker_starts_the_supervisor(self, world):
        base = world.http(world.make())
        status, body = call(base, "POST", f"/start/{NAME}")
        assert status == 200, body
        assert body["readiness"] == "ready" and body["redeploy_pending"] is False
        assert world.lines("sbx.log") == ["ls"]
        assert f"start {SUPERVISOR}" in world.lines("docker.log")
        assert not any("compose" in line for line in world.lines("docker.log"))

    def test_with_a_recorded_deployment_it_reports_a_redeploy_pending(self, world):
        base = world.http(world.make(deployment=DEPLOYMENT))
        status, body = call(base, "POST", f"/start/{NAME}")
        assert status == 200
        assert body["readiness"] == "services ready, redeploy pending"
        status, report = call(base, "GET", f"/status/{NAME}")
        assert report["redeploy_pending"] is True and report["services_ready"] is True

    def test_a_deployment_passed_in_on_the_request_counts(self, world):
        base = world.http(world.make())
        status, body = call(
            base, "POST",
            f"/start/{NAME}?compose_project=notes-live&identity_label=org.example.identity%3Dabc123",
        )
        assert status == 200 and body["redeploy_pending"] is True

    def test_a_lapsed_sign_in_refuses_the_start_plainly(self, world):
        (world.state / "signed_out").write_text("")
        base = world.http(world.make())
        status, body = call(base, "POST", f"/start/{NAME}")
        assert status == 503
        assert "sbx login" in body["message"]
        assert not any(line.startswith("start") for line in world.lines("docker.log"))

    def test_a_helper_that_never_answers_is_named(self, world):
        world.services.answers["/healthz"] = (503, '{"status": "starting"}')
        base = world.http(world.make())
        status, body = call(base, "POST", f"/start/{NAME}")
        assert status == 503
        assert "the deploy helper (/healthz)" in body["message"]
        assert "build runner" not in body["message"]

    def test_a_docker_start_that_fails_says_so(self, world):
        (world.state / "start_fails").write_text("")
        base = world.http(world.make())
        status, body = call(base, "POST", f"/start/{NAME}")
        assert status == 503 and "docker start" in body["message"]


class TestStatus:
    def test_status_reports_everything_and_never_calls_sbx(self, world):
        base = world.http(world.make(deployment=DEPLOYMENT))
        call(base, "POST", f"/start/{NAME}")
        call(base, "POST", f"/hold/{NAME}?owner=register-repo&until=services")
        before = world.lines("sbx.log")
        status, report = call(base, "GET", f"/status/{NAME}")
        assert status == 200
        assert world.lines("sbx.log") == before
        assert report["container"] == {"name": SUPERVISOR, "state": "running"}
        assert report["vm"]["state"] == "running" and report["vm"]["seen_at"]
        assert report["operations"]["queued"] == [] and report["operations"]["running"] is None
        assert report["operations"]["recovering"] == []
        assert report["operations"]["last"]["outcome"] == "done"
        assert [h["owner"] for h in report["holds"]] == ["register-repo"]
        assert report["holds"][0]["age_seconds"] >= 0
        assert report["managed"] == {"state": "active", "reason": None}
        assert report["services_ready"] is True
        assert report["readiness"] == "services ready, redeploy pending"
        assert report["deployment"] == DEPLOYMENT

    def test_the_operation_log_is_numbered_json_lines(self, world):
        base = world.http(world.make())
        call(base, "POST", f"/start/{NAME}")
        call(base, "POST", f"/stop/{NAME}")
        call(base, "POST", f"/start/{NAME}")
        entries = log_entries(world)
        assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
        queued = [(e["op_id"], e["op"]) for e in entries if e["kind"] == "op-queued"]
        assert queued == [(1, "start"), (2, "stop"), (3, "start")]
        assert [e["outcome"] for e in entries if e["kind"] == "op-end"] == ["done"] * 3


class TestStop:
    def test_a_stop_lists_the_engine_stops_the_supervisor_and_polls_sbx_ls(self, world):
        clock = FakeClock()
        s = world.make(clock=clock)
        s.start(NAME)
        (world.state / "vm_stops_after").write_text("3")
        result = s.stop(NAME)
        assert "is stopped" in result["message"]
        docker = world.lines("docker.log")
        assert f"stop -t 210 {SUPERVISOR}" in docker
        assert f"wait {SUPERVISOR}" in docker
        sbx = world.lines("sbx.log")
        assert sbx.count(f"exec {NAME} docker ps --format {{{{.Names}}}}\t{{{{.Labels}}}}") == 1
        # One ls for the start's sign-in, then four during the stop: running
        # three times, then stopped, five seconds apart and never faster.
        assert sbx.count("ls") == 5
        assert clock.slept.count(5.0) == 3
        assert s.status(NAME)["readiness"] == "stopped"

    def test_the_exec_happens_before_the_stop_and_only_while_running(self, world):
        s = world.make()
        s.start(NAME)
        s.stop(NAME)
        sbx = world.lines("sbx.log")
        assert sbx.index(f"exec {NAME} docker ps --format {{{{.Names}}}}\t{{{{.Labels}}}}") < len(sbx) - 1
        s.stop(NAME)  # already stopped: one ls, no exec, no docker stop
        assert world.lines("sbx.log")[len(sbx):] == ["ls"]
        assert sum(1 for line in world.lines("docker.log") if line.startswith("stop")) == 1

    def test_a_vm_that_never_stops_fails_after_the_bound(self, world):
        clock = FakeClock()
        s = world.make(clock=clock, STARTER_VM_STOP_LIMIT_SECONDS="30")
        s.start(NAME)
        (world.state / "vm_stops_after").write_text("1000")
        with pytest.raises(starter_mod.Unresolved, match="unresolved"):
            s.stop(NAME)
        assert world.lines("sbx.log").count("ls") <= 1 + 30 // 5 + 1
        assert "VM still showed running" in s.status(NAME)["operations"]["unresolved"]

    def test_a_hold_refuses_the_stop(self, world):
        base = world.http(world.make())
        call(base, "POST", f"/hold/{NAME}?owner=estate-check&until=services")
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409 and "held open" in body["message"] and "estate-check" in body["message"]
        assert not any(line.startswith("stop") for line in world.lines("docker.log"))

    def test_suspended_refuses_the_stop(self, world):
        base = world.http(world.make())
        call(base, "POST", f"/start/{NAME}")
        call(base, "POST", f"/suspend/{NAME}?reason=the%20redeploy%20failed%20its%20health%20check")
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409
        assert "managed: suspended, reason: the redeploy failed its health check" in body["message"]

    def test_an_operation_running_refuses_the_stop(self, world):
        (world.state / "start_sleep").write_text("1")
        base = world.http(world.make())
        box = in_background(call, base, "POST", f"/start/{NAME}")
        assert wait_for(lambda: world.starters[0].sandboxes[NAME].running is not None)
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409 and "queued or running" in body["message"]
        box["thread"].join(10)
        assert box["result"][0] == 200

    def test_a_busy_helper_refuses_the_stop(self, world):
        base = world.http(world.make())
        call(base, "POST", f"/start/{NAME}")
        world.services.answers["/busy"] = (200, '{"executing": [{"route": "/preview", "since": "x"}]}')
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409 and "still executing 1 request" in body["message"]
        assert not any(line.startswith("stop") for line in world.lines("docker.log"))

    @pytest.mark.parametrize(
        "answer",
        [(404, "not here"), (200, "not json"), (200, '{"other": 1}'), (200, "[]"),
         (200, '{"executing": "nothing"}'), (500, '{"executing": []}')],
    )
    def test_a_helper_that_does_not_answer_busy_refuses_the_stop(self, world, answer):
        base = world.http(world.make())
        call(base, "POST", f"/start/{NAME}")
        world.services.answers["/busy"] = answer
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409 and "did not answer GET /busy" in body["message"]
        assert not any(line.startswith("stop") for line in world.lines("docker.log"))

    def test_an_unreachable_helper_refuses_the_stop(self, world):
        s = world.make()
        s.start(NAME)
        world.services.close()
        with pytest.raises(starter_mod.Refused, match="did not answer GET /busy"):
            s.stop(NAME)

    def test_a_leftover_build_stack_refuses_the_stop_by_name(self, world):
        (world.state / "ps").write_text(
            f"{NAME}-helper\tcom.example=1\n"
            f"{NAME}-runner\t\n"
            "notes-live-web-1\tcom.docker.compose.project=notes-live,org.example.identity=abc123\n"
            "build-7f3a-db-1\tcom.docker.compose.project=build-7f3a\n"
        )
        base = world.http(world.make(deployment=DEPLOYMENT))
        call(base, "POST", f"/start/{NAME}")
        status, body = call(base, "POST", f"/stop/{NAME}")
        assert status == 409
        assert "build-7f3a-db-1 is running" in body["message"]
        assert "notes-live-web-1" not in body["message"] and f"{NAME}-helper" not in body["message"]
        assert not any(line.startswith("stop") for line in world.lines("docker.log"))

    def test_the_recorded_deployment_and_the_factorys_two_are_allowed(self, world):
        (world.state / "ps").write_text(
            f"{NAME}-helper\t\n{NAME}-runner\t\n"
            "notes-live-web-1\tcom.docker.compose.project=notes-live,org.example.identity=abc123\n"
        )
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        assert "is stopped" in s.stop(NAME)["message"]

    def test_the_right_project_with_another_identity_is_refused(self, world):
        (world.state / "ps").write_text(
            "notes-live-web-1\tcom.docker.compose.project=notes-live,org.example.identity=other\n"
        )
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        with pytest.raises(starter_mod.Refused, match="notes-live-web-1 is running"):
            s.stop(NAME)

    def test_an_engine_that_cannot_be_listed_refuses_the_stop(self, world):
        (world.state / "exec_fails").write_text("")
        s = world.make()
        s.start(NAME)
        with pytest.raises(starter_mod.Refused, match="could not be listed"):
            s.stop(NAME)


class TestHolds:
    def test_a_hold_starts_a_stopped_sandbox_and_never_expires(self, world):
        clock = FakeClock()
        s = world.make(clock=clock)
        result = s.hold(NAME, "register-repo", "services")
        assert result["hold_id"].startswith("hold-")
        assert f"start {SUPERVISOR}" in world.lines("docker.log")
        clock.now += 3 * 86400  # three days later
        report = s.status(NAME)
        assert [h["id"] for h in report["holds"]] == [result["hold_id"]]
        assert report["holds"][0]["age_seconds"] >= 3 * 86400
        with pytest.raises(starter_mod.Refused, match="held open"):
            s.stop(NAME)
        s.end_hold(result["hold_id"])
        assert "is stopped" in s.stop(NAME)["message"]

    def test_a_hold_on_a_running_ready_sandbox_starts_nothing(self, world):
        s = world.make()
        s.start(NAME)
        before = world.lines("sbx.log")
        s.hold(NAME, "estate-check", "services")
        assert world.lines("sbx.log") == before
        assert sum(1 for line in world.lines("docker.log") if line.startswith("start")) == 1

    def test_holds_are_kept_across_a_restart(self, world):
        first = world.make()
        held = first.hold(NAME, "merge-deploy", "services")
        first.close()
        second = world.make()
        assert [h["owner"] for h in second.status(NAME)["holds"]] == ["merge-deploy"]
        with pytest.raises(starter_mod.Refused, match="held open"):
            second.stop(NAME)
        second.end_hold(held["hold_id"])

    def test_until_deployed_returns_only_after_ready_is_recorded(self, world):
        base = world.http(world.make(deployment=DEPLOYMENT))
        box = in_background(call, base, "POST", f"/hold/{NAME}?owner=merge-deploy&until=deployed")
        assert wait_for(lambda: call(base, "GET", f"/status/{NAME}")[1]["redeploy_pending"])
        time.sleep(0.3)
        assert "result" not in box
        status, body = call(base, "POST", f"/ready/{NAME}")
        assert status == 200
        box["thread"].join(10)
        status, body = box["result"]
        assert status == 200 and body["readiness"] == "ready" and body["hold_id"]

    def test_until_deployed_ends_the_hold_after_fifteen_minutes(self, world):
        clock = FakeClock()
        s = world.make(deployment=DEPLOYMENT, clock=clock)
        with pytest.raises(
            starter_mod.Failed, match="within 15 minutes of the hold being taken, so the hold has ended"
        ) as caught:
            s.hold(NAME, "merge-deploy", "deployed")
        assert caught.value.extra["hold_ended"] is True
        assert s.status(NAME)["holds"] == []
        assert clock.now >= 1000.0 + 900

    def test_until_deployed_with_no_recorded_deployment_is_services(self, world):
        s = world.make()
        assert s.hold(NAME, "merge-deploy", "deployed")["readiness"] == "ready"

    def test_until_deployed_on_a_suspended_sandbox_refuses_at_once(self, world):
        base = world.http(world.make(deployment=DEPLOYMENT))
        call(base, "POST", f"/suspend/{NAME}?reason=proxy%20down")
        status, body = call(base, "POST", f"/hold/{NAME}?owner=merge-deploy&until=deployed")
        assert status == 409 and "suspended" in body["message"]
        assert call(base, "GET", f"/status/{NAME}")[1]["holds"] == []
        assert not any(line.startswith("start") for line in world.lines("docker.log"))

    def test_a_suspension_while_waiting_ends_the_hold(self, world):
        s = world.make(deployment=DEPLOYMENT)
        box = in_background(s.hold, NAME, "merge-deploy", "deployed")
        assert wait_for(lambda: s.status(NAME)["redeploy_pending"])
        s.suspend(NAME, "keycloak did not answer")
        box["thread"].join(10)
        assert isinstance(box["error"], starter_mod.Failed)
        assert "suspended" in box["error"].message
        assert s.status(NAME)["holds"] == []

    def test_a_hold_waits_for_a_running_stop_then_starts_again(self, world):
        (world.state / "stop_sleep").write_text("1")
        s = world.make()
        s.start(NAME)
        stopping = in_background(s.stop, NAME)
        assert wait_for(lambda: s.status(NAME)["readiness"] == "stopping")
        result = s.hold(NAME, "register-repo", "services")
        stopping["thread"].join(10)
        assert "result" in stopping
        assert s.status(NAME)["container"]["state"] == "running"
        assert [h["id"] for h in s.status(NAME)["holds"]] == [result["hold_id"]]

    def test_a_hold_that_lands_before_the_stop_is_delivered_wins(self, world):
        s = world.make()
        s.start(NAME)
        sb = s.sandboxes[NAME]
        original = s._container_check

        def check_then_a_hold_arrives(target):
            original(target)
            with s.lock:
                sb.holds["hold-racer"] = {"id": "hold-racer", "owner": "register-repo",
                                          "until": "services", "opened": s.wall()}

        s._container_check = check_then_a_hold_arrives
        with pytest.raises(starter_mod.Refused, match="held open"):
            s.stop(NAME)
        assert not any(line.startswith("stop") for line in world.lines("docker.log"))

    def test_bad_hold_requests_are_refused(self, world):
        base = world.http(world.make())
        assert call(base, "POST", f"/hold/{NAME}?until=services")[0] == 400
        assert call(base, "POST", f"/hold/{NAME}?owner=x")[0] == 400
        assert call(base, "POST", f"/hold/{NAME}?owner=x&until=forever")[0] == 400
        assert call(base, "DELETE", "/hold/hold-nothing")[0] == 404


class TestCoordinatorRecords:
    def test_ready_needs_a_running_supervisor(self, world):
        base = world.http(world.make())
        status, body = call(base, "POST", f"/ready/{NAME}")
        assert status == 409 and "not running" in body["message"]

    def test_a_suspension_is_durable_until_cleared(self, world):
        first = world.make()
        first.suspend(NAME, "the redeploy's health check failed")
        first.close()
        second = world.make()
        assert second.status(NAME)["managed"] == {
            "state": "suspended", "reason": "the redeploy's health check failed",
        }
        second.start(NAME)
        with pytest.raises(starter_mod.Refused, match="suspended"):
            second.stop(NAME)
        second.clear(NAME)
        assert second.status(NAME)["managed"]["state"] == "active"
        assert "is stopped" in second.stop(NAME)["message"]

    def test_a_suspension_needs_a_reason(self, world):
        base = world.http(world.make())
        assert call(base, "POST", f"/suspend/{NAME}")[0] == 400


class TestRecovery:
    def test_a_restart_in_the_middle_of_a_stop_waits_for_the_supervisor_to_exit(self, world):
        (world.state / "stop_sleep").write_text("1.5")
        first = world.make()
        first.start(NAME)
        in_background(first.stop, NAME)
        assert wait_for(lambda: any(e["kind"] == "stop-delivered" for e in log_entries(world)))
        first.close()  # the starter "restarts" while docker stop is still running
        second = world.make()
        report = second.status(NAME)
        assert [o["op"] for o in report["operations"]["recovering"]] == ["stop"]
        with pytest.raises(starter_mod.Refused, match="queued or running"):
            second.stop(NAME)
        assert wait_for(lambda: second.status(NAME)["operations"]["recovering"] == [])
        report = second.status(NAME)
        assert report["operations"]["unresolved"] is None
        assert report["operations"]["last"]["outcome"] == "recovered"
        assert "exited" in report["operations"]["last"]["message"]

    def test_a_start_with_no_end_and_a_running_supervisor_is_recovered_at_once(self, world, tmp_path):
        state_dir = tmp_path / "starter-state"
        state_dir.mkdir()
        (state_dir / f"{NAME}.jsonl").write_text(
            json.dumps({"seq": 1, "kind": "op-queued", "op_id": 4, "op": "start"}) + "\n"
            + json.dumps({"seq": 2, "kind": "op-begin", "op_id": 4, "op": "start"}) + "\n"
            + '{"seq": 3, "kind": "readin'  # cut short by the crash
        )
        (world.state / "container").write_text("running\n")
        s = world.make()
        assert wait_for(lambda: s.status(NAME)["operations"]["recovering"] == [])
        assert s.status(NAME)["operations"]["last"]["outcome"] == "recovered"
        assert s.start(NAME)["operation"] == 5

    def test_past_the_bound_it_is_unresolved_and_refuses_until_cleared(self, world, tmp_path):
        state_dir = tmp_path / "starter-state"
        state_dir.mkdir()
        (state_dir / f"{NAME}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in [
                {"seq": 1, "kind": "op-queued", "op_id": 1, "op": "stop"},
                {"seq": 2, "kind": "op-begin", "op_id": 1, "op": "stop"},
                {"seq": 3, "kind": "stop-delivered", "op_id": 1},
            ]) + "\n"
        )
        (world.state / "container").write_text("running\n")  # never exits
        clock = FakeClock()
        s = world.make(clock=clock, STARTER_RECOVERY_POLL_SECONDS="10")
        assert wait_for(lambda: s.status(NAME)["operations"]["unresolved"])
        assert clock.now >= 1000.0 + 240
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.start(NAME)
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.stop(NAME)
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.hold(NAME, "estate-check", "services")
        s.clear(NAME)
        assert s.start(NAME)["readiness"] == "ready"

    def test_unresolved_is_kept_across_another_restart(self, world, tmp_path):
        state_dir = tmp_path / "starter-state"
        state_dir.mkdir()
        (state_dir / f"{NAME}.jsonl").write_text(
            json.dumps({"seq": 1, "kind": "unresolved", "reason": "it could not be told"}) + "\n"
        )
        s = world.make()
        assert s.status(NAME)["operations"]["unresolved"] == "it could not be told"
        with pytest.raises(starter_mod.Refused, match="POST /clear"):
            s.start(NAME)


class TestTheComposeFileRenders:
    def test_it_renders_from_the_example_env_with_nothing_of_this_machine(self):
        import re
        import shutil
        import subprocess

        if shutil.which("docker") is None:
            pytest.skip("docker is not installed here, so there is nothing to render")
        bare = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_CONFIG", "XDG_RUNTIME_DIR") if k in os.environ}
        done = subprocess.run(
            ["docker", "compose", "--project-name", "forge-sandbox-starter-check", "--env-file",
             ".env.example", "--file", "compose.sandbox-starter.yaml", "config"],
            cwd=COMPOSE.parent, env=bare, capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, done.stderr
        rendered = done.stdout
        addresses = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", rendered))
        assert addresses and all(a.startswith("192.0.2.") for a in addresses), addresses
        assert "/home/" not in rendered
        assert "starter.py" in rendered and "network_mode: host" in rendered



def _write_log(world, entries, tail: str = "") -> Path:
    state_dir = world.tmp / "starter-state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{NAME}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + tail)
    return path


A_DELIVERED_STOP = [
    {"seq": 1, "kind": "op-queued", "op_id": 1, "op": "stop"},
    {"seq": 2, "kind": "op-begin", "op_id": 1, "op": "stop"},
    {"seq": 3, "kind": "stop-delivered", "op_id": 1},
]


class TestASentStopIsSeenThrough:
    """Codex round 1, R1: once a stop is sent, the operation stays open until
    the supervisor has exited and the VM shows stopped, or it is unresolved."""

    def test_a_lost_answer_mid_shutdown_and_a_hold_never_starts_the_sandbox_mid_stop(self, world):
        clock = FakeClock()
        s = world.make(clock=clock)
        s.start(NAME)
        (world.state / "stop_fails").write_text("")
        (world.state / "inspect_running_for").write_text("4")
        (world.state / "vm_stops_after").write_text("2")
        stopping = in_background(s.stop, NAME)  # its caller is gone: the answer is lost
        assert wait_for(lambda: any(e["kind"] == "stop-delivered" for e in log_entries(world)))
        held = s.hold(NAME, "register-repo", "services")
        stopping["thread"].join(10)
        assert not (world.state / "midstop").exists(), (world.state / "midstop").read_text()
        assert "is stopped" in stopping["result"]["message"]
        docker = world.lines("docker.log")
        last_stop = max(i for i, line in enumerate(docker) if line.startswith("stop"))
        last_start = max(i for i, line in enumerate(docker) if line.startswith("start"))
        assert last_start > last_stop
        report = s.status(NAME)
        assert report["container"]["state"] == "running"
        assert [h["id"] for h in report["holds"]] == [held["hold_id"]]
        assert report["operations"]["unresolved"] is None

    def test_a_shutdown_that_never_ends_is_unresolved_and_refuses_everything(self, world):
        clock = FakeClock()
        s = world.make(clock=clock, STARTER_RECOVERY_POLL_SECONDS="10")
        s.start(NAME)
        (world.state / "stop_fails").write_text("")
        (world.state / "stop_never_exits").write_text("")
        with pytest.raises(starter_mod.Unresolved):
            s.stop(NAME)
        reason = s.status(NAME)["operations"]["unresolved"]
        assert "still running 240s after its stop was sent" in reason
        assert "context deadline exceeded" in reason
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.hold(NAME, "register-repo", "services")
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.start(NAME)
        assert sum(1 for line in world.lines("docker.log") if line.startswith("start")) == 1
        assert [e["outcome"] for e in log_entries(world) if e["kind"] == "op-end"][-1] == "unresolved"


class TestRecoveryFinishesTheVmStop:
    """Codex round 1, R2."""

    def test_an_exited_supervisor_with_its_vm_still_running_is_seen_to_stopped(self, world):
        _write_log(world, A_DELIVERED_STOP)
        (world.state / "container").write_text("exited\n")
        (world.state / "vm_stops_after").write_text("2")
        clock = FakeClock()
        s = world.make(clock=clock)
        assert wait_for(lambda: s.status(NAME)["operations"]["recovering"] == [])
        report = s.status(NAME)
        assert report["operations"]["last"]["outcome"] == "recovered"
        assert "VM shows stopped" in report["operations"]["last"]["message"]
        assert report["readiness"] == "stopped"
        assert world.lines("sbx.log") == ["ls", "ls", "ls"]
        assert clock.slept.count(5.0) == 2
        assert not any(line.startswith("exec") for line in world.lines("sbx.log"))

    def test_a_vm_that_never_stops_after_a_restart_is_unresolved(self, world):
        _write_log(world, A_DELIVERED_STOP)
        (world.state / "container").write_text("exited\n")
        (world.state / "vm_override").write_text("running")
        clock = FakeClock()
        s = world.make(clock=clock)
        assert wait_for(lambda: s.status(NAME)["operations"]["unresolved"])
        assert "VM still showed running 180s later" in s.status(NAME)["operations"]["unresolved"]
        assert world.lines("sbx.log").count("ls") <= 180 // 5 + 1
        with pytest.raises(starter_mod.Refused, match="unresolved"):
            s.start(NAME)


class TestTheLogIsWholeOrRefused:
    """Codex round 1, R3."""

    def test_a_cut_off_tail_twice_then_an_acknowledged_hold_and_suspension_read_back(self, world):
        first = world.make()
        first.start(NAME)
        first.close()
        path = world.tmp / "starter-state" / f"{NAME}.jsonl"
        with open(path, "a") as handle:
            handle.write('{"seq": 99, "kind": "hold-op')
        second = world.make()
        second.close()
        assert path.read_text().endswith("\n")
        with open(path, "a") as handle:
            handle.write('{"seq": 99, "kin')
        third = world.make()
        held = third.hold(NAME, "estate-check", "services")
        third.suspend(NAME, "the redeploy's proxy did not answer")
        third.close()
        for line in path.read_text().splitlines():
            json.loads(line)
        fourth = world.make()
        report = fourth.status(NAME)
        assert [h["id"] for h in report["holds"]] == [held["hold_id"]]
        assert report["managed"] == {"state": "suspended", "reason": "the redeploy's proxy did not answer"}

    @pytest.mark.parametrize(
        "damage",
        ['{"seq": 2, "kind": "hold-op\n', "[1, 2]\n", "\n", '{"seq": "two", "kind": "clear"}\n',
         '{"seq": 2, "kind": "op-queued"}\n'],
    )
    def test_damage_before_the_last_line_refuses_to_start(self, world, damage):
        path = _write_log(world, [{"seq": 1, "kind": "clear"}])
        with open(path, "a") as handle:
            handle.write(damage + json.dumps({"seq": 3, "kind": "clear"}) + "\n")
        before = path.read_bytes()
        with pytest.raises(ValueError, match="damaged at line 2"):
            world.make()
        assert path.read_bytes() == before

    @pytest.mark.parametrize(
        "records,why",
        [
            (  # an unknown hold event kind
                [{"seq": 1, "kind": "hold-open", "hold_id": "hold-a", "owner": "x", "until": "services", "opened": 1},
                 {"seq": 2, "kind": "hold-released", "hold_id": "hold-a"},
                 {"seq": 3, "kind": "clear"}],
                "line 2 ('hold-released' is not a kind of record",
            ),
            (  # a missing numbered hold record: number 2, the hold's close, is gone
                [{"seq": 1, "kind": "hold-open", "hold_id": "hold-a", "owner": "x", "until": "services", "opened": 1},
                 {"seq": 3, "kind": "suspend", "reason": "r"}],
                "line 2 (its number is 3 where 2 was due",
            ),
            (  # a repeated sequence number
                [{"seq": 1, "kind": "clear"},
                 {"seq": 2, "kind": "hold-open", "hold_id": "hold-a", "owner": "x", "until": "services", "opened": 1},
                 {"seq": 2, "kind": "hold-close", "hold_id": "hold-a"}],
                "line 3 (its number is 2 where 3 was due",
            ),
            (  # a log that does not start at 1
                [{"seq": 2, "kind": "clear"}],
                "line 1 (its number is 2 where 1 was due",
            ),
        ],
    )
    def test_gaps_repeats_and_unknown_kinds_refuse_to_start(self, world, records, why):
        path = _write_log(world, records)
        before = path.read_bytes()
        with pytest.raises(ValueError, match=re.escape(why)):
            world.make()
        assert path.read_bytes() == before
        assert world.starters == [] and world.lines("docker.log") == []

    def test_every_kind_the_service_writes_is_known(self):
        written = set(re.findall(r'_record\(\s*sb,\s*"([a-z-]+)"', STARTER_PY.read_text()))
        assert written == set(starter_mod.LOG_KINDS)

    def test_the_program_refuses_a_gap_in_one_sentence(self, world):
        import subprocess

        world.settings()
        path = _write_log(world, [{"seq": 1, "kind": "clear"}, {"seq": 3, "kind": "clear"}])
        before = path.read_bytes()
        env = dict(os.environ)
        env.update({
            "STARTER_LISTEN_ADDRESS": "127.0.0.1", "STARTER_LISTEN_PORT": "0",
            "STARTER_SANDBOXES": NAME, "STARTER_SETTINGS_FILE": str(world.tmp / "sandboxes.json"),
            "STARTER_STATE_DIR": str(path.parent),
            "STARTER_DOCKER": str(world.bin / "docker"), "STARTER_SBX": str(world.bin / "sbx"),
        })
        done = subprocess.run([sys.executable, "-I", str(STARTER_PY)], env=env,
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 2
        assert "damaged at line 2" in done.stderr and "Nothing was started" in done.stderr
        assert len(done.stderr.strip().splitlines()) == 1
        assert path.read_bytes() == before

    def test_the_program_refuses_to_start_in_one_sentence(self, world):
        import subprocess

        settings = world.settings()  # writes the settings file
        path = _write_log(world, [{"seq": 1, "kind": "clear"}], tail="not json\n{}\n")
        env = dict(os.environ)
        env.update({
            "STARTER_LISTEN_ADDRESS": "127.0.0.1", "STARTER_LISTEN_PORT": "0",
            "STARTER_SANDBOXES": NAME, "STARTER_SETTINGS_FILE": str(world.tmp / "sandboxes.json"),
            "STARTER_STATE_DIR": str(path.parent),
            "STARTER_DOCKER": str(world.bin / "docker"), "STARTER_SBX": str(world.bin / "sbx"),
        })
        done = subprocess.run([sys.executable, "-I", str(STARTER_PY)], env=env,
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 2
        assert "damaged at line 2" in done.stderr and "Nothing was started" in done.stderr
        assert settings.sandboxes[NAME].name == NAME


class TestOneDeadlineForADeployedHold:
    """Codex round 1, R4."""

    def test_a_suspension_during_start_up_ends_the_hold(self, world):
        (world.state / "start_sleep").write_text("2")
        s = world.make(deployment=DEPLOYMENT)
        box = in_background(s.hold, NAME, "merge-deploy", "deployed")
        assert wait_for(lambda: s.sandboxes[NAME].running is not None)
        began = time.monotonic()
        s.suspend(NAME, "keycloak did not answer")
        box["thread"].join(10)
        assert time.monotonic() - began < 1.8  # before the start has finished
        assert isinstance(box["error"], starter_mod.Failed)
        assert "suspended" in box["error"].message
        assert box["error"].extra["hold_ended"] is True
        assert s.status(NAME)["holds"] == []
        hold_id = box["error"].extra["hold_id"]
        assert any(e["kind"] == "hold-close" and e["hold_id"] == hold_id for e in log_entries(world))

    def test_a_stop_then_a_start_that_together_pass_the_deadline(self, world):
        s = world.make(deployment=DEPLOYMENT, STARTER_DEPLOYED_LIMIT_SECONDS="2")
        s.start(NAME)
        (world.state / "stop_sleep").write_text("1.2")
        stopping = in_background(s.stop, NAME)
        assert wait_for(lambda: any(e["kind"] == "stop-delivered" for e in log_entries(world)))
        (world.state / "start_sleep").write_text("1.5")
        began = time.monotonic()
        with pytest.raises(starter_mod.Failed, match="within 2 seconds of the hold being taken") as caught:
            s.hold(NAME, "merge-deploy", "deployed")
        assert time.monotonic() - began < 2.6  # the start (done at about 2.7 s) was not waited for
        hold_id = caught.value.extra["hold_id"]
        assert s.status(NAME)["holds"] == []
        assert any(e["kind"] == "hold-close" and e["hold_id"] == hold_id for e in log_entries(world))
        stopping["thread"].join(10)



class TestTheAlreadyStoppedShortcut:
    """Codex round 3, R6: "already stopped" only from exited, created or no
    container, and only with sbx ls showing the VM stopped."""

    def _stops_sent(self, world):
        return [line for line in world.lines("docker.log") if line.startswith("stop")]

    @pytest.mark.parametrize("state", ["exited", "created", None])
    def test_a_supervisor_that_is_not_running_and_a_stopped_vm_is_already_stopped(self, world, state):
        if state is None:
            (world.state / "container").unlink()
        else:
            (world.state / "container").write_text(state + "\n")
        s = world.make()
        assert "is already stopped" in s.stop(NAME)["message"]
        assert self._stops_sent(world) == []
        assert world.lines("sbx.log") == ["ls"]

    def test_a_restarting_supervisor_with_its_vm_stopped_takes_the_stop_path(self, world):
        (world.state / "container").write_text("restarting\n")
        s = world.make()
        result = s.stop(NAME)
        assert "is stopped" in result["message"] and "already" not in result["message"]
        assert self._stops_sent(world) == [f"stop -t 210 {SUPERVISOR}"]
        assert f"wait {SUPERVISOR}" in world.lines("docker.log")
        # The VM was stopped, so nothing was asked of it through sbx exec.
        assert not any(line.startswith("exec") for line in world.lines("sbx.log"))
        assert s.status(NAME)["readiness"] == "stopped"

    def test_a_paused_supervisor_takes_the_stop_path_with_its_checks(self, world):
        (world.state / "container").write_text("paused\n")
        s = world.make()
        result = s.stop(NAME)
        assert "is stopped" in result["message"] and "already" not in result["message"]
        assert self._stops_sent(world) == [f"stop -t 210 {SUPERVISOR}"]
        # Its VM was still running, so the engine was listed before the stop.
        assert any(line.startswith(f"exec {NAME} docker ps") for line in world.lines("sbx.log"))

    def test_a_paused_supervisor_with_a_busy_helper_is_refused(self, world):
        (world.state / "container").write_text("paused\n")
        world.services.answers["/busy"] = (200, '{"executing": [{"route": "/merge"}]}')
        s = world.make()
        with pytest.raises(starter_mod.Refused, match="still executing 1 request"):
            s.stop(NAME)
        assert self._stops_sent(world) == []

    @pytest.mark.parametrize("state", ["dead", "removing", "something-new"])
    def test_any_other_state_refuses_the_stop_and_never_reports_success(self, world, state):
        (world.state / "container").write_text(state + "\n")
        s = world.make()
        with pytest.raises(starter_mod.Refused, match=f"is {state}, a state the starter does not stop from"):
            s.stop(NAME)
        assert self._stops_sent(world) == []
        assert world.lines("sbx.log") == []
        assert s.status(NAME)["readiness"] != "stopped"
        assert s.status(NAME)["operations"]["last"]["outcome"] == "refused"

    def test_a_supervisor_whose_state_cannot_be_read_refuses_the_stop(self, world):
        (world.state / "container").write_text("")  # docker inspect answers nothing
        s = world.make()
        with pytest.raises(starter_mod.Refused, match="is unknown, a state the starter does not stop from"):
            s.stop(NAME)
        assert self._stops_sent(world) == []



ANOTHER = {"compose_project": "notes-live", "identity_label": "org.example.identity=def456"}
A_PARAMS = "compose_project=notes-live&identity_label=org.example.identity%3Dabc123"
B_PARAMS = "compose_project=notes-live&identity_label=org.example.identity%3Ddef456"


class TestReadyBelongsToOneDeploymentIdentity:
    """Codex round 4, R7."""

    def test_a_deployment_supplied_after_services_only_readiness_needs_a_redeploy(self, world):
        base = world.http(world.make())
        assert call(base, "POST", f"/start/{NAME}")[1]["readiness"] == "ready"
        status, body = call(base, "POST", f"/hold/{NAME}?owner=estate-check&until=services&{A_PARAMS}")
        assert status == 200 and body["readiness"] == "services ready, redeploy pending"
        report = call(base, "GET", f"/status/{NAME}")[1]
        assert report["redeploy_pending"] is True and report["ready_confirmed_for"] is None
        assert any(e["kind"] == "readiness" and e.get("why") for e in log_entries(world))

    def test_a_changed_confirmed_identity_needs_a_redeploy_and_ready_for_the_new_one(self, world):
        base = world.http(world.make())
        call(base, "POST", f"/start/{NAME}?{A_PARAMS}")
        assert call(base, "POST", f"/ready/{NAME}")[1]["confirmed_for"] == DEPLOYMENT
        assert call(base, "GET", f"/status/{NAME}")[1]["readiness"] == "ready"
        call(base, "POST", f"/start/{NAME}?{B_PARAMS}")
        report = call(base, "GET", f"/status/{NAME}")[1]
        assert report["readiness"] == "services ready, redeploy pending"
        # A ready naming the old identity is refused; one naming the new is recorded.
        status, body = call(base, "POST", f"/ready/{NAME}?{A_PARAMS}")
        assert status == 409 and "nothing was recorded" in body["message"]
        assert call(base, "GET", f"/status/{NAME}")[1]["redeploy_pending"] is True
        status, body = call(base, "POST", f"/ready/{NAME}?{B_PARAMS}")
        assert status == 200 and body["confirmed_for"] == ANOTHER
        assert call(base, "GET", f"/status/{NAME}")[1]["ready_confirmed_for"] == ANOTHER

    def test_a_deployed_hold_waits_for_ready_for_the_new_identity(self, world):
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        s.ready(NAME)
        box = in_background(s.hold, NAME, "merge-deploy", "deployed",
                            starter_mod._told_deployment({"compose_project": ["notes-live"],
                                                          "identity_label": ["org.example.identity=def456"]}))
        time.sleep(0.5)
        assert "result" not in box and "error" not in box
        assert s.status(NAME)["redeploy_pending"] is True
        s.ready(NAME)
        box["thread"].join(10)
        assert box["result"]["readiness"] == "ready"

    def test_an_unchanged_confirmed_identity_stays_ready(self, world):
        base = world.http(world.make(deployment=DEPLOYMENT))
        call(base, "POST", f"/start/{NAME}")
        call(base, "POST", f"/ready/{NAME}")
        readiness_records = sum(1 for e in log_entries(world) if e["kind"] == "readiness")
        status, body = call(base, "POST", f"/hold/{NAME}?owner=estate-check&until=services&{A_PARAMS}")
        assert body["readiness"] == "ready"
        call(base, "POST", f"/start/{NAME}?{A_PARAMS}")
        report = call(base, "GET", f"/status/{NAME}")[1]
        assert report["readiness"] == "ready" and report["ready_confirmed_for"] == DEPLOYMENT
        assert sum(1 for e in log_entries(world) if e["kind"] == "readiness") == readiness_records

    def test_the_invalidation_survives_a_restart(self, world):
        first = world.make()
        first.start(NAME, DEPLOYMENT)
        first.ready(NAME)
        first.start(NAME, ANOTHER)
        assert first.status(NAME)["redeploy_pending"] is True
        first.close()
        second = world.make()
        report = second.status(NAME)
        assert report["readiness"] == "services ready, redeploy pending"
        assert report["deployment"] == ANOTHER
        second.ready(NAME)
        second.close()
        third = world.make()
        report = third.status(NAME)
        assert report["readiness"] == "ready" and report["ready_confirmed_for"] == ANOTHER

    def test_a_settings_file_naming_another_deployment_invalidates_at_start_up(self, world):
        first = world.make(deployment=DEPLOYMENT)
        first.start(NAME)
        first.ready(NAME)
        first.close()
        second = world.make(deployment=ANOTHER)
        assert second.status(NAME)["readiness"] == "services ready, redeploy pending"



class TestADeploymentRecordedWhileAHoldWaits:
    """Codex round 5, R8."""

    def test_a_first_deployment_recorded_during_start_up_must_be_confirmed(self, world):
        (world.state / "start_sleep").write_text("1")
        s = world.make()  # no deployment when the hold is taken
        box = in_background(s.hold, NAME, "merge-deploy", "deployed")
        assert wait_for(lambda: s.sandboxes[NAME].running is not None)
        other = in_background(s.hold, NAME, "register-repo", "services", DEPLOYMENT)
        other["thread"].join(10)
        assert other["result"]["readiness"] == "services ready, redeploy pending"
        time.sleep(1.5)  # the start has finished; the deployed hold must still wait
        assert "result" not in box and "error" not in box
        assert s.status(NAME)["redeploy_pending"] is True
        s.ready(NAME, DEPLOYMENT)
        box["thread"].join(10)
        assert box["result"]["readiness"] == "ready"
        assert s.status(NAME)["ready_confirmed_for"] == DEPLOYMENT

    def test_a_deployment_recorded_during_the_wait_keeps_the_original_deadline(self, world):
        (world.state / "start_sleep").write_text("1")
        s = world.make(STARTER_DEPLOYED_LIMIT_SECONDS="3")
        began = time.monotonic()
        box = in_background(s.hold, NAME, "merge-deploy", "deployed")
        assert wait_for(lambda: s.sandboxes[NAME].running is not None)
        s.hold(NAME, "register-repo", "services", DEPLOYMENT)
        box["thread"].join(10)
        assert isinstance(box["error"], starter_mod.Failed)
        assert "within 3 seconds of the hold being taken" in box["error"].message
        assert time.monotonic() - began < 4.5
        assert [h["owner"] for h in s.status(NAME)["holds"]] == ["register-repo"]

    def test_with_no_deployment_at_all_a_deployed_hold_returns_at_services(self, world):
        (world.state / "start_sleep").write_text("1")
        s = world.make()
        result = s.hold(NAME, "merge-deploy", "deployed")
        assert result["readiness"] == "ready"
        assert "services ready" in result["message"]



class TestReadyHoldsOnlyForTheSameSupervisorRun:
    """Codex round 6, R9."""

    def _confirmed(self, world):
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        result = s.ready(NAME)
        assert result["started_at"]
        assert s.status(NAME)["readiness"] == "ready"
        return s

    def test_a_supervisor_restarted_outside_the_starter_needs_a_redeploy(self, world):
        s = self._confirmed(world)
        # Restarted by someone else (same identity; the app's containers,
        # restart policy "no", are not restored): a new run, services answer.
        (world.state / "started_at").write_text("2026-10-19T09:30:00.000000002Z\n")
        report = s.status(NAME)
        assert report["readiness"] == "services ready, redeploy pending"
        assert report["deployment"] == DEPLOYMENT and report["services_ready"] is True
        why = [e.get("why", "") for e in log_entries(world) if e["kind"] == "readiness"][-1]
        assert "started again" in why

    def test_services_that_drop_briefly_and_come_back_need_a_redeploy(self, world):
        s = self._confirmed(world)
        world.services.answers["/ok"] = (503, "restarting")
        report = s.status(NAME)
        assert report["readiness"] == "unknown" and report["redeploy_pending"] is False
        world.services.answers["/ok"] = (200, '{"ok":true}')
        report = s.status(NAME)
        assert report["readiness"] == "services ready, redeploy pending"
        assert any("the runner did not answer" in e.get("why", "") for e in log_entries(world))

    def test_a_supervisor_that_is_no_longer_running_ends_the_confirmation(self, world):
        s = self._confirmed(world)
        (world.state / "container").write_text("exited\n")
        assert s.status(NAME)["readiness"] == "unknown"

    def test_an_unreadable_start_time_ends_the_confirmation_and_refuses_ready(self, world):
        s = self._confirmed(world)
        (world.state / "started_at").write_text("\n")
        assert s.status(NAME)["readiness"] == "services ready, redeploy pending"
        with pytest.raises(starter_mod.Refused, match="start time could not be read"):
            s.ready(NAME)
        assert s.status(NAME)["redeploy_pending"] is True

    def test_a_deployed_hold_on_a_restarted_supervisor_waits_for_a_new_ready(self, world):
        s = self._confirmed(world)
        (world.state / "started_at").write_text("2026-10-19T09:30:00.000000002Z\n")
        box = in_background(s.hold, NAME, "merge-deploy", "deployed")
        time.sleep(0.5)
        assert "result" not in box and "error" not in box
        s.ready(NAME)
        box["thread"].join(10)
        assert box["result"]["readiness"] == "ready"

    def test_a_stop_decision_reads_the_run_too(self, world):
        s = self._confirmed(world)
        (world.state / "started_at").write_text("2026-10-19T09:30:00.000000002Z\n")
        s.suspend(NAME, "held for a look")
        with pytest.raises(starter_mod.Refused, match="suspended"):
            s.stop(NAME)
        assert any("started again" in e.get("why", "") for e in log_entries(world))

    def test_control_only_the_starter_restarts_and_ready_stays(self, world):
        first = self._confirmed(world)
        first.close()
        records = sum(1 for e in log_entries(world) if e["kind"] == "readiness")
        second = world.make(deployment=DEPLOYMENT)
        report = second.status(NAME)
        assert report["readiness"] == "ready" and report["ready_confirmed_on_start"]
        assert second.hold(NAME, "merge-deploy", "deployed")["readiness"] == "ready"
        assert sum(1 for e in log_entries(world) if e["kind"] == "readiness") == records



HEALTHY_AS = '{{"status": "healthy", "instance": "{}"}}'


class TestReadyIsBoundToTheHelperInstanceToo:
    """Codex round 7, R9 continued: (supervisor StartedAt, helper instance)."""

    def _confirmed(self, world, **kw):
        s = world.make(deployment=DEPLOYMENT, **kw)
        s.start(NAME)
        assert s.ready(NAME)["instance"] == "helper-run-1"
        assert s.status(NAME)["readiness"] == "ready"
        return s

    def test_a_start_right_after_an_external_supervisor_restart_needs_a_redeploy(self, world):
        base = world.http(self._confirmed(world))
        (world.state / "started_at").write_text("2026-10-19T09:30:00.000000002Z\n")
        world.services.answers["/healthz"] = (200, HEALTHY_AS.format("helper-run-2"))
        status, body = call(base, "POST", f"/start/{NAME}")  # the first thing to look
        assert status == 200
        assert body["readiness"] == "services ready, redeploy pending"
        assert sum(1 for line in world.lines("docker.log") if line.startswith("start")) == 2

    def test_a_start_with_only_a_new_helper_instance_needs_a_redeploy(self, world):
        s = self._confirmed(world)
        world.services.answers["/healthz"] = (200, HEALTHY_AS.format("helper-run-2"))
        assert s.start(NAME)["readiness"] == "services ready, redeploy pending"

    def test_a_vm_recovered_between_probes_needs_a_redeploy(self, world):
        s = self._confirmed(world)
        # Same supervisor run; the VM restarted and its start-up script made a
        # new helper, all between two probes, so no probe ever failed.
        world.services.answers["/healthz"] = (200, HEALTHY_AS.format("helper-run-2"))
        report = s.status(NAME)
        assert report["readiness"] == "services ready, redeploy pending"
        why = [e.get("why", "") for e in log_entries(world) if e["kind"] == "readiness"][-1]
        assert "new instance (helper-run-2" in why

    def test_a_helper_with_no_instance_field_cannot_be_confirmed(self, world):
        world.services.answers["/healthz"] = (200, '{"status": "healthy"}')
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        with pytest.raises(starter_mod.Refused, match="did not report its instance id"):
            s.ready(NAME)
        assert s.status(NAME)["redeploy_pending"] is True

    def test_a_confirmed_helper_that_stops_reporting_its_instance_ends_it(self, world):
        s = self._confirmed(world)
        world.services.answers["/healthz"] = (200, '{"status": "healthy"}')
        assert s.status(NAME)["readiness"] == "services ready, redeploy pending"

    def test_control_the_starter_restarts_with_the_same_run_and_instance(self, world):
        first = self._confirmed(world)
        first.close()
        second = world.make(deployment=DEPLOYMENT)
        assert second.start(NAME)["readiness"] == "ready"
        report = second.status(NAME)
        assert report["readiness"] == "ready"
        assert report["ready_confirmed_on_instance"] == "helper-run-1"


class TestTheLastCheckOfADeployedHold:
    """Codex round 7, R10."""

    def test_a_suspension_during_the_final_probe_ends_the_hold(self, world):
        s = world.make(deployment=DEPLOYMENT)
        s.start(NAME)
        s.ready(NAME)
        original = s._verify

        def a_suspension_arrives_during_the_final_probe(sb, limit=None):
            result = original(sb, limit)
            if limit is not None:
                s.suspend(NAME, "keycloak went down")
            return result

        s._verify = a_suspension_arrives_during_the_final_probe
        with pytest.raises(starter_mod.Failed, match="suspended") as caught:
            s.hold(NAME, "merge-deploy", "deployed")
        assert caught.value.extra["hold_ended"] is True
        assert s.status(NAME)["holds"] == []
        assert any(e["kind"] == "hold-close" for e in log_entries(world))

    def test_a_final_probe_crossing_the_deadline_ends_the_hold(self, world):
        clock = FakeClock()
        s = world.make(deployment=DEPLOYMENT, clock=clock)
        s.start(NAME)
        s.ready(NAME)
        original = s._verify
        limits = []

        def the_final_probe_takes_past_the_deadline(sb, limit=None):
            if limit is not None:
                limits.append(limit)
                clock.now += 901  # the probe was slow
            return original(sb, limit)

        s._verify = the_final_probe_takes_past_the_deadline
        with pytest.raises(starter_mod.Failed, match="within 15 minutes of the hold being taken"):
            s.hold(NAME, "merge-deploy", "deployed")
        assert limits and limits[0] <= 900
        assert s.status(NAME)["holds"] == []
        # Running out of time is not evidence against the confirmation.
        assert s.status(NAME)["readiness"] == "ready"

    def test_the_final_reads_are_bounded_by_what_is_left(self, world):
        clock = FakeClock()
        s = world.make(deployment=DEPLOYMENT, clock=clock)
        s.start(NAME)
        s.ready(NAME)
        seen = []
        original_services = s._services

        def services(sb, timeout=3.0):
            seen.append(timeout)
            return original_services(sb, timeout)

        s._services = services
        clock.now += 0  # the hold is taken now: 900 s left
        original = s._verify

        def most_of_the_time_has_gone(sb, limit=None):
            if limit is not None:
                return original(sb, 1.5)
            return original(sb, limit)

        s._verify = most_of_the_time_has_gone
        assert s.hold(NAME, "merge-deploy", "deployed")["readiness"] == "ready"
        assert seen[-1] <= 1.5
