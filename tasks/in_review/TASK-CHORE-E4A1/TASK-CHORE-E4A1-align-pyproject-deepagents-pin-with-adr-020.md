---
id: TASK-CHORE-E4A1
title: Align `pyproject.toml` `deepagents` pin with ADR-ARCH-020
status: in_review
created: 2026-04-20T00:00:00Z
updated: 2026-04-20T00:00:00Z
implemented_in: pyproject.toml (commit to follow)
priority: medium
tags: [chore, pin-alignment, pyproject, adr-020, pre-system-design]
complexity: 1
task_type: chore
decision_required: false
parent_review: TASK-SPIKE-C1E9
scoping_source: docs/research/ideas/deepagents-053-verification.md §Pre-flight note
blocks:
  - /system-design
estimated_effort: 10 minutes
test_results:
  status: not_applicable
  coverage: null
  last_run: null
---

# Task: Align `pyproject.toml` `deepagents` pin with ADR-ARCH-020

## Description

During TASK-SPIKE-C1E9's pre-flight, the spike discovered that
`pyproject.toml` pins `deepagents>=0.4.11` while ADR-ARCH-020 declares the
project pin as `>=0.5.3, <0.6`. The spike installed 0.5.3 directly to
unblock its verification work; it did **not** mutate `pyproject.toml`
(scope discipline — `/task-work` warns spike scope creep is a Known Risk).

This chore reconciles that drift. `/system-design` is blocked on it
because the system-design command will `pip install -e .` based on
`pyproject.toml`, and a looser pin means it could run against a
DeepAgents version older than the ASSUM-008/-009 verified 0.5.3.

## Acceptance Criteria

- [ ] `pyproject.toml` `dependencies` line updated: `deepagents>=0.5.3, <0.6`.
- [ ] `pip install -e .` resolves cleanly on a fresh environment.
- [ ] No other pins are modified in the same commit — keep the blast
      radius one line.
- [ ] Commit references `TASK-CHORE-E4A1`, `TASK-SPIKE-C1E9`, and
      `ADR-ARCH-020` in the message body.
- [ ] `/system-design` unblocked with respect to this task (may still
      be blocked on TASK-SPIKE-D2F7).

## Out of scope

- Other provider/LangChain pin updates (separate chore if needed).
- Regenerating a lockfile (repo does not maintain one at this time).
- Any behavioural changes — this is a pin-alignment commit only.

## Source Material

- `docs/research/ideas/deepagents-053-verification.md` —
  §Pre-flight note.
- `docs/architecture/decisions/ADR-ARCH-020-adopt-deepagents-builtins.md`
  — authoritative pin source.
- `pyproject.toml` — the file being corrected.
