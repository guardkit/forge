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

FACTORY RECORDS. The factory's own build records are kept out of a
project's commits when the commit is made (forge.factory_files, design
"Factory records never enter a project's commits", 14 October 2026). The
publisher's own looks for them before a send went in the cull (27 October
2026, Rich's decision): they only guarded what the commit side already does.

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
    "GitSaid",
    "THE_ADDRESS_IS_NOT_WRITTEN_DOWN",
    "TheProjectsCommits",
    "TheRemotesList",
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
class TheRemotesList:
    """What one ``ls-remote`` of the remote said (missions design, section 4).

    ``default_branch`` and ``default_tip`` are the branch the remote's
    ``HEAD`` names and where it is; ``branch_tip`` is where the branch asked
    about is, or ``None`` when the remote has no such branch.
    """

    ok: bool
    default_branch: str | None = None
    default_tip: str | None = None
    branch_tip: str | None = None
    said: str = ""


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

    def what_the_remote_lists(self, branch: str) -> "TheRemotesList":
        """The remote's default branch, its tip, and ``branch``'s tip if any.

        One ``ls-remote --symref`` of ``HEAD`` and ``refs/heads/<branch>``,
        with this route's credential (missions design, section 4). Only exact
        names are read off the list: a pattern matches the end of a name, so
        ``refs/heads/x/HEAD`` is never taken for the remote's ``HEAD``.
        ``branch_tip`` is ``None`` when the remote has no such branch. Never
        raises; nothing is fetched or written.
        """
        if a_plain_branch_name(branch) is None:
            return TheRemotesList(
                ok=False, said="that is not a branch name the publisher will ask about"
            )
        ready = self.ready()
        if not ready.ok:
            return TheRemotesList(ok=False, said=ready.said)
        transport = the_remote_transport(self._route.remote)
        if transport is None:
            return TheRemotesList(
                ok=False,
                said=(
                    "the remote is not an https:// or SSH address, so the "
                    "publisher will not read it"
                ),
            )
        asked = self._git(
            "ls-remote",
            "--symref",
            "--",
            self._route.remote,
            "HEAD",
            f"refs/heads/{branch}",
            transport=transport,
            with_credential=True,
        )
        if not asked.ok:
            return TheRemotesList(
                ok=False,
                said=f"the remote named origin could not be listed: {asked.said}",
            )
        default: str | None = None
        listed: dict[str, str] = {}
        for line in asked.out.splitlines():
            stripped = line.strip()
            if stripped.startswith("ref:"):
                target, _tab, name = stripped[len("ref:"):].strip().partition("\t")
                if name.strip() == "HEAD" and target.startswith("refs/heads/"):
                    default = target[len("refs/heads/"):].strip()
                continue
            sha, _tab, name = stripped.partition("\t")
            if a_plain_commit_name(sha) and name.strip():
                listed[name.strip()] = sha.strip().lower()
        if default is not None and a_plain_branch_name(default) is None:
            default = None
        return TheRemotesList(
            ok=True,
            default_branch=default,
            # The commit the remote's HEAD resolves to IS its default
            # branch's tip; only the HEAD line carries it, because only HEAD
            # and the branch asked about were listed.
            default_tip=listed.get("HEAD") if default else None,
            branch_tip=listed.get(f"refs/heads/{branch}"),
        )

    def the_key_reads_the_remote(self) -> GitSaid:
        """``git ls-remote`` of the remote named origin, with this route's own
        credential and nothing written anywhere (17 October 2026): the first
        half of "confirmed private" (:mod:`forge.publisher.private_remote`)."""
        transport = the_remote_transport(self._route.remote)
        if transport is None:
            return GitSaid(
                ok=False,
                said=(
                    "the remote is not an https:// or SSH address, so the "
                    "publisher will not read it"
                ),
            )
        return self._git(
            "ls-remote",
            "--",
            self._route.remote,
            "HEAD",
            transport=transport,
            with_credential=True,
            git_dir=False,
        )

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
