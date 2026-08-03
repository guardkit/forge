"""The fix journey's commit probe — "did this build actually change anything?"

The conductor's revival, Stage 1b (design pass §a.3, owner lineage
TASK-MBC8-007). The Mode C terminal handler
(:func:`forge.pipeline.terminal_handlers.mode_c.evaluate_terminal`) has
carried a :data:`~forge.pipeline.terminal_handlers.mode_c.CommitProbe`
seam since it was written, with a docstring naming the exact contract and
**no implementation anywhere in the tree**. Without it the handler raises
``RuntimeError`` on the one branch that matters most — the split between
"the fix journey produced commits, hand back a gates-green branch" and
"the fix journey changed nothing, end quietly with a receipt". This module
fills the seam.

What it is
----------

One ``git rev-list --count <base>..HEAD`` against the build's recorded
worktree, and nothing else. No network (``rev-list`` reads local refs
only — there is no fetch, no remote, no ``gh``), no writes, no new path
resolver: the worktree comes from ``builds.worktree_path``.

**Correction (conductor activation §1).** This docstring used to claim
the column was one "the build state machine already writes when it
materialises the worktree". It did not. ``builds.worktree_path`` had ZERO
write sites — the one INSERT omitted it and no UPDATE touched it — which
is why this probe, the conductor dispatcher's pre-spawn check and the
gates reader all refused on every production fix journey. The writer now
exists and is named: :mod:`forge.cli._conductor_worktree` materialises
the tree at the router seam (after the cap-law belt, before the spawn)
and records it through
:meth:`~forge.lifecycle.persistence.SqliteLifecyclePersistence.record_worktree_path`
— a narrow, status-preserving UPDATE, NOT ``apply_transition``, whose
column set stays closed on purpose.

Failure is loud, never quiet
----------------------------

Every failure mode — no build row, no recorded worktree, an allowlist
denial, a non-zero git exit, unparseable output, a raised exception, a
timeout — returns
``CommitProbeResult(count=0, failed=True, error=...)``. The handler turns
that into :attr:`ModeCTerminal.FAILED` with rationale
``"mode-c-commit-check-failed"`` (TASK-MBC8-007 implementation note). A
probe that cannot answer must never be read as "no commits": that would
silently demote a real fix journey to a clean-review terminal and throw
the work away.

The base ref
------------

``base_branch`` defaults to ``"main"`` — the same fallback guardkit's
``_detect_base_branch`` lands on, and the estate's default trunk. It is a
factory argument rather than a per-build lookup because there is no
``base_branch`` column on ``builds`` today; recording the base per build
is a Stage-2 shakeout item (design pass §d, Stage 2). Until then an
operator whose fix journeys branch off something else passes it here once,
at wiring time.

References:
    - design pass §a.3 (`supervisor-revival-design-pass-2026-07-31`).
    - TASK-MBC8-007 — the ``has_commits`` flag's owner.
    - :mod:`forge.pipeline.terminal_handlers.mode_c` — the
      :data:`CommitProbe` contract this module implements.
    - :mod:`forge.adapters.git.operations` — the injectable subprocess
      primitive (list tokens, no shell, timeout + reap discipline).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from forge.adapters.git.operations import ExecuteCallable, _default_execute
from forge.lifecycle.persistence import Build
from forge.pipeline.terminal_handlers.mode_c import CommitProbe, CommitProbeResult

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BASE_BRANCH",
    "PROBE_TIMEOUT_SECONDS",
    "make_mode_c_commit_probe",
]


#: Default base ref for the ``<base>..HEAD`` range. Mirrors guardkit's
#: ``_detect_base_branch`` last-resort fallback and the estate's trunk.
DEFAULT_BASE_BRANCH: str = "main"

#: Wall-clock ceiling for the single git invocation. ``rev-list --count``
#: on a build-sized range is milliseconds; anything approaching this is a
#: hung or lock-contended repository, and a hung probe must surface as a
#: loud failure rather than stall a build's terminal resolution.
PROBE_TIMEOUT_SECONDS: float = 30.0


@runtime_checkable
class _BuildRowReader(Protocol):
    """Duck-typed slice of the persistence facade the probe needs.

    Only ``get_build_row`` is called. Typing it structurally keeps the
    probe testable with a two-line fake and keeps this module from
    depending on the full lifecycle facade.
    """

    def get_build_row(self, build_id: str) -> Any:  # pragma: no cover - stub
        """Return the ``builds`` row for ``build_id``, or ``None``."""
        ...


@runtime_checkable
class _WorktreeAllowlist(Protocol):
    """Optional defence-in-depth check over the recorded worktree path."""

    def is_allowed(self, build_id: str, path: str) -> bool:  # pragma: no cover - stub
        """Return ``True`` iff ``path`` lies inside ``build_id``'s worktree."""
        ...


def make_mode_c_commit_probe(
    pool: _BuildRowReader,
    *,
    base_branch: str = DEFAULT_BASE_BRANCH,
    execute: ExecuteCallable = _default_execute,
    worktree_allowlist: _WorktreeAllowlist | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> CommitProbe:
    """Build the production :data:`CommitProbe` for the fix journey.

    Args:
        pool: The daemon's lifecycle persistence facade (or anything
            exposing ``get_build_row``). Used to resolve the build's
            recorded ``worktree_path`` — the probe does **not** invent a
            second path resolver (TASK-MBC8-007 implementation note).
        base_branch: Left side of the ``<base>..HEAD`` range. See the
            module docstring on why this is a wiring-time argument.
        execute: Async subprocess primitive with the
            :data:`~forge.adapters.git.operations.ExecuteCallable` shape.
            Defaults to the git adapter's list-token, no-shell executor;
            tests inject a fake and never touch a real repository.
        worktree_allowlist: Optional FEAT-FORGE-005 allowlist. When
            supplied, the recorded worktree path is re-checked against it
            before git runs, and a denial is a probe failure — never a
            silent "no commits".
        timeout_seconds: Ceiling for the single git call.

    Returns:
        An ``async (Build) -> CommitProbeResult`` callable satisfying the
        :data:`CommitProbe` contract.
    """
    if not str(base_branch).strip():
        raise ValueError(
            "make_mode_c_commit_probe: base_branch must be a non-empty string"
        )
    base = str(base_branch).strip()

    async def _probe(build: Build) -> CommitProbeResult:
        build_id = getattr(build, "build_id", "") or ""
        try:
            row = pool.get_build_row(build_id)
        except Exception as exc:  # noqa: BLE001 — probe boundary: never raise
            return _failed(
                build_id,
                f"{type(exc).__name__}: {exc} (reading the build row)",
            )

        if row is None:
            return _failed(build_id, f"no builds row for build_id={build_id!r}")

        worktree = getattr(row, "worktree_path", None)
        if not worktree or not str(worktree).strip():
            return _failed(
                build_id,
                f"build_id={build_id!r} has no recorded worktree_path; "
                "the commit range cannot be resolved",
            )
        worktree = str(worktree).strip()

        if worktree_allowlist is not None:
            try:
                allowed = worktree_allowlist.is_allowed(build_id, worktree)
            except Exception as exc:  # noqa: BLE001 — a raising allowlist is a denial
                return _failed(
                    build_id,
                    f"{type(exc).__name__}: {exc} (worktree allowlist check)",
                )
            if not allowed:
                return _failed(
                    build_id,
                    f"worktree allowlist denied {worktree!r} for "
                    f"build_id={build_id!r}",
                )

        command = ["git", "rev-list", "--count", f"{base}..HEAD"]
        try:
            result = await execute(
                command=command,
                cwd=worktree,
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — probe boundary: never raise
            return _failed(
                build_id,
                f"{type(exc).__name__}: {exc} (running {' '.join(command)})",
            )

        exit_code = getattr(result, "exit_code", -1)
        stdout = (getattr(result, "stdout", "") or "").strip()
        stderr = (getattr(result, "stderr", "") or "").strip()

        if exit_code != 0:
            return _failed(
                build_id,
                f"git exited {exit_code} for {' '.join(command)} in "
                f"{worktree}: {stderr or stdout or '<no output>'}",
            )

        try:
            count = int(stdout)
        except (TypeError, ValueError):
            return _failed(
                build_id,
                f"git rev-list --count returned unparseable output "
                f"{stdout!r} for build_id={build_id!r}",
            )

        if count < 0:  # pragma: no cover - git cannot emit this
            return _failed(
                build_id,
                f"git rev-list --count returned a negative count {count}",
            )

        logger.debug(
            "mode_c_commit_probe: build_id=%s range=%s..HEAD count=%d",
            build_id,
            base,
            count,
        )
        return CommitProbeResult(count=count, failed=False)

    return _probe


def _failed(build_id: str, error: str) -> CommitProbeResult:
    """Log loudly and return the failed probe result.

    Centralised so every failure path logs at the same level with the
    same shape — the terminal handler records the ``error`` string
    verbatim onto the FAILED decision, so operators can debug the git
    fault without re-running the build.
    """
    logger.warning(
        "mode_c_commit_probe_failed",
        extra={"build_id": build_id, "error": error},
    )
    return CommitProbeResult(count=0, failed=True, error=error)
