"""The assumption review, pinned on the two manifests that taught it.

Both fixtures are the REAL manifests the spec seat wrote on 2026-09-13 for the
same sentence — arm A raised two assumptions about the window, arm B raised
four including "the endpoint requires authentication". Not fixtures a builder
wrote to satisfy its own test (the build loop's lesson 8).
"""

from __future__ import annotations

from pathlib import Path

from forge.planning.assumption_review import (
    CONVENTION_CONTRADICTED,
    INVENTED_REQUIREMENT,
    parse_manifest,
    review_assumptions,
)

FIXTURES = Path(__file__).parent / "fixtures" / "assumptions"
SENTENCE = (
    "Add a GET /users/created-per-day endpoint that returns the number of "
    "users created on each of the last 7 days, oldest first."
)
FACTS = (
    "src/users/router.py defines GET /users/count-today and GET "
    "/users/count-by-domain. None of them declares an authentication "
    "dependency. Both return their data unwrapped."
)


def _arm(name: str) -> str:
    return (FIXTURES / f"{name}-created-per-day.yaml").read_text(encoding="utf-8")


def test_arm_b_the_invented_authentication_is_caught() -> None:
    review = review_assumptions(_arm("arm-b"), request_text=SENTENCE)
    assert review.flagged_ids == ["ASSUM-004"]
    (finding,) = review.findings
    assert finding.pattern == INVENTED_REQUIREMENT
    assert finding.capability == "authentication"
    assert "was not asked for" in finding.sentence
    assert "Not stated in input" in finding.sentence  # the basis, quoted


def test_arm_b_with_the_fact_sheet_it_is_a_contradiction() -> None:
    review = review_assumptions(_arm("arm-b"), request_text=SENTENCE, repository_facts=FACTS)
    (finding,) = review.findings
    assert finding.pattern == CONVENTION_CONTRADICTED
    assert "sibling endpoints do the opposite" in finding.sentence


def test_arm_a_is_left_alone() -> None:
    review = review_assumptions(_arm("arm-a"), request_text=SENTENCE, repository_facts=FACTS)
    assert review.findings == []
    assert len(review.assumptions) == 2


def test_the_windows_shape_is_a_reading_not_an_addition() -> None:
    """Arm B's other three assumptions are about what 'last 7 days' means.
    Those are readings of what was said and must never be flagged."""
    review = review_assumptions(_arm("arm-b"), request_text=SENTENCE)
    assert set(review.flagged_ids) == {"ASSUM-004"}
    assert len(review.assumptions) == 4


def test_a_capability_the_person_asked_for_is_not_invented() -> None:
    manifest = """
assumptions:
- id: ASSUM-001
  scenario: A request without a token is rejected
  assumption: The endpoint requires authentication
  confidence: low
  basis: Not stated in input; common security practice
  human_response: deferred
"""
    asked = "Add a GET /users/count endpoint that requires an authentication token."
    assert review_assumptions(manifest, request_text=asked).findings == []


def test_no_basis_at_all_counts_as_not_asked_for() -> None:
    manifest = """
assumptions:
- id: ASSUM-009
  assumption: Results are paginated with a page size of 50
  confidence: low
  human_response: deferred
"""
    review = review_assumptions(manifest, request_text=SENTENCE)
    assert review.flagged_ids == ["ASSUM-009"]
    assert "no basis is given" in review.findings[0].sentence


def test_the_note_and_the_card_warning_say_it_plainly() -> None:
    review = review_assumptions(_arm("arm-b"), request_text=SENTENCE)
    note = review.note()
    assert note.startswith("The reviewer found 1 assumption(s)")
    assert "- ASSUM-004:" in note
    assert "Change nothing else." in note
    assert review.card_warning("ASSUM-004").startswith("⚠ not asked for")
    assert review.card_warning("ASSUM-001") is None


def test_an_unreadable_manifest_is_a_review_that_says_so() -> None:
    review = review_assumptions("assumptions: [unclosed", request_text=SENTENCE)
    assert review.findings == []
    assert review.assumptions == []
    assert review.unreadable and "could not be read" in review.unreadable
    assert review.receipt()["unreadable"] == review.unreadable


def test_empty_and_absent_manifests_are_empty_reviews() -> None:
    assert parse_manifest("") == ([], None)
    assert parse_manifest(None) == ([], None)
    assert review_assumptions("", request_text=SENTENCE).findings == []


def test_the_receipt_carries_every_finding() -> None:
    receipt = review_assumptions(_arm("arm-b"), request_text=SENTENCE).receipt()
    assert receipt["assumptions"] == 4
    assert receipt["flagged"] == ["ASSUM-004"]
    assert receipt["findings"][0]["pattern"] == INVENTED_REQUIREMENT


def test_a_word_that_merely_appears_is_not_a_capability() -> None:
    """Found on the first run of the driver's own suite: "build metadata" is not
    a response wrapper, and "UTC offset" is not pagination."""
    manifest = """
assumptions:
- id: ASSUM-001
  assumption: The version string comes from the build metadata.
  confidence: low
  basis: common practice; the input did not say
  human_response: deferred
- id: ASSUM-002
  assumption: Day boundaries use the server's UTC offset rather than local time
  confidence: low
  basis: Not stated in input; inferred from the phrasing
  human_response: deferred
- id: ASSUM-003
  assumption: The file header line is ignored when counting rows
  confidence: low
  basis: Not stated in input
  human_response: deferred
"""
    assert review_assumptions(manifest, request_text="add a GET /version endpoint").findings == []


def test_the_capability_phrases_still_catch_the_real_thing() -> None:
    manifest = """
assumptions:
- id: ASSUM-010
  assumption: Results are wrapped in an envelope with a total count field
  confidence: low
  basis: Not stated in input; common API practice
  human_response: deferred
- id: ASSUM-011
  assumption: The list is paginated with limit and offset query parameters
  confidence: low
  basis: Not stated in input
  human_response: deferred
- id: ASSUM-012
  assumption: A custom header X-Request-Source is required on every call
  confidence: low
  basis: Not stated in input; common practice
  human_response: deferred
"""
    review = review_assumptions(manifest, request_text=SENTENCE)
    assert review.flagged_ids == ["ASSUM-010", "ASSUM-011", "ASSUM-012"]
    assert [f.capability for f in review.findings] == ["a response wrapper", "pagination", "a required header"]
