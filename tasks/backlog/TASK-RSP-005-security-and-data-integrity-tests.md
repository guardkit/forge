---
id: TASK-RSP-005
title: Security and data-integrity tests
status: backlog
created: 2026-06-21T18:30:00Z
updated: 2026-06-21T18:30:00Z
priority: medium
task_type: testing
parent_review: TASK-REV-RSP-001
parent_feature: FEAT-RSP
feature_slug: runbook-and-step-persistence
wave: 4
implementation_mode: task-work
complexity: 5
estimated_minutes: 75
dependencies:
  - TASK-RSP-004
tags:
  - forge
  - persistence
  - runbook
  - testing
  - security
---

# Security and data-integrity tests

## TL;DR

Cover the Group E (Security) and Group G (Data Integrity) scenarios in a
**dedicated** test file `tests/forge/persistence/test_runbook_security.py`
so it owns its file and runs in parallel with the concurrency suite
(TASK-RSP-006). No production code changes — exercises the repository
built in TASK-RSP-003/004.

## Scope — scenarios covered

**Group E — Security:**

- Adversarial identifiers/target/`step_type` round-trip **verbatim**
  through the store (path-traversal, SQL-injection-shaped, `${jndi}`,
  null-byte, backtick payloads) — parameterised writes make them inert
  data, never executed or sanitised.
- Step `params` carrying a newline, a tab, an embedded quote, and a nested
  mapping round-trip without loss.
- One-million-character captured output survives persist-and-reload with
  identical length and content (no truncation that could hide activity).

**Group G — Data Integrity** (the subset not already proven in
`test_runbook.py`):

- A status update that fails mid-write (store made unavailable) leaves the
  step at its previously committed status (rollback atomicity).
- Re-running the migration against an already-migrated store changes
  nothing (idempotent migration; cross-checks TASK-RSP-002).

> The "shuffled-order reload" and "refused-advance consistency" Group G
> scenarios are owned by `test_runbook.py` (TASK-RSP-003/004) since they
> exercise the core methods directly; this file does not duplicate them.

## Acceptance Criteria

- [ ] Adversarial `<identifier>`/`<target>`/`<step_type>` examples from
      the feature file round-trip byte-identical through create→load.
- [ ] Adversarial `params` (newline/tab/quote/nested mapping) reload
      identical to what was stored.
- [ ] A 1,000,000-char `captured_output` reloads with the same length and
      identical content.
- [ ] A status update refused mid-write (simulated store-unavailable)
      leaves the prior committed status intact on reload.
- [ ] Re-applying `runbook.apply()` to a populated, already-migrated store
      leaves the runbook and all its steps unchanged.
- [ ] All tests live in `tests/forge/persistence/test_runbook_security.py`,
      use the `tmp_path` writer-db fixture pattern, and pass.

## Coach Validation

```bash
python -m pytest tests/forge/persistence/test_runbook_security.py -q
```

## Implementation Notes

- `testing` task_type — no architectural review, no lint gate required by
  the planner rules, but keep imports clean.
- Simulate "store becomes unavailable" deterministically (e.g. close the
  connection / point a fresh writer at a revoked path) rather than relying
  on timing — these must be reproducible unit tests, not flaky ones.
- Build adversarial inputs from the `Examples:` tables in
  `features/runbook-and-step-persistence/runbook-and-step-persistence.feature`
  (Group E) verbatim.
