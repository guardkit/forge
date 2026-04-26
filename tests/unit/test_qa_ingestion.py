"""Unit tests for ``forge.memory.qa_ingestion`` (TASK-IC-005).

Each test class maps to one or more acceptance criteria from
``tasks/design_approved/TASK-IC-005-qa-history-ingestion.md``:

* :class:`TestContentHashComparison`         — AC-001, AC-002
                                               (``@data-integrity
                                               re-scan-zero-writes``).
* :class:`TestEmitsCalibrationEventsOnChange` — AC-003.
* :class:`TestDeterministicEntityId`         — AC-004
                                               (``@data-integrity
                                               deterministic-qa-identity``).
* :class:`TestPartialParseTolerance`         — AC-005
                                               (``@negative
                                               partial-parse-tolerance``).
* :class:`TestAtomicSnapshotUpdate`          — AC-006.
* :class:`TestPostBuildIngestionRefresh`     — AC-007
                                               (``@edge-case
                                               post-build-ingestion-refresh``).
* :class:`TestParseQAPairs`                  — parser tolerance for
                                               whitespace, BOM, mixed
                                               line endings.
* :class:`TestSnapshotStoreLoad`             — defensive load behaviour
                                               for missing or corrupt
                                               snapshot files.

This single test module covers both ``test_qa_ingestion.py`` and the
secondary ``test_deterministic_qa_id.py`` from the task brief; the
"minimal" documentation level caps file creation at two artefacts
(source + test), so the deterministic-identity scenarios are
consolidated here under :class:`TestDeterministicEntityId`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.memory.qa_ingestion import (
    CALIBRATION_GROUP_ID,
    HashSnapshotStore,
    IngestionReport,
    ingest_qa_history,
    parse_qa_pairs,
)


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


SAMPLE_QA = """\
Q: How should the gating threshold be tuned?
A: Increase to 0.85 for high-stakes builds.

Q: When should we override the recommendation?
A: Only when the rationale is grounded in evidence.
"""

PARTIAL_QA = """\
Q: Valid question one
A: Valid answer one

Q: Orphan question without answer

Q: Valid question two
A: Valid answer two
"""


def _run(coro):
    """Drive an async coroutine to completion in a synchronous test."""
    return asyncio.run(coro)


def _write_qa_file(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def patch_writer():
    """Patch ``fire_and_forget_write`` so emissions are captured.

    The real writer would attempt to dispatch to Graphiti on a fresh
    asyncio loop; for unit tests we just want to count and inspect the
    arguments passed to the writer.
    """
    with patch(
        "forge.memory.qa_ingestion.fire_and_forget_write"
    ) as mock_write:
        yield mock_write


# ---------------------------------------------------------------------------
# AC-001 / AC-002: content-hash comparison and re-scan-zero-writes
# ---------------------------------------------------------------------------


class TestContentHashComparison:
    """AC-001 / AC-002 (``@data-integrity re-scan-zero-writes``)."""

    def test_first_scan_emits_events_and_records_hash(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        report = _run(ingest_qa_history([qa_file], store))

        assert report.scanned == 1
        assert report.changed == 1
        assert report.events_emitted == 2
        assert patch_writer.call_count == 2

        # Snapshot now has a hash for the file (re-load to bypass the
        # in-memory cache and prove it was persisted).
        reloaded = HashSnapshotStore(tmp_path / "snap.json")
        assert reloaded.get_hash(str(qa_file)) is not None

    def test_unchanged_file_emits_zero_writes(self, tmp_path, patch_writer):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")
        _run(ingest_qa_history([qa_file], store))
        patch_writer.reset_mock()

        # Re-scan with a freshly-loaded store (mimics a new process).
        store_again = HashSnapshotStore(tmp_path / "snap.json")
        report = _run(ingest_qa_history([qa_file], store_again))

        assert report.scanned == 1
        assert report.changed == 0
        assert report.events_emitted == 0
        assert patch_writer.call_count == 0

    def test_hash_change_triggers_full_reparse(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")
        _run(ingest_qa_history([qa_file], store))
        patch_writer.reset_mock()

        # Add another Q&A pair to the file.
        new_content = SAMPLE_QA + "\nQ: A new question\nA: A new answer\n"
        qa_file.write_text(new_content, encoding="utf-8")
        store_after = HashSnapshotStore(tmp_path / "snap.json")

        report = _run(ingest_qa_history([qa_file], store_after))

        # Full re-parse: every Q&A in the file is re-emitted.
        assert report.scanned == 1
        assert report.changed == 1
        assert report.events_emitted == 3
        assert patch_writer.call_count == 3


# ---------------------------------------------------------------------------
# AC-003: emit one CalibrationEvent per Q&A pair
# ---------------------------------------------------------------------------


class TestEmitsCalibrationEventsOnChange:
    """AC-003 — one :class:`CalibrationEvent` per parsed Q&A pair."""

    def test_one_event_per_qa_pair_with_correct_payload(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_file], store))

        assert patch_writer.call_count == 2
        for call in patch_writer.call_args_list:
            entity, group_id = call.args
            assert group_id == CALIBRATION_GROUP_ID
            assert entity.source_file == str(qa_file)
            assert entity.question
            assert entity.answer

    def test_routes_writes_to_calibration_group(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_file], store))

        for call in patch_writer.call_args_list:
            assert call.args[1] == "forge_calibration_history"

    def test_returns_ingestion_report_with_counts(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        report = _run(ingest_qa_history([qa_file], store))

        assert isinstance(report, IngestionReport)
        assert report.scanned == 1
        assert report.changed == 1
        assert report.events_emitted == 2
        assert report.partial_parses == 0


# ---------------------------------------------------------------------------
# AC-004: deterministic entity_id from (source_file, line_range_hash)
# ---------------------------------------------------------------------------


class TestDeterministicEntityId:
    """AC-004 — ``@data-integrity deterministic-qa-identity``.

    Re-ingesting the same file content (against a fresh snapshot store
    so the file is treated as "changed") must produce exactly the same
    set of ``entity_id`` values.
    """

    def test_same_content_yields_same_entity_id_set(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)

        store_a = HashSnapshotStore(tmp_path / "snap_a.json")
        _run(ingest_qa_history([qa_file], store_a))
        first_ids = {
            call.args[0].entity_id for call in patch_writer.call_args_list
        }
        patch_writer.reset_mock()

        # Use a fresh snapshot file so the second ingestion treats the
        # file as changed and re-emits every event.
        store_b = HashSnapshotStore(tmp_path / "snap_b.json")
        _run(ingest_qa_history([qa_file], store_b))
        second_ids = {
            call.args[0].entity_id for call in patch_writer.call_args_list
        }

        assert first_ids == second_ids
        assert len(first_ids) == 2

    def test_different_files_with_same_content_yield_different_ids(
        self, tmp_path, patch_writer
    ):
        # Identical content but different paths — entity_id must vary
        # because (source_file, line_range_hash) is the deterministic
        # key.
        qa_a = _write_qa_file(tmp_path, "qa_a.md", SAMPLE_QA)
        qa_b = _write_qa_file(tmp_path, "qa_b.md", SAMPLE_QA)

        store = HashSnapshotStore(tmp_path / "snap.json")
        _run(ingest_qa_history([qa_a, qa_b], store))

        ids_by_file: dict[str, set[str]] = {}
        for call in patch_writer.call_args_list:
            entity = call.args[0]
            ids_by_file.setdefault(entity.source_file, set()).add(
                entity.entity_id
            )
        assert ids_by_file[str(qa_a)].isdisjoint(ids_by_file[str(qa_b)])

    def test_entity_id_is_64char_hex_digest(self, tmp_path, patch_writer):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_file], store))

        for call in patch_writer.call_args_list:
            entity_id = call.args[0].entity_id
            assert len(entity_id) == 64
            assert all(ch in "0123456789abcdef" for ch in entity_id)


# ---------------------------------------------------------------------------
# AC-005: partial-parse tolerance
# ---------------------------------------------------------------------------


class TestPartialParseTolerance:
    """AC-005 — ``@negative partial-parse-tolerance``.

    Malformed Q&A blocks are skipped; well-formed surrounding pairs are
    still ingested; the snapshot record is flagged ``partial=True``.
    """

    def test_partial_block_skipped_valid_pairs_ingested(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", PARTIAL_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        report = _run(ingest_qa_history([qa_file], store))

        assert report.events_emitted == 2
        assert report.partial_parses == 1
        assert patch_writer.call_count == 2

    def test_partial_flag_recorded_on_snapshot_record(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", PARTIAL_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_file], store))

        snap_data = json.loads(
            (tmp_path / "snap.json").read_text(encoding="utf-8")
        )
        record = snap_data[str(qa_file)]
        assert record["partial"] is True

    def test_partial_flag_false_when_all_blocks_valid(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_file], store))

        snap_data = json.loads(
            (tmp_path / "snap.json").read_text(encoding="utf-8")
        )
        record = snap_data[str(qa_file)]
        assert record["partial"] is False


# ---------------------------------------------------------------------------
# AC-006: atomic snapshot update
# ---------------------------------------------------------------------------


class TestAtomicSnapshotUpdate:
    """AC-006 — snapshot store updated atomically.

    The on-disk snapshot file must never be observed in a half-updated
    state. The store buffers all updates in memory and persists via
    ``tempfile + os.replace``.
    """

    def test_stage_does_not_persist_until_commit(self, tmp_path):
        snap_path = tmp_path / "snap.json"
        store = HashSnapshotStore(snap_path)

        store.stage("foo.md", "abc123", False)
        assert not snap_path.exists()

        store.commit()
        assert snap_path.exists()
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        assert data == {"foo.md": {"hash": "abc123", "partial": False}}

    def test_commit_uses_temp_file_in_target_directory_then_rename(
        self, tmp_path, monkeypatch, patch_writer
    ):
        """Verify the temp file lives in the target directory (so the
        ``os.replace`` is a single-filesystem rename) and that exactly
        one rename call occurs per commit."""
        import os as _os

        replace_calls: list[tuple[str, str]] = []
        original_replace = _os.replace

        def tracking_replace(src, dst):
            replace_calls.append((str(src), str(dst)))
            return original_replace(src, dst)

        monkeypatch.setattr("os.replace", tracking_replace)

        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")
        _run(ingest_qa_history([qa_file], store))

        assert len(replace_calls) == 1
        src, dst = replace_calls[0]
        assert dst == str(tmp_path / "snap.json")
        assert Path(src).parent == Path(dst).parent

    def test_snapshot_atomic_for_multi_file_scan(
        self, tmp_path, patch_writer
    ):
        """All files end up in the snapshot after one commit; there is
        no in-flight intermediate state where some files reflect the
        new hash and others don't."""
        qa_a = _write_qa_file(tmp_path, "qa_a.md", SAMPLE_QA)
        qa_b = _write_qa_file(tmp_path, "qa_b.md", PARTIAL_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        _run(ingest_qa_history([qa_a, qa_b], store))

        snap_data = json.loads(
            (tmp_path / "snap.json").read_text(encoding="utf-8")
        )
        assert str(qa_a) in snap_data
        assert str(qa_b) in snap_data
        assert snap_data[str(qa_a)]["partial"] is False
        assert snap_data[str(qa_b)]["partial"] is True


# ---------------------------------------------------------------------------
# AC-007: post-build ingestion refresh / build-start invocation
# ---------------------------------------------------------------------------


class TestPostBuildIngestionRefresh:
    """AC-007 — ``@edge-case post-build-ingestion-refresh``.

    The same async entry point is invoked at build start AND after each
    successful build. Two consecutive invocations on unchanged content
    must produce identical scan counts and zero writes on the second
    call — that is the contract that makes post-build refresh safe.
    """

    def test_back_to_back_invocations_are_idempotent(
        self, tmp_path, patch_writer
    ):
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        first = _run(ingest_qa_history([qa_file], store))
        second = _run(ingest_qa_history([qa_file], store))

        assert first.events_emitted == 2
        assert second.events_emitted == 0
        assert second.changed == 0
        # Total writes only from the first invocation.
        assert patch_writer.call_count == 2

    def test_post_build_refresh_picks_up_new_qa(
        self, tmp_path, patch_writer
    ):
        """A Q&A file edited mid-build is re-ingested on the post-build
        refresh — the new pair becomes a fresh CalibrationEvent."""
        qa_file = _write_qa_file(tmp_path, "qa.md", SAMPLE_QA)
        store = HashSnapshotStore(tmp_path / "snap.json")

        # Build start.
        _run(ingest_qa_history([qa_file], store))
        patch_writer.reset_mock()

        # Operator answers a new Q&A during the build.
        qa_file.write_text(
            SAMPLE_QA + "\nQ: New mid-build Q\nA: New mid-build A\n",
            encoding="utf-8",
        )

        # Post-build refresh.
        store_post = HashSnapshotStore(tmp_path / "snap.json")
        report_post = _run(ingest_qa_history([qa_file], store_post))

        assert report_post.changed == 1
        assert report_post.events_emitted == 3  # full re-parse


# ---------------------------------------------------------------------------
# Parser unit coverage
# ---------------------------------------------------------------------------


class TestParseQAPairs:
    """Parser tolerance to whitespace, BOM, and mixed line endings."""

    def test_parses_two_blocks_separated_by_blank_line(self):
        pairs, partial = parse_qa_pairs(SAMPLE_QA)
        assert len(pairs) == 2
        assert partial == 0

    def test_partial_block_increments_count_only(self):
        pairs, partial = parse_qa_pairs(PARTIAL_QA)
        assert len(pairs) == 2
        assert partial == 1

    def test_tolerates_leading_whitespace_in_block(self):
        text = "  Q: indented question\n  A: indented answer\n"
        pairs, partial = parse_qa_pairs(text)
        assert len(pairs) == 1
        assert pairs[0].question == "indented question"
        assert pairs[0].answer == "indented answer"
        assert partial == 0

    def test_tolerates_bom_and_crlf_via_full_pipeline(
        self, tmp_path, patch_writer
    ):
        """The decode helper strips UTF-8 BOM and normalises CRLF/CR
        line endings before the parser sees the text."""
        qa_file = tmp_path / "qa.md"
        qa_file.write_bytes(
            b"\xef\xbb\xbfQ: With BOM\r\nA: And CRLF\r\n"
        )
        store = HashSnapshotStore(tmp_path / "snap.json")

        report = _run(ingest_qa_history([qa_file], store))

        assert report.events_emitted == 1


# ---------------------------------------------------------------------------
# Defensive snapshot-store load behaviour
# ---------------------------------------------------------------------------


class TestSnapshotStoreLoad:
    """A missing or corrupt snapshot file must not crash startup."""

    def test_missing_snapshot_starts_empty(self, tmp_path):
        store = HashSnapshotStore(tmp_path / "nonexistent.json")
        assert store.get_hash("anything") is None
        assert store.get_partial("anything") is None

    def test_corrupt_snapshot_is_ignored_with_warning(
        self, tmp_path, caplog
    ):
        snap_path = tmp_path / "snap.json"
        snap_path.write_text("not json {{", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="forge.memory.qa_ingestion"):
            store = HashSnapshotStore(snap_path)

        assert store.get_hash("foo") is None
        assert any(
            "qa_snapshot_load_failed" in record.message
            for record in caplog.records
        )

    def test_non_dict_root_is_ignored(self, tmp_path, caplog):
        snap_path = tmp_path / "snap.json"
        snap_path.write_text("[]", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="forge.memory.qa_ingestion"):
            store = HashSnapshotStore(snap_path)

        assert store.get_hash("foo") is None
