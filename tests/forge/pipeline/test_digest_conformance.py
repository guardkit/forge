"""Digest conformance v1 — pinned on the case that motivated it.

api_test FEAT-EF8D: the approved digest declared GET /users/created-per-day
and promised "exactly seven ... never more than seven", but the built router
served a 30-day query at that path and put the 7-day logic at an unrequested
/users/daily-counts. Every test was green (the ``== 7`` assertions all lived
in files that only mention the OTHER path); a human diff-read caught it. The
fixtures here replicate that shape, near-misses included (``["date"][7] ==``
and ``assert "7 day" in docs`` must NOT count as asserting the number 7).
"""

from __future__ import annotations

from pathlib import Path

from forge.pipeline.digest_conformance import (
    check_digest_conformance,
    find_digest_for_feature,
    run_digest_conformance,
)

# ---------------------------------------------------------------------------
# The FEAT-EF8D fixture (digest verbatim from the api_test branch)
# ---------------------------------------------------------------------------

EF8D_DIGEST = """\
feature: created-per-day
generated: '2026-07-09T14:32:00Z'
endpoint:
  method: GET
  path: /users/created-per-day
scenarios:
- title: A GET request to the daily counts endpoint returns the last 7 days of data
  tags:
  - '@key-example'
  - '@smoke'
  sentence: A request to the daily counts endpoint returns exactly seven days of data, each with a date and a count, ordered from oldest to newest.
- title: The endpoint returns exactly 7 days of data
  tags:
  - '@boundary'
  sentence: The response contains exactly seven entries, one for each of the last seven days.
- title: The endpoint does not return more than 7 days of data
  tags:
  - '@boundary'
  - '@negative'
  sentence: The response never includes more than seven entries.
- title: A POST request to the daily counts endpoint is rejected
  tags:
  - '@negative'
  sentence: The endpoint only accepts GET requests and rejects any POST requests.
- title: A GET request to an invalid path returns a not found response
  tags:
  - '@negative'
  sentence: The endpoint only responds to the correct path and rejects any other path.
- title: The endpoint includes days with zero creations in the 7-day window
  tags:
  - '@edge-case'
  sentence: Even on days when no users were created, the response still includes an entry for that day with a count of zero.
- title: The oldest day in the response is exactly 7 days ago
  tags:
  - '@edge-case'
  sentence: The oldest entry in the response is dated exactly seven days ago.
- title: The newest day in the response is today
  tags:
  - '@edge-case'
  sentence: The newest entry in the response is dated today.
"""

ROUTER_SRC = '''"""Users API router (trimmed to the routes that matter here)."""

from fastapi import APIRouter, Depends

router = APIRouter(prefix="/users", redirect_slashes=False)


@router.get(
    "/created-per-day",
    response_model=list,
    tags=["users"],
    summary="Get users created per day",
)
async def get_users_created_per_day(db=Depends(object)):
    """Get users created per day for the last 30 days."""
    return []


@router.get(
    "/daily-counts",
    response_model=list,
    tags=["users"],
    summary="Get daily user creation counts for the last 7 days",
)
async def get_daily_counts(db=Depends(object)):
    """Get daily user creation counts for the last 7 days."""
    return []
'''

# References /users/created-per-day but never compares against 7 — the
# ``[7] ==`` line is a bracket index, not an assertion of the number.
TESTS_CREATED_PER_DAY_SRC = '''"""Tests for the /users/created-per-day endpoint and service."""


async def test_endpoint_includes_date_and_count(async_client):
    response = await async_client.get("/users/created-per-day")
    data = response.json()
    assert len(data) == 1
    assert "date" in data[0]


async def test_date_format_is_iso(async_client):
    response = await async_client.get("/users/created-per-day")
    result = response.json()
    assert result[0]["date"][7] == "-"
'''

# References the path AND contains the digit 7 inside string literals — but
# never next to a comparison sign, so it must not satisfy the promise.
TESTS_DOCS_SRC = '''"""The GET /users/created-per-day endpoint is documented."""


def test_docs_mention_the_window(api_docs_content):
    assert "GET /users/created-per-day" in api_docs_content
    assert "7 day" in api_docs_content.lower() or "last 7" in api_docs_content.lower()
'''

# The real seven-assertions — pointed at the OTHER path.
TESTS_ACCEPTANCE_SRC = '''"""Acceptance tests for the GET /users/daily-counts endpoint."""


async def test_returns_exactly_seven_days(async_client):
    response = await async_client.get("/users/daily-counts")
    data = response.json()
    assert len(data) == 7


async def test_never_more_than_seven(async_client):
    response = await async_client.get("/users/daily-counts")
    assert len(response.json()) <= 7
'''


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_created_per_day_case(
    root: Path, *, feature_id: str = "FEAT-EF8D", conforming: bool
) -> None:
    """Build the FEAT-EF8D tree shape.

    ``conforming=False`` reproduces the caught case: the digest declares
    /users/created-per-day, the 7-assertions live at /users/daily-counts.
    ``conforming=True`` is what an honest build would have produced: the
    digest declares /users/daily-counts, where the 7-assertions really are.
    """
    slug = "daily-counts" if conforming else "created-per-day"
    declared = "/users/daily-counts" if conforming else "/users/created-per-day"
    _write(
        root,
        f".guardkit/features/{feature_id}.yaml",
        f'id: {feature_id}\nname: "User Creation Analytics - Daily Counts"\n'
        f"feature_files:\n  - features/{slug}/{slug}.feature\n",
    )
    _write(root, f"features/{slug}/{slug}.feature", "Feature: daily counts\n")
    _write(
        root,
        f"features/{slug}/{slug}_digest.yaml",
        EF8D_DIGEST.replace("/users/created-per-day", declared),
    )
    _write(root, "src/users/router.py", ROUTER_SRC)
    _write(root, "tests/users/test_created_per_day.py", TESTS_CREATED_PER_DAY_SRC)
    _write(root, "tests/test_analytics_documentation.py", TESTS_DOCS_SRC)
    _write(
        root,
        "tests/acceptance/test_daily_counts_acceptance.py",
        TESTS_ACCEPTANCE_SRC,
    )


# ---------------------------------------------------------------------------
# The motivating case
# ---------------------------------------------------------------------------


class TestMotivatingCase:
    def test_flags_the_untested_seven_promise(self, tmp_path: Path) -> None:
        write_created_per_day_case(tmp_path, conforming=False)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-EF8D"
        )
        assert report["conformant"] is False
        promises = [
            c
            for c in report["checks"]
            if c["check"] == "number-promise-is-tested"
        ]
        assert {c["subject"] for c in promises} == {
            "exactly 7",
            "no more than 7",
        }
        assert all(c["verdict"] == "fail" for c in promises)
        # The evidence names the files that mention the path but never
        # assert the number.
        assert "created_per_day" in promises[0]["evidence"]

    def test_endpoint_itself_is_found(self, tmp_path: Path) -> None:
        # The route EXISTS at the declared path — the defect is the untested
        # promise, and the check must say exactly that.
        write_created_per_day_case(tmp_path, conforming=False)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-EF8D"
        )
        endpoint_checks = [
            c for c in report["checks"] if c["check"] == "endpoint-exists"
        ]
        assert len(endpoint_checks) == 1
        assert endpoint_checks[0]["verdict"] == "pass"
        assert "src/users/router.py" in endpoint_checks[0]["evidence"]

    def test_warning_is_plain_english_and_names_the_number(
        self, tmp_path: Path
    ) -> None:
        write_created_per_day_case(tmp_path, conforming=False)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-EF8D"
        )
        warning = report["warning"]
        assert warning is not None
        assert "/users/created-per-day" in warning
        assert "7" in warning
        assert "did not block the merge" in warning

    def test_conforming_build_passes_clean(self, tmp_path: Path) -> None:
        write_created_per_day_case(tmp_path, conforming=True)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-EF8D"
        )
        assert report["conformant"] is True
        assert report["warning"] is None
        assert all(c["verdict"] == "pass" for c in report["checks"])


# ---------------------------------------------------------------------------
# The individual checks
# ---------------------------------------------------------------------------


class TestChecks:
    def test_missing_digest_is_skipped_not_failed(self, tmp_path: Path) -> None:
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-NONE"
        )
        assert report["conformant"] is None
        assert report["warning"] is None
        assert "skipped" in report
        assert find_digest_for_feature(tmp_path, "FEAT-NONE") is None

    def test_missing_route_fails_the_endpoint_check(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            ".guardkit/features/FEAT-GONE.yaml",
            "id: FEAT-GONE\nfeature_files:\n  - features/gone/gone.feature\n",
        )
        _write(
            tmp_path,
            "features/gone/gone_digest.yaml",
            "feature: gone\nendpoint:\n  method: GET\n  path: /users/nope\n"
            "scenarios:\n- title: The endpoint responds\n"
            "  sentence: The endpoint responds.\n",
        )
        _write(tmp_path, "src/users/router.py", ROUTER_SRC)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-GONE"
        )
        endpoint_check = [
            c for c in report["checks"] if c["check"] == "endpoint-exists"
        ][0]
        assert endpoint_check["verdict"] == "fail"
        assert report["conformant"] is False
        assert "no route" in report["warning"]

    def test_hurl_twin_satisfies_a_number_promise(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".guardkit/features/FEAT-HURL.yaml",
            "id: FEAT-HURL\nfeature_files:\n  - features/time/time.feature\n",
        )
        _write(
            tmp_path,
            "features/time/time_digest.yaml",
            "feature: time\nendpoint:\n  method: GET\n  path: /time\n"
            "scenarios:\n- title: The clock returns three readings\n"
            "  sentence: The response contains exactly three readings.\n",
        )
        _write(
            tmp_path,
            "src/app.py",
            '@app.get("/time")\nasync def get_time():\n    return []\n',
        )
        _write(
            tmp_path,
            "qa/twins/time/readings.hurl",
            'GET http://localhost:8901/time\nHTTP 200\n[Asserts]\n'
            'jsonpath "$.readings" count == 3\n',
        )
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-HURL"
        )
        assert report["conformant"] is True
        promise = [
            c
            for c in report["checks"]
            if c["check"] == "number-promise-is-tested"
        ][0]
        assert promise["subject"] == "exactly 3"
        assert "readings.hurl" in promise["evidence"]

    def test_number_words_become_digits(self, tmp_path: Path) -> None:
        digest = {
            "endpoint": {"method": "GET", "path": "/things"},
            "scenarios": [
                {
                    "title": "Limits hold",
                    "sentence": (
                        "The list never contains more than twelve items and "
                        "always has at least three entries."
                    ),
                }
            ],
        }
        report = check_digest_conformance(digest, tmp_path)
        subjects = {
            c["subject"]
            for c in report["checks"]
            if c["check"] == "number-promise-is-tested"
        }
        assert subjects == {"no more than 12", "at least 3"}

    def test_feature_with_no_tests_at_all_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            ".guardkit/features/FEAT-BARE.yaml",
            "id: FEAT-BARE\nfeature_files:\n  - features/bare/bare.feature\n",
        )
        _write(
            tmp_path,
            "features/bare/bare_digest.yaml",
            "feature: bare\nendpoint:\n  method: GET\n  path: /bare\n"
            "scenarios:\n- title: The endpoint returns exactly two rows\n"
            "  sentence: The response contains exactly two rows.\n",
        )
        _write(
            tmp_path,
            "src/app.py",
            '@app.get("/bare")\nasync def get_bare():\n    return []\n',
        )
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-BARE"
        )
        assert report["conformant"] is False
        scenario_check = [
            c for c in report["checks"] if c["check"] == "scenario-has-a-test"
        ][0]
        assert scenario_check["verdict"] == "fail"
        promise_check = [
            c
            for c in report["checks"]
            if c["check"] == "number-promise-is-tested"
        ][0]
        assert promise_check["verdict"] == "fail"
        assert "no test or twin mentions" in promise_check["evidence"]

    def test_receipt_names_its_own_blind_spots(self, tmp_path: Path) -> None:
        write_created_per_day_case(tmp_path, conforming=False)
        report = run_digest_conformance(
            repo_root=tmp_path, feature_id="FEAT-EF8D"
        )
        assert report["blind_spots"]
        joined = " ".join(report["blind_spots"])
        assert "does not prove" in joined
