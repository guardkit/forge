"""F14 record emission — shape + optional cross-seam validation (WS3-S5)."""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from forge.launch_environment import launch_setting_names
from forge.review_gate.assembler import assemble_review_findings
from forge.review_gate.models import RawFinding, RefuterVote, ReviewSubject
from forge.review_gate.record import (
    F14_KIND,
    GuardkitValidatorUnavailable,
    render_review_findings,
    validate_review_findings,
    write_review_findings,
)

SUBJ = ReviewSubject(kind="merge", ref="abc..def")


def _record():
    return assemble_review_findings(
        review_id="rec-test",
        subject=SUBJ,
        dimensions=("correctness", "spec-fidelity"),
        raw_findings=(
            RawFinding(
                id="F-01",
                dimension="correctness",
                severity="critical",
                summary="wrong kwargs",
                executed_reproduction="ran, TypeError",
                refuter_votes=(
                    RefuterVote("r1", "not_refuted", "could not refute"),
                    RefuterVote("r2", "not_refuted"),
                ),
            ),
            RawFinding(
                id="F-02",
                dimension="spec-fidelity",
                severity="low",
                summary="doc deviation",
                executed_reproduction="grep confirms",
            ),
        ),
    )


class TestRenderShape:
    def test_yaml_matches_f14_field_names(self):
        text = render_review_findings(_record())
        doc = yaml.safe_load(text)
        assert set(doc) == {
            "format_version",
            "review_id",
            "subject",
            "dimensions",
            "findings",
            "stats",
        }
        assert doc["format_version"] == "1.0"
        assert set(doc["subject"]) == {"kind", "ref"}
        assert set(doc["stats"]) == {
            "findings_total",
            "confirmed",
            "refuted",
            "refutations_attempted",
        }

    def test_confirmed_finding_carries_executed_reproduction(self):
        doc = yaml.safe_load(render_review_findings(_record()))
        f01 = next(f for f in doc["findings"] if f["id"] == "F-01")
        assert f01["status"] == "confirmed"
        assert f01["executed_reproduction"]
        assert len(f01["refuters"]) == 2

    def test_no_extra_keys_on_findings(self):
        # guardkit F14 is extra='forbid'; a stray key would fail validation.
        doc = yaml.safe_load(render_review_findings(_record()))
        allowed = {
            "id",
            "dimension",
            "severity",
            "status",
            "summary",
            "executed_reproduction",
            "refuters",
        }
        for f in doc["findings"]:
            assert set(f) <= allowed
        # A finding with no refuters/repro omits those keys rather than null.
        f02 = next(f for f in doc["findings"] if f["id"] == "F-02")
        assert "refuters" not in f02

    def test_optional_note_omitted_when_absent(self):
        doc = yaml.safe_load(render_review_findings(_record()))
        f01 = next(f for f in doc["findings"] if f["id"] == "F-01")
        r2 = next(r for r in f01["refuters"] if r["who"] == "r2")
        assert "note" not in r2


class TestWrite:
    def test_writes_expected_filename(self, tmp_path):
        ref = write_review_findings(_record(), root=tmp_path)
        assert ref.endswith("review-rec-test.yaml")
        assert (tmp_path / "review-rec-test.yaml").exists()

    def test_creates_missing_dir(self, tmp_path):
        nested = tmp_path / "qa" / "reviews"
        write_review_findings(_record(), root=nested)
        assert (nested / "review-rec-test.yaml").exists()


class TestValidateSeam:
    def test_absent_validator_is_loud(self, tmp_path, monkeypatch):
        ref = write_review_findings(_record(), root=tmp_path)
        # No guardkit binary resolvable ⇒ raise, never a silent pass.
        monkeypatch.setattr("forge.review_gate.record._resolve_guardkit", lambda: None)
        with pytest.raises(GuardkitValidatorUnavailable):
            validate_review_findings(ref)

    def test_kind_is_review_findings(self):
        assert F14_KIND == "review-findings"

    def test_the_validator_is_launched_with_the_named_list(self, tmp_path, monkeypatch):
        """This spawn passed no ``env=`` at all, and an omitted ``env=``
        inherits the whole environment of the process that made it — here the
        coordinator's own, an operator's shell and all. It takes the one
        written-down list now (the design pass of 2026-09-21, item 1, second
        revision, section D).

        Nothing is started: the spawn itself is replaced.
        """
        ref = write_review_findings(_record(), root=tmp_path)
        monkeypatch.setattr(
            "forge.review_gate.record._resolve_guardkit", lambda: "/opt/bin/guardkit"
        )
        monkeypatch.setenv("PATH", "/opt/venv/bin:/usr/bin")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GH_TOKEN", "a-credential-nobody-should-see")
        monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "forge.db"))
        seen: dict = {}

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr("forge.review_gate.record.subprocess.run", _fake_run)

        result = validate_review_findings(ref)

        assert result.ok
        env = seen["env"]
        assert env is not None, "an omitted env= is the whole environment"
        assert set(env) <= set(launch_setting_names())
        assert env["PATH"] == "/opt/venv/bin:/usr/bin"
        assert "GH_TOKEN" not in env
        assert "FORGE_DB_PATH" not in env

    @pytest.mark.skipif(
        shutil.which("guardkit") is None and shutil.which("guardkit-py") is None,
        reason="guardkit CLI not on PATH",
    )
    def test_live_guardkit_accepts_the_emitted_record(self, tmp_path):
        ref = write_review_findings(_record(), root=tmp_path)
        result = validate_review_findings(ref)
        assert result.ok, f"guardkit rejected the F14 record: {result.stderr}"
