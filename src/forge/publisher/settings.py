"""What the publisher is told, and nothing more than it needs.

One-true-copy design pass, item 1, second revision section D. The publisher
does ONE thing, so it is told only the things that one thing needs:

* **the one named file the credential is in** — its single setting for the
  credential, and the only source there is (:mod:`forge.publisher.credential`);
* **where the ledger is**, which it opens READ-ONLY and never writes;
* **for each project it may send for: two addresses.** The first is the
  read-only git service the project's copy is reachable at — the sandbox's
  own git service for a project built in a sandbox, or the copy itself for a
  project built here. The joined commit is brought out through that and never
  through a writable path. The second is where the remote named ``origin``
  is for that project, which is the only place anything is ever sent;
* **its own private folder**, where it keeps its own copy of each project's
  commits and the small program git asks for the credential through;
* **where to listen**, on loopback.

A PROJECT THE PUBLISHER WAS NOT TOLD ABOUT IS REFUSED. There is no default
address, no guess from a name and no lookup anywhere else: a project the
settings do not name cannot be published for, and the refusal says so.

NOTHING HERE NAMES a language, a test runner, a web protocol, a database, a
package manager, a hosting provider or a product. Two git addresses and a
branch name that came off the ledger are the whole of what it knows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ProjectRoute",
    "PublisherSettings",
    "SettingsRefused",
    "load_settings",
]


class SettingsRefused(ValueError):
    """The settings could not be read, in one sentence a person can act on."""


@dataclass(frozen=True)
class ProjectRoute:
    """The two addresses the publisher has for one project.

    ``source`` is read-only by contract: the publisher only ever fetches from
    it, and a test pins that no code path here writes to it.
    """

    name: str
    source: str
    remote: str


@dataclass(frozen=True)
class PublisherSettings:
    """Everything the publisher is told."""

    credential_file: str
    ledger: str
    state_dir: str
    projects: dict[str, ProjectRoute] = field(default_factory=dict)
    host: str = "127.0.0.1"
    #: 0 means "let the kernel pick", which is what a test and a bench want.
    port: int = 0
    git_timeout_seconds: float = 180.0

    def route(self, project: str) -> ProjectRoute | None:
        return self.projects.get(str(project or "").strip())

    def without_secrets(self) -> dict[str, Any]:
        """The settings as they may be logged: paths and addresses, no secret.

        The credential file's PATH is here, because a path is not a secret and
        a person reading a log needs to know which file the publisher was
        told to use. Its contents are never read by anything in this module.
        """
        return {
            "credential_file": self.credential_file,
            "ledger": self.ledger,
            "state_dir": self.state_dir,
            "projects": {
                name: {"source": route.source, "remote": route.remote}
                for name, route in sorted(self.projects.items())
            },
            "host": self.host,
            "port": self.port,
        }


def _text(block: Any, key: str, *, where: str, required: bool = True) -> str:
    value = block.get(key) if isinstance(block, dict) else None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise SettingsRefused(
                f"the publisher's settings need '{key}' in {where}, as one "
                f"non-empty line of text"
            )
        return ""
    return value.strip()


def load_settings(path: str | Path) -> PublisherSettings:
    """Read the publisher's settings from one file. Never guesses.

    The file is a plain JSON object. Every refusal is one sentence naming the
    line to fix, because the only person who ever reads it is the one setting
    the publisher up.
    """
    where = Path(path).expanduser()
    try:
        raw = where.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsRefused(
            f"the publisher's settings file {where} could not be read "
            f"({exc.strerror or type(exc).__name__})"
        ) from exc
    try:
        decoded = json.loads(raw)
    except ValueError as exc:
        raise SettingsRefused(
            f"the publisher's settings file {where} is not readable as JSON: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise SettingsRefused(
            f"the publisher's settings file {where} must hold one object"
        )
    projects: dict[str, ProjectRoute] = {}
    declared = decoded.get("projects")
    if declared is not None and not isinstance(declared, dict):
        raise SettingsRefused(
            "'projects' in the publisher's settings must be an object of "
            "project name to {source, remote}"
        )
    for name, block in sorted((declared or {}).items()):
        readable = str(name).strip()
        if not readable:
            raise SettingsRefused("a project in 'projects' has no name")
        projects[readable] = ProjectRoute(
            name=readable,
            source=_text(block, "source", where=f"projects.{readable}"),
            remote=_text(block, "remote", where=f"projects.{readable}"),
        )
    port = decoded.get("port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or port < 0:
        raise SettingsRefused(
            "'port' in the publisher's settings must be a whole number, 0 to "
            "let the kernel pick one"
        )
    timeout = decoded.get("git_timeout_seconds", 180.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SettingsRefused(
            "'git_timeout_seconds' in the publisher's settings must be a "
            "number of seconds greater than zero"
        )
    return PublisherSettings(
        credential_file=_text(decoded, "credential_file", where="the settings"),
        ledger=_text(decoded, "ledger", where="the settings"),
        state_dir=_text(decoded, "state_dir", where="the settings"),
        projects=projects,
        host=_text(decoded, "host", where="the settings", required=False)
        or "127.0.0.1",
        port=int(port),
        git_timeout_seconds=float(timeout),
    )
