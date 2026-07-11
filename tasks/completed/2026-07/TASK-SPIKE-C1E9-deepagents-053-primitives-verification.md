---
id: TASK-SPIKE-C1E9
title: DeepAgents 0.5.3 primitives verification (ASSUM-008 permissions + ASSUM-009 interrupt round-trip)
status: completed
created: 2026-04-19T00:00:00Z
updated: 2026-04-20T00:00:00Z
priority: high
tags: [spike, verification, deepagents, langgraph, adr-021, adr-023, pre-system-design]
complexity: 5
task_type: research
decision_required: false
parent_review: TASK-REV-A7D3
scoping_source: .claude/reviews/TASK-REV-A7D3-review-report.md §5
blocks:
  - /system-design
estimated_effort: 1-2 hours
spawned_tasks:
  - TASK-SPIKE-D2F7   # ASSUM-009 server-mode coverage (user-approved deferral, option [A])
  - TASK-CHORE-E4A1   # pyproject.toml pin alignment with ADR-ARCH-020
source_feedback:
  origin: TASK-REV-A7D3 §5 scoping
  received: 2026-04-19
  scope_summary: |
    Verification spike spawned from TASK-REV-A7D3 [I]mplement decision.
    Verifies two load-bearing DeepAgents 0.5.3 / LangGraph primitives
    before /system-design runs. If either fails, spawn a separate ADR
    revision task; do NOT mutate ADR-021 or ADR-023 from this spike.
test_results:
  status: not_applicable
  coverage: null
  last_run: 2026-04-20
reconciled: "2026-07-11 WS3-S8 sweep — TASK-SPIKE-C1E9: ['git: 3a6babc docs/tasks: close TASK-REV-A7D3 §1–§4; spawn TASK-SPIKE-C1E9; archive TASK-DOC-B2A4']"
---

# Task: DeepAgents 0.5.3 primitives verification

## Description

Verify at runtime that two DeepAgents 0.5.3 / LangGraph primitives behave
as their backing ADRs assume. Both assumptions are currently documented
behaviour only, and both are load-bearing for Forge's architecture:

- **ASSUM-008** (backs ADR-ARCH-023 — permissions as constitutional
  safety): the DeepAgents permissions system **refuses writes** outside
  the `allow_write` allowlist at runtime, not merely logs or warns.
- **ASSUM-009** (backs ADR-ARCH-021 — PAUSED via `interrupt()`):
  LangGraph `interrupt()` survives external resume with a **typed
  Pydantic payload** round-trip — the resumed value is a fully-typed
  Pydantic model instance, not a dict or serialised blob.

Full scoping, repro designs, success/failure criteria, and risk register
live in `.claude/reviews/TASK-REV-A7D3-review-report.md` §5. This task
file carries the acceptance criteria; the review report carries the
detailed design.

`/system-design` is **blocked on this task's completion.** Findings
must be committed to `docs/research/ideas/deepagents-053-verification.md`
before `/system-design` runs.

## Origin

Spawned from TASK-REV-A7D3 [I]mplement decision on 2026-04-19. §5 of
that task (the two verification spikes) was scoped but not executed
during the review, so it could live in a discrete commit trail with
bounded scope. TASK-REV-A7D3 §1–§4 (doc paperwork) are complete; this
spike is the last remaining gate before `/system-design`.

## Scope

### In scope

1. Minimal DeepAgents agent repro for permissions refusal
   (`spikes/deepagents-053/permissions_repro.py`).
2. Minimal two-file LangGraph repro for `interrupt()` + external
   `Command(resume=PydanticModel(...))` round-trip
   (`spikes/deepagents-053/interrupt_graph.py`,
   `spikes/deepagents-053/interrupt_resume.py`).
3. Findings file committed:
   `docs/research/ideas/deepagents-053-verification.md`, summarising
   results for both ASSUM-008 and ASSUM-009.
4. On **failure** of either primitive: spawn a separate revision task
   for the affected ADR (`TASK-ADR-REVISE-023-*` or
   `TASK-ADR-REVISE-021-*`). Mark this spike as blocked on that new
   task, and keep `/system-design` blocked until it resolves.

### Out of scope

- Integration with the Forge agent graph. Repros run standalone.
- Testing alternative pin ranges (e.g. 0.6.x). Pin is `>=0.5.3, <0.6`
  per ADR-ARCH-020.
- Improvements or abstractions around either primitive **if it works**.
  Verification is binary — record and move on. The parent task's Known
  Risks explicitly flag this as a scope-creep risk.
- Edits to ADR-021 or ADR-023 directly from this spike, even on
  failure. Revisions happen in a spawned task with its own review.

## Acceptance Criteria

- [ ] `spikes/deepagents-053/permissions_repro.py` exists and has been
      executed at least once. Result (pass/fail) is observed.
- [ ] `spikes/deepagents-053/interrupt_graph.py` and `interrupt_resume.py`
      exist and have been executed together to demonstrate a full
      pause-and-resume cycle. Result (pass/fail) is observed under
      both `langgraph dev` and direct `CompiledStateGraph.invoke`
      execution modes.
- [ ] `docs/research/ideas/deepagents-053-verification.md` is committed
      with one-paragraph findings for each of ASSUM-008 and ASSUM-009.
- [ ] If ASSUM-008 fails: `TASK-ADR-REVISE-023-*` exists in backlog;
      `/system-design` explicitly blocked on it.
- [ ] If ASSUM-009 fails: `TASK-ADR-REVISE-021-*` exists in backlog;
      `/system-design` explicitly blocked on it.
- [ ] Findings file is linked from ADR-021 and ADR-023's References
      section (append-only, one line each).
- [ ] Commit message references `TASK-SPIKE-C1E9` and `TASK-REV-A7D3`
      (and, if applicable, the spawned revision task IDs).

## Method

Follow the execution checklist in
`.claude/reviews/TASK-REV-A7D3-review-report.md` §5 (*"Execution
checklist (for the spawned spike task)"*) verbatim. That checklist
already covers pre-flight, ASSUM-008, ASSUM-009, and close-out. It is
the canonical source of sequence and stop-conditions for this task.

**Do not re-scope the spike from this task file.** The scoping is
frozen in the review report so that any divergence in interpretation
is explicit.

## Source Material

- `.claude/reviews/TASK-REV-A7D3-review-report.md` §5 — full scoping,
  repro designs, risk register, execution checklist.
- `docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md`
  — ASSUM-009 backing ADR.
- `docs/architecture/decisions/ADR-ARCH-023-permissions-constitutional-safety.md`
  — ASSUM-008 backing ADR.
- `docs/architecture/decisions/ADR-ARCH-020-adopt-deepagents-builtins.md`
  — pin rationale (`>=0.5.3, <0.6`).
- DeepAgents 0.5.3 release notes (15 April 2026) — fetch at execution
  time via WebFetch if needed, do not rely on cached derivations.

## Known Risks / Watch-outs

- **Scope creep.** If a primitive works, record a one-paragraph
  finding and stop. Do not propose abstractions.
- **Mode divergence on `interrupt()`.** `langgraph dev` server and
  direct `.invoke` may behave differently. Test both. If they diverge,
  that is itself a finding.
- **Permissions interference with spike filesystem.** Run the
  permissions repro with `allow_write=["/tmp/ok/**"]` explicitly;
  spike directory lives outside permission scope.
- **Findings file omission.** The acceptance criterion explicitly
  requires `docs/research/ideas/deepagents-053-verification.md`. Make
  writing the file the **first** action after observing either result.
- **ADR mutation.** Do NOT edit ADR-021 or ADR-023 directly. On
  failure, spawn a revision task.

## Test Requirements

- Executable repros. Each must run cleanly (pass or fail
  informatively) on the current DeepAgents 0.5.3 + LangGraph pinned
  versions.
- Findings file is prose; verification is peer review by Rich.

## Implementation Notes

Executed 2026-04-20. Versions: `deepagents==0.5.3`, `langgraph==1.1.8`,
`langchain-core==1.3.0`, Python 3.14.2. Driving model: Gemini 2.5 Flash
via `langchain-google-genai` (OpenAI key in `.env` was a `not_needed`
placeholder).

**Pre-flight:** ADR-ARCH-020 pin was **not** honoured by `pyproject.toml`
at spike start (`deepagents>=0.4.11` vs the ADR's `>=0.5.3, <0.6`).
Upgraded DeepAgents to 0.5.3 for the spike without mutating
`pyproject.toml` (scope discipline). Drift captured as TASK-CHORE-E4A1.

**ASSUM-008 (permissions runtime refusal):** PASS. Repro at
`spikes/deepagents-053/permissions_repro.py`. The `_PermissionMiddleware`
intercepts `write_file` in `wrap_tool_call` and returns
`ToolMessage(status="error", content="Error: permission denied for write
on <path>")` before the backend runs. The forbidden file never appeared
on disk. ADR-ARCH-023 stands as written.

**ASSUM-009 (typed `interrupt()` round-trip):**
- PASS for direct `CompiledStateGraph.invoke` with SqliteSaver
  checkpointer, cross-process (two separate Python invocations via the
  shared `interrupt_state.sqlite`). All verdict rows green:
  `isinstance(ApprovalDecision) == True`, nested `Requestor`/`datetime`/
  `UUID` fields intact, graph reached `finalise`.
- **NOT TESTED** for `langgraph dev` server mode. Per user choice at
  close-out (option [A] of the triage), server-mode coverage was
  deferred to TASK-SPIKE-D2F7 to preserve commit isolation rather than
  expanding this spike's blast radius. `/system-design` remains blocked
  on D2F7 for this reason.

**Additional observations recorded in findings:**
1. LangGraph emits a `Deserializing unregistered type` warning on the
   checkpoint/restore path for locally-defined Pydantic payloads. Benign
   in the current release; will require `allowed_msgpack_modules`
   registration in a future release. Actionable by `/system-design`
   when HITL payload types are defined.
2. `pyproject.toml` pin drift captured as TASK-CHORE-E4A1.

**Artefacts committed:**
- `spikes/deepagents-053/permissions_repro.py`
- `spikes/deepagents-053/interrupt_graph.py`
- `spikes/deepagents-053/interrupt_resume.py`
- `docs/research/ideas/deepagents-053-verification.md`
- `docs/architecture/decisions/ADR-ARCH-021-*.md` — appended References.
- `docs/architecture/decisions/ADR-ARCH-023-*.md` — appended References.

**`/system-design` unblock conditions:** TASK-SPIKE-D2F7 resolved **and**
TASK-CHORE-E4A1 merged. ADR revision tasks were **not** spawned because
neither primitive failed its verification — ADR-021 awaits only
server-mode closure (mode-coverage gap, not a failure).
