"""The spec example check (4 October 2026): worked examples about something
the request does not mention, by the words the PROJECT declares.

What this file proves:

* each kind in a web-API project's list catches the examples the 3 October
  evidence called padding, and keeps every example the owner accepted;
* the two positive controls (the readiness refusal, measured card 1018, and
  the version stamper's refusal, measured card 1395) are not flagged;
* negations: "takes no parameters" and "Do not write scenarios about … date
  ranges" license nothing, and an example that only restates "no" is not
  flagged; an owner's note that asks for an example licenses it;
* a command-line project's own list works the same way, so nothing here
  knows about web APIs;
* no list means no check, and a list that is there but cannot be read says so;
* the note never asks for removal without "unless the request needs it";
* the measured results are reproduced on a small saved sample, loaded from
  the project's declaration in a fixture file through the real reader.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from forge.planning.example_review import (
    ExampleReview,
    example_words_from,
    read_example_words,
    review_examples,
    worked_examples_in,
)
from forge.planning.repository_facts import LocalCheckoutReader, RepositoryUnreadable

FIXTURES = Path(__file__).parent / "fixtures" / "spec_examples"
WEB_API_CONFIG = FIXTURES / "web_api_config.yaml"
SAVED_CARDS = FIXTURES / "saved_cards.json"

CREATED_PER_DAY = (
    "Add a GET /users/created-per-day endpoint that returns the number of users "
    "created on each of the last 7 days, oldest first."
)

METHOD = "another request method"
ADDRESS = "another web address"
QUERY = "query parameters, filters or date ranges"
DOWN = "a dependency being down"
TIMES = "response times"
LOAD = "many requests at once or heavy load"


@pytest.fixture(scope="module")
def web_api_kinds():
    words = example_words_from(yaml.safe_load(WEB_API_CONFIG.read_text(encoding="utf-8")))
    assert words.kinds is not None
    return words.kinds


def _feature(*scenarios: tuple[str, list[str]]) -> str:
    lines = ["Feature: an endpoint", "", "  Background:", "    Given the service is running", ""]
    for title, steps in scenarios:
        lines += ["  # Why: written by the spec writer", "  @negative", f"  Scenario: {title}"]
        lines += [f"    {step}" for step in steps] + [""]
    return "\n".join(lines)


def _flags(review: ExampleReview) -> dict[str, tuple[str, ...]]:
    return {finding.title: finding.kinds for finding in review.findings}


# ---------------------------------------------------------------------------
# The rule, with a web-API project's list
# ---------------------------------------------------------------------------

#: The examples the 3 October evidence lists as padding, by title, and the
#: kind each is about.
_EVIDENCE_PADDING = [
    ("The endpoint fails gracefully when the audit log is unavailable", (DOWN,)),
    ("A request to an invalid path is rejected", (ADDRESS,)),
    ("The endpoint rejects non-GET requests", (METHOD,)),
    ("The endpoint returns an error when the database is unavailable", (DOWN,)),
    ("Concurrent requests return consistent results", (LOAD,)),
    ("A POST request to the endpoint is rejected", (METHOD,)),
    ("A request for a different date range is rejected", (QUERY,)),
    ("The endpoint rejects requests with query parameters", (QUERY,)),
    ("A PUT request to the active count endpoint is rejected", (METHOD,)),
    ("A HEAD request to the active count endpoint is rejected", (METHOD,)),
    ("A TRACE request to the active count endpoint is rejected", (METHOD,)),
    ("Query parameters are ignored and do not change the aggregate counts", (QUERY,)),
    ("The endpoint returns a response within acceptable time for large user sets", (TIMES, LOAD)),
    ("A request to an invalid path on the user statistics service is rejected", (ADDRESS,)),
]


@pytest.mark.parametrize(("title", "kinds"), _EVIDENCE_PADDING)
def test_each_kind_catches_its_evidence(web_api_kinds, title: str, kinds: tuple[str, ...]) -> None:
    review = review_examples(
        _feature((title, ["When the endpoint is called", "Then the reply is checked"])),
        request_text=CREATED_PER_DAY,
        kinds=web_api_kinds,
    )
    assert _flags(review) == {title: kinds}


def test_the_examples_the_owner_accepted_are_all_kept(web_api_kinds) -> None:
    accepted = _feature(
        ("The reply has seven entries", ["When GET /users/created-per-day is sent", "Then 7 entries come back"]),
        ("The oldest day is six days before today", ["Then the first entry is six days ago"]),
        ("The newest day is today", ["Then the last entry is today"]),
        ("Days with no users are reported as zero", ["Then a day with no users has a count of 0"]),
        ("Soft-deleted users are still counted", ["Given a user was soft-deleted", "Then it is counted"]),
        ("Both counts are returned", ["Then active and inactive counts come back"]),
        ("Zeros when there are no users", ["Given no users exist", "Then both counts are 0"]),
    )
    review = review_examples(accepted, request_text=CREATED_PER_DAY, kinds=web_api_kinds)
    assert review.findings == []
    assert len(review.titles) == 7


def test_the_readiness_refusal_is_not_flagged(web_api_kinds) -> None:
    """Positive control, measured card 1018: "service unavailable status" is
    the readiness endpoint's job, and the request asks for readiness."""
    feature = _feature(
        (
            "The ready endpoint returns failure when the service is not yet ready",
            [
                "When I request the ready endpoint",
                "Then the request should fail with a service unavailable status",
                "And the response body should indicate the service is not ready",
            ],
        ),
        (
            "The ready endpoint does not perform external dependency checks",
            [
                "Given the api_test service process is initialized",
                "And an external dependency is unavailable",
                "When I request the ready endpoint",
                "Then the request should succeed",
            ],
        ),
    )
    review = review_examples(
        feature, request_text="Add a *Ready endpoint on api_test* (D450)", kinds=web_api_kinds
    )
    assert review.findings == []


def test_the_stamper_refusal_is_not_flagged_but_an_outage_beside_it_is(web_api_kinds) -> None:
    """Positive control, measured card 1395: the request asks for the
    stamper's refusal path, and "metadata is unavailable" is that path. The
    service being unavailable, in the same card, was not asked for."""
    request = (
        'Add a `/version` endpoint that returns the app version and the git commit '
        "it was built from — a genuine test of the stamper's refusal path under enforcement."
    )
    feature = _feature(
        (
            "The version endpoint signals missing metadata when stamper refused to stamp",
            [
                "Given the application was built without stamper metadata",
                "When I request the version information",
                "Then the request should be rejected with precondition required",
                "And the response should indicate that version metadata is unavailable",
            ],
        ),
        (
            "The version endpoint fails gracefully when the service is unavailable",
            [
                "Given the version endpoint service is unavailable",
                "When I request the version information",
                "Then the request should fail with service unavailable",
            ],
        ),
        (
            "The stamper refuses to stamp when git rev-parse HEAD fails",
            ["Given git rev-parse HEAD exits non-zero", "Then the build fails"],
        ),
    )
    review = review_examples(feature, request_text=request, kinds=web_api_kinds)
    assert _flags(review) == {"The version endpoint fails gracefully when the service is unavailable": (DOWN,)}


# ---------------------------------------------------------------------------
# Negations, and what the owner asked for
# ---------------------------------------------------------------------------


def test_a_request_that_says_no_parameters_licenses_none(web_api_kinds) -> None:
    request = (
        "Add a GET /users/created-per-day endpoint. The endpoint takes no query "
        "parameters and always returns exactly 7 days. Do not write scenarios about "
        "rejecting unauthenticated requests, about other day counts, or about date ranges."
    )
    feature = _feature(
        ("The endpoint rejects requests with query parameters", ["When ?days=30 is sent", "Then it is refused"]),
        ("A request for a different date range is rejected", ["When a date range is sent", "Then it is refused"]),
    )
    review = review_examples(feature, request_text=request, kinds=web_api_kinds)
    assert set(_flags(review)) == {
        "The endpoint rejects requests with query parameters",
        "A request for a different date range is rejected",
    }


def test_a_request_that_asks_for_parameters_licenses_them(web_api_kinds) -> None:
    request = "Add GET /users with an optional limit parameter."
    feature = _feature(("A limit parameter of 5 returns five users", ["When ?limit=5 is sent"]))
    assert review_examples(feature, request_text=request, kinds=web_api_kinds).findings == []


def test_an_example_that_only_says_no_is_not_flagged(web_api_kinds) -> None:
    feature = _feature(
        ("The endpoint takes no query parameters", ["When it is called without parameters", "Then 7 days come back"]),
        ("The count is right", ["Then it does not depend on concurrent writers"]),
    )
    assert review_examples(feature, request_text=CREATED_PER_DAY, kinds=web_api_kinds).findings == []


def test_an_example_the_owner_asked_for_in_a_note_is_not_flagged(web_api_kinds) -> None:
    feature = _feature(("A POST request to the endpoint is rejected", ["When POST is sent", "Then 405"]))
    asked = review_examples(
        feature,
        request_text=CREATED_PER_DAY,
        notes=["Please also show that a POST is refused."],
        kinds=web_api_kinds,
    )
    assert asked.findings == []
    told_not_to = review_examples(
        feature,
        request_text=CREATED_PER_DAY,
        notes=["Drop the POST example."],
        kinds=web_api_kinds,
    )
    assert _flags(told_not_to) == {"A POST request to the endpoint is rejected": (METHOD,)}


def test_the_request_and_each_note_are_read_separately(web_api_kinds) -> None:
    """A request with no closing full stop must not swallow the note after
    it into its "Do not" sentence."""
    feature = _feature(("A request for a different date range is rejected", ["When a date range is sent"]))
    request = "Add GET /users/created-per-day. Do not write scenarios about date ranges"
    told_not_to = review_examples(feature, request_text=request, kinds=web_api_kinds)
    assert set(_flags(told_not_to)) == {"A request for a different date range is rejected"}
    asked = review_examples(
        feature, request_text=request, notes=["Please include one date range example."], kinds=web_api_kinds
    )
    assert asked.findings == []


def test_a_listed_drop_and_a_word_ending_in_nt_license_nothing(web_api_kinds) -> None:
    feature = _feature(
        ("A POST request to the endpoint is rejected", ["When POST is sent"]),
        ("Concurrent requests return consistent counts", ["When ten requests arrive at once"]),
    )
    notes = ["Two changes:\n- Drop the POST example\n- keep the rest", "It doesn't need concurrent handling."]
    review = review_examples(feature, request_text=CREATED_PER_DAY, notes=notes, kinds=web_api_kinds)
    assert set(_flags(review)) == {
        "A POST request to the endpoint is rejected",
        "Concurrent requests return consistent counts",
    }
    licensed = review_examples(
        feature, request_text=CREATED_PER_DAY, notes=["Two changes:\n- keep the POST example"], kinds=web_api_kinds
    )
    assert set(_flags(licensed)) == {"Concurrent requests return consistent counts"}


def test_a_hard_line_break_does_not_end_a_do_not_sentence(web_api_kinds) -> None:
    """Measured cards 2940 and 2961 were typed with hard line breaks; the
    "Do not" sentence runs across them."""
    request = (
        "Add a GET /users/created-per-day endpoint. Do not write scenarios about rejecting\n"
        "unauthenticated requests, about other day counts, or about date ranges."
    )
    feature = _feature(("A request for a different date range is rejected", ["When a date range is sent"]))
    assert set(_flags(review_examples(feature, request_text=request, kinds=web_api_kinds))) == {
        "A request for a different date range is rejected"
    }


def _kind(*example_words: str):
    words = example_words_from(
        {"spec_examples": {"not_asked_for": [{"name": "k", "example_words": list(example_words)}]}}
    )
    assert words.kinds is not None
    return words.kinds


def test_a_star_on_its_own_spans_at_most_two_words() -> None:
    kinds = _kind("database * unavailab*")
    for text, flagged in (
        ("Given the database is unavailable", True),
        ("Given the database is briefly unavailable", True),
        ("Given the database is very briefly unavailable", False),
        ("Given the database and the cache and the queue are unavailable", False),
    ):
        review = review_examples(_feature(("One", [text])), request_text="Add GET /x.", kinds=kinds)
        assert bool(review.findings) is flagged, text


def test_a_phrase_ends_on_a_word_boundary() -> None:
    kinds = _kind("405", "outage*")
    for text, flagged in (
        ("Then the reply is 405", True),
        ("Then the reply carries id 4051", False),
        ("Then the reply carries id x405", False),
        ("Given an outage of the store", True),
        ("Given outages", True),
    ):
        review = review_examples(_feature(("One", [text])), request_text="Add GET /x.", kinds=kinds)
        assert bool(review.findings) is flagged, text


def test_comments_and_tags_are_not_read() -> None:
    feature = (
        "Feature: f\n"
        "  # Why: the request says POST requests matter\n"
        "  @concurrency\n"
        "  Scenario: One\n"
        "    # [ASSUMPTION] concurrent writers\n"
        "    Given nothing\n"
        "  Scenario Outline: Two\n"
        "    When <x>\n"
    )
    assert worked_examples_in(feature) == [("One", "One\nGiven nothing"), ("Two", "Two\nWhen <x>")]


# ---------------------------------------------------------------------------
# Another kind of project: its own words, the same mechanism
# ---------------------------------------------------------------------------


def test_a_command_line_projects_own_list_works_the_same_way() -> None:
    words = example_words_from(
        {
            "spec_examples": {
                "not_asked_for": [
                    {
                        "name": "another command or option",
                        "example_words": ["unknown command*", "unknown option*", "unrecogni* flag*"],
                    },
                    {"name": "being interrupted", "example_words": ["ctrl-c", "interrupt*", "killed"]},
                ]
            }
        }
    )
    assert words.kinds is not None
    feature = _feature(
        ("Listing prints one line per file", ["When `ls-tool` runs", "Then each file is on its own line"]),
        ("An unknown option prints the usage", ["When `ls-tool --frob` runs", "Then the usage is printed"]),
        ("Pressing ctrl-c stops cleanly", ["When the user presses ctrl-c", "Then nothing is left behind"]),
    )
    review = review_examples(feature, request_text="Add an ls-tool that lists files.", kinds=words.kinds)
    assert _flags(review) == {
        "An unknown option prints the usage": ("another command or option",),
        "Pressing ctrl-c stops cleanly": ("being interrupted",),
    }
    licensed = review_examples(
        feature, request_text="Add an ls-tool; an unknown option must print the usage.", kinds=words.kinds
    )
    assert set(_flags(licensed)) == {"Pressing ctrl-c stops cleanly"}


# ---------------------------------------------------------------------------
# No list, and a list that cannot be read
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, files: dict[str, str], refused: dict[str, str] | None = None) -> None:
        self._files = files
        self.refused = dict(refused or {})

    def read_text(self, path: str) -> str | None:
        return self._files.get(path)


def test_no_block_means_no_check() -> None:
    words = example_words_from({"memory": {"project": "x"}})
    assert words.kinds is None and words.unreadable is None
    assert words.not_checked == "the project declares no `spec_examples` block"
    from_reader = read_example_words(_Reader({".guardkit/config.yaml": "memory:\n  project: x\n"}))
    assert from_reader.kinds is None and from_reader.unreadable is None


def test_a_file_that_cannot_be_read_is_no_check_and_the_record_says_why() -> None:
    missing = read_example_words(
        _Reader({}, refused={".guardkit/config.yaml": "the helper answered 404: no such file"})
    )
    assert missing.kinds is None and missing.unreadable is None
    assert missing.not_checked == (
        "`.guardkit/config.yaml` was not read (the helper answered 404: no such file)"
    )

    class _Down:
        def read_text(self, path: str) -> str | None:
            raise RepositoryUnreadable("the sandbox helper could not be reached")

    down = read_example_words(_Down())
    assert down.kinds is None and down.unreadable is None
    assert "the sandbox helper could not be reached" in (down.not_checked or "")

    broken = read_example_words(_Reader({".guardkit/config.yaml": "- just\n- a list\n"}))
    assert broken.kinds is None and broken.unreadable is None
    assert "could not be parsed" in (broken.not_checked or "")


@pytest.mark.parametrize(
    ("block", "why"),
    [
        (["not", "a mapping"], "is not a set of settings"),
        ({"not_asked_for": "outage"}, "has no `not_asked_for` list"),
        ({"not_asked_for": [{"example_words": ["x"]}]}, "has entry 1 with no name"),
        ({"not_asked_for": [{"name": "x", "example_words": "outage"}]}, "the example_words of 'x' is not a list"),
        (
            {"not_asked_for": [{"name": "x", "example_words": ["outage", 405]}]},
            "the example_words of 'x' has a phrase that is not text (405)",
        ),
        (
            {"not_asked_for": [{"name": "x", "example_words": ["a"], "request_words": [None]}]},
            "the request_words of 'x' has a phrase that is not text (None)",
        ),
    ],
)
def test_a_block_that_cannot_be_read_says_so(block, why: str) -> None:
    words = example_words_from({"spec_examples": block})
    assert words.kinds is None
    assert words.unreadable == why


# ---------------------------------------------------------------------------
# The note and the card lines
# ---------------------------------------------------------------------------


def test_the_note_names_each_example_and_never_orders_removal_outright(web_api_kinds) -> None:
    feature = _feature(
        ("A POST request to the endpoint is rejected", ["When POST is sent"]),
        ("The endpoint fails gracefully when the database is unavailable", ["Given the database is down"]),
    )
    note = review_examples(feature, request_text=CREATED_PER_DAY, kinds=web_api_kinds).note()
    assert note == (
        "These worked examples look like things the request does not mention:\n"
        '- "A POST request to the endpoint is rejected" (another request method)\n'
        '- "The endpoint fails gracefully when the database is unavailable" (a dependency being down)\n'
        "\n"
        "Remove each one unless the request needs it. If you keep one, quote the words of "
        "the request that need it in its # Why: line. Remove any assumption written only "
        "for an example you remove. Do not add other examples of the same kind. Keep every "
        "other worked example exactly as it is."
    )
    for sentence in note.split(". "):
        if "remove" in sentence.lower():
            assert "unless the request needs it" in sentence or "an example you remove" in sentence


def test_the_card_names_what_was_removed_and_what_was_kept(web_api_kinds) -> None:
    first = review_examples(
        _feature(
            ("A POST request to the endpoint is rejected", ["When POST is sent"]),
            ("The endpoint fails gracefully when the database is unavailable", ["Given the database is down"]),
            ("Concurrent requests return consistent counts", ["When ten requests arrive at once"]),
        ),
        request_text=CREATED_PER_DAY,
        kinds=web_api_kinds,
    )
    final = review_examples(
        _feature(("Concurrent requests return consistent counts", ["When ten requests arrive at once"])),
        request_text=CREATED_PER_DAY,
        kinds=web_api_kinds,
    )
    assert final.card_lines(first) == [
        'Removed as not asked for: "A POST request to the endpoint is rejected"; "The endpoint '
        'fails gracefully when the database is unavailable". If one of them was needed, send a note.',
        'Not asked for, but kept: "Concurrent requests return consistent counts" (many requests '
        "at once or heavy load). If you approve, it will be built; to drop it, send a note.",
    ]
    clean = review_examples(_feature(("Seven days", ["Then 7"])), request_text=CREATED_PER_DAY, kinds=web_api_kinds)
    assert clean.card_lines(clean) == []


# ---------------------------------------------------------------------------
# The measured results, reproduced on a small saved sample
# ---------------------------------------------------------------------------


def test_the_measured_results_are_reproduced_from_the_declared_file(tmp_path: Path) -> None:
    """A web-API project's own `.guardkit/config.yaml`, read through the real
    checkout reader, against eleven saved cards from the 4 October
    measurement: every example flagged or not exactly as measured (18 of 68
    flagged, both positive controls not)."""
    (tmp_path / ".guardkit").mkdir()
    (tmp_path / ".guardkit" / "config.yaml").write_text(WEB_API_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    words = read_example_words(LocalCheckoutReader(str(tmp_path)))
    assert words.kinds is not None and len(words.kinds) == 6

    cards = json.loads(SAVED_CARDS.read_text(encoding="utf-8"))
    measured: dict[tuple[int, str], tuple[str, ...]] = {}
    found: dict[tuple[int, str], tuple[str, ...]] = {}
    for card in cards:
        review = review_examples(
            card["worked_examples"],
            request_text=card["request_text"],
            notes=card["owner_notes_before"],
            kinds=words.kinds,
        )
        flags = _flags(review)
        for example in card["examples"]:
            key = (card["measured_card"], example["title"])
            measured[key] = tuple(example["flagged"])
            found[key] = flags.get(example["title"], ())
        assert review.titles == [example["title"] for example in card["examples"]]
    assert found == measured
    assert len(found) == 68
    assert sum(1 for kinds in found.values() if kinds) == 18
    for control in (
        (1018, "The ready endpoint returns failure when the service is not yet ready"),
        (1395, "The version endpoint signals missing metadata when stamper refused to stamp"),
    ):
        assert found[control] == ()
