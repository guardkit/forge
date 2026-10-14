"""The one credential the publisher holds, and every way it is never shown.

One-true-copy design pass, item 1, first revision item 3 and second revision
section D: *sending to the remote is done by a small separate publisher, its
own process, which holds the credential. The credential is never in the
coordinator's settings, the runner's settings or the sandbox's.*

THE RULES THIS MODULE KEEPS, and they are the whole of it:

* the credential is read ONCE, at start, from ONE named file path, which is
  the publisher's single setting for it. There is no other source: not an
  environment variable, not a settings field, not a request;
* it is never logged. :class:`Credential` shows itself as
  ``<the publisher's credential: not shown>`` on every path Python prints an
  object by, so a stray ``%s`` in somebody's log line cannot leak it;
* it is never in an answer. Nothing here is serialisable;
* it is never in a child's ENVIRONMENT or argument list. Git needs it to
  authenticate a send, and git's own way of asking for one is to run the
  program named by ``GIT_ASKPASS`` and read one line from it. So the child's
  environment carries the PATH OF A PROGRAM, never the secret, and the
  program the publisher writes reads the same one named file. The credential
  therefore exists in exactly one file on disk, ever: the one a person put it
  in. Nothing copies it to a second place.

WHY THE HELPER READS THE FILE RATHER THAN BEING HANDED THE TEXT. Any way of
handing text to a child — an argument, an environment variable, a second file
— makes a second copy of the credential somewhere a person did not put it.
Reading the same named file makes none. The helper is the publisher's own,
written into the publisher's own private folder with owner-only permissions,
and it is the only child that ever learns the credential.

ONE CREDENTIAL PER ROUTE (13 October 2026). A project's route may name a key
of its own, by bare name, inside the one protected folder the container
mounts read-only. Every rule above holds for each such file. Two more hold
for a route's key: it is never a symbolic link (it is opened with
``O_NOFOLLOW``, and the file opened has to be the very file that was looked
at), and the program git asks for it through has a name of its own, so that
two routes' programs never overwrite each other.

Nothing here names a language, a test runner, a hosting provider or a
product. It is a file path and a string.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CREDENTIAL_IS_NOT_SHOWN",
    "Credential",
    "a_remote_the_publisher_takes",
    "an_ssh_address",
    "CredentialRefusal",
    "read_the_credential",
    "a_source_the_publisher_takes",
    "the_askpass_program",
    "the_credential_environment",
    "the_environment_git_is_given",
    "the_remote_transport",
    "the_ssh_command_for",
    "the_source_transport",
]

#: What a credential says when anything asks it to describe itself.
CREDENTIAL_IS_NOT_SHOWN: str = "<the publisher's credential: not shown>"

#: The name of the program git asks the publisher-wide credential through.
THE_PUBLISHER_WIDE_PROGRAM: str = "ask-for-the-credential"


class Credential:
    """One credential, held in memory, which will not print itself.

    ``held`` is the only thing anybody outside this module asks it. The text
    is reachable only through :meth:`_secret`, which has exactly one caller
    here and none anywhere else, and which is named with a leading underscore
    so that it reads as what it is wherever it appears.
    """

    __slots__ = ("_text", "_path", "_own_program")

    def __init__(
        self,
        text: str,
        *,
        path: str | os.PathLike[str],
        own_program: bool = False,
    ) -> None:
        self._text = str(text)
        self._path = str(path)
        self._own_program = bool(own_program)

    # -- the ways a thing gets printed, all of them closed ------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial, pinned by a test
        return CREDENTIAL_IS_NOT_SHOWN

    def __str__(self) -> str:  # pragma: no cover - trivial, pinned by a test
        return CREDENTIAL_IS_NOT_SHOWN

    def __format__(self, spec: str) -> str:  # noqa: D105 - see __repr__
        return CREDENTIAL_IS_NOT_SHOWN

    @property
    def held(self) -> bool:
        """Is there a credential at all? The only question with an answer."""
        return bool(self._text.strip())

    @property
    def file(self) -> str:
        """The one named file it was read from. A path, never the secret."""
        return self._path

    @property
    def program_name(self) -> str:
        """The name of the program git asks for THIS credential through.

        The publisher-wide credential keeps the name it always had. A route's
        own key gets a name of its own, made from its file's PATH (never its
        contents), so that two routes' programs are two files: with one name,
        the second program written would overwrite the first, and the first
        route's git would send with the second route's token.
        """
        if not self._own_program:
            return THE_PUBLISHER_WIDE_PROGRAM
        digest = hashlib.sha256(self._path.encode("utf-8")).hexdigest()[:16]
        return f"{THE_PUBLISHER_WIDE_PROGRAM}.{digest}"

    def _secret(self) -> str:
        """The text itself. One caller, in this module, and nowhere else."""
        return self._text


@dataclass(frozen=True)
class CredentialRefusal:
    """Why there is no credential, in one sentence a person can act on."""

    sentence: str


def _read_without_following(
    where: Path, identity: tuple[int, int] | None
) -> str:
    """The file's text, opened with ``O_NOFOLLOW``: a symbolic link is refused.

    With ``identity`` given, the file opened has to be that very file — the
    ``(st_dev, st_ino)`` recorded when it was looked at — or nothing is read.
    """
    # Read as attributes, not through getattr(os, ...): the launch guard keeps
    # ``os`` to direct attribute use (14 October 2026).
    try:
        no_follow = os.O_NOFOLLOW
    except AttributeError:  # pragma: no cover — every POSIX system has it
        no_follow = 0
    try:
        close_on_exec = os.O_CLOEXEC
    except AttributeError:  # pragma: no cover — every POSIX system has it
        close_on_exec = 0
    flags = os.O_RDONLY | no_follow | close_on_exec
    descriptor = os.open(str(where), flags)
    try:
        found = os.fstat(descriptor)
        if identity is not None and (found.st_dev, found.st_ino) != tuple(identity):
            raise _NotTheFileLookedAt()
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class _NotTheFileLookedAt(Exception):
    """The file at the path changed between being looked at and being read."""


def read_the_credential(
    path: str | os.PathLike[str] | None,
    *,
    no_follow: bool = False,
    identity: tuple[int, int] | None = None,
    own_program: bool = False,
) -> tuple[Credential | None, CredentialRefusal | None]:
    """Read the credential ONCE, from the one named file. Never raises.

    A missing setting, a missing file, a file that cannot be read and an
    empty file are four different sentences, because they need four different
    things done about them. None of the four says anything about the file's
    contents.

    For a ROUTE's own key (13 October 2026) the publisher passes
    ``no_follow`` (the file is opened with ``O_NOFOLLOW``, so a symbolic link
    is refused), the ``identity`` it recorded when it looked at the file (the
    file opened has to be that one), and ``own_program`` (the program git
    asks for it through gets a name of its own). With none of them given,
    this reads exactly as it always has.
    """
    named = str(path or "").strip()
    if not named:
        return None, CredentialRefusal(
            "the publisher was given no credential file, so it holds no "
            "credential and can send nothing. Name one file in its settings "
            "and put the credential in it."
        )
    where = Path(named).expanduser()
    try:
        if no_follow:
            text = _read_without_following(where, identity)
        else:
            text = where.read_text(encoding="utf-8")
    except _NotTheFileLookedAt:
        return None, CredentialRefusal(
            f"the publisher's credential file {where} is not the file that "
            "was looked at a moment before (it was replaced in between), so "
            "it was not read, and the publisher holds no credential from it "
            "and can send nothing with it."
        )
    except FileNotFoundError:
        return None, CredentialRefusal(
            f"the publisher's credential file {where} is not there, so it "
            "holds no credential and can send nothing."
        )
    except OSError as exc:
        if no_follow and exc.errno == errno.ELOOP:
            return None, CredentialRefusal(
                f"the publisher's credential file {where} is a symbolic link, "
                "and a route's key has to be the file itself, so it was not "
                "read and the publisher can send nothing with it."
            )
        return None, CredentialRefusal(
            f"the publisher's credential file {where} could not be read "
            f"({exc.strerror or type(exc).__name__}), so it holds no "
            "credential and can send nothing."
        )
    if not text.strip():
        return None, CredentialRefusal(
            f"the publisher's credential file {where} is empty, so it holds "
            "no credential and can send nothing."
        )
    return Credential(text.strip(), path=str(where), own_program=own_program), None


#: The programs already written, by the folder they are in and the file they
#: read, so that the same one is handed out again instead of written over.
_ALREADY_WRITTEN: dict[tuple[str, str], Path] = {}
_WRITING_ONE = threading.Lock()


def _the_program_text(credential: Credential) -> str:
    # The publisher's own interpreter, named by its full path, so the program
    # does not depend on anything being on a PATH inside a container.
    return (
        "#!" + sys.executable + "\n"
        '"""Print the one named credential file, for git and for nothing else."""\n'
        "import sys\n"
        "with open(" + repr(credential.file) + ', "r", encoding="utf-8") as handle:\n'
        "    sys.stdout.write(handle.read().strip() + chr(10))\n"
    )


def the_askpass_program(credential: Credential, *, state_dir: Path) -> Path:
    """The small program git runs when it asks for a credential, written ONCE.

    Git's own contract: when ``GIT_ASKPASS`` names a program, git runs it with
    the prompt as its argument and reads one line of its output. So this
    writes a program that prints the one named file's contents and nothing
    else, into the publisher's own private folder, readable and runnable by
    its owner alone.

    THE CREDENTIAL IS NOT IN THIS FILE. The path of the credential file is,
    which is not a secret, and a test greps the written program to prove it.

    WRITTEN ONCE, NOT ONCE PER GIT COMMAND (22 September 2026). The publisher
    works on different projects at the same time — its one-at-a-time lock is
    per project, exactly so that it can — and every git command asked for this
    program, so two requests wrote the same file at the same moment. A file
    being written is momentarily empty or half-written, and git, running for
    the other request, could read a truncated program, get no credential out
    of it, and be refused by the remote for a reason that had nothing to do
    with the remote. Two things prevent that, and both are here because either
    alone leaves a gap:

    * **it is written once** for a given folder and credential file, and every
      call after that hands back the same path without touching the file;
    * **the write itself is whole.** The text goes into a file of its own and
      is moved into place in one step, so what git opens is either the old
      program or the new one and never a piece of one — which also holds for
      two publisher processes sharing a folder, where a lock inside one of
      them would settle nothing.
    """
    key = (str(state_dir), credential.file)
    with _WRITING_ONE:
        known = _ALREADY_WRITTEN.get(key)
        if known is not None and known.is_file():
            return known
        state_dir.mkdir(parents=True, exist_ok=True)
        # ONE PROGRAM PER CREDENTIAL (13 October 2026): a route's own key has
        # a program name of its own, so this never writes over another
        # credential's program (see Credential.program_name).
        program = state_dir / credential.program_name
        being_written = state_dir / (
            f"{credential.program_name}.{os.getpid()}.being-written"
        )
        being_written.write_text(_the_program_text(credential), encoding="utf-8")
        being_written.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        os.replace(being_written, program)
        _ALREADY_WRITTEN[key] = program
        return program


#: The two SSH forms the publisher recognises: ``ssh://user@host/path`` and
#: ``user@host:path``. Every other way git can spell an SSH address is
#: refused by the settings (:func:`a_remote_the_publisher_takes`), so no SSH
#: remote can slip past the pinned-host path.
_AN_SSH_ADDRESS = re.compile(
    r"^(?:ssh://[A-Za-z0-9._-]+@[A-Za-z0-9._-]+(?::[0-9]+)?/\S+"
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:(?!/)\S+)$"
)


def an_ssh_address(address: str) -> bool:
    """Is this remote reached over SSH (its credential then being a key)?"""
    return bool(_AN_SSH_ADDRESS.match(str(address or "").strip()))


def a_remote_the_publisher_takes(address: str) -> bool:
    """An ``https://`` address (a token) or a recognised SSH form (a key)."""
    text = str(address or "").strip()
    return text.startswith("https://") or an_ssh_address(text)


def the_remote_transport(address: str) -> str | None:
    """The one transport a remote is fetched and sent over (14 October 2026):
    ``ssh`` for the recognised SSH forms, ``https`` for an ``https://``
    address, else ``None``.

    Anything else, a local folder included, is refused here as the settings
    refuse it (:func:`a_remote_the_publisher_takes`): a remote is only ever
    reached over the network, with the route's own credential.
    """
    text = str(address or "").strip()
    if an_ssh_address(text):
        return "ssh"
    if text.startswith("https://"):
        return "https"
    return None


def the_source_transport(address: str) -> str | None:
    """The one transport the project's copy is read over (14 October 2026):
    ``git`` for a ``git://`` address (the factory's read-only export), ``file``
    for an absolute local folder, else ``None``. ``file://``, ``http(s)``,
    ``ssh``, ``ext::`` and relative paths are not taken."""
    from forge.network_git import transport_of

    found = transport_of(str(address or "").strip())
    return found if found in ("git", "file") else None


def a_source_the_publisher_takes(address: str) -> bool:
    """A ``git://`` address or an absolute local folder (see
    :func:`the_source_transport`)."""
    return the_source_transport(address) is not None


def the_ssh_command_for(key_file: str, known_hosts: str) -> str:
    """The ssh git runs for ONE key file and ONE pinned host list.

    Shared with ``forge register-repo``, whose check clone of a private
    repository uses the publisher's route key in exactly this way.
    """
    return _the_ssh_command(Credential("", path=key_file), known_hosts)


def _the_ssh_command(credential: Credential, known_hosts: str) -> str:
    """The ssh git runs: this one key, this one pinned host list, nothing else.

    No agent, no person's configuration, no other key, and a host whose key
    is not in the pinned file is refused rather than trusted on first sight.
    """
    return " ".join(
        [
            # The ssh program by its full path, found from the publisher's own
            # PATH (14 October 2026): git's environment is given a fixed PATH,
            # not the publisher's.
            shlex.quote(shutil.which("ssh") or "ssh"),
            "-F", "/dev/null",
            "-i", shlex.quote(credential.file),
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "UserKnownHostsFile=" + shlex.quote(known_hosts),
            "-o", "GlobalKnownHostsFile=/dev/null",
        ]
    )


def the_credential_environment(
    credential: Credential | None,
    *,
    state_dir: Path,
    known_hosts: str | None = None,
) -> dict[str, str]:
    """The ONE credential value a publisher git command may carry, or none.

    ``GIT_SSH_COMMAND`` naming the route's own key and the pinned host list
    for a remote reached over SSH; ``GIT_ASKPASS`` naming the publisher's own
    program, which reads the one named credential file, for ``https``. A
    path, never the secret.
    """
    if credential is None or not credential.held:
        return {}
    if known_hosts is not None:
        return {"GIT_SSH_COMMAND": _the_ssh_command(credential, known_hosts)}
    return {"GIT_ASKPASS": str(the_askpass_program(credential, state_dir=state_dir))}


def the_environment_git_is_given(
    credential: Credential | None,
    *,
    state_dir: Path,
    home: Path,
    known_hosts: str | None = None,
    transport: str = "none",
) -> dict[str, str]:
    """The WHOLE environment a publisher git command is given.

    Built from nothing, not copied from this process: a child that is handed
    the publisher's own environment is handed whatever else is in it. Since
    14 October 2026 it is :func:`forge.network_git.isolated_environment`:
    ``HOME`` is a FRESH EMPTY folder made for each command and removed after
    it (``home`` here), never the publisher's state folder, so git and its
    HTTP library find no ``.netrc``, ``.gitconfig`` or XDG settings file
    there; no system or personal settings file is read; exactly one
    ``transport`` is allowed; and the only credential is the one from
    :func:`the_credential_environment`. None of the values is a secret.
    """
    from forge.network_git import isolated_environment

    return isolated_environment(
        transport,
        Path(home),
        the_credential_environment(
            credential, state_dir=state_dir, known_hosts=known_hosts
        ),
    )
