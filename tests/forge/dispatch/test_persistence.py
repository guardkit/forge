"""Tests for ``forge.dispatch.persistence`` (TASK-SAD-002).

Acceptance criteria coverage map:

* AC-001: :class:`DispatchParameter` defines a ``sensitive: bool`` field
  (default ``False``) — see :class:`TestDispatchParameterSchema`.
* AC-002: :func:`persist_resolution` writes the resolution to a
  SQLite-backed history table without requiring schema changes to
  FEAT-FORGE-001 — see :class:`TestPersistResolutionWritesToSqlite`
  (sibling-table strategy described in the production module
  docstring).
* AC-003: Sensitive parameters are scrubbed at the persistence
  boundary — see :class:`TestSensitiveParameterScrub`.
* AC-004: Non-sensitive parameters are persisted in full — see
  :class:`TestNonSensitiveParameterRoundTrip`.
* AC-005: Atomic write — see :class:`TestAtomicWriteSemantics`.
* AC-006: ``DispatchParameter(name="api_token", value="secret",
  sensitive=True)`` results in a row whose ``value`` column is
  ``NULL`` — see
  :meth:`TestSensitiveParameterScrub.test_api_token_value_column_is_null`.
* AC-007: Pipeline-history view shows non-sensitive fields and the
  *names* of sensitive fields, never values — see
  :class:`TestPipelineHistoryView`.

Plus a "seam"-style round-trip pair mirroring the contract tests
listed in the task spec (``Seam Tests`` section): one for
:class:`CapabilityResolution.retry_of` round-trip, and one for the
sensitive-parameter contract — see
:class:`TestSeamCapabilityResolutionContract`. The task spec's
literal seam-test snippets contained a typo (``match_source="exact_tool"``
is not a valid :data:`MatchSource` literal) so the tests below use the
canonical ``"tool_exact"`` value while preserving the intent of the
seam contract.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from forge.discovery.models import CapabilityResolution
from forge.dispatch.persistence import (
    DispatchParameter,
    SqliteHistoryWriter,
    persist_resolution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_writer() -> SqliteHistoryWriter:
    """Fresh in-memory :class:`SqliteHistoryWriter` per test.

    Mirrors the seam-test fixture name in the task spec
    (``Seam Tests`` section). The in-memory database keeps the suite
    fast and self-contained — each test starts with empty tables.
    """
    writer = SqliteHistoryWriter.in_memory()
    yield writer
    writer.close()


def _resolution(
    *,
    resolution_id: str = "res-001",
    build_id: str = "build-001",
    stage_label: str = "implementation",
    requested_tool: str = "do_thing",
    matched_agent_id: str | None = "agent-a",
    match_source: str = "tool_exact",
    competing_agents: list[str] | None = None,
    retry_of: str | None = None,
) -> CapabilityResolution:
    """Construct a valid :class:`CapabilityResolution` for tests.

    The discovery model has multiple required fields and an internal
    invariant validator (``matched_agent_id is None`` ⇔
    ``match_source == "unresolved"``). This helper centralises a
    valid baseline so individual tests only need to override what
    they care about.
    """
    return CapabilityResolution(
        resolution_id=resolution_id,
        build_id=build_id,
        stage_label=stage_label,
        requested_tool=requested_tool,
        matched_agent_id=matched_agent_id,
        match_source=match_source,  # type: ignore[arg-type]
        competing_agents=competing_agents or [],
        resolved_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
        retry_of=retry_of,
    )


# ---------------------------------------------------------------------------
# AC-001 — DispatchParameter schema
# ---------------------------------------------------------------------------


class TestDispatchParameterSchema:
    """AC-001: ``DispatchParameter`` defines ``sensitive: bool``."""

    def test_sensitive_field_defaults_to_false(self) -> None:
        param = DispatchParameter(name="ticket_id", value="JIRA-123")
        assert param.sensitive is False

    def test_sensitive_field_is_bool(self) -> None:
        param = DispatchParameter(
            name="api_token", value="hidden", sensitive=True
        )
        assert isinstance(param.sensitive, bool)
        assert param.sensitive is True

    def test_name_required_and_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            DispatchParameter(name="", value="x")

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DispatchParameter(  # type: ignore[call-arg]
                name="api_token", value="hidden", sensitive=True, bogus="nope",
            )


# ---------------------------------------------------------------------------
# AC-002 — persist_resolution writes the resolution to SQLite
# ---------------------------------------------------------------------------


class TestPersistResolutionWritesToSqlite:
    """AC-002: resolution + retry_of round-trips through SQLite."""

    def test_writes_resolution_row(self, db_writer: SqliteHistoryWriter) -> None:
        resolution = _resolution()
        persist_resolution(resolution, parameters=[], db_writer=db_writer)

        rows = db_writer.read_resolutions()
        assert len(rows) == 1
        assert rows[0].resolution_id == "res-001"
        assert rows[0].matched_agent_id == "agent-a"
        assert rows[0].match_source == "tool_exact"

    def test_retry_of_round_trips_when_none(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # Mirrors task seam test
        # ``test_capability_resolution_persistence_round_trip``.
        resolution = _resolution(resolution_id="res-init", retry_of=None)
        persist_resolution(resolution, parameters=[], db_writer=db_writer)

        rows = db_writer.read_resolutions()
        assert rows[0].retry_of is None

    def test_retry_of_round_trips_when_populated(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # AC-002 + TASK-SAD-001 contract: retry_of survives a round
        # trip with its prior resolution_id intact.
        resolution = _resolution(
            resolution_id="res-retry",
            retry_of="res-original",
        )
        persist_resolution(resolution, parameters=[], db_writer=db_writer)

        rows = db_writer.read_resolutions()
        assert rows[0].retry_of == "res-original"


# ---------------------------------------------------------------------------
# AC-003 + AC-006 — sensitive-parameter scrub at persistence boundary
# ---------------------------------------------------------------------------


class TestSensitiveParameterScrub:
    """AC-003 + AC-006: sensitive ``value`` is dropped at the boundary."""

    def test_api_token_value_column_is_null(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # AC-006 — the literal example from the task acceptance
        # criteria. ``value`` column MUST be NULL after persistence.
        secret = DispatchParameter(
            name="api_token", value="VERY-SECRET", sensitive=True,
        )
        persist_resolution(
            _resolution(), parameters=[secret], db_writer=db_writer,
        )

        rows = db_writer.dump_all_parameter_rows()
        assert len(rows) == 1
        assert rows[0]["name"] == "api_token"
        assert rows[0]["value"] is None
        assert rows[0]["sensitive"] is True

    def test_sensitive_value_is_not_present_in_any_column(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # Belt-and-braces — search every text column in the database
        # for the literal secret. If the scrub leaks via, e.g., a
        # stringified competing_agents column, this test fails.
        secret = DispatchParameter(
            name="api_token", value="VERY-SECRET", sensitive=True,
        )
        public = DispatchParameter(
            name="ticket_id", value="JIRA-123", sensitive=False,
        )
        persist_resolution(
            _resolution(), parameters=[secret, public], db_writer=db_writer,
        )

        # Read everything back as raw rows.
        raw_rows = db_writer.dump_all_parameter_rows()
        assert "VERY-SECRET" not in str(raw_rows)
        assert "JIRA-123" in str(raw_rows)
        # Sensitive parameter *name* must still be recorded for audit.
        assert any(row["name"] == "api_token" for row in raw_rows)

        # Defence in depth — sweep the whole DB. The connection
        # exposes ``iterdump`` which serialises every row as SQL.
        full_dump = "\n".join(db_writer.connection.iterdump())
        assert "VERY-SECRET" not in full_dump

    def test_sensitive_flag_persists_alongside_scrubbed_value(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        secret = DispatchParameter(name="api_token", value="x", sensitive=True)
        persist_resolution(
            _resolution(), parameters=[secret], db_writer=db_writer,
        )
        rows = db_writer.dump_all_parameter_rows()
        assert rows[0]["sensitive"] is True
        assert rows[0]["value"] is None


# ---------------------------------------------------------------------------
# AC-004 — non-sensitive parameters round-trip in full
# ---------------------------------------------------------------------------


class TestNonSensitiveParameterRoundTrip:
    """AC-004: non-sensitive parameters keep their ``value``."""

    def test_non_sensitive_value_persists(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        public = DispatchParameter(
            name="ticket_id", value="JIRA-123", sensitive=False,
        )
        persist_resolution(
            _resolution(), parameters=[public], db_writer=db_writer,
        )
        rows = db_writer.dump_all_parameter_rows()
        assert rows[0]["name"] == "ticket_id"
        assert rows[0]["value"] == "JIRA-123"
        assert rows[0]["sensitive"] is False

    def test_default_parameter_is_non_sensitive(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # Defaulting to non-sensitive matches the schema; the *caller*
        # opts a parameter in to scrubbing by setting ``sensitive=True``.
        param = DispatchParameter(name="ticket_id", value="JIRA-123")
        persist_resolution(
            _resolution(), parameters=[param], db_writer=db_writer,
        )
        rows = db_writer.dump_all_parameter_rows()
        assert rows[0]["value"] == "JIRA-123"
        assert rows[0]["sensitive"] is False


# ---------------------------------------------------------------------------
# AC-005 — atomic write semantics
# ---------------------------------------------------------------------------


class TestAtomicWriteSemantics:
    """AC-005: resolution + parameters land atomically, or not at all."""

    def test_duplicate_resolution_rolls_back_parameters(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # First write succeeds.
        first = _resolution(resolution_id="res-dup")
        persist_resolution(first, parameters=[], db_writer=db_writer)
        assert len(db_writer.read_resolutions()) == 1

        # Second write of the SAME resolution_id violates the PRIMARY
        # KEY, raising IntegrityError — and importantly, the parameter
        # rows attempted alongside it must NOT survive.
        param = DispatchParameter(name="ticket_id", value="JIRA-123")
        with pytest.raises(sqlite3.IntegrityError):
            persist_resolution(
                _resolution(resolution_id="res-dup"),
                parameters=[param],
                db_writer=db_writer,
            )

        # No partial parameter rows should have leaked. The integrity
        # error fires on the resolution insert (first statement in
        # the transaction), so the parameter never executed; either
        # way the table must be empty.
        assert db_writer.dump_all_parameter_rows() == []

    def test_parameter_failure_rolls_back_resolution(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        # Force a failure during the parameter inserts by monkey-patching
        # the writer to raise on the second insert. The transaction
        # MUST roll back so the resolution row is also absent.
        original_insert = db_writer.insert_parameter
        call_state = {"count": 0}

        def explosive_insert(*args, **kwargs):
            call_state["count"] += 1
            if call_state["count"] == 2:
                raise sqlite3.DatabaseError("simulated mid-batch failure")
            return original_insert(*args, **kwargs)

        db_writer.insert_parameter = explosive_insert  # type: ignore[assignment]

        params = [
            DispatchParameter(name="ok_one", value="1"),
            DispatchParameter(name="ok_two", value="2"),  # triggers failure
        ]
        with pytest.raises(sqlite3.DatabaseError):
            persist_resolution(
                _resolution(resolution_id="res-atomic"),
                parameters=params,
                db_writer=db_writer,
            )

        # Atomic invariant: neither the resolution row nor any
        # parameter rows are visible.
        assert db_writer.read_resolutions() == []
        assert db_writer.dump_all_parameter_rows() == []


# ---------------------------------------------------------------------------
# AC-007 — pipeline-history view never reveals sensitive values
# ---------------------------------------------------------------------------


class TestPipelineHistoryView:
    """AC-007: the audit projection shows names but never values."""

    def test_view_includes_non_sensitive_value_and_sensitive_name_only(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        secret = DispatchParameter(
            name="api_token", value="VERY-SECRET", sensitive=True,
        )
        public = DispatchParameter(
            name="ticket_id", value="JIRA-123", sensitive=False,
        )
        persist_resolution(
            _resolution(resolution_id="res-view"),
            parameters=[secret, public],
            db_writer=db_writer,
        )

        view = db_writer.read_pipeline_history_view("res-view")
        assert view is not None

        params = view["parameters"]
        assert isinstance(params, list)

        # Sensitive entry: name preserved, NO ``value`` key, sensitive
        # flag visible to the auditor.
        sensitive_entries = [p for p in params if p.get("name") == "api_token"]
        assert len(sensitive_entries) == 1
        sensitive_entry = sensitive_entries[0]
        assert sensitive_entry == {"name": "api_token", "sensitive": True}
        assert "value" not in sensitive_entry

        # Non-sensitive entry: name + value visible.
        public_entries = [p for p in params if p.get("name") == "ticket_id"]
        assert len(public_entries) == 1
        assert public_entries[0]["value"] == "JIRA-123"
        assert public_entries[0]["sensitive"] is False

        # The whole view must not contain the secret value anywhere.
        assert "VERY-SECRET" not in str(view)

    def test_view_is_none_for_unknown_resolution(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        assert db_writer.read_pipeline_history_view("does-not-exist") is None


# ---------------------------------------------------------------------------
# Seam-style contract tests (mirror task ``Seam Tests`` section)
# ---------------------------------------------------------------------------


class TestSeamCapabilityResolutionContract:
    """Seam tests verifying the integration contract from TASK-SAD-001.

    The task spec includes two illustrative seam tests. The
    ``match_source="exact_tool"`` literal in the spec is a typo — the
    canonical literal is ``"tool_exact"``. These tests preserve the
    intent of the seam contract while constructing valid Pydantic
    models.
    """

    def test_capability_resolution_persistence_round_trip(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        res = _resolution(
            resolution_id="res-001",
            match_source="tool_exact",
            matched_agent_id="po-agent",
            competing_agents=[],
            retry_of=None,
        )
        persist_resolution(res, parameters=[], db_writer=db_writer)
        rows = db_writer.read_resolutions()
        assert len(rows) == 1
        assert rows[0].retry_of is None

    def test_sensitive_parameter_value_not_persisted(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        res = _resolution(
            resolution_id="res-002",
            match_source="tool_exact",
            matched_agent_id="po-agent",
            competing_agents=[],
        )
        secret = DispatchParameter(
            name="api_token", value="VERY-SECRET", sensitive=True,
        )
        public = DispatchParameter(
            name="ticket_id", value="JIRA-123", sensitive=False,
        )
        persist_resolution(
            res, parameters=[secret, public], db_writer=db_writer,
        )

        raw_rows = db_writer.dump_all_parameter_rows()
        assert "VERY-SECRET" not in str(raw_rows)
        assert "JIRA-123" in str(raw_rows)
        # Sensitive *name* still recorded for audit.
        assert any("api_token" in str(r) for r in raw_rows)


# ---------------------------------------------------------------------------
# Type-safety guard rails
# ---------------------------------------------------------------------------


class TestPersistResolutionInputValidation:
    """``persist_resolution`` rejects mistyped arguments at the boundary."""

    def test_rejects_non_resolution(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        with pytest.raises(TypeError):
            persist_resolution(
                "not-a-resolution",  # type: ignore[arg-type]
                parameters=[],
                db_writer=db_writer,
            )

    def test_rejects_non_writer(self) -> None:
        with pytest.raises(TypeError):
            persist_resolution(
                _resolution(),
                parameters=[],
                db_writer="not-a-writer",  # type: ignore[arg-type]
            )

    def test_rejects_non_dispatch_parameter_in_list(
        self, db_writer: SqliteHistoryWriter,
    ) -> None:
        with pytest.raises(TypeError):
            persist_resolution(
                _resolution(),
                parameters=[{"name": "ticket_id", "value": "JIRA-123"}],  # type: ignore[list-item]
                db_writer=db_writer,
            )


# ---------------------------------------------------------------------------
# SqliteHistoryWriter constructors
# ---------------------------------------------------------------------------


class TestSqliteHistoryWriterConstructors:
    """:class:`SqliteHistoryWriter` opens against in-memory + on-disk DBs."""

    def test_in_memory_constructor_creates_fresh_schema(self) -> None:
        writer = SqliteHistoryWriter.in_memory()
        try:
            assert writer.read_resolutions() == []
            assert writer.dump_all_parameter_rows() == []
        finally:
            writer.close()

    def test_from_path_constructor_persists_across_writers(self, tmp_path) -> None:
        db_path = tmp_path / "history.sqlite"
        writer = SqliteHistoryWriter.from_path(db_path)
        try:
            persist_resolution(
                _resolution(resolution_id="res-disk"),
                parameters=[DispatchParameter(name="ticket_id", value="JIRA-123")],
                db_writer=writer,
            )
        finally:
            writer.close()

        # Re-open and verify the data survived.
        reader = SqliteHistoryWriter.from_path(db_path)
        try:
            rows = reader.read_resolutions()
            assert len(rows) == 1
            assert rows[0].resolution_id == "res-disk"
            params = reader.dump_all_parameter_rows()
            assert params[0]["value"] == "JIRA-123"
        finally:
            reader.close()
