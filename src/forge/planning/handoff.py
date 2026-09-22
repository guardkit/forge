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
from dataclasses import dataclass, field
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
    "PRE_COMMIT_CHECK_NAMES",
    "PreCommitCheck",
    "PreCommitCheckOutcome",
    "PreCommitChecks",
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


# ---------------------------------------------------------------------------
# Declared pre-commit checks (sandbox first, 2026-09-07, rule 70)
#
# Rich's rule: nothing the factory runs on a repository runs on the host. A
# Python closure can only run where the driver runs, so a git runner that
# lives in the repository's sandbox cannot take one. Instead the driver
# DECLARES the checks by name, with their arguments, and the sandbox's own
# guardkit runs them against the worktree it materialised; the outcomes come
# back with the commit and the driver reads them through the same parsers it
# always used, so what a refusal means does not change.
# ---------------------------------------------------------------------------

#: The checks a sandbox git runner knows how to run, by name. Between them
#: they are every check the planning chain's four writing legs run before a
#: commit — the plan leg's two, the spec leg's two, the pass-bar leg's one
#: (declared once per bar) and the feature-gate leg's one — so every leg can
#: declare instead of handing over a Python function (rule 87, 2026-09-07):
#:
#: * ``normalize-stamps``    — ``guardkit qa normalize-stamps --feature <id>
#:   --repo <worktree> [--no-model]``; args ``feature_id`` and ``no_model``.
#:   Blocks the commit (when declared blocking) exactly when the driver's own
#:   hook stops the run: a partial / refused / failed normalizer.
#: * ``feature-validate``    — ``guardkit feature validate <id> --json``;
#:   args ``feature_id``. Blocks on a non-zero exit.
#: * ``classify-scenarios``  — ``guardkit qa classify-scenarios --feature-file
#:   <path> --repo <worktree> --json``; args ``feature_file``. Never blocks.
#: * ``normalize-feature``   — the gherkin normalizer the spec leg runs over
#:   the committed ``.feature`` (``python -m <the normalizer module>
#:   <worktree>/<path>``, resolved the way
#:   :func:`~forge.planning.target_terminal_tools.resolve_normalizer_command`
#:   resolves it); args ``feature_file``. Blocks on a non-zero exit.
#: * ``validate-pass-bar``   — ``guardkit qa validate pass-bar <path>``; args
#:   ``bar_file``, declared once per minted bar. Blocks on a non-zero exit.
#: * ``validate-gate-registry`` — ``guardkit qa validate gate-registry
#:   <path>``; args ``registry_file``. Blocks on a non-zero exit.
PRE_COMMIT_CHECK_NAMES: tuple[str, ...] = (
    "normalize-stamps",
    "feature-validate",
    "classify-scenarios",
    "normalize-feature",
    "validate-pass-bar",
    "validate-gate-registry",
)


@dataclass(frozen=True)
class PreCommitCheck:
    """One check to run in the worktree before the commit, named.

    ``blocking`` says whether a failing verdict refuses the commit. The
    driver sets it from the routing law's enforcement for the stamp
    normalizer (an unenforced repository proceeds past a refusal, as its
    closure does) and always for ``feature validate``; ``classify-scenarios``
    never blocks whatever is declared.
    """

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    blocking: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "args": dict(self.args), "blocking": self.blocking}


@dataclass(frozen=True)
class PreCommitChecks:
    """The declared list of checks, in the order they run. The first blocking
    failure stops the list and refuses the commit; the checks after it are
    reported as not run."""

    checks: tuple[PreCommitCheck, ...] = ()

    def to_wire(self) -> list[dict[str, Any]]:
        return [check.to_wire() for check in self.checks]


@dataclass(frozen=True)
class PreCommitCheckOutcome:
    """What one declared check did, as the sandbox reported it.

    ``ran`` is false for a check the sidecar never started because an earlier
    blocking check failed. ``passed`` is the check's own verdict (the same
    rule the driver's closure applied); ``blocking`` says whether that verdict
    could refuse the commit. ``stdout`` is the check's whole output (the JSON
    the parsers read); ``stderr_tail`` the end of its stderr. ``detail`` is
    the verdict in one sentence, in the words the driver's closure would have
    used. ``note`` carries one plain sentence when the sidecar had to do
    something the caller should know about (it ran the normalizer again
    without ``--no-model`` because the installed guardkit has no such option,
    or it repaired a task document's front matter before validating).
    """

    name: str
    blocking: bool
    ran: bool
    passed: bool
    exit_code: int
    stdout: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    detail: str = ""
    note: str = ""

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "PreCommitCheckOutcome":
        exit_code = raw.get("exit_code")
        return cls(
            name=str(raw.get("name") or ""),
            blocking=bool(raw.get("blocking")),
            ran=bool(raw.get("ran")),
            passed=bool(raw.get("passed")),
            exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else -1,
            stdout=str(raw.get("stdout") or ""),
            stderr_tail=str(raw.get("stderr_tail") or ""),
            timed_out=bool(raw.get("timed_out")),
            detail=str(raw.get("detail") or ""),
            note=str(raw.get("note") or ""),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "blocking": self.blocking,
            "ran": self.ran,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr_tail": self.stderr_tail,
            "timed_out": self.timed_out,
            "detail": self.detail,
            "note": self.note,
        }


class GitRunner(Protocol):
    """Protocol for git operations injected into the handoff handler.

    Production implementations wrap ``forge.adapters.git.operations``.
    Tests inject recording fakes to verify invocation counts and arguments.
    """

    async def fetch_remote_start_point(self, repo_path: str) -> Any:
        """Fetch the copy's remote ``origin`` and say where its default branch is.

        The starting rule's one operation (one true copy, item 1,
        2026-09-21). The answer is a
        :class:`~forge.deploy.candidate_tree.RemoteStartPoint`: a branch and a
        commit, or one plain sentence saying why there is nothing to start
        from. It never raises, never changes a checked-out branch and never
        touches a working folder.
        """
        ...

    async def prepare_branch_and_write(
        self,
        repo_path: str,
        branch: str,
        file_path: str,
        content: str,
        *,
        start_commit: str | None = None,
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
        start_commit:
            The commit a BRAND NEW branch is cut from (one true copy, item 1)
            — the commit the remote's default branch was at when the work
            started. A branch that already exists is re-attached and never
            moved onto it. ``None`` keeps the old behaviour: the branch is cut
            from whatever the copy has checked out.

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
        pre_commit: "PreCommitHook | PreCommitChecks | None" = None,
        start_commit: str | None = None,
    ) -> GitOpResult:
        """Write a MULTI-file tree onto ``branch`` in one commit (Lane B B2).

        Since 2026-09-07 (sandbox first, rule 70) ``pre_commit`` may also be
        a :class:`PreCommitChecks` declaration: a runner that lives in the
        repository's sandbox runs the named checks with its own guardkit and
        answers with their outcomes on the result (``checks``); the
        in-container :class:`~forge.adapters.git.planning_runner.WorktreeGitRunner`
        keeps taking the closure. A runner says which form it takes through
        ``supports_declared_checks()`` (absent = closures only).

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
        start_commit:
            The commit a BRAND NEW branch is cut from (one true copy, item 1).
            An existing branch is re-attached, never moved.

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
