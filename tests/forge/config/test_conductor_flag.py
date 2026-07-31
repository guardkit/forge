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

    def test_explicit_true_is_the_only_way_on(self) -> None:
        config = _config(_MINIMAL + "\nconductor:\n  enabled: true\n")
        assert conductor_enabled(config) is True

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
        path.write_text(_MINIMAL + "\nconductor:\n  enabled: true\n")
        assert conductor_enabled(load_config(path)) is True


# ---------------------------------------------------------------------------
# The refusal decision
# ---------------------------------------------------------------------------


class TestModeRefusalReason:
    def test_routine_build_is_never_refused(self) -> None:
        """Mode A is untouched — the whole point of the invariant."""
        assert mode_refusal_reason(BuildMode.MODE_A, _config(_MINIMAL)) is None
        on = _config(_MINIMAL + "\nconductor:\n  enabled: true\n")
        assert mode_refusal_reason(BuildMode.MODE_A, on) is None

    def test_full_journey_is_refused_regardless_of_the_flag(self) -> None:
        """Mode B is retired, not gated — no flag brings it back."""
        off = _config(_MINIMAL)
        on = _config(_MINIMAL + "\nconductor:\n  enabled: true\n")
        assert mode_refusal_reason(BuildMode.MODE_B, off) == MODE_B_RETIRED_MESSAGE
        assert mode_refusal_reason(BuildMode.MODE_B, on) == MODE_B_RETIRED_MESSAGE

    def test_fix_journey_is_refused_while_the_conductor_is_off(self) -> None:
        reason = mode_refusal_reason(BuildMode.MODE_C, _config(_MINIMAL))
        assert reason == MODE_C_NOT_ACTIVATED_MESSAGE

    def test_fix_journey_is_allowed_once_the_conductor_is_on(self) -> None:
        on = _config(_MINIMAL + "\nconductor:\n  enabled: true\n")
        assert mode_refusal_reason(BuildMode.MODE_C, on) is None


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
