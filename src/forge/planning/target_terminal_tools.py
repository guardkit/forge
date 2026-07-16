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
    "NormalizeFeatureSpecFn",
    "NormalizerModuleUnresolved",
    "TargetTestRootsUnresolved",
    "ToolOutcome",
    "ValidateFeaturePlanFn",
    "ValidateGateRegistryFn",
    "ValidatePassBarFn",
    "discover_target_test_roots",
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
        return ToolOutcome(ok=True, detail="")

    return _normalize


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
            return ToolOutcome(ok=True, detail="")
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


async def guardkit_run_shim(run_fn: Callable[..., Awaitable[object]], **kwargs: object):
    """Await ``run_fn(**kwargs)`` — a thin indirection so tests can inject a
    fake ``run`` without patching the module-level import."""
    return await run_fn(**kwargs)
