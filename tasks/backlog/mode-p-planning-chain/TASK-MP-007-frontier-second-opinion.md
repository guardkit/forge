---
id: TASK-MP-007
title: DF-006 frontier second opinion (FLAG-only, policy-filtered brief, degrade-to-human)
task_type: feature
status: in_review
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 4
implementation_mode: task-work
complexity: 4
estimated_minutes: 55
dependencies:
- TASK-MP-004B
- TASK-MP-001
tags:
- mode-p
- frontier
- df-006
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T13:40:59.209840'
  last_updated: '2026-07-06T13:52:24.972010'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Type mismatch: code returns boolean True instead of EscalationOutcome.ESCALATED
      enum value. Test failure: ''assert outcome == EscalationOutcome.ESCALATED''
      failed with ''AssertionError: assert True == <EscalationOutcome.ESCALATED: ''escalated''>''.
      This suggests the escalation module integration with frontier is incorrect,
      or return types don''t match the expected enum structure.: Review escalation.py
      integration with frontier.py. Ensure FrontierSecondOpinion.get_summary_for_approval
      returns the correct dict structure, not boolean values. Verify all code paths
      return expected types matching the SecondOpinionProvider protocol.

      - 3 tests failed in total. Tests are the authoritative oracle for acceptance
      criteria verification. Without passing tests, no AC can be independently verified.:
      Fix the type mismatch issue identified above, then re-run the full test suite.
      Ensure all tests pass before resubmitting.

      - Coverage requirement not met: coverage_met=false with coverage_required=true.
      The quality gate for test coverage was not satisfied.: After fixing test failures,
      verify coverage thresholds are met. Ensure all branches, error paths, and edge
      cases have test coverage per project configuration.

      ... and 1 more issues'
    timestamp: '2026-07-06T13:40:59.209840'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-07-06T13:45:48.544779'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-007 — DF-006 frontier second opinion

## Description

The config-gated escalation subcontractor, implementing TASK-MP-004B's
`SecondOpinionProvider` Protocol. Fires ONLY on flag-for-review outcomes (derived
from the existing Degraded -> FLAG_FOR_REVIEW dispatch-outcome contract, ASSUM-013
— no new numeric threshold). The brief is a **compressed, policy-filtered**
structured JSON (field allowlist over the PO output — DF-009 §2.3 verbatim,
restored by the panel's RT-09) and never contains the raw conversation. Unreachable
frontier degrades to forced human review. The provider returns data and
structurally cannot approve.

## BDD Scenarios

- "No frontier second opinion is sought when the toggle is disabled"
- "An unreachable frontier service degrades to forced human review"
- "A frontier second opinion receives only a compressed structured brief"

## Files

- Creates: `src/forge/planning/frontier.py` (`build_compressed_brief(po_output) -> dict` field-allowlisted; `FrontierSecondOpinion` implementing SecondOpinionProvider over an injected `FrontierClient` Protocol)
- Tests: `tests/forge/planning/test_frontier.py`

## Acceptance Criteria

- [ ] `frontier_enabled=False` (the default) -> the recording FrontierClient fake shows zero calls AND flagged product docs still pause for human review
- [ ] Enabled + non-flagged outcome -> zero calls (FLAG-only predicate)
- [ ] The brief passed to the client contains only allowlisted keys (docs summary, assumptions, coach evidence, structured findings) and no `transcript`/`messages`/`request_text`/raw-conversation field — schema assertion on the recorded call (policy-filter predicate)
- [ ] Client raising / timing out -> checkpoint still pauses and the approval request carries a "second opinion unavailable" note (degrade-to-human, DF-006)
- [ ] The provider's return type carries opinion data only — no approve/decision field exists on it (type-level never-approve predicate); the opinion is attached to the approval request for the human
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit with a recording fake FrontierClient; zero network anywhere.

## Implementation Notes

- DF-001/DF-006: this is the ONLY cloud-adjacent surface in Mode P and it is
  default-off, attended-by-construction (its output lands in front of a human
  gate). The DF-004 audit (TASK-MP-001) covers the model_resolution block.
- Timeout from `PlanningConfig.frontier_timeout_seconds`, enforced around the
  injected client call.
