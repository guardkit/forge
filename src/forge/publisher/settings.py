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
* **for a project that needs a key of its own** (13 October 2026): the bare
  NAME of that key, never a path. The name is a file directly inside the one
  protected, read-only folder the container mounts
  (:data:`ROUTE_KEYS_DIR`). A route that names no key uses the
  publisher-wide credential, exactly as before;
* **the folder names that exist only on the factory's own machines**
  (``private_path_prefixes``, 14 October 2026): each owner's home folder, the
  receipts folder, the sandbox storage folder. Required. Before every send
  the publisher refuses a commit that would publish a file holding one
  (:mod:`forge.publisher.git_work`). The values live in the deploy settings
  file only, never in code, and are never logged: the start-up line gives a
  count;
* **whether it publishes only to private repositories** (``private_only``,
  17 October 2026; design "Building production features from a signed-off
  prototype", section 2.8). Off unless the file says ``true``, which is
  today's behaviour exactly. When on, a route is served only when its
  repository is confirmed private (:mod:`forge.publisher.private_remote`),
  checked at start-up and again immediately before every push;
* **where to listen** — an address and a port. The loopback address by
  default; every address inside its own container when it is run as the
  service the compose fragment beside this package describes, which
  publishes no port and shares its network with the coordinator alone.

A PROJECT THE PUBLISHER WAS NOT TOLD ABOUT IS REFUSED. There is no default
address, no guess from a name and no lookup anywhere else: a project the
settings do not name cannot be published for, and the refusal says so.

NOTHING HERE NAMES a language, a test runner, a web protocol, a database, a
package manager, a hosting provider or a product. Two git addresses and a
branch name that came off the ledger are the whole of what it knows.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.publisher.credential import (
    a_remote_the_publisher_takes,
    a_source_the_publisher_takes,
    an_ssh_address,
)

__all__ = [
    "ROUTE_KEYS_DIR",
    "ProjectRoute",
    "PublisherSettings",
    "SettingsRefused",
    "a_route_key_name",
    "load_settings",
    "the_route_key_file",
]

#: The one folder every route's own key lives in, directly. The container
#: mounts it read-only; a route names a file in it by bare name and can name
#: nothing anywhere else. Read at call time, never copied, so a test can point
#: it at a folder of its own.
ROUTE_KEYS_DIR: str = "/etc/forge-publisher/route-keys"

#: A key's bare name: a letter or digit, then letters, digits, dot, underscore
#: or hyphen, at most 100 characters in all. No slash, so never a path; no
#: whitespace or newline. ``.`` and ``..`` cannot match (they start with a
#: dot) and are refused by name as well.
_A_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

#: The sentence that says what a key name is, shared by the settings and by
#: ``forge register-repo --publish-credential-name``.
WHAT_A_KEY_NAME_IS: str = (
    "the bare name of a file directly inside the publisher's route-keys "
    "folder: a letter or digit, then letters, digits, dot, underscore or "
    "hyphen, at most 100 characters, never a path"
)


def a_route_key_name(value: object) -> bool:
    """Is this a key name a route may give? Strings only; never a path."""
    return (
        isinstance(value, str)
        and value not in (".", "..")
        and _A_KEY_NAME.fullmatch(value) is not None
    )


def the_route_key_file(name: str) -> Path:
    """Where the key with this bare name is: directly inside the one folder."""
    return Path(ROUTE_KEYS_DIR) / name


class SettingsRefused(ValueError):
    """The settings could not be read, in one sentence a person can act on."""


@dataclass(frozen=True)
class ProjectRoute:
    """The two addresses the publisher has for one project.

    ``source`` is read-only by contract: the publisher only ever fetches from
    it, and a test pins that no code path here writes to it.

    ``credential_name`` is the bare name of this project's own key, or
    ``None`` to use the publisher-wide credential. It is never a path: the
    file is :func:`the_route_key_file` of it, and nothing else.
    """

    name: str
    source: str
    remote: str
    credential_name: str | None = None


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
    #: The pinned host keys for remotes reached over SSH, as a file. Never
    #: fetched at run time; needed only when a remote is an SSH address.
    known_hosts_file: str = ""
    #: Folder names that exist only on the factory's own machines. Required
    #: in the settings file (:func:`load_settings` refuses it missing, empty
    #: or generic). With none, nothing is sent: the publisher cannot look.
    private_path_prefixes: tuple[str, ...] = ()
    #: Publish only to repositories confirmed private (17 October 2026).
    #: False unless the settings file says ``true``: today's behaviour.
    private_only: bool = False

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
                name: {
                    "source": route.source,
                    "remote": route.remote,
                    # The NAME of the route's own key, which is not a secret;
                    # left out for a route that uses the publisher-wide one.
                    **(
                        {"credential_name": route.credential_name}
                        if route.credential_name is not None
                        else {}
                    ),
                }
                for name, route in sorted(self.projects.items())
            },
            "host": self.host,
            "port": self.port,
            "known_hosts_file": self.known_hosts_file,
            # A count, never the values: they name folders on the factory's
            # own machines.
            "private_path_prefix_count": len(self.private_path_prefixes),
            "private_only": self.private_only,
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


def _a_route_s_key_name(block: Any, project: str) -> str | None:
    """The route's own key name: absent means inherit; anything else is checked.

    A STRICT reader, not :func:`_text`: that one turns a number, a list or
    ``null`` into "nothing", which here would quietly mean "use the
    publisher-wide key" — the one fallback this field must never have. So a
    present field is either a key name or a refusal naming the route. The
    value is not repeated in the refusal: whatever was put there by mistake
    (a key's own text, say) is not written into a log.
    """
    if not isinstance(block, dict) or "credential_name" not in block:
        return None
    value = block["credential_name"]
    if not a_route_key_name(value):
        raise SettingsRefused(
            f"'credential_name' of project '{project}' in the publisher's "
            f"settings is not a key name: it must be {WHAT_A_KEY_NAME_IS} "
            f"({ROUTE_KEYS_DIR}). Leave the field out to use the "
            "publisher-wide credential. The value is not repeated here."
        )
    return str(value)


#: THE RULE FOR A PRIVATE FOLDER (coach review 1). A value is refused when:
#:
#: 1. it is not an absolute path at least two folders deep;
#: 2. it is under a system folder (:data:`_SYSTEM_FOLDERS`), at any depth;
#: 3. it is exactly two deep under a shared top folder (:data:`_SHARED_TOPS`),
#:    such as ``/opt/app`` or ``/var/lib``;
#: 4. it is the home folder of a common container or CI user
#:    (:data:`_SHARED_USERS`), such as ``/home/node``.
#:
#: Every one of these is a folder that ordinary products name, so it would
#: refuse ordinary work. A factory machine's own folders (an owner's home
#: folder, the receipts folder, the sandbox storage folder) pass.
_SYSTEM_FOLDERS = frozenset(
    {"usr", "bin", "sbin", "lib", "lib64", "etc", "proc", "sys", "dev", "run", "boot"}
)
_SHARED_TOPS = frozenset(
    {"var", "opt", "tmp", "srv", "root", "mnt", "media", "app", "data",
     "workspace", "workspaces", "private", "Volumes"}
)
_SHARED_USERS = frozenset(
    {"node", "runner", "ubuntu", "user", "app", "admin", "root", "vscode",
     "codespace", "jenkins", "circleci", "gitpod", "coder", "docker", "guest",
     "Shared", "www-data", "dev"}
)


def _a_shared_folder(parts: list[str]) -> bool:
    """Rules 2 to 4 above, for a path's folder names (rule 1 already held)."""
    if parts[0] in _SYSTEM_FOLDERS:
        return True
    if len(parts) == 2 and parts[0] in _SHARED_TOPS:
        return True
    return len(parts) == 2 and parts[0] in ("home", "Users") and parts[1] in _SHARED_USERS


#: Why a ``private_path_prefixes`` value was refused. The values are not
#: repeated: they name folders on the factory's own machines.
_PREFIXES_SENTENCE = (
    "'private_path_prefixes' in the publisher's settings must be a list of "
    "the folders that exist only on the factory's own machines (each owner's "
    "home folder, the receipts folder, the sandbox storage folder), each an "
    "absolute path at least two folders deep and not a folder ordinary "
    "products name, such as /home/, /var/lib, /opt/app, /usr/local/bin or "
    "/home/node. Before every send the publisher refuses a commit that "
    "would publish a file naming one, so it will not start without them. The "
    "values are not repeated here"
)


def _private_path_prefixes(decoded: dict[str, Any]) -> tuple[str, ...]:
    """The required ``private_path_prefixes``: a non-empty list of absolute
    folders, each at least two deep and none a shared folder. Each is kept
    without its trailing ``/``."""
    value = decoded.get("private_path_prefixes")
    if not isinstance(value, list) or not value:
        raise SettingsRefused(_PREFIXES_SENTENCE)
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SettingsRefused(_PREFIXES_SENTENCE)
        text = item.strip()
        if not text.startswith("/") or "\0" in text or ".." in text.split("/"):
            raise SettingsRefused(_PREFIXES_SENTENCE)
        normal = posixpath.normpath(text)
        if normal.startswith("//"):
            normal = "/" + normal.lstrip("/")
        parts = [part for part in normal.split("/") if part]
        if len(parts) < 2 or _a_shared_folder(parts):
            raise SettingsRefused(_PREFIXES_SENTENCE)
        if normal not in kept:
            kept.append(normal)
    return tuple(kept)


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
        remote = _text(block, "remote", where=f"projects.{readable}")
        if not a_remote_the_publisher_takes(remote):
            raise SettingsRefused(
                f"the remote of project '{readable}' must be an https:// address "
                "(pushed with a token) or an SSH address written as "
                "user@host:path or ssh://user@host/path (pushed with a deploy "
                "key); any other form is refused"
            )
        source = _text(block, "source", where=f"projects.{readable}")
        if not a_source_the_publisher_takes(source):
            raise SettingsRefused(
                f"the source of project '{readable}' must be a git:// address (the "
                "factory's read-only export of the project's copy) or an absolute "
                "local folder; file://, http(s), ssh, ext:: and relative paths are "
                "refused (the value is not repeated here)"
            )
        projects[readable] = ProjectRoute(
            name=readable,
            source=source,
            remote=remote,
            credential_name=_a_route_s_key_name(block, readable),
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
    known_hosts = _text(decoded, "known_hosts_file", where="the settings", required=False)
    if not known_hosts and any(an_ssh_address(r.remote) for r in projects.values()):
        raise SettingsRefused(
            "a project's remote is an SSH address, so the publisher's settings "
            "need 'known_hosts_file': the file holding that host's pinned key"
        )
    credential_file = _text(decoded, "credential_file", where="the settings")
    ledger = _text(decoded, "ledger", where="the settings")
    state_dir = _text(decoded, "state_dir", where="the settings")
    private_path_prefixes = _private_path_prefixes(decoded)
    private_only = decoded.get("private_only", False)
    if not isinstance(private_only, bool):
        raise SettingsRefused(
            "'private_only' in the publisher's settings must be true or false "
            "(true: publish only to repositories confirmed private); leave it "
            "out for false"
        )
    return PublisherSettings(
        credential_file=credential_file,
        ledger=ledger,
        state_dir=state_dir,
        projects=projects,
        host=_text(decoded, "host", where="the settings", required=False)
        or "127.0.0.1",
        port=int(port),
        git_timeout_seconds=float(timeout),
        known_hosts_file=known_hosts,
        private_path_prefixes=private_path_prefixes,
        private_only=private_only,
    )
