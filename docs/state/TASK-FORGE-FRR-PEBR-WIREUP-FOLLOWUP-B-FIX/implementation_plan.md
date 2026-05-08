# Implementation Plan — TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-B-FIX

**Task:** Fix translator-vs-autobuild_runner state shape contract.
**Complexity:** 6/10 (intensity=standard, full workflow).
**Mandatory checkpoint:** Phase 2.8 (per AC-1).
**Plan generated:** 2026-05-08, against `forge` HEAD `3857acc`.

---

## Phase 1 findings — codebase reality check

The spike (FOLLOWUP-B) diagnosed the symptom: with the FOLLOWUP-A migration
applied, the bridge attaches, the SSE stream opens, 30 `event="values"`
parts arrive with `data_keys=["files","messages","todos"]`, none carry an
`async_tasks` key, and `_extract_state` drops every part.

Phase 1 of this task surfaced one further fact the task description did
not call out:

> **`forge.subagents.autobuild_runner` has no production importers.**
> `grep -rn "from forge.subagents" src/ tests/` returns matches only in
> `tests/forge/test_autobuild_runner*.py` and
> `tests/forge/test_pause_resume_publish.py`. The exported `_update_state`,
> `LifecycleEmitterAdapter`, `AutobuildState`, `StateChannelWriter`, and
> `LIFECYCLE_TO_PIPELINE_EMIT` symbols are imported only by unit tests.

The production graph at `autobuild_runner.py:844` is built via
`create_deep_agent(model="openai:qwen36-workhorse", tools=[],
system_prompt=_AUTOBUILD_RUNNER_SYSTEM_PROMPT, name="autobuild_runner")`.
That graph carries **no** autobuild orchestration logic; it is an LLM
react agent with empty tools whose state schema is
`AgentState[ResponseT]` (deepagents 0.5.3 — `messages` / `todos` /
`files` only).

Net: **no source of `AutobuildState` transitions exists in production.**
The 30 stream parts the spike observed are deepagents framework state
churn (system prompt → `messages`, todo creation → `todos`, file
operations → `files`), not autobuild lifecycle progressions. The
translator is correct that the values projection has no `async_tasks`
key — but the deeper truth is that *no one is writing the lifecycle
state-machine right now.*

This reshapes the option space the task scopes. Below, the options are
rewritten against this finding.

### Reference: deepagents state-schema constraints

`create_deep_agent` (v0.5.3, `deepagents/graph.py:218`) does **not**
expose a `state_schema` parameter — only `context_schema`. The
realised state schema is `AgentState[ResponseT]` (a TypedDict whose
channels are `messages`, `todos`, `files`, plus middleware-injected
fields). The supervisor's parent graph picks up an `async_tasks`
channel when `AsyncSubAgentMiddleware` is composed in (the middleware's
`start_async_task` tool returns `Command(update={"async_tasks": ...})`).
The autobuild_runner subagent — which is the thread the bridge
streams against — is constructed without that middleware, so its
state has no `async_tasks` channel.

---

## Option space (revised)

The original task description scopes three options. Given the finding
above, each option's scope is bigger than the task description
implied. The revised option space:

### Option B1 — autobuild_runner state-shape repair via custom StateGraph

Rebuild the `autobuild_runner` graph **without** `create_deep_agent`,
using `langgraph.graph.StateGraph` directly. Define a state TypedDict
that includes `async_tasks: dict[str, AutobuildState]` as a top-level
channel. Wire deterministic lifecycle nodes that drive transitions
through `_update_state`, calling the existing `StateChannelWriter` to
write the `async_tasks` channel as a real langgraph reducer. The
existing `LifecycleEmitterAdapter` continues to publish at the same
boundary.

**What changes**
- `autobuild_runner.py`: replace `create_deep_agent(...)` with a
  `StateGraph` whose state TypedDict has `async_tasks` as a reducer-
  backed field. The state writer becomes a langgraph reducer instead
  of a side-effect-only `StateChannelWriter`.
- The graph's nodes implement the lifecycle progression (placeholder
  bodies acceptable — the contract is the state-channel write, not
  the work performed by each node). At minimum the nodes need to
  walk `starting → planning_waves → running_wave → completed` so the
  translator sees enough transitions to emit
  `BuildStartedPayload` + `BuildCompletePayload`.
- The `StateChannelWriter` Protocol stays in place but its production
  binding is rewired through a `Command(update={"async_tasks": ...})`
  shape instead of `_NullStateWriter` / a side-effect SQLite mirror.

**Footprint**
- Files modified: `src/forge/subagents/autobuild_runner.py`
  (substantial — re-architect `_build_runner_graph` and the writer
  Protocol).
- Files added: `tests/forge/lifecycle_bridge/test_translation_contract.py`
  gets a new fixture / test class that loads a deepagents-shaped
  `StreamPart` (with `async_tasks` in the values projection) and
  asserts `_extract_state` returns a non-None `_Snapshot`.
- New supporting: a recorded fixture
  `tests/forge/lifecycle_bridge/fixtures/sse_stream_deepagents_runner.jsonl`
  capturing the new shape.
- LOC: ~400-600 (graph rewrite + tests + fixture).

**Risks**
- **R1** — Diverging from `create_deep_agent` removes the LLM tool
  surface (`write_todos`, filesystem tools, etc.). If a future
  autobuild_runner needs LLM reasoning, we need to compose
  `create_agent` (langchain) + middleware manually. *Mitigation:* the
  current production graph has empty tools and no LLM reasoning loop
  anyway — the `_AUTOBUILD_RUNNER_SYSTEM_PROMPT` is informational. We
  are not removing capability that production uses.
- **R2** — A custom-state graph composed under
  `AsyncSubAgentMiddleware` might confuse the middleware's launch
  contract (which expects `messages` to be present so the
  `description` parameter can be threaded as the first user message).
  *Mitigation:* keep `messages` in the state schema (additive,
  not replacement) so the middleware's launch invariant is preserved.
- **R3** — Unit tests that import `LifecycleEmitterAdapter` /
  `_update_state` continue to pass (those are pure helpers); but
  `tests/forge/test_autobuild_runner.py`'s graph-level tests need to
  reflect the new construction path. Likely 1-2 test files revised.

### Option B2 — `Command(update={"async_tasks": ...})` from a tool

Keep `create_deep_agent` for the runner. Add a single custom tool
to the runner's `tools=[]` list that, when invoked, returns
`Command(update={"async_tasks": {...}})` carrying the AutobuildState
snapshot. The runner's system prompt instructs the LLM to call this
tool at every lifecycle transition.

**What changes**
- `autobuild_runner.py`: add a tool function (e.g.
  `report_lifecycle_transition`) that accepts AutobuildState fields
  and returns `Command(update={"async_tasks": {feature_id: state_dict}})`.
- The tool's docstring + the system prompt instruct the LLM when to
  call it.
- The translator-side fix is the same as B1 (additive shape support).

**Footprint**
- Files modified: 1 (`autobuild_runner.py`).
- Files added: as B1 (test + fixture).
- LOC: ~200-300.

**Risks**
- **R1** — LLM-driven lifecycle reporting is non-deterministic.
  Tests cannot reliably assert that the LLM emits the right
  `Command` shape at the right moment unless we either (a) replay a
  recorded model trace or (b) model-mock the response. The
  contract-test ergonomics are worse than B1.
- **R2** — `Command(update=...)` from a tool inside a deepagents
  agent: the merge semantics with `AgentState`'s reducers is not
  documented in deepagents 0.5.3 release notes; needs verification
  via probe.
- **R3** — The translator still has to handle the case where the
  LLM forgets to call the tool. We'd need a translator-side timeout
  or fallback, re-introducing the fragility that Option A was
  rejected for.

### Option C — pivot to in-process emitter publish (retire SSE-translation)

Retire the bridge's SSE-translation layer. Wire the autobuild logic
(wherever it ends up living) to call `LifecycleEmitterAdapter.on_transition(...)`
directly. The adapter already publishes typed `pipeline.*` envelopes
on the in-process NATS connection (`PipelineLifecycleEmitter`). The
JetStream consumer reads those envelopes from the same broker — no
SSE round-trip needed.

**What changes**
- `src/forge/lifecycle_bridge/`: most of the module retires:
  - `translation.py` → keep `MissingCorrelationIdError` if anything
    else uses it; remove `_extract_state`, `StreamEventTranslator`,
    `_Snapshot`, fixtures.
  - `wireup.py` → `LifecycleBridgeWireup` retains the registry +
    deadline-timer responsibilities, drops the per-build observer
    task and its reconnect / identity-resolution machinery.
  - `bridge.py` → unchanged (it owns the SQLite registry, which is
    still needed for deadline tracking).
  - Tests: remove the entire `test_translation*.py`,
    `test_stream_source.py`, `test_reconnect.py`, fixtures.
- `forge.subagents.autobuild_runner`: same as B1 — wire the
  autobuild logic to `_update_state`, except `state_writer` doesn't
  need to be a langgraph reducer (no SSE consumer needs the values
  projection).
- `src/forge/cli/_serve_production.py` /
  `_serve_deps_lifecycle.py`: rewire so `LifecycleBridgeWireup` no
  longer composes a translator / stream_source / publisher.

**Footprint**
- Files modified: ~10-15.
- Files removed: ~4 (translation.py contents, fixtures, related tests).
- LOC delta: net negative (removes ~1500 LOC of bridge translation
  infrastructure, adds ~300 LOC of `_update_state` wireup at the
  emitter call-site).

**Risks**
- **R1** — Blast radius. The SSE-translation layer is the
  centerpiece of FEAT-PEBR. Retiring it pivots the wave-2
  deliverable onto a different architectural foundation that wasn't
  validated by the FEAT-PEBR runbook so far.
- **R2** — Reconnect / fault tolerance. The bridge's reconnect
  policy and deadline-timer still serve a purpose in the in-process
  emit world (the deadline timer is what publishes `build-failed`
  on a stuck run). We don't lose them — but the wave-3 review
  needs to revalidate that the same fault-tolerance properties
  hold without the SSE feedback loop.
- **R3** — DDR-006 / DDR-007 (the two design records that ground
  this work) explicitly call out the SSE bridge as the production
  observability surface. Pivoting away from it is a DDR-superseding
  change; needs at least an ADR / DDR amendment.

---

## Recommendation

**Option B1, with B2 as a contingency if probing reveals
deepagents 0.5.3 cannot tolerate a custom-state subgraph under
`AsyncSubAgentMiddleware`.** Reasoning:

- **B1 is the smallest-blast-radius path that actually closes AC-3.**
  AC-3 requires a real `pipeline.build-started.FEAT-43DE` envelope
  on the wire from a fresh `queue_build`. B1 wires the autobuild
  logic (placeholder bodies acceptable for this fix) so the
  translator sees real transitions; the existing translator code
  + bridge + publisher path are unchanged.
- **B1 keeps the FEAT-PEBR wave-2 architecture intact.** The SSE
  bridge stays as the production observability surface. DDR-006 /
  DDR-007 contracts hold.
- **B2 has worse test ergonomics** (LLM non-determinism around
  tool-call emission) and an unverified assumption about
  `Command(update=...)` reducer semantics inside a deepagents react
  agent. If a Phase 3 spike disproves B2's reducer assumption, we'd
  fall back to B1 anyway — better to start with B1.
- **C is the right retreat** if Phase 3 reveals B1 hits a deepagents
  internal that the team cannot work around. But starting at C
  would discard ~1500 LOC of bridge infrastructure (the wave-2
  deliverable) without proving that the cheaper fix (B1) fails first.

The `intensity=standard, full_workflow` posture on the task already
budgets the design checkpoint and the architectural review for this
decision. Phase 2.8 is the right place to ratify it.

---

## Implementation phases (assuming Option B1)

### Phase 3 — implementation (~3-4 hours)

1. **Restructure `autobuild_runner.py`**:
   - Define a state TypedDict `AutobuildRunnerState` extending the
     deepagents-compatible shape: `{"messages": ..., "todos": ...,
     "files": ..., "async_tasks": dict[str, dict]}`. The first three
     keys preserve the launch-contract invariant; the fourth is the
     new channel.
   - Define a langgraph reducer for `async_tasks` (last-write-wins
     keyed by `feature_id`).
   - Replace `_build_runner_graph` body: build a `StateGraph` with
     this state TypedDict, add deterministic nodes for the wave-2
     "minimum viable lifecycle" (`starting → planning_waves →
     running_wave → completed`), wire `_update_state` calls in each
     node so `async_tasks` is updated as a real langgraph state
     update.
   - Keep `LifecycleEmitterAdapter` and `_update_state` as helpers;
     they're called from the new node bodies.
   - Update `StateChannelWriter` Protocol so its production
     implementation returns `Command(update={"async_tasks": {...}})`
     at the node boundary (not a side-effect SQLite write — the
     SQLite writer in `_serve_deps_state_channel.py` stays as the
     advisory mirror but is no longer the source of state-channel
     writes).

2. **Update translator contract docstring** (AC-4):
   - `translation.py:1-48` module docstring: name the contract
     source ("AutobuildState snapshots arrive in the langgraph
     values projection under the `async_tasks` key keyed by
     `feature_id`; the autobuild_runner graph's state schema
     includes this channel — see autobuild_runner.py `AutobuildRunnerState`").
   - `_extract_state` docstring at L222-281: clarify that the
     primary lookup path is the production path; the flat-data
     fallback is for legacy fixtures only.

3. **Add deepagents-shaped contract test** (AC-2):
   - New fixture `tests/forge/lifecycle_bridge/fixtures/sse_stream_deepagents_runner.jsonl`:
     a recorded sequence of stream parts whose `data` carries the
     deepagents-runner shape (top-level `messages`/`todos`/`files`
     plus the new `async_tasks` channel).
   - New test class in `test_translation_contract.py` that loads
     this fixture and asserts the same envelope sequence as the
     canonical fixture, validating the shape contract from the
     producer side.
   - Existing `sse_stream_canonical.jsonl` continues to pass — the
     translator handles both shapes additively.

4. **Validation harness**:
   - Run the full `tests/forge/lifecycle_bridge/` test suite plus
     `tests/forge/test_autobuild_runner.py` and
     `tests/forge/test_autobuild_runner_emit_taxonomy.py`.
   - Confirm the existing wireup contract tests
     (`test_wireup.py`, `test_wireup_seam.py`,
     `test_correlation_id_contract_lock.py`,
     `test_recovery*.py`) are untouched (AC-5).

### Phase 4 — testing (~1 hour)

- New contract test from Phase 3.3.
- Regression: full test suite for `tests/forge/lifecycle_bridge/`
  and `tests/forge/test_autobuild_runner*.py`.
- Coverage budget: line ≥ 80%, branch ≥ 75% on the touched files.

### Phase 4.5 — fix loop

Standard 3-attempt fix loop on test failures.

### Phase 5 — code review

Standard. Architect-level review on the StateGraph wiring.

### Phase 5.5 — plan audit

Auto-runs against this plan.

---

## Out of scope (per task description)

- AC-3's runbook revalidation lives in the runbook side, not in this
  fix's diff. AC-3 evidence will be captured in
  `/tmp/runbook-evidence-FOLLOWUP-B-FIX/` in a separate runbook run
  triggered after this fix lands and the image rebuilds.
- AC-6's runbook polish (Signature B split into cycle-1-rich vs
  cycle-2+-drained) is the runbook's job, not this fix.
- The deadline-timer `pipeline.build-failed.*` publish path is
  already filed as `TASK-FORGE-FRR-PEBR-DEADLINE-TIMER-PUBLISH`.
- AC-5 reverting the `FOLLOWUP-B trace` instrumentation lines: the
  task description says these should be reverted *before* this
  task starts. Confirm at Phase 2.8 whether this revert lives in
  this task's PR or has already happened upstream.

---

## Files touched (estimated, Option B1)

| File | Change | Estimated LOC |
|---|---|---|
| `src/forge/subagents/autobuild_runner.py` | Re-architect graph + writer Protocol | +300 / -100 |
| `src/forge/lifecycle_bridge/translation.py` | Docstring update for AC-4 | +20 / -5 |
| `tests/forge/lifecycle_bridge/test_translation_contract.py` | New test class | +120 / 0 |
| `tests/forge/lifecycle_bridge/fixtures/sse_stream_deepagents_runner.jsonl` | New fixture | +30 / 0 |
| `tests/forge/test_autobuild_runner.py` | Update graph-level tests | +50 / -30 |
| `tests/forge/lifecycle_bridge/fixtures/__init__.py` | Export new fixture path | +3 / 0 |

Net: ~+523 / -135 LOC across 6 files.

Plus: contingent revert of the `FOLLOWUP-B trace` instrumentation
in `wireup.py` / `translation.py` — Phase 2.8 to confirm whether
that revert is owned by this task or pre-existing.

---

## Open questions for Phase 2.8 checkpoint

1. **Option B1 vs C** — which architectural direction.
2. **AC-5 instrumentation revert** — owned by this task's PR or
   pre-existing?
3. **Minimum viable lifecycle progression** — the autobuild_runner's
   actual job (running waves of tasks via subagent dispatch) is far
   beyond this fix's scope. For B1, the placeholder lifecycle
   progression (`starting → planning_waves → running_wave →
   completed` with empty bodies) is sufficient to prove the contract;
   the real work happens in a follow-up. Confirm this is acceptable.
