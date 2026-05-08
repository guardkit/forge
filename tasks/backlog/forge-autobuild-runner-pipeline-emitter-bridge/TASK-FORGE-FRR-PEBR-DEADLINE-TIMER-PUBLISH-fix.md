---
id: TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH
title: Fix — bridge deadline timer doesn't publish pipeline.build-failed.* on stuck builds (silent stream ≠ unreachable stream)
status: backlog
created: 2026-05-08T13:30:00Z
updated: 2026-05-08T13:30:00Z
priority: medium
task_type: fix
discovered_during: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B (spike, 2026-05-08)
parent_review: TASK-REV-PEBR-004
parent_feature: FEAT-PEBR
related_tasks:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX  # primary FOLLOWUP-B fix; this is independent
  - TASK-FORGE-FRR-PEBR-WIREUP                # parent fix
complexity: 4
estimated_minutes: 120
implementation_mode: direct
wave: 3
intensity: light
intensity_reason: complexity=4, scope is one helper in the bridge's deadline path, well-bounded by the existing TerminalPublishLedger contract.
tags:
  - forge-serve
  - lifecycle-bridge
  - deadline-timer
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
forge_head_at_discovery: e1eef81
---

# Fix — bridge deadline timer doesn't publish on stuck builds

## TL;DR

The PEBR per-build deadline (300s, ASSUM-003) is documented as the
canonical fail-stop: when a build doesn't reach a terminal envelope
within the budget, the bridge's deadline timer should publish
`pipeline.build-failed.{feature_id}` so downstream consumers (jarvis,
operators) see the build close out instead of hanging silently. The
[FOLLOWUP-B spike](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-trace-silent-translator-spike.md)'s
30-min instrumented window with 4 unacked feature_ids and the
[wave-2 dry-run](../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-dryrun-wave2.md)'s
30-min window both captured **zero `pipeline.build-failed.*`** envelopes
on the wire — even though the 300s deadline elapsed for every queued
build in the window.

The deadline path appears gated on **stream unreachability** (transient
SSE errors, sidecar down) rather than on **silent-but-reachable streams**
(StopAsyncIteration with no terminal envelope). The bug is independent of
the FOLLOWUP-B-FIX translator-shape repair: even after FOLLOWUP-B-FIX
lands, the deadline path remains the backstop for runs that complete
without producing a recognised terminal transition (e.g. graph crashes
mid-run, deepagents exits cleanly without an explicit lifecycle move).

## Why

Without this fix, builds that run-to-no-terminal stay perpetually un-acked
in JetStream (`ack_floor` stuck), the bridge's SQLite registry row stays
in place, and operators see no terminal closure for the build. The
runbook's expected-FAIL signature B is "operator never gets notified",
which is a worse failure mode than a wrong-but-loud "build-failed"
envelope.

## Acceptance Criteria

- [ ] **AC-1** — **Repro.** With the bridge in production, simulate a
  build that registers, opens a stream, but never produces a terminal
  envelope (e.g. inject a stub `StreamSource` that yields zero events
  and stays alive past 300s). Confirm zero
  `pipeline.build-failed.{feature_id}` envelopes are published. This
  reproduces the bug observed in the FOLLOWUP-B spike.

- [ ] **AC-2** — **Deadline path fires on silent streams.** After the
  fix, the same scenario produces a single
  `pipeline.build-failed.{feature_id}` envelope on the wire within
  ~310s of registration (300s deadline + ~10s emit budget). The
  envelope's `failure_reason` names the silent-deadline cause (e.g.
  `"silent-deadline: no terminal envelope received within 300s"`).

- [ ] **AC-3** — **Idempotent against transient-error redeliveries.**
  When the build is in transient-error reconnect (the original
  TASK-FRR-PEB-008 path), the deadline timer does NOT pre-empt the
  reconnect loop. The deadline only fires when the bridge's registry
  row's `deadline_at` has elapsed AND no terminal has been observed —
  consistent with the existing AC-3 deadline contract on
  [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md).

- [ ] **AC-4** — **TerminalPublishLedger compatibility.** The deadline
  path publishes through the same `TerminalPublishLedger` as the normal
  terminal-arrival path so a deadline-published `build-failed` does not
  later double-publish if a delayed terminal envelope arrives after
  deadline expiry.

- [ ] **AC-5** — **Test coverage.** Unit test in
  `tests/forge/lifecycle_bridge/` simulating the silent-stream deadline
  path and asserting the `build-failed` publish fires exactly once.
  Existing tests for transient-error reconnect / terminal-arrival
  remain green.

## Out of scope

- The translator-vs-emission shape contract (FOLLOWUP-B-FIX) — that's
  the root cause of *why* terminal envelopes don't appear; this task
  fixes the *backstop* that's supposed to fire when terminals don't
  appear *for any reason*.
- Surfacing the deadline-fired build-failed in the operator-facing UI
  / `forge status` output (downstream consumer concern, not bridge
  scope).

## Inputs / Evidence

- **FOLLOWUP-B spike outcome**: `/tmp/runbook-evidence-FOLLOWUP-B/SPIKE-OUTCOME.md`
- **30-min baseline (4 unacked, 0 build-failed)**:
  `/tmp/jarvis-runbook-evidence-dryrun-20260508-120044/phase7-final-consumer-info.json`
- **Bridge wireup**:
  [src/forge/lifecycle_bridge/wireup.py](../../../src/forge/lifecycle_bridge/wireup.py)
  (`_observer_loop` warning at L589-596 references "deadline timer will
  publish build-failed if the sidecar stays unreachable" — that promise
  doesn't hold for silent-but-reachable streams).
- **Bridge deadline path**:
  [src/forge/lifecycle_bridge/bridge.py](../../../src/forge/lifecycle_bridge/bridge.py)
  (`LifecycleBridge`'s deadline handler — current implementation gated on
  unreachability rather than silence; the fix lands here).
- **TerminalPublishLedger** (idempotency surface):
  [src/forge/lifecycle_bridge/](../../../src/forge/lifecycle_bridge/)
  (the at-most-once-publish ledger that prevents double-publish across
  the deadline-vs-terminal race).

## References

- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-trace-silent-translator-spike.md) — surfaced this bug
- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix's AC-3 (deadline contract)
- ASSUM-003 (FEAT-PEBR brief): per-build deadline 300s, bridge is canonical enforcer.
