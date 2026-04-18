# ADR-ARCH-005: Graphiti-fed learning loop with per-stage notification tuning

- **Status:** Accepted (refined by ADR-ARCH-019 — notification tuning became reasoning-driven rather than statically configured)
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 2 Revision 3

## Context

Rich's motivation doc + fleet-master-index describe Forge as a *learning* orchestrator: threshold calibration from override rates, notifications tunable over time, compounding knowledge in Graphiti. Naive version: `forge.yaml` holds per-stage threshold overrides and per-event notification on/off flags, all adjusted manually. Problem: manual adjustment means Rich is doing the learning. If Forge reasons and learns, the priors live in Graphiti and the reasoning model uses them — manual YAML editing is the anti-pattern.

See ADR-ARCH-019 for the refined position that makes behavioural config reasoning-driven, not static.

## Decision

Establish two Graphiti groups for Forge's knowledge substrate:

- **`forge_pipeline_history`** — every gate decision, every override event, every session outcome, every `CapabilityResolution` (which agent served which dispatch). Written by `forge.adapters.graphiti` at each significant step.
- **`forge_calibration_history`** — see ADR-ARCH-006 (parsed history-file corpus).

`forge.learning` (pure domain core module) periodically scans `forge_pipeline_history` for override patterns. When a pattern crosses a configurable significance threshold (e.g. 6/10 flagged gates overridden at a scored band), it **proposes** a `CalibrationAdjustment` entity — routed to Rich via `ApprovalRequestPayload` over NATS. Rich approves via CLI; the entity is written to Graphiti; future builds' system-prompt retrievals include it.

**Notifications decided in-context**, not configured — see ADR-ARCH-019.

## Consequences

- **+** Learning is persistent, queryable, and explicitly auditable — entities in Graphiti are retrospectable.
- **+** Compounds across builds and projects (via `forge_pipeline_history` + scope filters).
- **+** Fleet D27 alignment — "Graphiti for relationships and decisions, not documents"; entities here are decisions, not documents.
- **+** Rich's manual intervention is approval, not configuration — matches the motivation doc's "operator → approver" reframing.
- **−** Requires Graphiti to be reachable for learning to progress (degraded behaviour when unavailable per ADR-ARCH-029).
- **−** First N builds have no priors — reasoning model naturally conservative (emergent training mode per ADR-ARCH-019).
