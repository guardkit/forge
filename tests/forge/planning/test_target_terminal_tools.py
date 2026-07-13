"""Lane B / Phase E1 (B2) — the target-terminal oracle seams.

Unit-level coverage of :mod:`forge.planning.target_terminal_tools`: the
normalizer subprocess wrapper and the ``guardkit feature validate`` seam, both
exercised through their stubbable injection points (no real subprocess, no real
guardkit binary).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.planning.target_terminal_tools import (
    ToolOutcome,
    make_normalize_feature_spec,
    make_validate_feature_plan,
)


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


def _fake_normalizer(*, exit_code: int = 0, stderr: str = "", timed_out: bool = False):
    calls: list[dict[str, Any]] = []

    async def _seam(*, command, cwd, timeout):
        calls.append({"command": list(command), "cwd": cwd, "timeout": timeout})
        return ("", stderr, exit_code, timed_out)

    return _seam, calls


@pytest.mark.asyncio
async def test_normalize_ok_on_exit_zero(tmp_path: Path) -> None:
    seam, calls = _fake_normalizer(exit_code=0)
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert outcome.ok
    # The absolute feature path is the last token of the command.
    assert calls[0]["command"][-1].endswith("features/x/x.feature")


@pytest.mark.asyncio
async def test_normalize_red_on_nonzero_exit(tmp_path: Path) -> None:
    seam, _ = _fake_normalizer(exit_code=1, stderr="unparseable")
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert not outcome.ok
    assert "exit 1" in outcome.detail
    assert "unparseable" in outcome.detail


@pytest.mark.asyncio
async def test_normalize_red_on_timeout(tmp_path: Path) -> None:
    seam, _ = _fake_normalizer(timed_out=True)
    normalize = make_normalize_feature_spec(subprocess_seam=seam, timeout_seconds=5)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert not outcome.ok
    assert "timed out" in outcome.detail


@pytest.mark.asyncio
async def test_normalize_never_crashes_on_raise(tmp_path: Path) -> None:
    async def _boom(*, command, cwd, timeout):
        raise RuntimeError("exec failed")

    normalize = make_normalize_feature_spec(subprocess_seam=_boom)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert not outcome.ok
    assert "RuntimeError" in outcome.detail


# ---------------------------------------------------------------------------
# feature validate
# ---------------------------------------------------------------------------


def _fake_run(status: str, exit_code: int, stderr: str = "", tail: str = ""):
    captured: dict[str, Any] = {}

    async def _run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status=status, exit_code=exit_code, stderr=stderr, stdout_tail=tail
        )

    return _run, captured


@pytest.mark.asyncio
async def test_validate_ok_on_success_exit_zero(tmp_path: Path) -> None:
    run_fn, captured = _fake_run("success", 0)
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-BEEF")
    assert outcome.ok
    # Rides the frozen seam as ``guardkit feature validate <id> --json``.
    assert captured["subcommand"] == "feature"
    assert captured["args"] == ["validate", "FEAT-BEEF", "--json"]
    assert captured["repo_path"] == tmp_path


@pytest.mark.asyncio
async def test_validate_red_on_validation_errors(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("failed", 1, stderr="schema errors")
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-BEEF")
    assert not outcome.ok
    assert "schema errors" in outcome.detail


@pytest.mark.asyncio
async def test_validate_red_on_timeout_status(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("timeout", -1)
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-BEEF")
    assert not outcome.ok


@pytest.mark.asyncio
async def test_validate_never_crashes_on_raise(tmp_path: Path) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("guardkit blew up")

    validate = make_validate_feature_plan(run_fn=_boom)
    outcome = await validate(tmp_path, "FEAT-BEEF")
    assert not outcome.ok
    assert "RuntimeError" in outcome.detail


def test_tool_outcome_is_frozen() -> None:
    o = ToolOutcome(ok=True)
    with pytest.raises(Exception):
        o.ok = False  # type: ignore[misc]
