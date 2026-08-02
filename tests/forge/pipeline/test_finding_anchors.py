"""The finding anchor — the fix journey's dedup key (LI stage-2 §5, FB3).

Two things are pinned here and they are different in kind:

1. **The producer's ``anchor`` field wins.** That is the contract with the
   builder checkout (``review_runner.finding_anchor`` /
   ``anchored_findings``): the side that knows the repo root mints the
   anchor, and this side reads it. A test that let the fallback quietly
   override a supplied anchor would be pinning a second statement of the
   rule.
2. **The fallback is tolerant and shaped like the producer's output.** It
   exists for a findings block minted before the producer emitted anchors,
   so its job is to be comparable with an anchored block across a cycle
   boundary — same two-part string, same sentinels, same trailing-``:line``
   strip.
"""

from __future__ import annotations

from forge.pipeline.finding_anchors import (
    ANCHOR_NO_FILE,
    ANCHOR_NO_SEVERITY,
    FINDING_ANCHORS_DETAILS_KEY,
    derive_finding_anchors,
    finding_anchor,
)


class TestTheProducerWins:
    def test_a_supplied_anchor_is_used_verbatim(self) -> None:
        assert (
            finding_anchor({"anchor": "src/core/config.py|critical",
                            "file": "somewhere/else.py",
                            "severity": "low"})
            == "src/core/config.py|critical"
        )

    def test_a_supplied_anchor_is_stripped_not_reshaped(self) -> None:
        assert finding_anchor({"anchor": "  a/b.py|high  "}) == "a/b.py|high"

    def test_a_blank_anchor_falls_back(self) -> None:
        """An empty string is not an anchor; it is a missing one."""
        assert (
            finding_anchor({"anchor": "   ", "file": "src/a.py", "severity": "High"})
            == "src/a.py|high"
        )


class TestTheFallback:
    def test_file_and_severity_make_the_two_part_key(self) -> None:
        assert (
            finding_anchor({"file": "src/a.py", "severity": "critical"})
            == "src/a.py|critical"
        )

    def test_severity_is_lowercased_so_case_drift_is_not_a_new_defect(
        self,
    ) -> None:
        assert finding_anchor({"file": "a.py", "severity": "HIGH"}) == "a.py|high"

    def test_a_trailing_line_number_never_enters_the_identity(self) -> None:
        """The measured drift: 14 / null / 0 / 36 for ONE defect."""
        assert (
            finding_anchor({"file": "src/parser.py:88", "severity": "high"})
            == finding_anchor({"file": "src/parser.py:14:4", "severity": "high"})
            == "src/parser.py|high"
        )

    def test_a_dot_slash_prefix_and_backslashes_normalize(self) -> None:
        assert (
            finding_anchor({"file": ".\\src\\a.py", "severity": "low"})
            == "src/a.py|low"
        )

    def test_a_missing_file_gets_the_named_sentinel_not_an_empty_half(
        self,
    ) -> None:
        assert (
            finding_anchor({"severity": "low"}) == f"{ANCHOR_NO_FILE}|low"
        )

    def test_a_missing_severity_gets_the_named_sentinel(self) -> None:
        assert (
            finding_anchor({"file": "a.py"}) == f"a.py|{ANCHOR_NO_SEVERITY}"
        )

    def test_non_string_fields_do_not_crash_the_turn(self) -> None:
        """Findings are model output that crossed a process boundary."""
        assert (
            finding_anchor({"file": 7, "severity": ["high"]})
            == f"{ANCHOR_NO_FILE}|{ANCHOR_NO_SEVERITY}"
        )


class TestDeriveFindingAnchors:
    def test_order_is_preserved_and_duplicates_collapse(self) -> None:
        """39 findings on one file under 23 titles = ONE anchor."""
        findings = [
            {"file": "src/core/config.py", "severity": "critical"},
            {"file": "src/api/routes.py", "severity": "high"},
            {"file": "src/core/config.py", "severity": "critical"},
        ]
        assert derive_finding_anchors(findings) == (
            "src/core/config.py|critical",
            "src/api/routes.py|high",
        )

    def test_none_and_empty_both_answer_empty(self) -> None:
        assert derive_finding_anchors(None) == ()
        assert derive_finding_anchors([]) == ()

    def test_non_mapping_elements_are_skipped_never_raised_on(self) -> None:
        assert derive_finding_anchors(
            ["a string", 7, None, {"file": "a.py", "severity": "low"}]
        ) == ("a.py|low",)

    def test_anchored_and_legacy_findings_compare_across_a_cycle(self) -> None:
        """The whole point of mirroring the producer's shape.

        Cycle 1's block was minted before the producer emitted anchors;
        cycle 2's carries them. The no-progress rule compares the two sets,
        so they have to be the SAME strings for the same defect.
        """
        legacy = derive_finding_anchors(
            [{"file": "./src/core/config.py:14", "severity": "Critical"}]
        )
        anchored = derive_finding_anchors(
            [
                {
                    "anchor": "src/core/config.py|critical",
                    "file": "src/core/config.py",
                    "line": 36,
                    "severity": "critical",
                }
            ]
        )
        assert legacy == anchored == ("src/core/config.py|critical",)


def test_the_details_key_is_spelled_once() -> None:
    """The writer and the projection both import THIS constant."""
    assert FINDING_ANCHORS_DETAILS_KEY == "finding_anchors"
