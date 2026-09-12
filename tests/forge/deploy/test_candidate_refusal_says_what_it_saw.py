"""A refusal that costs a merge says what the gate actually saw (2026-09-12).

The observed failure: Rich gave his merge word, the candidate was built and
checked, the check went red, and everything the estate said about why was the
NAME of one check — "failed 1 of 9 checks (created-per-day)". Not which
assertion inside it, not what was expected, not what was seen. By the time
anyone read the refusal the candidate had been torn down and its evidence had
gone with it, so nothing could be diagnosed at all.

The per-check results were already there and already thrown away. These tests
drive the real deploy stage — the real runner, the real runbook executor, a
real SQLite runbook repository, with the live-gate invoker as the one fake at
the seam the stage already has — and assert the words that reach each surface:

* the operator's line (the ``DeployFailed`` failure reason, which is also what
  the stage logs) names every failing assertion and what it expected and saw;
* the summary that becomes the merge report keeps the whole list;
* a gate that reported no assertion detail is said to have reported none;
* the cap fires and says how many were left out;
* a PASSING check's summary is exactly what it always was.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from forge.config.models import DeployStageConfig
from forge.deploy.live_gate import (
    DryRunBrokerInspector,
    LiveGateInvocation,
)
from forge.deploy.profile import parse_deploy_profile
from forge.deploy.reservation import InProcessReservationLease
from forge.deploy.stage import (
    MAX_FAILED_ASSERTIONS_IN_THE_LOG,
    MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT,
    DeployStageRunner,
    assertion_in_words,
    failed_assertions,
    gate_summary,
    refusal_assertion_clause,
)
from forge.persistence.repositories.runbook import RunbookRepository

FIXED = datetime(2026, 9, 12, 14, 22, 7, tzinfo=UTC)
CANDIDATE_TREE = "/home/x/api_test/.forge-candidates/FEAT-7A25"

#: The nine checks of the observed run, with the one that went red.
NINE_GATES = (
    "health",
    "created-per-day",
    "users_count",
    "etag",
    "a",
    "b",
    "c",
    "d",
    "e",
)


def _assertion(
    gate_id: str,
    status: str,
    *,
    assertion_id: str | None = None,
    expected: str | None = None,
    observed: str | None = None,
) -> dict[str, Any]:
    """One assertion result in the shape the live-gate backend reports."""
    entry: dict[str, Any] = {
        "id": assertion_id or f"{gate_id}::status",
        "gate_id": gate_id,
        "status": status,
    }
    if expected is not None:
        entry["expected"] = expected
    if observed is not None:
        entry["observed"] = observed
    return entry


#: What the gate reported on the day: one red check, two failing assertions
#: inside it, each with what it expected and what it saw.
RED_WITH_DETAIL: tuple[dict[str, Any], ...] = tuple(
    [
        _assertion(g, "pass")
        for g in NINE_GATES
        if g != "created-per-day"
    ]
    + [
        _assertion(
            "created-per-day",
            "fail",
            assertion_id="status-is-200",
            expected="200",
            observed="500",
        ),
        _assertion(
            "created-per-day",
            "fail",
            assertion_id="seven-days-returned",
            expected="7 entries",
            observed="0 entries",
        ),
    ]
)

#: The same red check, reported with no expected or observed value at all —
#: the shape that left the orchestrator with nothing to act on.
RED_WITHOUT_DETAIL: tuple[dict[str, Any], ...] = tuple(
    _assertion(g, "fail" if g == "created-per-day" else "pass") for g in NINE_GATES
)

ALL_GREEN: tuple[dict[str, Any], ...] = tuple(
    _assertion(g, "pass") for g in NINE_GATES
)


class _Invoker:
    """The live gate, fixed: one verdict and one set of per-check results.

    It answers the candidate leg through the two seams the stage uses to move
    it into the candidate's tree and overlay the candidate's environment.
    """

    def __init__(
        self, verdict: str, assertions: tuple[dict[str, Any], ...]
    ) -> None:
        self._verdict = verdict
        self._assertions = assertions

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        return LiveGateInvocation(
            verdict=self._verdict,
            run_id=f"run-{feature}",
            gate_ids=NINE_GATES,
            assertions=self._assertions,
            evidence_index_ref="ev/idx.json",
            dry_run=False,
        )

    def with_repo_path(self, repo_path: Any) -> "_Invoker":
        return self

    def with_extra_env(self, overlay: dict[str, str]) -> "_Invoker":
        return self


@pytest.fixture
def repository(tmp_path: Path) -> RunbookRepository:
    from forge.persistence.migrations.runbook import apply

    conn = sqlite3.connect(str(tmp_path / "deploy.db"))
    apply(conn)
    return RunbookRepository(connection=conn)


@pytest.fixture
def runbook_publisher() -> AsyncMock:
    pub = AsyncMock()
    pub.publish_runbook_started = AsyncMock()
    pub.publish_step_started = AsyncMock()
    pub.publish_step_result = AsyncMock()
    pub.publish_runbook_complete = AsyncMock()
    pub.publish_escalated = AsyncMock()
    return pub


class _RecordingDeployPublisher:
    def __init__(self) -> None:
        self.failed: list[Any] = []

    async def _ignore(self, payload: Any) -> None:  # pragma: no cover - unused
        return None

    publish_deploy_queued = _ignore
    publish_deploy_started = _ignore
    publish_deploy_complete = _ignore
    publish_deploy_reverted = _ignore
    publish_qa_verdict = _ignore
    publish_live_gate_result = _ignore

    async def publish_deploy_failed(self, payload: Any) -> None:
        self.failed.append(payload)


def _profile():
    return parse_deploy_profile(
        {
            "env_id": "local",
            "compose": {
                "file": "docker-compose.yml",
                "script": "deploy/sandbox-deploy.sh",
            },
            "health_checks": [{"cmd": "deploy/healthcheck.sh"}],
            "cwd": "/home/x/api_test",
            "candidate": {
                "env": {
                    "CANDIDATE_PORT": "8902",
                    "API_TEST_BASE_URL": "http://localhost:8902",
                },
            },
        }
    )


def _runner(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    deploy_publisher: Any,
    tmp_path: Path,
    *,
    invoker: Any,
) -> DeployStageRunner:
    return DeployStageRunner(
        repository=repository,
        runbook_publisher=runbook_publisher,
        deploy_publisher=deploy_publisher,
        reservation=InProcessReservationLease(),
        live_gate_invoker=invoker,
        broker_inspector=DryRunBrokerInspector(),
        config=DeployStageConfig(),
        deploy_record_root=str(tmp_path / "state"),
        dry_run=True,
        clock=lambda: FIXED,
    )


async def _check(
    repository: RunbookRepository,
    runbook_publisher: AsyncMock,
    tmp_path: Path,
    *,
    verdict: str,
    assertions: tuple[dict[str, Any], ...],
) -> tuple[Any, _RecordingDeployPublisher]:
    publisher = _RecordingDeployPublisher()
    runner = _runner(
        repository,
        runbook_publisher,
        publisher,
        tmp_path,
        invoker=_Invoker(verdict, assertions),
    )
    result = await runner.candidate_check(
        _profile(),
        correlation_id="c1",
        deploy_run_id="run-1",
        feature="FEAT-7A25",
        feat_id="FEAT-7A25",
        candidate_cwd=CANDIDATE_TREE,
    )
    return result, publisher


# ---------------------------------------------------------------------------
# The words themselves
# ---------------------------------------------------------------------------


class TestOneAssertionInWords:
    def test_it_names_the_check_the_assertion_and_both_values(self) -> None:
        assert assertion_in_words(
            _assertion(
                "created-per-day",
                "fail",
                assertion_id="status-is-200",
                expected="200",
                observed="500",
            )
        ) == "created-per-day (status-is-200): expected 200, saw 500"

    def test_a_missing_value_is_said_to_be_missing_never_invented(self) -> None:
        only_expected = assertion_in_words(
            _assertion("etag", "fail", expected="an ETag header")
        )
        assert only_expected == (
            "etag (etag::status): expected an ETag header; the gate did not "
            "say what it saw"
        )
        only_observed = assertion_in_words(
            _assertion("etag", "fail", observed="no ETag header")
        )
        assert "the gate did not say what it expected" in only_observed
        neither = assertion_in_words(_assertion("etag", "fail"))
        assert neither == (
            "etag (etag::status): the gate did not say what it expected or "
            "what it saw"
        )

    def test_a_long_value_is_trimmed_to_the_cap(self) -> None:
        words = assertion_in_words(
            _assertion("body", "fail", observed="x" * 900), value_cap=40
        )
        assert "x" * 39 + "…" in words
        assert len(words) < 120

    def test_an_assertion_with_no_status_counts_as_failed(self) -> None:
        """A check that says nothing about itself is never read as green."""
        assert failed_assertions([{"id": "a", "gate_id": "g"}]) == [
            {"id": "a", "gate_id": "g"}
        ]
        assert failed_assertions([_assertion("g", "pass")]) == []


# ---------------------------------------------------------------------------
# The summary that becomes the merge report
# ---------------------------------------------------------------------------


class TestTheSummaryKeepsTheWholeList:
    def test_a_red_check_carries_every_failing_assertion(self) -> None:
        summary = gate_summary(
            verdict="fail", gate_ids=NINE_GATES, assertions=RED_WITH_DETAIL
        )
        assert summary["failed_checks"] == ["created-per-day"]
        assert summary["checks_passed"] == 8 and summary["checks_total"] == 9
        assert summary["assertion_detail_reported"] is True
        assert summary["failed_assertions_left_out"] == 0
        assert [a["id"] for a in summary["failed_assertions"]] == [
            "status-is-200",
            "seven-days-returned",
        ]
        # The gate's own words, unchanged and un-renamed.
        assert summary["failed_assertions"][0] == {
            "id": "status-is-200",
            "gate_id": "created-per-day",
            "status": "fail",
            "expected": "200",
            "observed": "500",
        }

    def test_a_gate_that_reported_no_detail_says_so(self) -> None:
        summary = gate_summary(verdict="fail", gate_ids=NINE_GATES, assertions=())
        assert summary["failed_assertions"] == []
        assert summary["assertion_detail_reported"] is False

    def test_the_receipt_cap_fires_and_says_how_many_were_left_out(self) -> None:
        many = tuple(
            _assertion("created-per-day", "fail", assertion_id=f"a{i}", observed=str(i))
            for i in range(MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT + 7)
        )
        summary = gate_summary(
            verdict="fail", gate_ids=("created-per-day",), assertions=many
        )
        assert len(summary["failed_assertions"]) == MAX_FAILED_ASSERTIONS_ON_THE_RECEIPT
        assert summary["failed_assertions_left_out"] == 7

    def test_a_passing_check_summary_is_exactly_what_it_always_was(self) -> None:
        summary = gate_summary(
            verdict="pass",
            gate_ids=NINE_GATES,
            assertions=ALL_GREEN,
            live_gate_runbook_id="live-gate-cand-run-1",
        )
        assert summary == {
            "verdict": "pass",
            "checks_total": 9,
            "checks_passed": 9,
            "failed_checks": [],
            "gate_ids": list(NINE_GATES),
            "live_gate_runbook_id": "live-gate-cand-run-1",
        }


class TestTheOperatorsClause:
    def test_the_log_cap_fires_and_says_how_many_were_left_out(self) -> None:
        many = tuple(
            _assertion(
                "created-per-day",
                "fail",
                assertion_id=f"a{i}",
                expected="0",
                observed=str(i),
            )
            for i in range(MAX_FAILED_ASSERTIONS_IN_THE_LOG + 3)
        )
        clause = refusal_assertion_clause(
            gate_summary(verdict="fail", gate_ids=("created-per-day",), assertions=many)
        )
        assert clause.count("created-per-day") == MAX_FAILED_ASSERTIONS_IN_THE_LOG
        assert "and 3 more failing assertions not named here" in clause
        assert "the merge report has them all" in clause

    def test_one_left_out_is_said_in_the_singular(self) -> None:
        many = tuple(
            _assertion("g", "fail", assertion_id=f"a{i}")
            for i in range(MAX_FAILED_ASSERTIONS_IN_THE_LOG + 1)
        )
        clause = refusal_assertion_clause(
            gate_summary(verdict="fail", gate_ids=("g",), assertions=many)
        )
        assert "and 1 more failing assertion not named here" in clause

    def test_no_assertion_block_means_no_clause_at_all(self) -> None:
        """Every refusal that is not a gate verdict reads as it always did."""
        assert refusal_assertion_clause(None) == ""
        assert refusal_assertion_clause({"verdict": None}) == ""
        assert (
            refusal_assertion_clause(
                gate_summary(verdict="pass", gate_ids=NINE_GATES, assertions=ALL_GREEN)
            )
            == ""
        )


# ---------------------------------------------------------------------------
# The stage, driven for real
# ---------------------------------------------------------------------------


class TestTheStageSaysWhatItSaw:
    @pytest.mark.asyncio
    async def test_the_refusal_names_every_failing_assertion_and_what_it_saw(
        self, repository, runbook_publisher, tmp_path, caplog
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="forge.deploy.stage"):
            result, publisher = await _check(
                repository,
                runbook_publisher,
                tmp_path,
                verdict="fail",
                assertions=RED_WITH_DETAIL,
            )

        assert result.outcome == "failed"
        assert result.failed_step == "candidate_gate"
        reason = publisher.failed[-1].failure_reason
        # What it always said, unchanged.
        assert "candidate leg failed at 'candidate_gate'" in reason
        assert "candidate live-gate verdict 'fail' != 'pass'" in reason
        assert "was never touched (no promote, no revert)" in reason
        # ...and now, what it saw: both failing assertions, both values.
        assert "what the gate saw:" in reason
        assert (
            "created-per-day (status-is-200): expected 200, saw 500" in reason
        )
        assert (
            "created-per-day (seven-days-returned): expected 7 entries, "
            "saw 0 entries" in reason
        )
        # The operator reads the same line in the log.
        logged = [r.getMessage() for r in caplog.records]
        assert any("created-per-day (status-is-200)" in line for line in logged)

        # And the whole list rides the summary the merge report is built from.
        summary = result.detail["gate_summary"]
        assert summary["failed_checks"] == ["created-per-day"]
        assert [a["id"] for a in summary["failed_assertions"]] == [
            "status-is-200",
            "seven-days-returned",
        ]
        assert summary["assertion_detail_reported"] is True

    @pytest.mark.asyncio
    async def test_a_gate_that_reported_no_detail_is_said_to_have_reported_none(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        result, publisher = await _check(
            repository,
            runbook_publisher,
            tmp_path,
            verdict="fail",
            assertions=RED_WITHOUT_DETAIL,
        )

        reason = publisher.failed[-1].failure_reason
        assert (
            "created-per-day (created-per-day::status): the gate did not say "
            "what it expected or what it saw" in reason
        )
        summary = result.detail["gate_summary"]
        assert summary["assertion_detail_reported"] is True
        assert summary["failed_assertions"] == [
            {
                "id": "created-per-day::status",
                "gate_id": "created-per-day",
                "status": "fail",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_gate_that_reported_nothing_at_all_says_that_plainly(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        result, publisher = await _check(
            repository,
            runbook_publisher,
            tmp_path,
            verdict="fail",
            assertions=(),
        )

        reason = publisher.failed[-1].failure_reason
        assert (
            "the gate reported no assertion detail, so it did not say which "
            "assertion failed or what it saw" in reason
        )
        summary = result.detail["gate_summary"]
        assert summary["assertion_detail_reported"] is False
        assert summary["failed_assertions"] == []

    @pytest.mark.asyncio
    async def test_a_passing_check_is_untouched(
        self, repository, runbook_publisher, tmp_path
    ) -> None:
        result, publisher = await _check(
            repository,
            runbook_publisher,
            tmp_path,
            verdict="pass",
            assertions=ALL_GREEN,
        )

        assert result.outcome == "complete"
        assert publisher.failed == []
        assert result.detail["candidate"] == "standing"
        summary = dict(result.detail["gate_summary"])
        # The candidate leg's own two keys, then exactly the old summary.
        summary.pop("candidate_cwd")
        summary.pop("evidence_index_ref")
        assert summary == {
            "verdict": "pass",
            "checks_total": 9,
            "checks_passed": 9,
            "failed_checks": [],
            "gate_ids": list(NINE_GATES),
            "live_gate_runbook_id": "live-gate-cand-run-1",
        }
