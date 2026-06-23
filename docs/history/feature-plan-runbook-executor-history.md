# /feature-plan — runbook-executor — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-plan runbook-executor — 2026-06-21T17:47:56Z

### Prompt

```
/feature-plan "Runbook Executor" \
  --context features/runbook-executor/runbook-executor_summary.md
```

### Assistant response

I'll execute the `/feature-plan` workflow for "Runbook Executor". Let me start by reading the context file to understand the feature.

Now let me understand the existing project structure and the related persistence feature before proceeding with planning.

Let me read the executor feature spec, assumptions, and the persistence feature's implementation guide to mirror conventions.

Now let me understand the existing Forge codebase conventions that these tasks must ground in — the publisher pattern, CLI structure, and persistence repository surface.

Let me examine the key reuse points: the publisher pattern, CLI group, persistence repository surface, and the persistence feature's implementation guide.

Let me examine the publisher pattern and CLI group structure — the two main reuse points for the executor.

Let me check where `nats_core` events live (new EventType members needed) and the persistence repository location.

I have nearly everything. Let me verify one thing that determines whether a key question is even viable — whether `MessageEnvelope.event_type` strictly requires the `EventType` enum (which would force editing the sibling `nats-core` package) or accepts arbitrary strings.

I now have a complete picture of the feature and its codebase grounding. Two genuinely consequential decisions gate the task breakdown — both are flagged by the spec itself as needing human resolution, and both change what I generate. Let me get those settled.

Key findings from my analysis:
- **`MessageEnvelope.event_type` is a closed `EventType` enum** in the sibling `nats-core` package, with a payload registry (`payload_class_for_event_type` raises on unknown types). So emitting runbook events "by the book" means extending `nats-core`.
- **The ASSUM-004 conflict is real and unresolved**: the executor spec wants the pointer to rest *beyond* the final step (`== count`), but FEAT-RSP's locked plan refuses advancing past the last step.

Decision recorded. For Q1 you asked for the best long-term quality solution, so I'll go with **relaxing persistence** (pointer rests at `index == step_count` as the single, unambiguous durable completion marker). The alternative leaves the pointer ambiguous at the final step — you'd have to read step status to tell "pending" from "done", which is exactly the kind of dual-source-of-truth that causes resume bugs later. This requires reconciling the not-yet-built FEAT-RSP plan, which I'll do as part of this run.

Let me look at the exact conventions to mirror (an RSP task file, the bridge_registry repo, and the YAML generator's options) before generating.

I have everything I need. The design is settled:

- **Q1 → relax persistence (R1):** the resume pointer is the single, unambiguous completion marker. Because the executor stops on failure, the pointer only ever reaches `== step_count` when every step passed — so `(current_step_index, per-step statuses)` fully determines runbook state with no redundant overall-status writes. This needs exactly one narrow amendment to the not-yet-built FEAT-RSP: `advance()` may reach the terminal position `== count` (but not beyond). FEAT-RSP's ASSUM-009 (overall status not mutated) stays intact.
- **Q2 → extend nats-core.**

Let me track this generation work and create the feature folder.

Now I'll generate the full feature plan. Starting with the review/decision document, then the seven task files. Let me write the review doc first.

Now the seven task files. TASK-RBX-001 (registry foundation):

Now TASK-RBX-002 (nats-core lifecycle events — declarative, the §4 producer):

TASK-RBX-003 (RunbookPublisher — consumer of the events contract, with seam test):

TASK-RBX-004 (executor dispatch loop — the core, consumer of multiple contracts):

TASK-RBX-005 (CLI `forge runbook run`):

TASK-RBX-006 and TASK-RBX-007 (the two scenario-coverage test tasks):

All seven task files written. Now the IMPLEMENTATION-GUIDE.md with the mandatory diagrams (data flow, integration-contract sequence since aggregate complexity ≥ 5, task dependency graph, and §4 contracts):

Now the README. Let me mirror the persistence feature's README format:

All planning docs written. Now generate the structured YAML feature file via the generator (with `--discover` to resolve the real file paths and `--feature-id FEAT-RBX` to match the FEAT-RSP naming convention):

The YAML generated cleanly — **0 unverifiable ACs**, 5 waves. The R3 nudge correctly notes no smoke gates. Like FEAT-RSP, I'll add a between-wave smoke gate. Let me read the generated YAML and verify the available test roots before authoring the gate (the command emphasizes path verification to avoid the TASK-REV-DEA8 regression).

Verified: `tests/forge` and `tests/bdd` exist; `tests/forge/executor` correctly does **not** exist yet (it's created by wave 1, so I won't hardcode it). Note the grep matched *file contents*, but `-k` filters on *node ids* (path + test name) — so `-k "runbook or executor"` will target only the new tests (their paths contain those words), not the existing files that merely mention the words in comments.

Like FEAT-RSP, I'll add a between-wave smoke gate using the existing `tests/forge` root + a `-k` filter (spatially verified root; temporally safe since executor tests exist from wave 3 on):

Now run the two validation passes the command requires — feature pre-flight (8.5) and the smoke-gate validator (8.6, since I hand-injected smoke gates):

Both validations pass: **✓ Feature FEAT-RBX is valid** and **✓ smoke_gates.command paths OK**.

Now the cross-feature reconciliation my R1 decision requires. Since I stated in the guide that the FEAT-RSP amendment is applied as part of this run, I'll make that true — surgically. Let me first see the exact persistence scenarios/assumptions affected.

The persistence `.feature` encodes the **old** ASSUM-004 directly in human-confirmed scenarios ("Advancing past the final step is refused", lines 119–123 and 360–365). These were confirmed by you during `/feature-spec`. I created the FEAT-RBX files in this run, but I did **not** create the FEAT-RSP spec — so rather than silently flip your confirmed BDD across four files, I'll keep the executor plan self-contained and surface the reconciliation as a precise, required pre-build action for you to apply atomically. Let me correct my two over-stated wording spots and turn the guide section into an exact checklist.

Wording corrected — the reconciliation is now surfaced as a precise, required, *not-yet-applied* checklist that you own.

Now **Step 11: BDD scenario linking** — wire `@task:<TASK-ID>` tags onto the executor's `.feature` scenarios so the R2 BDD oracle activates during build. Let me run the prepare step:

`status: ready` — 28 scenarios, 7 tasks. Now Step 11.2: read the request payload and invoke the `bdd-linker` subagent to map scenarios → tasks.

The payload has all 28 scenarios with steps + the 7-task list. Now I'll invoke the `bdd-linker` subagent to produce the scenario→task mapping (it reads the request file and returns the `TaskMatch` JSON array):

Now I'll invoke the `bdd-linker` subagent to do the matching:

All 28 scenarios matched with high confidence. The distribution looks right: registry-specific → 001, CLI scenarios → 005, security/data-integrity → 006, concurrency/real-broker → 007, the executor core → 004. Now Step 11.3 — write the response and apply the tags:

All 28 scenarios tagged. Let me verify the tags landed in the `.feature` file and clean up temp files:

All 28 scenarios carry `@task:` tags (R2 BDD oracle now activates during build). Distribution confirms the design — TASK-RBX-002 (nats-core declarative) and TASK-RBX-003 (publisher, unit-tested directly) correctly have no BDD scenarios; the executor core owns 17.

Let me do a final integrity check of the complete deliverable set:
