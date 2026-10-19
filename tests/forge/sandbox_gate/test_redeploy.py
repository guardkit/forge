"""The redeploy route: the recorded artifact, back on its target after a start
(design 2.3a, Proof: restore checks).

A fake helper stands in for the promote leg (with the project's health checks
and live gate inside it) and a fake venue for laying the commit's tree out.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.pipeline import merge_executor
from forge.sandbox_gate.gate import READY
from forge.sandbox_gate.redeploy import recorded_targets, redeploy_recorded

from tests.forge.sandbox_gate._fakes import (
    REPO,
    SANDBOX,
    Clock,
    FakeStarter,
    a_build,
    a_config,
    a_gate,
    a_ledger,
)

TARGET = f"{REPO}::production"
COMMIT = "c0ffee1234567890c0ffee1234567890c0ffee12"
IDENTITY = "notes-app@c0ffee1"
ARTIFACT = "notes-app:checked-c0ffee1"


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    cx = a_ledger(tmp_path)
    a_build(cx, "COMPLETE", build_id="B-1")
    cx.execute(
        "INSERT INTO publication_records (build_id, feature_id, repo, j_commit, result, "
        "checked_json) VALUES ('B-1', 'FEAT-1', ?, ?, 'merged and running', ?)",
        (REPO, COMMIT, json.dumps({"artifact": ARTIFACT})),
    )
    cx.execute(
        "INSERT INTO deployment_targets (target, counter, running_commit, running_identity, "
        "running_build) VALUES (?, 3, ?, ?, 'B-1')",
        (TARGET, COMMIT, IDENTITY),
    )
    cx.commit()
    yield cx
    cx.close()


@pytest.fixture(autouse=True)
def _profile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def profile(git: Any, repo_root: Any, commit: Any) -> tuple[Any, None]:
        assert commit == COMMIT  # read at the recorded commit
        return (
            SimpleNamespace(
                env_id="production",
                identity={
                    "setting": "APP_IDENTITY",
                    "reported_as": "RUNNING_IDENTITY",
                    "artifact_setting": "APP_ARTIFACT",
                },
            ),
            None,
        )

    monkeypatch.setattr(merge_executor, "_the_profile_where_it_lives", profile)


class Venue:
    def __init__(self) -> None:
        self.laid: list[tuple[str, str]] = []
        self.removed: list[str] = []

    async def ensure_candidate_trees_excluded(self) -> bool:
        return True

    async def materialise_candidate_tree(self, feature_id: str, sha: str) -> Any:
        self.laid.append((feature_id, sha))
        return SimpleNamespace(path=f"/work/candidates/{feature_id}", confirmed=True)

    async def remove_candidate_tree(self, feature_id: str, path: str | None = None) -> bool:
        self.removed.append(feature_id)
        return True


class Helper:
    """The promote leg, with the project's own health checks inside it."""

    def __init__(self, *, outcome: str = "complete", reports: str | None = IDENTITY, delay_steps: int = 0) -> None:
        self.outcome = outcome
        self.reports = reports
        self.delay_steps = delay_steps
        self.calls: list[dict[str, Any]] = []
        self.lock_seen: list[Any] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        for _ in range(self.delay_steps):  # a database that starts late
            await asyncio.sleep(0)
        output = "starting the checked images\n"
        if self.reports:
            output += f"RUNNING_IDENTITY={self.reports}\n"
        return SimpleNamespace(outcome=self.outcome, confirmed=True, detail={"deploy_output": output})


def _deps(ledger: sqlite3.Connection, helper: Helper, venue: Venue) -> Any:
    return SimpleNamespace(
        config=a_config(),
        pool=SimpleNamespace(connection=ledger),
        git_surface=lambda repo, root: venue,
        deploy_dispatcher=helper,
        http_get=None,
    )


def _lock(ledger: sqlite3.Connection) -> tuple[Any, ...]:
    return ledger.execute(
        "SELECT counter, holder_build, running_commit, running_identity FROM deployment_targets "
        "WHERE target = ?",
        (TARGET,),
    ).fetchone()


class TestTheRoute:
    def test_the_recorded_targets_are_read(self, ledger: sqlite3.Connection) -> None:
        assert recorded_targets(ledger, (REPO,)) == [
            {"repo": REPO, "target": TARGET, "commit": COMMIT, "identity": IDENTITY, "build": "B-1"}
        ]

    @pytest.mark.asyncio
    async def test_the_checked_artifact_is_promoted_and_verified(self, ledger: sqlite3.Connection) -> None:
        helper, venue = Helper(), Venue()
        outcome = await redeploy_recorded(_deps(ledger, helper, venue), SANDBOX, (REPO,))
        assert outcome.ok, outcome.sentence
        [call] = helper.calls
        assert call["leg"] == "promote" and call["dry_run"] is False
        # R5: a restoration folder of its own, never the feature's candidate folder.
        assert call["candidate_cwd"].startswith("/work/candidates/FEAT-R")
        assert call["candidate_cwd"] != "/work/candidates/FEAT-1"
        ownership = call["deploy_ownership"]
        assert ownership["artifact"] == ARTIFACT and ownership["identity"] == IDENTITY
        assert ownership["target"] == TARGET and ownership["build"] == "B-1"
        assert ownership["target_counter"] == 4  # a fresh grant, above the record's 3
        [(restoration, sha)] = venue.laid
        assert restoration.startswith("FEAT-R") and sha == COMMIT
        assert venue.removed == [restoration]  # an answered promote: removed
        counter, holder, commit, identity = _lock(ledger)
        assert holder is None  # the lock is put down
        assert (commit, identity) == (COMMIT, IDENTITY)

    @pytest.mark.asyncio
    async def test_a_proxy_that_is_down_fails_even_when_the_identity_matches(
        self, ledger: sqlite3.Connection
    ) -> None:
        helper = Helper(outcome="failed", reports=IDENTITY)
        outcome = await redeploy_recorded(_deps(ledger, helper, Venue()), SANDBOX, (REPO,))
        assert not outcome.ok and not outcome.retry
        assert "health checks and live gate, did not finish (failed)" in outcome.sentence
        assert _lock(ledger)[1] is None

    @pytest.mark.asyncio
    async def test_a_different_identity_fails(self, ledger: sqlite3.Connection) -> None:
        outcome = await redeploy_recorded(
            _deps(ledger, Helper(reports="notes-app@0ld0ld0"), Venue()), SANDBOX, (REPO,)
        )
        assert not outcome.ok and "reported notes-app@0ld0ld0" in outcome.sentence

    @pytest.mark.asyncio
    async def test_no_identity_reported_fails(self, ledger: sqlite3.Connection) -> None:
        outcome = await redeploy_recorded(_deps(ledger, Helper(reports=None), Venue()), SANDBOX, (REPO,))
        assert not outcome.ok and "no identity at all" in outcome.sentence

    @pytest.mark.asyncio
    async def test_a_lock_another_build_holds_is_asked_for_again(self, ledger: sqlite3.Connection) -> None:
        ledger.execute(
            "UPDATE deployment_targets SET holder_build = 'B-2', expires_at = '2099-01-01T00:00:00+00:00'"
        )
        ledger.commit()
        helper = Helper()
        outcome = await redeploy_recorded(_deps(ledger, helper, Venue()), SANDBOX, (REPO,))
        assert not outcome.ok and outcome.retry and "B-2" in outcome.sentence
        assert helper.calls == []

    @pytest.mark.asyncio
    async def test_a_command_a_takeover_stopped_is_asked_for_again(self, ledger: sqlite3.Connection) -> None:
        helper = Helper(outcome="failed", reports="the-deploy-command-was-stopped-by-a-takeover")
        outcome = await redeploy_recorded(_deps(ledger, helper, Venue()), SANDBOX, (REPO,))
        assert outcome.retry

    @pytest.mark.asyncio
    async def test_no_checked_artifact_fails(self, ledger: sqlite3.Connection) -> None:
        ledger.execute("UPDATE publication_records SET checked_json = '{}'")
        ledger.commit()
        outcome = await redeploy_recorded(_deps(ledger, Helper(), Venue()), SANDBOX, (REPO,))
        assert not outcome.ok and "names no checked artifact" in outcome.sentence

    @pytest.mark.asyncio
    async def test_nothing_recorded_is_nothing_to_do(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        cx = a_ledger(tmp_path / "empty")
        helper = Helper()
        assert (await redeploy_recorded(_deps(cx, helper, Venue()), SANDBOX, (REPO,))).ok
        assert helper.calls == []


class TestThroughTheGate:
    @pytest.mark.asyncio
    async def test_a_late_database_passes_before_ready(self, ledger: sqlite3.Connection) -> None:
        starter = FakeStarter(running=False)
        helper = Helper(delay_steps=20)
        order: list[str] = []
        real_ready = starter.ready_sync

        def ready_sync(name: str, *, deployment: Any = None) -> Any:
            order.append(f"ready after {len(helper.calls)} promote(s)")
            return real_ready(name, deployment=deployment)

        starter.ready_sync = ready_sync  # type: ignore[method-assign]
        deps = _deps(ledger, helper, Venue())

        async def redeploy(name: str, repos: tuple[str, ...]) -> Any:
            return await redeploy_recorded(deps, name, repos)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert order == ["ready after 1 promote(s)"]
        assert gate.state_of(SANDBOX) == READY

    @pytest.mark.asyncio
    async def test_a_proxy_down_suspends_and_the_sandbox_stays_up(self, ledger: sqlite3.Connection) -> None:
        starter = FakeStarter(running=False)
        deps = _deps(ledger, Helper(outcome="failed"), Venue())

        async def redeploy(name: str, repos: tuple[str, ...]) -> Any:
            return await redeploy_recorded(deps, name, repos)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert SANDBOX in starter.suspended and "did not finish" in starter.suspended[SANDBOX]
        assert starter.running[SANDBOX] is True
        assert "ready notes-app-sbx" not in starter.calls
        # A suspended sandbox is never asked to stop.
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls

    @pytest.mark.asyncio
    async def test_the_gate_redeploys_after_its_own_start_even_unflagged(
        self, ledger: sqlite3.Connection
    ) -> None:
        """The starter's settings may name no deployment, so it reports ready
        rather than "redeploy pending"; the ledger records one, so the gate
        that started the sandbox brings it back all the same."""
        starter = FakeStarter(running=False)
        real_start = starter.start_sync

        def start_sync(name: str, *, deployment: Any = None) -> Any:
            answer = real_start(name, deployment=deployment)
            starter.redeploy_pending[name] = False  # its settings name none
            return answer

        starter.start_sync = start_sync  # type: ignore[method-assign]
        helper = Helper()
        deps = _deps(ledger, helper, Venue())

        async def redeploy(name: str, repos: tuple[str, ...]) -> Any:
            return await redeploy_recorded(deps, name, repos)

        gate = a_gate(ledger, starter, clock=Clock(), redeploy=redeploy)
        assert await gate.ensure_ready(SANDBOX)
        assert len(helper.calls) == 1

    @pytest.mark.asyncio
    async def test_a_coordinator_restart_between_redeploy_and_ready_ends_ready_never_stopped(
        self, ledger: sqlite3.Connection
    ) -> None:
        """The first coordinator redeployed and stopped before telling the
        starter; the starter still says "redeploy pending". The new one (every
        sandbox unknown, idle clocks at zero) redeploys again and records
        ready; nothing is stopped on the way."""
        starter = FakeStarter(running=True)
        starter.redeploy_pending[SANDBOX] = True
        helper = Helper()
        deps = _deps(ledger, helper, Venue())

        async def redeploy(name: str, repos: tuple[str, ...]) -> Any:
            return await redeploy_recorded(deps, name, repos)

        clock = Clock()
        gate = a_gate(ledger, starter, clock=clock, redeploy=redeploy)
        assert gate.state_of(SANDBOX) == "unknown"
        clock.advance(2 * 60 * 60)
        await gate.poll_once()
        from tests.forge.sandbox_gate._fakes import settle

        await settle(gate)
        assert gate.state_of(SANDBOX) == READY
        assert len(helper.calls) == 1 and "ready notes-app-sbx" in starter.calls
        await gate.poll_once()
        assert "stop notes-app-sbx" not in starter.calls  # the idle clock starts again


class TestTheRecordIsReadAgainUnderTheLock:
    @pytest.mark.asyncio
    async def test_a_newer_deployment_recorded_meanwhile_is_followed(
        self, ledger: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4: while this attempt reads the profile, another holder records and
        releases a newer deployment; the attempt is abandoned under the lock and
        the current record is the one redeployed."""
        newer_commit = "beef" * 10
        newer_identity = "notes-app@beef000"
        a_build(ledger, "COMPLETE", build_id="B-2")
        ledger.execute(
            "INSERT INTO publication_records (build_id, feature_id, repo, j_commit, result, "
            "checked_json) VALUES ('B-2', 'FEAT-1', ?, ?, 'merged and running', ?)",
            (REPO, newer_commit, json.dumps({"artifact": "notes-app:checked-beef000"})),
        )
        ledger.commit()
        reads: list[str] = []

        async def profile(git: Any, repo_root: Any, commit: Any) -> tuple[Any, None]:
            reads.append(commit)
            if len(reads) == 1:
                # Another holder deploys the newer result and puts its lock down.
                ledger.execute(
                    "UPDATE deployment_targets SET counter = counter + 1, running_commit = ?, "
                    "running_identity = ?, running_build = 'B-2', holder_build = NULL "
                    "WHERE target = ?",
                    (newer_commit, newer_identity, TARGET),
                )
                ledger.commit()
            return (
                SimpleNamespace(
                    env_id="production",
                    identity={"setting": "APP_IDENTITY", "reported_as": "RUNNING_IDENTITY",
                              "artifact_setting": "APP_ARTIFACT"},
                ),
                None,
            )

        monkeypatch.setattr(merge_executor, "_the_profile_where_it_lives", profile)
        helper, venue = Helper(reports=newer_identity), Venue()
        outcome = await redeploy_recorded(_deps(ledger, helper, venue), SANDBOX, (REPO,))
        assert outcome.ok, outcome.sentence
        assert reads == [COMMIT, newer_commit]
        [call] = helper.calls  # only the current record was promoted
        assert call["deploy_ownership"]["identity"] == newer_identity
        assert call["deploy_ownership"]["build"] == "B-2"
        assert [sha for _key, sha in venue.laid] == [newer_commit]  # nothing laid out for the old
        assert _lock(ledger)[2:] == (newer_commit, newer_identity)


class TestRestorationFoldersAfterAnUncertainAnswer:
    @pytest.mark.asyncio
    async def test_a_coordinator_restart_during_an_outstanding_promote_keeps_the_folder(
        self, ledger: sqlite3.Connection
    ) -> None:
        """R5: the promote's answer is lost; the restoration folder is kept and
        written down. After a restart, while the helper still shows a request
        executing, nothing is laid out again; once it is idle the kept folder is
        removed and the redeploy goes on in a new folder."""
        from forge.sandbox_gate.redeploy import outstanding_restorations

        class LostAnswer(Helper):
            async def __call__(self, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                raise TimeoutError("the helper's answer did not come back")

        venue = Venue()
        first = await redeploy_recorded(_deps(ledger, LostAnswer(), venue), SANDBOX, (REPO,))
        assert not first.ok and first.retry  # never a suspension for a lost answer
        [kept] = outstanding_restorations(ledger, (REPO,))
        assert venue.removed == []
        assert _lock(ledger)[1] is None  # the lock is still put down

        # The coordinator restarts: new parts, the same ledger.
        busy = {"executing": [{"route": "/run", "repo": REPO, "seconds": 40}]}
        answers = [busy]

        def helper_get(url: str, timeout: float) -> tuple[int, str]:
            assert url.endswith("/busy")
            return 200, json.dumps(answers[0])

        helper, venue_after = Helper(), Venue()
        deps = _deps(ledger, helper, venue_after)
        deps.http_get = helper_get
        waiting = await redeploy_recorded(deps, SANDBOX, (REPO,))
        assert not waiting.ok and waiting.retry and "may still be running" in waiting.sentence
        assert helper.calls == [] and venue_after.laid == [] and venue_after.removed == []

        answers[0] = {"executing": []}
        done = await redeploy_recorded(deps, SANDBOX, (REPO,))
        assert done.ok, done.sentence
        assert venue_after.removed[0] == kept["restoration"]  # the old folder, first
        [(new_key, _sha)] = venue_after.laid
        assert new_key != kept["restoration"]
        assert outstanding_restorations(ledger, (REPO,)) == []

    @pytest.mark.asyncio
    async def test_no_answer_from_busy_counts_as_busy(self, ledger: sqlite3.Connection) -> None:
        class LostAnswer(Helper):
            async def __call__(self, **kwargs: Any) -> Any:
                raise TimeoutError("lost")

        await redeploy_recorded(_deps(ledger, LostAnswer(), Venue()), SANDBOX, (REPO,))

        def helper_get(url: str, timeout: float) -> tuple[int, str]:
            raise ConnectionRefusedError("helper down")

        deps = _deps(ledger, Helper(), Venue())
        deps.http_get = helper_get
        outcome = await redeploy_recorded(deps, SANDBOX, (REPO,))
        assert outcome.retry and "may still be running" in outcome.sentence
