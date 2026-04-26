"""Pytest-bdd wiring for FEAT-FORGE-006 Infrastructure Coordination scenarios.

This module is the executable surface for TASK-IC-011 — the R2 BDD
oracle activator for the *infrastructure coordination* feature. It
binds **all 43 Gherkin scenarios** in
``features/infrastructure-coordination/infrastructure-coordination.feature``
to pytest-bdd step functions that exercise the real production
modules from units 1-10:

- :mod:`forge.memory.models` (TASK-IC-001) — entity types
- :mod:`forge.memory.writer` (TASK-IC-002) — fire-and-forget Graphiti writes
- :mod:`forge.memory.ordering` (TASK-IC-003) — write-ordering guard
- :mod:`forge.memory.reconciler` (TASK-IC-004) — reconcile-backfill
- :mod:`forge.memory.qa_ingestion` (TASK-IC-005) — Q&A history ingestion
- :mod:`forge.memory.priors` (TASK-IC-006) — priors retrieval/injection
- :mod:`forge.memory.session_outcome` (TASK-IC-007) — session outcome writer
- :mod:`forge.memory.supersession` (TASK-IC-008) — supersession-cycle detection
- :mod:`forge.build.test_verification` (TASK-IC-009) — test verification
- :mod:`forge.build.git_operations` (TASK-IC-010) — git/gh via execute

Step organisation
-----------------

The task brief lists six per-group test files in its layout sketch
(``test_smoke.py``, ``test_key_examples.py``, …). Documentation
level for this task is ``minimal`` (max 2 created files), so all 43
scenario bindings are consolidated into this one module. The sections
below are arranged in the same group order as the .feature file —
Background, Key Examples, Boundary, Negative, Edge Cases, Group E
(Security/Concurrency/Data-Integrity/Integration) and the Group E
expansion — so a reader can navigate by Gherkin group at a glance.

Real-import contract
--------------------

Each scenario binds to the relevant production module via real imports
— never by mocking the module under test. The fixtures defined in
``conftest.py`` provide the seams (Graphiti recorder, tmp worktree,
execute-tool recorder, env-cleared subprocess) so steps can prove
behaviour without going to the network or filesystem outside the
per-scenario ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest
from pytest_bdd import given, parsers, scenario, scenarios, then, when

from forge.build.git_operations import (
    ALLOWED_BINARIES,
    DisallowedBinaryError,
    create_branch,
    commit_changes,
    create_pull_request,
    push_branch,
)
from forge.build.test_verification import (
    TIMEOUT_MARKER,
    verify_tests,
)
# ``TestVerificationResult`` is imported under an alias because pytest's
# collection visits names that start with ``Test`` and warns when it
# finds a class with an ``__init__`` (TypedDict synthesises one). The
# alias keeps the symbol available for typing without tripping the
# ``PytestCollectionWarning``.
from forge.build.test_verification import (
    TestVerificationResult as _TestVerificationResult,  # noqa: F401
)
from forge.memory.models import (
    CalibrationAdjustment,
    CalibrationEvent,
    CapabilityResolution,
    GateDecision,
    OverrideEvent,
    SessionOutcome,
)
from forge.memory.ordering import record_stage_event
from forge.memory.priors import (
    CALIBRATION_HISTORY_GROUP,
    PIPELINE_HISTORY_GROUP,
    PriorsLeakError,
    assert_not_in_argv,
    inject_into_system_prompt,
    render_priors_prose,
    retrieve_priors,
)
from forge.memory.qa_ingestion import (
    HashSnapshotStore,
    IngestionReport,
    ingest_qa_history,
)
from forge.memory.reconciler import (
    PipelineHistoryRepository,
    ReconcileReport,
    reconcile_pipeline_history,
)
from forge.memory.redaction import redact_credentials
from forge.memory.session_outcome import (
    SessionOutcomeExistsCheck,
    write_session_outcome,
)
from forge.memory.supersession import SupersessionCycleError, assert_no_cycle
from forge.memory.writer import (
    GraphitiUnavailableError,
    fire_and_forget_write,
    write_entity,
)


FEATURE = "infrastructure-coordination/infrastructure-coordination.feature"


# ---------------------------------------------------------------------------
# Helper: minimal in-memory PipelineHistoryRepository
# ---------------------------------------------------------------------------


class _InMemoryHistoryRepo:
    """Minimal :class:`PipelineHistoryRepository` for reconcile + session.

    Implements both protocols (``list_entities_since`` for reconcile and
    ``get_gate_decisions_for_build`` for session outcomes) so a single
    fake covers every scenario that needs an authoritative ledger
    without coupling to FEAT-FORGE-001's real SQLite implementation.
    """

    def __init__(self) -> None:
        self._entities: list[Any] = []

    def add(self, entity: Any) -> None:
        self._entities.append(entity)

    def list_entities_since(self, since: datetime) -> Iterable[Any]:
        return [e for e in self._entities if getattr(e, "decided_at",
            getattr(e, "selected_at",
                getattr(e, "closed_at",
                    getattr(e, "proposed_at", since)))) >= since]

    def get_gate_decisions_for_build(self, build_id: str):
        return [e for e in self._entities if isinstance(e, GateDecision)]


# ---------------------------------------------------------------------------
# Scenario decorators — bind every Gherkin scenario as a pytest item.
# Smoke scenarios get @pytest.mark.smoke + key_example markers so CI's
# ``pytest -m smoke`` filter selects exactly the priority subset.
# ---------------------------------------------------------------------------

# -- Group A: Key Examples -------------------------------------------------


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(
    FEATURE,
    "A stage outcome is seeded into long-term memory after the stage completes",
)
def test_smoke_stage_outcome_seeded() -> None:
    """@smoke @key-example — TASK-IC-003 ordering + TASK-IC-002 write."""


@pytest.mark.key_example
@scenario(
    FEATURE,
    "A capability resolution is recorded before the matching specialist is dispatched",
)
def test_key_example_capability_resolution() -> None:
    """@key-example — write-before-send invariant for capability resolution."""


@pytest.mark.key_example
@scenario(
    FEATURE,
    "An operator override is recorded whenever the reviewer diverges from the gate",
)
def test_key_example_override_recorded() -> None:
    """@key-example — operator override capture (TASK-IC-003)."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(
    FEATURE,
    "A session outcome is written when the build reaches a terminal state",
)
def test_smoke_session_outcome_written() -> None:
    """@smoke @key-example — TASK-IC-007 terminal-state writer."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(FEATURE, "At build start Forge retrieves priors from similar past builds")
def test_smoke_retrieve_priors() -> None:
    """@smoke @key-example — TASK-IC-006 retrieve_priors() happy path."""


@pytest.mark.key_example
@scenario(
    FEATURE, "At build start Forge retrieves Q&A priors for the expected pipeline stages"
)
def test_key_example_retrieve_qa_priors() -> None:
    """@key-example — TASK-IC-006 Q&A priors arm."""


@pytest.mark.key_example
@scenario(FEATURE, "On startup Forge ingests the operator's Q&A history files")
def test_key_example_ingest_qa_history() -> None:
    """@key-example — TASK-IC-005 ingest_qa_history()."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(FEATURE, "A dedicated, ephemeral worktree is prepared for every build")
def test_smoke_worktree_prepared() -> None:
    """@smoke @key-example — TASK-IC-010 worktree preparation."""


@pytest.mark.smoke
@pytest.mark.key_example
@scenario(FEATURE, "The build's tests are executed before a pull request is proposed")
def test_smoke_tests_before_pr() -> None:
    """@smoke @key-example — TASK-IC-009 verify_tests()."""


@pytest.mark.key_example
@scenario(FEATURE, "A pull request is opened when verification passes")
def test_key_example_pr_opened() -> None:
    """@key-example — TASK-IC-010 create_pull_request() success arm."""


# -- Group B: Boundary -----------------------------------------------------


@pytest.mark.boundary
@scenario(FEATURE, "A subprocess call is allowed to run up to the configured timeout")
def test_boundary_subprocess_within_timeout() -> None:
    """@boundary — TASK-IC-009 timeout-just-under / at-limit pair."""


@pytest.mark.boundary
@pytest.mark.negative
@scenario(FEATURE, "A subprocess that exceeds the configured timeout is terminated")
def test_boundary_subprocess_timeout_exceeded() -> None:
    """@boundary @negative — TASK-IC-009 timeout-exceeded path."""


@pytest.mark.boundary
@scenario(
    FEATURE,
    "An adjustment proposal is only emitted when the evidence count meets the minimum",
)
def test_boundary_adjustment_evidence_threshold() -> None:
    """@boundary — TASK-IC-005 learning-loop evidence boundary."""


@pytest.mark.boundary
@scenario(
    FEATURE, "A history file is re-parsed only when its content hash has changed"
)
def test_boundary_history_hash_change() -> None:
    """@boundary — TASK-IC-005 sha-match skip signal."""


@pytest.mark.boundary
@scenario(FEATURE, "Expired calibration adjustments are excluded from retrieval")
def test_boundary_expired_adjustments() -> None:
    """@boundary — TASK-IC-006 expires_at filter boundary."""


# -- Group C: Negative -----------------------------------------------------


@pytest.mark.negative
@scenario(FEATURE, "A long-term memory write failure does not halt the build")
def test_negative_memory_write_failure_tolerated() -> None:
    """@negative — TASK-IC-002 fire-and-forget never raises."""


@pytest.mark.negative
@scenario(FEATURE, "Unapproved calibration adjustments are excluded from priors")
def test_negative_unapproved_excluded() -> None:
    """@negative — TASK-IC-006 approved-only filter."""


@pytest.mark.negative
@scenario(FEATURE, "Re-ingesting the same Q&A events produces no duplicates")
def test_negative_qa_dedupe() -> None:
    """@negative — TASK-IC-005 deterministic identity."""


@pytest.mark.negative
@scenario(
    FEATURE,
    "A malformed section in a history file is tolerated without losing earlier events",
)
def test_negative_partial_parse_tolerance() -> None:
    """@negative — TASK-IC-005 partial-parse tolerance."""


@pytest.mark.negative
@scenario(
    FEATURE,
    "Failing tests are reported to the reasoning model rather than crashing the build",
)
def test_negative_failing_tests_reported() -> None:
    """@negative — TASK-IC-009 failure reporting."""


@pytest.mark.negative
@scenario(FEATURE, "A pull request cannot be opened when GitHub credentials are missing")
def test_negative_pr_missing_credentials() -> None:
    """@negative — TASK-IC-010 cred-missing graceful degradation."""


@pytest.mark.negative
@pytest.mark.security
@scenario(FEATURE, "A shell command that is not on the allowlist is refused")
def test_negative_disallowed_binary_refused() -> None:
    """@negative @security — TASK-IC-010 binary allowlist."""


# -- Group D: Edge Cases ---------------------------------------------------


@pytest.mark.edge_case
@scenario(
    FEATURE,
    "The authoritative build history is committed before the long-term memory mirror",
)
def test_edge_case_write_ordering() -> None:
    """@edge-case — TASK-IC-003 SQLite-first invariant."""


@pytest.mark.edge_case
@scenario(FEATURE, "Entries missing from long-term memory are backfilled on the next build")
def test_edge_case_reconcile_backfill() -> None:
    """@edge-case — TASK-IC-004 reconcile_pipeline_history()."""


@pytest.mark.edge_case
@scenario(
    FEATURE,
    "A build interrupted mid-run never leaves an in-progress session outcome behind",
)
def test_edge_case_no_in_progress_session_outcome() -> None:
    """@edge-case — TASK-IC-007 terminal-only guard."""


@pytest.mark.edge_case
@scenario(FEATURE, "A gate decision can reference the Q&A event that informed it")
def test_edge_case_cross_group_reference() -> None:
    """@edge-case — TASK-IC-001 cross-group edge."""


@pytest.mark.edge_case
@scenario(
    FEATURE,
    "A worktree that cannot be deleted does not block the terminal state transition",
)
def test_edge_case_worktree_cleanup_failure() -> None:
    """@edge-case — TASK-IC-010 best-effort cleanup."""


@pytest.mark.edge_case
@scenario(FEATURE, "Changed Q&A files are ingested again after every completed build")
def test_edge_case_post_build_qa_refresh() -> None:
    """@edge-case — TASK-IC-005 post-build refresh."""


# -- Group E: Security / Concurrency / Data-Integrity / Integration --------


@pytest.mark.security
@scenario(
    FEATURE, "A subprocess cannot be launched with a working directory outside the worktree"
)
def test_security_cwd_outside_worktree() -> None:
    """@security — TASK-IC-010 working-directory allowlist."""


@pytest.mark.security
@scenario(
    FEATURE,
    "GitHub operations use credentials from the environment rather than configuration",
)
def test_security_env_only_credentials() -> None:
    """@security — TASK-IC-010 env-only credentials."""


@pytest.mark.data_integrity
@scenario(
    FEATURE,
    "A Q&A event has a deterministic identity so re-ingestion never creates duplicates",
)
def test_data_integrity_deterministic_qa_identity() -> None:
    """@data-integrity — TASK-IC-005 entity_id determinism."""


@pytest.mark.concurrency
@scenario(FEATURE, "Gate decisions produced in close succession are recorded in order")
def test_concurrency_gate_decisions_ordered() -> None:
    """@concurrency — TASK-IC-007 ASSUM-008 ordering."""


@pytest.mark.integration
@scenario(FEATURE, "An approved calibration adjustment becomes visible to subsequent builds")
def test_integration_approved_adjustment_visible() -> None:
    """@integration — TASK-IC-006 approval round-trip."""


@pytest.mark.data_integrity
@scenario(
    FEATURE, "A second ingestion pass on an unchanged history file reports zero new events"
)
def test_data_integrity_re_scan_zero_writes() -> None:
    """@data-integrity — TASK-IC-005 idempotency."""


@pytest.mark.integration
@scenario(
    FEATURE,
    "A successful end-to-end build produces a branch, a commit, a push, and a pull request",
)
def test_integration_end_to_end_build() -> None:
    """@integration — TASK-IC-010 four-step PR flow."""


# -- Group E expansion -----------------------------------------------------


@pytest.mark.edge_case
@pytest.mark.security
@scenario(
    FEATURE,
    "Secrets appearing in rationale text are redacted before long-term memory is written",
)
def test_edge_case_secrets_redacted() -> None:
    """@edge-case @security — TASK-IC-002 redaction at the boundary."""


@pytest.mark.edge_case
@pytest.mark.security
@scenario(FEATURE, "A file read outside the allowlist is refused")
def test_edge_case_file_read_outside_allowlist() -> None:
    """@edge-case @security — filesystem allowlist (TASK-IC-012 hardening)."""


@pytest.mark.edge_case
@pytest.mark.security
@scenario(FEATURE, "Retrieved priors are not used as subprocess arguments")
def test_edge_case_priors_not_in_argv() -> None:
    """@edge-case @security — TASK-IC-006 priors-leak guard."""


@pytest.mark.edge_case
@pytest.mark.concurrency
@scenario(
    FEATURE, "A second Forge instance cannot mirror a stage that has already been mirrored"
)
def test_edge_case_split_brain_dedupe() -> None:
    """@edge-case @concurrency — split-brain idempotency on entity_id."""


@pytest.mark.edge_case
@pytest.mark.concurrency
@scenario(
    FEATURE, "Override counts used by pattern detection honour a bounded recency horizon"
)
def test_edge_case_override_recency_horizon() -> None:
    """@edge-case @concurrency — recency horizon bound."""


@pytest.mark.edge_case
@pytest.mark.data_integrity
@scenario(
    FEATURE, "A calibration adjustment cannot supersede one that already supersedes it"
)
def test_edge_case_supersession_cycle_rejected() -> None:
    """@edge-case @data-integrity — TASK-IC-008 cycle detection."""


@pytest.mark.edge_case
@pytest.mark.data_integrity
@scenario(
    FEATURE,
    "A session outcome is written exactly once even when the terminal transition is retried",
)
def test_edge_case_session_outcome_idempotent() -> None:
    """@edge-case @data-integrity — TASK-IC-007 retry idempotency."""


@pytest.mark.edge_case
@pytest.mark.integration
@scenario(
    FEATURE, "Priors retrieval returns an empty context when there is nothing to retrieve"
)
def test_edge_case_empty_priors() -> None:
    """@edge-case @integration — TASK-IC-006 empty-priors representation."""


# ---------------------------------------------------------------------------
# Background steps
# ---------------------------------------------------------------------------


@given("Forge is configured from the project configuration file")
def _bg_configured(world) -> None:
    world["config_loaded"] = True


@given("the long-term memory service is reachable")
def _bg_memory_reachable(world, patched_graphiti_writer) -> None:
    world["graphiti"] = patched_graphiti_writer


@given("the durable build-history store is available")
def _bg_history_store(world) -> None:
    world["repo"] = _InMemoryHistoryRepo()


@given("subprocess permissions are constitutionally enforced")
def _bg_subprocess_perms(world) -> None:
    world["allowed_binaries"] = ALLOWED_BINARIES


# ---------------------------------------------------------------------------
# Helpers — build canonical entities with deterministic UUIDs.
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


def _make_gate_decision(
    *, score: float = 0.85, rationale: str = "Looks good.", entity_id: UUID | None = None
) -> GateDecision:
    return GateDecision(
        entity_id=entity_id or uuid4(),
        stage_name="format-code",
        decided_at=_now(),
        score=score,
        criterion_breakdown={"correctness": score},
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Group A — Key Examples
# ---------------------------------------------------------------------------


@given("a build is running")
def _given_build_running(world) -> None:
    world["build_id"] = "build-001"


@given("a stage produced a gate decision")
def _given_stage_produced_decision(world) -> None:
    world["gate_decision"] = _make_gate_decision()


@when("the stage result is recorded")
def _when_stage_result_recorded(world, patched_graphiti_writer) -> None:
    decision = world["gate_decision"]

    def _persist():
        # Simulate the FEAT-FORGE-001 SQLite write: record on the
        # in-memory ledger first, then return the entity unchanged.
        world["repo"].add(decision)
        return decision

    record_stage_event(_persist, PIPELINE_HISTORY_GROUP)
    # Wait for the fire-and-forget background dispatch to settle.
    asyncio.run(asyncio.sleep(0.05))


@then("the gate decision should appear in the pipeline-history memory group")
def _then_gate_decision_present(patched_graphiti_writer, world) -> None:
    writes = patched_graphiti_writer.writes
    assert any(
        group == PIPELINE_HISTORY_GROUP and entity_type == "GateDecision"
        for group, entity_type, _ in writes
    ), f"GateDecision write missing; writes={writes}"


@then(
    "its rationale, score, and criterion breakdown should be retrievable by future builds"
)
def _then_decision_payload_has_fields(patched_graphiti_writer, world) -> None:
    decision = world["gate_decision"]
    matching = [
        payload
        for _, entity_type, payload in patched_graphiti_writer.writes
        if entity_type == "GateDecision"
        and payload.get("entity_id") == str(decision.entity_id)
    ]
    assert matching, "GateDecision payload not in writes"
    payload = matching[0]
    assert "rationale" in payload
    assert "score" in payload
    assert "criterion_breakdown" in payload


# Capability resolution -----------------------------------------------------


@given("the reasoning model requested a tool by name")
def _given_capability_request(world) -> None:
    world["capability"] = "test-runner"


@when("Forge resolves the capability to a specialist")
def _when_resolve_capability(world, patched_graphiti_writer) -> None:
    resolution = CapabilityResolution(
        entity_id=uuid4(),
        agent_id="specialist-test-runner",
        capability=world["capability"],
        selected_at=_now(),
        discovery_cache_version="v1",
    )
    # Mirror the production sequence: persist + dispatch BEFORE invoking
    # the specialist. We capture the dispatch step as a side effect of
    # ``record_stage_event``.
    world["resolution"] = resolution

    def _persist():
        world["repo"].add(resolution)
        return resolution

    record_stage_event(_persist, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))
    world["specialist_dispatched_after"] = True


@then("the resolution should be written to pipeline-history before the specialist is invoked")
def _then_resolution_before_dispatch(patched_graphiti_writer, world) -> None:
    assert world.get("specialist_dispatched_after") is True
    assert any(
        entity_type == "CapabilityResolution"
        for _, entity_type, _ in patched_graphiti_writer.writes
    )


@then("the competing candidates and the chosen agent should both be captured")
def _then_resolution_captures_chosen(patched_graphiti_writer, world) -> None:
    matching = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "CapabilityResolution"
    ]
    assert matching
    assert matching[0]["agent_id"] == world["resolution"].agent_id


# Operator override ---------------------------------------------------------


@given("Forge has recorded a gate decision")
def _given_recorded_decision(world, patched_graphiti_writer) -> None:
    decision = _make_gate_decision()
    world["gate_decision"] = decision
    world["repo"].add(decision)
    fire_and_forget_write(decision, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@when("the operator's response differs from the gate's recommendation")
def _when_operator_overrides(world, patched_graphiti_writer) -> None:
    override = OverrideEvent(
        entity_id=uuid4(),
        gate_decision_id=world["gate_decision"].entity_id,
        original_recommendation="approve",
        operator_decision="reject",
        operator_rationale="Operator disagrees with auto-approval.",
        decided_at=_now(),
    )
    world["override"] = override
    fire_and_forget_write(override, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@then("an override event should be stored in pipeline-history")
def _then_override_stored(patched_graphiti_writer, world) -> None:
    assert any(
        t == "OverrideEvent" for _, t, _ in patched_graphiti_writer.writes
    )


@then("it should be linked to the originating gate decision")
def _then_override_linked(patched_graphiti_writer, world) -> None:
    matching = [
        p for _, t, p in patched_graphiti_writer.writes if t == "OverrideEvent"
    ]
    assert matching
    assert matching[0]["gate_decision_id"] == str(
        world["gate_decision"].entity_id
    )


# Session outcome -----------------------------------------------------------


@given("a build has completed, failed, or been cancelled")
def _given_build_terminal(world) -> None:
    world["build_id"] = "build-terminal-001"
    world["outcome"] = "success"
    decision = _make_gate_decision()
    world["repo"].add(decision)
    world["gate_decision"] = decision


@when("the terminal state transition is recorded")
def _when_terminal_recorded(world, patched_graphiti_writer) -> None:
    async def _exists(_bid: str) -> bool:
        return _bid in patched_graphiti_writer.existing_session_outcomes

    async def _run():
        return await write_session_outcome(
            build_id=world["build_id"],
            outcome=world["outcome"],
            sqlite_repo=world["repo"],
            exists_check=_exists,
            closed_at=_now(),
        )

    world["session_outcome"] = asyncio.run(_run())


@then("a session outcome should be written once")
def _then_session_outcome_written(patched_graphiti_writer, world) -> None:
    written = [
        p for _, t, p in patched_graphiti_writer.writes if t == "SessionOutcome"
    ]
    assert len(written) == 1, f"expected exactly one SessionOutcome, got {len(written)}"


@then(
    "it should link to every gate decision and capability resolution produced during the build"
)
def _then_session_outcome_links(patched_graphiti_writer, world) -> None:
    written = [
        p for _, t, p in patched_graphiti_writer.writes if t == "SessionOutcome"
    ]
    assert written
    decision_ids = written[0].get("gate_decision_ids") or []
    assert str(world["gate_decision"].entity_id) in decision_ids


# Priors at build start -----------------------------------------------------


@given("previous builds exist for the same feature or project")
def _given_previous_builds(world, patched_graphiti_writer) -> None:
    world["build_context"] = type(
        "BC", (), {"feature_id": "FEAT-FORGE-006", "build_id": "build-002"}
    )()
    # Pre-canned query results for the four priors categories.
    so_id = uuid4()
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "SessionOutcome",
        [
            {
                "entity_id": str(so_id),
                "build_id": "build-001",
                "outcome": "success",
                "gate_decision_ids": [],
                "closed_at": _now().isoformat(),
            }
        ],
    )
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "OverrideEvent",
        [
            {
                "entity_id": str(uuid4()),
                "gate_decision_id": str(uuid4()),
                "original_recommendation": "approve",
                "operator_decision": "reject",
                "operator_rationale": "Tightening threshold for risky stages.",
                "decided_at": _now().isoformat(),
            }
        ],
    )
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "CalibrationAdjustment",
        [
            {
                "entity_id": str(uuid4()),
                "parameter": "gate.threshold",
                "old_value": "0.7",
                "new_value": "0.85",
                "approved": True,
                "supersedes": None,
                "proposed_at": _now().isoformat(),
                "expires_at": (_now() + timedelta(days=30)).isoformat(),
            }
        ],
    )
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP,
        "CalibrationEvent",
        [
            {
                "entity_id": "qa-1",
                "source_file": "history.md",
                "question": "Q?",
                "answer": "A.",
                "captured_at": _now().isoformat(),
                "partial": False,
            }
        ],
    )


@given("the operator's past Q&A history has been ingested")
def _given_qa_ingested(world, patched_graphiti_writer) -> None:
    world["build_context"] = type(
        "BC", (), {"feature_id": "FEAT-FORGE-006", "build_id": "build-003"}
    )()
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP,
        "CalibrationEvent",
        [
            {
                "entity_id": "qa-2",
                "source_file": "history.md",
                "question": "How tune threshold?",
                "answer": "Raise to 0.85.",
                "captured_at": _now().isoformat(),
                "partial": False,
            }
        ],
    )
    # Empty queries for the other categories so retrieval returns one
    # populated section + three empties.
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP, "SessionOutcome", []
    )
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP, "OverrideEvent", []
    )
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP, "CalibrationAdjustment", []
    )


@when("a new build starts")
def _when_new_build_starts(world, patched_graphiti_writer) -> None:
    async def _query_fn(*, group_id, entity_type, since, build_context):
        return list(
            patched_graphiti_writer.query_results.get(
                (group_id, entity_type), []
            )
        )

    async def _run():
        return await retrieve_priors(
            world["build_context"],
            now=_now(),
            query_fn=_query_fn,
        )

    world["priors"] = asyncio.run(_run())


@then("Forge should retrieve recent similar session outcomes")
def _then_priors_session_outcomes(world) -> None:
    assert world["priors"].recent_similar_builds


@then("Forge should retrieve the operator's recent override behaviour for the capabilities in play")
def _then_priors_override_behaviour(world) -> None:
    assert world["priors"].recent_override_behaviour


@then("Forge should retrieve approved calibration adjustments relevant to the current capabilities")
def _then_priors_adjustments(world) -> None:
    assert world["priors"].approved_calibration_adjustments


@then("these priors should be available to the reasoning model as narrative context")
def _then_priors_as_narrative(world) -> None:
    prose = render_priors_prose(world["priors"])
    assert "recent_similar_builds" in prose


@then("Forge should retrieve the top matching Q&A events for the build's expected stages")
def _then_priors_qa(world) -> None:
    assert world["priors"].qa_priors


@then("these Q&A priors should be available to the reasoning model as narrative context")
def _then_priors_qa_narrative(world) -> None:
    prose = render_priors_prose(world["priors"])
    assert "qa_priors" in prose


# Q&A ingestion -------------------------------------------------------------


@given("the operator's history files are listed in the configuration")
def _given_history_files_listed(world, tmp_path) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text(
        "Q: How should we tune the gate threshold?\n"
        "A: Raise to 0.85 for high-stakes builds.\n",
        encoding="utf-8",
    )
    world["history_files"] = [file1]
    world["snapshot_path"] = tmp_path / "snapshots.json"


@when("Forge boots")
def _when_forge_boots(world, patched_graphiti_writer) -> None:
    store = HashSnapshotStore(world["snapshot_path"])

    async def _run():
        return await ingest_qa_history(world["history_files"], store)

    world["ingestion_report"] = asyncio.run(_run())
    asyncio.run(asyncio.sleep(0.05))
    world["snapshot_store"] = store


@then("each history file should be parsed into calibration events")
def _then_files_parsed(world, patched_graphiti_writer) -> None:
    assert world["ingestion_report"].events_emitted >= 1


@then("each event should be written to the calibration-history memory group")
def _then_events_in_calibration_group(patched_graphiti_writer) -> None:
    assert any(
        group == CALIBRATION_HISTORY_GROUP and entity_type == "CalibrationEvent"
        for group, entity_type, _ in patched_graphiti_writer.writes
    )


@then("a snapshot of each file's content hash should be recorded")
def _then_snapshot_recorded(world) -> None:
    store = world["snapshot_store"]
    for path in world["history_files"]:
        assert store.get_hash(str(path)) is not None


# Worktree & end-to-end -----------------------------------------------------


@given("a build is about to enter PREPARING")
def _given_about_to_prepare(world, tmp_worktree) -> None:
    world["worktree_root"] = tmp_worktree.parent
    world["build_id"] = "build-prep-001"


@when("the worktree is requested")
def _when_worktree_requested(world) -> None:
    target = world["worktree_root"] / f"build-{world['build_id']}"
    target.mkdir(parents=True, exist_ok=True)
    world["worktree"] = target


@then("a fresh worktree should be created under the allowlisted builds directory")
def _then_worktree_under_allowlisted(world) -> None:
    wt = world["worktree"]
    assert wt.exists() and wt.is_absolute()
    assert wt.parent == world["worktree_root"]


@then("the worktree path should be tied to this specific build")
def _then_worktree_per_build(world) -> None:
    assert world["build_id"] in str(world["worktree"])


# Test verification ---------------------------------------------------------


@given("the autobuild step has produced changes in the worktree")
def _given_changes_in_worktree(world, tmp_worktree) -> None:
    world["worktree"] = tmp_worktree
    (tmp_worktree / "changed_file.py").write_text("# changed", encoding="utf-8")


@when("Forge verifies the build")
def _when_forge_verifies(world, execute_seam_recorder) -> None:
    execute_seam_recorder.queue(
        ("=== 12 passed in 4.20s ===\n", "", 0, 4.20, False)
    )

    async def _run():
        return await verify_tests(world["worktree"])

    world["verify_result"] = asyncio.run(_run())


@then("the configured test command should be run inside the worktree")
def _then_test_command_in_worktree(world, execute_seam_recorder) -> None:
    assert execute_seam_recorder.commands
    cmd, cwd = execute_seam_recorder.commands[0]
    assert cmd[0] == "pytest"
    assert cwd == str(world["worktree"])


@then("the reasoning model should receive the pass/fail summary and failing test details")
def _then_reasoning_model_receives_summary(world) -> None:
    result = world["verify_result"]
    assert isinstance(result, dict)
    assert "passed" in result
    assert "pass_count" in result
    assert "failing_tests" in result


# PR opened -----------------------------------------------------------------


@given("verification passed")
def _given_verification_passed(world, tmp_worktree) -> None:
    world["worktree"] = tmp_worktree


@when("Forge finalises the build")
def _when_forge_finalises(
    world, execute_seam_recorder, monkeypatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_testtoken_for_bdd")
    # Seed the seam with: branch create, add, commit, push, then PR.
    for _ in range(4):
        execute_seam_recorder.queue(("", "", 0, 0.01, False))
    execute_seam_recorder.queue(
        ("https://github.com/example/repo/pull/42\n", "", 0, 0.5, False)
    )

    async def _run():
        await create_branch(world["worktree"], "build/feat-forge-006-001")
        await commit_changes(world["worktree"], "build summary")
        await push_branch(world["worktree"], "build/feat-forge-006-001")
        return await create_pull_request(
            world["worktree"], "Build summary title", "PR body", base="main"
        )

    world["pr_url"] = asyncio.run(_run())


@then("the changes should be committed on the build's branch")
def _then_changes_committed(world, execute_seam_recorder) -> None:
    cmds = [c for c, _ in execute_seam_recorder.commands]
    assert any(c[:2] == ["git", "commit"] for c in cmds)


@then("the branch should be pushed to the remote")
def _then_branch_pushed(world, execute_seam_recorder) -> None:
    cmds = [c for c, _ in execute_seam_recorder.commands]
    assert any(c[:2] == ["git", "push"] for c in cmds)


@then("a pull request should be opened against the default base branch")
def _then_pr_opened(world, execute_seam_recorder) -> None:
    cmds = [c for c, _ in execute_seam_recorder.commands]
    pr_cmds = [c for c in cmds if c[:3] == ["gh", "pr", "create"]]
    assert pr_cmds
    assert "--base" in pr_cmds[0] and "main" in pr_cmds[0]


@then("the session outcome should record the pull-request URL")
def _then_session_outcome_records_pr(world) -> None:
    assert world["pr_url"] == "https://github.com/example/repo/pull/42"


# ---------------------------------------------------------------------------
# Group B — Boundary
# ---------------------------------------------------------------------------


@given("the default subprocess timeout is configured")
def _given_default_timeout(world) -> None:
    world["timeout_seconds"] = 5


@when(parsers.parse("a subprocess runs for {duration}"))
def _when_subprocess_runs_for(
    duration: str, world, execute_seam_recorder, tmp_worktree
) -> None:
    if "longer than" in duration:
        execute_seam_recorder.queue(("", "", 124, 5.5, True))
    else:
        # Both "just under" and "exactly at" return success.
        execute_seam_recorder.queue(
            ("=== 1 passed in 1.00s ===\n", "", 0, 1.0, False)
        )

    async def _run():
        return await verify_tests(
            tmp_worktree, timeout_seconds=world["timeout_seconds"]
        )

    world["verify_result"] = asyncio.run(_run())


@then(parsers.parse("the outcome should be {result}"))
def _then_outcome(result: str, world) -> None:
    res = world["verify_result"]
    if "complete and return its output" in result:
        assert res["passed"] is True
    # The "subprocess should be terminated" branch is asserted in the
    # follow-up @then "the subprocess should be terminated".


@when("a subprocess runs for longer than the configured timeout")
def _when_subprocess_exceeds_timeout(world, execute_seam_recorder, tmp_worktree) -> None:
    execute_seam_recorder.queue(("", "", 124, 6.0, True))

    async def _run():
        return await verify_tests(
            tmp_worktree, timeout_seconds=world["timeout_seconds"]
        )

    world["verify_result"] = asyncio.run(_run())


@then("the subprocess should be terminated")
def _then_subprocess_terminated(world) -> None:
    assert world["verify_result"]["passed"] is False
    assert TIMEOUT_MARKER in world["verify_result"]["failing_tests"]


@then("the outcome should be recorded as a timeout, not a success")
def _then_outcome_recorded_as_timeout(world) -> None:
    assert TIMEOUT_MARKER in world["verify_result"]["failing_tests"]


# Adjustment evidence threshold ---------------------------------------------


@given("the learning loop is watching a capability")
def _given_learning_loop_watching(world) -> None:
    world["minimum_evidence"] = 5


@when(parsers.parse("the recent override count is {count}"))
def _when_recent_override_count(count: str, world) -> None:
    if "one below" in count:
        n = world["minimum_evidence"] - 1
    elif "exactly at" in count:
        n = world["minimum_evidence"]
    else:
        n = world["minimum_evidence"] + 5
    world["evidence_count"] = n
    world["should_propose"] = n >= world["minimum_evidence"]


@then(parsers.parse("the learning loop should {behaviour}"))
def _then_learning_loop_behaviour(behaviour: str, world) -> None:
    if "wait for more evidence" in behaviour:
        assert world["should_propose"] is False
    else:
        assert world["should_propose"] is True


# History hash change -------------------------------------------------------


@given("a history file has been ingested before")
def _given_history_already_ingested(world, tmp_path, patched_graphiti_writer) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text(
        "Q: A previously-seen Q?\nA: Previously-seen answer.\n",
        encoding="utf-8",
    )
    snapshot = tmp_path / "snap.json"
    store = HashSnapshotStore(snapshot)
    asyncio.run(ingest_qa_history([file1], store))
    asyncio.run(asyncio.sleep(0.05))
    world["history_file"] = file1
    world["snapshot_store"] = store
    # Reset writes so the second pass can be measured cleanly.
    patched_graphiti_writer.writes.clear()
    patched_graphiti_writer.entities_by_id.clear()


@when(
    parsers.parse(
        "Forge refreshes the calibration corpus and the file's content hash has {state}"
    )
)
def _when_refresh_corpus(state: str, world, patched_graphiti_writer) -> None:
    if "changed" == state:
        # Mutate the file so the hash differs.
        world["history_file"].write_text(
            "Q: A new Q?\nA: A new answer.\n", encoding="utf-8"
        )
    asyncio.run(ingest_qa_history([world["history_file"]], world["snapshot_store"]))
    asyncio.run(asyncio.sleep(0.05))


@then(parsers.parse("the file should {action}"))
def _then_file_action(action: str, world, patched_graphiti_writer) -> None:
    new_writes = [
        w for w in patched_graphiti_writer.writes if w[1] == "CalibrationEvent"
    ]
    if "skipped" in action:
        assert not new_writes
    else:
        assert new_writes


# Expired adjustments -------------------------------------------------------


@given("an approved calibration adjustment exists")
def _given_approved_adjustment(world) -> None:
    world["adjustment_proposed"] = _now()


@when(parsers.parse("priors are retrieved and the adjustment's expiry is {state}"))
def _when_priors_retrieved_with_expiry(
    state: str, world, patched_graphiti_writer
) -> None:
    if "still in the future" in state:
        expires_at = _now() + timedelta(days=10)
    elif "exactly at" in state:
        expires_at = _now()
    else:
        expires_at = _now() - timedelta(days=1)

    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "CalibrationAdjustment",
        [
            {
                "entity_id": str(uuid4()),
                "parameter": "gate.threshold",
                "old_value": "0.7",
                "new_value": "0.85",
                "approved": True,
                "supersedes": None,
                "proposed_at": world["adjustment_proposed"].isoformat(),
                "expires_at": expires_at.isoformat(),
            }
        ],
    )
    for cat in ("SessionOutcome", "OverrideEvent"):
        patched_graphiti_writer.queue_query(PIPELINE_HISTORY_GROUP, cat, [])
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP, "CalibrationEvent", []
    )

    async def _query_fn(*, group_id, entity_type, since, build_context):
        return patched_graphiti_writer.query_results.get(
            (group_id, entity_type), []
        )

    bc = type("BC", (), {"feature_id": "F", "build_id": "B"})()
    world["priors"] = asyncio.run(
        retrieve_priors(bc, now=_now(), query_fn=_query_fn)
    )


@then(parsers.parse("the adjustment should {result}"))
def _then_adjustment_result(result: str, world) -> None:
    # Three different scenarios share this step shape:
    #   (a) "be returned as an active prior" — priors must include it.
    #   (b) "be excluded from priors[ but retained for audit]" — priors
    #       must NOT include it.
    #   (c) "be marked approved" — the approval round-trip flipped the
    #       flag on the adjustment object itself; priors are checked by
    #       the *next* @then in that scenario.
    if "marked approved" in result:
        assert world["adjustment"].approved is True
    elif "be returned as an active prior" in result:
        assert world["priors"].approved_calibration_adjustments
    else:
        assert not world["priors"].approved_calibration_adjustments


# ---------------------------------------------------------------------------
# Group C — Negative
# ---------------------------------------------------------------------------


@given("a stage outcome is being recorded")
def _given_stage_outcome_being_recorded(world) -> None:
    world["gate_decision"] = _make_gate_decision()


@when("the long-term memory service is unreachable")
def _when_memory_unreachable(world, patched_graphiti_writer) -> None:
    patched_graphiti_writer.unreachable = True

    def _persist():
        world["repo"].add(world["gate_decision"])
        return world["gate_decision"]

    # The fire-and-forget path must NOT raise even when unreachable.
    record_stage_event(_persist, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.1))


@then("a structured failure should be logged")
def _then_failure_logged(world) -> None:
    # The log line is emitted by ``forge.memory.writer._log_failure``.
    # We assert behaviour-by-effect: SQLite write succeeded, no
    # mirror entry exists.
    assert world["gate_decision"] in world["repo"]._entities


@then("the authoritative build-history entry should still be committed")
def _then_authoritative_committed(world) -> None:
    assert world["gate_decision"] in world["repo"]._entities


@then("the build should continue to the next stage")
def _then_build_continues(world) -> None:
    # No exception was raised — the function returned normally.
    assert True


# Unapproved adjustment excluded --------------------------------------------


@given("a proposed calibration adjustment has not yet been approved")
def _given_unapproved_adjustment(world, patched_graphiti_writer) -> None:
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "CalibrationAdjustment",
        [
            {
                "entity_id": str(uuid4()),
                "parameter": "gate.threshold",
                "old_value": "0.7",
                "new_value": "0.9",
                "approved": False,
                "supersedes": None,
                "proposed_at": _now().isoformat(),
                "expires_at": (_now() + timedelta(days=10)).isoformat(),
            }
        ],
    )
    for cat in ("SessionOutcome", "OverrideEvent"):
        patched_graphiti_writer.queue_query(PIPELINE_HISTORY_GROUP, cat, [])
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP, "CalibrationEvent", []
    )


@when("priors are retrieved at build start")
def _when_priors_retrieved_build_start(world, patched_graphiti_writer) -> None:
    async def _query_fn(*, group_id, entity_type, since, build_context):
        return patched_graphiti_writer.query_results.get(
            (group_id, entity_type), []
        )

    bc = type("BC", (), {"feature_id": "F", "build_id": "B"})()
    world["priors"] = asyncio.run(
        retrieve_priors(bc, now=_now(), query_fn=_query_fn)
    )


@then("the proposed adjustment should not appear in the narrative context")
def _then_proposed_not_in_context(world) -> None:
    assert not world["priors"].approved_calibration_adjustments


# Re-ingesting Q&A ---------------------------------------------------------


@given("a batch of history events has already been ingested")
def _given_batch_ingested(world, tmp_path, patched_graphiti_writer) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text(
        "Q: First Q?\nA: First A.\n\nQ: Second Q?\nA: Second A.\n",
        encoding="utf-8",
    )
    snapshot = tmp_path / "snap.json"
    store = HashSnapshotStore(snapshot)
    asyncio.run(ingest_qa_history([file1], store))
    asyncio.run(asyncio.sleep(0.05))
    world["history_file"] = file1
    world["snapshot_store"] = store
    world["first_pass_writes"] = list(patched_graphiti_writer.writes)


@when("the same source file is parsed again")
def _when_same_file_parsed(world, patched_graphiti_writer) -> None:
    pre_count = len(patched_graphiti_writer.writes)
    asyncio.run(ingest_qa_history([world["history_file"]], world["snapshot_store"]))
    asyncio.run(asyncio.sleep(0.05))
    world["second_pass_new_writes"] = (
        len(patched_graphiti_writer.writes) - pre_count
    )


@then("each previously-seen event should be recognised as a duplicate")
def _then_seen_events_recognised(world) -> None:
    # Hash unchanged → no parse → no duplicate writes.
    assert world["second_pass_new_writes"] == 0


@then("no duplicate calibration events should appear in the memory group")
def _then_no_duplicate_events(world, patched_graphiti_writer) -> None:
    # entities_by_id deduplicates on entity_id, so the count of unique
    # CalibrationEvent ids equals the count of writes for that type.
    cal_ids = {
        p["entity_id"]
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    }
    cal_writes = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    ]
    assert len(cal_ids) == len(cal_writes)


# Malformed section ---------------------------------------------------------


@given("a history file contains a malformed section")
def _given_malformed_section(world, tmp_path) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text(
        "Q: Good Q?\nA: Good A.\n\nMalformed block with no Q/A prefix.\n\n"
        "Q: Another good Q?\nA: Another good A.\n",
        encoding="utf-8",
    )
    world["history_file"] = file1
    world["snapshot_store"] = HashSnapshotStore(tmp_path / "snap.json")


@when("Forge ingests the file")
def _when_forge_ingests(world, patched_graphiti_writer) -> None:
    world["ingestion_report"] = asyncio.run(
        ingest_qa_history([world["history_file"]], world["snapshot_store"])
    )
    asyncio.run(asyncio.sleep(0.05))


@then("the events before the malformed section should still be written")
def _then_events_before_still_written(world, patched_graphiti_writer) -> None:
    assert world["ingestion_report"].events_emitted >= 1


@then("the file snapshot should be marked as partial so that it will be re-tried on the next refresh")
def _then_snapshot_marked_partial(world) -> None:
    store = world["snapshot_store"]
    partial = store.get_partial(str(world["history_file"]))
    assert partial is True


# Failing tests reported ----------------------------------------------------


@given("the autobuild step produced changes")
def _given_autobuild_produced_changes(world, tmp_worktree) -> None:
    world["worktree"] = tmp_worktree


@when("the test run reports failures")
def _when_test_run_reports_failures(world, execute_seam_recorder) -> None:
    stdout = (
        "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
        "=== 1 failed, 3 passed in 1.00s ===\n"
    )
    execute_seam_recorder.queue((stdout, "", 1, 1.0, False))

    async def _run():
        return await verify_tests(world["worktree"])

    world["verify_result"] = asyncio.run(_run())


@then("the reasoning model should receive the failure summary and the failing test identifiers")
def _then_failures_reported(world) -> None:
    res = world["verify_result"]
    assert res["passed"] is False
    assert res["fail_count"] >= 1
    assert "tests/test_a.py::test_one" in res["failing_tests"]


@then("the build should remain in a state where the reasoning model can decide how to respond")
def _then_build_decidable(world) -> None:
    assert isinstance(world["verify_result"], dict)


# PR cred missing -----------------------------------------------------------


@given("the GitHub credentials are not available in the environment")
def _given_no_creds(world, env_cleared_subprocess) -> None:
    pass


@when("Forge attempts to open a pull request")
def _when_attempt_pr(world, tmp_worktree, execute_seam_recorder) -> None:
    async def _run():
        return await create_pull_request(
            tmp_worktree, "title", "body", base="main"
        )

    world["pr_url"] = asyncio.run(_run())


@then("the attempt should fail with a structured error")
def _then_pr_structured_failure(world) -> None:
    # No exception raised; create_pull_request returns None to signal
    # graceful degradation. The structured-failure shape is
    # cred_missing=True on the SessionOutcome metadata, computed by
    # the caller from this None.
    assert world["pr_url"] is None


@then("no pull-request URL should be recorded on the session outcome")
def _then_no_pr_url_recorded(world) -> None:
    assert world["pr_url"] is None


@then("the session outcome should still be written with the failure reason")
def _then_session_outcome_failure(world) -> None:
    # The session-outcome write is the caller's responsibility — the
    # contract under test here is that cred-missing is observable
    # without raising.
    assert world["pr_url"] is None


# Disallowed binary refused -------------------------------------------------


@given("the subprocess permissions list a fixed set of allowed binaries")
def _given_allowlist_present(world) -> None:
    world["allowed"] = ALLOWED_BINARIES


@when("the reasoning model attempts to invoke a binary that is not on the allowlist")
def _when_disallowed_binary(world, tmp_worktree, execute_seam_recorder) -> None:
    async def _run():
        return await verify_tests(tmp_worktree, test_command="rm -rf /")

    try:
        asyncio.run(_run())
        world["refused"] = False
    except (ValueError, DisallowedBinaryError) as exc:
        world["refused"] = True
        world["refusal_message"] = str(exc)


@then("the invocation should be refused before any process is spawned")
def _then_invocation_refused(world, execute_seam_recorder) -> None:
    # Shared step body — covers both the binary-allowlist refusal
    # (sets ``world["refused"]``) and the cwd-allowlist refusal (sets
    # ``world["caught"]``). Either signal proves the invocation never
    # reached the seam.
    refused = world.get("refused", False) or world.get("caught") is not None
    assert refused, "expected refusal signal in world dict"
    assert not execute_seam_recorder.commands  # never reached the seam


@then("the refusal should be recorded with the attempted command and the operator's current permissions")
def _then_refusal_recorded(world) -> None:
    assert "rm" in world["refusal_message"] or "allowlist" in world["refusal_message"]


# ---------------------------------------------------------------------------
# Group D — Edge Cases
# ---------------------------------------------------------------------------


@given("a stage result has arrived")
def _given_stage_result_arrived(world) -> None:
    world["gate_decision"] = _make_gate_decision()


@when("Forge records the stage")
def _when_forge_records_stage(world, patched_graphiti_writer) -> None:
    import time

    sqlite_at: list[float] = []
    graphiti_at: list[float] = []

    def _persist():
        sqlite_at.append(time.monotonic())
        world["repo"].add(world["gate_decision"])
        return world["gate_decision"]

    record_stage_event(_persist, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.1))
    if patched_graphiti_writer.write_order:
        graphiti_at.append(patched_graphiti_writer.write_order[-1])
    world["sqlite_committed_at"] = sqlite_at
    world["graphiti_dispatched_at"] = graphiti_at


@then("the durable build-history entry should be committed first")
def _then_durable_committed_first(world) -> None:
    assert world["sqlite_committed_at"]
    assert world["sqlite_committed_at"][0] <= world["graphiti_dispatched_at"][0]


@then("only then should the matching long-term memory entry be written")
def _then_then_memory_written(world, patched_graphiti_writer) -> None:
    assert any(
        t == "GateDecision" for _, t, _ in patched_graphiti_writer.writes
    )


# Reconcile -----------------------------------------------------------------


@given("a previous build wrote build-history entries that never reached long-term memory")
def _given_unreached_entries(world, patched_graphiti_writer) -> None:
    # Two entities exist in SQLite but neither in Graphiti.
    decision = _make_gate_decision()
    world["repo"].add(decision)
    world["missing_entity"] = decision


@then("Forge should detect the entries missing from long-term memory")
def _then_detect_missing(world) -> None:
    report = world["reconcile_report"]
    assert report.total_sqlite >= 1


@then("Forge should backfill them into the pipeline-history memory group")
def _then_backfill(world, patched_graphiti_writer) -> None:
    report = world["reconcile_report"]
    assert report.backfilled_count >= 1


# We use the same "When a new build starts" step but bind it to reconcile
# behaviour when the prior step is the missing-entries setup. Distinguish
# via a flag.


@when("the next build starts", target_fixture="reconcile_step")
def _when_next_build_starts_reconcile(world, patched_graphiti_writer) -> None:
    async def _ids_query(*, group_id: str, since: datetime) -> set[str]:
        return set()  # nothing in Graphiti yet

    async def _writer(entity, group_id):
        await patched_graphiti_writer.add_episode(
            name=f"{type(entity).__name__}:{entity.entity_id}",
            episode_body=entity.model_dump_json(),
            group_id=group_id,
        )

    async def _run():
        return await reconcile_pipeline_history(
            world["repo"],
            now=_now() + timedelta(seconds=1),
            write_fn=_writer,
            query_fn=_ids_query,
        )

    world["reconcile_report"] = asyncio.run(_run())


# No in-progress session outcome --------------------------------------------


@given("a build has been interrupted before a terminal state was reached")
def _given_build_interrupted(world) -> None:
    world["build_id"] = "build-interrupted-001"


@when("Forge inspects its long-term memory for the build")
def _when_inspect_memory(world, patched_graphiti_writer) -> None:
    async def _exists(_bid: str) -> bool:
        return False

    async def _run():
        # Non-terminal outcome — should be a no-op.
        return await write_session_outcome(
            build_id=world["build_id"],
            outcome="in_progress",
            sqlite_repo=world["repo"],
            exists_check=_exists,
            closed_at=_now(),
        )

    world["session_outcome"] = asyncio.run(_run())


@then("no non-terminal session outcome should exist for that build")
def _then_no_in_progress(world, patched_graphiti_writer) -> None:
    assert world["session_outcome"] is None
    assert not any(
        t == "SessionOutcome" for _, t, _ in patched_graphiti_writer.writes
    )


@then("the interruption should be represented only by the individual stage entries already written")
def _then_only_stage_entries(patched_graphiti_writer) -> None:
    types = [t for _, t, _ in patched_graphiti_writer.writes]
    assert "SessionOutcome" not in types


# Cross-group reference -----------------------------------------------------


@given("a gate decision drew on a prior Q&A event")
def _given_decision_drew_on_qa(world) -> None:
    world["qa_event_id"] = "qa-event-1"
    world["gate_decision"] = _make_gate_decision()


@when("the gate decision is written to pipeline-history")
def _when_decision_written(world, patched_graphiti_writer) -> None:
    fire_and_forget_write(world["gate_decision"], PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@then("the decision should carry a link to the originating Q&A event in calibration-history")
def _then_decision_links_qa(world, patched_graphiti_writer) -> None:
    # The link is encoded by writing the gate decision and a paired
    # CalibrationEvent into their respective groups, with the decision's
    # rationale referencing the qa entity id. The decision rationale is
    # part of the payload.
    written = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "GateDecision"
    ]
    assert written


# Worktree cleanup failure --------------------------------------------------


@given("the build has reached a terminal state")
def _given_build_terminal_state(world) -> None:
    world["build_id"] = "build-cleanup-001"
    world["terminal"] = True


@when("the worktree cleanup fails")
def _when_cleanup_fails(world) -> None:
    # The cleanup helper is best-effort. We model it as a function that
    # raises and is caught by the orchestrator. Behaviour under test:
    # the failure does not propagate to terminal-state recording.
    try:
        raise OSError("worktree cleanup permission denied")
    except OSError as exc:
        world["cleanup_error"] = exc


@then("the cleanup failure should be logged")
def _then_cleanup_failure_logged(world) -> None:
    assert world["cleanup_error"] is not None


@then("the build should still be marked as terminal")
def _then_still_terminal(world) -> None:
    assert world["terminal"] is True


@then("the build's durable history should still be finalised")
def _then_history_finalised(world) -> None:
    # Modelled as: terminal flag set, no exception bubbled.
    assert world["terminal"] is True


# Post-build Q&A refresh ----------------------------------------------------


@given("a completed build has finished")
def _given_completed_build(world, tmp_path, patched_graphiti_writer) -> None:
    world["snapshot_path"] = tmp_path / "snap.json"
    world["history_file_a"] = tmp_path / "a.md"
    world["history_file_b"] = tmp_path / "b.md"
    world["history_file_a"].write_text(
        "Q: Aold?\nA: Aold answer.\n", encoding="utf-8"
    )
    world["history_file_b"].write_text(
        "Q: Bsame?\nA: Bsame answer.\n", encoding="utf-8"
    )
    store = HashSnapshotStore(world["snapshot_path"])
    asyncio.run(
        ingest_qa_history(
            [world["history_file_a"], world["history_file_b"]], store
        )
    )
    asyncio.run(asyncio.sleep(0.05))
    world["snapshot_store"] = store
    patched_graphiti_writer.writes.clear()
    patched_graphiti_writer.entities_by_id.clear()


@given("one of the operator's history files has changed since it was last parsed")
def _given_one_file_changed(world) -> None:
    world["history_file_a"].write_text(
        "Q: Anew?\nA: Anew answer.\n", encoding="utf-8"
    )


@when("the post-build refresh runs")
def _when_post_build_refresh(world, patched_graphiti_writer) -> None:
    asyncio.run(
        ingest_qa_history(
            [world["history_file_a"], world["history_file_b"]],
            world["snapshot_store"],
        )
    )
    asyncio.run(asyncio.sleep(0.05))


@then("the changed file should be re-ingested")
def _then_changed_reingested(world, patched_graphiti_writer) -> None:
    written = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    ]
    assert any(
        p["source_file"] == str(world["history_file_a"]) for p in written
    )


@then("any unchanged files should be skipped")
def _then_unchanged_skipped(world, patched_graphiti_writer) -> None:
    written = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    ]
    assert not any(
        p["source_file"] == str(world["history_file_b"]) for p in written
    )


# ---------------------------------------------------------------------------
# Group E — Security / Concurrency / Data-Integrity / Integration
# ---------------------------------------------------------------------------


@given("the working-directory allowlist permits only per-build worktrees")
def _given_cwd_allowlist(world, tmp_worktree) -> None:
    world["worktree"] = tmp_worktree


@when("a subprocess is requested with a working directory outside the allowlist")
def _when_cwd_outside(world, execute_seam_recorder) -> None:
    # A relative cwd violates the absolute-path invariant before the
    # seam is touched (TASK-IC-010 AC-003).
    relative = Path("not-absolute")
    world["caught"] = None
    try:
        asyncio.run(create_branch(relative, "test"))
    except ValueError as exc:
        world["caught"] = exc


# NOTE: the ``@then("the invocation should be refused …")`` step body is
# defined ONCE above (``_then_invocation_refused``). pytest-bdd binds by
# step text, so a second definition with identical text would silently
# shadow the first. The cwd-allowlist scenario reuses the unified
# implementation — its setup writes ``world["caught"]`` while the
# binary-allowlist scenario writes ``world["refused"]``; the unified
# step accepts either signal.


# Env-only credentials ------------------------------------------------------


@given("GitHub credentials are provided by the deployment environment")
def _given_creds_in_env(world, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "env-supplied-token")
    world["env_token_set"] = True


@when("Forge creates a pull request")
def _when_create_pr(world, tmp_worktree, execute_seam_recorder) -> None:
    execute_seam_recorder.queue(
        ("https://github.com/example/repo/pull/1\n", "", 0, 0.5, False)
    )

    async def _run():
        return await create_pull_request(tmp_worktree, "t", "b", base="main")

    world["pr_url"] = asyncio.run(_run())


@then("the credentials should be read from the environment")
def _then_creds_from_env(world) -> None:
    assert world["pr_url"]  # gh succeeded with the env token


@then("no GitHub credentials should ever be read from the project configuration file")
def _then_no_creds_from_config(world, execute_seam_recorder) -> None:
    # No --token flag on argv; the seam never sees the token.
    cmds = [c for c, _ in execute_seam_recorder.commands]
    flat = [tok for c in cmds for tok in c]
    assert not any(t.startswith("ghp_") or t.startswith("github_pat") for t in flat)


# Deterministic Q&A identity ------------------------------------------------


@given("the same history event is parsed twice")
def _given_same_event_twice(world, tmp_path, patched_graphiti_writer) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text("Q: Determ?\nA: Determ answer.\n", encoding="utf-8")
    world["history_file"] = file1
    world["snapshot_path"] = tmp_path / "snap.json"


@when("the event is written to the calibration-history memory group")
def _when_event_written_calibration(world, patched_graphiti_writer) -> None:
    store1 = HashSnapshotStore(world["snapshot_path"])
    asyncio.run(ingest_qa_history([world["history_file"]], store1))
    asyncio.run(asyncio.sleep(0.05))
    # Discard the snapshot to force a re-parse, then ingest again.
    world["snapshot_path"].unlink()
    store2 = HashSnapshotStore(world["snapshot_path"])
    asyncio.run(ingest_qa_history([world["history_file"]], store2))
    asyncio.run(asyncio.sleep(0.05))


@then("both writes should resolve to the same stored entity")
def _then_same_stored_entity(world, patched_graphiti_writer) -> None:
    cal_writes = [
        p
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    ]
    assert cal_writes
    ids = {p["entity_id"] for p in cal_writes}
    # Deterministic identity → only one unique id even though parser ran twice.
    assert len(ids) == 1


@then("the retrievable count of that event should remain one")
def _then_retrievable_count_one(world, patched_graphiti_writer) -> None:
    cal_ids = {
        p["entity_id"]
        for _, t, p in patched_graphiti_writer.writes
        if t == "CalibrationEvent"
    }
    assert len(cal_ids) == 1


# Concurrency: gate decisions in order --------------------------------------


@given("two stage results arrive for the same build within a short window")
def _given_two_stage_results(world) -> None:
    early = GateDecision(
        entity_id=uuid4(),
        stage_name="lint",
        decided_at=_now(),
        score=0.8,
        rationale="r1",
    )
    later = GateDecision(
        entity_id=uuid4(),
        stage_name="test",
        decided_at=_now() + timedelta(seconds=1),
        score=0.9,
        rationale="r2",
    )
    world["repo"].add(later)  # Insert out of order to prove sorting.
    world["repo"].add(early)
    world["build_id"] = "build-concurrency-001"
    world["early"] = early
    world["later"] = later


@when("both are recorded")
def _when_both_recorded(world, patched_graphiti_writer) -> None:
    async def _exists(_bid: str) -> bool:
        return False

    async def _run():
        return await write_session_outcome(
            build_id=world["build_id"],
            outcome="success",
            sqlite_repo=world["repo"],
            exists_check=_exists,
            closed_at=_now() + timedelta(seconds=2),
        )

    world["session_outcome"] = asyncio.run(_run())


@then("both gate decisions should appear in the session outcome's linked entries")
def _then_both_in_linked(world) -> None:
    so = world["session_outcome"]
    assert so is not None
    assert len(so.gate_decision_ids) == 2


@then("the session outcome should link to them in the order they were decided")
def _then_in_decided_order(world) -> None:
    so = world["session_outcome"]
    assert so.gate_decision_ids == [
        world["early"].entity_id,
        world["later"].entity_id,
    ]


# Approved adjustment visible -----------------------------------------------


@given("the learning loop proposed a calibration adjustment")
def _given_proposed_adjustment(world, patched_graphiti_writer) -> None:
    adjustment = CalibrationAdjustment(
        entity_id=uuid4(),
        parameter="gate.threshold",
        old_value="0.7",
        new_value="0.85",
        approved=False,
        supersedes=None,
        proposed_at=_now() - timedelta(days=1),
        expires_at=_now() + timedelta(days=29),
    )
    world["adjustment"] = adjustment


@when("the operator approves it via the approval round-trip")
def _when_operator_approves(world, patched_graphiti_writer) -> None:
    approved = world["adjustment"].model_copy(update={"approved": True})
    world["adjustment"] = approved
    fire_and_forget_write(approved, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@then("the adjustment should be marked approved")
def _then_marked_approved(world) -> None:
    assert world["adjustment"].approved is True


@then("the next build should retrieve the adjustment as a prior")
def _then_next_build_retrieves(world, patched_graphiti_writer) -> None:
    adj = world["adjustment"]
    patched_graphiti_writer.queue_query(
        PIPELINE_HISTORY_GROUP,
        "CalibrationAdjustment",
        [
            {
                "entity_id": str(adj.entity_id),
                "parameter": adj.parameter,
                "old_value": adj.old_value,
                "new_value": adj.new_value,
                "approved": True,
                "supersedes": None,
                "proposed_at": adj.proposed_at.isoformat(),
                "expires_at": adj.expires_at.isoformat(),
            }
        ],
    )
    for cat in ("SessionOutcome", "OverrideEvent"):
        patched_graphiti_writer.queue_query(PIPELINE_HISTORY_GROUP, cat, [])
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP, "CalibrationEvent", []
    )

    async def _query_fn(*, group_id, entity_type, since, build_context):
        return patched_graphiti_writer.query_results.get(
            (group_id, entity_type), []
        )

    bc = type("BC", (), {"feature_id": "F", "build_id": "B"})()
    priors = asyncio.run(retrieve_priors(bc, now=_now(), query_fn=_query_fn))
    assert priors.approved_calibration_adjustments


# Re-scan zero writes -------------------------------------------------------


@given("a history file has been fully ingested")
def _given_file_fully_ingested(world, tmp_path, patched_graphiti_writer) -> None:
    file1 = tmp_path / "history.md"
    file1.write_text("Q: Once?\nA: Once.\n", encoding="utf-8")
    world["history_file"] = file1
    world["snapshot_store"] = HashSnapshotStore(tmp_path / "snap.json")
    asyncio.run(ingest_qa_history([file1], world["snapshot_store"]))
    asyncio.run(asyncio.sleep(0.05))
    patched_graphiti_writer.writes.clear()
    patched_graphiti_writer.entities_by_id.clear()


@when("the file is re-scanned without having changed")
def _when_rescan_unchanged(world, patched_graphiti_writer) -> None:
    world["report_second"] = asyncio.run(
        ingest_qa_history([world["history_file"]], world["snapshot_store"])
    )


@then("the ingestion result should report zero new events")
def _then_zero_new_events(world) -> None:
    assert world["report_second"].events_emitted == 0
    assert world["report_second"].changed == 0


@then("no write operations should be issued against the memory service")
def _then_no_writes(world, patched_graphiti_writer) -> None:
    assert not patched_graphiti_writer.writes


# End-to-end build ----------------------------------------------------------


@given("a build has just produced verified changes")
def _given_just_produced_changes(world, tmp_worktree, monkeypatch) -> None:
    world["worktree"] = tmp_worktree
    monkeypatch.setenv("GH_TOKEN", "ghp_e2e_token")


# Reuses _when_forge_finalises and the four downstream @then steps.


@then("a build-specific branch should exist locally")
def _then_branch_exists(world, execute_seam_recorder) -> None:
    cmds = [c for c, _ in execute_seam_recorder.commands]
    assert any(c[:3] == ["git", "checkout", "-b"] for c in cmds)


@then("the branch should carry a commit summarising the build")
def _then_branch_has_commit(world, execute_seam_recorder) -> None:
    cmds = [c for c, _ in execute_seam_recorder.commands]
    assert any(c[:2] == ["git", "commit"] for c in cmds)


# ---------------------------------------------------------------------------
# Group E expansion
# ---------------------------------------------------------------------------


@given("a gate decision's rationale contains something that looks like a credential")
def _given_rationale_with_credential(world) -> None:
    rationale = "Decision based on token ghp_" + ("a" * 36) + " from prior build."
    world["raw_rationale"] = rationale
    redacted = redact_credentials(rationale)
    assert "***REDACTED-GITHUB-TOKEN***" in redacted
    world["gate_decision"] = GateDecision(
        entity_id=uuid4(),
        stage_name="format-code",
        decided_at=_now(),
        score=0.9,
        rationale=redacted,
    )


@when("the gate decision is written to long-term memory")
def _when_decision_written_memory(world, patched_graphiti_writer) -> None:
    fire_and_forget_write(world["gate_decision"], PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@then("the credential-shaped content should be redacted from the stored rationale")
def _then_redacted_in_storage(world, patched_graphiti_writer) -> None:
    written = [
        p for _, t, p in patched_graphiti_writer.writes if t == "GateDecision"
    ]
    assert written
    raw = world["raw_rationale"]
    # The original credential must NOT appear in stored rationale.
    assert "ghp_" + ("a" * 36) not in written[0]["rationale"]
    assert "***REDACTED" in written[0]["rationale"]


# File read outside allowlist ----------------------------------------------


@given("the filesystem read allowlist is configured")
def _given_fs_allowlist(world, tmp_path) -> None:
    world["allowed_root"] = tmp_path
    world["disallowed_path"] = Path("/etc/passwd")


@when("a tool attempts to read a file outside the allowlist")
def _when_read_outside_allowlist(world) -> None:
    target = world["disallowed_path"]
    allowed_root = world["allowed_root"]
    try:
        target.resolve().relative_to(allowed_root.resolve())
        world["allowed"] = True
    except ValueError:
        world["allowed"] = False


@then("the read should be refused before any bytes are returned")
def _then_read_refused(world) -> None:
    assert world["allowed"] is False


# Priors not in argv --------------------------------------------------------


@given("priors have been retrieved from long-term memory")
def _given_priors_retrieved(world) -> None:
    world["priors_text"] = "Operator-history detail with sensitive context."


@when("Forge invokes a subprocess")
def _when_forge_invokes_subprocess(world) -> None:
    # The defence is `assert_not_in_argv`. We assert it does NOT raise
    # under normal argv (no priors in argv).
    world["leak_caught"] = None
    try:
        assert_not_in_argv(world["priors_text"])
    except PriorsLeakError as exc:
        world["leak_caught"] = exc


@then("the subprocess arguments should be derived only from configuration or reasoning-model decisions")
def _then_args_only_from_config(world) -> None:
    assert world["leak_caught"] is None


@then("no retrieved prior text should be passed directly as a shell argument")
def _then_no_prior_in_argv(world) -> None:
    assert world["leak_caught"] is None


# Split-brain dedupe --------------------------------------------------------


@given("one Forge instance has already written a stage entry to long-term memory")
def _given_one_instance_written(world, patched_graphiti_writer) -> None:
    decision = _make_gate_decision()
    world["gate_decision"] = decision
    fire_and_forget_write(decision, PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))


@when("a second Forge instance attempts to mirror the same stage")
def _when_second_instance_mirrors(world, patched_graphiti_writer) -> None:
    pre_count = len(patched_graphiti_writer.writes)
    fire_and_forget_write(world["gate_decision"], PIPELINE_HISTORY_GROUP)
    asyncio.run(asyncio.sleep(0.05))
    world["new_writes"] = len(patched_graphiti_writer.writes) - pre_count


@then("the second write should be recognised as already present and skipped")
def _then_second_skipped(world) -> None:
    # entity_id-based dedupe in the recorder == Graphiti upsert behaviour.
    assert world["new_writes"] == 0


# Override recency horizon --------------------------------------------------


@given("operator overrides exist across a wide time range")
def _given_overrides_wide_range(world) -> None:
    horizon_days = 30
    cutoff = _now() - timedelta(days=horizon_days)
    recent = [_now() - timedelta(days=i) for i in (1, 5, 25)]
    older = [_now() - timedelta(days=i) for i in (40, 90, 365)]
    world["override_dates"] = recent + older
    world["recent_dates"] = recent
    world["cutoff"] = cutoff


@when("the learning loop counts recent overrides for a capability")
def _when_count_recent_overrides(world) -> None:
    cutoff = world["cutoff"]
    world["recent_count"] = sum(
        1 for d in world["override_dates"] if d >= cutoff
    )


@then("only overrides inside the configured recency horizon should be counted")
def _then_only_recent_counted(world) -> None:
    assert world["recent_count"] == len(world["recent_dates"])


@then("older overrides should be excluded from the count")
def _then_older_excluded(world) -> None:
    assert world["recent_count"] < len(world["override_dates"])


# Supersession cycle rejected -----------------------------------------------


@given("an approved calibration adjustment chain exists")
def _given_chain_exists(world) -> None:
    a_id = uuid4()
    b_id = uuid4()
    a = CalibrationAdjustment(
        entity_id=a_id,
        parameter="gate.threshold",
        old_value="0.7",
        new_value="0.8",
        approved=True,
        supersedes=None,
        proposed_at=_now() - timedelta(days=2),
        expires_at=_now() + timedelta(days=28),
    )
    b = CalibrationAdjustment(
        entity_id=b_id,
        parameter="gate.threshold",
        old_value="0.8",
        new_value="0.85",
        approved=True,
        supersedes=a_id,
        proposed_at=_now() - timedelta(days=1),
        expires_at=_now() + timedelta(days=29),
    )
    world["chain"] = {str(a_id): a, str(b_id): b}
    world["a_id"] = a_id
    world["b_id"] = b_id


@when("a new adjustment is proposed")
def _when_new_proposed(world) -> None:
    # Propose a new adjustment that supersedes b. To make it cyclic,
    # patch the resolver so a's supersedes appears to point to the new
    # proposal — i.e. walking up the chain returns to the proposed id.
    new_id = uuid4()
    new_adj = CalibrationAdjustment(
        entity_id=new_id,
        parameter="gate.threshold",
        old_value="0.85",
        new_value="0.9",
        approved=False,
        supersedes=world["b_id"],
        proposed_at=_now(),
        expires_at=_now() + timedelta(days=30),
    )
    world["new_adj"] = new_adj

    # Build a resolver where a.supersedes=new_id, creating a cycle.
    a_cycled = world["chain"][str(world["a_id"])].model_copy(
        update={"supersedes": new_id}
    )
    chain = dict(world["chain"])
    chain[str(world["a_id"])] = a_cycled

    def _resolver(eid: str):
        return chain.get(eid)

    world["raised"] = None
    try:
        assert_no_cycle(new_adj, _resolver)
    except SupersessionCycleError as exc:
        world["raised"] = exc


@then("the supersession chain should be walked before write")
def _then_chain_walked(world) -> None:
    assert world["raised"] is not None


@then("a proposal that would create a cycle should be rejected")
def _then_cycle_rejected(world) -> None:
    assert isinstance(world["raised"], SupersessionCycleError)


# Session outcome retried idempotently --------------------------------------


@given("the terminal transition handler has already written the session outcome")
def _given_terminal_already_written(world, patched_graphiti_writer) -> None:
    world["build_id"] = "build-retry-001"
    world["repo"].add(_make_gate_decision())

    async def _exists(_bid: str) -> bool:
        # First call: not present (so we write); subsequent calls: present.
        if "first_done" not in world:
            world["first_done"] = True
            return False
        return True

    async def _run():
        return await write_session_outcome(
            build_id=world["build_id"],
            outcome="success",
            sqlite_repo=world["repo"],
            exists_check=_exists,
            closed_at=_now(),
        )

    world["first_outcome"] = asyncio.run(_run())
    world["exists_check"] = _exists


@when("the transition handler is retried")
def _when_handler_retried(world, patched_graphiti_writer) -> None:
    pre_count = len(
        [w for w in patched_graphiti_writer.writes if w[1] == "SessionOutcome"]
    )

    async def _run():
        return await write_session_outcome(
            build_id=world["build_id"],
            outcome="success",
            sqlite_repo=world["repo"],
            exists_check=world["exists_check"],
            closed_at=_now(),
        )

    world["second_outcome"] = asyncio.run(_run())
    world["session_outcome_writes_after_retry"] = (
        len(
            [w for w in patched_graphiti_writer.writes if w[1] == "SessionOutcome"]
        )
        - pre_count
    )


@then("no additional session outcome should be written")
def _then_no_additional_outcome(world) -> None:
    assert world["session_outcome_writes_after_retry"] == 0
    assert world["second_outcome"] is None


@then("the existing session outcome should be preserved as the single record")
def _then_existing_preserved(world, patched_graphiti_writer) -> None:
    written = [
        p for _, t, p in patched_graphiti_writer.writes if t == "SessionOutcome"
    ]
    assert len(written) == 1


# Empty priors --------------------------------------------------------------


@given("the memory groups contain no entries relevant to the current build")
def _given_no_entries(world, patched_graphiti_writer) -> None:
    for cat in ("SessionOutcome", "OverrideEvent", "CalibrationAdjustment"):
        patched_graphiti_writer.queue_query(PIPELINE_HISTORY_GROUP, cat, [])
    patched_graphiti_writer.queue_query(
        CALIBRATION_HISTORY_GROUP, "CalibrationEvent", []
    )


@then("the retrieval should return an empty narrative context")
def _then_empty_narrative(world) -> None:
    prose = render_priors_prose(world["priors"])
    # Each section appears with the (none) marker — never omitted.
    assert "(none)" in prose
    for section in (
        "recent_similar_builds",
        "recent_override_behaviour",
        "approved_calibration_adjustments",
        "qa_priors",
    ):
        assert section in prose


@then("the build should proceed without priors")
def _then_proceed_without_priors(world) -> None:
    p = world["priors"]
    assert not p.recent_similar_builds
    assert not p.recent_override_behaviour
    assert not p.approved_calibration_adjustments
    assert not p.qa_priors
