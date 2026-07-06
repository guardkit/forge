---
id: TASK-MP-010
title: Live-daemon operator validation (GB10, jarvis round-trip, kill-NATS recovery)
task_type: operator_handoff
status: backlog
parent_review: TASK-REV-83E4
feature_id: FEAT-3ED2
feature_ref: FEAT-SPL-002
wave: 6
implementation_mode: manual
complexity: 3
estimated_minutes: 45
dependencies: [TASK-MP-009]
tags: [mode-p, operator, live-validation]
---

# TASK-MP-010 — Live-daemon operator validation (operator_handoff — AutoBuild skips)

## Description

The runtime-observation half of the FEAT-SPL-002 validation (build-plan Session 3:
"an injected PlanningQueuedPayload drives PO dispatch -> checkpoint pause ->
approval resume -> branch commit + notification, with SQLite rows at every
transition; kill NATS mid-run and confirm crash recovery resumes at the
checkpoint"). Every scenario above already has an offline AC; this task re-verifies
the composed system against the live GB10 deployment. AutoBuild MUST skip this task
(`task_type: operator_handoff`).

## Preconditions

- TASK-MP-009 merged; forge-prod image rebuilt and deployed on the gate+planning code
- **TASK-MP-012 merged first** (2026-07-06 post-merge review: the merged Mode P
  library had no working production path — boot kwargs TypeError, unbound durable,
  stub dispatch/rearm. TASK-MP-012 implements the wiring; this task would fail at
  step 1 without it.)
- **TASK-FWD-004 completed first** (duplicate `forge-autobuild-runner` unit on the
  GB10 can double-dispatch during any live run — RT-12)
  - *2026-07-06 partial-completion note*: the **unit-disable half is DONE**
    (`forge-autobuild-runner` disabled on the GB10, 2026-07-06). Still open:
    (a) the attended-run override revert (P2-scoped — does NOT gate this task)
    and (b) the `JARVIS_NATS_PASSWORD` rotation, which **DOES still gate this
    task** — MP-010 exercises the jarvis approval round-trip on NATS and must
    not run against a leaked credential.
- Live db is `~/forge-prod-state/.forge` (NOT `~/forge-state`); schema_v3 migration
  applies on boot — verify a backup exists before the first boot on the new image
- `planning.enabled=true` + planning config (escalation approver, target_repo_paths)
  deployed config-before-image; NATS creds via `~/.config/forge/nats.env`
  - *2026-07-06 (TASK-MP-012 / decisions session)*: set the ratified wait
    thresholds EXPLICITLY in the GB10 config — `originator_wait_seconds: 3600`,
    `escalated_wait_seconds: 14400` (ASSUM-004 ratified; defaults now match but
    live configs pin policy values). The composition also needs the NATS URL
    threaded (ServeConfig.nats_url — automatic in production wiring) for the
    dedicated fleet-watcher client, and a `product_owner_specialist` capability
    must be registered in the fleet or every PO dispatch degrades to
    `no_specialist_resolvable`.
- **TASK-JNB-110 (jarvis truthful decided_by) decided 2026-07-06** — jarvis must
  send the clicker's Slack member ID and forge.yaml `approval.expected_approver`
  must be Rich's member ID BEFORE this validation, so the round-trip validates
  the final identity contract once (see AC-3).

## Required operator follow-up

This task is `task_type: operator_handoff` — AutoBuild will not attempt it. The
operator verifies the runtime acceptance criteria below manually, then marks the
task complete via `/task-complete`.

- **AC-1**: An injected `PlanningQueuedPayload` (synthetic_response_injector pattern) on `pipeline.planning-queued.{cid}` produces a `planning_runs` row on the live forge-prod db, and `nats consumer info` shows the `forge-serve-planning` durable with filter `pipeline.planning-queued.*` (also a JNB-107-style pre-flight: confirm the PIPELINE durable set is healthy)
- **AC-2**: Container restart while a run is PAUSED -> exactly one re-issued approval request reaches the phone (no duplicate re-emits); the run remains answerable
- **AC-3**: Identity-pinned approve from the pinned member-id resumes the run; a different responder is refused with only a WARNING in the logs
- **AC-4**: The planning branch `planning/{cid}` + `feature_spec_inputs/{cid}.md` are visible in the target repo, and the Slack notification carries the exact attended `/feature-spec` command
- **AC-5**: Kill NATS mid-pause and restore -> the run is still PAUSED and completes to PLANNED-HANDOFF after approval (SQLite-authoritative recovery)
- **AC-6**: Build intake verified unaffected before and after (queue a no-op/toy build; the pre-dispatch build gate still pauses it addressed to `rich`)
- **AC-7**: DF-004 negative probe: deploy a config with a non-empty planning fallback chain to a THROWAWAY container -> boot log shows the loud audit failure, planning durable absent, build consumer healthy; revert

## Files

- Runbook only (this file). No src changes. Findings recorded in
  `docs/state/TASK-MP-010/` with a dated deploy record (the D659 audit lesson:
  runtime re-pins need committed artifacts).
