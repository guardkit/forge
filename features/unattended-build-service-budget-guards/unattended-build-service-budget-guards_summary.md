# Feature Spec Summary: Unattended build-profile budget guards

**Feature ID**: FEAT-UBS-002
**Stack**: python
**Generated**: 2026-07-03
**Scenarios**: 18 total (5 smoke)
**Assumptions**: 3 total (0 high / 1 medium / 2 low confidence) — all confirmed
**Review required**: No

## Scope

Named budget profiles for the Unattended Build Service. The reserved `attended`
profile keeps all caps unset (ASSUM-010); `unattended` carries conservative caps
(review-cycle, wall-clock, optional token, optional coach-score floor). A pure
evaluator judges a build over/within budget (first-breach-wins); on breach a
running build pauses and raises a high-risk approval — never a silent stop or
continue. `forge queue --profile` validates + echoes the caps.

## Skeleton vs deferred (honest state)

This spec was written **retroactively** against a shipped skeleton, so each
scenario is marked in the `.feature` file:

- **[SKELETON-SATISFIED]** (15): config models, the pure `budget_guard`
  evaluator, CLI `--profile`, and their unit tests exist and pass now.
- **[DEFERRED]** (3): the live behaviours that need
  `tasks/backlog/unattended-build-service/TASK-UBS-002-integration.md`:
  1. A running build actually pausing + escalating on breach (supervisor wiring).
  2. The coach-score floor firing on a real score (blocked on the coach-score
     gap — ADR-ARCH-033).
  3. The per-build `--profile` reaching the daemon (no wire/daemon channel yet).

The single most important behaviour of the feature — *pause and escalate on
breach* — is one of the DEFERRED three. The spec states it so the gap is
visible, not hidden.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 3 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 6 |
| Edge cases (@edge-case) | 5 |

## Deferred Items

3 scenarios tagged [DEFERRED] in-file (not a curation defer — they are specced,
just not yet implemented). Tracked by TASK-UBS-002-integration.

## Open Assumptions (low confidence)

- ASSUM-002 (wall-clock cap = 5400s) — confirmed but low-confidence; revisit
  after the first supervised overnight run shows real build durations.
- ASSUM-003 (coach-score floor unset by default) — confirmed; the illustrative
  0.8 has no production effect until the coach-score feed lands.

## Integration with /feature-plan

    /feature-plan "Unattended build-profile budget guards" \
      --context features/unattended-build-service-budget-guards/unattended-build-service-budget-guards_summary.md
