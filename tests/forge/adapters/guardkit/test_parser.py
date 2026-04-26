"""Unit tests for :mod:`forge.adapters.guardkit.parser` (TASK-GCI-004).

Test classes mirror the acceptance criteria for the tolerant
``parse_guardkit_output()`` parser:

- AC-001 — ``parse_guardkit_output()`` is exported from
  ``src/forge/adapters/guardkit/parser.py``.
- AC-002 — ``timed_out=True`` always yields ``status="timeout"``,
  regardless of ``exit_code``.
- AC-003 — ``timed_out=False, exit_code != 0`` yields ``status="failed"``
  and preserves ``stderr``.
- AC-004 — ``timed_out=False, exit_code == 0`` with a recognised stdout
  shape yields ``status="success"`` with ``artefacts``, ``coach_score``,
  ``criterion_breakdown`` and ``detection_findings`` populated.
- AC-005 — ``timed_out=False, exit_code == 0`` with an unrecognised
  stdout shape yields ``status="success"``, ``artefacts=[]``, and
  raises no exception.
- AC-006 — ``stdout_tail`` is the *last* 4 KB of stdout when stdout is
  larger than 4 KB; preserved verbatim when smaller.
- AC-007 — Tail boundary is byte-based and safe for multi-byte UTF-8
  (decoded with ``errors="ignore"`` on the leading remainder).
- AC-008 — Internal parse errors are caught and surfaced as
  ``GuardKitWarning(code="parser_unrecognised_shape")``; they never
  propagate as exceptions.

The parser is **pure**: it only depends on its inputs and never imports
the subprocess wrapper.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest import mock

import pytest

from forge.adapters.guardkit import parser as parser_module
from forge.adapters.guardkit.models import GuardKitResult, GuardKitWarning
from forge.adapters.guardkit.parser import parse_guardkit_output


def _call(
    *,
    subcommand: str = "feature-spec",
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    duration_secs: float = 1.0,
    timed_out: bool = False,
) -> GuardKitResult:
    """Invoke the parser with sensible defaults."""
    return parse_guardkit_output(
        subcommand=subcommand,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_secs=duration_secs,
        timed_out=timed_out,
    )


class TestParserModuleSurface:
    """AC-001 — The parser is defined where the contract says it is."""

    def test_parser_module_path_is_under_guardkit_adapter(self) -> None:
        module_file = inspect.getsourcefile(parser_module) or ""
        assert module_file.endswith("forge/adapters/guardkit/parser.py")

    def test_parse_function_is_defined_in_parser_module(self) -> None:
        assert parse_guardkit_output.__module__ == "forge.adapters.guardkit.parser"

    def test_parser_module_does_not_import_subprocess_wrapper(self) -> None:
        # The parser must be a pure function on its inputs (Implementation
        # Notes — "Do not import the subprocess wrapper here").
        # Inspect compiled imports rather than raw source so docstring
        # mentions of the word "subprocess" do not generate false positives.
        import ast

        source = inspect.getsource(parser_module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "subprocess" not in alias.name.split(
                        "."
                    ), f"parser must not import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "subprocess" not in node.module.split(
                    "."
                ), f"parser must not import from {node.module}"


class TestTimeoutTakesPrecedenceOverExitCode:
    """AC-002 — ``timed_out=True`` → ``status="timeout"`` regardless of exit."""

    @pytest.mark.parametrize("exit_code", [0, 1, -9, 124])
    def test_timed_out_true_always_yields_timeout(self, exit_code: int) -> None:
        result = _call(
            subcommand="feature-spec",
            stdout="some-output",
            stderr="killed",
            exit_code=exit_code,
            timed_out=True,
        )
        assert result.status == "timeout"

    def test_timeout_preserves_subcommand_exit_code_and_duration(self) -> None:
        result = _call(
            subcommand="autobuild",
            stdout="x",
            stderr="killed",
            exit_code=-9,
            duration_secs=601.5,
            timed_out=True,
        )
        assert result.subcommand == "autobuild"
        assert result.exit_code == -9
        assert result.duration_secs == 601.5

    def test_timeout_preserves_stderr(self) -> None:
        result = _call(
            stdout="",
            stderr="process killed by SIGKILL",
            exit_code=-9,
            timed_out=True,
        )
        assert result.stderr == "process killed by SIGKILL"


class TestNonZeroExitYieldsFailedWithStderr:
    """AC-003 — Non-zero exit → ``status="failed"`` with stderr preserved."""

    def test_non_zero_exit_yields_failed(self) -> None:
        result = _call(
            stdout="",
            stderr="boom",
            exit_code=2,
            timed_out=False,
        )
        assert result.status == "failed"

    def test_non_zero_exit_preserves_stderr_verbatim(self) -> None:
        stderr = "Error: context manifest missing\nTraceback...\n"
        result = _call(
            stdout="",
            stderr=stderr,
            exit_code=1,
            timed_out=False,
        )
        assert result.stderr == stderr

    @pytest.mark.parametrize("exit_code", [1, 2, 64, 127, 255])
    def test_any_non_zero_exit_is_failed(self, exit_code: int) -> None:
        result = _call(exit_code=exit_code, stderr="x")
        assert result.status == "failed"
        assert result.exit_code == exit_code


class TestRecognisedShapeYieldsSuccessWithArtefacts:
    """AC-004 — Recognised shape populates artefacts / coach_score / etc."""

    def test_artefacts_section_extracted_from_stdout(self) -> None:
        stdout = (
            "## Artefacts\n"
            "- /var/forge/builds/abc/spec.md\n"
            "- /var/forge/builds/abc/plan.md\n"
            "\n"
        )
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.artefacts == [
            "/var/forge/builds/abc/spec.md",
            "/var/forge/builds/abc/plan.md",
        ]

    def test_coach_score_line_extracted(self) -> None:
        stdout = "Some preamble\ncoach_score: 0.83\nMore output\n"
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.coach_score == pytest.approx(0.83)

    def test_coach_breakdown_table_extracted(self) -> None:
        stdout = (
            "## Coach Breakdown\n"
            "| Criterion | Score |\n"
            "|-----------|-------|\n"
            "| clarity | 0.5 |\n"
            "| completeness | 0.7 |\n"
            "\n"
        )
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.criterion_breakdown == {
            "clarity": 0.5,
            "completeness": 0.7,
        }

    def test_detection_findings_json_block_extracted(self) -> None:
        findings = [
            {"rule": "R1", "severity": "high"},
            {"rule": "R2", "severity": "low"},
        ]
        stdout = (
            "## Detection Findings\n" "```json\n" f"{json.dumps(findings)}\n" "```\n"
        )
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.detection_findings == findings

    def test_full_recognised_shape_populates_all_optional_fields(self) -> None:
        stdout = (
            "Pipeline complete.\n"
            "coach_score: 0.91\n"
            "## Artefacts\n"
            "- /var/forge/builds/zzz/out.md\n"
            "## Coach Breakdown\n"
            "| Criterion | Score |\n"
            "|-----------|-------|\n"
            "| precision | 0.9 |\n"
            "## Detection Findings\n"
            "```json\n"
            '[{"rule":"R3","severity":"medium"}]\n'
            "```\n"
        )
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.artefacts == ["/var/forge/builds/zzz/out.md"]
        assert result.coach_score == pytest.approx(0.91)
        assert result.criterion_breakdown == {"precision": 0.9}
        assert result.detection_findings == [{"rule": "R3", "severity": "medium"}]


class TestUnrecognisedShapeDegradesToSuccessEmpty:
    """AC-005 — Unknown shape → success, empty artefacts, no raise."""

    def test_completely_unknown_shape_yields_success_empty_artefacts(
        self,
    ) -> None:
        stdout = "??? not a guardkit output ???"
        result = _call(stdout=stdout)
        assert result.status == "success"
        assert result.artefacts == []

    def test_empty_stdout_yields_success_empty(self) -> None:
        result = _call(stdout="")
        assert result.status == "success"
        assert result.artefacts == []
        assert result.coach_score is None
        assert result.criterion_breakdown is None
        assert result.detection_findings is None

    def test_unknown_shape_does_not_raise(self) -> None:
        # Random bytes / arbitrary text must not crash the parser.
        try:
            _call(stdout="\x00\x01\x02 binary noise \xff\xfe")
        except Exception as exc:  # pragma: no cover — defensive
            pytest.fail(f"parser raised on unknown shape: {exc!r}")


class TestStdoutTailTruncation:
    """AC-006 + AC-007 — ``stdout_tail`` is the last 4 KB, byte-safe."""

    def test_small_stdout_preserved_verbatim(self) -> None:
        stdout = "compact output line 1\ncompact output line 2\n"
        result = _call(stdout=stdout)
        assert result.stdout_tail == stdout

    def test_large_stdout_truncated_to_last_4096_bytes(self) -> None:
        # Build a stdout much larger than 4 KB; the tail must be the
        # *last* 4096 bytes (after byte-slicing).
        big = "A" * 10_000
        result = _call(stdout=big)
        # Because all bytes are ASCII single-byte, the tail must be
        # exactly 4096 'A' characters.
        assert result.stdout_tail == "A" * 4096

    def test_tail_is_taken_from_the_end_not_the_start(self) -> None:
        # Head + leading marker + filler + trailing marker. We assert the
        # tail contains the *trailing* marker (proving we kept the end)
        # and excludes the leading marker (proving we discarded the start).
        leading_marker = "HEAD-MARKER"
        trailing_marker = "TAIL-MARKER"
        # 10_000 X's keeps the leading marker safely outside the 4 KB tail.
        stdout = leading_marker + ("X" * 10_000) + trailing_marker
        result = _call(stdout=stdout)
        assert trailing_marker in result.stdout_tail
        assert leading_marker not in result.stdout_tail
        # And the tail must end with the trailing marker.
        assert result.stdout_tail.endswith(trailing_marker)

    def test_multibyte_utf8_tail_is_byte_safe(self) -> None:
        # Pad with a multi-byte character (3 bytes per char in UTF-8) so
        # the 4096-byte boundary lands inside a code point. The parser
        # must not raise; the leading remainder may be truncated by the
        # ``errors="ignore"`` decode of the boundary remainder.
        em_dash = "—"  # 3 bytes in UTF-8
        # 2000 em-dashes = 6000 bytes — comfortably above 4096.
        stdout = em_dash * 2000
        try:
            result = _call(stdout=stdout)
        except UnicodeDecodeError as exc:  # pragma: no cover
            pytest.fail(f"parser raised UnicodeDecodeError: {exc!r}")
        # The tail must decode cleanly back to a string and be at most
        # 4096 bytes when re-encoded.
        assert isinstance(result.stdout_tail, str)
        assert len(result.stdout_tail.encode("utf-8")) <= 4096
        # Every surviving character must be the em-dash — i.e. no
        # mojibake from the boundary.
        for ch in result.stdout_tail:
            assert ch == em_dash, "boundary slice produced corrupt characters"

    def test_exactly_4096_byte_stdout_preserved_verbatim(self) -> None:
        stdout = "Z" * 4096
        result = _call(stdout=stdout)
        assert result.stdout_tail == stdout


class TestInternalErrorsBecomeWarningsNotRaises:
    """AC-008 — Internal parse errors → warning, never raised."""

    def test_malformed_json_in_detection_findings_yields_warning(
        self,
    ) -> None:
        stdout = "## Detection Findings\n" "```json\n" "{not valid json,\n" "```\n"
        result = _call(stdout=stdout)
        # Status still success — tolerant by design.
        assert result.status == "success"
        # detection_findings must remain None — we couldn't parse it.
        assert result.detection_findings is None
        # And we must surface the failure as a structured warning.
        codes = [w.code for w in result.warnings]
        assert "parser_unrecognised_shape" in codes

    def test_internal_exception_caught_and_returned_as_warning(self) -> None:
        # Force an internal failure by patching one of the helpers the
        # parser is expected to call. The function must still return a
        # GuardKitResult — never re-raise.
        target = "forge.adapters.guardkit.parser._extract_artefacts"
        try:
            with mock.patch(target, side_effect=RuntimeError("boom")):
                result = _call(stdout="## Artefacts\n- /tmp/x\n")
        except AttributeError:
            pytest.skip(
                "parser does not expose _extract_artefacts hook; "
                "covered indirectly by malformed-JSON test"
            )
        except RuntimeError:
            pytest.fail("parser must catch internal exceptions, not re-raise")
        assert isinstance(result, GuardKitResult)
        assert result.status == "success"
        codes = [w.code for w in result.warnings]
        assert "parser_unrecognised_shape" in codes

    def test_warning_includes_exception_message_in_details(self) -> None:
        stdout = "## Detection Findings\n" "```json\n" "{not valid json,\n" "```\n"
        result = _call(stdout=stdout)
        unrecognised: list[GuardKitWarning] = [
            w for w in result.warnings if w.code == "parser_unrecognised_shape"
        ]
        assert unrecognised, "expected parser_unrecognised_shape warning"
        # Either the message or details should describe the failure.
        warning = unrecognised[0]
        descriptive = warning.message or json.dumps(warning.details)
        assert descriptive, "warning must carry diagnostic context"


class TestResultFieldsAlwaysPopulated:
    """Cross-cutting — every return path produces a fully-formed result."""

    def test_subcommand_round_trips(self) -> None:
        result = _call(subcommand="quality-coach")
        assert result.subcommand == "quality-coach"

    def test_duration_round_trips(self) -> None:
        result = _call(duration_secs=42.5)
        assert result.duration_secs == 42.5

    def test_exit_code_round_trips_on_success_path(self) -> None:
        result = _call(exit_code=0)
        assert result.exit_code == 0

    def test_exit_code_round_trips_on_timeout_path(self) -> None:
        result = _call(exit_code=-9, timed_out=True)
        assert result.exit_code == -9

    def test_default_timed_out_is_false(self) -> None:
        # Calling without timed_out must work — it defaults to False.
        result = parse_guardkit_output(
            subcommand="feature-spec",
            stdout="",
            stderr="",
            exit_code=0,
            duration_secs=1.0,
        )
        assert result.status == "success"

    def test_warnings_default_to_empty_list_on_clean_input(self) -> None:
        result = _call(stdout="## Artefacts\n- /tmp/x\n")
        assert result.warnings == []
