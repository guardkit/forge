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

from forge.planning import target_terminal_tools as ttt
from forge.planning.target_terminal_tools import (
    NORMALIZER_MODULE_CANDIDATES,
    NormalizerModuleUnresolved,
    ToolOutcome,
    make_normalize_feature_spec,
    make_validate_feature_plan,
    resolve_normalizer_command,
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


# ---------------------------------------------------------------------------
# Dual-candidate normalizer module resolution (B4 run 4b3b0893 fix)
#
# The guardkit distribution exposes ``feature_spec_normalize`` at DIFFERENT
# module paths depending on install layout: the wheel/pip form under the
# guardkit namespace (``guardkit._installer_core.…``, tried first) and the
# source/editable form (``installer.core.…``). resolve_normalizer_command picks
# whichever is importable and raises loudly — naming BOTH — when neither is.
# ---------------------------------------------------------------------------


def _find_spec_for(importable: set[str]):
    """A fake ``importlib.util.find_spec`` that only "imports" the given names.

    Names outside the set raise ``ModuleNotFoundError`` — the parent-missing
    shape real ``find_spec`` produces when e.g. no top-level ``installer``
    package exists (a wheel install with no source checkout).
    """

    def _fake(name: str):
        if name in importable:
            return object()
        raise ModuleNotFoundError(f"No module named {name!r}")

    return _fake


def test_resolve_prefers_the_wheel_candidate() -> None:
    wheel, source = NORMALIZER_MODULE_CANDIDATES
    # Both importable -> the wheel/pip path wins (production container form).
    cmd = resolve_normalizer_command(find_spec=_find_spec_for({wheel, source}))
    assert cmd == ("python", "-m", wheel)
    assert wheel.startswith("guardkit._installer_core")


def test_resolve_falls_back_to_the_source_candidate() -> None:
    _wheel, source = NORMALIZER_MODULE_CANDIDATES
    # Only the source-checkout path importable (dev/editable form).
    cmd = resolve_normalizer_command(find_spec=_find_spec_for({source}))
    assert cmd == ("python", "-m", source)
    assert source.startswith("installer.core")


def test_resolve_raises_naming_both_when_neither_resolves() -> None:
    with pytest.raises(NormalizerModuleUnresolved) as excinfo:
        resolve_normalizer_command(find_spec=_find_spec_for(set()))
    message = str(excinfo.value)
    # The loud error names BOTH candidates and points at the build fix.
    for candidate in NORMALIZER_MODULE_CANDIDATES:
        assert candidate in message
    assert "guardkit" in message


def test_resolve_treats_none_spec_as_not_importable() -> None:
    # find_spec may return None (not raise) for a missing leaf module — that is
    # also "not importable", so with no importable candidate we still raise.
    def _always_none(_name: str):
        return None

    with pytest.raises(NormalizerModuleUnresolved):
        resolve_normalizer_command(find_spec=_always_none)


def test_resolve_honours_a_custom_python_executable() -> None:
    wheel, _source = NORMALIZER_MODULE_CANDIDATES
    cmd = resolve_normalizer_command(
        python_executable="/opt/venv/bin/python",
        find_spec=_find_spec_for({wheel}),
    )
    assert cmd == ("/opt/venv/bin/python", "-m", wheel)


@pytest.mark.asyncio
async def test_normalize_none_prefix_resolves_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # command_prefix=None (production wiring) resolves the module path lazily on
    # first call via resolve_normalizer_command; the resolved module leads the
    # subprocess command.
    wheel = NORMALIZER_MODULE_CANDIDATES[0]
    monkeypatch.setattr(
        ttt, "resolve_normalizer_command", lambda **_kw: ("python", "-m", wheel)
    )
    seam, calls = _fake_normalizer(exit_code=0)
    normalize = make_normalize_feature_spec(command_prefix=None, subprocess_seam=seam)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert outcome.ok
    assert calls[0]["command"][:3] == ["python", "-m", wheel]
    assert calls[0]["command"][-1].endswith("features/x/x.feature")


@pytest.mark.asyncio
async def test_normalize_none_prefix_red_when_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When neither candidate resolves, the oracle returns a loud red outcome
    # (contained per the boundary doctrine) and never invokes the subprocess.
    def _raise(**_kw):
        raise NormalizerModuleUnresolved("candidate-a / candidate-b both absent")

    monkeypatch.setattr(ttt, "resolve_normalizer_command", _raise)
    seam, calls = _fake_normalizer(exit_code=0)
    normalize = make_normalize_feature_spec(command_prefix=None, subprocess_seam=seam)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert not outcome.ok
    assert "both absent" in outcome.detail
    assert calls == []
