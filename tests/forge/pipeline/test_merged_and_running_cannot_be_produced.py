"""While publication is switched off, "merged and running" cannot be produced.

One-true-copy design pass, item 1, second revision B (the three result names)
and the "Done when" case 9: *nothing anywhere reports a merge on the evidence
of a merge inside the factory's copy alone; every place that says "merged and
running" is found and listed.*

The press used to say it on the evidence of a local merge and a local deploy.
It now stops at "checked and ready to publish". This file is the fence: it
reads the source of every place the estate could produce those words and
proves that none of them is reachable while the switch is off, and it pins
the switch itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forge.pipeline import merge_executor
from forge.pipeline.publication_switch import (
    PUBLICATION_IS_OFF_SENTENCE,
    publication_is_switched_on,
    why_publication_is_off,
)

#: Where the source this file reads lives.
_SRC = Path(merge_executor.__file__).resolve().parent.parent

#: The exact strings the old press could put on a card or a report. Anything
#: that can PRODUCE one of these — as a result word or as a sentence — is a
#: place where the factory would claim a merge it has not made.
FORBIDDEN = ("merged-and-running", "merged and running")


def _python_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _string_literals(path: Path) -> list[str]:
    """Every string LITERAL in the file — not its comments or docstrings.

    A comment saying the words is a comment. A literal is a thing the program
    can hand to somebody, so it is a literal that has to be accounted for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(
                    first.value, ast.Constant
                ):
                    docstrings.add(id(first.value))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append(node.value)
    return found


#: The ONE place in the estate that still holds the old word as a literal, and
#: why it may. It is a READER: it counts rows written before the merge word
#: had a join, and it cannot produce the word for anything.
ALLOWED_READERS: dict[str, str] = {
    "lifecycle/metrics.py": (
        "the self-closed defect rate READS this word off rows written before "
        "the join existed; it never writes one"
    ),
}


class TestNowhereCanProduceTheOldWords:
    def test_every_literal_is_either_gone_or_a_known_reader(self) -> None:
        offenders: dict[str, list[str]] = {}
        for path in _python_files():
            said = [
                text
                for text in _string_literals(path)
                if any(bad in text for bad in FORBIDDEN)
            ]
            if not said:
                continue
            relative = str(path.relative_to(_SRC))
            if relative in ALLOWED_READERS:
                continue
            offenders[relative] = said
        assert offenders == {}, (
            "these places can still produce the words the press must no "
            f"longer say: {offenders}"
        )

    def test_the_one_reader_is_named_with_its_reason(self) -> None:
        for relative, reason in ALLOWED_READERS.items():
            assert (_SRC / relative).is_file()
            assert reason


class TestThePressStopsAtChecked:
    def test_the_switch_is_off_unless_a_setting_says_on(self) -> None:
        """No setting, no publication. Nothing looser turns it on."""
        assert publication_is_switched_on() is False
        assert publication_is_switched_on(None) is False
        assert publication_is_switched_on(object()) is False

    def test_a_setting_that_says_on_is_not_permission(self) -> None:
        """Section G: turning it on runs a check, and the check can refuse.

        A configuration that answers True to everything is exactly the shape
        that must NOT publish: the three conditions only the real machine can
        settle have nobody's answer behind them, and an unexamined wall is
        not a wall.
        """

        class _SaysYes:
            publication = True
            enabled = True

            def __getattr__(self, name: str) -> bool:  # noqa: D105
                return True

        assert publication_is_switched_on(_SaysYes()) is False

    def test_it_says_why_in_plain_words(self) -> None:
        assert "no setting turns publication on" in why_publication_is_off()
        assert "nothing was sent to the remote" in PUBLICATION_IS_OFF_SENTENCE

    def test_the_press_defines_all_three_names_and_two_are_reachable(
        self,
    ) -> None:
        assert merge_executor.RESULT_WORD_PUBLICATION_PENDING == "publication-pending"
        assert (
            merge_executor.RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING
            == "published-deployment-pending"
        )
        assert (
            merge_executor.RESULT_WORD_MERGED_AND_RUNNING
            == "merged-into-the-remote-and-running"
        )
        # THE THIRD IS STILL UNREACHABLE. The publisher stage makes the second
        # one reachable — a commit really is on the remote's branch, read
        # back — and stops there. "Merged into the remote and running" needs
        # a deploy, and the deploy is the stage after this one.
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        produced = [
            line
            for line in source.splitlines()
            if "result=RESULT_WORD_" in line.replace(" ", "")
        ]
        assert produced
        for line in produced:
            assert (
                "PUBLICATION_PENDING" in line
                or "PUBLISHED_DEPLOYMENT_PENDING" in line
            ), line
        assert not any("MERGED_AND_RUNNING" in line for line in produced)

    def test_nothing_in_the_press_deploys_anything(self) -> None:
        """The deploy is the next stage, and the press cannot reach it.

        The press drives the deploy stage through one seam, ``_dispatch``,
        and the only legs it asks for are the candidate check and the tear
        down that follows it. The promote leg — the one that would put
        something live — is named nowhere the press can run it.
        """
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        dispatches = [
            line.strip()
            for line in source.splitlines()
            if "_dispatch(" in line and "async def _dispatch" not in line
        ]
        assert dispatches
        for line in dispatches:
            assert "promote" not in line, line

    def test_the_press_says_plainly_that_nothing_was_deployed(self) -> None:
        """The sentence a person reads never implies a deploy that did not run."""
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        assert "Nothing has been deployed" in source
        assert "the deploy is its own stage" in source


#: The merge word's own modules. Central orchestration: they know that there
#: is a remote named ``origin`` and which branch of it a piece of work is
#: aimed at, and nothing else about who hosts it.
_CENTRAL = (
    "pipeline/merge_executor.py",
    "pipeline/publication_record.py",
    "pipeline/merge_join.py",
    "pipeline/publication_switch.py",
    "pipeline/publication_activation.py",
    "pipeline/publisher_client.py",
    "cli/merge_deploy.py",
    # The publisher is the one thing that talks to a remote, so it is the
    # one most likely to name whoever is hosting it. It knows two git
    # addresses it was told and the word "origin", and nothing else.
    "publisher/service.py",
    "publisher/git_work.py",
    "publisher/settings.py",
    "publisher/credential.py",
    "publisher/the_record.py",
)

#: Names of hosting providers. A project's own settings may say whatever they
#: like; the factory's own vocabulary may not name one, because the factory
#: does not know and must not claim to.
_PROVIDERS = ("github", "gitlab", "bitbucket", "azure devops", "codeberg", "sourcehut")


class TestTheVocabularyNamesNoHostingProvider:
    """The factory knows "the remote named origin". It knows nothing else.

    ``RESULT_MERGED_AND_RUNNING`` read "merged into GitHub and running" and
    its result word read ``merged-into-github-and-running`` until 22
    September 2026. Both were the design's own wording and both were wrong
    for central code: a project whose remote is hosted anywhere else would
    have been told, in the factory's words, that it was merged into a service
    it has never heard of.
    """

    @pytest.mark.parametrize("relative", _CENTRAL)
    def test_no_literal_in_the_merge_word_names_one(self, relative: str) -> None:
        path = _SRC / relative
        assert path.is_file(), relative
        named = {
            provider: text
            for text in _string_literals(path)
            for provider in _PROVIDERS
            if provider in text.lower()
        }
        assert named == {}, (
            f"{relative} names a hosting provider in a string the factory can "
            f"hand to somebody: {named}"
        )

    def test_the_two_words_say_the_remote_instead(self) -> None:
        from forge.pipeline.publication_record import RESULT_MERGED_AND_RUNNING

        assert RESULT_MERGED_AND_RUNNING == "merged into the remote and running"
        assert (
            merge_executor.RESULT_WORD_MERGED_AND_RUNNING
            == "merged-into-the-remote-and-running"
        )
        # And the third is STILL unreachable, which neither the rename nor
        # the publisher stage changes: it needs a deploy.
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        produced = [
            line
            for line in source.splitlines()
            if "result=RESULT_WORD_" in line.replace(" ", "")
        ]
        assert produced
        assert not any("MERGED_AND_RUNNING" in line for line in produced)


@pytest.mark.parametrize("word", FORBIDDEN)
def test_the_words_are_written_down_here_so_they_cannot_creep_back(word: str) -> None:
    """This file names them, which is how a later change is caught."""
    assert word
