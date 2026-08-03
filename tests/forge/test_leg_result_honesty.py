"""LEG-RESULT HONESTY — the first production fix journey, reproduced.

The live defect (build ``build-FEAT-TST1-20260803200757``, 2026-08-03). The
dispatched review leg::

    guardkit task-review ... --model openai:qwen36-workhorse

exited ``rc=2`` in about a second with a loud refusal banner and **no
pipeline-markers block at all**. Three things then went wrong, each of
which this module reproduces before it is fixed:

1. **The refusal was laundered into a review.** The ``stage_log`` row was
   written from a leg that never ran, its stray artefact was read as a fix
   task, and the journey's own log said ``0 fix task(s): none (clean
   review)``. Nothing in the chain distinguished "the reviewer looked and
   found nothing" from "the reviewer never spoke".
2. **The build row never moved.** The journey reached a terminal, the
   conductor logged "closed out" — and ``builds.status`` stayed ``RUNNING``
   with an empty ``error`` column, forever.
3. **The receipts went nowhere durable.** They were exported under a
   home-derived path that is unbound (ephemeral) inside the production
   container, rather than the estate's configured receipts root.

The rig is the sibling of ``test_fix_journey_total_failure_drive``: the
real turn loop, the real Supervisor, the real Mode C planner, the real
``stage_log`` projection, the real terminal handler, a real SQLite
database — fakes at exactly one edge (the GuardKit subprocess). Nothing
here touches a broker, a model server or the live estate.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.guardkit.models import GuardKitResult
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_conductor import (
    build_conductor_driver_deps_factory,
    build_conductor_supervisor_factory,
)
from forge.config.models import ForgeConfig
from forge.lifecycle import migrations
from forge.lifecycle.persistence import SqliteLifecyclePersistence
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.conductor_driver import drive_fix_journey
from forge.pipeline.forward_context_builder import ForwardContextBuilder
from forge.pipeline.terminal_handlers.mode_c import (
    RATIONALE_FAILED_REVIEW_LEG,
)

BUILD_ID = "build-FEAT-TST1-20260803200757"
SOURCE_BUILD_ID = "build-FEAT-TST1-20260803"
TASK_ID = "TASK-TST1"

#: The banner the live leg printed, verbatim in shape: a Phase-0 refusal,
#: on the way out, with no ``## Detection Findings`` block anywhere near it.
REFUSAL_BANNER = (
    "REFUSED (Phase 0, ad-hoc task creation): the review leg is id-form "
    "only and no task file exists for TASK-TST1FIX1"
)


def _bank_a_failure_pack(receipts_root: Path) -> None:
    pack = receipts_root / SOURCE_BUILD_ID
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "failure-manifest.json").write_text(
        json.dumps(
            {
                "build_id": SOURCE_BUILD_ID,
                "feature_id": "FEAT-TST1",
                "correlation_id": "corr-tst1-1",
                "reason": "gates red: the runtime smoke never ran",
                "branch": "feat/FEAT-TST1",
                "failed_at": "2026-08-03T19:04:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class FakeReviewLeg:
    """One review leg, three shapes — the two lies and the honest one.

    ``shape``:

    * ``"refused"`` — the live defect: ``rc=2``, the banner on stderr, no
      findings block. It DID leave one stray task artefact behind, which
      is the hazard: a leg that died after touching the tasks directory
      must not have its debris read as the review's fix-task list.
    * ``"silent"`` — the quieter twin: ``rc=0`` and no findings block at
      all. Indistinguishable from a clean review to anything that reads
      only the exit code.
    * ``"clean"`` — a genuine clean review: ``rc=0`` AND a readable
      findings block reporting zero findings. This one must keep working
      exactly as it always has.
    """

    def __init__(self, worktree: Path, *, shape: str) -> None:
        self.worktree = worktree
        self.shape = shape
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> GuardKitResult:
        self.calls.append(kwargs)
        subcommand = kwargs["subcommand"]
        if self.shape == "refused":
            return GuardKitResult(
                status="failed",
                subcommand=subcommand,
                exit_code=2,
                stdout_tail="",
                stderr=REFUSAL_BANNER,
                duration_secs=1.0,
                artefacts=[str(self.worktree / "tasks" / "TASK-TST1FIX1.yaml")],
                detection_findings=None,
                warnings=[],
            )
        if self.shape == "silent":
            return GuardKitResult(
                status="success",
                subcommand=subcommand,
                exit_code=0,
                stdout_tail="(the leg printed nothing a marker parser could read)",
                stderr="",
                duration_secs=1.0,
                artefacts=[],
                detection_findings=None,
                warnings=[],
            )
        return GuardKitResult(
            status="success",
            subcommand=subcommand,
            exit_code=0,
            stdout_tail="## Detection Findings\n```json\n[]\n```\n",
            stderr="",
            duration_secs=1.0,
            artefacts=[],
            detection_findings=[],
            warnings=[],
        )

    def subcommands(self) -> list[str]:
        return [c["subcommand"] for c in self.calls]


def _config() -> ForgeConfig:
    return ForgeConfig.model_validate(
        {
            "pipeline": {
                "build_queue_subject": "pipeline.build-queued.team-a",
                "approved_originators": ["terminal"],
            },
            "permissions": {"filesystem": {"allowlist": ["/"]}},
            "conductor": {"enabled": True, "seat": "qwen3-coder-30b"},
        }
    )


@pytest.fixture
def rig(tmp_path: Path):
    """The journey's real machinery over a real SQLite file.

    No git repository: none of the three shapes reaches the commit probe
    (two stop on the leg, the third on the initial-clean-review branch),
    and scenery in a fixture reads as a claim the drive does not make.
    """
    worktree = tmp_path / "worktree"
    (worktree / "tasks").mkdir(parents=True)
    receipts_root = tmp_path / "receipts"
    _bank_a_failure_pack(receipts_root)

    cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
    migrations.apply_at_boot(cx)
    cx.execute(
        "INSERT INTO builds (build_id, feature_id, repo, branch, "
        "feature_yaml_path, status, triggered_by, correlation_id, queued_at, "
        "started_at, worktree_path, mode, task_id) VALUES (?, 'FEAT-TST1', "
        "'r', 'fix/FEAT-TST1', ?, 'RUNNING', 'cli', 'corr-tst1-2', "
        "'2026-08-03T20:07:57Z', '2026-08-03T20:07:57Z', ?, 'mode-c', ?)",
        (
            BUILD_ID,
            str(worktree / "tasks" / "fix-task.yaml"),
            str(worktree),
            TASK_ID,
        ),
    )
    cx.commit()
    pool = SqliteLifecyclePersistence(connection=cx)

    class _Rig:
        def __init__(self) -> None:
            self.pool = pool
            self.worktree = worktree
            self.receipts_root = receipts_root
            self.guardkit: FakeReviewLeg | None = None

        def run(self, shape: str):
            from forge.cli._serve_deps_forward_context import (
                ForgeConfigWorktreeAllowlist,
                build_stage_log_reader,
            )

            self.guardkit = FakeReviewLeg(worktree, shape=shape)
            config = _config()
            allowlist = ForgeConfigWorktreeAllowlist(allowed_roots=(str(tmp_path),))
            forward_context_builder = ForwardContextBuilder(
                build_stage_log_reader(pool), allowlist
            )
            supervisor_factory = build_conductor_supervisor_factory(
                pool=pool,
                config=config,
                forward_context_builder=forward_context_builder,
                worktree_allowlist=allowlist,
                read_allowlist=[tmp_path],
                subprocess_runner=self.guardkit,
                publish_card=_never_published,
                gates_green_reader=lambda **_: True,
                receipts_root=receipts_root,
                failure_pack_source_reader=lambda _bid: SOURCE_BUILD_ID,
            )
            deps_factory = build_conductor_driver_deps_factory(
                pool=pool,
                config=config,
                receipts_root=receipts_root,
                source_build_id_reader=lambda _bid: SOURCE_BUILD_ID,
            )
            supervisor = supervisor_factory(BUILD_ID)
            deps = deps_factory(BUILD_ID, supervisor)
            return asyncio.run(drive_fix_journey(BUILD_ID, deps))

        def review_rows(self) -> list[Any]:
            return [
                r
                for r in self.pool.read_stages(BUILD_ID)
                if r.stage_label == "task-review"
                and r.details.get("lifecycle_state") != "running"
            ]

        def build_row(self) -> Any:
            return self.pool.get_build_row(BUILD_ID)

    return _Rig()


async def _never_published(**kwargs: Any) -> str:  # pragma: no cover - guard
    raise AssertionError(
        "no merge card may be published on a journey whose review leg "
        f"never spoke (kwargs={sorted(kwargs)})"
    )


# ---------------------------------------------------------------------------
# 1 — a refused leg is a FAILED leg, and its debris is not a fix-task list
# ---------------------------------------------------------------------------


class TestARefusedLegIsNeverACleanReview:
    def test_the_stage_row_is_failed_and_carries_the_legs_own_banner(
        self, rig
    ) -> None:
        rig.run("refused")

        rows = rig.review_rows()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.status == "FAILED"
        assert REFUSAL_BANNER in row.details["rationale"]

    def test_a_failed_legs_stray_artefact_is_not_read_as_a_fix_task(
        self, rig
    ) -> None:
        """The debris hazard: the leg died, but it had touched ``tasks/``.

        Reading a dead leg's leftovers as its findings is the same lie as
        reading its silence as zero findings, one layer down.
        """
        rig.run("refused")

        assert rig.review_rows()[0].details["fix_tasks"] == []

    def test_the_journey_stops_on_a_named_tooling_fault_carrying_the_reason(
        self, rig
    ) -> None:
        report = rig.run("refused")

        assert RATIONALE_FAILED_REVIEW_LEG in report.rationale, report.rationale
        assert REFUSAL_BANNER in report.rationale, report.rationale

    def test_the_build_row_transitions_out_of_running(self, rig) -> None:
        rig.run("refused")

        row = rig.build_row()
        assert row.status is BuildState.FAILED
        assert row.error, "a FAILED row with an empty error is the stuck row again"
        assert RATIONALE_FAILED_REVIEW_LEG in row.error


# ---------------------------------------------------------------------------
# 2 — the quieter twin: rc=0 and no markers is not a clean review either
# ---------------------------------------------------------------------------


class TestASilentLegIsNeverACleanReview:
    def test_no_readable_markers_block_fails_the_leg(self, rig) -> None:
        rig.run("silent")

        row = rig.review_rows()[0]
        assert row.status == "FAILED"
        assert "findings" in row.details["rationale"].lower()

    def test_the_journey_does_not_report_a_clean_review(self, rig) -> None:
        report = rig.run("silent")

        assert "clean-review" not in report.rationale, report.rationale
        assert RATIONALE_FAILED_REVIEW_LEG in report.rationale, report.rationale

    def test_the_build_row_transitions_out_of_running(self, rig) -> None:
        rig.run("silent")

        assert rig.build_row().status is BuildState.FAILED


# ---------------------------------------------------------------------------
# 3 — the honest clean review keeps working, and now completes the row
# ---------------------------------------------------------------------------


class TestAGenuineCleanReviewStillCompletes:
    def test_the_stage_row_passes_and_records_the_empty_finding(self, rig) -> None:
        rig.run("clean")

        row = rig.review_rows()[0]
        assert row.status == "PASSED"
        assert row.details["fix_tasks"] == []
        assert row.details["finding_anchors"] == []

    def test_the_build_row_transitions_to_complete(self, rig) -> None:
        rig.run("clean")

        row = rig.build_row()
        assert row.status is BuildState.COMPLETE
        assert row.completed_at is not None

    def test_exactly_one_leg_ran(self, rig) -> None:
        rig.run("clean")

        assert rig.guardkit.subcommands() == ["task-review"]
