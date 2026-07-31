"""The plain-name table is COMPLETE, and its fallback is a net not a norm.

The stage-names ruling (Rich, 2026-07-31) has three parts. This file fences two
of them:

* **the table is the noun's single source** — a CI fence asserts that EVERY
  :class:`~forge.pipeline.stage_taxonomy.StageClass` member and EVERY planning-
  driver stage label has a real row. The fence reads the driver's source with
  the AST, so a lane that mints a new leg label and forgets its row fails here
  rather than shipping an internal string to Slack;
* **the humaniser is deterministic and total** — hyphens, underscores, empty,
  weird: always English, never a crash, never a raw field name.

Network-free, service-free, filesystem-read-only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forge.pipeline.stage_names import (
    STAGE_PLAIN_NAMES,
    humanise_stage_label,
    plain_stage_name,
)
from forge.pipeline.stage_taxonomy import StageClass
from forge.planning.revision import REVISION_STAGE_LABEL

_DRIVER_SOURCE = (
    Path(__file__).resolve().parents[3] / "src" / "forge" / "planning" / "driver.py"
)


def _driver_stage_labels() -> set[str]:
    """Every stage label the planning driver can stamp, read off its AST.

    Three shapes, all of them mechanical (no hand-maintained list to drift):

    1. module-level constants named ``_*_STAGE`` bound to a string literal;
    2. any ``stage_label=<string literal>`` keyword argument, anywhere;
    3. the second positional argument of a ``self._fail_leg(cid, <literal>, …)``
       call (the leg label, when a call site passes it inline).
    """
    tree = ast.parse(_DRIVER_SOURCE.read_text(encoding="utf-8"))
    labels: set[str] = set()

    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_STAGE"):
                labels.add(node.value.value)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "stage_label":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                labels.add(keyword.value.value)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "_fail_leg"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            labels.add(node.args[1].value)

    return labels


class TestTheFence:
    """Every known stage has a hand-written plain name — the fallback is rare."""

    def test_the_driver_source_is_where_this_fence_thinks_it_is(self) -> None:
        # Guard the AST scan itself: a moved driver would silently empty the
        # fence and let every missing row through.
        assert _DRIVER_SOURCE.is_file()
        assert _driver_stage_labels(), "the AST scan found no stage labels at all"

    @pytest.mark.parametrize("stage", list(StageClass), ids=lambda s: s.value)
    def test_every_stage_class_member_has_a_table_row(self, stage: StageClass) -> None:
        assert stage.value in STAGE_PLAIN_NAMES, (
            f"StageClass.{stage.name} ({stage.value!r}) has no plain name. Add its "
            "row to forge.pipeline.stage_names.STAGE_PLAIN_NAMES in the same "
            "commit that mints it — the humaniser is a net, not the norm."
        )

    def test_every_planning_driver_stage_label_has_a_table_row(self) -> None:
        missing = sorted(_driver_stage_labels() - set(STAGE_PLAIN_NAMES))
        assert not missing, (
            f"planning driver stage labels with no plain name: {missing}. Add a "
            "row to forge.pipeline.stage_names.STAGE_PLAIN_NAMES."
        )

    def test_the_revision_leg_label_has_a_table_row(self) -> None:
        assert REVISION_STAGE_LABEL in STAGE_PLAIN_NAMES

    def test_no_plain_name_is_itself_an_internal_label(self) -> None:
        """A row that just echoes the key is not a plain name."""
        for label, plain in STAGE_PLAIN_NAMES.items():
            assert plain and plain.strip() == plain
            assert plain != label
            assert "_" not in plain, f"{label!r} → {plain!r} still looks internal"
            assert plain == plain.lower() or plain[0].islower()

    def test_the_merge_ready_noun_matches_the_checkpoints_own_label(self) -> None:
        """One noun, not two: the table and the checkpoint agree verbatim."""
        from forge.pipeline.merge_ready_checkpoint import MERGE_READY_CHECKPOINT_LABEL

        assert (
            STAGE_PLAIN_NAMES[StageClass.PULL_REQUEST_REVIEW.value]
            == MERGE_READY_CHECKPOINT_LABEL
        )


class TestTheHumaniser:
    """The fallback: deterministic, total, never a crash, never a raw field."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("qa-feature-gate", "the qa feature gate step"),
            ("some_weird_label", "the some weird label step"),
            ("mixed-sep_label", "the mixed sep label step"),
            ("double__under--hyphen", "the double under hyphen step"),
            ("  padded-label  ", "the padded label step"),
            ("solo", "the solo step"),
            ("transition-to-cancelled", "the transition to cancelled step"),
        ],
    )
    def test_hyphens_and_underscores_become_words(
        self, label: str, expected: str
    ) -> None:
        assert humanise_stage_label(label) == expected

    @pytest.mark.parametrize("label", ["", "   ", "-", "_", "--__--", "\t\n"])
    def test_an_empty_or_separator_only_label_is_still_a_sentence(
        self, label: str
    ) -> None:
        assert humanise_stage_label(label) == "the current step"

    def test_it_never_raises_on_a_weird_value(self) -> None:
        for weird in (None, 12, 3.5, object()):
            rendered = humanise_stage_label(weird)  # type: ignore[arg-type]
            assert rendered.startswith("the ") and rendered.endswith(" step")

    def test_it_is_deterministic(self) -> None:
        assert humanise_stage_label("a-b") == humanise_stage_label("a-b")


class TestPlainStageName:
    """Table first, humaniser as the net — the one function render sites call."""

    def test_a_known_label_comes_from_the_table(self) -> None:
        assert plain_stage_name("qa-pass-bars") == "registering the quality checklist"
        assert plain_stage_name("build-queued") == "handing to the build system"
        assert plain_stage_name("feature-spec") == "writing the spec"
        assert plain_stage_name("pull-request-review") == "the merge-ready checkpoint"

    def test_an_unknown_label_falls_back_to_the_humaniser(self) -> None:
        assert plain_stage_name("never-heard-of-it") == "the never heard of it step"

    def test_it_never_returns_the_raw_internal_label(self) -> None:
        for label in ("qa-pass-bars", "qa-pass-bars-auth-confirm", "brand_new_leg", ""):
            assert plain_stage_name(label) != label

    def test_a_stage_class_member_can_be_passed_straight_in(self) -> None:
        # StageClass is a StrEnum, so a member IS its value at the render site.
        assert plain_stage_name(StageClass.AUTOBUILD) == "running the build"
