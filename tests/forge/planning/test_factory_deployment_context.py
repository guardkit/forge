"""Factory-owned interface evidence survives restart; record loss is advice
(step 2, 26 October 2026, census FRG-049)."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest
import yaml

from forge.planning import factory_deployment_context as context
from forge.planning.run_store import SqlitePlanningRunStore
from forge.planning.states import PlanningState
from tests.forge.planning import test_project_documents_for_the_writers as docs
from tests.forge.planning.test_project_documents_for_the_writers import chain_store  # noqa: F401


def _event(store, stage, payload, *, status="recorded"):
    store._record_event(correlation_id=docs.door.CID, stage_label=stage,
                        status=status, details_json=json.dumps(payload))


def _remove(store, *stages):
    for stage in stages:
        store._connection.execute("DELETE FROM planning_run_events WHERE correlation_id=? AND stage_label=?",
                                  (docs.door.CID, stage))
    store._connection.commit()


def _capture(store):
    docs.door._queue(store)
    document = context.ensure_factory_context(store, docs.door.CID)
    assert document is not None
    return document


def _draft_receipt(store, document):
    _event(store, "feature-spec-draft", {"spec_draft": {
        "sha": "a" * 40, "spec_files": ["features/sample.feature"],
        "factory_context": document.receipt(),
    }}, status="drafted")



class _PlanReceiptCommitted(BaseException):
    """Process interruption after approval is durable, before build dispatch."""



def test_reference_profile_has_real_first_deployment_declarations(tmp_path):
    from forge.deploy.profile import load_deploy_profile, parse_deploy_profile
    from forge.pipeline.deployment_identity import declared_identity
    from forge.pipeline.merge_executor import DEPLOY_PROFILE_PATH, first_deployment_profile_is_usable

    document = context.load_bundled_document()
    assert DEPLOY_PROFILE_PATH in document.text
    example = document.text.split("```yaml\n", 1)[1].split("```", 1)[0]
    path = tmp_path / DEPLOY_PROFILE_PATH
    path.parent.mkdir()
    path.write_text(example)
    profile = load_deploy_profile(path)
    assert first_deployment_profile_is_usable(profile)
    identity = declared_identity(profile)
    assert identity.declared and identity.asked_with == "RUNNING_IDENTITY"
    assert identity.marker == "DEPLOYED_IDENTITY" and identity.running_as == "RUNNING_IDENTITY"
    assert identity.checked_as == "CHECKED_ARTIFACT" and identity.artifact_setting == "DEPLOY_ARTIFACT"
    raw = yaml.safe_load(example)
    for missing in ("identity", "candidate", "live_gate"):
        negative = copy.deepcopy(raw)
        del negative[missing]
        accepted_by_loader = parse_deploy_profile(negative)
        assert not first_deployment_profile_is_usable(accepted_by_loader)
    raw["identity"] = {}
    assert not first_deployment_profile_is_usable(parse_deploy_profile(raw))
    minimal = parse_deploy_profile({"env_id": "sample-service", "compose": {"file": "deploy/compose.yaml"}})
    assert not first_deployment_profile_is_usable(minimal)


def test_reference_names_are_anchored_to_installed_handlers():
    from forge.deploy import live_gate, stage
    from forge.executor import shell_steps
    from forge.pipeline import deployment_identity as identity
    from forge.pipeline import merge_executor
    text = context.load_bundled_document().text
    for name in ("DEFAULT_SETTING_NAME", "DEFAULT_REPORT_MARKER", "DEFAULT_CHECKED_MARKER",
                 "DEFAULT_ARTIFACT_SETTING", "DEFAULT_RUNNING_MARKER", "NOTHING_IS_RUNNING", "UNKNOWN_SUFFIX"):
        assert getattr(identity, name) in text
    for flag in ("CANDIDATE", "PROMOTE", "CANDIDATE_DOWN"):
        assert flag in text and flag in inspect.getsource(stage)
    for flag in ("REVERT", "ROLLBACK_IMAGE_REF", "ENV_FILE"):
        assert flag in text and flag in inspect.getsource(shell_steps)
    for flag in ("--feature", "--target", "--gates"):
        assert flag in text and flag in inspect.getsource(live_gate.RepoDriverLiveGateInvoker.invoke)
    # Production calls the shared predicate rather than leaving a test-only facsimile.
    assert "if not first_deployment_profile_is_usable(profile):" in inspect.getsource(merge_executor)
    assert (
        "It does not request a new feature, override a prohibition, or enlarge an unrelated feature."
        in " ".join(text.split())
    )


