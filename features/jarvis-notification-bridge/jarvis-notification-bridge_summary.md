# Feature Spec Summary: Jarvis Notification Bridge

**Stack**: python
**Generated**: 2026-07-03T10:52:30Z
**Scenarios**: 29 total (4 smoke, 0 regression)
**Assumptions**: 9 total (4 high / 3 medium / 2 low confidence)
**Review required**: Yes

## Scope

The bridge subscribes to the build service's pipeline lifecycle events and its
approval channel, and pushes a curated subset to the operator's phone via
Telegram. v1 is one-way: terminal states (complete, failed, cancelled) and
approval pauses, each carrying feature id, build id, correlation, stage, coach
score and rationale. v1.1 adds the reply path — an approve/reject reply from
the phone resolves a paused build back into the resume flow. Every notification
is best-effort: a delivery failure is logged as a WARNING and never regresses a
build (DDR-007), because the build ledger, not the notification, is
authoritative.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 6 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 7 |
| Edge cases (@edge-case) | 12 |

(@smoke: 4 · @security: 2 · @regression: 0. Category columns overlap where a
scenario carries more than one tag, e.g. the just-outside approval-window
scenario is both @boundary and @negative.)

## Deferred Items

None — all four proposal groups and all six edge-case-expansion scenarios were
accepted during curation.

## Version Split (informational)

The feature file interleaves the two ship stages by design; downstream planning
may want to sequence them:

- **v1 (one-way notify)**: the four terminal/pause notification scenarios, the
  suppression and delivery-failure negatives, duplicate-terminal dedup,
  outcome-preservation, no-replay-on-restart, inert-text rendering, concurrent
  terminal events, throttling, degraded start.
- **v1.1 (reply → resume)**: approve-resumes, reject-does-not-resume, the
  approval-window boundary pair, unrecognised-decision and unauthorised-responder
  refusals, duplicate-approval dedup, reply-after-ended, approve-one-not-another,
  wrong-correlation refusal, and the approve/window-expiry race.

## Open Assumptions (low confidence)

These need human verification before the spec is treated as settled:

- **ASSUM-003** — coach score range assumed 0.0–1.0 inclusive (payload field is
  an unconstrained optional float).
- **ASSUM-007** — Telegram target assumed to be a single configured operator
  chat via a bot (concrete bot/chat binding unspecified).

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Jarvis Notification Bridge" \
      --context features/jarvis-notification-bridge/jarvis-notification-bridge_summary.md
