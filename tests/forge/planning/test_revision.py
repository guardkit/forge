"""Unit tests for the assumption-dialogue revision assembler (TASK-SPL003F-001).

Covers the forge counterpart of jarvis FEAT-SPL-003 ASSUM-003/ASSUM-006:
parsing per-assumption dispositions off the approval response (structured
0.7.0 field + the notes-JSON bridge + defensive-on-malformed), the aggregate
revise/proceed/defer mapping keyed on the dispositions, the EnrichmentBatch
assembly (incl. modified-with-edit_delta), and the backward-edge episode
projectors (§4.1 / §4.2).
"""

from __future__ import annotations

import json

from nats_core.events import ApprovalResponsePayload, AssumptionDisposition

from forge.planning.revision import (
    CYCLE_CAP,
    aggregate_outcome,
    assemble_enrichment_batch,
    build_approval_decision_episode,
    build_planning_outcome_episode,
    dispositions_by_assumption,
    normalize_assumptions,
    parse_dispositions,
)


def _response(*, decision="approve", dispositions=None, notes=None):
    return ApprovalResponsePayload(
        request_id="req-1",
        decision=decision,
        decided_by="U03QR8WKT29",
        dispositions=dispositions,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# parse_dispositions — structured (0.7.0) first, notes-JSON bridge fallback
# ---------------------------------------------------------------------------


class TestParseDispositions:
    def test_structured_field_wins(self):
        response = _response(
            decision="approve",
            dispositions=[
                AssumptionDisposition(assumption_id="A1", disposition="accepted"),
                AssumptionDisposition(
                    assumption_id="A2", disposition="modified", edit_delta="Use SQLite."
                ),
            ],
        )
        parsed = parse_dispositions(response)
        assert [d.assumption_id for d in parsed] == ["A1", "A2"]
        assert parsed[1].disposition == "modified"
        assert parsed[1].edit_delta == "Use SQLite."

    def test_notes_json_bridge_fallback(self):
        # ASSUM-003 bridge shape: {"cycle": N, "dispositions": [{id, disposition, value}]}
        notes = json.dumps(
            {
                "cycle": 1,
                "dispositions": [
                    {"id": "A1", "disposition": "accepted"},
                    {"id": "A2", "disposition": "modified", "value": "Prefer gRPC."},
                ],
            }
        )
        parsed = parse_dispositions(_response(decision="approve", notes=notes))
        by_id = {d.assumption_id: d for d in parsed}
        assert set(by_id) == {"A1", "A2"}
        assert by_id["A2"].disposition == "modified"
        assert by_id["A2"].edit_delta == "Prefer gRPC."

    def test_structured_preferred_over_notes(self):
        response = _response(
            decision="approve",
            dispositions=[
                AssumptionDisposition(assumption_id="A1", disposition="accepted")
            ],
            notes=json.dumps(
                {"dispositions": [{"id": "IGNORED", "disposition": "modified"}]}
            ),
        )
        parsed = parse_dispositions(response)
        assert [d.assumption_id for d in parsed] == ["A1"]

    def test_human_free_text_notes_is_not_bridge(self):
        # notes carrying human text (not JSON) → no dispositions, no crash
        assert parse_dispositions(_response(notes="looks good, ship it")) == []

    def test_malformed_bridge_items_dropped_not_crashed(self):
        notes = json.dumps(
            {
                "dispositions": [
                    {"id": "A1", "disposition": "accepted"},
                    {"disposition": "modified"},  # no id → dropped
                    "not-a-dict",  # → dropped
                    {"id": "A3"},  # no disposition → dropped
                ]
            }
        )
        parsed = parse_dispositions(_response(notes=notes))
        assert [d.assumption_id for d in parsed] == ["A1"]

    def test_absent_dispositions_and_notes(self):
        assert parse_dispositions(_response()) == []


# ---------------------------------------------------------------------------
# aggregate_outcome — keyed on dispositions, NOT the decision literal
# ---------------------------------------------------------------------------


class TestAggregateOutcome:
    def test_all_accepted_is_proceed(self):
        disps = [
            AssumptionDisposition(assumption_id="A1", disposition="accepted"),
            AssumptionDisposition(assumption_id="A2", disposition="accepted"),
        ]
        assert aggregate_outcome(disps) == "proceed"

    def test_any_modified_is_revise(self):
        disps = [
            AssumptionDisposition(assumption_id="A1", disposition="accepted"),
            AssumptionDisposition(
                assumption_id="A2", disposition="modified", edit_delta="x"
            ),
        ]
        assert aggregate_outcome(disps) == "revise"

    def test_any_deferred_takes_precedence_over_modified(self):
        disps = [
            AssumptionDisposition(
                assumption_id="A1", disposition="modified", edit_delta="x"
            ),
            AssumptionDisposition(assumption_id="A2", disposition="deferred"),
        ]
        assert aggregate_outcome(disps) == "defer"

    def test_rejected_and_undecided_are_revise(self):
        assert (
            aggregate_outcome(
                [AssumptionDisposition(assumption_id="A1", disposition="rejected")]
            )
            == "revise"
        )
        assert (
            aggregate_outcome(
                [AssumptionDisposition(assumption_id="A1", disposition="undecided")]
            )
            == "revise"
        )

    def test_empty_is_proceed(self):
        assert aggregate_outcome([]) == "proceed"

    def test_modification_rides_decision_approve(self):
        # The wire handshake: an override rides in as decision="approve" while
        # the dispositions carry a modification — the outcome keys on the
        # dispositions (revise), never the literal (which would proceed).
        response = _response(
            decision="approve",
            dispositions=[
                AssumptionDisposition(
                    assumption_id="A1", disposition="modified", edit_delta="x"
                )
            ],
        )
        assert aggregate_outcome(parse_dispositions(response)) == "revise"


# ---------------------------------------------------------------------------
# normalize_assumptions
# ---------------------------------------------------------------------------


class TestNormalizeAssumptions:
    def test_canonical_shape_passes_through(self):
        raw = [{"id": "A1", "text": "t", "confidence": "high", "basis": "b"}]
        assert normalize_assumptions(raw) == raw

    def test_key_variants_mapped(self):
        raw = [
            {
                "assumption_id": "A1",
                "statement": "t",
                "confidence": "low",
                "rationale": "b",
            }
        ]
        assert normalize_assumptions(raw) == [
            {"id": "A1", "text": "t", "confidence": "low", "basis": "b"}
        ]

    def test_items_without_id_dropped(self):
        assert normalize_assumptions([{"text": "no id"}]) == []

    def test_non_list_is_empty(self):
        assert normalize_assumptions(None) == []
        assert normalize_assumptions({"id": "A1"}) == []


# ---------------------------------------------------------------------------
# assemble_enrichment_batch — incl. modified-with-edit_delta
# ---------------------------------------------------------------------------


class TestAssembleEnrichmentBatch:
    def test_modified_carries_edit_delta(self):
        prior = [
            {
                "id": "A1",
                "text": "REST API",
                "confidence": "medium",
                "basis": "house default",
            },
            {
                "id": "A2",
                "text": "Postgres",
                "confidence": "low",
                "basis": "durable state",
            },
        ]
        disps = [
            AssumptionDisposition(assumption_id="A1", disposition="accepted"),
            AssumptionDisposition(
                assumption_id="A2",
                disposition="modified",
                edit_delta="Use SQLite instead.",
            ),
        ]
        batch = assemble_enrichment_batch(
            correlation_id="cid1", cycle=2, prior_assumptions=prior, dispositions=disps
        )
        assert batch["kind"] == "enrichment_batch"
        assert batch["correlation_id"] == "cid1"
        assert batch["cycle"] == 2
        by_id = {r["assumption_id"]: r for r in batch["revisions"]}
        assert by_id["A2"]["disposition"] == "modified"
        assert by_id["A2"]["edit_delta"] == "Use SQLite instead."
        assert by_id["A2"]["prior"]["text"] == "Postgres"
        assert by_id["A1"]["disposition"] == "accepted"
        assert by_id["A1"]["edit_delta"] is None

    def test_batch_is_total_over_prior_assumptions(self):
        # An assumption with no returned disposition is carried as undecided.
        prior = [{"id": "A1", "text": "t", "confidence": "high", "basis": "b"}]
        batch = assemble_enrichment_batch(
            correlation_id="cid1", cycle=2, prior_assumptions=prior, dispositions=[]
        )
        assert len(batch["revisions"]) == 1
        assert batch["revisions"][0]["disposition"] == "undecided"

    def test_batch_is_json_serialisable(self):
        prior = [{"id": "A1", "text": "t", "confidence": "high", "basis": "b"}]
        disps = [
            AssumptionDisposition(
                assumption_id="A1", disposition="modified", edit_delta="d"
            )
        ]
        batch = assemble_enrichment_batch(
            correlation_id="cid1", cycle=2, prior_assumptions=prior, dispositions=disps
        )
        json.dumps(batch)  # must not raise


# ---------------------------------------------------------------------------
# dispositions_by_assumption — WS4 curation join (keyed by assumption id)
# ---------------------------------------------------------------------------


def test_dispositions_by_assumption_is_keyed_and_recoverable():
    disps = [
        AssumptionDisposition(assumption_id="A1", disposition="accepted"),
        AssumptionDisposition(
            assumption_id="A2", disposition="modified", edit_delta="d", notes="n"
        ),
    ]
    block = dispositions_by_assumption(disps)
    assert set(block) == {"A1", "A2"}
    assert block["A2"] == {"disposition": "modified", "edit_delta": "d", "notes": "n"}


# ---------------------------------------------------------------------------
# Backward-edge episode projectors (§4.1 / §4.2)
# ---------------------------------------------------------------------------


class TestPlanningOutcomeEpisode:
    def test_first_class_disposition_block(self):
        surfaced = [
            {"id": "A1", "text": "REST", "confidence": "medium", "basis": "b"},
            {"id": "A2", "text": "PG", "confidence": "low", "basis": "b"},
        ]
        trace = {
            "A1": {"disposition": "accepted", "edit_delta": None, "notes": None},
            "A2": {
                "disposition": "modified",
                "edit_delta": "Use SQLite.",
                "notes": None,
            },
        }
        episode = build_planning_outcome_episode(
            correlation_id="cid1",
            originator="U03QR8WKT29",
            terminal_state="planned_handoff",
            assumptions_trace=trace,
            surfaced_assumptions=surfaced,
            started_at="2026-07-09T00:00:00Z",
            duration_seconds=42,
            approval_cycles_used=2,
            trace_ref="planning_runs/cid1",
        )
        assert episode["mode"] == "mode_p"
        assert episode["assumption_count"] == 2
        assert len(episode["assumptions"]) == 2
        a2 = next(a for a in episode["assumptions"] if a["assumption_id"] == "A2")
        assert a2["disposition"] == "modified"
        assert a2["edit_delta"] == "Use SQLite."
        # observed originator, never a config echo
        assert episode["originator"] == "U03QR8WKT29"
        assert episode["trace_ref"] == "planning_runs/cid1"


class TestApprovalDecisionEpisode:
    def test_approver_required_for_observed_decision(self):
        episode = build_approval_decision_episode(
            gate_id="plan-cid1",
            decision="approved",
            cycle=1,
            latency_seconds=12,
            correlation_id="cid1",
            approver="U03QR8WKT29",
        )
        assert episode["gate_kind"] == "planning_assumptions"
        assert episode["approver"] == "U03QR8WKT29"
        assert episode["cycle"] == 1

    def test_approver_forbidden_for_timed_out(self):
        episode = build_approval_decision_episode(
            gate_id="plan-cid1",
            decision="timed_out",
            cycle=3,
            latency_seconds=3600,
            correlation_id="cid1",
            approver="U03QR8WKT29",  # supplied but MUST be dropped (LPA-19)
        )
        assert "approver" not in episode


def test_cycle_cap_matches_jarvis():
    # Frozen cap-3 contract mirrored across venues.
    assert CYCLE_CAP == 3
