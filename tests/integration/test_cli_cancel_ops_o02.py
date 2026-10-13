"""O-02 / TASK-FWD-005 — ``forge cancel`` works inside the prod container.

The hermetic gate for the ops safety-valve. Three diagnosed breaks, three
proofs, all on stubbed sessions / the in-memory test bus (never the live NATS
bus, forge-prod, or any running container):

* **Break #1 (stale-DB no-op).** ``forge cancel`` resolves the canonical ledger
  (``$FORGE_DB_PATH`` → ``~/.forge/forge.db``) and FAILS LOUDLY — exit 2, DB
  named — when the resolved ledger has no such run, instead of silently
  no-op'ing against a stale mount.
  :class:`TestCanonicalDbResolution`, :class:`TestNoSuchRunFailsLoudly`.
* **Break #2 (``os.getlogin()`` crash).** The responder resolves without a
  controlling terminal, so cancel never raises ``OSError`` under ``docker
  exec``. :class:`TestCancelRoundTripReachesTerminalState`,
  :class:`TestPausedCancelThreadsResponderWithoutTty`.
* **Break #3 (hardcoded ``'rich'`` vs the identity-pinned gate).** The paused
  path's synthetic reject carries the pinned responder, so an identity-pinned
  approval subscriber ACCEPTS it (and refuses a wrong identity).
  :class:`TestPausedCancelIdentityRoundTrip`.

The full-CLI round-trip (fixture run → ``forge cancel --responder <id>`` → run
reaches CANCELLED) uses a real migrated SQLite ledger; the ``build-cancelled``
publish seam is stubbed so no bus is touched.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import forge.cli.queue as queue_module
from forge.adapters.nats.approval_subscriber import (
    APPROVAL_SUBJECT_PREFIX,
    ApprovalSubscriber,
    ApprovalSubscriberDeps,
)
from forge.adapters.nats.synthetic_response_injector import (
    SYNTHETIC_RESPONDER,
    SyntheticResponseInjector,
)
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.cancel import cancel_cmd
from forge.config.models import ApprovalConfig
from forge.gating.identity import derive_request_id
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState

from .conftest import BUILD_ID, RICH, STAGE_LABEL, FakeMonotonicClock, InMemoryNats

#: The forge-prod pinned approver of record (TASK-FWD-005): a Slack member id,
#: NOT the human-readable ``"rich"`` the old injector hardcoded.
PINNED_APPROVER = "U03QR8WKT29"


# ---------------------------------------------------------------------------
# Real-SQLite fixture-run helpers (stubbed session; no NATS, no container)
# ---------------------------------------------------------------------------


def _seed_queued_build(
    db_path: Path, *, feature_id: str, correlation_id: str = "corr-o02"
) -> str:
    """Migrate a fresh ledger and seed one QUEUED (active) build. Returns its id."""
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    persistence = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
    payload = SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/test/test.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter=None,
        originating_user="rich",
        correlation_id=correlation_id,
        parent_request_id=None,
        queued_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        requested_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
    )
    build_id = persistence.record_pending_build(payload)
    cx.close()
    return build_id


def _final_status(db_path: Path, build_id: str) -> BuildState:
    """Re-open the ledger and read the persisted build status."""
    cx = sqlite_connect.connect_writer(db_path)
    try:
        persistence = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        row = persistence.get_build_row(build_id)
        assert row is not None
        return row.status
    finally:
        cx.close()


@pytest.fixture
def stub_publish(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    """Capture ``build-cancelled`` emits so the CLI never contacts a bus."""
    captured: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        queue_module, "publish", lambda subject, body: captured.append((subject, body))
    )
    return captured


# ---------------------------------------------------------------------------
# Break #2 — cancel round-trip reaches a terminal cancelled state (no tty)
# ---------------------------------------------------------------------------


class TestCancelRoundTripReachesTerminalState:
    """Fixture run → ``forge cancel --responder <id>`` → run reaches CANCELLED."""

    def test_cancel_transitions_active_build_to_cancelled(
        self, tmp_path: Path, stub_publish: list[tuple[str, bytes]]
    ) -> None:
        db_path = tmp_path / "forge.db"
        build_id = _seed_queued_build(db_path, feature_id="FEAT-RT01")

        result = CliRunner().invoke(
            cancel_cmd,
            ["FEAT-RT01", "--responder", PINNED_APPROVER, "--db", str(db_path)],
        )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "Cancelled" in result.output
        # The run reached the terminal cancelled state.
        assert _final_status(db_path, build_id) is BuildState.CANCELLED
        # build-cancelled landed on the wire, stamped with the responder.
        assert any(
            subject == "pipeline.build-cancelled.FEAT-RT01"
            for subject, _ in stub_publish
        )
        assert PINNED_APPROVER in result.output

    def test_cancel_resolves_responder_from_env_under_docker_exec(
        self, tmp_path: Path, stub_publish: list[tuple[str, bytes]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Break #2: os.getlogin() raising + no flag → resolves via env, no crash."""
        import os as _os

        def _no_tty() -> str:
            raise OSError(6, "No such device or address")

        monkeypatch.setattr(_os, "getlogin", _no_tty)
        monkeypatch.setenv("FORGE_RESPONDER", PINNED_APPROVER)

        db_path = tmp_path / "forge.db"
        build_id = _seed_queued_build(db_path, feature_id="FEAT-RT02")

        result = CliRunner().invoke(cancel_cmd, ["FEAT-RT02", "--db", str(db_path)])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert _final_status(db_path, build_id) is BuildState.CANCELLED
        assert PINNED_APPROVER in result.output


# ---------------------------------------------------------------------------
# Break #1 — canonical DB resolution + loud failure (no silent no-op)
# ---------------------------------------------------------------------------


class TestCanonicalDbResolution:
    """``forge cancel`` resolves ``$FORGE_DB_PATH`` when ``--db`` is omitted."""

    def test_cancel_uses_forge_db_path_env_without_db_flag(
        self, tmp_path: Path, stub_publish: list[tuple[str, bytes]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "forge.db"
        build_id = _seed_queued_build(db_path, feature_id="FEAT-CANON")
        monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

        # No --db flag: the ledger is resolved canonically, the same source
        # `forge serve` boots against.
        result = CliRunner().invoke(
            cancel_cmd, ["FEAT-CANON", "--responder", PINNED_APPROVER]
        )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert _final_status(db_path, build_id) is BuildState.CANCELLED


class TestNoSuchRunFailsLoudly:
    """A resolved ledger with no such run FAILS LOUDLY (exit 2, DB named)."""

    def test_missing_ledger_exits_two_naming_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nowhere" / "forge.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(missing))

        result = CliRunner().invoke(cancel_cmd, ["FEAT-X", "--responder", "u"])

        assert result.exit_code == 2
        assert "no forge.db" in result.stderr.lower()
        assert str(missing) in result.stderr

    def test_no_such_run_in_live_ledger_exits_two_naming_the_db(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "forge.db"
        _seed_queued_build(db_path, feature_id="FEAT-REAL")

        # The run exists in NO ledger under this id — a stale/wrong-DB footgun.
        # This must be loud, not a silent no-op.
        result = CliRunner().invoke(
            cancel_cmd, ["FEAT-GHOST", "--responder", "u", "--db", str(db_path)]
        )

        assert result.exit_code == 2
        assert "no active or recent build" in result.stderr.lower()
        assert str(db_path) in result.stderr


# ---------------------------------------------------------------------------
# Break #3 — paused cancel: identity-pinned round-trip on the test bus
# ---------------------------------------------------------------------------


async def _await_registered(nats: InMemoryNats, build_id: str) -> None:
    subject = f"{APPROVAL_SUBJECT_PREFIX}.{build_id}.response"
    for _ in range(50):
        if nats.subscribers.get(subject):
            return
        await asyncio.sleep(0)


class TestPausedCancelIdentityRoundTrip:
    """The synthetic reject the CLI injects carries the pinned responder."""

    @pytest.mark.asyncio
    async def test_pinned_responder_is_accepted_and_resolves_as_reject(
        self,
    ) -> None:
        """A cancel stamped with the gate's expected approver lands as a reject."""
        nats = InMemoryNats()
        deps = ApprovalSubscriberDeps(
            nats_client=nats,
            config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=1),
            publish_refresh=None,
            expected_approver=PINNED_APPROVER,  # identity-pinned gate
            clock=FakeMonotonicClock(),
        )
        subscriber = ApprovalSubscriber(deps)
        rid = derive_request_id(
            build_id=BUILD_ID, stage_label=STAGE_LABEL, attempt_count=3
        )
        wait_task = asyncio.create_task(
            subscriber.await_response(BUILD_ID, stage_label=STAGE_LABEL)
        )
        await _await_registered(nats, BUILD_ID)

        # This is what the CLI paused path publishes (via _cancel_gate_inject).
        await SyntheticResponseInjector(nats_client=nats).inject_cli_cancel(
            build_id=BUILD_ID,
            stage_label=STAGE_LABEL,
            attempt_count=3,
            responder=PINNED_APPROVER,
        )

        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is not None, "pinned responder was refused by the gate"
        assert result.decision == "reject"
        assert result.decided_by == PINNED_APPROVER
        assert rid in subscriber._dedup

    @pytest.mark.asyncio
    async def test_wrong_responder_is_refused_and_build_stays_paused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The old hardcoded 'rich' vs a pinned gate → refused, still paused."""
        nats = InMemoryNats()
        deps = ApprovalSubscriberDeps(
            nats_client=nats,
            config=ApprovalConfig(default_wait_seconds=1, max_wait_seconds=1),
            publish_refresh=None,
            expected_approver=PINNED_APPROVER,
            clock=FakeMonotonicClock(),
        )
        subscriber = ApprovalSubscriber(deps)
        rid = derive_request_id(
            build_id=BUILD_ID, stage_label=STAGE_LABEL, attempt_count=0
        )
        wait_task = asyncio.create_task(
            subscriber.await_response(BUILD_ID, stage_label=STAGE_LABEL)
        )
        await _await_registered(nats, BUILD_ID)

        with caplog.at_level(logging.WARNING):
            # The legacy default identity — what a fix-free injector would send.
            await SyntheticResponseInjector(nats_client=nats).inject_cli_cancel(
                build_id=BUILD_ID,
                stage_label=STAGE_LABEL,
                attempt_count=0,
                responder=RICH,  # == SYNTHETIC_RESPONDER, the wrong identity here
            )

        assert RICH == SYNTHETIC_RESPONDER  # documents the regressed default
        assert not wait_task.done(), (
            "wrong responder resolved the wait — the identity-pinned gate "
            "failed to refuse it (break #3 regressed)"
        )
        assert any(
            "unrecognised responder" in rec.message.lower() for rec in caplog.records
        )
        assert rid not in subscriber._dedup  # dedup not poisoned; build stays paused

        wait_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await wait_task


# ---------------------------------------------------------------------------
# 12 October 2026 — cancel a build waiting at its card, signed by the approver
# the card is pinned to, and report it only once the row reads CANCELLED.
#
# These drive the real ``execute_cancel`` against a real SQLite ledger. The
# bus seam (``_inject_synthetic_reject``) carries the rejection to the real
# ``ApprovalSubscriber`` on the in-memory bus, pinned to the approver in the
# same settings file; a stand-in for the coordinator's gate frame cancels the
# row through the real SQLite gate state machine when the subscriber accepts.
# ---------------------------------------------------------------------------

CARD_APPROVER = "U-PINNED"
CARD_STAGE = "Implementation"


def _settings_file(tmp_path: Path, approver: str) -> Path:
    import yaml

    data = {
        "permissions": {"filesystem": {"allowlist": [str(tmp_path)]}},
        "approval": {"expected_approver": approver},
    }
    path = tmp_path / "settings" / "forge.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(data))
    return path


def _seed_paused_build(
    db_path: Path, *, feature_id: str, card_build_id: str | None = None
) -> str:
    """A build waiting at its card; ``card_build_id`` names whose card it is."""
    from forge.lifecycle.persistence import Build
    from forge.lifecycle.state_machine import transition as compose_transition

    build_id = _seed_queued_build(db_path, feature_id=feature_id)
    cx = sqlite_connect.connect_writer(db_path)
    try:
        persistence = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        for frm, to in (
            (BuildState.QUEUED, BuildState.PREPARING),
            (BuildState.PREPARING, BuildState.RUNNING),
        ):
            persistence.apply_transition(
                compose_transition(Build(build_id=build_id, status=frm), to)
            )
        persistence.mark_paused(
            build_id,
            derive_request_id(
                build_id=card_build_id or build_id,
                stage_label=CARD_STAGE,
                attempt_count=0,
            ),
        )
    finally:
        cx.close()
    return build_id


@pytest.fixture
def incident_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Inside the coordinator: user ``forge``, no --config, no ./forge.yaml."""
    monkeypatch.chdir(tmp_path)  # no forge.yaml here
    monkeypatch.setenv("USER", "forge")
    monkeypatch.setenv("LOGNAME", "forge")
    monkeypatch.delenv("FORGE_RESPONDER", raising=False)
    monkeypatch.delenv("FORGE_CONFIG_PATH", raising=False)
    return tmp_path


class _Card:
    """The coordinator's side: a gate waiting on the in-memory bus."""

    def __init__(
        self,
        db_path: Path,
        build_id: str,
        pinned_to: str | None,
        *,
        per_attempt_seconds: float | None = None,
        max_total_wait_seconds: int | None = None,
    ) -> None:
        from forge.gating.sqlite_adapters import build_sqlite_gate_adapters

        self.nats = InMemoryNats()
        self.build_id = build_id
        self.sent: list[str | None] = []
        self._cx = sqlite_connect.connect_writer(db_path)
        pool = SqliteLifecyclePersistence(connection=self._cx, db_path=db_path)
        _repo, self._state_machine = build_sqlite_gate_adapters(
            pool, clock=lambda: datetime.now(UTC)
        )
        self.subscriber = ApprovalSubscriber(
            ApprovalSubscriberDeps(
                nats_client=self.nats,
                config=ApprovalConfig(default_wait_seconds=30, max_wait_seconds=30),
                publish_refresh=None,
                expected_approver=pinned_to,
                clock=FakeMonotonicClock(),
                max_total_wait_seconds=max_total_wait_seconds,
            )
        )
        self._per_attempt_seconds = per_attempt_seconds
        self.response: Any = None

    async def wait(self) -> None:
        """Await the answer and apply it to the row, as the gate frame does."""
        response = await self.subscriber.await_response(
            self.build_id,
            stage_label=CARD_STAGE,
            timeout_seconds=self._per_attempt_seconds,  # type: ignore[arg-type]
        )
        self.response = response
        if response is not None and response.decision == "reject":
            await self._state_machine.transition_to_cancelled(
                build_id=self.build_id, reason=response.notes or "rejected"
            )
        elif response is not None and response.decision == "approve":
            await self._state_machine.transition_to_running(build_id=self.build_id)

    def bus_seam(self, loop: asyncio.AbstractEventLoop) -> Callable[..., None]:
        """Replacement for ``_inject_synthetic_reject`` that uses this bus."""

        def _send(
            *,
            build_id: str,
            stage_label: str,
            attempt_count: int,
            correlation_id: str | None,
            responder: str | None,
        ) -> None:
            self.sent.append(responder)
            asyncio.run_coroutine_threadsafe(
                SyntheticResponseInjector(nats_client=self.nats).inject_cli_cancel(
                    build_id=build_id,
                    stage_label=stage_label,
                    attempt_count=attempt_count,
                    correlation_id=correlation_id,
                    responder=responder,
                ),
                loop,
            ).result(timeout=5)

        return _send

    def close(self) -> None:
        self._cx.close()


async def _cancel_with_card_waiting(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    feature_id: str,
    build_id: str,
    *,
    card_pinned_to: str | None,
    card_options: dict[str, Any] | None = None,
    wait_before_cancel: float = 0.0,
    **cancel_kwargs: object,
) -> tuple[int, _Card]:
    """Run ``execute_cancel`` (in a worker thread) while the card waits."""
    from forge.cli import _cancel_gate_inject
    from forge.cli._cancel_run import execute_cancel

    card = _Card(db_path, build_id, card_pinned_to, **(card_options or {}))
    monkeypatch.setattr(
        _cancel_gate_inject,
        "_inject_synthetic_reject",
        card.bus_seam(asyncio.get_running_loop()),
    )
    waiting = asyncio.create_task(card.wait())
    await _await_registered(card.nats, build_id)
    await asyncio.sleep(wait_before_cancel)

    def _run() -> int:
        try:
            execute_cancel(
                ctx=SimpleNamespace(obj=None),  # no --config, no ./forge.yaml
                identifier=feature_id,
                reason="cli cancel",
                db_path=db_path,
                **cancel_kwargs,  # type: ignore[arg-type]
            )
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    try:
        exit_code = await asyncio.to_thread(_run)
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        card.close()
    return exit_code, card


class TestCancelAtCardSignedByTheConfiguredApprover:
    """``forge cancel`` clears a build waiting at its card (12 October)."""

    @pytest.mark.asyncio
    async def test_incident_cancel_in_coordinator_is_accepted_and_confirmed(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_publish: list[tuple[str, bytes]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No --config, no ./forge.yaml, FORGE_CONFIG_PATH set, USER=forge."""
        from forge.config.loader import load_config

        settings = _settings_file(incident_env, CARD_APPROVER)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD1")

        exit_code, card = await _cancel_with_card_waiting(
            monkeypatch,
            db_path,
            "FEAT-CARD1",
            build_id,
            # The gate is pinned to the approver in the same settings file.
            card_pinned_to=load_config(settings).approval.expected_approver,
            responder=None,
        )

        out = capsys.readouterr()
        assert card.sent == [CARD_APPROVER], out.err
        assert _final_status(db_path, build_id) is BuildState.CANCELLED
        assert exit_code == 0, out.err
        assert "cancelled" in out.out.lower()

    @pytest.mark.asyncio
    async def test_no_identity_known_sends_nothing_and_says_how_to_fix(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No --responder, no $FORGE_RESPONDER and no settings file anywhere."""
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD2")

        exit_code, card = await _cancel_with_card_waiting(
            monkeypatch,
            db_path,
            "FEAT-CARD2",
            build_id,
            card_pinned_to=CARD_APPROVER,
            responder=None,
        )

        err = capsys.readouterr().err
        assert exit_code != 0
        assert card.sent == []
        assert _final_status(db_path, build_id) is BuildState.PAUSED
        assert "settings file" in err and "--config" in err

    @pytest.mark.asyncio
    async def test_responder_differing_from_the_approver_sends_nothing(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = _settings_file(incident_env, CARD_APPROVER)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD3")

        exit_code, card = await _cancel_with_card_waiting(
            monkeypatch,
            db_path,
            "FEAT-CARD3",
            build_id,
            card_pinned_to=CARD_APPROVER,
            responder="someone-else",
        )

        err = capsys.readouterr().err
        assert exit_code != 0
        assert card.sent == []
        assert _final_status(db_path, build_id) is BuildState.PAUSED
        assert CARD_APPROVER in err and "someone-else" in err

    @pytest.mark.asyncio
    async def test_card_naming_another_build_sends_nothing(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = _settings_file(incident_env, CARD_APPROVER)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(
            db_path, feature_id="FEAT-CARD4", card_build_id="build-OTHER-1"
        )

        exit_code, card = await _cancel_with_card_waiting(
            monkeypatch,
            db_path,
            "FEAT-CARD4",
            build_id,
            card_pinned_to=CARD_APPROVER,
            responder=None,
        )

        err = capsys.readouterr().err
        assert exit_code != 0
        assert card.sent == []
        assert _final_status(db_path, build_id) is BuildState.PAUSED
        assert "another build" in err

    def test_row_that_left_paused_sends_nothing(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The card was answered between the lookup and the re-read."""
        from forge.cli import _cancel_gate_inject, _cancel_run
        from forge.lifecycle.persistence import Build
        from forge.lifecycle.state_machine import transition as compose_transition

        settings = _settings_file(incident_env, CARD_APPROVER)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD5")
        sent: list[object] = []
        monkeypatch.setattr(
            _cancel_gate_inject,
            "_inject_synthetic_reject",
            lambda **kw: sent.append(kw),
        )
        real_pending = _cancel_gate_inject.pending_gate_request

        def _answered_first(runtime, *, build_id):  # type: ignore[no-untyped-def]
            runtime.persistence.apply_transition(
                compose_transition(
                    Build(build_id=build_id, status=BuildState.PAUSED),
                    BuildState.RUNNING,
                )
            )
            return real_pending(runtime, build_id=build_id)

        monkeypatch.setattr(_cancel_run, "pending_gate_request", _answered_first)

        with pytest.raises(SystemExit) as exc:
            _cancel_run.execute_cancel(
                ctx=SimpleNamespace(obj=None),
                identifier="FEAT-CARD5",
                reason="cli cancel",
                responder=None,
                db_path=db_path,
            )

        assert exc.value.code != 0
        assert sent == []
        assert _final_status(db_path, build_id) is BuildState.RUNNING
        assert "no longer waiting at its card" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_not_confirmed_when_the_card_refuses_the_rejection(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate is pinned to someone else: the cancel says so, plainly."""
        settings = _settings_file(incident_env, CARD_APPROVER)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD6")

        exit_code, card = await _cancel_with_card_waiting(
            monkeypatch,
            db_path,
            "FEAT-CARD6",
            build_id,
            card_pinned_to="U-SOMEONE-ELSE",
            responder=None,
            confirm_seconds=0.3,
            poll_seconds=0.05,
        )

        captured = capsys.readouterr()
        assert card.sent == [CARD_APPROVER]
        assert exit_code != 0
        assert "did not accept" in captured.err
        assert "cancelled" not in captured.out.lower()
        assert _final_status(db_path, build_id) is BuildState.PAUSED


# ---------------------------------------------------------------------------
# Design 2026-10-13 section 4 step 5: a rejection applies to the gate it was
# issued for, whichever copy of the card it answers.
# ---------------------------------------------------------------------------

FIRST_GATE = "Planning"  # an earlier gate of the same build


class TestRejectionAppliesToItsOwnGate:
    @pytest.mark.asyncio
    async def test_card_replaced_after_the_cli_read_it_still_cancels(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The real gate refreshes its card between the CLI's row read and
        its send; the rejection of the older copy still cancels the build."""
        import threading

        import yaml

        from forge.cli import _cancel_gate_inject, _cancel_run, _serve_deps_gating
        from forge.cli._serve_deps_gating import build_approval_gate_parts
        from forge.cli._serve_deps_lifecycle import build_publisher_and_emitter
        from forge.cli._serve_gate_activation import _GATE_STAGE_LABEL, maybe_gate_build
        from forge.config.loader import load_config
        from forge.gating.degraded import EmptyPriorsReader
        from forge.gating.sqlite_adapters import build_sqlite_gate_adapters

        settings = incident_env / "forge.yaml.d" / "forge.yaml"
        settings.parent.mkdir()
        settings.write_text(
            yaml.safe_dump(
                {
                    "permissions": {"filesystem": {"allowlist": [str(incident_env)]}},
                    # One-second windows so the real refresh runs quickly.
                    "approval": {
                        "expected_approver": CARD_APPROVER,
                        "default_wait_seconds": 1,
                    },
                }
            )
        )
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(settings))
        db_path = incident_env / "forge.db"
        build_id = _seed_queued_build(db_path, feature_id="FEAT-CARD7")

        # The coordinator: the real gate, its real refresh, the same settings.
        _serve_deps_gating._reset_for_tests()
        nats = InMemoryNats()
        cx = sqlite_connect.connect_writer(db_path)
        pool = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        repo, state_machine = build_sqlite_gate_adapters(
            pool, clock=lambda: datetime.now(UTC)
        )
        _publisher, emitter = build_publisher_and_emitter(nats)
        parts = build_approval_gate_parts(
            nats,
            load_config(settings),
            priors_reader=EmptyPriorsReader(),
            emitter=emitter,
            repository=repo,
            bridge_registry=None,
        )
        gate = asyncio.create_task(
            maybe_gate_build(
                parts=parts,
                sqlite_pool=pool,
                gate_repository=repo,
                gate_state_machine=state_machine,
                build_id=build_id,
                feature_id="FEAT-CARD7",
                correlation_id="corr-o02",
                clock=lambda: datetime.now(UTC),
            )
        )

        def _pending() -> str | None:
            row = pool.get_build_row(build_id)
            return row.pending_approval_request_id if row else None

        first = derive_request_id(
            build_id=build_id, stage_label=_GATE_STAGE_LABEL, attempt_count=0
        )
        refreshed = derive_request_id(
            build_id=build_id, stage_label=_GATE_STAGE_LABEL, attempt_count=1
        )
        for _ in range(500):
            if _pending() == first and nats.subscribers.get(
                f"{APPROVAL_SUBJECT_PREFIX}.{build_id}.response"
            ):
                break
            await asyncio.sleep(0.01)
        assert _pending() == first

        # The CLI pauses just after its row read until the card is replaced.
        cli_read, card_replaced = threading.Event(), threading.Event()
        real_pending = _cancel_gate_inject.pending_gate_request

        def _read_then_pause(runtime, *, build_id):  # type: ignore[no-untyped-def]
            request = real_pending(runtime, build_id=build_id)
            cli_read.set()
            assert card_replaced.wait(timeout=10)
            return request

        monkeypatch.setattr(_cancel_run, "pending_gate_request", _read_then_pause)
        loop = asyncio.get_running_loop()
        sent: list[int] = []

        def _send(*, build_id, stage_label, attempt_count, correlation_id, responder):  # type: ignore[no-untyped-def]
            sent.append(attempt_count)
            asyncio.run_coroutine_threadsafe(
                SyntheticResponseInjector(nats_client=nats).inject_cli_cancel(
                    build_id=build_id,
                    stage_label=stage_label,
                    attempt_count=attempt_count,
                    correlation_id=correlation_id,
                    responder=responder,
                ),
                loop,
            ).result(timeout=5)

        monkeypatch.setattr(_cancel_gate_inject, "_inject_synthetic_reject", _send)

        def _run() -> int:
            try:
                _cancel_run.execute_cancel(
                    ctx=SimpleNamespace(obj=None),
                    identifier="FEAT-CARD7",
                    reason="cli cancel",
                    responder=None,
                    db_path=db_path,
                    poll_seconds=0.05,
                )
            except SystemExit as exc:
                return int(exc.code or 0)
            return 0

        try:
            cli = asyncio.create_task(asyncio.to_thread(_run))
            for _ in range(500):
                if cli_read.is_set():
                    break
                await asyncio.sleep(0.01)
            for _ in range(500):  # the real refresh replaces the pending request
                if _pending() == refreshed:
                    break
                await asyncio.sleep(0.01)
            assert _pending() == refreshed, "the gate never refreshed its card"
            card_replaced.set()
            exit_code = await asyncio.wait_for(cli, timeout=15)
            await asyncio.wait_for(gate, timeout=5)
        finally:
            card_replaced.set()
            if not gate.done():
                gate.cancel()
            await asyncio.gather(gate, return_exceptions=True)
            cx.close()
            _serve_deps_gating._reset_for_tests()

        out = capsys.readouterr()
        assert sent == [0], "the CLI should answer the copy it read"
        assert exit_code == 0, out.err
        assert "cancelled" in out.out.lower()
        assert _final_status(db_path, build_id) is BuildState.CANCELLED

    @pytest.mark.asyncio
    async def test_rejection_for_an_earlier_gate_is_dropped(
        self,
        incident_env: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Waiting at its second gate, the build ignores a rejection naming
        its first; a reply queued behind it for the current gate applies."""
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD8")
        card = _Card(db_path, build_id, CARD_APPROVER)
        waiting = asyncio.create_task(card.wait())
        await _await_registered(card.nats, build_id)
        injector = SyntheticResponseInjector(nats_client=card.nats)

        try:
            with caplog.at_level(logging.WARNING):
                await injector.inject_cli_cancel(
                    build_id=build_id,
                    stage_label=FIRST_GATE,
                    attempt_count=0,
                    responder=CARD_APPROVER,
                )
                await asyncio.sleep(0.05)

            assert not waiting.done(), "a rejection for the first gate was applied"
            assert _final_status(db_path, build_id) is BuildState.PAUSED
            assert any(
                "a rejection for another gate was ignored" in rec.message
                for rec in caplog.records
            )

            # Another late first-gate rejection, with the current gate's
            # answer queued right behind it.
            current = derive_request_id(
                build_id=build_id, stage_label=CARD_STAGE, attempt_count=0
            )
            await injector.inject_cli_cancel(
                build_id=build_id,
                stage_label=FIRST_GATE,
                attempt_count=1,
                responder=CARD_APPROVER,
            )
            await card.nats.deliver_response(
                build_id=build_id,
                request_id=current,
                decision="approve",
                decided_by=CARD_APPROVER,
            )
            await asyncio.wait_for(waiting, timeout=5)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            card.close()

        assert card.response.request_id == current
        assert card.response.decision == "approve"
        assert _final_status(db_path, build_id) is BuildState.RUNNING

    @pytest.mark.asyncio
    async def test_rejection_with_an_unparsable_request_id_is_applied(
        self, incident_env: Path
    ) -> None:
        """An old-format id cannot be shown to name another gate: applied."""
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD10")
        card = _Card(db_path, build_id, CARD_APPROVER)
        waiting = asyncio.create_task(card.wait())
        await _await_registered(card.nats, build_id)
        try:
            await card.nats.deliver_response(
                build_id=build_id,
                request_id="legacy-uuid-no-colons",
                decision="reject",
                decided_by=CARD_APPROVER,
            )
            await asyncio.wait_for(waiting, timeout=5)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            card.close()

        assert card.response is not None
        assert card.response.request_id == "legacy-uuid-no-colons"
        assert _final_status(db_path, build_id) is BuildState.CANCELLED

    @pytest.mark.asyncio
    async def test_recovered_gate_applies_a_rejection_after_several_timeouts(
        self,
        incident_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_publish: list[tuple[str, bytes]],
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A recovered gate waits without a refresh publisher, unbounded; its
        windows time out several times, then the CLI's rejection applies."""
        monkeypatch.setenv(
            "FORGE_CONFIG_PATH", str(_settings_file(incident_env, CARD_APPROVER))
        )
        db_path = incident_env / "forge.db"
        build_id = _seed_paused_build(db_path, feature_id="FEAT-CARD9")

        with caplog.at_level(logging.DEBUG, logger="forge.adapters.nats"):
            exit_code, card = await _cancel_with_card_waiting(
                monkeypatch,
                db_path,
                "FEAT-CARD9",
                build_id,
                card_pinned_to=CARD_APPROVER,
                # The rearm path's shape: no refresh publisher, no ceiling.
                card_options={
                    "per_attempt_seconds": 0.05,
                    "max_total_wait_seconds": 0,
                },
                wait_before_cancel=0.3,
                responder=None,
                poll_seconds=0.05,
            )

        timeouts = [
            rec
            for rec in caplog.records
            if "per-attempt timeout with no refresh publisher" in rec.message
        ]
        out = capsys.readouterr()
        assert len(timeouts) >= 3
        assert card.sent == [CARD_APPROVER]
        assert exit_code == 0, out.err
        assert card.response is not None and card.response.decision == "reject"
        assert _final_status(db_path, build_id) is BuildState.CANCELLED
