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
from forge.deploy.candidate_tree import (
    FileAtCommit,
    RemoteStartPoint,
    answered_as_ordinary,
    answered_as_raw,
    answered_for_branch,
)
from forge.planning.handoff import (
    PreCommitCheckOutcome,
    PreCommitChecks,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSURE_REFUSED_SENTENCE",
    "RepoRoutedGitRunner",
    "FACT_GATHERING_BUDGET_S",
    "PartialPlaces",
    "SidecarCodeReader",
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


#: How long one read of the code door may take. ABOVE the helper's own walls,
#: so the two never race and a slow answer is the helper's own "timed out"
#: rather than a socket giving up first: its git listing may take up to 60
#: seconds and a search walks for up to 30 more (``CODE_GIT_TIMEOUT_SECONDS``,
#: ``CODE_SEARCH_TIMEOUT_SECONDS``). Never longer than what is left of
#: :data:`FACT_GATHERING_BUDGET_S`.
_DEFAULT_CODE_READ_TIMEOUT_S: float = 100.0

#: The whole of one fact-gathering pass — the fact sheet, or the plan-writer's
#: repository description — may spend at most this long on the helper. Two
#: minutes: a healthy pass on api_test takes a fraction of a second, and the
#: planner must say "could not read" well before a person wonders why
#: planning has stalled (a hung helper used to cost five 100-second waits).
FACT_GATHERING_BUDGET_S: float = 120.0

#: How many matching lines one search asks for — the helper's own ceiling.
_SEARCH_MAX_RESULTS: int = 200

#: The helper searches only this much of any one line
#: (``CODE_SEARCH_MAX_LINE_CHARS``); a line past it is "partly searched".
_HELPER_LINE_CHARS: int = 500

#: How many further requests one search may spend recovering what the
#: helper's caps left out, before the rest is reported as not searched.
_MAX_RECOVERY_REQUESTS: int = 24


class PartialPlaces(list):  # type: ignore[type-arg]
    """``path:line`` places, with :attr:`cut` set to a plain sentence when
    the search could not be completed — what WAS found is kept."""

    cut: str | None = None


class SidecarCodeReader:
    """The planner's reading of a sandboxed repository through the helper's
    read-only code door (``/code/list-files``, ``/code/search``,
    ``/code/read-file``), on the factory's own clone inside the sandbox.

    A :class:`~forge.planning.repository_facts.RepositoryReader`. It rides
    the same address, repository key and HTTP seam as the
    :class:`SidecarGitRunner` that builds it (the 1 October planner fix, 1 October
    2026).

    NEVER A PARTIAL ANSWER AS A WHOLE ONE, NEVER WHAT WAS READ THROWN AWAY.
    The helper cuts a search at 200 matching lines and 30 seconds, searches
    only the first 500 characters of a line, and cuts a listing at 5,000
    files, and says so (``capped``, ``timed_out``,
    ``long_lines_partly_searched``). Every cut is either recovered or said:

    * a capped search is covered again piece by piece, by the helper's own
      walk order (full paths, sorted): every folder not proven complete is
      searched again by itself, and every file not proven complete — the one
      the cap fell inside included, even at the top of the repository — is
      read whole and searched here;
    * lines past 500 characters are found with one search for long lines,
      and those files are read whole and searched here;
    * whatever cannot be recovered within :data:`_MAX_RECOVERY_REQUESTS` is
      a plain sentence: in :attr:`cuts` for the fact sheet's notes, and on
      the :class:`PartialPlaces` answer of :meth:`places_mentioning`.

    A helper that fails on the wire once is unavailable for the rest of this
    reader's life (one planning run) and every later read says so at once;
    each pass started with :meth:`begin` may spend at most
    :data:`FACT_GATHERING_BUDGET_S`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        repo: str,
        post: HttpPost = _urllib_post,
        timeout_s: float = _DEFAULT_CODE_READ_TIMEOUT_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self._base_url = base_url.rstrip("/")
        self._repo = repo
        self._post = post
        self._timeout_s = timeout_s
        self._clock = clock or time.monotonic
        self.where = f"{repo} through the sandbox helper at {self._base_url}"
        #: Plain sentences, one per answer that came back incomplete.
        self.cuts: list[str] = []
        #: The sentence when the listing itself was cut, else ``None``.
        self.listing_cut: str | None = None
        #: Why each file the helper would not serve was refused, by path.
        self.refused: dict[str, str] = {}
        self._listing: list[str] | None = None
        self._dead: str | None = None
        self._deadline: float | None = None
        self._long_line_files: tuple[list[str], str | None] | None = None

    def begin(self, budget_s: float = FACT_GATHERING_BUDGET_S) -> None:
        """Start one fact-gathering pass with its own time budget. A helper
        already found unreachable stays so."""
        self._deadline = self._clock() + budget_s

    # -- the wire ----------------------------------------------------------

    def _answer(self, route: str, body: dict[str, Any]) -> tuple[int, Any]:
        from forge.planning.repository_facts import RepositoryUnreadable

        if self._dead is not None:
            raise RepositoryUnreadable(self._dead)
        timeout = self._timeout_s
        if self._deadline is not None:
            left = self._deadline - self._clock()
            if left <= 0:
                raise RepositoryUnreadable(
                    f"the planner's {int(FACT_GATHERING_BUDGET_S)}-second allowance "
                    f"for reading the repository through the sandbox helper at "
                    f"{self._base_url} ran out"
                )
            timeout = min(timeout, left)
        url = f"{self._base_url}{route}"
        try:
            return self._post(url, {"repo": self._repo, **body}, timeout)
        except Exception as exc:  # noqa: BLE001 — transport boundary
            self._dead = (
                f"the sandbox helper at {self._base_url} could not be reached "
                f"for {route} ({type(exc).__name__}: {str(exc)[:160]})"
            )
            raise RepositoryUnreadable(self._dead) from exc

    def _refused(self, route: str, status: int, decoded: Any) -> Exception:
        from forge.planning.repository_facts import RepositoryUnreadable

        error = decoded.get("error") if isinstance(decoded, dict) else decoded
        return RepositoryUnreadable(
            f"the sandbox helper at {self._base_url} answered {status} to "
            f"{route} for {self._repo}: {str(error)[:200]}"
        )

    def list_files(self) -> list[str]:
        """Every tracked file — asked once per reader and kept."""
        if self._listing is not None:
            return list(self._listing)
        route = "/code/list-files"
        status, decoded = self._answer(route, {})
        if status != 200 or not isinstance(decoded, dict):
            raise self._refused(route, status, decoded)
        files = [str(path) for path in decoded.get("files") or []]
        if decoded.get("capped"):
            self.listing_cut = (
                f"the helper listed only the first {len(files)} of "
                f"{decoded.get('total_tracked', 'more')} tracked files"
            )
            self.cuts.append(self.listing_cut)
        self._listing = files
        return list(files)

    def _search_once(self, body: dict[str, Any]) -> dict[str, Any]:
        route = "/code/search"
        status, decoded = self._answer(route, body)
        if status != 200 or not isinstance(decoded, dict):
            raise self._refused(route, status, decoded)
        return decoded

    # -- searching, and covering what the helper's caps left out ------------

    def _files_with_long_lines(self) -> tuple[list[str], str | None]:
        """The tracked files holding a line the helper only partly searches,
        found once per reader; and a sentence when that list is incomplete."""
        if self._long_line_files is None:
            answer = self._search_once(
                {"pattern": f".{{{_HELPER_LINE_CHARS}}}", "max_results": _SEARCH_MAX_RESULTS}
            )
            files = list(
                dict.fromkeys(str(m.get("path") or "") for m in answer.get("matches") or [])
            )
            gap = None
            if answer.get("capped") or answer.get("timed_out"):
                gap = (
                    "more files hold lines longer than "
                    f"{_HELPER_LINE_CHARS} characters than could be listed"
                )
            self._long_line_files = ([f for f in files if f], gap)
        return self._long_line_files

    def _search(
        self,
        text: str,
        *,
        ignore_case: bool,
        relevant: Callable[[str], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Every match for ``text`` — recovered where the helper cut — and the
        plain sentences saying what could still not be searched."""
        body: dict[str, Any] = {
            "pattern": text,
            "fixed_string": True,
            "case_insensitive": bool(ignore_case),
            "max_results": _SEARCH_MAX_RESULTS,
        }
        needle = text.lower() if ignore_case else text
        found: dict[tuple[str, int], dict[str, Any]] = {}
        gaps: list[str] = []
        unrecovered: list[str] = []
        read_whole: set[str] = set()
        spent = [0]

        def add(matches: Any) -> None:
            for m in matches or []:
                if isinstance(m, dict) and m.get("path") and m.get("line") is not None:
                    found.setdefault((str(m["path"]), int(m["line"])), m)

        def budget_left() -> bool:
            return spent[0] < _MAX_RECOVERY_REQUESTS

        def read_and_search(path: str) -> None:
            if path in read_whole:
                return
            if relevant is not None and not relevant(path):
                return  # the caller would set it aside anyway
            if not budget_left():
                unrecovered.append(f"`{path}`")
                return
            spent[0] += 1
            content = self.read_text(path)
            if content is None:
                gaps.append(f"`{path}` could not be searched in full (too large or not text)")
                return
            read_whole.add(path)
            for number, line in enumerate(content.split("\n"), start=1):
                hay = line.lower() if ignore_case else line
                if needle in hay:
                    found.setdefault((path, number), {"path": path, "line": number, "text": line[:200]})

        def cover(under: str | None) -> None:
            answer = self._search_once({**body, **({"under": under} if under else {})})
            add(answer.get("matches"))
            where = f"`{under}/`" if under else "the repository"
            if int(answer.get("long_lines_partly_searched") or 0):
                files, gap = self._files_with_long_lines()
                for path in files:
                    if under is None or path.startswith(under + "/"):
                        read_and_search(path)
                if gap:
                    gaps.append(f"in {where}, {gap}, so some long lines were checked only in part")
            if answer.get("timed_out"):
                gaps.append(
                    f"the search in {where} stopped at the helper's time limit after "
                    f"{answer.get('files_searched', 'some')} file(s)"
                )
                return
            if not answer.get("capped"):
                return
            matches = [m for m in answer.get("matches") or [] if isinstance(m, dict)]
            if not matches:
                gaps.append(f"the search in {where} was cut short")
                return
            # The helper walks full paths in sorted order: everything that
            # sorts before the file it stopped in was searched; that file
            # and everything after it was not.
            last = str(matches[-1].get("path") or "")
            head = (under + "/") if under else ""
            inside = [p for p in self.list_files() if p.startswith(head)]
            if self.listing_cut is not None:
                # The helper's search walks every tracked file, but its
                # listing stops at 5,000: files past the listing can never be
                # proven recovered, so this search stays marked partial.
                gaps.append(
                    f"the search in {where} was cut short and the file list "
                    "used to finish it was itself incomplete, so some files "
                    "may not have been searched"
                )
            folders: dict[str, list[str]] = {}
            files: list[str] = []
            for path in inside:
                rest = path[len(head):]
                if "/" in rest:
                    folders.setdefault(head + rest.split("/", 1)[0], []).append(path)
                else:
                    files.append(path)
            for folder, members in sorted(folders.items()):
                if max(members) < last:
                    continue  # searched whole before the cut
                if relevant is not None and not any(relevant(m) for m in members):
                    continue  # nothing in it the caller would use
                if not budget_left():
                    unrecovered.append(f"`{folder}/`")
                    continue
                spent[0] += 1
                cover(folder)
            for path in files:
                if path >= last:
                    read_and_search(path)

        cover(None)
        if unrecovered:
            shown = ", ".join(unrecovered[:3])
            more = f" and {len(unrecovered) - 3} more" if len(unrecovered) > 3 else ""
            gaps.append(
                f"{len(unrecovered)} part(s) of the repository past the helper's "
                f"cap were not searched ({shown}{more}) — too many pieces to recover"
            )
        ordered = [found[key] for key in sorted(found)]
        sentences = [f"the search for `{text}`: {gap}" for gap in dict.fromkeys(gaps)]
        return ordered, sentences

    def files_mentioning(
        self,
        text: str,
        *,
        ignore_case: bool = False,
        relevant: Callable[[str], bool] | None = None,
    ) -> list[str]:
        """The files whose text holds ``text``. ``relevant``, when given, says
        which files the caller can use: what the helper's caps left out is
        recovered — and reported missing — only for those."""
        matches, gaps = self._search(text, ignore_case=ignore_case, relevant=relevant)
        self.cuts.extend(gaps)
        return list(dict.fromkeys(str(m.get("path")) for m in matches if m.get("path")))

    def places_mentioning(self, text: str) -> list[str]:
        matches, gaps = self._search(text, ignore_case=False)
        places = PartialPlaces(
            dict.fromkeys(f"{m.get('path')}:{m.get('line')}" for m in matches)
        )
        if gaps:
            places.cut = "; ".join(gaps)
        return places

    def read_text(self, path: str) -> str | None:
        status, decoded = self._answer("/code/read-file", {"path": path})
        if status == 200 and isinstance(decoded, dict) and not decoded.get("partial"):
            content = decoded.get("content")
            return content if isinstance(content, str) else None
        if 400 <= status < 500:
            # This one file is not served; the repository still is. The
            # helper's reason is kept so the sheet can say it.
            error = decoded.get("error") if isinstance(decoded, dict) else decoded
            self.refused[path] = f"the helper answered {status}: {str(error or 'no reason given')[:200]}"
            return None
        raise self._refused("/code/read-file", status, decoded)


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

    def code_reader(self) -> SidecarCodeReader:
        """A reader of this repository's tracked files through the helper's
        read-only code door — what the planner's fact sheet reads."""
        return SidecarCodeReader(self._base_url, repo=self._repo, post=self._post)

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

    async def fetch_remote_start_point(
        self, repo_path: str, branch: str | None = None
    ) -> RemoteStartPoint:
        """The starting rule's operation, run on the clone inside the sandbox.

        ``{repo}`` to ``/git/remote-start-point``; the answer is a branch and
        a commit, or one plain sentence. A sandbox that could not be reached
        is itself a refusal, so the driver never has to tell "no answer" from
        "no remote". Never raises. ``branch`` is sent only when given, and its
        commit comes back as ``branch_commit``.
        """
        body: dict[str, Any] = {"repo": self._repo}
        if branch:
            body["branch"] = str(branch)
        answer = await self._call(
            "/git/remote-start-point",
            body,
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
        start = answered_for_branch(RemoteStartPoint.from_wire(decoded), branch)
        if not start.ok:
            logger.warning("fetch_remote_start_point: %s", start.refusal)
        return start

    async def read_file_at_commit(
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
        raw: bool = False,
    ) -> FileAtCommit:
        """One file out of one commit, read on the clone inside the sandbox.

        The project's own memory (item 2, 2026-09-21): ``{repo, commit,
        file_path}`` to ``/git/read-file-at-commit``. A sandbox that could not
        be reached is a refusal in its own words, never "the project declares
        nothing" — the sentence a person is shown turns on that difference.
        Never raises. ``ordinary_file_only`` is sent only when asked for, and
        an answer that does not confirm the check was made is a refusal.
        """
        body: dict[str, Any] = {
            "repo": self._repo,
            "commit": commit,
            "file_path": file_path,
        }
        if ordinary_file_only:
            body["ordinary_file_only"] = True
        if raw:
            # The exact committed bytes and the entry's mode (4 October 2026);
            # sent only when asked for, and an answer that found the file
            # without saying its mode is a refusal.
            body["raw"] = True
        answer = await self._call(
            "/git/read-file-at-commit",
            body,
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
        read = answered_as_ordinary(FileAtCommit.from_wire(decoded), ordinary_file_only)
        read = answered_as_raw(read, raw)
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
        build: str | None = None,
        declared_at: str | None = None,
    ) -> SidecarGitOpResult:
        """Write ``files`` onto ``branch`` in one commit on the sandbox's clone,
        with the declared checks run there first.

        ``pre_commit`` is a :class:`PreCommitChecks` declaration or ``None``.
        A Python closure is refused with :data:`CLOSURE_REFUSED_SENTENCE` —
        a failed result, never a raise, so the leg fails loudly in the
        driver's own words. Never raises.

        THIS IS A FACTORY DOOR AND IT SAYS SO (23 September 2026, the tenth
        review). Until now this door sent ``by_hand: true`` whenever the write
        named a memory or a setting — the label a person at a keyboard wears,
        which asks the helper to read the project's own declarations at the
        committed HEAD of whatever copy it has. The reason given was that
        planning runs before there is a build to name. That was wrong about
        this factory's own records: the driver writes the run's starting
        commit onto the planning run BEFORE it writes any tree, and reads the
        memory name and the declared setting names off that same run. So the
        run knows whose work it is (``build`` — the id the run's own row is
        keyed by) and where its declarations were said (``declared_at`` — the
        commit recorded for that run, read off the store, never composed and
        never HEAD), and it sends both, exactly as the merge word's own
        command sends the pair it reads off the build's row.

        A write that names a memory or a setting and has NO recorded starting
        commit is refused here, before a file is written or a check launched,
        with the sentence that says how to recover. Falling back to HEAD is
        what the label used to buy and it is what this closes. A write that
        names nothing declared asks the project for nothing, so it needs no
        pair and carries whatever it was given.
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
        if build:
            # WHOSE WORK THIS IS: the id this factory's own record keeps this
            # run under. It is a label on every request, whether or not
            # anything declared is being read.
            body["build"] = str(build)
        if declared_at:
            # AND WHERE ITS DECLARATIONS WERE SAID: the commit written onto
            # the run before the first branch was cut. The helper does not
            # take this on the request's word — it asks this factory's own
            # read-only answer about the run named above and reads at the
            # commit THAT names.
            body["declared_at"] = str(declared_at)
        if (
            body.get("memory_project") or body.get("launch_settings")
        ) and not body.get("declared_at"):
            # A DECLARATION WITH NOWHERE TO READ IT. Nothing is written and no
            # check is launched: the recovery is to record where this run
            # starts, which is what its target-terminal step does.
            sentence = (
                f"this planning run ({build or 'unnamed'}) asks for "
                f"{self._repo}'s own declarations to be read, and this "
                "factory has no recorded commit for where the run starts, so "
                "there is nowhere to read them. They were NOT read at the "
                "committed HEAD of the copy in the sandbox: a declaration is "
                "read at the commit the record names, and no record here "
                "names one. Nothing was written and no check was run. To "
                "recover, re-run this run's target-terminal step, which fetches "
                "the project's default branch and writes the starting commit "
                "onto the run before anything is cut."
            )
            logger.error("%s: %s", _TREE_OPERATION, sentence)
            return SidecarGitOpResult(
                status="failed",
                operation=_TREE_OPERATION,
                stderr=sentence,
                detail=sentence,
                exit_code=-1,
            )
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

    async def fetch_remote_start_point(
        self, repo_path: str, branch: str | None = None
    ) -> RemoteStartPoint:
        """The starting rule's operation, routed exactly as the others are.

        ``branch`` is passed on only when given, so a runner that predates it
        is called exactly as before."""
        runner = self.runner_for_path(repo_path)
        if branch:
            return await runner.fetch_remote_start_point(repo_path, branch)
        return await runner.fetch_remote_start_point(repo_path)

    async def read_file_at_commit(
        self,
        repo_path: str,
        commit: str,
        file_path: str,
        *,
        ordinary_file_only: bool = False,
        raw: bool = False,
    ) -> FileAtCommit:
        """One file out of one commit, routed exactly as the others are."""
        runner = self.runner_for_path(repo_path)
        if raw:
            return await runner.read_file_at_commit(
                repo_path,
                commit,
                file_path,
                ordinary_file_only=ordinary_file_only,
                raw=True,
            )
        if ordinary_file_only:
            return await runner.read_file_at_commit(
                repo_path, commit, file_path, ordinary_file_only=True
            )
        return await runner.read_file_at_commit(repo_path, commit, file_path)

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
        build: str | None = None,
        declared_at: str | None = None,
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
            build=build,
            declared_at=declared_at,
        )

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        return await self.runner_for_path(repo_path).read_file_from_branch(
            repo_path=repo_path, branch=branch, file_path=file_path
        )
