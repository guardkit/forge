---
id: TASK-MP-001
title: PlanningConfig section + DF-004 planning model-resolution audit
task_type: feature
status: in_review
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 1
implementation_mode: task-work
complexity: 4
estimated_minutes: 60
dependencies: []
tags:
- mode-p
- config
- df-004
autobuild_state:
  current_turn: 2
  max_turns: 5
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2
  base_branch: main
  started_at: '2026-07-06T12:58:39.978407'
  last_updated: '2026-07-06T13:13:47.077145'
  turns:
  - turn: 1
    decision: feedback
    feedback: '- Deterministic honesty record (claim_audit, severity=critical): Player
      claim: Player claimed file `src/forge/config/models.py. Actual: Path absent
      from ''git status --porcelain'' so ''git add -A'' would not stage it. Probes:
      path_exists=False; gitignore_match=no rule matched; tracked=no. Most likely
      cause: the Player claimed work on a file that does not exist on disk..

      - Deterministic honesty record (claim_audit, severity=critical): Player claim:
      Player claimed file `src/forge/planning/audit.py. Actual: Path absent from ''git
      status --porcelain'' so ''git add -A'' would not stage it. Probes: path_exists=False;
      gitignore_match=no rule matched; tracked=no. Most likely cause: the Player claimed
      work on a file that does not exist on disk..

      - Evidence gathering aborted with status ''partial_honesty_abort'' due to malformed
      file paths in Player report. The Player''s files_modified and files_created
      lists contain backtick-prefixed entries (e.g., ''`src/forge/config/models.py''
      and ''`src/forge/planning/audit.py'') that the honesty checker correctly identified
      as non-existent paths. These appear to be markdown formatting artifacts accidentally
      included as literal path strings. All independent verification (tests, coverage,
      quality gates, architectural review) is null/absent.: Remove the malformed backtick-prefixed
      file path entries from files_modified and files_created lists. Keep only the
      correctly-formatted absolute paths (e.g., ''/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-3ED2/src/forge/config/models.py'')
      or relative paths without backtick prefixes (e.g., ''src/forge/config/models.py'').
      Once the report is corrected, the orchestrator will re-run evidence gathering
      to provide independent verification.

      ... and 2 more issues'
    timestamp: '2026-07-06T12:58:39.978407'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
  - turn: 2
    decision: approve
    feedback: null
    timestamp: '2026-07-06T13:07:29.640135'
    player_summary: 'Implementation via task-work delegation. Files planned: 0, Files
      actual: 0'
    player_success: true
    coach_success: true
---

# TASK-MP-001 — PlanningConfig section + DF-004 planning model-resolution audit

## Description

Add the minimal planning approval-routing/config surface Mode P needs as a NEW
sibling section in forge config (`ApprovalConfig` is a deliberately closed surface:
`extra="forbid"`, its docstring forbids escalation fields — src/forge/config/models.py:162-236),
plus the DF-004 `fallbacks:[]` audit as a **pure function** so the boot scenario
anchors to a unit seam.

## BDD Scenarios (features/mode-p-planning-chain/mode-p-planning-chain.feature)

- Background (config surface)
- "A cloud fallback in planning model resolution fails the planning audit loudly" (unit half; boot-integration half is TASK-MP-009)
- "A planning request without a target repository hands off to the configured default repository" (config surface; behaviour is TASK-MP-006)

## Files

- Creates: `src/forge/planning/__init__.py`, `src/forge/planning/audit.py`
- Modifies: `src/forge/config/models.py` (new `PlanningConfig`), `src/forge/config/__init__.py` (re-export)
- Tests: `tests/forge/config/test_planning_config.py`, `tests/forge/planning/test_audit.py`

## Acceptance Criteria

- [ ] `PlanningConfig` exists in `src/forge/config/models.py` with `model_config = ConfigDict(extra="forbid")` and exactly these fields: `enabled: bool = False` (intake-is-deliberate), `escalation_approver: str | None = None`, `originator_wait_seconds: int` (ge=0), `escalated_wait_seconds: int` (ge=0), `defer_cap: int = 3` (ge=1), `default_target_repo: str | None = None` (org/name-validated when set, mirroring the wire pattern `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`), `target_repo_paths: dict[str, str] = {}` (org/name -> absolute local working-copy path), `terminal: str = "planned-handoff"`, `frontier_enabled: bool = False`, `frontier_timeout_seconds: int`, `model_resolution: PlanningModelResolution` (sub-model: `model: str | None = None`, `fallbacks: list[str] = []`)
- [ ] `ForgeConfig` gains an optional `planning: PlanningConfig` field with `default_factory` (house shape, models.py:488-510); **`ApprovalConfig` is byte-identical to main** (verified by test comparing against its current field set — no new fields, no edits)
- [ ] `audit_planning_model_resolution(config: PlanningConfig) -> PlanningAuditResult` in `src/forge/planning/audit.py` is a pure function (no I/O, no raising): non-empty `fallbacks` -> violation result naming DF-004; empty -> pass; result carries a human-readable reason string
- [ ] Defaults verified by test: `enabled=False`, `frontier_enabled=False`, `defer_cap=3`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Test Requirements

- Unit tests only, offline: Pydantic validation cases (extra key rejected; invalid
  org/name rejected; negative waits rejected), audit pure-function cases (empty
  fallbacks pass / non-empty fail / missing planning section means Mode P disabled).

## Implementation Notes

- DF-004 (fleet REGISTER): planning model resolution can never silently escalate to
  cloud. The audit must be an **audit, not a Pydantic validator** — a validator
  would brick the whole daemon on violation, contradicting ASSUM-011's "build
  intake unaffected" (DDR-007 soft-fail posture). Boot wiring happens in TASK-MP-009.
- The PO model itself resolves inside specialist-agent; forge's only planning model
  surface is the DF-006 frontier client, but the audit covers the whole
  `model_resolution` block so config drift is caught at the boundary.
- Do NOT touch `ApprovalConfig`, `src/forge/gating/`, or `src/forge/adapters/guardkit/run.py`.
