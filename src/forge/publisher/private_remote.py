"""Is a route's repository confirmed private? One answer, for two callers.

Design "Building production features from a signed-off prototype", section
2.8 (17 October 2026, Codex R6). When the factory publishes only to private
repositories, the publisher (at start-up and immediately before every push)
and ``forge register-repo`` (before it writes a route) ask the same question
here, and get the same answer.

A REPOSITORY IS CONFIRMED PRIVATE only when both of these hold:

1. **the route's own key reads the remote** (``git ls-remote`` with the
   route's credential, the way the publisher reads the remote before every
   send), so the repository exists and the key reaches it; and
2. **an anonymous lookup is not found**: ``GET
   https://api.github.com/repos/<owner>/<name>`` with no credential at all
   answers 404. GitHub answers a private repository exactly as it answers a
   missing one to somebody who is not signed in.

Every other result is a refusal with its own plain reason:

* the anonymous lookup answers 200: the repository is public;
* the key cannot read the remote: the repository is missing, or the key does
  not reach it;
* the lookup answers anything else (403, a rate limit, a server error) or
  does not answer (a network failure, a timeout): visibility could not be
  told, so the repository is not taken as private;
* the remote is not on github.com: there is nobody to ask, so the same.

NO CREDENTIAL IS USED FOR THE LOOKUP, on purpose: it is the anonymous view
that says "not public". Nothing here is sent to the sandbox, and nothing here
reads or prints a key. Both checks are injectable, so the tests run neither.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

__all__ = [
    "CONFIRMED_PRIVATE",
    "GITHUB_HOST",
    "IS_PUBLIC",
    "LOOKUP_FAILED",
    "NOT_ON_GITHUB",
    "UNREACHABLE",
    "Visibility",
    "anonymous_lookup",
    "confirmed_private",
    "github_repository_of",
]

#: The one host whose visibility can be looked up.
GITHUB_HOST: str = "github.com"

#: Where the anonymous lookup goes.
_API: str = "https://api.github.com/repos/{owner}/{name}"

#: How long the anonymous lookup may take.
LOOKUP_TIMEOUT_SECONDS: float = 15.0

#: The short names of the answers, for logs and tests.
CONFIRMED_PRIVATE: str = "confirmed-private"
IS_PUBLIC: str = "public"
UNREACHABLE: str = "missing-or-unreachable"
LOOKUP_FAILED: str = "lookup-failed"
NOT_ON_GITHUB: str = "not-on-github"

_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_SSH_SHORT = re.compile(rf"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9.-]+):(?P<owner>{_PART})/(?P<name>{_PART})$")
_URL = re.compile(
    rf"^(?:https|ssh)://(?:[A-Za-z0-9._-]+@)?(?P<host>[A-Za-z0-9.-]+)(?::\d+)?/"
    rf"(?P<owner>{_PART})/(?P<name>{_PART})/?$"
)


@dataclass(frozen=True)
class Visibility:
    """The answer: confirmed private, or not, with one plain reason."""

    private: bool
    kind: str
    reason: Optional[str] = None


def github_repository_of(remote: str) -> Optional[Tuple[str, str]]:
    """``(owner, name)`` of a github.com remote, or ``None``.

    Takes the publisher's remote forms: ``git@github.com:owner/name.git``,
    ``ssh://git@github.com/owner/name.git`` and
    ``https://github.com/owner/name(.git)``. Any other host is ``None``.
    """
    text = str(remote or "").strip()
    found = _SSH_SHORT.match(text) or _URL.match(text)
    if not found or found.group("host").lower() != GITHUB_HOST:
        return None
    owner, name = found.group("owner"), found.group("name")
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not name or name in (".", ".."):
        return None
    return owner, name


def anonymous_lookup(owner: str, name: str, *, timeout: float = LOOKUP_TIMEOUT_SECONDS) -> Optional[int]:
    """The HTTP status of an anonymous ``GET`` of the repository, or ``None``
    when nothing answered (a network failure or a timeout).

    No credential of any kind: no ``Authorization`` header, and urllib reads
    no ``.netrc``. A redirect (a renamed repository) is followed, and its
    final status is the answer.
    """
    request = urllib.request.Request(
        _API.format(owner=owner, name=name),
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "forge-publisher-visibility-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - fixed https host
            return int(answer.status)
    except urllib.error.HTTPError as refused:
        return int(refused.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def confirmed_private(
    remote: str,
    *,
    key_reads_the_remote: Callable[[], bool],
    lookup: Callable[[str, str], Optional[int]] = anonymous_lookup,
) -> Visibility:
    """Is the repository at ``remote`` confirmed private? See the module.

    ``key_reads_the_remote`` runs ``git ls-remote`` with the route's own
    credential and says whether it worked; ``lookup`` is the anonymous
    lookup. The key is asked first, so a repository the route cannot reach
    is never looked up.
    """
    where = github_repository_of(remote)
    if where is None:
        return Visibility(
            False,
            NOT_ON_GITHUB,
            "its remote is not a github.com repository, so whether it is private "
            "cannot be told; the factory publishes only to repositories it has "
            "confirmed private",
        )
    owner, name = where
    said = f"{owner}/{name}"
    try:
        reads = bool(key_reads_the_remote())
    except Exception:  # noqa: BLE001 - a failed read is a refusal, never a crash
        reads = False
    if not reads:
        return Visibility(
            False,
            UNREACHABLE,
            f"the route's own key could not read {said}, so it is missing or the "
            "key does not reach it; it is not confirmed private",
        )
    try:
        status = lookup(owner, name)
    except Exception:  # noqa: BLE001 - a failed lookup is a refusal
        status = None
    if status == 404:
        return Visibility(True, CONFIRMED_PRIVATE)
    if status == 200:
        return Visibility(
            False,
            IS_PUBLIC,
            f"{said} is public (GitHub shows it to anybody), and the factory "
            "publishes only to private repositories",
        )
    told = "no answer came (a network failure or a timeout)" if status is None else (
        f"GitHub answered {status}"
    )
    return Visibility(
        False,
        LOOKUP_FAILED,
        f"whether {said} is private could not be told: the anonymous lookup "
        f"failed ({told}), so it is not taken as private",
    )
