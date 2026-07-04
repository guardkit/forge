# Feature Spec Summary: Jarvis Notification Bridge

**Stack**: python
**Generated**: 2026-07-03T10:52:30Z
**Revised**: 2026-07-03 (Slack pivot; v1 full-lifecycle scope; assumptions resolved)
**Scenarios**: 31 total (6 smoke, 0 regression)
**Assumptions**: 11 total (10 high / 1 medium confidence)
**Review required**: No — both low-confidence assumptions resolved (see below)

## Scope

The bridge subscribes to the build service's pipeline lifecycle events and its
approval channel, and pushes the build lifecycle to the operator's phone via
**Slack** (operator decision 2026-07-03; replaces the ideation-doc Telegram
default — the operator has no Telegram account). v1 is one-way and covers the
full lifecycle: queued (jarvis-intake builds), running, terminal states
(complete, failed, cancelled) and approval pauses, carrying feature id, build
id and correlation, plus stage, coach score and rationale where the event
provides them (per ASSUM-003 the score is always absent on today's live path). v1.1 adds the reply
path — an approve/reject interactive-button click from the phone, received
over Slack Socket Mode (outbound WebSocket, no public endpoint) and authorized
against the configured operator member id, resolves a paused build back into
the resume flow. Every notification is best-effort: a delivery failure is
logged as a WARNING and never regresses a build (DDR-007), because the build
ledger, not the notification, is authoritative.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 7 |
| Edge cases (@edge-case) | 12 |

(@smoke: 6 · @security: 2 · @regression: 0. Category columns overlap where a
scenario carries more than one tag, e.g. the just-outside approval-window
scenario is both @boundary and @negative.)

## Deferred Items

None — all four proposal groups and all six edge-case-expansion scenarios were
accepted during curation; the 2026-07-03 revision added the queued and running
key examples.

## Version Split (informational)

The feature file interleaves the two ship stages by design; downstream planning
may want to sequence them:

- **v1 (one-way notify, 20 scenarios)**: queued-at-intake, build-started, the
  four terminal/pause notification scenarios, the coach-score boundary
  renders, no-coach-score pause (today's live default — @smoke),
  long-rationale delivery, the suppression, delivery-failure,
  unrecognised-source and malformed-update negatives, duplicate-terminal
  dedup, outcome-preservation, no-replay-on-restart, inert-text rendering,
  concurrent terminal events, throttling, degraded start. **v1 is the
  checkpoint: a toy feature queued from Open WebUI must reach the phone as
  queued → running → terminal before any v1.1 work starts.**
- **v1.1 (reply → resume, 11 scenarios)**: approve-resumes,
  reject-does-not-resume, the approval-window boundary pair,
  unrecognised-decision and unauthorised-responder refusals,
  duplicate-approval dedup, reply-after-ended, approve-one-not-another,
  wrong-correlation refusal, and the approve/window-expiry race.

## Resolved Assumptions (2026-07-03)

- **ASSUM-003 (was low)** — coach score range **resolved from source**:
  0.0–1.0 is forge's pydantic-enforced contract (dispatch/models.py:85,
  gating/models.py:204-208, config/models.py:361-364), but the nats_core wire
  schema is unconstrained and the value is **always None today** (ADR-ARCH-033
  coach-score population gap), so the no-score rendering path is the live
  default. The bridge renders defensively; it never rejects on range.
- **ASSUM-007 (was low)** — surface **resolved by operator decision**: a
  single configured operator **Slack channel** via a Slack bot
  (JARVIS_SLACK_BOT_TOKEN / JARVIS_SLACK_CHANNEL_ID); v1.1 replies over Socket
  Mode (JARVIS_SLACK_APP_TOKEN) authorized against
  JARVIS_SLACK_OPERATOR_USER_ID. jarvis has no prior Slack/Telegram code.

## Source-Verification Notes for /feature-plan (2026-07-03)

- **Workqueue constraint decides the fork**: the PIPELINE stream is workqueue
  retention; a second consumer with overlapping filters is rejected
  (err_code=10100, TASK-FRR-F010Db). The Slack surface must **extend the
  existing in-process `ForgeNotificationsSubscriber`** (jarvis
  forge_notifications.py) or consume a downstream `jarvis.notification.*`
  subject (the FEAT-JARVIS-006 pattern) — it cannot bind pipeline.* itself.
- **Queued notification** fires at jarvis intake publish time
  (tools/dispatch.py queue_build), not from the stream (ASSUM-011); new
  subjects build-paused / build-cancelled can be added to the existing
  consumer's filter (no other consumer binds them — verified live 2026-07-03).
- **build-cancelled has no verified forge producer today** (ASSUM-010): the
  scenario can be unit-validated but not live-validated until forge emits it.
  Operator-facing consequence: a v1.1 reject or max-wait breach transitions
  the build to CANCELLED in SQLite, but until forge emits build-cancelled the
  phone gets no terminal notification for it — the pause notification is the
  last signal. /feature-plan should either wire the emit or accept this
  explicitly.
- **v1.1 forge-side gap**: no production wiring instantiates
  `ApprovalSubscriber` (no call sites for `mark_resume_pending`); the reply
  path needs that wiring (or its absence confirmed as deliberate) before
  Slack replies can resume anything.
- **NATS prerequisite fixed 2026-07-03**: ships-computer-nats was
  crash-looping (missing FORGE/FLEET_MEMORY/GUARDKIT password env vars + stale
  April image entrypoint clobbering $JS subjects); .env repaired, image
  rebuilt, broker healthy; PIPELINE/AGENTS/JARVIS streams provisioned and
  verified live as the `forge` user.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Jarvis Notification Bridge" \
      --context features/jarvis-notification-bridge/jarvis-notification-bridge_summary.md
