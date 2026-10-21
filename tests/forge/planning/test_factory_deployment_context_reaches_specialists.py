"""Captured factory evidence reaches the real spec/plan consumers after restart.

Run this connected check with the candidate specialist-agent source and its test
fixtures importable. Git, SQLite, command builders, registered-mode validation,
input assembly, generation loops and postprocessors are real. Only transport and
model seats are scripted. All project artifacts and traces stay in tmp_path.
This proves carriage and persistence, not a model's deployment judgement.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from forge.adapters.git.planning_runner import WorktreeGitRunner
from forge.adapters.sqlite import connect as sqlite_connect
from forge.cli._serve_planning import (
    build_feature_plan_command_args,
    build_feature_spec_command_args,
)
from forge.config.models import PlanningConfig, TargetTerminalConfig
from forge.lifecycle import migrations
from forge.pipeline.dispatchers.specialist import (
    StageDispatchOutcome,
    StageDispatchResult,
    build_specialist_command,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning import factory_deployment_context as factory_context
from forge.planning.driver import PlanningDriverDeps, PlanningRunDriver
from forge.planning.gate_adapters import build_planning_gate_adapters
from forge.planning.reference_material import REFERENCE_FILE_HEADING
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.target_terminal_tools import (
    make_normalize_feature_spec,
    make_validate_feature_plan,
)
from tests.forge.planning import test_driver_spec_digest_door as door
from tests.forge.planning.test_memory_name_at_the_door import _git
from tests.forge.planning.test_project_documents_for_the_writers import (
    AGENTS,
    DECLARED,
    DECLARES_DOCUMENTS,
    MISSION_V1,
    TECH_V1,
    _seed_project,
)
from tests.forge.recorded_fetch_address import allow_local_remotes

_REPO = "example/uploads"
_REFERENCE_PATH = "reference/upload-notes.md"
_REFERENCE_TEXT = "# Upload notes\n\nKeep filenames such as résumé.txt.\n"
_REQUEST = (
    "Upload files up to 50MB, including the size limit.\n"
    "Keep filenames such as résumé.txt; see reference/upload-notes.md."
)


class _ResponseInterrupted(BaseException):
    """A process stop after model work, before the transport returns its reply."""


class _PlanCommitted(BaseException):
    """Stop at the plan receipt, before downstream QA/build operations."""


def _load_fixture(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_sources(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import specialist_agent
    from specialist_agent.adapters.manifest import _ensure_roles_registered

    root = Path(specialist_agent.__file__).resolve().parents[2]
    tests = root / "tests"
    assert tests.is_dir(), "This connected check requires specialist source fixtures"
    monkeypatch.syspath_prepend(str(tests))
    monkeypatch.delenv("SPECIALIST_TRACE_SINK_DIR", raising=False)
    _ensure_roles_registered()
    return SimpleNamespace(
        root=root,
        spec=_load_fixture(
            "connected_spec_fixture",
            tests / "roles/product_owner/test_feature_spec_dispatch.py",
        ),
        plan=_load_fixture(
            "connected_plan_fixture",
            tests / "roles/architect/test_feature_plan_dispatch.py",
        ),
    )


def _plan_output(fixtures: SimpleNamespace, args: dict[str, Any]) -> str:
    """Use the conformant plan fixture, naming this driver's spec and identity."""
    from fixtures.plan_content_list import plan_output_with_list
    from fixtures.plan_list_references import output_with_references

    source = fixtures.plan
    output = source._multi_task_output().replace(source._FID, args["feature_id"])
    owners = {
        title: "TASK-DEMO-001"
        for title in re.findall(r"^\s*Scenario: (.+)$", args["spec_feature"], re.MULTILINE)
    }
    output = output.replace(source._OWNERS, yaml.safe_dump({"scenario_owners": owners}))
    # The production plan checker requires the testing task before the feature
    # task. These are the same wave/dependency declarations as its good fixture.
    output = output.replace(
        "  - - TASK-DEMO-001\n  - - TASK-DEMO-002\n  - - TASK-DEMO-003\n",
        "  - - TASK-DEMO-001\n    - TASK-DEMO-003\n  - - TASK-DEMO-002\n",
    ).replace(
        source._tdoc("TASK-DEMO-003", "testing", 3, "task-work", 4).replace(
            source._FID, args["feature_id"]
        ),
        source._tdoc("TASK-DEMO-003", "testing", 1, "direct", 3).replace(
            source._FID, args["feature_id"]
        ),
    ).replace(
        source._entry("TASK-DEMO-003", source._D3, 4, ["TASK-DEMO-002"], "task-work"),
        source._entry("TASK-DEMO-003", source._D3, 3, [], "direct"),
    )
    # Include the actual committed spec paths up front so Forge need not fill
    # them in and ask for another semantic review of a changed artifact set.
    output = output.replace(
        "scenario_owners:\n",
        yaml.safe_dump({"feature_files": args["spec_feature_paths"]})
        + "scenario_owners:\n",
        1,
    )
    return output_with_references(plan_output_with_list(output, _REQUEST))


class _RecordingSeat:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.inputs: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.inputs.append(copy.deepcopy(payload))
        reply = self.replies[min(len(self.inputs) - 1, len(self.replies) - 1)]
        return {"messages": [{"role": "assistant", "content": reply}]}


def _message_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        message["content"] if isinstance(message, dict) else message.content
        for message in payload["messages"]
    )


def _assert_documents(payload: dict[str, Any], expected: list[str]) -> None:
    text = _message_text(payload)
    offsets = [text.index(document) for document in expected]
    assert offsets == sorted(offsets)
    for document, offset in zip(expected, offsets, strict=True):
        actual = text[offset : offset + len(document)].encode("utf-8")
        assert actual == document.encode("utf-8")
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(
            document.encode("utf-8")
        ).hexdigest()


def _records(store: SqlitePlanningRunStore, stage: str) -> list[dict[str, Any]]:
    return [
        json.loads(event["details_json"])
        for event in store.list_events(door.CID)
        if event["stage_label"] == stage
    ]


@pytest.mark.asyncio
async def test_captured_reference_reaches_real_writers_and_coaches_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specialist_agent.core.api import run_specialist_mode
    from specialist_agent.criteria.loader import load_criteria, select_criteria

    fixtures = _fixture_sources(monkeypatch)
    allow_local_remotes(monkeypatch, tmp_path / "fetch-copies")
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Fixture Author")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "fixture@example.invalid")
    remote, project = _seed_project(
        tmp_path,
        extra={
            ".guardkit/config.yaml": DECLARES_DOCUMENTS
            + 'reference:\n  paths: ["reference/"]\n',
            _REFERENCE_PATH: _REFERENCE_TEXT,
        },
    )
    start_commit = _git(project, "rev-parse", "HEAD")
    original = factory_context.load_bundled_document()
    expected = [
        f"File: AGENTS.md\n{AGENTS}",
        f"File: {DECLARED[0]}\n{MISSION_V1}",
        f"File: {DECLARED[1]}\n{TECH_V1}",
        f"{REFERENCE_FILE_HEADING}\nFile: {_REFERENCE_PATH}\n{_REFERENCE_TEXT}",
        original.text,
    ]
    db = tmp_path / "planning.db"
    calls: list[dict[str, Any]] = []
    stores: list[SqlitePlanningRunStore] = []
    connections: list[Any] = []
    interrupted: set[StageClass] = set()
    notifications: list[tuple[str, str]] = []
    current: SqlitePlanningRunStore

    def open_store() -> SqlitePlanningRunStore:
        connection = sqlite_connect.connect_writer(db)
        connections.append(connection)
        migrations.apply_at_boot(connection)
        store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
        stores.append(store)
        return store

    def assert_recorded_pair() -> None:
        assert _records(current, factory_context.EXPECTATION_STAGE) == [
            {"factory_context": original.receipt()}
        ]
        assert _records(current, factory_context.CAPTURE_STAGE) == [
            {"factory_context": original.to_record()}
        ]
        assert current.get_run(door.CID)["request_text"].encode("utf-8") == _REQUEST.encode("utf-8")

    async def dispatch(stage: StageClass, kwargs: dict[str, Any]) -> StageDispatchResult:
        assert_recorded_pair()  # Before either model seat can be created/called.
        assert kwargs["context"] == expected
        assert kwargs["request_text"].encode("utf-8") == _REQUEST.encode("utf-8")
        fields = {k: v for k, v in kwargs.items() if k not in {"plan_run_id", "correlation_id"}}
        if stage is StageClass.FEATURE_SPEC:
            fields["from_input"] = fields.pop("spec_input")
            built = build_feature_spec_command_args(**fields)
            role = "product-owner"
            output = fixtures.spec._valid_triple()
        else:
            built = build_feature_plan_command_args(**fields)
            role = "architect"
            output = _plan_output(fixtures, built)
        command, wire = build_specialist_command(
            stage, request_text=None, context_entries=[], extra_command_args=built
        )
        # A JSON roundtrip stands in for the transport, not for argument validation.
        wire = json.loads(json.dumps(wire, ensure_ascii=False))
        assert wire["context"] == expected
        assert wire["request_text"] == _REQUEST
        definitions = load_criteria(fixtures.root / "roles" / role / "criteria" / f"{command}.yaml")
        criteria = select_criteria(definitions, revision=bool(wire.get("validate_feedback")))

        def scoring_reply(score: float) -> str:
            return json.dumps({
                "criterion_scores": [
                    {"criterion_id": c.id, "weight": c.weight, "score": score,
                     "reasoning": "scripted carriage check"} for c in criteria
                ],
                "detection_findings": [],
                "feedback": ["Revise once for the carriage check."] if score < 1 else [],
            })

        player = _RecordingSeat([output])
        exact_review = "semantic_review_artifacts" in wire
        coach = _RecordingSeat(
            [scoring_reply(1)] if exact_review else [scoring_reply(0.4), scoring_reply(1)]
        )
        coherence_prompt = (fixtures.root / "roles/product-owner/prompts/coach_coherence.md").read_text()
        support_prompt = (fixtures.root / "roles/product-owner/prompts/coach_example_support.md").read_text()

        def create_coach(_self: Any, system_prompt: str | None = None) -> Any:
            assert_recorded_pair()
            if system_prompt == coherence_prompt:
                return _RecordingSeat([json.dumps({"contradictions": []})])
            if system_prompt == support_prompt:
                return _RecordingSeat([json.dumps({"examples": [
                    {"title": title, "obligation": "upload", "verdict": "FOLLOWS", "why": "fixture"}
                    for title in ("Upload a small file", "Upload at the exact size limit")
                ]})])
            return coach

        with (
            monkeypatch.context() as specialist_runtime,
            patch("specialist_agent.orchestrator.session.Orchestrator._create_player", return_value=player),
            patch("specialist_agent.orchestrator.session.Orchestrator._create_coach", create_coach),
        ):
            # The public API loads roles/ relative to the service working
            # directory. Reproduce that supported runtime layout using this
            # candidate's checkout; all generated output still goes to tmp_path.
            specialist_runtime.chdir(fixtures.root)
            result = await run_specialist_mode(
                role, command, wire, output_path=str(tmp_path / f"specialist-{len(calls)}")
            )
        assert result.success, result
        assert json.loads(result.artifacts["validation.json"])["accepted"] is True
        if not exact_review:
            assert len(player.inputs) == len(coach.inputs) == 2
            for payload in player.inputs:
                _assert_documents(payload, expected)
            assert "Revise once for the carriage check." in _message_text(player.inputs[1])
        for payload in coach.inputs:
            _assert_documents(payload, expected)
            assert _REQUEST in _message_text(payload)
        calls.append({"stage": stage, "wire": wire, "player": player, "coach": coach})
        if stage not in interrupted:
            interrupted.add(stage)
            raise _ResponseInterrupted()
        return StageDispatchResult(
            outcome=StageDispatchOutcome.COMPLETED,
            stage=stage,
            build_id=kwargs["plan_run_id"],
            correlation_id=kwargs["correlation_id"],
            stage_log_entry_id=f"fixture-{len(calls)}",
            coach_score=1.0,
            role_output=result.artifacts,
        )

    async def dispatch_spec(**kwargs: Any) -> StageDispatchResult:
        return await dispatch(StageClass.FEATURE_SPEC, kwargs)

    async def dispatch_plan(**kwargs: Any) -> StageDispatchResult:
        return await dispatch(StageClass.FEATURE_PLAN, kwargs)

    async def product_owner(**kwargs: Any) -> StageDispatchResult:
        return StageDispatchResult(
            outcome=StageDispatchOutcome.COMPLETED,
            stage=StageClass.PRODUCT_OWNER,
            build_id=kwargs["plan_run_id"],
            correlation_id=kwargs["correlation_id"],
            stage_log_entry_id="fixture-product-owner",
            coach_score=1.0,
            role_output={"title": "File Upload", "problem_statement": _REQUEST},
        )

    async def notify(_cid: str, message: str, level: str) -> None:
        notifications.append((level, message))
        # This is the last scope of this test. The plan receipt already exists;
        # no task execution, broker publish, deployment, or real product follows.
        if _records(current, "feature-plan"):
            raise _PlanCommitted()

    def new_driver() -> PlanningRunDriver:
        clock = lambda: datetime.now(UTC)
        repository, state_machine = build_planning_gate_adapters(current, clock=clock)
        locations = {
            "target_repo_paths": {_REPO: str(project)},
            "fetch_addresses": {_REPO: str(remote)},
        }
        return PlanningRunDriver(PlanningDriverDeps(
            store=current,
            repository=repository,
            state_machine=state_machine,
            approval_publisher=door.FakePublisher(),
            subscriber_factory=door.SharedScriptFactory([door._answer("approve")]),
            dispatch_product_owner=product_owner,
            second_opinion_provider=door.FakeSecondOpinion(),
            git_runner=WorktreeGitRunner(worktrees_root=tmp_path / "worktrees", **locations),
            planning_config=PlanningConfig(
                enabled=True, target_terminal=TargetTerminalConfig(enabled=True), **locations
            ),
            clock=clock,
            publish_notification=notify,
            dispatch_feature_spec=dispatch_spec,
            dispatch_feature_plan=dispatch_plan,
            normalize_feature_spec=make_normalize_feature_spec(command_prefix=None),
            validate_feature_plan=make_validate_feature_plan(),
        ))

    try:
        current = open_store()
        current.record_queued(
            correlation_id=door.CID,
            originating_user=door.ORIGINATOR,
            expected_approver=door.ORIGINATOR,
            request_text=_REQUEST,
            triggered_by="jarvis",
            target_repo=_REPO,
        )
        with pytest.raises(_ResponseInterrupted):
            await new_driver().drive(door.CID)
        assert [c["stage"] for c in calls] == [StageClass.FEATURE_SPEC]
        assert_recorded_pair()

        changed = factory_context.FactoryDeploymentDocument.of(
            original.text + "\nA later installed reference must not enter this run.\n",
            version="next-fixture-version",
        )
        assert changed.sha256 != original.sha256
        bundle_reads: list[None] = []

        def current_bundle() -> Any:
            bundle_reads.append(None)
            return changed

        monkeypatch.setattr(factory_context, "load_bundled_document", current_bundle)
        connections[-1].close()
        current = open_store()
        with pytest.raises(_ResponseInterrupted):
            await new_driver().drive(door.CID)
        assert [c["stage"] for c in calls] == [
            StageClass.FEATURE_SPEC, StageClass.FEATURE_SPEC, StageClass.FEATURE_PLAN,
        ]

        connections[-1].close()
        current = open_store()
        with pytest.raises(_PlanCommitted):
            await new_driver().drive(door.CID)
        assert len({id(store) for store in stores}) == 3
        assert_recorded_pair()
        assert bundle_reads == [], "A valid captured pair must not consult today's package"
        assert [c["stage"] for c in calls if "semantic_review_artifacts" not in c["wire"]] == [
            StageClass.FEATURE_SPEC, StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN, StageClass.FEATURE_PLAN,
        ]
        assert all(changed.text not in _message_text(p)
                   for call in calls for p in call["coach"].inputs + call["player"].inputs)
        drafts = _records(current, "feature-spec-draft")
        assert len(drafts) == 1
        assert drafts[0]["spec_draft"]["factory_context"] == original.receipt()
        (plan,) = _records(current, "feature-plan")
        assert plan["factory_context"] == original.receipt()
        assert plan["semantic_review"]["decision"] == "approved"
        project_capture = _records(current, "project-documents")
        assert len(project_capture) == 1
        assert [d["path"] for d in project_capture[0]["project_documents"]] == ["AGENTS.md", *DECLARED]
        assert {d["commit"] for d in project_capture[0]["project_documents"]} == {start_commit}
        dispatches = _records(current, factory_context.DISPATCH_STAGE)
        assert len(dispatches) >= 4
        assert all(d["factory_context"] == original.receipt() for d in dispatches)
        assert not [message for level, message in notifications if level == "error"]
    finally:
        for connection in connections:
            connection.close()
