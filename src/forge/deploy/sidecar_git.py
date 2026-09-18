"""The merge press's git, spoken to the sidecar inside a repository's sandbox
(sandbox first, 2026-09-07, rule 89).

Rich's rule: nothing the factory runs on a repository runs on the host. For a
repository named in ``planning.sandboxes`` the build's branch, the clone it is
on and the Docker engine the candidate comes up in are all inside that
sandbox, so the merge press's own git has to happen in there too — otherwise
the press looks for the branch in the copy of the repository on this side, and
does not find it.

This is the same five operations
:class:`~forge.deploy.candidate_tree.InContainerCandidateGit` performs, said
over HTTP to that sandbox's deploy sidecar:

===============================  =========================================
operation                        route
===============================  =========================================
``rev_parse``                    ``POST /git/rev-parse``
``is_ancestor``                  ``POST /git/is-ancestor``
``ensure_candidate_trees_...``   (none — the lay-out route does it)
``materialise_candidate_tree``   ``POST /git/candidate-tree``
``remove_candidate_tree``        ``POST /git/candidate-tree-remove``
===============================  =========================================

Every request names the repository by its ``org/name`` key and never by a
path: the sidecar resolves the key against its own repository map, which is
the clone's path inside the sandbox, so nothing on this side can send it
somewhere else. The transport is the planning chain's own
(:func:`forge.planning.sidecar_git_runner._urllib_post`), reused rather than
written twice.

Nothing here raises except where the press already expects a raise — a
lay-out that fails is a :class:`~forge.deploy.candidate_tree.
CandidateTreeError`, exactly as it is when the lay-out happens here. Every
other failure is an honest ``None`` or ``False`` with the sidecar's own
sentence in the log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from forge.deploy.candidate_tree import CandidateTreeError, CandidateTreeLayout
from forge.planning.sidecar_git_runner import HttpPost, _urllib_post

logger = logging.getLogger(__name__)

__all__ = ["SidecarCandidateGit"]

#: How long a read (a commit, an ancestry question, a removal) may take.
_DEFAULT_READ_TIMEOUT_S: float = 60.0

#: How long laying a branch's tree out may take. A big repository's archive
#: is extracted file by file, so this is generous where the reads are not.
_DEFAULT_LAYOUT_TIMEOUT_S: float = 600.0


class SidecarCandidateGit:
    """The merge press's five git operations, run inside one repository's sandbox.

    Args:
        base_url: that sandbox's deploy sidecar (``http://127.0.0.1:8225``).
        repo: the repository's ``org/name`` key — what every request names.
        post: the HTTP seam (tests inject a fake; production uses urllib).
        read_timeout_s / layout_timeout_s: the wire's own time limits.
    """

    def __init__(
        self,
        base_url: str,
        *,
        repo: str,
        post: HttpPost = _urllib_post,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
        layout_timeout_s: float = _DEFAULT_LAYOUT_TIMEOUT_S,
    ) -> None:
        if not repo or not str(repo).strip():
            raise ValueError("SidecarCandidateGit needs the repository's org/name key")
        self._base_url = str(base_url).rstrip("/")
        self._repo = str(repo)
        self._post = post
        self._read_timeout_s = read_timeout_s
        self._layout_timeout_s = layout_timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def venue(self) -> str:
        """Where this surface does its work, for a sentence a person reads."""
        return f"in the sandbox that holds {self._repo}"

    # -- the wire ----------------------------------------------------------

    async def _call(
        self, route: str, body: dict[str, Any], *, timeout: float
    ) -> tuple[int, Any] | Exception:
        """One POST, off the event loop (urllib blocks); an exception comes
        back as a value so every caller stays never-raising."""
        url = f"{self._base_url}{route}"
        try:
            return await asyncio.to_thread(self._post, url, body, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport boundary
            return exc

    def _sentence(self, route: str, answer: Any) -> str:
        """One plain sentence for anything that was not a 200."""
        if isinstance(answer, Exception):
            return (
                f"the sandbox sidecar at {self._base_url} could not be reached "
                f"for {route}: {type(answer).__name__}: {answer}"
            )
        status, decoded = answer
        error = decoded.get("error") if isinstance(decoded, dict) else None
        return (
            f"the sandbox sidecar at {self._base_url} answered {status} for "
            f"{route}: {error or decoded!s}"
        )

    async def _ok(
        self, route: str, body: dict[str, Any], *, timeout: float
    ) -> tuple[dict[str, Any] | None, str | None]:
        """``(answer, None)`` on a 200 with a JSON object, else ``(None, why)``."""
        answer = await self._call(route, body, timeout=timeout)
        if isinstance(answer, Exception):
            return None, self._sentence(route, answer)
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            return None, self._sentence(route, answer)
        return decoded, None

    # -- the five operations -----------------------------------------------

    async def rev_parse(self, ref: str) -> str | None:
        """The commit (or tree) ``ref`` names in the sandbox's clone, or None."""
        decoded, why = await self._ok(
            "/git/rev-parse",
            {"repo": self._repo, "ref": str(ref)},
            timeout=self._read_timeout_s,
        )
        if decoded is None:
            logger.error("sandbox git: rev-parse %s: %s", ref, why)
            return None
        sha = decoded.get("sha")
        return str(sha) if isinstance(sha, str) and sha else None

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        """Is ``ancestor`` in ``descendant``? ``None`` = it could not be said."""
        decoded, why = await self._ok(
            "/git/is-ancestor",
            {
                "repo": self._repo,
                "ancestor": str(ancestor),
                "descendant": str(descendant),
            },
            timeout=self._read_timeout_s,
        )
        if decoded is None:
            logger.error(
                "sandbox git: is-ancestor %s %s: %s", ancestor, descendant, why
            )
            return None
        answer = decoded.get("is_ancestor")
        if answer is None:
            logger.warning(
                "sandbox git: %s",
                decoded.get("detail")
                or f"git could not say whether {ancestor} is in {descendant}",
            )
            return None
        return bool(answer)

    async def ensure_candidate_trees_excluded(self) -> bool | None:
        """Nothing to do on its own here.

        The sandbox lays the tree out and keeps it excluded in one act (the
        ``/git/candidate-tree`` route), and says in its answer whether it wrote
        the line, so asking separately would be a second round trip for a
        thing already done. ``None`` says "this venue answers that when it
        lays the tree out".
        """
        return None

    async def materialise_candidate_tree(
        self, feature_id: str, sha: str
    ) -> CandidateTreeLayout:
        """Lay ``sha``'s tree out in the sandbox's clone; raise on failure.

        The path comes back from the sandbox and is the path the tree has in
        there — which is the path the deploy stage and the live gate, both of
        which already run in that sandbox, are given as their working
        directory.
        """
        decoded, why = await self._ok(
            "/git/candidate-tree",
            {"repo": self._repo, "feature_id": str(feature_id), "sha": str(sha)},
            timeout=self._layout_timeout_s,
        )
        if decoded is None:
            raise CandidateTreeError(str(why))
        path = decoded.get("path")
        if not isinstance(path, str) or not path.strip():
            raise CandidateTreeError(
                f"the sandbox sidecar at {self._base_url} laid no tree out for "
                f"{feature_id}: its answer named no path"
            )
        tree = decoded.get("tree")
        excluded = decoded.get("exclude_written")
        return CandidateTreeLayout(
            path=path,
            tree=str(tree) if isinstance(tree, str) and tree else None,
            exclude_written=None if excluded is None else bool(excluded),
        )

    async def remove_candidate_tree(
        self, feature_id: str, path: str | None = None
    ) -> bool:
        """Remove the laid-out tree in the sandbox. Never raises.

        ``path`` is accepted for the surface's sake and deliberately not sent:
        the route derives the tree's place from the repository it is named,
        so no path from this side can reach git in there.
        """
        decoded, why = await self._ok(
            "/git/candidate-tree-remove",
            {"repo": self._repo, "feature_id": str(feature_id)},
            timeout=self._read_timeout_s,
        )
        if decoded is None:
            logger.warning(
                "sandbox git: the candidate tree for %s was not removed: %s",
                feature_id,
                why,
            )
            return False
        return bool(decoded.get("removed"))

    async def inspect_autobuild_worktree(
        self, build_id: str, path: str
    ) -> dict[str, Any]:
        decoded, why = await self._ok(
            "/git/autobuild-worktree-inspect",
            {"repo": self._repo, "build_id": str(build_id), "path": str(path)},
            timeout=self._read_timeout_s,
        )
        if decoded is None:
            return {
                "ok": False,
                "build_id": str(build_id),
                "path": str(path),
                "detail": str(why),
            }
        return decoded

    async def retire_autobuild_worktree(
        self, build_id: str, path: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        decoded, why = await self._ok(
            "/git/autobuild-worktree-retire",
            {
                "repo": self._repo,
                "build_id": str(build_id),
                "path": str(path),
                "expected": expected,
            },
            timeout=self._layout_timeout_s,
        )
        if decoded is None:
            return {
                "status": "kept",
                "build_id": str(build_id),
                "path": str(path),
                "detail": str(why),
            }
        return decoded
