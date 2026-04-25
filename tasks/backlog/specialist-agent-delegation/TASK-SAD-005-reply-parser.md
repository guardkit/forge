---
id: TASK-SAD-005
title: 'Reply parser: Coach top-level/nested, FLAG_FOR_REVIEW fallback, malformed
  envelope'
task_type: feature
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 2
implementation_mode: task-work
complexity: 5
dependencies:
- TASK-SAD-001
tags:
- dispatch
- parser
- coach-output
- schema-validation
- gate-fallback
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-003
  base_branch: main
  started_at: '2026-04-25T16:36:39.056500'
  last_updated: '2026-04-25T16:46:03.453096'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T16:36:39.056500'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# Task: Reply parser — Coach output extraction, FLAG_FOR_REVIEW fallback, malformed envelope handling

## Description

Implement the parser that converts a specialist reply payload into a
`DispatchOutcome` (TASK-SAD-001). Three behaviours the spec calls out:

1. **Top-level Coach fields are preferred over nested** (A.coach-output-top-vs-nested).
   The reply may carry `coach_score` / `criterion_breakdown` / `detection_findings`
   either at the top of the payload or nested under `result`. The parser MUST
   prefer the top-level fields and treat the nested ones as fallback evidence
   only.
2. **Missing Coach score → FLAG_FOR_REVIEW** (C.missing-coach-score, ASSUM-006).
   When neither top-level nor nested Coach score is present, the parser
   produces a `SyncResult` with `coach_score=None`. The gating layer
   (FEAT-FORGE-004) interprets this as FLAG_FOR_REVIEW; the parser does not
   fabricate a score.
3. **Malformed envelope → DispatchError** (C.malformed-reply-envelope). A
   reply that fails schema validation produces a `DispatchError` with a
   schema-validation reason. It is NOT fed to the gating layer.

Also handles:
- Specialist error result (C.specialist-error) → `DispatchError` with the
  specialist's error explanation.
- Async-mode initial reply (D.async-mode-polling-initial) → `AsyncPending`
  with the run identifier.

## Interface

```python
# src/forge/dispatch/reply_parser.py
from forge.dispatch.models import DispatchOutcome, SyncResult, AsyncPending, DispatchError


def parse_reply(
    payload: dict,
    *,
    resolution_id: str,
    attempt_no: int,
) -> DispatchOutcome:
    """Convert a specialist reply payload into a DispatchOutcome.

    Resolution order:
      1. envelope validation fails → DispatchError(schema_validation)
      2. payload.error → DispatchError(specialist_error)
      3. payload.run_identifier → AsyncPending
      4. otherwise → SyncResult, with Coach fields extracted top-level-first
    """
```

## Coach extraction rule

```python
def _extract_coach_fields(payload: dict) -> tuple[Optional[float], dict, list]:
    """Top-level fields preferred. Nested only as fallback.

    The nested `result` block is retained as fallback evidence (logged) but
    NOT passed to the gating layer when top-level fields are present.
    """
    top = payload
    nested = payload.get("result", {})
    score = top.get("coach_score") or nested.get("coach_score")
    breakdown = top.get("criterion_breakdown") or nested.get("criterion_breakdown") or {}
    findings = top.get("detection_findings") or nested.get("detection_findings") or []
    return score, breakdown, findings
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/reply_parser.py` exposes `parse_reply()` returning
      a `DispatchOutcome` (sum type from TASK-SAD-001).
- [ ] Test (A.coach-output-top-vs-nested): payload with both top-level and
      nested Coach fields → `SyncResult` uses top-level values; nested are
      not used.
- [ ] Test (A.coach-output-nested-fallback): payload with only nested Coach
      fields → `SyncResult` falls back to nested values.
- [ ] Test (C.missing-coach-score): payload with no Coach score anywhere →
      `SyncResult` with `coach_score=None`. The parser does NOT fabricate
      a default score; the gating layer's FLAG_FOR_REVIEW rule kicks in.
- [ ] Test (C.malformed-reply-envelope): payload missing required envelope
      fields → `DispatchError` with `error_explanation` mentioning schema
      validation. Coach fields (even if present in the malformed payload)
      are NOT extracted.
- [ ] Test (C.specialist-error): payload carrying an `error` key →
      `DispatchError` with the specialist's explanation copied verbatim
      into `error_explanation`.
- [ ] Test (D.async-mode-polling-initial): payload carrying a
      `run_identifier` key → `AsyncPending` with that identifier.
- [ ] Parser is pure (no I/O, no logging of payload values — log only the
      outcome kind for observability).
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Implementation Notes

- The "envelope validation" step should be a Pydantic model
  (`SpecialistReplyEnvelope`) so failures produce a structured
  `ValidationError`. Convert that into a `DispatchError` at the boundary.
- Order matters: schema validation is FIRST. A payload with an `error` key
  AND a malformed envelope is a malformed-envelope `DispatchError` — not a
  specialist-error. This makes the schema the source of truth.
- Do NOT log raw payload values in error paths. Use only the field names
  / outcome kind so sensitive parameters cannot leak via log scraping.
