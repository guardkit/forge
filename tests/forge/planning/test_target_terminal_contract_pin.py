"""Contract-pin: the exact wire arg-name sets each target-terminal leg sends.

These are LITERAL assertions of the argument names forge puts on the wire for
the 007 / 008 specialist legs. They exist because a stubbed hermetic round-trip
accepts whatever the driver passes, so an arg-name drift ships green and only
fails a LIVE run (it did — run 8c4e156f rejected at the feature-spec leg because
forge sent ``spec_input`` where 007 requires ``from_input``, and
``scope``/``target_repo`` where 008 requires the spec triple + descriptor). A pin
here breaks LOUDLY at test time on the next drift instead.

SOURCE OF RECORD (the specialist mode registration files — forge is READ-ONLY
against them; these pins MUST track them byte-for-byte):

  007  specialist-agent/src/specialist_agent/roles/product_owner/modes/
       feature_spec.py
         required_args = ("from_input",)
         optional      = context, stack, revision_of, validate_feedback

  008  specialist-agent/src/specialist_agent/roles/architect/modes/
       feature_plan.py
         required_args = ("feature_id", "spec_feature", "spec_summary",
                          "target_repo_descriptor")
         optional      = spec_assumptions, revision_of, validate_feedback
         TARGET_REPO_DESCRIPTOR_SCHEMA required = {"repo", "test_roots"}
"""

from __future__ import annotations

from forge.cli._serve_planning import (
    build_feature_plan_command_args,
    build_feature_spec_command_args,
)
from forge.pipeline.dispatchers.specialist import SPECIALIST_REQUIRED_ARGS_BY_STAGE
from forge.pipeline.stage_taxonomy import StageClass

# The literal contract sets, transcribed from the two mode files above.
_FEATURE_SPEC_REQUIRED = {"from_input"}
_FEATURE_PLAN_REQUIRED = {
    "feature_id",
    "spec_feature",
    "spec_summary",
    "target_repo_descriptor",
}
_FEATURE_PLAN_OPTIONAL_ON_WIRE = {"spec_assumptions"}
_TARGET_REPO_DESCRIPTOR_REQUIRED = {"repo", "test_roots"}


# ---------------------------------------------------------------------------
# 007 — po_feature_spec
# ---------------------------------------------------------------------------


def test_feature_spec_wire_args_are_exactly_from_input() -> None:
    args = build_feature_spec_command_args(from_input="the approved input content")
    assert set(args) == _FEATURE_SPEC_REQUIRED
    assert args["from_input"] == "the approved input content"


def test_feature_spec_required_mirror_matches_contract() -> None:
    # forge's dispatch-side required-args mirror must equal the mode's set.
    assert set(SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.FEATURE_SPEC]) == (
        _FEATURE_SPEC_REQUIRED
    )


# ---------------------------------------------------------------------------
# 008 — architect_feature_plan
# ---------------------------------------------------------------------------


def test_feature_plan_wire_args_are_exactly_the_required_four() -> None:
    args = build_feature_plan_command_args(
        feature_id="FEAT-BEEF",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "guardkit/api_test", "test_roots": ["tests"]},
    )
    # Exactly the four required names — no invented fields, no legacy
    # scope/target_repo, and spec_assumptions absent when not supplied.
    assert set(args) == _FEATURE_PLAN_REQUIRED
    assert args["feature_id"] == "FEAT-BEEF"
    assert args["target_repo_descriptor"] == {
        "repo": "guardkit/api_test",
        "test_roots": ["tests"],
    }


def test_feature_plan_wire_args_include_spec_assumptions_when_present() -> None:
    args = build_feature_plan_command_args(
        feature_id="FEAT-BEEF",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "guardkit/api_test", "test_roots": []},
        spec_assumptions="assumptions: []\n",
    )
    assert set(args) == _FEATURE_PLAN_REQUIRED | _FEATURE_PLAN_OPTIONAL_ON_WIRE
    assert args["spec_assumptions"] == "assumptions: []\n"


def test_feature_plan_wire_args_omit_blank_spec_assumptions() -> None:
    for blank in (None, "", "   "):
        args = build_feature_plan_command_args(
            feature_id="FEAT-BEEF",
            spec_feature="Feature: x\n",
            spec_summary="# summary\n",
            target_repo_descriptor={"repo": "guardkit/api_test", "test_roots": []},
            spec_assumptions=blank,
        )
        assert "spec_assumptions" not in args


def test_feature_plan_descriptor_carries_only_schema_fields() -> None:
    args = build_feature_plan_command_args(
        feature_id="FEAT-BEEF",
        spec_feature="Feature: x\n",
        spec_summary="# summary\n",
        target_repo_descriptor={"repo": "guardkit/api_test", "test_roots": ["tests"]},
    )
    descriptor = args["target_repo_descriptor"]
    # The two required descriptor keys are present, and every key is defined by
    # TARGET_REPO_DESCRIPTOR_SCHEMA (no invented fields).
    assert _TARGET_REPO_DESCRIPTOR_REQUIRED <= set(descriptor)
    assert set(descriptor) <= {
        "repo",
        "default_branch",
        "test_roots",
        "sibling_repos",
        "stack",
    }


def test_feature_plan_required_mirror_matches_contract() -> None:
    assert set(SPECIALIST_REQUIRED_ARGS_BY_STAGE[StageClass.FEATURE_PLAN]) == (
        _FEATURE_PLAN_REQUIRED
    )
