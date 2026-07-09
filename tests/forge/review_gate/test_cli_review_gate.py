"""``forge review-gate`` CLI — attended entry, gated on review_gate.enabled."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from forge.cli.review_gate import review_gate_cmd
from forge.config.models import (
    FilesystemPermissions,
    ForgeConfig,
    PermissionsConfig,
    ReviewGateConfig,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dd4f_review_input.json"


def _config(enabled: bool) -> ForgeConfig:
    return ForgeConfig(
        permissions=PermissionsConfig(
            filesystem=FilesystemPermissions(allowlist=["/tmp"])
        ),
        review_gate=ReviewGateConfig(enabled=enabled),
    )


def _write_input(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "input.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


class TestGating:
    def test_disabled_refuses(self, tmp_path):
        inp = _write_input(tmp_path, {"findings": []})
        result = CliRunner().invoke(
            review_gate_cmd,
            ["--feature", "FEAT-X", "--input", str(inp), "--no-validate"],
            obj=_config(enabled=False),
        )
        assert result.exit_code == 1
        assert "disabled" in result.output

    def test_enabled_clean_run(self, tmp_path):
        doc = {
            "findings": [
                {
                    "id": "F-01",
                    "dimension": "correctness",
                    "severity": "low",
                    "summary": "doc nit",
                    "executed_reproduction": "grep confirms",
                }
            ]
        }
        inp = _write_input(tmp_path, doc)
        result = CliRunner().invoke(
            review_gate_cmd,
            [
                "--feature",
                "FEAT-X",
                "--input",
                str(inp),
                "--no-validate",
                "--record-dir",
                str(tmp_path / "qa"),
            ],
            obj=_config(enabled=True),
        )
        assert result.exit_code == 0, result.output
        assert "CLEAN" in result.output


class TestDD4FEndToEnd:
    def test_dd4f_fixture_blocks_with_exit_4(self, tmp_path):
        result = CliRunner().invoke(
            review_gate_cmd,
            [
                "--feature",
                "FEAT-SPL-002",
                "--input",
                str(FIXTURE),
                "--no-validate",
                "--record-dir",
                str(tmp_path / "qa"),
            ],
            obj=_config(enabled=True),
        )
        # DD4F had confirmed CRITICAL/HIGH findings ⇒ BLOCKED (exit 4).
        assert result.exit_code == 4, result.output
        assert "BLOCKED" in result.output
        # The F14 record was written under the record dir.
        written = list((tmp_path / "qa").glob("review-*.yaml"))
        assert len(written) == 1
        assert written[0].name == "review-FEAT-SPL-002-DD4F-postmerge.yaml"

    def test_bad_subject_kind_is_loud_even_without_validate(self, tmp_path):
        # The conformance-review reproduction: a subject.kind outside the F14
        # Literal must fail loud on the --no-validate path too (no silently
        # schema-invalid record written).
        inp = _write_input(
            tmp_path,
            {
                "subject": {"kind": "workingtree", "ref": "a..b"},
                "findings": [
                    {
                        "id": "F-01",
                        "dimension": "correctness",
                        "severity": "low",
                        "summary": "x",
                        "executed_reproduction": "ran",
                    }
                ],
            },
        )
        result = CliRunner().invoke(
            review_gate_cmd,
            [
                "--feature",
                "FEAT-X",
                "--input",
                str(inp),
                "--no-validate",
                "--record-dir",
                str(tmp_path / "qa"),
            ],
            obj=_config(enabled=True),
        )
        assert result.exit_code == 1
        assert "subject.kind" in result.output
        # No record was written for the invalid subject.
        assert not list((tmp_path / "qa").glob("review-*.yaml"))

    def test_bad_input_is_loud(self, tmp_path):
        # A finding asserting its own status is rejected.
        inp = _write_input(
            tmp_path,
            {
                "findings": [
                    {
                        "id": "F-01",
                        "dimension": "correctness",
                        "severity": "low",
                        "summary": "x",
                        "status": "confirmed",
                    }
                ]
            },
        )
        result = CliRunner().invoke(
            review_gate_cmd,
            ["--feature", "FEAT-X", "--input", str(inp), "--no-validate"],
            obj=_config(enabled=True),
        )
        assert result.exit_code == 1
        assert "status" in result.output

    @pytest.mark.skipif(
        shutil.which("guardkit") is None and shutil.which("guardkit-py") is None,
        reason="guardkit CLI not on PATH",
    )
    def test_dd4f_record_validates_via_guardkit(self, tmp_path):
        result = CliRunner().invoke(
            review_gate_cmd,
            [
                "--feature",
                "FEAT-SPL-002",
                "--input",
                str(FIXTURE),
                "--validate",
                "--record-dir",
                str(tmp_path / "qa"),
            ],
            obj=_config(enabled=True),
        )
        assert "F14 record validated" in result.output
        assert result.exit_code == 4  # still BLOCKED, but validation passed
