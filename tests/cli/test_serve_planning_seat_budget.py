"""The planning composition tells each seat its time budget (23 October 2026).

Forge cuts a planning stage off after a fixed time. The composition builds the
one dispatch orchestrator every planning stage goes through, and it must ask
that orchestrator to tell the seat the same cut-off, less the margin, so a seat
can stop before Forge's cut-off fires. The orchestrator's own tests prove what
the command carries; this proves the real composition asks for it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.sqlite import connect_writer
from forge.cli._serve_planning import compose_planning_consumer_and_dispatch
from forge.dispatch import orchestrator as orchestrator_module
from forge.dispatch.timeout import seat_time_budget_seconds
from forge.lifecycle import migrations as lifecycle_migrations

from tests.cli.test_serve_planning import FixedClock, JetStreamNats, _make_planning_config


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_planning.db"
    lifecycle_migrations.apply_at_boot(connect_writer(db_path))
    return db_path


@pytest.mark.asyncio
async def test_the_planning_orchestrator_tells_the_seat_its_budget(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    real = orchestrator_module.DispatchOrchestrator

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(orchestrator_module, "DispatchOrchestrator", Recording)

    result = await compose_planning_consumer_and_dispatch(
        db_path=tmp_db,
        nats_client=JetStreamNats(),
        config=_make_planning_config(),
        clock=FixedClock(),
    )
    try:
        assert result is not None
        (orchestrator,) = built
        assert orchestrator._tell_seat_time_budget is True
        cut_off = orchestrator._timeout.default_timeout_seconds
        assert cut_off == 3600.0
        assert seat_time_budget_seconds(cut_off) == 3480
    finally:
        for task in list(result.background_tasks or []) if result else []:
            task.cancel()
        if result is not None:
            await asyncio.gather(*(result.background_tasks or []), return_exceptions=True)
