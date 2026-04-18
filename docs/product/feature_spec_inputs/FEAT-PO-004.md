# Graphiti-optional coordination with explicit knowledge-compounding status

## Description

Forge should treat Graphiti coordination as an explicit optional capability rather than a silent best-effort integration. When specialist-agent Phase G or Graphiti runtime support is unavailable, Forge should continue the pipeline but publish and persist a clear status that knowledge seeding, cross-project querying, and compounding metrics were skipped, so operators understand that the pipeline ran in a non-compounding mode rather than assuming the knowledge layer was active.

## Bounded Context

Infrastructure Coordination

## Source Documents

- forge-build-plan.md
- conversation-starter-gap-analysis.md
- big-picture-vision-and-durability.md
- fleet-master-index.md

## Constraints

- Must honour the build plan rule that Graphiti runtime is valuable but not blocking
- Must not claim knowledge compounding when Phase G is not built
- Must preserve pipeline continuity while increasing operator visibility

## Dependencies

- FEAT-PO-001

## Suggested Context Files

- forge/docs/research/ideas/forge-build-plan.md
- forge/docs/research/ideas/conversation-starter-gap-analysis.md
- forge/docs/research/ideas/big-picture-vision-and-durability.md
- forge/docs/research/ideas/fleet-master-index.md
