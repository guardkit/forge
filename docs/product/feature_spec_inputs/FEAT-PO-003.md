# Context manifest validation and missing-manifest fallback policy

## Description

Forge should validate `.guardkit/context-manifest.yaml` availability and schema quality across target repos before assembling `--context` flags, because the current plan depends on manifests that are not yet complete in all repos. When a required manifest is missing or incomplete, Forge should fall back to a documented degraded policy that identifies which context categories cannot be assembled automatically, flags the pipeline for review, and records the missing dependency map as an operational issue rather than silently proceeding with partial context.

## Bounded Context

Context Resolution

## Source Documents

- forge-build-plan.md
- fleet-master-index.md
- forge-pipeline-orchestrator-refresh.md

## Constraints

- Must use the context manifest convention and category filtering described in the docs
- Must account for lpa-platform and specialist-agent manifests still being incomplete
- Must force review when automated context assembly is degraded

## Dependencies

- FEAT-PO-001

## Suggested Context Files

- forge/docs/research/ideas/forge-build-plan.md
- forge/docs/research/ideas/fleet-master-index.md
- forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md
