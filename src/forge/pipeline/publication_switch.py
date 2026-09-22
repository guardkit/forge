"""Is publication switched on? One setting, and five things that must hold.

One-true-copy design pass, item 1: the second revision's section D and the
third revision's section G.

WHAT PUBLICATION MEANS. Sending the joined commit to the project's remote
(the publisher's stage, built), and then deploying what was checked (the
executor's stage, NOT built). With publication on, the merge word sends and
stops at "published, deployment pending"; with it off, the merge word joins,
checks, and stops at "publication pending" without sending anything.

TWO THINGS HAVE TO BE TRUE, and both are asked here, in one place, so that
every caller asks the same question:

1. **a setting turns it on.** ``publication.enabled`` is False by default, so
   a forge that says nothing publishes nothing;
2. **the activation check passes.** Turning the setting on is not permission:
   the five conditions of section G are asked
   (:mod:`forge.pipeline.publication_activation`) and publication stays off,
   with the reason in plain words, unless every one of them holds. The check
   is asked again on every press and at every coordinator start, so a
   condition that becomes false while publication is on takes it off again.

IT FAILS CLOSED. A question nobody has looked at is not a pass; a setting of
an unexpected shape is not a pass; an error reading the settings is not a
pass. There is no path through this module on which publication switches on
because something could not be determined.

WHY IT IS A FUNCTION AND NOT SIMPLY ABSENT CODE — the reason the first
version of this module gave, and it still holds. The merge press used to say
"merged and running" on the evidence of a merge inside the factory's own copy
alone. That sentence, and the result word beside it, have to be unreachable —
not merely unused — until a remote has really been read back, and a thing
that is unreachable behind a named switch can be pinned by a test. A thing
that is unreachable because somebody deleted the lines cannot be, and comes
back by accident.

Nothing here reads a credential, an environment or a secrets file, and
nothing here names a language, a host or a product.
"""

from __future__ import annotations

import logging
from typing import Any

from forge.pipeline.publication_activation import (
    TheVerdict,
    WhatTheMachineSays,
    run_the_activation_check,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PUBLICATION_IS_OFF_SENTENCE",
    "publication_is_switched_on",
    "the_activation_check",
    "why_publication_is_off",
]

#: What a person is told, in one sentence, when the merge word finishes with
#: publication off. The reason follows it.
PUBLICATION_IS_OFF_SENTENCE: str = (
    "publication is switched off, so nothing was sent to the remote and "
    "nothing was deployed"
)


def _the_setting_says_on(config: Any) -> bool:
    """``publication.enabled``, and nothing looser."""
    publication = getattr(config, "publication", None)
    return getattr(publication, "enabled", None) is True


def the_activation_check(
    config: Any = None, machine: WhatTheMachineSays | None = None
) -> TheVerdict:
    """Ask section G's five questions. Never raises."""
    return run_the_activation_check(config, machine)


def publication_is_switched_on(
    config: Any = None, machine: WhatTheMachineSays | None = None
) -> bool:
    """Is publication on: the setting says so AND all five conditions hold."""
    if not _the_setting_says_on(config):
        return False
    verdict = the_activation_check(config, machine)
    if not verdict.all_hold:
        logger.warning(
            "publication: the setting says on and the activation check "
            "refuses — publication stays off: %s",
            verdict.sentence,
        )
        return False
    return True


def why_publication_is_off(
    config: Any = None, machine: WhatTheMachineSays | None = None
) -> str:
    """One plain sentence saying why nothing was published."""
    if not _the_setting_says_on(config):
        return (
            "no setting turns publication on, so the merge word joins and "
            "checks and stops there"
        )
    return the_activation_check(config, machine).sentence
