# Feature Spec Summary: Fleet-memory Deploy Runbook

**Stack**: python
**Generated**: 2026-06-22T14:47:58Z
**Scenarios**: 21 total (4 smoke, 0 regression)
**Assumptions**: 7 total (2 high / 4 medium / 1 low confidence)
**Review required**: Yes

## Scope

FORGE-OL-04, the payoff feature of the output-side-loop exemplar. A hand-authored,
typed two-step runbook (`deploy_compose` then `run_smoke_tests`, both targeting the
fleet-memory deploy directory with `.env.deploy`, no approval gates) is persisted via
the runbook model and executed via the executor through `forge runbook run`. Running
it stands fleet-memory (Postgres + pgvector) up and verifies it with the existing
smoke script whose exit status is the verdict (gates G3-G5). The runbook JSON is saved
as the first harvested exemplar; an end-to-end run proves deploy → smoke → complete
against a disposable compose target, and the same executor then runs the runbook
against the real NAS as the actual stand-up — closing TASK-MEM-008 and ticking
FEAT-MEM-01's NAS acceptance criterion.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 3 |
| Negative cases (@negative) | 4 |
| Edge cases (@edge-case) | 9 |

(2 negative/edge scenarios also carry @security or @concurrency.)

## Implementation Note (gap found during context gathering)

`src/forge/cli/runbook.py` today constructs an **empty `StepTypeRegistry()`** and a
**`_NoOpNATSClient`** ("full wiring will come in integration", lines 196-199). This
feature is where that wiring happens: `forge runbook run` must register the real
`deploy_compose` / `run_smoke_tests` handlers (from the Shell Script Step Handlers
feature, FEAT-SSH) and publish the real lifecycle events. Scenarios A3
("uses the real step handlers") and the event-stream edge case depend on this.

## Deferred Items

None deferred. Note that the real-NAS stand-up scenario ("The executor stands
fleet-memory up on the real NAS") is a one-shot **operational act** that closes
TASK-MEM-008 — it is captured as a behavioural scenario but is not a repeatable
automated test; the automated end-to-end coverage runs against the disposable
`deploy/local` target.

## Open Assumptions (low confidence)

- **ASSUM-002** — runbook identifier `fleet-memory-nas-deploy` is inferred; no naming
  convention is stated in the source. Confirm before authoring the exemplar JSON.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Fleet-memory Deploy Runbook" \
      --context features/fleet-memory-deploy-runbook/fleet-memory-deploy-runbook_summary.md
