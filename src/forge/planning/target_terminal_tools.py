"""Deterministic oracle seams for the Lane B / Phase E1 target terminal (B2).

The target-terminal spec leg (:mod:`forge.planning.driver`) writes the
machine-made spec to the branch and runs the gherkin **normalizer** over its
``.feature`` file. This module is the forge-side home of that call, of the
test-root discovery the plan writer is given, and of the plan YAML's
``feature_files:`` fill. (``guardkit feature validate`` over the plan tree
went in the cull, 27 October 2026: GuardKit validates the feature itself when
it loads it to build.)

Design
------

* **Injected, stubbable** — the driver receives the normalizer as an async
  callable on its deps (like ``dispatch_product_owner``), so the hermetic
  round-trip stubs it on the test bus and never shells out. Production wires
  the :func:`make_normalize_feature_spec` closure below.
* **Bounded (M12)** — the normalizer's subprocess is wrapped in a finite
  ``asyncio.wait_for``; there is no unbounded wait.
* **Frozen bytes untouchable** — forge never edits the guardkit templates or
  the ``feature validate`` schema; it only *invokes* them. The normalizer is
  the same ``feature_spec_normalize`` collapse-then-parse backstop guardkit
  ships (``installer/core/commands/lib/feature_spec_normalize.py``); the exact
  interpreter/module path binds at Lane A / B4 and is a config knob here.

References: post-factory-2-three-lanes-handoff §3 B2; factory-2 record hops 6-7;
sovereign-planning-loop-scope §4 (guardkit ``feature validate`` = the oracle);
ai-transition/docs/routing-law-stamp-normalizer-rules-2026-08-15.md (R1–R10).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import json
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from forge.launch_environment import build_launch_env

logger = logging.getLogger(__name__)

__all__ = [
    "discover_test_roots_from_listing",
    "folders_holding_tests",
    "NORMALIZER_MODULE_CANDIDATES",
    "TEST_ROOT_DISCOVERY_MODULE_CANDIDATES",
    "NormalizeFeatureSpecFn",
    "NormalizerModuleUnresolved",
    "FeatureFilesFill",
    "declare_feature_files_if_absent",
    "TargetTestRootsUnresolved",
    "ToolOutcome",
    "DividerRepairResult",
    "comment_box_drawing_dividers",
    "TS_TEST_FILE_SUFFIXES",
    "discover_target_test_roots",
    "discover_ts_shape_test_roots",
    "shallow_discover_test_roots",
    "make_normalize_feature_spec",
    "resolve_normalizer_command",
]

#: Default per-oracle wall-clock budget. Bounded (M12) — a stuck oracle fails
#: the leg loudly rather than hanging the planning run forever.
_DEFAULT_ORACLE_TIMEOUT_SECONDS: int = 600


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


def discover_test_roots_from_listing(paths: Sequence[str]) -> list[str]:
    """The test roots of a repository known only by its tracked-file listing.

    For a repository read through its sandbox helper (the 1 October planner fix,
    1 October 2026) there is no checkout here for the discovery above to
    walk, and it used to answer ``[]`` in silence. Rather than re-implement
    the discovery's rules over strings, this lays the listing out as an empty
    skeleton (each tracked file as an empty file, so every directory that
    holds a tracked file exists) in a temporary folder and runs the SAME
    discovery on it: guardkit's own :func:`discover_target_test_roots`, or
    the shallow fallback when guardkit is not importable. Both decide by
    which directories and file names exist, which the skeleton reproduces.

    A path that is absolute or climbs out with ``..`` is skipped. Never
    raises; an empty listing is ``[]``.
    """
    with tempfile.TemporaryDirectory(prefix="forge-test-roots-") as scratch:
        root = Path(scratch)
        for rel in paths:
            rel = str(rel or "")
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts or "" in parts:
                continue
            target = root.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            except OSError:
                continue
        try:
            return discover_target_test_roots(root)
        except TargetTestRootsUnresolved:
            return shallow_discover_test_roots(root)


def folders_holding_tests(test_roots: Sequence[str]) -> tuple[str, ...]:
    """The folders that hold tests, by the discovery's own shapes: each root,
    and — for a root that is an immediate subfolder of a top-level folder
    (``tests/users``, ``test/unit``) — that top-level folder too, since the
    discovery found the root by looking inside it. A colocated ``__tests__``
    root names only itself."""
    prefixes: list[str] = []
    for root in test_roots:
        root = str(root).strip("/")
        if not root:
            continue
        prefixes.append(root + "/")
        parts = root.split("/")
        if len(parts) == 2 and parts[1] != "__tests__":
            prefixes.append(parts[0] + "/")
    return tuple(dict.fromkeys(prefixes))


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


async def _default_normalizer_subprocess(
    *,
    command: Sequence[str],
    cwd: str,
    timeout: int,
    memory_project: str | None = None,
    launch_settings: Sequence[str] | None = None,
) -> tuple[str, str, int, bool]:
    """Bounded subprocess seam for the normalizer (stubbable in tests).

    Returns ``(stdout, stderr, exit_code, timed_out)``. Never raises for a
    non-zero exit — the caller maps exit codes to :class:`ToolOutcome`.

    THE ENVIRONMENT DOOR (23 September 2026; the one-true-copy design pass,
    item 1, fourth revision H, "the environment door"). This launch named no
    environment at all, which is the same door ``executor/shell_steps.py`` had
    by the other spelling: a launch with no ``env=`` hands the child this
    whole process's environment. It is now BUILT — the factory's own named
    list, plus the memory name recorded for the work and the setting NAMES the
    project itself declared. What is not named is not passed, so a credential
    the launching process happened to be holding does not travel into a
    program run against a project's working folder.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=build_launch_env(
            memory_project=memory_project, declared=launch_settings or ()
        ),
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
# The plan YAML's ``feature_files:`` (the committed spec .feature), written by
# Forge when the plan-writer left the key out. The stamp normalizer, the
# feature validate oracle, the provability check and the pass-bar and
# gate-registry validators that used to live here went in the cull
# (27 October 2026).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureFilesFill:
    """Receipt of :func:`declare_feature_files_if_absent`."""

    fired: bool
    yaml_rel: str
    feature_files: tuple[str, ...] = ()
    reason: str = ""



_FEATURE_FILES_KEY_RE = re.compile(r"^feature_files\s*:", re.MULTILINE)


def declare_feature_files_if_absent(
    worktree_path: Path, feature_id: str, feature_paths: Sequence[str]
) -> FeatureFilesFill:
    """Append ``feature_files:`` to the plan YAML when the plan-writer omitted it.

    The 008 plan-writer omits the key (live: api_test FEAT-F924, planning run
    59e31c95), and forge is the one party that knows the file: it committed the
    spec ``.feature`` itself one leg earlier. So forge names that file ONLY when
    the top-level key is absent altogether (a present key, even empty, is the
    plan-writer's statement and is left alone). A missing YAML is a no-op.
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
        # A present key, even empty, is the plan-writer's statement and is
        # left alone.
        return FeatureFilesFill(False, rel, reason="feature_files: already declared")
    if not text.endswith("\n"):
        text += "\n"
    text += (
        "# feature_files: declared by forge at plan-commit — the committed spec\n"
        "# .feature of this planning run (the 008 plan-writer omitted the key).\n"
        "feature_files:\n"
        + "".join(f"  - {json.dumps(p, ensure_ascii=False)}\n" for p in paths)
    )
    path.write_text(text, encoding="utf-8")
    logger.warning(
        "plan files: %s declared no feature_files:; forge wrote the committed "
        "spec .feature path(s) %s into it",
        rel,
        list(paths),
    )
    return FeatureFilesFill(True, rel, feature_files=paths, reason="filled by forge")

