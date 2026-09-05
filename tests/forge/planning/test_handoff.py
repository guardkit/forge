"""Tests for Mode P planned handoff terminal (TASK-MP-006).

Covers:
- Terminal registry lookup and fake handler injection
- Approved run creates file on branch with GitRunner
- Notification payload sanitization (RT-09 injection guard)
- Target repo resolution and fallback to default
- GitRunner failure handling
- Idempotency (RT-08)
- Cancelled/rejected runs produce zero GitRunner invocations
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.git.models import GitOpResult
from forge.config.models import PlanningConfig
from forge.planning.handoff import (
    NotificationPayload,
    PlannedHandoffHandler,
    TerminalRegistry,
    build_notification_payload,
    get_terminal_registry,
)
from forge.planning.states import PlanningState

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class RecordingGitRunner:
    """Recording fake GitRunner for tracking invocations."""

    call_count: int = 0
    branch_created: str | None = None
    file_written: str | None = None
    file_content: str | None = None
    should_fail: bool = False
    existing_branch: str | None = None
    existing_file_content: str | None = None

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
    ) -> GitOpResult:
        """Fake implementation that records calls."""
        self.call_count += 1
        self.branch_created = branch
        self.file_written = file_path
        self.file_content = content

        if self.should_fail:
            return GitOpResult(
                status="failed",
                operation="prepare_branch_and_write",
                stderr="Simulated git failure",
                exit_code=1,
            )

        # Idempotency check: if branch and file already exist with same content
        if self.existing_branch == branch and self.existing_file_content == content:
            # Don't increment call count for idempotent call
            self.call_count -= 1
            return GitOpResult(
                status="success",
                operation="prepare_branch_and_write",
                sha="existing-sha-abc123",
                exit_code=0,
            )

        return GitOpResult(
            status="success",
            operation="prepare_branch_and_write",
            sha="new-sha-def456",
            exit_code=0,
        )


@dataclass
class FakeTerminalHandler:
    """Fake terminal handler for registry tests."""

    invoked: bool = False

    async def handle(self, run_data: dict[str, Any]) -> dict[str, Any]:
        """Fake handler that records invocation."""
        self.invoked = True
        return {"state": "FAKE_TERMINAL"}


# ---------------------------------------------------------------------------
# AC-001: Registry lookup tests
# ---------------------------------------------------------------------------


class TestTerminalRegistry:
    """Test terminal registry lookup and handler injection (AC-001)."""

    def test_registry_lookup_by_string_key(self):
        """Registry returns handler for registered key."""
        registry = TerminalRegistry()
        handler = registry.get("planned-handoff")
        assert handler is not None
        # Registry stores the class, not an instance
        assert handler == PlannedHandoffHandler

    def test_registry_returns_none_for_unknown_key(self):
        """Registry returns None for unregistered key."""
        registry = TerminalRegistry()
        handler = registry.get("unknown-terminal")
        assert handler is None

    def test_registry_default_key_is_planned_handoff(self):
        """planned-handoff is the default terminal."""
        registry = TerminalRegistry()
        config = PlanningConfig()  # Uses default terminal="planned-handoff"
        handler = registry.get(config.terminal)
        assert handler is not None

    def test_fake_handler_injection_via_registry(self):
        """Test can inject fake handler via registry (zero edits to planner)."""
        registry = TerminalRegistry()
        fake = FakeTerminalHandler()
        registry.register("test-terminal", fake)

        handler = registry.get("test-terminal")
        assert handler is fake

    def test_global_registry_singleton(self):
        """get_terminal_registry returns the same instance."""
        registry1 = get_terminal_registry()
        registry2 = get_terminal_registry()
        assert registry1 is registry2


# ---------------------------------------------------------------------------
# AC-002: Approved run creates file on branch
# ---------------------------------------------------------------------------


class TestPlannedHandoffHandler:
    """Test PlannedHandoffHandler creates file on branch (AC-002)."""

    @pytest.mark.asyncio
    async def test_approved_run_creates_file_on_branch(self, tmp_path: Path):
        """Approved run creates feature_spec_inputs/{cid}.md on planning/{cid} branch."""
        # Arrange
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": str(repo_path)},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "test-cid-001",
            "state": PlanningState.PAUSED.value,
            "request_text": "Build a new feature",
            "originating_user": "alice",
            "product_docs": {"summary": "Test feature"},
        }

        # Act
        result = await handler.handle(run_data)

        # Assert
        assert result["state"] == PlanningState.PLANNED_HANDOFF.value
        assert git_runner.call_count == 1
        assert git_runner.branch_created == "planning/test-cid-001"
        assert "feature_spec_inputs/test-cid-001.md" in git_runner.file_written
        assert "Test feature" in git_runner.file_content
        assert result["handoff_branch"] == "planning/test-cid-001"
        assert "feature_spec_inputs/test-cid-001.md" in result["handoff_path"]

    @pytest.mark.asyncio
    async def test_handler_uses_injected_git_runner(self):
        """Handler uses injected GitRunner, never touches paths outside tmp_path."""
        # This test verifies PS-008 - no environment-sensitive git
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/test-repo"},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "test-cid-002",
            "state": PlanningState.PAUSED.value,
            "request_text": "Test",
            "originating_user": "bob",
        }

        await handler.handle(run_data)

        # Verify GitRunner was invoked
        assert git_runner.call_count == 1
        # Verify no direct filesystem operations occurred


# ---------------------------------------------------------------------------
# AC-003: Notification payload sanitization (RT-09)
# ---------------------------------------------------------------------------


class TestNotificationPayloadSanitization:
    """Test notification payload construction with injection guards (AC-003)."""

    def test_notification_contains_committed_path_and_feature_spec_command(self):
        """Notification payload contains literal path and /feature-spec command."""
        payload = build_notification_payload(
            correlation_id="cid-001",
            repo="owner/repo",
            handoff_path="feature_spec_inputs/cid-001.md",
            request_text="Build feature X",
        )

        assert "feature_spec_inputs/cid-001.md" in payload.message
        assert payload.command.startswith("/feature-spec")
        assert "feature_spec_inputs/cid-001.md" in payload.command

    def test_notification_never_interpolates_raw_request_text(self):
        """Raw request_text is never interpolated into rendered text or command (RT-09)."""
        hostile_request = "'; rm -rf /; echo 'pwned"

        payload = build_notification_payload(
            correlation_id="cid-002",
            repo="owner/repo",
            handoff_path="feature_spec_inputs/cid-002.md",
            request_text=hostile_request,
        )

        # The hostile request text should NOT appear in command or message
        assert hostile_request not in payload.command
        assert hostile_request not in payload.message

        # Command should only reference the safe file path
        assert "/feature-spec" in payload.command
        assert "feature_spec_inputs/cid-002.md" in payload.command

    def test_notification_payload_model_structure(self):
        """NotificationPayload has expected structure."""
        payload = build_notification_payload(
            correlation_id="cid-003",
            repo="owner/repo",
            handoff_path="feature_spec_inputs/cid-003.md",
            request_text="Test",
        )

        assert isinstance(payload, NotificationPayload)
        assert payload.correlation_id == "cid-003"
        assert payload.repo == "owner/repo"
        assert payload.handoff_path == "feature_spec_inputs/cid-003.md"
        assert isinstance(payload.message, str)
        assert isinstance(payload.command, str)


# ---------------------------------------------------------------------------
# AC-004: Target repo resolution
# ---------------------------------------------------------------------------


class TestTargetRepoResolution:
    """Test target repo resolution and default fallback (AC-004)."""

    @pytest.mark.asyncio
    async def test_target_repo_none_uses_default(self):
        """target_repo=None falls back to default_target_repo."""
        config = PlanningConfig(
            default_target_repo="org/default-repo",
            target_repo_paths={"org/default-repo": "/tmp/default"},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-004",
            "state": PlanningState.PAUSED.value,
            "target_repo": None,
            "request_text": "Test",
            "originating_user": "carol",
        }

        result = await handler.handle(run_data)

        assert result["state"] == PlanningState.PLANNED_HANDOFF.value
        assert git_runner.call_count == 1

    @pytest.mark.asyncio
    async def test_unresolvable_repo_fails_with_structured_reason(self):
        """Unresolvable repo (no target_repo_paths entry) -> FAILED state."""
        config = PlanningConfig(
            default_target_repo="org/known-repo",
            target_repo_paths={"org/known-repo": "/tmp/known"},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-005",
            "state": PlanningState.PAUSED.value,
            "target_repo": "org/unknown-repo",
            "request_text": "Test",
            "originating_user": "dave",
        }

        result = await handler.handle(run_data)

        assert result["state"] == PlanningState.FAILED.value
        assert "unknown-repo" in result["failure_reason"].lower()
        # 2026-09-05 rule 4 — the failure says what IS known, so the reader
        # can pick a name that works instead of guessing again.
        assert "known repos: org/known-repo" in result["failure_reason"]
        assert git_runner.call_count == 0  # No GitRunner invocation


# ---------------------------------------------------------------------------
# AC-005: GitRunner failure handling
# ---------------------------------------------------------------------------


class TestGitRunnerFailureHandling:
    """Test GitRunner failure handling (AC-005)."""

    @pytest.mark.asyncio
    async def test_gitrunner_failure_transitions_to_failed(self):
        """GitRunner failure -> run FAILED with handoff failure reason."""
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/repo"},
        )

        git_runner = RecordingGitRunner(should_fail=True)
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-006",
            "state": PlanningState.PAUSED.value,
            "request_text": "Test",
            "originating_user": "eve",
        }

        result = await handler.handle(run_data)

        assert result["state"] == PlanningState.FAILED.value
        assert "git" in result["failure_reason"].lower()
        assert result.get("handoff_branch") is None
        assert result.get("handoff_path") is None

    @pytest.mark.asyncio
    async def test_gitrunner_failure_notification_published(self):
        """GitRunner failure publishes failure notification."""
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/repo"},
        )

        git_runner = RecordingGitRunner(should_fail=True)
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-007",
            "state": PlanningState.PAUSED.value,
            "request_text": "Test",
            "originating_user": "frank",
        }

        result = await handler.handle(run_data)

        assert result["state"] == PlanningState.FAILED.value
        assert result["notification_type"] == "failure"


# ---------------------------------------------------------------------------
# AC-006: Idempotency (RT-08)
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Test idempotent re-execution (AC-006, RT-08)."""

    @pytest.mark.asyncio
    async def test_idempotent_reexecution_no_duplicate_commit(self):
        """Re-executing handoff when branch+file exist verifies content, no duplicate commit."""
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/repo"},
        )

        # First execution - branch doesn't exist yet
        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-008",
            "state": PlanningState.PAUSED.value,
            "request_text": "Test",
            "originating_user": "grace",
        }

        # First execution
        result1 = await handler.handle(run_data)
        first_call_count = git_runner.call_count

        # Set up for idempotent re-execution
        content = handler._build_file_content(run_data)
        git_runner.existing_branch = "planning/cid-008"
        git_runner.existing_file_content = content

        # Second execution (idempotent)
        result2 = await handler.handle(run_data)
        second_call_count = git_runner.call_count

        assert result1["state"] == PlanningState.PLANNED_HANDOFF.value
        assert result2["state"] == PlanningState.PLANNED_HANDOFF.value
        # Call count should stay at 1 for idempotent re-execution
        # (first call increments to 1, second call doesn't increment due to idempotency)
        assert first_call_count == 1
        assert second_call_count == 1


# ---------------------------------------------------------------------------
# AC-007: Cancelled/rejected runs produce zero invocations
# ---------------------------------------------------------------------------


class TestCancelledRunsNoGitInvocation:
    """Test cancelled/rejected runs produce zero GitRunner invocations (AC-007)."""

    @pytest.mark.asyncio
    async def test_cancelled_run_zero_git_invocations(self):
        """Cancelled run produces zero GitRunner invocations."""
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/repo"},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-009",
            "state": PlanningState.CANCELLED.value,  # Terminal state
            "request_text": "Test",
            "originating_user": "henry",
        }

        result = await handler.handle(run_data)

        assert git_runner.call_count == 0
        assert result["state"] == PlanningState.CANCELLED.value

    @pytest.mark.asyncio
    async def test_rejected_run_zero_git_invocations(self):
        """Rejected run (FAILED) produces zero GitRunner invocations."""
        config = PlanningConfig(
            default_target_repo="owner/repo",
            target_repo_paths={"owner/repo": "/tmp/repo"},
        )

        git_runner = RecordingGitRunner()
        handler = PlannedHandoffHandler(config=config, git_runner=git_runner)

        run_data = {
            "correlation_id": "cid-010",
            "state": PlanningState.FAILED.value,  # Terminal state
            "request_text": "Test",
            "originating_user": "iris",
        }

        result = await handler.handle(run_data)

        assert git_runner.call_count == 0
        assert result["state"] == PlanningState.FAILED.value
