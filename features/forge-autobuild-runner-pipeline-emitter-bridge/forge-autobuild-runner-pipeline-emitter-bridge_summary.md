# Feature Spec Summary: Wire the autobuild_runner sidecar lifecycle bridge into forge serve

**Stack**: python
**Generated**: 2026-05-06T00:00:00Z
**Anchor task**: TASK-FORGE-FRR-F010M (scoping deliverable; this spec is its Phase 2 output)
**Anchor scoping doc**: `docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md`
**Scenarios**: 26 total (2 smoke, 14 regression)
**Assumptions**: 10 total (1 high / 7 medium / 2 low confidence)
**Review required**: Yes — 2 low-confidence assumptions need verification

## Scope

Closes the F010J → F010M wire gap: every state transition the autobuild reaches
inside the langgraph-runner sidecar (success, async failure, pause, resume,
cancel) must produce a wire-visible `pipeline.*` envelope on JetStream so the
chat REPL can render between-prompt notifications. Specifies the bridge's
contract — its emit catalogue, its deferred-ack semantics, its
restart-recovery shape, its interaction with F010F's sync-raise safety net,
and its interaction with FW10-010's pause/resume design — without committing
to a specific implementation option (the scoping doc recommends C — Streaming
via `runs.join_stream` with `Last-Event-ID` — with E — Hybrid — as named
fallback).

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 4 |
| Negative cases (@negative) | 4 |
| Edge cases (@edge-case) | 17 (7 Group D + 6 expansion + 4 cross-tagged from B/C) |
| Smoke (@smoke) | 2 |
| Regression (@regression) | 14 |

## Deferred Items

None. All four primary groups (A/B/C/D) and the optional edge-case expansion
batch were accepted in Phase 3. Concrete reconnect-schedule numbers (count,
backoff shape) are deferred to `/feature-plan` per ASSUM-003.

## Open Assumptions (low confidence)

- **ASSUM-003** — Bridge reconnect-schedule bound. The scenario "the bridge
  declares a build failed if the sidecar remains unreachable beyond the
  reconnect schedule" passes regardless of the concrete numbers, but
  `/feature-plan` must pick (a) the reconnect-attempt count, (b) the backoff
  shape (linear / exponential / capped exponential), and (c) the
  failure-after-N-attempts threshold. Affects user-visible latency before
  declaring a sidecar-unreachable failure.

- **ASSUM-009** — Cross-process correlation-id enforcement. Conditional on
  the option choice. If `/feature-spec` ratifies Option C (the scoping doc's
  recommendation), this scenario becomes a no-op test that still locks the
  contract should the option choice flip later. If Option D/E is ratified,
  this scenario gates a real cross-process validator that must be
  implemented in F010M's wave-plan.

## Key option-discriminating assumptions

These assumptions are the spec's working hypothesis for `/feature-plan` to
ratify or revise. They follow the scoping doc's §Recommended option (C) and
the §Open questions for `/feature-spec` answer set:

| ASSUM | Sub-option ratified | What it commits |
|---|---|---|
| ASSUM-001 + ASSUM-002 | Q2 sub-option (b) | Replay-via-Last-Event-ID + recovery-sweep terminal floor |
| ASSUM-004 | Q3 sub-option (b) | Deferred-ack at terminal arrival |
| ASSUM-005 | Q4 sub-option (a) | Bridge owns pause+resume; FW10-010's resume site is amended out |
| ASSUM-006 | Q7 sub-option (b) | Bridge synthesises build-cancelled on observed terminal=interrupted |
| ASSUM-007 | Q6 sub-option (a) | `forge status --in-flight` is in-scope for the wave-plan |
| ASSUM-008 | Q8 sub-option (a) | Separate sidecar-aware E2E test; FW10-011 unchanged |

## Files in this feature folder

- `forge-autobuild-runner-pipeline-emitter-bridge.feature` — 26 Gherkin scenarios
- `forge-autobuild-runner-pipeline-emitter-bridge_assumptions.yaml` — 10 resolved assumptions
- `forge-autobuild-runner-pipeline-emitter-bridge_summary.md` — this file

## Integration with /feature-plan

This summary is the canonical `--context` for `/feature-plan`. Suggested invocation:

```bash
/feature-plan "Wire the autobuild_runner sidecar lifecycle bridge into forge serve" \
  --context features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge_summary.md \
  --context features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge.feature \
  --context docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md \
  --context tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md \
  --context tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md \
  --context tasks/completed/TASK-FW10-011-end-to-end-lifecycle-integration-test.md \
  --context tasks/completed/TASK-FORGE-FRR-F010F/TASK-FORGE-FRR-F010F-publish-build-failed-envelope-on-dispatch-error.md \
  --context tasks/completed/TASK-FORGE-FRR-F010J/TASK-FORGE-FRR-F010J-wire-langgraph-runner-sidecar-url-into-async-subagent-registration.md \
  --context docs/design/contracts/API-nats-pipeline-events.md \
  --context docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md \
  --context docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md \
  tasks/in_progress/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010M-scope-autobuild-runner-pipeline-emitter-bridge.md
```

The wave-plan output should land at
`tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/` (per
TASK-FORGE-FRR-F010M AC-6) with each child task carrying
`parent_task: TASK-FORGE-FRR-F010M` in its frontmatter (per AC-7).
