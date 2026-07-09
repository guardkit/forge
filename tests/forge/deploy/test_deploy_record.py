"""Tests for the F7 deploy-record writer (WS2-B8, scope-design §2 F7)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forge.deploy.deploy_record import (
    DeployAddendum,
    DeployClaim,
    DeployRecord,
    DeployRecordError,
    render_deploy_record,
    write_deploy_record,
)

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _claim(text: str = "svc up", artifact: str = "boot-log:ok") -> DeployClaim:
    return DeployClaim(runtime_claim=text, evidence_artifact=artifact, committed_at=NOW)


class TestRenderValidation:
    """F7 enforcement: no complete record without evidenced claims."""

    def test_no_claims_refused(self) -> None:
        record = DeployRecord(
            env="e",
            date=NOW,
            deployer="s1",
            runbook_ref="rb",
            deploy_profile_ref="deploy/profile.yaml",
            claims=(),
        )
        with pytest.raises(DeployRecordError, match="no claims"):
            render_deploy_record(record)

    def test_claim_without_artifact_refused(self) -> None:
        record = DeployRecord(
            env="e",
            date=NOW,
            deployer="s1",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(
                DeployClaim(runtime_claim="up", evidence_artifact="", committed_at=NOW),
            ),
        )
        with pytest.raises(DeployRecordError, match="no artifact"):
            render_deploy_record(record)


class TestRender:
    def test_header_and_claims_present(self) -> None:
        record = DeployRecord(
            env="fleet-memory-nas",
            date=NOW,
            deployer="deploy-run-1",
            runbook_ref="deploy-run-1",
            deploy_profile_ref="deploy/profile.yaml",
            claims=(_claim("pg reachable", "consumer-info.json"),),
            image_digests={"db": "sha256:abc"},
            dry_run=True,
        )
        md = render_deploy_record(record)
        assert "# Deploy record — fleet-memory-nas (DRY RUN)" in md
        assert "**deployer**: deploy-run-1" in md
        assert "**dry_run**: true" in md
        assert "pg reachable" in md
        assert "consumer-info.json" in md
        assert "db=sha256:abc" in md

    def test_addenda_rendered(self) -> None:
        record = DeployRecord(
            env="e",
            date=NOW,
            deployer="s",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(_claim(),),
            addenda=(
                DeployAddendum(title="rollback", date=NOW, body="ran rollback.sh"),
            ),
        )
        md = render_deploy_record(record)
        assert "### Addendum 1 — rollback" in md
        assert "ran rollback.sh" in md

    def test_pipe_in_claim_escaped(self) -> None:
        record = DeployRecord(
            env="e",
            date=NOW,
            deployer="s",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(_claim("a | b", "x | y"),),
        )
        md = render_deploy_record(record)
        assert "a \\| b" in md


class TestWrite:
    def test_writes_file_and_returns_ref(self, tmp_path: Path) -> None:
        record = DeployRecord(
            env="fleet-memory-nas",
            date=NOW,
            deployer="s",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(_claim(),),
            task_id="WS2-B8",
        )
        ref = write_deploy_record(record, root=tmp_path)
        out = Path(ref)
        assert out.exists()
        assert out.parent.name == "WS2-B8"
        assert out.name == "deploy-record-2026-07-09.md"
        assert "# Deploy record" in out.read_text(encoding="utf-8")

    def test_env_dir_when_no_task(self, tmp_path: Path) -> None:
        record = DeployRecord(
            env="fleet-memory-nas",
            date=NOW,
            deployer="s",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(_claim(),),
        )
        ref = write_deploy_record(record, root=tmp_path)
        assert Path(ref).parent.name == "deploy-fleet-memory-nas"

    def test_incomplete_record_not_written(self, tmp_path: Path) -> None:
        record = DeployRecord(
            env="e",
            date=NOW,
            deployer="s",
            runbook_ref="rb",
            deploy_profile_ref=None,
            claims=(),
        )
        with pytest.raises(DeployRecordError):
            write_deploy_record(record, root=tmp_path)
        # No partial file left behind.
        assert list(tmp_path.rglob("*.md")) == []
