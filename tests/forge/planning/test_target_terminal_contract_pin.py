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

import json
from pathlib import Path
from types import SimpleNamespace

from forge.cli._serve_planning import (
    build_feature_plan_command_args,
    build_feature_spec_command_args,
)
from forge.pipeline.dispatchers.specialist import SPECIALIST_REQUIRED_ARGS_BY_STAGE
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.driver import PlanningRunDriver

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


# ===========================================================================
# RESULT-SIDE contract pins (B4 run f6781ad4 — the RESULT half of the same
# unpinned contract layer whose ARG half was pinned above / fixed in 35ec192).
#
# The specialist wraps every reply via wrap_role_output (specialist-agent
# adapters/result_wrapper.py) into
#   {role_id, coach_score, criterion_breakdown, detection_findings,
#    role_output: <the mode's NATIVE dict>}
# and forge's reply parser unwraps the envelope so the driver receives the
# NATIVE map at ``result.role_output``. The 007 native map is keyed by BARE
# FILENAME with the three contract suffixes PLUS extras (a pass-bar-seed-*.yaml
# and validation.json). These pins drive the REAL B4 gold artifacts through the
# driver's projection so a shape drift breaks LOUDLY at test time — the live
# run f6781ad4 COMPLETED coach=0.91 but forge failed it "no three-file spec
# contract" because the projection only understood a 'files' mapping.
#
# SOURCE OF RECORD for the native shapes:
#   007  product_owner/modes/feature_spec.py  _ARTIFACT_SUFFIXES + postprocess
#          (artifact map + validation.json + pass-bar-seed-<slug>.yaml)
#   008  architect/modes/feature_plan.py       postprocess_feature_plan
#          ({.guardkit/features/<id>.yaml, tasks/backlog/**, qa/*} + validation.json)
# ===========================================================================

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "b4_hermetic_007_r2"

# The verbatim filenames of the REAL 007 gold artifact set (run f6781ad4).
_SPEC_TRIPLE_NAMES = (
    "uptime-endpoint.feature",
    "uptime-endpoint_assumptions.yaml",
    "uptime-endpoint_summary.md",
)
_SPEC_EXTRA_NAMES = ("pass-bar-seed-uptime-endpoint.yaml", "validation.json")


def _load_real_native_map() -> dict[str, str]:
    """The REAL 007 gold artifact map (verbatim filenames, real content)."""
    return {
        name: (_FIXTURE_DIR / name).read_text(encoding="utf-8")
        for name in (*_SPEC_TRIPLE_NAMES, *_SPEC_EXTRA_NAMES)
    }


def _clean_native_map() -> dict[str, str]:
    """The real map with validation.json flipped to a clean/accepted channel."""
    native = _load_real_native_map()
    native["validation.json"] = json.dumps(
        {"accepted": True, "errors": [], "gates_run": ["gherkin_backstop"]}
    )
    return native


def _wrap(native: dict[str, str]) -> dict[str, object]:
    """The full wrap_role_output envelope around a native map (double-nest)."""
    return {
        "role_id": "product-owner",
        "coach_score": 0.91,
        "criterion_breakdown": [],
        "detection_findings": [],
        "role_output": native,
    }


# ---- _role_output_of: descend the wrap nesting (belt and braces) -----------


def test_role_output_of_bare_native_map_passes_through() -> None:
    """The DEPLOYED shape: the reply parser already unwrapped, so the driver
    receives the bare native map at ``result.role_output``."""
    native = _clean_native_map()
    result = SimpleNamespace(role_output=native)
    projected = PlanningRunDriver._role_output_of(result)
    assert set(projected) == set(native)
    assert "uptime-endpoint.feature" in projected


def test_role_output_of_descends_a_doubly_wrapped_envelope() -> None:
    """Belt-and-braces: if a wrap_role_output envelope is handed through as
    ``role_output`` (double nesting), the projection descends one level to the
    bare native map — never leaks the envelope scalars."""
    native = _clean_native_map()
    result = SimpleNamespace(role_output=_wrap(native))
    projected = PlanningRunDriver._role_output_of(result)
    assert set(projected) == set(native)
    assert "role_id" not in projected and "coach_score" not in projected


def test_role_output_of_non_mapping_degrades_to_empty() -> None:
    assert PlanningRunDriver._role_output_of(SimpleNamespace(role_output=None)) == {}
    assert PlanningRunDriver._role_output_of(SimpleNamespace(role_output="x")) == {}


# ---- _slug_of + _spec_triple_files: project the suffix-keyed triple --------


def test_slug_derived_from_the_feature_filename_stem() -> None:
    native = _clean_native_map()
    assert PlanningRunDriver._slug_of(native, "CID-123") == "uptime-endpoint"


def test_slug_falls_back_to_deterministic_when_no_feature_key() -> None:
    assert PlanningRunDriver._slug_of({"note.txt": "x"}, "CID-9") == "feature-CID-9"


def test_spec_triple_projected_from_the_real_suffix_keyed_map() -> None:
    native = _clean_native_map()
    slug = PlanningRunDriver._slug_of(native, "CID-123")
    files = PlanningRunDriver._spec_triple_files(native, slug)
    assert files is not None
    # EXACTLY the committed triple — extras (seed, validation.json) excluded.
    assert set(files) == {
        "features/uptime-endpoint/uptime-endpoint.feature",
        "features/uptime-endpoint/uptime-endpoint_assumptions.yaml",
        "features/uptime-endpoint/uptime-endpoint_summary.md",
    }
    # The committed content is the REAL artifact content, verbatim.
    assert "Feature: Uptime Endpoint" in (
        files["features/uptime-endpoint/uptime-endpoint.feature"]
    )
    # The pass-bar seed + validation.json are NEVER part of the committed triple.
    for path in files:
        assert "pass-bar-seed" not in path
        assert "validation.json" not in path


def test_feature_file_rel_finds_the_projected_feature() -> None:
    native = _clean_native_map()
    slug = PlanningRunDriver._slug_of(native, "CID-123")
    files = PlanningRunDriver._spec_triple_files(native, slug)
    assert files is not None
    rel = PlanningRunDriver._feature_file_rel(files)
    assert rel == "features/uptime-endpoint/uptime-endpoint.feature"


# ---- poison: a missing suffix file is invalid-artifacts (None) -------------


def test_spec_triple_missing_a_suffix_file_is_none() -> None:
    native = _clean_native_map()
    del native["uptime-endpoint_summary.md"]  # drop one leg of the triple
    slug = PlanningRunDriver._slug_of(native, "CID-123")
    assert PlanningRunDriver._spec_triple_files(native, slug) is None


def test_spec_triple_duplicate_suffix_is_none() -> None:
    native = _clean_native_map()
    native["other-endpoint.feature"] = "Feature: other\n"  # a second .feature
    slug = PlanningRunDriver._slug_of(native, "CID-123")
    assert PlanningRunDriver._spec_triple_files(native, slug) is None


# ---- VALIDATION HONESTY: validation.json errors -> loud fail signal --------


def test_real_validation_json_with_errors_is_a_loud_failure() -> None:
    """The REAL B4 gold set carried accepted=false — the leg must NOT proceed
    silently; the honesty check names the decidable-gate error."""
    native = _load_real_native_map()  # the REAL (accepted:false) channel
    failures = PlanningRunDriver._validation_failures(native)
    assert failures  # non-empty => the leg fails loudly
    assert any("@negative" in f for f in failures)


def test_clean_validation_channel_proceeds() -> None:
    assert PlanningRunDriver._validation_failures(_clean_native_map()) == []


def test_absent_validation_channel_proceeds() -> None:
    assert PlanningRunDriver._validation_failures({"x.feature": "Feature: x\n"}) == []


def test_unparseable_validation_json_is_a_loud_failure() -> None:
    failures = PlanningRunDriver._validation_failures(
        {"validation.json": "{not json"}
    )
    assert failures and "not parseable" in failures[0]


# ---- 008: the native plan tree is already repo-relative paths --------------

# A minimal but REAL-shaped 008 native map (architect/modes/feature_plan.py
# postprocess_feature_plan): keys are repo-relative paths; validation.json is
# the out-of-band channel (never committed).
_PLAN_NATIVE_MAP = {
    ".guardkit/features/FEAT-BEEF.yaml": "id: FEAT-BEEF\ntasks: []\n",
    "tasks/backlog/uptime-endpoint/IMPLEMENTATION-GUIDE.md": "# guide\n",
    "tasks/backlog/uptime-endpoint/TASK-UPT-001.md": "# task\n",
    "qa/pass-bar-TASK-UPT-001.yaml": "task_id: TASK-UPT-001\n",
    "qa/leak-sweep.yaml": "surfaces: []\n",
    "validation.json": json.dumps(
        {"accepted": True, "errors": [], "gates_run": ["feature_validate"]}
    ),
}


def test_plan_tree_projected_from_native_repo_relative_paths() -> None:
    files = PlanningRunDriver._plan_tree_files(_PLAN_NATIVE_MAP)
    assert files is not None
    # Every real repo path commits; validation.json is EXCLUDED (data channel).
    assert ".guardkit/features/FEAT-BEEF.yaml" in files
    assert "tasks/backlog/uptime-endpoint/TASK-UPT-001.md" in files
    assert "qa/pass-bar-TASK-UPT-001.yaml" in files
    assert "qa/leak-sweep.yaml" in files
    assert "validation.json" not in files


def test_plan_tree_native_carries_a_feature_yaml_for_build_trigger() -> None:
    files = PlanningRunDriver._plan_tree_files(_PLAN_NATIVE_MAP)
    assert files is not None
    assert any(f.endswith(".yaml") and "features/" in f for f in files)


def test_plan_validation_errors_are_a_loud_failure() -> None:
    poisoned = dict(_PLAN_NATIVE_MAP)
    poisoned["validation.json"] = json.dumps(
        {"accepted": False, "errors": ["ungated waves without an activation notice"]}
    )
    failures = PlanningRunDriver._validation_failures(poisoned)
    assert failures == ["ungated waves without an activation notice"]


def test_plan_tree_empty_map_is_none() -> None:
    assert PlanningRunDriver._plan_tree_files({"validation.json": "{}"}) is None
