---
id: TASK-SAD-012
title: "Contract & seam tests for the dispatch boundary"
task_type: testing
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 5
implementation_mode: task-work
complexity: 5
dependencies: [TASK-SAD-010]
tags: [testing, contract-tests, seam-tests, integration-contracts]
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Contract & seam tests for the dispatch boundary

## Description

Add contract and seam tests that verify the §4 Integration Contracts
declared in `IMPLEMENTATION-GUIDE.md` hold across the dispatch boundary.
These tests complement (not replace) the per-task seam tests — they
exercise contracts spanning multiple tasks and the
domain-vs-transport seam between `forge.dispatch` and
`forge.adapters.nats`.

Mirrors the existing `tests/forge/test_contract_and_seam.py` pattern
established in FEAT-FORGE-002 (TASK-NFI-010).

## Coverage

| Contract | Test |
|---|---|
| `CapabilityResolution` schema | round-trip with `retry_of` field across producer/consumer tasks |
| `CorrelationKey` format invariant | 32 lowercase hex, no PII, distinct across concurrent dispatches |
| `DispatchOutcome` sum type | every variant survives `model_dump`/`model_validate`; discriminator round-trips |
| Dispatch-command envelope | subject regex `^agents\.command\.[a-z0-9-]+$`; reply regex `^agents\.result\.[a-z0-9-]+\.[0-9a-f]{32}$` |
| `correlate_outcome()` | idempotency under repeated calls with same args |

## Seam tests

| Seam | What is asserted |
|---|---|
| `dispatch ↔ discovery` | dispatch reads cache snapshot only; never imports or mutates `discovery.cache` internals |
| `dispatch ↔ persistence` | sensitive parameter values never appear in any persisted column |
| `dispatch ↔ NATS adapter` | orchestrator does not import `nats.aio`; adapter is the sole import site |
| `correlation ↔ adapter` | subscribe-before-publish ordering: adapter's subscribe call recorded before adapter's publish call across the orchestrator path |
| `outcome ↔ FEAT-FORGE-004 (downstream)` | `correlate_outcome()` signature and idempotency are stable; gating layer can call it with no coordination |

## Acceptance Criteria

- [ ] `tests/forge/dispatch/test_contract_and_seam.py` created.
- [ ] Each §4 Integration Contract has at least one contract test in
      this module.
- [ ] Each seam in the table above has at least one seam test in this
      module.
- [ ] Subject-format tests use regex assertions with the exact patterns
      declared in `IMPLEMENTATION-GUIDE.md` §4.
- [ ] Import-boundary seam test: a Python AST-level test verifies that
      `forge/dispatch/*.py` does NOT import `nats` (any submodule). Use
      `ast.parse()` + walk imports — do NOT do a string grep.
- [ ] Test (`CorrelationKey` distinctness): 1000 generated keys are
      unique and all match `[0-9a-f]{32}`.
- [ ] Test (`correlate_outcome` idempotency): two consecutive calls with
      the same args produce the same record AND issue exactly one
      database UPDATE.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Implementation Notes

- This task is the equivalent of TASK-NFI-010 in FEAT-FORGE-002 — model
  it on that existing module rather than re-inventing structure.
- The import-boundary seam test is the strongest guarantee that the
  domain/transport split holds over time. New developers tend to add
  imports without thinking; an AST-level test catches it on the next
  CI run.
- Contract tests are NOT a substitute for the per-task seam tests
  declared on consumer tasks (TASK-SAD-002, TASK-SAD-006, TASK-SAD-007,
  TASK-SAD-009, TASK-SAD-010). Both are needed — per-task seam tests
  catch issues at the producer-consumer level; this module's tests
  catch issues across the whole boundary.
