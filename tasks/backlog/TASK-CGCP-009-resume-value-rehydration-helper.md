---
id: TASK-CGCP-009
title: 'Wire resume_value_as helper at every interrupt() consumer (DDR-002)'
task_type: feature
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-CG44
feature_id: FEAT-FORGE-004
wave: 3
implementation_mode: task-work
complexity: 4
dependencies:
- TASK-CGCP-005
tags:
- langgraph
- adapter
- rehydration
- ddr-002
- contract-test
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Wire resume_value_as helper at every interrupt() consumer (DDR-002)

## Description

Per DDR-002 / `API-nats-approval-protocol.md §4.2`, under `langgraph dev`
or LangGraph server mode the value returned by `interrupt()` arrives as a
`dict`, not a typed Pydantic instance. **Every call-site that reads the
resume value MUST route through `resume_value_as(ApprovalResponsePayload, raw)`**
before attribute access. Direct `.decision` / `.responder` access on a
`dict` is a regression that passes silently in direct-invoke mode (where
typed round-trip already holds).

This task closes risk **R2** (rehydration drift) and provides the
contract test for the Group D `Scenario Outline` "Approval responses are
handled identically whether they arrive typed or as a bare mapping".

Files to create / modify:

- `src/forge/adapters/langgraph/__init__.py` — re-export `resume_value_as`
- `src/forge/adapters/langgraph/resume_value.py` — `resume_value_as[T](typ: type[T], raw) -> T` with `isinstance` short-circuit + `model_validate` fallback
- Wire the helper at every `interrupt()` consumer in `forge.gating.wrappers` (TASK-CGCP-010 will add the call sites; this task delivers the helper they import)
- Add a CI grep guard test: any line containing `interrupt(` followed (within N lines, same function) by attribute access on the resume value without going through `resume_value_as` is a regression

## Acceptance Criteria

- [ ] `resume_value_as[T](typ: type[T], raw: T | dict | Any) -> T` defined as a generic function
- [ ] `isinstance(raw, typ)` short-circuit returns `raw` unchanged (no-op in direct-invoke mode)
- [ ] Otherwise calls `typ.model_validate(raw)` (Pydantic v2) and returns the result
- [ ] Raises a clear `TypeError` for inputs that are neither `typ` nor `dict`-like
- [ ] **Group D `Scenario Outline`** parametrised contract test: same caller code observes a typed `ApprovalResponsePayload` whether the input was a typed instance or a bare mapping of equivalent content
- [ ] Direct-invoke test: typed input is returned unchanged (identity check, not just equality)
- [ ] Server-mode test: `dict` input round-trips through `model_validate` and produces an equivalent typed instance
- [ ] CI grep guard (`tests/integration/test_rehydration_guard.py` or similar): scan `forge/` for `interrupt(...)` followed by `.decision` / `.responder` attribute access in the same function without an intervening `resume_value_as`; fail the test if found
- [ ] Module imports nothing from `nats_core` or `nats-py` (only `langgraph` types if needed)
- [ ] All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- The helper is small (≤ 20 lines) but the test surface (parametrised round-trip + grep guard) is what gives it value
- Per `API §4.2` "The `isinstance` short-circuit makes this a no-op in direct-invoke mode" — there must be no extra validation work in direct mode
- The grep guard is best implemented as a regex over file contents in `forge/`; any false positives can be allow-listed via inline `# noqa: rehydration-guard`
- TDD ordering (per Context B): write the parametrised contract test first
