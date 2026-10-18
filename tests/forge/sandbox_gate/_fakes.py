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


def a_config(
    *, idle_stop: bool = True, starter_url: str | None = "http://169.254.0.2:8930"
) -> ForgeConfig:
    planning: dict[str, Any] = {
        "target_repo_paths": {REPO: "/tmp/notes-app", OTHER_REPO: "/tmp/plain-app"},
        "sandboxes": {
            REPO: {
                "name": SANDBOX,
                "sidecar_url": HELPER,
                "runner_url": RUNNER,
                "idle_stop": idle_stop,
            },
            OTHER_REPO: {
                "name": "plain-app-sbx",
                "sidecar_url": "http://127.0.0.1:18945",
                "runner_url": "http://127.0.0.1:18944",
            },
        },
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
    """The starter's four jobs and its holds, for one or more sandboxes."""

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
        #: What each start and stop was told about the recorded deployment.
        self.told: list[tuple[str, Any]] = []
        self.stop_gate: asyncio.Event | None = None

    # -- the JSON the real starter answers with --------------------------------

    def body(self, name: str) -> dict[str, Any]:
        """The starter's own ``GET /status/<name>`` answer."""
        running = self.running.get(name, False)
        queued: list[dict[str, Any]] = []
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
                "queued": queued,
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
            "deployment": None,
        }

    def _tick_start(self, name: str) -> None:
        if name in self._reads_until_up:
            self._reads_until_up[name] -= 1
            if self._reads_until_up[name] <= 0:
                del self._reads_until_up[name]
                self.running[name] = True
                self.redeploy_pending[name] = self.told[-1][1] != {} if self.told else True

    # -- the client's interface ----------------------------------------------------

    def status_sync(self, name: str) -> StarterStatus:
        self.calls.append(f"status {name}")
        if not self.reachable:
            return StarterStatus(name=name, reached=False, why="the starter could not be reached")
        if not self.listed:
            return read_status(name, 404, {"refusal": f"{name} is not in my list"})
        self._tick_start(name)
        return read_status(name, 200, self.body(name))

    def start_sync(self, name: str, *, deployment: Any = None) -> StarterAnswer:
        self.calls.append(f"start {name}")
        self.told.append(("start", deployment))
        if self.start_refusal:
            return StarterAnswer(ok=False, refused=True, sentence=self.start_refusal)
        if self.start_delay_reads:
            self._reads_until_up[name] = self.start_delay_reads
        else:
            self.running[name] = True
            self.redeploy_pending[name] = deployment != {}
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
        self.running[name] = False
        self.redeploy_pending.pop(name, None)
        if self.lose_stop_answer:
            return StarterAnswer(ok=False, sentence="the answer was lost")
        return StarterAnswer(ok=True)

    def ready_sync(self, name: str) -> StarterAnswer:
        self.calls.append(f"ready {name}")
        self.redeploy_pending.pop(name, None)
        return StarterAnswer(ok=True)

    def suspend_sync(self, name: str, reason: str) -> StarterAnswer:
        self.calls.append(f"suspend {name}: {reason}")
        self.redeploy_pending.pop(name, None)
        self.suspended[name] = reason
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
        if not self.reachable:
            return HoldAnswer(None, "the starter could not be reached")
        self._next_hold += 1
        hold_id = f"h{self._next_hold}"
        self.holds[hold_id] = (name, owner, until)
        if until == "deployed" and name in self.suspended:
            del self.holds[hold_id]
            return HoldAnswer(None, f"{name} is managed: suspended")
        if not self.running.get(name):
            self.running[name] = True
            self.redeploy_pending[name] = deployment != {}
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

    async def ready(self, name: str) -> StarterAnswer:
        return self.ready_sync(name)

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
        if not self.starter.running.get(SANDBOX):
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
