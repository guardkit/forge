"""Tests for ``_node_planning_waves`` feature task graph reading (TASK-UBS1C-002).

Validates that ``_node_planning_waves`` reads the feature's task graph from
the target repo's ``.guardkit/features/<feature_id>.yaml`` and populates
``wave_total`` and ``task_total`` in the existing ``AutobuildState`` schema.

Test cases map to acceptance criteria:

* ``test_planning_waves_reads_task_graph`` — AC-001: fixture yaml with 3
  tasks and parallel_groups [[a,b],[c]] produces correct totals.
* ``test_planning_waves_missing_yaml`` — AC-002: missing yaml produces
  placeholder snapshot + WARNING.
* ``test_planning_waves_malformed_yaml`` — AC-002: malformed yaml produces
  placeholder snapshot + WARNING.
* ``test_planning_waves_feature_id_absent`` — AC-002: feature id absent
  from file produces placeholder snapshot + WARNING.
* ``test_planning_waves_uses_resolve_repo_path`` — AC-003: asserts that
  the same ``_resolve_repo_path`` helper is used (via patch assertion).
* ``test_planning_waves_schema_unchanged`` — AC-003: snapshot is a valid
  ``AutobuildState`` dict with the frozen schema.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from forge.subagents.autobuild_runner import (
    AutobuildState,
    _build_snapshot,
    _extract_launch_payload,
    _node_planning_waves,
    _resolve_repo_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_launch_payload(
    feature_id: str = "FEAT-TEST",
    repo: str = "test-repo",
    **extra: Any,
) -> dict[str, Any]:
    """Construct a launch payload dict matching the expected format."""
    payload: dict[str, Any] = {
        "feature_id": feature_id,
        "repo": repo,
        "build_id": f"build-{feature_id}-001",
    }
    payload.update(extra)
    return payload


def _make_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Wrap a payload into the messages format expected by the runner."""
    return [
        {
            "content": (
                f'RUN_AUTOBUILD subagent=autobuild_runner '
                f'payload={json.dumps(payload)}'
            )
        }
    ]


def _setup_tmp_repo(
    tmp_path: Path,
    feature_yaml: dict[str, Any] | None = None,
    feature_id: str = "FEAT-TEST",
    repo_name: str = "test-repo",
) -> Path:
    """Create a minimal git repo structure with an optional feature yaml.

    Returns the resolved repo path.
    """
    repo_dir = tmp_path / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    if feature_yaml is not None:
        features_dir = repo_dir / ".guardkit" / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = features_dir / f"{feature_id}.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(feature_yaml, f)

    return repo_dir


# ---------------------------------------------------------------------------
# AC-001: Valid fixture reads correct totals
# ---------------------------------------------------------------------------


class TestPlanningWavesReadsTaskGraph:
    """AC-001: fixture feature yaml produces correct wave/task totals."""

    def test_planning_waves_reads_task_graph(self, tmp_path: Path) -> None:
        """3 tasks + parallel_groups [[a,b],[c]] → wave_total=2, task_total=3."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [
                {"id": "TASK-A"},
                {"id": "TASK-B"},
                {"id": "TASK-C"},
            ],
            "orchestration": {
                "parallel_groups": [
                    ["TASK-A", "TASK-B"],
                    ["TASK-C"],
                ]
            },
        }
        repo_path = _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        # Override FORGE_REPO_BASE so _resolve_repo_path finds our tmp repo.
        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 2
        assert snapshot["task_total"] == 3
        assert snapshot["lifecycle"] == "planning_waves"
        assert snapshot["feature_id"] == "FEAT-TEST"


# ---------------------------------------------------------------------------
# AC-002: Error cases produce placeholder + WARNING
# ---------------------------------------------------------------------------


class TestPlanningWavesErrorCases:
    """AC-002: missing/malformed yaml and absent feature id → placeholder."""

    def test_planning_waves_missing_yaml(self, tmp_path: Path) -> None:
        """Missing yaml → wave_total=0, task_total=0 + WARNING log."""
        repo_path = _setup_tmp_repo(
            tmp_path,
            feature_yaml=None,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 0
        assert snapshot["lifecycle"] == "planning_waves"

    def test_planning_waves_malformed_yaml(self, tmp_path: Path) -> None:
        """Malformed yaml → wave_total=0, task_total=0 + WARNING log."""
        repo_dir = _setup_tmp_repo(
            tmp_path,
            feature_yaml=None,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )
        features_dir = repo_dir / ".guardkit" / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = features_dir / "FEAT-TEST.yaml"
        with open(yaml_path, "w") as f:
            f.write("{{invalid: yaml: [[[:")

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 0
        assert snapshot["lifecycle"] == "planning_waves"

    def test_planning_waves_feature_id_absent(self, tmp_path: Path) -> None:
        """Feature id absent from file → wave_total=0, task_total=0 + WARNING."""
        feature_yaml = {
            "id": "FEAT-OTHER",
            "tasks": [{"id": "TASK-X"}],
            "orchestration": {"parallel_groups": [["TASK-X"]]},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-OTHER",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 0
        assert snapshot["lifecycle"] == "planning_waves"

    def test_planning_waves_missing_yaml_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing yaml emits a WARNING naming the resolved path."""
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=None,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            with caplog.at_level(logging.WARNING):
                _node_planning_waves({"messages": messages})

        assert any("feature yaml not found" in record.message for record in caplog.records)

    def test_planning_waves_malformed_yaml_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed yaml emits a WARNING naming the resolved path."""
        repo_dir = _setup_tmp_repo(
            tmp_path,
            feature_yaml=None,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )
        features_dir = repo_dir / ".guardkit" / "features"
        features_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = features_dir / "FEAT-TEST.yaml"
        with open(yaml_path, "w") as f:
            f.write("{{invalid}}")

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            with caplog.at_level(logging.WARNING):
                _node_planning_waves({"messages": messages})

        assert any("failed to parse feature yaml" in record.message for record in caplog.records)

    def test_planning_waves_feature_absent_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Feature id absent from file emits a WARNING naming the resolved path."""
        feature_yaml = {
            "id": "FEAT-OTHER",
            "tasks": [{"id": "TASK-X"}],
            "orchestration": {"parallel_groups": [["TASK-X"]]},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-OTHER",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            with caplog.at_level(logging.WARNING):
                _node_planning_waves({"messages": messages})

        assert any("feature yaml not found" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# AC-003: Repo path resolution, schema unchanged, existing tests pass
# ---------------------------------------------------------------------------


class TestPlanningWavesIntegration:
    """AC-003: uses same resolver, schema unchanged, existing tests pass."""

    def test_planning_waves_uses_resolve_repo_path(self, tmp_path: Path) -> None:
        """Asserts _resolve_repo_path is called (no duplicated logic)."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [{"id": "TASK-A"}],
            "orchestration": {"parallel_groups": [["TASK-A"]]},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            with patch(
                "forge.subagents.autobuild_runner._resolve_repo_path",
                wraps=_resolve_repo_path,
            ) as mock_resolver:
                _node_planning_waves({"messages": messages})
                mock_resolver.assert_called_once_with(payload)

    def test_planning_waves_schema_unchanged(self, tmp_path: Path) -> None:
        """Snapshot is a valid AutobuildState dict with the frozen schema."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [{"id": "TASK-A"}],
            "orchestration": {"parallel_groups": [["TASK-A"]]},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]

        # Validate the snapshot against the AutobuildState model.
        state = AutobuildState(**snapshot)
        assert state.lifecycle == "planning_waves"
        assert state.wave_total == 1
        assert state.task_total == 1

        # Ensure no unexpected extra keys (schema byte-unchanged).
        expected_keys = {
            "task_id",
            "build_id",
            "feature_id",
            "lifecycle",
            "wave_index",
            "wave_total",
            "task_index",
            "task_total",
            "current_task_label",
            "tasks_completed",
            "tasks_failed",
            "last_coach_score",
            "aggregate_coach_score",
            "waiting_for",
            "pending_directives",
            "started_at",
            "last_activity_at",
            "estimated_completion_at",
            "worktree_path",
            "correlation_id",
        }
        assert set(snapshot.keys()) == expected_keys

    def test_planning_waves_no_repo_resolved(self, tmp_path: Path) -> None:
        """When repo cannot be resolved, falls back to placeholder."""
        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="nonexistent-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 0
        assert snapshot["lifecycle"] == "planning_waves"

    def test_planning_waves_empty_tasks_list(self, tmp_path: Path) -> None:
        """Empty tasks list → task_total=0."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [],
            "orchestration": {"parallel_groups": [["TASK-A"]]},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["task_total"] == 0
        assert snapshot["wave_total"] == 1

    def test_planning_waves_empty_parallel_groups(self, tmp_path: Path) -> None:
        """Empty parallel_groups → wave_total=0."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [{"id": "TASK-A"}],
            "orchestration": {"parallel_groups": []},
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 1

    def test_planning_waves_missing_orchestration(self, tmp_path: Path) -> None:
        """Missing orchestration section → wave_total=0, task_total from tasks."""
        feature_yaml = {
            "id": "FEAT-TEST",
            "tasks": [{"id": "TASK-A"}, {"id": "TASK-B"}],
        }
        _setup_tmp_repo(
            tmp_path,
            feature_yaml=feature_yaml,
            feature_id="FEAT-TEST",
            repo_name="test-repo",
        )

        payload = _make_launch_payload(
            feature_id="FEAT-TEST",
            repo="test-repo",
        )
        messages = _make_messages(payload)

        with patch.dict(
            "os.environ",
            {"FORGE_REPO_BASE": str(tmp_path)},
            clear=False,
        ):
            result = _node_planning_waves({"messages": messages})

        snapshot = result["async_tasks"]["FEAT-TEST"]
        assert snapshot["wave_total"] == 0
        assert snapshot["task_total"] == 2
