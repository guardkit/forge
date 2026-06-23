# /feature-spec — runbook-and-step-data-model-for-the-forge-output-side-loop-a — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-spec runbook-and-step-data-model-for-the-forge-output-side-loop-a — 2026-06-21T17:04:13Z

### Prompt

```
/feature-spec "Runbook and Step data model for the Forge output-side loop: a typed Step (step_type, params dict, status enum pending/running/passed/failed/awaiting_approval, result with exit_code + captured_output + timestamps, sequence_index) and a Runbook (runbook_id, ordered list of steps, current_step_index, overall status, target name, created_at) persisted in forge's existing SQLite store via a sibling table reusing adapters/sqlite/connect.py and the existing migration path; status-per-step queryable; resume pointer (current_step_index); gates-as-data (a step in awaiting_approval) modelled even though this phase uses none; NO executor logic yet — model + persistence + repository methods (create_runbook, load_runbook, update_step_status, advance) with unit tests; no NATS, no subprocess, no LLM"
```

### Assistant response

I'll execute the `/feature-spec` Propose-Review methodology. Starting with **Phase 1: Context Gathering** (silent, no interaction).

Let me gather context about the stack, the existing SQLite store, migration path, and any prior conventions.

Stack is **Python**. Let me examine the SQLite adapter, migration path, persistence layer, and existing repository/model conventions.

Let me examine the migration system, repository pattern, and model conventions in depth.

Let me examine the boot migration path, the canonical schema, and existing `.feature` conventions.

Let me read the canonical schema and a relevant existing `.feature` file to match conventions.
