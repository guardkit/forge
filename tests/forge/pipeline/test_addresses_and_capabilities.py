"""The web-address and capability readers the merge card's scope report uses.

These readers were written on 15 September 2026 for a check that read a
plan's task documents against the request before the plan was committed.
That check had no caller after 17 September and was deleted on 16 October
2026; the scope report was always the live reader of these helpers, so they
moved to :mod:`forge.pipeline.addresses_and_capabilities` unchanged. The
tests below are the old check's tests for the same cases, asked of the
helpers directly.
"""

from __future__ import annotations

import pytest

from forge.pipeline.addresses_and_capabilities import (
    _ALL_CAPABILITIES,
    _is_a_place_on_disk,
    _route_shaped,
)

#: The scope report's own default when a repository declares no test roots.
DEFAULT_TEST_ROOTS = ("tests", "test", "spec", "specs")


def _capabilities_named_in(text: str) -> list[str]:
    return [name for name, pattern in _ALL_CAPABILITIES if pattern.search(text)]


# ---------------------------------------------------------------------------
# Web addresses
# ---------------------------------------------------------------------------


def test_a_web_address_inside_an_acceptance_criterion_is_read() -> None:
    found = _route_shaped("- [ ] GET /stats/users-created-per-day returns 200 OK\n")

    assert found == ["/stats/users-created-per-day"]


def test_a_file_under_a_declared_test_root_is_a_place_on_disk() -> None:
    roots = ["tests/health", "tests/users"]

    found = _route_shaped("- Add to /tests/users/test_analytics.py and /docs/api.md\n")

    # The route reader stops at the dot, so the file names come back without
    # their extensions; either way they are places on disk.
    assert found == ["/tests/users/test_analytics", "/docs/api"]
    assert all(_is_a_place_on_disk(path, roots) for path in found)


@pytest.mark.parametrize("disk_path", ["/tmp/kcadm-", "/dev/null", "/var/log/app", "/dev/tcp/keycloak/8080", "/opt/keycloak/bin/kcadm", "/tmp}/kc-readiness-XXXXXX"])
def test_an_operating_system_folder_is_a_place_on_disk(disk_path: str) -> None:
    # Found live on 15 October 2026: a Keycloak setup script in a task
    # document wrote its temporary files to /tmp/kcadm- and its noise to
    # /dev/null, and the card said the task "answers at /tmp/kcadm-".
    text = f"- The setup script writes to {disk_path} while it runs\n"

    assert all(
        _is_a_place_on_disk(route, DEFAULT_TEST_ROOTS) for route in _route_shaped(text)
    )


@pytest.mark.parametrize("route", ["/dev/seed", "/sys/user"])
def test_a_web_address_under_a_system_sounding_folder_is_still_named(
    route: str,
) -> None:
    # ``/dev`` and ``/sys`` are real places on disk, but applications also
    # answer at ``/dev/seed`` and ``/sys/user``. Only the well-known device
    # files are skipped inside ``/dev``, and ``/sys`` is not skipped at all.
    found = _route_shaped(f"- Add POST {route} for local development\n")

    assert found == [route]
    assert _is_a_place_on_disk(route, DEFAULT_TEST_ROOTS) is False


def test_an_unnamed_web_address_beside_a_disk_path_is_still_named() -> None:
    found = _route_shaped(
        "- The script logs to /dev/null and the service answers at /api/notes\n"
    )

    assert [
        route for route in found if not _is_a_place_on_disk(route, DEFAULT_TEST_ROOTS)
    ] == ["/api/notes"]


def test_a_closing_markup_tag_is_not_a_web_address() -> None:
    found = _route_shaped('<span className="date">{created}</span> <time>{t}</time> GET /api/notes')
    assert "/span" not in found and "/time" not in found
    assert "/api/notes" in found


def test_no_text_names_no_web_address() -> None:
    assert _route_shaped("") == []
    assert _route_shaped(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "capability"),
    [
        ("Endpoint requires authentication", "authentication"),
        ("Add an alembic revision for the new column", "a database migration"),
        ("Implement the analytics service layer", "a service layer or module"),
        ("Implement error handling for the endpoint", "error handling"),
        ("Add structured logs for every call", "logging"),
        ("GET /notes applies rate limiting to each caller", "rate limiting"),
    ],
)
def test_a_capability_is_named(words: str, capability: str) -> None:
    assert capability in _capabilities_named_in(words)


def test_metrics_is_deliberately_not_on_the_list() -> None:
    """A plan that calls everything "metrics" chose a naming family; it did not
    invent a capability. Flagging it would teach a person to ignore the card."""
    assert _capabilities_named_in("# Create metrics endpoint\n\nGET /users/created-per-day.\n") == []


def test_a_paraphrase_names_no_capability() -> None:
    assert _capabilities_named_in("Listing all the notes there are.") == []
