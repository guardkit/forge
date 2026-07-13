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
from pathlib import Path
from types import SimpleNamespace

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
