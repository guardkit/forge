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


def test_restart_replays_captured_bytes_not_current_package(chain_store, monkeypatch, tmp_path):
    document = _capture(chain_store)
    # A real reopened SQLite connection, not a retained driver/object cache.
    connection = sqlite3.connect(str(tmp_path / "chain.db"))
    connection.row_factory = sqlite3.Row
    restarted = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    monkeypatch.setattr(context, "load_bundled_document", lambda: context.FactoryDeploymentDocument.of("new install", version="2"))
    assert context.ensure_factory_context(restarted, docs.door.CID) == document
    assert len([e for e in restarted.list_events(docs.door.CID)
                if e["stage_label"] == context.CAPTURE_STAGE]) == 1
    connection.close()


def test_interrupted_capture_recovers_only_matching_package(chain_store, monkeypatch):
    document = _capture(chain_store)
    _remove(chain_store, context.CAPTURE_STAGE)
    changed = context.FactoryDeploymentDocument.of("different installed reference", version="2")
    monkeypatch.setattr(context, "load_bundled_document", lambda: changed)
    assert context.ensure_factory_context(chain_store, docs.door.CID) is None
    assert not any(e["stage_label"] == context.CAPTURE_STAGE for e in chain_store.list_events(docs.door.CID))
    monkeypatch.setattr(context, "load_bundled_document", lambda: document)
    assert context.ensure_factory_context(chain_store, docs.door.CID) == document


@pytest.mark.parametrize("fault", [
    "bad-json", "bad-shape", "bad-text", "bad-size", "bad-hash", "bad-identity",
    "conflicting-capture", "conflicting-expectation", "missing-expectation",
    "missing-capture", "double-loss", "dispatch-before-draft-loss", "conflicting-receipt",
])
@pytest.mark.asyncio
async def test_corruption_and_loss_are_advice_and_the_run_carries_on(chain_store, fault):
    document = _capture(chain_store)
    if fault == "bad-json":
        chain_store._connection.execute("UPDATE planning_run_events SET details_json='{' WHERE stage_label=?", (context.CAPTURE_STAGE,))
        chain_store._connection.commit()
    elif fault in {"bad-shape", "bad-text", "bad-size", "bad-hash", "bad-identity"}:
        record = document.to_record()
        if fault == "bad-shape": record = []
        elif fault == "bad-text": record["text"] += "altered"
        elif fault == "bad-size": record["bytes"] += 1
        elif fault == "bad-hash": record["sha256"] = "0" * 64
        elif fault == "bad-identity": record["document_id"] = "another-document"
        _remove(chain_store, context.CAPTURE_STAGE)
        _event(chain_store, context.CAPTURE_STAGE, {"factory_context": record})
    elif fault == "conflicting-capture":
        changed = context.FactoryDeploymentDocument.of("another valid reference")
        _event(chain_store, context.CAPTURE_STAGE, {"factory_context": changed.to_record()})
    elif fault == "conflicting-expectation":
        _event(chain_store, context.EXPECTATION_STAGE,
               {"factory_context": {**document.receipt(), "sha256": "0" * 64}})
    elif fault == "missing-expectation":
        _remove(chain_store, context.EXPECTATION_STAGE)
    elif fault in {"missing-capture", "double-loss"}:
        _draft_receipt(chain_store, document)
        _remove(chain_store, context.CAPTURE_STAGE)
        if fault == "double-loss":
            _remove(chain_store, context.EXPECTATION_STAGE)
    elif fault == "dispatch-before-draft-loss":
        context.record_factory_dispatch(chain_store, docs.door.CID, document, stage="spec")
        _remove(chain_store, context.CAPTURE_STAGE)
    elif fault == "conflicting-receipt":
        _draft_receipt(chain_store, context.FactoryDeploymentDocument.of("other"))
    h = docs._chain(chain_store, docs._declared_tree(), answers=[docs.door._answer("approve")])
    await h.driver.drive(docs.door.CID)
    # Bookkeeping never ends a run: nothing stops for the factory context.
    # (The faults that plant a spec draft of their own then go on to read
    # that planted draft, whose files are not on any branch.)
    assert not any("factory deployment context" in message for _, message, _ in h.notifications)
    if fault not in {"missing-capture", "double-loss", "conflicting-receipt"}:
        assert chain_store.get_run(docs.door.CID)["state"] != PlanningState.FAILED.value
        assert h.calls["spec"] != []


def test_identical_duplicates_are_not_conflicts(chain_store):
    document = _capture(chain_store)
    _event(chain_store, context.EXPECTATION_STAGE, {"factory_context": document.receipt()})
    _event(chain_store, context.CAPTURE_STAGE, {"factory_context": document.to_record()})
    assert context.ensure_factory_context(chain_store, docs.door.CID) == document


@pytest.mark.asyncio
async def test_legacy_dispatch_keeps_original_context_and_closed_card(chain_store):
    # Run the ordinary chain to obtain its real closed-card/approved receipts,
    # then retain exactly the receipt shape that predates this feature.
    docs.door._queue(chain_store)
    original = docs._chain(chain_store, docs._declared_tree(), answers=[docs.door._answer("approve")])
    await original.driver.drive(docs.door.CID)
    assert chain_store.get_run(docs.door.CID)["state"] == PlanningState.BUILD_QUEUED.value
    _remove(chain_store, context.EXPECTATION_STAGE, context.CAPTURE_STAGE, context.DISPATCH_STAGE)
    for event in chain_store.list_events(docs.door.CID):
        data = json.loads(event["details_json"] or "{}")
        if isinstance(data, dict):
            data.pop("factory_context", None)
            if isinstance(data.get("spec_draft"), dict):
                data["spec_draft"].pop("factory_context", None)
            chain_store._connection.execute("UPDATE planning_run_events SET details_json=? WHERE id=?", (json.dumps(data), event["id"]))
    chain_store._connection.commit()
    assert context.ensure_factory_context(chain_store, docs.door.CID) is None
    assert context.ensure_factory_context(chain_store, docs.door.CID) is None
    legacy = [e for e in chain_store.list_events(docs.door.CID) if e["stage_label"] == context.LEGACY_STAGE]
    assert len(legacy) == 1
    assert json.loads(legacy[0]["details_json"])["disposition"] == "legacy-spec-dispatch-without-factory-context"
    replay = docs._chain(chain_store, docs._declared_tree(), answers=[])
    await replay.driver.drive(docs.door.CID)
    assert replay.calls == {"po": [], "spec": [], "plan": []}
    assert replay.notifications == []


class _PlanReceiptCommitted(BaseException):
    """Process interruption after approval is durable, before build dispatch."""


@pytest.mark.parametrize("fault", [
    "valid", "legacy", "missing-capture", "corrupt-capture",
    "corrupt-expectation", "double-loss",
])
@pytest.mark.asyncio
async def test_approved_plan_restart_validates_context_before_build(
    chain_store, tmp_path, fault,
):
    docs.door._queue(chain_store)
    original = docs._chain(
        chain_store, docs._declared_tree(), answers=[docs.door._answer("approve")],
    )
    builds = []
    dispatch_build = original.driver._deps.dispatch_build_trigger

    async def record_build(**kwargs):
        builds.append(kwargs)
        return await dispatch_build(**kwargs)

    original.driver._deps.dispatch_build_trigger = record_build
    publish = original.driver._deps.publish_notification

    async def interrupt_after_receipt(cid, message, level):
        if "machine spec + plan complete and validated" in message:
            raise _PlanReceiptCommitted()
        await publish(cid, message, level)

    original.driver._deps.publish_notification = interrupt_after_receipt
    with pytest.raises(_PlanReceiptCommitted):
        await original.driver.drive(docs.door.CID)
    assert chain_store.get_run(docs.door.CID)["state"] == PlanningState.FEATURE_PLAN.value
    assert builds == []
    approved = [e for e in chain_store.list_events(docs.door.CID)
                if e["stage_label"] == "feature-plan" and e["status"] == "approved"]
    assert len(approved) == 1
    receipt = json.loads(approved[0]["details_json"])["factory_context"]
    assert receipt == context.load_bundled_document().receipt()
    drafts_before = docs._drafted(chain_store)

    if fault in {"missing-capture", "double-loss"}:
        _remove(chain_store, context.CAPTURE_STAGE)
        if fault == "double-loss":
            _remove(chain_store, context.EXPECTATION_STAGE)
    elif fault == "corrupt-capture":
        document = context.load_bundled_document().to_record()
        document["text"] += "changed after approval"
        _remove(chain_store, context.CAPTURE_STAGE)
        _event(chain_store, context.CAPTURE_STAGE, {"factory_context": document})
    elif fault == "corrupt-expectation":
        chain_store._connection.execute(
            "UPDATE planning_run_events SET details_json='{' WHERE stage_label=?",
            (context.EXPECTATION_STAGE,),
        )
        chain_store._connection.commit()
    elif fault == "legacy":
        _remove(chain_store, context.EXPECTATION_STAGE, context.CAPTURE_STAGE, context.DISPATCH_STAGE)
        for event in chain_store.list_events(docs.door.CID):
            data = json.loads(event["details_json"] or "{}")
            if isinstance(data, dict):
                data.pop("factory_context", None)
                if isinstance(data.get("spec_draft"), dict):
                    data["spec_draft"].pop("factory_context", None)
                chain_store._connection.execute(
                    "UPDATE planning_run_events SET details_json=? WHERE id=?",
                    (json.dumps(data), event["id"]),
                )
        chain_store._connection.commit()
        drafts_before = docs._drafted(chain_store)

    # New SQLite connection and driver start from the real durable boundary.
    connection = sqlite3.connect(str(tmp_path / "chain.db"))
    connection.row_factory = sqlite3.Row
    restarted_store = SqlitePlanningRunStore(connection, target_terminal_enabled=True)
    replay = docs._chain(restarted_store, docs._declared_tree(), answers=[])
    replay.driver._deps.git_runner = original.driver._deps.git_runner
    replay.driver._deps.dispatch_build_trigger = record_build
    try:
        await replay.driver.drive(docs.door.CID)
        assert replay.calls == {"po": [], "spec": [], "plan": []}
        assert docs._drafted(restarted_store) == drafts_before
        # Step 2: every fault is advice; the approved plan goes on to the build.
        assert restarted_store.get_run(docs.door.CID)["state"] == PlanningState.BUILD_QUEUED.value
        assert len(builds) == 1
        if fault in {"valid", "legacy"}:
            if fault == "legacy":
                dispositions = [e for e in restarted_store.list_events(docs.door.CID)
                                if e["stage_label"] == context.LEGACY_STAGE]
                assert len(dispositions) == 1
                assert json.loads(dispositions[0]["details_json"])["disposition"] == "legacy-spec-dispatch-without-factory-context"
    finally:
        connection.close()


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


def test_actual_built_wheel_includes_exact_reference(tmp_path):
    # Build a copy with the real backend, no network/dependencies or mutation
    # of the reviewed checkout. Read the wheel archive, not checkout resources.
    root = Path(__file__).resolve().parents[3]
    build_root = tmp_path / "package"
    build_root.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(root / name, build_root / name)
    shutil.copytree(root / "src", build_root / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    output = tmp_path / "wheel"
    output.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])", str(output)],
        cwd=build_root, env={**os.environ, "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = output.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        raw = archive.read("forge/planning/" + context.RESOURCE_NAME)
    assert raw == context.load_bundled_document().text.encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == context.DOCUMENT_SHA256
