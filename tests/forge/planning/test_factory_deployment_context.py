"""The factory's bundled deployment reference: its example profile is a usable
first deployment, and the names it gives are the ones the installed handlers
use. (Its receipts and restart checks went in the cull, 27 October 2026.)"""
from __future__ import annotations

import copy
import inspect

import yaml

from forge.planning import factory_deployment_context as context


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


