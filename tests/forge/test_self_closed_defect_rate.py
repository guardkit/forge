"""M5 — the number is pinned before there is a number to report.

Conductor rewire spec 2026-09-05, rule 6, and the tests rule 8 asks for.

What these pin, in the words of the thing they protect:

- **One green-and-merged repair out of two taken on reads 1 of 2.** A repair
  whose build stopped at a red gate is in the denominator and not in the
  numerator: it was taken on and it did not close itself.
- **The drive era is outside the window.** Everything filed before
  2026-09-05 is excluded, and the 71 mode-c builds from the drive era are
  not ``work_queue`` rows at all, so they cannot leak in either way.
- **The 4 August journey would not count even inside the window.** Its
  close-out reads ``clean-review-no-fixes`` and it has no merge report; a
  measure that counted it would mint a false one-of-one on day one. This is
  the single most load-bearing test in the file.
- **The line says so in words when there is nothing to say.** No repair rows
  yet is not a rate of zero.

Nothing here opens a socket and nothing reads the live database: every test
gets its own SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli.status import m5_line, status_cmd
from forge.lifecycle import migrations
from forge.lifecycle.metrics import (
    M5_SINCE,
    MERGED_AND_RUNNING,
    MERGE_READY_TARGET_IDENTIFIER,
    MERGE_REPORT_STAGE_LABEL,
    self_closed_defect_rate,
)
from forge.lifecycle.persistence import SqliteLifecyclePersistence

# The two days that matter: the cutoff, and the day of the one journey the
# conductor ever completed.
INSIDE = "2026-09-05T09:00:00+00:00"
DRIVE_ERA = "2026-08-04T10:24:30+00:00"


# ---------------------------------------------------------------------------
# A database of our own
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "forge.db"
    cx = sqlite_connect.connect_writer(path)
    migrations.apply_at_boot(cx)
    cx.close()
    return path


@pytest.fixture()
def cx(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite_connect.connect_writer(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Seeding: a repair row, its build, and the stage rows that say how it ended
# ---------------------------------------------------------------------------


def file_repair_row(
    cx: sqlite3.Connection,
    *,
    correlation_id: str,
    queued_at: str,
    kind: str = "fix",
    status: str = "ADMITTED",
    rank: float = 1.0,
) -> int:
    cursor = cx.execute(
        """
        INSERT INTO work_queue (
            sentence, target_repo, kind, status, rank, originating_user,
            correlation_id, queued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "The build of FEAT-44A8 in api_test failed: gates red.",
            "appmilla_github/api_test",
            kind,
            status,
            rank,
            "rich",
            correlation_id,
            queued_at,
        ),
    )
    cx.commit()
    return int(cursor.lastrowid)


def open_build(
    cx: sqlite3.Connection,
    *,
    build_id: str,
    correlation_id: str,
    feature_id: str = "FEAT-44A8",
    queued_at: str = INSIDE,
    mode: str = "mode-c",
) -> str:
    cx.execute(
        """
        INSERT INTO builds (
            build_id, feature_id, repo, branch, feature_yaml_path, status,
            triggered_by, correlation_id, queued_at, max_turns,
            sdk_timeout_seconds, mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            build_id,
            feature_id,
            "appmilla_github/api_test",
            "main",
            f".guardkit/features/{feature_id}.yaml",
            "COMPLETE",
            "forge-internal",
            correlation_id,
            queued_at,
            5,
            1800,
            mode,
        ),
    )
    cx.commit()
    return build_id


def record_stage(
    cx: sqlite3.Connection,
    *,
    build_id: str,
    stage_label: str,
    target_identifier: str,
    at: str,
    status: str = "PASSED",
    details: dict[str, Any] | None = None,
) -> None:
    cx.execute(
        """
        INSERT INTO stage_log (
            build_id, stage_label, target_kind, target_identifier, status,
            started_at, completed_at, duration_secs, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            build_id,
            stage_label,
            "local_tool",
            target_identifier,
            status,
            at,
            at,
            0.0,
            json.dumps(details or {}),
        ),
    )
    cx.commit()


def merge_ready_card(cx: sqlite3.Connection, build_id: str, *, at: str) -> None:
    """The gates were proven green and Rich was shown the merge-ready card."""
    record_stage(
        cx,
        build_id=build_id,
        stage_label="the merge-ready checkpoint",
        target_identifier=MERGE_READY_TARGET_IDENTIFIER,
        at=at,
        status="GATED",
        details={"merge_ready": {"gates": "green"}},
    )


def merge_report(
    cx: sqlite3.Connection,
    build_id: str,
    *,
    at: str,
    result: str = MERGED_AND_RUNNING,
) -> None:
    """The merge executor's outcome report — merged, deployed, still green."""
    record_stage(
        cx,
        build_id=build_id,
        stage_label=MERGE_REPORT_STAGE_LABEL,
        target_identifier="merge_deploy_executor",
        at=at,
        status="PASSED" if result == MERGED_AND_RUNNING else "FAILED",
        details={
            "result": result,
            "detail": "FEAT-44A8 merged and running — checks 12/12.",
        },
    )


def a_repair_that_closed_itself(
    cx: sqlite3.Connection,
    *,
    build_id: str,
    queued_at: str = INSIDE,
    rank: float = 1.0,
) -> int:
    """A whole green journey: the row, the build, the card, the report."""
    correlation_id = f"fix-{build_id}"
    queue_id = file_repair_row(
        cx, correlation_id=correlation_id, queued_at=queued_at, rank=rank
    )
    repair_build = f"{build_id}-repair"
    open_build(
        cx,
        build_id=repair_build,
        correlation_id=correlation_id,
        queued_at=queued_at,
    )
    merge_ready_card(cx, repair_build, at=f"{queued_at[:11]}10:00:00+00:00")
    merge_report(cx, repair_build, at=f"{queued_at[:11]}10:05:00+00:00")
    return queue_id


def a_repair_that_stopped_at_a_red_gate(
    cx: sqlite3.Connection, *, build_id: str, rank: float = 2.0
) -> int:
    """Taken on, admitted, and stopped before the merge-ready card."""
    correlation_id = f"fix-{build_id}"
    queue_id = file_repair_row(
        cx, correlation_id=correlation_id, queued_at=INSIDE, rank=rank
    )
    repair_build = f"{build_id}-repair"
    open_build(cx, build_id=repair_build, correlation_id=correlation_id)
    record_stage(
        cx,
        build_id=repair_build,
        stage_label="autobuild",
        target_identifier="autobuild_runner",
        at="2026-09-05T09:40:00+00:00",
        status="FAILED",
        details={"gate": "pytest red"},
    )
    return queue_id


# ---------------------------------------------------------------------------
# The rate
# ---------------------------------------------------------------------------


class TestTheRate:
    def test_one_green_one_red_and_one_from_the_drive_era_reads_one_of_two(
        self, cx: sqlite3.Connection
    ) -> None:
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")
        a_repair_that_stopped_at_a_red_gate(
            cx, build_id="build-FEAT-B0EF-20260905091500"
        )
        # A journey from the drive era that WOULD have counted on its shape.
        # It does not count, because of its date and nothing else.
        a_repair_that_closed_itself(
            cx,
            build_id="build-FEAT-TST1-20260804102430",
            queued_at=DRIVE_ERA,
            rank=3.0,
        )

        assert self_closed_defect_rate(cx) == (1, 2)

    def test_the_drive_era_mode_c_builds_are_not_queue_rows_at_all(
        self, cx: sqlite3.Connection
    ) -> None:
        """The 71 of them cannot leak into either half: they have no row."""
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")
        drive_era = open_build(
            cx,
            build_id="build-FEAT-TST1-20260804102430",
            correlation_id="corr-drive-era",
            feature_id="FEAT-TST1",
            queued_at=DRIVE_ERA,
        )
        merge_ready_card(cx, drive_era, at="2026-08-04T10:30:00+00:00")
        merge_report(cx, drive_era, at="2026-08-04T10:36:00+00:00")

        assert self_closed_defect_rate(cx) == (1, 1)

    def test_the_cutoff_is_the_fifth_of_september(self) -> None:
        assert M5_SINCE == "2026-09-05"

    def test_a_row_filed_a_second_before_the_cutoff_is_outside_the_window(
        self, cx: sqlite3.Connection
    ) -> None:
        a_repair_that_closed_itself(
            cx,
            build_id="build-FEAT-44A8-20260904235959",
            queued_at="2026-09-04T23:59:59+00:00",
        )
        assert self_closed_defect_rate(cx) == (0, 0)

    def test_a_withdrawn_repair_is_in_neither_half(
        self, cx: sqlite3.Connection
    ) -> None:
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")
        queue_id = a_repair_that_stopped_at_a_red_gate(
            cx, build_id="build-FEAT-B0EF-20260905091500"
        )
        cx.execute(
            "UPDATE work_queue SET status = 'WITHDRAWN' WHERE id = ?", (queue_id,)
        )
        cx.commit()

        assert self_closed_defect_rate(cx) == (1, 1)

    def test_a_feature_row_is_not_a_repair(self, cx: sqlite3.Connection) -> None:
        file_repair_row(
            cx,
            correlation_id="plan-abc",
            queued_at=INSIDE,
            kind="feature",
            status="DONE",
        )
        assert self_closed_defect_rate(cx) == (0, 0)

    def test_no_repair_rows_yet_is_not_a_rate_of_zero(
        self, cx: sqlite3.Connection
    ) -> None:
        assert self_closed_defect_rate(cx) == (0, 0)

    def test_it_reads_the_persistence_facade_as_well_as_a_connection(
        self, cx: sqlite3.Connection, db_path: Path
    ) -> None:
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")
        pool = SqliteLifecyclePersistence(connection=cx, db_path=db_path)
        assert self_closed_defect_rate(pool) == (1, 1)

    def test_anything_else_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError, match="sqlite connection"):
            self_closed_defect_rate(object())


# ---------------------------------------------------------------------------
# What does NOT count — the numerator's two halves, each on its own
# ---------------------------------------------------------------------------


class TestWhatDoesNotCount:
    def test_the_fourth_of_august_shape_would_not_count_inside_the_window(
        self, cx: sqlite3.Connection
    ) -> None:
        """The one completed journey ever fixed nothing.

        Its close-out reads ``clean-review-no-fixes`` and there is no merge
        report behind it. Dated forward into the window it is still not a
        self-closed defect — it is a repair the factory took on and did not
        close. One of one would have been a lie on day one.
        """
        correlation_id = "fix-build-FEAT-TST1-20260905110414"
        file_repair_row(cx, correlation_id=correlation_id, queued_at=INSIDE)
        build_id = open_build(
            cx,
            build_id="build-FEAT-TST1-20260905110414",
            correlation_id=correlation_id,
            feature_id="FEAT-TST1",
        )
        record_stage(
            cx,
            build_id=build_id,
            stage_label="conductor-close-out",
            target_identifier="conductor",
            at="2026-09-05T11:06:46+00:00",
            details={
                "outcome": "terminal",
                "rationale": "clean-review-no-fixes: mode-c-task-review-empty",
            },
        )

        assert self_closed_defect_rate(cx) == (0, 1)

    def test_a_merge_ready_card_with_no_report_behind_it_does_not_count(
        self, cx: sqlite3.Connection
    ) -> None:
        correlation_id = "fix-build-FEAT-44A8-20260905083000"
        file_repair_row(cx, correlation_id=correlation_id, queued_at=INSIDE)
        build_id = open_build(
            cx, build_id="build-repair-1", correlation_id=correlation_id
        )
        merge_ready_card(cx, build_id, at="2026-09-05T10:00:00+00:00")

        assert self_closed_defect_rate(cx) == (0, 1)

    def test_a_report_that_went_red_after_the_merge_does_not_count(
        self, cx: sqlite3.Connection
    ) -> None:
        correlation_id = "fix-build-FEAT-44A8-20260905083000"
        file_repair_row(cx, correlation_id=correlation_id, queued_at=INSIDE)
        build_id = open_build(
            cx, build_id="build-repair-1", correlation_id=correlation_id
        )
        merge_ready_card(cx, build_id, at="2026-09-05T10:00:00+00:00")
        merge_report(
            cx,
            build_id,
            at="2026-09-05T10:05:00+00:00",
            result="merged-deploy-reverted",
        )

        assert self_closed_defect_rate(cx) == (0, 1)

    def test_a_report_before_the_card_is_not_a_subsequent_report(
        self, cx: sqlite3.Connection
    ) -> None:
        """A report from an earlier attempt does not close a later gate."""
        correlation_id = "fix-build-FEAT-44A8-20260905083000"
        file_repair_row(cx, correlation_id=correlation_id, queued_at=INSIDE)
        build_id = open_build(
            cx, build_id="build-repair-1", correlation_id=correlation_id
        )
        merge_report(cx, build_id, at="2026-09-05T10:00:00+00:00")
        merge_ready_card(cx, build_id, at="2026-09-05T10:05:00+00:00")

        assert self_closed_defect_rate(cx) == (0, 1)

    def test_another_builds_green_ending_does_not_close_this_repair(
        self, cx: sqlite3.Connection
    ) -> None:
        """The correlation id is the spine; a neighbour's receipts are not."""
        file_repair_row(
            cx, correlation_id="fix-build-FEAT-44A8-20260905083000", queued_at=INSIDE
        )
        stranger = open_build(
            cx, build_id="build-stranger", correlation_id="corr-someone-else"
        )
        merge_ready_card(cx, stranger, at="2026-09-05T10:00:00+00:00")
        merge_report(cx, stranger, at="2026-09-05T10:05:00+00:00")

        assert self_closed_defect_rate(cx) == (0, 1)


# ---------------------------------------------------------------------------
# The line ``forge status --m5`` prints
# ---------------------------------------------------------------------------


class TestTheStatusLine:
    def test_it_reads_n_of_d_since_the_cutoff(self) -> None:
        assert (
            m5_line(1, 2, since=M5_SINCE)
            == "self-closed defects: 1 of 2 since 2026-09-05"
        )

    def test_with_nothing_taken_on_it_says_so_in_words(self) -> None:
        assert m5_line(0, 0, since=M5_SINCE) == "no repair rows yet since 2026-09-05"

    def test_the_command_prints_the_line(
        self, cx: sqlite3.Connection, db_path: Path
    ) -> None:
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")
        a_repair_that_stopped_at_a_red_gate(
            cx, build_id="build-FEAT-B0EF-20260905091500"
        )

        result = CliRunner().invoke(status_cmd, ["--m5", "--db-path", str(db_path)])

        assert result.exit_code == 0, result.output
        assert "self-closed defects: 1 of 2 since 2026-09-05" in result.output

    def test_the_command_says_nothing_yet_on_an_empty_queue(
        self, db_path: Path
    ) -> None:
        result = CliRunner().invoke(status_cmd, ["--m5", "--db-path", str(db_path)])

        assert result.exit_code == 0, result.output
        assert "no repair rows yet since 2026-09-05" in result.output

    def test_the_command_prints_no_table(
        self, cx: sqlite3.Connection, db_path: Path
    ) -> None:
        a_repair_that_closed_itself(cx, build_id="build-FEAT-44A8-20260905083000")

        result = CliRunner().invoke(status_cmd, ["--m5", "--db-path", str(db_path)])

        assert result.exit_code == 0, result.output
        assert "BUILD" not in result.output
        assert result.output.strip().count("\n") == 0

    def test_a_database_older_than_the_queue_is_answered_plainly(
        self, tmp_path: Path
    ) -> None:
        """A v9 backup has no work queue; that is not an error to shout at."""
        path = tmp_path / "old.db"
        original = migrations._MIGRATIONS
        migrations._MIGRATIONS = tuple(m for m in original if m[0] <= 9)
        try:
            connection = sqlite_connect.connect_writer(path)
            migrations.apply_at_boot(connection)
            connection.close()
        finally:
            migrations._MIGRATIONS = original

        result = CliRunner().invoke(status_cmd, ["--m5", "--db-path", str(path)])

        assert result.exit_code == 0, result.output
        assert "no work queue yet" in result.output
