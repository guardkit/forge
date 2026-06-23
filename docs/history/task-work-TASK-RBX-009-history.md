# /task-work — TASK-RBX-009 — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /task-work TASK-RBX-009 — 2026-06-22T13:29:56Z

### Prompt

```
/task-work TASK-RBX-009
```

### Assistant response

I'll start by loading the task context. Let me find the task file.

Found it in backlog. Let me read the task file.

This is a complexity-5 task from a review (`parent_review`), so it auto-detects to **LIGHT** intensity. The scope explicitly says "Pick the approach with the team," so I need to understand the code first, then get your decision on the approach. Let me read the relevant files.

Now let me read the runbook repository and models.

Now let me look at the migration schema and the existing tests, plus check how the executor handles the running step in its recovery shortcut.

Let me look at the concurrency test and the migration application pattern to understand idempotency conventions.

Now let me look at the executor test to understand the test harness (fake repo vs real repo).

I now have a thorough understanding. The task explicitly requires a **team decision** on the approach ("Pick the approach with the team; ship at least the backoff guard"). Here's what I found:

- The `while True` loop in [executor.py:156](src/forge/executor/executor.py#L156) reloads the runbook each iteration. A `running` step at the pointer is neither fast-advanced (only `passed` is) nor claimable (`try_claim_step_for_execution` excludes `running`), so the loop `continue`s forever → busy-spin.
- All ACs can be satisfied by the **backoff guard** alone (escalate as `stalled`); the lease/owner column is explicitly conditional ("*If* a lease/owner column is added…").

Let me get your decision on scope before implementing.

The user chose the **full lease/heartbeat** approach. That elevates this beyond LIGHT (schema migration + repo + executor + idempotency + no-steal guarantee), so I'll treat it with standard rigor. 

A key design insight: the lease alone doesn't prevent busy-spin — while a `running` step's lease is *unexpired* (held by a live or recently-crashed executor), the claim still fails and the loop would hot-spin until expiry. So the correct design is **lease + backoff guard** (which the task mandates as the minimum anyway). Let me read the rest of the executor tests and the seam test to preserve announce/claim semantics.

Let me check the seam test and the existing repo test for claim, to mirror their patterns.

Let me check the existing claim test and where the executor/claim is wired, plus the lifecycle migration registration.

There's an existing lease idiom in `coexistence.py` using `claimed_at`/`claimed_by`. Let me read it to mirror project conventions.

Good — there's an established `claimed_at`/`claimed_by` lease idiom in the project. Now let me check the BDD tests and CLI wiring for any `step_started`/executor-construction assumptions I might affect.

The CLI formats `result.reason` into a message, so `"stalled"` slots in fine. Let me check the BDD test around line 222 (the "adversarial_step_type" / step_started==1 assertions) to confirm my plan to announce `step_started` only after a successful claim won't break them.

The test at line 222-224 requires `step_started` to be announced for the unknown-handler case, so I'll announce it in that branch and after a successful claim. Let me verify `EscalatedPayload.reason` accepts a `"stalled"` value (it might be a constrained Literal).

`EscalatedPayload.reason` is a strict `Literal` and lives in the **`nats_core` sibling package** (which per my memory I shouldn't edit from a forge-scoped task). So for the `stalled` safety-net I'll return `RunResult(reason="stalled")` + log, but not publish a NATS escalated event (documented). Let me confirm the CLI handles the escalated result generically.

I have a complete design. Let me transition the task to in_progress, then implement. First the state transition:

Now let me implement. **1. Migration** — add `claimed_at`/`claimed_by` columns to the DDL and an idempotent upgrade path for existing tables:

Update the migration docstring to document the new columns:

**2. Repository** — add the lease-aware claim. First the imports and a lease constant:

Now remove the two local `from datetime import datetime` imports since it's now top-level:

Now the core change — the lease-aware `try_claim_step_for_execution`:

**3. Executor** — add `stalled` to `RunResult`, imports, and a lease/backoff constant:

Now the `__init__` with the new lease/backoff parameters:

Now the loop restructure. Let me re-read the current loop body to edit precisely:

Now I'll restructure the loop. First, add stall-tracking state before the loop and remove the early `step_started` announce (it moves to after a successful claim):

Let me verify the modules compile and run the existing test suites to check for regressions:

All pass except one schema test that asserts an exact column set (legitimately needs updating for the new columns). Let me read and fix it:

The schema test is fixed. Now let me write the new tests. First check whether `test_runbook.py` already covers the claim method:

No direct unit test exists for the claim method. Let me create a focused test module for the lease behavior:

Now the migration idempotency + upgrade-path tests. Let me check the fixtures in the migration test file:

Let me look at the existing idempotency test class to extend it:

Now the executor crash-recovery + stalled tests. Let me append them to `test_executor.py`:

Now run the full set of affected tests:

All green. Now let me run lint/format (AC4) and the wider runbook-related suite for regressions:

zsh doesn't word-split unquoted variables. Let me pass files explicitly:

Lint clean. Let me apply formatting:

Formatting applied and clean. Let me run the broader runbook-related suite (CLI, security, integration, BDD) to confirm no wider regressions:

All 203 pass. Let me also run the integration BDD test (it may skip without nats-py):

The 2 integration errors are a pre-existing pytest-asyncio fixture/live-broker infra issue, unrelated to my change. Let me confirm they fail identically on the baseline (stash my changes):

Confirmed pre-existing — identical errors on baseline without my changes. 

Now let me finalize the task: mark ACs complete, add an implementation summary, and move to `in_review`.

Now move the task to `in_review` and update its frontmatter:

Let me do a final confirmation run of the new/changed tests for the report:

✅ **Task Work Complete — TASK-RBX-009** (Executor crash-recovery for steps stuck in `running`)

**Approach:** Lease/heartbeat (your choice) + backoff guard. The lease alone doesn't prevent busy-spin — while a `running` step's lease is *unexpired*, the claim still fails, so the loop would hot-spin until expiry. The two mechanisms together cover both crash-recovery and the spin guard the task mandated.

## What changed

| File | Change |
|------|--------|
| [migrations/runbook.py](src/forge/persistence/migrations/runbook.py) | Added nullable `claimed_at`/`claimed_by` to `runbook_steps` (fresh DBs in `CREATE TABLE`; older DBs via guarded `ALTER TABLE` — `_ensure_claim_lease_columns`). Idempotent across schema versions. |
| [repositories/runbook.py](src/forge/persistence/repositories/runbook.py) | `try_claim_step_for_execution` stamps the lease and reclaims a `running` step whose `claimed_at` is NULL/older than `lease_seconds` (default `DEFAULT_CLAIM_LEASE_SECONDS=900`) — all in one atomic UPDATE, so no-steal holds. |
| [executor/executor.py](src/forge/executor/executor.py) | Passes the lease to the claim; backs off (`asyncio.sleep`) instead of hot-spinning; after `max_stall_cycles` no-progress cycles returns `RunResult(reason="stalled")`. `step_started` now fires once per claimed step, not per poll. |

## Acceptance criteria — all met
- ✅ Crashed `running` step makes progress (reclaimed) or escalates `stalled`, never busy-spinning
- ✅ Live in-flight `running` step is **not** stolen (live-lease claim refused)
- ✅ Lease columns added; migration idempotent + upgrade path; repo seam tests cover reclaim
- ✅ Lint (`ruff check`) and format (`ruff format`) clean on all modified files

## Tests
- **New:** `test_runbook_claim_lease.py` (reclaim, NULL-lease, no-steal, passed-refusal, owner stamping); 2 migration tests (idempotent re-apply, ALTER upgrade of a pre-RBX-009 table); 2 executor tests (end-to-end reclaim, `stalled` escalation without spin).
- **Updated:** schema column-set assertion to include the additive lease columns.
- **Results:** 203 passed / 1 skipped across the runbook suite. The 2 errors in `test_runbook_executor_integration.py` are **pre-existing** (pytest-asyncio fixture + live-broker infra) — confirmed identical on baseline via stash.

## Two follow-ups (documented in the task file)
1. **Lease vs. handler duration:** the executor doesn't heartbeat during a handler, so a handler outliving the 900s lease could be reclaimed → double-run. Periodic claim-renewal during long handlers (notably FEAT-SSH shell steps) is the robust fix.
2. **`stalled` not on the wire:** not published as a NATS escalated event because `nats_core.EscalatedPayload.reason` is a closed Literal in that **sibling package** (out of scope per [[autobuild-cannot-edit-sibling-repos]]). It's logged + surfaced via `RunResult`/CLI. Adding `stalled` to that Literal is a small upstream follow-up.

**State:** `backlog` → `in_review` (moved to [tasks/in_review/runbook-executor/](tasks/in_review/runbook-executor/TASK-RBX-009-crash-recovery-lease.md)). Changes are staged/working-tree only — not committed, per your standing preference.
