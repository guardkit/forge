"""How the coordinator asks the publisher to send, and reads its answer.

One-true-copy design pass, item 1, second revision section D. The publisher
is its own process because the coordinator must not hold the credential; this
module is the whole of what crosses between them, and it is worth saying what
does NOT cross it:

* **no credential, in either direction.** The request carries a project name,
  a build, a turn number, a commit and a branch. The answer carries whether
  the branch now contains the commit, where the branch is, and a sentence.
  Neither ever carries a secret, and a test greps both;
* **nothing the publisher is asked to trust.** The publisher checks the
  record for itself, reads the commit for itself and asks the remote for
  itself. The request is what it is asked ABOUT, not what it is asked to
  believe.

IT NEVER RAISES. A publisher that is not configured, cannot be reached, is
slow, or answers with something that is not an answer all come back as the
same shape: not published, with a sentence saying which of those it was. The
merge word then says "publication pending" with that reason and sends
nothing, which is the honest end of every one of them.

Nothing here names a language, a hosting provider or a product.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "PUBLISHER_COULD_NOT_BE_REACHED",
    "THERE_IS_NO_PUBLISHER",
    "THE_REMOTE_MOVED",
    "ask_the_publisher",
    "the_publishers_address",
    "the_remote_moved",
]

#: The short names on the two refusals this module can produce itself.
THERE_IS_NO_PUBLISHER: str = "there-is-no-publisher"
PUBLISHER_COULD_NOT_BE_REACHED: str = "the-publisher-could-not-be-reached"

#: THE ONE REFUSAL A NEW ATTEMPT IS THE ANSWER TO: the remote's branch moved
#: under this send, so the work has to be joined onto where it is now. Every
#: other refusal is a stop — trying it again would fail the same way.
#:
#: The word is written out here as well as in the publisher's own service,
#: rather than imported from it: the coordinator does not import the
#: publisher, because the whole point of the publisher is that it is a
#: separate process the coordinator does not contain. A test pins the two
#: spellings equal, which is the same way the publisher pins the step names
#: it reads off the ledger.
THE_REMOTE_MOVED: str = "the-remote-moved"


def the_remote_moved(answer: dict[str, Any] | None) -> bool:
    """Is this the one refusal a new attempt is the answer to?"""
    if not isinstance(answer, dict):
        return False
    return str(answer.get("refusal_kind") or "") == THE_REMOTE_MOVED


def the_publishers_address(config: Any) -> str | None:
    """``publication.publisher_url``, or None because none is configured."""
    url = getattr(getattr(config, "publication", None), "publisher_url", None)
    text = str(url or "").strip()
    return text or None


def _timeout(config: Any) -> float:
    value = getattr(
        getattr(config, "publication", None), "request_timeout_seconds", None
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 300.0
    return float(value)


def _refusal(kind: str, sentence: str) -> dict[str, Any]:
    return {
        "published": False,
        "remote_now": None,
        "contains_j": False,
        "refusal": sentence,
        "refusal_kind": kind,
    }


def _ask(url: str, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One POST, in a thread. Never raises."""
    body = json.dumps(request).encode("utf-8")
    post = urllib.request.Request(  # noqa: S310 - a loopback URL from settings
        url.rstrip("/") + "/publish",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(post, timeout=timeout) as answered:  # noqa: S310
            raw = answered.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            decoded = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable body is still a refusal
            decoded = None
        if isinstance(decoded, dict) and "published" in decoded:
            return decoded
        return _refusal(
            PUBLISHER_COULD_NOT_BE_REACHED,
            f"the publisher answered {exc.code} and said nothing this side "
            "could read, so nothing is known to have been sent",
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _refusal(
            PUBLISHER_COULD_NOT_BE_REACHED,
            f"the publisher at {url} could not be reached ({exc}), so nothing "
            "was sent",
        )
    try:
        decoded = json.loads(raw)
    except ValueError:
        return _refusal(
            PUBLISHER_COULD_NOT_BE_REACHED,
            "the publisher answered with something that is not an answer, so "
            "nothing is known to have been sent",
        )
    if not isinstance(decoded, dict) or "published" not in decoded:
        return _refusal(
            PUBLISHER_COULD_NOT_BE_REACHED,
            "the publisher's answer did not say whether it published, so "
            "nothing is known to have been sent",
        )
    return decoded


async def ask_the_publisher(config: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Ask the publisher to send, and answer in its own shape either way.

    ``request`` is ``{project, build_id, turn, j_commit, target_branch}`` and
    nothing else.
    """
    url = the_publishers_address(config)
    if url is None:
        return _refusal(
            THERE_IS_NO_PUBLISHER,
            "no publisher is configured, so there is nothing to send with. "
            "Name one in the coordinator's settings",
        )
    return await asyncio.to_thread(_ask, url, dict(request), _timeout(config))
