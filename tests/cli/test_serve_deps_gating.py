"""Unit tests for ``forge.cli._serve_deps_gating`` (TASK-JNB-101).

One class per behaviour of the approval-gate composition module:

* ``TestFactoryShape`` — :func:`build_approval_gate_parts` constructs the
  real adapter types and threads config into them.
* ``TestExpectedApproverThreading`` — the config-alignment guard (arch
  review R3): the factory ALWAYS passes ``expected_approver`` explicitly
  so config and wired behaviour cannot silently diverge.
* ``TestRefreshAndBridgeWiring`` — optional collaborators toggle the
  refresh callback and the PEB-006 bridge lookup.
* ``TestBindGateParts`` — module-level anchor + reset-for-tests.
* ``TestBoundContextSubscriber`` — the per-build adapter forwards the
  three resume-publish kwargs verbatim.
* ``TestMakeGateCheckDeps`` — the AC-1 typed injection seam.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adapters.nats.approval_publisher import ApprovalPublisher
from forge.adapters.nats.approval_subscriber import ApprovalSubscriber
from forge.adapters.nats.synthetic_response_injector import (
    SyntheticResponseInjector,
)
from forge.cli import _serve_deps_gating
from forge.cli._serve_deps_gating import (
    ApprovalGateParts,
    _BoundContextSubscriber,
    bind_gate_parts,
    bound_gate_parts,
    build_approval_gate_parts,
    make_gate_check_deps,
)
from forge.config.models import ForgeConfig
from forge.gating.degraded import EmptyPriorsReader
from forge.gating.wrappers import GateCheckDeps
from forge.pipeline import BuildContext


class _StubClient:
    """Publish-recording NATS stand-in; construction-time-only surface."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, body: bytes) -> None:
        self.published.append((subject, body))

    async def subscribe(self, subject: str, callback: Any) -> Any:
        raise AssertionError("unit tier never subscribes")


def _forge_config(**approval_overrides: Any) -> ForgeConfig:
    doc: dict[str, Any] = {
        "permissions": {"filesystem": {"allowlist": ["/srv/forge"]}},
    }
    if approval_overrides:
        doc["approval"] = approval_overrides
    return ForgeConfig.model_validate(doc)


def _build_parts(client: Any, config: ForgeConfig, **kwargs: Any) -> ApprovalGateParts:
    """Call the real factory with the required ``priors_reader`` pinned.

    The field is required-with-no-default by design (the no-silent-
    fallback seam); unit fixtures pin ``EmptyPriorsReader()`` — never a
    real fleet-memory reader.
    """
    kwargs.setdefault("priors_reader", EmptyPriorsReader())
    return build_approval_gate_parts(client, config, **kwargs)


@pytest.fixture(autouse=True)
def _isolate_bound_parts() -> Any:
    _serve_deps_gating._reset_for_tests()
    yield
    _serve_deps_gating._reset_for_tests()


class TestFactoryShape:
    """The factory constructs the real production adapter types."""

    def test_constructs_real_adapter_types(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert isinstance(parts, ApprovalGateParts)
        assert isinstance(parts.publisher, ApprovalPublisher)
        assert isinstance(parts.subscriber, ApprovalSubscriber)
        assert isinstance(parts.injector, SyntheticResponseInjector)

    def test_approval_config_slice_is_threaded(self) -> None:
        cfg = _forge_config(default_wait_seconds=7, max_wait_seconds=11)
        parts = _build_parts(_StubClient(), cfg)
        assert parts.approval_config is cfg.approval
        assert parts.subscriber._deps.config is cfg.approval

    def test_parts_are_frozen(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        with pytest.raises(FrozenInstanceError):
            parts.expected_approver = "other"  # type: ignore[misc]

    def test_emitter_defaults_to_none_and_is_carried(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert parts.emitter is None
        sentinel = object()
        parts2 = _build_parts(
            _StubClient(),
            _forge_config(),
            emitter=sentinel,  # type: ignore[arg-type]
        )
        assert parts2.emitter is sentinel


class TestExpectedApproverThreading:
    """Arch-review R3: config → subscriber deps, always explicit.

    ``ApprovalSubscriberDeps.expected_approver`` defaults to ``None``
    (permissive). If the factory ever stopped passing the kwarg, the
    pinned config default ``"rich"`` would silently degrade to
    accept-anyone — exactly the class of silent divergence the
    config-alignment AC exists to prevent.
    """

    def test_config_default_rich_reaches_subscriber_deps(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert parts.expected_approver == "rich"
        assert parts.subscriber._deps.expected_approver == "rich"

    def test_custom_value_reaches_subscriber_deps(self) -> None:
        cfg = _forge_config(expected_approver="someone-else")
        parts = _build_parts(_StubClient(), cfg)
        assert parts.subscriber._deps.expected_approver == "someone-else"

    def test_explicit_none_is_permissive_mode(self) -> None:
        cfg = _forge_config(expected_approver=None)
        parts = _build_parts(_StubClient(), cfg)
        assert parts.expected_approver is None
        assert parts.subscriber._deps.expected_approver is None


class TestRefreshAndBridgeWiring:
    """Optional collaborators wire the refresh + PEB-006 probes."""

    def test_no_repository_disables_refresh(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert parts.subscriber._deps.publish_refresh is None

    def test_repository_enables_refresh_callback(self) -> None:
        class _Repo:
            async def list_paused_builds(self) -> list[Any]:
                return []

            async def record_paused_build(self, **_: Any) -> None: ...

        parts = _build_parts(
            _StubClient(), _forge_config(), repository=_Repo()
        )
        assert callable(parts.subscriber._deps.publish_refresh)

    def test_no_bridge_registry_leaves_lookup_absent(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert parts.subscriber._deps.bridge_registry_lookup is None

    def test_bridge_lookup_requires_matching_correlation_id(self) -> None:
        # The probe answers "does the bridge own THIS build?" — an entry
        # for the same feature but a DIFFERENT correlation_id (stale or
        # earlier attach) must NOT suppress the subscriber's resume
        # emit (review finding, 2026-07-05).
        class _Registry:
            def __init__(self) -> None:
                self.entry: SimpleNamespace | None = None
                self.calls: list[tuple[str, str]] = []

            def get(
                self, feature_id: str, *, correlation_id: str
            ) -> SimpleNamespace | None:
                self.calls.append((feature_id, correlation_id))
                return self.entry

        registry = _Registry()
        parts = _build_parts(
            _StubClient(),
            _forge_config(),
            bridge_registry=registry,  # type: ignore[arg-type]
        )
        lookup = parts.subscriber._deps.bridge_registry_lookup
        assert lookup is not None
        # No entry at all → bridge absent.
        assert lookup("FEAT-X", "corr-1") is False
        # Entry for a DIFFERENT build of the feature → still absent.
        registry.entry = SimpleNamespace(correlation_id="corr-other")
        assert lookup("FEAT-X", "corr-2") is False
        # Entry for THIS build → bridge canonical.
        registry.entry = SimpleNamespace(correlation_id="corr-3")
        assert lookup("FEAT-X", "corr-3") is True
        assert registry.calls == [
            ("FEAT-X", "corr-1"),
            ("FEAT-X", "corr-2"),
            ("FEAT-X", "corr-3"),
        ]

    def test_subscriber_clock_override_is_threaded(self) -> None:
        class _Clock:
            def monotonic(self) -> float:
                return 42.0

        clock = _Clock()
        parts = _build_parts(
            _StubClient(),
            _forge_config(),
            subscriber_clock=clock,
            dedup_ttl_seconds=17,
        )
        assert parts.subscriber._deps.clock is clock
        assert parts.subscriber._deps.dedup_ttl_seconds == 17


class TestBindGateParts:
    """Module-level anchor mirrors ``_serve_production._bound_resources``."""

    def test_unbound_returns_none(self) -> None:
        assert bound_gate_parts() is None

    def test_bind_then_bound_round_trips(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        assert bind_gate_parts(parts) is parts
        assert bound_gate_parts() is parts

    def test_rebind_replaces_previous(self) -> None:
        first = _build_parts(_StubClient(), _forge_config())
        second = _build_parts(_StubClient(), _forge_config())
        bind_gate_parts(first)
        bind_gate_parts(second)
        assert bound_gate_parts() is second

    def test_reset_for_tests_clears_binding(self) -> None:
        bind_gate_parts(_build_parts(_StubClient(), _forge_config()))
        _serve_deps_gating._reset_for_tests()
        assert bound_gate_parts() is None


class TestBoundContextSubscriber:
    """The per-build adapter forwards the resume-publish kwargs verbatim."""

    @pytest.mark.asyncio
    async def test_forwards_bound_context_kwargs(self) -> None:
        sentinel_result = object()

        class _Inner:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def await_response(self, build_id: str, **kwargs: Any) -> Any:
                self.calls.append({"build_id": build_id, **kwargs})
                return sentinel_result

        inner = _Inner()
        emitter = object()
        ctx = BuildContext(
            feature_id="FEAT-X",
            build_id="build-X",
            correlation_id="corr-X",
            wave_total=1,
        )
        bound = _BoundContextSubscriber(
            inner,  # type: ignore[arg-type]
            lifecycle_emitter=emitter,  # type: ignore[arg-type]
            build_context=ctx,
            expected_correlation_id="corr-X",
        )

        result = await bound.await_response(
            "build-X", stage_label="Stage", attempt_count=2, timeout_seconds=5
        )

        assert result is sentinel_result
        assert inner.calls == [
            {
                "build_id": "build-X",
                "stage_label": "Stage",
                "attempt_count": 2,
                "timeout_seconds": 5,
                "lifecycle_emitter": emitter,
                "build_context": ctx,
                "expected_correlation_id": "corr-X",
            }
        ]


class _NullReader:
    async def read_priors(self, **_: Any) -> list[Any]:
        return []

    async def read_adjustments(self, **_: Any) -> list[Any]:
        return []

    async def read_rules(self, **_: Any) -> list[Any]:
        return []


class TestMakeGateCheckDeps:
    """The AC-1 typed seam: subscriber injected as GateCheckDeps.subscriber."""

    def _deps_kwargs(self) -> dict[str, Any]:
        reader = _NullReader()
        return {
            "priors_reader": reader,
            "adjustments_reader": reader,
            "rules_reader": reader,
            "repository": object(),
            "state_machine": object(),
            "reasoning_model_call": lambda _p: "{}",
        }

    def test_returns_typed_gate_check_deps(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        deps = make_gate_check_deps(parts, **self._deps_kwargs())
        assert isinstance(deps, GateCheckDeps)
        assert deps.publisher is parts.publisher
        assert deps.injector is parts.injector

    def test_without_ctx_injects_raw_subscriber(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        deps = make_gate_check_deps(parts, **self._deps_kwargs())
        assert deps.subscriber is parts.subscriber

    def test_with_ctx_and_emitter_binds_per_build_context(self) -> None:
        emitter = object()
        parts = _build_parts(
            _StubClient(),
            _forge_config(),
            emitter=emitter,  # type: ignore[arg-type]
        )
        ctx = BuildContext(
            feature_id="FEAT-X",
            build_id="build-X",
            correlation_id="corr-X",
            wave_total=1,
        )
        deps = make_gate_check_deps(parts, ctx=ctx, **self._deps_kwargs())
        assert isinstance(deps.subscriber, _BoundContextSubscriber)
        assert deps.subscriber._inner is parts.subscriber
        assert deps.subscriber._expected_correlation_id == "corr-X"

    def test_with_ctx_but_no_emitter_stays_raw(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        ctx = BuildContext(
            feature_id="FEAT-X",
            build_id="build-X",
            correlation_id="corr-X",
            wave_total=1,
        )
        deps = make_gate_check_deps(parts, ctx=ctx, **self._deps_kwargs())
        assert deps.subscriber is parts.subscriber

    def test_per_attempt_wait_seconds_is_threaded(self) -> None:
        parts = _build_parts(_StubClient(), _forge_config())
        deps = make_gate_check_deps(
            parts, per_attempt_wait_seconds=13, **self._deps_kwargs()
        )
        assert deps.per_attempt_wait_seconds == 13


def _sample_decision() -> Any:
    from forge.gating.models import GateDecision, GateMode

    return GateDecision(
        build_id="build-X",
        stage_label="Stage",
        target_kind="local_tool",
        target_identifier="t",
        mode=GateMode.FLAG_FOR_REVIEW,
        rationale="paused for review",
        coach_score=0.7,
        criterion_breakdown={"completeness": 0.7},
        detection_findings=[],
        evidence=[],
        decided_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
    )


class TestPublishRefreshClosure:
    """The API §7 refresh callback: lookup → persist → publish."""

    def _make_ordered_repo_and_client(
        self, rows: list[Any]
    ) -> tuple[Any, Any, list[str]]:
        order: list[str] = []

        class _Repo:
            async def list_paused_builds(self) -> list[Any]:
                return list(rows)

            async def record_paused_build(self, **kwargs: Any) -> None:
                order.append(f"record:{kwargs['request_id']}")

        class _Client:
            def __init__(self) -> None:
                self.published: list[tuple[str, bytes]] = []

            async def publish(self, subject: str, body: bytes) -> None:
                order.append(f"publish:{subject}")
                self.published.append((subject, body))

        return _Repo(), _Client(), order

    @pytest.mark.asyncio
    async def test_missing_row_skips_publish_and_record(self) -> None:
        repo, client, order = self._make_ordered_repo_and_client([])
        parts = _build_parts(client, _forge_config(), repository=repo)
        refresh = parts.subscriber._deps.publish_refresh
        assert refresh is not None

        await refresh("build-X", "Stage", 1)

        assert order == []
        assert client.published == []

    @pytest.mark.asyncio
    async def test_records_refreshed_row_before_publish(self) -> None:
        from forge.gating.identity import derive_request_id
        from forge.gating.wrappers import PausedBuildSnapshot

        decision = _sample_decision()
        row = PausedBuildSnapshot(
            build_id="build-X",
            feature_id="FEAT-X",
            stage_label="Stage",
            request_id=derive_request_id(
                build_id="build-X", stage_label="Stage", attempt_count=0
            ),
            attempt_count=0,
            decision_snapshot=decision,
            correlation_id="corr-X",
        )
        repo, client, order = self._make_ordered_repo_and_client([row])
        parts = _build_parts(client, _forge_config(), repository=repo)
        refresh = parts.subscriber._deps.publish_refresh
        assert refresh is not None

        await refresh("build-X", "Stage", 1)

        refreshed_id = derive_request_id(
            build_id="build-X", stage_label="Stage", attempt_count=1
        )
        # Persist-before-publish: a crash between the two is recovered
        # by boot re-emission of the NEW request_id.
        assert order == [
            f"record:{refreshed_id}",
            "publish:agents.approval.forge.build-X",
        ]
        # The republished envelope carries the refreshed id AND the
        # build's correlation_id (correlation-guard threading).
        import json as _json

        envelope = _json.loads(client.published[0][1])
        assert envelope["payload"]["request_id"] == refreshed_id
        assert envelope["correlation_id"] == "corr-X"

    @pytest.mark.asyncio
    async def test_newest_matching_row_wins(self) -> None:
        from forge.gating.identity import derive_request_id
        from forge.gating.wrappers import PausedBuildSnapshot

        old = PausedBuildSnapshot(
            build_id="build-X",
            feature_id="FEAT-X",
            stage_label="OldStage",
            request_id="req-old",
            attempt_count=0,
            decision_snapshot=_sample_decision(),
            correlation_id="corr-old",
        )
        new = PausedBuildSnapshot(
            build_id="build-X",
            feature_id="FEAT-X",
            stage_label="Stage",
            request_id="req-new",
            attempt_count=0,
            decision_snapshot=_sample_decision(),
            correlation_id="corr-new",
        )
        repo, client, _order = self._make_ordered_repo_and_client([old, new])
        parts = _build_parts(client, _forge_config(), repository=repo)
        refresh = parts.subscriber._deps.publish_refresh
        assert refresh is not None

        await refresh("build-X", "Stage", 1)

        import json as _json

        envelope = _json.loads(client.published[0][1])
        # Append-shaped repositories keep superseded rows; only the
        # NEWEST row carries the current pause's correlation context.
        assert envelope["correlation_id"] == "corr-new"
        assert envelope["payload"]["request_id"] == derive_request_id(
            build_id="build-X", stage_label="Stage", attempt_count=1
        )


class TestServeComposeSeam:
    """The production _compose closure binds the gate parts (AC-1)."""

    @pytest.mark.asyncio
    async def test_compose_constructs_and_binds_gate_parts(self, tmp_path: Any) -> None:
        import sqlite3

        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.lifecycle import migrations
        from forge.lifecycle.persistence import SqliteLifecyclePersistence

        cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
        try:
            migrations.apply_at_boot(cx)
            persistence = SqliteLifecyclePersistence(connection=cx)
            client = _StubClient()
            previous_dispatch = _serve_daemon.dispatch_payload
            try:
                compose = serve_module.bind_production_dispatch_chain(
                    forge_config=_forge_config(),
                    sqlite_pool=persistence,
                )
                await compose(client)

                parts = bound_gate_parts()
                assert parts is not None
                # Config default flowed through the serve seam.
                assert parts.expected_approver == "rich"
                # The compose-level emitter is bound for the resume emit.
                assert parts.emitter is not None
                # No bridge parts threaded in this composition → no probe.
                assert parts.subscriber._deps.bridge_registry_lookup is None
                # The dispatch chain rebind still happened.
                assert _serve_daemon.dispatch_payload is not previous_dispatch
            finally:
                _serve_daemon.dispatch_payload = previous_dispatch
        finally:
            cx.close()

    @pytest.mark.asyncio
    async def test_compose_soft_fails_when_gate_parts_raise(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        # DDR-007 boot protection: a v1.1 approval-wiring defect must
        # never brick v1 dispatch boot — compose completes, the
        # dispatcher is rebound, and the failure is an ERROR log line.
        import logging
        import sqlite3

        from forge.adapters.sqlite import connect as sqlite_connect
        from forge.cli import _serve_daemon
        from forge.cli import serve as serve_module
        from forge.lifecycle import migrations
        from forge.lifecycle.persistence import SqliteLifecyclePersistence

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("wiring defect")

        monkeypatch.setattr(_serve_deps_gating, "build_approval_gate_parts", _boom)

        cx: sqlite3.Connection = sqlite_connect.connect_writer(tmp_path / "forge.db")
        try:
            migrations.apply_at_boot(cx)
            persistence = SqliteLifecyclePersistence(connection=cx)
            client = _StubClient()
            previous_dispatch = _serve_daemon.dispatch_payload
            try:
                compose = serve_module.bind_production_dispatch_chain(
                    forge_config=_forge_config(),
                    sqlite_pool=persistence,
                )
                with caplog.at_level(logging.ERROR):
                    await compose(client)

                assert bound_gate_parts() is None
                assert _serve_daemon.dispatch_payload is not previous_dispatch
                assert any(
                    "approval gate parts construction FAILED" in r.message
                    for r in caplog.records
                )
            finally:
                _serve_daemon.dispatch_payload = previous_dispatch
        finally:
            cx.close()
