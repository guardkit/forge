# Mode P Planning Chain (FEAT-SPL-002)

**Problem**: James (non-technical, Slack-only) has no governed door into the
factory's planning loop. DF-009 (Accepted 2026-07-05) makes planning attendance an
approval-gate property — a named, identity-pinned human approves at a
`product_docs` checkpoint — but forge has no planning-chain orchestration: nothing
consumes the frozen `PlanningQueuedPayload` (nats-core 0.5.0), and the existing
approval machinery pins a single static approver.

**Solution**: a standalone, additive planning subsystem (`src/forge/planning/`) —
intake consumer (ack-on-persist, trust-boundary validation) → durable
`planning_runs` records → pure-function planner → local PO via the specialist
dispatcher (first production composition, Coach-scored) → `product_docs`
checkpoint built from the D659 gate primitives with a per-run escalation-mutable
expected approver (James → Rich on timeout/defer-cap; never auto-approves) →
registry-indirected `PLANNED-HANDOFF` terminal (idempotent branch commit of spec
inputs + Slack notification with the exact attended `/feature-spec` command).
DF-004 boot audit guarantees planning model resolution can never silently
escalate to cloud; DF-006 frontier second opinion is config-gated, FLAG-only,
policy-filtered, degrade-to-human.

**Provenance**: spec `features/mode-p-planning-chain/` (33 scenarios; 16 deferred
assumptions, 7 panel-amended) · decision review TASK-REV-83E4 (3-agent panel) ·
SPL scope/build-plan Session 3 (`../ai-transition/docs/`) · fleet decisions
DF-009/DF-007/DF-006/DF-004/DF-001.

## Subtasks (11; 6 waves; ~740 min)

| Task | Title | Type | Cx | Wave |
|------|-------|------|----|------|
| TASK-MP-001 | PlanningConfig + DF-004 audit | feature | 4 | 1 |
| TASK-MP-002 | planning_runs durable store (schema_v3) | feature | 6 | 1 |
| TASK-MP-003 | chain data + pure-function planner | feature | 5 | 1 |
| TASK-MP-004A | planning gate protocol adapters | feature | 4 | 2 |
| TASK-MP-006 | PLANNED-HANDOFF terminal + registry | feature | 6 | 2 |
| TASK-MP-008 | planning intake consumer | integration | 5 | 2 |
| TASK-MP-004B | product_docs checkpoint flow | feature | 5 | 3 |
| TASK-MP-005 | escalation + defer-cap policy | feature | 6 | 4 |
| TASK-MP-007 | DF-006 frontier second opinion | feature | 4 | 4 |
| TASK-MP-009 | serve composition + recovery | integration | 7 | 5 |
| TASK-MP-010 | live-daemon operator validation | operator_handoff | 3 | 6 |

Operator follow-up tasks: 1 (TASK-MP-010 — plus the deferred assumption review by
Rich in `features/mode-p-planning-chain/mode-p-planning-chain_assumptions.yaml`).

See `IMPLEMENTATION-GUIDE.md` for diagrams, integration contracts, waves, and the
hard-constraint compliance map.
