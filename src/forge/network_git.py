"""The one place Forge's git talks to another repository.

THE RULE. Forge never runs a network git command in a repository whose
settings a project or a model could have written. A project's clone is shared
with the build's model worktrees, so its ``.git/config`` can name an address
rewrite (``url.*.insteadOf``), an ssh program, a credential helper, an askpass
program, extra HTTP headers, a proxy program or an upload-pack program, and
plain git would use every one of them. So every network command here:

* runs only in a repository the factory made and only the factory writes to
  (a bare "factory copy" per fetch address, or the publisher's own copy), or
  with no repository at all (``clone``, or ``--git-dir`` naming an empty one);
* gets an environment built from nothing: ``PATH``; ``HOME`` and the working
  folder set to a fresh empty folder made for this one command and removed
  after it, with ``XDG_CONFIG_HOME`` inside it, so no ``.netrc``,
  ``.gitconfig`` or XDG settings file can be found; ``GIT_CONFIG_NOSYSTEM=1``
  and ``GIT_CONFIG_GLOBAL=/dev/null``, so no system or personal settings file
  is read; ``GIT_TERMINAL_PROMPT=0``; an empty ``GIT_ASKPASS`` and
  ``SSH_ASKPASS`` (unless the factory names its own); ``GIT_NO_LAZY_FETCH=1``;
* allows exactly ONE transport, the one the address's checked form uses
  (``GIT_ALLOW_PROTOCOL``);
* clears every credential source on the command line
  (:data:`NETWORK_SETTINGS`): credential helpers, extra headers, cookie
  files; hooks and background work are off;
* uses the address the factory wrote down and the credential the factory
  chose (or none), and nothing from the project.

Git's own words never leave this module: every result carries a fixed
sentence (:func:`forge.project_git.failure_sentence`), and at most one of a
few recognised reasons (:data:`REASONS`), recognised from git's text but never
quoted.

FETCHING A PROJECT'S STARTING POINT is two steps (:func:`fetch_start_point`,
:func:`fetch_all_branches`): the network step, into the factory's own bare
copy (:func:`factory_copy_for`), then a local-only step that brings the
commits into the project's clone (:func:`bring_in_from_copy`, under every
local rule of :mod:`forge.project_git`), after which the clone's
remote-tracking branches must equal what the copy holds.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from forge import project_git

logger = logging.getLogger(__name__)

__all__ = [
    "COPY_REFUSAL",
    "CREDENTIAL_ENVIRONMENT_NAMES",
    "LOCAL_TRANSPORTS",
    "NetworkRefused",
    "FETCH_ADDRESS_SETTING",
    "FETCH_COPIES_ENV",
    "FETCH_TRANSPORTS",
    "GIT_DOOR_SETTING",
    "NETWORK_SETTINGS",
    "NetworkResult",
    "PROXY_NAMES",
    "REASONS",
    "StartPoint",
    "bring_in_from_copy",
    "checked_proxies",
    "check_fetch_address",
    "factory_copy_for",
    "fetch_all_branches",
    "fetch_default_branch_copy",
    "fetch_start_point",
    "git_door_address",
    "git_door_problem",
    "https_address_problem",
    "isolated_environment",
    "no_fetch_address_sentence",
    "proxy_environment",
    "run_network_git",
    "sandbox_git_command",
    "sandbox_network_git",
    "sandbox_proxy_environment",
    "transport_of",
]

#: Settings on every network command line. Command-line settings beat every
#: settings file; an empty ``credential.helper`` or ``http.extraHeader``
#: empties the list git would otherwise build.
NETWORK_SETTINGS: Tuple[str, ...] = (
    "credential.helper=",
    "http.extraHeader=",
    "http.cookieFile=",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "fetch.recurseSubmodules=false",
    "submodule.recurse=false",
)

#: The transports a project's start-point fetch address may use. Today the
#: sandbox reaches public repositories over ``https`` only.
FETCH_TRANSPORTS: Tuple[str, ...] = ("https",)

#: The coordinator setting that records each project's fetch address.
FETCH_ADDRESS_SETTING: str = "planning.fetch_addresses"

#: The coordinator setting naming the Git door (17 October 2026): the one
#: ``http://<address>:<port>/`` base a private repository is read through.
#: The door, on the factory's own machine, adds a read-only token and allows
#: only git's two read requests, so the sandbox still holds no credential.
#: An ``http`` fetch address is taken only when it is this base followed by
#: ``<owner>/<name>.git``; with no door declared, only ``https`` is taken.
GIT_DOOR_SETTING: str = "planning.git_door"

#: Where the factory's own fetch copies live, when set. Otherwise a folder of
#: this container's own temporary space (lost on restart; the first fetch
#: after that downloads the history again).
FETCH_COPIES_ENV: str = "FORGE_FETCH_COPIES_DIR"

#: The only credential values the factory may hand a network command: the ssh
#: program naming its own key and pinned hosts, or its own askpass program.
CREDENTIAL_ENVIRONMENT_NAMES: Tuple[str, ...] = ("GIT_SSH_COMMAND", "GIT_ASKPASS")

#: Proxy settings copied when set, in either case; one carrying a user name or
#: password is refused (the same rule a project's own programs follow).
PROXY_NAMES: Tuple[str, ...] = tuple(
    name
    for base in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")
    for name in (base, base.lower())
)

#: The reasons recognised from git's text, by key, as the sentence said. Git's
#: own text is never quoted.
REASONS: Dict[str, str] = {
    "moved-on": "the remote refused the send because its branch has moved on",
    "key-refused": "the remote refused the key",
    "host-key": "the remote's host key is not the pinned one",
}

_REASON_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("moved-on", re.compile(r"\[rejected\]|non-fast-forward|fetch first", re.I)),
    ("host-key", re.compile(r"host key verification failed|no \S+ host key is known|"
                            r"remote host identification has changed", re.I)),
    ("key-refused", re.compile(r"permission denied \(publickey|authentication failed|"
                               r"could not read username|could not read password|"
                               r"invalid username or password", re.I)),
)

_DEFAULT_TIMEOUT_S = 180.0


class NetworkRefused(OSError):
    """A network command not started. ``str()`` is the plain sentence."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence

    def __str__(self) -> str:
        return self.sentence


@dataclass(frozen=True)
class NetworkResult:
    """What one network-boundary git command did, without git's own words."""

    returncode: int
    stdout: str = ""
    sentence: str = ""
    reason: Optional[str] = None
    #: The output as raw bytes, for a command run with ``binary=True`` (the
    #: publisher's whole-blob read); empty otherwise.
    data: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def said(self) -> str:
        """The fixed sentence, with the recognised reason when there is one."""
        if self.ok:
            return ""
        if self.reason:
            return f"{self.sentence} ({REASONS[self.reason]})"
        return self.sentence


# ---------------------------------------------------------------------------
# Addresses


_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:(?!/)\S+$")


def transport_of(address: str) -> Optional[str]:
    """The one transport an address uses, or ``None`` for a form not taken.

    ``https://``, ``http://``, ``git://``, ``ssh://`` and the ``user@host:path``
    ssh form, or an absolute local folder (``file``). ``file://`` addresses,
    ``ext::`` programs and relative paths are not taken.
    """
    text = str(address or "").strip()
    if not text or any(ch.isspace() for ch in text) or "::" in text.split("/", 1)[0]:
        return None
    for scheme in ("https", "http", "git", "ssh"):
        if text.startswith(scheme + "://"):
            return scheme
    if _SCP_LIKE.match(text):
        return "ssh"
    if text.startswith("/") and "://" not in text:
        return "file"
    return None


def https_address_problem(address: str) -> Optional[str]:
    """Why ``address`` is not a plain ``https://<host>/<path>`` address.

    The answer never repeats the address: what was typed may hold a
    credential (``https://<token>@host/...``), so only the shape that was
    wrong is named. ``None`` when it is a plain https address.
    """
    text = str(address or "").strip()
    for char, called in (("@", "an @ (a user name or token before the host)"),
                         ("?", "a ? (a query)"), ("#", "a # (a fragment)")):
        if char in text:
            return f"it contains {called}"
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        return "it contains a space or a control character"
    if not text.startswith("https://") or text.startswith("https:///"):
        return "it does not begin with https:// and a host"
    try:
        host = urlsplit(text).hostname
    except ValueError:
        host = None
    if not host:
        return "it names no host"
    if urlsplit(text).path.strip("/") == "":
        return "it names no repository"
    return None


#: One part of a repository's ``owner/name``: a letter or digit first, so it
#: is never ``.``, ``..`` or an option, then letters, digits, dot, underscore
#: or hyphen.
_A_REPO_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_DOOR_REPO = re.compile(rf"^({_A_REPO_PART})/({_A_REPO_PART})\.git$")
_A_HOST_NAME = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def git_door_problem(door: Optional[str]) -> Optional[str]:
    """Why ``door`` is not a Git door base address, else ``None``.

    A Git door base is exactly ``http://<address>:<port>/`` (17 October
    2026): ``http`` only, one specific address (an IPv4 address, an IPv6
    address in brackets, or a host name; never an "every address" one), an
    explicit port from 1 to 65535 written plainly, and the path exactly
    ``/``. No user name, query or fragment. The answer never repeats the
    value.
    """
    import ipaddress

    text = str(door or "")
    if not text.strip():
        return "it is empty"
    if text != text.strip() or any(ch.isspace() or ord(ch) < 32 for ch in text):
        return "it contains a space or a control character"
    for char, called in (("@", "an @ (a user name or token before the address)"),
                         ("?", "a ? (a query)"), ("#", "a # (a fragment)")):
        if char in text:
            return f"it contains {called}"
    if not text.startswith("http://"):
        return "it does not begin with http://"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return "its address or port cannot be read"
    host = parts.hostname or ""
    if not host:
        return "it names no address"
    if port is None:
        return "it names no port"
    if parts.path != "/":
        return "its path is not exactly /"
    try:
        found = ipaddress.ip_address(host)
    except ValueError:
        found = None
    if found is not None:
        if found.is_unspecified or str(found) == "255.255.255.255":
            return "it names every address rather than one"
        shown = f"[{host}]" if found.version == 6 else host
        if found.version == 4 and host != str(found):
            return "its address is not written plainly"
    else:
        if not _A_HOST_NAME.match(host) or host.replace(".", "").isdigit():
            return "its address is not one address or host name"
        shown = host
    if not 1 <= port <= 65535 or parts.netloc.lower() != f"{shown}:{port}".lower():
        return "its port is not written plainly as a number from 1 to 65535"
    return None


def git_door_address(door: str, repo: str) -> str:
    """The fetch address of ``repo`` (``owner/name``) through ``door``.

    Raises ``ValueError`` when the door or the name will not do.
    """
    problem = git_door_problem(door)
    if problem:
        raise ValueError(f"the Git door ({GIT_DOOR_SETTING}) will not do: {problem}")
    address = f"{door}{str(repo or '').strip()}.git"
    if not _DOOR_REPO.match(address[len(door):]):
        raise ValueError(f"{repo!r} is not a repository name of the form owner/name")
    return address


def _door_address_problem(text: str, git_door: str) -> Optional[str]:
    """Why the http address ``text`` is not ``git_door`` + ``owner/name.git``."""
    problem = git_door_problem(git_door)
    if problem:
        return (
            f"The declared Git door ({GIT_DOOR_SETTING}) is not an "
            f"http://<address>:<port>/ address: {problem} (the value is not "
            "repeated here)."
        )
    if not text.startswith(git_door) or not _DOOR_REPO.match(text[len(git_door):]):
        return (
            "The recorded fetch address is an http:// address, and the factory "
            "takes one only when it is the declared Git door "
            f"({GIT_DOOR_SETTING}) followed by <owner>/<name>.git; this one is "
            "not (the value is not repeated here)."
        )
    return None


def check_fetch_address(
    address: Optional[str], *, git_door: Optional[str] = None
) -> Optional[str]:
    """A plain sentence when ``address`` may not be fetched from, else ``None``.

    The address must use one of :data:`FETCH_TRANSPORTS`; for ``https`` it
    must be a plain address (no user name, query or fragment).

    ``git_door`` (17 October 2026) is the declared Git door
    (:data:`GIT_DOOR_SETTING`). With it, an ``http`` address is also taken,
    but only when it begins with exactly that door and the rest is
    ``<owner>/<name>.git``. Without it the rule is today's.
    """
    text = str(address or "").strip()
    if not text:
        return "No fetch address was given."
    transport = transport_of(text)
    if transport == "http" and git_door is not None:
        return _door_address_problem(text, git_door)
    if transport not in FETCH_TRANSPORTS:
        return (
            "The recorded fetch address is not one the factory fetches from: it "
            f"must be a plain {' or '.join(t + '://' for t in FETCH_TRANSPORTS)} "
            "address (the value is not repeated here)."
        )
    if transport in ("https", "http"):
        problem = https_address_problem(
            text if transport == "https" else "https://" + text[len("http://"):]
        )
        if problem:
            return (
                f"The recorded fetch address is not a plain address: {problem} "
                "(the value is not repeated here)."
            )
    return None


def no_fetch_address_sentence(repo: Optional[str]) -> str:
    """The refusal for a project with no recorded fetch address."""
    named = f" for {repo}" if repo else ""
    return (
        f"No fetch address is recorded{named}, so the factory will not fetch "
        f"its starting point. Record the project's https address under "
        f"{FETCH_ADDRESS_SETTING} in the coordinator's settings (forge "
        f"register-repo writes it), then ask again."
    )


def address_for_path(
    target_repo_paths: Mapping[str, str],
    fetch_addresses: Mapping[str, str],
    repo_path: "str | os.PathLike[str]",
) -> Tuple[Optional[str], Optional[str]]:
    """``(org/name, recorded fetch address)`` for the project whose clone is
    at ``repo_path``, by the coordinator's own two settings. Either may be
    ``None``; the clone's own origin is never read."""
    wanted = os.path.normpath(os.fspath(repo_path))
    for repo, path in target_repo_paths.items():
        if os.path.normpath(str(path)) == wanted:
            return repo, fetch_addresses.get(repo)
    return None, None


# ---------------------------------------------------------------------------
# The isolated environment


#: Transports that reach nothing over a network, so no proxy is looked at.
LOCAL_TRANSPORTS: Tuple[str, ...] = ("none", "file")


def checked_proxies(values: Mapping[str, str]) -> Dict[str, str]:
    """The proxy settings among ``values`` that may be handed to git.

    Only :data:`PROXY_NAMES`, only when set to something, and never one that
    carries a user name or password: that raises :class:`NetworkRefused`
    with a sentence naming the setting (never its value).
    """
    found: Dict[str, str] = {}
    for name in PROXY_NAMES:
        value = str(values.get(name) or "")
        if not value:
            continue
        if any(ord(ch) < 32 for ch in value):
            raise NetworkRefused(
                f"The proxy setting {name} holds a control character, so the "
                "command was not run (the value is not shown)."
            )
        if name.upper() != "NO_PROXY":
            try:
                parts = urlsplit(value if "://" in value else "http://" + value)
                carries = bool(parts.username or parts.password)
            except ValueError:
                carries = True
            if carries:
                raise NetworkRefused(
                    f"The proxy setting {name} carries a user name or password, and "
                    "the factory does not pass credentials to git through a proxy, "
                    "so the command was not run (the value is not shown)."
                )
        found[name] = value
    return found


def proxy_environment() -> Dict[str, str]:
    """This process's login-free proxy settings (see :func:`checked_proxies`)."""
    return checked_proxies(os.environ)


def _proxy_values() -> Dict[str, str]:
    return proxy_environment()


def isolated_environment(
    transport: str,
    home: Path,
    credential_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The WHOLE environment a network git command gets. Built from nothing.

    ``transport`` is the one transport allowed (``none`` for a command that
    needs none). Login-free proxy settings of this process are passed only
    for a transport that reaches a network. ``credential_env`` may carry only
    :data:`CREDENTIAL_ENVIRONMENT_NAMES`, chosen by the factory.
    """
    extra = dict(credential_env or {})
    unknown = sorted(set(extra) - set(CREDENTIAL_ENVIRONMENT_NAMES))
    if unknown:
        raise ValueError(f"network git does not pass these environment names: {unknown}")
    folder = os.path.dirname(project_git.git_program())
    env: Dict[str, str] = {
        "PATH": f"{folder}:/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": transport,
    }
    # Proxies matter only to a command that reaches a remote: a local command
    # (making the factory's copy, reading its refs) never looks at them, so a
    # proxy with a login cannot refuse it (coach fix pass, 14 October 2026).
    if transport not in LOCAL_TRANSPORTS:
        env.update(_proxy_values())
    env.update({k: str(v) for k, v in extra.items()})
    return env


@contextlib.contextmanager
def _fresh_home() -> Iterator[Path]:
    """A fresh empty folder for one command, removed after it."""
    home = Path(tempfile.mkdtemp(prefix="forge-git-home-"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _reason(stderr: str) -> Optional[str]:
    for key, pattern in _REASON_PATTERNS:
        if pattern.search(stderr or ""):
            return key
    return None


def _label(args: Sequence[str]) -> str:
    for arg in args:
        if not arg.startswith("-"):
            return arg
    return "git"


def run_network_git(
    args: Sequence[str],
    *,
    transport: str,
    git_dir: Optional[Path] = None,
    timeout: Optional[float] = None,
    credential_env: Optional[Mapping[str, str]] = None,
    binary: bool = False,
    input_bytes: Optional[bytes] = None,
) -> NetworkResult:
    """Run one git command at the network boundary. Never raises.

    ``args`` is the command (``["fetch", ...]``, ``["clone", ...]``); it runs
    with ``--git-dir`` naming ``git_dir`` when given, from a fresh empty
    folder that is also ``HOME``, with :func:`isolated_environment` and
    :data:`NETWORK_SETTINGS`. Git's stderr is read only to recognise one of
    :data:`REASONS`; it is never returned.

    ``binary`` returns the output as raw bytes in :attr:`NetworkResult.data`
    (14 October 2026: the publisher reads whole blobs with ``git cat-file
    --batch`` and searches their bytes). ``input_bytes``, allowed only with
    ``binary``, is what the command reads on its standard input; otherwise
    standard input is empty.
    """
    if input_bytes is not None and not binary:
        raise ValueError("run_network_git takes input_bytes only with binary=True")
    label = _label(args)
    argv: List[str] = [project_git.git_program(), "--no-pager"]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    for setting in NETWORK_SETTINGS:
        argv += ["-c", setting]
    argv += [str(a) for a in args]
    limit = _DEFAULT_TIMEOUT_S if timeout is None else timeout
    with _fresh_home() as home:
        try:
            env = isolated_environment(transport, home, credential_env)
        except NetworkRefused as refused:
            return NetworkResult(project_git.REFUSED_EXIT_CODE, "", refused.sentence)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                cwd=str(home),
                env=env,
                stdin=None if input_bytes is not None else subprocess.DEVNULL,
                input=input_bytes,
                capture_output=True,
                text=not binary,
                errors=None if binary else "replace",
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return NetworkResult(124, "", project_git.timeout_sentence(label, limit))
        except OSError:
            return NetworkResult(127, "", f"git {label} could not be started.")
    if binary:
        data = done.stdout or b""
        stderr = (done.stderr or b"").decode("utf-8", errors="replace")
        if done.returncode == 0:
            return NetworkResult(0, "", "", data=data)
        return NetworkResult(
            done.returncode,
            "",
            project_git.failure_sentence(label, done.returncode),
            _reason(stderr),
            data=data,
        )
    if done.returncode == 0:
        return NetworkResult(0, done.stdout or "", "")
    return NetworkResult(
        done.returncode,
        done.stdout or "",
        project_git.failure_sentence(label, done.returncode),
        _reason(done.stderr or ""),
    )


# ---------------------------------------------------------------------------
# The factory's own copies


def fetch_copies_root() -> Path:
    """Where the factory's own fetch copies live (see :data:`FETCH_COPIES_ENV`)."""
    named = os.environ.get(FETCH_COPIES_ENV, "").strip()
    if named and os.path.isabs(named):
        return Path(named)
    return Path(tempfile.gettempdir()) / f"forge-fetch-copies-{os.getuid()}"


def factory_copy_for(address: str, root: Optional[Path] = None) -> Path:
    """The factory's own bare copy for one fetch address (not yet made)."""
    digest = hashlib.sha256(str(address).strip().encode("utf-8")).hexdigest()[:24]
    return (root or fetch_copies_root()) / f"{digest}.git"


@contextlib.contextmanager
def _held(copy: Path) -> Iterator[None]:
    """One fetch at a time per copy (an advisory lock beside it)."""
    import fcntl

    copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(str(copy) + ".lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ready(copy: Path, timeout: float) -> NetworkResult:
    if (copy / "HEAD").is_file():
        return NetworkResult(0)
    copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return run_network_git(
        ["init", "--bare", "-q", "--template=", str(copy)], transport="none", timeout=timeout
    )


def _copy_refs(copy: Path, pattern: str, timeout: float) -> Optional[Dict[str, str]]:
    done = run_network_git(
        ["for-each-ref", "--format=%(objectname) %(refname)", pattern],
        transport="none", git_dir=copy, timeout=timeout,
    )
    if not done.ok:
        return None
    out: Dict[str, str] = {}
    for line in done.stdout.splitlines():
        sha, _, ref = line.partition(" ")
        if sha and ref:
            out[ref] = sha
    return out


def _clone_refs(clone: Path, pattern: str, timeout: float) -> Optional[Dict[str, str]]:
    done = project_git.run_git(
        ["-C", str(clone), "for-each-ref", "--format=%(objectname) %(refname)", pattern],
        timeout=timeout,
    )
    if done.returncode != 0:
        return None
    out: Dict[str, str] = {}
    for line in (done.stdout or "").splitlines():
        sha, _, ref = line.partition(" ")
        if sha and ref:
            out[ref] = sha
    return out


_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _forget(clone: Path, refs: Sequence[str], timeout: float) -> None:
    """Delete remote-tracking refs a refused bring-in left pointing at the
    wrong commit, so nothing later reads them as the starting point."""
    for ref in refs:
        project_git.run_git(["-C", str(clone), "update-ref", "-d", ref], timeout=timeout)

COPY_REFUSAL = (
    "The commit could not be brought in from the factory's own copy, because "
    "that copy is not a local folder."
)


def bring_in_from_copy(
    clone: Path, copy: "Path | str", refspecs: Sequence[str], *,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Step 2 of the two-step fetch: bring commits into the project's clone
    from the factory's own local copy. Never raises.

    It runs through :func:`forge.project_git.run_from_local_copy`, so every
    local rule applies (hooks, the file monitor, filters and the rest are
    off, setting names are read just before, git's text is replaced), and:

    * ``copy`` must be an absolute folder on this machine, and is given after
      ``--``, so it is never read as an option;
    * only the ``file`` transport is allowed, so a project ``url.*.insteadOf``
      that rewrites the copy's folder into a program or a network address is
      refused by git before anything runs;
    * git's own ``upload-pack`` is named on the command line, which beats any
      ``remote.*.uploadpack`` setting;
    * no tags, no submodules, no ``FETCH_HEAD``;
    * ``--atomic``: either every requested ref is updated or none is, so a
      fetch that fails part way leaves no ref at a commit nobody verified
      (implementation review round 1, 14 October 2026).

    The caller must then check that what arrived is what the copy holds (a
    rewrite to another local repository is caught there), and on any failure
    forget the refs it asked for (:func:`_bring_in_or_forget`).
    """
    copy_text = os.fspath(copy)
    if not os.path.isabs(copy_text) or "://" in copy_text or not os.path.isdir(copy_text):
        return subprocess.CompletedProcess(
            ["git", "fetch"], project_git.REFUSED_EXIT_CODE, "", COPY_REFUSAL
        )
    for spec in refspecs:
        if not isinstance(spec, str) or spec.startswith("-"):
            raise ValueError("bring_in_from_copy takes plain refspecs only")
    upload_pack = os.path.join(project_git.git_exec_folder(), "git-upload-pack")
    return project_git.run_from_local_copy(
        clone,
        ["fetch", "--atomic", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
         f"--upload-pack={upload_pack}", "--", copy_text, *refspecs],
        timeout=timeout,
    )


def _bring_in_or_forget(
    clone: Path, copy: Path, branches: Sequence[str], timeout: float
) -> Optional[str]:
    """Bring ``branches`` in from the copy, one exact refspec each (never a
    wildcard), into ``refs/remotes/origin/<branch>``. ``None`` on success;
    otherwise the sentence, after forgetting every ref it asked for, so a
    failed or timed-out bring-in leaves no ref at an unverified commit."""
    wanted = [f"refs/remotes/origin/{b}" for b in branches]
    try:
        brought = bring_in_from_copy(
            clone, copy, [f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in branches],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _forget(clone, wanted, timeout)
        return (
            "The commits could not be brought into the project's copy from the "
            f"factory's own copy: git fetch did not finish within {timeout:g} seconds."
        )
    if brought.returncode != 0:
        _forget(clone, wanted, timeout)
        return (
            "The commits could not be brought into the project's copy from the "
            f"factory's own copy: {brought.stderr}"
        )
    return None


REDIRECTED_SENTENCE = (
    "The commit brought into the project's copy is not the one the factory's "
    "own copy holds, so the starting point was refused (the project's git "
    "settings may rewrite where it is fetched from)."
)


@dataclass(frozen=True)
class StartPoint:
    """The remote's default branch and its commit, or one plain sentence."""

    branch: Optional[str] = None
    commit: Optional[str] = None
    branch_commit: Optional[str] = None
    refusal: Optional[str] = None
    #: Every branch the copy holds (for :func:`fetch_all_branches`).
    branches: Dict[str, str] = field(default_factory=dict)


def _proxy_refusal(transport: str) -> Optional[str]:
    """The proxy sentence when a network fetch would be refused for a proxy
    with a login, said as what it is before anything is made or run."""
    if transport in LOCAL_TRANSPORTS:
        return None
    try:
        _proxy_values()
    except NetworkRefused as refused:
        return refused.sentence
    return None


def _default_and_listed(ls_remote: str) -> Tuple[Optional[str], Dict[str, str]]:
    """``(default branch, {ref: commit})`` read from ``ls-remote --symref``."""
    default: Optional[str] = None
    listed: Dict[str, str] = {}
    for line in ls_remote.splitlines():
        stripped = line.strip()
        if stripped.startswith("ref:") and default is None:
            rest = stripped[len("ref:"):].strip().split()
            ref = rest[0] if rest else ""
            if ref.startswith("refs/heads/"):
                default = ref[len("refs/heads/"):]
            continue
        sha, _, ref = stripped.partition("\t")
        if sha and ref:
            listed[ref] = sha
    return default, listed


def fetch_default_branch_copy(
    address: Optional[str],
    *,
    copies_root: Path,
    timeout: float = _DEFAULT_TIMEOUT_S,
    git_door: Optional[str] = None,
) -> StartPoint:
    """The remote's default branch at ``address``, with its whole history,
    fetched into the factory's own bare copy under ``copies_root``. Never
    raises.

    The nightly security sweep's fetch (security check design, 17 October
    2026, piece 3). It is the network half of :func:`fetch_start_point` and
    nothing more: the same address check, isolated environment and one
    transport, into a copy only the sweep owns
    (:func:`factory_copy_for` under ``copies_root``), and nothing is brought
    into any project's clone. The sweep reads the copy itself: its committed
    files for the tree scans and its history for the secret count.

    ``branch`` and ``commit`` name what was fetched; otherwise ``refusal``
    is one plain sentence (git's own words are never kept).

    ``git_door`` is the declared Git door, when there is one, exactly as for
    :func:`fetch_start_point`: a door address is fetched over ``http`` with
    no credential, and only an address of exactly that door is taken.
    """
    if not str(address or "").strip():
        return StartPoint(refusal=no_fetch_address_sentence(None))
    problem = check_fetch_address(address, git_door=git_door)
    if problem:
        return StartPoint(refusal=problem)
    address = str(address).strip()
    transport = transport_of(address) or "none"
    refused = _proxy_refusal(transport)
    if refused:
        return StartPoint(refusal=refused)
    copy = factory_copy_for(address, copies_root)
    with _held(copy):
        made = _ready(copy, timeout)
        if not made.ok:
            return StartPoint(refusal=f"The factory's own copy could not be made: {made.said}")
        asked = run_network_git(
            ["ls-remote", "--symref", "--", address, "HEAD"],
            transport=transport, git_dir=copy, timeout=timeout,
        )
        if not asked.ok:
            return StartPoint(
                refusal=f"The project's recorded fetch address could not be reached: {asked.said}"
            )
        default, _listed = _default_and_listed(asked.stdout)
        if not default or not _BRANCH.match(default):
            return StartPoint(
                refusal="The project's remote does not say which branch is its default."
            )
        fetched = run_network_git(
            ["fetch", "--no-tags", "--no-recurse-submodules", "--", address,
             f"+refs/heads/{default}:refs/heads/{default}"],
            transport=transport, git_dir=copy, timeout=timeout,
        )
        if not fetched.ok:
            return StartPoint(
                refusal=f"The branch '{default}' could not be fetched: {fetched.said}"
            )
        held = _copy_refs(copy, "refs/heads/", timeout)
        commit = (held or {}).get(f"refs/heads/{default}")
        if not commit:
            return StartPoint(refusal="The factory's own copy could not say which commit it holds.")
    return StartPoint(branch=default, commit=commit)


def fetch_start_point(
    clone: Path,
    address: Optional[str],
    *,
    branch: Optional[str] = None,
    repo: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    copies_root: Optional[Path] = None,
    git_door: Optional[str] = None,
) -> StartPoint:
    """The remote's default branch (and ``branch``, when named) at ``address``,
    fetched in two steps and brought into ``clone``'s
    ``refs/remotes/origin/<b>``. Never raises.

    1. In the factory's own copy: ``ls-remote --symref`` and ``fetch``, with
       the isolated policy and the one transport.
    2. Into the clone, through :func:`bring_in_from_copy`;
       then the clone's ``refs/remotes/origin/<b>`` must equal the copy's.

    ``git_door`` is the declared Git door, when there is one
    (:func:`check_fetch_address`); a door address is fetched over ``http``
    with no credential, as every fetch is.
    """
    if not str(address or "").strip():
        return StartPoint(refusal=no_fetch_address_sentence(repo))
    problem = check_fetch_address(address, git_door=git_door)
    if problem:
        return StartPoint(refusal=problem)
    address = str(address).strip()
    transport = transport_of(address) or "none"
    refused = _proxy_refusal(transport)
    if refused:
        return StartPoint(refusal=refused)
    wanted = (branch or "").strip() or None
    if wanted is not None and not _BRANCH.match(wanted):
        return StartPoint(
            refusal=f"the branch name {wanted!r} is not one this factory will pass to git"
        )
    copy = factory_copy_for(address, copies_root)
    with _held(copy):
        made = _ready(copy, timeout)
        if not made.ok:
            return StartPoint(
                refusal=f"The factory's own copy could not be made: {made.said}"
            )
        asked = run_network_git(
            ["ls-remote", "--symref", "--", address, "HEAD",
             *([f"refs/heads/{wanted}"] if wanted else [])],
            transport=transport, git_dir=copy, timeout=timeout,
        )
        if not asked.ok:
            return StartPoint(
                refusal=(
                    "The project's recorded fetch address could not be reached, so "
                    f"the work cannot be started from it: {asked.said}"
                )
            )
        default, listed = _default_and_listed(asked.stdout)
        if not default or not _BRANCH.match(default):
            return StartPoint(
                refusal=(
                    "The project's remote does not say which branch is its default, "
                    "so there is nothing to start the work from. Set that remote's "
                    "default branch, then ask again."
                )
            )
        branches = [default]
        if wanted is not None and wanted != default:
            if f"refs/heads/{wanted}" not in listed:
                return StartPoint(
                    refusal=(
                        f"The project's remote has no branch called '{wanted}', so "
                        "there is nothing to build from it. Push that branch, then "
                        "ask again."
                    )
                )
            branches.append(wanted)
        fetched = run_network_git(
            ["fetch", "--no-tags", "--no-recurse-submodules", "--", address,
             *[f"+refs/heads/{b}:refs/heads/{b}" for b in branches]],
            transport=transport, git_dir=copy, timeout=timeout,
        )
        if not fetched.ok:
            return StartPoint(
                refusal=(
                    f"The branch '{default}' could not be fetched from the project's "
                    f"recorded address: {fetched.said}"
                )
            )
        held = _copy_refs(copy, "refs/heads/", timeout)
        if held is None or any(f"refs/heads/{b}" not in held for b in branches):
            return StartPoint(
                refusal="The factory's own copy could not say which commit it holds."
            )
        refused = _bring_in_or_forget(clone, copy, branches, timeout)
        if refused:
            return StartPoint(refusal=refused)
    got = _clone_refs(clone, "refs/remotes/origin/", timeout)
    if got is None:
        return StartPoint(refusal="The project's copy could not say which commit it holds.")
    wrong = [
        f"refs/remotes/origin/{b}"
        for b in branches
        if got.get(f"refs/remotes/origin/{b}") != held[f"refs/heads/{b}"]
    ]
    if wrong:
        _forget(clone, wrong, timeout)
        return StartPoint(refusal=REDIRECTED_SENTENCE)
    commit = held[f"refs/heads/{default}"]
    if wanted is None:
        return StartPoint(branch=default, commit=commit)
    return StartPoint(
        branch=default, commit=commit, branch_commit=held[f"refs/heads/{wanted}"]
    )


def fetch_all_branches(
    clone: Path,
    address: Optional[str],
    *,
    repo: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    copies_root: Optional[Path] = None,
    git_door: Optional[str] = None,
) -> StartPoint:
    """Every branch at ``address``, brought into ``clone``'s
    ``refs/remotes/origin/*`` in the same two steps. Never raises.

    What used to be ``git fetch origin`` when a commit was missing. Only
    ``refusal`` and ``branches`` are filled in. ``git_door`` as for
    :func:`fetch_start_point`.
    """
    if not str(address or "").strip():
        return StartPoint(refusal=no_fetch_address_sentence(repo))
    problem = check_fetch_address(address, git_door=git_door)
    if problem:
        return StartPoint(refusal=problem)
    address = str(address).strip()
    transport = transport_of(address) or "none"
    refused = _proxy_refusal(transport)
    if refused:
        return StartPoint(refusal=refused)
    copy = factory_copy_for(address, copies_root)
    with _held(copy):
        made = _ready(copy, timeout)
        if not made.ok:
            return StartPoint(refusal=f"The factory's own copy could not be made: {made.said}")
        fetched = run_network_git(
            ["fetch", "--no-tags", "--no-recurse-submodules", "--prune", "--", address,
             "+refs/heads/*:refs/heads/*"],
            transport=transport, git_dir=copy, timeout=timeout,
        )
        if not fetched.ok:
            return StartPoint(
                refusal=f"The project's recorded fetch address could not be fetched: {fetched.said}"
            )
        held = _copy_refs(copy, "refs/heads/", timeout)
        if held is None:
            return StartPoint(refusal="The factory's own copy could not say which commits it holds.")
        # The copy's own verified branch inventory, one exact refspec per
        # branch (implementation review round 1): a wildcard would take in
        # whatever a rewritten source offers besides. A name that cannot go
        # on a command line is left out, and so is not expected below.
        branches: Dict[str, str] = {
            ref[len("refs/heads/"):]: sha
            for ref, sha in held.items()
            if _BRANCH.match(ref[len("refs/heads/"):])
        }
        if branches:
            refused = _bring_in_or_forget(clone, copy, sorted(branches), timeout)
            if refused:
                return StartPoint(refusal=refused)
    got = _clone_refs(clone, "refs/remotes/origin/", timeout)
    if got is None:
        return StartPoint(refusal="The project's copy could not say which commits it holds.")
    expected = {f"refs/remotes/origin/{name}": sha for name, sha in branches.items()}
    # Exactly the copy's branches at the copy's commits: anything else under
    # refs/remotes/origin/ is not the recorded repository's, and goes
    # (origin/HEAD, git's own pointer to one of them, is left).
    strays = [
        ref for ref in got if ref not in expected and ref != "refs/remotes/origin/HEAD"
    ]
    wrong = [ref for ref, sha in expected.items() if got.get(ref) != sha]
    if strays:
        _forget(clone, strays, timeout)
    if wrong:
        _forget(clone, wrong, timeout)
        return StartPoint(refusal=REDIRECTED_SENTENCE)
    return StartPoint(branches=branches)


# ---------------------------------------------------------------------------
# Commands run inside a sandbox (register-repo)


def sandbox_git_command(
    exec_prefix: Sequence[str],
    args: Sequence[str],
    *,
    transport: str,
    home: str,
    cwd: Optional[str] = None,
    git_dir: Optional[str] = None,
    credential_env: Optional[Mapping[str, str]] = None,
    settings: Sequence[str] = NETWORK_SETTINGS,
    proxy_env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """One whole command line for a git command run through ``env -i``.

    For commands that run somewhere this process cannot hand an environment
    to directly: inside a sandbox (``exec_prefix`` is
    ``["sbx", "exec", "-u", <user>, <sandbox>]``), or through register-repo's
    command runner on the operator's machine (``exec_prefix`` empty). The
    rest is ``env -i -C <cwd or home>`` with the isolated environment (no
    proxy but the login-free ones in ``proxy_env`` for a command that reaches
    a network, which raise :class:`NetworkRefused` when one carries a login;
    the factory's own credential only when ``credential_env`` names one),
    then ``git`` with ``settings`` and, when given, ``--git-dir``. So
    neither the machine's own settings files nor a repository around the
    working folder are read. ``home`` must be a folder that exists and is
    empty (or one that does not exist, for a read that needs no home).
    """
    extra = dict(credential_env or {})
    unknown = sorted(set(extra) - set(CREDENTIAL_ENVIRONMENT_NAMES))
    if unknown:
        raise ValueError(f"network git does not pass these environment names: {unknown}")
    env = [
        "PATH=/usr/local/bin:/usr/bin:/bin",
        f"HOME={home}",
        f"XDG_CONFIG_HOME={home}/.config",
        "LC_ALL=C",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_TERMINAL_PROMPT=0",
        "GIT_ASKPASS=",
        "SSH_ASKPASS=",
        "GIT_NO_LAZY_FETCH=1",
        f"GIT_ALLOW_PROTOCOL={transport}",
    ]
    if transport not in LOCAL_TRANSPORTS:
        env += [f"{name}={value}" for name, value in checked_proxies(proxy_env or {}).items()]
    env += [f"{name}={value}" for name, value in sorted(extra.items())]
    command = [*exec_prefix, "env", "-i", "-C", cwd or home, *env, "git", "--no-pager"]
    if git_dir is not None:
        command += ["--git-dir", git_dir]
    for setting in settings:
        command += ["-c", setting]
    return command + [str(a) for a in args]


def sandbox_network_git(
    sandbox: object,
    args: Sequence[str],
    *,
    bare: bool,
    transport: str = "https",
    timeout: float = 900.0,
) -> subprocess.CompletedProcess:
    """One network git command inside a sandbox, under the isolated policy.

    ``sandbox`` runs commands there: ``exec(*argv)`` for a plain command,
    ``run_whole(command, timeout=...)`` for a command line built whole, and
    ``exec_prefix`` (``sbx exec -u <user> <sandbox>``). A fresh folder of its
    own is made by one ``exec`` before (an empty home, and with ``bare`` an
    empty bare repository the command names with ``--git-dir``, so git never
    discovers a repository around its working folder) and removed by one
    after. The command runs through :func:`sandbox_git_command`: ``env -i``,
    no settings files, exactly one ``transport`` (``https``: register-repo
    takes only an https address), no credential (the sandbox holds none).
    Git's words are not kept: the caller reports the exit code.
    """
    import secrets

    prefix = list(getattr(sandbox, "exec_prefix"))
    # The sandbox's own proxy settings (its network may be reached only
    # through one), read before anything is made: login-free ones go on the
    # command line, one with a login raises NetworkRefused naming it.
    proxies = sandbox_proxy_environment(sandbox) if transport not in LOCAL_TRANSPORTS else {}
    base = f"/tmp/forge-register-{secrets.token_hex(8)}"
    home = f"{base}/home"
    made = sandbox.exec("mkdir", "-m", "700", base, home)  # type: ignore[attr-defined]
    if made.returncode != 0:
        return subprocess.CompletedProcess(list(args), made.returncode, "", "")
    try:
        git_dir = None
        if bare:
            git_dir = f"{base}/probe.git"
            init = sandbox.run_whole(  # type: ignore[attr-defined]
                sandbox_git_command(
                    prefix, ["init", "--bare", "-q", "--template=", git_dir],
                    transport="none", home=home,
                )
            )
            if init.returncode != 0:
                return subprocess.CompletedProcess(list(args), init.returncode, "", "")
        done = sandbox.run_whole(  # type: ignore[attr-defined]
            sandbox_git_command(
                prefix, list(args), transport=transport, home=home, git_dir=git_dir,
                proxy_env=proxies,
            ),
            timeout=timeout,
        )
        return subprocess.CompletedProcess(list(args), done.returncode, done.stdout, "")
    finally:
        sandbox.exec("rm", "-rf", base)  # type: ignore[attr-defined]


#: Prints each proxy setting the sandbox holds as NAME=VALUE (empty when
#: unset), one per line, and nothing else of its environment.
SANDBOX_PROXY_READ: str = "printf '%s\\n' " + " ".join(
    f'"{name}=${{{name}-}}"' for name in PROXY_NAMES
)


def sandbox_proxy_environment(sandbox: object) -> Dict[str, str]:
    """The sandbox's own login-free proxy settings, read with one
    ``sandbox.exec("sh", "-c", ...)`` that prints only the proxy names. Raises
    :class:`NetworkRefused` naming a proxy that carries a login."""
    done = sandbox.exec("sh", "-c", SANDBOX_PROXY_READ)  # type: ignore[attr-defined]
    if getattr(done, "returncode", 1) != 0:
        return {}
    values: Dict[str, str] = {}
    for line in str(getattr(done, "stdout", "") or "").splitlines():
        name, sep, value = line.partition("=")
        if sep and name in PROXY_NAMES and value:
            values[name] = value
    return checked_proxies(values)
