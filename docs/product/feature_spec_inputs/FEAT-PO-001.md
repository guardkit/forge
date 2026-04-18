# Prerequisite readiness gate for Forge build start

## Description

Forge should provide an explicit readiness gate that validates all hard prerequisites before `/system-arch` can begin, rather than relying on the build plan checklist being interpreted manually. The gate should verify NATS infrastructure availability, live nats-core integration status, specialist-agent Phase 3 completion, and at least one NATS-callable specialist role, then produce a machine-readable readiness report that explains which blocking conditions remain and why the build cannot start.

## Bounded Context

Pipeline Orchestration

## Source Documents

- forge-build-plan.md
- conversation-starter-gap-analysis.md
- fleet-master-index.md

## Constraints

- Must enforce the hard prerequisites listed before Step 1 in the existing build plan
- Must distinguish blocking prerequisites from soft prerequisites
- Must not start Forge build stages when specialist-agent Phase 3 or live NATS validation is missing

## Dependencies

None

## Suggested Context Files

- forge/docs/research/ideas/forge-build-plan.md
- forge/docs/research/ideas/fleet-master-index.md
- forge/docs/research/ideas/conversation-starter-gap-analysis.md
