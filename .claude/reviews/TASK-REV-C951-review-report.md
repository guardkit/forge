# Review Report: TASK-REV-C951 — Plan: Jarvis Notification Bridge (FEAT-UBS-003)

## Executive Summary

Three independent planning approaches were generated and adversarially judged
(quality + maintainability priority, architecture focus). The winner —
**validation-first in-process Slack sink with correlation-independent fan-out**
(88/100) — reaches the live v1 checkpoint after just **three autobuildable
jarvis tasks + one operator handoff**, with zero forge changes and zero new
NATS consumers before validation. The synthesized plan grafts the runner-up
plans' best elements: plain-pytest test strategy (eliminates the known
pytest-bdd silent-false-green class), Block Kit ~3000-char rationale chunking,
and boot-reconcile request dedup. 16 tasks across 10 waves; v1 (9 tasks)
strictly gates v1.1 (7 tasks); three live-validation tasks are
`operator_handoff`.

## Review Details

- **Mode**: decision · **Depth**: standard
- **Method**: 3-planner judge panel (workflow `ubs003-decision-review`),
  preceded by 4-agent source verification (2026-07-03)
- **Context A**: focus=architecture; tradeoff=quality+maintainability; 4 concerns
- **Graphiti context**: unified-messaging conventions; jarvis pytest-bdd
  missing-step-def-glue false-approval pattern; pipeline-state KV as secondary
  status channel

## Options Evaluation Matrix

| Option | Score | Key strength | Key weakness |
|---|---|---|---|
| 1. Reuse-first in-process fan-out (minimal diff) | 80 | One component owns Slack policy; strongest test strategy | Sink keyed off restart-lossy correlation map — a jarvis restart silently blinds the phone; slower to checkpoint |
| 2. Adapter-decoupled over `jarvis.notification.slack` (FEAT-JARVIS-006) | 76 | Cleanest long-term seam (the docstring-reserved wire promotion); correlation-independent | Slowest to checkpoint; JARVIS-stream limits retention adds a silent age-out failure mode; superset-envelope contract fragility |
| **3. Validation-first in-process sink (WINNER)** | **88** | 3 tasks to live checkpoint; correlation-independent fan-out survives restart; NotificationSink protocol keeps the FEAT-JARVIS-006 promotion as a future plug-in | Checkpoint runs before dedup/throttling (cosmetic double-post possible in toy run) |

## Recommended Approach (synthesized)

**v1**: `SlackNotifier` (new `src/jarvis/infrastructure/slack_notifier.py`)
inside the jarvis supervisor process, fed via a `NotificationSink` protocol
from the existing `ForgeNotificationsSubscriber` — the sink fires **after**
envelope decode + `source_id=='forge'` gate + payload validation but
**independent of the correlation-map lookup** (the phone is per-operator, not
per-session; an LRU/restart loss must not silence it). Queued notification
fires at `queue_build` publish time (ASSUM-011). Bounded asyncio queue, one
worker, plain-text Block Kit (inert rendering per @security), ~3000-char
chunking, 429 Retry-After backoff, WARNING-and-continue everywhere (DDR-007),
no-op sink when Slack config is absent. Filter later widens 4→6 subjects
(+paused/+cancelled — verified unbound; a filter change on the ONE consumer,
never a new consumer). Dedup (ASSUM-006, 300s first-wins) lands post-checkpoint
in the hardening wave. FEAT-JARVIS-006 wire-payload promotion explicitly
deferred; the sink protocol is its future plug point (DDR-recorded).

**v1.1** (hard-gated on the live v1 checkpoint): forge-side, construct
`ApprovalSubscriber` in the serve composition root and inject it as the
already-typed `ApprovalGateDeps.subscriber` (first-ever
`mark_resume_pending` call sites); separately wire `publish_build_cancelled`
onto the three CANCELLED transitions (reject / max-wait / CLI cancel — closes
ASSUM-010). jarvis-side, an AGENTS-stream approval-request capture (limits
retention — overlap legal), Block Kit Approve/Reject buttons carrying
`{request_id, build_id, correlation_id, approval_subject}`, `chat.update` on
defer-refresh, and a Socket Mode reply path whose sole Slack-side gate is
`user.id == JARVIS_SLACK_OPERATOR_USER_ID`, publishing
`ApprovalResponsePayload` into forge's untouched four-step validation chain.
Window/expiry-race enforcement stays exclusively forge-side.

### ASSUM-010 decision (Context A concern 2)

**Split: accept the gap for v1, wire the emit in v1.1 (TASK-JNB-102).** In v1
the only CANCELLED producer is the operator's own CLI cancel (off the
checkpoint path); the bridge unit-validates the cancelled handler from day one.
At v1.1 the calculus inverts — a phone Reject transitions the build to
CANCELLED and the operator must receive terminal confirmation, so the emit is
wired onto reject/max-wait/CLI-cancel and live-validated (phone reject →
phone cancelled notification).

## Task Breakdown (16 tasks, 10 waves)

| ID | V | Wave | Repo | Type | Cx | Deps | Name |
|---|---|---|---|---|---|---|---|
| TASK-JNB-001 | v1 | 1 | jarvis | feature | 5 | — | SlackNotifier + settings + slack-sdk |
| TASK-JNB-002 | v1 | 1 | jarvis | feature | 5 | — | Sink seam in subscriber + queued hook in queue_build |
| TASK-JNB-003 | v1 | 2 | jarvis | feature | 3 | 001, 002 | Lifecycle wiring in build_app_state |
| TASK-JNB-004 | v1 | 3 | jarvis | **operator_handoff** | 3 | 003 | **LIVE v1 CHECKPOINT**: queued→running→terminal on phone |
| TASK-JNB-005 | v1 | 4 | jarvis | feature | 5 | 003 | Pause+cancelled: filter 4→6 + rendering |
| TASK-JNB-006 | v1 | 4 | jarvis | feature | 5 | 003 | Hardening: dedup 300s, throttling, overflow |
| TASK-JNB-007 | v1 | 4 | jarvis | documentation | 2 | 003 | DDR set (sink seam, fan-out rationale, ASSUM-010 interim) |
| TASK-JNB-008 | v1 | 5 | jarvis | testing | 6 | 005, 006 | v1 scenario matrix (plain pytest, 20 scenarios) |
| TASK-JNB-009 | v1 | 6 | jarvis | **operator_handoff** | 3 | 008 | LIVE v1 hardening validation (pause/burst/restart) |
| TASK-JNB-101 | v1.1 | 7 | forge | feature | 7 | 004 | ApprovalSubscriber production wiring |
| TASK-JNB-103 | v1.1 | 7 | jarvis | feature | 6 | 004, 005 | Approval capture + Block Kit buttons |
| TASK-JNB-102 | v1.1 | 8 | forge | feature | 5 | 101 | build-cancelled emit on CANCELLED transitions |
| TASK-JNB-104 | v1.1 | 8 | jarvis | feature | 7 | 103 | Socket Mode reply path + operator-id auth |
| TASK-JNB-105 | v1.1 | 9 | jarvis | testing | 5 | 104 | v1.1 jarvis reply-path tests |
| TASK-JNB-106 | v1.1 | 9 | forge | testing | 5 | 101, 102 | v1.1 forge chain tests (incl. expiry race) |
| TASK-JNB-107 | v1.1 | 10 | both | **operator_handoff** | 3 | 102,104,105,106 | LIVE v1.1 validation: approve/reject from phone |

Full per-task descriptions and acceptance sketches: see the workflow output
(`tasks/wpvjudv6i.output`) — carried verbatim into the task files at
generation time.

## Context A Concern Coverage

1. **No second PIPELINE consumer**: structurally impossible — in-process sink
   inside the one ephemeral consumer; new subjects are a filter change on that
   consumer; queued never touches the stream; the only new consumers (approval
   capture, ApprovalSubscriber) bind the AGENTS stream (limits retention,
   overlap legal). Explicit no-err-10100 ACs + live verification.
2. **ASSUM-010**: split decision (above) — accepted+documented for v1, wired
   and live-validated in v1.1.
3. **ApprovalSubscriber wiring**: one dedicated, deliberately minimal forge
   task (TASK-JNB-101), first v1.1 wave, reusing the validation chain
   byte-for-byte; cancelled emit serialized after it (both touch wrappers.py).
4. **Reply-auth**: layered — Socket Mode operator-id gate (jarvis) + forge's
   untouched four-step chain; the shared `expected_approver`/`decided_by`
   identity value is a named config-alignment AC probed live in TASK-JNB-107.

## Key Risks

- ForgeNotification model widening touches the FEAT-JARVIS-005 cross-adapter
  contract — CLI rendering/schema tests must be updated in the same tasks.
- Correlation-independent fan-out means the phone also notifies for builds not
  queued via jarvis (deliberate; DDR + config toggle as rollback lever).
- Checkpoint runs before dedup — cosmetic double-post possible in the toy run.
- TASK-JNB-101 is highest-uncertainty (await_response has zero production call
  sites); isolated so slippage delays only v1.1.
- Cross-repo sequencing: jarvis tasks seed into jarvis's own tasks/ backlog,
  forge tasks into forge's (autobuild cannot edit sibling repos); waves 7–9
  interleave repos — wave discipline is the coordination mechanism.

## Open Decisions for the Operator

1. Correlation-independent fan-out noise trade (recommended: accept).
2. Plain pytest instead of pytest-bdd .feature glue (recommended: accept —
   eliminates the known silent-false-green class).
3. Pin the shared approver identity string (forge `expected_approver` ==
   jarvis `slack_decided_by`) — v1.1 config, can be set at TASK-JNB-101.
4. Bot `/invite` to #forge-builds still pending (TASK-JNB-004 precondition).

## Decision

_Pending operator checkpoint: [A]ccept / [R]evise / [I]mplement / [C]ancel._
