---
id: TASK-CGCP-003
title: Define request_id derivation helper (deterministic, pure)
task_type: declarative
status: in_review
priority: high
created: 2026-04-25 00:00:00+00:00
updated: 2026-04-25 00:00:00+00:00
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 1
implementation_mode: direct
complexity: 3
dependencies: []
tags:
- gating
- identity
- pure
- declarative
- idempotency
test_results:
  status: pending
  coverage: null
  last_run: null
autobuild_state:
  current_turn: 1
  max_turns: 30
  worktree_path: /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-004
  base_branch: main
  started_at: '2026-04-25T17:43:15.782838'
  last_updated: '2026-04-25T17:49:29.358876'
  turns:
  - turn: 1
    decision: approve
    feedback: null
    timestamp: '2026-04-25T17:43:15.782838'
    player_summary: "Created src/forge/gating/identity.py exposing derive_request_id(*,\
      \ build_id, stage_label, attempt_count) -> str. Implementation is a pure function\
      \ \u2014 keyword-only, only stdlib imports (urllib.parse.quote), no I/O, no\
      \ hidden state. Format is the documented wire contract f'{enc(build_id)}:{enc(stage_label)}:{attempt_count}',\
      \ where enc() runs urllib.parse.quote(safe='') and additionally percent-encodes\
      \ '.' and '~'. The result alphabet is restricted to [A-Za-z0-9_\\-:%], which\
      \ is simultaneously RFC 39"
    player_success: true
    coach_success: true
---

# Task: Define request_id derivation helper (deterministic, pure)

## Description

Implement the deterministic `request_id` derivation that closes risk **R5**
(re-emission diverges from original `request_id`, breaking responder
idempotency on Rich's side).

Per ASSUM-006 (high) and `API-nats-approval-protocol.md §6`, the responder
deduplicates on `request_id` with first-response-wins semantics. Forge
re-emits the request after a crash, and the re-emitted request **must**
carry the same `request_id` for that dedup to hold. Therefore the
derivation must be a pure function of `(build_id, stage_label, attempt_count)`
— no UUIDs, no timestamps, no ambient state.

File to create:

- `src/forge/gating/identity.py` — `derive_request_id(build_id, stage_label, attempt_count) -> str`

The derivation is consumed by:
- TASK-CGCP-006 (publisher emits `request_id` on first send)
- TASK-CGCP-007 (subscriber dedups on `request_id`)
- TASK-CGCP-008 (synthetic CLI injector reuses for the paused stage)
- TASK-CGCP-010 (state-machine integration; reads attempt_count from SQLite for re-emission)

## Acceptance Criteria

- [ ] `derive_request_id(*, build_id: str, stage_label: str, attempt_count: int) -> str` is a pure function — keyword-only, no I/O, no hidden state
- [ ] Same inputs produce identical output across calls (property test)
- [ ] Different `attempt_count` values produce different output (so refresh-on-timeout per `API §7` produces a distinguishable id)
- [ ] Output format is stable, documented, and URL-safe (no characters that would break NATS subject parsing if ever embedded)
- [ ] Negative `attempt_count` raises `ValueError`
- [ ] Empty `build_id` or `stage_label` raises `ValueError`
- [ ] Module imports nothing from `nats_core`, `nats-py`, or `langgraph`
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- A simple stable format such as `f"{build_id}:{stage_label}:{attempt_count}"` satisfies the contract. Document choice in the docstring.
- Stage labels may contain spaces ("Architecture Review"); document and test the chosen separator
- Property-based test recommended via `hypothesis` for the round-trip identity property
