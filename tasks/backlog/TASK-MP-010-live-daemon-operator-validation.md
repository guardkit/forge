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
- **TASK-FWD-004 completed first** (duplicate `forge-autobuild-runner` unit on the
  GB10 can double-dispatch during any live run — RT-12)
- Live db is `~/forge-prod-state/.forge` (NOT `~/forge-state`); schema_v3 migration
  applies on boot — verify a backup exists before the first boot on the new image
- `planning.enabled=true` + planning config (escalation approver, target_repo_paths)
  deployed config-before-image; NATS creds via `~/.config/forge/nats.env`

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
