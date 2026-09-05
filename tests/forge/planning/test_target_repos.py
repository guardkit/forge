"""Resolving and naming the repository a planning sentence asks for.

The 2026-09-05 spec, rule 3: an exact configuration key wins; a name with no
slash may match keys by their short half, resolving when every match is one
checkout and being refused when the matches are different checkouts. Rule 4:
the refusal names, in plain words, every repository a person MAY ask for,
with two keys for one checkout collapsed to a single name.

These are the short-name cases. They are proved here rather than through the
intake consumer because the wire payload's own validator
(``PlanningQueuedPayload``) accepts only ``org/name``, so a short name cannot
reach the consumer today — see this lane's report.
"""

from __future__ import annotations

from forge.planning.target_repos import (
    format_known_repos,
    known_repo_names,
    resolve_target_repo,
    unknown_repo_message,
)


class TestResolution:
    def test_exact_key_wins(self) -> None:
        paths = {"guardkit/api_test": "/srv/api_test"}
        assert resolve_target_repo("guardkit/api_test", paths).name == (
            "guardkit/api_test"
        )

    def test_basename_resolves_to_one_checkout(self) -> None:
        """Two keys, one checkout: the short name is unambiguous."""
        paths = {
            "guardkit/api_test": "/srv/repos/api_test",
            "appmilla/api_test": "/srv/repos/api_test",
        }

        resolution = resolve_target_repo("api_test", paths)

        assert resolution.name == "guardkit/api_test", (
            "the first exact key for that one checkout"
        )
        assert resolution.reason == ""

    def test_ambiguous_basename_refused(self) -> None:
        """Two keys, two checkouts: the short name is genuinely ambiguous."""
        paths = {
            "guardkit/api_test": "/srv/repos/guardkit-api_test",
            "appmilla/api_test": "/srv/repos/appmilla-api_test",
        }

        resolution = resolve_target_repo("api_test", paths)

        assert resolution.name is None
        assert "guardkit/api_test" in resolution.reason
        assert "appmilla/api_test" in resolution.reason

    def test_unknown_short_name_is_refused(self) -> None:
        resolution = resolve_target_repo("nowhere", {"g/api_test": "/srv/a"})
        assert resolution.name is None
        assert "nowhere" in resolution.reason

    def test_unknown_full_key_is_refused(self) -> None:
        resolution = resolve_target_repo("elsewhere/nowhere", {"g/a": "/srv/a"})
        assert resolution.name is None


class TestKnownNames:
    def test_aliases_collapse_to_one_name_per_checkout(self) -> None:
        paths = {
            "guardkit/api_test": "/srv/repos/api_test",
            "appmilla/api_test": "/srv/repos/api_test",
            "appmilla/study-tutor": "/srv/repos/study-tutor",
        }

        assert known_repo_names(paths) == ["api_test", "appmilla/study-tutor"]

    def test_the_refusal_sentence_lists_the_names(self) -> None:
        paths = {
            "guardkit/api_test": "/srv/repos/api_test",
            "appmilla/study-tutor": "/srv/repos/study-tutor",
        }

        assert unknown_repo_message("nowhere", paths) == (
            "I don't know a repository called nowhere. I can build in: "
            "guardkit/api_test, appmilla/study-tutor."
        )

    def test_no_repositories_configured_still_reads_as_a_sentence(self) -> None:
        assert format_known_repos({}) == (
            "nothing yet — no repositories are configured"
        )
