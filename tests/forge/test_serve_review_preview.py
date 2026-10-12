"""The review-preview service in the serve wiring (web-readiness W7).

One service is composed with the merge press, handed to the merge card's
offer and to the merge press's consumer, reconciled once at start-up before
the daemon can offer a card, and swept beside the daemon while it runs.
Without the module, or with the merge press off, none of it exists and the
serve loop is what it was.

Network-free: the NATS client, the gating parts and the consumer deps are the
same fakes ``tests/forge/test_serve_conductor_composition.py`` uses for the
same closure, and the service is the stand-in in
``tests/forge/_a_stand_in_review_preview.py``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli import serve as serve_mod
from forge.cli._serve_config import ServeConfig
from forge.cli._serve_state import SubscriptionState
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from tests.forge._a_stand_in_review_preview import StandInReviewPreview
from tests.forge.test_cli_serve_skeleton import _StubNatsClient


@pytest.fixture
def pool(tmp_path: Path) -> SqliteLifecyclePersistence:
    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    return SqliteLifecyclePersistence(connection=cx)


@pytest.fixture(autouse=True)
def _no_service_left_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts, and leaves the module, with no service composed."""
    monkeypatch.setattr(serve_mod, "review_preview_service", None)


def _config(*, merge_on: bool) -> ForgeConfig:
    raw: dict[str, Any] = {
        "pipeline": {
            "build_queue_subject": "pipeline.build-queued.team-a",
            "approved_originators": ["terminal"],
        },
        "permissions": {"filesystem": {"allowlist": ["/work"]}},
    }
    if merge_on:
        raw["merge_executor"] = {"enabled": True}
    return ForgeConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Composing the service
# ---------------------------------------------------------------------------


class TestComposingTheService:
    def test_it_is_built_on_the_ledger_and_the_press_s_deploy_dispatcher(
        self, monkeypatch: pytest.MonkeyPatch, pool, tmp_path: Path
    ) -> None:
        built: list[tuple[Any, dict[str, Any]]] = []
        dispatchers: list[dict[str, Any]] = []
        dispatcher = object()

        def _build(config: Any, **kwargs: Any) -> Any:
            built.append((config, kwargs))
            return "the service"

        def _dispatcher(**kwargs: Any) -> Any:
            dispatchers.append(kwargs)
            return dispatcher

        monkeypatch.setattr(
            "forge.deploy.review_preview.build_review_preview_service", _build
        )
        monkeypatch.setattr(
            "forge.pipeline.merge_executor.build_in_daemon_deploy_dispatcher",
            _dispatcher,
        )
        config = _config(merge_on=True)
        client = object()

        service = serve_mod.compose_review_preview(
            forge_config=config,
            sqlite_pool=pool,
            nats_client=client,
            db_path=tmp_path / "forge.db",
        )

        assert service == "the service"
        assert built == [
            (config, {"connection": pool.connection, "deploy_dispatcher": dispatcher})
        ]
        assert dispatchers == [
            {"config": config, "nats_client": client, "db_path": tmp_path / "forge.db"}
        ]

    def test_the_real_service_composes_from_a_plain_config(
        self, pool, tmp_path: Path
    ) -> None:
        from forge.deploy.review_preview import ReviewPreviewService

        service = serve_mod.compose_review_preview(
            forge_config=_config(merge_on=True),
            sqlite_pool=pool,
            nats_client=MagicMock(),
            db_path=tmp_path / "forge.db",
        )
        assert isinstance(service, ReviewPreviewService)
        # A project with no deploy profile here has no preview.
        assert service.enabled_for("appmilla/nothing-here") is False

    def test_a_composition_that_raises_leaves_previews_off_not_boot(
        self, monkeypatch: pytest.MonkeyPatch, pool, tmp_path: Path
    ) -> None:
        def _raises(**_k: Any) -> Any:
            raise RuntimeError("no ledger")

        monkeypatch.setattr(serve_mod, "compose_review_preview", _raises)
        seen = _drive_boot(pool, tmp_path, merge_on=True, preview=None, compose=False)
        assert len(seen["offer"]) == 1
        assert "review_preview" not in seen["offer"][0]
        assert serve_mod.review_preview_service is None


def _drive_boot(
    pool: SqliteLifecyclePersistence,
    tmp_path: Path,
    *,
    merge_on: bool,
    preview: Any,
    compose: bool = True,
) -> dict[str, Any]:
    """Run the production dispatch-chain composition once; say what was built."""
    seen: dict[str, Any] = {"offer": [], "consumer": []}

    class _FakeNats:
        async def subscribe(self, subject: str, callback: Any) -> Any:
            return MagicMock()

        async def publish(self, subject: str, body: bytes) -> None:
            return None

    class _Offer:
        def __init__(self, **kwargs: Any) -> None:
            seen["offer"].append(kwargs)
            self.maybe_offer = AsyncMock()

    class _Consumer:
        def __init__(self, deps: Any) -> None:
            seen["consumer"].append(deps)

        async def attach(self, client: Any) -> Any:
            return None

    composer = (lambda **_k: preview) if compose else serve_mod.compose_review_preview
    with (
        patch.object(serve_mod, "compose_review_preview", composer),
        patch("forge.pipeline.merge_offer.MergeOfferService", _Offer),
        patch("forge.pipeline.merge_executor.MergeApprovalConsumer", _Consumer),
        patch("forge.cli._serve_deps_gating.bind_gate_parts") as gate_parts,
        patch("forge.cli._serve_deps.build_pipeline_consumer_deps") as deps,
        patch(
            "forge.cli._serve_deps_lifecycle.build_publisher_and_emitter"
        ) as publisher,
    ):
        gate_parts.return_value = None
        deps.return_value = MagicMock()
        publisher.return_value = (MagicMock(), MagicMock())
        compose = serve_mod.bind_production_dispatch_chain(
            forge_config=_config(merge_on=merge_on),
            sqlite_pool=pool,
            db_path=tmp_path / "forge.db",
        )
        asyncio.run(compose(_FakeNats()))
    return seen


class TestTheServiceReachesTheCardAndThePress:
    def test_one_service_is_shared_by_the_offer_the_press_and_the_loop(
        self, pool, tmp_path: Path
    ) -> None:
        preview = StandInReviewPreview()
        seen = _drive_boot(pool, tmp_path, merge_on=True, preview=preview)

        assert len(seen["offer"]) == 1
        assert seen["offer"][0]["review_preview"] is preview
        assert len(seen["consumer"]) == 1
        assert seen["consumer"][0].review_preview is preview
        assert serve_mod.review_preview_service is preview

    def test_without_a_service_the_offer_is_built_exactly_as_before(
        self, pool, tmp_path: Path
    ) -> None:
        seen = _drive_boot(pool, tmp_path, merge_on=True, preview=None)

        assert "review_preview" not in seen["offer"][0]
        assert seen["consumer"][0].review_preview is None
        assert serve_mod.review_preview_service is None

    def test_with_the_merge_press_off_nothing_is_composed(
        self, pool, tmp_path: Path
    ) -> None:
        seen = _drive_boot(
            pool, tmp_path, merge_on=False, preview=StandInReviewPreview()
        )

        assert seen == {"offer": [], "consumer": []}
        assert serve_mod.review_preview_service is None


# ---------------------------------------------------------------------------
# The serve loop: reconciled at start-up, swept while running
# ---------------------------------------------------------------------------


def _run_serve_once(
    monkeypatch: pytest.MonkeyPatch, preview: Any, events: list[str]
) -> None:
    from forge.cli import _serve_daemon

    stub_client = _StubNatsClient()

    async def _fake_connect(servers: str) -> object:
        return stub_client

    async def _fake_open_fleet(nats_url: str) -> object:
        return stub_client

    async def _fake_recovery(client: object) -> None:
        events.append("recovery_reconcile")

    async def _fake_consumer(client: object) -> None:
        events.append("consumer_reconcile")

    async def _fake_compose(client: object) -> None:
        events.append("compose")
        serve_mod.review_preview_service = preview

    async def _fake_daemon(config: object, state: object, *, client: object = None) -> None:
        events.append("daemon_started")
        # Long enough for the sweep, at an interval of 0, to run.
        for _ in range(20):
            await asyncio.sleep(0)

    async def _fake_healthz(config: object, state: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(_serve_daemon, "nats_connect", _fake_connect)
    monkeypatch.setattr(serve_mod, "open_fleet_client", _fake_open_fleet)
    monkeypatch.setattr(serve_mod, "recovery_reconcile_on_boot", _fake_recovery)
    monkeypatch.setattr(serve_mod, "consumer_reconcile_on_boot", _fake_consumer)
    monkeypatch.setattr(serve_mod, "compose_dispatch_chain", _fake_compose)
    monkeypatch.setattr(serve_mod, "run_daemon", _fake_daemon)
    monkeypatch.setattr(serve_mod, "run_healthz_server", _fake_healthz)
    monkeypatch.setattr(serve_mod, "REVIEW_PREVIEW_SWEEP_SECONDS", 0)
    asyncio.run(serve_mod._run_serve(ServeConfig(), SubscriptionState()))


class TestTheServeLoop:
    def test_reconciled_after_the_boot_reconciles_and_before_the_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []

        class _Recording(StandInReviewPreview):
            async def reconcile_on_boot(self) -> None:
                events.append("preview_reconcile")
                await super().reconcile_on_boot()

        preview = _Recording()
        _run_serve_once(monkeypatch, preview, events)

        assert preview.reconciles == 1
        assert events.index("consumer_reconcile") < events.index("compose")
        assert events.index("compose") < events.index("preview_reconcile")
        assert events.index("preview_reconcile") < events.index("daemon_started")
        # And swept while the daemon ran.
        assert preview.sweeps

    def test_a_reconcile_that_raises_never_blocks_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []

        class _Raises(StandInReviewPreview):
            async def reconcile_on_boot(self) -> None:
                raise RuntimeError("ledger busy")

        _run_serve_once(monkeypatch, _Raises(), events)

        assert "daemon_started" in events

    def test_without_a_service_there_is_nothing_to_reconcile_or_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        _run_serve_once(monkeypatch, None, events)
        assert "daemon_started" in events


class TestTheSweep:
    @pytest.mark.asyncio
    async def test_a_sweep_that_raises_never_stops_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        monkeypatch.setattr(serve_mod.asyncio, "sleep", fake_sleep)

        class _RaisesOnce(StandInReviewPreview):
            async def sweep(self, *, now: Any) -> None:
                await super().sweep(now=now)
                if len(self.sweeps) == 1:
                    raise RuntimeError("compose down")

        preview = _RaisesOnce()
        with pytest.raises(asyncio.CancelledError):
            await serve_mod._run_review_preview_sweep(preview, 300)

        assert len(preview.sweeps) == 2
        assert all(now.tzinfo is not None for now in preview.sweeps)
