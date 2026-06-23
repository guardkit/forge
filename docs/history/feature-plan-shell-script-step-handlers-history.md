# /feature-plan — shell-script-step-handlers — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-plan shell-script-step-handlers — 2026-06-22T06:59:21Z

### Prompt

```
/feature-plan "Shell Script Step Handlers" \
      --context features/shell-script-step-handlers/shell-script-step-handlers_summary.md
```

### Assistant response

I'll follow the `/feature-plan` orchestration flow. Let me start by reading the context file to understand the feature.

Let me explore the existing feature artifacts and the codebase context before proceeding.

I now have a complete picture of the contract this feature builds on. Let me summarize what I found before clarifying scope:

- **Upstream interface (FEAT-RBX, in progress)**: `StepHandler` Protocol = `(step: Step) -> StepOutcome`; `StepOutcome(status: StepStatus, result: dict|None)`; `StepTypeRegistry.register/resolve`. Step params come from `Step.params` (a Mapping). Handlers must map exit status → `StepStatus.passed/failed`.
- **Scrubber gap**: `src/forge/memory/redaction.py::redact_credentials` covers GitHub/bearer/hex only and is Graphiti-scoped. This feature needs a **new sibling** `scrub_process_output` with postgres-DSN + password patterns, following the same purity/idempotency contract.
- **Spec is mature**: 20 scenarios, BDD `.feature` file present, 14 assumptions (3 low-confidence open ones).

Per the `/feature-plan` flow, I'll start with **Context A: Review Scope Clarification**. The three low-confidence open assumptions materially change the plan, so I'll fold them in.

Context A recorded: **Security + correctness** focus, **harden timeout + size cap now** (extends the spec's ASSUM-008/009 — ASSUM-013 env-file pre-validation stays deferred), **Standard** depth.

Let me ground task conventions against existing artifacts before producing the analysis.


---

## /feature-plan shell-script-step-handlers — 2026-06-22T12:56:37Z

### Prompt

```
/feature-plan "Shell Script Step Handlers" \
      --context features/shell-script-step-handlers/shell-script-step-handlers_summary.md
```

### Assistant response

I'll follow the `/feature-plan` orchestration flow. Let me start by reading the context file to understand the feature.

Let me gather more context about the feature spec, the existing executor, and the codebase structure.

The `.feature` file already has `@task:TASK-SSH-*` tags, and git status shows an existing `.guardkit/features/FEAT-SSH.yaml`. Let me inspect these and the registry/executor to ground the plan.

It appears `/feature-plan` has already produced a complete plan for FEAT-SSH. Let me verify the task files and implementation guide actually exist before deciding whether to re-run.

The plan for **"Shell Script Step Handlers"** already exists and is complete — it appears `/feature-plan` was already run earlier today (artifacts timestamped 2026-06-22 09:09–09:14). Before I re-run and risk clobbering good work, here's what's already on disk:

**`.guardkit/features/FEAT-SSH.yaml`** — 6 tasks, complexity 5, 5 waves, smoke gates configured:
- TASK-SSH-001: `scrub_process_output` credential scrubber (the new sibling scrubber called out in the summary)
- TASK-SSH-002: Subprocess-runner core (timeout + size-cap + scrub) — also hardens ASSUM-008/009
- TASK-SSH-003 / 004: `deploy_compose` / `run_smoke_tests` handlers (parallel)
- TASK-SSH-005: registry wiring
- TASK-SSH-006: integration test against fleet-memory `smoke.sh`

**Also present and well-formed:**
- All 6 task markdown files + `IMPLEMENTATION-GUIDE.md` + `README.md` in [tasks/backlog/shell-script-step-handlers/](tasks/backlog/shell-script-step-handlers/)
- The `.feature` file is already BDD-linked — all 20 scenarios carry `@task:TASK-SSH-*` tags (Step 11 already done)
- Smoke-gate command targets `tests/forge`, which **exists** (so the known stale-path footgun doesn't apply)

This is a genuine fork, so I'd rather not guess.

Understood — keeping the existing plan as-is. No changes made.

**FEAT-SSH is ready for the next step.** When you want to implement it:

```bash
/feature-build FEAT-SSH
```

The plan is intact:
- `.guardkit/features/FEAT-SSH.yaml` — 6 tasks across 5 waves, smoke gate on `tests/forge` (path verified present)
- [tasks/backlog/shell-script-step-handlers/](tasks/backlog/shell-script-step-handlers/) — 6 task files + `IMPLEMENTATION-GUIDE.md` + `README.md`
- `.feature` fully BDD-linked (all 20 scenarios tagged `@task:TASK-SSH-*`)

One thing worth a glance before you build: the summary flags **ASSUM-008/009/013** as low-confidence (subprocess timeout, output size-cap, env-file pre-validation). The YAML shows TASK-SSH-002 already folds in timeout + size-cap hardening and defers env-file validation — so those assumptions were resolved at plan time. Nothing to do unless you want to revisit that call.
