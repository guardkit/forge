# TASK-MP-012 + TASK-JNB-109 — pre-commit review (2026-07-06, evening)

**Scope:** the uncommitted forge working tree (vs HEAD `4e47d47`) holding the
Mode P production wiring + the JNB-109 envelope-subscribe fix, reviewed before
Rich's commit decision. Method: 4 review agents (findings closure, fresh
defects, JNB-109 supersession verification, claims/tracker) + 2 adversarial
refuters on the one serious finding. Companion: the post-merge review this
work answers, `feat-spl-002-post-merge-review-2026-07-06.md`.

## Verdict: COMMIT (with the mechanics note below), then redeploy promptly

**15 of the 16 confirmed findings are genuinely closed with real
verification** — the new pin tests are `inspect.signature`-binding (not
permissive fakes), the `forge-serve-planning` durable is actually bound
(PIPELINE, `pipeline.planning-queued.*`, ack_wait=3600, max_ack_pending=1,
non-overlap preserved), the first production DispatchOrchestrator composition
is real, the approval envelope was executed against frozen nats-core 0.5.0 and
validates, rearm re-emits the persisted request_id verbatim arm-before-post,
escalation/defer run off durable anchors with SQL-level CAS, and the handoff
terminal has a production WorktreeGitRunner + PLANNED_HANDOFF row +
contract-valid notification publish. All 8 mediums closed too. 543 tests green
across the affected suites (run live in this review); both hard guards
zero-diff; no secrets in the untracked files; tracker reconciliation complete
(12 in_review, YAML pointers, ASSUM-004 ratified 1h/4h). The one item not
code-closed is the approver-identity drift — correctly decided (truthful
member IDs), filed as jarvis TASK-JNB-110, and gating the live runs.

## The JNB-109 supersession claim is TRUE — and louder than reported

Verified end-to-end: on committed forge main, ApprovalSubscriber calls
`subscribe(subject, callback)` **positionally** against the daemon's raw
nats.aio client, whose second parameter is `queue: str` — reproduced
TypeError; **the reply-path subscription never existed in production**, while
jarvis publishes a correctly-enveloped ApprovalResponsePayload to exactly that
subject (G2-pinned bytes). A phone approval could never have been received.
Symptom is worse than a silent timeout: the live path **mis-emits
build-failed and acks a PAUSED build**; the rearm path logs ERROR every boot.

**Operational consequence: deployed forge-prod (image `034a2836`) still
carries this defect.** Until this tree is committed AND forge-prod redeployed,
do not run any gated build on it — every gated dispatch would misbehave. The
working-tree fix (shared EnvelopeSubscribeClient at both build-gate
composition sites + Mode P) is correct and pinned by production-signature
tests including an end-to-end resolve through the real
`build_approval_gate_parts`.

## Commit mechanics (matters — read before committing)

- **The staged copy of the TASK-MP-012 task file is a stale `in_progress`
  snapshot with unticked ACs. A staged-only commit would record the wrong
  state and ZERO code.** Commit with a reviewed `git add -A` (the untracked
  set was scanned: no secrets, no stray artifacts; `uv.lock` is a coherent
  intended first commit — nats-core 0.5.0 editable matches the `<0.6` pin).
- jarvis is mid-JNB-110 (its working tree went dirty during this review and
  the committed JNB-105 G2 wire-bytes tests now fail there — expected, the
  identity contract v2 changes the pinned bytes; that session owns fixing
  its own tests before committing). Nothing in the forge commit depends on it.

## Carried findings (none blocking; Mode P ships inert)

- MEDIUM: WorktreeGitRunner `git worktree add --force` re-attach can silently
  advance the handoff branch under an operator's live checkout of the target
  repo (empirically reproduced — phantom staged modification). File a
  follow-up before the first real handoff into a repo a human also works in.
- MEDIUM: a redelivered intake for a still-QUEUED run never re-kicks the
  driver (stalls until next restart; behavior test-pinned — a deliberate
  choice worth revisiting).
- LOWs: approve-vs-checkpoint_cleared crash window re-asks with the answered
  attempt-0 request_id (≤1h stall); waiter re-arm gap on core-NATS responses
  (phase 2 lacks a republish backstop); nak-on-store-failure has no
  redelivery delay (hot loop under persistent store failure); PO
  criterion_breakdown rendered as docs_summary in the handoff brief;
  pass-count cosmetics (buildplan "5286" vs task record "5289+"); stale
  `docs/state/TASK-MP-012/implementation_plan.md` (pre-arch-review artifacts,
  never reconciled).

## Sequence after commit

Commit + push → **redeploy forge-prod** (config+image together; schema_v3
migrates the prod db at first boot — back up `~/forge-prod-state/.forge`) →
jarvis JNB-110 lands → OPS-001 addendum (delete `JARVIS_SLACK_DECIDED_BY`
from both hosts' env files; set GB10 `forge.yaml` `approval.expected_approver`
to Rich's member ID; move to the `JARVIS_SLACK_OPERATOR_USER_IDS` allowlist) →
JNB-107 live round-trip → TASK-MP-010 (also gated on the
`JARVIS_NATS_PASSWORD` rotation per the D659 deploy-record addendum).

*Review run: workflow wf_c4f0ffb5-e3d, 6 agents, 2026-07-06 evening.*
