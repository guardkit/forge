"""Per-build budget wiring + THE CAP-MAPPING LAW.

Revival design pass §b.1 / §d Stage 3 / risk h.7, Stage 1c.

Two things are proven here.

**1. The four serve-side builders are wired at per-build supervisor
setup.** They have existed, complete and tested, with zero callers since
FEAT-UBS-002 — the design pass named the driver loop as the caller they
were waiting for. :func:`build_conductor_budget_kwargs` is that setup.

**2. THE CAP-MAPPING LAW (risk h.7).** "One follow-up review" is
``max_review_cycles: 2``, NEVER ``1``.
:func:`~forge.pipeline.budget_guard.count_review_cycles` counts EVERY
review entry, and a bounded fix journey has two — the initial review that
finds the fix tasks and the one follow-up that confirms they landed. The
guard is consulted before the step it would allow, with ``>=`` semantics.
So a profile of ``1`` breaches at the mandatory follow-up: a false pause
on every single fix build.

:class:`TestTheCapMappingLaw` pins the MAPPING, not the mistake — it
shows ``1`` false-pausing and ``2`` passing on the same history, so the
reason the built-in profile says ``2`` is executable, not a comment
somebody can quietly edit away.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.serve import build_conductor_budget_kwargs
from forge.config.models import (
    FIX_JOURNEY_MAX_REVIEW_CYCLES,
    FIX_JOURNEY_PROFILE_NAME,
    BudgetGuards,
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
)
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.pipeline.budget_guard import (
    BuildBudgetMetrics,
    count_review_cycles,
    evaluate_budget,
)
from forge.pipeline.stage_taxonomy import StageClass


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


def _payload(feature_id: str = "FEAT-BUD") -> SimpleNamespace:
    return SimpleNamespace(
        feature_id=feature_id,
        repo="guardkit/forge",
        branch="main",
        feature_yaml_path="features/fix.yaml",
        max_turns=5,
        sdk_timeout_seconds=1800,
        triggered_by="cli",
        originating_adapter="terminal",
        originating_user="budget-test",
        correlation_id="aaaa1111-bbbb-2222-cccc-333333333333",
        parent_request_id=None,
        queued_at=datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# 1. The four builders, wired
# ---------------------------------------------------------------------------


class TestPerBuildBudgetWiring:
    def test_all_four_builders_are_wired(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        build_id = persistence.record_pending_build(
            _payload(), profile=FIX_JOURNEY_PROFILE_NAME
        )
        emitted: list[Any] = []

        async def publish(payload: Any, subject: str) -> None:
            emitted.append((payload, subject))

        kwargs = build_conductor_budget_kwargs(
            pool=persistence,
            config=forge_config,
            build_id=build_id,
            publish_approval_request=publish,
            lifecycle_emitter=object(),
        )

        # (1) resolve_budget_for_build
        assert isinstance(kwargs["budget_guards"], BudgetGuards)
        assert kwargs["budget_profile_name"] == FIX_JOURNEY_PROFILE_NAME
        # (2) budget_wall_clock
        assert callable(kwargs["budget_wall_clock"])
        assert isinstance(kwargs["budget_wall_clock"](), datetime)
        # (3) make_budget_started_at_reader
        assert callable(kwargs["budget_started_at_reader"])
        assert kwargs["budget_started_at_reader"](build_id) is None
        # (4) make_budget_pause
        assert callable(kwargs["budget_pause"])

    def test_the_kwargs_drop_straight_onto_the_supervisor(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """Every key must be a real ``Supervisor`` field."""
        from dataclasses import fields

        from forge.pipeline.supervisor import Supervisor

        build_id = persistence.record_pending_build(_payload())
        kwargs = build_conductor_budget_kwargs(
            pool=persistence, config=forge_config, build_id=build_id
        )

        supervisor_fields = {f.name for f in fields(Supervisor)}
        assert set(kwargs) <= supervisor_fields

    def test_an_attended_build_resolves_caps_off(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """ASSUM-010 — a NULL profile is attended, and attended has no caps."""
        build_id = persistence.record_pending_build(_payload("FEAT-ATT"))

        kwargs = build_conductor_budget_kwargs(
            pool=persistence, config=forge_config, build_id=build_id
        )

        assert kwargs["budget_guards"].caps_enabled is False
        assert kwargs["budget_profile_name"] == "attended"

    def test_the_pause_seam_is_omitted_rather_than_faked(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """No publisher, no emitter, no pause — the cap still refuses."""
        build_id = persistence.record_pending_build(_payload("FEAT-NOP"))

        kwargs = build_conductor_budget_kwargs(
            pool=persistence, config=forge_config, build_id=build_id
        )

        assert "budget_pause" not in kwargs

    def test_the_started_at_reader_reads_the_row(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
        writer_db: sqlite3.Connection,
    ) -> None:
        build_id = persistence.record_pending_build(_payload("FEAT-SAT"))
        writer_db.execute(
            "UPDATE builds SET started_at = ? WHERE build_id = ?",
            ("2026-07-31T10:05:00+00:00", build_id),
        )
        writer_db.commit()

        kwargs = build_conductor_budget_kwargs(
            pool=persistence, config=forge_config, build_id=build_id
        )

        started = kwargs["budget_started_at_reader"](build_id)
        assert started is not None
        assert started.isoformat().startswith("2026-07-31T10:05:00")


# ---------------------------------------------------------------------------
# 2. THE CAP-MAPPING LAW
# ---------------------------------------------------------------------------


class _Review:
    """Minimal history entry the review counter reads."""

    def __init__(self, stage_class: StageClass) -> None:
        self.stage_class = stage_class


#: The bounded fix journey's history at the moment the guard is asked
#: "may I run the follow-up review?": ONE review has happened.
_BEFORE_THE_FOLLOWUP = [_Review(StageClass.TASK_REVIEW), _Review(StageClass.TASK_WORK)]

#: …and at the moment it is asked "may I run ANOTHER review?" after the
#: follow-up already ran: TWO reviews have happened.
_AFTER_THE_FOLLOWUP = [
    _Review(StageClass.TASK_REVIEW),
    _Review(StageClass.TASK_WORK),
    _Review(StageClass.TASK_REVIEW),
]


def _cycles(history: list[_Review]) -> int:
    return count_review_cycles(
        history, is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW
    )


class TestTheCapMappingLaw:
    def test_the_counter_counts_ALL_reviews_including_the_initial_one(
        self,
    ) -> None:
        """The fact the whole law rests on."""
        assert _cycles(_BEFORE_THE_FOLLOWUP) == 1
        assert _cycles(_AFTER_THE_FOLLOWUP) == 2

    def test_a_profile_of_1_false_pauses_before_the_mandatory_followup(
        self,
    ) -> None:
        """The mistake, pinned so nobody re-introduces it."""
        wrong = BudgetGuards(max_review_cycles=1)

        verdict = evaluate_budget(
            wrong,
            BuildBudgetMetrics(review_cycles=_cycles(_BEFORE_THE_FOLLOWUP)),
        )

        assert verdict.ok is False, (
            "max_review_cycles=1 must breach at the follow-up review — this "
            "is exactly the false pause risk h.7 names"
        )
        assert verdict.breached_cap == "max_review_cycles"

    def test_a_profile_of_2_permits_exactly_one_followup_review(self) -> None:
        """The mapping: one follow-up review == max_review_cycles 2."""
        right = BudgetGuards(max_review_cycles=FIX_JOURNEY_MAX_REVIEW_CYCLES)

        allowed = evaluate_budget(
            right,
            BuildBudgetMetrics(review_cycles=_cycles(_BEFORE_THE_FOLLOWUP)),
        )
        refused = evaluate_budget(
            right,
            BuildBudgetMetrics(review_cycles=_cycles(_AFTER_THE_FOLLOWUP)),
        )

        assert allowed.ok is True, "the ONE follow-up review must be allowed"
        assert refused.ok is False, "a SECOND follow-up must be refused"
        assert refused.breached_cap == "max_review_cycles"

    def test_the_builtin_fix_journey_profile_encodes_the_mapping(self) -> None:
        config = ForgeConfig(
            permissions=PermissionsConfig(
                filesystem=FilesystemPermissions(allowlist=[Path("/tmp")]),
            ),
        )

        guards = config.budget.resolve(FIX_JOURNEY_PROFILE_NAME)

        assert FIX_JOURNEY_MAX_REVIEW_CYCLES == 2
        assert guards.max_review_cycles == 2, (
            "the built-in fix-journey profile must say 2 — 'one follow-up "
            "review' counts the initial review too (design pass risk h.7)"
        )
        assert guards.max_build_wallclock_seconds is not None, (
            "the wall-clock cap is on from day one (design pass §d Stage 3)"
        )
        assert guards.caps_enabled is True

    def test_a_build_queued_on_the_fix_journey_profile_resolves_the_cap(
        self,
        persistence: SqliteLifecyclePersistence,
        forge_config: ForgeConfig,
    ) -> None:
        """End to end: the row's profile → the supervisor's caps."""
        build_id = persistence.record_pending_build(
            _payload("FEAT-FJP"), profile=FIX_JOURNEY_PROFILE_NAME
        )

        kwargs = build_conductor_budget_kwargs(
            pool=persistence, config=forge_config, build_id=build_id
        )

        assert kwargs["budget_guards"].max_review_cycles == 2

    def test_the_attended_profile_can_never_be_armed(self) -> None:
        """ASSUM-010 stands — the fix-journey profile is a SEPARATE name."""
        from pydantic import ValidationError

        from forge.config.models import BudgetConfig

        with pytest.raises(ValidationError):
            BudgetConfig(
                profiles={"attended": BudgetGuards(max_review_cycles=2)},
            )
