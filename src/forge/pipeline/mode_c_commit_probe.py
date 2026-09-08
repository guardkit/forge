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

Where the counting happens (the thirteenth seam, 2026-09-08)
------------------------------------------------------------

For a repository that has a sandbox the journey worktree is cut inside
that sandbox (``forge.cli._conductor_worktree`` asks the sandbox's deploy
sidecar for it), so forge-prod cannot see the path at all: running git on
it here raises ``FileNotFoundError``, the journey is closed out
"mode-c-commit-check-failed", and a finished fix journey dies one step
short of its card. That is what happened on 2026-09-08.

So there are two probes with one contract, and a chooser between them:

* :func:`make_mode_c_commit_probe` — today's, one git subprocess in the
  worktree, for every repository that has no sandbox. Unchanged.
* :func:`make_sidecar_mode_c_commit_probe` — the same question asked of
  the sidecar inside the repository's sandbox
  (``POST /git/worktree-commit-count``), which runs the same two git
  commands where the worktree actually is.
* :func:`make_mode_c_commit_probe_chooser` — per build, reads the build
  row's repository and asks
  :func:`forge.config.sandboxes.sandbox_for`. With ``planning.sandboxes``
  empty — the default — the chooser IS today's probe, composed exactly as
  it was before this lane, with no per-build look-up at all.

Both probes fail with the same words for the same reasons: no build row,
no recorded worktree, a refusal, a timeout, an unreadable answer. The
handler turns any of them into the one terminal sentence below.

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

One exception, per build (Part L of the 2026-09-06 spec, added
2026-09-07, widened by Part M rule 56 the same day): a build queued on a
branch of its own — ``repair/<task id>``, or whatever ``forge queue --mode c
--branch`` named — has its journey tree cut from that branch, not from
``main`` (:func:`forge.cli._conductor_worktree.journey_base_ref`), because
the branch carries the repair's task file as a committed file. The probe
reads the row's ``branch`` through the same function and counts from it, so
the branch's own commits are never counted as a leg's work — a journey that
changed nothing must still end quietly, not be handed back as a fix. A row
whose branch is ``main`` (or names none) leaves the wiring-time base in
force.

References:
    - design pass §a.3 (`supervisor-revival-design-pass-2026-07-31`).
    - TASK-MBC8-007 — the ``has_commits`` flag's owner.
    - :mod:`forge.pipeline.terminal_handlers.mode_c` — the
      :data:`CommitProbe` contract this module implements.
    - :mod:`forge.adapters.git.operations` — the injectable subprocess
      primitive (list tokens, no shell, timeout + reap discipline).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from forge.adapters.git.operations import ExecuteCallable, _default_execute
from forge.lifecycle.persistence import Build
from forge.pipeline.terminal_handlers.mode_c import CommitProbe, CommitProbeResult

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BASE_BRANCH",
    "PROBE_TIMEOUT_SECONDS",
    "SIDECAR_PROBE_TIMEOUT_SECONDS",
    "make_mode_c_commit_probe",
    "make_mode_c_commit_probe_chooser",
    "make_sidecar_mode_c_commit_probe",
]


#: Default base ref for the ``<base>..HEAD`` range. Mirrors guardkit's
#: ``_detect_base_branch`` last-resort fallback and the estate's trunk.
DEFAULT_BASE_BRANCH: str = "main"

#: Wall-clock ceiling for the single git invocation. ``rev-list --count``
#: on a build-sized range is milliseconds; anything approaching this is a
#: hung or lock-contended repository, and a hung probe must surface as a
#: loud failure rather than stall a build's terminal resolution.
PROBE_TIMEOUT_SECONDS: float = 30.0

#: Wall-clock ceiling for the same question asked over the wire: the
#: sidecar's own git ceiling (thirty seconds) plus room for the loopback
#: round trip. An unreachable sidecar must surface as a loud failure, not
#: as a stalled build.
SIDECAR_PROBE_TIMEOUT_SECONDS: float = 60.0


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
        resolved = _resolve_range(
            pool, build, base=base, worktree_allowlist=worktree_allowlist
        )
        if isinstance(resolved, CommitProbeResult):
            return resolved
        build_id, worktree, range_base = resolved

        command = ["git", "rev-list", "--count", f"{range_base}..HEAD"]
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
            range_base,
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


def _resolve_range(
    pool: _BuildRowReader,
    build: Build,
    *,
    base: str,
    worktree_allowlist: _WorktreeAllowlist | None,
) -> "CommitProbeResult | tuple[str, str, str]":
    """The three facts both probes need, or the failure that stops them.

    Answers ``(build_id, worktree, range_base)`` — the build's recorded
    journey worktree and the left side of the ``<base>..HEAD`` range — or a
    failed :class:`CommitProbeResult` carrying the sentence for whichever of
    the four things went wrong: the row could not be read, there is no row,
    the row has no worktree recorded, or the allowlist denied the path. Both
    probes ask this one function, so the words a build fails with never
    depend on where the counting would have happened.
    """
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

    # The wiring-time base — unless the build was queued on a branch of its
    # own, whose journey tree is cut from that branch (module docstring,
    # "The base ref"; one rule with the conductor's writer, Part M rule 56):
    # counting from main there would count the branch's own commits as a
    # leg's work.
    from forge.cli._conductor_worktree import JOURNEY_BASE_REF, journey_base_ref

    row_branch = getattr(row, "branch", None)
    journey_base = journey_base_ref(row_branch)
    range_base = base if journey_base == JOURNEY_BASE_REF else journey_base
    return build_id, worktree, range_base


def make_sidecar_mode_c_commit_probe(
    pool: _BuildRowReader,
    *,
    sidecar_url: str,
    repo: str,
    base_branch: str = DEFAULT_BASE_BRANCH,
    worktree_allowlist: _WorktreeAllowlist | None = None,
    post: Any = None,
    timeout_seconds: float = SIDECAR_PROBE_TIMEOUT_SECONDS,
    sandbox_name: str = "?",
) -> CommitProbe:
    """The same probe, asked of the sidecar inside the repository's sandbox.

    For a sandboxed repository the journey worktree lives inside the sandbox
    and there is no such path on this side, so the counting is done there:
    one ``POST /git/worktree-commit-count`` with the repository's key, the
    tree's path and the base, and the sidecar runs the same two git commands
    where the tree actually is. The transport is the planning chain's own
    (:func:`forge.planning.sidecar_git_runner._urllib_post`), awaited off the
    event loop the way every other sidecar seam is.

    The failure words are the shared ones (see :func:`_resolve_range`) plus
    the three this form can add: the sidecar could not be reached, it refused,
    or it answered something that is not a count. Every one of them is
    ``failed=True`` — never a quiet zero, which would throw a real fix
    journey's work away.

    Args:
        pool: The lifecycle persistence facade (``get_build_row``).
        sidecar_url: The sandbox sidecar's address.
        repo: The repository's ``org/name`` key. The sidecar resolves it
            against its own map, so no path of ours is ever acted on blind.
        base_branch: Left side of the ``<base>..HEAD`` range, as today.
        worktree_allowlist: Optional FEAT-FORGE-005 allowlist, as today.
        post: ``(url, body, timeout) -> (status, decoded)`` — injected by
            tests; production uses the planning chain's urllib seam.
        timeout_seconds: Ceiling on the round trip.
        sandbox_name: The sandbox's name, for the log lines and the
            refusal sentences.
    """
    if not str(base_branch).strip():
        raise ValueError(
            "make_sidecar_mode_c_commit_probe: base_branch must be a "
            "non-empty string"
        )
    if not str(repo).strip():
        raise ValueError(
            "make_sidecar_mode_c_commit_probe: repo must be the "
            "repository's org/name key"
        )
    # One statement of the route's name, the sidecar's own (imported here
    # rather than at module import time: the sidecar module pulls the config
    # models in, and this module is imported by the daemon's composition).
    from forge.deploy_sidecar.service import GIT_WORKTREE_COMMIT_COUNT_ROUTE

    base = str(base_branch).strip()
    url = f"{str(sidecar_url).rstrip('/')}{GIT_WORKTREE_COMMIT_COUNT_ROUTE}"

    async def _probe(build: Build) -> CommitProbeResult:
        resolved = _resolve_range(
            pool, build, base=base, worktree_allowlist=worktree_allowlist
        )
        if isinstance(resolved, CommitProbeResult):
            return resolved
        build_id, worktree, range_base = resolved

        if post is not None:
            sender = post
        else:  # pragma: no cover - the production transport
            from forge.planning.sidecar_git_runner import _urllib_post

            sender = _urllib_post
        body = {"repo": str(repo), "path": worktree, "base": range_base}
        try:
            status, decoded = await asyncio.to_thread(
                sender, url, body, timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — probe boundary: never raise
            return _failed(
                build_id,
                f"{type(exc).__name__}: {exc} (asking the sidecar in sandbox "
                f"{sandbox_name} at {url} to count {range_base}..HEAD in "
                f"{worktree})",
            )

        answer = decoded if isinstance(decoded, dict) else {}
        if status != 200:
            sentence = str(answer.get("error") or "").strip() or f"HTTP {status}"
            return _failed(
                build_id,
                f"the sidecar in sandbox {sandbox_name} at {url} refused to "
                f"count {range_base}..HEAD for build_id={build_id!r}: "
                f"{sentence}",
            )

        raw = answer.get("count")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return _failed(
                build_id,
                f"the sidecar in sandbox {sandbox_name} answered "
                f"{raw!r} instead of a commit count for build_id={build_id!r}",
            )
        if raw < 0:  # pragma: no cover - git cannot emit this
            return _failed(
                build_id,
                f"the sidecar in sandbox {sandbox_name} answered a negative "
                f"count {raw}",
            )

        logger.debug(
            "mode_c_commit_probe (sandbox %s): build_id=%s range=%s..HEAD "
            "count=%d head=%s",
            sandbox_name,
            build_id,
            range_base,
            raw,
            answer.get("head"),
        )
        return CommitProbeResult(count=raw, failed=False)

    return _probe


def make_mode_c_commit_probe_chooser(
    pool: _BuildRowReader,
    *,
    config: Any,
    base_branch: str = DEFAULT_BASE_BRANCH,
    worktree_allowlist: _WorktreeAllowlist | None = None,
    post: Any = None,
    sidecar_probe_factory: Any = None,
) -> CommitProbe:
    """Return the probe to use, choosing per build where the counting happens.

    One daemon serves every repository and only some of them have a sandbox,
    so the choice is made per build rather than per boot: the build row names
    the repository, :func:`forge.config.sandboxes.sandbox_for` says whether it
    has a sandbox, and the answer picks the sidecar probe or today's.

    With ``planning.sandboxes`` empty — the default, and every estate that has
    not been given a sandbox — this returns today's probe itself, so the
    composition is byte for byte what it was before this lane and no row is
    read to decide anything.

    A row that cannot be read, or a build with no row at all, falls to today's
    probe, which is where the "no builds row" sentence lives: the fault is
    reported once, in the words it has always had.
    """
    from forge.config.sandboxes import has_sandboxes, sandbox_for

    in_container = make_mode_c_commit_probe(
        pool, base_branch=base_branch, worktree_allowlist=worktree_allowlist
    )
    if not has_sandboxes(config):
        return in_container

    probes: dict[str, CommitProbe] = {}

    def _build_for(repo: str, entry: Any) -> CommitProbe:
        if sidecar_probe_factory is not None:
            return sidecar_probe_factory(repo=repo, entry=entry)
        return make_sidecar_mode_c_commit_probe(
            pool,
            sidecar_url=str(entry.sidecar_url),
            repo=repo,
            base_branch=base_branch,
            worktree_allowlist=worktree_allowlist,
            post=post,
            sandbox_name=str(getattr(entry, "name", "?")),
        )

    async def _choose(build: Build) -> CommitProbeResult:
        build_id = getattr(build, "build_id", "") or ""
        try:
            row = pool.get_build_row(build_id)
        except Exception as exc:  # noqa: BLE001 — never break a terminal
            logger.warning(
                "mode_c_commit_probe: reading build_id=%s to decide where its "
                "commits are counted raised %s: %s — asking git here, as it "
                "always did",
                build_id,
                type(exc).__name__,
                exc,
            )
            return await in_container(build)
        repo = str(getattr(row, "repo", "") or "") if row is not None else ""
        entry = sandbox_for(config, repo)
        if entry is None:
            return await in_container(build)
        if repo not in probes:
            probes[repo] = _build_for(repo, entry)
            logger.info(
                "mode_c_commit_probe: %s has a sandbox (%s), so its fix "
                "journey's commits are counted inside it through the sidecar "
                "at %s — the journey worktree is in there, not here",
                repo,
                getattr(entry, "name", "?"),
                getattr(entry, "sidecar_url", "?"),
            )
        return await probes[repo](build)

    return _choose
