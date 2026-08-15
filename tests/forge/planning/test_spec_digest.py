"""The spec digest's deterministic consistency check — nine gates, one per row.

The digest is the plain-language list a person reads INSTEAD of the spec, so
this check is the whole reason that read can be trusted. Every gate is proven
here by its own named error string, and the two load-bearing cases get their own
tests: an OMISSION (a worked example dropped from the digest) and a SUBSTITUTION
(a title quietly paraphrased). Neither can pass.

Pure functions, no network, no store, no broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge.planning.spec_digest import (
    DIGEST_ERROR_PREFIX,
    check_digest_consistency,
    parse_scenarios,
)

FEATURE = (
    "Feature: version endpoint\n"
    "\n"
    "  @key-example @smoke\n"
    "  Scenario: Version endpoint returns the running build\n"
    "    Given the service is running\n"
    "    When the version is asked for\n"
    "    Then the build it started from comes back\n"
    "\n"
    "  @negative\n"
    "  Scenario: Version endpoint rejects an unknown format\n"
    "    Given the service is running\n"
    "    When an unpublished format is asked for\n"
    "    Then the request is refused\n"
)

MANIFEST: dict[str, Any] = {
    "assumptions": [
        {
            "id": "ASSUM-001",
            "assumption": "The version string comes from the build metadata.",
            "basis": "common practice; the input did not say",
        }
    ]
}


def _digest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "feature": "version-endpoint",
        "generated": "2026-08-14T10:00:00Z",
        "scenarios": [
            {
                "title": "Version endpoint returns the running build",
                "tags": ["@key-example", "@smoke"],
                "sentence": (
                    "Asking the service which version it is running returns the "
                    "build it was started from."
                ),
            },
            {
                "title": "Version endpoint rejects an unknown format",
                "tags": ["@negative"],
                "sentence": (
                    "Asking for the version in a format the service does not "
                    "publish is refused rather than guessed at."
                ),
            },
        ],
        "assumptions": [
            {
                "id": "ASSUM-001",
                "text": "The version string comes from the build metadata.",
                "basis": "common practice; the input did not say",
            }
        ],
    }
    base.update(overrides)
    return base


def _check(digest: Any, feature: str = FEATURE) -> list[str]:
    return check_digest_consistency(digest, feature, MANIFEST, "version-endpoint")


def test_a_true_digest_passes_every_gate() -> None:
    assert _check(_digest()) == []


def test_every_error_is_named_so_the_pipeline_can_separate_it() -> None:
    """The prefix is load-bearing: the leg tells a digest failure (which stops
    the run) from the advisory self-check errors (which the oracles follow)."""
    errors = _check(_digest(scenarios=[]))
    assert errors
    assert all(e.startswith(DIGEST_ERROR_PREFIX) for e in errors)


# ---------------------------------------------------------------------------
# The two load-bearing cases
# ---------------------------------------------------------------------------


def test_an_omitted_worked_example_is_named_and_the_count_fires_too() -> None:
    """Gate 1 names the missing title; gate 2 fires INDEPENDENTLY, so lowering
    the count to match cannot hide the omission — it adds a second error."""
    digest = _digest()
    digest["scenarios"] = digest["scenarios"][:1]

    errors = _check(digest)

    assert any(
        "'Version endpoint rejects an unknown format' is in the spec but "
        "missing from the digest" in e
        for e in errors
    )
    assert any("describes 1 worked examples but the spec has 2" in e for e in errors)


def test_a_paraphrased_title_is_named_both_ways() -> None:
    """Gate 1 reports the spec's title as MISSING and the digest's as EXTRA —
    the titles are transcribed, not the generator's to choose."""
    digest = _digest()
    digest["scenarios"][1]["title"] = "Version endpoint refuses a weird format"

    errors = _check(digest)

    assert any("is in the spec but missing from the digest" in e for e in errors)
    assert any("which is not a worked example in the spec" in e for e in errors)


def test_reordering_is_caught_even_though_the_set_matches() -> None:
    digest = _digest()
    digest["scenarios"].reverse()

    errors = _check(digest)

    assert any("the order must match the spec" in e for e in errors)


# ---------------------------------------------------------------------------
# Gate by gate
# ---------------------------------------------------------------------------


def test_gate_3_labels_must_be_verbatim_and_in_order() -> None:
    digest = _digest()
    digest["scenarios"][0]["tags"] = ["@smoke", "@key-example"]

    errors = _check(digest)

    assert any("the labels for" in e and "but the spec has" in e for e in errors)


def test_gate_3_survives_the_comment_the_prompt_itself_asks_for() -> None:
    """A ``# Why:`` line between a scenario's labels and its header is exactly
    what the spec-writer's own prompt asks for. Reading it as a gap emptied the
    label list and failed every scenario of a spec written as instructed."""
    commented = FEATURE.replace(
        "  @key-example @smoke\n  Scenario:",
        "  @key-example @smoke\n  # Why: the main thing this endpoint is for\n  Scenario:",
    )

    assert parse_scenarios(commented)[0][1] == ["@key-example", "@smoke"]
    assert check_digest_consistency(
        _digest(), commented, MANIFEST, "version-endpoint"
    ) == []


def test_gate_4_refuses_an_empty_or_unterminated_sentence() -> None:
    assert any(
        "has no plain-English sentence" in e
        for e in _check(_replace_sentence(_digest(), 0, "   "))
    )
    assert any(
        "does not end in a full stop" in e
        for e in _check(_replace_sentence(_digest(), 0, "no full stop here"))
    )


def test_gate_4_refuses_two_sentences() -> None:
    two = "The build comes back. The service then settles."
    assert any(
        "is more than one sentence" in e
        for e in _check(_replace_sentence(_digest(), 0, two))
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "The version is returned in the published format, e.g. plain text.",
        "The identifier is the build number, i.e. the one baked in at build time.",
        "The times are shown in U.S. Eastern time rather than the server's.",
        "The newer build wins vs. the one already running.",
    ],
)
def test_gate_4_does_not_call_an_abbreviation_a_second_sentence(sentence: str) -> None:
    """The gate exists to catch two sentences, not to fail correct digests."""
    assert _check(_replace_sentence(_digest(), 0, sentence)) == []


def test_gate_4_still_catches_a_sentence_ending_on_the_word_no() -> None:
    """"no." is deliberately absent from the abbreviation list: a sentence
    really can end on that word, and swallowing the break would let a genuine
    two-sentence digest through."""
    assert any(
        "is more than one sentence" in e
        for e in _check(
            _replace_sentence(_digest(), 0, "The answer is no. The build is refused.")
        )
    )


def test_gate_5_refuses_pasted_specification_steps() -> None:
    pasted = "Given the service is running the version is returned."
    assert any(
        "contains a specification step line" in e
        for e in _check(_replace_sentence(_digest(), 0, pasted))
    )
    assert any(
        "uses the specification word" in e
        for e in _check(
            _replace_sentence(_digest(), 0, "This Scenario covers the happy path.")
        )
    )


def test_gate_5_allows_when_opening_ordinary_english() -> None:
    """"When the service restarts, …" is a perfect plain-English digest
    sentence. A bare substring ban rejected it and failed a spec that was fine."""
    fine = "When the service restarts, the version endpoint waits rather than answering with the old build."
    assert _check(_replace_sentence(_digest(), 0, fine)) == []


def test_gate_6_checks_the_assumption_and_its_reason() -> None:
    digest = _digest()
    digest["assumptions"][0]["text"] = "Something the machine did not assume."
    assert any("wording of assumption ASSUM-001 differs" in e for e in _check(digest))

    digest = _digest()
    digest["assumptions"][0]["basis"] = "confirmed with the operations team"
    assert any("reason for assumption ASSUM-001 differs" in e for e in _check(digest))


def test_gate_6_catches_a_missing_and_an_invented_assumption() -> None:
    assert any(
        "ASSUM-001 is in the assumptions file but missing from the digest" in e
        for e in _check(_digest(assumptions=[]))
    )
    digest = _digest()
    digest["assumptions"].append({"id": "ASSUM-999", "text": "x", "basis": "y"})
    assert any(
        "lists assumption ASSUM-999, which is not in the assumptions file" in e
        for e in _check(digest)
    )


def test_gate_7_requires_a_full_timestamp() -> None:
    assert any(
        "is not a full ISO 8601 timestamp" in e
        for e in _check(_digest(generated="2026-08-14"))
    )


def test_gate_7_accepts_the_stamp_yaml_parses_into_a_datetime() -> None:
    """An unquoted stamp is resolved to a datetime by YAML, whose str() has a
    space where the T belongs. The gate reads the stamp that was written."""
    import datetime

    stamped = datetime.datetime(2026, 8, 14, 10, 0, tzinfo=datetime.UTC)
    assert _check(_digest(generated=stamped)) == []


def test_gate_8_catches_a_digest_about_a_different_feature() -> None:
    assert any(
        "names feature 'other-thing' but the spec files are" in e
        for e in _check(_digest(feature="other-thing"))
    )


def test_gate_9_refuses_a_field_with_nothing_behind_it() -> None:
    """``earlier_assumptions`` claimed to quote the brief's record, and no code
    can check it against one. Free text wearing a record's clothes is refused."""
    assert any(
        "which is not part of the digest" in e
        for e in _check(_digest(earlier_assumptions=[{"text": "anything"}]))
    )


def test_a_non_mapping_digest_is_refused_rather_than_crashing() -> None:
    assert _check("not a digest") == [
        DIGEST_ERROR_PREFIX + "the digest is not a mapping"
    ]
    assert _check(_digest(scenarios="nope")) == [
        DIGEST_ERROR_PREFIX + "the digest 'scenarios' field is not a list"
    ]


def test_a_feature_ending_flush_against_its_last_step_keeps_that_example() -> None:
    """Without a guaranteed final newline the block scan drops the last
    scenario, and the digest's correct final entry is reported as an invention."""
    flush = FEATURE.rstrip("\n")
    assert len(parse_scenarios(flush)) == 2
    assert check_digest_consistency(
        _digest(), flush, MANIFEST, "version-endpoint"
    ) == []


def _replace_sentence(digest: dict[str, Any], index: int, sentence: str) -> dict[str, Any]:
    digest["scenarios"][index]["sentence"] = sentence
    return digest
