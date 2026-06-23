# /feature-plan — fleet-memory-deploy-runbook — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-plan fleet-memory-deploy-runbook — 2026-06-22T14:50:29Z

### Prompt

```
/feature-plan "Fleet-memory Deploy Runbook" --context features/fleet-memory-deploy-runbook/fleet-memory-deploy-runbook_summary.md
```

### Assistant response

I'll follow the `/feature-plan` workflow. Let me start by reading the context file you specified.

Let me explore the feature spec directory and the relevant codebase context.

Now let me explore the relevant codebase to ground the plan in reality. I'll dispatch parallel exploration.

Let me map the executor, registry, step handlers (FEAT-SSH), and runbooks location in parallel.

I have a clear picture now. Before I build the plan, let me flag the key finding and confirm a couple of decisions that genuinely change the task breakdown.

**Critical finding:** The FEAT-SSH handlers (`deploy_compose`, `run_smoke_tests`) are implemented but **only in the `.guardkit/worktrees/FEAT-SSH/` worktree — not merged into `main`**. TASK-SSH-005 (`register_shell_handlers`) and TASK-SSH-006 (integration test) are still pending. This feature's core wiring (`forge runbook run` → real registry + real NATS publisher) depends on that code landing. Also, the deploy/smoke scripts live in the **sibling `fleet-memory` repo**, which an autobuild worktree can't edit (per project memory).

Decisions noted: **depend on FEAT-SSH merge** (hard prerequisite — no handler re-implementation), **operator_handoff** for the real-NAS run, **balanced/all-aspects** review. Proceeding to the review analysis.

Let me quickly ground one detail — how a real NATS client is constructed elsewhere, since TASK-FMDR-002 must replace `_NoOpNATSClient`.
