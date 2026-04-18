# Forge v2.2 Doc Alignment

**Parent review:** TASK-REV-A1F2
**Alignment report:** [docs/research/forge-build-plan-alignment-review.md](../../../docs/research/forge-build-plan-alignment-review.md)
**Anchor:** [docs/research/forge-pipeline-architecture.md](../../../docs/research/forge-pipeline-architecture.md) v2.1 → v2.2

## Problem

The three in-repo forge docs that preceded the 15 April 2026 anchor v2.1 (`forge-build-plan.md`, `forge-pipeline-orchestrator-refresh.md` v3, `fleet-master-index.md` v2) are now drifted. TASK-REV-A1F2 identified 40 corrections across these files plus the anchor itself — the anchor needs Jarvis/dual-role/stream-retention/topic-naming updates it is currently silent on, and the three dependent docs must be re-pointed at the corrected anchor before any Forge code is written.

## Solution

Four sequential tasks that each target one document. TASK-FVD1 lands the anchor changes (the source of truth) first; the other three apply their corrections against the updated anchor. All four are documentation-only — no code, no schema changes.

## Scope — Subtasks

| Task | File | Changes | Depends on |
|------|------|---------|-----------|
| TASK-FVD1 | `docs/research/forge-pipeline-architecture.md` | Apply v2.2 anchor additions: §5.0 Build Request Sources (Jarvis), §3.1 Specialist Agent Deployment Model, stream retention reconcile, topic naming singular, promote FLEET/JARVIS/NOTIFICATIONS streams. Promote the four ADRs (SP-014..017, currently Proposed) to Accepted once reviewed. | — |
| TASK-FVD2 | `docs/research/ideas/forge-build-plan.md` | Corrections 13–21 from the alignment review: caveat nats-core prerequisite, reconcile feature list with anchor §10 Phase 4, fix `max_concurrent: 3` → `1`, `agents.command.forge`, replace `greenfield` CLI surface with `forge queue`, reconcile `forge-pipeline-config.yaml` schema, remove stale context doc refs, add Jarvis integration subsection, add specialist-agent dual-role prerequisite | TASK-FVD1 |
| TASK-FVD3 | `docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | Corrections 8–12: align framing (pipeline orchestrator, not checkpoint manager), map greenfield flow to anchor's 5 stages, name state machine states, add Jarvis-as-upstream-trigger subsection, decide fate of `FeaturePlannedPayload` / `FeatureReadyForBuildPayload` | TASK-FVD1 |
| TASK-FVD4 | `docs/research/ideas/fleet-master-index.md` | Corrections 22–25: expand Jarvis description to include Forge-trigger role, document build-trigger mechanism, fix `max_concurrent: 3` → `1`, execute pending `TASK-update-fleet-index-d22.md` inline (repo inventory refresh) | TASK-FVD1 |

## Out of scope

- Any code changes (those are in `nats-core/`, `specialist-agent/`, `nats-infrastructure/`, `jarvis/`)
- Rewriting `docs/product/roadmap.md` or `docs/product/feature_spec_inputs/*` — they align with v2.1 already
- Rewriting the `forge-build-plan-alignment-review.md` itself (it is the audit; it stays as-is)
- Changing the anchor file's §1–8 core narrative — only additions and targeted corrections

## Execution

Execute TASK-FVD1 first. It establishes the v2.2 anchor that the other three reference. Afterwards, TASK-FVD2/3/4 can run in parallel (they touch independent files) or sequentially — the alignment review's per-file correction lists are self-contained.

Once all four are merged, re-run the relevant parts of TASK-REV-A1F2's cross-doc consistency checks (quick scan) to confirm the drift is gone.
