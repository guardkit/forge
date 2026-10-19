"""Stand-ins for the sandbox starter and the two services it starts.

The fake starter keeps the facts the real one reports — whether the
supervisor is running, any operation queued or running, holds, a suspension,
"redeploy pending" after a start — and answers in the JSON the real one is
written to, so the client's own reading of a status is exercised too.
Nothing opens a socket. Every name here is invented.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.sandbox_gate.gate import SandboxGate, managed_sandboxes
from forge.sandbox_gate.starter_client import (
    HoldAnswer,
    StarterAnswer,
    StarterStatus,
    read_status,
)

REPO = "example-org/notes-app"
OTHER_REPO = "example-org/plain-app"
SANDBOX = "notes-app-sbx"
HELPER = "http://127.0.0.1:18935"
RUNNER = "http://127.0.0.1:18934"
START = datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc)
#: A second managed project, for the tests that need two sandboxes.
SECOND_REPO = "example-org/recipes-app"
SECOND_SANDBOX = "recipes-app-sbx"
SECOND_HELPER = "http://127.0.0.1:18955"
SECOND_RUNNER = "http://127.0.0.1:18954"


#: How the example project's deployment is named to the starter.
COMPOSE_PROJECT = "notes-live"
LABEL_KEY = "org.example.identity"


def a_config(
    *,
    idle_stop: bool = True,
    starter_url: str | None = "http://169.254.0.2:8930",
    names_its_deployment: bool = True,
    two_managed: bool = False,
) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {REPO: "/tmp/notes-app", OTHER_REPO: "/tmp/plain-app"},
        "sandboxes": {
            REPO: {
                "name": SANDBOX,
                "sidecar_url": HELPER,
                "runner_url": RUNNER,
                "idle_stop": idle_stop,
                **(
                    {
                        "deployment_compose_project": COMPOSE_PROJECT,
                        "deployment_identity_label": LABEL_KEY,
                    }
                    if names_its_deployment
                    else {}
                ),
            },
            OTHER_REPO: {
                "name": "plain-app-sbx",
                "sidecar_url": "http://127.0.0.1:18945",
                "runner_url": "http://127.0.0.1:18944",
            },
        },
    }
    if two_managed:
        planning["target_repo_paths"][SECOND_REPO] = "/tmp/recipes-app"
        planning["sandboxes"][SECOND_REPO] = {
            "name": SECOND_SANDBOX,
            "sidecar_url": SECOND_HELPER,
            "runner_url": SECOND_RUNNER,
            "idle_stop": True,
        }
    if starter_url is not None:
        planning["sandbox_starter"] = {"url": starter_url}
    return ForgeConfig.model_validate(
        {"permissions": {"filesystem": {"allowlist": ["/tmp"]}}, "planning": planning}
    )


def a_ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    from forge.lifecycle.missions import ensure_tables

    ensure_tables(cx)
    return cx


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.instant = start

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant = self.instant + timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class FakeStarter:
    """The starter's jobs and its holds, with the real starter's semantics for
    what it is told (deploy/compose/sandbox-starter/starter.py, 69692e62):

    - the deployment it is told is kept, durably; one not said leaves it;
    - a start reports "services ready, redeploy pending" when it has a
      deployment, else "ready"; ready is ready FOR one deployment, and a new or
      changed deployment invalidates it;
    - ``/ready`` is refused when the supervisor is not running, or when it
      names another deployment than the current one;
    - a suspension is durable and changes nothing else: stops are refused,
      ``until=deployed`` holds refused, ``until=services`` holds accepted,
      readiness kept as it was, until ``/clear``.
    """

    def __init__(self, *, running: bool = False, listed: bool = True) -> None:
        self.running: dict[str, bool] = {SANDBOX: running}
        self.listed = listed
        self.reachable = True
        self.calls: list[str] = []
        #: An operation the starter reports as queued or running.
        self.pending_operation: str | None = None
        self.redeploy_pending: dict[str, bool] = {}
        self.suspended: dict[str, str] = {}
        self.unresolved: dict[str, str] = {}
        self.holds: dict[str, tuple[str, str, str]] = {}
        self.stop_refusal: str | None = None
        #: A stop that happens but whose answer is lost.
        self.lose_stop_answer = False
        #: A start the starter refuses, with this sentence.
        self.start_refusal: str | None = None
        #: The supervisor comes up only after this many status reads.
        self.start_delay_reads = 0
        self._reads_until_up: dict[str, int] = {}
        self._next_hold = 0
        self.stop_gate: asyncio.Event | None = None
        #: What each start, stop, hold and ready was told about the deployment.
        self.told: list[tuple[str, Any]] = []
        #: The deployment as told (``{}`` = none); absent = never told.
        self.deployment: dict[str, dict[str, Any]] = {}

    def current(self, name: str) -> dict[str, Any] | None:
        return self.deployment.get(name) or None

    def _note(self, name: str, told: Any) -> None:
        if told is None:
            return
        before = self.current(name)
        self.deployment[name] = dict(told)
        if self.current(name) != before and self.running.get(name):
            # A new or changed identity invalidates readiness (the real rule).
            self.redeploy_pending[name] = self.current(name) is not None

    def _woke(self, name: str) -> None:
        self.running[name] = True
        self.redeploy_pending[name] = self.current(name) is not None

    # -- the JSON the real starter answers with --------------------------------

    def body(self, name: str) -> dict[str, Any]:
        """The starter's own ``GET /status/<name>`` answer."""
        running = self.running.get(name, False)
        busy_op = None
        if self.pending_operation:
            busy_op = {"id": 7, "op": self.pending_operation, "queued_at": "x"}
        if name in self._reads_until_up:
            busy_op = {"id": 8, "op": "start", "queued_at": "x"}
        readiness = "stopped"
        if busy_op is not None and busy_op["op"] == "start":
            readiness = "starting"
        elif running:
            readiness = (
                "services ready, redeploy pending" if self.redeploy_pending.get(name) else "ready"
            )
        return {
            "ok": True,
            "sandbox": name,
            "container": {
                "name": f"forge-sbx-{name}-sandbox-runner-1",
                "state": "running" if running else "exited",
            },
            "vm": {"state": "running" if running else "stopped"},
            "operations": {
                "queued": [],
                "running": busy_op,
                "recovering": [],
                "unresolved": self.unresolved.get(name),
                "last": None,
            },
            "holds": [
                {"id": hid, "owner": owner, "until": until, "age_seconds": 1}
                for hid, (hname, owner, until) in self.holds.items()
                if hname == name
            ],
            "managed": {
                "state": "suspended" if name in self.suspended else "active",
                "reason": self.suspended.get(name),
            },
            "services_ready": running,
            "readiness": readiness,
            "redeploy_pending": readiness == "services ready, redeploy pending",
            "deployment": self.current(name),
        }

    def _tick_start(self, name: str) -> None:
        if name in self._reads_until_up:
            self._reads_until_up[name] -= 1
            if self._reads_until_up[name] <= 0:
                del self._reads_until_up[name]
                self._woke(name)

    # -- the client's interface ----------------------------------------------------

    def status_sync(self, name: str) -> StarterStatus:
        self.calls.append(f"status {name}")
        if not self.reachable:
            return StarterStatus(name=name, reached=False, why="the starter could not be reached")
        if not self.listed:
            return read_status(name, 404, {"ok": False, "message": f"{name} is not in my list"})
        self._tick_start(name)
        return read_status(name, 200, self.body(name))

    def start_sync(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        self.calls.append(f"start {name}")
        self.told.append(("start", deployment))
        if self.start_refusal:
            return StarterAnswer(ok=False, refused=True, sentence=self.start_refusal)
        self._note(name, deployment)
        if self.start_delay_reads:
            self._reads_until_up[name] = self.start_delay_reads
        elif not self.running.get(name):
            self._woke(name)
        return StarterAnswer(ok=True)

    def stop_sync(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        self.calls.append(f"stop {name}")
        self.told.append(("stop", deployment))
        if self.stop_refusal:
            return StarterAnswer(ok=False, refused=True, sentence=self.stop_refusal)
        open_holds = [owner for (hname, owner, _u) in self.holds.values() if hname == name]
        if open_holds:
            return StarterAnswer(
                ok=False, refused=True, sentence=f"a hold is open ({open_holds[0]})"
            )
        if name in self.suspended:
            return StarterAnswer(ok=False, refused=True, sentence="management is suspended")
        self._note(name, deployment)
        self.running[name] = False
        self.redeploy_pending.pop(name, None)
        if self.lose_stop_answer:
            return StarterAnswer(ok=False, sentence="the answer was lost")
        return StarterAnswer(ok=True)

    def ready_sync(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        self.calls.append(f"ready {name}")
        self.told.append(("ready", deployment))
        if not self.running.get(name):
            return StarterAnswer(ok=False, refused=True, sentence="the supervisor is not running")
        if deployment is not None and (deployment or None) != self.current(name):
            return StarterAnswer(
                ok=False, refused=True, sentence="the ready names another deployment"
            )
        self.redeploy_pending.pop(name, None)
        return StarterAnswer(ok=True)

    def suspend_sync(self, name: str, reason: str) -> StarterAnswer:
        self.calls.append(f"suspend {name}: {reason}")
        self.suspended[name] = reason
        return StarterAnswer(ok=True)

    def clear_sync(self, name: str) -> StarterAnswer:
        self.calls.append(f"clear {name}")
        self.suspended.pop(name, None)
        self.unresolved.pop(name, None)
        return StarterAnswer(ok=True)

    def hold_sync(
        self,
        name: str,
        *,
        owner: str,
        until: str,
        deployment: Any = None,
        timeout_s: float | None = None,
    ) -> HoldAnswer:
        self.calls.append(f"hold {name} {owner} {until}")
        self.told.append(("hold", deployment))
        if not self.reachable:
            return HoldAnswer(None, "the starter could not be reached")
        if until == "deployed" and name in self.suspended:
            return HoldAnswer(None, f"{name} is managed: suspended")
        self._note(name, deployment)
        self._next_hold += 1
        hold_id = f"h{self._next_hold}"
        self.holds[hold_id] = (name, owner, until)
        if not self.running.get(name):
            self._woke(name)
        return HoldAnswer(hold_id)

    def release_sync(self, hold_id: str) -> bool:
        self.calls.append(f"release {hold_id}")
        return self.holds.pop(hold_id, None) is not None

    async def status(self, name: str) -> StarterStatus:
        return self.status_sync(name)

    async def start(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        return self.start_sync(name, deployment=deployment)

    async def stop(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        if self.stop_gate is not None:
            self.calls.append(f"stop {name} (waiting)")
            await self.stop_gate.wait()
        return self.stop_sync(name, deployment=deployment)

    async def ready(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        return self.ready_sync(name, deployment=deployment)

    async def suspend(self, name: str, reason: str) -> StarterAnswer:
        return self.suspend_sync(name, reason)


class Services:
    """The helper's ``/healthz`` and the runner's ``/ok``, up while the starter
    says the supervisor is running (unless a test takes one down)."""

    def __init__(self, starter: FakeStarter) -> None:
        self.starter = starter
        self.helper_down = False
        self.runner_down = False
        self.asked: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, str]:
        self.asked.append(url)
        name = SECOND_SANDBOX if (SECOND_HELPER in url or SECOND_RUNNER in url) else SANDBOX
        if not self.starter.running.get(name):
            raise ConnectionRefusedError("nothing listening")
        if url.endswith("/healthz"):
            if self.helper_down:
                raise ConnectionRefusedError("helper down")
            return 200, '{"status": "healthy", "rev": "git-0000000"}'
        if url.endswith("/ok"):
            if self.runner_down:
                raise ConnectionRefusedError("runner down")
            return 200, '{"ok": true}'
        return 404, "{}"


def a_gate(
    connection: sqlite3.Connection | None,
    starter: FakeStarter,
    *,
    clock: Clock,
    services: Services | None = None,
    redeploy: Any = None,
    config: ForgeConfig | None = None,
    ready_timeout_seconds: float = 600.0,
    idle_minutes: int = 30,
) -> SandboxGate:
    return SandboxGate(
        sandboxes=managed_sandboxes(config or a_config()),
        starter=starter,  # type: ignore[arg-type]
        connection=lambda: connection,
        idle_minutes=idle_minutes,
        ready_timeout_seconds=ready_timeout_seconds,
        redeploy=redeploy,
        http_get=services or Services(starter),
        clock=clock.now,
        sleep=clock.sleep,
    )


async def stopped(gate: SandboxGate, name: str = SANDBOX) -> None:
    """Wait for the stop the poll started for ``name`` (it runs on its own)."""
    task = gate._slots[name].stop_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)


async def settle(gate: SandboxGate, repo: str = REPO) -> None:
    """Wait for the background bring-up of ``repo``'s sandbox to finish."""
    slot = gate._slot_for(repo) or gate._by_repo.get(repo)
    if slot is not None and slot.bring_up is not None:
        await asyncio.wait_for(asyncio.shield(slot.bring_up), timeout=10)


# ---------------------------------------------------------------------------
# Ledger rows, one of each kind the idle rule reads
# ---------------------------------------------------------------------------

_WHEN = "2026-10-01T09:00:00+00:00"


def a_queue_row(cx: sqlite3.Connection, status: str, repo: str = REPO) -> None:
    cx.execute(
        "INSERT INTO work_queue (sentence, target_repo, kind, status, rank, originating_user, "
        "correlation_id, queued_at) VALUES ('add tags', ?, 'feature', ?, 0, 'U', ?, ?)",
        (repo, status, f"wq-{status}-{repo}", _WHEN),
    )
    cx.commit()


def a_planning_run(cx: sqlite3.Connection, state: str, repo: str = REPO) -> None:
    cx.execute(
        "INSERT INTO planning_runs (correlation_id, state, originating_user, expected_approver, "
        "request_text, triggered_by, queued_at, target_repo) VALUES (?, ?, 'U', 'U', 'add tags', "
        "'jarvis', ?, ?)",
        (f"pr-{state}-{repo}", state, _WHEN, repo),
    )
    cx.commit()


def a_build(cx: sqlite3.Connection, status: str, build_id: str = "B-1", repo: str = REPO) -> None:
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, status, "
        "triggered_by, originating_user, correlation_id, queued_at) VALUES "
        "(?, 'FEAT-1', ?, 'main', 'x.yaml', ?, 'jarvis', 'U', ?, ?)",
        (build_id, repo, status, f"c-{build_id}", _WHEN),
    )
    cx.commit()


def a_mission(cx: sqlite3.Connection, status: str, repo: str = REPO) -> None:
    cx.execute(
        "INSERT INTO missions (id, title, target_repo, branch, status, correlation_id, "
        "originating_user, created_at) VALUES (?, 'Tags', ?, 'mission/tags', ?, ?, 'U', ?)",
        (f"M-{status}", repo, status, f"m-{status}", _WHEN),
    )
    cx.commit()


def a_stage_row(cx: sqlite3.Connection, build_id: str, target: str) -> None:
    cx.execute(
        "INSERT INTO stage_log (build_id, stage_label, target_kind, target_identifier, status, "
        "started_at, completed_at, duration_secs, details_json) VALUES "
        "(?, 'merge', 'subagent', ?, 'PASSED', ?, ?, 0, '{}')",
        (build_id, target, _WHEN, _WHEN),
    )
    cx.commit()


def a_preview_lease(cx: sqlite3.Connection, state: str, repo: str = REPO) -> None:
    cx.execute(
        "INSERT INTO preview_leases (lease_id, token, project, feature_id, build_id, "
        "candidate_sha, candidate_tree, run_id, state, created_at, app_expires_at, "
        "evidence_expires_at, updated_at) VALUES (?, 't', ?, 'FEAT-1', 'B-1', 'a', 'b', ?, ?, "
        "?, ?, ?, ?)",
        (f"L-{state}", repo, f"r-{state}", state, _WHEN, _WHEN, _WHEN, _WHEN),
    )
    cx.commit()


def a_publication(
    cx: sqlite3.Connection,
    *,
    result: str | None,
    lease_holder: str | None = None,
    build_id: str = "B-1",
    repo: str = REPO,
) -> None:
    cx.execute(
        "INSERT INTO publication_records (build_id, feature_id, repo, result, lease_holder, "
        "lease_expires_at) VALUES (?, 'FEAT-1', ?, ?, ?, ?)",
        (build_id, repo, result, lease_holder, "2026-10-01T09:30:00+00:00"),
    )
    cx.commit()


def a_deployment_target(
    cx: sqlite3.Connection,
    target: str,
    *,
    holder: str | None = None,
    running_commit: str | None = None,
) -> None:
    cx.execute(
        "INSERT INTO deployment_targets (target, counter, holder_build, expires_at, "
        "running_commit) VALUES (?, 1, ?, '2026-10-01T09:30:00+00:00', ?)",
        (target, holder, running_commit),
    )
    cx.commit()
