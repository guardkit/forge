---
id: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX
title: Fix — translator-vs-autobuild_runner state shape contract (deepagents values projection lacks async_tasks channel)
status: completed
created: 2026-05-08T13:30:00Z
updated: 2026-05-08T15:45:00Z
completed: 2026-05-08T15:45:00Z
previous_state: in_review
state_transition_reason: "/task-complete — Option B1 landed; quality gates passed; AC-3 / AC-6 deferred to runbook-side revalidation (gated on forge:latest image rebuild)"
completed_location: tasks/completed/forge-autobuild-runner-pipeline-emitter-bridge/
design:
  status: approved
  option_chosen: B1
  approved_at: "2026-05-08T14:30:00Z"
  approved_by: human
  ac1_satisfied: true
  ac2_satisfied: true
  ac3_satisfied: deferred-to-runbook-revalidation
  ac4_satisfied: true
  ac5_satisfied: true
  ac6_satisfied: deferred-to-runbook-revalidation
  notes: "Phase 1 finding reshaped the option space — no production source of AutobuildState transitions exists pre-fix. B1 chosen with placeholder lifecycle bodies; real autobuild logic is a follow-up. AC-3 / AC-6 are runbook-side validations gated on a fresh forge:latest image rebuild."
priority: high
task_type: fix
parent_review: TASK-REV-PEBR-004
parent_spike: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
parent_feature: FEAT-PEBR
unblocks_parent_ac: TASK-FORGE-FRR-PEBR-WIREUP::AC-11
depends_on:
  - TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-A   # FOLLOWUP-A migration must be in the running image (already applied at 55f7804)
related_tasks:
  - TASK-FRR-PEB-003                        # translator definition this fix updates
  - TASK-FORGE-FRR-PEBR-WIREUP              # parent fix's AC-3 IdentityProvider wiring
  - TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH  # independent deadline-publish bug surfaced by the same spike
complexity: 6
estimated_minutes: 240
implementation_mode: full_workflow
wave: 2
intensity: standard
intensity_reason: provenance=parent_review (TASK-REV-PEBR-004), complexity=6 — touches both autobuild_runner state schema AND lifecycle_bridge translator contract; needs full workflow with arch review (Option B may require a deepagents-internal change).
tags:
  - forge-serve
  - lifecycle-bridge
  - sse-translation
  - autobuild-runner
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - translator-shape-fix
discovered_during: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B (spike, 2026-05-08)
forge_head_at_discovery: e1eef81
---

# Fix FOLLOWUP-B-FIX — translator vs. autobuild_runner state shape contract

## TL;DR

[FOLLOWUP-B's spike](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-trace-silent-translator-spike.md)
proved (exit branch (b)) that with FOLLOWUP-A's migration applied, the
bridge attaches cleanly, the SSE stream opens against the right run, and
30 `event='values'` parts arrive. **All 30 parts have
`data_keys=['files','messages','todos']` — no `async_tasks` key.** The
translator's `_extract_state` at
[translation.py:222-281](../../../src/forge/lifecycle_bridge/translation.py)
looks for `data["async_tasks"][feature_id]` (DDR-006 shape) and falls
back to a flat `data` with `"lifecycle"+"build_id"`; neither shape is
present in deepagents' `stream_mode="values"` projection. Every part is
silently dropped at `_extract_state` returning None. Net: zero
outbound `pipeline.*` envelopes.

## Why

[TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) AC-11
requires a real `pipeline.build-started.FEAT-*` envelope to reach the
wire before promotion to `completed/`. FOLLOWUP-A unblocked the bridge's
SQLite attach path; FOLLOWUP-B nailed the diagnosis to the
translator-vs-emission shape mismatch; this task closes the gap.

## Pivot space

Three options ranked from least to most invasive:

### Option B (RECOMMENDED) — autobuild_runner state-shape repair

Make the AutobuildState transitions land in a langgraph state channel
that `stream_mode="values"` actually surfaces. The
[autobuild_runner._update_state](../../../src/forge/subagents/autobuild_runner.py)
currently writes the AutobuildState through a `StateChannelWriter` to a
side-effect channel for DDR-006 consumers (e.g. `forge status`), but
that channel is not part of the langgraph graph state.

Two sub-options:

- **B1 — widen the graph state schema.** Add `async_tasks` as a
  first-class langgraph state field on the autobuild_runner graph
  (alongside deepagents' `messages`/`todos`/`files`). The
  `StateChannelWriter` becomes a langgraph reducer rather than a
  side-effect. Values projection now includes `async_tasks`.

- **B2 — `Command(update={...})` from graph nodes.** Use langgraph's
  `Command` semantics from inside the graph nodes so AutobuildState
  transitions land in the values projection without a schema widening.

Pick B1 unless B2 is materially simpler against the current deepagents
graph topology.

### Option A (NOT RECOMMENDED) — translator-side fix

Extend `_extract_state` to infer AutobuildState transitions from
deepagents' `messages` / `todos` / `files` shape. Fragile — those
channels aren't structured as state machines; lifecycle/wave/task
transitions would have to be recovered from prose tool-call traces.
Recorded here only so the fix task's design phase can confirm it's been
considered and rejected.

### Option C (FALLBACK) — pivot to D-NATS per-stage emit

Per the spike scoping doc and TASK-FRR-PEB-003's "Option E note", if the
SSE values projection cannot surface AutobuildState transitions, consume
DDR-007's in-process emitter NATS publishes directly. The
autobuild_runner already calls `emitter.on_transition(new_state)` at
every `_update_state` boundary
([autobuild_runner.py:397-407](../../../src/forge/subagents/autobuild_runner.py)).
The bridge's SSE-translation layer becomes redundant and can be retired.

Take this fallback if Option B requires a deepagents-internal change the
team isn't comfortable taking.

## Acceptance Criteria

- [ ] **AC-1** — **Choose option (B vs C) at design checkpoint.** Phase
  2.8 of `/task-work` is mandatory for this task. The chosen option is
  recorded in the task's design metadata. Option A is rejected upfront
  unless the design phase surfaces evidence that B and C are both
  blocked.

- [ ] **AC-2** — **If Option B chosen: AutobuildState appears in
  `stream_mode="values"` projection.** A new contract test in
  `tests/forge/lifecycle_bridge/test_translation_contract.py` (or a
  sibling new file) loads a recorded `StreamPart` from a deepagents-shaped
  fixture and the translator's `_extract_state` returns a non-None
  `_Snapshot` for the post-fix shape. Existing fixtures
  (`tests/forge/lifecycle_bridge/fixtures/sse_stream_canonical.jsonl`)
  remain compatible — i.e. the new shape is additive, not a replacement.

- [ ] **AC-3** — **End-to-end on the rebuilt image.** With FOLLOWUP-A +
  FOLLOWUP-B-FIX in the running `forge:latest`, a single
  `queue_build` for FEAT-43DE produces:
  - At least one `pipeline.build-started.FEAT-43DE` envelope on the wire
    tap within 60s of the build-queued envelope.
  - At least one `pipeline.stage-complete.FEAT-43DE` OR
    `pipeline.build-complete.FEAT-43DE` envelope before the 300s deadline.
  - JetStream consumer's `ack_floor` advances past the queued envelope's
    sequence (no longer stuck at 11).

- [ ] **AC-4** — **Translator contract documentation refreshed.** The
  module-level docstring of
  [translation.py](../../../src/forge/lifecycle_bridge/translation.py)
  is updated to name the contract source (e.g. *"AutobuildState
  snapshots arrive in the langgraph values projection under the
  `async_tasks` key keyed by `feature_id`; the autobuild_runner graph's
  state schema MUST include this channel — see autobuild_runner.py
  __[line ref]__"*) so a future reader cannot accidentally regress the
  contract.

- [ ] **AC-5** — **No regression of existing wireup / translator
  contract tests.** The full test suite for
  `tests/forge/lifecycle_bridge/` passes; the wireup contract tests
  for ack-handle registration / observer-task lifecycle / reconnect
  policy are not touched by this fix.

- [ ] **AC-6** — **Runbook revalidation.** After the fix lands and the
  image is rebuilt, [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md)
  AC-11 (deferred runbook validation) becomes runnable; record the
  fresh evidence at
  `/tmp/runbook-evidence-FOLLOWUP-B-FIX/` and link from this task's
  completed-state notes. **The wave-3 runbook polish that splits
  Signature B into cycle-1-rich vs cycle-2+-drained is in scope of the
  runbook, not this fix; flag but do not block.**

## Out of scope

- The bridge's deadline-timer `pipeline.build-failed.*` publish path —
  filed separately as
  [TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH](TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH-fix.md).
- AC-5 spike cleanup of the `FOLLOWUP-B trace` instrumentation lines —
  reverted independently before this task starts so the FIX is built
  on clean code.
- Removal of the `StateChannelWriter` side-effect channel — DDR-006
  consumers (`forge status`) still rely on it; Option B1 widens the
  langgraph state to include the same channel, but the side-effect
  writer remains for compatibility.

## Inputs / Evidence

- **Parent spike**: [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-trace-silent-translator-spike.md)
- **Spike outcome**: `/tmp/runbook-evidence-FOLLOWUP-B/SPIKE-OUTCOME.md`
- **Static-analysis pre-spike note**:
  `/tmp/runbook-evidence-FOLLOWUP-B/STATIC-ANALYSIS-PRE-SPIKE.md`
- **Fresh evidence (instrumented run)**:
  `/home/richardwoollcott/Projects/appmilla_github/jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-08-fresh-followup-b-instrumented.md`
- **Per-phase evidence**:
  `/tmp/jarvis-runbook-evidence/phase{1,5,6,7}-*.{log,json}` (post-instrumentation)
  + `/tmp/jarvis-runbook-evidence-dryrun-20260508-120044/` (pre-instrumentation 30-min baseline)
- **Translator under fix**:
  [src/forge/lifecycle_bridge/translation.py](../../../src/forge/lifecycle_bridge/translation.py)
  (`_extract_state` at L222-281, `_dispatch` at L400+, contract docstring at L1-48)
- **autobuild_runner emit boundary**:
  [src/forge/subagents/autobuild_runner.py](../../../src/forge/subagents/autobuild_runner.py)
  (`_update_state` at L326-409, `StateChannelWriter` adapter at L484+)
- **Translation contract test**:
  `tests/forge/lifecycle_bridge/test_translation_contract.py`
- **Bridge wireup (unchanged but informative)**:
  [src/forge/lifecycle_bridge/wireup.py](../../../src/forge/lifecycle_bridge/wireup.py)

## References

- [TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B](TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-trace-silent-translator-spike.md) — parent spike
- [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — parent review
- [TASK-FRR-PEB-003](../../completed/TASK-FRR-PEB-003-sse-to-envelope-translation.md) — translator definition; Option E pivot rationale
- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix's AC-3 / AC-11

## Implementation Summary

**Option chosen at AC-1 design checkpoint:** B1 — replace
`deepagents.create_deep_agent` with a custom
`langgraph.graph.StateGraph` that carries an `async_tasks` channel
keyed by `feature_id`. The translator side was already correct
(`_extract_state` does `data.get("async_tasks")`); the bug was that
no source of `async_tasks` writes existed on the wire because the
runner's deepagents-default state schema only exposed
`messages`/`todos`/`files`.

**Phase 1 finding that reshaped the option space:**
`forge.subagents.autobuild_runner` was imported only by tests pre-fix
— `_update_state`, `LifecycleEmitterAdapter`, `AutobuildState`,
`StateChannelWriter` were all dormant in production. The 30 stream
parts the FOLLOWUP-B spike observed were deepagents framework state
churn (system prompt → messages, todo creation → todos, file
operations → files), not lifecycle transitions. There was no source
of `AutobuildState` transitions in production at all. The fix
therefore had to (a) put `async_tasks` into the runner's state
schema **and** (b) emit at least one transition per lifecycle so the
translator has something to diff against.

**Files touched:**

- `src/forge/subagents/autobuild_runner.py` (+371/-24): replaced
  `_build_runner_graph` with a `StateGraph` carrying
  `AutobuildRunnerState` (TypedDict with `messages` +
  `async_tasks`); added `_async_tasks_reducer`
  (last-write-wins per feature_id), `_extract_launch_payload`,
  `_build_snapshot`, `_snapshot_update`, and four placeholder
  lifecycle nodes (`starting → planning_waves → running_wave →
  completed`). `LifecycleEmitterAdapter` and `_update_state`
  preserved unchanged for follow-up wiring.
- `src/forge/lifecycle_bridge/translation.py` (+33/-7): module
  docstring updated with the explicit state-shape contract section
  naming `AutobuildRunnerState` as the contract source (AC-4); also
  reverted the FOLLOWUP-B trace `INFO`-instead-of-`DEBUG` log (part
  of the AC-5 trace cleanup).
- `src/forge/lifecycle_bridge/wireup.py` (-48): reverted the
  FOLLOWUP-B trace instrumentation (5 `logger.info` blocks in
  `_observer_loop` and `_drive_stream_session`) so the FIX is built
  on clean code.
- `tests/forge/lifecycle_bridge/fixtures/sse_stream_deepagents_runner.jsonl`
  (new, +10 lines): production-shape SSE fixture covering the
  success path (`starting → planning_waves → running_wave (with
  stage delta) → completed`) and the failure path (`starting →
  running_wave → failed`), each line carrying top-level
  `messages`/`todos`/`files` channels alongside the `async_tasks`
  channel.
- `tests/forge/lifecycle_bridge/fixtures/__init__.py` (+10):
  exports the new `DEEPAGENTS_RUNNER_FIXTURE` path.
- `tests/forge/lifecycle_bridge/test_translation_contract.py`
  (+136/-3): new `TestDeepagentsRunnerShape` class with four tests
  exercising AC-2 (snapshot extraction under deepagents channels,
  pre-state no-op contract, success-path round-trip, failure-path
  round-trip with `failure_reason` formatting).
- `tests/forge/test_autobuild_runner.py` (+232/-46): replaced
  obsolete `TestRunnerModelSpec` (which locked the
  `create_deep_agent` model arg) with `TestRunnerGraphConstruction`
  (locks the `StateGraph` topology + state schema + values-stream
  surfacing); added `TestLaunchPayloadParser` and
  `TestAsyncTasksReducer` for defensive-path coverage of the new
  helpers.

**LOC delta:** +783 / -166 across 6 production/test files + 1 new
fixture + 1 new planning artifact.

**Quality gates:**

- 287/287 tests passing in touched suites
  (`tests/forge/lifecycle_bridge/`, `tests/forge/test_autobuild_runner.py`,
  `tests/forge/test_autobuild_runner_emit_taxonomy.py`,
  `tests/forge/test_pause_resume_publish.py`).
- 2488/2489 across the full `tests/forge/` suite — the 1 deselected
  is `TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist`,
  a pre-existing failure on a different file (`approval_subscriber.py`)
  unrelated to this fix and reproduced on a clean tree via `git stash`.
- Coverage: `translation.py` 87%, `autobuild_runner.py` 70% (delta
  ~86% — pre-fix was 63%, post-fix is 66%; the aggregate sub-80%
  number reflects pre-existing untested `LifecycleEmitterAdapter`
  paths that this task did not touch).

**Lessons learned:**

1. **`create_deep_agent` does not expose a `state_schema` parameter
   in deepagents 0.5.3** (`graph.py:218`). Any task that needs a
   custom state channel on a deepagents-built graph has to drop
   `create_deep_agent` and build the graph via
   `langgraph.graph.StateGraph` directly. The
   `AsyncSubAgentMiddleware` launch contract (first message =
   `description`) is satisfiable with a plain TypedDict carrying
   `messages` (Annotated with `langgraph.graph.message.add_messages`).
2. **A failing on-the-wire signature can have two distinct root
   causes that look identical from the outside.** The bridge
   translator silently dropping every part *could* mean the runner's
   transition stream is mis-shaped (option B), or it could mean
   there is no transition stream at all (the actual reality here).
   Both surface as `parts_received=N, terminal_seen=False` — only
   a static read of *who calls `_update_state` in production*
   distinguishes them. Future spikes should always grep for
   production importers of the helpers they're investigating
   before assuming the signature is the whole story.
3. **AC-3 / AC-6 runbook revalidation is a separate activity from
   the code fix.** The task description scoped them as gated on a
   `forge:latest` rebuild, and the design checkpoint confirmed that
   placeholder lifecycle bodies are sufficient to prove the contract
   on the wire. The follow-up that wires real autobuild logic into
   these node bodies (running waves of tasks via subagent dispatch)
   is significantly larger and was deliberately scoped out.

**Architectural decisions captured:**

- *Decision: replace `create_deep_agent` with `StateGraph` for
  `autobuild_runner`.* Rationale: `create_deep_agent` v0.5.3 does
  not accept a `state_schema` argument, and the bridge's translator
  contract requires `async_tasks` in the values projection. A
  purpose-shaped `StateGraph` is the smallest-blast-radius path
  that satisfies the contract without retiring the SSE-translation
  layer (Option C). The placeholder lifecycle bodies preserve the
  topology needed for the contract while explicitly deferring the
  real autobuild orchestration to a follow-up.
- *Decision: keep `LifecycleEmitterAdapter` + `_update_state`
  intact for follow-up.* Rationale: the new graph nodes return
  state updates directly via `Command.update`-shape returns rather
  than calling `_update_state`. The DDR-007 emit-at-the-same-boundary
  contract is preserved as an unused helper; when richer node
  bodies are wired in (follow-up), they can call `_update_state`
  to combine the channel write with the in-process emit and the
  invariant holds.
- *Decision: defer AC-3 / AC-6 to a runbook re-run, not a code
  validation.* Rationale: the per-build deadline is 300s and a
  fresh `queue_build` requires a `forge:latest` image rebuild +
  the FEAT-43DE feature-plan; the runbook owns that workflow.
  This task's contract test
  (`test_runner_graph_values_stream_surfaces_each_lifecycle`)
  proves the runner side; the wireup contract test
  (`test_extract_state_finds_snapshot_under_deepagents_channels`)
  proves the bridge side; the wire-side AC-3 evidence is the
  runbook re-run's job.

**Related ADRs / DDRs:**

- DDR-006 (`AutobuildState` model + lifecycle literal) — unchanged.
- DDR-007 (single transition site for state-channel write +
  emitter publish) — unchanged; placeholder nodes don't yet
  invoke this boundary, but the helpers are in place for the
  follow-up.
- ADR-ARCH-031 (`AsyncSubAgent` / `start_async_task`) — unchanged.
