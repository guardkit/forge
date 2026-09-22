"""Is publication switched on? In this version: no, and here is why that is here.

One-true-copy design pass, item 1: the second revision's section D and the
third revision's section G.

WHAT PUBLICATION MEANS. Sending the joined commit to the project's remote, and
then deploying what was checked. Neither exists yet: the publisher is its own
stage and the executor is the one after it. So this answers ``False`` on every
path there is, and the merge word stops at "checked and ready to publish".

WHY IT IS A FUNCTION AND NOT SIMPLY ABSENT CODE. Two reasons, and both matter.

First, the words. The merge press used to say "merged and running" on the
evidence of a merge inside the factory's own copy alone. That sentence, and
the result word beside it, have to become **unreachable** — not merely
unused — while publication is off, and a thing that is unreachable behind a
named switch can be pinned by a test. A thing that is unreachable because
somebody deleted the lines cannot be, and comes back by accident.

Second, the condition. Section G says the switch that turns publication on
must run a check first and refuse, in plain words, unless every one of these
holds: nothing in the coordinator can start a build or a project check inside
itself; no sandbox can write the coordinator's settings file or see the
ledger; no sandbox can reach the publisher; the publisher's credential is in
none of their settings. That check is the publisher stage's work. This module
is the seam it attaches to, so that when it is written there is exactly one
place that decides, and every caller already asks it.

Nothing here reads a credential, an environment or a secrets file, and nothing
here names a language, a host or a product.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PUBLICATION_IS_OFF_SENTENCE",
    "publication_is_switched_on",
    "why_publication_is_off",
]

#: What a person is told, in one sentence, when the merge word finishes.
PUBLICATION_IS_OFF_SENTENCE: str = (
    "publication is not switched on, so nothing was sent to the remote and "
    "nothing was deployed"
)


def publication_is_switched_on(config: Any = None) -> bool:  # noqa: ARG001
    """Always ``False`` in this version.

    ``config`` is taken and deliberately ignored: no setting turns publication
    on, because there is nothing yet to turn on. Section G's conditions decide
    this when the publisher exists, and this is the one place that will
    answer.
    """
    return False


def why_publication_is_off(config: Any = None) -> str:  # noqa: ARG001
    """One plain sentence saying why nothing was published."""
    return (
        "the publisher has not been built yet, so nothing can be sent to the "
        "remote"
    )
