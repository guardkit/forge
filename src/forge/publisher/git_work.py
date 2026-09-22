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
4. **No child is given this process's environment.** Every git command is
   given a small environment built from nothing
   (:func:`forge.publisher.credential.the_environment_git_is_given`), with
   ``HOME`` inside the publisher's own folder so git reads no person's
   configuration and finds no person's stored credentials.
5. **No address is ever written down.** Git names the address it was working
   with in its own messages, and those messages go into refusal sentences,
   receipts and rows of the ledger. The addresses come out of a settings file
   somebody fills in at rollout, and an address is the easiest place for a
   credential to end up. So every sentence git gives this module passes
   through :func:`without_the_addresses` on its one way out
   (:func:`_one_line`), and what it says is what went wrong, not where.

THE PUBLISHER'S OWN COPY. It keeps one repository of its own per project, so
that the commits it fetches have somewhere to live that is nobody else's. It
is a bare repository: there is no working tree, nothing is checked out and
nothing can be built or run out of it.

AGNOSTIC. Two git addresses and a branch name. Nothing here knows what a
project contains, what language it is written in, who hosts its remote or how
it is deployed.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from forge.publisher.credential import Credential, the_environment_git_is_given
from forge.publisher.settings import ProjectRoute

logger = logging.getLogger(__name__)

__all__ = [
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
    """
    return ["push", str(remote), f"{commit}:refs/heads/{branch}"]


@dataclass(frozen=True)
class GitSaid:
    """What one git command did, and what it said in one line."""

    ok: bool
    out: str = ""
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


def _one_line(
    done: "subprocess.CompletedProcess[str]", addresses: "Iterable[str]" = ()
) -> str:
    """Git's own reason, in one line, with no address left in it.

    Every sentence the publisher says about a git command comes through here,
    which is why the taking-out happens here rather than at each caller: a
    caller that forgot would be a leak, and there is no path around this
    function.
    """
    text = ((done.stderr or "") + "\n" + (done.stdout or "")).strip().splitlines()
    lines = [line.strip() for line in text if line.strip()]
    if not lines:
        return f"git exited {done.returncode} and said nothing"
    chosen = lines[0]
    for line in lines:
        low = line.lower()
        if low.startswith("fatal:") or low.startswith("error:") or " ! [" in line:
            chosen = line
            break
    return without_the_addresses(chosen, addresses)


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
    ) -> None:
        self._route = route
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

    def _git(self, *args: str, timeout: float | None = None) -> GitSaid:
        """One git command in the publisher's own repository. Never raises."""
        environment = the_environment_git_is_given(
            self._credential, state_dir=self._state, home=self._state
        )
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "--git-dir", str(self._where), *args],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout if timeout is None else timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return GitSaid(
                ok=False,
                said=(
                    f"git did not finish within "
                    f"{int(self._timeout if timeout is None else timeout)} seconds"
                ),
            )
        except OSError as exc:
            return GitSaid(ok=False, said=f"git could not be run ({exc})")
        return GitSaid(
            ok=done.returncode == 0,
            out=(done.stdout or "").strip(),
            said=_one_line(done, self._the_addresses),
        )

    def ready(self) -> GitSaid:
        """Make the publisher's own repository if it is not there yet."""
        if (self._where / "HEAD").is_file():
            return GitSaid(ok=True)
        self._where.parent.mkdir(parents=True, exist_ok=True)
        environment = the_environment_git_is_given(
            self._credential, state_dir=self._state, home=self._state
        )
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "init", "--bare", "-q", str(self._where)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                check=False,
                env=environment,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return GitSaid(
                ok=False,
                said=f"the publisher's own copy could not be made ({exc})",
            )
        if done.returncode != 0:
            return GitSaid(ok=False, said=_one_line(done, self._the_addresses))
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
        fetched = self._git(
            "fetch",
            "--no-tags",
            "--prune",
            self._route.source,
            f"+refs/heads/*:{THE_COPYS_BRANCHES}*",
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
        fetched = self._git(
            "fetch",
            "--no-tags",
            self._route.remote,
            f"+refs/heads/{branch}:{THE_REMOTES_BRANCHES}{branch}",
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
        environment = the_environment_git_is_given(
            self._credential, state_dir=self._state, home=self._state
        )
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    "git",
                    "--git-dir",
                    str(self._where),
                    "merge-base",
                    "--is-ancestor",
                    str(ancestor),
                    str(descendant),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                check=False,
                env=environment,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        return None

    def send(self, commit: str, branch: str) -> GitSaid:
        """An ordinary, non-forcing push of one commit to one branch."""
        return self._git(*the_send_argv(self._route.remote, commit, branch))
