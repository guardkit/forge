"""Pytest-bdd wiring for TASK-PSM-011 (cancel + skip CLI scenarios).

Binds the four PSM-011 Gherkin scenarios in
``features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature``
to step functions that exercise the real ``forge cancel`` /
``forge skip`` thin wrappers (TASK-PSM-011) end-to-end via
:class:`click.testing.CliRunner`. The wrapper delegations terminate at
in-memory fakes (a recording :class:`FakeCliRuntime`) so the suite runs
without standing up SQLite, NATS, or the LangGraph runtime.

AC-008 coverage map:

* Group C "Skip on non-paused refused"
  :func:`test_skip_on_non_paused_is_refused`
* Group C "Cancel of unknown feature → not-found"
  :func:`test_cancel_unknown_feature_is_refused`
* Group D "Cancel paused → synthetic reject"
  :func:`test_cancel_paused_resolves_pending_review_as_rejection`
* Group D "Skip flagged-stage → resume running"
  :func:`test_skip_on_flagged_stage_resumes_build_and_marks_skipped`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pytest_bdd import given, scenario, then, when

from forge.cli import _cancel_run as cancel_run_module
from forge.cli import cancel as cancel_module  # noqa: F401 — kept for symmetry
from forge.cli import runtime as cli_runtime
from forge.cli import skip as skip_module
from forge.lifecycle.persistence import Build
from forge.lifecycle.state_machine import BuildState
from forge.pipeline.cli_steering import (
    BuildLifecycle,
    BuildSnapshot,
    CancelOutcome,
    CancelStatus,
    SkipOutcome,
    SkipStatus,
)
from forge.pipeline.constitutional_guard import SkipDecision, SkipVerdict
from forge.pipeline.stage_taxonomy import StageClass

_FEATURE = (
    "pipeline-state-machine-and-configuration/"
    "pipeline-state-machine-and-configuration.feature"
)


# ---------------------------------------------------------------------------
# In-memory recording fakes — exercised by every PSM-011 BDD scenario
# ---------------------------------------------------------------------------


@dataclass
class _FakePersistence:
    builds: dict[str, Build] = field(default_factory=dict)

    def find_active_or_recent(self, identifier: str) -> Build | None:
        if identifier in self.builds:
            return self.builds[identifier]
        for build in self.builds.values():
            if build.build_id == identifier:
                return build
        return None

    def get_build_row(self, build_id: str) -> None:
        # These scenarios model a PAUSED build with no persisted pending
        # approval request id, so ``try_inject_paused_cancel`` returns False
        # and the wrapper falls back to ``handle_cancel`` (the path the
        # Gherkin then-steps assert). Returning ``None`` is the falsy row.
        return None


@dataclass
class _FakeSnapshotReader:
    snapshots: dict[str, BuildSnapshot] = field(default_factory=dict)

    def get_snapshot(self, build_id: str) -> BuildSnapshot:
        return self.snapshots.get(
            build_id,
            BuildSnapshot(build_id=build_id, lifecycle=BuildLifecycle.TERMINAL),
        )


@dataclass
class _FakeHandler:
    snapshot_reader: _FakeSnapshotReader
    cancel_calls: list[dict[str, Any]] = field(default_factory=list)
    skip_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_outcome: CancelOutcome | None = None
    skip_outcome: SkipOutcome | None = None

    def handle_cancel(
        self, *, build_id: str, reason: str, responder: str
    ) -> CancelOutcome:
        self.cancel_calls.append(
            {"build_id": build_id, "reason": reason, "responder": responder}
        )
        return self.cancel_outcome or CancelOutcome(
            build_id=build_id,
            status=CancelStatus.CANCELLED_DIRECT,
            rationale=f"cancel({build_id}, reason={reason!r}, by={responder!r})",
        )

    def handle_skip(
        self,
        *,
        build_id: str,
        stage: StageClass,
        reason: str,
        responder: str,
    ) -> SkipOutcome:
        self.skip_calls.append(
            {
                "build_id": build_id,
                "stage": stage,
                "reason": reason,
                "responder": responder,
            }
        )
        return self.skip_outcome or SkipOutcome(
            build_id=build_id,
            stage=stage,
            status=SkipStatus.SKIPPED,
            rationale=(
                f"skip({build_id}, stage={stage.value}, reason={reason!r}, "
                f"by={responder!r})"
            ),
            guard_decision=SkipDecision(
                stage=stage,
                verdict=SkipVerdict.ALLOWED,
                rationale="permitted",
            ),
        )


# ---------------------------------------------------------------------------
# Scenario decorators — bind the four PSM-011 Gherkin scenarios
# ---------------------------------------------------------------------------


@scenario(_FEATURE, "Skipping a build that is not paused is refused")
def test_skip_on_non_paused_is_refused() -> None:
    """Group C — skip on a non-paused build is refused."""


@scenario(_FEATURE, "Cancelling a feature with no active or recent builds is refused")
def test_cancel_unknown_feature_is_refused() -> None:
    """Group C — cancel of unknown feature surfaces a not-found error."""


@scenario(
    _FEATURE,
    "Cancelling a paused build resolves its pending approval as a rejection",
)
def test_cancel_paused_resolves_pending_review_as_rejection() -> None:
    """Group D — cancel during pause resolves as synthetic reject."""


@scenario(
    _FEATURE,
    "Skipping a stage on a flagged-for-review pause resumes the build and "
    "marks the stage skipped",
)
def test_skip_on_flagged_stage_resumes_build_and_marks_skipped() -> None:
    """Group D — skip on flagged stage resumes the build."""


# ---------------------------------------------------------------------------
# Shared per-scenario state
# ---------------------------------------------------------------------------


@pytest.fixture
def psm011_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Per-scenario state bundle shared across the PSM-011 BDD steps."""
    db_path = tmp_path / "forge.db"
    db_path.write_bytes(b"")
    persistence = _FakePersistence()
    snapshot_reader = _FakeSnapshotReader()
    handler = _FakeHandler(snapshot_reader=snapshot_reader)
    fake_runtime = cli_runtime.CliRuntime(  # type: ignore[arg-type]
        persistence=persistence,  # type: ignore[arg-type]
        cli_steering_handler=handler,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli_runtime, "build_cli_runtime", lambda *_a, **_kw: fake_runtime
    )
    # ``forge cancel`` builds its runtime in ``_cancel_run`` (O-02 extraction);
    # ``forge skip`` still builds it in-module.
    monkeypatch.setattr(
        cancel_run_module, "build_cli_runtime", lambda *_a, **_kw: fake_runtime
    )
    monkeypatch.setattr(
        skip_module, "build_cli_runtime", lambda *_a, **_kw: fake_runtime
    )

    # O-02: the responder resolves via $FORGE_RESPONDER (the operator identity
    # the Gherkin records "on my behalf") rather than a tty-dependent
    # os.getlogin(). Setting it here keeps the assertion deterministic.
    monkeypatch.setenv("FORGE_RESPONDER", "alice")

    return {
        "db_path": db_path,
        "persistence": persistence,
        "snapshot_reader": snapshot_reader,
        "handler": handler,
        "result": None,
    }


# ---------------------------------------------------------------------------
# Given steps
# ---------------------------------------------------------------------------


@given("Forge is configured from the project configuration file")
def given_forge_configured(psm011_world: dict[str, Any]) -> None:
    """Background step — forge.yaml is implicit for these scenarios."""
    # No additional wiring needed: psm011_world already provides the
    # FakeCliRuntime that stands in for the production loader.
    return None


@given("the pipeline is running a build that is not paused for review")
def given_running_build(psm011_world: dict[str, Any]) -> None:
    build = Build(build_id="build-RUNNING-001", status=BuildState.RUNNING)
    psm011_world["persistence"].builds["FEAT-RUN"] = build
    psm011_world["snapshot_reader"].snapshots[build.build_id] = BuildSnapshot(
        build_id=build.build_id, lifecycle=BuildLifecycle.OTHER_RUNNING
    )
    psm011_world["feature_id"] = "FEAT-RUN"


@given("there is no build on record for a given feature")
def given_no_build_on_record(psm011_world: dict[str, Any]) -> None:
    psm011_world["feature_id"] = "FEAT-MISSING"


@given("a build is paused awaiting a review decision")
def given_paused_build(psm011_world: dict[str, Any]) -> None:
    build = Build(build_id="build-PAUSED-001", status=BuildState.PAUSED)
    psm011_world["persistence"].builds["FEAT-PAUSE"] = build
    psm011_world["snapshot_reader"].snapshots[build.build_id] = BuildSnapshot(
        build_id=build.build_id,
        lifecycle=BuildLifecycle.PAUSED_AT_GATE,
        paused_stage=StageClass.AUTOBUILD,
        paused_feature_id="FEAT-PAUSE",
    )
    psm011_world["handler"].cancel_outcome = CancelOutcome(
        build_id=build.build_id,
        status=CancelStatus.CANCELLED_VIA_PAUSE_REJECT,
        rationale="cancel-via-pause-reject",
        paused_stage=StageClass.AUTOBUILD,
        paused_feature_id="FEAT-PAUSE",
    )
    psm011_world["feature_id"] = "FEAT-PAUSE"


@given("a build is paused on a flag-for-review gate")
def given_flagged_paused_build(psm011_world: dict[str, Any]) -> None:
    build = Build(build_id="build-FLAGGED-001", status=BuildState.PAUSED)
    psm011_world["persistence"].builds["FEAT-FLAG"] = build
    psm011_world["snapshot_reader"].snapshots[build.build_id] = BuildSnapshot(
        build_id=build.build_id,
        lifecycle=BuildLifecycle.PAUSED_AT_GATE,
        paused_stage=StageClass.FEATURE_PLAN,
        paused_feature_id="FEAT-FLAG",
    )
    psm011_world["feature_id"] = "FEAT-FLAG"


# ---------------------------------------------------------------------------
# When steps
# ---------------------------------------------------------------------------


@when("I ask to skip the current stage of that build")
def when_skip_current_stage(psm011_world: dict[str, Any]) -> None:
    runner = CliRunner()
    psm011_world["result"] = runner.invoke(
        skip_module.skip_cmd,
        [psm011_world["feature_id"], "--db", str(psm011_world["db_path"])],
    )


@when("I ask to cancel that feature")
def when_cancel_unknown_feature(psm011_world: dict[str, Any]) -> None:
    runner = CliRunner()
    psm011_world["result"] = runner.invoke(
        cancel_module.cancel_cmd,
        [psm011_world["feature_id"], "--db", str(psm011_world["db_path"])],
    )


@when("I ask to cancel that build with a reason")
def when_cancel_paused_with_reason(psm011_world: dict[str, Any]) -> None:
    runner = CliRunner()
    psm011_world["result"] = runner.invoke(
        cancel_module.cancel_cmd,
        [
            psm011_world["feature_id"],
            "--reason",
            "operator override",
            "--db",
            str(psm011_world["db_path"]),
        ],
    )


@when("I ask to skip the flagged stage with a reason")
def when_skip_flagged_with_reason(psm011_world: dict[str, Any]) -> None:
    runner = CliRunner()
    psm011_world["result"] = runner.invoke(
        skip_module.skip_cmd,
        [
            psm011_world["feature_id"],
            "--reason",
            "approved by reviewer",
            "--db",
            str(psm011_world["db_path"]),
        ],
    )


# ---------------------------------------------------------------------------
# Then steps
# ---------------------------------------------------------------------------


@then("the skip command should be refused with an error")
def then_skip_refused(psm011_world: dict[str, Any]) -> None:
    result = psm011_world["result"]
    assert result.exit_code != 0
    assert "REFUSED" in result.stderr


@then("no skip decision should be sent to the pipeline")
def then_no_skip_decision_sent(psm011_world: dict[str, Any]) -> None:
    assert psm011_world["handler"].skip_calls == []


@then("the cancel command should be refused with a not-found error")
def then_cancel_refused_not_found(psm011_world: dict[str, Any]) -> None:
    result = psm011_world["result"]
    assert result.exit_code != 0
    assert "no active or recent build" in result.stderr.lower()
    assert psm011_world["handler"].cancel_calls == []


@then(
    "the cancel command should resolve the pending review as a rejection on my behalf"
)
def then_cancel_resolves_via_pause_reject(psm011_world: dict[str, Any]) -> None:
    assert len(psm011_world["handler"].cancel_calls) == 1
    call = psm011_world["handler"].cancel_calls[0]
    assert call["responder"] == "alice"
    assert call["reason"] == "operator override"


@then("the build should transition from paused to cancelled")
def then_build_transitions_to_cancelled(psm011_world: dict[str, Any]) -> None:
    # The fake handler's pre-canned outcome exercises the
    # CANCELLED_VIA_PAUSE_REJECT branch — the wrapper echoes the rationale
    # and exits zero. Real state writes are covered by
    # tests/forge/test_cli_steering.py against the actual handler.
    result = psm011_world["result"]
    assert result.exit_code == 0
    assert "Cancelled" in result.output


@then("the reason I supplied should be recorded on the build")
def then_reason_recorded(psm011_world: dict[str, Any]) -> None:
    call = psm011_world["handler"].cancel_calls[0]
    assert call["reason"] == "operator override"


@then("the paused stage should be recorded as skipped with my reason")
def then_paused_stage_recorded_skipped(psm011_world: dict[str, Any]) -> None:
    assert len(psm011_world["handler"].skip_calls) == 1
    call = psm011_world["handler"].skip_calls[0]
    assert call["stage"] is StageClass.FEATURE_PLAN
    assert call["reason"] == "approved by reviewer"
    assert call["responder"] == "alice"


@then("the build should resume from running")
def then_build_resumes_running(psm011_world: dict[str, Any]) -> None:
    result = psm011_world["result"]
    assert result.exit_code == 0


@then("the overall build should still be allowed to complete successfully")
def then_build_completes_successfully(psm011_world: dict[str, Any]) -> None:
    # The wrapper emits a "Skipped <build_id> stage=<stage>" line on the
    # permitted-skip path. Downstream completion is covered by the
    # supervisor / state-machine suites.
    result = psm011_world["result"]
    assert "Skipped" in result.output
