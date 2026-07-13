"""Production GitRunner for the Mode P planned-handoff terminal (TASK-MP-012).

Implements the :class:`forge.planning.handoff.GitRunner` Protocol —
``prepare_branch_and_write(repo_path, branch, file_path, content)`` — by
composing the existing :mod:`forge.adapters.git.operations` primitives
(one subprocess boundary, per the TASK-MP-012 architectural review):

1. **Idempotency probe (RT-08)**: if ``branch`` already exists and the
   file on that branch already holds ``content`` byte-identically, return
   success with the existing branch tip SHA and make ZERO further git
   mutations — re-executing the handoff after a crash between the branch
   commit and the run-record update must not duplicate the commit.
2. **Isolation**: all mutations happen in an ephemeral ``git worktree``
   under ``worktrees_root`` — the primary working copy's checkout is
   never touched (ASSUM-006). ``git worktree add -b`` creates the new
   branch from the repo's current HEAD; ``--force`` re-attaches to an
   existing branch left checked out by a crashed prior attempt.
3. **Checkout-collision guard (TASK-MP-013)**: before the ``--force``
   re-attach, ``git worktree list --porcelain`` is consulted; if the
   handoff branch is checked out in ANY live worktree of the target repo
   (including the main working copy), the re-attach is REFUSED with a
   failed result carrying the ``handoff-branch-checked-out`` marker —
   otherwise the handoff commit would silently advance the branch under
   an operator's live checkout. Worktrees whose directory no longer
   exists (or that git marks ``prunable``) are the orphaned crash
   leftovers ``--force`` exists for and do NOT block.
4. **Never raises** (ADR-ARCH-025): every failure becomes
   ``GitOpResult(status="failed", ...)``.

No push in v1 (ASSUM-006): the branch stays local to the target working
copy for the attended ``/feature-spec`` follow-up.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path

from forge.adapters.git.models import GitOpResult
from forge.adapters.git.operations import (
    ExecuteCallable,
    _default_execute,
    _exception_failure,
    _failure_stderr,
    commit_all,
)
from forge.planning.handoff import PreCommitHook

logger = logging.getLogger(__name__)

__all__ = ["WorktreeGitRunner"]

_OPERATION = "prepare_branch_and_write"
_TREE_OPERATION = "prepare_branch_and_write_tree"


class WorktreeGitRunner:
    """GitRunner backed by ephemeral ``git worktree`` isolation.

    Args:
        worktrees_root: Directory under which ephemeral worktrees are
            created. Defaults to ``<tempdir>/forge-planning-worktrees``.
        execute: Injectable subprocess primitive (tests inject a fake;
            production uses the shared list-token, no-shell default).
    """

    def __init__(
        self,
        *,
        worktrees_root: Path | None = None,
        execute: ExecuteCallable = _default_execute,
    ) -> None:
        self._worktrees_root = worktrees_root or (
            Path(tempfile.gettempdir()) / "forge-planning-worktrees"
        )
        self._execute = execute

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
    ) -> GitOpResult:
        """Prepare ``branch`` in an isolated worktree and commit ``file_path``.

        See module docstring for the idempotency / isolation contract.
        """
        try:
            repo = Path(repo_path)
            if not repo.is_dir():
                return GitOpResult(
                    status="failed",
                    operation=_OPERATION,
                    stderr=f"repo_path is not a directory: {repo_path}",
                    exit_code=-1,
                )

            branch_exists = await self._branch_exists(repo, branch)

            # RT-08 idempotency: identical content already on the branch →
            # success with the existing tip, zero mutations.
            if branch_exists:
                existing = await self._show_file(repo, branch, file_path)
                if existing is not None and existing == content:
                    sha = await self._rev_parse(repo, branch)
                    logger.info(
                        "prepare_branch_and_write: branch %s already carries "
                        "identical %s; idempotent success (sha=%s)",
                        branch,
                        file_path,
                        sha,
                    )
                    return GitOpResult(
                        status="success",
                        operation=_OPERATION,
                        sha=sha,
                        exit_code=0,
                    )

                # TASK-MP-013: refuse the --force re-attach when the branch
                # is checked out in any live worktree (main working copy
                # included) — the handoff commit would silently advance the
                # branch under the operator's checkout. Runs AFTER the
                # RT-08 idempotency probe: identical content means zero
                # mutations, so an idempotent re-handoff is never blocked.
                blocker = await self._blocking_checkout(repo, branch)
                if blocker is not None:
                    logger.error(
                        "handoff-branch-checked-out: refusing --force "
                        "re-attach of branch %s; blocking worktree: %s",
                        branch,
                        blocker,
                    )
                    return GitOpResult(
                        status="failed",
                        operation=_OPERATION,
                        stderr=(
                            f"handoff-branch-checked-out: branch {branch} is "
                            f"checked out at {blocker}; release or prune that "
                            "checkout, then retry the handoff"
                        ),
                        exit_code=-1,
                    )

            self._worktrees_root.mkdir(parents=True, exist_ok=True)
            worktree = self._worktrees_root / (
                f"{branch.replace('/', '-')}-{uuid.uuid4().hex[:8]}"
            )

            if branch_exists:
                # --force re-attaches a branch a crashed prior attempt may
                # have left checked out in an orphaned worktree.
                add_cmd = [
                    "git",
                    "worktree",
                    "add",
                    "--force",
                    str(worktree),
                    branch,
                ]
            else:
                add_cmd = ["git", "worktree", "add", "-b", branch, str(worktree)]

            add_res = await self._execute(command=add_cmd, cwd=str(repo))
            if add_res.exit_code != 0:
                return GitOpResult(
                    status="failed",
                    operation=_OPERATION,
                    stderr=_failure_stderr(add_res.stderr, add_res.stdout),
                    exit_code=add_res.exit_code,
                )

            try:
                # Defence-in-depth path containment (correlation_id is
                # already validated at the intake trust boundary, RT-03).
                target = (worktree / file_path).resolve()
                worktree_resolved = worktree.resolve()
                if not str(target).startswith(str(worktree_resolved) + os.sep):
                    return GitOpResult(
                        status="failed",
                        operation=_OPERATION,
                        stderr=f"file_path escapes the worktree: {file_path}",
                        exit_code=-1,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

                message = f"planning: add {file_path} (Mode P planned handoff)"
                commit_res = await commit_all(worktree, message, execute=self._execute)
                if commit_res.status == "failed":
                    stderr = commit_res.stderr or ""
                    if "nothing to commit" in stderr:
                        # Content byte-identical on disk (e.g. probe could
                        # not read the blob but the tree already matches) —
                        # idempotent success per RT-08.
                        sha = await self._rev_parse(repo, branch)
                        return GitOpResult(
                            status="success",
                            operation=_OPERATION,
                            sha=sha,
                            exit_code=0,
                        )
                    return GitOpResult(
                        status="failed",
                        operation=_OPERATION,
                        stderr=commit_res.stderr,
                        exit_code=commit_res.exit_code,
                    )

                return GitOpResult(
                    status="success",
                    operation=_OPERATION,
                    sha=commit_res.sha,
                    exit_code=0,
                )
            finally:
                # Best-effort: a failed cleanup is a warning, never a
                # handoff blocker. Runs from the SOURCE repo — unlike
                # operations.cleanup_worktree (cwd=worktree.parent), the
                # ephemeral planning worktrees root is not itself a git
                # directory, so the remove must anchor in the repo.
                await self._cleanup_worktree(repo, worktree)
        except Exception as exc:  # noqa: BLE001 — adapter boundary, ADR-ARCH-025
            logger.exception(
                "prepare_branch_and_write failed (branch=%s, file=%s)",
                branch,
                file_path,
            )
            return _exception_failure(_OPERATION, exc)

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
        *,
        pre_commit: PreCommitHook | None = None,
    ) -> GitOpResult:
        """Write a multi-file tree onto ``branch`` in one commit (Lane B B2).

        Additive sibling of :meth:`prepare_branch_and_write` — see the
        :class:`forge.planning.handoff.GitRunner` protocol docstring for the
        contract. Reuses the same worktree isolation, checkout-collision guard
        (TASK-MP-013), idempotency probe (RT-08) and best-effort cleanup; adds
        the multi-file write and the optional pre-commit oracle hook.
        """
        try:
            repo = Path(repo_path)
            if not repo.is_dir():
                return GitOpResult(
                    status="failed",
                    operation=_TREE_OPERATION,
                    stderr=f"repo_path is not a directory: {repo_path}",
                    exit_code=-1,
                )
            if not files:
                return GitOpResult(
                    status="failed",
                    operation=_TREE_OPERATION,
                    stderr="no files supplied to prepare_branch_and_write_tree",
                    exit_code=-1,
                )

            branch_exists = await self._branch_exists(repo, branch)

            # RT-08 idempotency: every file already byte-identical on the
            # branch AND no mutating hook required → success with the existing
            # tip, zero mutations. A pre_commit hook may mutate files (the
            # normalizer), so only short-circuit when there is no hook.
            if branch_exists and pre_commit is None:
                all_identical = True
                for rel_path, content in files.items():
                    existing = await self._show_file(repo, branch, rel_path)
                    if existing is None or existing != content:
                        all_identical = False
                        break
                if all_identical:
                    sha = await self._rev_parse(repo, branch)
                    logger.info(
                        "%s: branch %s already carries identical %d file(s); "
                        "idempotent success (sha=%s)",
                        _TREE_OPERATION,
                        branch,
                        len(files),
                        sha,
                    )
                    return GitOpResult(
                        status="success",
                        operation=_TREE_OPERATION,
                        sha=sha,
                        exit_code=0,
                    )

            if branch_exists:
                blocker = await self._blocking_checkout(repo, branch)
                if blocker is not None:
                    logger.error(
                        "handoff-branch-checked-out: refusing --force "
                        "re-attach of branch %s; blocking worktree: %s",
                        branch,
                        blocker,
                    )
                    return GitOpResult(
                        status="failed",
                        operation=_TREE_OPERATION,
                        stderr=(
                            f"handoff-branch-checked-out: branch {branch} is "
                            f"checked out at {blocker}; release or prune that "
                            "checkout, then retry"
                        ),
                        exit_code=-1,
                    )

            self._worktrees_root.mkdir(parents=True, exist_ok=True)
            worktree = self._worktrees_root / (
                f"{branch.replace('/', '-')}-{uuid.uuid4().hex[:8]}"
            )

            if branch_exists:
                add_cmd = [
                    "git",
                    "worktree",
                    "add",
                    "--force",
                    str(worktree),
                    branch,
                ]
            else:
                add_cmd = ["git", "worktree", "add", "-b", branch, str(worktree)]

            add_res = await self._execute(command=add_cmd, cwd=str(repo))
            if add_res.exit_code != 0:
                return GitOpResult(
                    status="failed",
                    operation=_TREE_OPERATION,
                    stderr=_failure_stderr(add_res.stderr, add_res.stdout),
                    exit_code=add_res.exit_code,
                )

            try:
                worktree_resolved = worktree.resolve()
                for rel_path, content in files.items():
                    target = (worktree / rel_path).resolve()
                    if not str(target).startswith(str(worktree_resolved) + os.sep):
                        return GitOpResult(
                            status="failed",
                            operation=_TREE_OPERATION,
                            stderr=f"file_path escapes the worktree: {rel_path}",
                            exit_code=-1,
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")

                # Pre-commit oracle (normalizer / feature validate). Runs
                # against the materialised worktree; a red oracle aborts the
                # commit with zero branch mutation.
                if pre_commit is not None:
                    hook_result = await pre_commit(worktree_resolved)
                    if not hook_result.ok:
                        return GitOpResult(
                            status="failed",
                            operation=_TREE_OPERATION,
                            stderr=(
                                "pre-commit oracle refused the commit: "
                                f"{hook_result.detail}"
                            ),
                            exit_code=-1,
                        )

                commit_res = await commit_all(worktree, message, execute=self._execute)
                if commit_res.status == "failed":
                    stderr = commit_res.stderr or ""
                    if "nothing to commit" in stderr:
                        # Tree already matched on disk (e.g. an idempotent
                        # re-run whose hook made no change) — RT-08 success.
                        sha = await self._rev_parse(repo, branch)
                        return GitOpResult(
                            status="success",
                            operation=_TREE_OPERATION,
                            sha=sha,
                            exit_code=0,
                        )
                    return GitOpResult(
                        status="failed",
                        operation=_TREE_OPERATION,
                        stderr=commit_res.stderr,
                        exit_code=commit_res.exit_code,
                    )

                return GitOpResult(
                    status="success",
                    operation=_TREE_OPERATION,
                    sha=commit_res.sha,
                    exit_code=0,
                )
            finally:
                await self._cleanup_worktree(repo, worktree)
        except Exception as exc:  # noqa: BLE001 — adapter boundary, ADR-ARCH-025
            logger.exception(
                "%s failed (branch=%s, files=%d)",
                _TREE_OPERATION,
                branch,
                len(files),
            )
            return _exception_failure(_TREE_OPERATION, exc)

    async def _cleanup_worktree(self, repo: Path, worktree: Path) -> None:
        """Best-effort worktree removal anchored in the source repo."""
        try:
            res = await self._execute(
                command=["git", "worktree", "remove", str(worktree), "--force"],
                cwd=str(repo),
            )
            if res.exit_code != 0:
                logger.warning(
                    "planning worktree cleanup non-zero exit (best-effort, "
                    "worktree=%s, exit=%d): %s",
                    worktree,
                    res.exit_code,
                    _failure_stderr(res.stderr, res.stdout),
                )
        except Exception:  # noqa: BLE001 — cleanup never blocks the handoff
            logger.exception(
                "planning worktree cleanup raised (best-effort, worktree=%s)",
                worktree,
            )

    # -- probes ---------------------------------------------------------- #

    async def _branch_exists(self, repo: Path, branch: str) -> bool:
        res = await self._execute(
            command=[
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            cwd=str(repo),
        )
        return res.exit_code == 0

    async def _blocking_checkout(self, repo: Path, branch: str) -> str | None:
        """Path of a live worktree holding ``branch`` checked out, or None.

        Parses ``git worktree list --porcelain`` (blank-line-separated
        entries: ``worktree <path>``, optional ``branch refs/heads/<name>``,
        optional ``prunable ...``). Entries whose directory is gone or that
        git marks prunable are orphaned crash leftovers and do not block
        (TASK-MP-013 AC4). Fails safe: if the porcelain command itself
        fails, returns a descriptive marker so the caller refuses the
        ``--force`` re-attach rather than mutating an unverifiable branch.
        """
        res = await self._execute(
            command=["git", "worktree", "list", "--porcelain"],
            cwd=str(repo),
        )
        if res.exit_code != 0:
            return (
                "<unverifiable: 'git worktree list --porcelain' exited "
                f"{res.exit_code}: {_failure_stderr(res.stderr, res.stdout)}>"
            )

        branch_ref = f"refs/heads/{branch}"
        path: str | None = None
        holds_branch = False
        prunable = False
        # Trailing sentinel flushes the final entry (porcelain output may
        # or may not end with a blank line).
        for line in [*(res.stdout or "").splitlines(), ""]:
            if not line.strip():
                if path and holds_branch and not prunable and Path(path).is_dir():
                    return path
                path, holds_branch, prunable = None, False, False
                continue
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line.strip() == f"branch {branch_ref}":
                holds_branch = True
            elif line.startswith("prunable"):
                prunable = True
        return None

    async def _show_file(self, repo: Path, branch: str, file_path: str) -> str | None:
        res = await self._execute(
            command=["git", "show", f"{branch}:{file_path}"],
            cwd=str(repo),
        )
        if res.exit_code != 0:
            return None
        return res.stdout

    async def _rev_parse(self, repo: Path, branch: str) -> str | None:
        res = await self._execute(
            command=["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=str(repo),
        )
        if res.exit_code != 0:
            return None
        return res.stdout.strip() or None
