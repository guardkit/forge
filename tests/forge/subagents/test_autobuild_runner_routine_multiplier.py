"""The seat's companion: the budget that scales with it.

WHY THIS FILE EXISTS. A task's budget is its timeout MINUS however long its
wave has already been running. On 2026-09-14 arm B of the coder comparison
lost a feature to that arithmetic: two of its three tasks passed in a single
turn each, and the third was killed mid-turn when the wave's remaining budget
had fallen to 750 seconds — a budget tuned on a faster seat. Nothing was wrong
with the code it wrote. So the dial that scales the budget is named in the
same place as the seat, and it moves with it.

WHAT IS PINNED HERE. That an unnamed multiplier changes NOTHING (the whole
point — every deployed forge.yaml keeps working), that a named one rides the
command line as two tokens, and that every unreadable, malformed or
out-of-range value degrades to "no multiplier named" rather than killing a
build an owner has already approved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.subagents.autobuild_runner import (
    _load_routine_seat,
    _load_routine_timeout_multiplier,
)


def _config(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "forge.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("FORGE_CONFIG_PATH", str(path))
    return path


_MINIMAL = "permissions:\n  filesystem:\n    allowlist:\n      - /tmp\n"


def test_no_multiplier_named_is_today_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, _MINIMAL + "routine:\n  seat: openai:workhorse\n", monkeypatch)
    assert _load_routine_timeout_multiplier() is None
    assert _load_routine_seat() == "openai:workhorse"  # the seat is untouched


def test_no_routine_section_at_all_is_today_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, _MINIMAL, monkeypatch)
    assert _load_routine_timeout_multiplier() is None


def test_a_named_multiplier_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        _MINIMAL + "routine:\n  seat: openai:flash-next\n  timeout_multiplier: 2.0\n",
        monkeypatch,
    )
    assert _load_routine_timeout_multiplier() == 2.0
    assert _load_routine_seat() == "openai:flash-next"


def test_an_unreadable_config_never_kills_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, "routine: [this is not a mapping\n", monkeypatch)
    assert _load_routine_timeout_multiplier() is None


def test_a_missing_config_never_kills_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_CONFIG_PATH", str(tmp_path / "nowhere.yaml"))
    assert _load_routine_timeout_multiplier() is None


@pytest.mark.parametrize("bad", ["0", "-1", "11", "'not a number'"])
def test_a_value_outside_the_range_is_refused_at_load(
    bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RoutineConfig refuses these outright (extra=forbid, gt=0, le=10), so the
    loader's own guard is the second fence, not the first. Either way the build
    carries the budgets it always did."""
    _config(
        tmp_path,
        _MINIMAL + f"routine:\n  seat: openai:x\n  timeout_multiplier: {bad}\n",
        monkeypatch,
    )
    assert _load_routine_timeout_multiplier() is None
