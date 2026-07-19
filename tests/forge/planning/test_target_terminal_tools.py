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
