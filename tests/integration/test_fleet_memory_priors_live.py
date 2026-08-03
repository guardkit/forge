"""Live-store integration test for the fleet-memory PriorsReader.

Runs the REAL read path — Settings → psycopg pool → embed-on-read →
``fm_search`` → PriorReference mapping — against the fleet store. It is
env-gated, not fixture-fed: without the live DSN it skips.

F10-clean run recipe (the credentials never touch the shell history —
sops decrypts to the child process's environment only)::

    sops exec-env ~/.config/fleet-secrets/fleet-memory-pg/leg-env.enc.env \
        'pytest -m integration tests/integration/test_fleet_memory_priors_live.py -q'

The leg env file carries ``FLEET_MEMORY_ENABLED=true``,
``FLEET_MEMORY_PG_DSN`` and ``FLEET_MEMORY_EMBED_URL`` (plus the model/
dims pins matching the live corpus).

LESSON (2026-08-03, binding): test deselection hid a memory-dark MONTH —
three never-fired MCP breaks sat green behind ``-m`` filters while every
factory build ran without memory. A skipped run of THIS file is a
loud, named skip, not a pass; when the memory wire is claimed live,
run it deliberately under the recipe above and read the skip/pass line.
"""

from __future__ import annotations

import os

import pytest

from forge.adapters.fleet_memory.priors import (
    FleetMemoryPriorsReader,
    build_priors_reader_from_env,
    load_priors_config_from_env,
)
from forge.gating.models import PriorReference

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FLEET_MEMORY_ENABLED", "").lower() != "true"
        or not os.environ.get("FLEET_MEMORY_PG_DSN"),
        reason=(
            "live fleet-memory store not configured — run under "
            "sops exec-env (see module docstring recipe)"
        ),
    ),
]


@pytest.fixture()
def live_reader() -> FleetMemoryPriorsReader:
    pytest.importorskip(
        "fleet_memory",
        reason="fleet_memory not installed (forge `memory` extra)",
    )
    reader = build_priors_reader_from_env()
    assert isinstance(reader, FleetMemoryPriorsReader), (
        "env says ON but the factory degraded — read the memory: "
        "OFF/DEGRADED log line above"
    )
    return reader


class TestLivePriorsRead:
    """One real read against the fleet store, mapped end to end."""

    @pytest.mark.asyncio
    async def test_read_priors_returns_valid_prior_references(
        self, live_reader: FleetMemoryPriorsReader
    ) -> None:
        config = load_priors_config_from_env()
        try:
            priors = await live_reader.read_priors(
                target_kind="subagent",
                target_identifier="autobuild_runner",
                stage_label="autobuild",
                build_id="build-live-priors-1",
            )

            # Empty is legal (a young corpus); malformed is not.
            assert isinstance(priors, list)
            assert len(priors) <= 5
            for prior in priors:
                assert isinstance(prior, PriorReference)
                assert prior.entity_id
                assert prior.group_id == "forge_pipeline_history"
                assert prior.relevance_score is not None
                assert 0.0 <= prior.relevance_score <= 1.0
                assert len(prior.summary) <= 600

            # The lazy store survives a second read (no reconnect churn).
            again = await live_reader.read_priors(
                target_kind="subagent",
                target_identifier="autobuild_runner",
                stage_label="autobuild",
                build_id="build-live-priors-2",
            )
            assert isinstance(again, list)
            assert config.project  # scoped read, never a blank namespace
        finally:
            await live_reader.aclose()
