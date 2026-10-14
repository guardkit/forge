"""The git the publisher does: bring the joined commit out, look, send, read back.

One-true-copy design pass, item 1, second revision section D. Four rules hold
the whole of this module, and each one is a property of the code rather than a
habit:

1. **The project's copy is only ever read.** The joined commit is brought out
   of it by FETCHING from the read-only address the publisher was given. There
   is no push, no write and no path into the copy anywhere in this file, and a
   test reads the source to pin that.
2. **The send cannot force.** There is exactly one function that builds a push
   argument list, :func:`the_send_argv`, and it produces
   ``push <remote> <commit>:refs/heads/<branch>`` and nothing else — no
   ``--force``, no ``--force-with-lease``, no leading ``+`` on the refspec. A
   forced push is not refused at run time; it is not expressible.
3. **Nothing is ever put on a command line that was not checked first.** A
   commit has to be a plain hexadecimal name and a branch has to be a plain
   branch name, both anchored at a character that cannot be read as an
   option.
4. **No child is given this process's environment.** Every git command runs
   through :func:`forge.network_git.run_network_git` (14 October 2026): an
   environment built from nothing, a FRESH EMPTY home folder for each command
   (never the publisher's state folder, where a ``.netrc`` or ``.gitconfig``
   would be read), no system or personal settings file, credential helpers,
   extra headers and cookie files cleared, and exactly ONE transport per
   step: ``git`` or ``file`` for the project's copy (by the source's checked
   form), ``ssh`` or ``https`` for the remote (by the remote's). The only
   credential is the route's own, chosen here.
5. **Git's own words are never written down.** Git names the address it was
   working with in its own messages, and a remote's reason for refusing a
   send is the remote's text. Every sentence this module says is a fixed one
   (which step, git's exit code), with at most one of three reasons
   recognised from git's text but never quoted
   (:data:`forge.network_git.REASONS`): the remote's branch has moved on, the
   remote refused the key, the host key is not the pinned one.

THE PUBLISHER'S OWN COPY. It keeps one repository of its own per project, so
that the commits it fetches have somewhere to live that is nobody else's. It
is a bare repository: there is no working tree, nothing is checked out and
nothing can be built or run out of it.

WHAT MUST NOT BE PUBLISHED (14 October 2026, design "Factory records never
enter a project's commits"). Before a send, :meth:`TheProjectsCommits.what_must_not_be_published`
looks at exactly the commits the send would add, four ways: a record path
added or changed (a), a record moved or copied out, on the final state (b), a
record's exact content under another path (c), and any new file version
holding one of the factory's private folder names (d). It only reads the
publisher's own copy; it never removes or rewrites anything.

AGNOSTIC. Two git addresses and a branch name. Nothing here knows what a
project contains, what language it is written in, who hosts its remote or how
it is deployed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from forge.factory_files import factory_record_pathspecs, is_factory_record
from forge.network_git import run_network_git
from forge.publisher.credential import (
    Credential,
    an_ssh_address,
    the_credential_environment,
    the_remote_transport,
    the_source_transport,
)
from forge.publisher.settings import ProjectRoute

logger = logging.getLogger(__name__)

__all__ = [
    "Finding",
    "GitSaid",
    "THE_ADDRESS_IS_NOT_WRITTEN_DOWN",
    "TheProjectsCommits",
    "a_plain_branch_name",
    "a_plain_commit_name",
    "the_send_argv",
    "without_the_addresses",
]

#: What stands in an address's place in every sentence the publisher says.
THE_ADDRESS_IS_NOT_WRITTEN_DOWN: str = "<the address is not written down>"

#: The part of an address that names who is asking, which is where somebody
#: setting this up at rollout is most likely to put a secret. It is taken out
#: of anything git said before that text is written anywhere, whatever address
#: it belongs to — including one this module was never told about.
_WHO_IS_ASKING = re.compile(r"(?<=//)[^/@\s]*@")

#: A commit as git names it: hexadecimal, full length, nothing else. The
#: publisher is always given a commit somebody already wrote down, never a
#: reference to resolve, so nothing looser is needed or allowed.
_A_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

#: A branch name that can go on a command line: it starts with a letter or a
#: digit, so it can never be read as an option, and carries only the
#: characters branch names use.
_A_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,255}$")

#: Where the publisher's own copy keeps the project's branches, and where it
#: keeps the remote's. Two separate namespaces, so that what came out of the
#: project's copy and what came off the remote can never be confused.
THE_COPYS_BRANCHES: str = "refs/remotes/the-projects-copy/"
THE_REMOTES_BRANCHES: str = "refs/remotes/the-remote/"


def a_plain_commit_name(commit: str | None) -> str | None:
    """``commit`` if it is a commit git could have written down, else None."""
    text = str(commit or "").strip().lower()
    return text if _A_COMMIT.match(text) else None


def a_plain_branch_name(branch: str | None) -> str | None:
    """``branch`` if it is a branch name that may go on a command line."""
    text = str(branch or "").strip()
    if not _A_BRANCH.match(text) or ".." in text or text.endswith("/"):
        return None
    return text


def the_send_argv(remote: str, commit: str, branch: str) -> list[str]:
    """The ONE argument list a send is ever made with.

    ``push <remote> <commit>:refs/heads/<branch>``. An ordinary, non-forcing
    push: git itself refuses it when it would not move the branch forwards,
    which is the guarantee the design asks for — the remote is the thing that
    decides, not the factory.

    There is no argument to this function that makes it force, and no other
    function in the publisher builds a push. That is what "impossible by
    construction" means here, and a test reads both this list and the whole
    module's source to pin it.

    It also checks its own inputs (23 September 2026): the service checks them
    before it ever gets here, but a function that says it is the one place a
    send is built must not build ``x:refs/heads/+main`` for a caller that did
    not. A bad name raises, and nothing is run.
    """
    plain_commit = a_plain_commit_name(commit)
    plain_branch = a_plain_branch_name(branch)
    if plain_commit is None or plain_branch is None:
        raise ValueError(
            "the send is built only from a plain commit and a plain branch "
            "name; one of them is not"
        )
    return ["push", str(remote), f"{plain_commit}:refs/heads/{plain_branch}"]


@dataclass(frozen=True)
class GitSaid:
    """What one git command did, and what it said in one line."""

    ok: bool
    out: str = ""
    said: str = ""


@dataclass(frozen=True)
class Finding:
    """One look's answer: what it found, how many, and the first one.

    ``look`` is ``"a"``, ``"b"``, ``"c"`` or ``"d"``. ``first`` names a file
    (and, for looks b and c, the record it came from); it never quotes a
    file's content or a private folder name.
    """

    look: str
    count: int
    first: str
    #: The files found, for looks a to c (so one file found by two looks is
    #: counted once); for look d, the files whose new version matched.
    paths: tuple[str, ...] = ()

    @property
    def private_paths(self) -> bool:
        return self.look == "d"


#: An object id of all zeros: no object (a deletion, or nothing before).
_NO_OBJECT = re.compile(r"^0+$")

#: The empty blob, in both object formats. Look c never counts it: an empty
#: product file beside an empty record is not a copy of anything.
_EMPTY_BLOBS = frozenset(
    {
        "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",
    }
)

#: The modes whose object is a blob: a file, an executable, a symbolic link
#: (whose blob is the link's text). A gitlink (160000) is never read.
_BLOB_MODES = frozenset({"100644", "100755", "120000"})

#: Options every history read carries: the commits being sent, merges judged
#: only on files whose result differs from every parent, no rename pairing,
#: full object names, NUL-separated, nothing but the raw lines. ``--full-history``
#: keeps a record one commit added and a later one deleted.
_HISTORY = (
    "log",
    "--no-ext-diff",
    "--no-textconv",
    "--full-history",
    "--diff-merges=dense-combined",
    "--no-renames",
    "--raw",
    "--no-abbrev",
    "-z",
    "--format=",
)

#: How many blobs one ``cat-file --batch`` reads at a time.
_BLOBS_PER_READ = 256


@dataclass(frozen=True)
class _Entry:
    """One raw entry: the result's mode and object, and the path."""

    mode: str
    object_id: str
    path: str

    @property
    def is_new_blob(self) -> bool:
        return self.mode in _BLOB_MODES and not _NO_OBJECT.match(self.object_id)


def _raw_entries(text: str) -> list[_Entry]:
    """Parse ``--raw -z`` output, ordinary and combined (``::``) entries alike.

    An entry is a header (``:<modes> <objects> <status>``) and one path, each
    ended by a NUL; ``--no-renames`` means there is never a second path. The
    header's last mode and object are the result's.
    """
    fields = text.split("\0")
    entries: list[_Entry] = []
    index = 0
    while index < len(fields):
        head = fields[index].lstrip("\n")
        index += 1
        if not head.startswith(":"):
            continue
        path = fields[index] if index < len(fields) else ""
        index += 1
        colons = len(head) - len(head.lstrip(":"))
        parts = head[colons:].split()
        sides = colons + 1
        if len(parts) < 2 * sides:
            continue
        entries.append(_Entry(parts[sides - 1], parts[2 * sides - 1], path))
    return entries


def _raw_moves(text: str) -> list[tuple[str, str, str]]:
    """``(source, destination, destination's object)`` of each rename or copy
    in ``git diff --raw -z`` output with rename detection on. A rename or copy
    names two paths; every other status names one."""
    fields = text.split("\0")
    moves: list[tuple[str, str, str]] = []
    index = 0
    while index < len(fields):
        head = fields[index].lstrip("\n")
        index += 1
        if not head.startswith(":"):
            continue
        parts = head[1:].split()
        status = parts[4] if len(parts) >= 5 else ""
        if status[:1] in ("R", "C"):
            source, destination = (fields[index : index + 2] + ["", ""])[:2]
            index += 2
            moves.append((source, destination, parts[3]))
        else:
            index += 1
    return moves


def _blobs_holding(data: bytes, wanted: list[str], needles: list[bytes]) -> set[str] | None:
    """The blob ids among ``wanted`` whose bytes hold any of ``needles``,
    from ``git cat-file --batch`` output. ``None`` when the output is not
    what was asked for (a missing object, a short read)."""
    found: set[str] = set()
    at = 0
    for object_id in wanted:
        end = data.find(b"\n", at)
        if end < 0:
            return None
        header = data[at:end].split()
        if len(header) != 3 or header[0].decode("ascii", "replace") != object_id:
            return None
        if header[1] != b"blob":
            return None
        try:
            size = int(header[2])
        except ValueError:
            return None
        start = end + 1
        content = data[start : start + size]
        if len(content) != size:
            return None
        if any(needle in content for needle in needles):
            found.add(object_id)
        at = start + size + 1  # the newline after the content
    return found


def without_the_addresses(said: str, addresses: "Iterable[str]" = ()) -> str:
    """The same sentence with the addresses taken out of it.

    WHY AN ADDRESS IS NEVER WRITTEN DOWN. Git puts the address it was working
    with into its own messages, and the publisher's sentences go into a
    refusal a person reads, a receipt and a row of the ledger. An address is
    also the place a credential most easily ends up: the ordinary way to hand
    one to git without a helper is to put it in the address itself. Nothing in
    this estate does that today — the publisher's credential lives in one file
    and reaches git through the program named by ``GIT_ASKPASS`` — but the
    addresses come from a settings file somebody will fill in at rollout, and
    a rule that depends on nobody ever doing the easy thing is not a rule.

    So two things are taken out: the addresses this publisher was told about,
    by exact text, and the "who is asking" part of ANY address in the line,
    including one nobody here has ever seen. What is left still says what went
    wrong; it just does not say where.
    """
    cleaned = str(said or "")
    for address in addresses:
        text = str(address or "").strip()
        if text:
            cleaned = cleaned.replace(text, THE_ADDRESS_IS_NOT_WRITTEN_DOWN)
    return _WHO_IS_ASKING.sub(THE_ADDRESS_IS_NOT_WRITTEN_DOWN + "@", cleaned)


class TheProjectsCommits:
    """The publisher's own bare repository for one project.

    Made on first use under the publisher's own folder, and reused after
    that. It holds nothing but commits: no working tree, nothing checked out,
    nothing that can be run.
    """

    def __init__(
        self,
        route: ProjectRoute,
        *,
        state_dir: Path,
        credential: Credential | None,
        timeout_seconds: float = 180.0,
        known_hosts: str = "",
    ) -> None:
        self._route = route
        # Over SSH the credential is a key and the host is checked against
        # the pinned file; otherwise git asks for the token as before.
        # An SSH remote ALWAYS takes the pinned-host path: with no file named,
        # an empty one stands in and every host is refused.
        self._known_hosts = (
            (known_hosts or "/dev/null") if an_ssh_address(route.remote) else None
        )
        self._state = Path(state_dir)
        self._credential = credential
        self._timeout = float(timeout_seconds)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", route.name).strip("-") or "project"
        self._where = self._state / "copies" / f"{safe}.git"

    @property
    def where(self) -> Path:
        return self._where

    @property
    def _the_addresses(self) -> tuple[str, ...]:
        """The two addresses this project has, which are never written down."""
        return (str(self._route.remote), str(self._route.source))

    # -- running git -------------------------------------------------------

    def _git(
        self,
        *args: str,
        transport: str = "none",
        with_credential: bool = False,
        git_dir: bool = True,
        timeout: float | None = None,
    ) -> GitSaid:
        """One git command at the network boundary. Never raises.

        In the publisher's own repository (unless ``git_dir`` is False), with
        exactly one ``transport`` and, only when ``with_credential``, the
        route's own credential. The sentence is fixed; git's words are not
        kept.
        """
        done = run_network_git(
            list(args),
            transport=transport,
            git_dir=self._where if git_dir else None,
            timeout=self._timeout if timeout is None else timeout,
            credential_env=(
                the_credential_environment(
                    self._credential, state_dir=self._state, known_hosts=self._known_hosts
                )
                if with_credential
                else None
            ),
        )
        return GitSaid(
            ok=done.ok,
            out=(done.stdout or "").strip(),
            said=done.said.rstrip("."),
        )

    def ready(self) -> GitSaid:
        """Make the publisher's own repository if it is not there yet."""
        if (self._where / "HEAD").is_file():
            return GitSaid(ok=True)
        self._where.parent.mkdir(parents=True, exist_ok=True)
        made = self._git(
            "init", "--bare", "-q", "--template=", str(self._where), git_dir=False
        )
        if not made.ok:
            return GitSaid(
                ok=False, said=f"the publisher's own copy could not be made ({made.said})"
            )
        return GitSaid(ok=True)

    # -- the four things it does -------------------------------------------

    def bring_the_joined_commit_out(self, commit: str) -> GitSaid:
        """Fetch the project's branches from the READ-ONLY address, and look.

        The joined commit lives on a branch of the factory's own in the
        project's copy, so fetching that copy's branches brings it out. The
        address is the read-only git service the publisher was given: nothing
        here can write to the copy, and nothing here knows a path into it.
        """
        ready = self.ready()
        if not ready.ok:
            return ready
        transport = the_source_transport(self._route.source)
        if transport is None:
            return GitSaid(
                ok=False,
                said=(
                    f"{self._route.name}'s copy is not at a git:// address or an "
                    "absolute local folder, so the publisher will not read it"
                ),
            )
        fetched = self._git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--prune",
            "--",
            self._route.source,
            f"+refs/heads/*:{THE_COPYS_BRANCHES}*",
            transport=transport,
        )
        if not fetched.ok:
            return GitSaid(
                ok=False,
                said=(
                    f"the joined commit could not be brought out of "
                    f"{self._route.name}'s copy: {fetched.said}"
                ),
            )
        found = self._git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        if not found.ok or not found.out:
            return GitSaid(
                ok=False,
                said=(
                    f"the joined commit {commit[:10]} is not in "
                    f"{self._route.name}'s copy, so there is nothing to send"
                ),
            )
        return GitSaid(ok=True, out=found.out)

    def the_parents_of(self, commit: str) -> list[str]:
        """The commit's parents, in order. An empty list when it has none."""
        said = self._git("rev-list", "--parents", "-n", "1", commit)
        if not said.ok or not said.out:
            return []
        names = said.out.split()
        return names[1:]

    def where_the_remotes_branch_is(self, branch: str) -> GitSaid:
        """Fetch that branch from the remote named ``origin`` and say where it is."""
        ready = self.ready()
        if not ready.ok:
            return ready
        transport = the_remote_transport(self._route.remote)
        if transport is None:
            return GitSaid(
                ok=False,
                said=(
                    "the remote is not an https:// or SSH address, so the "
                    "publisher will not read it"
                ),
            )
        fetched = self._git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            self._route.remote,
            f"+refs/heads/{branch}:{THE_REMOTES_BRANCHES}{branch}",
            transport=transport,
            with_credential=True,
        )
        if not fetched.ok:
            return GitSaid(
                ok=False,
                said=(
                    f"the branch '{branch}' could not be read from the remote "
                    f"named origin: {fetched.said}"
                ),
            )
        at = self._git(
            "rev-parse", "--verify", "--quiet", f"{THE_REMOTES_BRANCHES}{branch}"
        )
        if not at.ok or not at.out:
            return GitSaid(
                ok=False,
                said=(
                    f"the remote named origin has no branch '{branch}', so "
                    "there is nothing to send to"
                ),
            )
        return GitSaid(ok=True, out=at.out)

    def is_in(self, ancestor: str, descendant: str) -> bool | None:
        """Is ``ancestor`` part of ``descendant``? ``None`` = git could not say."""
        done = run_network_git(
            ["merge-base", "--is-ancestor", str(ancestor), str(descendant)],
            transport="none",
            git_dir=self._where,
            timeout=self._timeout,
        )
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        return None

    def _read(self, *args: str) -> str | None:
        """Run one reading command in the publisher's own copy; its whole
        output, or ``None`` when git failed."""
        done = run_network_git(
            list(args), transport="none", git_dir=self._where, timeout=self._timeout
        )
        return done.stdout if done.ok else None

    def what_must_not_be_published(
        self,
        since: str,
        commit: str,
        *,
        private_prefixes: "Iterable[str]",
    ) -> list[Finding] | None:
        """What the commits a send would add hold that must not be published.

        The range is ``<since>..<commit>``: the commits reachable from the
        join and not from the published target branch, which is exactly what
        the send adds. An empty list means nothing was found; ``None`` means a
        look could not be made (git failed, or no private prefixes were given),
        and the caller sends nothing.

        a. **Records added or changed**, at any depth, including one a commit
           added and a later one deleted: every entry at a record path whose
           result is not a deletion. Merges are judged only on files whose
           result differs from every parent, so a record inherited unchanged
           from the target is not reported, while one the join itself
           introduces is.
        b. **Records moved out, judged on the final state**: the published
           branch's tree against the join's, with copies found against
           unchanged files too (``-C -C``) and no rename limit (``-l0``,
           because over a limit git skips the comparison quietly). Any rename
           or copy whose source is a record and whose destination is not,
           except an empty destination (git pairs empty files with each
           other; an empty file holds nothing of a record).
        c. **Record content elsewhere**: every non-empty blob that sat at a
           record path in the range's boundary commits, or that look a saw a
           new commit put at one; then any added, changed or retyped file
           outside the record paths whose new blob is one of them.
        d. **Private folder names**: every blob the range adds, changes or
           retypes, at any path, merges included, is read whole and its raw
           bytes searched for each private prefix. A gitlink is never read.
           A blob seen twice is read once.

        No look parses a patch, and nothing is removed or rewritten.
        """
        needles = [p.encode("utf-8") for p in private_prefixes if str(p or "")]
        if not needles:
            return None
        if a_plain_commit_name(since) is None or a_plain_commit_name(commit) is None:
            return None
        span = f"{since}..{commit}"

        # -- a. records added or changed ------------------------------------
        raw = self._read(*_HISTORY, span, "--", *factory_record_pathspecs())
        if raw is None:
            return None
        record_paths: list[str] = []
        record_blobs: dict[str, str] = {}
        for entry in _raw_entries(raw):
            if _NO_OBJECT.match(entry.object_id) or not is_factory_record(entry.path):
                continue
            if entry.path not in record_paths:
                record_paths.append(entry.path)
            if entry.is_new_blob:
                record_blobs.setdefault(entry.object_id, entry.path)
        findings: list[Finding] = []
        if record_paths:
            findings.append(
                Finding("a", len(record_paths), record_paths[0], tuple(record_paths))
            )

        # -- b. records moved out, judged on the final state ---------------
        moved = self._read(
            "diff", "--no-ext-diff", "--no-textconv", "--raw", "--no-abbrev",
            "-z", "-M", "-C", "-C", "-l0", since, commit,
        )
        if moved is None:
            return None
        # An empty file is never a copy of anything: git pairs empty files
        # with each other, and an empty product file beside an empty record
        # is ordinary work (design test 32's control).
        moved_out = [
            (source, destination)
            for source, destination, object_id in _raw_moves(moved)
            if is_factory_record(source)
            and destination
            and not is_factory_record(destination)
            and object_id not in _EMPTY_BLOBS
        ]
        if moved_out:
            source, destination = moved_out[0]
            findings.append(
                Finding(
                    "b",
                    len(moved_out),
                    f"{destination}, moved or copied from {source}",
                    tuple(dict.fromkeys(d for _, d in moved_out)),
                )
            )

        # -- c. record content elsewhere -----------------------------------
        listed = self._read("rev-list", "--boundary", span)
        if listed is None:
            return None
        boundary = [
            line[1:].strip()
            for line in listed.splitlines()
            if line.startswith("-") and a_plain_commit_name(line[1:].strip())
        ]
        for edge in boundary:
            tree = self._read("ls-tree", "-r", "-l", "-z", edge)
            if tree is None:
                return None
            for item in tree.split("\0"):
                meta, _, path = item.partition("\t")
                parts = meta.split()
                if len(parts) != 4 or parts[1] != "blob" or not is_factory_record(path):
                    continue
                record_blobs.setdefault(parts[2], path)
        for empty in _EMPTY_BLOBS:
            record_blobs.pop(empty, None)

        added = self._read(*_HISTORY, "--diff-filter=AMT", span)
        if added is None:
            return None
        new_blobs = [entry for entry in _raw_entries(added) if entry.is_new_blob]
        copies: dict[str, str] = {}
        for entry in new_blobs:
            if is_factory_record(entry.path) or entry.object_id not in record_blobs:
                continue
            copies.setdefault(entry.path, record_blobs[entry.object_id])
        if copies:
            path, record = next(iter(copies.items()))
            findings.append(
                Finding(
                    "c",
                    len(copies),
                    f"{path}, the same content as {record}",
                    tuple(copies),
                )
            )

        # -- d. private folder names in any new file version ---------------
        wanted = list(dict.fromkeys(entry.object_id for entry in new_blobs))
        holding: set[str] = set()
        for start in range(0, len(wanted), _BLOBS_PER_READ):
            chunk = wanted[start : start + _BLOBS_PER_READ]
            done = run_network_git(
                ["cat-file", "--batch"],
                transport="none",
                git_dir=self._where,
                timeout=self._timeout,
                binary=True,
                input_bytes="".join(f"{o}\n" for o in chunk).encode("ascii"),
            )
            if not done.ok:
                return None
            found = _blobs_holding(done.data, chunk, needles)
            if found is None:
                return None
            holding |= found
        if holding:
            matched = tuple(
                dict.fromkeys(e.path for e in new_blobs if e.object_id in holding)
            )
            findings.append(Finding("d", len(holding), matched[0], matched))
        return findings

    def send(self, commit: str, branch: str) -> GitSaid:
        """An ordinary, non-forcing push of one commit to one branch."""
        transport = the_remote_transport(self._route.remote)
        if transport is None:
            return GitSaid(
                ok=False,
                said="the remote is not an https:// or SSH address, so nothing was sent",
            )
        return self._git(
            *the_send_argv(self._route.remote, commit, branch),
            transport=transport,
            with_credential=True,
        )
