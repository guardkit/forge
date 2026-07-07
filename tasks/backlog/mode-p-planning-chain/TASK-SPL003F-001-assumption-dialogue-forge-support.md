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

- [ ] `build_planning_approval_envelope` details include, per cycle:
  `parent_request_id`, originating channel, `cycle` number, and structured
  assumptions `[{id, text, confidence, basis}]`
- [ ] `parent_request_id` is read from the `planning_runs` row (durable),
  never re-derived or held in transient state
- [ ] Approval-response handler parses the ASSUM-003 `notes` JSON schema; a
  malformed/absent dispositions payload is handled defensively (logged; does
  not crash the chain)
- [ ] Disposition mapping: all-confirmed → proceed; any-overridden → revision
  cycle (EnrichmentBatch → stateless PO re-invoke); any-deferred →
  `handle_defer_request` — keyed on parsed dispositions, not the decision literal
- [ ] Cap of 3 dialogue cycles enforced → escalation path on the 4th
- [ ] Each cycle's dispositions recorded in `planning_run_events.details_json`,
  keyed by assumption id, recoverable distinctly (WS4 join)
- [ ] Outbound `NotificationPayload` projection of `parent_request_id` /
  `target_user` guarded on Session I field availability (no hard dependency
  before it lands)
- [ ] All modified files pass project-configured lint/format checks
- [ ] No jarvis-side or nats-core edits from this task

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
