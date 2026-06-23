# /feature-plan — runbook-and-step-persistence — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /feature-plan runbook-and-step-persistence — 2026-06-21T17:22:31Z

### Prompt

```
/feature-plan "Runbook and Step Persistence" --context features/runbook-and-step-persistence/runbook-and-step-persistence_summary.md
```

### Assistant response

I'll execute the `/feature-plan` workflow for "Runbook and Step Persistence". Let me start by reading the context file and understanding the existing feature structure.

Let me read the full feature spec and explore the existing Forge persistence conventions referenced in the summary.

Now I understand the spec fully. Let me explore the existing Forge persistence conventions that this feature must reuse, so the plan is grounded in real code.

I now have a complete picture: the spec (33 scenarios, 11 confirmed assumptions) plus the exact Forge persistence conventions this feature must reuse (`BridgeRegistry` exemplar, `connect_writer`/`read_only_connect`, idempotent `apply(connection)` migrations, STRICT tables with CHECK constraints, `tmp_path` test fixtures).

The technical approach here is essentially **pre-determined by the spec** — it explicitly says to mirror `BridgeRegistry`. So the review's real value is task breakdown, not choosing between competing options. Before I generate the plan, let me confirm the few preferences that actually shape the output.

Preview fields can't be null — let me fix that (keep previews only where they aid comparison).

Decisions captured: **Layered breakdown (~6 tasks)**, **TDD**, **Full structure + YAML**. That's effectively the `[I]mplement` path with Context A/B answered.

Before generating files, let me verify the environment — task directories, the feature-YAML generator CLI, and existing task ID conventions.

Environment confirmed. Let me examine an existing task file and feature YAML in this repo to match the exact local conventions (not just the generic docs), plus the test exemplar.

Let me check the existing `smoke_gates` format in this repo to match it exactly.

I have full fidelity on conventions now. Let me check the generator CLI options (to control the feature ID), then track the build with a todo list.

Everything is grounded now. I have the spec, the exact Forge persistence conventions, the local task/YAML/smoke-gate formats, and the user's choices (Layered / TDD / Full structure + YAML).

Here's the review analysis, then I'll generate the structure.

---

## 🔍 FEATURE PLANNING: Runbook and Step Persistence

### Technical Options Analysis

The `/feature-spec` output already resolved the hard analytical questions (33 scenarios, 11 confirmed assumptions). The implementation notes are explicit: **mirror the `BridgeRegistry` exemplar**. So this is less "choose between options" and more "decompose correctly". For completeness:

| Option | Verdict |
|---|---|
| **1. Sibling tables + repository mirroring `BridgeRegistry` (Recommended)** | STRICT `runbooks` + `runbook_steps` tables, idempotent `apply(connection)` migration, `RunbookRepository` with `BEGIN IMMEDIATE` writes, `_safe_rollback`, JSON `params`/`result`, ISO-8601 TEXT timestamps. Reuses `connect_writer`/`read_only_connect`. **Zero new infra, full convention reuse, every scenario satisfiable as pure unit tests.** |
| 2. Single denormalised `runbooks` table with JSON steps blob | Simpler schema, but loses per-step query/`CHECK`/sequence-order guarantees the spec demands (Group G "reload in sequence order", per-step status query). Rejected. |
| 3. New ORM/SQLModel layer | Adds a dependency and diverges from the established raw-`sqlite3` convention. Rejected (LCOI/YAGNI). |

**Recommended: Option 1** — complexity 5/6, risk **Low**, pure-unit (no NATS/subprocess/LLM), `tmp_path` SQLite per test.

The three low-confidence assumptions are now **locked as decisions** (all marked `confirmed` in the manifest): empty step-list refused (ASSUM-002), advance-past-final refused (ASSUM-004), overall status immutable this phase (ASSUM-009).

### Decision: **[I]mplement** (per your answers) → generating the layered, TDD, full-structure plan now.

Let me create the folder and the provenance-anchoring review task.

Now generating all artifacts. Starting with the review-task provenance anchor and the two Wave 1 tasks (models + migration).

Now the Wave 2 + Wave 3 repository tasks (§4 consumers — these get `consumer_context` frontmatter and seam-test stubs).

Now the Wave 4 test tasks (security/data-integrity and concurrency/integration — separate test files for parallel safety).

All six tasks + review anchor created. Now the **IMPLEMENTATION-GUIDE.md** with the mandatory Mermaid diagrams (data flow, integration-contract sequence, dependency graph, and §4 contracts).

All markdown artifacts are written. Now generating the structured YAML feature file via the CLI (this also runs the AC linter + R2/R3 nudges from steps 10.5–10.7).

YAML generated cleanly: **FEAT-RSP, 6 tasks, 4 waves**, and the **AC linter passed with 0 unverifiable criteria** (my ACs are all concrete/verifiable). The R3 nudge confirms `tests/forge` is a real test root.

Let me add a feature-level smoke gate (between-wave composition check) and then run the validators. First, let me see the YAML's orchestration tail to place the block correctly.

YAML structure confirmed. Let me append the `smoke_gates` block (composition check after waves 3 and 4, both temporally safe since all referenced test files exist by then), then run the validators.

Now run the validators — smoke-gate path validation (Step 8.6) and the full feature pre-flight (Step 8.5):

Both validators pass — **smoke-gate paths OK**, **feature FEAT-RSP is valid** (task files exist, deps valid, no intra-wave conflicts, task_types valid).

Now **Step 11: BDD scenario linking** — tag the 33 Gherkin scenarios with `@task:<TASK-ID>` to activate the R2 task-level BDD oracle. Let me prepare the matching request:

Status `ready` — 33 scenarios, 6 tasks. Now I invoke the `bdd-linker` subagent with the request payload, then apply its mapping.

Now I'll invoke the `bdd-linker` subagent with this payload to map all 33 scenarios to the 6 tasks.

All 33 scenarios mapped with high confidence (0.82–0.93, all above the 0.6 threshold). Let me write the matcher response to a file and apply the mapping (rewrites the `.feature` file with `@task:` tags).

All 33 scenarios linked (`@task:` count = 33, 0 below threshold). R2 BDD oracle now activated. Let me clean up temp files and show a sample of the tagged `.feature` file.
