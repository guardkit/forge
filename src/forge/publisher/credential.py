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

Nothing here names a language, a test runner, a hosting provider or a
product. It is a file path and a string.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CREDENTIAL_IS_NOT_SHOWN",
    "Credential",
    "CredentialRefusal",
    "read_the_credential",
    "the_askpass_program",
    "the_environment_git_is_given",
]

#: What a credential says when anything asks it to describe itself.
CREDENTIAL_IS_NOT_SHOWN: str = "<the publisher's credential: not shown>"


class Credential:
    """One credential, held in memory, which will not print itself.

    ``held`` is the only thing anybody outside this module asks it. The text
    is reachable only through :meth:`_secret`, which has exactly one caller
    here and none anywhere else, and which is named with a leading underscore
    so that it reads as what it is wherever it appears.
    """

    __slots__ = ("_text", "_path")

    def __init__(self, text: str, *, path: str | os.PathLike[str]) -> None:
        self._text = str(text)
        self._path = str(path)

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

    def _secret(self) -> str:
        """The text itself. One caller, in this module, and nowhere else."""
        return self._text


@dataclass(frozen=True)
class CredentialRefusal:
    """Why there is no credential, in one sentence a person can act on."""

    sentence: str


def read_the_credential(
    path: str | os.PathLike[str] | None,
) -> tuple[Credential | None, CredentialRefusal | None]:
    """Read the credential ONCE, from the one named file. Never raises.

    A missing setting, a missing file, a file that cannot be read and an
    empty file are four different sentences, because they need four different
    things done about them. None of the four says anything about the file's
    contents.
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
        text = where.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, CredentialRefusal(
            f"the publisher's credential file {where} is not there, so it "
            "holds no credential and can send nothing."
        )
    except OSError as exc:
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
    return Credential(text.strip(), path=str(where)), None


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
        program = state_dir / "ask-for-the-credential"
        being_written = state_dir / (
            f"ask-for-the-credential.{os.getpid()}.being-written"
        )
        being_written.write_text(_the_program_text(credential), encoding="utf-8")
        being_written.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        os.replace(being_written, program)
        _ALREADY_WRITTEN[key] = program
        return program


def the_environment_git_is_given(
    credential: Credential | None, *, state_dir: Path, home: Path
) -> dict[str, str]:
    """The WHOLE environment the publisher's git commands are given.

    Built from nothing, not copied from this process: a child that is handed
    the publisher's own environment is handed whatever else is in it. Only
    these names are set, and none of their values is a credential.

    ``HOME`` is the publisher's own private folder, so git reads no person's
    configuration and finds no person's stored credentials: the only
    credential in reach is the one named file.
    """
    given = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        # Never stop and ask a person: there is nobody at this end.
        "GIT_TERMINAL_PROMPT": "0",
        # Nothing this process does needs a system or a person's config.
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    if credential is not None and credential.held:
        given["GIT_ASKPASS"] = str(the_askpass_program(credential, state_dir=state_dir))
    return given
