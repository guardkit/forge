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
    DividerRepairResult,
    comment_box_drawing_dividers,
    FrontmatterIdRepair,
    FrontmatterRepairResult,
    repair_frontmatter_feature_id,
    repair_plan_task_frontmatter,
    NormalizerModuleUnresolved,
    TargetTestRootsUnresolved,
    ToolOutcome,
    discover_target_test_roots,
    discover_ts_shape_test_roots,
    shallow_discover_test_roots,
    make_normalize_feature_spec,
    make_validate_feature_plan,
    make_validate_gate_registry,
    make_validate_pass_bar,
    resolve_normalizer_command,
)

def _find_sibling_checkout(name: str) -> Path:
    """First ancestor directory that CONTAINS a checkout called ``name``.

    Replaces a fixed ``parents[4]`` hop, which was only correct when the tests
    ran from the forge repo root: inside a git worktree
    (``forge/.guardkit/worktrees/<LANE>/tests/forge/planning``) it resolved to
    ``forge/.guardkit/worktrees/api_test``, so the live-checkout test silently
    SKIPPED in every worktree run — the one venue the lanes actually work in.
    Returns a non-existent path when nothing is found, so the callers' skip
    guards still fire in a clean CI image.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return here.parents[-1] / name


# The real api_test sibling checkout (Rich's estate); present on the dev host,
# absent in a clean CI image — the REAL-repo assertion skips when it is missing.
_REAL_API_TEST = _find_sibling_checkout("api_test")


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
# P-7 box-drawing divider repair (defect-#14/#15 deterministic-repair pattern)
#
# The HB-4 pilot's 007 legs twice emitted decorative box-drawing divider lines
# at top level between scenarios; gherkin-official refuses them and the run died
# with no revision loop. The repair comments those lines out (conservative,
# information-preserving, LOUD) BEFORE the parse. The four fixtures below pin the
# ratified class exactly — (a) fires+GREEN, (b) refuses (conservative law),
# (c) byte-untouched, (d) docstring-safe.
# ---------------------------------------------------------------------------

# (a) run-6 shape: valid gherkin + 3 box-drawing divider lines at top level
#     (dividers at 1-based lines 3, 11, 17).
_FIXTURE_RUN6 = (
    "Feature: Invoice grouping\n"
    "\n"
    "  ━━ GROUP A: Key Examples (3 scenarios) ━━\n"
    "\n"
    "  Scenario: first\n"
    "    Given a thing\n"
    "    When I act\n"
    "    Then it works\n"
    "\n"
    "\n"
    "  ━━ GROUP B: Boundary Conditions (4 scenarios) ━━\n"
    "\n"
    "  Scenario: second\n"
    "    Given another\n"
    "    Then ok\n"
    "\n"
    "  ══ GROUP C ══\n"
    "  Scenario: third\n"
    "    Given x\n"
    "    Then y\n"
)

# (b) a bare NON-box-drawing text line in the same between-scenarios slot — the
#     parser refuses it and the conservative law says the repair must NOT fire.
_FIXTURE_BARE_NONBOX = (
    "Feature: F\n"
    "\n"
    "  Scenario: first\n"
    "    Given a\n"
    "    Then b\n"
    "\n"
    "  GROUP B plain prose that lost its keyword\n"
    "\n"
    "  Scenario: second\n"
    "    Given c\n"
    "    Then d\n"
)

# (c) a clean, already-parseable spec — the repair must not fire, byte-untouched.
_FIXTURE_CLEAN = (
    "Feature: F\n"
    "  Scenario: s\n"
    "    Given a\n"
    "    Then b\n"
)

# (d) a box-drawing line INSIDE a docstring block — legal content, not a
#     top-level divider; must be left byte-untouched.
_FIXTURE_DOCSTRING = (
    "Feature: F\n"
    "  Scenario: s\n"
    "    Given a payload\n"
    '      """\n'
    "      ━━ this is content, not a divider ━━\n"
    "      normal text\n"
    '      """\n'
    "    Then b\n"
)


def _gherkin_parses(text: str) -> bool:
    """True iff gherkin-official parses ``text`` (the downstream oracle's rule)."""
    parser_mod = pytest.importorskip("gherkin.parser")
    scanner_mod = pytest.importorskip("gherkin.token_scanner")
    try:
        parser_mod.Parser().parse(scanner_mod.TokenScanner(text))
        return True
    except Exception:  # noqa: BLE001 — any parse error == refuse
        return False


def _gherkin_parse_seam():
    """A fake normalizer subprocess that runs the REAL gherkin parse in-process.

    Reads the target ``.feature`` file (as rewritten by the pre-parse repair) and
    returns exit 0 iff gherkin-official accepts it — exactly the accept/refuse the
    production ``python -m …feature_spec_normalize`` subprocess encodes, but with
    no shell-out. This exercises the full wired ``_normalize`` path end-to-end.
    """
    pytest.importorskip("gherkin.parser")
    calls: list[dict[str, Any]] = []

    async def _seam(*, command, cwd, timeout):
        target = Path(command[-1])
        text = target.read_text(encoding="utf-8")
        calls.append({"text": text})
        return ("", "", 0, False) if _gherkin_parses(text) else ("", "refuse", 1, False)

    return _seam, calls


# --- pure repair function --------------------------------------------------


def test_repair_a_run6_fires_and_names_three_lines() -> None:
    result = comment_box_drawing_dividers(_FIXTURE_RUN6)
    assert isinstance(result, DividerRepairResult)
    assert result.fired
    # N=3, and the exact 1-based line numbers of the divider lines.
    assert result.commented_lines == [3, 11, 17]
    # The divider lines are now legal comments; the file parses GREEN.
    assert not _gherkin_parses(_FIXTURE_RUN6)  # raw refused
    assert _gherkin_parses(result.text)  # repaired accepted
    # Information-preserving: the original divider text survives inside a comment.
    assert "# " in result.text
    assert "GROUP A: Key Examples" in result.text
    assert "GROUP C" in result.text


def test_repair_b_bare_nonbox_line_does_not_fire_still_refuses() -> None:
    result = comment_box_drawing_dividers(_FIXTURE_BARE_NONBOX)
    # Conservative by law: only the box-drawing class triggers the repair.
    assert not result.fired
    assert result.commented_lines == []
    assert result.text == _FIXTURE_BARE_NONBOX  # byte-identical
    # And it still refuses exactly as today.
    assert not _gherkin_parses(_FIXTURE_BARE_NONBOX)


def test_repair_c_clean_spec_is_byte_untouched() -> None:
    result = comment_box_drawing_dividers(_FIXTURE_CLEAN)
    assert not result.fired
    assert result.commented_lines == []
    assert result.text == _FIXTURE_CLEAN
    assert _gherkin_parses(_FIXTURE_CLEAN)


def test_repair_d_divider_inside_docstring_is_not_touched() -> None:
    result = comment_box_drawing_dividers(_FIXTURE_DOCSTRING)
    # Docstring content is legal free text — the scan must respect docstring
    # context and leave the interior byte-untouched.
    assert not result.fired
    assert result.text == _FIXTURE_DOCSTRING
    assert _gherkin_parses(_FIXTURE_DOCSTRING)


def test_repair_is_idempotent() -> None:
    once = comment_box_drawing_dividers(_FIXTURE_RUN6)
    twice = comment_box_drawing_dividers(once.text)
    assert not twice.fired
    assert twice.text == once.text


# --- wired _normalize end-to-end (real gherkin parse via the seam) ---------


def _write_feature(tmp_path: Path, text: str) -> str:
    rel = "features/x/x.feature"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return rel


@pytest.mark.asyncio
async def test_normalize_a_repairs_in_place_then_parses_green(tmp_path: Path) -> None:
    rel = _write_feature(tmp_path, _FIXTURE_RUN6)
    seam, _calls = _gherkin_parse_seam()
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, rel)
    assert outcome.ok  # repaired file parsed GREEN
    # LOUD receipt: count + line numbers surfaced on the outcome.
    assert "P-7 divider repair" in outcome.detail
    assert "commented 3" in outcome.detail
    assert "[3, 11, 17]" in outcome.detail
    # The file on disk was rewritten with the dividers commented out.
    on_disk = (tmp_path / rel).read_text(encoding="utf-8")
    assert on_disk != _FIXTURE_RUN6
    assert _gherkin_parses(on_disk)


@pytest.mark.asyncio
async def test_normalize_b_bare_nonbox_still_refuses(tmp_path: Path) -> None:
    rel = _write_feature(tmp_path, _FIXTURE_BARE_NONBOX)
    seam, _calls = _gherkin_parse_seam()
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, rel)
    assert not outcome.ok  # refuse path unchanged
    assert "exit 1" in outcome.detail
    # File untouched (repair never fired).
    assert (tmp_path / rel).read_text(encoding="utf-8") == _FIXTURE_BARE_NONBOX


@pytest.mark.asyncio
async def test_normalize_c_clean_spec_untouched_no_receipt(tmp_path: Path) -> None:
    rel = _write_feature(tmp_path, _FIXTURE_CLEAN)
    seam, _calls = _gherkin_parse_seam()
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, rel)
    assert outcome.ok
    assert outcome.detail == ""  # repair did not fire → no receipt
    assert (tmp_path / rel).read_text(encoding="utf-8") == _FIXTURE_CLEAN


@pytest.mark.asyncio
async def test_normalize_d_docstring_divider_untouched(tmp_path: Path) -> None:
    rel = _write_feature(tmp_path, _FIXTURE_DOCSTRING)
    seam, _calls = _gherkin_parse_seam()
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, rel)
    assert outcome.ok
    assert outcome.detail == ""  # repair did not fire
    assert (tmp_path / rel).read_text(encoding="utf-8") == _FIXTURE_DOCSTRING


@pytest.mark.asyncio
async def test_normalize_missing_file_is_noop_repair(tmp_path: Path) -> None:
    # No file on disk (the existing stub-seam tests' shape): the repair is a
    # silent no-op and the leg proceeds to the seam exactly as before.
    seam, _calls = _fake_normalizer(exit_code=0)
    normalize = make_normalize_feature_spec(subprocess_seam=seam)
    outcome = await normalize(tmp_path, "features/x/x.feature")
    assert outcome.ok
    assert outcome.detail == ""


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


# ---------------------------------------------------------------------------
# THE REAL TEST-ROOT CURE (design §D.3(ii)) — the TypeScript shapes.
#
# The stage-B stopgap was to BEND ts-api-test: move ``tests/health.test.ts`` to
# ``tests/health/health.test.ts`` so the Python-shaped discovery had a
# subdirectory to find. These tests make that bend reversible — the ORIGINAL
# flat shape now discovers, so the repo can move back.
# ---------------------------------------------------------------------------


def _ts_api_test_original_shape(root: Path) -> None:
    """ts-api-test EXACTLY as scaffolded, before the stage-B shape-bend.

    Verified against the real checkout (design §C): ``tests/health.test.ts``
    flat, sources under ``src/``, no ``tests/<name>/`` subdirectory anywhere.
    """
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "health.test.ts").write_text(
        "import { describe, it } from 'vitest';\n", encoding="utf-8"
    )
    (root / "src" / "health").mkdir(parents=True)
    (root / "src" / "health" / "routes.ts").write_text("export {};\n", encoding="utf-8")
    (root / "package.json").write_text('{"name": "ts-api-test"}\n', encoding="utf-8")


def test_flat_ts_shape_discovers_tests_itself(tmp_path: Path) -> None:
    """ts-api-test's ORIGINAL flat shape yields ``["tests"]``, never ``[]``.

    ``[]`` was the near-blocker: the descriptor emitted ``test_roots: []`` and
    ASSUM-010 then made ANY ``smoke_gates`` block a plan-containment error, so a
    TypeScript feature could not carry an inter-wave smoke gate at all.
    """
    _ts_api_test_original_shape(tmp_path)
    assert discover_ts_shape_test_roots(tmp_path) == ["tests"]
    assert discover_target_test_roots(tmp_path) == ["tests"]


def test_the_stage_b_shape_bend_still_works_unchanged(tmp_path: Path) -> None:
    """The bent shape (``tests/health/health.test.ts``) is untouched by the cure.

    Reversibility cuts both ways: the repo may move back to flat, and it may
    also stay bent. The bent tree is a plain Python-shaped subdirectory, so
    guardkit's own discovery answers it and the TS pass adds nothing.
    """
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "health" / "health.test.ts").write_text("", encoding="utf-8")
    assert discover_ts_shape_test_roots(tmp_path) == []
    assert discover_target_test_roots(tmp_path) == ["tests/health"]


def test_singular_test_dir_flat(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "app.spec.ts").write_text("", encoding="utf-8")
    assert discover_target_test_roots(tmp_path) == ["test"]


def test_singular_test_dir_with_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "test" / "health").mkdir(parents=True)
    (tmp_path / "test" / "health" / "health.test.ts").write_text("", encoding="utf-8")
    (tmp_path / "test" / "users").mkdir(parents=True)
    (tmp_path / "test" / "users" / "users.test.ts").write_text("", encoding="utf-8")
    assert discover_target_test_roots(tmp_path) == ["test/health", "test/users"]


def test_colocated_dunder_tests_under_src(tmp_path: Path) -> None:
    (tmp_path / "src" / "health" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "health" / "__tests__" / "routes.test.ts").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "src" / "users" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "users" / "__tests__" / "svc.spec.tsx").write_text(
        "", encoding="utf-8"
    )
    assert discover_target_test_roots(tmp_path) == [
        "src/health/__tests__",
        "src/users/__tests__",
    ]


def test_empty_dunder_tests_dir_is_not_a_root(tmp_path: Path) -> None:
    (tmp_path / "src" / "health" / "__tests__").mkdir(parents=True)
    assert discover_ts_shape_test_roots(tmp_path) == []


def test_node_modules_is_never_walked(tmp_path: Path) -> None:
    """An installed dependency tree must not contribute roots (or cost)."""
    vendored = tmp_path / "src" / "node_modules" / "left-pad" / "__tests__"
    vendored.mkdir(parents=True)
    (vendored / "index.test.js").write_text("", encoding="utf-8")
    (tmp_path / "src" / "health" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "health" / "__tests__" / "a.test.ts").write_text(
        "", encoding="utf-8"
    )
    assert discover_ts_shape_test_roots(tmp_path) == ["src/health/__tests__"]


# --- REGRESSION PINS: the Python shape is byte-unchanged --------------------


def test_python_shape_gains_nothing_from_the_ts_pass(tmp_path: Path) -> None:
    """api_test's shape — flat ``test_*.py`` beside per-suite dirs — is untouched."""
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "users").mkdir(parents=True)
    (tmp_path / "tests" / "test_main.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")

    assert discover_ts_shape_test_roots(tmp_path) == []
    assert discover_target_test_roots(tmp_path) == ["tests/health", "tests/users"]


def test_a_python_repo_with_a_singular_test_dir_is_unchanged(tmp_path: Path) -> None:
    """``test/`` opens ONLY on TypeScript evidence — a Python ``test/`` tree is
    invisible to discovery exactly as it is today."""
    (tmp_path / "test" / "pkg").mkdir(parents=True)
    (tmp_path / "test" / "pkg" / "test_thing.py").write_text("", encoding="utf-8")
    assert discover_ts_shape_test_roots(tmp_path) == []
    assert discover_target_test_roots(tmp_path) == []


def test_ts_discovery_never_raises_on_a_missing_tree(tmp_path: Path) -> None:
    assert discover_ts_shape_test_roots(tmp_path / "nope") == []


# --- The guardkit-less degraded fallback ------------------------------------


def test_shallow_fallback_reproduces_the_per_suite_python_shape(
    tmp_path: Path,
) -> None:
    """The degraded path no longer answers the round-10 defect shape.

    It used to return a bare ``["tests"]`` — a PREFIX of every ``tests/<x>``
    path, which is exactly what let 008 invent ``tests/smoke`` and pass the
    in-session containment gate. It now returns the same per-suite roots
    guardkit would.
    """
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "users").mkdir(parents=True)
    (tmp_path / "tests" / "__pycache__").mkdir()
    assert shallow_discover_test_roots(tmp_path) == ["tests/health", "tests/users"]
    assert "tests" not in shallow_discover_test_roots(tmp_path)


def test_shallow_fallback_knows_the_flat_ts_shape(tmp_path: Path) -> None:
    _ts_api_test_original_shape(tmp_path)
    assert shallow_discover_test_roots(tmp_path) == ["tests"]


def test_shallow_fallback_empty_on_a_bare_repo(tmp_path: Path) -> None:
    assert shallow_discover_test_roots(tmp_path) == []


@pytest.mark.skipif(
    not (_REAL_API_TEST / "tests").is_dir(),
    reason="real api_test sibling checkout not present",
)
def test_discovery_against_the_REAL_api_test_checkout() -> None:
    """Against Rich's REAL api_test checkout, discovery returns a well-SHAPED
    root set — never a hard-coded inventory of that checkout's contents.

    THE ROT THIS FIXES (second-repo readiness, 2026-07-31). This assertion used
    to pin the exact list ``["tests/health", "tests/users", "tests/version"]``
    with an instruction to "update this list with a dated note — never loosen
    it to a pattern". That instruction made a forge unit test a downstream
    consumer of a SIBLING WORKING TREE's contents: every feature api_test grows
    breaks forge's suite for a reason that has nothing to do with forge. It had
    already been updated once by hand (FEAT-B70F / tests/version) and it broke
    again on FEAT-TIME's merge, which landed ``tests/time``.

    What is actually worth asserting is the CONTRACT, and it is fully
    shape-expressible: discovery of a real Python checkout yields a non-empty
    set, every entry is a proper subdirectory of ``tests/`` (never bare
    ``tests``, which is the round-10 defect that let 008 invent ``tests/smoke``
    as a "prefix"), the set is sorted and duplicate-free, and each entry names a
    directory that exists on disk. The exact inventory is api_test's business.

    Exact-contents pinning of a live checkout is now out of bounds for this
    file (design §F.5): a fixture that needs specific contents is a COMMITTED
    fixture under ``tests/``, built with ``tmp_path`` — see the shape tests
    above, which do exactly that.
    """
    roots = discover_target_test_roots(_REAL_API_TEST)

    # Non-empty: a real Python checkout with a tests/ tree has roots.
    assert roots, "real api_test checkout yielded no test roots"
    # Sorted, duplicate-free.
    assert roots == sorted(roots)
    assert len(roots) == len(set(roots))
    for root in roots:
        # A proper SUBDIRECTORY of tests/ — never bare "tests".
        assert root.startswith("tests/"), root
        assert root != "tests"
        suffix = root[len("tests/") :]
        assert suffix, root
        assert "/" not in suffix, f"{root}: roots are one level deep"
        assert (_REAL_API_TEST / root).is_dir(), f"{root} is not a directory"


# ---------------------------------------------------------------------------
# P-8 — task-doc frontmatter feature_id truncation repair
# ---------------------------------------------------------------------------


def _task_doc(feature_id: str, task_id: str = "TASK-A001") -> str:
    """A minimal task doc with a YAML frontmatter block carrying ``feature_id``."""
    return (
        "---\n"
        f"id: {task_id}\n"
        'title: "A task"\n'
        "status: backlog\n"
        f"feature_id: {feature_id}\n"
        "wave: 1\n"
        "---\n"
        "\n"
        "# A task\n"
        "\n"
        "Body prose that mentions the feature id in passing.\n"
    )


# --- pure repair function --------------------------------------------------


def test_p8_repair_fires_on_strict_prefix_truncation() -> None:
    # run-7 grain: id-minus-one-char is a strict prefix -> repaired.
    doc = _task_doc("FEAT-07F")  # canonical FEAT-07F3
    new_text, before = repair_frontmatter_feature_id(doc, "FEAT-07F3")
    assert before == "FEAT-07F"
    assert "feature_id: FEAT-07F3\n" in new_text
    # ONLY the feature_id field changed — every other byte survives.
    assert new_text == doc.replace("feature_id: FEAT-07F\n", "feature_id: FEAT-07F3\n")


def test_p8_repair_preserves_quoting_style() -> None:
    doc = _task_doc('"FEAT-048"')  # canonical FEAT-0482, quoted
    new_text, before = repair_frontmatter_feature_id(doc, "FEAT-0482")
    assert before == "FEAT-048"
    assert 'feature_id: "FEAT-0482"\n' in new_text


def test_p8_repair_negative_non_prefix_is_untouched() -> None:
    # A NON-prefix mismatch must NOT be touched (conservative by law).
    doc = _task_doc("FEAT-9999")  # canonical FEAT-07F3
    new_text, before = repair_frontmatter_feature_id(doc, "FEAT-07F3")
    assert before is None
    assert new_text == doc  # byte-identical


def test_p8_repair_clean_doc_byte_untouched() -> None:
    doc = _task_doc("FEAT-07F3")  # already canonical
    new_text, before = repair_frontmatter_feature_id(doc, "FEAT-07F3")
    assert before is None
    assert new_text == doc


def test_p8_repair_empty_value_untouched() -> None:
    doc = _task_doc("")  # empty frontmatter value
    new_text, before = repair_frontmatter_feature_id(doc, "FEAT-07F3")
    assert before is None
    assert new_text == doc


def test_p8_repair_no_frontmatter_untouched() -> None:
    body = "# A task\n\nNo frontmatter here, feature_id: FEAT-07F in prose.\n"
    new_text, before = repair_frontmatter_feature_id(body, "FEAT-07F3")
    assert before is None
    assert new_text == body


def test_p8_repair_is_idempotent() -> None:
    doc = _task_doc("FEAT-07F")
    once, before = repair_frontmatter_feature_id(doc, "FEAT-07F3")
    assert before == "FEAT-07F"
    twice, before2 = repair_frontmatter_feature_id(once, "FEAT-07F3")
    assert before2 is None
    assert twice == once


# --- tree-level scan (repair_plan_task_frontmatter) ------------------------


def _write_task_tree(root: Path, docs: dict[str, str]) -> None:
    for rel, content in docs.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_p8_tree_run7_shape_all_five_docs_repaired(tmp_path: Path) -> None:
    docs = {
        f"tasks/backlog/TASK-A00{i}.md": _task_doc("FEAT-07F", f"TASK-A00{i}")
        for i in range(1, 6)
    }
    _write_task_tree(tmp_path, docs)
    result = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert isinstance(result, FrontmatterRepairResult)
    assert result.fired
    assert len(result.repairs) == 5
    assert {r.rel_path for r in result.repairs} == set(docs)
    assert all(r.before == "FEAT-07F" and r.after == "FEAT-07F3" for r in result.repairs)
    # Every doc on disk now carries the canonical id.
    for rel in docs:
        assert "feature_id: FEAT-07F3\n" in (tmp_path / rel).read_text(encoding="utf-8")
    # LOUD receipt names the count and every doc.
    receipt = result.receipt("FEAT-07F3")
    assert "rewrote 5" in receipt
    assert "FEAT-07F -> FEAT-07F3" in receipt


def test_p8_tree_run4_shape_only_drifted_doc_repaired(tmp_path: Path) -> None:
    docs = {
        "tasks/backlog/TASK-A001.md": _task_doc("FEAT-0482", "TASK-A001"),  # clean
        "tasks/backlog/TASK-A002.md": _task_doc("FEAT-048", "TASK-A002"),   # drifted
        "tasks/backlog/TASK-A003.md": _task_doc("FEAT-0482", "TASK-A003"),  # clean
    }
    _write_task_tree(tmp_path, docs)
    clean_before = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    result = repair_plan_task_frontmatter(tmp_path, "FEAT-0482")
    assert result.fired
    assert len(result.repairs) == 1
    assert result.repairs[0].rel_path == "tasks/backlog/TASK-A002.md"
    # The clean docs are byte-untouched.
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == clean_before


def test_p8_tree_negative_non_prefix_untouched(tmp_path: Path) -> None:
    docs = {"tasks/backlog/TASK-A001.md": _task_doc("FEAT-9999", "TASK-A001")}
    _write_task_tree(tmp_path, docs)
    original = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    result = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert not result.fired
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == original


def test_p8_tree_clean_docs_do_not_fire(tmp_path: Path) -> None:
    docs = {"tasks/backlog/TASK-A001.md": _task_doc("FEAT-07F3", "TASK-A001")}
    _write_task_tree(tmp_path, docs)
    original = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    result = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert not result.fired
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == original


def test_p8_tree_idempotent(tmp_path: Path) -> None:
    docs = {"tasks/backlog/TASK-A001.md": _task_doc("FEAT-07F", "TASK-A001")}
    _write_task_tree(tmp_path, docs)
    first = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert first.fired
    after_first = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    second = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert not second.fired
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == after_first


def test_p8_tree_no_tasks_dir_is_noop(tmp_path: Path) -> None:
    result = repair_plan_task_frontmatter(tmp_path, "FEAT-07F3")
    assert not result.fired


# --- wired _validate end-to-end (fake oracle checks per-doc parity) --------


def _parity_validate_seam(canonical: str):
    """A fake guardkit ``feature validate`` that exits 0 iff EVERY task doc's
    frontmatter feature_id equals ``canonical`` — the accept/refuse the real
    oracle encodes for per-doc parity, with no shell-out. Returns the offending
    doc's line on stdout_tail when it refuses (so P-5 can be exercised too)."""
    calls: list[dict[str, Any]] = []

    async def _run(**kwargs):
        calls.append(dict(kwargs))
        repo = Path(kwargs["repo_path"])
        tasks = repo / "tasks"
        bad: list[str] = []
        if tasks.is_dir():
            for doc in sorted(tasks.rglob("*.md")):
                text = doc.read_text(encoding="utf-8")
                # Parity check: the frontmatter must carry the canonical id line.
                if f"feature_id: {canonical}\n" not in text:
                    bad.append(str(doc.relative_to(repo)))
        if bad:
            return SimpleNamespace(
                status="failed",
                exit_code=1,
                stderr="",
                stdout_tail="FRONTMATTER PARITY FAILED: " + ", ".join(bad),
            )
        return SimpleNamespace(status="success", exit_code=0, stderr="", stdout_tail="")

    return _run, calls


@pytest.mark.asyncio
async def test_p8_wired_run7_all_repaired_validate_proceeds(tmp_path: Path) -> None:
    docs = {
        f"tasks/backlog/TASK-A00{i}.md": _task_doc("FEAT-07F", f"TASK-A00{i}")
        for i in range(1, 6)
    }
    _write_task_tree(tmp_path, docs)
    run_fn, _calls = _parity_validate_seam("FEAT-07F3")
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-07F3")
    assert outcome.ok  # repaired tree passes the parity oracle
    # LOUD receipt surfaced on the outcome.
    assert "P-8 frontmatter repair" in outcome.detail
    assert "rewrote 5" in outcome.detail


@pytest.mark.asyncio
async def test_p8_wired_run4_one_repaired_validate_proceeds(tmp_path: Path) -> None:
    docs = {
        "tasks/backlog/TASK-A001.md": _task_doc("FEAT-0482", "TASK-A001"),
        "tasks/backlog/TASK-A002.md": _task_doc("FEAT-048", "TASK-A002"),
    }
    _write_task_tree(tmp_path, docs)
    run_fn, _calls = _parity_validate_seam("FEAT-0482")
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-0482")
    assert outcome.ok
    assert "rewrote 1" in outcome.detail


@pytest.mark.asyncio
async def test_p8_wired_negative_non_prefix_still_refuses(tmp_path: Path) -> None:
    docs = {"tasks/backlog/TASK-A001.md": _task_doc("FEAT-9999", "TASK-A001")}
    _write_task_tree(tmp_path, docs)
    original = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    run_fn, _calls = _parity_validate_seam("FEAT-07F3")
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-07F3")
    assert not outcome.ok  # refuse path unchanged — repair never touched it
    assert "FRONTMATTER PARITY FAILED" in outcome.detail
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_p8_wired_clean_docs_no_receipt(tmp_path: Path) -> None:
    docs = {"tasks/backlog/TASK-A001.md": _task_doc("FEAT-07F3", "TASK-A001")}
    _write_task_tree(tmp_path, docs)
    original = (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8")
    run_fn, _calls = _parity_validate_seam("FEAT-07F3")
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-07F3")
    assert outcome.ok
    assert outcome.detail == ""  # repair did not fire -> no receipt
    assert (tmp_path / "tasks/backlog/TASK-A001.md").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# P-5 — persist the FULL validate stdout+stderr (observability rider)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p5_refusing_stdout_line_survives_stderr_preamble(tmp_path: Path) -> None:
    # The real incident: an INFO/WARNING stderr preamble drowned the actual
    # refusing ``--json`` lines on stdout, which were then lost. Both must now
    # reach the persisted error.
    stderr = "INFO boot\nWARNING deprecated flag\n" + ("noise\n" * 40)
    tail = 'DISTINCTIVE: {"errors": ["feature_id parity: FEAT-07F != FEAT-07F3"]}'
    run_fn, _ = _fake_run("failed", 1, stderr=stderr, tail=tail)
    validate = make_validate_feature_plan(run_fn=run_fn)
    outcome = await validate(tmp_path, "FEAT-07F3")
    assert not outcome.ok
    assert "DISTINCTIVE" in outcome.detail
    assert "feature_id parity" in outcome.detail
    assert "WARNING deprecated flag" in outcome.detail  # stderr preserved too


def test_p5_combine_keeps_tail_and_marks_truncated_head() -> None:
    stderr = "S" * 20
    # A large stdout whose refusing line is at the very END.
    tail = ("X" * 20000) + "\nTHE-REFUSING-LINE"
    combined = ttt._combine_validate_error_streams(stdout_tail=tail, stderr=stderr)
    assert combined.startswith("[truncated head]\n")
    assert "THE-REFUSING-LINE" in combined  # tail survived
    assert len(combined.encode("utf-8")) <= 8192 + len("[truncated head]\n") + 1


def test_p5_combine_no_streams_is_empty() -> None:
    assert ttt._combine_validate_error_streams(stdout_tail="", stderr="") == ""


def test_python_only_dunder_tests_dir_is_not_a_root(tmp_path):
    """The src/**/__tests__ shape is TS-gated: a Python repo's answer stays []."""
    (tmp_path / "src" / "pkg" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__tests__" / "test_x.py").write_text("def test_x(): pass\n")
    from forge.planning.target_terminal_tools import discover_target_test_roots

    assert discover_target_test_roots(tmp_path) == []


def test_ts_dunder_tests_dir_is_a_root(tmp_path):
    (tmp_path / "src" / "pkg" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__tests__" / "x.test.ts").write_text("test\n")
    from forge.planning.target_terminal_tools import discover_target_test_roots

    assert "src/pkg/__tests__" in discover_target_test_roots(tmp_path)


# ---------------------------------------------------------------------------
# THE STAMP NORMALIZER seam (Rich's condition 1, 2026-08-16)
#
# ``guardkit qa normalize-stamps --feature <id> --repo <worktree>`` through the
# frozen guardkit run seam; the classifier is exercised on the CLI's REAL
# captured stdout shapes (tests/forge/planning/fixtures/stamp_normalizer/), the
# clipped-tail and console-echo fallbacks, click's "No such command" (older
# image), and the feature_files fill.
# ---------------------------------------------------------------------------

import shutil  # noqa: E402

from forge.planning.target_terminal_tools import (  # noqa: E402
    FeatureFilesFill,
    classify_normalizer_result,
    declare_feature_files_if_absent,
    make_normalize_stamps,
    parse_normalizer_payload,
)

_STAMP_FIXTURES = Path(__file__).parent / "fixtures" / "stamp_normalizer"
_CLICK_NO_SUCH_COMMAND = (
    "Usage: guardkit qa [OPTIONS] COMMAND [ARGS]...\n"
    "Try 'guardkit qa --help' for help.\n\n"
    "Error: No such command 'normalize-stamps'.\n"
)
_MOON = (
    "The moon is made of a very particular kind of cheese that no rule family "
    "in the design has ever heard about at all"
)


def _fixture(name: str) -> str:
    return (_STAMP_FIXTURES / name).read_text(encoding="utf-8")


def _tail(text: str, n: int = 4096) -> str:
    """The frozen seam's 4 KB stdout tail (parser._tail_bytes shape)."""
    b = text.encode("utf-8")
    return b[-n:].decode("utf-8", errors="ignore") if len(b) > n else text


# -- classify: the real shapes -------------------------------------------------


def test_classify_written_on_the_real_exit0_shape() -> None:
    out = classify_normalizer_result(
        "FEAT-TIME", status="success", exit_code=0,
        stdout_tail=_fixture("written-stdout.txt"), stderr="",
    )
    assert out.status == "written"
    assert not out.stops_the_run
    assert out.stamped == {
        "Reading the current server time": "hurl",
        "The time is fresh on every request": "hurl",
        "Write methods are rejected": "hurl",
        "The endpoint is unaffected by database unavailability": "probe:process",
    }
    assert out.already_stamped == ()
    assert "4 scenario(s) stamped by rule" in out.detail
    rec = out.receipt()
    assert rec["status"] == "written" and rec["stamped_count"] == 4
    assert rec["refused_titles"] == []


def test_classify_nothing_to_do_on_the_real_exit0_shape() -> None:
    out = classify_normalizer_result(
        "FEAT-TIME", status="success", exit_code=0,
        stdout_tail=_fixture("nothing-to-do-stdout.txt"), stderr="",
    )
    assert out.status == "nothing-to-do"
    assert not out.stops_the_run
    assert out.stamped == {}
    assert len(out.already_stamped) == 4
    assert "4 scenario(s) already stamped" in out.detail


def test_classify_refused_on_the_real_exit2_shape_names_titles_verbatim() -> None:
    out = classify_normalizer_result(
        "FEAT-TIME", status="failed", exit_code=2,
        stdout_tail=_fixture("refusal-stdout.txt"), stderr="",
    )
    assert out.status == "refused"
    assert out.stops_the_run
    assert out.refused_titles == (_MOON, "Another undecidable one")
    assert not out.titles_recovered_from_console_echo  # read from the JSON
    assert "2 scenario(s) undecidable by rule" in out.detail
    assert out.receipt()["refused_titles"] == [_MOON, "Another undecidable one"]


def test_classify_cannot_run_is_failed_with_the_reason() -> None:
    out = classify_normalizer_result(
        "FEAT-NOPE", status="failed", exit_code=2,
        stdout_tail=_fixture("not-found-stdout.txt"), stderr="",
    )
    assert out.status == "failed"
    assert out.stops_the_run
    assert out.refused_titles == ()
    assert "feature file not found" in out.detail
    assert "exit 2" in out.detail


def test_classify_unavailable_on_clicks_no_such_command() -> None:
    """An OLDER guardkit (pre-rebake image): continue, never silent."""
    out = classify_normalizer_result(
        "FEAT-X", status="failed", exit_code=2, stdout_tail="", stderr=_CLICK_NO_SUCH_COMMAND
    )
    assert out.status == "unavailable"
    assert not out.stops_the_run
    assert out.detail.startswith("normalizer unavailable")
    assert "No such command 'normalize-stamps'" in out.detail
    assert "backward compatible until the rebake" in out.detail
    # a guardkit so old it lacks the whole `qa` group reads the same way
    out2 = classify_normalizer_result(
        "FEAT-X", status="failed", exit_code=2, stdout_tail="",
        stderr="Error: No such command 'qa'.\n",
    )
    assert out2.status == "unavailable"


def test_classify_unavailable_needs_a_nonzero_exit() -> None:
    """The phrase alone on a GREEN run is not an unavailability (a title could
    contain it); only click's non-zero usage error is."""
    out = classify_normalizer_result(
        "FEAT-X", status="success", exit_code=0,
        stdout_tail=_fixture("written-stdout.txt"),
        stderr="INFO something No such command 'normalize-stamps' mentioned\n",
    )
    assert out.status == "written"


def test_classify_timeout_is_failed_and_stops() -> None:
    out = classify_normalizer_result(
        "FEAT-X", status="timeout", exit_code=-1, stdout_tail="", stderr=""
    )
    assert out.status == "failed" and out.stops_the_run
    assert "timed out" in out.detail


def test_classify_nonzero_without_any_readable_result_is_failed_with_streams() -> None:
    out = classify_normalizer_result(
        "FEAT-X", status="failed", exit_code=1,
        stdout_tail="Traceback ...\nKeyError: 'boom'\n", stderr="warn\n",
    )
    assert out.status == "failed" and out.stops_the_run
    assert "no readable result" in out.detail
    assert "KeyError: 'boom'" in out.detail and "warn" in out.detail


# -- parse: whole JSON, clipped tail, console echo -----------------------------


def test_parse_whole_json_ignores_the_trailing_console_echo() -> None:
    payload = parse_normalizer_payload(_fixture("refusal-stdout.txt"))
    assert payload is not None and "partial" not in payload
    assert payload["refused"] == [_MOON, "Another undecidable one"]
    assert payload["written"] is False
    assert payload["error"].startswith("STAMP NORMALIZER: feature FEAT-TIME has 2 UNDECIDABLE")


def test_parse_clipped_head_reads_the_refused_block_from_the_json_tail() -> None:
    """Many refused titles push the head past the seam's 4 KB tail: the JSON's
    ``"refused": [...]`` block (before ``"written"``) still reads whole and
    the titles come from it, not the wrapped echo."""
    text = _fixture("refusal-stdout.txt")
    # Clip INTO the error line so the tail no longer starts at "{".
    clipped = text[text.index('"error"') + 40 :]
    payload = parse_normalizer_payload(clipped)
    assert payload is not None and payload.get("partial") is True
    assert payload["refused"] == [_MOON, "Another undecidable one"]
    assert payload["written"] is False
    assert "from_console_echo" not in payload
    out = classify_normalizer_result(
        "FEAT-TIME", status="failed", exit_code=2, stdout_tail=clipped, stderr=""
    )
    assert out.status == "refused"
    assert out.refused_titles == (_MOON, "Another undecidable one")
    assert not out.titles_recovered_from_console_echo


def test_parse_console_echo_is_the_last_resort_and_rejoins_wrapped_titles() -> None:
    """Only the rich echo survived (wrapped at 80 cols): the titles are
    re-joined on single spaces and the outcome SAYS they came from the echo."""
    text = _fixture("refusal-stdout.txt")
    echo_only = text[text.index("✗ normalize-stamps REFUSED") :]
    assert "\n" in _MOON[:0] + echo_only  # sanity: it is wrapped text
    payload = parse_normalizer_payload(echo_only)
    assert payload is not None
    assert payload["from_console_echo"] is True
    assert payload["refused"] == [_MOON, "Another undecidable one"]
    out = classify_normalizer_result(
        "FEAT-TIME", status="failed", exit_code=2, stdout_tail=echo_only, stderr=""
    )
    assert out.status == "refused"
    assert out.refused_titles == (_MOON, "Another undecidable one")
    assert out.titles_recovered_from_console_echo
    assert out.receipt()["titles_recovered_from_console_echo"] is True


def test_parse_returns_none_on_noise() -> None:
    assert parse_normalizer_payload("") is None
    assert parse_normalizer_payload("just some log lines\nno json here\n") is None
    assert parse_normalizer_payload("{ not json") is None


def test_parse_exit0_with_a_clipped_head_still_reads_written() -> None:
    text = _fixture("written-stdout.txt")
    clipped = text[text.index('"reasons"') :]
    payload = parse_normalizer_payload(clipped)
    assert payload == {"written": True, "refused": [], "partial": True}
    out = classify_normalizer_result(
        "FEAT-TIME", status="success", exit_code=0, stdout_tail=clipped, stderr=""
    )
    assert out.status == "written"


# -- the feature_files fill -----------------------------------------------------


def _write_plan_yaml(root: Path, feature_id: str, body: str) -> Path:
    p = root / ".guardkit" / "features" / f"{feature_id}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_fill_declares_feature_files_when_the_plan_writer_omitted_it(tmp_path: Path) -> None:
    """The live 008 shape (api_test FEAT-F924): no ``feature_files:`` at all.
    forge names the committed spec .feature — appended, commented, loud."""
    p = _write_plan_yaml(tmp_path, "FEAT-F924", "id: FEAT-F924\ntasks:\n- id: TASK-F924-001\n")
    fill = declare_feature_files_if_absent(
        tmp_path, "FEAT-F924", ["features/user-search/user-search.feature"]
    )
    assert fill == FeatureFilesFill(
        True,
        ".guardkit/features/FEAT-F924.yaml",
        feature_files=("features/user-search/user-search.feature",),
        reason="filled by forge",
    )
    text = p.read_text(encoding="utf-8")
    assert text.endswith(
        'feature_files:\n  - "features/user-search/user-search.feature"\n'
    )
    assert "declared by forge at plan-commit" in text
    import yaml as _yaml

    data = _yaml.safe_load(text)
    assert data["feature_files"] == ["features/user-search/user-search.feature"]
    assert data["tasks"] == [{"id": "TASK-F924-001"}]  # nothing else touched


def test_fill_leaves_a_present_key_alone_even_when_empty(tmp_path: Path) -> None:
    for body in (
        "id: FEAT-A\nfeature_files:\n  - features/a/a.feature\ntasks: []\n",
        "id: FEAT-A\nfeature_files: []\ntasks: []\n",
        "id: FEAT-A\nfeature_files:\ntasks: []\n",
    ):
        p = _write_plan_yaml(tmp_path, "FEAT-A", body)
        fill = declare_feature_files_if_absent(tmp_path, "FEAT-A", ["features/x/x.feature"])
        assert not fill.fired and fill.reason == "feature_files: already declared"
        assert p.read_text(encoding="utf-8") == body


def test_fill_is_a_noop_without_a_yaml_or_without_a_known_feature_path(tmp_path: Path) -> None:
    fill = declare_feature_files_if_absent(tmp_path, "FEAT-A", ["features/x/x.feature"])
    assert not fill.fired and fill.reason == "plan YAML not present"
    _write_plan_yaml(tmp_path, "FEAT-A", "id: FEAT-A\n")
    fill = declare_feature_files_if_absent(tmp_path, "FEAT-A", [])
    assert not fill.fired and fill.reason == "no committed .feature path known"


def test_fill_does_not_match_a_nested_or_commented_feature_files(tmp_path: Path) -> None:
    """Only a TOP-LEVEL ``feature_files:`` counts as declared."""
    body = "id: FEAT-A\n# feature_files: (todo)\nmeta:\n  feature_files: nested\n"
    _write_plan_yaml(tmp_path, "FEAT-A", body)
    fill = declare_feature_files_if_absent(tmp_path, "FEAT-A", ["features/x/x.feature"])
    assert fill.fired


# -- make_normalize_stamps: the seam call ---------------------------------------


@pytest.mark.asyncio
async def test_make_normalize_stamps_rides_the_frozen_seam_with_cwd_the_worktree(tmp_path: Path) -> None:
    run_fn, captured = _fake_run("success", 0, tail=_fixture("written-stdout.txt"))
    normalize = make_normalize_stamps(run_fn=run_fn)
    out = await normalize(tmp_path, "FEAT-TIME")
    assert out.status == "written"
    assert captured["subcommand"] == "qa"
    assert captured["args"] == [
        "normalize-stamps", "--feature", "FEAT-TIME", "--repo", str(tmp_path)
    ]
    assert captured["repo_path"] == tmp_path
    assert captured["read_allowlist"] == [tmp_path]
    assert captured["with_nats_streaming"] is False
    assert captured["timeout_seconds"] == 120  # rules, not a model: seconds


@pytest.mark.asyncio
async def test_make_normalize_stamps_reads_the_stamp_count_back_off_the_branch(tmp_path: Path) -> None:
    """The receipt's ``stamps_on_branch`` comes from the plan of record on disk
    (the close-side reader), not from the seam's clipped stdout."""
    _write_plan_yaml(
        tmp_path,
        "FEAT-TIME",
        'id: FEAT-TIME\nfeature_files:\n  - f.feature\nscenarios:\n'
        '  "a":\n    verifier: "hurl"\n  "b":\n    verifier: "probe:process"\n',
    )
    run_fn, _ = _fake_run("success", 0, tail=_fixture("written-stdout.txt"))
    out = await make_normalize_stamps(run_fn=run_fn)(tmp_path, "FEAT-TIME")
    assert out.status == "written"
    assert out.stamps_on_branch == 2
    assert out.receipt()["stamps_on_branch"] == 2


@pytest.mark.asyncio
async def test_make_normalize_stamps_unavailable_and_refused_and_raise(tmp_path: Path) -> None:
    run_fn, _ = _fake_run("failed", 2, stderr=_CLICK_NO_SUCH_COMMAND)
    assert (await make_normalize_stamps(run_fn=run_fn)(tmp_path, "FEAT-X")).status == "unavailable"
    run_fn, _ = _fake_run("failed", 2, tail=_fixture("refusal-stdout.txt"))
    out = await make_normalize_stamps(run_fn=run_fn)(tmp_path, "FEAT-TIME")
    assert out.status == "refused" and out.refused_titles[1] == "Another undecidable one"

    async def _boom(**kwargs):
        raise RuntimeError("guardkit blew up")

    out = await make_normalize_stamps(run_fn=_boom)(tmp_path, "FEAT-X")
    assert out.status == "failed" and "RuntimeError" in out.detail


# -- LIVE: the real guardkit CLI through the real parser ------------------------
#
# Skips unless a guardkit checkout carrying the normalizer is reachable (see
# tests/forge/planning/_live_guardkit.py: FORGE_GUARDKIT_NORMALIZER_CHECKOUT /
# _PYTHON, else the sibling checkout once the lane merges). Drives
# ``python -m guardkit.cli.main qa normalize-stamps`` as a subprocess exactly as
# the frozen seam would (stdout NOT a tty), folds it through
# ``parse_guardkit_output`` (the 4 KB tail and all), and proves written /
# nothing-to-do / refused / not-found end to end on the api_test 5bc6fd1 fixture.

from tests.forge.planning._live_guardkit import live_guardkit_or_skip, live_run_fn  # noqa: E402


@pytest.mark.asyncio
async def test_live_guardkit_normalize_stamps_written_refused_and_not_found(tmp_path: Path) -> None:
    checkout, python = live_guardkit_or_skip(Path(__file__))
    fixture_src = checkout / "tests" / "fixtures" / "stamp_normalizer" / "api_test_5bc6fd1"
    if not fixture_src.is_dir():
        pytest.skip("guardkit's api_test_5bc6fd1 fixture not present")

    wt = tmp_path / "wt"
    shutil.copytree(fixture_src, wt)
    yaml_path = wt / ".guardkit" / "features" / "FEAT-TIME.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    import re as _re

    text = _re.sub(r"scenarios:\n(?:  .*\n)+", "", text)  # strip the hand stamps
    yaml_path.write_text(text, encoding="utf-8")

    normalize = make_normalize_stamps(run_fn=live_run_fn(checkout, python))

    # (1) written — and the stamps are ON DISK in the worktree.
    out = await normalize(wt, "FEAT-TIME")
    assert out.status == "written", out
    assert out.stamped["The endpoint is unaffected by database unavailability"] == "probe:process"
    assert out.stamps_on_branch == 4
    assert 'verifier: "hurl"' in yaml_path.read_text(encoding="utf-8")

    # (2) nothing-to-do on a second pass (never overwrites).
    out2 = await normalize(wt, "FEAT-TIME")
    assert out2.status == "nothing-to-do" and len(out2.already_stamped) == 4

    # (3) refused — an undecidable scenario stops with the title verbatim,
    #     nothing written (the earlier stamps stand, the new one is absent).
    feat = wt / "features" / "time-endpoint" / "time-endpoint.feature"
    feat.write_text(
        feat.read_text(encoding="utf-8")
        + f"\n  Scenario: {_MOON}\n    Given the moon\n    Then it is cheese\n",
        encoding="utf-8",
    )
    out3 = await normalize(wt, "FEAT-TIME")
    assert out3.status == "refused", out3
    assert out3.refused_titles == (_MOON,)
    assert not out3.titles_recovered_from_console_echo
    assert _MOON not in yaml_path.read_text(encoding="utf-8")

    # (4) cannot run — no such feature file.
    out4 = await normalize(wt, "FEAT-NOPE")
    assert out4.status == "failed" and "feature file not found" in out4.detail
