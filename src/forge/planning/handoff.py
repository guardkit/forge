"""Mode P planned handoff terminal (TASK-MP-006).

This module implements the v1 terminal handler for Mode P planning runs:

- :class:`TerminalRegistry` — string-keyed registry for terminal handlers
- :class:`PlannedHandoffHandler` — the "planned-handoff" terminal implementation
- :class:`GitRunner` — Protocol for injecting git operations
- :func:`build_notification_payload` — sanitized notification builder (RT-09)

Design notes
------------

**Registry indirection (AC-001)**:
The :class:`TerminalRegistry` allows the FEAT-SPL-007/008 target terminal to
replace the v1 handler by configuration, with zero edits to planner/checkpoint
modules. Tests prove this by injecting fake handlers purely via the registry.

**GitRunner injection (AC-002, PS-008)**:
The handler never touches git directly. It receives a :class:`GitRunner`
protocol implementing ``prepare_branch_and_write()``. Tests inject a
recording fake; production wires in ``adapters/git/operations``.

**Idempotency (AC-006, RT-08)**:
Re-executing the handoff when the branch and file already exist verifies
content and proceeds to PLANNED_HANDOFF without a duplicate commit. The
GitRunner implementation handles this by checking for existing branches/files.

**Sanitized notifications (AC-003, RT-09)**:
Notification payloads are constructed ONLY from validated components
(repo, path, correlation_id). Raw ``request_text`` is never interpolated
into the rendered text or copy-pasteable command, preventing injection attacks.

**Order (AC-006)**:
commit → record → notify, with notify best-effort (DDR-007).

References
----------
- TASK-MP-006 — this implementation
- FEAT-SPL-002 — Mode P planning workflow
- RT-08 — idempotent re-execution requirement
- RT-09 — injection guard requirement
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from forge.adapters.git.models import GitOpResult
from forge.config.models import PlanningConfig
from forge.planning.states import PlanningState
from forge.planning.target_repos import format_known_repos

logger = logging.getLogger(__name__)

__all__ = [
    "GitRunner",
    "NotificationPayload",
    "PlannedHandoffHandler",
    "PreCommitHook",
    "PreCommitResult",
    "TerminalRegistry",
    "build_feature_spec_input_content",
    "build_notification_payload",
    "get_terminal_registry",
]


def build_feature_spec_input_content(run_data: dict[str, Any]) -> str:
    """Render the ``feature_spec_inputs/<id>.md`` markdown for a planning run.

    Mirrors specialist-agent's ``feature_spec_inputs/<id>.md`` shape: a
    minimal v1 with the product docs + originator + the raw request. This is
    the deterministic input BOTH the flag-OFF PLANNED_HANDOFF terminal
    (:meth:`PlannedHandoffHandler._build_file_content`) and the flag-ON
    Lane B target terminal write to the branch and feed to the
    ``po_feature_spec`` (007) leg — extracted to a module function so the two
    paths can never drift (the 007 input must be byte-identical to what the
    fallback handoff commits).

    Deterministic in ``run_data`` — the same run always renders the same
    bytes, which is what makes the target-terminal spec leg re-entrant.
    """
    correlation_id = run_data["correlation_id"]
    originator = run_data.get("originating_user", "unknown")
    request_text = run_data.get("request_text", "")
    product_docs = run_data.get("product_docs", {})

    content = f"""# Feature Spec Input: {correlation_id}

**Originator**: {originator}

**Request**: {request_text}

## Product Documentation

"""
    if product_docs:
        for key, value in product_docs.items():
            content += f"**{key}**: {value}\n\n"
    else:
        content += "_No product documentation provided._\n"

    return content


# ---------------------------------------------------------------------------
# GitRunner Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreCommitResult:
    """Outcome of a target-terminal pre-commit hook (Lane B / Phase E1 B2).

    A hook (the gherkin normalizer for the spec leg, ``guardkit feature
    validate`` for the plan leg) runs against the materialised worktree
    AFTER forge writes the artifacts but BEFORE the commit lands. ``ok=False``
    aborts the commit and surfaces ``detail`` as the loud-failure reason —
    a red oracle never reaches the branch.
    """

    ok: bool
    detail: str = ""


#: A pre-commit hook: given the materialised worktree path, run an oracle
#: (normalizer / validate) against the on-disk artifacts and return whether
#: the commit may proceed. May mutate files in place (the normalizer collapses
#: wrapped gherkin steps), and those edits are included in the commit.
PreCommitHook = Callable[[Path], Awaitable[PreCommitResult]]


class GitRunner(Protocol):
    """Protocol for git operations injected into the handoff handler.

    Production implementations wrap ``forge.adapters.git.operations``.
    Tests inject recording fakes to verify invocation counts and arguments.
    """

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
    ) -> GitOpResult:
        """Prepare a worktree branch and write a file with content.

        Parameters
        ----------
        repo_path:
            Absolute path to the repository's working copy.
        branch:
            Branch name (e.g., "planning/correlation-id").
        file_path:
            Relative path within the repo (e.g., "feature_spec_inputs/cid.md").
        content:
            File content to write.

        Returns
        -------
        GitOpResult:
            status="success" with sha on success, status="failed" otherwise.
        """
        ...

    async def prepare_branch_and_write_tree(
        self,
        repo_path: str,
        branch: str,
        files: Mapping[str, str],
        message: str,
        *,
        pre_commit: PreCommitHook | None = None,
    ) -> GitOpResult:
        """Write a MULTI-file tree onto ``branch`` in one commit (Lane B B2).

        The additive multi-file sibling of :meth:`prepare_branch_and_write`,
        for the target-terminal spec/plan legs which write the three-file spec
        contract and the plan tree under ``features/<slug>/``. Semantics:

        - ``files`` maps repo-relative paths → content; all are written into an
          isolated worktree of ``branch`` (created if absent, re-attached if a
          prior leg already advanced it — the spec triple and the plan tree
          land on the SAME ``planning/<cid>`` branch across the two legs).
        - ``pre_commit`` (optional) runs against the worktree path after the
          writes but before the commit; ``ok=False`` aborts the commit with
          ZERO branch mutation and surfaces ``detail`` (the normalizer/validate
          red path). The hook may rewrite files in place (normalizer collapse);
          those edits are committed.
        - Idempotent: if every file is already byte-identical on the branch and
          the hook passes, returns success with the existing tip and no commit.
        - Never raises (ADR-ARCH-025): failures become
          ``GitOpResult(status="failed", ...)``.

        Parameters
        ----------
        repo_path:
            Absolute path to the target repository's working copy.
        branch:
            Branch name (e.g. ``"planning/<correlation-id>"``).
        files:
            Mapping of repo-relative path → file content.
        message:
            Commit message.
        pre_commit:
            Optional oracle hook (normalizer / ``feature validate``).

        Returns
        -------
        GitOpResult:
            status="success" with sha on success, status="failed" otherwise.
        """
        ...

    async def read_file_from_branch(
        self, *, repo_path: str, branch: str, file_path: str
    ) -> str | None:
        """Return the content of ``file_path`` on ``branch`` (None if absent).

        Lane B (008 leg): the plan leg reads the committed spec triple back off
        the ``planning/<cid>`` branch so it can thread the ``.feature`` /
        ``_summary.md`` / ``_assumptions.yaml`` CONTENTS (not paths) into
        ``architect_feature_plan`` — correct on an idempotent re-drive where the
        spec contents are no longer in memory. Never raises (ADR-ARCH-025); a
        missing file or a read failure returns ``None``.
        """
        ...


# ---------------------------------------------------------------------------
# Notification Payload
# ---------------------------------------------------------------------------


class NotificationPayload(BaseModel):
    """Sanitized notification payload for handoff completion (RT-09).

    All fields are validated at construction. The ``command`` field contains
    a copy-pasteable command string that references only the committed file
    path — raw ``request_text`` is never interpolated.
    """

    correlation_id: str
    repo: str
    handoff_path: str
    message: str
    command: str


def build_notification_payload(
    correlation_id: str,
    repo: str,
    handoff_path: str,
    request_text: str,
) -> NotificationPayload:
    """Build sanitized notification payload (RT-09 injection guard).

    The notification contains:
    - The literal committed file path
    - A ``/feature-spec`` command referencing the file
    - NO interpolation of raw ``request_text`` into command or message

    Parameters
    ----------
    correlation_id:
        Planning run correlation ID.
    repo:
        Target repository identifier (e.g., "owner/repo").
    handoff_path:
        Relative path to the committed file.
    request_text:
        Original planning request (NOT interpolated into output).

    Returns
    -------
    NotificationPayload:
        Sanitized payload safe for NATS publication.
    """
    # Build message from validated components only
    message = (
        f"Planning complete for {correlation_id}. "
        f"Feature spec inputs committed to {repo} at {handoff_path}."
    )

    # Build command referencing only the safe file path
    command = f"/feature-spec {handoff_path}"

    return NotificationPayload(
        correlation_id=correlation_id,
        repo=repo,
        handoff_path=handoff_path,
        message=message,
        command=command,
    )


# ---------------------------------------------------------------------------
# PlannedHandoffHandler
# ---------------------------------------------------------------------------


class PlannedHandoffHandler:
    """Terminal handler for Mode P planned handoff (v1).

    Resolves the target repo → prepares branch ``planning/{correlation_id}``
    → writes ``feature_spec_inputs/{correlation_id}.md`` → commits (no push)
    → transitions PLANNED_HANDOFF → publishes notification.

    Idempotent (RT-08): re-executing when branch+file exist verifies content
    and proceeds without duplicate commit.
    """

    def __init__(self, config: PlanningConfig, git_runner: GitRunner) -> None:
        """Initialize handler with config and injected GitRunner.

        Parameters
        ----------
        config:
            Planning configuration with target_repo_paths mapping.
        git_runner:
            Injected GitRunner protocol for git operations.
        """
        self._config = config
        self._git_runner = git_runner

    async def handle(self, run_data: dict[str, Any]) -> dict[str, Any]:
        """Execute the planned handoff terminal action.

        Parameters
        ----------
        run_data:
            Planning run data including correlation_id, state, request_text,
            originating_user, target_repo (optional), product_docs (optional).

        Returns
        -------
        dict:
            Updated run data with state, handoff_branch, handoff_path,
            notification_type, and (if failed) failure_reason.
        """
        correlation_id = run_data["correlation_id"]
        current_state = run_data["state"]

        # AC-007: Cancelled/rejected runs produce zero GitRunner invocations
        if current_state in {
            PlanningState.CANCELLED.value,
            PlanningState.FAILED.value,
            PlanningState.TIMED_OUT.value,
            PlanningState.PLANNED_HANDOFF.value,
        }:
            logger.info(
                "Skipping handoff for terminal state %s (correlation_id=%s)",
                current_state,
                correlation_id,
            )
            return {**run_data, "state": current_state}

        # AC-004: Resolve target repo (fallback to default)
        target_repo = run_data.get("target_repo") or self._config.default_target_repo
        if target_repo is None:
            logger.error(
                "No target repo specified and no default configured (correlation_id=%s)",
                correlation_id,
            )
            return {
                **run_data,
                "state": PlanningState.FAILED.value,
                "failure_reason": "No target repository configured",
                "notification_type": "failure",
            }

        # AC-004: Resolve repo path from config
        repo_path = self._config.target_repo_paths.get(target_repo)
        if repo_path is None:
            known = format_known_repos(self._config.target_repo_paths)
            logger.error(
                "Target repo %s not in target_repo_paths (correlation_id=%s); "
                "known repos: %s",
                target_repo,
                correlation_id,
                known,
            )
            return {
                **run_data,
                "state": PlanningState.FAILED.value,
                "failure_reason": (
                    f"Target repository {target_repo} not found in "
                    f"configuration; known repos: {known}"
                ),
                "notification_type": "failure",
            }

        # AC-002: Prepare branch and write file
        branch = f"planning/{correlation_id}"
        file_path = f"feature_spec_inputs/{correlation_id}.md"
        content = self._build_file_content(run_data)

        try:
            result = await self._git_runner.prepare_branch_and_write(
                repo_path=repo_path,
                branch=branch,
                file_path=file_path,
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception(
                "GitRunner raised exception (correlation_id=%s)", correlation_id
            )
            return {
                **run_data,
                "state": PlanningState.FAILED.value,
                "failure_reason": f"Git operation failed: {type(exc).__name__}: {exc}",
                "notification_type": "failure",
            }

        # AC-005: GitRunner failure handling
        if result.status == "failed":
            logger.error(
                "GitRunner failed: %s (correlation_id=%s)",
                result.stderr,
                correlation_id,
            )
            return {
                **run_data,
                "state": PlanningState.FAILED.value,
                "failure_reason": f"Git operation failed: {result.stderr}",
                "notification_type": "failure",
            }

        # AC-002: Success - record handoff details and transition
        handoff_path = f"{repo_path}/{file_path}"

        # AC-003: Build sanitized notification
        notification = build_notification_payload(
            correlation_id=correlation_id,
            repo=target_repo,
            handoff_path=file_path,
            request_text=run_data.get("request_text", ""),
        )

        logger.info(
            "Handoff complete: branch=%s, path=%s (correlation_id=%s)",
            branch,
            file_path,
            correlation_id,
        )

        return {
            **run_data,
            "state": PlanningState.PLANNED_HANDOFF.value,
            "handoff_branch": branch,
            "handoff_path": handoff_path,
            "notification_type": "success",
            "notification_payload": notification.model_dump(),
        }

    def _build_file_content(self, run_data: dict[str, Any]) -> str:
        """Build file content from run data.

        Mirrors specialist-agent's feature_spec_inputs/<id>.md shape:
        minimal v1 with product docs + originator + approval provenance.

        Parameters
        ----------
        run_data:
            Planning run data.

        Returns
        -------
        str:
            Markdown content for feature spec input file.
        """
        return build_feature_spec_input_content(run_data)


# ---------------------------------------------------------------------------
# Terminal Registry
# ---------------------------------------------------------------------------


class TerminalRegistry:
    """Registry mapping terminal names to handler instances.

    Enables FEAT-SPL-007/008 target terminal replacement by configuration
    with zero edits to planner/checkpoint modules (AC-001).
    """

    def __init__(self) -> None:
        """Initialize registry with v1 planned-handoff handler."""
        self._handlers: dict[str, Any] = {}
        # Register planned-handoff as a factory function
        # Actual instances are created with injected GitRunner
        self._handlers["planned-handoff"] = PlannedHandoffHandler

    def register(self, name: str, handler: Any) -> None:
        """Register a terminal handler.

        Parameters
        ----------
        name:
            Terminal name (matches PlanningConfig.terminal).
        handler:
            Handler instance with async handle(run_data) method.
        """
        self._handlers[name] = handler

    def get(self, name: str) -> Any | None:
        """Look up a terminal handler by name.

        Parameters
        ----------
        name:
            Terminal name (from PlanningConfig.terminal).

        Returns
        -------
        handler or None:
            Registered handler or None if not found.
        """
        return self._handlers.get(name)


# Singleton registry instance
_TERMINAL_REGISTRY: TerminalRegistry | None = None


def get_terminal_registry() -> TerminalRegistry:
    """Get the global terminal registry singleton.

    Returns
    -------
    TerminalRegistry:
        The singleton registry instance.
    """
    global _TERMINAL_REGISTRY  # noqa: PLW0603 — singleton pattern
    if _TERMINAL_REGISTRY is None:
        _TERMINAL_REGISTRY = TerminalRegistry()
        # Register v1 planned-handoff handler
        # Note: In production, this would be wired with a real GitRunner
        # For now, we just register the class itself
        # Actual handler instances are created with injected GitRunner
    return _TERMINAL_REGISTRY
