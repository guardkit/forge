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
    pytest.importorskip("specialist_agent", reason="a sibling repository this test reads (specialist_agent) is not checked out here (the GitHub workflow checks out forge, nats-core and guardkit only)")
    import specialist_agent
    pytest.importorskip("specialist_agent", reason="a sibling repository this test reads (specialist_agent) is not checked out here (the GitHub workflow checks out forge, nats-core and guardkit only)")
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
    return plan_output_with_list(output, _REQUEST)


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


def _records(
    store: SqlitePlanningRunStore, stage: str, *, status: str | None = None
) -> list[dict[str, Any]]:
    return [
        json.loads(event["details_json"])
        for event in store.list_events(door.CID)
        if event["stage_label"] == stage
        and (status is None or event["status"] == status)
    ]


