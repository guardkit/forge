"""The journey's evidence fields are read only for the review preview (W7).

A driver's results envelope may carry ``app_url``, ``screenshot_refs``,
``trace_refs``, ``media_refs``, ``report_ref`` and ``outcome``. Only the review
preview's recorded journey reads them. The ordinary live gate and the
candidate leg read an envelope exactly as Forge ``cbd1fc48`` did, even when
the envelope carries every one of those fields: the reading and the step
result are compared with a golden copy taken from that commit's own code.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from forge.deploy import steps as steps_module
from forge.deploy.live_gate import (
    RepoDriverLiveGateInvoker,
    SidecarLiveGateInvoker,
    project_driver_answer,
)
from forge.persistence.repositories.runbook_models import Step, StepStatus

GOLDEN = (
    Path(__file__).parents[1] / "fixtures" / "live_gate_answer_without_journey_evidence.json"
)

#: An envelope that carries every optional journey field.
ENVELOPE: dict[str, Any] = {
    "verdict": "pass",
    "run_id": "run-1",
    "gates": [{"gate_id": "health"}, {"gate_id": "journey"}],
    "evidence_index_ref": "evidence/index.json",
    "dispositions_ref": "evidence/dispositions.json",
    "attempts_ledger_ref": "evidence/attempts.json",
    "app_url": "https://preview.example.test:8950/",
    "screenshot_refs": ["shots/a.png"],
    "trace_refs": ["trace.zip"],
    "media_refs": ["video/journey.webm"],
    "report_ref": "report/index.html",
    "outcome": {"passed": 3, "failed": 0, "skipped": None, "flaky": None},
}


class _Fixed:
    def __init__(self, invocation: Any) -> None:
        self.invocation = invocation

    def invoke(self, *, feature: str, target: str, gates: tuple[str, ...] = ()):
        return self.invocation


def what_a_gate_reads(answer: Any, steps: Any, **kwargs: Any) -> dict[str, Any]:
    """The reading of :data:`ENVELOPE` and the step result built from it."""
    invocation = answer(
        argv=["driver", "--feature", "F", "--target", "env"],
        cwd="/repo",
        exit_code=0,
        stdout=json.dumps(ENVELOPE),
        stderr="",
        gate_ids=("health",),
        run_id_fallback="F-env",
        **kwargs,
    )
    handler = steps.make_run_live_gate_handler(live_gate_invoker=_Fixed(invocation))
    outcome = handler(
        Step(
            step_type="run_live_gate",
            params={"feature": "F", "target": "env"},
            status=StepStatus.pending,
            sequence_index=0,
        )
    )
    return json.loads(
        json.dumps(
            {
                "invocation": dataclasses.asdict(invocation),
                "step_result": outcome.result,
            },
            default=list,
        )
    )


def test_an_ordinary_gate_reads_exactly_what_cbd1fc48_read() -> None:
    read = what_a_gate_reads(project_driver_answer, steps_module)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    # The step result — what the stage forwards into its QA verdict and
    # live-gate result events — is byte for byte the old one.
    assert json.dumps(read["step_result"]) == json.dumps(golden["step_result"])
    # The invocation is the old one plus five new fields (15 October 2026:
    # ``answer_lost`` False and ``confirmed`` True for a gate that answered).
    added = {
        key: read["invocation"].pop(key)
        for key in ("media_refs", "report_ref", "outcome", "answer_lost", "confirmed")
    }
    assert added == {
        "media_refs": [],
        "report_ref": None,
        "outcome": None,
        "answer_lost": False,
        "confirmed": True,
    }
    assert read["invocation"] == golden["invocation"]


def test_the_preview_journey_reads_every_evidence_field() -> None:
    read = what_a_gate_reads(project_driver_answer, steps_module, journey_evidence=True)
    invocation, result = read["invocation"], read["step_result"]
    assert invocation["app_url"] == ENVELOPE["app_url"]
    assert invocation["screenshot_refs"] == ENVELOPE["screenshot_refs"]
    assert invocation["trace_refs"] == ENVELOPE["trace_refs"]
    assert result["media_refs"] == ENVELOPE["media_refs"]
    assert result["report_ref"] == ENVELOPE["report_ref"]
    assert result["outcome"] == ENVELOPE["outcome"]


def test_only_a_marked_copy_of_an_invoker_reads_the_evidence(tmp_path: Path) -> None:
    repo = RepoDriverLiveGateInvoker(repo_path=tmp_path, driver_argv=["driver"])
    marked = repo.with_journey_evidence()
    assert repo._journey_evidence is False
    assert marked._journey_evidence is True
    # It survives the copies the stage makes after marking, and before.
    assert marked.with_extra_env({"X": "1"})._journey_evidence is True
    assert marked.with_repo_path(tmp_path)._journey_evidence is True
    assert repo.with_extra_env({"X": "1"})._journey_evidence is False

    sidecar = SidecarLiveGateInvoker(
        base_url="http://sidecar.example.test",
        repo="example/notes",
        repo_path=tmp_path,
        driver_argv=["driver"],
        by_hand=True,
    )
    assert sidecar._journey_evidence is False
    assert sidecar.with_journey_evidence()._journey_evidence is True
    assert sidecar.with_journey_evidence().with_extra_env({})._journey_evidence is True
