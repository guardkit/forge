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
    TEST_ROOT_DISCOVERY_MODULE_CANDIDATES,
    DclAuthorOutcome,
    NormalizerModuleUnresolved,
    TargetTestRootsUnresolved,
    ToolOutcome,
    discover_target_test_roots,
    make_dcl_author,
    make_normalize_feature_spec,
    make_validate_feature_plan,
    make_validate_gate_registry,
    make_validate_pass_bar,
    resolve_normalizer_command,
)

# The real api_test sibling checkout (Rich's estate); present on the dev host,
# absent in a clean CI image — the REAL-repo assertion skips when it is missing.
_REAL_API_TEST = Path(__file__).resolve().parents[4] / "api_test"


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


# ---------------------------------------------------------------------------
# qa validate pass-bar (B4 round-19)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_pass_bar_ok_on_success_exit_zero(tmp_path: Path) -> None:
    run_fn, captured = _fake_run("success", 0)
    validate = make_validate_pass_bar(run_fn=run_fn)
    outcome = await validate(tmp_path, "qa/pass-bar-TASK-VER-001.yaml")
    assert outcome.ok
    # Rides the frozen seam as ``guardkit qa validate pass-bar <path>``.
    assert captured["subcommand"] == "qa"
    assert captured["args"] == [
        "validate",
        "pass-bar",
        "qa/pass-bar-TASK-VER-001.yaml",
    ]
    assert captured["repo_path"] == tmp_path
    assert captured["with_nats_streaming"] is False


@pytest.mark.asyncio
async def test_validate_pass_bar_red_on_schema_error(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("failed", 1, stderr="VALIDATION FAILED: missing task_id")
    validate = make_validate_pass_bar(run_fn=run_fn)
    outcome = await validate(tmp_path, "qa/pass-bar-X.yaml")
    assert not outcome.ok
    assert "missing task_id" in outcome.detail


@pytest.mark.asyncio
async def test_validate_pass_bar_never_crashes_on_raise(tmp_path: Path) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("guardkit blew up")

    validate = make_validate_pass_bar(run_fn=_boom)
    outcome = await validate(tmp_path, "qa/pass-bar-X.yaml")
    assert not outcome.ok
    assert "RuntimeError" in outcome.detail


# ---------------------------------------------------------------------------
# qa validate gate-registry (F2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_gate_registry_ok_on_success_exit_zero(tmp_path: Path) -> None:
    run_fn, captured = _fake_run("success", 0)
    validate = make_validate_gate_registry(run_fn=run_fn)
    outcome = await validate(tmp_path, "qa/gates/registry.yaml")
    assert outcome.ok
    # Rides the frozen seam as ``guardkit qa validate gate-registry <path>``.
    assert captured["subcommand"] == "qa"
    assert captured["args"] == [
        "validate",
        "gate-registry",
        "qa/gates/registry.yaml",
    ]
    assert captured["repo_path"] == tmp_path
    assert captured["with_nats_streaming"] is False


@pytest.mark.asyncio
async def test_validate_gate_registry_red_on_schema_error(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("failed", 1, stderr="VALIDATION FAILED: unknown gate id")
    validate = make_validate_gate_registry(run_fn=run_fn)
    outcome = await validate(tmp_path, "qa/gates/registry.yaml")
    assert not outcome.ok
    assert "unknown gate id" in outcome.detail


@pytest.mark.asyncio
async def test_validate_gate_registry_red_on_timeout_status(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("timeout", -1)
    validate = make_validate_gate_registry(run_fn=run_fn)
    outcome = await validate(tmp_path, "qa/gates/registry.yaml")
    assert not outcome.ok


@pytest.mark.asyncio
async def test_validate_gate_registry_never_crashes_on_raise(tmp_path: Path) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("guardkit blew up")

    validate = make_validate_gate_registry(run_fn=_boom)
    outcome = await validate(tmp_path, "qa/gates/registry.yaml")
    assert not outcome.ok
    assert "RuntimeError" in outcome.detail


def test_tool_outcome_is_frozen() -> None:
    o = ToolOutcome(ok=True)
    with pytest.raises(Exception):
        o.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# dcl author (W1-S2) — the §10 seat seam
# ---------------------------------------------------------------------------


def _fake_run_with_stdout(status: str, exit_code: int, stdout_tail: str = "", stderr: str = ""):
    captured: dict[str, Any] = {}

    async def _run(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status=status, exit_code=exit_code, stderr=stderr, stdout_tail=stdout_tail
        )

    return _run, captured


@pytest.mark.asyncio
async def test_dcl_author_wire_shape_on_exit_zero(tmp_path: Path) -> None:
    """The exact CLI contract: rides the frozen seam as
    ``guardkit dcl author --feature <slug> --task <id> --repo <wt> --request …
    --criteria … --json`` with ``with_nats_streaming=False``, and parses the
    --json envelope on exit 0."""
    envelope = (
        '{"authored": true, "attempts": 2, "zero_shot_clean": false, '
        '"repaired_clean": true, "artifact": "features/x/x.dcl", '
        '"receipt": "qa/dcl/authoring-x.yaml", "failure_reason": null}'
    )
    run_fn, captured = _fake_run_with_stdout("success", 0, stdout_tail=envelope)
    author = make_dcl_author(run_fn=run_fn)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="feature_spec_inputs/cid.md",
        criteria_rel=".guardkit/dcl-inputs/criteria-x.yaml",
    )
    assert isinstance(outcome, DclAuthorOutcome)
    assert outcome.authored is True
    assert outcome.exit_class == "authored"
    assert outcome.envelope["attempts"] == 2
    assert outcome.envelope["repaired_clean"] is True
    # Wire shape.
    assert captured["subcommand"] == "dcl"
    assert captured["args"] == [
        "author",
        "--feature",
        "x",
        "--task",
        "TASK-X-001",
        "--repo",
        str(tmp_path),
        "--request",
        "feature_spec_inputs/cid.md",
        "--criteria",
        ".guardkit/dcl-inputs/criteria-x.yaml",
        "--json",
    ]
    assert captured["repo_path"] == tmp_path
    assert captured["with_nats_streaming"] is False


@pytest.mark.asyncio
async def test_dcl_author_exit_one_is_loud_authoring_failure(tmp_path: Path) -> None:
    run_fn, _ = _fake_run_with_stdout(
        "failed", 1, stdout_tail='{"authored": false, "failure_reason": "dirty second attempt"}'
    )
    author = make_dcl_author(run_fn=run_fn)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="r.md",
        criteria_rel="c.yaml",
    )
    assert outcome.authored is False
    assert outcome.exit_class == "authoring-failed"
    assert "dirty second attempt" in outcome.detail


@pytest.mark.asyncio
async def test_dcl_author_exit_two_is_instrument_error(tmp_path: Path) -> None:
    run_fn, _ = _fake_run_with_stdout("failed", 2, stderr="node/checker missing")
    author = make_dcl_author(run_fn=run_fn)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="r.md",
        criteria_rel="c.yaml",
    )
    assert outcome.authored is False
    assert outcome.exit_class == "instrument-error"
    assert "node/checker missing" in outcome.detail


@pytest.mark.asyncio
async def test_dcl_author_timeout_status(tmp_path: Path) -> None:
    run_fn, _ = _fake_run_with_stdout("timeout", -1)
    author = make_dcl_author(run_fn=run_fn)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="r.md",
        criteria_rel="c.yaml",
    )
    assert outcome.authored is False
    assert outcome.exit_class == "timeout"


@pytest.mark.asyncio
async def test_dcl_author_never_crashes_on_raise(tmp_path: Path) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("guardkit blew up")

    author = make_dcl_author(run_fn=_boom)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="r.md",
        criteria_rel="c.yaml",
    )
    assert outcome.authored is False
    assert outcome.exit_class == "invocation-error"
    assert "RuntimeError" in outcome.detail


@pytest.mark.asyncio
async def test_dcl_author_exit_zero_but_envelope_authored_false(tmp_path: Path) -> None:
    """Exit 0 yet the envelope reports authored=false — treated as a loud
    authoring failure (never a silent success)."""
    run_fn, _ = _fake_run_with_stdout("success", 0, stdout_tail='{"authored": false}')
    author = make_dcl_author(run_fn=run_fn)
    outcome = await author(
        worktree_path=tmp_path,
        slug="x",
        task_id="TASK-X-001",
        request_rel="r.md",
        criteria_rel="c.yaml",
    )
    assert outcome.authored is False
    assert outcome.exit_class == "authoring-failed"


def test_dcl_author_outcome_is_frozen() -> None:
    o = DclAuthorOutcome(authored=True, exit_class="authored", exit_code=0, envelope={})
    with pytest.raises(Exception):
        o.authored = False  # type: ignore[misc]


def test_extract_json_envelope_tolerates_preamble() -> None:
    from forge.planning.target_terminal_tools import _extract_json_envelope

    # A log preamble before the envelope — the last balanced object wins.
    text = 'INFO probing seat\n{"authored": true, "attempts": 1}'
    assert _extract_json_envelope(text) == {"authored": True, "attempts": 1}
    # Garbage → empty (advisory metadata, never a gate).
    assert _extract_json_envelope("no json here") == {}
    assert _extract_json_envelope("") == {}


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


# ---------------------------------------------------------------------------
# Descriptor test-root discovery — REUSE of guardkit's discover_test_roots
# (B4 run 36629c5a, round 10). These pin that forge asks GUARDKIT for the roots
# (dual-candidate import), never re-guesses them, so the 008 descriptor carries
# the EXACT ``tests/<name>`` set the pre-commit ``feature validate`` oracle
# enforces (tests/health, tests/users for api_test — never a shallow "tests").
# ---------------------------------------------------------------------------


def _fake_import_for(modules: dict[str, Any]):
    """A fake ``importlib.import_module`` that only "imports" the given names.

    Names outside the mapping raise ``ModuleNotFoundError`` — the parent-missing
    shape real ``import_module`` produces when e.g. no top-level ``installer``
    package exists (a wheel install with no source checkout).
    """

    def _fake(name: str):
        if name in modules:
            return modules[name]
        raise ModuleNotFoundError(f"No module named {name!r}")

    return _fake


def test_discovery_candidates_mirror_the_wheel_then_source_shape() -> None:
    wheel, source = TEST_ROOT_DISCOVERY_MODULE_CANDIDATES
    assert wheel == "guardkit._installer_core.commands.lib.smoke_gates_nudge"
    assert source == "installer.core.commands.lib.smoke_gates_nudge"


def test_discovery_prefers_the_wheel_candidate() -> None:
    wheel, source = TEST_ROOT_DISCOVERY_MODULE_CANDIDATES
    wheel_mod = SimpleNamespace(
        discover_test_roots=lambda root: ["tests/from_wheel"]
    )
    source_mod = SimpleNamespace(
        discover_test_roots=lambda root: ["tests/from_source"]
    )
    roots = discover_target_test_roots(
        "/repo",
        import_module=_fake_import_for({wheel: wheel_mod, source: source_mod}),
    )
    # Both importable -> the wheel/pip path wins (production container form).
    assert roots == ["tests/from_wheel"]


def test_discovery_falls_back_to_the_source_candidate() -> None:
    _wheel, source = TEST_ROOT_DISCOVERY_MODULE_CANDIDATES
    source_mod = SimpleNamespace(
        discover_test_roots=lambda root: ["tests/from_source"]
    )
    # Only the source-checkout path importable (dev-host / editable form).
    roots = discover_target_test_roots(
        "/repo", import_module=_fake_import_for({source: source_mod})
    )
    assert roots == ["tests/from_source"]


def test_discovery_raises_naming_both_when_neither_resolves() -> None:
    with pytest.raises(TargetTestRootsUnresolved) as excinfo:
        discover_target_test_roots(
            "/repo", import_module=_fake_import_for({})
        )
    message = str(excinfo.value)
    for candidate in TEST_ROOT_DISCOVERY_MODULE_CANDIDATES:
        assert candidate in message
    assert "guardkit" in message


def test_discovery_passes_the_repo_path_through_to_guardkit() -> None:
    wheel, _source = TEST_ROOT_DISCOVERY_MODULE_CANDIDATES
    seen: list[Path] = []

    def _discover(root: Path) -> list[str]:
        seen.append(root)
        return ["tests/x"]

    discover_target_test_roots(
        "/srv/repos/api_test",
        import_module=_fake_import_for(
            {wheel: SimpleNamespace(discover_test_roots=_discover)}
        ),
    )
    # guardkit is handed a Path built from the repo_path string.
    assert seen == [Path("/srv/repos/api_test")]


def test_discovery_against_an_api_test_shaped_fixture(tmp_path: Path) -> None:
    """The real guardkit function (via the conftest source-checkout path) over an
    api_test-shaped tree returns the EXACT per-suite roots — not a bare 'tests'."""
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "users").mkdir(parents=True)
    (tmp_path / "tests" / "__pycache__").mkdir()  # skipped by guardkit
    roots = discover_target_test_roots(tmp_path)
    assert roots == ["tests/health", "tests/users"]


def test_discovery_empty_when_no_tests_tree(tmp_path: Path) -> None:
    # ASSUM-010: no tests/ tree -> empty roots (the plan may emit no smoke gate).
    assert discover_target_test_roots(tmp_path) == []


@pytest.mark.skipif(
    not (_REAL_API_TEST / "tests").is_dir(),
    reason="real api_test sibling checkout not present",
)
def test_discovery_against_the_REAL_api_test_checkout() -> None:
    """Against Rich's REAL api_test checkout, the descriptor roots are exactly
    the set guardkit's ``feature validate`` reports — the round-10 byte-identical
    roots (tests/health, tests/users) plus tests/version since the FEAT-B70F
    merge (``e0ad48a``, the B4 first live pass) landed the /version endpoint's
    tests (dated truth-update 2026-07-16). A live-fixture test: when the real
    checkout legitimately grows a root, update this list with a dated note —
    never loosen it to a pattern."""
    roots = discover_target_test_roots(_REAL_API_TEST)
    assert roots == ["tests/health", "tests/users", "tests/version"]
