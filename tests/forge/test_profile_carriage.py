"""AC-04 profile carriage — queue-time write → build row → daemon resolve.

TASK-UBS-002-integration §2(a): ``forge queue --profile <name>`` persists the
selected budget profile on the ``builds.profile`` column (schema_v5) so the
daemon can resolve the caps for the build via
``config.budget.resolve(row.profile)`` instead of always applying
``config.budget.default_profile``.

These tests exercise the persistence carriage directly (the CLI surface is
covered by ``test_cli_profile_flag``) and the daemon-side resolution the daemon
performs on the hydrated :class:`BuildRow`:

- ``--profile unattended`` lands in ``builds.profile`` and resolves to the
  unattended caps.
- a NULL-profile row (no ``--profile``) resolves to ``default_profile``
  (backward-compatible = attended = caps off).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.config.models import (
    DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
    DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES,
    BudgetConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence


def _make_payload(
    *, feature_id: str = "FEAT-PROF-001", correlation_id: str = "corr-prof-1"
) -> SimpleNamespace:
    """Duck-typed BuildQueuedPayload with no ``profile`` attribute.

    Mirrors the real nats-core payload, which carries no profile field
    (§2(b) barred) — the profile is supplied to the write via the explicit
    keyword, exactly as the CLI does.
    """
    queued_at = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    return SimpleNamespace(
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
        queued_at=queued_at,
        requested_at=queued_at,
    )


@pytest.fixture()
def persistence(tmp_path: Path) -> SqliteLifecyclePersistence:
    db_path = tmp_path / "forge.db"
    cx: sqlite3.Connection = sqlite_connect.connect_writer(db_path)
    migrations.apply_at_boot(cx)
    facade = SqliteLifecyclePersistence(connection=cx)
    yield facade
    cx.close()


def test_unattended_profile_lands_and_daemon_resolves_unattended_caps(
    persistence: SqliteLifecyclePersistence,
) -> None:
    build_id = persistence.queue_build(_make_payload(), profile="unattended")

    # Carriage: the profile is persisted on the build row.
    row = persistence.get_build_row(build_id)
    assert row is not None
    assert row.profile == "unattended"

    # Daemon-side resolution: caps come from the row's profile, not the default.
    config = BudgetConfig()  # default_profile = attended (caps off)
    guards = config.resolve(row.profile)
    assert guards.caps_enabled is True
    assert guards.max_review_cycles == DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES
    assert (
        guards.max_build_wallclock_seconds
        == DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS
    )


def test_null_profile_row_resolves_to_default(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # No profile passed → NULL column value.
    build_id = persistence.queue_build(_make_payload())

    row = persistence.get_build_row(build_id)
    assert row is not None
    assert row.profile is None

    # resolve(None) falls back to default_profile (attended = caps off).
    config = BudgetConfig()
    guards = config.resolve(row.profile)
    assert guards.caps_enabled is False


def test_record_pending_build_also_carries_profile(
    persistence: SqliteLifecyclePersistence,
) -> None:
    # The lower-level writer accepts the profile keyword too (queue_build is a
    # thin alias over it).
    build_id = persistence.record_pending_build(
        _make_payload(correlation_id="corr-prof-2"), profile="unattended"
    )
    row = persistence.get_build_row(build_id)
    assert row is not None and row.profile == "unattended"
