"""Commands outside the coordinator take a hold at the starter (design 2.3a).

``register-repo`` and ``forge cancel`` hold ``until=services``; ``forge
merge-deploy`` holds ``until=deployed``; each ends its hold afterwards and
refuses in one plain sentence when no hold can be taken. ``estate-check``
reports a stopped managed sandbox as "stopped (managed)" without waking it,
and holds a running one while it asks. A hold racing an idle stop either wins
(the stop is refused) or follows it (the hold starts the sandbox again).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from forge.sandbox_gate import holds as holds_module
from forge.sandbox_gate.gate import READY, STOPPED, UNKNOWN
from forge.sandbox_gate.holds import (
    HoldRefused,
    hold_for_config,
    managed_in_settings,
    starter_hold,
)

from tests.forge.sandbox_gate._fakes import (
    OTHER_REPO,
    REPO,
    SANDBOX,
    Clock,
    FakeStarter,
    a_config,
    a_deployment_target,
    a_gate,
    a_ledger,
    settle,
)


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = a_ledger(tmp_path)
    yield cx
    cx.close()


class TestHoldForConfig:
    def test_an_unmanaged_repository_takes_no_hold(self) -> None:
        starter = FakeStarter()
        with hold_for_config(a_config(), OTHER_REPO, command="x", until="services", client=starter):
            pass
        with hold_for_config(a_config(starter_url=None), REPO, command="x", until="services", client=starter):
            pass
        assert starter.calls == []

    def test_a_managed_repository_is_held_and_released(self, ledger: sqlite3.Connection) -> None:
        starter = FakeStarter(running=False)
        with hold_for_config(
            a_config(), REPO, command="forge merge-deploy", until="deployed",
            connection=ledger, client=starter,
        ) as hold_id:
            assert starter.running[SANDBOX] is True  # the hold started it
            assert hold_id in starter.holds
        assert starter.holds == {}
        assert any(c.startswith("hold notes-app-sbx forge merge-deploy") for c in starter.calls)

    def test_no_recorded_deployment_is_said_to_the_starter(self, ledger: sqlite3.Connection) -> None:
        told: list[Any] = []
        starter = FakeStarter()
        real = starter.hold_sync

        def hold_sync(name: str, **kw: Any) -> Any:
            told.append(kw.get("deployment"))
            return real(name, **kw)

        starter.hold_sync = hold_sync  # type: ignore[method-assign]
        with hold_for_config(a_config(), REPO, command="x", until="deployed", connection=ledger, client=starter):
            pass
        a_deployment_target(ledger, REPO, running_commit="abc1234")
        with hold_for_config(a_config(), REPO, command="x", until="deployed", connection=ledger, client=starter):
            pass
        assert told == [{}, None]  # none; then unsaid, for the starter's settings

    def test_a_hold_that_cannot_be_taken_refuses_in_one_sentence(self) -> None:
        starter = FakeStarter()
        starter.reachable = False
        with pytest.raises(HoldRefused) as refused:
            with hold_for_config(a_config(), REPO, command="forge register-repo", until="services", client=starter):
                raise AssertionError("the body must not run")
        sentence = str(refused.value)
        assert sentence.startswith("forge register-repo did not run:")
        assert "could not be reached" in sentence and "Nothing was changed." in sentence

    def test_a_suspended_sandbox_refuses_a_deployed_hold_at_once(self) -> None:
        starter = FakeStarter(running=True)
        starter.suspended[SANDBOX] = "the proxy did not answer"
        with pytest.raises(HoldRefused, match="managed: suspended"):
            with starter_hold("http://x", SANDBOX, command="forge merge-deploy", until="deployed", client=starter):
                pass
        with starter_hold("http://x", SANDBOX, command="forge register-repo", until="services", client=starter):
            pass  # until=services still works for a suspended sandbox

    def test_a_killed_command_leaves_its_hold_and_the_sandbox_stays_up(self) -> None:
        starter = FakeStarter(running=True)
        starter.release_sync = lambda hold_id: False  # type: ignore[method-assign]  # the end is lost
        with starter_hold("http://x", SANDBOX, command="forge register-repo", until="services", client=starter):
            pass
        assert starter.holds  # still open: the safe direction
        assert starter.stop_sync(SANDBOX).refused


class TestManagedInSettings:
    def test_read_from_the_coordinators_settings(self) -> None:
        settings = {
            "planning": {
                "sandbox_starter": {"url": "http://169.254.0.2:8930/"},
                "sandboxes": {REPO: {"name": SANDBOX, "idle_stop": True}},
            }
        }
        assert managed_in_settings(settings, SANDBOX) == "http://169.254.0.2:8930"
        assert managed_in_settings(settings, "another-sbx") is None
        settings["planning"]["sandboxes"][REPO]["idle_stop"] = False
        assert managed_in_settings(settings, SANDBOX) is None
        assert managed_in_settings({"planning": {"sandboxes": {}}}, SANDBOX) is None
        assert managed_in_settings(None, SANDBOX) is None


class TestRaces:
    @pytest.mark.asyncio
    async def test_a_hold_taken_first_wins_over_an_idle_stop(self, ledger: sqlite3.Connection) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = a_gate(ledger, starter, clock=clock)
        assert await gate.ensure_ready(SANDBOX)
        await gate.poll_once()
        clock.advance(31 * 60)
        before_the_hold = _status_without_holds(starter)  # read a moment earlier
        with starter_hold("http://x", SANDBOX, command="forge register-repo", until="services", client=starter):
            # The gate decided on a status read before the hold; the starter,
            # which decides every stop, refuses it because the hold is open.
            await gate._consider_stop(gate._slots[SANDBOX], before_the_hold)
        assert starter.running[SANDBOX] is True
        assert gate.state_of(SANDBOX) == UNKNOWN
        assert any(c == "stop notes-app-sbx" for c in starter.calls)

    @pytest.mark.asyncio
    async def test_a_stop_that_finishes_first_is_followed_by_the_hold_starting_it(
        self, ledger: sqlite3.Connection
    ) -> None:
        clock = Clock()
        starter = FakeStarter(running=True)
        gate = a_gate(ledger, starter, clock=clock)
        assert await gate.ensure_ready(SANDBOX)
        await gate.poll_once()
        clock.advance(31 * 60)
        await gate.poll_once()
        assert gate.state_of(SANDBOX) == STOPPED and starter.running[SANDBOX] is False
        with starter_hold("http://x", SANDBOX, command="forge register-repo", until="services", client=starter):
            assert starter.running[SANDBOX] is True
            await gate.poll_once()  # the coordinator brings it to ready
            await settle(gate)
            assert gate.state_of(SANDBOX) == READY
            clock.advance(2 * 60 * 60)
            await gate.poll_once()
            await gate.poll_once()
            assert starter.running[SANDBOX] is True  # held: never stopped
        assert starter.calls.count("stop notes-app-sbx") == 1


def _status_without_holds(starter: FakeStarter) -> Any:
    from forge.sandbox_gate.starter_client import read_status

    body = starter.body(SANDBOX)
    body["holds"] = []
    return read_status(SANDBOX, 200, body)


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


class TestMergeDeployHolds:
    def test_merge_deploy_refuses_when_no_hold_can_be_taken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli import merge_deploy as merge_deploy_module
        from forge.cli.merge_deploy import merge_deploy_cmd
        from forge.lifecycle import migrations
        from forge.lifecycle.persistence import SqliteLifecyclePersistence

        cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
        migrations.apply_at_boot(cx)
        pool = SqliteLifecyclePersistence(connection=cx)
        cx.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, feature_yaml_path, status, "
            "triggered_by, correlation_id, queued_at, mode) VALUES ('B-1', 'FEAT-1', ?, "
            "'autobuild/FEAT-1', 'f.yaml', 'COMPLETE', 'jarvis', 'c-1', '2026-10-19T00:00:00Z', 'mode-a')",
            (REPO,),
        )
        cx.commit()
        starter = FakeStarter()
        starter.reachable = False
        monkeypatch.setattr(merge_deploy_module, "_open_pool", lambda _p: pool)
        monkeypatch.setattr(holds_module, "StarterClient", lambda url: starter)

        def no_git(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("nothing may touch the sandbox without a hold")

        monkeypatch.setattr("forge.cli.serve.compose_merge_git_surface", no_git)
        result = CliRunner().invoke(merge_deploy_cmd, ["FEAT-1"], obj=a_config())
        assert result.exit_code != 0
        assert "forge merge-deploy did not run:" in result.output
        assert "could not be held open" in result.output


class TestRegisterRepoHolds:
    def test_register_repo_holds_a_managed_sandbox_and_ends_the_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from forge.cli import register_repo as rr

        settings = (
            "planning:\n  sandbox_starter:\n    url: http://169.254.0.2:8930\n"
            f"  sandboxes:\n    {REPO}:\n      name: {SANDBOX}\n      idle_stop: true\n"
        )
        monkeypatch.setattr(rr.VolumeStore, "read", lambda self, name: settings)
        starter = FakeStarter(running=False)
        monkeypatch.setattr(holds_module, "StarterClient", lambda url: starter)
        seen: list[Any] = []

        def body(options: Any, run: Any, steps: list[Any]) -> list[str]:
            seen.append((starter.running[SANDBOX], dict(starter.holds)))
            return ["done"]

        monkeypatch.setattr(rr, "_register", body)
        steps: list[Any] = []
        options = SimpleNamespace(volume="v", sandbox=SANDBOX)
        assert rr.register(options, lambda *a, **k: None, steps) == ["done"]  # type: ignore[arg-type]
        running, holds_then = seen[0]
        assert running is True and len(holds_then) == 1
        assert starter.holds == {}
        assert any("held open at the sandbox starter" in s.detail for s in steps)

    def test_register_repo_refuses_without_a_starter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from forge.cli import register_repo as rr

        settings = (
            "planning:\n  sandbox_starter:\n    url: http://169.254.0.2:8930\n"
            f"  sandboxes:\n    {REPO}:\n      name: {SANDBOX}\n      idle_stop: true\n"
        )
        monkeypatch.setattr(rr.VolumeStore, "read", lambda self, name: settings)
        starter = FakeStarter()
        starter.reachable = False
        monkeypatch.setattr(holds_module, "StarterClient", lambda url: starter)
        monkeypatch.setattr(rr, "_register", lambda *a: (_ for _ in ()).throw(AssertionError("ran")))
        with pytest.raises(rr.Refused) as refused:
            rr.register(SimpleNamespace(volume="v", sandbox=SANDBOX), None, [])  # type: ignore[arg-type]
        assert "forge register-repo did not run:" in refused.value.detail

    def test_an_unmanaged_sandbox_registers_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from forge.cli import register_repo as rr

        monkeypatch.setattr(rr.VolumeStore, "read", lambda self, name: "planning: {}\n")
        monkeypatch.setattr(rr, "_register", lambda options, run, steps: ["as before"])
        assert rr.register(SimpleNamespace(volume="v", sandbox="api-test-deploy"), None, []) == ["as before"]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# estate-check's item 9 for a managed sandbox
# ---------------------------------------------------------------------------

CHECK = Path(__file__).resolve().parents[3] / "deploy" / "estate" / "estate-check"

_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["CURL_LOG"]
url = args[-1]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
with open(log, "a") as f:
    f.write(f"{method} {url}\n")
state = os.environ.get("STAND_IN_CONTAINER", "running")
if os.environ.get("STAND_IN_STARTER_DOWN"):
    sys.exit(7)
if method == "GET" and "/status/" in url:
    holds = json.loads(os.environ.get("STAND_IN_HOLDS", "[]"))
    print(json.dumps({"ok": True, "container": {"state": state}, "holds": holds}))
elif method == "POST" and "/hold/" in url:
    print(json.dumps({"ok": True, "hold_id": "hold-1"}))
elif method == "DELETE":
    print(json.dumps({"ok": True}))
'''


def _run_item9(tmp_path: Path, **env: str) -> tuple[str, str, str]:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    curl = tools / "curl"
    curl.write_text(_CURL, encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "curl.log"
    log.write_text("", encoding="utf-8")
    block = CHECK.read_text(encoding="utf-8")
    start = block.index("# BEGIN managed sandbox probe")
    end = block.index("# END managed sandbox probe")
    script = (
        "set -uo pipefail\n"
        "RETAINED_SANDBOX=''\n"
        "declare -A ENVV=([SANDBOX_STARTER_URL]='http://169.254.0.2:8930' [STARTER_SANDBOXES]='notes-app-sbx,other-sbx')\n"
        'get() { printf "%s" "${ENVV[$1]:-}"; }\n'
        'found() { ONE_VERDICT="$1"; ONE_SAID="$2"; }\n'
        'probe_one_sandbox() { echo "PROBED $1" >> "$CURL_LOG"; found ok "asked from inside $1"; }\n'
        + block[start:end]
        + '\nprobe_sandbox "$1"\nprintf "%s\\n%s\\n" "$ONE_VERDICT" "$ONE_SAID"\n'
    )
    path = tmp_path / "item9.sh"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run(
        ["bash", str(path), env.pop("NAME", SANDBOX)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "CURL_LOG": str(log), **env},
        timeout=60,
    )
    verdict, _, said = done.stdout.partition("\n")
    return verdict, said.strip(), log.read_text(encoding="utf-8")


class TestEstateCheck:
    def test_a_stopped_managed_sandbox_is_reported_and_not_woken(self, tmp_path: Path) -> None:
        verdict, said, log = _run_item9(tmp_path, STAND_IN_CONTAINER="exited")
        assert verdict == "unchecked"
        assert said.startswith("stopped (managed):")
        assert "/hold/" not in log and "PROBED" not in log

    def test_a_running_managed_sandbox_is_held_while_it_is_asked(self, tmp_path: Path) -> None:
        verdict, said, log = _run_item9(tmp_path)
        assert verdict == "ok"
        lines = log.splitlines()
        assert lines[0].startswith("GET http://169.254.0.2:8930/status/notes-app-sbx")
        assert lines[1].startswith("POST http://169.254.0.2:8930/hold/notes-app-sbx?owner=estate-check&until=services")
        assert lines[2] == "PROBED notes-app-sbx"
        assert lines[3] == "DELETE http://169.254.0.2:8930/hold/hold-1"

    def test_no_starter_answer_leaves_it_untouched(self, tmp_path: Path) -> None:
        verdict, said, log = _run_item9(tmp_path, STAND_IN_STARTER_DOWN="1")
        assert verdict == "fail" and "did not answer" in said
        assert "PROBED" not in log

    def test_a_hold_older_than_a_day_is_reported(self, tmp_path: Path) -> None:
        holds = json.dumps([{"id": "hold-old", "owner": "register-repo x", "age_seconds": 90000}])
        verdict, said, _log = _run_item9(tmp_path, STAND_IN_HOLDS=holds)
        assert "hold-old(register-repo_x)" in said and "older than a day" in said

    def test_an_unmanaged_sandbox_is_asked_as_before(self, tmp_path: Path) -> None:
        verdict, said, log = _run_item9(tmp_path, NAME="api-test-deploy")
        assert verdict == "ok" and log.splitlines() == ["PROBED api-test-deploy"]


class TestCancelHolds:
    def _runtime(self, ledger: sqlite3.Connection) -> Any:
        from types import SimpleNamespace

        from tests.forge.sandbox_gate._fakes import a_build

        a_build(ledger, "RUNNING", build_id="B-9")
        return SimpleNamespace(persistence=SimpleNamespace(connection=ledger))

    def test_cancel_holds_the_sandbox_while_it_reaches_the_runner(
        self, ledger: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from forge.cli import _cancel_run

        starter = FakeStarter(running=False)
        monkeypatch.setattr(holds_module, "StarterClient", lambda url: starter)
        seen: list[Any] = []

        async def interrupt(pool: Any, config: Any, build_id: str) -> bool:
            seen.append((build_id, starter.running[SANDBOX], len(starter.holds)))
            return True

        monkeypatch.setattr(_cancel_run, "interrupt_recorded_run", interrupt)
        assert _cancel_run._interrupt_holding_the_sandbox(self._runtime(ledger), a_config(), "B-9")
        assert seen == [("B-9", True, 1)]
        assert starter.holds == {}

    def test_cancel_refuses_when_no_hold_can_be_taken(
        self, ledger: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from forge.cli import _cancel_run

        starter = FakeStarter()
        starter.reachable = False
        monkeypatch.setattr(holds_module, "StarterClient", lambda url: starter)
        with pytest.raises(SystemExit):
            _cancel_run._interrupt_holding_the_sandbox(self._runtime(ledger), a_config(), "B-9")
        assert "forge cancel did not run:" in capsys.readouterr().err
