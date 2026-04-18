# Pipeline status query and stakeholder-facing outcome summary

## Description

Forge should provide a queryable pipeline status view that answers the documented stakeholder question, 'where are we?', using stage state, checkpoint outcome, degraded-mode flags, and artifact links instead of traditional status reporting. The summary should show whether a project is blocked on prerequisites, awaiting approval, building features, running without Graphiti compounding, or ready for PR review, so the system delivers the proactive signals promised by the context-first delivery model.

## Bounded Context

Pipeline Orchestration

## Source Documents

- fleet-master-index.md
- conversation-starter-gap-analysis.md
- big-picture-vision-and-durability.md
- forge-pipeline-orchestrator-refresh.md

## Constraints

- Must support context-first delivery and outcome-gate framing rather than ticket-based reporting
- Must include degraded-mode and checkpoint status in the output
- Must be compatible with the existing Forge identity and `forge_status` tool intent

## Dependencies

- FEAT-PO-001
- FEAT-PO-005

## Suggested Context Files

- forge/docs/research/ideas/fleet-master-index.md
- forge/docs/research/ideas/conversation-starter-gap-analysis.md
- forge/docs/research/ideas/big-picture-vision-and-durability.md
- forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md
