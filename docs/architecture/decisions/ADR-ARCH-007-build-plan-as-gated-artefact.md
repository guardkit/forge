# ADR-ARCH-007: Build Plan as explicit gated artefact with relaxation criteria

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 2 Revision 3

## Context

`fleet-master-index` Pattern 1 formalises **Build Plans** as a first-class artefact across repos: `phase1-build-plan.md`, `phase1b-unified-harness-build-plan.md`, `phase1c-domain-fidelity-build-plan.md`, `buildplan.md` in lpa-platform, etc. A build plan captures the full GuardKit command sequence with `--context` flags, prerequisites, expected outputs, feature dependency graph, and estimated timeline.

Rich wants Forge to produce these automatically between feature-plan and autobuild, gated on Rich's approval during training, then relaxing to confidence-gated as Forge's judgment is validated.

## Decision

- Introduce a pre-declared sub-agent `build_plan_composer` (one of two pre-declared sub-agents — the other is `autobuild_runner`). It synthesises a `buildplan.md` in the canonical Pattern-1 structure, consuming the feature-plan output + context manifests + dependency graph.
- Introduce a new gate mode in `forge.gating`:
  ```python
  class GateMode(str, Enum):
      AUTO_APPROVE = "auto_approve"
      FLAG_FOR_REVIEW = "flag_for_review"
      HARD_STOP = "hard_stop"
      MANDATORY_HUMAN_APPROVAL = "mandatory_human_approval"  # NEW
  ```
  `MANDATORY_HUMAN_APPROVAL` bypasses Coach score entirely — always emits `ApprovalRequestPayload` and enters `interrupt()`. Intended for the build-plan review in early life.
- **Relaxation**: `forge.learning` watches build-plan approval history. When Rich has approved N consecutive plans without edits (default: 10, configurable) with rejection rate <10%, it proposes a `CalibrationAdjustment` swapping the build-plan gate to confidence-gated. Rich confirms; entity lands in Graphiti; future builds evaluate the build plan with the Coach's own score.

## Consequences

- **+** First-class support for Rich's existing Pattern-1 artefact — no retrofit.
- **+** Clear early-life safety story: every build plan reviewed by Rich before autobuild begins.
- **+** Relaxation mechanism aligns with the emergent-training-mode principle (ADR-ARCH-019) — graduated trust based on evidence.
- **+** `MANDATORY_HUMAN_APPROVAL` gate mode is reusable for any other stage Rich wants to lock down early (e.g. first uses of new specialist capabilities).
- **−** Adds one more sub-agent + one more gate mode to the domain model. Acceptable complexity for the value delivered.
- **−** Coach scoring for "is this a good build plan?" requires evaluation criteria that don't exist yet — likely reused from existing criteria weights in specialist-agent (c4_completeness, boundary_clarity) adapted for plans. Out of scope for this ADR; design concern for `/system-design`.
