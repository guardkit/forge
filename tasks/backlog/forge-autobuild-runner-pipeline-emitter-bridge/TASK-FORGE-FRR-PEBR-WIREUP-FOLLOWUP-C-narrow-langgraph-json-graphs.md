---
id: TASK-FORGE-FRR-PEBR-WIREUP-FOLLOWUP-C
title: Narrow langgraph.json to autobuild_runner only (drop broken orchestrator graph reference)
status: backlog
created: 2026-05-08T11:30:00Z
updated: 2026-05-08T11:30:00Z
priority: high
task_type: fix
parent_review: TASK-REV-PEBR-004
parent_task: TASK-FORGE-FRR-PEBR-WIREUP
parent_feature: FEAT-PEBR
related_tasks:
  - TASK-FORGE-FRR-PEBR-WIREUP   # parent fix; this followup is independent of AC-11 but blocks the canonical sidecar config from loading
complexity: 2
estimated_minutes: 25
estimated_test_minutes: 30
implementation_mode: task-work
wave: 1
intensity: light
intensity_reason: provenance=parent_review (TASK-REV-PEBR-004), complexity=2, two-line config edit + sidecar boot test, no high-risk keywords
tags:
  - forge-serve
  - langgraph-deployment
  - sidecar-config
  - feat-pebr
  - pebr-wireup-followup
  - first-real-run-followup
  - regression-protection
discovered_during: TASK-REV-PEBR-004 (jarvis runbook RUNBOOK-FEAT-JARVIS-INTERNAL-001 post-PEBR-WIREUP revalidation, 2026-05-08)
forge_head_at_discovery: 1b82236
chosen_option: ii   # narrow langgraph.json to production graphs only
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Fix FOLLOWUP-C — narrow `langgraph.json` to `autobuild_runner` only

## TL;DR

[`langgraph.json`](../../../langgraph.json) declares two graphs:

```json
{"graphs": {
    "orchestrator": "./src/forge/agent.py:agent",
    "autobuild_runner": "./src/forge/subagents/autobuild_runner.py:graph"
}}
```

[`src/forge/agent.py:23`](../../../src/forge/agent.py#L23) does `from agents import create_orchestrator`, which fails:

```
ModuleNotFoundError: No module named 'agents'
```

**There is no `agents/` package anywhere in the repo** (verified during TASK-REV-PEBR-004): not at the top level, not at `src/agents/`, not at `src/forge/agents/`. **`def create_orchestrator` is not defined anywhere in the codebase**. The `agent.py` entrypoint is template carry-over from langchain-deepagents that was never ported to Forge — Forge's actual production graph is `autobuild_runner` only.

Operator-facing impact: the canonical sidecar config cannot load. Operator was forced to start the langgraph-runner sidecar with a stripped `langgraph.json` containing only `autobuild_runner`. The production deploy cannot do this gracefully.

## Why

The chosen option (per TASK-REV-PEBR-004 AC-3) is **Option (ii)** — narrow `langgraph.json` to ship only what works.

| Option | Why not chosen |
|---|---|
| (i) "fix the import" | Would require *manufacturing* a `create_orchestrator` factory and an `agents/` package. There is no production caller for it; building it would be net-new work outside the scope of unblocking the canonical sidecar boot. |
| (iii) "split the file" — keep both graphs in `langgraph.json` for `langgraph dev`, ship a production override | Assumes the orchestrator graph has a real dev consumer. It does not — there's no working factory at all. Splitting preserves a broken artifact. |
| **(ii) "narrow langgraph.json"** | Minimal, ships only what works, matches reality (Forge production = `autobuild_runner` only). If a deepagents orchestrator graph is wanted later, it can be re-introduced once `create_orchestrator` actually exists. |

## Acceptance Criteria

- [ ] **AC-1** — **Pre-flight: confirm no consumer exists.** Before deleting the `orchestrator` entry, grep the workspace for any consumer:
  - `grep -rn '"orchestrator"' --include='*.json' --include='*.yaml' --include='*.yml' --include='*.py' --include='*.md'`
  - `grep -rn "graphs\['orchestrator'\]\|graphs.orchestrator" --include='*.py'`
  - `grep -rn "from agents import\|from forge.agents import" --include='*.py'`
  - Check `tests/`, `docs/`, `scripts/`, `tools/`, `runbooks/`, and any CI config for references to the `orchestrator` graph name as a langgraph graph identifier.

  **If any consumer is found** (test harness, dev tooling, runbook, etc.), STOP and re-scope this task: fall back to **Option (iii)** (move `orchestrator` to `langgraph-dev.json`) and document the consumer. Otherwise proceed with Option (ii).

- [ ] **AC-2** — **Narrow `langgraph.json`.** Edit [`langgraph.json`](../../../langgraph.json) to remove the `orchestrator` graph entry. Final shape:

  ```json
  {
      "dependencies": ["."],
      "graphs": {
          "autobuild_runner": "./src/forge/subagents/autobuild_runner.py:graph"
      },
      "env": ".env"
  }
  ```

- [ ] **AC-3** — **Mark `agent.py` as non-shipping.** Either:
  - **(preferred)** Delete `src/forge/agent.py` outright. The file's `from agents import create_orchestrator` import has never resolved; deleting the dead file is the cleanest answer and removes the future temptation to wire it.
  - **(only if AC-1's grep finds an importer of `forge.agent`)** Leave the file in place and add a module-level docstring noting that the entrypoint is template scaffolding not used in Forge production; do NOT attempt to fix the import.

  Document the choice in the task body's "Implementation Notes" section before merging.

- [ ] **AC-4** — **Sidecar boot regression test.** Stand up the langgraph-runner with the narrowed `langgraph.json` and confirm:
  - The sidecar boots cleanly (no `ModuleNotFoundError: No module named 'agents'`).
  - The `autobuild_runner` graph loads and is reachable at the langgraph-runner SDK URL.
  - Either: (a) add a CI test that runs `langgraph dev --no-browser` for ~10s and asserts the boot log contains `autobuild_runner` and does not contain `ModuleNotFoundError`; OR (b) document the manual sidecar-boot validation step in the task's body and capture the boot log to `/tmp/runbook-evidence-FOLLOWUP-C/sidecar-boot.log`. Pick (a) if the existing test infra supports it; (b) is acceptable for a 25-min task.

- [ ] **AC-5** — **Update parent runbook reference (if applicable).** If `RUNBOOK-FEAT-JARVIS-INTERNAL-001-first-real-run.md` (or any sibling runbook) references the `orchestrator` graph or instructs operators to run with the un-stripped config, update the runbook to reflect the narrowed config. Out of scope: the six runbook gap-folds (those are tracked under `TASK-FRR-RUNBOOK-002`).

- [ ] **AC-6** — **Lint clean.** Black + ruff (no Python files touched in AC-2; AC-3 may delete one — confirm imports elsewhere don't break).

## Implementation Notes

- **AC-1 is the gate.** If the pre-flight grep finds a consumer, fall back to Option (iii) before proceeding. The review's recommendation assumes no consumer exists; verify that assumption before deleting.

- **`agent.py` is template carry-over.** It was scaffolding from the langchain-deepagents Pipeline Orchestrator template. Forge's production architecture routes builds through the `autobuild_runner` deepagents graph (TASK-FRR-PEB family), not through a separate orchestrator graph. There is no design decision lost by removing it.

- **Why not refactor `agent.py` to wrap `autobuild_runner`?** Because `autobuild_runner` is already directly registered in `langgraph.json` and reachable at its own graph name. Adding an orchestrator wrapper would be net-new product surface, not a fix.

- **Out of scope.** Do NOT define a new `create_orchestrator` factory. Do NOT create an `agents/` package. Do NOT touch `pyproject.toml` to add a new package directory. This task narrows config and removes dead code; any architectural reintroduction of an orchestrator graph belongs in a separate, design-led task.

- **Independence from FOLLOWUP-A and -B.** This task is independent of the AC-11 wire blockage. It can land in any order relative to A and B; it can ship in its own PR.

## Inputs / Evidence

- **Parent review**: [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — diagnosis and option selection.
- **Broken import**: [src/forge/agent.py:23](../../../src/forge/agent.py#L23) — `from agents import create_orchestrator`.
- **Deployment config**: [langgraph.json](../../../langgraph.json) — declares both graphs.
- **Production graph**: [src/forge/subagents/autobuild_runner.py](../../../src/forge/subagents/autobuild_runner.py) — the surviving entry.
- **Operator evidence**: sidecar logs at `/tmp/jarvis-runbook-evidence/` — `ModuleNotFoundError: No module named 'agents'` on canonical-config boot; success on stripped config.

## References

- [TASK-REV-PEBR-004](TASK-REV-PEBR-004-pebr-wireup-runbook-revalidation-followups.md) — parent review.
- [TASK-FORGE-FRR-PEBR-WIREUP](TASK-FORGE-FRR-PEBR-WIREUP-fix.md) — parent fix; this followup is collateral, not on the AC-11 critical path.
