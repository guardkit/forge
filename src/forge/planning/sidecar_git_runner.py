"""The planning chain's git, made where the repository lives (sandbox first,
2026-09-07, rules 70 and 71).

Rich's rule: nothing the factory runs on a repository runs on the host. The
planning chain's commits used to be made by :class:`WorktreeGitRunner` in a
worktree of the operator's checkout, inside the forge container, with the
plan stage's pre-commit checks run as a Python closure beside them. For a
repository that has a sandbox, this module's :class:`SidecarGitRunner` makes
the same commits over HTTP against the deploy sidecar running inside that
sandbox (``POST /git/prepare-branch-and-write-tree``, ``/git/read-file-from-
branch``, ``/git/rev-parse``, ``/git/remote-start-point``), on the factory's
own clone.

Two things differ from the in-container runner, and both are said out loud:

* it cannot run a Python closure. A caller that passes one gets a failed
  result carrying the sentence "a sandbox git runner cannot run a Python
  closure; declare the checks" — the checks are declared instead
  (:class:`~forge.planning.handoff.PreCommitChecks`) and the sidecar runs them
  with the guardkit beside it. ``supports_declared_checks()`` is how the
  driver finds out which form a runner takes.
* the answer carries the checks' outcomes (:class:`SidecarGitOpResult`), so
  the driver reads them through the parsers it always used.

:class:`RepoRoutedGitRunner` is what the composition hands the driver when
any repository has a sandbox: every call routes by the repository path the
protocol already carries — a sandboxed repository's calls go to its sidecar,
every other repository's to the in-container runner, byte for byte as today.

What is moved, said plainly (rule 87, 2026-09-07). EVERY planning leg that
writes to the branch now declares its checks: the spec leg's gherkin
normalizer and its provability check, the plan leg's stamp normalizer and
``guardkit feature validate``, the pass-bar leg's ``qa validate pass-bar``
(once per minted bar) and the feature-gate leg's ``qa validate
gate-registry``. Those six names are the closed list the sidecar runs. A
Python closure is still refused here with the sentence below rather than
quietly run on the host — that is the fence, not a gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import Field

from forge.adapters.git.models import GitOpResult
from forge.deploy.candidate_tree import FileAtCommit, RemoteStartPoint
from forge.planning.handoff import (
    PreCommitCheckOutcome,
    PreCommitChecks,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSURE_REFUSED_SENTENCE",
    "RepoRoutedGitRunner",
    "SidecarGitOpResult",
    "SidecarGitRunner",
]

#: The sentence a sandbox runner answers a Python closure with. The lane's
#: own words (rule 70): the checks are declared, never run on the host.
CLOSURE_REFUSED_SENTENCE: str = (
    "a sandbox git runner cannot run a Python closure; declare the checks"
)

_TREE_OPERATION = "prepare_branch_and_write_tree"
_SINGLE_OPERATION = "prepare_branch_and_write"

#: How long one write may take end to end: the sidecar's ceiling on the
#: checks (fifteen minutes) plus room for the worktree and the commit.
_DEFAULT_WRITE_TIMEOUT_S: float = 960.0

#: How long a read or a rev-parse may take.
_DEFAULT_READ_TIMEOUT_S: float = 60.0


class SidecarGitOpResult(GitOpResult):
    """A :class:`GitOpResult` that also carries the declared checks' outcomes
    and the sidecar's plain-sentence ``detail`` (its ``stderr`` field holds
    the same sentence, so every existing reader of a failed result works)."""

    checks: list[PreCommitCheckOutcome] = Field(default_factory=list)
    detail: str = ""


#: ``(url, body, timeout) -> (http_status, decoded_json)`` — the one HTTP seam,
#: injectable so a test can stand in for the wire without a socket.
HttpPost = Callable[[str, dict[str, Any], float], tuple[int, Any]]


def _urllib_post(url: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    """POST ``body`` as JSON with the standard library; return the status and
    the decoded answer (an HTTP error's body is decoded too — the sidecar's
    refusals are JSON with one plain ``error`` sentence)."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — loopback sidecar
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded = {"error": raw.decode("utf-8", errors="replace")[:2000]}
    return status, decoded


class SidecarGitRunner:
    """The :class:`~forge.planning.handoff.GitRunner` protocol over the
    sandbox sidecar's git routes, for ONE repository.

    Args:
        base_url: the sidecar's address (``http://127.0.0.1:8225``).
        repo: the repository's ``org/name`` key — the sidecar resolves it
            against its own ``planning.target_repo_paths`` (the clone's path
            inside the sandbox), so the ``repo_path`` every protocol call
            carries is recorded but never sent as a path to act on.
        post: the HTTP seam (tests inject a fake; production uses urllib).
        write_timeout_s / read_timeout_s: the wire's own time limits.
    """

    def __init__(
        self,
        base_url: str,
        *,
        repo: str,
        post: HttpPost = _urllib_post,
        write_timeout_s: float = _DEFAULT_WRITE_TIMEOUT_S,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        if not repo or not repo.strip():
            raise ValueError("SidecarGitRunner needs the repository's org/name key")
        self._base_url = base_url.rstrip("/")
        self._repo = repo
        self._post = post
        self._write_timeout_s = write_timeout_s
        self._read_timeout_s = read_timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def repo(self) -> str:
        return self._repo

    def supports_declared_checks(self) -> bool:
        """This runner takes the declared form, never a closure."""
        return True

    # -- the wire ----------------------------------------------------------

    async def _call(
        self, route: str, body: dict[str, Any], *, timeout: float
    ) -> tuple[int, Any] | Exception:
        """One POST, off the event loop (urllib blocks); an exception is
        returned rather than raised so every caller stays never-raising."""
        url = f"{self._base_url}{route}"
        try:
            return await asyncio.to_thread(self._post, url, body, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport boundary
            return exc

    @staticmethod
    def _transport_sentence(route: str, answer: Exception) -> str:
        return (
            f"the sandbox sidecar could not be reached for {route}: "
            f"{type(answer).__name__}: {answer}"
        )

    # -- the protocol --------------------------------------------------------

    async def fetch_remote_start_point(self, repo_path: str) -> RemoteStartPoint:
        """The starting rule's operation, run on the clone inside the sandbox.

        ``{repo}`` to ``/git/remote-start-point``; the answer is a branch and
        a commit, or one plain sentence. A sandbox that could not be reached
        is itself a refusal, so the driver never has to tell "no answer" from
        "no remote". Never raises.
        """
        answer = await self._call(
            "/git/remote-start-point",
            {"repo": self._repo},
            timeout=self._read_timeout_s,
        )
        if isinstance(answer, Exception):
            sentence = self._transport_sentence("/git/remote-start-point", answer)
            logger.error("fetch_remote_start_point: %s", sentence)
            return RemoteStartPoint(refusal=sentence)
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            sentence = self._refusal_sentence(status, decoded)
            logger.error("fetch_remote_start_point: %s", sentence)
            return RemoteStartPoint(refusal=sentence)
        start = RemoteStartPoint.from_wire(decoded)
        if not start.ok:
            logger.warning("fetch_remote_start_point: %s", start.refusal)
        return start

    async def read_file_at_commit(
        self, repo_path: str, commit: str, file_path: str
    ) -> FileAtCommit:
        """One file out of one commit, read on the clone inside the sandbox.

        The project's own memory (item 2, 2026-09-21): ``{repo, commit,
        file_path}`` to ``/git/read-file-at-commit``. A sandbox that could not
        be reached is a refusal in its own words, never "the project declares
        nothing" — the sentence a person is shown turns on that difference.
        Never raises.
        """
        answer = await self._call(
            "/git/read-file-at-commit",
            {"repo": self._repo, "commit": commit, "file_path": file_path},
            timeout=self._read_timeout_s,
        )
        if isinstance(answer, Exception):
            sentence = self._transport_sentence("/git/read-file-at-commit", answer)
            logger.error("read_file_at_commit: %s", sentence)
            return FileAtCommit(refusal=sentence)
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            sentence = self._refusal_sentence(status, decoded)
            logger.error("read_file_at_commit: %s", sentence)
            return FileAtCommit(refusal=sentence)
        read = FileAtCommit.from_wire(decoded)
        if not read.ok:
            logger.warning("read_file_at_commit: %s", read.refusal)
        return read

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
        *,
        start_commit: str | None = None,
    ) -> GitOpResult:
        """The single-file form: the tree route with one file and the same
        message the in-container runner writes."""
        result = await self.prepare_branch_and_write_tree(
            repo_path,
            branch,
            {file_path: content},
            f"planning: add {file_path} (Mode P planned handoff)",
            start_commit=start_commit,
        )
        return result.model_copy(update={"operation": _SINGLE_OPERATION})

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
        *,
        pre_commit: Any = None,
        start_commit: str | None = None,
        memory_project: str | None = None,
        launch_settings: Sequence[str] | None = None,
    ) -> SidecarGitOpResult:
        """Write ``files`` onto ``branch`` in one commit on the sandbox's clone,
        with the declared checks run there first.

        ``pre_commit`` is a :class:`PreCommitChecks` declaration or ``None``.
        A Python closure is refused with :data:`CLOSURE_REFUSED_SENTENCE` —
        a failed result, never a raise, so the leg fails loudly in the
        driver's own words. Never raises.
        """
        if pre_commit is not None and not isinstance(pre_commit, PreCommitChecks):
            logger.error(
                "%s: %s (repo=%s, branch=%s)",
                _TREE_OPERATION,
                CLOSURE_REFUSED_SENTENCE,
                self._repo,
                branch,
            )
            return SidecarGitOpResult(
                status="failed",
                operation=_TREE_OPERATION,
                stderr=CLOSURE_REFUSED_SENTENCE,
                detail=CLOSURE_REFUSED_SENTENCE,
                exit_code=-1,
            )
        body: dict[str, Any] = {
            "repo": self._repo,
            "branch": branch,
            "files": {str(k): str(v) for k, v in files.items()},
            "message": message,
            "checks": pre_commit.to_wire() if pre_commit is not None else [],
        }
        if start_commit:
            # The named starting point (one true copy, item 1): the sandbox
            # cuts a brand new branch from this commit, and leaves a branch
            # that already exists exactly where it is.
            body["start_commit"] = str(start_commit)
        # WHAT THE DECLARED CHECKS ARE LAUNCHED WITH (22 September 2026). The
        # checks declared above ARE the build system, run inside the sandbox,
        # so they are launched the way every other call of it is: the memory
        # this run belongs to, and the NAMES the project itself declared its
        # builds and checks need. Both were read out of the project's own
        # settings file at the commit the work started from and written onto
        # the run; names only, never values.
        if memory_project:
            body["memory_project"] = str(memory_project)
        if launch_settings:
            body["launch_settings"] = [str(name) for name in launch_settings]
        if body.get("memory_project") or body.get("launch_settings"):
            # AND THIS DOOR CITES NO BUILD RECORD, AND SAYS SO (23 September
            # 2026, the eighth review). The helper refuses a request that asks
            # for a project's declarations to be read and says nothing about
            # whose work it is, because that is also what a request whose
            # coordinator stamp had been dropped looks like. THIS door never
            # had a stamp to drop: planning runs before there is a build to
            # name, so there is no record to bind to and nothing is claiming
            # one. It asks to be read at the committed HEAD of the copy the
            # helper has, in as many words. The one door that IS stamped — the
            # deploy stage's — says nothing here, so a stamp dropped there
            # still goes red.
            body["by_hand"] = True
        logger.info(
            "%s: %d file(s) onto %s for %s via %s (%d declared check(s); "
            "repo_path %s is the sandbox's to resolve)",
            _TREE_OPERATION,
            len(files),
            branch,
            self._repo,
            self._base_url,
            len(body["checks"]),
            repo_path,
        )
        answer = await self._call(
            "/git/prepare-branch-and-write-tree", body, timeout=self._write_timeout_s
        )
        if isinstance(answer, Exception):
            sentence = self._transport_sentence("/git/prepare-branch-and-write-tree", answer)
            logger.error("%s: %s", _TREE_OPERATION, sentence)
            return SidecarGitOpResult(
                status="failed",
                operation=_TREE_OPERATION,
                stderr=sentence,
                detail=sentence,
                exit_code=-1,
            )
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            sentence = self._refusal_sentence(status, decoded)
            logger.error("%s: %s", _TREE_OPERATION, sentence)
            return SidecarGitOpResult(
                status="failed",
                operation=_TREE_OPERATION,
                stderr=sentence,
                detail=sentence,
                exit_code=-1,
                checks=self._checks_of(decoded),
            )
        checks = self._checks_of(decoded)
        detail = str(decoded.get("detail") or "")
        if decoded.get("status") == "success":
            sha = decoded.get("sha")
            return SidecarGitOpResult(
                status="success",
                operation=_TREE_OPERATION,
                sha=str(sha) if sha else None,
                exit_code=0,
                checks=checks,
                detail=detail,
            )
        return SidecarGitOpResult(
            status="failed",
            operation=_TREE_OPERATION,
            stderr=detail or "the sandbox sidecar reported the write as failed",
            detail=detail,
            exit_code=-1,
            checks=checks,
        )

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        """The content of ``file_path`` on ``branch`` in the sandbox's clone,
        or ``None`` (absent, refused, or unreachable — logged). Never raises."""
        answer = await self._call(
            "/git/read-file-from-branch",
            {"repo": self._repo, "branch": branch, "file_path": file_path},
            timeout=self._read_timeout_s,
        )
        if isinstance(answer, Exception):
            logger.error(
                "read_file_from_branch: %s",
                self._transport_sentence("/git/read-file-from-branch", answer),
            )
            return None
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            logger.error("read_file_from_branch: %s", self._refusal_sentence(status, decoded))
            return None
        content = decoded.get("content")
        return content if isinstance(content, str) else None

    async def rev_parse(self, repo_path: str, ref: str) -> str | None:
        """The commit ``ref`` names in the sandbox's clone, or ``None``."""
        answer = await self._call(
            "/git/rev-parse", {"repo": self._repo, "ref": ref}, timeout=self._read_timeout_s
        )
        if isinstance(answer, Exception):
            logger.error("rev_parse: %s", self._transport_sentence("/git/rev-parse", answer))
            return None
        status, decoded = answer
        if status != 200 or not isinstance(decoded, dict):
            logger.error("rev_parse: %s", self._refusal_sentence(status, decoded))
            return None
        sha = decoded.get("sha")
        return str(sha) if isinstance(sha, str) and sha else None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _checks_of(decoded: Any) -> list[PreCommitCheckOutcome]:
        raw = decoded.get("checks") if isinstance(decoded, dict) else None
        if not isinstance(raw, list):
            return []
        return [PreCommitCheckOutcome.from_wire(item) for item in raw if isinstance(item, dict)]

    def _refusal_sentence(self, status: int, decoded: Any) -> str:
        error = decoded.get("error") if isinstance(decoded, dict) else None
        return (
            f"the sandbox sidecar at {self._base_url} answered {status}: "
            f"{error or decoded!s}"
        )


class RepoRoutedGitRunner:
    """One runner for the driver, routing every call by the repository path
    the protocol already carries (rule 71).

    The composition builds it from the repository map: each sandboxed
    repository's path maps to its :class:`SidecarGitRunner`; any other path
    goes to ``default`` (the in-container :class:`WorktreeGitRunner`), so a
    repository without a sandbox behaves exactly as today. ``runner_for``
    answers by the ``org/name`` key, which is how the plan leg asks whether
    its runner takes declared checks.
    """

    def __init__(
        self,
        *,
        runners_by_repo: Mapping[str, Any],
        repo_paths: Mapping[str, str],
        default: Any,
    ) -> None:
        self._by_repo: dict[str, Any] = dict(runners_by_repo)
        self._default = default
        self._by_path: dict[str, Any] = {}
        for repo, runner in self._by_repo.items():
            path = repo_paths.get(repo)
            if path:
                self._by_path[os.path.normpath(path)] = runner

    @property
    def default(self) -> Any:
        return self._default

    def runner_for(self, target_repo: str) -> Any:
        """The runner for ``org/name`` — its sidecar's, else the default."""
        return self._by_repo.get(target_repo, self._default)

    def runner_for_path(self, repo_path: str) -> Any:
        return self._by_path.get(os.path.normpath(str(repo_path)), self._default)

    def supports_declared_checks(self) -> bool:
        """Only a per-repository answer is meaningful: ask ``runner_for``."""
        return False

    async def fetch_remote_start_point(self, repo_path: str) -> RemoteStartPoint:
        """The starting rule's operation, routed exactly as the others are."""
        return await self.runner_for_path(repo_path).fetch_remote_start_point(
            repo_path
        )

    async def read_file_at_commit(
        self, repo_path: str, commit: str, file_path: str
    ) -> FileAtCommit:
        """One file out of one commit, routed exactly as the others are."""
        return await self.runner_for_path(repo_path).read_file_at_commit(
            repo_path, commit, file_path
        )

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
        *,
        start_commit: str | None = None,
    ) -> GitOpResult:
        return await self.runner_for_path(repo_path).prepare_branch_and_write(
            repo_path, branch, file_path, content, start_commit=start_commit
        )

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
        *,
        pre_commit: Any = None,
        start_commit: str | None = None,
        memory_project: str | None = None,
        launch_settings: Sequence[str] | None = None,
    ) -> GitOpResult:
        return await self.runner_for_path(repo_path).prepare_branch_and_write_tree(
            repo_path,
            branch,
            files,
            message,
            pre_commit=pre_commit,
            start_commit=start_commit,
            memory_project=memory_project,
            launch_settings=launch_settings,
        )

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        return await self.runner_for_path(repo_path).read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=file_path
        )
