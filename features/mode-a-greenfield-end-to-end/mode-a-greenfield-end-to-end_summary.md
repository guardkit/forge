# Feature Spec Summary: Mode A Greenfield End-to-End

**Feature ID**: FEAT-FORGE-007
**Stack**: python
**Generated**: 2026-04-25T00:00:00Z
**Scenarios**: 47 total (4 smoke, 4 regression)
**Assumptions**: 8 total (5 high / 3 medium / 0 low confidence)
**Review required**: No — all assumptions traceable to supplied context files

## Scope

Specifies Forge's capstone Mode A greenfield orchestration: how a single one-line
product brief is driven through the eight-stage chain (product-owner delegation →
architect delegation → architecture → system design → per-feature specification →
per-feature planning → autobuild → pull-request review) under one supervised
build. Covers stage-ordering invariants (no downstream dispatch before
prerequisite approval), forward propagation of stage outputs into the next stage's
context, the asynchronous-subagent dispatch pattern for long-running autobuild
runs (with live wave/task progress and supervisor responsiveness during the
run), the constitutional belt-and-braces rule that pins pull-request review to
mandatory human approval at both the prompt and executor layers, crash recovery
as retry-from-scratch with durable history as the authoritative source against
the advisory live state channel, CLI steering (cancel → synthetic reject; skip
honoured on non-constitutional stages, refused on pull-request review), pause
isolation across simultaneously paused builds, idempotent first-write-wins
discipline on duplicate or simultaneous responses, and end-to-end integrity
invariants — correlation-identifier threading, calibration-priors snapshot
stability for the duration of one build, and per-feature artefact attribution.
Behaviour is described in domain terms; the AsyncSubAgent state channel,
LangGraph interrupt round-trip, NATS approval channel, and SQLite history surface
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 9 |
| Edge cases (@edge-case) | 11 |
| Smoke (@smoke) | 4 |
| Regression (@regression) | 4 |
| Security (@security) | 3 |
| Concurrency (@concurrency) | 3 |
| Data integrity (@data-integrity) | 6 |
| Integration (@integration) | 4 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + concurrency, integration + data-integrity). Group totals do not
sum to 47.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — full chain to PR-awaiting-review, forward propagation between stages, async-subagent dispatch, flag/resume cycle, constitutional PR-review pin, session-outcome chain | 8 |
| B | Boundary Conditions — single-feature catalogue, multi-feature outline (count=2,5), zero-feature catalogue, stage-ordering invariant outline (7 prerequisites), single-wave plan, concurrent status query during running wave | 6 |
| C | Negative Cases — PO hard-stop, no PO specialist (degraded), failed feature-spec halts inner loop, max-score does not auto-approve PR review, skip refused at PR review, autobuild internal hard-stop blocks PR creation, PR-creation failure marks build failed, reject decision is terminal | 8 |
| D | Edge Cases — crash recovery outline (7 stages), durable-history authority on recovery, mid-flight steering as pending directive, cancel during pause, cancel during autobuild, skip on non-constitutional stage, approval routed by build identifier, internal autobuild pause observable via supervisor, per-feature sequencing within a build, duplicate response idempotent | 10 |
| E | Security — belt-and-braces holds against misconfigured prompt, specialist override claim ignored at gating, subprocess worktree-allowlist confinement | 3 |
| F | Concurrency — two concurrent builds with isolated channels and task IDs, supervisor dispatches second build's stage during first build's autobuild | 2 |
| G | Data Integrity — canonical Mode A stage-history ordering, per-feature artefact attribution, notification publish failure does not regress approval | 3 |
| H | Integration Boundaries — minimal greenfield E2E smoke, no-specialists degraded E2E, multi-feature E2E with one PR per feature | 3 |
| I | Expansion — first-wins on simultaneous approvals, correlation threading queue→terminal, calibration-priors snapshot stability, memory-seeding failure does not regress approval | 4 |

## Deferred Items

None.

## Assumptions Summary

| ID | Confidence | Subject | Response |
|----|------------|---------|----------|
| ASSUM-001 | high | Eight Mode A stage classes | confirmed |
| ASSUM-002 | high | Autobuild via AsyncSubAgent + state channel | confirmed |
| ASSUM-003 | high | PR review pinned to mandatory human approval | confirmed |
| ASSUM-004 | high | Durable history authoritative on crash recovery | confirmed |
| ASSUM-005 | high | Constitutional belt-and-braces (prompt + executor) | confirmed |
| ASSUM-006 | medium | Per-feature autobuild sequenced within a build | confirmed |
| ASSUM-007 | medium | Calibration priors snapshotted at build start | confirmed |
| ASSUM-008 | medium | One pull request per feature in catalogue | confirmed |

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The build queue,
  state-machine transitions, durable history, crash recovery (retry-from-scratch),
  and CLI steering surface are referenced here as the substrate every Mode A
  stage rides on. FEAT-FORGE-007 adds no new transitions; it composes them.
- **FEAT-FORGE-002** — NATS Fleet Integration. The live discovery cache,
  fleet-lifecycle subscription, pipeline-event publishing (correlation
  threading), and approval channel are inherited; FEAT-FORGE-007 specifies how
  the supervisor sequences dispatches over them.
- **FEAT-FORGE-003** — Specialist Agent Delegation. The product-owner and
  architect specialist dispatches plus the soft/hard timeout, retry-with-context,
  and degraded-mode behaviour are inherited; FEAT-FORGE-007 specifies their
  ordering and forward propagation.
- **FEAT-FORGE-004** — Confidence-Gated Checkpoint Protocol. The auto-approve /
  flag-for-review / hard-stop / mandatory-human-approval gate modes, the
  build-keyed approval round-trip, idempotent first-wins, max-wait refresh,
  CLI cancel/skip mapping, and the constitutional PR-review rule are inherited;
  FEAT-FORGE-007 specifies how those gates compose across the eight-stage chain.
- **FEAT-FORGE-005** — GuardKit Command Invocation Engine. The subprocess
  contract for /system-arch, /system-design, /feature-spec, /feature-plan,
  autobuild, and /task-* — including context-flag construction and worktree
  confinement — is inherited; FEAT-FORGE-007 specifies the order and inputs.
- **FEAT-FORGE-006** — Infrastructure Coordination. Long-term-memory seeding,
  priors retrieval at build start, test verification, and git/gh PR creation are
  inherited; FEAT-FORGE-007 specifies how their failure modes interact with the
  build's authoritative recorded progress.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Mode A Greenfield End-to-End" \
      --context features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
