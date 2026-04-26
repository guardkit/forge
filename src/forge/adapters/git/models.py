"""Pydantic v2 result DTOs for the git/gh adapters.

These models are the **declarative producer** for the GuardKit Command
Invocation Engine feature (FEAT-FORGE-005). They describe the shapes
returned by the git adapter (TASK-GCI-006) and the gh adapter
(TASK-GCI-007), and are consumed by the tool wrappers
(TASK-GCI-009 / TASK-GCI-010).

Per ``docs/design/contracts/API-subprocess.md`` §4 (git/gh adapter return
contract — never raises past the adapter boundary, ADR-ARCH-025), every
git or gh subprocess invocation is funnelled into one of these structured
results: ``status="success"`` for the happy path and ``status="failed"``
for any non-zero exit, missing credential, or transport error. Adapters
must convert exceptions into ``status="failed"`` instances rather than
propagating them past the adapter boundary.

Notes
-----

- Pydantic v2 — keep declarative, no validators or business logic.
- ``status`` is a ``Literal[...]`` (no ``Enum``) per the project's
  declarative-DTO style.
- Optional fields explicitly default to ``None`` so JSON round-tripping
  via ``model_dump_json()`` / ``model_validate_json()`` is symmetric.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitOpResult(BaseModel):
    """Structured result of a single git adapter operation.

    The ``operation`` field identifies which adapter call produced the
    result. Currently defined operation labels:

    - ``"prepare_worktree"`` — sets ``worktree_path`` on success.
    - ``"commit_all"`` — sets ``sha`` on success.
    - ``"push"`` — sets ``sha`` on success (the SHA pushed).
    - ``"cleanup_worktree"`` — neither ``sha`` nor ``worktree_path`` is
      populated.

    On ``status="failed"``, ``stderr`` carries the captured subprocess
    stderr (truncated by the adapter if necessary) and ``exit_code``
    holds the non-zero return code.
    """

    status: Literal["success", "failed"]
    operation: str = Field(
        ...,
        description=(
            "Adapter operation label: "
            '"prepare_worktree" | "commit_all" | "push" | "cleanup_worktree".'
        ),
    )
    sha: str | None = Field(
        default=None,
        description="Commit SHA (commit/push operations only).",
    )
    worktree_path: str | None = Field(
        default=None,
        description="Absolute path of the prepared worktree (prepare_worktree only).",
    )
    stderr: str | None = Field(
        default=None,
        description="Captured stderr (populated on failure).",
    )
    exit_code: int = Field(
        ...,
        description="Subprocess exit code (0 on success, non-zero on failure).",
    )


class PRResult(BaseModel):
    """Structured result of a ``gh pr create`` adapter call.

    ``error_code`` is a stable, machine-readable failure tag the tool
    layer can branch on without parsing ``stderr``. Known values:

    - ``"missing_credentials"`` — the gh CLI reported no authenticated
      account (BDD scenario "A pull-request creation without GitHub
      credentials returns a structured error"). This is the only
      error_code currently emitted by the adapter; additional values
      may be added as new failure modes are catalogued.

    On ``status="success"``, both ``pr_url`` and ``pr_number`` are
    populated and ``error_code``/``stderr`` remain ``None``. On
    ``status="failed"``, ``pr_url`` and ``pr_number`` remain ``None``
    and at least one of ``error_code`` / ``stderr`` is populated.
    """

    status: Literal["success", "failed"]
    pr_url: str | None = Field(
        default=None,
        description="Full URL of the created pull request (success only).",
    )
    pr_number: int | None = Field(
        default=None,
        description="Numeric pull request number (success only).",
    )
    error_code: str | None = Field(
        default=None,
        description=(
            'Stable failure tag. Known values: "missing_credentials" '
            "(no GitHub authentication available)."
        ),
    )
    stderr: str | None = Field(
        default=None,
        description="Captured stderr (populated on failure).",
    )
