---
id: TASK-FORGE-FRR-F010L
title: "Retarget autobuild_runner subagent's LLM from Anthropic Claude to llama-swap qwen36-workhorse (local-only ethos)"
status: completed
created: 2026-05-06T00:00:00Z
updated: 2026-05-06T00:00:00Z
completed: 2026-05-06T00:00:00Z
completed_location: tasks/completed/TASK-FORGE-FRR-F010L/
previous_state: in_review
state_transition_reason: "Operator confirmed option (a) — proceeded with renamed target after AC-1 escalation found the originally-named qwen3-code-next is not served on llama-swap; the actual served workhorse model designated for AutoBuild Player/Coach/Forge per RUNBOOK-v3 Phase 5.2 is qwen36-workhorse. AC-1, AC-2, AC-3, AC-4, AC-5, AC-7 satisfied locally; AC-6 deferred to operator runbook re-run (sibling-task scope)."
priority: high
task_type: fix
tags:
  - forge-serve
  - autobuild-runner
  - llm-config
  - llama-swap
  - qwen36-workhorse
  - local-only-ethos
  - adr-arch-001
  - feat-forge-010-followup
  - first-real-run-followup
  - sub-feature
complexity: 2
estimated_minutes: 60
estimated_effort: "30-90 minutes (model spec change + 1-2 unit tests; verify llama-swap serves the model)"
parent_feature: FEAT-FORGE-010
related_tasks:
  - TASK-FW10-002        # autobuild_runner async subagent definition (where the model is wired)
  - TASK-FORGE-FRR-F010J # production composer + sidecar URL threading (prerequisite — verified Addendum 5)
  - TASK-FRR-F010-002    # NB: the sibling jarvis-side TASK-FRR-002 dropped JARVIS_OPENAI_BASE_URL on the same local-only-ethos grounds
correlation_id: e9433033-ea80-449f-885d-b2d1bdfb839e
discovered_on:
  date: 2026-05-04
  machine: GB10 (promaxgb10-41b1)
  context: "Joint live-wire validation rerun late evening — F010J wired the autobuild dispatch path through the sidecar (httpx /threads + /runs HTTP 200), the autobuild_runner graph launched with a real task_id, then stalled inside the sidecar on TypeError 'Could not resolve authentication method' from anthropic._client._validate_headers because the autobuild_runner's first node calls Claude and ANTHROPIC_API_KEY was never set. Operator decision: retarget to llama-swap qwen36-workhorse instead of provisioning an Anthropic key."
context_files:
  - ../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md
  - src/forge/subagents/autobuild_runner.py
  - forge.langgraph.json
  - langgraph.json
  - tasks/completed/TASK-FW10-002-implement-autobuild-runner-async-subagent.md
test_results:
  status: passing
  coverage: "61% file-level on src/forge/subagents/autobuild_runner.py (the modified line is covered by both new tests in TestRunnerModelSpec)"
  last_run: 2026-05-06T00:00:00Z
  scoped_run: "tests/forge/test_autobuild_runner.py — 27 passed in 0.62s (includes 2 new tests added under TestRunnerModelSpec)"
  full_suite_run: "tests/forge/ tests/ — 4289 passed, 3 skipped, 2 failed; both failures are pre-existing on clean main and match AC-7's carved-out exclusion (test_no_raw_clock_primitives_outside_allowlist on approval_subscriber.py:684) plus the unrelated forge:production-validation docker-image entrypoint test"
---

# Task: Retarget `autobuild_runner` subagent's LLM from Anthropic Claude to llama-swap `qwen36-workhorse` (local-only ethos)

## Rename note (2026-05-06)

The task as originally filed named the target `qwen3-code-next`. AC-1's
prerequisite check (`curl http://localhost:9000/v1/models | jq
'.data[].id'`) found that no model with that name is served by the
operator's llama-swap on either `localhost:9000` or
`promaxgb10-41b1:9000`. Reading
`guardkit/docs/research/dgx-spark/RUNBOOK-v3-production-deployment.md`
Phase 5.2 (the canonical production llama-swap config) made it clear
that the model designated for "AutoBuild Player/Coach, Forge, Jarvis
GP, Dataset Factory" is `qwen36-workhorse` (Qwen3.6-35B-A3B Q4_K_XL),
with explicit aliases `autobuild-player`, `coach`, `jarvis-reasoner`,
`forge-orchestrator`, `dataset-factory`, `claude-sonnet-4-6`, and
`claude-opus-4-7`. The operator confirmed (option (a)) that the
local-only-ethos intent of this task is satisfied by retargeting to
`qwen36-workhorse` — the served model whose role in the runbook is
exactly the autobuild_runner's role on the forge side. All references
to `qwen3-code-next` below have been rewritten to `qwen36-workhorse`
without changing the rest of the task's intent. (`/v1/models` only
exposes the canonical id and not all aliases on the deployed
llama-swap, so we use the canonical id `qwen36-workhorse` rather than
an alias.)

## TL;DR

Addendum 5 of the jarvis RESULTS file confirmed F010J wires the
production autobuild dispatch path end-to-end on the wire — forge POSTs
to the langgraph dev sidecar's `/threads` and `/runs` endpoints with
HTTP 200, the `autobuild_runner` graph launches with a real `task_id`,
and the autobuild then stalls inside the sidecar on
`Could not resolve authentication method` because its first node calls
Anthropic Claude and the sidecar has no `ANTHROPIC_API_KEY`. Per
ADR-ARCH-001 (local-first inference) and the operator's explicit
decision, this task retargets the autobuild_runner's model from
Anthropic Claude to llama-swap's `qwen36-workhorse` (a coding-specialist
Qwen variant already served on the local llama-swap). The same
local-only-ethos reasoning that drove the jarvis-side TASK-FRR-002
(`JARVIS_OPENAI_BASE_URL` removal) applies here.

## Symptom (verbatim from RESULTS Addendum 5 — sidecar log)

```
2026-05-04T20:12:23 [error] Run encountered an error in graph:
  TypeError: "Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set.
              Or for one of the `X-Api-Key` or `Authorization` headers to be explicitly omitted"
  graph_id=autobuild_runner
  assistant_id=ae0c7786-6033-5b6f-8e62-284f9135934c
  thread_id=019df49e-d419-79a2-9f9b-307a935b9157
  run_id=019df49e-d41c-71f3-aa42-77297d0954bb
```

The TypeError fires from `anthropic._client._validate_headers` per the
full traceback in `/tmp/runbook-evidence-canonical-final/sidecar.log`.
The autobuild_runner's first node is calling Claude — confirmed at
`src/forge/subagents/autobuild_runner.py:802` (`model="anthropic:claude-haiku-4-5"`).

The chat REPL drained no notification line during the rerun because
the autobuild stalled async (no terminal envelope was published back).
This is **config / sub-feature work, not wiring drift** (RESULTS
Addendum 5, "What the sidecar did with the launched run").

## Why

The operator has standardised on local LLM inference via llama-swap on
the GB10 (per ADR-ARCH-001 and the local-only ethos reinforced during
TASK-FRR-002). Provisioning `ANTHROPIC_API_KEY` for the langgraph dev
sidecar would contradict that ethos and add a cost/latency dependency
on cloud APIs that this deployment was specifically designed to avoid.
Retargeting to a local model is the aligned fix.

The chosen model — **`qwen36-workhorse`** — is a coding-specialist Qwen
variant already served by the operator's llama-swap on `:9000`. The
implementer should validate availability with
`curl http://localhost:9000/v1/models | jq '.data[].id'` before
starting; if `qwen36-workhorse` is not in the returned list, **stop and
escalate** before changing the model spec.

## Investigation needed (mandatory before changing the model spec)

1. **Confirm llama-swap serves `qwen36-workhorse`:**
   ```bash
   curl -s http://localhost:9000/v1/models | jq -r '.data[].id'
   ```
   The exact model name is operator-named. If `qwen36-workhorse` does
   not appear, stop and escalate — the operator-named model must exist
   before the autobuild_runner can be retargeted.
2. **Document the model wiring site** in
   `src/forge/subagents/autobuild_runner.py` — currently a `model=`
   keyword on the `create_deep_agent(...)` call at lines 800-806 inside
   `_build_runner_graph()`. Note the exact line and the full kwarg set
   in §Implementation Notes before the fix lands.
3. **Confirm the provider-prefix convention** the existing wiring uses.
   deepagents `create_deep_agent` takes a `provider:model` string that
   `init_chat_model` resolves; the current value is
   `"anthropic:claude-haiku-4-5"`. For llama-swap (OpenAI-compatible
   API) the spec should be `"openai:qwen36-workhorse"`. Confirm by
   reading the deepagents 0.5.3 `create_deep_agent` signature and
   `langchain.chat_models.init_chat_model`'s provider routing.
4. **Plumb the base URL.** llama-swap is on
   `http://promaxgb10-41b1:9000` (or `http://localhost:9000` from a
   process on the same host). The OpenAI-style binding needs
   `OPENAI_BASE_URL=http://localhost:9000/v1` set in the **sidecar's**
   environment (the langgraph dev process where the autobuild_runner
   graph actually executes), OR a `base_url=` kwarg explicitly threaded
   through. Mirror jarvis's pattern at
   `src/jarvis/infrastructure/lifecycle.py:576-577`
   (`os.environ["OPENAI_BASE_URL"] = f"{config.llama_swap_base_url}/v1"`)
   — same precedent TASK-FRR-002 set when removing
   `JARVIS_OPENAI_BASE_URL`. Decide whether to export the env var at
   sidecar boot (operator runbook handoff) or thread the base_url
   through the factory call.
5. **Cross-reference TASK-FW10-002.** Re-read the autobuild_runner
   subagent design task body and any DDR / ADR that fixes the model
   choice. If FW10-002 or a sibling DDR explicitly fixes the model to
   Claude, this retarget needs an explicit decision-log update or ADR
   amendment, not just a code change. Note findings in §Implementation
   Notes.

## The fix shape (likely simple once investigation completes)

- **Single-line model spec change** in
  `src/forge/subagents/autobuild_runner.py:802`:
  `model="anthropic:claude-haiku-4-5"` →
  `model="openai:qwen36-workhorse"` (exact provider prefix to be
  confirmed by investigation step 3).
- **Sidecar environment** for `langgraph dev`: add
  `OPENAI_BASE_URL=http://localhost:9000/v1` and
  `OPENAI_API_KEY=<any-non-empty-sentinel>` to the process env.
  llama-swap doesn't validate the API key beyond non-emptiness; mirror
  jarvis's pattern of using a sentinel value. Either add the values to
  `forge/.env` (already referenced by `forge.langgraph.json`'s
  `"env": ".env"` field) or document them in the operator runbook
  invocation.
- **Test fixture**: any unit test that asserts the autobuild_runner is
  built with a particular model string should be updated to expect the
  new spec.

## Acceptance Criteria

- [ ] **AC-1 (verify the local model is served):** `curl -s
  http://localhost:9000/v1/models | jq -r '.data[].id'` includes
  `qwen36-workhorse` (or the exact operator-named id) before any code
  changes land. Record the verbatim curl output in §Implementation
  Notes.
- [ ] **AC-2 (document the wiring site):** Record the autobuild_runner
  model wiring file + line and the existing kwarg set (currently
  `src/forge/subagents/autobuild_runner.py:800-806`,
  `create_deep_agent(model="anthropic:claude-haiku-4-5", tools=[],
  system_prompt=..., name=AUTOBUILD_RUNNER_NAME)`) in
  §Implementation Notes before the fix lands.
- [ ] **AC-3 (model spec change):** Replace
  `model="anthropic:claude-haiku-4-5"` with `model="openai:qwen36-workhorse"`
  (or the exact provider-prefix that matches the existing factory
  shape — confirm during investigation step 3) at
  `src/forge/subagents/autobuild_runner.py:802`. No other behavioural
  changes.
- [ ] **AC-4 (sidecar environment handoff):** Document the sidecar
  environment requirements (`OPENAI_BASE_URL=http://localhost:9000/v1`
  and `OPENAI_API_KEY=<sentinel>`) in either (a) `forge/.env` (since
  `forge.langgraph.json` already loads it via `"env": ".env"`), or
  (b) prose in the operator runbook startup section. Pick whichever
  matches the existing convention; cross-link from F010J's operator
  handoff notes.
- [ ] **AC-5 (unit test):** A test under `tests/forge/test_autobuild_runner.py`
  asserts `_build_runner_graph` constructs `create_deep_agent` with
  the new model spec (`openai:qwen36-workhorse`). Use `unittest.mock` to
  patch `create_deep_agent` and assert the `model=` kwarg.
- [ ] **AC-6 (operator acceptance — deferred to runbook re-run):**
  Re-run jarvis runbook §6.2 + §7 with the langgraph dev sidecar
  booted with the new env vars. Expected outcome on the sidecar log:
  **NO** `Could not resolve authentication method` TypeError; the
  autobuild's first LLM node executes against llama-swap and produces
  a real response. (Whether the autobuild then progresses depends on
  Gap F010.M — see sibling task.) Capture the new correlation_id in
  completion notes.
- [ ] **AC-7 (regression — full suite):** Full forge test suite
  (`pytest tests/forge/ tests/`) passes. Pre-existing
  `test_clock_hygiene` failure on `approval_subscriber.py:684`
  remains deselected (carried since F010G; same exclusion F010H/F010J
  AC-7 carried).

## Files Expected to Change

- `src/forge/subagents/autobuild_runner.py` — model spec (1 line at
  L802).
- `tests/forge/test_autobuild_runner.py` (or a new sibling file) —
  add a model-spec assertion test for AC-5.
- Possibly `forge/.env` — add `OPENAI_BASE_URL` + `OPENAI_API_KEY`
  sentinel if AC-4 picks the env-file path. (`forge.langgraph.json`'s
  `"env": ".env"` already covers loading.)
- Possibly an ADR or DDR amendment if investigation step 5 finds a
  binding decision-log entry fixing the model to Claude.

## Implementation Notes

- **Why this is a "fix task" not a "review task" despite touching
  model selection.** The operator has named the model
  (`qwen36-workhorse`); the local-only ethos is established
  (ADR-ARCH-001 + the TASK-FRR-002 precedent on the jarvis side); the
  fix path is a single-line model spec change plus a sidecar-env
  handoff. Promote to review only if investigation step 5 finds a
  binding ADR fixing the model to Claude.
- **Sequence vs F010M.** F010M (autobuild_runner ↔ pipeline-emitter
  bridge for async stall / async failure paths) is a **sibling**
  task being filed in parallel. Both are downstream of F010J's
  wiring win. F010L closes the model-config gap; F010M closes the
  result-bridging gap. Both are needed before the canonical Phase 7
  happy-path renders in the chat REPL. **Land F010L first** — it's
  smaller and can be validated independently against the runbook
  (sidecar log will show the autobuild executing real LLM calls
  instead of failing on auth). F010M can then build on a working
  autobuild base.
- **Operator handoff for the runbook re-run** (deferred AC-6): the
  next rerun's `langgraph dev` invocation must include
  `OPENAI_BASE_URL=http://localhost:9000/v1` and
  `OPENAI_API_KEY=<any-non-empty>` in its environment. These can come
  from `~/Projects/appmilla_github/forge/.env` if AC-4 takes the
  env-file path (already loaded by `forge.langgraph.json`), or be
  inlined into the recipe in `command_history.md`.
- **Reproducer:** boot the sidecar from current `main` (no F010L
  fix). Drive jarvis chat with the standard runbook §6.2 prompt. The
  sidecar log deterministically shows
  `Could not resolve authentication method` within 1-2 seconds of the
  run launch (RESULTS Addendum 5, evidence file `sidecar.log`).
- **Why a single coding-specialist model and not a multi-model split.**
  The autobuild_runner subagent's role is to drive feature builds —
  generating / editing code, running tests, planning fixes — so a
  coding-specialist model (`qwen36-workhorse`) is the aligned choice.
  If a future iteration wants reasoning-vs-implementation split per
  the two-model orchestration pattern documented in
  `.claude/CLAUDE.md`, that's a separate (larger) refactor; F010L is
  scoped to the minimal change that gets the autobuild running.

## References

- **Source-of-truth (forge):**
  - `src/forge/subagents/autobuild_runner.py` — model spec site
    (L800-806 inside `_build_runner_graph()`).
  - `forge.langgraph.json` — sidecar config (created during Addendum
    5 rerun); registers only `autobuild_runner` and loads
    `"env": ".env"`.
  - `langgraph.json` — default langgraph config (registers both
    `orchestrator` and `autobuild_runner`).
- **Source-of-truth (operational):**
  - `../../../../../jarvis/docs/runbooks/RESULTS-FEAT-JARVIS-INTERNAL-001-first-real-run-2026-05-04.md`
    — Addendum 5 ("What the sidecar did with the launched run" +
    "Recommended follow-ups (final)" item 1 → Gap F010.L).
  - `../../../../../jarvis/.env.example` — llama-swap base URL
    convention (`JARVIS_LLAMA_SWAP_BASE_URL=http://promaxgb10-41b1:9000`).
  - `../../../../../jarvis/src/jarvis/infrastructure/lifecycle.py:576-577`
    — `OPENAI_BASE_URL` export precedent (TASK-FRR-002 reference
    pattern).
- **Sibling tasks:**
  - `TASK-FORGE-FRR-F010J` — production composer + sidecar URL
    threading (prerequisite, verified live in Addendum 5).
  - `TASK-FORGE-FRR-F010M` — sibling, autobuild_runner ↔
    pipeline-lifecycle-emitter bridge for async stall / async failure
    paths (filed in parallel with this task).
  - `TASK-FW10-002` — autobuild_runner subagent definition (the file
    F010L's one-line change touches).
  - `TASK-FRR-002` (jarvis-side, completed) — the precedent for
    local-only-ethos retargeting (dropped `JARVIS_OPENAI_BASE_URL`
    on the same grounds).
- **Run that surfaced this:**
  - **correlation_id**: `e9433033-ea80-449f-885d-b2d1bdfb839e`
  - **Date**: 2026-05-04 (late evening, post-F010J)
  - **Machine**: GB10 (`promaxgb10-41b1`)
  - **Image**: `forge:latest` = sha256 `807c65f13c842...`
  - **Sidecar:** `langgraph dev --config forge.langgraph.json --port
    8124 --host 0.0.0.0 --no-browser --allow-blocking --no-reload`
  - **Assistant:** `ae0c7786-6033-5b6f-8e62-284f9135934c`
    (autobuild_runner)

## Completion Notes (2026-05-06)

### AC-1 — verify the local model is served

`curl -s http://localhost:9000/v1/models | jq -r '.data[].id'`
returned (verbatim, both `localhost:9000` and `promaxgb10-41b1:9000`):

```
architect-agent
gemma4-tutor
nomic-embed
qwen-graphiti
qwen36-workhorse
```

The originally-named `qwen3-code-next` is NOT served. The runbook
(`guardkit/docs/research/dgx-spark/RUNBOOK-v3-production-deployment.md`
Phase 5.2) makes clear that `qwen36-workhorse` (Qwen3.6-35B-A3B
Q4_K_XL) is the model designated for "AutoBuild Player/Coach, Forge,
Jarvis GP, Dataset Factory" with explicit aliases `autobuild-player`,
`coach`, `jarvis-reasoner`, `forge-orchestrator`, `dataset-factory`,
`claude-sonnet-4-6`, `claude-opus-4-7`. `/v1/models` only exposes the
canonical id, so we use `qwen36-workhorse` rather than an alias. See
the **Rename note (2026-05-06)** at the top of this file for the
operator-confirmed substitution.

### AC-2 — wiring site documented

File: `src/forge/subagents/autobuild_runner.py`
Function: `_build_runner_graph()` (private; called once at module
import time to build the module-level `graph` constant addressed by
`langgraph.json`).
Original kwargs (lines 800-806 before the change):

```python
return create_deep_agent(
    model="anthropic:claude-haiku-4-5",
    tools=[],
    system_prompt=_AUTOBUILD_RUNNER_SYSTEM_PROMPT,
    name=AUTOBUILD_RUNNER_NAME,
)
```

deepagents 0.5.3's `create_deep_agent(model=...)` accepts a
`provider:model` string that LangChain's `init_chat_model` resolves.
The `openai:` prefix routes through `langchain_openai.ChatOpenAI`
which honours the `OPENAI_BASE_URL` env var — exactly the binding
shape we need for llama-swap's OpenAI-compatible endpoint.

### AC-3 — model spec change

`src/forge/subagents/autobuild_runner.py:802` rewritten to
`model="openai:qwen36-workhorse"` with an inline comment
cross-referencing this task ID, ADR-ARCH-001, and RUNBOOK-v3 Phase
5.2. No other behavioural changes; same `tools=[]`,
`system_prompt=_AUTOBUILD_RUNNER_SYSTEM_PROMPT`,
`name=AUTOBUILD_RUNNER_NAME` kwargs.

### AC-4 — sidecar environment handoff

Took the env-file path: `forge/.env` now declares

```
OPENAI_BASE_URL=http://localhost:9000/v1
OPENAI_API_KEY=not_needed
```

`forge.langgraph.json`'s `"env": ".env"` field already loads this
file into the langgraph dev sidecar's process environment, so no
runbook changes are required for the next re-run. The `not_needed`
sentinel mirrors jarvis's precedent at
`src/jarvis/infrastructure/lifecycle.py:576-577` (TASK-FRR-002). The
existing `OPENAI_API_KEY=not_needed` line was retained; the new line
adds `OPENAI_BASE_URL` and contextual comments cross-linking F010L
and the runbook.

### AC-5 — unit test

Added `TestRunnerModelSpec` class to
`tests/forge/test_autobuild_runner.py` with two tests:

- `test_build_runner_graph_uses_local_workhorse_model_spec` — patches
  `deepagents.create_deep_agent`, calls `_build_runner_graph()`, and
  asserts `kwargs["model"] == "openai:qwen36-workhorse"` plus the
  rest of the kwarg shape (tools, name, system_prompt).
- `test_build_runner_graph_does_not_target_anthropic` — regression
  guard asserting the model spec does not start with `"anthropic:"`,
  so a future revert that re-introduces the
  `Could not resolve authentication method` TypeError will fail at
  test time.

Both pass; the full `tests/forge/test_autobuild_runner.py` suite is
green at 27/27 in 0.62s.

### AC-6 — operator acceptance (deferred to runbook re-run)

Not satisfied locally — requires the operator to boot the langgraph
dev sidecar with `forge.langgraph.json` (which now loads the updated
`forge/.env`) and rerun jarvis runbook §6.2 + §7. Expected outcome
on the sidecar log: NO `Could not resolve authentication method`
TypeError; the autobuild_runner's first LLM node executes against
llama-swap and produces a real response. Whether the autobuild then
progresses past the first node depends on Gap F010.M (the sibling
task that handles autobuild_runner ↔ pipeline-emitter bridging for
async stall / async failure paths). Capture the new
`correlation_id`, `assistant_id`, `thread_id`, `run_id` quad on the
re-run for traceability and append to this section.

### AC-7 — regression (full suite)

`pytest tests/forge/ tests/`: **4289 passed, 3 skipped, 2 failed**.

Both failures are pre-existing on clean `main` (verified by
`git stash`-ing my changes and re-running):

1. `tests/forge/test_contract_and_seam.py::TestClockHygiene::test_no_raw_clock_primitives_outside_allowlist`
   — verbatim violation `src/forge/adapters/nats/approval_subscriber.py:684:
   resumed_at=datetime.now(timezone.utc).isoformat(),`. This is the
   same clock-hygiene exclusion AC-7 explicitly carries forward from
   F010G/F010H/F010J.
2. `tests/integration/test_forge_production_image.py::test_forge_serve_arfs_inside_image`
   — `forge:production-validation` docker image's entrypoint is
   `forge`, but the test calls `docker run forge:production-validation
   python -c …`, which the click-based CLI rejects with
   `No such command 'python'`. Unrelated to F010L; the test's image
   contract itself looks broken at HEAD.

Neither failure was introduced by F010L's changes.

### Files touched

- `src/forge/subagents/autobuild_runner.py` — model spec change at
  L802 + an inline comment block cross-referencing F010L,
  ADR-ARCH-001, and RUNBOOK-v3 Phase 5.2.
- `tests/forge/test_autobuild_runner.py` — added `unittest.mock`
  import, added `_build_runner_graph` to the import block, appended
  the `TestRunnerModelSpec` class with two tests.
- `forge/.env` — added `OPENAI_BASE_URL=http://localhost:9000/v1`
  and contextual comments; preserved the existing
  `OPENAI_API_KEY=not_needed` sentinel.
- This task file — Rename note + Completion Notes + frontmatter
  status/test_results updates.

No ADR or DDR amendment was needed (Investigation step 5): no
binding decision-log entry fixes the autobuild_runner model to
Anthropic. ADR-ARCH-001's local-only ethos is what motivates the
retarget.
