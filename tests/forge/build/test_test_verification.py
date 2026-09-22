"""Unit tests for ``forge.build.test_verification`` (TASK-IC-009 → TASK-F8-001).

The tests inject a fake seam and assert on:

* output parsing (all-pass, mixed-fail, summary-mismatch fallback);
* timeout marker on ``timed_out=True`` and exit-code-124 paths;
* allowlist guard rejecting non-allowlisted binaries;
* identity invariant ``_allowed_binaries_for_test() is ALLOWED_BINARIES``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge.build import test_verification
from forge.build.git_operations import ALLOWED_BINARIES, DisallowedBinaryError
from forge.build.test_verification import (
    TIMEOUT_MARKER,
    _allowed_binaries_for_test,
    verify_tests,
)
# ``TestVerificationResult`` is imported under an alias to avoid pytest's
# ``PytestCollectionWarning`` for a name starting with ``Test`` that has
# a synthesised ``__init__`` (TypedDict). The BDD harness applies the
# same workaround at ``tests/bdd/test_infrastructure_coordination.py``.
from forge.build.test_verification import (
    TestVerificationResult as _TestVerificationResult,
)


@dataclass
class FakeSeam:
    commands: list[tuple[list[str], str, int]] = field(default_factory=list)
    next_results: list[tuple[str, str, int, float, bool]] = field(default_factory=list)
    default_result: tuple[str, str, int, float, bool] = ("", "", 0, 0.01, False)

    def queue(self, result: tuple[str, str, int, float, bool]) -> None:
        self.next_results.append(result)

    async def __call__(
        self, *, command: list[str], cwd: str, timeout: int, **_launch: Any
    ) -> tuple[str, str, int, float, bool]:
        self.commands.append((list(command), cwd, timeout))
        if self.next_results:
            return self.next_results.pop(0)
        return self.default_result


@pytest.fixture
def fake_seam(monkeypatch: pytest.MonkeyPatch) -> FakeSeam:
    seam = FakeSeam()
    monkeypatch.setattr(test_verification, "_execute_via_deepagents", seam)
    return seam


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "worktree"
    wt.mkdir()
    return wt


# --------------------------------------------------------------------------- #
# Allowlist identity (seam-test contract from TASK-IC-010).
# --------------------------------------------------------------------------- #


class TestAllowlistIdentity:
    def test_helper_returns_same_object_as_git_operations(self) -> None:
        # ``is``, not ``==`` — the seam test in TASK-IC-010 §Seam Tests
        # asserts identity to prevent accidental duplicate constants.
        assert _allowed_binaries_for_test() is ALLOWED_BINARIES


# --------------------------------------------------------------------------- #
# verify_tests — happy paths.
# --------------------------------------------------------------------------- #


class TestVerifyTestsHappyPath:
    def test_all_pass_summary_parsed(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("=== 12 passed in 4.20s ===\n", "", 0, 4.20, False))

        result = asyncio.run(verify_tests(worktree))

        assert result["passed"] is True
        assert result["pass_count"] == 12
        assert result["fail_count"] == 0
        assert result["failing_tests"] == []
        assert result["duration_seconds"] == pytest.approx(4.20)

    def test_default_command_is_pytest(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        asyncio.run(verify_tests(worktree))

        cmd, cwd, _timeout = fake_seam.commands[0]
        assert cmd == ["pytest"]
        assert cwd == str(worktree)

    def test_custom_test_command_split_into_argv(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("=== 1 passed in 0.10s ===\n", "", 0, 0.10, False))

        asyncio.run(verify_tests(worktree, test_command="pytest -x --tb=short"))

        cmd, _cwd, _timeout = fake_seam.commands[0]
        assert cmd == ["pytest", "-x", "--tb=short"]

    def test_timeout_seconds_threaded_into_seam(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        asyncio.run(verify_tests(worktree, timeout_seconds=42))

        _cmd, _cwd, timeout = fake_seam.commands[0]
        assert timeout == 42


# --------------------------------------------------------------------------- #
# verify_tests — failure parsing.
# --------------------------------------------------------------------------- #


class TestVerifyTestsFailures:
    def test_failure_summary_parsed(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        stdout = (
            "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
            "FAILED tests/test_b.py::test_two - assert False\n"
            "=== 2 failed, 3 passed in 1.50s ===\n"
        )
        fake_seam.queue((stdout, "", 1, 1.50, False))

        result = asyncio.run(verify_tests(worktree))

        assert result["passed"] is False
        assert result["pass_count"] == 3
        assert result["fail_count"] == 2
        assert "tests/test_a.py::test_one" in result["failing_tests"]
        assert "tests/test_b.py::test_two" in result["failing_tests"]
        assert result["duration_seconds"] == pytest.approx(1.50)

    def test_summary_mismatch_falls_back_to_exit_code(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        # Older pytest / plugin output without the standard summary line.
        fake_seam.queue(("garbage output without summary\n", "", 0, 1.0, False))

        result = asyncio.run(verify_tests(worktree))

        assert result["passed"] is True  # exit_code 0 → authoritative
        assert result["pass_count"] == -1  # signals "counts unavailable"
        assert result["fail_count"] == -1
        assert result["duration_seconds"] == pytest.approx(1.0)  # wall-clock fallback

    def test_summary_mismatch_with_failing_lines_still_extracts_them(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        stdout = (
            "FAILED tests/test_x.py::test_y - boom\n"
            "irrelevant trailing line without summary\n"
        )
        fake_seam.queue((stdout, "", 1, 0.5, False))

        result = asyncio.run(verify_tests(worktree))

        assert result["passed"] is False  # exit_code 1
        assert "tests/test_x.py::test_y" in result["failing_tests"]


# --------------------------------------------------------------------------- #
# verify_tests — timeout.
# --------------------------------------------------------------------------- #


class TestVerifyTestsTimeout:
    def test_timed_out_flag_marks_result(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("", "", 124, 6.0, True))

        result = asyncio.run(verify_tests(worktree, timeout_seconds=5))

        assert result["passed"] is False
        assert TIMEOUT_MARKER in result["failing_tests"]
        assert result["duration_seconds"] == pytest.approx(6.0)

    def test_exit_code_124_alone_marks_timeout(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        # Some seam impls return exit_code=124 without setting the flag.
        fake_seam.queue(("", "", 124, 5.5, False))

        result = asyncio.run(verify_tests(worktree))

        assert result["passed"] is False
        assert TIMEOUT_MARKER in result["failing_tests"]


# --------------------------------------------------------------------------- #
# verify_tests — allowlist guard.
# --------------------------------------------------------------------------- #


class TestVerifyTestsAllowlist:
    def test_disallowed_binary_raises_before_seam(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        with pytest.raises((ValueError, DisallowedBinaryError)):
            asyncio.run(verify_tests(worktree, test_command="rm -rf /"))

        # Seam never invoked.
        assert fake_seam.commands == []

    def test_empty_test_command_raises(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        with pytest.raises(DisallowedBinaryError):
            asyncio.run(verify_tests(worktree, test_command=""))

    def test_relative_worktree_raises(self, fake_seam: FakeSeam) -> None:
        with pytest.raises(ValueError, match="absolute"):
            asyncio.run(verify_tests(Path("relative"), test_command="pytest"))


# --------------------------------------------------------------------------- #
# Output tail.
# --------------------------------------------------------------------------- #


class TestOutputTail:
    def test_output_tail_returns_last_n_chars(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        long_stdout = "x" * 5000 + "\n=== 1 passed in 0.10s ===\n"
        fake_seam.queue((long_stdout, "", 0, 0.10, False))

        result = asyncio.run(verify_tests(worktree, output_tail_chars=100))

        assert len(result["output_tail"]) == 100
        assert result["output_tail"].endswith("=== 1 passed in 0.10s ===\n")

    def test_output_tail_default_is_4000(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        long_stdout = "y" * 6000 + "\n=== 1 passed in 0.10s ===\n"
        fake_seam.queue((long_stdout, "", 0, 0.10, False))

        result = asyncio.run(verify_tests(worktree))

        assert len(result["output_tail"]) == 4000

    def test_output_tail_zero_disables_tail(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("=== 1 passed in 0.10s ===\n", "", 0, 0.10, False))

        result = asyncio.run(verify_tests(worktree, output_tail_chars=0))

        assert result["output_tail"] == ""


# --------------------------------------------------------------------------- #
# Result type sanity.
# --------------------------------------------------------------------------- #


class TestResultShape:
    def test_typed_dict_has_all_six_keys(
        self, fake_seam: FakeSeam, worktree: Path
    ) -> None:
        fake_seam.queue(("=== 1 passed in 0.10s ===\n", "", 0, 0.10, False))

        result: _TestVerificationResult = asyncio.run(verify_tests(worktree))

        assert set(result.keys()) == {
            "passed",
            "pass_count",
            "fail_count",
            "failing_tests",
            "output_tail",
            "duration_seconds",
        }
