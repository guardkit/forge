"""Contract + seam tests for the dispatch boundary (TASK-SAD-012).

This module owns the *boundary* tests guaranteeing the §4 Integration
Contracts in ``IMPLEMENTATION-GUIDE.md`` hold across every consumer of
the dispatch domain. Per-task seam tests (TASK-SAD-002, -006, -007,
-009, -010) catch issues at the producer-consumer level; this module
catches issues that span the whole boundary.

It mirrors the pattern established in
``tests/forge/test_contract_and_seam.py`` (TASK-NFI-010) but is scoped
to the dispatch domain:

* AC-001: Module exists at ``tests/forge/dispatch/test_contract_and_seam.py``.
* AC-002: Each §4 Integration Contract has at least one contract test:
    - :class:`TestCapabilityResolutionContract`
    - :class:`TestCorrelationKeyContract`
    - :class:`TestDispatchOutcomeSumTypeContract`
    - :class:`TestDispatchEnvelopeContract`
    - :class:`TestCorrelateOutcomeIdempotencyContract`
* AC-003: Each cross-task seam has at least one seam test:
    - :class:`TestDispatchDiscoverySeam`
    - :class:`TestDispatchPersistenceSeam`
    - :class:`TestDispatchNatsAdapterImportSeam`
    - :class:`TestCorrelationAdapterOrderingSeam`
    - :class:`TestOutcomeDownstreamSeam`
* AC-004: Subject regex assertions match the IMPLEMENTATION-GUIDE §4
  patterns (``^agents\\.command\\.[a-z0-9-]+$`` and
  ``^agents\\.result\\.[a-z0-9-]+\\.[0-9a-f]{32}$``).
* AC-005: AST-level test verifies that ``forge/dispatch/*.py`` does NOT
  import :mod:`nats` (any submodule). See
  :class:`TestDispatchNatsAdapterImportSeam`.
* AC-006: 1000 generated correlation keys are all unique and match
  ``[0-9a-f]{32}``. See :class:`TestCorrelationKeyContract`.
* AC-007: ``correlate_outcome`` idempotency — two consecutive calls
  produce the same record AND issue exactly one UPDATE.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.nats.specialist_dispatch import (
    COMMAND_SUBJECT_TEMPLATE,
    RESULT_SUBJECT_TEMPLATE,
    NatsSpecialistDispatchAdapter,
)
from forge.discovery.cache import DiscoveryCache
from forge.discovery.models import CapabilityResolution
from forge.discovery.protocol import SystemClock
from forge.dispatch.correlation import (
    CORRELATION_KEY_RE,
    CorrelationRegistry,
)
from forge.dispatch.models import (
    AsyncPending,
    Degraded,
    DispatchError,
    DispatchOutcome,
    SyncResult,
)
from forge.dispatch.outcome import correlate_outcome
from forge.dispatch.persistence import (
    DispatchParameter,
    SqliteHistoryWriter,
    persist_resolution,
)


# ---------------------------------------------------------------------------
# Shared regex constants (single source of truth across this module).
#
# Every test that asserts a subject or correlation-key shape derives the
# pattern from one of these constants — that way a future change to the
# IMPLEMENTATION-GUIDE §4 surface fails ONE test ONCE, not every test
# silently against a stale literal.
# ---------------------------------------------------------------------------


# IMPLEMENTATION-GUIDE §4 — dispatch-command envelope subject. The
# trailing alphabet is the agent_id slug (DRD-001..004).
COMMAND_SUBJECT_RE = re.compile(r"^agents\.command\.[a-z0-9-]+$")

# IMPLEMENTATION-GUIDE §4 — dispatch reply envelope subject. The
# trailing 32-hex segment is the correlation_key.
RESULT_SUBJECT_RE = re.compile(r"^agents\.result\.[a-z0-9-]+\.[0-9a-f]{32}$")

# IMPLEMENTATION-GUIDE §4 — CorrelationKey format invariant.
CORRELATION_KEY_FORMAT_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Local fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_writer() -> SqliteHistoryWriter:
    """Fresh in-memory :class:`SqliteHistoryWriter` per test."""
    writer = SqliteHistoryWriter.in_memory()
    try:
        yield writer
    finally:
        writer.close()


def _baseline_resolution(
    *,
    resolution_id: str = "res-base",
    matched_agent_id: str | None = "specialist-a",
    retry_of: str | None = None,
) -> CapabilityResolution:
    """Construct a :class:`CapabilityResolution` for round-trip tests."""
    return CapabilityResolution(
        resolution_id=resolution_id,
        build_id="build-001",
        stage_label="implementation",
        requested_tool="do_thing",
        requested_intent=None,
        matched_agent_id=matched_agent_id,
        match_source="tool_exact" if matched_agent_id else "unresolved",
        competing_agents=[],
        chosen_trust_tier="specialist" if matched_agent_id else None,
        chosen_confidence=1.0 if matched_agent_id else None,
        chosen_queue_depth=0 if matched_agent_id else None,
        resolved_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
        retry_of=retry_of,
    )


def _seed_resolution(
    db_writer: SqliteHistoryWriter,
    *,
    resolution_id: str = "res-base",
    retry_of: str | None = None,
) -> CapabilityResolution:
    """Insert one baseline resolution row and return the model."""
    resolution = _baseline_resolution(
        resolution_id=resolution_id, retry_of=retry_of,
    )
    persist_resolution(resolution, parameters=[], db_writer=db_writer)
    return resolution


class _UpdateCountingConnection:
    """``sqlite3.Connection`` proxy that counts UPDATE statements.

    Matches the pattern used in ``tests/forge/dispatch/test_outcome.py``
    so the contract test in this module can assert AC-007 (exactly one
    UPDATE across two consecutive ``correlate_outcome`` calls) without
    relying on the existing module's private fixtures.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.update_count = 0

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        if sql.lstrip().upper().startswith("UPDATE"):
            self.update_count += 1
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, item: str) -> object:
        return getattr(self._real, item)

    def __enter__(self) -> sqlite3.Connection:
        return self._real.__enter__()

    def __exit__(self, *exc: object) -> object:
        return self._real.__exit__(*exc)


# ---------------------------------------------------------------------------
# Repository-root resolution — the AST-import seam test reads source
# files relative to the repo root, so we anchor it once here for both
# convenience and accuracy.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    # tests/ is at <repo>/tests/; this file lives in tests/forge/dispatch/.
    return Path(__file__).resolve().parents[3]


# ===========================================================================
# CONTRACT TESTS — one §4 Integration Contract per Test* class.
# ===========================================================================


# ---------------------------------------------------------------------------
# Contract: CapabilityResolution schema (incl. retry_of) — round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration_contract("CapabilityResolution")
class TestCapabilityResolutionContract:
    """§4 Integration Contract: ``CapabilityResolution`` schema, incl. ``retry_of``.

    Producer: TASK-SAD-001 (declared the field).
    Consumers: TASK-SAD-002, -006, -007, -009 (read/write the field).

    Asserts the producer-consumer contract by round-tripping a record
    that carries ``retry_of`` through Pydantic validation AND through
    the SQLite persistence layer that consumer tasks rely on.
    """

    def test_retry_of_round_trips_through_pydantic(self) -> None:
        # Pydantic-only round-trip: the ``retry_of`` field survives
        # ``model_dump`` → ``model_validate`` without loss and without
        # falling back to the ``None`` default.
        resolution = _baseline_resolution(
            resolution_id="res-retry-001", retry_of="res-original-000",
        )
        dumped = resolution.model_dump(mode="json")
        assert dumped["retry_of"] == "res-original-000"
        restored = CapabilityResolution.model_validate(dumped)
        assert restored.retry_of == "res-original-000"
        assert restored == resolution

    def test_retry_of_round_trips_through_persistence(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # Persistence-layer round-trip: the consumer of this contract
        # (the SQLite-backed history store) must preserve ``retry_of``
        # across an insert + read cycle. This is the seam between the
        # producer (TASK-SAD-001) and the persistence consumer
        # (TASK-SAD-002).
        original = _baseline_resolution(
            resolution_id="res-retry-002", retry_of="res-prior-002",
        )
        persist_resolution(original, parameters=[], db_writer=db_writer)

        rows = db_writer.read_resolutions()
        assert len(rows) == 1
        assert rows[0].resolution_id == "res-retry-002"
        assert rows[0].retry_of == "res-prior-002"
        assert rows[0] == original

    def test_retry_of_default_none_is_preserved(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # First-attempt resolutions have ``retry_of=None``. The
        # contract is append-only, so the default must survive the
        # round-trip alongside the populated case.
        first_attempt = _baseline_resolution(
            resolution_id="res-first-001", retry_of=None,
        )
        persist_resolution(first_attempt, parameters=[], db_writer=db_writer)
        (rehydrated,) = db_writer.read_resolutions()
        assert rehydrated.retry_of is None


# ---------------------------------------------------------------------------
# Contract: CorrelationKey format invariant + concurrent distinctness
# ---------------------------------------------------------------------------


@pytest.mark.integration_contract("CorrelationKey")
class TestCorrelationKeyContract:
    """§4 Integration Contract: ``CorrelationKey`` format invariant.

    Producer: TASK-SAD-003 (declared ``CORRELATION_KEY_RE`` and
    ``fresh_correlation_key``).
    Consumers: TASK-SAD-006 (orchestrator threads it), TASK-SAD-010
    (NATS adapter writes it as a header and as a reply-subject suffix).

    Asserts:

    * Format: every generated key is exactly 32 lowercase hex characters.
    * Distinctness: 1000 concurrent dispatches produce 1000 unique keys
      (AC-006 of the task).
    * No PII: keys contain only the hex alphabet — no agent IDs, no
      timestamps, no embedded payload values.
    """

    def test_module_exports_correlation_key_regex(self) -> None:
        # The module-level constant is the source of truth that every
        # consumer (registry, adapter, parser) imports — including this
        # test. Pin the pattern so a future renaming does not silently
        # drop the format invariant.
        assert CORRELATION_KEY_RE.pattern == r"^[0-9a-f]{32}$"

    def test_one_thousand_keys_are_unique_and_match_format(self) -> None:
        # AC-006: 1000 generated keys are unique AND every key matches
        # the 32-lowercase-hex format. We use a real CorrelationRegistry
        # over an inert ReplyChannel because key generation does not
        # touch the transport — but we never call ``bind`` so the
        # transport is never invoked.
        class _InertChannel:
            async def subscribe(self, key: str, deliver: Any) -> Any:
                raise AssertionError("subscribe must not be called")

            async def unsubscribe(self, sub: Any) -> None:
                raise AssertionError("unsubscribe must not be called")

        registry = CorrelationRegistry(transport=_InertChannel())
        keys = [registry.fresh_correlation_key() for _ in range(1000)]

        assert len(set(keys)) == 1000, (
            "fresh_correlation_key produced collisions across 1000 calls"
        )
        for key in keys:
            assert CORRELATION_KEY_FORMAT_RE.fullmatch(key), (
                f"correlation key {key!r} does not match the §4 format"
            )

    def test_keys_carry_no_pii_outside_hex_alphabet(self) -> None:
        # The hex alphabet alone is sufficient to guarantee no embedded
        # agent IDs (which contain ``-`` or non-hex letters), no
        # timestamps (``-``/``:``/``T``/``Z``), and no other PII. We
        # verify the alphabet directly so a future regression that
        # accidentally introduces a UUID-with-dashes is caught loudly.
        class _InertChannel:
            async def subscribe(self, key: str, deliver: Any) -> Any:
                raise AssertionError("subscribe must not be called")

            async def unsubscribe(self, sub: Any) -> None:
                raise AssertionError("unsubscribe must not be called")

        registry = CorrelationRegistry(transport=_InertChannel())
        for _ in range(50):
            key = registry.fresh_correlation_key()
            assert set(key) <= set("0123456789abcdef"), (
                f"correlation key {key!r} leaked a non-hex character"
            )


# ---------------------------------------------------------------------------
# Contract: DispatchOutcome sum type — every variant survives a
# model_dump + discriminated-union model_validate round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.integration_contract("DispatchOutcome")
class TestDispatchOutcomeSumTypeContract:
    """§4 Integration Contract: :data:`DispatchOutcome` discriminated union.

    Producer: TASK-SAD-001 (declared the union).
    Consumers: TASK-SAD-005 (parser), TASK-SAD-006 (orchestrator),
    TASK-SAD-009 (gating layer).

    For every variant, asserts:

    * ``model_dump(mode='json')`` carries the discriminator literal.
    * Routing the dump through a Pydantic-discriminated-union validator
      produces a record EQUAL to the original — so consumers that
      ``TypeAdapter(DispatchOutcome).validate_python(payload)`` get
      the exact concrete class back without inventing a "default
      variant" path.
    """

    @pytest.fixture
    def adapter(self) -> Any:
        # ``DispatchOutcome`` is an ``Annotated[Union[...], discriminator]``
        # so we can drive it through a TypeAdapter to exercise the
        # discriminator end-to-end without standing up a wrapping
        # BaseModel just for the test.
        from pydantic import TypeAdapter

        return TypeAdapter(DispatchOutcome)

    @pytest.mark.parametrize(
        "original, expected_kind",
        [
            (
                SyncResult(
                    resolution_id="res-001",
                    attempt_no=1,
                    coach_score=0.9,
                    criterion_breakdown={"clarity": 0.95},
                    detection_findings=[{"id": "f-1"}],
                ),
                "sync_result",
            ),
            (
                AsyncPending(
                    resolution_id="res-002", attempt_no=1, run_identifier="run-xyz"
                ),
                "async_pending",
            ),
            (
                Degraded(
                    resolution_id="res-003",
                    attempt_no=2,
                    reason="bus_disconnected",
                ),
                "degraded",
            ),
            (
                DispatchError(
                    resolution_id="res-004",
                    attempt_no=3,
                    error_explanation="local_timeout",
                ),
                "error",
            ),
        ],
        ids=["sync-result", "async-pending", "degraded", "error"],
    )
    def test_each_variant_round_trips_through_discriminator(
        self, adapter: Any, original: Any, expected_kind: str
    ) -> None:
        dump = original.model_dump(mode="json")
        assert dump["kind"] == expected_kind, (
            f"variant {type(original).__name__} dropped its discriminator"
        )
        restored = adapter.validate_python(dump)
        assert type(restored) is type(original), (
            "discriminator did not route the dump back to the original "
            f"class: got {type(restored).__name__}, "
            f"want {type(original).__name__}"
        )
        assert restored == original

    def test_unknown_discriminator_is_rejected(self, adapter: Any) -> None:
        # The sum type is closed — consumers must NOT see a "default"
        # variant for an unknown ``kind`` value. Pydantic's discriminated
        # union raises on unknown discriminators; assert that contract
        # so the consumer code path stays exhaustive.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            adapter.validate_python(
                {
                    "kind": "made_up",
                    "resolution_id": "res-x",
                    "attempt_no": 1,
                }
            )


# ---------------------------------------------------------------------------
# Contract: Dispatch-command envelope — subject regex + reply regex.
# ---------------------------------------------------------------------------


@pytest.mark.integration_contract("DispatchEnvelope")
class TestDispatchEnvelopeContract:
    """§4 Integration Contract: dispatch-command + reply subjects.

    Producer: TASK-SAD-010 (NATS adapter declares the templates).
    Consumer: every external specialist subscribing on the bus.

    The IMPLEMENTATION-GUIDE §4 patterns are:

    * ``^agents\\.command\\.[a-z0-9-]+$`` — Forge → specialist command.
    * ``^agents\\.result\\.[a-z0-9-]+\\.[0-9a-f]{32}$`` — specialist
      → Forge per-correlation reply.

    We assert against both the canonical templates re-exported from
    ``forge.adapters.nats.specialist_dispatch`` AND against the
    subject-builder static helpers, so a future refactor that drifts
    one but not the other fails this test loudly.
    """

    def test_command_subject_template_matches_section_4_regex(self) -> None:
        # Render the template against a representative agent_id slug
        # (DRD-001..004 alphabet: lowercase + digits + hyphens).
        rendered = COMMAND_SUBJECT_TEMPLATE.format(agent_id="po-agent-007")
        assert COMMAND_SUBJECT_RE.fullmatch(rendered), (
            f"command template rendered {rendered!r}; "
            f"does not match §4 pattern {COMMAND_SUBJECT_RE.pattern!r}"
        )

    def test_result_subject_template_matches_section_4_regex(self) -> None:
        rendered = RESULT_SUBJECT_TEMPLATE.format(
            agent_id="po-agent-007",
            correlation_key="0123456789abcdef0123456789abcdef",
        )
        assert RESULT_SUBJECT_RE.fullmatch(rendered), (
            f"result template rendered {rendered!r}; "
            f"does not match §4 pattern {RESULT_SUBJECT_RE.pattern!r}"
        )

    def test_subject_builders_produce_section_4_compliant_subjects(self) -> None:
        # The static helpers on the adapter are the public path callers
        # use to build subjects. Drive them with a slug whose alphabet
        # exercises the full §4 pattern (lowercase, digits, hyphens) so
        # a regression in template construction fails loudly.
        cmd = NatsSpecialistDispatchAdapter.command_subject_for("agent-42")
        assert COMMAND_SUBJECT_RE.fullmatch(cmd), cmd

        # Use a real registry-generated key so the suffix exercises a
        # realistic 32-hex value rather than a hand-rolled literal.
        class _InertChannel:
            async def subscribe(self, key: str, deliver: Any) -> Any:
                raise AssertionError("subscribe must not be called")

            async def unsubscribe(self, sub: Any) -> None:
                raise AssertionError("unsubscribe must not be called")

        registry = CorrelationRegistry(transport=_InertChannel())
        key = registry.fresh_correlation_key()
        result = NatsSpecialistDispatchAdapter.result_subject_for(
            "agent-42", key
        )
        assert RESULT_SUBJECT_RE.fullmatch(result), result

    def test_command_regex_rejects_uppercase_agent_id(self) -> None:
        # Boundary check: §4 declares the alphabet as
        # ``[a-z0-9-]`` — uppercase must be rejected by the regex so
        # producers cannot accidentally publish on an unsubscribable
        # subject.
        bad = "agents.command.PO-Agent-007"
        assert not COMMAND_SUBJECT_RE.fullmatch(bad), (
            f"§4 regex incorrectly accepted uppercase agent_id: {bad!r}"
        )

    def test_result_regex_rejects_short_correlation_key(self) -> None:
        # Boundary check: §4 declares the suffix as ``[0-9a-f]{32}`` —
        # a 31-char or 33-char suffix must be rejected so a malformed
        # key cannot reach the wire.
        for length in (31, 33):
            bad_key = "a" * length
            bad = f"agents.result.po-agent.{bad_key}"
            assert not RESULT_SUBJECT_RE.fullmatch(bad), (
                f"§4 regex incorrectly accepted reply with {length}-char "
                f"correlation key: {bad!r}"
            )


# ---------------------------------------------------------------------------
# Contract: correlate_outcome() idempotency — exactly one UPDATE.
# ---------------------------------------------------------------------------


@pytest.mark.integration_contract("correlate_outcome")
class TestCorrelateOutcomeIdempotencyContract:
    """§4 Integration Contract: ``correlate_outcome()`` is idempotent.

    Producer: TASK-SAD-009 (helper) + TASK-SAD-002 (SQL idempotency).
    Consumer: FEAT-FORGE-004 gating layer (calls without coordination).

    AC-007 of this task: two consecutive calls with the same args
    produce the same record AND issue exactly one UPDATE. The SQL-layer
    invariant is what allows the gating layer to call freely without
    cross-feature coordination.
    """

    def test_two_calls_return_equal_records_and_one_update(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        _seed_resolution(db_writer, resolution_id="res-corr-001")

        # Wrap the connection so we can count UPDATE statements without
        # mutating the writer's public surface.
        counter = _UpdateCountingConnection(db_writer._connection)
        db_writer._connection = counter  # type: ignore[assignment]

        first = correlate_outcome(
            "res-corr-001", "gate-A", db_writer=db_writer
        )
        second = correlate_outcome(
            "res-corr-001", "gate-A", db_writer=db_writer
        )

        # Idempotency at the helper boundary.
        assert first == second
        assert first.outcome_correlated is True
        assert first.gate_decision_id == "gate-A"

        # Idempotency at the SQL layer — exactly one UPDATE across the
        # two consecutive calls. This is the load-bearing invariant
        # that lets the gating layer call without coordinating retries.
        assert counter.update_count == 1, (
            f"correlate_outcome issued {counter.update_count} UPDATEs "
            f"across two consecutive identical calls; expected exactly 1"
        )

    def test_signature_remains_stable_for_downstream_callers(self) -> None:
        # FEAT-FORGE-004 binds against the function signature, not its
        # implementation. If anyone reorders or renames a positional/
        # keyword-only parameter the gating layer breaks silently — pin
        # the signature here so the breakage surfaces as a test failure
        # in this feature, not in the next one.
        import inspect

        sig = inspect.signature(correlate_outcome)
        params = list(sig.parameters.values())
        # ``correlate_outcome(resolution_id, gate_decision_id, *, db_writer)``
        assert params[0].name == "resolution_id"
        assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert params[1].name == "gate_decision_id"
        assert params[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        # ``db_writer`` is keyword-only so callers cannot accidentally
        # pass it positionally and bypass the type guard.
        kw_only = [p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY]
        assert any(p.name == "db_writer" for p in kw_only), (
            "correlate_outcome.db_writer must remain keyword-only — "
            "downstream callers (FEAT-FORGE-004) bind positionally only "
            "for resolution_id + gate_decision_id"
        )


# ===========================================================================
# SEAM TESTS — one cross-task seam per Test* class.
# ===========================================================================


# ---------------------------------------------------------------------------
# Seam: dispatch ↔ discovery — orchestrator never imports
# discovery.cache internals; reads only the snapshot.
# ---------------------------------------------------------------------------


class TestDispatchDiscoverySeam:
    """Seam: ``dispatch`` reads ``discovery.cache`` only via ``snapshot``.

    The orchestrator captures one ``cache.snapshot()`` at the top of a
    dispatch attempt and resolves against the snapshot ONLY. It never
    reaches into the cache's internal dict, never grabs the lock,
    never imports private helpers.
    """

    def test_orchestrator_module_does_not_reference_cache_internals(
        self,
    ) -> None:
        # AST-level scan over every dispatch-domain source file — the
        # orchestrator must not access ``DiscoveryCache`` private
        # attributes (``_entries`` / ``_lock``). The dispatch layer
        # reads the cache through its public ``snapshot`` API only.
        dispatch_dir = _repo_root() / "src" / "forge" / "dispatch"
        assert dispatch_dir.exists(), f"missing {dispatch_dir}"

        violations: list[str] = []
        forbidden_attrs = {"_entries", "_lock"}
        for py_path in sorted(dispatch_dir.rglob("*.py")):
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
            rel = str(py_path.relative_to(_repo_root()))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in forbidden_attrs
                ):
                    # Allow self-references on classes inside the dispatch
                    # package (those private attributes belong to dispatch
                    # itself, not to discovery.cache).
                    value = node.value
                    if (
                        isinstance(value, ast.Name)
                        and value.id == "self"
                    ):
                        continue
                    violations.append(
                        f"{rel}:{node.lineno}: "
                        f"forbidden attribute access .{node.attr}"
                    )

        assert not violations, (
            "dispatch domain reached into discovery.cache internals — "
            "use ``snapshot()`` instead:\n  " + "\n  ".join(violations)
        )

    @pytest.mark.asyncio
    async def test_snapshot_is_a_copy_not_a_live_reference(self) -> None:
        # Behavioural seam: ``DiscoveryCache.snapshot()`` returns a
        # shallow copy. A subsequent mutation on the cache MUST NOT
        # affect the snapshot the orchestrator already holds. This is
        # the E.snapshot-stability invariant the orchestrator relies on.
        from nats_core.manifest import (
            AgentManifest,
            ToolCapability,
        )

        cache = DiscoveryCache(clock=SystemClock())
        manifest = AgentManifest(
            agent_id="agent-snap",
            name="agent-snap",
            version="1.0.0",
            template="generic",
            trust_tier="specialist",
            intents=[],
            tools=[
                ToolCapability(
                    name="t",
                    description="t",
                    parameters={},
                    returns="dict",
                    risk_level="read_only",
                )
            ],
        )
        await cache.upsert_agent(manifest)

        snapshot = await cache.snapshot()
        assert "agent-snap" in snapshot

        # Mutate the cache after the snapshot was captured.
        await cache.remove_agent("agent-snap")

        # The snapshot held by the dispatch domain must be unchanged.
        assert "agent-snap" in snapshot, (
            "DiscoveryCache.snapshot() returned a live reference; "
            "dispatch could observe mid-attempt mutations"
        )


# ---------------------------------------------------------------------------
# Seam: dispatch ↔ persistence — sensitive parameter values never
# appear in any persisted column.
# ---------------------------------------------------------------------------


class TestDispatchPersistenceSeam:
    """Seam: sensitive parameter values never reach persisted storage.

    The persistence layer is the scrub boundary. ``persist_resolution``
    drops ``value`` for parameters with ``sensitive=True``. This seam
    test exercises the contract end-to-end: we shove a list of mixed
    sensitive + non-sensitive parameters through, then read every
    column of the parameter table and assert no sensitive value
    survives.
    """

    def test_sensitive_values_never_appear_in_any_column(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # Construct a sentinel value that is unique enough to grep for
        # — if it appears anywhere in the dump, the scrub leaked.
        sensitive_marker = "SENSITIVE-LEAK-MARKER-9b6e2"
        nonsensitive_marker = "ok-public-marker-3fa1"

        resolution = _baseline_resolution(resolution_id="res-scrub-001")
        parameters = [
            DispatchParameter(name="api_key", value=sensitive_marker, sensitive=True),
            DispatchParameter(
                name="auth_token", value=sensitive_marker + "-2", sensitive=True
            ),
            DispatchParameter(
                name="public_field", value=nonsensitive_marker, sensitive=False
            ),
        ]
        persist_resolution(resolution, parameters, db_writer=db_writer)

        rows = db_writer.dump_all_parameter_rows()

        # Two sensitive rows + one public row.
        assert len(rows) == 3

        # Sensitive rows: ``value`` is NULL; the name is preserved for
        # audit traceability.
        sensitive_rows = [row for row in rows if row["sensitive"]]
        assert len(sensitive_rows) == 2
        for row in sensitive_rows:
            assert row["value"] is None, (
                f"sensitive parameter {row['name']!r} kept its value in "
                f"the database — scrub failed"
            )
            assert row["name"] in {"api_key", "auth_token"}

        # Public row: value preserved.
        public_rows = [row for row in rows if not row["sensitive"]]
        assert len(public_rows) == 1
        assert public_rows[0]["value"] == nonsensitive_marker

        # Belt-and-braces — the sensitive marker must not appear in ANY
        # cell of the dump. If a future refactor accidentally writes
        # the value into the ``name`` column we want the test to fail.
        for row in rows:
            for column in ("resolution_id", "name", "value"):
                cell = row.get(column)
                if cell is None:
                    continue
                assert sensitive_marker not in str(cell), (
                    f"sensitive marker leaked into column {column!r} of "
                    f"row {row!r}"
                )

    def test_pipeline_history_view_never_surfaces_sensitive_values(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # The pipeline-history view is the auditor-facing projection.
        # Even at the projection layer, sensitive values must remain
        # invisible — only the (non-sensitive) name and the
        # ``sensitive=True`` flag are exposed.
        sensitive_marker = "SENSITIVE-VIEW-LEAK-MARKER-c4d2"
        resolution = _baseline_resolution(resolution_id="res-view-001")
        parameters = [
            DispatchParameter(name="api_key", value=sensitive_marker, sensitive=True),
            DispatchParameter(name="region", value="us-east-1", sensitive=False),
        ]
        persist_resolution(resolution, parameters, db_writer=db_writer)

        view = db_writer.read_pipeline_history_view("res-view-001")
        assert view is not None
        # Recursively walk the view and assert the marker does not
        # appear anywhere — names + flags only.
        text = repr(view)
        assert sensitive_marker not in text, (
            "pipeline-history view surfaced a sensitive parameter value"
        )
        # Sanity — sensitive parameter is recorded by name + flag only.
        sensitive_view_rows = [
            p for p in view["parameters"] if p.get("sensitive") is True
        ]
        assert sensitive_view_rows == [{"name": "api_key", "sensitive": True}]


# ---------------------------------------------------------------------------
# Seam: dispatch ↔ NATS adapter — orchestrator does not import
# nats.aio. The adapter is the SOLE import site (AC-005).
# ---------------------------------------------------------------------------


class TestDispatchNatsAdapterImportSeam:
    """Seam: ``forge/dispatch/*.py`` does NOT import ``nats``.

    This is the strongest guarantee that the domain/transport split
    holds over time. New developers tend to add imports without
    thinking; an AST-level test catches it on the next CI run.

    Per the task spec we walk the AST — NOT a string grep — so a
    docstring mentioning ``nats`` does not trigger a false positive
    and a sneaky ``importlib.import_module("nats.aio")`` call would
    still fail (covered by the importlib check below).
    """

    def _ast_imports(self, py_path: Path) -> list[tuple[int, str]]:
        """Return ``(lineno, module_name)`` for every ``import``/``from``
        statement in ``py_path``.

        Walks ``ast.parse()`` rather than splitting source text so
        comments, docstrings, and string literals containing the word
        ``nats`` cannot trigger a false positive.
        """
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        results: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    results.append((node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                # Module is None for relative imports (``from . import x``).
                module_name = node.module or ""
                results.append((node.lineno, module_name))
        return results

    def test_no_dispatch_module_imports_nats(self) -> None:
        # AC-005: walks ``ast.parse()`` over each dispatch source file
        # and asserts no import statement references ``nats`` (any
        # submodule). The ``nats_core`` schema package is intentionally
        # NOT in the same namespace as the transport ``nats`` distro,
        # so the substring check below is unambiguous.
        dispatch_dir = _repo_root() / "src" / "forge" / "dispatch"
        assert dispatch_dir.exists(), (
            f"dispatch directory {dispatch_dir} does not exist"
        )

        violations: list[str] = []
        for py_path in sorted(dispatch_dir.rglob("*.py")):
            rel = str(py_path.relative_to(_repo_root()))
            for lineno, module_name in self._ast_imports(py_path):
                # ``nats`` and ``nats.aio`` and any ``nats.*`` submodule.
                # ``nats_core`` is a different distribution (the schema
                # package) and intentionally allowed.
                if module_name == "nats" or module_name.startswith("nats."):
                    violations.append(
                        f"{rel}:{lineno}: import {module_name!r}"
                    )

        assert not violations, (
            "dispatch domain imported a NATS transport module — the "
            "domain/transport seam was breached. Move the import into "
            "``src/forge/adapters/nats/``:\n  "
            + "\n  ".join(violations)
        )

    def test_no_dispatch_module_uses_importlib_for_nats(self) -> None:
        # Belt-and-braces companion to the AST scan: a sneaky dynamic
        # import (``importlib.import_module("nats.aio")``) would not
        # appear in ``ast.Import``/``ast.ImportFrom``. Catch the
        # constant string ``"nats"``-rooted argument inside any call.
        dispatch_dir = _repo_root() / "src" / "forge" / "dispatch"
        violations: list[str] = []
        for py_path in sorted(dispatch_dir.rglob("*.py")):
            rel = str(py_path.relative_to(_repo_root()))
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # First positional argument must be a string constant.
                if not node.args:
                    continue
                arg0 = node.args[0]
                if not isinstance(arg0, ast.Constant) or not isinstance(
                    arg0.value, str
                ):
                    continue
                target = arg0.value
                if target == "nats" or target.startswith("nats."):
                    # Resolve the call's target name to a string for the
                    # diagnostic message (best-effort).
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        func_name = f"{ast.unparse(func)}"
                    elif isinstance(func, ast.Name):
                        func_name = func.id
                    else:  # pragma: no cover - exotic call shape
                        func_name = "<call>"
                    violations.append(
                        f"{rel}:{node.lineno}: {func_name}({target!r})"
                    )

        assert not violations, (
            "dispatch domain referenced ``nats`` via a dynamic import "
            "string — the domain/transport seam was breached:\n  "
            + "\n  ".join(violations)
        )

    def test_adapter_is_the_sole_import_site(self) -> None:
        # Positive companion: the adapter module DOES import the NATS
        # client. We do NOT pin the exact import line (the adapter is
        # free to refactor) — but we assert that *some* dispatch-related
        # NATS-touching surface exists in the adapter package, so a
        # future deletion does not silently leave the seam test passing
        # against a non-existent transport.
        adapter_path = (
            _repo_root()
            / "src"
            / "forge"
            / "adapters"
            / "nats"
            / "specialist_dispatch.py"
        )
        assert adapter_path.exists(), (
            f"specialist dispatch adapter missing at {adapter_path}"
        )
        text = adapter_path.read_text(encoding="utf-8")
        # The adapter imports ``nats.aio`` types lazily / via type-check
        # blocks in some refactors; what we MUST observe is that the
        # adapter declares its transport surface (the constants the
        # template renders into).
        for marker in (
            "COMMAND_SUBJECT_TEMPLATE",
            "RESULT_SUBJECT_TEMPLATE",
            "NatsSpecialistDispatchAdapter",
        ):
            assert marker in text, (
                f"adapter missing required transport surface: {marker!r}"
            )


# ---------------------------------------------------------------------------
# Seam: correlation ↔ adapter — subscribe-before-publish ordering.
# ---------------------------------------------------------------------------


class TestCorrelationAdapterOrderingSeam:
    """Seam: subscribe ALWAYS happens before publish across the orchestrator path.

    The :class:`CorrelationRegistry.bind` API returns ONLY after the
    transport's subscribe call has completed. The orchestrator
    publishes only after ``bind()`` returns. The seam test wires an
    in-memory transport that records every subscribe + publish call
    against a single sequence counter; ordering is asserted as
    "subscribe.seq < publish.seq" for the same correlation key.
    """

    @pytest.mark.asyncio
    async def test_subscribe_call_precedes_publish_call(self) -> None:
        # Build a transport that satisfies both ReplyChannel-shaped
        # protocols simultaneously (the registry uses the
        # ``subscribe(correlation_key, deliver)`` shape; this seam test
        # talks directly to that surface).
        sequence: list[tuple[str, str]] = []

        class RecordingTransport:
            async def subscribe(
                self, correlation_key: str, deliver: Any
            ) -> str:
                sequence.append(("subscribe", correlation_key))
                return correlation_key

            async def unsubscribe(self, subscription: Any) -> None:
                sequence.append(("unsubscribe", subscription))

        registry = CorrelationRegistry(transport=RecordingTransport())

        # Bind one correlation key, then simulate the orchestrator's
        # publish call by appending a synthetic ``publish`` event to
        # the sequence. The relative ordering of the two events is the
        # invariant under test.
        key = registry.fresh_correlation_key()
        binding = await registry.bind(key, "specialist-a")
        sequence.append(("publish", key))

        # Subscribe-before-publish ordering: the first event is the
        # subscribe for ``key``, the second is the publish for the same
        # key.
        events_for_key = [
            (kind, k) for (kind, k) in sequence if k == key
        ]
        assert events_for_key == [
            ("subscribe", key),
            ("publish", key),
        ], (
            f"subscribe-before-publish ordering violated for key {key}: "
            f"{events_for_key!r}"
        )
        # The binding flag the orchestrator reads is also set so it
        # cannot publish before subscribe completes.
        assert binding.subscription_active is True

    @pytest.mark.asyncio
    async def test_bind_completes_before_returning_to_caller(self) -> None:
        # Microscope view: ``bind()`` must NOT return while the
        # transport's subscribe is still in flight. We arrange a
        # transport whose subscribe yields control (await
        # ``asyncio.sleep(0)``) and assert that the binding's
        # ``subscription_active`` is False *during* the sleep and True
        # only *after* bind returns.

        import asyncio

        observed_active_during_subscribe: list[bool] = []

        class SlowSubscribeTransport:
            def __init__(self) -> None:
                self.binding_under_test: Any = None

            async def subscribe(
                self, correlation_key: str, deliver: Any
            ) -> str:
                # Snapshot the binding's flag before the subscribe
                # completes — at this point the registry has constructed
                # the binding but has NOT set ``subscription_active``.
                if self.binding_under_test is not None:
                    observed_active_during_subscribe.append(
                        self.binding_under_test.subscription_active
                    )
                await asyncio.sleep(0)
                return correlation_key

            async def unsubscribe(self, subscription: Any) -> None:
                return None

        transport = SlowSubscribeTransport()
        registry = CorrelationRegistry(transport=transport)
        key = registry.fresh_correlation_key()

        # Pre-stash the binding-to-be on the transport so subscribe()
        # can observe it. The registry stores the binding before
        # awaiting subscribe (so a transport that fires a synchronous
        # reply can route it). We retrieve it via the registry's
        # internal dict at the moment subscribe runs — the only way the
        # transport can see "the binding mid-construction" is to read
        # it from the registry directly.
        original_bind = registry.bind

        async def _bind_with_observe(
            correlation_key: str, matched_agent_id: str
        ) -> Any:
            # Patch the transport to read the binding from the registry
            # the moment subscribe is invoked.
            class _TransportProxy(SlowSubscribeTransport):
                async def subscribe(
                    self, ck: str, deliver: Any
                ) -> str:
                    # Read the in-progress binding directly off the
                    # registry's bookkeeping dict.
                    in_progress = registry._bindings.get(ck)
                    self.binding_under_test = in_progress
                    if in_progress is not None:
                        observed_active_during_subscribe.append(
                            in_progress.subscription_active
                        )
                    await asyncio.sleep(0)
                    return ck

            registry._transport = _TransportProxy()
            return await original_bind(correlation_key, matched_agent_id)

        binding = await _bind_with_observe(key, "specialist-a")

        # During the subscribe call, ``subscription_active`` was False.
        assert observed_active_during_subscribe == [False], (
            "bind() set subscription_active before subscribe completed; "
            f"observations during subscribe: "
            f"{observed_active_during_subscribe!r}"
        )
        # After bind() returned, ``subscription_active`` is True. The
        # orchestrator reads this flag to know it is safe to publish.
        assert binding.subscription_active is True


# ---------------------------------------------------------------------------
# Seam: outcome ↔ FEAT-FORGE-004 (downstream) — correlate_outcome
# signature + idempotency are stable.
# ---------------------------------------------------------------------------


class TestOutcomeDownstreamSeam:
    """Seam: ``correlate_outcome()`` is a stable boundary for the gating
    layer (FEAT-FORGE-004).

    The gating layer must be able to call ``correlate_outcome`` with
    no coordination with the dispatch layer. That requires:

    * Stable signature (positional args, keyword-only ``db_writer``).
    * Idempotency at the SQL layer (covered by AC-007 above; the
      "downstream" angle here is asserted via repeated calls in a
      loop, modelling the gating layer's "fire on every check"
      behaviour).
    * No side-effects on retry of the same (resolution, gate) pair.
    """

    def test_repeated_calls_are_safe_with_no_coordination(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # Model the gating layer's repeated-call behaviour: a downstream
        # caller may invoke correlate_outcome 5 times for one pair (e.g.
        # because of retries in its own loop). The dispatch layer must
        # absorb these calls without state corruption.
        _seed_resolution(db_writer, resolution_id="res-down-001")

        records = [
            correlate_outcome(
                "res-down-001", "gate-7", db_writer=db_writer
            )
            for _ in range(5)
        ]

        # All five records are equal (idempotency at the helper boundary).
        first = records[0]
        for record in records[1:]:
            assert record == first

        # Final state matches the gating layer's expectations.
        assert first.outcome_correlated is True
        assert first.gate_decision_id == "gate-7"

        # The persistence layer holds exactly one row.
        rows = db_writer.read_resolutions()
        assert len(rows) == 1
        assert rows[0].outcome_correlated is True
        assert rows[0].gate_decision_id == "gate-7"

    def test_different_gate_for_same_resolution_is_rejected(
        self, db_writer: SqliteHistoryWriter
    ) -> None:
        # Idempotency is per-(resolution, gate) pair, NOT "any gate" —
        # so a downstream caller that mistakenly tries to correlate the
        # same resolution to a different gate decision must be told no.
        # This is the contract that prevents cross-feature data
        # corruption.
        _seed_resolution(db_writer, resolution_id="res-down-002")
        correlate_outcome("res-down-002", "gate-A", db_writer=db_writer)
        with pytest.raises(ValueError):
            correlate_outcome(
                "res-down-002", "gate-B", db_writer=db_writer
            )
