"""Round-trip contract test for the SSE translator (TASK-FRR-PEB-003).

AC-4 of TASK-FRR-PEB-003 mandates a contract test that round-trips a
known ``AutobuildState`` mutation sequence through a recorded SSE stream
fixture and validates the emitted ``pipeline.*`` envelopes against the
``nats_core.events`` Pydantic schemas. The fixture covers both:

* the **success path** (``starting → planning_waves → running_wave →
  running_wave (with stage delta) → completed``); and
* the **failure path** (``starting → running_wave → failed``).

The test feeds each recorded line through
:meth:`StreamEventTranslator.translate` in order, and asserts:

1. Each emitted payload is a valid Pydantic instance whose
   ``correlation_id`` is non-empty (matches the §4 STREAM_EVENT_SCHEMA
   format constraint).
2. The emitted payload type matches the fixture's ``_expected_envelope``
   tag — fixture authors annotate every line with the envelope they
   expect (or ``null`` for stream parts that should be no-ops).
3. The full sequence emits exactly one envelope per non-null fixture
   line — no duplicate emits, no skipped emits.

When the ``langgraph-api`` minor version is bumped (per AC-5), this
fixture MUST be re-recorded against the new sidecar — silent SSE-shape
drift is the Option C risk this contract test is designed to surface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from langgraph_sdk.schema import StreamPart
from nats_core.events import (
    BuildCompletePayload,
    BuildFailedPayload,
    BuildStartedPayload,
    StageCompletePayload,
)

from forge.lifecycle_bridge.bridge import BuildContext
from forge.lifecycle_bridge.translation import (
    PipelineEvent,
    StreamEventTranslator,
)

from tests.forge.lifecycle_bridge.fixtures import CANONICAL_FIXTURE


_ENVELOPE_BY_NAME: dict[str, type] = {
    "BuildStartedPayload": BuildStartedPayload,
    "StageCompletePayload": StageCompletePayload,
    "BuildCompletePayload": BuildCompletePayload,
    "BuildFailedPayload": BuildFailedPayload,
}


def _load_fixture() -> list[dict]:
    """Load the JSONL fixture as a list of dicts (one per line)."""
    lines: list[dict] = []
    text = CANONICAL_FIXTURE.read_text(encoding="utf-8")
    for raw in text.splitlines():
        if not raw.strip():
            continue
        lines.append(json.loads(raw))
    return lines


def _make_context(
    feature_id: str, *, correlation_id: str
) -> BuildContext:
    return BuildContext(
        feature_id=feature_id,
        thread_id="thread-contract",
        run_id="run-contract",
        correlation_id=correlation_id,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )


def _stream_part_from_record(record: dict) -> StreamPart:
    return StreamPart(
        event=record["event"],
        data=record.get("data") or {},
        id=record.get("id"),
    )


# ---------------------------------------------------------------------------
# AC-4: success-path round-trip
# ---------------------------------------------------------------------------


class TestSuccessPathRoundTrip:
    """Success-path round-trip: starting → … → completed."""

    def test_success_path_emits_expected_envelope_sequence(self) -> None:
        records = [r for r in _load_fixture() if r.get("_path") in ("success", "common")]
        translator = StreamEventTranslator()
        ctx = _make_context("FEAT-CANON-OK", correlation_id="corr-canon-ok")

        emitted: list[tuple[str | None, PipelineEvent | None]] = []
        for record in records:
            part = _stream_part_from_record(record)
            out = translator.translate(part, ctx)
            emitted.append((record.get("_expected_envelope"), out))

        # Each non-null expected_envelope MUST yield a matching payload type.
        for expected_name, payload in emitted:
            if expected_name is None:
                assert payload is None, (
                    f"fixture marked no-op but translator emitted {type(payload).__name__}"
                )
                continue
            expected_cls = _ENVELOPE_BY_NAME[expected_name]
            assert isinstance(payload, expected_cls), (
                f"expected {expected_name}; got {type(payload).__name__}"
            )
            # AC-3 / §4 schema constraint: correlation_id is non-empty.
            cid = getattr(payload, "correlation_id", None)
            assert isinstance(cid, str) and cid, (
                f"{expected_name} must carry a non-empty correlation_id; got {cid!r}"
            )
            assert cid == ctx.correlation_id


# ---------------------------------------------------------------------------
# AC-4: failure-path round-trip
# ---------------------------------------------------------------------------


class TestFailurePathRoundTrip:
    """Failure-path round-trip: starting → running_wave → failed."""

    def test_failure_path_emits_build_failed(self) -> None:
        records = [r for r in _load_fixture() if r.get("_path") == "failure"]
        translator = StreamEventTranslator()
        ctx = _make_context("FEAT-CANON-FAIL", correlation_id="corr-canon-fail")

        emitted_types: list[str | None] = []
        for record in records:
            part = _stream_part_from_record(record)
            out = translator.translate(part, ctx)
            emitted_types.append(type(out).__name__ if out is not None else None)

        # The terminal envelope MUST be BuildFailedPayload.
        assert "BuildFailedPayload" in emitted_types

    def test_failure_path_terminal_carries_correlation_id(self) -> None:
        records = [r for r in _load_fixture() if r.get("_path") == "failure"]
        translator = StreamEventTranslator()
        ctx = _make_context("FEAT-CANON-FAIL", correlation_id="corr-canon-fail")

        terminal: PipelineEvent | None = None
        for record in records:
            out = translator.translate(_stream_part_from_record(record), ctx)
            if isinstance(out, BuildFailedPayload):
                terminal = out
        assert terminal is not None
        assert getattr(terminal, "correlation_id", None) == "corr-canon-fail"


# ---------------------------------------------------------------------------
# Property: every StreamPart produces ≤ 1 envelope (no double-emits)
# ---------------------------------------------------------------------------


class TestNoDoubleEmits:
    """Per AC test requirement: every ``StreamPart`` in the canonical
    fixture produces exactly one envelope or ``None``.
    """

    def test_each_fixture_line_yields_at_most_one_envelope(self) -> None:
        records = _load_fixture()
        # We use two translators (one per build) because the success and
        # failure paths share a single fixture but represent two
        # independent builds.
        ok_translator = StreamEventTranslator()
        fail_translator = StreamEventTranslator()
        ok_ctx = _make_context("FEAT-CANON-OK", correlation_id="corr-canon-ok")
        fail_ctx = _make_context("FEAT-CANON-FAIL", correlation_id="corr-canon-fail")

        for record in records:
            part = _stream_part_from_record(record)
            path = record.get("_path")
            translator = ok_translator if path != "failure" else fail_translator
            ctx = ok_ctx if path != "failure" else fail_ctx
            out = translator.translate(part, ctx)
            # ``out`` is either None or a PipelineEvent instance — never
            # a list, never a tuple.
            assert out is None or hasattr(out, "model_dump"), (
                f"translate() returned non-payload {type(out).__name__} for "
                f"fixture id={record.get('id')!r}"
            )


# ---------------------------------------------------------------------------
# §4 contract: payload validates as Pydantic model with non-empty correlation_id
# ---------------------------------------------------------------------------


class TestSchemaContract:
    """T4's seam test will import the translator, feed a recorded
    StreamPart, and assert the returned PipelineEvent is a valid
    Pydantic model with non-empty correlation_id. Mirror that
    assertion here so the contract is locked from the producer side
    too.
    """

    @pytest.mark.parametrize(
        "envelope_name",
        ["BuildStartedPayload", "StageCompletePayload", "BuildCompletePayload"],
    )
    def test_success_path_envelope_validates_and_carries_correlation_id(
        self, envelope_name: str
    ) -> None:
        records = [
            r for r in _load_fixture() if r.get("_path") in ("success", "common")
        ]
        translator = StreamEventTranslator()
        ctx = _make_context("FEAT-CANON-OK", correlation_id="corr-canon-ok")

        found: PipelineEvent | None = None
        for record in records:
            out = translator.translate(_stream_part_from_record(record), ctx)
            if out is not None and type(out).__name__ == envelope_name:
                found = out
                break
        assert found is not None, (
            f"expected at least one {envelope_name} in success-path fixture"
        )
        # Pydantic round-trip — model_dump() ⇒ model_validate() recovers
        # the value (modulo non-schema fields like the v1 attached
        # correlation_id, which is intentionally not in the v1 schema).
        cls = type(found)
        round_tripped = cls.model_validate(found.model_dump())
        assert isinstance(round_tripped, cls)
        # correlation_id is non-empty.
        cid = getattr(found, "correlation_id", None)
        assert isinstance(cid, str) and cid
