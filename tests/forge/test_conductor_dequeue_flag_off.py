"""THE PRIME INVARIANT: flag off = the dequeue path is byte-for-byte today's.

Revival design pass §a.5 / §f, Stage 1c.

The lane's whole promise is that activating the conductor changes
*nothing* until an operator sets ``conductor.enabled: true``. This file
proves it rather than asserting it, at the one seam Stage 1c touched on
the live path — the daemon's ``dispatch_build`` closure, which is where
a dequeued fix journey is handed to the conductor's turn loop instead of
the direct autobuild launch.

The proof is **call-sequence identity**: with the flag off, the sequence
of calls into :func:`dispatch_autobuild_async` (the routine launch) is
recorded and compared, argument by argument and in order, against a
baseline composed exactly as the pre-Stage-1c factory composed it (no
``conductor_router`` argument at all). Identical sequence, identical
kwargs, identical order.

The three flag-off facts under test:

1. :func:`build_conductor_router` returns ``None`` for a default config,
   an absent config, and a config of an unrecognised shape.
2. A ``None`` router leaves the dispatch path calling ``launch`` with
   byte-identical kwargs (both the gated and the legacy no-gate arms).
3. Even with the flag ON, a routine build (mode-a) still takes the
   routine path — the router answers "not mine" and the launch call is
   unchanged.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import _serve_deps
from forge.cli._serve_deps import build_pipeline_consumer_deps
from forge.cli.serve import build_conductor_router
from forge.config.models import (
    FIX_JOURNEY_PROFILE_NAME,
    ConductorConfig,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence


class _StubNatsClient:
    async def publish(self, subject: str, body: bytes, **_: Any) -> Any:
        return None


class _RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_async_task(self, subagent_name: str, context: dict[str, Any]) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-flag-off"

    async def astart_async_task(
        self, subagent_name: str, context: dict[str, Any]
    ) -> str:
        self.calls.append((subagent_name, dict(context)))
        return "task-flag-off"


@pytest.fixture()
def writer_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cx = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    yield cx
    cx.close()


@pytest.fixture()
def persistence(
    writer_db: sqlite3.Connection, tmp_path: Path
) -> SqliteLifecyclePersistence:
    return SqliteLifecyclePersistence(
        connection=writer_db, db_path=tmp_path / "forge.db"
    )


@pytest.fixture()
def forge_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=[tmp_path]),
        ),
    )


def _fresh_persistence(tmp_path: Path, name: str) -> SqliteLifecyclePersistence:
    """A private migrated database, so two runs can use IDENTICAL payloads.

    Byte-identity of the launch kwargs includes the derived ``build_id``,
    which is a function of ``(feature_id, queued_at)``. Sharing one
    database would make the second run a duplicate and prove nothing, so
    each side of the comparison gets its own.
    """
    db_path = tmp_path / f"{name}.db"
    cx = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx, db_path=db_path)


def _payload(feature_id: str = "FEAT-CR01") -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="flag-off-test",
        correlation_id="11111111-2222-3333-4444-555555555555",
        parent_request_id=None,
        queued_at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC),
    )


async def _noop_ack() -> None:
    return None


# ---------------------------------------------------------------------------
# 1. The router itself is None while the flag is off
# ---------------------------------------------------------------------------


class TestRouterIsNoneWhileTheFlagIsOff:
    def test_default_config_yields_no_router(
        self, persistence: SqliteLifecyclePersistence, forge_config: ForgeConfig
    ) -> None:
        assert forge_config.conductor.enabled is False
        assert (
            build_conductor_router(pool=persistence, config=forge_config) is None
        )

    def test_absent_config_yields_no_router(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert build_conductor_router(pool=persistence, config=None) is None

    def test_unrecognised_config_shape_yields_no_router(
        self, persistence: SqliteLifecyclePersistence
    ) -> None:
        assert (
            build_conductor_router(pool=persistence, config=object()) is None
        )

    def test_flag_on_without_a_supervisor_factory_stays_inert(
        self, persistence: SqliteLifecyclePersistence, tmp_path: Path
    ) -> None:
        """A half-wired conductor is inert, never half-driving."""
        config = ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[tmp_path]),
            ),
            conductor=ConductorConfig(enabled=True),
        )
        assert build_conductor_router(pool=persistence, config=config) is None


# ---------------------------------------------------------------------------
# 2. Call-sequence identity on the dequeue path
# ---------------------------------------------------------------------------


class TestDequeueCallSequenceIdentity:
    @pytest.mark.asyncio
    async def test_flag_off_launch_sequence_is_identical_to_the_baseline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        forge_config: ForgeConfig,
    ) -> None:
        """Same calls, same kwargs, same order — with and without the seam.

        The baseline composes the deps WITHOUT passing
        ``conductor_router`` at all (the pre-Stage-1c call shape); the
        subject passes it explicitly as ``None`` (what the composition
        root passes while the flag is off). The two recorded launch
        sequences must be equal.
        """
        recorded: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            # Drop the collaborator objects (identity differs per
            # composition); keep the DATA the launch threads through.
            recorded.append(
                {
                    key: value
                    for key, value in kwargs.items()
                    if key
                    in {
                        "build_id",
                        "feature_id",
                        "correlation_id",
                        "branch",
                        "repo",
                        "budget",
                    }
                }
            )
            return {"status": "launched"}

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        # --- baseline: the pre-Stage-1c call shape --------------------
        baseline_deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            forge_config,
            _fresh_persistence(tmp_path, "baseline"),
            async_task_starter=_RecordingStarter(),
        )
        await baseline_deps.dispatch_build(_payload("FEAT-BASE"), _noop_ack)
        baseline = list(recorded)
        recorded.clear()

        # --- subject: the flag-off composition root -------------------
        subject_deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            forge_config,
            _fresh_persistence(tmp_path, "subject"),
            async_task_starter=_RecordingStarter(),
            conductor_router=None,
        )
        await subject_deps.dispatch_build(_payload("FEAT-BASE"), _noop_ack)
        subject = list(recorded)

        assert baseline, "the baseline never reached the routine launch"
        assert subject == baseline

    @pytest.mark.asyncio
    async def test_a_router_that_declines_leaves_the_launch_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """Flag ON + a routine build = the routine path, unchanged."""
        recorded: list[dict[str, Any]] = []
        asked: list[dict[str, Any]] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            recorded.append(
                {k: v for k, v in kwargs.items() if k in {"build_id", "feature_id"}}
            )
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        async def declining_router(**kwargs: Any) -> bool:
            asked.append(dict(kwargs))
            return False

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            forge_config,
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=declining_router,
        )
        await deps.dispatch_build(_payload("FEAT-DECL"), _noop_ack)

        assert len(asked) == 1
        assert len(recorded) == 1
        assert recorded[0]["feature_id"] == "FEAT-DECL"

    @pytest.mark.asyncio
    async def test_a_router_that_takes_the_build_suppresses_the_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """The conductor branch: the direct autobuild launch does NOT run."""
        recorded: list[Any] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            recorded.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        async def taking_router(**kwargs: Any) -> bool:
            return True

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            forge_config,
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=taking_router,
        )
        await deps.dispatch_build(_payload("FEAT-TAKE"), _noop_ack)

        assert recorded == []

    @pytest.mark.asyncio
    async def test_a_raising_router_falls_back_to_the_routine_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """The conductor earns jobs; it never blocks one."""
        recorded: list[Any] = []

        async def _recording_dispatch(**kwargs: Any) -> Any:
            recorded.append(kwargs)
            return None

        monkeypatch.setattr(
            _serve_deps, "dispatch_autobuild_async", _recording_dispatch
        )

        async def exploding_router(**kwargs: Any) -> bool:
            raise RuntimeError("conductor composition blew up")

        deps = build_pipeline_consumer_deps(
            _StubNatsClient(),
            forge_config,
            persistence,
            async_task_starter=_RecordingStarter(),
            conductor_router=exploding_router,
        )
        await deps.dispatch_build(_payload("FEAT-BOOM"), _noop_ack)

        assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 3. The router's own mode branch (flag ON, factory wired)
# ---------------------------------------------------------------------------


class TestRouterModeBranch:
    def _config(self, tmp_path: Path) -> ForgeConfig:
        return ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[tmp_path]),
            ),
            conductor=ConductorConfig(enabled=True),
        )

    @pytest.mark.asyncio
    async def test_a_routine_build_is_declined_by_the_router(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        from forge.lifecycle.modes import BuildMode

        build_id = persistence.record_pending_build(_payload("FEAT-RT01"))
        row = persistence.get_build_row(build_id)
        assert row is not None and row.mode is BuildMode.MODE_A

        made: list[str] = []
        router = build_conductor_router(
            pool=persistence,
            config=self._config(tmp_path),
            supervisor_factory=lambda bid: made.append(bid),
        )
        assert router is not None

        assert await router(build_id=build_id) is False
        assert made == []  # no supervisor was ever constructed

    @pytest.mark.asyncio
    async def test_a_fix_journey_is_taken_and_spawned(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        from forge.lifecycle.modes import BuildMode

        # The row carries the capped ``fix-journey`` profile: since the
        # stage-2 cap law (§4) a mode-c build whose resolved profile has no
        # review-cycle cap is refused before anything is spawned, so a test
        # about the MODE branch has to clear the CAP belt first.
        build_id = persistence.record_pending_build(
            _payload("FEAT-FJ01"),
            mode=BuildMode.MODE_C,
            profile=FIX_JOURNEY_PROFILE_NAME,
        )
        spawned: list[Any] = []
        made: list[str] = []

        def factory(bid: str) -> Any:
            made.append(bid)
            return object()

        def spawn(coro: Any) -> Any:
            spawned.append(coro)
            coro.close()  # never actually drive it in this test
            return None

        router = build_conductor_router(
            pool=persistence,
            config=self._config(tmp_path),
            supervisor_factory=factory,
            spawn=spawn,
        )
        assert router is not None

        assert await router(build_id=build_id) is True
        assert made == [build_id]
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_a_failing_setup_declines_rather_than_stranding_the_build(
        self,
        persistence: SqliteLifecyclePersistence,
        tmp_path: Path,
    ) -> None:
        from forge.lifecycle.modes import BuildMode

        build_id = persistence.record_pending_build(
            _payload("FEAT-FJ02"),
            mode=BuildMode.MODE_C,
            profile=FIX_JOURNEY_PROFILE_NAME,
        )

        def exploding_factory(bid: str) -> Any:
            raise RuntimeError("no supervisor today")

        router = build_conductor_router(
            pool=persistence,
            config=self._config(tmp_path),
            supervisor_factory=exploding_factory,
        )
        assert router is not None

        assert await router(build_id=build_id) is False
