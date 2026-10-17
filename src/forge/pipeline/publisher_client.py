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
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

__all__ = [
    "MISSION_BRANCH_CREATED",
    "MISSION_BRANCH_REFUSED",
    "MISSION_BRANCH_UNREACHABLE",
    "MissionBranchAnswer",
    "PUBLISHER_COULD_NOT_BE_REACHED",
    "SELF_CHECK_PASSED",
    "THERE_IS_NO_PUBLISHER",
    "THE_REMOTE_MOVED",
    "ask_the_publisher",
    "ask_the_publisher_to_create_mission_branch",
    "the_publishers_address",
    "the_publishers_self_check",
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
            # The publisher's address is not written down either (the same
            # rule as a project's remote address, 23 September 2026): a
            # sentence that reaches a receipt and a ledger row must never be
            # a place a credential could end up at some future rollout.
            f"the publisher could not be reached ({type(exc).__name__}), so "
            "nothing was sent",
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


#: What the publisher's health route says when it passed its start-up
#: self-check. Written out here as well as in the publisher's own service,
#: because the coordinator does not import the publisher; a test pins the two.
SELF_CHECK_PASSED: str = "passed"


def the_publishers_self_check(config: Any, *, timeout: float = 10.0) -> Any:
    """Ask the publisher's health route whether it passed its start-up check.

    The publisher refuses to start, and its health route re-checks on every
    request, that its credential file is a regular file owned by its UID with no
    group or other access and that it has exactly one non-loopback interface
    (not which network: the compose file and the host firewall check decide
    that); the route says ``"self_check": "passed"`` when both hold. Anything else — no publisher
    configured, one that does not answer, an answer without that word — is
    "nobody could ask", which keeps publication off. Never raises.
    """
    from forge.pipeline.publication_activation import WhatTheMachineSays

    url = the_publishers_address(config)
    if url is None:
        return WhatTheMachineSays(
            why_nobody_has_looked="no publisher is configured "
            "(publication.publisher_url is not set)"
        )
    try:
        with urllib.request.urlopen(  # noqa: S310 - an address from settings
            url.rstrip("/") + "/healthz", timeout=timeout
        ) as answered:
            said = json.loads(answered.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - not answering is "nobody asked"
        return WhatTheMachineSays(
            why_nobody_has_looked=f"it did not answer ({type(exc).__name__})"
        )
    if not isinstance(said, dict) or "self_check" not in said:
        return WhatTheMachineSays(
            why_nobody_has_looked="its health answer does not report the check"
        )
    return WhatTheMachineSays(
        the_publisher_passed_its_self_check=said["self_check"] == SELF_CHECK_PASSED
    )


# ---------------------------------------------------------------------------
# Creating a mission branch (missions design, 17 October 2026, section 4)
# ---------------------------------------------------------------------------

#: The three answers to ``create-mission-branch``.
MISSION_BRANCH_CREATED: str = "created"
MISSION_BRANCH_REFUSED: str = "refused"
MISSION_BRANCH_UNREACHABLE: str = "unreachable"

#: The publisher's refusals that say the outcome is not known (it stopped
#: part way, or pushed and could not read the remote back). The client reads
#: them as "unreachable": asking again is safe and is how recovery happens.
_OUTCOME_NOT_KNOWN: frozenset[str] = frozenset(
    {"the-publisher-could-not-finish", "the-remote-could-not-be-read-back"}
)

#: The publisher's route for the request. Written out here as well as in the
#: publisher's own service, because the coordinator does not import the
#: publisher; a test pins the two.
MISSION_BRANCH_ROUTE: str = "/create-mission-branch"


@dataclass(frozen=True)
class MissionBranchAnswer:
    """The publisher's answer to ``create-mission-branch``, in one of three.

    * ``created``: the publisher read the remote back and the branch's tip IS
      ``commit``. ``already_there`` says it was created by an earlier request
      and nothing was pushed this time.
    * ``refused``: the publisher answered and did not create it; ``refusal``
      is its sentence and ``refusal_kind`` its short name.
    * ``unreachable``: no answer came back (no publisher configured, it could
      not be reached, a server error, or something that is not an answer), or
      the publisher answered that it does not know (it stopped part way, or
      pushed and could not read the remote back). Whether a
      branch was created is not known; asking again is always safe, because a
      repeated request for a branch the publisher made is answered "created"
      without a second push.
    """

    outcome: Literal["created", "refused", "unreachable"]
    branch: str
    commit: str
    remote_tip: str | None = None
    already_there: bool = False
    refusal: str | None = None
    refusal_kind: str | None = None

    @property
    def created(self) -> bool:
        """True only when created AND the read-back tip is the commit."""
        return self.outcome == MISSION_BRANCH_CREATED and self.remote_tip == self.commit


def _mission_branch_post(
    url: str, request: dict[str, Any], timeout: float
) -> tuple[dict[str, Any] | None, str]:
    """One POST, in a thread: the decoded answer, or ``None`` and why. Never raises."""
    body = json.dumps(request).encode("utf-8")
    post = urllib.request.Request(  # noqa: S310 - a loopback URL from settings
        url.rstrip("/") + MISSION_BRANCH_ROUTE,
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
        except Exception:  # noqa: BLE001 - an unreadable body is no answer
            decoded = None
        if (
            exc.code < 500
            and isinstance(decoded, dict)
            and isinstance(decoded.get("created"), bool)
        ):
            return decoded, ""
        # A server error is not an answer: the publisher stopped part way, so
        # whether the branch was created is not known and asking again is
        # safe. It is "unreachable", never "refused".
        return None, (
            f"the publisher answered {exc.code} and did not finish, so it is "
            "not known whether the branch was created"
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # The publisher's address is never written down (as for a send).
        return None, (
            f"the publisher could not be reached ({type(exc).__name__}), so it "
            "is not known whether the branch was created"
        )
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("created"), bool):
        return None, (
            "the publisher's answer did not say whether it created the branch, "
            "so it is not known whether it was"
        )
    return decoded, ""


async def ask_the_publisher_to_create_mission_branch(
    config: Any,
    *,
    project: str,
    mission_id: str,
    branch: str,
    commit: str,
) -> MissionBranchAnswer:
    """Ask the publisher to create ``branch`` at ``commit``. Never raises.

    The request is ``create-mission-branch {project, mission_id, branch,
    commit}`` and nothing else. The coordinator sends the same request again
    after a lost reply or a restart of either side; the publisher answers it
    "created" without pushing again only when its own record shows it created
    exactly this branch for this mission at this commit.
    """
    request = {
        "project": str(project),
        "mission_id": str(mission_id),
        "branch": str(branch),
        "commit": str(commit),
    }
    url = the_publishers_address(config)
    if url is None:
        return MissionBranchAnswer(
            outcome=MISSION_BRANCH_UNREACHABLE,
            branch=request["branch"],
            commit=request["commit"],
            refusal="no publisher is configured, so no branch was asked for. "
            "Name one in the coordinator's settings",
            refusal_kind=THERE_IS_NO_PUBLISHER,
        )
    decoded, why = await asyncio.to_thread(
        _mission_branch_post, url, request, _timeout(config)
    )
    if decoded is None:
        return MissionBranchAnswer(
            outcome=MISSION_BRANCH_UNREACHABLE,
            branch=request["branch"],
            commit=request["commit"],
            refusal=why,
            refusal_kind=PUBLISHER_COULD_NOT_BE_REACHED,
        )
    remote_tip = decoded.get("remote_tip")
    remote_tip = str(remote_tip) if isinstance(remote_tip, str) else None
    refusal = decoded.get("refusal")
    kind = decoded.get("refusal_kind")
    if decoded.get("created") is not True and kind in _OUTCOME_NOT_KNOWN:
        # The publisher answered, and its answer is that it does not know
        # whether the branch is there: the same as no answer.
        return MissionBranchAnswer(
            outcome=MISSION_BRANCH_UNREACHABLE,
            branch=request["branch"],
            commit=request["commit"],
            remote_tip=remote_tip,
            refusal=str(refusal) if refusal else None,
            refusal_kind=str(kind),
        )
    if decoded.get("created") is True and remote_tip == request["commit"]:
        return MissionBranchAnswer(
            outcome=MISSION_BRANCH_CREATED,
            branch=request["branch"],
            commit=request["commit"],
            remote_tip=remote_tip,
            already_there=decoded.get("already_there") is True,
        )
    if decoded.get("created") is True:
        # "Created" is only ever said with the tip read back; one without it
        # is not taken on trust.
        return MissionBranchAnswer(
            outcome=MISSION_BRANCH_REFUSED,
            branch=request["branch"],
            commit=request["commit"],
            remote_tip=remote_tip,
            refusal="the publisher said the branch was created but did not say "
            "it is at the commit asked for, so it is not taken as created",
            refusal_kind="the-answer-did-not-match",
        )
    return MissionBranchAnswer(
        outcome=MISSION_BRANCH_REFUSED,
        branch=request["branch"],
        commit=request["commit"],
        remote_tip=remote_tip,
        refusal=str(refusal) if refusal else "the publisher did not create the branch",
        refusal_kind=str(kind) if kind else None,
    )
