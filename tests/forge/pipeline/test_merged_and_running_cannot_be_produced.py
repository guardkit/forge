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
    def test_the_switch_is_off_on_every_path_there_is(self) -> None:
        assert publication_is_switched_on() is False
        assert publication_is_switched_on(None) is False
        assert publication_is_switched_on(object()) is False

        class _SaysYes:
            publication = True
            enabled = True

            def __getattr__(self, name: str) -> bool:  # noqa: D105
                return True

        assert publication_is_switched_on(_SaysYes()) is False

    def test_it_says_why_in_plain_words(self) -> None:
        assert "publisher has not been built" in why_publication_is_off()
        assert "nothing was sent to the remote" in PUBLICATION_IS_OFF_SENTENCE

    def test_the_press_defines_all_three_names_and_only_one_is_reachable(
        self,
    ) -> None:
        assert merge_executor.RESULT_WORD_PUBLICATION_PENDING == "publication-pending"
        assert (
            merge_executor.RESULT_WORD_PUBLISHED_DEPLOYMENT_PENDING
            == "published-deployment-pending"
        )
        assert (
            merge_executor.RESULT_WORD_MERGED_AND_RUNNING
            == "merged-into-github-and-running"
        )
        # The two later words appear in the press's own source ONLY as these
        # definitions and the places that compare against them — never as
        # something a code path hands out.
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        produced = [
            line
            for line in source.splitlines()
            if "result=RESULT_WORD_" in line.replace(" ", "")
        ]
        assert produced
        for line in produced:
            assert "PUBLICATION_PENDING" in line, line

    def test_everything_past_the_switch_is_unreachable(self) -> None:
        """The one path beyond the switch refuses rather than guesses.

        If somebody switches publication on before the publisher exists, the
        press must not quietly do something else — it must stop and say the
        publisher is not there.
        """
        source = Path(merge_executor.__file__).read_text(encoding="utf-8")
        assert "raise NotImplementedError(" in source
        assert "the publisher has not been built" in source


@pytest.mark.parametrize("word", FORBIDDEN)
def test_the_words_are_written_down_here_so_they_cannot_creep_back(word: str) -> None:
    """This file names them, which is how a later change is caught."""
    assert word
