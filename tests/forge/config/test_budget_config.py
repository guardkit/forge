"""Tests for the FEAT-UBS-002 budget-guard config models.

Each test class mirrors one acceptance criterion of the UBS-002 budget-guard
skeleton so the criterion→verifier mapping stays explicit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forge.config import BudgetConfig, BudgetGuards, ForgeConfig, load_config
from forge.config.models import (
    ATTENDED_PROFILE_NAME,
    DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS,
    DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES,
)


def _minimal_forge_yaml(tmp_path: Path, *, budget_block: str = "") -> Path:
    """Write a minimal valid forge.yaml (permissions required) + optional budget."""
    allow = tmp_path / "checkouts"
    allow.mkdir()
    doc = (
        "permissions:\n" "  filesystem:\n" f"    allowlist:\n      - {allow}\n"
    ) + budget_block
    path = tmp_path / "forge.yaml"
    path.write_text(doc, encoding="utf-8")
    return path


class TestDefaults:
    """AC: built-in profiles exist with the documented defaults."""

    def test_default_profile_is_attended(self) -> None:
        assert BudgetConfig().default_profile == ATTENDED_PROFILE_NAME

    def test_attended_profile_has_all_caps_unset(self) -> None:
        guards = BudgetConfig().resolve(ATTENDED_PROFILE_NAME)
        assert guards.caps_enabled is False
        assert guards.max_review_cycles is None
        assert guards.max_build_wallclock_seconds is None

    def test_unattended_profile_has_conservative_caps(self) -> None:
        guards = BudgetConfig().resolve("unattended")
        assert guards.max_review_cycles == DEFAULT_UNATTENDED_MAX_REVIEW_CYCLES
        assert (
            guards.max_build_wallclock_seconds
            == DEFAULT_UNATTENDED_MAX_BUILD_WALLCLOCK_SECONDS
        )
        assert guards.caps_enabled is True


class TestResolve:
    """AC: resolve() returns the named profile or the default, else KeyError."""

    def test_resolve_none_returns_default(self) -> None:
        cfg = BudgetConfig()
        assert cfg.resolve(None) is cfg.profiles[cfg.default_profile]

    def test_resolve_named(self) -> None:
        assert BudgetConfig().resolve("unattended").caps_enabled is True

    def test_resolve_unknown_raises_keyerror_listing_known(self) -> None:
        with pytest.raises(KeyError) as exc:
            BudgetConfig().resolve("nope")
        # The message lists the known profiles so the CLI can echo them.
        assert "unattended" in str(exc.value)


class TestValidation:
    """AC: the attended profile cannot be armed; default must exist."""

    def test_armed_attended_profile_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetConfig(
                profiles={ATTENDED_PROFILE_NAME: BudgetGuards(max_review_cycles=1)}
            )

    def test_missing_default_profile_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetConfig(
                default_profile="ghost",
                profiles={ATTENDED_PROFILE_NAME: BudgetGuards()},
            )

    def test_non_positive_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetGuards(max_review_cycles=0)

    def test_coach_floor_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetGuards(min_coach_score=1.5)


class TestForgeConfigIntegration:
    """AC: ForgeConfig.budget is optional (backward compatible) and loads."""

    def test_budget_defaults_when_absent(self, tmp_path: Path) -> None:
        cfg = load_config(_minimal_forge_yaml(tmp_path))
        assert cfg.budget.default_profile == ATTENDED_PROFILE_NAME
        assert cfg.budget.resolve("unattended").caps_enabled is True

    def test_budget_block_round_trips(self, tmp_path: Path) -> None:
        budget = (
            "budget:\n"
            "  default_profile: unattended\n"
            "  profiles:\n"
            "    attended: {}\n"
            "    unattended:\n"
            "      max_review_cycles: 3\n"
            "      max_build_wallclock_seconds: 1200\n"
        )
        cfg = load_config(_minimal_forge_yaml(tmp_path, budget_block=budget))
        assert cfg.budget.default_profile == "unattended"
        assert cfg.budget.resolve("unattended").max_review_cycles == 3

    def test_armed_attended_in_yaml_is_rejected(self, tmp_path: Path) -> None:
        budget = (
            "budget:\n" "  profiles:\n" "    attended:\n" "      max_review_cycles: 2\n"
        )
        with pytest.raises(ValidationError):
            load_config(_minimal_forge_yaml(tmp_path, budget_block=budget))


class TestLegBudgets:
    """The per-leg knobs: optional fields that turn the leg argv.

    Before this group the pipeline threaded exactly one extra argv token
    pair (``--model <seat>``), so the build system's hardcoded 2 turns /
    420s / 1620s governed production and moving them was an image-level
    change. These pin the two properties that let the group land on an
    ``extra=forbid`` schema with no migration: every field is optional and
    defaults to ``None``, and ``None`` is what every profile written
    before the group existed already resolves to.
    """

    def test_every_leg_field_defaults_to_none(self) -> None:
        guards = BudgetGuards()
        assert guards.leg_max_turns is None
        assert guards.leg_sdk_timeout_seconds is None
        assert guards.leg_budget_seconds is None

    def test_the_built_in_profiles_carry_no_leg_budgets(self) -> None:
        """Byte-identical defaults: the shipped profiles turn nothing."""
        for name in BudgetConfig().profiles:
            guards = BudgetConfig().resolve(name)
            assert guards.leg_max_turns is None
            assert guards.leg_sdk_timeout_seconds is None
            assert guards.leg_budget_seconds is None

    def test_leg_budgets_are_not_caps(self) -> None:
        """``caps_enabled`` answers "unattended-style profile?", not "tuned?".

        It gates the budget guard, the lifecycle budget observer and the
        attended-arming validator. None of those has any business firing
        because a leg was given fewer turns — and folding the group in
        would also make the reserved ``attended`` profile unable to carry
        a leg budget at all (ASSUM-010).
        """
        guards = BudgetGuards(
            leg_max_turns=4, leg_sdk_timeout_seconds=300, leg_budget_seconds=900
        )
        assert guards.caps_enabled is False

    def test_the_reserved_attended_profile_may_carry_leg_budgets(self) -> None:
        cfg = BudgetConfig(
            profiles={ATTENDED_PROFILE_NAME: BudgetGuards(leg_max_turns=4)}
        )
        assert cfg.resolve(ATTENDED_PROFILE_NAME).leg_max_turns == 4

    @pytest.mark.parametrize(
        "field",
        ["leg_max_turns", "leg_sdk_timeout_seconds", "leg_budget_seconds"],
    )
    def test_a_non_positive_leg_budget_is_rejected_at_load(self, field: str) -> None:
        with pytest.raises(ValidationError):
            BudgetGuards(**{field: 0})

    def test_a_leg_budget_block_round_trips_through_yaml(self, tmp_path: Path) -> None:
        budget = (
            "budget:\n"
            "  default_profile: fix-journey\n"
            "  profiles:\n"
            "    attended: {}\n"
            "    fix-journey:\n"
            "      max_review_cycles: 2\n"
            "      max_build_wallclock_seconds: 3600\n"
            "      leg_max_turns: 4\n"
            "      leg_sdk_timeout_seconds: 300\n"
            "      leg_budget_seconds: 900\n"
        )
        cfg = load_config(_minimal_forge_yaml(tmp_path, budget_block=budget))
        guards = cfg.budget.resolve("fix-journey")

        assert guards.leg_max_turns == 4
        assert guards.leg_sdk_timeout_seconds == 300
        assert guards.leg_budget_seconds == 900
        # The cap law's reading is untouched by the new group.
        assert guards.max_review_cycles == 2
        assert guards.caps_enabled is True

    def test_a_yaml_written_before_the_group_existed_still_loads(
        self, tmp_path: Path
    ) -> None:
        """The safe direction of the deploy-order law.

        Adding OPTIONAL fields to an ``extra=forbid`` model keeps every
        old yaml valid against the NEW schema. The REVERSE is the hazard —
        a yaml that has grown ``leg_max_turns:`` read by a process still
        on the old schema is refused WHOLE, and the langgraph sidecar,
        which lazily re-reads the same file, degrades to a permissive
        filesystem check on any load failure. Merge and redeploy both
        surfaces first, then add the keys.
        """
        budget = (
            "budget:\n"
            "  default_profile: unattended\n"
            "  profiles:\n"
            "    attended: {}\n"
            "    unattended:\n"
            "      max_review_cycles: 3\n"
            "      max_build_wallclock_seconds: 1200\n"
        )
        guards = load_config(
            _minimal_forge_yaml(tmp_path, budget_block=budget)
        ).budget.resolve("unattended")

        assert guards.max_review_cycles == 3
        assert guards.leg_max_turns is None
        assert guards.leg_sdk_timeout_seconds is None
        assert guards.leg_budget_seconds is None

    def test_an_unknown_leg_field_is_still_refused(self, tmp_path: Path) -> None:
        """``extra=forbid`` is not loosened by adding optional fields."""
        budget = (
            "budget:\n"
            "  profiles:\n"
            "    attended: {}\n"
            "    unattended:\n"
            "      leg_max_tunrs: 4\n"
        )
        with pytest.raises(ValidationError):
            load_config(_minimal_forge_yaml(tmp_path, budget_block=budget))
