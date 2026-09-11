"""The routine build's seat, as a setting: a lever, never an obligation.

Found 2026-09-11: forge named a model for exactly one thing, the fix
journey's legs (``conductor.seat``). A routine build named none, so the
build system's own command line fell back to its default — a frontier
vendor's model NAME that reaches a local model only because the estate's
proxy carries a wildcard row. Naming the seat is the cure, and
``routine.seat`` is where an operator names it.

This module pins the setting's contract, and above all what it does when
nobody sets it: nothing. Every ``forge.yaml`` deployed today carries no
``routine:`` section at all, so the section must be optional, its seat must
default to absent, and an absent seat must never be filled in with a
default of our choosing.

The posture is deliberately the fix journey's, field for field
(:class:`ConductorConfig`): a blank value reads as absent, a value starting
with a dash is refused because it would land on the command line as an
option rather than as a name, and the refusal happens at CONFIG LOAD so the
daemon refuses to boot rather than dying on a build an owner has already
approved. The one difference is the requirement: the conductor refuses to
be switched on without a seat, because an unseated fix journey is a
half-configured factory. There is no routine switch — an unnamed routine
seat is simply today — so nothing here is required.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from forge.config.loader import load_config
from forge.config.models import ForgeConfig, RoutineConfig


_MINIMAL = """
permissions:
  filesystem:
    allowlist: ["/tmp"]
"""


def _config(text: str) -> ForgeConfig:
    return ForgeConfig.model_validate(yaml.safe_load(text))


# ---------------------------------------------------------------------------
# Absent by default — today, exactly
# ---------------------------------------------------------------------------


class TestTheDefaultIsNoSeatAtAll:
    """A factory that says nothing about a seat behaves as it does now."""

    def test_the_model_default_names_no_seat(self) -> None:
        assert RoutineConfig().seat is None

    def test_a_forge_yaml_with_no_routine_section_names_no_seat(self) -> None:
        config = _config(_MINIMAL)
        assert config.routine.seat is None

    def test_an_empty_routine_section_names_no_seat(self) -> None:
        config = _config(_MINIMAL + "\nroutine: {}\n")
        assert config.routine.seat is None

    def test_an_explicitly_null_seat_is_absent_not_an_error(self) -> None:
        """Absent is the whole default — spelling it out cannot be a refusal."""
        config = _config(_MINIMAL + "\nroutine:\n  seat: null\n")
        assert config.routine.seat is None


# ---------------------------------------------------------------------------
# A named seat
# ---------------------------------------------------------------------------


class TestANamedSeat:
    """What an operator writes is what rides the command line."""

    def test_a_named_seat_is_read_off_the_yaml(self) -> None:
        config = _config(_MINIMAL + "\nroutine:\n  seat: qwen3-coder-30b\n")
        assert config.routine.seat == "qwen3-coder-30b"

    @pytest.mark.parametrize(
        "seat",
        ["qwen3-coder-30b", "gpt-oss-120b", "a-b-c", "seat_with_underscores"],
    )
    def test_a_seat_with_dashes_inside_it_is_perfectly_valid(
        self, seat: str
    ) -> None:
        """Only a LEADING dash is the hazard — fleet aliases are full of them."""
        assert RoutineConfig(seat=seat).seat == seat

    def test_a_seat_is_stripped_rather_than_passed_through_with_whitespace(
        self,
    ) -> None:
        """``--model ' qwen3 '`` would be a different seat name on the wire."""
        assert RoutineConfig(seat="  qwen3-coder-30b  ").seat == "qwen3-coder-30b"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_seat_reads_as_absent(self, blank: str) -> None:
        """A named nothing would put an empty token on the command line."""
        assert RoutineConfig(seat=blank).seat is None

    def test_a_blank_seat_in_the_yaml_reads_as_absent_too(self) -> None:
        config = _config(_MINIMAL + '\nroutine:\n  seat: "   "\n')
        assert config.routine.seat is None


# ---------------------------------------------------------------------------
# A seat must be shaped like a name
# ---------------------------------------------------------------------------


class TestAnOptionShapedSeatIsRefusedAtLoad:
    """``seat: -m`` is not a seat; it is another flag."""

    @pytest.mark.parametrize(
        "option_shaped", ["-m", "--model", "--dangerously-skip-permissions"]
    )
    def test_a_seat_starting_with_a_dash_is_refused(
        self, option_shaped: str
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            RoutineConfig(seat=option_shaped)

        message = str(excinfo.value)
        assert "routine.seat" in message
        assert option_shaped in message

    def test_the_refusal_is_a_plain_sentence_a_person_can_act_on(self) -> None:
        """It must say what is wrong, why it matters, and what to write."""
        with pytest.raises(ValidationError) as excinfo:
            _config(_MINIMAL + "\nroutine:\n  seat: '-m'\n")

        message = str(excinfo.value)
        assert "starts with a dash" in message
        assert "--model <seat>" in message
        assert "qwen3-coder-30b" in message

    def test_the_refusal_happens_at_config_load_not_at_dispatch(
        self, tmp_path
    ) -> None:
        """The daemon refuses to BOOT rather than dying on a build.

        A typo discovered on the first dispatch is discovered after an
        owner has already approved the work — the whole reason this check
        is at load time and not on the command line.
        """
        path = tmp_path / "forge.yaml"
        path.write_text(
            _MINIMAL + "\nroutine:\n  seat: --model\n", encoding="utf-8"
        )

        with pytest.raises(ValidationError) as excinfo:
            load_config(path)

        assert "routine.seat" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The section's edges
# ---------------------------------------------------------------------------


class TestTheSectionsEdges:
    """A typo must not read as a working switch that does nothing."""

    def test_an_unknown_key_beside_the_seat_is_refused(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _config(
                _MINIMAL + "\nroutine:\n  seat: qwen3-coder-30b\n  turbo: true\n"
            )

        assert "extra_forbidden" in str(excinfo.value)

    def test_a_misspelt_seat_key_refuses_rather_than_reading_as_unseated(
        self,
    ) -> None:
        """``seat_name:`` would otherwise be a setting that silently does nothing."""
        with pytest.raises(ValidationError):
            _config(_MINIMAL + "\nroutine:\n  seat_name: qwen3-coder-30b\n")

    def test_the_routine_seat_and_the_fix_journeys_seat_are_two_settings(
        self,
    ) -> None:
        """Naming one must never change the other — two paths, two seats."""
        config = _config(
            _MINIMAL
            + "\nroutine:\n  seat: qwen3-coder-30b\n"
            + "\nconductor:\n  enabled: true\n  seat: gpt-oss-120b\n"
        )

        assert config.routine.seat == "qwen3-coder-30b"
        assert config.conductor.seat == "gpt-oss-120b"

    def test_a_routine_seat_alone_leaves_the_conductor_switched_off(
        self,
    ) -> None:
        """The lever is the routine path's; it activates nothing else."""
        config = _config(_MINIMAL + "\nroutine:\n  seat: qwen3-coder-30b\n")

        assert config.conductor.enabled is False
        assert config.conductor.seat is None
