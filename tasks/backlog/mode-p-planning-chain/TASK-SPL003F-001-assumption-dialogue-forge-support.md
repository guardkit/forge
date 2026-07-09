---
id: TASK-SPL003F-001
title: 'Mode P assumption-dialogue support — checkpoint detail projection + revision assembler'
task_type: feature
status: backlog
priority: high
created: 2026-07-07T00:00:00Z
updated: 2026-07-07T00:00:00Z
parent_review: null
feature_id: FEAT-SPL-003
wave: null
implementation_mode: direct
complexity: 6
dependencies:
- FEAT-SPL-002
tags:
- planning
- mode-p
- assumption-dialogue
- checkpoint
- revision-assembler
- forge-half
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Mode P assumption-dialogue support — checkpoint detail projection + revision assembler

## Provenance

Forge half of **FEAT-SPL-003 (Assumption Dialogue)**, whose jarvis half is
specified in `../jarvis/features/feat-spl-003-assumption-dialogue/`. Filed
2026-07-07 from that spec's `_summary.md §Forge Half` after Rich **confirmed
ASSUM-014** in the decision-queue curation session: verified against forge
HEAD, SPL-002's checkpoint design supplies the approval surface (`plan-{cid}`
requests, `expected_approver` pinning, defer/escalation machinery) but does
**not** carry the thread anchor or dialogue cycle number today, so this delta
does not fully fall out of SPL-002. Per WS1 §5 the delta is a **task, never a
second feature**.

## Description

Extend the Mode P planning chain (FEAT-SPL-002, `src/forge/planning/`) so the
`product_docs` checkpoint can drive jarvis's per-assumption decision dialogue
and consume the aggregate dispositions it returns. Three parts:

**(1) Checkpoint detail projection.** Project into
`build_planning_approval_envelope` details, per dialogue cycle:
`parent_request_id` (the Slack thread anchor, read from the `planning_runs`
row — already durable, schema_v3), the originating channel, the dialogue
`cycle` number, and the structured assumptions list
`{id, text, confidence, basis}`. Today the details dict carries only
`summary` / `rationale` / `attempt_count`.

**(2) Revision assembler.** Parse per-assumption dispositions from the
approval response — **v1 carrier: JSON in the response `notes` field**, schema
pinned in jarvis FEAT-SPL-003 **ASSUM-003**
(`{"cycle": N, "dispositions": [{id, disposition, value, decided_by, decided_at}]}`).
Then map (per jarvis **ASSUM-006**, aggregate decision mapping): all
`confirmed` → proceed; any `overridden` → assemble an EnrichmentBatch-shaped
revision input → stateless PO re-invoke; any `deferred` → existing
`handle_defer_request`. **The revise-vs-proceed choice is keyed on the parsed
dispositions, NOT on the `decision` literal** (an override rides in as
`decision=approve` carrying the overridden dispositions).

**(3) Cycle cap + trace.** Cap 3 dialogue cycles → escalate to Rich via the
existing escalation path (`checkpoint_type=product_docs_escalated`, durable
`expected_approver` re-target). Record each cycle's dispositions in
`planning_run_events.details_json` (the FEAT-SPL-005 trace spine) — this is the
WS4 curation join: dispositions must be recoverable keyed by assumption id.

**(4) Outbound notification projection.** Project `parent_request_id` /
`target_user` into outbound `NotificationPayload`s **once nats-core Session I
lands those fields** (jarvis ASSUM-001). Until then, notifications degrade to
top-level channel posts on the jarvis side (visible, traceable, unthreaded).

## Cross-venue pins (do not re-derive — these are the jarvis-side contracts)

- **ASSUM-003 (medium)** — dispositions ride `ApprovalResponsePayload.notes`
  as JSON against the frozen nats-core 0.5.0 contract. A first-class
  structured `dispositions` field is the clean follow-up flagged to Session I;
  when it lands, migrate the carrier. **Wire-hygiene:** the `notes` field must
  not be surfaced as human-facing free text while it carries JSON.
- **ASSUM-006 (medium)** — aggregate decision mapping; revise-vs-proceed keyed
  on dispositions. This task's parse logic is the **counterpart** of that
  assumption and must stay in lock-step with it.
- **ASSUM-014 (high)** — this task's own justification (the detail-projection
  delta is real).
- **ASSUM-007 (overridden → ephemeral, 2026-07-07)** — jarvis's notification
  consumer is ephemeral-NEW (no restart replay). This does **not** change the
  forge side, but note: forge remains the once-and-only publisher of each
  notification, so the projection in part (4) still matters for threading.

## Acceptance Criteria

- [x] `build_planning_approval_envelope` details include, per cycle:
  `parent_request_id`, originating channel, `cycle` number, and structured
  assumptions `[{id, text, confidence, basis}]`
- [x] `parent_request_id` is read from the `planning_runs` row (durable),
  never re-derived or held in transient state
- [x] Approval-response handler parses the ASSUM-003 `notes` JSON schema; a
  malformed/absent dispositions payload is handled defensively (logged; does
  not crash the chain)
- [x] Disposition mapping: all-confirmed → proceed; any-overridden → revision
  cycle (EnrichmentBatch → stateless PO re-invoke); any-deferred →
  `handle_defer_request` — keyed on parsed dispositions, not the decision literal
- [x] Cap of 3 dialogue cycles enforced → escalation path on the 4th
- [x] Each cycle's dispositions recorded in `planning_run_events.details_json`,
  keyed by assumption id, recoverable distinctly (WS4 join)
- [x] Outbound `NotificationPayload` projection of `parent_request_id` /
  `target_user` guarded on Session I field availability (no hard dependency
  before it lands)
- [x] All modified files pass project-configured lint/format checks
- [x] No jarvis-side or nats-core edits from this task

## Implementation Notes

- Sequencing: nats-core Session I (items 2/3) lands before or with the build;
  the jarvis notification consumer (deliverable 1) can ship first and degrade
  gracefully until the payload fields exist.
- Keep the disposition schema definition single-sourced with the jarvis
  ASSUM-003 schema — a drift between the writer (jarvis) and parser (forge) is
  the primary failure mode for this task.
- The revision assembler reuses the existing stateless-PO re-invoke path
  (propose-never-elicit — forge assembles the EnrichmentBatch delta; jarvis
  does no reasoning).

## STATUS — 2026-07-09 (forge L13 Opus session): BUILT + full-suite green

Forge half of FEAT-SPL-003 landed on forge main (see commit). Ships **INERT**
(`planning.enabled=False` default untouched — verified). nats-core **0.7.0**
consumed as-is (no nats-core/jarvis code edits — AC honoured).

**What landed (all four parts):**

1. **Checkpoint detail projection** — `build_planning_approval_envelope`
   (`checkpoint.py`) gained `parent_request_id` / `cycle` / `originating_channel`
   / `assumptions` params. Per cycle it projects `parent_request_id` (read from
   the `planning_runs` row — DD-SPL003-1, never re-derived), the 1-based
   dialogue `cycle` (from the durable revision-event count), and the structured
   `[{id,text,confidence,basis}]` under `summary.assumptions` (+ `summary.checkpoint`).
   `checkpoint_product_docs` reads the anchors; escalation/defer re-publishes
   thread them too.
2. **Revision assembler** — new `revision.py`: `parse_dispositions`
   (structured 0.7.0 field first, ASSUM-003 notes-JSON bridge fallback,
   defensive-empty on malformed — never crashes the chain), `aggregate_outcome`
   (proceed/revise/defer keyed on the DISPOSITIONS, not the decision literal —
   ASSUM-006 override-rides-in-as-approve handshake), `assemble_enrichment_batch`
   (EnrichmentBatch-shaped delta, incl. modified-with-`edit_delta`). Wired into
   `_dispatch_approval_response` + the driver's `_handle_revision` (stateless PO
   re-invoke via the existing dispatch path).
3. **Cap-3 + trace** — cap 3 dialogue cycles → escalate to Rich via the existing
   escalation path (`escalate_planning_run`, `checkpoint_type=product_docs_escalated`,
   durable `expected_approver` re-target). Each cycle's dispositions recorded in
   `planning_run_events.details_json` keyed by assumption id (WS4-S7 curation join).
4. **Outbound notification projection** — `notifications.py` /
   `_serve_planning.publish_planning_notification` project `parent_request_id`
   (thread anchor) + `target_user` (originator) into the outbound
   `NotificationPayload`; the anchor fields landed in nats-core 0.7.0, degrade
   to a top-level post when absent (never dropped).

**GATE evidence:**
- Forge's real projection **satisfies jarvis J04's contract fixture**
  (`tests/fixtures/spl003_forge_details.json`, byte-copy of jarvis's; drift-guarded
  against the sibling). Producer is a documented superset — jarvis reads a subset
  of `details` and ignores forge-internal routing keys; the committed jarvis
  fixture already omits them, so no fixture correction was needed (no jarvis edit).
  Proven by `test_spl003f_projection.py` (deep-contains + summary byte-identity +
  real-checkpoint wiring).
- Revision assembler unit-tested incl. modified-with-`edit_delta` and the
  notes-JSON bridge (`test_revision.py`, 24 tests); driver revise→re-invoke→handoff
  + cap-3 escalation (`test_driver_revision.py`).
- Full forge suite: **8 failed / 5389 passed / 8 skipped / 2 errors** — failing
  set **byte-identical** to the pre-existing infra baseline
  (docker/postgres/live-broker; verified via sorted-failset diff). +35 new passing.
- Planning-**inert** test green; `PlanningConfig.enabled` default `False` untouched.

**Merge-review sweep (DD4F rule).** 4-agent adversarial review of the diff.
Two real findings fixed pre-commit, each pinned by a test:
- **Stale request_id across dialogue cycles (dominant correctness).** The revise
  re-checkpoint reused `attempt_count=0`, so every cycle derived the SAME
  `request_id` → the driver's stale-round guard would accept a redelivered
  prior-cycle response and jarvis's JNB-103 capture would treat the new prompt
  as a duplicate. Fixed: `checkpoint_product_docs` now derives a **monotonic**
  `attempt_count` from the persisted `pending_approval_request_id`
  (`_next_checkpoint_attempt`) — initial 0, every re-round distinct — unifying
  revise with the defer/escalation scheme. Pinned: `test_driver_revision`
  asserts cycle-2 bumps to attempt 1.
- **Cap-3 escalation asked Rich to decide blind.** The escalated/deferred
  re-publish carried a thin summary omitting the assumptions under dispute.
  Fixed: `_latest_assumptions` reads the latest PO output and both re-publishes
  now project the assumptions. Pinned:
  `test_cap_escalation_envelope_carries_the_assumptions`.
Also applied: single-sourced the `planning-revision` label + cycle arithmetic
(`revision.dialogue_cycle` / `REVISION_STAGE_LABEL`) so the projected cycle and
the cap gate can never drift; reused `store.get_run` (dropped hand-rolled
f-string SQL); exempted `decision="override"` from the dialogue interception so
its audit branch is never shadowed.

**Dated deviation (backward-edge §7.6 producer obligations).** The 2026-07-08
_summary.md amendment places `planning_outcome` / `approval_decision` /
`spec_survival` fleet-memory episode-producer obligations on this build. The
durable substrate they require IS landed: per-cycle dispositions are first-class
(`disposition`+`edit_delta`, keyed by assumption id) in `planning_run_events`, and
**pure projector functions** `build_planning_outcome_episode` (§4.1) /
`build_approval_decision_episode` (§4.2) are shipped + unit-tested against the
contract (observed-originator/approver rules, trace_ref-when-not-accepted, cycle
carried). The **live graphiti episode EMISSION + `spec_survival` v1/v2 edges are
deferred to WS4-S7** — that wiring is downstream-gated (no fleet-memory registry
merge until WS4-S7 per contract §5) and would be un-integration-testable dead
wiring in this inert build (the exact anti-pattern this repo's post-merge reviews
flagged). Forge remains the single writer; the producer locus is here and ready.
