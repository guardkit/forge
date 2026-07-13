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
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from forge.adapters.guardkit.run import run as guardkit_run

logger = logging.getLogger(__name__)

__all__ = [
    "NormalizeFeatureSpecFn",
    "ToolOutcome",
    "ValidateFeaturePlanFn",
    "make_normalize_feature_spec",
    "make_validate_feature_plan",
]

#: Default per-oracle wall-clock budget. Bounded (M12) — a stuck oracle fails
#: the leg loudly rather than hanging the planning run forever.
_DEFAULT_ORACLE_TIMEOUT_SECONDS: int = 600

#: Default normalizer invocation. ``feature_spec_normalize`` is a guardkit
#: module (``python -m …``), not a ``guardkit`` subcommand; the interpreter and
#: module path are a config knob so Lane A / B4 can bind the deployed form.
_DEFAULT_NORMALIZER_COMMAND: tuple[str, ...] = (
    "python",
    "-m",
    "installer.core.commands.lib.feature_spec_normalize",
)


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
    command_prefix: Sequence[str] = _DEFAULT_NORMALIZER_COMMAND,
    timeout_seconds: int = _DEFAULT_ORACLE_TIMEOUT_SECONDS,
    subprocess_seam: Callable[..., Awaitable[tuple[str, str, int, bool]]]
    | None = None,
) -> NormalizeFeatureSpecFn:
    """Build the production normalizer oracle (spec-leg pre-commit hook).

    The returned callable rewrites the ``.feature`` file in place (collapsing
    any wrapped gherkin steps) and validates it parses — exit 0 means the
    committed spec is parseable by the downstream ``/feature-plan`` linker.
    """
    seam = subprocess_seam or _default_normalizer_subprocess

    async def _normalize(worktree_path: Path, feature_rel_path: str) -> ToolOutcome:
        target = (worktree_path / feature_rel_path).resolve()
        command = [*command_prefix, str(target)]
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


async def guardkit_run_shim(run_fn: Callable[..., Awaitable[object]], **kwargs: object):
    """Await ``run_fn(**kwargs)`` — a thin indirection so tests can inject a
    fake ``run`` without patching the module-level import."""
    return await run_fn(**kwargs)
