"""Deterministic oracle seams for the Lane B / Phase E1 target terminal (B2).

The target-terminal spec/plan legs (:mod:`forge.planning.driver`) write the
machine-made artifacts to the branch and then run two deterministic oracles
against them, exactly as the Factory-2 coordinator did by hand (record hops
6-7): the gherkin **normalizer** over the spec ``.feature`` file and
``guardkit feature validate`` over the plan tree. This module is the forge-side
home of those two oracle calls.

Design
------

* **Injected, stubbable** — the driver receives the two oracles as async
  callables on its deps (like ``dispatch_product_owner``), so the hermetic
  round-trip stubs them on the test bus and never shells out. Production wires
  the :func:`make_normalize_feature_spec` / :func:`make_validate_feature_plan`
  closures below.
* **Bounded (M12)** — every subprocess is wrapped in a finite
  ``asyncio.wait_for``; there is no new unbounded wait. ``feature validate``
  rides the frozen :func:`forge.adapters.guardkit.run.run` seam (600 s default
  cap); the normalizer wraps its own bounded subprocess seam.
* **Frozen bytes untouchable** — forge never edits the guardkit templates or
  the ``feature validate`` schema; it only *invokes* them. The normalizer is
  the same ``feature_spec_normalize`` collapse-then-parse backstop guardkit
  ships (``installer/core/commands/lib/feature_spec_normalize.py``); the exact
  interpreter/module path binds at Lane A / B4 and is a config knob here.

Since 2026-08-16 a third seam sits between the two: THE STAMP NORMALIZER
(``guardkit qa normalize-stamps``, Rich's condition 1) runs against the plan
tree immediately BEFORE ``feature validate`` so the rule-minted ``verifier:``
stamps are WRITTEN on the planning branch and ride the plan commit — see
:func:`make_normalize_stamps` / :func:`classify_normalizer_result`.

References: post-factory-2-three-lanes-handoff §3 B2; factory-2 record hops 6-7;
sovereign-planning-loop-scope §4 (guardkit ``feature validate`` = the oracle);
ai-transition/docs/routing-law-stamp-normalizer-rules-2026-08-15.md (R1–R10).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import importlib.util
import logging
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.adapters.guardkit.run import run as guardkit_run

logger = logging.getLogger(__name__)

__all__ = [
    "NORMALIZER_MODULE_CANDIDATES",
    "TEST_ROOT_DISCOVERY_MODULE_CANDIDATES",
    "NormalizeFeatureSpecFn",
    "NormalizeStampsFn",
    "NormalizerModuleUnresolved",
    "StampNormalizerOutcome",
    "FeatureFilesFill",
    "classify_normalizer_result",
    "declare_feature_files_if_absent",
    "make_normalize_stamps",
    "parse_normalizer_payload",
    "TargetTestRootsUnresolved",
    "ToolOutcome",
    "ValidateFeaturePlanFn",
    "ValidateGateRegistryFn",
    "ValidatePassBarFn",
    "DividerRepairResult",
    "comment_box_drawing_dividers",
    "FrontmatterIdRepair",
    "FrontmatterRepairResult",
    "repair_frontmatter_feature_id",
    "repair_plan_task_frontmatter",
    "TS_TEST_FILE_SUFFIXES",
    "discover_target_test_roots",
    "discover_ts_shape_test_roots",
    "shallow_discover_test_roots",
    "make_normalize_feature_spec",
    "make_validate_feature_plan",
    "make_validate_gate_registry",
    "make_validate_pass_bar",
    "resolve_normalizer_command",
]

#: Default per-oracle wall-clock budget. Bounded (M12) — a stuck oracle fails
#: the leg loudly rather than hanging the planning run forever.
_DEFAULT_ORACLE_TIMEOUT_SECONDS: int = 600

#: P-5 observability cap for the persisted validate-refusal error. The oracle's
#: REAL refusing lines land at the END of its stdout (the ``--json`` report),
#: behind an INFO/WARNING stderr preamble, so we keep the TAIL and mark a
#: ``[truncated head]`` when the combined stream exceeds this bound.
_VALIDATE_ERROR_CAP_BYTES: int = 8192

#: The interpreter used to launch the ``python -m …`` normalizer. Inside the
#: forge image this resolves (via PATH) to the same ``/opt/venv`` interpreter the
#: forge daemon itself runs under, so an in-process :func:`importlib.util.find_spec`
#: probe is an accurate predictor of what ``python -m`` will be able to import.
_DEFAULT_PYTHON_EXECUTABLE: str = "python"

#: The two module paths ``feature_spec_normalize`` is importable at, in
#: resolution priority order. This mirrors the specialist's dual-candidate
#: template-loader: the guardkit distribution exposes the normalizer at DIFFERENT
#: paths depending on how it was installed, and forge must be robust to BOTH.
#:
#: * ``guardkit._installer_core.commands.lib.feature_spec_normalize`` — a plain
#:   ``pip install`` (DF-011 wheel). Hatch ``force-include`` maps the authoring
#:   source ``installer/core`` under the guardkit namespace as
#:   ``guardkit/_installer_core`` (never a top-level ``installer`` distribution,
#:   which would collide with PyPI ``pypa/installer``). This is the form the
#:   forge production image installs, so it is tried FIRST.
#: * ``installer.core.commands.lib.feature_spec_normalize`` — a source / editable
#:   checkout, where the repo root carries an importable top-level ``installer``
#:   package. This is the dev-host form.
#:
#: LIVE INCIDENT (B4 run 4b3b0893, round 5): the forge image shipped no guardkit,
#: so the hard-coded ``installer.core.…`` invocation failed in-container with
#: ``ModuleNotFoundError: No module named 'installer'`` AFTER the reply had already
#: been projected and the branch written. The image now installs guardkit and the
#: default production wiring resolves whichever of these two paths is importable.
NORMALIZER_MODULE_CANDIDATES: tuple[str, ...] = (
    "guardkit._installer_core.commands.lib.feature_spec_normalize",
    "installer.core.commands.lib.feature_spec_normalize",
)

#: Default normalizer invocation for the DEV / source-checkout form. Production
#: wiring passes ``command_prefix=None`` to request dual-candidate resolution via
#: :func:`resolve_normalizer_command` instead (robust to a wheel install too).
#: ``feature_spec_normalize`` is a guardkit *module* (``python -m …``), not a
#: ``guardkit`` subcommand.
_DEFAULT_NORMALIZER_COMMAND: tuple[str, ...] = (
    _DEFAULT_PYTHON_EXECUTABLE,
    "-m",
    NORMALIZER_MODULE_CANDIDATES[1],
)


class NormalizerModuleUnresolved(RuntimeError):
    """Neither normalizer module candidate is importable in this interpreter.

    Raised by :func:`resolve_normalizer_command` when the guardkit distribution
    is absent from the image entirely (the B4 run 4b3b0893 failure mode). The
    message names BOTH candidates and the fix, mirroring the loud-error grammar
    the loaders use elsewhere.
    """


def resolve_normalizer_command(
    *,
    python_executable: str = _DEFAULT_PYTHON_EXECUTABLE,
    module_candidates: Sequence[str] = NORMALIZER_MODULE_CANDIDATES,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> tuple[str, ...]:
    """Resolve the ``python -m <module>`` prefix for the normalizer subprocess.

    Probes :data:`NORMALIZER_MODULE_CANDIDATES` in priority order using an
    in-process :func:`importlib.util.find_spec` and returns
    ``(python, "-m", <first importable candidate>)``. Because the forge daemon
    and the ``python -m`` subprocess share the same ``/opt/venv`` interpreter,
    an importable spec here is an accurate predictor that ``python -m`` will
    resolve the module in-container.

    Raises :class:`NormalizerModuleUnresolved` — naming BOTH candidates — when
    neither resolves (a guardkit-less image; the seam that shipped the live B4
    failure).
    """
    for candidate in module_candidates:
        try:
            spec = find_spec(candidate)
        except (ImportError, ModuleNotFoundError, ValueError):
            # ``find_spec`` raises rather than returns None when a *parent*
            # package is missing (e.g. no top-level ``installer`` at all).
            # Treat that exactly like "not importable" and try the next.
            spec = None
        if spec is not None:
            return (python_executable, "-m", candidate)
    raise NormalizerModuleUnresolved(
        "target-terminal normalizer module could not be resolved: none of the "
        f"candidates import in this interpreter — {list(module_candidates)}. "
        "The forge image must install guardkit (pip install the DF-011 wheel, "
        "which exposes guardkit._installer_core.*) or provide a source checkout "
        "with an importable top-level installer package. See scripts/build-image.sh "
        "and scripts/verify-forge-oracles.sh."
    )


# ---------------------------------------------------------------------------
# Descriptor test-root discovery — REUSE guardkit's OWN function (B4 run
# 36629c5a, round 10)
#
# The 008 ``target_repo_descriptor.test_roots`` must be the EXACT set the
# downstream ``guardkit feature validate`` pre-commit oracle enforces, never a
# shallow re-guess. guardkit builds both the smoke-gate ``available_roots`` and
# the "Available test roots: …" validate error from a single function —
# ``installer/core/commands/lib/smoke_gates_nudge.py:42`` ``discover_test_roots``
# (called at ``guardkit/orchestrator/feature_loader.py:931`` and :1139). We
# import and call THAT function so forge tells 008 the truth the oracle holds it
# to.
#
# LIVE INCIDENT this fixes: forge's old shallow builder discovered checkout-root
# ``tests/`` dirs only -> ``['tests']``; the 008 model then invented
# ``tests/smoke`` (a PREFIX of ``tests``), which the specialist's in-session
# ``smoke_gate_containment`` gate PASSED (keyed on ``descriptor.test_roots``,
# ``path.startswith(root + '/')`` — feature_plan_oracle.py:915) — but the real
# ``feature validate`` knows the repo's roots are ``tests/health, tests/users``
# (no ``tests/smoke``) and refused the plan at the pre-commit oracle. Handing the
# EXACT roots makes prefix-containment == membership for these shapes, so the
# in-session gate catches the invention where the revision loop can correct it.
# ---------------------------------------------------------------------------

#: The two module paths guardkit's ``discover_test_roots`` is importable at, in
#: resolution priority order — the SAME dual-candidate shape as
#: :data:`NORMALIZER_MODULE_CANDIDATES`:
#:
#: * ``guardkit._installer_core.commands.lib.smoke_gates_nudge`` — the wheel/pip
#:   form (DF-011 wheel, hatch ``force-include`` of ``installer/core`` under the
#:   guardkit namespace). This is what the forge production image installs, so it
#:   is tried FIRST.
#: * ``installer.core.commands.lib.smoke_gates_nudge`` — the source/editable
#:   checkout form (a repo root carrying an importable top-level ``installer``
#:   package). The dev-host form.
TEST_ROOT_DISCOVERY_MODULE_CANDIDATES: tuple[str, ...] = (
    "guardkit._installer_core.commands.lib.smoke_gates_nudge",
    "installer.core.commands.lib.smoke_gates_nudge",
)


#: Filename suffixes that mark a file as a TypeScript/JavaScript test. These are
#: the vitest/jest/mocha conventions, not a guess: ``*.test.ts`` / ``*.spec.ts``
#: and their tsx/js/mjs siblings. Used ONLY as *evidence* that a directory is a
#: TS test tree — never to build a command.
TS_TEST_FILE_SUFFIXES: tuple[str, ...] = (
    ".test.ts",
    ".test.tsx",
    ".test.mts",
    ".test.js",
    ".test.jsx",
    ".test.mjs",
    ".spec.ts",
    ".spec.tsx",
    ".spec.mts",
    ".spec.js",
    ".spec.jsx",
    ".spec.mjs",
)

#: Directory names never walked when looking for ``src/**/__tests__`` — build
#: output and dependency trees. ``node_modules`` first and foremost: without it
#: a single walk of a real TypeScript app would stat tens of thousands of paths.
_TS_WALK_SKIP_NAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        "out",
        "coverage",
        ".next",
        ".turbo",
        ".svelte-kit",
        "__pycache__",
    }
)

#: How deep under ``src/`` a ``__tests__`` directory is looked for. Six levels
#: covers every layout seen in practice and keeps the walk bounded.
_TS_DUNDER_TESTS_MAX_DEPTH: int = 6

#: Names never returned as a test root even when present as a subdirectory.
#: Mirrors guardkit's own ``_TEST_ROOT_SKIP_NAMES`` so the two discoveries agree
#: on what a root is NOT.
_TEST_ROOT_SKIP_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", "node_modules"}
)


def _is_ts_test_file(name: str) -> bool:
    """True iff ``name`` ends with one of :data:`TS_TEST_FILE_SUFFIXES`."""
    return any(name.endswith(suffix) for suffix in TS_TEST_FILE_SUFFIXES)


def _has_flat_ts_tests(directory: Path) -> bool:
    """True iff ``directory`` holds a TS test file as an IMMEDIATE child."""
    try:
        return any(
            child.is_file() and _is_ts_test_file(child.name)
            for child in directory.iterdir()
        )
    except OSError:
        return False


def _contains_ts_tests(directory: Path, *, max_depth: int = 2) -> bool:
    """True iff a TS test file exists at or below ``directory``, depth-bounded.

    Used as the EVIDENCE gate for the singular ``test/`` directory: a Python
    repo that happens to carry ``test/`` must keep returning exactly what it
    returns today, so that branch only opens when the tree actually holds
    TypeScript tests.
    """
    if max_depth < 0:
        return False
    try:
        children = list(directory.iterdir())
    except OSError:
        return False
    for child in children:
        try:
            if child.is_file():
                if _is_ts_test_file(child.name):
                    return True
                continue
            if not child.is_dir():
                continue
        except OSError:
            continue
        if child.name in _TS_WALK_SKIP_NAMES or child.name.startswith("."):
            continue
        if _contains_ts_tests(child, max_depth=max_depth - 1):
            return True
    return False


def _eligible_subdir_names(directory: Path) -> list[str]:
    """Immediate subdirectory names of ``directory``, cache/vendor dirs removed.

    Byte-compatible with guardkit's own filter (dot-prefixed names and
    :data:`_TEST_ROOT_SKIP_NAMES` are dropped) so the Python shape keeps
    producing exactly what it produces today.
    """
    try:
        names = [
            child.name
            for child in directory.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name not in _TEST_ROOT_SKIP_NAMES
        ]
    except OSError:
        return []
    return sorted(names)


def _discover_dunder_tests_roots(src_dir: Path, repo_root: Path) -> list[str]:
    """Return every non-empty ``src/**/__tests__`` directory, repo-relative.

    The colocated-tests convention (jest/vitest). Bounded by
    :data:`_TS_DUNDER_TESTS_MAX_DEPTH` and skipping
    :data:`_TS_WALK_SKIP_NAMES`, so a repo with an installed ``node_modules``
    is not walked into.
    """
    found: list[str] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > _TS_DUNDER_TESTS_MAX_DEPTH:
            return
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if child.name in _TS_WALK_SKIP_NAMES or child.name.startswith("."):
                continue
            if child.name == "__tests__":
                try:
                    non_empty = any(
                        entry.is_file() and _is_ts_test_file(entry.name)
                        for entry in child.iterdir()
                    )
                except OSError:
                    non_empty = False
                if non_empty:
                    found.append(child.relative_to(repo_root).as_posix())
                # A __tests__ dir is a leaf root; do not descend further.
                continue
            _walk(child, depth + 1)

    _walk(src_dir, 1)
    return found


def discover_ts_shape_test_roots(repo_path: Path | str) -> list[str]:
    """Return the TypeScript-shaped test roots of ``repo_path`` (sorted).

    THE REAL TEST-ROOT CURE (TypeScript-lane design §D.3(ii)). guardkit's
    ``discover_test_roots`` answers one shape only — the immediate
    SUBDIRECTORIES of ``tests/`` — which is the Python packaging convention.
    A TypeScript repo scaffolded the normal way has none of those: vitest's
    default layout is a FLAT ``tests/health.test.ts``, so discovery returned
    ``[]``, the 008 descriptor emitted ``test_roots: []``, and ASSUM-010 then
    turned any ``smoke_gates`` block into a plan-containment error. The repo was
    bent to fit the tool (``tests/health/health.test.ts``) as a dated, reversible
    stopgap. This function is the cure that makes that bend reversible.

    Three shapes are taught, each purely ADDITIVE and each gated on TypeScript
    EVIDENCE so a Python repo's answer is byte-unchanged:

    1. **flat ``tests/``** — ``tests`` itself is a root when it holds
       ``*.test.ts`` / ``*.spec.ts`` (and tsx/js/mjs siblings) as immediate
       children. This is ts-api-test's original shape.
    2. **singular ``test/``** — its eligible subdirectories become
       ``test/<name>``, plus ``test`` itself when it holds flat TS test files.
       The whole branch only opens when a TS test file exists at or below
       ``test/``, so a Python repo carrying a ``test/`` directory still gets
       nothing from it.
    3. **``src/**/__tests__/``** — the colocated convention; every non-empty
       ``__tests__`` directory under ``src/`` is a root, bounded in depth and
       never walking into ``node_modules``/build output.

    The honest cost of shape 1, stated plainly: ``tests`` is a root whose name
    is a PREFIX of every ``tests/<x>`` path, which is exactly the geometry that
    let the round-10 incident's in-session containment gate pass an invented
    ``tests/smoke``. For a flat TS repo there is no more precise truth to tell —
    ``tests`` really is the only root — so the containment gate is weaker on
    that shape by nature, not by accident.

    Pure and side-effect-free; every ``OSError`` degrades to "found nothing".
    Never raises.
    """
    root = Path(repo_path)
    roots: list[str] = []

    tests_dir = root / "tests"
    if tests_dir.is_dir() and _has_flat_ts_tests(tests_dir):
        roots.append("tests")

    test_dir = root / "test"
    if test_dir.is_dir() and _contains_ts_tests(test_dir):
        roots.extend(f"test/{name}" for name in _eligible_subdir_names(test_dir))
        if _has_flat_ts_tests(test_dir):
            roots.append("test")

    src_dir = root / "src"
    if src_dir.is_dir():
        roots.extend(_discover_dunder_tests_roots(src_dir, root))

    return sorted(set(roots))


def shallow_discover_test_roots(repo_path: Path | str) -> list[str]:
    """Degraded, guardkit-free test-root discovery (both shapes).

    Used ONLY when guardkit's own ``discover_test_roots`` cannot be imported —
    an interpreter where the real ``feature validate`` oracle cannot run either.
    Reproduces guardkit's Python shape (immediate subdirectories of ``tests/``)
    instead of the historical bare ``["tests"]`` guess, and adds the TypeScript
    shapes from :func:`discover_ts_shape_test_roots`, so the degraded answer has
    the same geometry as the healthy one.
    """
    root = Path(repo_path)
    roots: list[str] = []
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        roots.extend(f"tests/{name}" for name in _eligible_subdir_names(tests_dir))
    roots.extend(discover_ts_shape_test_roots(root))
    return sorted(set(roots))


class TargetTestRootsUnresolved(RuntimeError):
    """guardkit's ``discover_test_roots`` is not importable in this interpreter.

    Raised by :func:`discover_target_test_roots` when the guardkit distribution
    is absent from the image entirely (the same failure mode
    :class:`NormalizerModuleUnresolved` names for the normalizer). The message
    names BOTH candidates and the build fix; the descriptor builder catches it
    and degrades to a shallow discovery so a guardkit-less env — where the real
    ``feature validate`` oracle cannot run either — still builds a descriptor
    rather than crashing the drive.
    """


def discover_target_test_roots(
    repo_path: Path | str,
    *,
    module_candidates: Sequence[str] = TEST_ROOT_DISCOVERY_MODULE_CANDIDATES,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> list[str]:
    """Return the target checkout's ``tests/<name>`` roots via guardkit's OWN discovery.

    Imports guardkit's ``discover_test_roots`` — dual-candidate, wheel form
    (``guardkit._installer_core.*``) first, source-checkout form
    (``installer.core.*``) second — and calls it in-process against
    ``repo_path``. The result is the byte-identical ``tests/<name>`` set
    ``guardkit feature validate`` reports as its "Available test roots"
    (``installer/core/commands/lib/smoke_gates_nudge.py:42``), so the 008
    descriptor carries exactly what the pre-commit oracle will enforce.

    THE TYPESCRIPT SHAPES RIDE ON TOP (design §D.3(ii)). guardkit's function
    answers the Python shape only. Its answer is kept verbatim and UNIONed with
    :func:`discover_ts_shape_test_roots`, which is purely additive and gated on
    TypeScript evidence — so a Python checkout's roots are byte-unchanged, and a
    TypeScript checkout stops answering ``[]``.

    This does NOT put forge out of step with the downstream oracle. guardkit's
    ``feature validate`` decides a smoke-gate path by EXISTENCE
    (``feature_loader.py:927`` / ``:1135`` — ``not (repo_root / p).exists()``);
    ``discover_test_roots`` only renders the "Available test roots" line of the
    resulting error message. A root this function adds is a directory that
    exists, so a plan built on it passes the oracle. The guardkit-side twin —
    teaching the same shapes to ``discover_test_roots`` so the ERROR MESSAGE
    lists them too — is a guardkit-venue change and is reported, not made here.

    Raises :class:`TargetTestRootsUnresolved` — naming BOTH candidates — when
    neither module imports (a guardkit-less interpreter).
    """
    last_exc: Exception | None = None
    for candidate in module_candidates:
        try:
            module = import_module(candidate)
        except (ImportError, ModuleNotFoundError, ValueError) as exc:
            # ``import_module`` raises rather than returns None when a parent
            # package is missing (e.g. no top-level ``installer`` at all).
            # Treat that as "not importable" and try the next candidate.
            last_exc = exc
            continue
        # guardkit's discover_test_roots(repo_root) -> sorted list[str] of
        # ``tests/<name>`` paths; [] when there is no tests/ tree.
        python_roots = list(module.discover_test_roots(Path(repo_path)))
        ts_roots = [
            root
            for root in discover_ts_shape_test_roots(repo_path)
            if root not in python_roots
        ]
        if not ts_roots:
            # Python-shape repo: byte-identical to what guardkit returned.
            return python_roots
        return sorted(python_roots + ts_roots)
    raise TargetTestRootsUnresolved(
        "target-terminal test-root discovery could not import guardkit's "
        "discover_test_roots: none of the candidates import in this "
        f"interpreter — {list(module_candidates)}. The forge image must "
        "install guardkit (pip install the DF-011 wheel, which exposes "
        "guardkit._installer_core.*) or provide a source checkout with an "
        "importable top-level installer package. See scripts/build-image.sh."
    ) from last_exc


@dataclass(frozen=True)
class ToolOutcome:
    """Result of running one target-terminal oracle.

    ``ok`` is True only when the oracle passed (exit 0, not timed out). ``detail``
    carries the loud-failure reason surfaced to the terminal FAILED state and
    the jarvis notification on a red oracle.
    """

    ok: bool
    detail: str = ""


#: ``async (worktree_path, feature_rel_path) -> ToolOutcome`` — normalize the
#: spec ``.feature`` at ``feature_rel_path`` (relative to the worktree).
NormalizeFeatureSpecFn = Callable[[Path, str], Awaitable[ToolOutcome]]

#: ``async (worktree_path, feature_id) -> ToolOutcome`` — run
#: ``guardkit feature validate <feature_id>`` against the worktree.
ValidateFeaturePlanFn = Callable[[Path, str], Awaitable[ToolOutcome]]

#: ``async (worktree_path, feature_id) -> StampNormalizerOutcome`` — run THE
#: STAMP NORMALIZER (``guardkit qa normalize-stamps --feature <id> --repo
#: <worktree>``) against the planning worktree, immediately BEFORE the
#: plan-commit ``feature validate`` (Rich's condition 1, 2026-08-16: the
#: ``verifier:`` stamps are WRITTEN on the planning branch before validate).
NormalizeStampsFn = Callable[[Path, str], Awaitable["StampNormalizerOutcome"]]

#: ``async (worktree_path, bar_rel_path) -> ToolOutcome`` — run
#: ``guardkit qa validate pass-bar <bar_rel_path>`` against the worktree. The
#: forge-minted per-task QA pass bar (registered from the 007 seed at
#: plan-commit) must pass guardkit's OWN F1 schema before it is committed.
ValidatePassBarFn = Callable[[Path, str], Awaitable[ToolOutcome]]

#: ``async (worktree_path, registry_rel_path) -> ToolOutcome`` — run
#: ``guardkit qa validate gate-registry <registry_rel_path>`` against the
#: worktree. The forge-appended per-feature GateEntry (registered from the 007
#: seed's derivable GET endpoint at plan-commit) must pass guardkit's OWN
#: gate-registry schema before the filled gate + registry edit are committed.
ValidateGateRegistryFn = Callable[[Path, str], Awaitable[ToolOutcome]]


async def _default_normalizer_subprocess(
    *, command: Sequence[str], cwd: str, timeout: int
) -> tuple[str, str, int, bool]:
    """Bounded subprocess seam for the normalizer (stubbable in tests).

    Returns ``(stdout, stderr, exit_code, timed_out)``. Never raises for a
    non-zero exit — the caller maps exit codes to :class:`ToolOutcome`.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()
        timed_out = True
    exit_code = proc.returncode if proc.returncode is not None else -1
    return (
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
        exit_code,
        timed_out,
    )


# ---------------------------------------------------------------------------
# Box-drawing "divider" repair (P-7 — defect-#14/#15 deterministic-repair
# pattern).
#
# LIVE INCIDENT this fixes: the HB-4 pilot's 007 feature-spec legs twice emitted
# decorative divider lines at top level between scenarios inside the generated
# ``.feature`` file, e.g.::
#
#     ━━ GROUP A: Key Examples (3 scenarios) ━━
#     ━━ GROUP B: Boundary Conditions (4 scenarios) ━━
#
# gherkin-official rightly refuses these (a top-level line that is neither a
# keyword, comment, tag, nor docstring content), the pre-commit normalizer exits
# 1, and the whole run dies with NO revision loop.
#
# THE REPAIR — conservative by law. ONLY a line whose first non-whitespace
# character is a Unicode box-drawing character (block U+2500–U+257F: ━ ─ ═ │ …)
# and that sits OUTSIDE any gherkin docstring block is rewritten by prefixing
# ``# `` (turning it into a legal comment — information-preserving, never
# deleted). Every other malformed line (a step that lost its keyword, prose that
# is not a divider, …) is left exactly as-is for the parser to refuse, exactly as
# today. Docstring interiors are legal free text and are left byte-untouched
# (a divider *inside* a docstring is content, not a top-level line): the scan
# tracks docstring context the same way guardkit's own ``feature_spec_normalize``
# collapse pass does, so a box-drawing line inside ``"""``/``'''``/```` ``` ```` is
# never touched. This is the simplest provably-safe rule for requirement (d).
# ---------------------------------------------------------------------------

#: A "divider" line: first non-whitespace char is in the box-drawing block.
_BOX_DRAWING_DIVIDER_RE = re.compile(r"^\s*[─-╿]")

#: Gherkin docstring delimiters (``"""`` / ``'''`` / triple-backtick). Mirrors
#: guardkit ``feature_spec_normalize`` (which tracks ``"""``/``'''``); the
#: triple-backtick form is included for completeness.
_DOCSTRING_DELIMITER_RE = re.compile(r"^\s*(\"\"\"|'''|```)")


@dataclass
class DividerRepairResult:
    """Outcome of :func:`comment_box_drawing_dividers`.

    ``text`` is the (possibly) rewritten feature text; ``commented_lines`` is the
    1-based line numbers of the divider lines that were commented out (empty when
    the repair did not fire — in which case ``text`` is the input byte-for-byte).
    """

    text: str
    commented_lines: list[int]

    @property
    def fired(self) -> bool:
        return bool(self.commented_lines)


def comment_box_drawing_dividers(text: str) -> DividerRepairResult:
    """Comment out top-level box-drawing divider lines (P-7 repair).

    Pure and deterministic. Scans ``text`` line by line, tracking gherkin
    docstring context, and prefixes ``# `` to every line that (a) is OUTSIDE a
    docstring and (b) has a box-drawing character (U+2500–U+257F) as its first
    non-whitespace character. All other bytes — including divider-looking lines
    *inside* a docstring, and any non-box-drawing malformed line — are preserved
    exactly. Idempotent: a line already commented no longer starts with a
    box-drawing char, so a second pass is a no-op.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    commented: list[int] = []
    in_docstring = False
    open_delim: str | None = None
    for idx, line in enumerate(lines, start=1):
        delim_match = _DOCSTRING_DELIMITER_RE.match(line)
        if in_docstring:
            # Inside a docstring: NEVER touch content. Only a matching closing
            # delimiter flips us back out.
            if delim_match and delim_match.group(1) == open_delim:
                in_docstring = False
                open_delim = None
            out.append(line)
            continue
        if delim_match:
            # Opening a docstring — the delimiter line itself is not a divider.
            in_docstring = True
            open_delim = delim_match.group(1)
            out.append(line)
            continue
        if _BOX_DRAWING_DIVIDER_RE.match(line):
            out.append("# " + line)
            commented.append(idx)
            continue
        out.append(line)
    return DividerRepairResult(text="".join(out), commented_lines=commented)


def make_normalize_feature_spec(
    *,
    command_prefix: Sequence[str] | None = _DEFAULT_NORMALIZER_COMMAND,
    timeout_seconds: int = _DEFAULT_ORACLE_TIMEOUT_SECONDS,
    subprocess_seam: Callable[..., Awaitable[tuple[str, str, int, bool]]]
    | None = None,
    python_executable: str = _DEFAULT_PYTHON_EXECUTABLE,
) -> NormalizeFeatureSpecFn:
    """Build the production normalizer oracle (spec-leg pre-commit hook).

    The returned callable rewrites the ``.feature`` file in place (collapsing
    any wrapped gherkin steps) and validates it parses — exit 0 means the
    committed spec is parseable by the downstream ``/feature-plan`` linker.

    ``command_prefix`` controls how the ``python -m …`` module path is chosen:

    * an explicit sequence — used verbatim (the dev default + the injection
      point unit tests drive through the stub seam);
    * ``None`` — **dual-candidate resolution** via
      :func:`resolve_normalizer_command`, robust to BOTH the wheel/pip layout
      (``guardkit._installer_core.*``) and the source-checkout layout
      (``installer.core.*``). This is what production wiring passes. Resolution
      is lazy (first invocation) and cached; if neither candidate resolves the
      oracle returns a loud red :class:`ToolOutcome` naming both — contained per
      the oracle-boundary doctrine, never a crashed run.
    """
    seam = subprocess_seam or _default_normalizer_subprocess
    resolved_prefix: list[str] | None = (
        list(command_prefix) if command_prefix is not None else None
    )

    async def _normalize(worktree_path: Path, feature_rel_path: str) -> ToolOutcome:
        nonlocal resolved_prefix
        target = (worktree_path / feature_rel_path).resolve()
        # P-7 pre-parse repair: comment out any top-level box-drawing divider
        # lines BEFORE the gherkin parse, so the run gets a parseable spec instead
        # of dying with no revision loop. Conservative (only that char class),
        # information-preserving (comments, never deletes), and LOUD (logs the
        # count + line numbers when it fires). Contained per the oracle-boundary
        # doctrine — a missing/unreadable file is left for the parse to report,
        # never crashes the leg. If the file STILL fails to parse afterwards, the
        # exit/refuse path below is unchanged.
        repair_note = _repair_box_drawing_dividers(target, feature_rel_path)
        if resolved_prefix is None:
            try:
                resolved_prefix = list(
                    resolve_normalizer_command(python_executable=python_executable)
                )
            except NormalizerModuleUnresolved as exc:
                logger.error("normalize_feature_spec: %s", exc)
                return ToolOutcome(ok=False, detail=str(exc))
        command = [*resolved_prefix, str(target)]
        started = time.monotonic()
        try:
            stdout, stderr, exit_code, timed_out = await seam(
                command=command, cwd=str(worktree_path), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary, never crash the run
            logger.exception("normalize_feature_spec subprocess raised")
            return ToolOutcome(
                ok=False, detail=f"normalizer raised {type(exc).__name__}: {exc}"
            )
        duration = time.monotonic() - started
        if timed_out:
            return ToolOutcome(
                ok=False,
                detail=f"normalizer timed out after {timeout_seconds}s ({feature_rel_path})",
            )
        if exit_code != 0:
            return ToolOutcome(
                ok=False,
                detail=(
                    f"normalizer exit {exit_code} for {feature_rel_path}: "
                    f"{(stderr or stdout).strip()[:500]}"
                ),
            )
        logger.info(
            "normalize_feature_spec: %s parseable (%.2fs)", feature_rel_path, duration
        )
        return ToolOutcome(ok=True, detail=repair_note or "")

    return _normalize


def _repair_box_drawing_dividers(target: Path, feature_rel_path: str) -> str | None:
    """Apply the P-7 divider repair to ``target`` in place, if it fires.

    Reads the feature file, runs :func:`comment_box_drawing_dividers`, and only
    rewrites the file when at least one divider was commented (a clean file is
    left byte-untouched). Returns a LOUD one-line receipt (naming the count and
    the commented 1-based line numbers) when the repair fired, else ``None``.

    Contained per the oracle-boundary doctrine: a missing/unreadable/undecodable
    file is a no-op (returns ``None``) and is left for the downstream parse to
    report — this never crashes the planning leg.
    """
    try:
        original = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError,
            UnicodeDecodeError):
        return None
    result = comment_box_drawing_dividers(original)
    if not result.fired:
        return None
    try:
        target.write_text(result.text, encoding="utf-8")
    except OSError:
        # Could not persist the repair — leave the original bytes for the parse
        # to refuse, exactly as before the repair existed.
        return None
    note = (
        f"P-7 divider repair: commented {len(result.commented_lines)} top-level "
        f"box-drawing line(s) at {result.commented_lines} in {feature_rel_path}"
    )
    logger.warning("normalize_feature_spec: %s", note)
    return note


# ---------------------------------------------------------------------------
# Task-doc frontmatter feature_id repair (P-8 — defect-#14/#15 deterministic-
# repair pattern; sibling of the P-7 divider repair above).
#
# LIVE INCIDENT this fixes: the HB-4 pilot's 008 feature-plan legs
# DETERMINISTICALLY truncated the LAST character of the feature id when writing
# the ``feature_id`` field of a task doc's YAML frontmatter — run 4 wrote
# ``feature_id: FEAT-048`` for FEAT-0482 (1 of N docs); run 7 wrote
# ``feature_id: FEAT-07F`` for FEAT-07F3 (ALL 5 docs). The in-session oracle
# does NOT check per-doc frontmatter parity, so the drift survives to the
# pre-commit ``guardkit feature validate`` oracle, which exits 1 and kills the
# run with NO bounded revision loop. Frontmatter is the last unrepaired
# truncation site (tree/path truncation already has the defect-#14/#15 repairs).
#
# THE REPAIR — conservative by law. For every task doc the plan wrote, parse the
# ``feature_id`` in its FIRST YAML frontmatter block; ONLY when that value V is a
# non-empty STRICT PREFIX of the canonical feature id
# (``canonical.startswith(V) and V != canonical``) — i.e. a truncation — is the
# one value rewritten in place to the canonical id (quoting style preserved). A
# NON-prefix mismatch (e.g. ``FEAT-9999`` under FEAT-07F3), an already-correct
# id, an empty value, a doc with no frontmatter / no feature_id, and every other
# frontmatter field are all left byte-for-byte untouched so the oracle refuses
# exactly as today. Idempotent by construction: once V == canonical the strict-
# prefix guard no longer fires.
# ---------------------------------------------------------------------------

#: A ``---`` YAML frontmatter delimiter line (leading/trailing blanks tolerated).
_FRONTMATTER_DELIM_RE = re.compile(r"^[ \t]*---[ \t]*$")

#: A top-level ``feature_id:`` frontmatter line — captures the key+colon prefix
#: (preserved verbatim) and the raw value token that follows.
_FEATURE_ID_LINE_RE = re.compile(r"^(?P<prefix>[ \t]*feature_id[ \t]*:[ \t]*)(?P<value>.*)$")


@dataclass
class FrontmatterIdRepair:
    """One repaired task doc's before -> after frontmatter feature_id.

    ``rel_path`` is the worktree-relative doc path; ``before`` is the truncated
    id found in the frontmatter; ``after`` is the canonical id it was rewritten
    to.
    """

    rel_path: str
    before: str
    after: str


@dataclass
class FrontmatterRepairResult:
    """Outcome of :func:`repair_plan_task_frontmatter` over a plan tree.

    ``repairs`` is the per-doc before->after record (empty when the repair did
    not fire — in which case every scanned doc is byte-untouched on disk).
    """

    repairs: list[FrontmatterIdRepair]

    @property
    def fired(self) -> bool:
        return bool(self.repairs)

    def receipt(self, canonical_feature_id: str) -> str:
        """A LOUD one-line receipt naming every repaired doc + before->after."""
        detail = "; ".join(
            f"{r.rel_path}: {r.before} -> {r.after}" for r in self.repairs
        )
        return (
            f"P-8 frontmatter repair: rewrote {len(self.repairs)} truncated "
            f"task-doc feature_id(s) to canonical {canonical_feature_id} [{detail}]"
        )


def _unquote_yaml_scalar(value: str) -> str:
    """Strip a single pair of matching surrounding YAML quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _requote_yaml_scalar(original_token: str, replacement: str) -> str:
    """Re-emit ``replacement`` with the SAME quoting style as ``original_token``.

    The feature-id truncation only ever shortens a plain scalar, so this
    preserves a ``"…"`` / ``'…'`` wrapper when the original carried one and emits
    a bare scalar otherwise (a feature id needs no quoting).
    """
    if (
        len(original_token) >= 2
        and original_token[0] == original_token[-1]
        and original_token[0] in "\"'"
    ):
        quote = original_token[0]
        return f"{quote}{replacement}{quote}"
    return replacement


def repair_frontmatter_feature_id(
    text: str, canonical_feature_id: str
) -> tuple[str, str | None]:
    """Repair a strict-prefix-truncated ``feature_id`` in one task doc (P-8).

    Pure and deterministic. If ``text`` opens with a ``---`` YAML frontmatter
    block whose ``feature_id`` value V is a NON-EMPTY STRICT PREFIX of
    ``canonical_feature_id`` (``canonical.startswith(V) and V != canonical``),
    that single value is rewritten to the canonical id in place (quoting style
    preserved) and ``(new_text, V)`` is returned. Every other case — no
    frontmatter, no ``feature_id``, an already-correct id, an empty value, or a
    NON-prefix mismatch — returns ``(text, None)`` byte-for-byte unchanged. Only
    the ``feature_id`` field in the FIRST frontmatter block is ever touched.
    """
    if not canonical_feature_id:
        return text, None
    lines = text.splitlines(keepends=True)
    if not lines or _FRONTMATTER_DELIM_RE.match(lines[0].rstrip("\r\n")) is None:
        return text, None
    close_idx: int | None = None
    for i in range(1, len(lines)):
        if _FRONTMATTER_DELIM_RE.match(lines[i].rstrip("\r\n")) is not None:
            close_idx = i
            break
    if close_idx is None:
        return text, None
    for i in range(1, close_idx):
        match = _FEATURE_ID_LINE_RE.match(lines[i].rstrip("\r\n"))
        if match is None:
            continue
        raw_token = match.group("value").strip()
        before = _unquote_yaml_scalar(raw_token)
        # First (and only) feature_id line decides the outcome for this doc.
        if not before or before == canonical_feature_id:
            return text, None
        if not canonical_feature_id.startswith(before):
            # NON-prefix mismatch — DO NOT touch; let the oracle refuse as today.
            return text, None
        newline_suffix = lines[i][len(lines[i].rstrip("\r\n")):]
        lines[i] = (
            match.group("prefix")
            + _requote_yaml_scalar(raw_token, canonical_feature_id)
            + newline_suffix
        )
        return "".join(lines), before
    return text, None


def repair_plan_task_frontmatter(
    worktree_path: Path, canonical_feature_id: str
) -> FrontmatterRepairResult:
    """Repair strict-prefix-truncated frontmatter feature_ids across a plan tree.

    Scans every ``*.md`` task doc under ``worktree_path/tasks`` (the plan tree's
    task files — ``tasks/backlog/**`` per the 008 contract) and applies
    :func:`repair_frontmatter_feature_id` in place. A doc is rewritten ONLY when
    its frontmatter feature_id is a strict-prefix truncation of
    ``canonical_feature_id``; a clean doc is left byte-untouched. Idempotent.

    Contained per the oracle-boundary doctrine: a missing ``tasks`` tree, or an
    unreadable / undecodable / unwritable doc, is a silent no-op left for the
    downstream oracle to report — this never crashes the planning leg.
    """
    repairs: list[FrontmatterIdRepair] = []
    tasks_root = worktree_path / "tasks"
    if not tasks_root.is_dir():
        return FrontmatterRepairResult(repairs=repairs)
    for doc in sorted(tasks_root.rglob("*.md")):
        try:
            original = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_text, before = repair_frontmatter_feature_id(original, canonical_feature_id)
        if before is None:
            continue
        try:
            doc.write_text(new_text, encoding="utf-8")
        except OSError:
            continue
        try:
            rel = str(doc.relative_to(worktree_path))
        except ValueError:  # pragma: no cover — doc is under worktree by construction
            rel = str(doc)
        repairs.append(
            FrontmatterIdRepair(
                rel_path=rel, before=before, after=canonical_feature_id
            )
        )
    return FrontmatterRepairResult(repairs=repairs)


def _combine_validate_error_streams(
    *, stdout_tail: str, stderr: str, cap_bytes: int = _VALIDATE_ERROR_CAP_BYTES
) -> str:
    """Combine the validate subprocess's stderr + stdout tail for the persisted
    error (P-5 observability rider).

    The prior ``(stderr or tail)`` picked ONE stream and then clipped it to 500
    chars — on a real refusal the INFO/WARNING stderr preamble won, so the actual
    refusing ``--json`` lines (on stdout) were lost and diagnosis needed manual
    log archaeology. This captures BOTH, ordered stderr-then-stdout so the
    refusing stdout lines sit at the TAIL, then keeps at most ``cap_bytes`` bytes
    from the END (prefixing ``[truncated head]`` when clipped) so the errors that
    live at the tail always survive. Pure and side-effect-free; changes only what
    is persisted, never the accept/refuse decision.
    """
    parts: list[str] = []
    if stderr.strip():
        parts.append("stderr:\n" + stderr.strip())
    if stdout_tail.strip():
        parts.append("stdout:\n" + stdout_tail.strip())
    combined = "\n".join(parts).strip()
    if not combined:
        return ""
    encoded = combined.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return combined
    tail = encoded[-cap_bytes:].decode("utf-8", errors="ignore")
    return "[truncated head]\n" + tail


def make_validate_feature_plan(
    *,
    read_allowlist: Sequence[Path] | None = None,
    timeout_seconds: int = _DEFAULT_ORACLE_TIMEOUT_SECONDS,
    run_fn: Callable[..., Awaitable[object]] = guardkit_run,
) -> ValidateFeaturePlanFn:
    """Build the production ``guardkit feature validate`` oracle (plan leg).

    Rides the frozen :func:`forge.adapters.guardkit.run.run` seam (untouched
    guardkit bytes). ``ok`` iff the subcommand exits 0 (feature YAML +
    structural integrity valid).
    """

    async def _validate(worktree_path: Path, feature_id: str) -> ToolOutcome:
        allowlist = list(read_allowlist or [worktree_path])
        # P-8 pre-oracle repair: rewrite any strict-prefix-truncated task-doc
        # frontmatter feature_id to the canonical id BEFORE the guardkit validate
        # oracle sees the tree, so a deterministic last-character truncation gets
        # a parity-clean tree instead of dying with no revision loop. Conservative
        # (only strict-prefix truncations of feature_id), information-preserving,
        # LOUD (logs + receipts the per-doc before->after when it fires), and
        # contained (a missing/unreadable doc is a no-op). If the tree STILL fails
        # to validate afterwards (e.g. a NON-prefix mismatch was left untouched),
        # the refuse path below is byte-for-byte unchanged.
        repair = repair_plan_task_frontmatter(worktree_path, feature_id)
        repair_note: str | None = None
        if repair.fired:
            repair_note = repair.receipt(feature_id)
            logger.warning("validate_feature_plan: %s", repair_note)
        try:
            result = await guardkit_run_shim(
                run_fn,
                subcommand="feature",
                args=["validate", feature_id, "--json"],
                repo_path=worktree_path,
                read_allowlist=allowlist,
                timeout_seconds=timeout_seconds,
                with_nats_streaming=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary
            logger.exception("validate_feature_plan invocation raised")
            return ToolOutcome(
                ok=False, detail=f"feature validate raised {type(exc).__name__}: {exc}"
            )
        status = getattr(result, "status", "failed")
        exit_code = getattr(result, "exit_code", -1)
        if status == "success" and exit_code == 0:
            logger.info("validate_feature_plan: %s valid", feature_id)
            return ToolOutcome(ok=True, detail=repair_note or "")
        stderr = getattr(result, "stderr", None) or ""
        tail = getattr(result, "stdout_tail", "") or ""
        # P-5: persist the FULL stdout+stderr (tail-kept, 8KB cap) so the oracle's
        # real refusing lines reach ``planning_runs.error`` instead of just the
        # INFO/WARNING preamble. Observability only — the refuse decision is
        # unchanged.
        streams = _combine_validate_error_streams(stdout_tail=tail, stderr=stderr)
        return ToolOutcome(
            ok=False,
            detail=(
                f"guardkit feature validate {status} (exit {exit_code}) for "
                f"{feature_id}: {streams}"
            ),
        )

    return _validate


def make_validate_pass_bar(
    *,
    read_allowlist: Sequence[Path] | None = None,
    timeout_seconds: int = _DEFAULT_ORACLE_TIMEOUT_SECONDS,
    run_fn: Callable[..., Awaitable[object]] = guardkit_run,
) -> ValidatePassBarFn:
    """Build the production ``guardkit qa validate pass-bar`` oracle (plan leg).

    Rides the SAME frozen :func:`forge.adapters.guardkit.run.run` seam
    ``make_validate_feature_plan`` uses (the vendored guardkit BINARY the image
    carries — the defect-#7 dual-candidate resolution lives inside that binary,
    not here). ``ok`` iff ``guardkit qa validate pass-bar <path>`` exits 0 (the
    F1 schema — :mod:`guardkit.qa.formats.pass_bar` — accepts the instance).

    The forge-minted per-task bars (fanned out from the 007 seed at plan-commit)
    are validated by guardkit's OWN checker BEFORE they land on the branch, so a
    malformed forge-minted bar fails the leg loudly rather than reaching the B2
    precondition gate with a bar guardkit would later reject.
    """

    async def _validate(worktree_path: Path, bar_rel_path: str) -> ToolOutcome:
        allowlist = list(read_allowlist or [worktree_path])
        try:
            result = await guardkit_run_shim(
                run_fn,
                subcommand="qa",
                args=["validate", "pass-bar", bar_rel_path],
                repo_path=worktree_path,
                read_allowlist=allowlist,
                timeout_seconds=timeout_seconds,
                with_nats_streaming=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary
            logger.exception("validate_pass_bar invocation raised")
            return ToolOutcome(
                ok=False,
                detail=f"qa validate pass-bar raised {type(exc).__name__}: {exc}",
            )
        status = getattr(result, "status", "failed")
        exit_code = getattr(result, "exit_code", -1)
        if status == "success" and exit_code == 0:
            logger.info("validate_pass_bar: %s valid", bar_rel_path)
            return ToolOutcome(ok=True, detail="")
        stderr = getattr(result, "stderr", None) or ""
        tail = getattr(result, "stdout_tail", "") or ""
        return ToolOutcome(
            ok=False,
            detail=(
                f"guardkit qa validate pass-bar {status} (exit {exit_code}) for "
                f"{bar_rel_path}: {(stderr or tail).strip()[:500]}"
            ),
        )

    return _validate


def make_validate_gate_registry(
    *,
    read_allowlist: Sequence[Path] | None = None,
    timeout_seconds: int = _DEFAULT_ORACLE_TIMEOUT_SECONDS,
    run_fn: Callable[..., Awaitable[object]] = guardkit_run,
) -> ValidateGateRegistryFn:
    """Build the production ``guardkit qa validate gate-registry`` oracle (F2).

    Sibling of :func:`make_validate_pass_bar`: rides the SAME frozen
    :func:`forge.adapters.guardkit.run.run` seam, ``ok`` iff
    ``guardkit qa validate gate-registry <path>`` exits 0 (guardkit's OWN
    gate-registry schema accepts the instance).

    The forge-appended per-feature GateEntry (mirrored from the target repo's own
    existing registry entries, pointing at the filled feature-behaviour gate) is
    validated by guardkit's own checker BEFORE the gate script + registry edit
    land on the branch, so a malformed forge-appended entry fails the leg loudly
    rather than reaching the post-deploy live-gate with an entry guardkit would
    later reject.
    """

    async def _validate(worktree_path: Path, registry_rel_path: str) -> ToolOutcome:
        allowlist = list(read_allowlist or [worktree_path])
        try:
            result = await guardkit_run_shim(
                run_fn,
                subcommand="qa",
                args=["validate", "gate-registry", registry_rel_path],
                repo_path=worktree_path,
                read_allowlist=allowlist,
                timeout_seconds=timeout_seconds,
                with_nats_streaming=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary
            logger.exception("validate_gate_registry invocation raised")
            return ToolOutcome(
                ok=False,
                detail=(
                    f"qa validate gate-registry raised {type(exc).__name__}: {exc}"
                ),
            )
        status = getattr(result, "status", "failed")
        exit_code = getattr(result, "exit_code", -1)
        if status == "success" and exit_code == 0:
            logger.info("validate_gate_registry: %s valid", registry_rel_path)
            return ToolOutcome(ok=True, detail="")
        stderr = getattr(result, "stderr", None) or ""
        tail = getattr(result, "stdout_tail", "") or ""
        return ToolOutcome(
            ok=False,
            detail=(
                f"guardkit qa validate gate-registry {status} (exit {exit_code}) "
                f"for {registry_rel_path}: {(stderr or tail).strip()[:500]}"
            ),
        )

    return _validate


# ---------------------------------------------------------------------------
# THE STAMP NORMALIZER hook (Rich's condition 1, 2026-08-16)
#
# guardkit-side seam: ``guardkit qa normalize-stamps --feature FEAT-X --repo
# <path>`` (guardkit/orchestrator/stamp_normalizer.py + cli/qa.py). It mints
# ``verifier:`` stamps by RULE (R1–R10) from the parsed .feature and WRITES
# them into ``.guardkit/features/<id>.yaml``'s ``scenarios:`` map — only titles
# lacking a stamp, never overwriting; anything undecidable REFUSES LOUD
# (exit 2, JSON ``refused`` names every title, nothing written). Forge invokes
# it through the SAME frozen ``forge.adapters.guardkit.run`` seam the validate
# oracles ride, with cwd = the planning worktree, so on success the written
# YAML rides the plan commit (``commit_all`` = ``git add -A``).
#
# Contract of the CLI's stdout (JSON, indent=2) + exit codes:
#   exit 0 → the NormalizeResult dict (``written``, ``stamped``,
#            ``already_stamped``, ``refused: []`` …)
#   exit 2 → {"feature_id", "error", "refused": [...], "written": false} —
#            ``refused`` non-empty = StampNormalizerRefusal; empty = the
#            normalizer could not RUN (StampNormalizerError / no such file)
#   an OLDER guardkit (pre-rebake image) has no such subcommand: click exits 2
#   with ``Error: No such command 'normalize-stamps'.`` on stderr and no JSON.
# The frozen seam keeps only a 4 KB stdout TAIL and guardkit's rich console
# echoes the refusal (wrapped at 80 cols) AFTER the JSON, so the parser below
# reads the JSON when whole, the JSON's ``refused`` block when the head was
# clipped, and the console echo as the last resort — never guessing a title
# it did not see.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampNormalizerOutcome:
    """What THE STAMP NORMALIZER did (or could not do) for one plan.

    ``status``:

    * ``"written"``       — stamps minted by rule and written into the YAML
    * ``"nothing-to-do"`` — every scenario already carried a stamp (or the
      feature has no scenarios); nothing written, exit 0
    * ``"refused"``       — undecidable titles (``refused_titles`` names every
      one verbatim); nothing written; THE RUN STOPS with a card
    * ``"failed"``        — the normalizer could not run / timed out / raised;
      THE RUN STOPS (loud, never a silent skip)
    * ``"unavailable"``   — the guardkit on this image predates the
      subcommand; the run CONTINUES (backward compatible until the rebake)
      and the receipts say so
    * ``"not-wired"``     — no collaborator injected (hermetic composition);
      the run continues and the receipts say so
    """

    status: str
    detail: str = ""
    refused_titles: tuple[str, ...] = ()
    stamped: Mapping[str, str] = field(default_factory=dict)  # title -> verifier
    already_stamped: tuple[str, ...] = ()
    feature_files_filled: tuple[str, ...] = ()
    stamps_on_branch: int | None = None
    titles_recovered_from_console_echo: bool = False

    @property
    def stops_the_run(self) -> bool:
        return self.status in ("refused", "failed")

    def receipt(self) -> dict[str, Any]:
        """JSON-safe record for the plan receipts (the durable feature-plan
        event details) — the hook is NEVER silent, whichever way it went."""
        rec: dict[str, Any] = {
            "status": self.status,
            "detail": self.detail,
            "stamped": dict(self.stamped),
            "stamped_count": len(self.stamped),
            "already_stamped_count": len(self.already_stamped),
            "refused_titles": list(self.refused_titles),
        }
        if self.feature_files_filled:
            rec["feature_files_filled_by_forge"] = list(self.feature_files_filled)
        if self.stamps_on_branch is not None:
            rec["stamps_on_branch"] = self.stamps_on_branch
        if self.titles_recovered_from_console_echo:
            rec["titles_recovered_from_console_echo"] = True
        return rec


@dataclass(frozen=True)
class FeatureFilesFill:
    """Receipt of :func:`declare_feature_files_if_absent`."""

    fired: bool
    yaml_rel: str
    feature_files: tuple[str, ...] = ()
    reason: str = ""
    #: condition 4: a present model list that omits forge's committed spec path
    inconsistent: bool = False


#: click's usage error for a subcommand the installed guardkit does not have
#: (an older image, pre-rebake) — the ONE shape that means "unavailable" rather
#: than "refused"/"failed". Matched on stderr only.
_NORMALIZER_UNAVAILABLE_RE = re.compile(
    r"No such command ['\"](normalize-stamps|qa)['\"]"
)
_NORMALIZER_JSON_REFUSED_RE = re.compile(
    r'^  "refused": (\[\]|\[\n(?:    .*\n)*?  \])', re.MULTILINE
)
_NORMALIZER_JSON_ERROR_RE = re.compile(r'^  "error": (".*"),?$', re.MULTILINE)
_NORMALIZER_JSON_WRITTEN_RE = re.compile(r'^  "written": (true|false),?$', re.MULTILINE)


def parse_normalizer_payload(stdout_tail: str) -> dict[str, Any] | None:
    """Recover the ``normalize-stamps`` JSON result from the seam's stdout TAIL.

    1. the whole JSON object when the tail still starts at its ``{`` (the
       common case — a plan of a dozen scenarios is well under 4 KB);
    2. else the ``"refused": [...]`` / ``"error": "…"`` / ``"written":`` lines
       when the head was clipped but the tail of the object survived
       (``partial: True``);
    3. else the rich console echo's ``Undecidable:`` block (wrapped at 80
       columns by rich when stdout is not a tty; continuation lines are
       re-joined on a single space) — ``partial: True`` and
       ``from_console_echo: True`` so the receipt says how the titles were read.

    Returns ``None`` when none of the three shapes is present.
    """
    text = stdout_tail or ""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        start = text.find("{")
        # indent=2 → the object's own closing brace is the LAST line that is
        # exactly "}" (the console echo that follows never emits one).
        end = -1
        for m in re.finditer(r"^\}\s*$", text[start:], re.MULTILINE):
            end = start + m.end()
        if end > start:
            try:
                data = json.loads(text[start:end])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    partial: dict[str, Any] = {}
    m = _NORMALIZER_JSON_REFUSED_RE.search(text)
    if m:
        try:
            refused = json.loads(m.group(1))
            if isinstance(refused, list):
                partial["refused"] = [str(t) for t in refused]
        except json.JSONDecodeError:
            pass
    m = _NORMALIZER_JSON_ERROR_RE.search(text)
    if m:
        try:
            partial["error"] = str(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    m = _NORMALIZER_JSON_WRITTEN_RE.search(text)
    if m:
        partial["written"] = m.group(1) == "true"
    if partial:
        partial["partial"] = True
        return partial
    # Last resort: the console echo. Take the LAST "Undecidable:" block.
    idx = text.rfind("Undecidable:")
    if idx < 0:
        return None
    titles: list[str] = []
    for line in text[idx:].split("\n")[1:]:
        if line.startswith("FIX:") or line.startswith("STAMP NORMALIZER:"):
            break
        if line.startswith("  - "):
            titles.append(line[4:].rstrip())
        elif titles and line.strip():
            titles[-1] = titles[-1].rstrip() + " " + line.strip()
        elif not line.strip():
            break
    if not titles:
        return None
    return {"refused": titles, "partial": True, "from_console_echo": True}


def classify_normalizer_result(
    feature_id: str,
    *,
    status: str,
    exit_code: int,
    stdout_tail: str,
    stderr: str,
) -> StampNormalizerOutcome:
    """Map one ``guardkit qa normalize-stamps`` seam result onto an outcome.

    Pure; the decision table the hook stands on:

    * ``timeout``                              → ``failed`` (stops the run)
    * non-zero + click ``No such command``     → ``unavailable`` (continues)
    * exit 0                                   → ``written`` / ``nothing-to-do``
    * non-zero + JSON ``refused`` non-empty    → ``refused`` (stops, titles)
    * non-zero + JSON ``error`` (no refused)   → ``failed`` (stops, the error)
    * anything else non-zero                   → ``failed`` (stops, streams)
    """
    stderr = stderr or ""
    tail = stdout_tail or ""
    if status == "timeout":
        return StampNormalizerOutcome(
            status="failed",
            detail=f"guardkit qa normalize-stamps timed out for {feature_id}",
        )
    if exit_code != 0 and _NORMALIZER_UNAVAILABLE_RE.search(stderr):
        line = next(
            (ln.strip() for ln in stderr.splitlines() if "No such command" in ln),
            stderr.strip(),
        )
        return StampNormalizerOutcome(
            status="unavailable",
            detail=(
                "normalizer unavailable: the guardkit on this image has no "
                f"`qa normalize-stamps` subcommand ({line}); verifier stamps "
                f"were NOT minted for {feature_id} — the plan is committed as "
                "the plan-writer wrote it (backward compatible until the rebake)"
            ),
        )
    payload = parse_normalizer_payload(tail)
    if status == "success" and exit_code == 0:
        stamped_raw = (payload or {}).get("stamped") or {}
        stamped = {str(k): str(v) for k, v in stamped_raw.items()} if isinstance(stamped_raw, dict) else {}
        already = tuple(str(t) for t in ((payload or {}).get("already_stamped") or []))
        written = (payload or {}).get("written")
        if written is True or (written is None and stamped):
            return StampNormalizerOutcome(
                status="written",
                detail=(
                    f"{len(stamped)} scenario(s) stamped by rule, "
                    f"{len(already)} already stamped (untouched)"
                ),
                stamped=stamped,
                already_stamped=already,
            )
        if payload is None:
            return StampNormalizerOutcome(
                status="written",
                detail=(
                    "exit 0 (stamps minted and written by guardkit) but the JSON "
                    "result could not be read back from the output tail; see "
                    "stamps_on_branch for what the plan of record carries"
                ),
            )
        return StampNormalizerOutcome(
            status="nothing-to-do",
            detail=(
                f"nothing to write: {len(already)} scenario(s) already stamped, "
                "none unstamped"
            ),
            stamped=stamped,
            already_stamped=already,
        )
    # Non-zero exit.
    refused = tuple(str(t) for t in ((payload or {}).get("refused") or []))
    if refused:
        return StampNormalizerOutcome(
            status="refused",
            detail=(
                f"{len(refused)} scenario(s) undecidable by rule (R1–R10) — no "
                "fallback home; nothing was written"
            ),
            refused_titles=refused,
            titles_recovered_from_console_echo=bool(
                (payload or {}).get("from_console_echo")
            ),
        )
    error = (payload or {}).get("error")
    if error:
        return StampNormalizerOutcome(
            status="failed",
            detail=f"guardkit qa normalize-stamps could not run (exit {exit_code}): {error}",
        )
    streams = _combine_validate_error_streams(stdout_tail=tail, stderr=stderr)
    return StampNormalizerOutcome(
        status="failed",
        detail=(
            f"guardkit qa normalize-stamps {status} (exit {exit_code}) for "
            f"{feature_id} with no readable result: {streams or '(no output)'}"
        ),
    )


_FEATURE_FILES_KEY_RE = re.compile(r"^feature_files\s*:", re.MULTILINE)


def _committed_paths_missing_from_declared(
    yaml_text: str, committed: Sequence[str]
) -> list[str]:
    """Which of forge's committed spec paths are absent from a PRESENT
    ``feature_files:`` list (substring match on the normalised path)."""
    declared_block = yaml_text.split("feature_files:", 1)[1] if "feature_files:" in yaml_text else ""
    declared_block = declared_block.split("\n\n", 1)[0]
    norm = lambda x: str(x).strip().strip("'\"").lstrip("./")
    declared = norm(declared_block)
    # An EMPTY present list ({}, [], ~, null, or nothing) is the plan-writer's
    # statement and is left alone (condition 2) — only a NON-EMPTY list that
    # omits forge's committed path is inconsistent (condition 4).
    if not any(line.strip().startswith("- ") for line in declared_block.splitlines()):
        return []
    return [c for c in committed if norm(c) not in declared]


def declare_feature_files_if_absent(
    worktree_path: Path, feature_id: str, feature_paths: Sequence[str]
) -> FeatureFilesFill:
    """Append ``feature_files:`` to the plan YAML when the plan-writer omitted it.

    The normalizer's scenario universe is EXPLICIT, never inferred — it reads
    the YAML's ``feature_files:``. The 008 plan-writer under the routing-law
    templates omits the key (live: api_test FEAT-F924, planning run 59e31c95),
    and forge is the one party that KNOWS the universe: it committed the spec
    ``.feature`` itself one leg earlier. So forge names that file — the same
    append guardkit's own ``write_stamps`` performs when handed the universe —
    ONLY when the top-level key is absent altogether (a present key, even
    empty, is the plan-writer's statement and is left alone for the
    normalizer to judge loudly). Fires LOUDLY (logged + receipted); a missing
    YAML is a no-op here (the normalizer / validate say so themselves).
    """
    rel = f".guardkit/features/{feature_id}.yaml"
    path = worktree_path / rel
    if not path.is_file():
        alt = worktree_path / f".guardkit/features/{feature_id}.yml"
        if alt.is_file():
            path, rel = alt, f".guardkit/features/{feature_id}.yml"
        else:
            return FeatureFilesFill(False, rel, reason="plan YAML not present")
    paths = tuple(str(p) for p in feature_paths if str(p).strip())
    if not paths:
        return FeatureFilesFill(False, rel, reason="no committed .feature path known")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return FeatureFilesFill(False, rel, reason=f"unreadable: {exc}")
    if _FEATURE_FILES_KEY_RE.search(text):
        # Coordinator condition 4 (review, 08-16): a PRESENT model-written list
        # that omits the path forge itself committed the spec to is a real
        # inconsistency — refuse LOUD here; never silently add to it.
        missing = _committed_paths_missing_from_declared(text, paths)
        if missing:
            return FeatureFilesFill(
                False,
                rel,
                reason=(
                    "feature_files: declared by the plan-writer but it does NOT "
                    "name the spec .feature forge committed on this branch: "
                    + ", ".join(missing)
                    + " — inconsistent plan; refusing (not silently added)"
                ),
                inconsistent=True,
            )
        return FeatureFilesFill(False, rel, reason="feature_files: already declared")
    if not text.endswith("\n"):
        text += "\n"
    text += (
        "# feature_files: declared by forge at plan-commit — the committed spec\n"
        "# .feature of this planning run (the 008 plan-writer omitted the key);\n"
        "# the routing law's scenario universe is explicit, never inferred.\n"
        "feature_files:\n"
        + "".join(f"  - {json.dumps(p, ensure_ascii=False)}\n" for p in paths)
    )
    path.write_text(text, encoding="utf-8")
    logger.warning(
        "stamp normalizer hook: %s declared no feature_files:; forge wrote the "
        "committed spec .feature path(s) %s into it (explicit universe)",
        rel,
        list(paths),
    )
    return FeatureFilesFill(True, rel, feature_files=paths, reason="filled by forge")


def _count_stamps_on_branch(worktree_path: Path, feature_id: str) -> int | None:
    """How many ``scenarios:`` stamps the plan of record carries on disk —
    read back with the SAME reader the close-side check uses, so the receipt
    does not depend on the seam's clipped stdout."""
    try:
        from forge.pipeline.routing_stamps import read_scenario_stamps

        for suffix in ("yaml", "yml"):
            p = worktree_path / ".guardkit" / "features" / f"{feature_id}.{suffix}"
            if p.is_file():
                read = read_scenario_stamps(p)
                return None if read.error else len(read.stamps)
    except Exception:  # noqa: BLE001 — a receipt reader, never a decider
        logger.debug("stamp count read-back failed", exc_info=True)
    return None


#: The normalizer is rules over a handful of files (no model in the loop) —
#: seconds, not minutes. Bounded well under the plan-commit hook's 900 s ceiling
#: (``WorktreeGitRunner.hook_timeout_s``) so normalizer + validate (600 s) still
#: fit inside ONE pre-commit hook; a wedged normalizer fails the leg loudly.
_DEFAULT_NORMALIZER_TIMEOUT_SECONDS: int = 120


def make_normalize_stamps(
    *,
    read_allowlist: Sequence[Path] | None = None,
    timeout_seconds: int = _DEFAULT_NORMALIZER_TIMEOUT_SECONDS,
    run_fn: Callable[..., Awaitable[object]] = guardkit_run,
) -> NormalizeStampsFn:
    """Build the production THE-STAMP-NORMALIZER hook (plan leg, pre-validate).

    Rides the SAME frozen :func:`forge.adapters.guardkit.run.run` seam the
    validate oracles ride: ``guardkit qa normalize-stamps --feature <id>
    --repo <worktree>`` with cwd = the planning worktree. Never raises: every
    outcome — written / nothing-to-do / refused / failed / unavailable — comes
    back as a :class:`StampNormalizerOutcome` the driver decides on
    (refused/failed stop the run with a card; unavailable continues and is
    receipted).
    """

    async def _normalize(worktree_path: Path, feature_id: str) -> StampNormalizerOutcome:
        allowlist = list(read_allowlist or [worktree_path])
        try:
            result = await guardkit_run_shim(
                run_fn,
                subcommand="qa",
                args=[
                    "normalize-stamps",
                    "--feature",
                    feature_id,
                    "--repo",
                    str(worktree_path),
                ],
                repo_path=worktree_path,
                read_allowlist=allowlist,
                timeout_seconds=timeout_seconds,
                with_nats_streaming=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary
            logger.exception("normalize_stamps invocation raised")
            return StampNormalizerOutcome(
                status="failed",
                detail=f"qa normalize-stamps raised {type(exc).__name__}: {exc}",
            )
        outcome = classify_normalizer_result(
            feature_id,
            status=str(getattr(result, "status", "failed")),
            exit_code=int(getattr(result, "exit_code", -1)),
            stdout_tail=str(getattr(result, "stdout_tail", "") or ""),
            stderr=str(getattr(result, "stderr", None) or ""),
        )
        if outcome.status in ("written", "nothing-to-do"):
            count = _count_stamps_on_branch(worktree_path, feature_id)
            if count is not None:
                outcome = dataclasses.replace(outcome, stamps_on_branch=count)
            logger.info(
                "normalize_stamps: %s %s (%s; stamps on branch: %s)",
                feature_id,
                outcome.status,
                outcome.detail,
                outcome.stamps_on_branch,
            )
        elif outcome.status == "unavailable":
            logger.warning("normalize_stamps: %s", outcome.detail)
        else:
            logger.error("normalize_stamps: %s %s: %s", feature_id, outcome.status, outcome.detail)
        return outcome

    return _normalize


async def guardkit_run_shim(run_fn: Callable[..., Awaitable[object]], **kwargs: object):
    """Await ``run_fn(**kwargs)`` — a thin indirection so tests can inject a
    fake ``run`` without patching the module-level import."""
    return await run_fn(**kwargs)
