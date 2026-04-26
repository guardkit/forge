"""Subprocess wrapper for the GuardKit adapter (TASK-GCI-008).

The :func:`run` coroutine is the *single boundary* through which every
``guardkit_*`` tool wrapper invokes a GuardKit subcommand. It composes:

- :func:`forge.adapters.guardkit.context_resolver.resolve_context_flags`
  (TASK-GCI-003 / DDR-005) for ``--context`` flag synthesis;
- a stubbable ``_execute_subprocess`` seam standing in for the
  DeepAgents ``execute`` tool — production wiring keeps the same
  ``(command, cwd, timeout)`` shape mandated by
  ``docs/design/contracts/API-subprocess.md`` §3.1;
- :func:`forge.adapters.guardkit.parser.parse_guardkit_output`
  (TASK-GCI-004) to fold raw subprocess output into the canonical
  :class:`~forge.adapters.guardkit.models.GuardKitResult`.

Behaviour contract (per ADR-ARCH-025 and the task acceptance criteria):

- the function **never raises** past its boundary, with one exception —
  :class:`asyncio.CancelledError` is re-raised so the surrounding async
  context unwinds cleanly (Implementation Notes, TASK-GCI-008);
- a 600 second default timeout (ASSUM-001) caps every invocation; on
  expiry the in-flight subprocess is terminated, the parser is told
  ``timed_out=True``, and the result carries ``status="timeout"``;
- ``cwd`` is enforced to be absolute and to resolve to a path inside the
  caller's ``read_allowlist`` (worktree confinement —
  defence-in-depth atop DeepAgents' own enforcement);
- Graphiti subcommands (``graphiti …``) skip context-manifest
  resolution entirely (DDR-005);
- ``extra_context_paths`` are merged for the current call only — the
  resolver remains stateless (ASSUM-005, ASSUM-007);
- the function holds no module-level mutable state, so two concurrent
  ``run()`` invocations against the same worktree do not interfere
  (ASSUM-006).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from forge.adapters.guardkit.context_resolver import resolve_context_flags
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.parser import parse_guardkit_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_GUARDKIT_BINARY: str = "/usr/local/bin/guardkit"
_DEFAULT_TIMEOUT_SECONDS: int = 600  # ASSUM-001
_KILL_GRACE_SECONDS: float = 5.0  # SIGTERM → SIGKILL grace window
_GRAPHITI_PREFIX: str = "graphiti"


# ---------------------------------------------------------------------------
# Subprocess seam
# ---------------------------------------------------------------------------


async def _execute_subprocess(
    *,
    command: list[str],
    cwd: str,
    timeout: int,
) -> tuple[str, str, int, float, bool]:
    """Execute a command via :func:`asyncio.create_subprocess_exec`.

    This is the **stubbable seam** standing in for DeepAgents' ``execute``
    tool. Tests monkeypatch this function to feed deterministic outputs
    through :func:`run` without ever spawning a real process. Production
    wiring may swap in a DeepAgents-backed executor with the same
    signature.

    The command is passed as a *list* (never a shell string) — this is
    the contract ``run()`` enforces for shell-injection safety.

    Returns ``(stdout, stderr, exit_code, duration_secs, timed_out)``.
    """
    started_at = time.monotonic()
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
        # Terminate; escalate to SIGKILL after a grace window.
        proc.terminate()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_KILL_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
        timed_out = True
    except asyncio.CancelledError:
        # Caller cancelled — terminate the child and re-raise.
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise

    duration = time.monotonic() - started_at
    exit_code = proc.returncode if proc.returncode is not None else -1
    return (
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
        exit_code,
        duration,
        timed_out,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run(
    *,
    subcommand: str,
    args: list[str],
    repo_path: Path,
    read_allowlist: list[Path],
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    with_nats_streaming: bool = True,
    extra_context_paths: list[str] | None = None,
) -> GuardKitResult:
    """Single subprocess entry point for every GuardKit subcommand.

    Composes :func:`resolve_context_flags`, the stubbable subprocess seam,
    and :func:`parse_guardkit_output`. Enforces the 600-second default
    timeout (ASSUM-001), confines ``cwd`` to ``read_allowlist``, and
    folds every outcome (success, failure, timeout, internal error) into
    a structured :class:`GuardKitResult` rather than raising
    (ADR-ARCH-025).

    Parameters
    ----------
    subcommand:
        GuardKit subcommand to invoke (e.g. ``"feature-spec"``,
        ``"graphiti add-context"``).
    args:
        Positional arguments and flags appended after the subcommand
        token.
    repo_path:
        Working directory for the subprocess. Must be absolute and
        resolve to a path under at least one ``read_allowlist`` entry.
    read_allowlist:
        Caller's permitted-read paths. Used both for resolver
        filtering and as the ``cwd`` allowlist (defence in depth atop
        DeepAgents' own check).
    timeout_seconds:
        Per-call timeout. Defaults to 600 (ASSUM-001).
    with_nats_streaming:
        When ``True``, append ``--nats`` to the command line so
        GuardKit publishes ``pipeline.stage-complete.*`` progress
        messages (TASK-GCI-005 wires the subscriber separately).
    extra_context_paths:
        Caller-supplied ``--context`` paths merged on top of the
        manifest-derived ones for **this call only** — never persisted
        (ASSUM-005, retry path).

    Returns
    -------
    GuardKitResult
        Always — exceptions are captured and surfaced as a
        ``status="failed"`` result with a ``wrapper_internal_error``
        warning. The single exception is
        :class:`asyncio.CancelledError`, which is re-raised so the
        surrounding async context unwinds correctly.
    """
    started_at = time.monotonic()
    warnings: list[GuardKitWarning] = []

    try:
        # Defence-in-depth: cwd must be absolute. DeepAgents' permission
        # layer enforces the working_directory_allowlist, but we also
        # check here so a test or a misconfigured caller cannot bypass
        # the contract by passing a relative path.
        if not repo_path.is_absolute():
            return _refused_cwd_result(
                subcommand=subcommand,
                duration_secs=time.monotonic() - started_at,
                detail=(
                    f"repo_path {repo_path!s} is not absolute; "
                    "the worktree allowlist requires absolute paths"
                ),
                allowlist=read_allowlist,
            )

        resolved_repo = repo_path.resolve(strict=False)
        resolved_allowlist = [p.resolve(strict=False) for p in read_allowlist]
        if not any(
            _is_within(resolved_repo, allowed) for allowed in resolved_allowlist
        ):
            return _refused_cwd_result(
                subcommand=subcommand,
                duration_secs=time.monotonic() - started_at,
                detail=(
                    f"repo_path {repo_path!s} resolves to {resolved_repo!s} "
                    "which is outside the read allowlist"
                ),
                allowlist=read_allowlist,
            )

        # Graphiti subcommands skip the resolver entirely (DDR-005).
        context_flags: list[str] = []
        if not _is_graphiti_subcommand(subcommand):
            try:
                resolved_ctx = resolve_context_flags(
                    resolved_repo, subcommand, read_allowlist
                )
            except KeyError:
                # Unknown-to-resolver subcommand: emit a warning rather
                # than failing — let the parser+exit-code arbitrate.
                warnings.append(
                    GuardKitWarning(
                        code="context_resolver_unknown_subcommand",
                        message=(
                            f"resolver has no category filter for "
                            f"subcommand {subcommand!r}; proceeding with "
                            "no --context flags"
                        ),
                        details={"subcommand": subcommand},
                    )
                )
            else:
                warnings.extend(resolved_ctx.warnings)
                context_flags = list(resolved_ctx.flags)

        # ASSUM-005: caller-supplied context for this call only.
        if extra_context_paths:
            for path in extra_context_paths:
                context_flags.extend(["--context", str(path)])

        nats_flag = ["--nats"] if with_nats_streaming else []
        command: list[str] = [
            _GUARDKIT_BINARY,
            subcommand,
            *args,
            *context_flags,
            *nats_flag,
        ]

        try:
            stdout, stderr, exit_code, duration, timed_out = (
                await _execute_subprocess(
                    command=command,
                    cwd=str(resolved_repo),
                    timeout=timeout_seconds,
                )
            )
        except PermissionError as exc:
            # Binary not in DeepAgents' shell allowlist — convert to a
            # structured failed result with the canonical warning code.
            warnings.append(
                GuardKitWarning(
                    code="permissions_refused",
                    message=(
                        f"subprocess refused by permissions layer: {exc}"
                    ),
                    details={
                        "binary": _GUARDKIT_BINARY,
                        "subcommand": subcommand,
                        "error": str(exc),
                    },
                )
            )
            return GuardKitResult(
                status="failed",
                subcommand=subcommand,
                duration_secs=time.monotonic() - started_at,
                stdout_tail="",
                stderr=str(exc),
                exit_code=-1,
                warnings=warnings,
            )

        result = parse_guardkit_output(
            subcommand=subcommand,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_secs=duration,
            timed_out=timed_out,
        )
        # Pre-execution warnings (resolver, retry merge) must lead the
        # parser warnings so callers see boundary-level warnings first.
        if warnings:
            return result.model_copy(
                update={"warnings": warnings + list(result.warnings)}
            )
        return result

    except asyncio.CancelledError:
        # The single, deliberate exception to "never raises". Cancellation
        # MUST propagate so the surrounding asyncio task unwinds
        # correctly (Implementation Notes; ADR-ARCH-025 footnote).
        raise

    except Exception as exc:
        logger.exception("guardkit.run() internal error: %r", exc)
        warnings.append(
            GuardKitWarning(
                code="wrapper_internal_error",
                message=f"{type(exc).__name__}: {exc}",
                details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "subcommand": subcommand,
                },
            )
        )
        return GuardKitResult(
            status="failed",
            subcommand=subcommand,
            duration_secs=time.monotonic() - started_at,
            stdout_tail="",
            stderr=f"{type(exc).__name__}: {exc}",
            exit_code=-1,
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_graphiti_subcommand(subcommand: str) -> bool:
    """Return ``True`` iff ``subcommand`` is in the Graphiti family.

    Matches both the space-separated form (``"graphiti add-context"``)
    and a bare ``"graphiti"`` token. The resolver MUST be skipped for
    these per DDR-005 / TASK-GCI-010.
    """
    if not subcommand:
        return False
    head, _, _ = subcommand.partition(" ")
    return head == _GRAPHITI_PREFIX


def _is_within(child: Path, parent: Path) -> bool:
    """Return ``True`` iff ``child`` is equal to or nested under ``parent``.

    Both paths must already be resolved/absolute. Mirrors the helper in
    :mod:`forge.adapters.guardkit.context_resolver` so the two
    confinement checks stay symmetrical.
    """
    try:
        return child == parent or child.is_relative_to(parent)
    except ValueError:
        return False


def _refused_cwd_result(
    *,
    subcommand: str,
    duration_secs: float,
    detail: str,
    allowlist: list[Path],
) -> GuardKitResult:
    """Build a ``status="failed"`` result for a refused working directory."""
    return GuardKitResult(
        status="failed",
        subcommand=subcommand,
        duration_secs=duration_secs,
        stdout_tail="",
        stderr=detail,
        exit_code=-1,
        warnings=[
            GuardKitWarning(
                code="cwd_outside_allowlist",
                message=detail,
                details={
                    "allowlist": [str(p) for p in allowlist],
                },
            )
        ],
    )


__all__ = ["run"]
