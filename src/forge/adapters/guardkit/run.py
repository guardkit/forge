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

Scope note (ADR-ARCH-033): "single boundary" here means every *one-shot* tool
invocation. The long-running ``guardkit autobuild`` build deliberately uses a
*separate* streaming subprocess in ``subagents/autobuild_runner.py`` today (live
progress + 3600s budget + direct lifecycle mapping). Converging that path onto a
streaming variant of this module — so the runner consumes a structured
``GuardKitResult.coach_score`` instead of scraping stdout — is the tracked
follow-up in ADR-ARCH-033 (and the prerequisite for FEAT-UBS-002).

Behaviour contract (per ADR-ARCH-025 and the task acceptance criteria):

- the function **never raises** past its boundary, with one exception —
  :class:`asyncio.CancelledError` is re-raised so the surrounding async
  context unwinds cleanly (Implementation Notes, TASK-GCI-008);
- a 600 second default timeout (ASSUM-001) caps every invocation; on
  expiry the in-flight subprocess's whole PROCESS GROUP is terminated
  (SIGTERM → grace → SIGKILL), the parser is told ``timed_out=True``, and
  the result carries ``status="timeout"``. The post-kill read is bounded
  (:data:`_POST_KILL_READ_SECONDS`); past that window the stdout/stderr
  tails are surrendered as EMPTY, and the loss is NAMED — in the log and,
  load-bearingly, as a ``post_kill_output_surrendered`` warning ON THE
  RESULT, because the dispatcher threads warnings verbatim into the
  stage rationale. The alternative is an await with no ceiling, because a
  pipe holder that outlived the kill means EOF never comes (LI stage-2
  design §1);
- ``cwd`` is enforced to be absolute and to resolve to a path inside the
  caller's ``read_allowlist`` (worktree confinement —
  defence-in-depth atop DeepAgents' own enforcement);
- Graphiti subcommands (``graphiti …``) skip context-manifest
  resolution entirely (DDR-005);
- ``extra_context_paths`` are merged for the current call only — the
  resolver remains stateless (ASSUM-005, ASSUM-007);
- the function holds no *per-call* module-level mutable state, so two
  concurrent ``run()`` invocations against the same worktree do not
  interfere (ASSUM-006). The single module-level cache is the write-once
  binary resolution below, which carries no per-call information.

Binary resolution (design pass ``leg-invocation-design-pass-2026-08-02``
§d stage-1 venue-B): the spawn's ``argv[0]`` was hardcoded to
``/usr/local/bin/guardkit``, which exists only inside the container — so
outside it this seam could not spawn at all. It now walks the same ladder
the long-running autobuild path walks (``FORGE_GUARDKIT_PATH`` → ``PATH`` →
``~/.agentecflow/bin/guardkit``), resolved once and logged at INFO on first
use; the container's binary keeps winning because ``/usr/local/bin`` is on
the image's ``PATH``. Nothing else about the dispatch contract moves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from forge.adapters.guardkit.context_resolver import resolve_context_flags
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.parser import parse_guardkit_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Environment override for the absolute path to the ``guardkit``
#: executable. Deliberately the *same* operator knob the long-running
#: autobuild path already honours
#: (:data:`forge.subagents.autobuild_runner.FORGE_GUARDKIT_PATH_ENV`) — one
#: env var configures both seams. Declared locally rather than imported so
#: the adapter layer keeps no dependency on the subagent layer.
GUARDKIT_PATH_ENV: str = "FORGE_GUARDKIT_PATH"

#: The executable's name, as looked up on ``PATH`` (rung 2).
_GUARDKIT_BINARY_NAME: str = "guardkit"

#: Last-resort rung: the local launcher the installer writes. Expanded with
#: :func:`os.path.expanduser` at resolution time (so ``$HOME`` is read then,
#: not at import).
_AGENTECFLOW_GUARDKIT: str = "~/.agentecflow/bin/guardkit"

#: The container's install location (``Dockerfile`` runtime stage symlinks
#: ``/opt/venv/bin/guardkit-py`` here). It is *not* a rung of its own: it is
#: reached through the PATH rung, because ``/usr/local/bin`` is on the image's
#: default ``PATH`` and no earlier entry (``/opt/venv/bin``) ships a file
#: named ``guardkit`` — only ``guardkit-py``. Retained as documentation and
#: as the name the hardening corpus checks.
_GUARDKIT_BINARY: str = "/usr/local/bin/guardkit"

_DEFAULT_TIMEOUT_SECONDS: int = 600  # ASSUM-001
_KILL_GRACE_SECONDS: float = 5.0  # SIGTERM → SIGKILL grace window
_GRAPHITI_PREFIX: str = "graphiti"

#: Ceiling on the post-kill ``communicate()`` (LI stage-2 design §1, the
#: kill fix). After SIGKILL the child is gone, but any pipe holder it left
#: behind — a grandchild the group kill could not reach — keeps the read
#: end open, so EOF never arrives and an unbounded await never returns.
#: There is no watchdog above this call (the conductor's turn is a bare
#: await), so an unbounded read here is an unbounded stage. Past this
#: window the tails are surrendered as EMPTY and the loss is NAMED twice:
#: in the log, and as a ``post_kill_output_surrendered`` warning on the
#: :class:`GuardKitResult` — which the dispatcher folds verbatim into the
#: stage rationale, so a leg that LOST its output can never be read as a
#: leg that produced none. A degraded read that ends beats a perfect read
#: that does not.
_POST_KILL_READ_SECONDS: float = 10.0


# ---------------------------------------------------------------------------
# Binary resolution (design pass 2026-08-02 §d stage-1 venue-B)
# ---------------------------------------------------------------------------


#: Memoised result of :func:`_resolve_guardkit_binary`. The module's
#: "no mutable module state" property (ASSUM-006) is preserved in substance:
#: this cache is *write-once, idempotent and read-only thereafter*, it is
#: filled by a purely synchronous helper (no ``await`` inside, so two
#: concurrent ``run()`` calls cannot interleave mid-resolution), and it never
#: participates in a call's result beyond supplying ``argv[0]``. Only a
#: successful resolution is cached — a failure is retried on the next call so
#: an operator who installs the binary mid-process is picked up.
_resolved_guardkit_binary: str | None = None


def _is_executable_file(path: str) -> bool:
    """Return ``True`` iff ``path`` is an existing, executable file."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _absolute(path: str) -> str:
    """Expand ``~`` and make absolute **without following symlinks**.

    Deliberately :func:`os.path.abspath`, not :meth:`Path.resolve` (which the
    autobuild path uses): in the container ``/usr/local/bin/guardkit`` is a
    symlink to ``/opt/venv/bin/guardkit-py``, and resolving it would spawn —
    and receipt — a different path than the one the image installs. The
    ladder must hand the spawn the path it actually found.
    """
    return os.path.abspath(os.path.expanduser(path))


def _remember_guardkit_binary(path: str, *, rung: str) -> str:
    """Cache ``path`` as the resolved binary and log it once, at INFO."""
    global _resolved_guardkit_binary
    _resolved_guardkit_binary = path
    logger.info(
        "guardkit adapter: resolved guardkit binary to %s (via %s)",
        path,
        rung,
    )
    return path


def _resolve_guardkit_binary() -> tuple[str | None, list[str]]:
    """Resolve the ``guardkit`` executable, once, lazily.

    Resolution ladder — the same one
    :func:`forge.subagents.autobuild_runner._resolve_guardkit_path` walks for
    the long-running build path, extended with the local-launcher rung:

    1. :data:`GUARDKIT_PATH_ENV` (``FORGE_GUARDKIT_PATH``), when it points at
       an executable file. A set-but-unusable value logs a WARNING and falls
       through rather than failing the dispatch.
    2. :func:`shutil.which` on :data:`_GUARDKIT_BINARY_NAME`. This is the rung
       the container takes: ``/usr/local/bin/guardkit`` is on the image's
       ``PATH`` and nothing ahead of it shadows the name, so the container's
       binary still wins whenever it is present.
    3. :data:`_AGENTECFLOW_GUARDKIT` — the local installer's launcher, the
       only one that exists outside the container on the fleet boxes.

    The first success is memoised in :data:`_resolved_guardkit_binary` and
    logged once at INFO.

    Returns:
        ``(path, searched)``. ``path`` is ``None`` only when every rung
        missed; ``searched`` is the human-readable list of what was tried, in
        order, for the honest dispatch failure. On a memo hit ``searched`` is
        empty — it is only ever read on the failure path.
    """
    if _resolved_guardkit_binary is not None:
        return _resolved_guardkit_binary, []

    searched: list[str] = []

    # Rung 1 — explicit operator override.
    override = os.environ.get(GUARDKIT_PATH_ENV, "").strip()
    if override:
        candidate = _absolute(override)
        searched.append(f"{GUARDKIT_PATH_ENV}={override!r} -> {candidate}")
        if _is_executable_file(candidate):
            return _remember_guardkit_binary(
                candidate, rung=GUARDKIT_PATH_ENV
            ), searched
        logger.warning(
            "guardkit adapter: %s=%r does not resolve to an executable file "
            "— falling back to PATH lookup",
            GUARDKIT_PATH_ENV,
            override,
        )
    else:
        searched.append(f"{GUARDKIT_PATH_ENV} (unset)")

    # Rung 2 — PATH lookup (the container's /usr/local/bin/guardkit).
    which_result = shutil.which(_GUARDKIT_BINARY_NAME)
    if which_result:
        searched.append(
            f"PATH lookup for {_GUARDKIT_BINARY_NAME!r} -> {which_result}"
        )
        return _remember_guardkit_binary(
            _absolute(which_result), rung="PATH"
        ), searched
    searched.append(f"PATH lookup for {_GUARDKIT_BINARY_NAME!r} (not found)")

    # Rung 3 — the local installer's launcher.
    fallback = _absolute(_AGENTECFLOW_GUARDKIT)
    searched.append(f"{_AGENTECFLOW_GUARDKIT} -> {fallback}")
    if _is_executable_file(fallback):
        return _remember_guardkit_binary(
            fallback, rung=_AGENTECFLOW_GUARDKIT
        ), searched

    logger.warning(
        "guardkit adapter: guardkit binary not found — searched %s",
        "; ".join(searched),
    )
    return None, searched


# ---------------------------------------------------------------------------
# Subprocess seam
# ---------------------------------------------------------------------------


def _signal_process_group(proc: Any, sig: int) -> None:
    """Signal the child's whole process group, never just the child.

    The spawn runs with ``start_new_session=True``, so the child LEADS a
    group of its own and this call cannot reach the forge daemon. That
    pairing is the point: a ``guardkit`` leg spawns its own children (a
    test runner, a harness), and ``Process.terminate()`` reaches exactly
    one pid — the grandchildren survive, keep working, and keep the
    child's pipes open.

    **The pgid is the child's pid, and it is NOT read back from the
    kernel.** ``start_new_session=True`` makes the child both session and
    group leader, so ``pgid == pid`` by construction — and a process
    group outlives its dead leader. The escalation step depends on
    exactly that: by the time SIGKILL runs, the child is usually already
    dead from the group SIGTERM *and reaped* by asyncio's child watcher,
    so ``os.getpgid(pid)`` raises :class:`ProcessLookupError` and a
    getpgid-first implementation signals NOTHING — not the group, not
    even the child. That is the one case the SIGTERM → grace → SIGKILL
    ladder exists for (a grandchild that ignored SIGTERM), so reading the
    pgid back would disarm the ladder precisely when it matters.

    A :class:`ProcessLookupError` from :func:`os.killpg` therefore means
    what it says: **no process remains in the group** — nothing to signal.
    (The pid is not reused while the child is unreaped, and after that the
    window is the kill ladder's few seconds; the estate accepts that
    residual over an escalation that never fires.) The child-alone
    fallback is for the cases where a *group* signal is impossible but a
    process may still be there: a platform with no :func:`os.killpg`, or a
    permission/OS error from the group call. A failure to signal is
    logged, never raised: this is the teardown half of a boundary whose
    contract is "never raises".
    """
    pid = getattr(proc, "pid", None)
    if pid is None:  # pragma: no cover — asyncio always sets pid
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            # The whole group is gone — nothing to signal, nothing to say.
            return
        except OSError as exc:
            # PermissionError is an OSError subclass; one clause covers
            # both, and the group signal failing is the only thing that
            # makes the child-alone fallback below worth trying.
            logger.warning(
                "guardkit adapter: could not signal process group of pid=%s "
                "(%s: %s) — falling back to the child alone; any "
                "grandchildren it started may survive",
                pid,
                type(exc).__name__,
                exc,
            )
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):  # pragma: no cover — race only
        return


async def _bounded_post_kill_read(proc: Any) -> tuple[bytes, bytes, bool]:
    """Read the killed child's tails, but never wait forever for EOF.

    Returns ``(stdout, stderr, surrendered)``. On expiry the tails are
    ``(b"", b"")`` and ``surrendered`` is ``True`` — the honest degraded
    answer, and the flag :func:`run` turns into a
    ``post_kill_output_surrendered`` warning so the LOSS RIDES THE
    RESULT, not just the daemon log. The alternative is the defect this
    replaces: an orphaned pipe holder means EOF never comes, so the await
    never returns and the stage has no ceiling at all.
    """
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_POST_KILL_READ_SECONDS
        )
        return stdout_b, stderr_b, False
    except asyncio.TimeoutError:
        logger.warning(
            "guardkit adapter: post-kill read of pid=%s did not reach EOF "
            "within %.1fs — a pipe holder outlived the process group kill. "
            "SURRENDERING stdout/stderr as EMPTY: the timeout result loses "
            "its output tails, and the parser sees nothing to parse. A pipe "
            "holder this read could not outlast may still be RUNNING — the "
            "group kill reaches the child's group, not a process that "
            "escaped into a session of its own",
            getattr(proc, "pid", "?"),
            _POST_KILL_READ_SECONDS,
        )
        return b"", b"", True


async def _execute_subprocess(
    *,
    command: list[str],
    cwd: str,
    timeout: int,
) -> tuple[str, str, int, float, bool, bool]:
    """Execute a command via :func:`asyncio.create_subprocess_exec`.

    This is the **stubbable seam** standing in for DeepAgents' ``execute``
    tool. Tests monkeypatch this function to feed deterministic outputs
    through :func:`run` without ever spawning a real process. Production
    wiring may swap in a DeepAgents-backed executor with the same
    signature.

    The command is passed as a *list* (never a shell string) — this is
    the contract ``run()`` enforces for shell-injection safety.

    Returns ``(stdout, stderr, exit_code, duration_secs, timed_out,
    output_surrendered)``. ``run()`` unpacks the trailing flag
    *tolerantly*, so a stub that answers the historical 5-tuple still
    works and reads as ``output_surrendered=False`` — which is the truth
    for any stub: a seam that kills nothing surrenders nothing.

    The kill path (LI stage-2 design §1)
    ------------------------------------

    The spawn takes ``start_new_session=True`` so the child leads its own
    process group, and every kill signals the GROUP. A ``guardkit`` leg is
    not a leaf process — it starts a harness and a test runner — and the
    old ``terminate()``/``kill()`` pair reached only the child, leaving
    working grandchildren behind (POSIX; the estate's daemon is Linux).
    The post-kill read is then bounded at
    :data:`_POST_KILL_READ_SECONDS`, because a pipe holder that outlives
    the kill means EOF never comes.
    """
    started_at = time.monotonic()
    output_surrendered = False
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # The child LEADS its own process group — the precondition that
        # makes the group kill below safe (it can never reach the forge
        # daemon) and necessary (nothing else will reap the leg's own
        # children).
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        timed_out = False
    except asyncio.TimeoutError:
        # Terminate the GROUP; escalate to SIGKILL after a grace window.
        _signal_process_group(proc, signal.SIGTERM)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_KILL_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            _signal_process_group(proc, signal.SIGKILL)
            stdout_b, stderr_b, output_surrendered = (
                await _bounded_post_kill_read(proc)
            )
        timed_out = True
    except asyncio.CancelledError:
        # Caller cancelled — tear the group down and re-raise. The same
        # group law applies: with the child in a session of its own,
        # signalling the child alone would strand every grandchild.
        _signal_process_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            _signal_process_group(proc, signal.SIGKILL)
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
        output_surrendered,
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

        # The binary the spawn will use. Resolved once per process, lazily,
        # AFTER the cwd guards (a refused cwd never needs a binary) and
        # BEFORE any resolver work (a missing binary must not pay for
        # context resolution it will never use).
        guardkit_binary, searched = _resolve_guardkit_binary()
        if guardkit_binary is None:
            return _binary_not_found_result(
                subcommand=subcommand,
                duration_secs=time.monotonic() - started_at,
                searched=searched,
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
            guardkit_binary,
            subcommand,
            *args,
            *context_flags,
            *nats_flag,
        ]

        try:
            # Tolerant unpack: the production seam answers a 6-tuple whose
            # last element is the post-kill surrender flag; a stub that
            # answers the historical 5-tuple reads as "nothing surrendered",
            # which is the truth for a seam that kills nothing.
            (
                stdout,
                stderr,
                exit_code,
                duration,
                timed_out,
                *_surrender,
            ) = await _execute_subprocess(
                command=command,
                cwd=str(resolved_repo),
                timeout=timeout_seconds,
            )
            output_surrendered = bool(_surrender[0]) if _surrender else False
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
                        "binary": guardkit_binary,
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

        if output_surrendered:
            # The log is NOT the only channel, and pretending it was is
            # how a leg that lost 30 minutes of output became
            # byte-indistinguishable from a leg that produced none. This
            # warning rides the result, and the dispatcher threads every
            # warning verbatim into the stage's rationale
            # (``pipeline/dispatchers/subprocess.py``), so the stage_log
            # row and the receipt both NAME the loss.
            warnings.append(
                GuardKitWarning(
                    code="post_kill_output_surrendered",
                    message=(
                        f"stdout/stderr were SURRENDERED as empty: the "
                        f"post-kill read did not reach EOF within "
                        f"{_POST_KILL_READ_SECONDS:.0f}s after the timeout "
                        f"kill. A pipe holder outlived the process-group "
                        f"kill (a grandchild in a session of its own) and "
                        f"may still be running. The tails below are EMPTY "
                        f"because they were LOST, not because the leg was "
                        f"silent"
                    ),
                    details={
                        "subcommand": subcommand,
                        "post_kill_read_seconds": _POST_KILL_READ_SECONDS,
                        "timeout_seconds": timeout_seconds,
                    },
                )
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


def _binary_not_found_result(
    *,
    subcommand: str,
    duration_secs: float,
    searched: list[str],
) -> GuardKitResult:
    """Build a ``status="failed"`` result for an unresolvable binary.

    The same honest-dispatch-failure shape every other refusal on this
    boundary uses (never raises, ``exit_code=-1``, a structured warning), and
    it **names every rung that was tried** so the operator is told what to
    fix rather than being handed a bare ``FileNotFoundError`` from the spawn.
    """
    detail = (
        f"guardkit binary not found — searched: {'; '.join(searched)}. "
        f"Set {GUARDKIT_PATH_ENV} to the executable, put it on PATH, or "
        f"install the launcher at {_AGENTECFLOW_GUARDKIT}."
    )
    return GuardKitResult(
        status="failed",
        subcommand=subcommand,
        duration_secs=duration_secs,
        stdout_tail="",
        stderr=detail,
        exit_code=-1,
        warnings=[
            GuardKitWarning(
                code="guardkit_binary_not_found",
                message=detail,
                details={
                    "searched": list(searched),
                    "env_var": GUARDKIT_PATH_ENV,
                    "binary_name": _GUARDKIT_BINARY_NAME,
                    "fallback": _AGENTECFLOW_GUARDKIT,
                },
            )
        ],
    )


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
