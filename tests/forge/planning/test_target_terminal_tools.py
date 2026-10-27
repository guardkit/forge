"""Lane B / Phase E1 (B2) — the target-terminal oracle seams.

Unit-level coverage of :mod:`forge.planning.target_terminal_tools`: the
normalizer subprocess wrapper and the ``guardkit feature validate`` seam, both
exercised through their stubbable injection points (no real subprocess, no real
guardkit binary).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from forge.planning import target_terminal_tools as ttt
from forge.planning.target_terminal_tools import (
    NORMALIZER_MODULE_CANDIDATES,
    DividerRepairResult,
    comment_box_drawing_dividers,
    NormalizerModuleUnresolved,
    ToolOutcome,
    discover_ts_shape_test_roots,
    shallow_discover_test_roots,
    make_normalize_feature_spec,
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
# qa validate gate-registry (F2)
# ---------------------------------------------------------------------------


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
# Descriptor test-root discovery (B4 run 36629c5a, round 10): the per-suite
# ``tests/<name>`` roots (tests/health, tests/users for api_test — never a bare
# "tests"). Forge's own discovery; it used to import GuardKit's from its
# smoke-gate module, which went in GuardKit's cull (27 October 2026).
# ---------------------------------------------------------------------------



def test_discovery_against_an_api_test_shaped_fixture(tmp_path: Path) -> None:
    """An api_test-shaped tree gives the EXACT per-suite roots — not a bare 'tests'."""
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "users").mkdir(parents=True)
    (tmp_path / "tests" / "__pycache__").mkdir()  # skipped by guardkit
    roots = shallow_discover_test_roots(tmp_path)
    assert roots == ["tests/health", "tests/users"]


def test_discovery_empty_when_no_tests_tree(tmp_path: Path) -> None:
    # ASSUM-010: no tests/ tree -> empty roots (the plan may emit no smoke gate).
    assert shallow_discover_test_roots(tmp_path) == []


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
    assert shallow_discover_test_roots(tmp_path) == ["tests"]


def test_the_stage_b_shape_bend_still_works_unchanged(tmp_path: Path) -> None:
    """The bent shape (``tests/health/health.test.ts``) is untouched by the cure.

    Reversibility cuts both ways: the repo may move back to flat, and it may
    also stay bent. The bent tree is a plain Python-shaped subdirectory, so
    the per-suite discovery answers it and the TS pass adds nothing.
    """
    (tmp_path / "tests" / "health").mkdir(parents=True)
    (tmp_path / "tests" / "health" / "health.test.ts").write_text("", encoding="utf-8")
    assert discover_ts_shape_test_roots(tmp_path) == []
    assert shallow_discover_test_roots(tmp_path) == ["tests/health"]


def test_singular_test_dir_flat(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "app.spec.ts").write_text("", encoding="utf-8")
    assert shallow_discover_test_roots(tmp_path) == ["test"]


def test_singular_test_dir_with_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "test" / "health").mkdir(parents=True)
    (tmp_path / "test" / "health" / "health.test.ts").write_text("", encoding="utf-8")
    (tmp_path / "test" / "users").mkdir(parents=True)
    (tmp_path / "test" / "users" / "users.test.ts").write_text("", encoding="utf-8")
    assert shallow_discover_test_roots(tmp_path) == ["test/health", "test/users"]


def test_colocated_dunder_tests_under_src(tmp_path: Path) -> None:
    (tmp_path / "src" / "health" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "health" / "__tests__" / "routes.test.ts").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "src" / "users" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "users" / "__tests__" / "svc.spec.tsx").write_text(
        "", encoding="utf-8"
    )
    assert shallow_discover_test_roots(tmp_path) == [
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
    assert shallow_discover_test_roots(tmp_path) == ["tests/health", "tests/users"]


def test_a_python_repo_with_a_singular_test_dir_is_unchanged(tmp_path: Path) -> None:
    """``test/`` opens ONLY on TypeScript evidence — a Python ``test/`` tree is
    invisible to discovery exactly as it is today."""
    (tmp_path / "test" / "pkg").mkdir(parents=True)
    (tmp_path / "test" / "pkg" / "test_thing.py").write_text("", encoding="utf-8")
    assert discover_ts_shape_test_roots(tmp_path) == []
    assert shallow_discover_test_roots(tmp_path) == []


def test_ts_discovery_never_raises_on_a_missing_tree(tmp_path: Path) -> None:
    assert discover_ts_shape_test_roots(tmp_path / "nope") == []


# --- The per-suite shape, never a bare "tests" --------------------------------


def test_shallow_fallback_reproduces_the_per_suite_python_shape(
    tmp_path: Path,
) -> None:
    """Discovery never answers the round-10 defect shape.

    A bare ``["tests"]`` is a PREFIX of every ``tests/<x>`` path, which is
    exactly what let 008 invent ``tests/smoke`` and pass the in-session
    containment gate. It returns the per-suite roots instead.
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
    roots = shallow_discover_test_roots(_REAL_API_TEST)

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


def test_python_only_dunder_tests_dir_is_not_a_root(tmp_path):
    """The src/**/__tests__ shape is TS-gated: a Python repo's answer stays []."""
    (tmp_path / "src" / "pkg" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__tests__" / "test_x.py").write_text("def test_x(): pass\n")
    assert shallow_discover_test_roots(tmp_path) == []


def test_ts_dunder_tests_dir_is_a_root(tmp_path):
    (tmp_path / "src" / "pkg" / "__tests__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__tests__" / "x.test.ts").write_text("test\n")
    assert "src/pkg/__tests__" in shallow_discover_test_roots(tmp_path)


# ---------------------------------------------------------------------------
# The feature_files fill: a plan that declares no feature_files is given the
# committed spec's .feature. (The stamp normalizer these tests sat beside went
# in the cull, 27 October 2026.)
# ---------------------------------------------------------------------------


from forge.planning.target_terminal_tools import (  # noqa: E402
    FeatureFilesFill,
    declare_feature_files_if_absent,
)


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
        "id: FEAT-A\nfeature_files:\n  - features/x/x.feature\ntasks: []\n",
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


def test_fill_still_fires_on_a_malformed_plan_with_no_feature_files_key(
    tmp_path: Path,
) -> None:
    """The fill is gated on the top-level KEY being absent, and that gate reads
    text, not a parse — so a plan that is broken elsewhere and never declared
    the key is still given its explicit universe (unchanged behaviour, pinned
    so the parsing added here does not quietly narrow the fill)."""
    _write_plan_yaml(tmp_path, "FEAT-X", "id: FEAT-X\ntasks:\n  - id: T1\n   broken: [\n")
    fill = declare_feature_files_if_absent(
        tmp_path, "FEAT-X", ["features/user-search/user-search.feature"]
    )
    assert fill.fired is True
    assert fill.feature_files == ("features/user-search/user-search.feature",)
