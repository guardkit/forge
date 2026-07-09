"""WS2-B8 taxonomy inertness tests.

DEPLOY and LIVE_GATE are added to the StageClass enum + STAGE_PREREQUISITES for
canonical naming and ordering, but they must be **inert** in the greenfield
reasoning loop: dispatched only by the DeployStageRunner (config-gated), never
offered to the Mode A/B/C supervisor. These tests pin that inertness so a future
edit that leaks them into dispatch goes red.
"""

from __future__ import annotations

from forge.pipeline.mode_chains_data import (
    MODE_A_CHAIN,
    MODE_B_CHAIN,
    MODE_C_CHAIN,
)
from forge.pipeline.stage_ordering_guard import StageOrderingGuard
from forge.pipeline.stage_taxonomy import (
    PER_FEATURE_STAGES,
    POST_REVIEW_STAGES,
    STAGE_PREREQUISITES,
    StageClass,
)


class _FakeReader:
    """A stage-log reader where every stage is approved for every feature."""

    def __init__(self, features: list[str]) -> None:
        self._features = features

    def is_approved(self, build_id: str, stage: StageClass, feature_id=None) -> bool:
        return True  # everything approved — the most permissive case

    def feature_catalogue(self, build_id: str):
        return list(self._features)


class TestTaxonomyShape:
    def test_deploy_stages_exist(self) -> None:
        assert StageClass.DEPLOY.value == "deploy"
        assert StageClass.LIVE_GATE.value == "live-gate"

    def test_post_review_set(self) -> None:
        assert POST_REVIEW_STAGES == frozenset(
            {StageClass.DEPLOY, StageClass.LIVE_GATE}
        )

    def test_prerequisites_ordering(self) -> None:
        assert STAGE_PREREQUISITES[StageClass.DEPLOY] == [
            StageClass.PULL_REQUEST_REVIEW
        ]
        assert STAGE_PREREQUISITES[StageClass.LIVE_GATE] == [StageClass.DEPLOY]

    def test_leading_eight_unchanged(self) -> None:
        # The Mode A leading-eight iteration order is preserved (guard invariant).
        assert list(StageClass)[:8] == [
            StageClass.PRODUCT_OWNER,
            StageClass.ARCHITECT,
            StageClass.SYSTEM_ARCH,
            StageClass.SYSTEM_DESIGN,
            StageClass.FEATURE_SPEC,
            StageClass.FEATURE_PLAN,
            StageClass.AUTOBUILD,
            StageClass.PULL_REQUEST_REVIEW,
        ]


class TestInertness:
    def test_not_in_any_mode_chain(self) -> None:
        for stage in POST_REVIEW_STAGES:
            assert stage not in MODE_A_CHAIN
            assert stage not in MODE_B_CHAIN
            assert stage not in MODE_C_CHAIN

    def test_not_per_feature(self) -> None:
        for stage in POST_REVIEW_STAGES:
            assert stage not in PER_FEATURE_STAGES

    def test_next_dispatchable_never_offers_deploy_even_when_all_approved(
        self,
    ) -> None:
        # Even with EVERYTHING approved (PR review included), the reasoning-loop
        # permitted set must never include DEPLOY / LIVE_GATE. Their addition to
        # the enum + prereq map leaves Mode A dispatch byte-identical.
        guard = StageOrderingGuard()
        reader = _FakeReader(features=["FEAT-ABCD"])
        permitted = guard.next_dispatchable("build-1", reader)
        assert StageClass.DEPLOY not in permitted
        assert StageClass.LIVE_GATE not in permitted

    def test_explicit_stages_arg_can_still_opt_in(self) -> None:
        # A caller that explicitly asks for the deploy stages (a future
        # deploy-aware walk) can still get them — the filter is only the default.
        guard = StageOrderingGuard()
        reader = _FakeReader(features=["FEAT-ABCD"])
        permitted = guard.next_dispatchable(
            "build-1", reader, stages=[StageClass.DEPLOY]
        )
        assert StageClass.DEPLOY in permitted
