"""Unit tests for :mod:`forge.adapters.gh.operations`.

Covers TASK-GCI-007 acceptance criteria:

- AC-001: ``create_pr`` lives in ``src/forge/adapters/gh/operations.py`` and
  returns :class:`PRResult`.
- AC-002: Missing/empty ``GH_TOKEN`` returns
  ``PRResult(status="failed", error_code="missing_credentials",
  stderr="GH_TOKEN not set in environment")`` **without invoking gh**
  (Scenario "A pull-request creation without GitHub credentials returns a
  structured error").
- AC-003: With credentials present, invokes
  ``gh pr create --title <t> --body <b> --base <base>`` (plus ``--draft``
  if requested) via the underlying execute primitive with ``cwd =
  worktree``.
- AC-004: On success, parses the PR URL from gh's stdout and populates
  ``PRResult.pr_url`` and ``PRResult.pr_number``.
- AC-005: On non-zero exit, returns ``PRResult(status="failed",
  stderr=...)``, no exception.
- AC-006: Function body wrapped in ``try/except Exception`` returning a
  ``failed`` ``PRResult`` (ADR-ARCH-025) — never raises.
- AC-007: Arguments containing shell metacharacters (backticks, dollar
  signs) are passed as separate list tokens (Scenario "Shell
  metacharacters in subprocess arguments are passed as literal tokens").

The seam tests below mock the underlying ``_execute`` primitive rather
than spawning the real ``gh`` binary (avoids network + auth dependencies
in CI). The missing-credential branch is asserted to never reach the
mock.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from forge.adapters.gh import operations
from forge.adapters.gh.operations import create_pr
from forge.adapters.git.models import PRResult


pytestmark = [
    pytest.mark.integration_contract("gh_adapter_subprocess_contract"),
]


def _patch_execute(mock: AsyncMock):
    """Convenience: patch ``operations._execute`` with the supplied mock."""

    return patch.object(operations, "_execute", mock)


def _extract_command(mock: AsyncMock) -> list[str]:
    """Pull the ``command`` list out of the most recent ``_execute`` call."""

    args, kwargs = mock.call_args
    if "command" in kwargs:
        return list(kwargs["command"])
    return list(args[0])


def _extract_cwd(mock: AsyncMock) -> str:
    args, kwargs = mock.call_args
    if "cwd" in kwargs:
        return kwargs["cwd"]
    # positional fallback: (command, cwd, ...)
    return args[1]


class TestCreatePrMissingCredentials:
    """AC-002 — pre-flight check rejects missing/empty GH_TOKEN."""

    @pytest.mark.asyncio
    @pytest.mark.seam
    async def test_missing_gh_token_returns_missing_credentials_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        execute_mock = AsyncMock()

        with _patch_execute(execute_mock):
            result = await create_pr(
                worktree=Path("/var/forge/builds/B1"),
                title="t",
                body="b",
            )

        assert isinstance(result, PRResult)
        assert result.status == "failed"
        assert result.error_code == "missing_credentials"
        assert result.stderr == "GH_TOKEN not set in environment"
        assert result.pr_url is None
        assert result.pr_number is None
        # AC-002: gh must not be invoked when the credential is missing.
        execute_mock.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.seam
    async def test_empty_gh_token_is_treated_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An explicit empty string is just as bad as unset — gh would
        # otherwise fall through to its interactive auth prompt.
        monkeypatch.setenv("GH_TOKEN", "")
        execute_mock = AsyncMock()

        with _patch_execute(execute_mock):
            result = await create_pr(
                worktree=Path("/var/forge/builds/B1"),
                title="t",
                body="b",
            )

        assert result.status == "failed"
        assert result.error_code == "missing_credentials"
        execute_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_gh_token_is_re_read_on_every_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Implementation note: do NOT cache GH_TOKEN at import time —
        # re-check on every call. Verify by toggling the env between
        # invocations.
        monkeypatch.delenv("GH_TOKEN", raising=False)
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/1\n", "")
        )

        with _patch_execute(execute_mock):
            first = await create_pr(Path("/wt"), "t", "b")
            assert first.error_code == "missing_credentials"

            monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
            second = await create_pr(Path("/wt"), "t", "b")
            assert second.status == "success"


class TestCreatePrSuccessPath:
    """AC-003 / AC-004 — happy path command shape and URL parsing."""

    @pytest.mark.asyncio
    async def test_success_parses_pr_url_and_number_from_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        stdout = (
            "Creating pull request for feature into main in owner/repo\n"
            "https://github.com/owner/repo/pull/123\n"
        )
        execute_mock = AsyncMock(return_value=(0, stdout, ""))

        with _patch_execute(execute_mock):
            result = await create_pr(
                worktree=Path("/var/forge/builds/B1"),
                title="Feature",
                body="Body",
            )

        assert result.status == "success"
        assert result.pr_url == "https://github.com/owner/repo/pull/123"
        assert result.pr_number == 123
        assert result.error_code is None
        assert result.stderr is None

    @pytest.mark.asyncio
    async def test_default_command_shape_uses_separate_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/9\n", "")
        )

        with _patch_execute(execute_mock):
            await create_pr(
                worktree=Path("/var/forge/builds/B1"),
                title="My Title",
                body="My Body",
            )

        cmd = _extract_command(execute_mock)
        assert cmd == [
            "gh",
            "pr",
            "create",
            "--title",
            "My Title",
            "--body",
            "My Body",
            "--base",
            "main",
        ]
        assert _extract_cwd(execute_mock) == "/var/forge/builds/B1"

    @pytest.mark.asyncio
    async def test_draft_flag_appends_dash_dash_draft(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/9\n", "")
        )

        with _patch_execute(execute_mock):
            await create_pr(
                worktree=Path("/wt"),
                title="t",
                body="b",
                base="develop",
                draft=True,
            )

        cmd = _extract_command(execute_mock)
        assert "--draft" in cmd
        # --base must reflect the override, not the default "main".
        assert "--base" in cmd
        assert cmd[cmd.index("--base") + 1] == "develop"

    @pytest.mark.asyncio
    async def test_pr_number_parsed_from_trailing_slash_component(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        # Multiline stdout — gh sometimes prints progress before the URL.
        stdout = (
            "Validating credentials...\n"
            "Detected base branch: main\n"
            "https://github.com/owner-foo/repo-bar/pull/4567\n"
        )
        execute_mock = AsyncMock(return_value=(0, stdout, ""))

        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")

        assert result.pr_number == 4567
        assert result.pr_url == "https://github.com/owner-foo/repo-bar/pull/4567"


class TestCreatePrFailurePath:
    """AC-005 / AC-006 — non-zero exit and exception handling."""

    @pytest.mark.asyncio
    async def test_non_zero_exit_returns_failed_with_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(
            return_value=(1, "", "fatal: not a git repository\n")
        )

        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")

        assert result.status == "failed"
        assert result.error_code is None  # only set for credential failures
        assert "not a git repository" in (result.stderr or "")
        assert result.pr_url is None
        assert result.pr_number is None

    @pytest.mark.asyncio
    async def test_execute_exception_is_caught_and_returned_as_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(side_effect=RuntimeError("transport boom"))

        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")

        # ADR-ARCH-025: the adapter must NEVER raise.
        assert result.status == "failed"
        assert result.error_code is None
        assert "RuntimeError" in (result.stderr or "")
        assert "transport boom" in (result.stderr or "")

    @pytest.mark.asyncio
    async def test_success_exit_without_url_returns_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # gh exited 0 but emitted no recognisable PR URL — that's a
        # contract violation by gh; we surface it as ``failed`` rather
        # than fabricating a success.
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(return_value=(0, "no url here\n", ""))

        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")

        assert result.status == "failed"
        assert result.pr_url is None
        assert result.pr_number is None


class TestCreatePrShellMetacharacters:
    """AC-007 — shell metacharacters in args are passed as literal tokens."""

    @pytest.mark.asyncio
    async def test_backticks_in_body_pass_through_literal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        body_with_backticks = "Run `rm -rf /` to test"
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/1\n", "")
        )

        with _patch_execute(execute_mock):
            await create_pr(Path("/wt"), title="t", body=body_with_backticks)

        cmd = _extract_command(execute_mock)
        # The body must appear as a single, separate list token, not
        # spliced into a shell string.
        assert body_with_backticks in cmd
        idx = cmd.index(body_with_backticks)
        assert cmd[idx - 1] == "--body"

    @pytest.mark.asyncio
    async def test_dollar_signs_in_body_pass_through_literal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        body_with_dollars = "Set $HOME and $PATH explicitly"
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/1\n", "")
        )

        with _patch_execute(execute_mock):
            await create_pr(Path("/wt"), title="t", body=body_with_dollars)

        cmd = _extract_command(execute_mock)
        assert body_with_dollars in cmd
        idx = cmd.index(body_with_dollars)
        assert cmd[idx - 1] == "--body"

    @pytest.mark.asyncio
    async def test_metacharacters_in_title_pass_through_literal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        title_with_meta = "Fix `bug` in $service && rm tmp"
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/1\n", "")
        )

        with _patch_execute(execute_mock):
            await create_pr(Path("/wt"), title=title_with_meta, body="b")

        cmd = _extract_command(execute_mock)
        assert title_with_meta in cmd
        idx = cmd.index(title_with_meta)
        assert cmd[idx - 1] == "--title"


class TestCreatePrReturnContract:
    """AC-001 — return type is PRResult on every code path."""

    @pytest.mark.asyncio
    async def test_return_type_is_prresult_on_missing_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        result = await create_pr(Path("/wt"), "t", "b")
        assert isinstance(result, PRResult)

    @pytest.mark.asyncio
    async def test_return_type_is_prresult_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(
            return_value=(0, "https://github.com/o/r/pull/1\n", "")
        )
        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")
        assert isinstance(result, PRResult)

    @pytest.mark.asyncio
    async def test_return_type_is_prresult_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(return_value=(1, "", "boom"))
        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")
        assert isinstance(result, PRResult)

    @pytest.mark.asyncio
    async def test_return_type_is_prresult_on_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_abcdef")
        execute_mock = AsyncMock(side_effect=ValueError("nope"))
        with _patch_execute(execute_mock):
            result = await create_pr(Path("/wt"), "t", "b")
        assert isinstance(result, PRResult)
