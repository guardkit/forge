#!/usr/bin/env python3
"""The sandbox starter: starts, stops and reports on a project's own sandbox.

One sandbox per project, stage S2 (design sections 2.3 and 2.3a, 18 October
2026). A small standard-library HTTP service, in a container of its own on the
machine that runs the sandboxes. It holds the machine's Docker socket, the
docker client, the sandbox client (sbx) and the sandbox daemon's socket, and it
does exactly these things, only for the names in STARTER_SANDBOXES:

    GET    /status/<name>    container and VM state, operations, holds,
                             managed state, whether the services answer, and
                             whether a redeploy is pending
    POST   /start/<name>     sign-in check, `docker start` of the project's
                             supervisor container, then both readiness checks
    POST   /stop/<name>      refused while anything is open; otherwise the
                             container check, `docker stop -t 210`, `docker
                             wait`, then `sbx ls` until the VM shows stopped
    POST   /hold/<name>?owner=..&until=services|deployed
                             keeps the sandbox up for an outside command;
                             starts it when stopped
    DELETE /hold/<id>        ends a hold
    POST   /ready/<name>     the coordinator records the redeploy as ready,
                             for the sandbox's current deployment identity
                             (which it may name, and must then match)
    POST   /suspend/<name>?reason=..
                             the coordinator records a failed redeploy: no
                             stop is accepted until a session clears it
    POST   /clear/<name>     a session clears a suspension, or an operation
                             the starter could not resolve after a restart

It never runs `docker compose up` (that would read the Compose files again),
never creates or removes a sandbox, and never acts for api-test-deploy, the
shared sandbox, which is refused by name.

THE SANDBOX CLIENT IS NEVER CALLED IN A FAST LOOP. A client asked over and over
crashed the machine's sign-in keyring on 17 October 2026. So the client is
called once per start (the sign-in check, which also reads the VM's state),
once per stop for the container check, and then `sbx ls` at most every five
seconds, only during a stop, and for a bounded time. A status request never
calls it: the VM state it reports is the one last seen, with when.

Everything the starter must remember across a restart is one numbered log of
JSON lines per sandbox, on a volume: operations, holds, readiness, suspensions.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The shared sandbox. It hosts a product with an in-memory database (design
#: 2.6), so it is never stopped, started or held by this service.
NEVER_MANAGED = "api-test-deploy"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

#: What the two readiness checks look for, exactly as the sandbox's own start
#: script checks them (sandbox-runner.sh, wait_until_it_answers).
HELPER_HEALTHY = '"healthy"'
RUNNER_OK = '"ok":true'

#: Readiness values reported by /status.
READY = "ready"
REDEPLOY_PENDING = "services ready, redeploy pending"
STARTING = "starting"
STOPPED = "stopped"
STOPPING = "stopping"
UNKNOWN = "unknown"

#: Every kind of record the operation log holds. op-begin is informational:
#: replay reads nothing from it, but it is a kind this service writes.
LOG_KINDS = frozenset({
    "op-queued", "op-begin", "stop-delivered", "op-end",
    "hold-open", "hold-close",
    "readiness", "vm", "deployment",
    "suspend", "unresolved", "clear",
})

#: Supervisor states a stop starts from: the normal stop is sent.
SUPERVISOR_STOPPABLE = ("running", "restarting", "paused")

#: Supervisor states from which a stop may answer "already stopped", if and
#: only if sbx ls also shows the VM stopped.
SUPERVISOR_NOT_RUNNING = ("exited", "created", "missing")

#: Supervisor states that, once a stop has been sent, mean it has ended.
EXITED = ("exited", "dead", "created", "missing")

#: The fewest seconds between two `sbx ls` calls during a stop.
MIN_VM_POLL_SECONDS = 5.0

#: Words in the sandbox client's answer that mean its sign-in has lapsed.
SIGNED_OUT = re.compile(
    r"sbx login|no valid user session|not signed in|not logged in|sign in|authenticat",
    re.IGNORECASE,
)


class Refused(Exception):
    """The request is refused, in one plain sentence. Nothing was changed."""

    status = HTTPStatus.CONFLICT

    def __init__(self, message: str, status: HTTPStatus | None = None, **extra):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        self.extra = extra


class Failed(Refused):
    """The request was tried and did not succeed, in one plain sentence."""

    status = HTTPStatus.SERVICE_UNAVAILABLE


class Unresolved(Failed):
    """A stop was sent and could not be seen through; the sandbox is unresolved."""


def _iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SandboxSettings:
    """What the starter knows about one sandbox, from its settings file."""

    def __init__(self, name: str, entry: dict):
        self.name = name
        helper = entry.get("helper_url")
        runner = entry.get("runner_url")
        if not helper or not runner:
            raise ValueError(
                f"the settings file must give {name} both helper_url and runner_url "
                "(the helper's and the runner's own addresses)"
            )
        self.helper_url = str(helper).rstrip("/")
        self.runner_url = str(runner).rstrip("/")
        self.container_prefix = str(entry.get("container_prefix") or name)
        self.supervisor = f"forge-sbx-{name}-sandbox-runner-1"
        self.deployment = _deployment_from(entry.get("deployment"), f"the settings for {name}")

    @property
    def helper_container(self) -> str:
        return f"{self.container_prefix}-helper"

    @property
    def runner_container(self) -> str:
        return f"{self.container_prefix}-runner"


def _deployment_from(value, where: str) -> dict | None:
    """A recorded deployment: its compose project and its identity label."""
    if value in (None, {}, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: deployment must be an object with compose_project and identity_label")
    project = str(value.get("compose_project") or "")
    label = str(value.get("identity_label") or "")
    if not project or "=" not in label or label.startswith("="):
        raise ValueError(
            f"{where}: a deployment needs compose_project and identity_label "
            "(the label as key=value)"
        )
    return {"compose_project": project, "identity_label": label}


class Settings:
    def __init__(self, env: dict):
        def number(key: str, default: float) -> float:
            raw = env.get(key, "")
            if raw == "":
                return default
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{key} must be a number of seconds, and it is '{raw}'") from None
            if value < 0:
                raise ValueError(f"{key} must not be negative")
            return value

        self.listen_address = env.get("STARTER_LISTEN_ADDRESS", "")
        port = env.get("STARTER_LISTEN_PORT", "")
        if not self.listen_address or not port.isdigit():
            raise ValueError(
                "STARTER_LISTEN_ADDRESS and STARTER_LISTEN_PORT must name the one "
                "address and port this service listens on"
            )
        self.listen_port = int(port)
        self.state_dir = env.get("STARTER_STATE_DIR") or "/var/lib/forge-sandbox-starter"
        self.docker = env.get("STARTER_DOCKER") or "docker"
        self.sbx = env.get("STARTER_SBX") or "sbx"
        self.stop_grace = number("STARTER_STOP_GRACE_SECONDS", 210)
        self.margin = number("STARTER_WAIT_MARGIN_SECONDS", 30)
        self.ready_limit = number("STARTER_READY_LIMIT_SECONDS", 600)
        self.ready_poll = number("STARTER_READY_POLL_SECONDS", 2)
        self.deployed_limit = number("STARTER_DEPLOYED_LIMIT_SECONDS", 900)
        self.vm_poll = max(MIN_VM_POLL_SECONDS, number("STARTER_VM_POLL_SECONDS", 5))
        self.vm_stop_limit = number("STARTER_VM_STOP_LIMIT_SECONDS", 180)
        self.recovery_poll = number("STARTER_RECOVERY_POLL_SECONDS", 2)
        self.client_timeout = number("STARTER_CLIENT_TIMEOUT_SECONDS", 60)

        names = [n for n in re.split(r"[\s,]+", env.get("STARTER_SANDBOXES", "")) if n]
        if not names:
            raise ValueError("STARTER_SANDBOXES names no sandbox, so there is nothing to look after")
        if NEVER_MANAGED in names:
            raise ValueError(
                f"STARTER_SANDBOXES names {NEVER_MANAGED}, the shared sandbox, which "
                "is never managed by this service. Take it out of the list"
            )
        for name in names:
            if not NAME_PATTERN.match(name):
                raise ValueError(f"'{name}' in STARTER_SANDBOXES is not a sandbox name")
        path = env.get("STARTER_SETTINGS_FILE", "")
        if not path:
            raise ValueError("STARTER_SETTINGS_FILE must name the file with each sandbox's addresses")
        try:
            with open(path, encoding="utf-8") as handle:
                table = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"the settings file {path} could not be read as JSON ({exc})") from None
        if not isinstance(table, dict):
            raise ValueError(f"the settings file {path} must hold one object, keyed by sandbox name")
        self.sandboxes: dict[str, SandboxSettings] = {}
        for name in names:
            if name not in table or not isinstance(table[name], dict):
                raise ValueError(f"{name} is in STARTER_SANDBOXES but the settings file has no entry for it")
            self.sandboxes[name] = SandboxSettings(name, table[name])


# ---------------------------------------------------------------------------
# Per-sandbox state
# ---------------------------------------------------------------------------


class Op:
    """One operation in a sandbox's queue."""

    def __init__(self, op_id, kind: str, queued_at: float):
        self.id = op_id
        self.kind = kind
        self.queued_at = queued_at
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: Refused | None = None

    def describe(self) -> dict:
        return {"id": self.id, "op": self.kind, "queued_at": _iso(self.queued_at)}


class Sandbox:
    def __init__(self, settings: SandboxSettings, lock: threading.RLock):
        self.settings = settings
        self.cond = threading.Condition(lock)
        self.queue: collections.deque[Op] = collections.deque()
        self.running: Op | None = None
        self.recovering: list[dict] = []
        self.holds: dict[str, dict] = {}
        self.suspended: str | None = None
        self.unresolved: str | None = None
        self.readiness = UNKNOWN
        self.ready_for: dict | None = None  # the identity "ready" was confirmed for
        self.vm_state: str | None = None
        self.vm_seen_at: float | None = None
        self.vm_how: str | None = None
        self.deployment_told: dict | None = None  # {} means told "none"
        self.next_op = 1
        self.next_seq = 1
        self.last_operation: dict | None = None
        self.log_path = ""

    @property
    def name(self) -> str:
        return self.settings.name

    def deployment(self) -> dict | None:
        if self.deployment_told is not None:
            return self.deployment_told or None
        return self.settings.deployment

    def busy(self) -> bool:
        return bool(self.queue or self.running is not None or self.recovering)


# ---------------------------------------------------------------------------
# The starter
# ---------------------------------------------------------------------------


class Starter:
    def __init__(self, settings: Settings, *, clock=time.monotonic, wall=time.time,
                 sleep=time.sleep, wait=None, say=None):
        self.settings = settings
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        self.say = say or (lambda line: print(f"sandbox-starter: {line}", flush=True))
        self.lock = threading.RLock()
        self.sandboxes = {name: Sandbox(s, self.lock) for name, s in settings.sandboxes.items()}
        self.closing = False
        self.threads: list[threading.Thread] = []

    # -- the log ------------------------------------------------------------

    def _record(self, sb: Sandbox, kind: str, **fields) -> dict:
        with self.lock:
            entry = {"seq": sb.next_seq, "at": _iso(self.wall()), "kind": kind, **fields}
            sb.next_seq += 1
            with open(sb.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return entry

    def _replay(self, sb: Sandbox) -> None:
        """Read the sandbox's log back. Fails closed on anything but a cut-off last line.

        Every record is written as one line ending in a newline and fsynced
        before the request it belongs to is answered or acted on. So a final
        line without its newline was never acknowledged: it is cut off here,
        back to the end of the last whole line, and fsynced, before anything
        else is accepted. Anything else wrong with the log is damage this
        service cannot judge, and it refuses to start.
        """
        sb.log_path = os.path.join(self.settings.state_dir, f"{sb.name}.jsonl")
        unended: dict[int, dict] = {}
        if not os.path.exists(sb.log_path):
            sb.recovering = []
            return
        with open(sb.log_path, "rb") as handle:
            data = handle.read()
        if data and not data.endswith(b"\n"):
            keep = data.rfind(b"\n") + 1
            with open(sb.log_path, "r+b") as handle:
                handle.truncate(keep)
                handle.flush()
                os.fsync(handle.fileno())
            self.say(
                f"{sb.name}: the log's last line was cut off by a crash and was never "
                f"acknowledged; it has been removed ({len(data) - keep} bytes)"
            )
            data = data[:keep]
        expected = 1
        for number, raw in enumerate(data.split(b"\n")[:-1] if data else [], 1):
            try:
                entry = json.loads(raw.decode("utf-8"))
                if not isinstance(entry, dict) or not isinstance(entry.get("seq"), int) \
                        or isinstance(entry.get("seq"), bool) or not isinstance(entry.get("kind"), str):
                    raise ValueError("not a record")
                # The records are numbered 1, 2, 3 ... with nothing missing and
                # nothing repeated, and each is of a kind this service writes.
                if entry["seq"] != expected:
                    raise ValueError(
                        f"its number is {entry['seq']} where {expected} was due, so a record "
                        "is missing or repeated"
                    )
                if entry["kind"] not in LOG_KINDS:
                    raise ValueError(f"'{entry['kind']}' is not a kind of record this service writes")
                expected += 1
                self._apply(sb, entry, unended)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(
                    f"the operation log {sb.log_path} is damaged at line {number} ({exc}), "
                    "so the starter will not start until a session looks at it"
                ) from None
        sb.recovering = sorted(unended.values(), key=lambda o: o["id"])

    def _apply(self, sb: Sandbox, entry: dict, unended: dict) -> None:
        sb.next_seq = max(sb.next_seq, entry["seq"] + 1)
        kind = entry["kind"]
        if kind == "op-queued":
            op_id = int(entry["op_id"])
            sb.next_op = max(sb.next_op, op_id + 1)
            unended[op_id] = {"id": op_id, "op": entry["op"], "stop_delivered": False}
        elif kind == "stop-delivered" and entry.get("op_id") in unended:
            unended[entry["op_id"]]["stop_delivered"] = True
        elif kind == "op-end":
            unended.pop(entry.get("op_id"), None)
            sb.last_operation = {k: entry.get(k) for k in ("op_id", "op", "outcome", "message", "at")}
        elif kind == "hold-open":
            sb.holds[entry["hold_id"]] = {
                "id": entry["hold_id"], "owner": entry.get("owner"),
                "until": entry.get("until"), "opened": float(entry.get("opened", 0)),
            }
        elif kind == "hold-close":
            sb.holds.pop(entry.get("hold_id"), None)
        elif kind == "readiness":
            sb.readiness = entry.get("state", UNKNOWN)
            sb.ready_for = entry.get("confirmed_for") or None
        elif kind == "vm":
            sb.vm_state = entry.get("state")
            sb.vm_seen_at = float(entry.get("seen", 0)) or None
            sb.vm_how = entry.get("how")
        elif kind == "suspend":
            sb.suspended = entry.get("reason") or "no reason was given"
        elif kind == "unresolved":
            sb.unresolved = entry.get("reason")
        elif kind == "clear":
            sb.suspended = None
            sb.unresolved = None
        elif kind == "deployment":
            sb.deployment_told = entry.get("deployment") or {}

    # -- life cycle ---------------------------------------------------------

    def boot(self) -> None:
        os.makedirs(self.settings.state_dir, exist_ok=True)
        # Every log is read back (and a cut-off last line removed) before any
        # sandbox's queue starts, so a damaged log stops the whole service.
        for sb in self.sandboxes.values():
            self._replay(sb)
        for sb in self.sandboxes.values():
            # The settings file may name another deployment than the one a
            # recorded "ready" was confirmed for.
            self._reconcile_confirmation(sb, "the deployment identity differs from the one ready was confirmed for")
            if sb.recovering:
                ids = ", ".join(str(o["id"]) for o in sb.recovering)
                self.say(f"{sb.name}: operation(s) {ids} have no recorded end; recovering")
                sb.queue.appendleft(Op(None, "recover", self.clock()))
            thread = threading.Thread(target=self._work, args=(sb,), name=f"starter-{sb.name}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self) -> None:
        with self.lock:
            self.closing = True
            for sb in self.sandboxes.values():
                sb.cond.notify_all()

    def _work(self, sb: Sandbox) -> None:
        while True:
            with self.lock:
                while not sb.queue and not self.closing:
                    sb.cond.wait()
                if self.closing:
                    return
                op = sb.queue.popleft()
                sb.running = op
                if op.id is not None:
                    self._record(sb, "op-begin", op_id=op.id, op=op.kind)
            try:
                if op.kind == "recover":
                    op.result = self._run_recover(sb)
                elif op.kind == "start":
                    op.result = self._run_start(sb, op)
                elif op.kind == "stop":
                    op.result = self._run_stop(sb, op)
                outcome, message = "done", (op.result or {}).get("message", "")
            except Refused as exc:
                op.error = exc
                outcome = ("unresolved" if isinstance(exc, Unresolved)
                           else "failed" if isinstance(exc, Failed) else "refused")
                message = exc.message
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                op.error = Failed(f"the {op.kind} stopped on an unexpected error: {exc!r}")
                outcome, message = "failed", op.error.message
            with self.lock:
                if op.id is not None:
                    entry = self._record(sb, "op-end", op_id=op.id, op=op.kind, outcome=outcome, message=message)
                    sb.last_operation = {k: entry.get(k) for k in ("op_id", "op", "outcome", "message", "at")}
                    self.say(f"{sb.name}: operation {op.id} ({op.kind}) {outcome}: {message}")
                sb.running = None
                op.done.set()
                sb.cond.notify_all()

    def _enqueue(self, sb: Sandbox, kind: str) -> Op:
        with self.lock:
            op = Op(sb.next_op, kind, self.clock())
            sb.next_op += 1
            self._record(sb, "op-queued", op_id=op.id, op=kind)
            sb.queue.append(op)
            sb.cond.notify_all()
            return op

    def _start_or_join(self, sb: Sandbox) -> Op:
        with self.lock:
            for op in sb.queue:
                if op.kind == "start":
                    return op
            if sb.running is not None and sb.running.kind == "start" and not sb.queue:
                return sb.running
            return self._enqueue(sb, "start")

    @staticmethod
    def _finish(op: Op) -> dict:
        op.done.wait()
        if op.error is not None:
            raise op.error
        return op.result or {}

    # -- the outside world --------------------------------------------------

    def _run(self, argv: list[str], timeout: float) -> tuple[int | None, str]:
        """Run one command. Returns (exit status or None on timeout, its output)."""
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"{argv[0]} {argv[1] if len(argv) > 1 else ''} gave no answer within {int(timeout)}s"
        except OSError as exc:
            return 127, f"{argv[0]} could not be run ({exc.strerror or exc})"
        return done.returncode, (done.stdout + done.stderr).strip()

    def _container_state(self, sb: Sandbox) -> str:
        rc, out = self._run(
            [self.settings.docker, "inspect", "--format", "{{.State.Status}}", sb.settings.supervisor],
            self.settings.client_timeout,
        )
        if rc == 0 and out:
            return out.splitlines()[0].strip()
        if rc is not None and "no such" in out.lower():
            return "missing"
        return UNKNOWN

    def _sbx_ls(self, sb: Sandbox) -> tuple[bool, str]:
        """One `sbx ls`. Returns (it worked, the VM state or the reason it did not)."""
        rc, out = self._run([self.settings.sbx, "ls"], self.settings.client_timeout)
        if rc != 0 or SIGNED_OUT.search(out or ""):
            first = (out or "no output").splitlines()[0][:200]
            return False, first
        state = "not listed"
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == sb.name:
                state = parts[2] if len(parts) >= 3 else UNKNOWN
                break
        self._note_vm(sb, state, "sbx ls")
        return True, state

    def _note_vm(self, sb: Sandbox, state: str, how: str) -> None:
        with self.lock:
            sb.vm_state, sb.vm_seen_at, sb.vm_how = state, self.wall(), how
            self._record(sb, "vm", state=state, seen=sb.vm_seen_at, how=how)

    def _probe(self, url: str, expected: str, timeout: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as answer:  # noqa: S310 - fixed http address
                body = answer.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return expected in body

    def _services(self, sb: Sandbox) -> tuple[bool, bool]:
        return (
            self._probe(sb.settings.helper_url + "/healthz", HELPER_HEALTHY),
            self._probe(sb.settings.runner_url + "/ok", RUNNER_OK),
        )

    def _helper_busy(self, sb: Sandbox) -> list | None:
        """What the helper says it is executing; None if it gave no usable answer."""
        try:
            with urllib.request.urlopen(sb.settings.helper_url + "/busy", timeout=5) as answer:  # noqa: S310
                if answer.status != 200:
                    return None
                data = json.loads(answer.read(65536).decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        # Only an object whose "executing" is a list counts as an answer.
        if isinstance(data, dict) and isinstance(data.get("executing"), list):
            return data["executing"]
        return None

    # -- readiness bookkeeping ---------------------------------------------

    # "ready" is always ready FOR ONE DEPLOYMENT IDENTITY (none for a sandbox
    # with only its services): it is recorded with the identity it was
    # confirmed for, and it counts only while that is still the sandbox's
    # deployment. A new or changed identity invalidates it durably.

    def _set_readiness(self, sb: Sandbox, state: str, why: str | None = None) -> None:
        with self.lock:
            confirmed_for = sb.deployment() if state == READY else None
            if sb.readiness != state or sb.ready_for != confirmed_for:
                sb.readiness, sb.ready_for = state, confirmed_for
                extra = {"why": why} if why else {}
                self._record(sb, "readiness", state=state, confirmed_for=confirmed_for, **extra)

    def _confirmed(self, sb: Sandbox) -> bool:
        with self.lock:
            return sb.readiness == READY and sb.ready_for == sb.deployment()

    def _reconcile_confirmation(self, sb: Sandbox, why: str) -> None:
        """The deployment identity may have changed: re-judge what readiness means."""
        with self.lock:
            if sb.readiness == READY and sb.ready_for != sb.deployment():
                self._set_readiness(sb, REDEPLOY_PENDING if sb.deployment() else READY, why)
            elif sb.readiness == REDEPLOY_PENDING and sb.deployment() is None:
                self._set_readiness(sb, READY, why)

    def _services_answered(self, sb: Sandbox, woke: bool) -> str:
        """Both services answer. A wake (or an unknown state) needs a redeploy."""
        with self.lock:
            if woke or sb.readiness not in (READY, REDEPLOY_PENDING):
                self._set_readiness(sb, REDEPLOY_PENDING if sb.deployment() else READY)
            else:
                self._reconcile_confirmation(sb, "the deployment identity changed")
            return sb.readiness

    def _note_deployment(self, sb: Sandbox, told) -> None:
        if told is None:
            return
        with self.lock:
            if sb.deployment_told != told:
                before = sb.deployment()
                sb.deployment_told = told
                self._record(sb, "deployment", deployment=told)
                if sb.deployment() != before:
                    self._reconcile_confirmation(
                        sb, "the deployment identity was supplied or changed, so its redeploy must be confirmed again")

    # -- start --------------------------------------------------------------

    def _run_start(self, sb: Sandbox, op: Op) -> dict:
        with self.lock:
            if sb.unresolved:
                raise Refused(self._unresolved_sentence(sb))
        worked, said = self._sbx_ls(sb)
        if not worked:
            raise Failed(
                f"the sandbox client is not signed in or did not answer (it said: {said}), so "
                f"{sb.name} was not started. Sign in on the machine with sbx login, then ask again"
            )
        was = self._container_state(sb)
        if was != "running":
            self._set_readiness(sb, STARTING)
        rc, out = self._run([self.settings.docker, "start", sb.settings.supervisor], self.settings.client_timeout)
        if rc != 0:
            self._set_readiness(sb, UNKNOWN)
            raise Failed(f"docker start {sb.settings.supervisor} did not work ({out[:200] or 'no output'})")
        deadline = self.clock() + self.settings.ready_limit
        while True:
            helper, runner = self._services(sb)
            if helper and runner:
                break
            state = self._container_state(sb)
            if state not in ("running", "restarting", UNKNOWN):
                self._set_readiness(sb, UNKNOWN)
                raise Failed(
                    f"the supervisor container {sb.settings.supervisor} is {state} again before "
                    f"{sb.name}'s services answered; its log says why"
                )
            if self.clock() >= deadline:
                self._set_readiness(sb, UNKNOWN)
                missing = " and ".join(
                    what for what, ok in (("the deploy helper (/healthz)", helper),
                                          ("the build runner (/ok)", runner)) if not ok
                )
                raise Failed(
                    f"{missing} in {sb.name} did not answer within "
                    f"{int(self.settings.ready_limit)}s of the start"
                )
            self.sleep(self.settings.ready_poll)
        # Its services answering inside it shows the VM is up, without asking
        # the sandbox client again.
        self._note_vm(sb, "running", "its helper and runner answer")
        readiness = self._services_answered(sb, woke=(was != "running"))
        return {
            "readiness": readiness,
            "redeploy_pending": readiness == REDEPLOY_PENDING,
            "message": (
                f"{sb.name} is up: services ready, redeploy pending"
                if readiness == REDEPLOY_PENDING else f"{sb.name} is up and ready"
            ),
        }

    # -- stop ---------------------------------------------------------------

    def _hold_sentence(self, sb: Sandbox) -> str:
        now = self.wall()
        held = "; ".join(
            f"{h['id']} by {h['owner']} ({int(now - h['opened'])}s old)" for h in sb.holds.values()
        )
        return f"{sb.name} is held open ({held}), so it was not stopped"

    def _unresolved_sentence(self, sb: Sandbox) -> str:
        return (
            f"{sb.name} is unresolved: {sb.unresolved}. Start and stop are refused until a "
            f"session looks and clears it with POST /clear/{sb.name}"
        )

    def _stop_refusal(self, sb: Sandbox, *, starting: bool) -> str | None:
        """Why a stop may not go ahead now, or None. Called under the lock."""
        if sb.unresolved:
            return self._unresolved_sentence(sb)
        if sb.suspended:
            return (
                f"{sb.name} is managed: suspended, reason: {sb.suspended}. No stop is accepted "
                f"until a session clears it with POST /clear/{sb.name}"
            )
        if sb.holds:
            return self._hold_sentence(sb)
        if starting and sb.busy():
            pending = [o.describe() for o in sb.queue]
            if sb.running is not None:
                pending.insert(0, sb.running.describe())
            what = ", ".join(f"{p['op']} {p['id'] or '(recovery)'}" for p in pending) or "recovery"
            return f"{sb.name} has an operation queued or running ({what}), so it was not stopped"
        return None

    def _container_check(self, sb: Sandbox) -> None:
        """Everything running in the sandbox's engine must be the factory's or the deployment's."""
        rc, out = self._run(
            [self.settings.sbx, "exec", sb.name, "docker", "ps", "--format", "{{.Names}}\t{{.Labels}}"],
            self.settings.client_timeout,
        )
        if rc != 0:
            raise Refused(
                f"the containers running in {sb.name}'s engine could not be listed "
                f"({(out or 'no output')[:200]}), so it was not stopped"
            )
        deployment = sb.deployment()
        allowed = {sb.settings.helper_container, sb.settings.runner_container}
        strangers = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name, _, labels = line.partition("\t")
            name = name.strip()
            if name in allowed:
                continue
            pairs = {part.strip() for part in labels.split(",") if part.strip()}
            if deployment and (
                f"com.docker.compose.project={deployment['compose_project']}" in pairs
                and deployment["identity_label"] in pairs
            ):
                continue
            strangers.append(name)
        if strangers:
            what = "the recorded deployment" if deployment else "a recorded deployment (there is none)"
            raise Refused(
                f"{', '.join(strangers)} {'is' if len(strangers) == 1 else 'are'} running in "
                f"{sb.name}'s engine and {'is' if len(strangers) == 1 else 'are'} neither the "
                f"factory's helper or runner nor part of {what}, so it was not stopped"
            )

    def _run_stop(self, sb: Sandbox, op: Op) -> dict:
        with self.lock:
            why = self._stop_refusal(sb, starting=False)
        if why:
            raise Refused(why)
        # WHAT THE SUPERVISOR CONTAINER IS DOING DECIDES THE PATH, bluntly:
        #   running, restarting, paused  the normal stop: docker stop -t 210,
        #                                then the completion barrier;
        #   exited, created, missing     "already stopped" only if sbx ls shows
        #                                the VM stopped; a VM still running is
        #                                seen to stopped through the barrier;
        #   anything else (dead, removing, no answer ...) refuses the stop.
        state = self._container_state(sb)
        if state in SUPERVISOR_STOPPABLE:
            vm = "running"
            if state != "running":
                # A paused or restarting supervisor may or may not still hold
                # the VM. Only a running VM is asked anything: a listing of a
                # stopped one through sbx exec would wake it.
                worked, vm = self._sbx_ls(sb)
                if not worked:
                    raise Failed(
                        f"the sandbox client did not answer (it said: {vm}), so {sb.name}'s VM "
                        f"state is not known and the {state} supervisor was not stopped"
                    )
            if vm == "running":
                busy = self._helper_busy(sb)
                if busy is None:
                    raise Refused(
                        f"the deploy helper in {sb.name} did not answer GET /busy, so it cannot be "
                        "shown to be idle and the sandbox was not stopped"
                    )
                if busy:
                    shown = "; ".join(json.dumps(item, sort_keys=True)[:120] for item in busy[:5])
                    raise Refused(
                        f"the deploy helper in {sb.name} is still executing {len(busy)} request(s) "
                        f"({shown}), so it was not stopped"
                    )
                self._container_check(sb)
        elif state in SUPERVISOR_NOT_RUNNING:
            worked, vm = self._sbx_ls(sb)
            if not worked:
                raise Failed(f"the sandbox client did not answer (it said: {vm}), so {sb.name}'s VM state is not known")
            if vm in ("stopped", "not listed"):
                self._set_readiness(sb, STOPPED)
                return {"message": f"{sb.name} is already stopped (supervisor {state}, VM {vm})"}
            if vm != "running":
                raise Refused(
                    f"{sb.name}'s supervisor is {state} but its VM shows {vm}, which is neither "
                    "running nor stopped, so it was not stopped"
                )
            # The VM is up without its supervisor: listing its engine does not
            # wake anything, and what runs there decides the stop as usual.
            self._container_check(sb)
        else:
            raise Refused(
                f"{sb.name}'s supervisor container {sb.settings.supervisor} is {state}, a state "
                "the starter does not stop from, so it was not stopped"
            )

        with self.lock:
            why = self._stop_refusal(sb, starting=False)
            if why:
                raise Refused(why)
            self._set_readiness(sb, STOPPING)
            self._record(sb, "stop-delivered", op_id=op.id)
        # FROM HERE ON THE OPERATION STAYS OPEN, blocking every other operation
        # for this sandbox, until the supervisor has exited AND sbx ls shows the
        # VM stopped. A docker command that fails or gives no answer says
        # nothing about the shutdown it started, which may still be running.
        # Past the bounds the sandbox is unresolved until a session clears it.
        exit_deadline = self.clock() + self.settings.stop_grace + self.settings.margin
        said = ""
        if state in SUPERVISOR_STOPPABLE:
            grace = self.settings.stop_grace
            rc, out = self._run(
                [self.settings.docker, "stop", "-t", str(int(grace)), sb.settings.supervisor],
                grace + self.settings.margin,
            )
            if rc != 0:
                said = f"docker stop gave {out[:200] or 'no output'}; "
                self.say(f"{sb.name}: docker stop did not answer cleanly ({out[:200]}); waiting for the supervisor to exit")
            rc, out = self._run(
                [self.settings.docker, "wait", sb.settings.supervisor],
                max(1.0, min(self.settings.margin, exit_deadline - self.clock())),
            )
        self._see_the_stop_through(sb, exit_deadline, said)
        self._set_readiness(sb, STOPPED)
        return {"message": f"{sb.name} is stopped: its supervisor has exited and its VM shows stopped"}

    def _see_the_stop_through(self, sb: Sandbox, exit_deadline: float, said: str = "") -> None:
        """Wait, bounded, for the supervisor to exit and then the VM to stop; else unresolved."""
        while True:
            state = self._container_state(sb)
            if state in EXITED:
                break
            if self.clock() >= exit_deadline:
                self._unresolve(
                    sb,
                    f"{said}the supervisor container {sb.settings.supervisor} was still {state} "
                    f"{int(self.settings.stop_grace + self.settings.margin)}s after its stop was sent",
                )
            self.sleep(self.settings.recovery_poll)
        vm_deadline = self.clock() + self.settings.vm_stop_limit
        while True:
            worked, vm = self._sbx_ls(sb)
            if worked and vm in ("stopped", "not listed"):
                return
            if self.clock() >= vm_deadline:
                self._unresolve(
                    sb,
                    f"{said}the supervisor of {sb.name} has exited but its VM still showed "
                    f"{vm if worked else 'no answer (' + vm + ')'} "
                    f"{int(self.settings.vm_stop_limit)}s later",
                )
            self.sleep(self.settings.vm_poll)

    def _unresolve(self, sb: Sandbox, reason: str) -> None:
        with self.lock:
            sb.unresolved = reason
            self._record(sb, "unresolved", reason=reason)
            self._set_readiness(sb, UNKNOWN)
        self.say(f"{sb.name}: unresolved: {reason}")
        raise Unresolved(self._unresolved_sentence(sb))

    # -- recovery after a restart -------------------------------------------

    def _run_recover(self, sb: Sandbox) -> dict:
        with self.lock:
            pending = list(sb.recovering)
        ids = ", ".join(str(o["id"]) for o in pending)
        delivered = any(o["stop_delivered"] for o in pending)
        bound = self.settings.stop_grace + self.settings.margin
        deadline = self.clock() + bound
        try:
            if delivered:
                # A stop was sent before the restart: see it through exactly as
                # the stop itself would have, supervisor exit and VM stop both.
                self._see_the_stop_through(
                    sb, deadline, f"after a restart of the starter, with operation(s) {ids} unended, ")
                found, readiness = "the supervisor container has exited and its VM shows stopped", STOPPED
            else:
                while True:
                    state = self._container_state(sb)
                    if state in EXITED:
                        found, readiness = f"the supervisor container is {state}", UNKNOWN
                        break
                    if state == "running":
                        found = "the supervisor container is running and no stop was delivered"
                        readiness = None
                        break
                    if self.clock() >= deadline:
                        self._unresolve(
                            sb,
                            f"after a restart of the starter, operation(s) {ids} had no recorded "
                            f"end and, {int(bound)}s later, the supervisor container is {state}",
                        )
                    self.sleep(self.settings.recovery_poll)
        except Unresolved:
            with self.lock:
                for o in pending:
                    self._record(sb, "op-end", op_id=o["id"], op=o["op"], outcome="unresolved", message=sb.unresolved)
                sb.recovering = []
                sb.last_operation = {"op_id": pending[-1]["id"], "op": pending[-1]["op"],
                                     "outcome": "unresolved", "message": sb.unresolved}
            return {"message": sb.unresolved}
        with self.lock:
            for o in pending:
                self._record(sb, "op-end", op_id=o["id"], op=o["op"], outcome="recovered", message=found)
            sb.recovering = []
            sb.last_operation = {"op_id": pending[-1]["id"], "op": pending[-1]["op"],
                                 "outcome": "recovered", "message": found}
        if readiness is None:
            self._services_answered(sb, woke=False)
        else:
            self._set_readiness(sb, readiness)
        self.say(f"{sb.name}: recovered: {found}")
        return {"message": found}

    # -- the requests -------------------------------------------------------

    def sandbox(self, name: str) -> Sandbox:
        if name == NEVER_MANAGED:
            raise Refused(
                f"{NEVER_MANAGED} is the shared sandbox and is never started, stopped or held by this service",
                HTTPStatus.FORBIDDEN,
            )
        if name not in self.sandboxes:
            raise Refused(f"{name} is not in this starter's list of sandboxes", HTTPStatus.NOT_FOUND)
        return self.sandboxes[name]

    def status(self, name: str) -> dict:
        sb = self.sandbox(name)
        state = self._container_state(sb)
        helper = runner = False
        if state == "running":
            helper, runner = self._services(sb)
        now = self.wall()
        with self.lock:
            return {
                "sandbox": sb.name,
                "container": {"name": sb.settings.supervisor, "state": state},
                "vm": {
                    "state": sb.vm_state or UNKNOWN,
                    "seen_at": _iso(sb.vm_seen_at) if sb.vm_seen_at else None,
                    "seen_by": sb.vm_how,
                    "note": "as last seen by the starter during a start or stop; a status request never calls sbx",
                },
                "operations": {
                    "queued": [o.describe() for o in sb.queue if o.id is not None],
                    "running": sb.running.describe() if sb.running and sb.running.id is not None else None,
                    "recovering": [{"id": o["id"], "op": o["op"]} for o in sb.recovering],
                    "unresolved": sb.unresolved,
                    "last": sb.last_operation,
                },
                "holds": [
                    {"id": h["id"], "owner": h["owner"], "until": h["until"],
                     "opened_at": _iso(h["opened"]), "age_seconds": int(now - h["opened"])}
                    for h in sb.holds.values()
                ],
                "managed": {"state": "suspended" if sb.suspended else "active", "reason": sb.suspended},
                "services_ready": helper and runner,
                "services": {"helper": helper, "runner": runner},
                "readiness": sb.readiness,
                "ready_confirmed_for": sb.ready_for if sb.readiness == READY else None,
                "redeploy_pending": sb.readiness == REDEPLOY_PENDING,
                "deployment": sb.deployment(),
            }

    def start(self, name: str, told=None) -> dict:
        sb = self.sandbox(name)
        with self.lock:
            if sb.unresolved:
                raise Refused(self._unresolved_sentence(sb))
            self._note_deployment(sb, told)
            op = self._enqueue(sb, "start")
        return {"operation": op.id, **self._finish(op)}

    def stop(self, name: str, told=None) -> dict:
        sb = self.sandbox(name)
        with self.lock:
            why = self._stop_refusal(sb, starting=True)
            if why:
                raise Refused(why)
            self._note_deployment(sb, told)
            op = self._enqueue(sb, "stop")
        return {"operation": op.id, **self._finish(op)}

    def hold(self, name: str, owner: str, until: str, told=None) -> dict:
        sb = self.sandbox(name)
        if not owner or len(owner) > 200:
            raise Refused("a hold must name its owner (owner=..., at most 200 characters)", HTTPStatus.BAD_REQUEST)
        if until not in ("services", "deployed"):
            raise Refused("until must be services or deployed", HTTPStatus.BAD_REQUEST)
        started = self.clock()
        with self.lock:
            if sb.unresolved:
                raise Refused(self._unresolved_sentence(sb))
            if until == "deployed" and sb.suspended:
                raise Refused(
                    f"{sb.name} is managed: suspended, reason: {sb.suspended}, so it cannot be "
                    "held until deployed; no hold was opened"
                )
            self._note_deployment(sb, told)
            hold_id = f"hold-{uuid.uuid4().hex[:12]}"
            opened = self.wall()
            sb.holds[hold_id] = {"id": hold_id, "owner": owner, "until": until, "opened": opened}
            self._record(sb, "hold-open", hold_id=hold_id, owner=owner, until=until, opened=opened)
            quiet = not sb.busy()
        # ONE DEADLINE for a hold until deployed, from the moment the hold is
        # taken, across waiting in the queue, the start and the redeploy's
        # confirmation, with the suspension checked throughout. With no
        # recorded deployment, deployed is the same as services.
        deployed = until == "deployed" and sb.deployment() is not None
        deadline = started + self.settings.deployed_limit

        def checkpoint() -> None:
            with self.lock:
                if hold_id not in sb.holds:
                    raise Failed(f"hold {hold_id} was ended while it waited for {sb.name}")
                if deployed and sb.suspended:
                    raise Failed(
                        f"{sb.name} became managed: suspended, reason: {sb.suspended}, while "
                        "the hold waited for its recorded deployment"
                    )
            if deployed and self.clock() >= deadline:
                limit = self.settings.deployed_limit
                said = f"{int(limit // 60)} minutes" if limit % 60 == 0 else f"{int(limit)} seconds"
                raise Failed(
                    f"{sb.name}'s recorded deployment was not confirmed back by the coordinator "
                    f"within {said} of the hold being taken, so the hold has ended"
                )

        try:
            checkpoint()
            if quiet and self._container_state(sb) == "running" and all(self._services(sb)):
                readiness = self._services_answered(sb, woke=False)
            else:
                op = self._start_or_join(sb)
                while not self.wait(op.done, 1.0):
                    checkpoint()
                readiness = self._finish(op)["readiness"]
            if not deployed:
                return {"hold_id": hold_id, "readiness": readiness,
                        "message": f"{sb.name} is held for {owner}: services ready"}
            while True:
                checkpoint()
                with self.lock:
                    if self._confirmed(sb):
                        return {"hold_id": hold_id, "readiness": READY,
                                "message": f"{sb.name} is held for {owner}: its recorded deployment is back"}
                self.sleep(1.0)
        except Refused as exc:
            with self.lock:
                if hold_id in sb.holds:
                    sb.holds.pop(hold_id)
                    self._record(sb, "hold-close", hold_id=hold_id, why=f"the hold call failed: {exc.message}")
            exc.extra["hold_id"] = hold_id
            exc.extra["hold_ended"] = True
            raise

    def end_hold(self, hold_id: str) -> dict:
        with self.lock:
            for sb in self.sandboxes.values():
                if hold_id in sb.holds:
                    sb.holds.pop(hold_id)
                    self._record(sb, "hold-close", hold_id=hold_id, why="ended by DELETE")
                    return {"sandbox": sb.name, "message": f"hold {hold_id} on {sb.name} has ended"}
        raise Refused(f"there is no open hold {hold_id}", HTTPStatus.NOT_FOUND)

    def ready(self, name: str, told=None) -> dict:
        """The coordinator confirms the redeploy. When it names the identity it
        redeployed, that must be the sandbox's current one."""
        sb = self.sandbox(name)
        if self._container_state(sb) != "running":
            raise Refused(f"{sb.name}'s supervisor is not running, so its redeploy cannot be ready")
        with self.lock:
            current = sb.deployment()
            if told is not None and (told or None) != current:
                raise Refused(
                    f"the ready names {told or 'no deployment'} but {sb.name}'s recorded deployment is "
                    f"{current or 'none'}, so nothing was recorded"
                )
            self._set_readiness(sb, READY)
        return {"readiness": READY, "confirmed_for": current,
                "message": f"{sb.name} is recorded as ready"}

    def suspend(self, name: str, reason: str) -> dict:
        sb = self.sandbox(name)
        if not reason:
            raise Refused("a suspension must give its reason (reason=...)", HTTPStatus.BAD_REQUEST)
        with self.lock:
            sb.suspended = reason[:500]
            self._record(sb, "suspend", reason=sb.suspended)
        return {"managed": "suspended", "message": f"{sb.name} is managed: suspended, reason: {sb.suspended}"}

    def clear(self, name: str) -> dict:
        sb = self.sandbox(name)
        with self.lock:
            had = [w for w, v in (("a suspension", sb.suspended), ("an unresolved operation", sb.unresolved)) if v]
            sb.suspended = None
            sb.unresolved = None
            self._record(sb, "clear", cleared=had)
        return {"managed": "active",
                "message": f"{sb.name}: cleared {' and '.join(had) if had else 'nothing (nothing was set)'}"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _told_deployment(query: dict):
    """A deployment passed in on a request, or None when nothing was said."""
    if query.get("deployment") == ["none"]:
        return {}
    project = (query.get("compose_project") or [""])[0]
    label = (query.get("identity_label") or [""])[0]
    if not project and not label:
        return None
    try:
        return _deployment_from({"compose_project": project, "identity_label": label}, "the request")
    except ValueError as exc:
        raise Refused(str(exc), HTTPStatus.BAD_REQUEST) from None


def make_handler(starter: Starter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "forge-sandbox-starter"

        def log_message(self, fmt, *args):  # one line per request, to stdout
            starter.say(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _answer(self, status: int, body: dict) -> None:
            data = json.dumps(body, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 65536))
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            one = lambda key: (query.get(key) or [""])[0]  # noqa: E731
            try:
                if len(parts) != 2:
                    raise Refused(f"there is no {method} {parsed.path} here", HTTPStatus.NOT_FOUND)
                verb, name = parts
                if method == "GET" and verb == "status":
                    result = starter.status(name)
                elif method == "POST" and verb == "start":
                    result = starter.start(name, _told_deployment(query))
                elif method == "POST" and verb == "stop":
                    result = starter.stop(name, _told_deployment(query))
                elif method == "POST" and verb == "hold":
                    result = starter.hold(name, one("owner"), one("until"), _told_deployment(query))
                elif method == "DELETE" and verb == "hold":
                    result = starter.end_hold(name)
                elif method == "POST" and verb == "ready":
                    result = starter.ready(name, _told_deployment(query))
                elif method == "POST" and verb == "suspend":
                    result = starter.suspend(name, one("reason"))
                elif method == "POST" and verb == "clear":
                    result = starter.clear(name)
                else:
                    raise Refused(f"there is no {method} {parsed.path} here", HTTPStatus.NOT_FOUND)
            except Refused as exc:
                self._answer(int(exc.status), {"ok": False, "message": exc.message, **exc.extra})
                return
            except Exception as exc:  # noqa: BLE001
                self._answer(500, {"ok": False, "message": f"the starter failed unexpectedly: {exc!r}"})
                return
            self._answer(200, {"ok": True, **result})

        def do_GET(self):  # noqa: N802
            self._route("GET")

        def do_POST(self):  # noqa: N802
            self._route("POST")

        def do_DELETE(self):  # noqa: N802
            self._route("DELETE")

    return Handler


def serve(starter: Starter, address: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((address, port), make_handler(starter))
    server.daemon_threads = True
    return server


def main() -> int:
    try:
        settings = Settings(dict(os.environ))
    except ValueError as exc:
        print(f"sandbox-starter: FATAL: {exc}. Nothing was started.", file=sys.stderr, flush=True)
        return 2
    starter = Starter(settings)
    try:
        starter.boot()
    except (ValueError, OSError) as exc:
        print(f"sandbox-starter: FATAL: {exc}. Nothing was started.", file=sys.stderr, flush=True)
        return 2
    server = serve(starter, settings.listen_address, settings.listen_port)
    starter.say(
        f"listening on {settings.listen_address}:{settings.listen_port} for "
        f"{', '.join(settings.sandboxes)}"
    )

    def on_stop(signum, frame):  # noqa: ARG001
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    server.serve_forever()
    starter.close()
    starter.say("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
