# /feature-spec — minimal-runbook-executor-for-the-forge-output-side-loop-disp — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-spec minimal-runbook-executor-for-the-forge-output-side-loop-disp — 2026-06-21T17:31:47Z

### Prompt

```
/feature-spec "Minimal runbook executor for the Forge output-side loop: dispatch-by-step-type loop over a persisted Runbook (from the runbook model feature) — resolve each step's handler via a step_type registry, run it, persist result + status, advance the current-step pointer; stop-and-resume on failure (re-enter at the failed step index, no restart); publish NATS lifecycle events runbook-started/step-started/step-result/runbook-complete/escalated reusing the existing pipeline_publisher pattern and envelope; the executor has NO knowledge of step internals (registry indirection only); a CLI entry 'forge runbook run <path-to-runbook-json>' to load and execute; in-memory fake step handlers in tests so unit gates need no subprocess or NATS broker; one marker-gated integration test with a real NATS publish"
```

### Assistant response

I'll execute the `/feature-spec` workflow. Starting with **Phase 1: Context Gathering** (AI-only, no interaction). Let me scan the codebase for the referenced components: the Runbook model, the `pipeline_publisher` pattern, the `forge` CLI, and any existing `.feature` files.

The runbook model feature already exists (`features/runbook-and-step-persistence/`). Let me read the key artifacts: the Runbook persistence model, the `pipeline_publisher`, the existing runbook feature spec, and the `forge` CLI structure.

Now let me understand the runbook model implementation status, the `forge` CLI entry structure, and the existing NATS events/envelope module.

Let me ground the last two pieces: the `forge` CLI main group (how `forge runbook run` attaches) and the integration-test marker convention used in this repo.
