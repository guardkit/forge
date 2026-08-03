"""The conductor's activation seam — one flag, read one way.

Revival design pass §a.5 / §h.8. Two surfaces need the same answer to the
same question (``forge queue`` at enqueue time, the driver loop at dequeue
time) and must never disagree, so the read lives in one accessor. This
module pins that accessor's contract — above all its default.

The lane's prime invariant is that the flag defaults OFF and that with it
off the tree is byte-for-byte today's behaviour. "Defaults OFF" is not a
claim to assert in a docstring; it is a property to test from every angle
a config can arrive in — absent section, absent field, absent config,
wrong-shaped config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.config.conductor import (
    CONDUCTOR_FLAG_PATH,
    MODE_B_RETIRED_MESSAGE,
    MODE_C_NOT_ACTIVATED_MESSAGE,
    conductor_enabled,
    mode_refusal_reason,
)
from forge.config.loader import load_config
from forge.config.models import ConductorConfig, ForgeConfig
from forge.lifecycle.modes import BuildMode


def _config(text: str) -> ForgeConfig:
    return ForgeConfig.model_validate(yaml.safe_load(text))


_MINIMAL = """
permissions:
  filesystem:
    allowlist: ["/tmp"]
"""

#: Switching the conductor ON now takes TWO statements, not one: the flag
#: and the SEAT the fix journey's legs run on (conductor-activation design
#: pass §2). ``enabled: true`` alone is a REFUSAL, so every "the conductor
#: is on" fixture in this module carries a seat.
_ON = """
conductor:
  enabled: true
  seat: qwen3-coder-30b
"""


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


class TestDefaultsOff:
    def test_model_default_is_off(self) -> None:
        assert ConductorConfig().enabled is False

    def test_a_forge_yaml_with_no_conductor_section_is_off(self) -> None:
        """The overwhelmingly common case: every existing forge.yaml.

        Not one deployed config mentions the conductor, so this is the
        path that guarantees "nothing changes until someone opts in".
        """
        assert conductor_enabled(_config(_MINIMAL)) is False

    def test_an_empty_conductor_section_is_off(self) -> None:
        config = _config(_MINIMAL + "\nconductor: {}\n")
        assert conductor_enabled(config) is False

    def test_explicit_false_is_off(self) -> None:
        config = _config(_MINIMAL + "\nconductor:\n  enabled: false\n")
        assert conductor_enabled(config) is False

    def test_explicit_true_with_a_seat_is_the_only_way_on(self) -> None:
        """On takes the flag AND the seat — see :class:`TestTheSeatIsRequired`."""
        assert conductor_enabled(_config(_MINIMAL + _ON)) is True

    @pytest.mark.parametrize("bad", [None, object(), "conductor: enabled", 42])
    def test_unrecognised_shapes_degrade_to_off(self, bad: object) -> None:
        """Degrade toward inert, never toward active.

        A half-wired or mis-typed config must leave the conductor asleep —
        the same safety direction the mode reader's own fallback takes.
        """
        assert conductor_enabled(bad) is False

    def test_the_section_rejects_unknown_fields(self) -> None:
        """A typo'd key must fail loudly, not silently read as OFF.

        ``conductor: {enable: true}`` looking like a working switch that
        does nothing is exactly the failure mode a strict model prevents.
        """
        with pytest.raises(Exception):
            _config(_MINIMAL + "\nconductor:\n  enable: true\n")

    def test_flag_path_names_the_real_field(self) -> None:
        """The message points operators at a key that exists."""
        section, _, field = CONDUCTOR_FLAG_PATH.partition(".")
        assert section in ForgeConfig.model_fields
        assert field in ConductorConfig.model_fields

    def test_loader_round_trips_the_section(self, tmp_path) -> None:
        path = tmp_path / "forge.yaml"
        path.write_text(_MINIMAL + _ON)
        loaded = load_config(path)
        assert conductor_enabled(loaded) is True
        assert loaded.conductor.seat == "qwen3-coder-30b"


# ---------------------------------------------------------------------------
# The refusal decision
# ---------------------------------------------------------------------------


class TestModeRefusalReason:
    def test_routine_build_is_never_refused(self) -> None:
        """Mode A is untouched — the whole point of the invariant."""
        assert mode_refusal_reason(BuildMode.MODE_A, _config(_MINIMAL)) is None
        on = _config(_MINIMAL + _ON)
        assert mode_refusal_reason(BuildMode.MODE_A, on) is None

    def test_full_journey_is_refused_regardless_of_the_flag(self) -> None:
        """Mode B is retired, not gated — no flag brings it back."""
        off = _config(_MINIMAL)
        on = _config(_MINIMAL + _ON)
        assert mode_refusal_reason(BuildMode.MODE_B, off) == MODE_B_RETIRED_MESSAGE
        assert mode_refusal_reason(BuildMode.MODE_B, on) == MODE_B_RETIRED_MESSAGE

    def test_fix_journey_is_refused_while_the_conductor_is_off(self) -> None:
        reason = mode_refusal_reason(BuildMode.MODE_C, _config(_MINIMAL))
        assert reason == MODE_C_NOT_ACTIVATED_MESSAGE

    def test_fix_journey_is_allowed_once_the_conductor_is_on(self) -> None:
        on = _config(_MINIMAL + _ON)
        assert mode_refusal_reason(BuildMode.MODE_C, on) is None


# ---------------------------------------------------------------------------
# The seat — config-as-code, and required whenever the flag is on
# ---------------------------------------------------------------------------


class TestTheSeatIsRequired:
    """Conductor-activation design pass §2 — the seat field's contract.

    The fix journey's legs run on a LOCAL model, and the pipeline names it
    on the argv (``--model <seat>``). Before this landed the seat rode an
    operator env var the deployed daemon never set, so the first leg of
    the first production journey would have refused ``model=None`` — the
    fence working, the journey dying. The seat is now config-as-code and
    an activated conductor that names none is refused AT LOAD: the daemon
    fails to boot rather than failing on the first leg of a journey an
    owner already approved. That is the cap law's own posture ("an unset
    cap is REFUSED — never silently read as unlimited").
    """

    def test_the_model_default_names_no_seat(self) -> None:
        assert ConductorConfig().seat is None

    def test_enabled_with_no_seat_refuses(self) -> None:
        with pytest.raises(Exception):
            _config(_MINIMAL + "\nconductor:\n  enabled: true\n")

    def test_enabled_with_a_blank_seat_refuses(self) -> None:
        """A named nothing is worse than no name: ``--model ''``."""
        with pytest.raises(Exception):
            _config(_MINIMAL + '\nconductor:\n  enabled: true\n  seat: "   "\n')

    def test_enabled_with_an_explicitly_null_seat_refuses(self) -> None:
        with pytest.raises(Exception):
            _config(_MINIMAL + "\nconductor:\n  enabled: true\n  seat: null\n")

    def test_the_refusal_names_the_key_and_the_way_out(self) -> None:
        """An operator must not have to grep the source to fix their yaml."""
        with pytest.raises(Exception) as excinfo:
            ConductorConfig(enabled=True)
        message = str(excinfo.value)
        assert "conductor.seat" in message
        assert "enabled: false" in message

    def test_disabled_with_no_seat_stays_valid(self) -> None:
        """Every deployed forge.yaml today — nothing is broken by landing."""
        assert conductor_enabled(_config(_MINIMAL)) is False
        assert ConductorConfig().enabled is False
        assert ConductorConfig(enabled=False).seat is None

    def test_disabled_may_still_carry_a_seat(self) -> None:
        """Pre-loading the seat, then flipping the flag, is a valid order."""
        config = _config(
            _MINIMAL + "\nconductor:\n  enabled: false\n  seat: qwen3-coder-30b\n"
        )
        assert conductor_enabled(config) is False
        assert config.conductor.seat == "qwen3-coder-30b"

    def test_a_blank_seat_normalises_to_absent_when_disabled(self) -> None:
        """Blank = absent, the composition root's long-standing posture."""
        assert ConductorConfig(enabled=False, seat="   ").seat is None
        assert ConductorConfig(enabled=False, seat="").seat is None

    def test_a_seat_is_stripped_not_passed_through_with_whitespace(self) -> None:
        """``--model ' qwen3 '`` would be a different seat name on the wire."""
        assert ConductorConfig(enabled=True, seat="  qwen3-coder-30b  ").seat == (
            "qwen3-coder-30b"
        )

    def test_the_section_still_rejects_unknown_fields_beside_the_seat(self) -> None:
        """The seat is a NEW key, not a hole in ``extra='forbid'``."""
        with pytest.raises(Exception):
            _config(
                _MINIMAL
                + "\nconductor:\n  enabled: true\n  seat: qwen3-coder-30b\n"
                + "  model: qwen3-coder-30b\n"
            )

    def test_a_seat_typo_refuses_rather_than_reading_as_unseated(self) -> None:
        """``seat_name:`` must not look like a working switch that does nothing."""
        with pytest.raises(Exception):
            _config(
                _MINIMAL + "\nconductor:\n  enabled: true\n  seat_name: qwen3\n"
            )

    def test_the_deploy_order_law_a_seat_key_against_the_old_schema(self) -> None:
        """The hazard the activation runbook sequences around, pinned.

        A ``conductor.seat`` key written into the deployed yaml BEFORE the
        image carrying this field is running refuses the WHOLE config —
        ``extra='forbid'`` plus a loader that propagates the error
        unwrapped. Stand in for the old schema with a model that has the
        flag but not the seat, and prove the blast radius is the whole
        config, not just the section: that is why the activation sitting
        redeploys both surfaces BEFORE the yaml act.
        """
        from pydantic import BaseModel, ConfigDict

        class _OldConductorConfig(BaseModel):
            model_config = ConfigDict(extra="forbid")

            enabled: bool = False

        class _OldForgeConfig(BaseModel):
            model_config = ConfigDict(extra="forbid")

            conductor: _OldConductorConfig = _OldConductorConfig()

        with pytest.raises(Exception):
            _OldForgeConfig.model_validate(
                yaml.safe_load(_ON)  # {"conductor": {"enabled": ..., "seat": ...}}
            )

    def test_the_loader_refuses_an_enabled_yaml_with_no_seat(self, tmp_path) -> None:
        """The daemon's own boot path, not just the model."""
        path = tmp_path / "forge.yaml"
        path.write_text(_MINIMAL + "\nconductor:\n  enabled: true\n")
        with pytest.raises(Exception):
            load_config(path)


class TestTheDeployOrderLawsSecondSurface:
    """The sidecar reads the SAME yaml — and degrades PERMISSIVE on failure.

    Conductor-activation design pass §5: the langgraph sidecar lazily
    re-reads ``$FORGE_CONFIG_PATH`` per invocation, and on ANY load failure
    falls back to a base-dir-only filesystem check. So a ``conductor:`` key
    written into the deployed yaml before the sidecar is running the schema
    that defines it does not merely refuse the daemon — it silently WEAKENS
    the routine path's worktree-confinement gate. That is why activation is
    TWO acts (daemon recreate AND sidecar stop-wait-start) BEFORE the yaml
    act, and this pins the mechanism rather than trusting the runbook's
    prose.
    """

    def _allowlist(self, tmp_path, body: str, monkeypatch):
        from forge.subagents.autobuild_runner import _load_filesystem_allowlist

        path = tmp_path / "forge.yaml"
        path.write_text(body)
        monkeypatch.setenv("FORGE_CONFIG_PATH", str(path))
        monkeypatch.chdir(tmp_path)
        return _load_filesystem_allowlist()

    def test_a_config_the_running_schema_understands_yields_the_allowlist(
        self, tmp_path, monkeypatch
    ) -> None:
        """The healthy case — the confinement gate has real roots to check."""
        assert self._allowlist(tmp_path, _MINIMAL + _ON, monkeypatch) == [
            Path("/tmp")
        ]

    def test_a_conductor_key_the_schema_does_not_know_degrades_permissive(
        self, tmp_path, monkeypatch
    ) -> None:
        """The hazard itself, driven.

        An unknown key under ``conductor:`` is exactly what ``seat:`` was to
        yesterday's image: ``extra='forbid'`` refuses the WHOLE config, the
        lazy loader swallows it, and the answer is ``None`` — the permissive
        base-dir-only fallback. Loud on the daemon, SILENT here.
        """
        body = _MINIMAL + "\nconductor:\n  enabled: false\n  seat_from_a_newer_image: x\n"
        assert self._allowlist(tmp_path, body, monkeypatch) is None

    def test_an_enabled_but_seatless_config_also_degrades_permissive(
        self, tmp_path, monkeypatch
    ) -> None:
        """The new validator is a load failure like any other, sidecar-side."""
        body = _MINIMAL + "\nconductor:\n  enabled: true\n"
        assert self._allowlist(tmp_path, body, monkeypatch) is None


# ---------------------------------------------------------------------------
# The words themselves
# ---------------------------------------------------------------------------


class TestMessagesSpeakPlainNames:
    """User surfaces speak human — the phrase-book is law here."""

    def test_retirement_message_names_the_mechanism_that_replaced_it(
        self,
    ) -> None:
        assert "spec-writer chain" in MODE_B_RETIRED_MESSAGE

    def test_activation_message_names_the_conductor_and_the_switch(
        self,
    ) -> None:
        assert "conductor" in MODE_C_NOT_ACTIVATED_MESSAGE
        assert CONDUCTOR_FLAG_PATH in MODE_C_NOT_ACTIVATED_MESSAGE

    @pytest.mark.parametrize(
        "message", [MODE_B_RETIRED_MESSAGE, MODE_C_NOT_ACTIVATED_MESSAGE]
    )
    def test_every_refusal_says_that_nothing_was_queued(
        self, message: str
    ) -> None:
        """The operator's first question is "did it write anything?".

        Answer it in the message rather than making them read the queue.
        """
        assert "Nothing was queued" in message

    @pytest.mark.parametrize(
        "message", [MODE_B_RETIRED_MESSAGE, MODE_C_NOT_ACTIVATED_MESSAGE]
    )
    def test_refusals_carry_no_internal_labels(self, message: str) -> None:
        """No task IDs, no ASSUM- refs, no spec IDs on an owner surface."""
        lowered = message.lower()
        for internal in ("assum-", "task-", "feat-forge-", "mbc8", "adr-"):
            assert internal not in lowered

    @pytest.mark.parametrize(
        "message", [MODE_B_RETIRED_MESSAGE, MODE_C_NOT_ACTIVATED_MESSAGE]
    )
    def test_refusals_never_use_a_mode_codename_as_a_label(
        self, message: str
    ) -> None:
        """"Mode B" is a codename; "the full journey" is the name.

        The flag spelling (``--mode b``) is the operator's own vocabulary
        and stays; what must not appear is the codename used as if it
        described anything.
        """
        import re

        # Any "mode <letter>" NOT preceded by the literal flag "--".
        assert re.search(r"(?<!--)\bmode[ -][abc]\b", message.lower()) is None
