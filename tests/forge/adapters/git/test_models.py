"""Declarative-schema tests for ``forge.adapters.git.models``.

Covers TASK-GCI-002 acceptance criteria:

- AC-001: GitOpResult and PRResult live in ``src/forge/adapters/git/models.py``.
- AC-003: ``status`` fields are ``Literal[...]``, not ``Enum``.
- AC-004: All optional fields explicitly default to ``None``.
- AC-005: ``forge.adapters.git`` re-exports both models.
- AC-006: ``PRResult.error_code`` documents ``"missing_credentials"`` as a
  known value.
- AC-007: ``model_dump_json()`` round-trips through
  ``model_validate_json()`` for both models.
"""

from __future__ import annotations

import enum
from typing import Literal, get_args, get_origin

import pytest
from pydantic import ValidationError

from forge.adapters.git import GitOpResult as ReexportedGitOpResult
from forge.adapters.git import PRResult as ReexportedPRResult
from forge.adapters.git.models import GitOpResult, PRResult


class TestGitOpResultDefinition:
    """AC-001 / AC-003 / AC-004 / AC-005 — GitOpResult schema shape."""

    def test_module_path(self) -> None:
        # AC-001: model is sourced from the canonical path.
        assert GitOpResult.__module__ == "forge.adapters.git.models"

    def test_status_is_literal_not_enum(self) -> None:
        # AC-003: ``status`` is a typing.Literal, not an Enum subclass.
        annotation = GitOpResult.model_fields["status"].annotation
        assert get_origin(annotation) is Literal
        assert set(get_args(annotation)) == {"success", "failed"}
        assert not (isinstance(annotation, type) and issubclass(annotation, enum.Enum))

    def test_optional_fields_default_to_none(self) -> None:
        # AC-004: every optional field is explicitly None by default.
        for name in ("sha", "worktree_path", "stderr"):
            assert GitOpResult.model_fields[name].default is None, name

    def test_required_fields_have_no_default(self) -> None:
        # ``status``, ``operation``, and ``exit_code`` are required.
        for name in ("status", "operation", "exit_code"):
            assert GitOpResult.model_fields[name].is_required(), name

    def test_reexported_from_package_init(self) -> None:
        # AC-005: re-export shim works.
        assert ReexportedGitOpResult is GitOpResult


class TestPRResultDefinition:
    """AC-001 / AC-003 / AC-004 / AC-005 / AC-006 — PRResult schema shape."""

    def test_module_path(self) -> None:
        # AC-001: model is sourced from the canonical path.
        assert PRResult.__module__ == "forge.adapters.git.models"

    def test_status_is_literal_not_enum(self) -> None:
        # AC-003: ``status`` is a typing.Literal, not an Enum subclass.
        annotation = PRResult.model_fields["status"].annotation
        assert get_origin(annotation) is Literal
        assert set(get_args(annotation)) == {"success", "failed"}
        assert not (isinstance(annotation, type) and issubclass(annotation, enum.Enum))

    def test_optional_fields_default_to_none(self) -> None:
        # AC-004: every optional field is explicitly None by default.
        for name in ("pr_url", "pr_number", "error_code", "stderr"):
            assert PRResult.model_fields[name].default is None, name

    def test_status_is_only_required_field(self) -> None:
        # All non-status fields are optional with None defaults.
        assert PRResult.model_fields["status"].is_required()
        for name in ("pr_url", "pr_number", "error_code", "stderr"):
            assert not PRResult.model_fields[name].is_required(), name

    def test_reexported_from_package_init(self) -> None:
        # AC-005: re-export shim works.
        assert ReexportedPRResult is PRResult

    def test_error_code_documents_missing_credentials(self) -> None:
        # AC-006: the docstring / field description must mention
        # ``missing_credentials`` so consumers can branch on it.
        description = PRResult.model_fields["error_code"].description or ""
        docstring = PRResult.__doc__ or ""
        assert (
            "missing_credentials" in description
            or "missing_credentials" in docstring
        )


class TestGitOpResultBehaviour:
    """Construction + validation + JSON round-trip."""

    def test_minimal_success_construction(self) -> None:
        result = GitOpResult(status="success", operation="commit_all", exit_code=0)
        assert result.status == "success"
        assert result.sha is None
        assert result.worktree_path is None
        assert result.stderr is None

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GitOpResult(  # type: ignore[arg-type]
                status="weird", operation="push", exit_code=0
            )

    def test_json_round_trip_success(self) -> None:
        # AC-007: model_dump_json -> model_validate_json round-trip.
        original = GitOpResult(
            status="success",
            operation="prepare_worktree",
            worktree_path="/tmp/wt",
            exit_code=0,
        )
        rebuilt = GitOpResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_json_round_trip_failure(self) -> None:
        # AC-007: failure-shaped instances also round-trip.
        original = GitOpResult(
            status="failed",
            operation="push",
            stderr="fatal: remote rejected push",
            exit_code=128,
        )
        rebuilt = GitOpResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original


class TestPRResultBehaviour:
    """Construction + validation + JSON round-trip."""

    def test_minimal_success_construction(self) -> None:
        result = PRResult(status="success", pr_url="https://x/y/pull/1", pr_number=1)
        assert result.status == "success"
        assert result.error_code is None
        assert result.stderr is None

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PRResult(status="pending")  # type: ignore[arg-type]

    def test_json_round_trip_success(self) -> None:
        # AC-007: model_dump_json -> model_validate_json round-trip.
        original = PRResult(
            status="success",
            pr_url="https://github.com/x/y/pull/42",
            pr_number=42,
        )
        rebuilt = PRResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_json_round_trip_missing_credentials(self) -> None:
        # AC-006 + AC-007: the documented "missing_credentials" tag
        # round-trips cleanly through the wire shape.
        original = PRResult(
            status="failed",
            error_code="missing_credentials",
            stderr="gh: not authenticated",
        )
        rebuilt = PRResult.model_validate_json(original.model_dump_json())
        assert rebuilt == original
        assert rebuilt.error_code == "missing_credentials"
