---
id: TASK-REV-PEBR-004
title: Triage three Forge-side gaps surfaced by post-PEBR-WIREUP runbook revalidation (AC-11 catch + bridge silent + langgraph.json orchestrator import)
status: review_complete
created: 2026-05-08T11:00:00Z
updated: 2026-05-08T11:30:00Z
review_completed_at: 2026-05-08T11:30:00Z
spawned_tasks:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A   # apply lifecycle_bridge_registry migration at boot (5-line fix + 1 regression test)
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B   # 90-min spike: trace silent translator (Path 1 placeholder rebind vs Path 2 shape mismatch)
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C   # narrow langgraph.json to autobuild_runner only (Option ii — orchestrator import is unresolvable, no agents/ package exists)
review_decision: accept_all_three
review_results:
  mode: gap-analysis
  depth: standard
  followup_a_diagnosis: confirmed_unambiguous   # Step 3.5b at _serve_production.py:445-468 calls _bridge_coexistence.apply_migration but not lifecycle_bridge_registry.apply
  followup_b_diagnosis: confirmed_ambiguous     # two distinct candidate root causes (placeholder thread_id rebind vs translator shape mismatch); 90-min spike scoped to settle the differential
  followup_c_diagnosis: confirmed_dead_import   # from agents import create_orchestrator — no agents/ package anywhere, no create_orchestrator definition anywhere; template carry-over
  followup_c_chosen_option: ii                  # narrow langgraph.json (vs i fix-the-import or iii split-the-file); rationale documented in spawned task
  parent_ac11_status_updated: true              # TASK-FORGE-FRR-PEBR-WIREUP frontmatter ac_status.AC-11 changed from deferred → blocked; ac_11_blocked_on list added; promotion gate documented
priority: high
task_type: review
review_mode: gap-analysis
review_depth: standard
decision_required: true
parent_feature: FEAT-PEBR
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
companion_to:
  - TASK-REV-PEBR-001
  - TASK-REV-PEBR-002
  - TASK-REV-PEBR-003
discovered_during: jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md walkthrough on GB10 (post-PEBR-WIREUP rebuild)
discovered_at: 2026-05-08T10:30:00Z
forge_head_at_discovery: 1b82236
runbook_results_doc: docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-post-pebr-wireup.md
runbook_evidence_dir: /tmp/jarvis-runbook-evidence/
parent_ac_blocked: TASK-FORGE-FRR-PEBR-WIREUP::AC-11 (deferred runbook revalidation) — NOT met until FOLLOWUP-A lands and the wire shows a real pipeline.build-started.FEAT-*
gaps_in_scope:
  - FOLLOWUP-A   # migration wireup (lifecycle_bridge_registry.apply not invoked at boot)
  - FOLLOWUP-B   # bridge attaches but emits nothing (SSE→envelope translator silent)
  - FOLLOWUP-C   # langgraph.json orchestrator graph import broken (from agents import create_orchestrator)
out_of_scope:
  - jarvis-side gaps (separate review created in jarvis repo)
  - the six runbook gap-folds (tracked separately as TASK-FRR-RUNBOOK-002)
tags:
  - forge-serve
  - lifecycle-bridge
  - production-binding
  - migration-wireup
  - langgraph-deployment
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - regression-protection
estimated_review_minutes: 60
estimated_implementation_minutes_after_review: 90  # FOLLOWUP-A ~5min, FOLLOWUP-B ~60min spike, FOLLOWUP-C ~25min
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Triage three Forge-side gaps surfaced by post-PEBR-WIREUP runbook revalidation

## TL;DR

The 2026-05-08 runbook revalidation of [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11 (rebuilt forge image at `1b82236`) confirmed Phases 0–6 green and the dispatch-chain composer line landed cleanly:

```
forge-serve: dispatch chain composed; _serve_daemon.dispatch_payload rebound to handle_message dispatcher
```

But Phase 7 **failed** with two distinct gaps — and a third gap (langgraph.json orchestrator graph) blocked the operator from running with the canonical sidecar config. All three are Forge-side. This review triages them, scopes the fix shape for each, and decides which spawn standalone implementation tasks.

**No code changes in this task — analysis + decision + spawn implementation tasks only.**

## Why

**Parent AC-11 is the gate.** [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11 (deferred at merge) requires the rebuilt image to publish a real `pipeline.build-started.FEAT-*` envelope and JetStream `ack_floor` to advance past the inbound. Until FOLLOWUP-A lands, every dispatch logs:

```
register_ack_handle raised (no such table: lifecycle_bridge_registry); continuing with legacy ack_callback fallback
```

The legacy `ack_callback` fallback acks-on-dispatch-return — exactly the redelivery-storm closure the bridge was built to replace — so even on a green migration the wire stays empty.

**The parent task must NOT be moved to `tasks/completed/` until at minimum FOLLOWUP-A lands and Phase 7 of the runbook captures a real `pipeline.build-started.FEAT-*` envelope.**

## Background

### Discovery context (operator narrative)

End-to-end runbook walkthrough on GB10 against:

- **forge HEAD** `1b82236` (PEBR-WIREUP — `fix(FEAT-PEBR): compose LifecycleBridgeWireup in bind_production_serve`)
- **Phases 0–6** all green; jarvis chat boot clean (TASK-FRR-001 reconciliation has landed)
- **Phase 7** FAIL — the failure is exactly the kind AC-11 was designed to catch

Two real envelopes published by `queue_build` with correlation_ids `af772739-…` and `7657ed5a-…`; PIPELINE last_seq advanced 19→21. Inbound side worked. Outbound side did not.

### Operator-facing evidence on the rebuilt image

| Symptom | Evidence file | One-line |
|---|---|---|
| Migration drift on every dispatch | forge prod logs | `register_ack_handle raised (no such table: lifecycle_bridge_registry); continuing with legacy ack_callback fallback` |
| Bridge attaches but emits nothing (post hot-fix) | wire-tap pre/post + sidecar logs | `lifecycle_bridge.attach … observer task scheduled`; `SSE GET 200`; **zero outbound** envelopes; deepagents tool loop ran 13+ min against llama-swap |
| Sidecar boot can't load orchestrator graph | sidecar logs | `from agents import create_orchestrator` ⇒ `No module named 'agents'`; operator forced to start sidecar with stripped `langgraph.json` containing only `autobuild_runner` |

Hot-fix during the walkthrough: `docker exec` + persistent volume mount applied `lifecycle_bridge_registry.apply()` manually; bridge then attached cleanly per its own logs — but the silent-bridge gap (FOLLOWUP-B) remained. JetStream `ack_floor` stuck at 11.

### The three gaps in scope

| Gap | One-line | Estimated fix shape |
|---|---|---|
| **FOLLOWUP-A** | `bind_production_serve` Step 3.5b applies `_bridge_coexistence.apply_migration(connection)` but does **not** apply `forge.persistence.migrations.lifecycle_bridge_registry.apply(connection)`. ~5 lines. | Trivial — extend the existing Step 3.5b block with one extra `apply()` call. |
| **FOLLOWUP-B** | After FOLLOWUP-A's hot-fix the bridge attaches and the SSE GET returns 200, but the SSE→envelope translator stays silent. Likely candidates: placeholder `thread_id=pending-FEAT-43DE` not getting re-bound to the real task_id after dispatch; or the `autobuild_runner._update_state` shape doesn't match what the translator expects. | Investigation/spike task — needs forge-internal tracing. |
| **FOLLOWUP-C** | `langgraph.json` declares two graphs (`orchestrator` + `autobuild_runner`); `forge.agent` does `from agents import create_orchestrator` which fails: `No module named 'agents'`. Forced operator to start the sidecar with a stripped config containing only `autobuild_runner`. | Forge-side import resolution — likely a missing `src/forge/agents/__init__.py` re-export or a path-shim issue, or langgraph.json should point at a different module. |

## Acceptance Criteria

- [ ] **AC-1** — **FOLLOWUP-A diagnosis & spawn.** Confirm the missing-migration root cause by reading [_serve_production.py:445-468](../../../src/forge/cli/_serve_production.py#L445) Step 3.5/3.5b and verifying:
  - `_bridge_coexistence.apply_migration(connection)` IS invoked (line 468);
  - `lifecycle_bridge_registry.apply(connection)` is NOT;
  - `BridgeRegistry` (the class that owns `lifecycle_bridge_registry`) instantiates fine but its first SQL touch raises because the table doesn't exist on a fresh DB.

  Spawn implementation task **TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A** with:
  - Single AC: extend `_serve_production.py` Step 3.5b to call `lifecycle_bridge_registry.apply(connection)` after the existing `_bridge_coexistence.apply_migration(connection)` line.
  - Idempotent (`CREATE TABLE IF NOT EXISTS`).
  - One regression test: extend `tests/forge/test_cli_serve_production.py::TestLifecycleBridgeWireupComposition` (or a sibling class) to assert that on a fresh writer connection both tables (`lifecycle_bridge_terminal_publishes` AND `lifecycle_bridge_registry`) exist after `bind_production_serve()` returns. Test must fail on pre-fix HEAD `1b82236` and pass after the fix.
  - Lint + format clean on touched files.

- [ ] **AC-2** — **FOLLOWUP-B scope decision.** Decide which of the two suspected root causes to investigate first. Document the decision and the rationale:

  - **Path 1 — placeholder thread_id rebind.** The hot-fix evidence shows `lifecycle_bridge.attach` happening on `pipeline.build-queued.FEAT-43DE` BEFORE the dispatcher writes the real `task_id` to the `async_tasks` mirror. The wireup's `register_ack_handle` runs before `dispatch_autobuild_async`, so the `IdentityProvider` is polled with `feature_id=FEAT-43DE` and (per the parent task's AC-3 wiring) returns `None` until the dispatcher writes the row, then resolves `(thread_id, run_id)`. **Verify with tracing**: log every IdentityProvider invocation, log the resolved `(thread_id, run_id)`, log every `langgraph_sdk.client.runs.join_stream` call's args.

  - **Path 2 — translator state-update shape mismatch.** The bridge logged `observer task scheduled` and `SSE GET 200`, so the stream IS open. But [TASK-FRR-PEB-003](../../completed/TASK-FRR-PEB-003-sse-to-envelope-translation.md) defines what `StreamPart` shapes the translator recognises. `autobuild_runner._update_state` in [src/forge/subagents/autobuild_runner.py](../../../src/forge/subagents/autobuild_runner.py) emits `Command(update={...})` whose payload shape may not match. **Verify with tracing**: log every event yielded by `runs.join_stream`; cross-reference against the translator's `_translate_event` switch.

  Spawn investigation task **TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B** with:
  - Mode: `direct` (spike) — instrument with logging, capture the wire trace, then decide between fix-here vs. spawn-followup-fix.
  - Time-box: 90 minutes of tracing on a local rebuild before escalating.
  - Out-of-band: capture trace logs to `/tmp/runbook-evidence-FOLLOWUP-B/` for handover if the spike doesn't conclude in one session.
  - Exit criteria: either (a) one targeted fix task spawned with a clear root-cause; or (b) a documented "translator shape contract" PR proposal handed back to FEAT-PEBR.

- [ ] **AC-3** — **FOLLOWUP-C scope decision.** Read [src/forge/agent.py:23](../../../src/forge/agent.py#L23) and [langgraph.json](../../../langgraph.json) and decide between:

  - **Option (i) — fix the import.** `from agents import create_orchestrator` looks like a relative-to-`src/` import. Confirm whether `src/forge/agents/__init__.py` exists and whether the langgraph-runner sidecar's PYTHONPATH includes `src/`. If the agents module ships under `src/forge/agents/` not top-level `agents/`, change the import to `from forge.agents import create_orchestrator`; if `agents/` is a separate top-level package, ensure it's on the deployment path.

  - **Option (ii) — re-point langgraph.json.** If the orchestrator graph is not actually meant to load inside the sidecar at production deploy time (only `autobuild_runner` is the production graph), narrow `langgraph.json` "graphs" to `{"autobuild_runner": "./src/forge/subagents/autobuild_runner.py:graph"}` and move the orchestrator entry to a separate `langgraph-dev.json` or similar.

  - **Option (iii) — split the file.** Keep both graphs in `langgraph.json` for `langgraph dev` (template-style), but ship a production override at deploy time.

  Spawn implementation task **TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C** with the decision recorded, plus:
  - Single AC matching the chosen option.
  - One sidecar boot test: stand up the langgraph-runner with the canonical (un-stripped) config and confirm both graphs load cleanly.
  - Document in the task body why the chosen option was preferred over the others.

- [ ] **AC-4** — **Parent AC-11 status.** Update [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) frontmatter to record:
  - `ac_status.AC-11`: `blocked_on: [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A, TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B]`
  - A note in the body that the parent task remains in `in_review/` (NOT `completed/`) until at minimum FOLLOWUP-A lands and the runbook captures a real `pipeline.build-started.FEAT-*` envelope on the wire (FOLLOWUP-B may follow-up post-hoc if its scope expands).

- [ ] **AC-5** — **Cross-link integrity.** This task references three implementation tasks it spawns. After spawning, update this task's `spawned_tasks` frontmatter list and verify each spawned task's `parent_review` field points back to `TASK-REV-PEBR-004`.

- [ ] **AC-6** — **Out-of-scope confirmation.** Explicitly confirm in the review notes that:
  - Six runbook gap-folds (forge serve `--config` requirement, FORGE_AUTOBUILD_RUNNER_URL + langgraph-runner sidecar, `/home/forge/.forge` host mount, supervisor's `queue_build` markdown-vs-JSON output, §5.1 stale expected-warnings table, §4.2 graphiti probe vs. open-webui :8080 collision) are **out of scope** for this review and are tracked under the runbook gap-fold task (TASK-FRR-RUNBOOK-002).
  - Jarvis-side gaps are **out of scope** for this review and tracked in a separate review in the jarvis repo.

## Inputs / Evidence

- **Runbook results doc**: `docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-post-pebr-wireup.md` — full per-phase RESULTS, two-gap deep dive, six runbook gap-folds.
- **Runbook evidence dir**: `/tmp/jarvis-runbook-evidence/` — wire-tap, forge logs, sidecar logs, stream/consumer info pre/post hot-fix, DDR-019 traces.
- **Command history**: `docs/history/command_history.md` — appended runbook entry pointing to RESULTS + evidence.
- **Parent task**: [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — the work whose AC-11 surfaced these gaps.
- **Migration source**: [src/forge/persistence/migrations/lifecycle_bridge_registry.py](../../../src/forge/persistence/migrations/lifecycle_bridge_registry.py) — the `apply(connection)` function that needs invoking at boot.
- **Boot composer**: [src/forge/cli/_serve_production.py:445-468](../../../src/forge/cli/_serve_production.py#L445) — the existing Step 3.5/3.5b block where FOLLOWUP-A lands.
- **Orchestrator entrypoint**: [src/forge/agent.py:23](../../../src/forge/agent.py#L23) — the `from agents import create_orchestrator` line that fails.
- **Deployment config**: [langgraph.json](../../../langgraph.json) — declares both graphs.

## Decision Checkpoint

After review, present the operator with the three implementation-task scopes and confirm:

1. **[A]ccept all three** — spawn FOLLOWUP-A, -B, -C as described.
2. **[M]odify scopes** — adjust before spawning (e.g. expand FOLLOWUP-B's time-box, fold FOLLOWUP-C into a runbook gap-fold instead).
3. **[D]efer one or more** — leave a gap unfixed if the operator judges priority lower than other in-flight work.
4. **[C]ancel review** — discard if the operator finds the diagnosis flawed.

Default recommendation: **[A]ccept** — FOLLOWUP-A is the AC-11 catch and a 5-line fix; FOLLOWUP-C unblocks the canonical sidecar config; FOLLOWUP-B is the only one with genuine scope ambiguity and is therefore framed as a 90-min spike.

## References

- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix whose AC-11 is blocked by this review's spawned tasks.
- [TASK-REV-PEBR-003](TASK-REV-PEBR-003-analyse-bind-production-serve-wireup-gap.md) — predecessor review that defined the wireup composition shape (Option B helper-factory).
- [TASK-FRR-PEB-002](../../completed/TASK-FRR-PEB-002-bridge-skeleton-and-registry.md) — bridge skeleton + `BridgeRegistry` (the consumer of the missing migration).
- [TASK-FRR-PEB-003](../../completed/TASK-FRR-PEB-003-sse-to-envelope-translation.md) — SSE→envelope translator whose silence FOLLOWUP-B investigates.
- [TASK-FRR-PEB-013](../../completed/TASK-FRR-PEB-013-sidecar-aware-e2e-integration-test.md) — sidecar-aware E2E (audited as bypassing `bind_production_serve`, so does NOT exercise FOLLOWUP-A's migration path).
- `forge.persistence.migrations.lifecycle_bridge_registry.apply(connection)` — the missing `apply()` call.
- `forge.lifecycle_bridge.coexistence.apply_migration(connection)` — the existing call FOLLOWUP-A sits beside.

## Notes for the Reviewer

- **Trust the evidence.** The composer log line confirms PEBR-WIREUP wiring landed. Two operator-facing log lines confirm the migration drift (`no such table: lifecycle_bridge_registry`) and the silent translator (`observer task scheduled` + `SSE GET 200` + zero outbound). FOLLOWUP-A's diagnosis is unambiguous; FOLLOWUP-B's is genuinely between two candidates.
- **FOLLOWUP-A is the AC-11 catch.** This is exactly the failure mode AC-11 was scoped to detect, and it did. Treat as a clean validation of the parent task's deferred-AC discipline — the gap was caught before promotion, not after.
- **FOLLOWUP-C is independent.** It surfaced as collateral during sidecar boot; it does not block the bridge happy-path once FOLLOWUP-A + FOLLOWUP-B land — it blocks the **canonical** sidecar config from loading. The runbook revalidation worked around it with a stripped config; the production deploy cannot.
- **Don't bundle.** Resist the temptation to ship one large fix. FOLLOWUP-A is 5 lines + 1 test; FOLLOWUP-B is a spike; FOLLOWUP-C is an import/deployment-config decision. Three tasks keep the blast radius and the review trail clean.
