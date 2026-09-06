"""``planning.rewrite_on_refusal`` — the switch on the machine's one rewrite.

Rule 8 of the rewrite-on-refusal lane (2026-09-06): on by default, so the
plan stage sends refused worked examples back to the spec writer once before
it asks a person; off restores the 2026-09-05 behaviour byte for byte. The
surface is closed (``extra="forbid"``), so the field has to be declared for a
settings file to be allowed to set it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.config.models import PlanningConfig


def test_the_switch_is_on_by_default() -> None:
    assert PlanningConfig().rewrite_on_refusal is True


def test_the_switch_can_be_turned_off_from_settings() -> None:
    assert PlanningConfig(rewrite_on_refusal=False).rewrite_on_refusal is False
    assert PlanningConfig.model_validate({"rewrite_on_refusal": False}).rewrite_on_refusal is False


def test_the_field_is_declared_on_the_closed_surface() -> None:
    assert "rewrite_on_refusal" in PlanningConfig.model_fields
    # The surface stays closed: a misspelt key is still refused.
    with pytest.raises(ValidationError):
        PlanningConfig.model_validate({"rewrite_on_refusa": False})


def test_the_description_is_plain_words() -> None:
    """The settings file is a surface a person reads: no rule ids, no house
    shorthand in the description."""
    description = PlanningConfig.model_fields["rewrite_on_refusal"].description or ""
    assert "spec writer" in description and "stamps again" in description
    assert "rule " not in description.lower()
