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

References: post-factory-2-three-lanes-handoff §3 B2; factory-2 record hops 6-7;
sovereign-planning-loop-scope §4 (guardkit ``feature validate`` = the oracle).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.adapters.guardkit.run import run as guardkit_run

logger = logging.getLogger(__name__)

__all__ = [
    "NORMALIZER_MODULE_CANDIDATES",
    "TEST_ROOT_DISCOVERY_MODULE_CANDIDATES",
    "DclAuthorFn",
    "DclAuthorOutcome",
    "NormalizeFeatureSpecFn",
    "NormalizerModuleUnresolved",
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
    "discover_target_test_roots",
    "make_dcl_author",
    "make_normalize_feature_spec",
    "make_validate_feature_plan",
    "make_validate_gate_registry",
    "make_validate_pass_bar",
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
        return list(module.discover_test_roots(Path(repo_path)))
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


@dataclass(frozen=True)
class DclAuthorOutcome:
    """Result of one ``guardkit dcl author`` invocation (W1-S2).

    Richer than :class:`ToolOutcome` because the plan-commit leg needs more than
    a pass/fail bit: it reads the ``--json`` envelope
    (``{authored, attempts, zero_shot_clean, repaired_clean, artifact, receipt,
    failure_reason}``) to record the durable ``dcl-author`` event, and it maps
    the guardkit exit CLASS onto the leg's three outcomes:

    * ``exit_class == "authored"`` (exit 0) — the seat authored the artifact +
      receipt into the worktree under the §10 protocol; the leg reads and
      commits them.
    * ``exit_class == "authoring-failed"`` (exit 1) — a LOUD authoring failure
      (dirty second attempt / refusal / empty final content); no artifact
      written. The leg records ``authored:false`` and CONTINUES (never a blocked
      plan).
    * ``exit_class == "instrument-error"`` (exit 2) — node/checker missing,
      config invalid, inputs missing, seat unreachable. Same loud-continue.
    * ``exit_class == "timeout"`` — the seat exceeded the (generous) budget.
    * ``exit_class == "invocation-error"`` — the seam itself raised; contained
      here, never crashes the run.

    ``authored`` is True ONLY on the exit-0 path with the envelope's own
    ``authored`` flag set. ``envelope`` is the parsed ``--json`` dict (empty when
    absent/unparseable). ``detail`` is the loud-failure reason surfaced to the
    warning notification.
    """

    authored: bool
    exit_class: str
    exit_code: int
    envelope: dict[str, Any]
    detail: str = ""


#: ``async (*, worktree_path, slug, task_id, request_rel, criteria_rel)
#: -> DclAuthorOutcome`` — run ``guardkit dcl author`` under the §10 protocol
#: against the materialised worktree. The seat authors ``features/<slug>/
#: <slug>.dcl`` + ``qa/dcl/authoring-<slug>.yaml`` into the worktree; the leg
#: reads and commits them. Injected + default-None on the driver deps so the
#: gherkin track never touches it (SINGLE-SLOT LAW: tests inject a spy; the real
#: closure is the ONLY thing that would call the seat).
DclAuthorFn = Callable[..., Awaitable[DclAuthorOutcome]]


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
        return ToolOutcome(
            ok=False,
            detail=(
                f"guardkit feature validate {status} (exit {exit_code}) for "
                f"{feature_id}: {(stderr or tail).strip()[:500]}"
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


#: The §10 authoring seat can take ~15 min worst case (two calls x ~900s: the
#: zero-shot author + one bounded compile→repair pass). The forge-side budget is
#: generous enough to bound BOTH without ever cutting a live seat off mid-repair.
_DEFAULT_DCL_AUTHOR_TIMEOUT_SECONDS: int = 1900


def _extract_json_envelope(text: str) -> dict[str, Any]:
    """Best-effort parse of the ``guardkit dcl author --json`` envelope.

    ``dcl author --json`` prints a single machine envelope on stdout; the frozen
    run seam hands us the (≤4 KB) ``stdout_tail`` verbatim. Try the whole tail as
    JSON first, then fall back to the LAST balanced ``{...}`` object in the tail
    (robust to a preamble line). Returns ``{}`` on any failure — the envelope is
    advisory metadata for the receipt, never a gate.
    """
    import json

    tail = (text or "").strip()
    if not tail:
        return {}
    try:
        parsed = json.loads(tail)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to the last balanced top-level object in the tail.
    depth = 0
    end = -1
    for idx in range(len(tail) - 1, -1, -1):
        ch = tail[idx]
        if ch == "}":
            if depth == 0:
                end = idx
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end != -1:
                candidate = tail[idx : end + 1]
                try:
                    parsed = json.loads(candidate)
                    return parsed if isinstance(parsed, dict) else {}
                except (json.JSONDecodeError, ValueError):
                    end = -1
    return {}


def make_dcl_author(
    *,
    read_allowlist: Sequence[Path] | None = None,
    timeout_seconds: int = _DEFAULT_DCL_AUTHOR_TIMEOUT_SECONDS,
    run_fn: Callable[..., Awaitable[object]] = guardkit_run,
) -> DclAuthorFn:
    """Build the production ``guardkit dcl author`` oracle (W1-S2, the §10 seat).

    The DCL sibling of :func:`make_validate_pass_bar` / the gate-registry oracle,
    but its outcome is richer (:class:`DclAuthorOutcome`) because the plan-commit
    leg both COMMITS the seat's output and RECORDS the ``--json`` envelope.

    Rides the SAME frozen :func:`forge.adapters.guardkit.run.run` seam the other
    oracles use (untouched guardkit bytes; ``with_nats_streaming=False``). The
    generous ``timeout_seconds`` (default 1900s) bounds BOTH the zero-shot author
    call and the one bounded compile→repair pass (§10) at ~900s each without
    cutting the live seat off mid-repair.

    Exit-class mapping (the CLI contract): exit 0 → ``authored``; exit 1 →
    ``authoring-failed`` (LOUD, no artifact); exit 2 → ``instrument-error``;
    timeout → ``timeout``; the seam itself raising → ``invocation-error``. Every
    non-zero class is contained here (never crashes the run) — the leg turns it
    into a warning + a ``authored:false`` durable event + a CONTINUED plan.
    """

    async def _author(
        *,
        worktree_path: Path,
        slug: str,
        task_id: str,
        request_rel: str,
        criteria_rel: str,
    ) -> DclAuthorOutcome:
        allowlist = list(read_allowlist or [worktree_path])
        args = [
            "author",
            "--feature",
            slug,
            "--task",
            task_id,
            "--repo",
            str(worktree_path),
            "--request",
            request_rel,
            "--criteria",
            criteria_rel,
            "--json",
        ]
        try:
            result = await guardkit_run_shim(
                run_fn,
                subcommand="dcl",
                args=args,
                repo_path=worktree_path,
                read_allowlist=allowlist,
                timeout_seconds=timeout_seconds,
                with_nats_streaming=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — oracle boundary, never crash
            logger.exception("dcl author invocation raised")
            return DclAuthorOutcome(
                authored=False,
                exit_class="invocation-error",
                exit_code=-1,
                envelope={},
                detail=f"dcl author raised {type(exc).__name__}: {exc}",
            )

        status = getattr(result, "status", "failed")
        exit_code = int(getattr(result, "exit_code", -1))
        tail = getattr(result, "stdout_tail", "") or ""
        stderr = getattr(result, "stderr", None) or ""
        envelope = _extract_json_envelope(tail)

        if status == "timeout":
            return DclAuthorOutcome(
                authored=False,
                exit_class="timeout",
                exit_code=exit_code,
                envelope=envelope,
                detail=f"dcl author timed out after {timeout_seconds}s ({slug})",
            )
        if exit_code == 0 and status == "success":
            authored = bool(envelope.get("authored", True))
            logger.info(
                "dcl author: %s authored (attempts=%s, zero_shot_clean=%s)",
                slug,
                envelope.get("attempts"),
                envelope.get("zero_shot_clean"),
            )
            return DclAuthorOutcome(
                authored=authored,
                exit_class="authored" if authored else "authoring-failed",
                exit_code=exit_code,
                envelope=envelope,
                detail="" if authored else "exit 0 but envelope authored=false",
            )
        # Non-zero exits: 1 = loud authoring failure, 2 = instrument/usage error.
        reason = str(
            envelope.get("failure_reason") or (stderr or tail).strip()[:500]
        )
        exit_class = {1: "authoring-failed", 2: "instrument-error"}.get(
            exit_code, "authoring-failed"
        )
        return DclAuthorOutcome(
            authored=False,
            exit_class=exit_class,
            exit_code=exit_code,
            envelope=envelope,
            detail=(
                f"guardkit dcl author exit {exit_code} for {slug}: {reason}"
                if reason
                else f"guardkit dcl author exit {exit_code} for {slug}"
            ),
        )

    return _author


async def guardkit_run_shim(run_fn: Callable[..., Awaitable[object]], **kwargs: object):
    """Await ``run_fn(**kwargs)`` — a thin indirection so tests can inject a
    fake ``run`` without patching the module-level import."""
    return await run_fn(**kwargs)
