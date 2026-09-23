"""Only forwards: publication is not permission to deploy (section B).

One-true-copy design pass, 21 September 2026, item 1, second revision,
section B. *"'The remote contains J' is the right test for published. It is
the wrong test for deploy: build A publishes and pauses; build B publishes a
later commit and deploys; A is picked up and would put the older result
back."*

So, **while holding the deployment lock**, before anything is deployed:

* read what is running now, R — the commit recorded at the last deploy and
  confirmed from the running thing's own reported identity;
* nothing running, or R an ancestor of J  ⇒ deploy what was checked for J;
* J an ancestor of R                      ⇒ do NOT deploy. A's change is
  already part of what is running, and the result says so;
* neither contains the other              ⇒ do not deploy. Say so plainly and
  leave it for a person. With every result joined onto one branch this should
  not happen, which is why it is a stop and not a guess.

The ancestry question is asked of git, through whichever venue this project's
git happens in. An answer of "git could not say" is NOT read as either yes or
no: it is its own ending, and it stops rather than guesses, for the same
reason the fourth case does.

Nothing here knows what a project deploys or what an identity is. It compares
commits and produces a word and a sentence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ONLY_FORWARDS_WORDS",
    "OnlyForwards",
    "what_to_do_about_j",
]


#: The four answers, as the words the record and the sentence carry.
ONLY_FORWARDS_WORDS: tuple[str, ...] = (
    "deploy",
    "already-running",
    "neither-contains-the-other",
    "cannot-tell",
)


@dataclass(frozen=True)
class OnlyForwards:
    """What the only-forwards rule says about this joined commit.

    ``word`` is one of :data:`ONLY_FORWARDS_WORDS`. ``deploy`` is the only one
    on which anything is deployed; every other one leaves the target exactly
    as it is and carries a sentence a person can act on.
    """

    word: str
    sentence: str
    running_commit: str | None = None
    running_identity: str | None = None

    @property
    def go(self) -> bool:
        return self.word == "deploy"

    @property
    def already(self) -> bool:
        return self.word == "already-running"

    def to_wire(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "sentence": self.sentence,
            "running_commit": self.running_commit,
            "running_identity": self.running_identity,
        }


async def what_to_do_about_j(
    git: Any,
    *,
    j_commit: str,
    running_commit: str | None,
    running_identity: str | None = None,
    target: str,
) -> OnlyForwards:
    """Apply the only-forwards rule. Never raises; every ending is an answer.

    Args:
        git: The venue this project's git happens in — the same surface every
            other git operation of the press uses, so the sandbox case and the
            in-container case are one code path.
        j_commit: The joined commit this build wants running.
        running_commit: R, off the deployment target's own row. ``None``
            means nothing has ever been deployed here, which is a fact about
            the record rather than a guess.
        running_identity: What the running thing reported last time, carried
            through into the answer so a sentence can name it.
        target: The deployment target, for the sentences only.
    """
    j = str(j_commit or "").strip()
    if not j:
        return OnlyForwards(
            word="cannot-tell",
            sentence=(
                "there is no joined commit to deploy, so nothing was deployed "
                f"to {target}."
            ),
            running_commit=running_commit,
            running_identity=running_identity,
        )
    running = str(running_commit or "").strip()
    if not running:
        return OnlyForwards(
            word="deploy",
            sentence=(
                f"nothing is recorded as running on {target}, so what was "
                f"checked for {j[:10]} is what gets deployed."
            ),
            running_commit=None,
            running_identity=running_identity,
        )
    if running == j:
        # The same commit is already running. That is not "deploy it again"
        # and it is not a stop either: it is the ancestor case's own limit,
        # and reading it as "already running" is what keeps a pick-up after a
        # confirmed deploy from deploying a second time.
        return OnlyForwards(
            word="already-running",
            sentence=(
                f"{j[:10]} is already what is running on {target}, so nothing "
                "was deployed: published; a later result that includes it is "
                "already running."
            ),
            running_commit=running,
            running_identity=running_identity,
        )

    running_is_ancestor = await _ask(git, running, j)
    if running_is_ancestor is True:
        return OnlyForwards(
            word="deploy",
            sentence=(
                f"what is running on {target} ({running[:10]}) is part of "
                f"{j[:10]}, so deploying it moves forwards."
            ),
            running_commit=running,
            running_identity=running_identity,
        )
    j_is_ancestor = await _ask(git, j, running)
    if j_is_ancestor is True:
        return OnlyForwards(
            word="already-running",
            sentence=(
                f"{j[:10]} is already part of what is running on {target} "
                f"({running[:10]}), so nothing was deployed: published; a "
                "later result that includes it is already running."
            ),
            running_commit=running,
            running_identity=running_identity,
        )
    if running_is_ancestor is None or j_is_ancestor is None:
        return OnlyForwards(
            word="cannot-tell",
            sentence=(
                f"git could not say how {j[:10]} and what is running on "
                f"{target} ({running[:10]}) are related, so nothing was "
                "deployed. Somebody has to look."
            ),
            running_commit=running,
            running_identity=running_identity,
        )
    return OnlyForwards(
        word="neither-contains-the-other",
        sentence=(
            f"{j[:10]} and what is running on {target} ({running[:10]}) have "
            "diverged: neither contains the other, so nothing was deployed. "
            "With every result joined onto one branch this should not happen, "
            "so it is left for a person rather than guessed at."
        ),
        running_commit=running,
        running_identity=running_identity,
    )


async def _ask(git: Any, ancestor: str, descendant: str) -> bool | None:
    """Is ``ancestor`` part of ``descendant``? ``None`` = git could not say."""
    try:
        answer = await git.is_ancestor(ancestor, descendant)
    except Exception as exc:  # noqa: BLE001 — a question, never a crash
        logger.warning(
            "only-forwards: git could not be asked whether %s is part of %s "
            "(%s: %s)",
            ancestor[:10],
            descendant[:10],
            type(exc).__name__,
            exc,
        )
        return None
    if answer is True:
        return True
    if answer is False:
        return False
    return None
