# Build plan placeholder resolution and command-history enforcement

## Description

After `/system-arch`, `/system-design`, and each `/feature-spec` session, Forge should detect unresolved placeholders in the build plan and require them to be replaced with concrete file paths before downstream steps continue. It should also enforce creation and updating of `command-history.md` and `feature-spec-FEAT-FORGE-XXX-history.md` so that the formalised history patterns documented in the fleet are captured as part of execution, not left as optional housekeeping.

## Bounded Context

Delivery Workflow Governance

## Source Documents

- forge-build-plan.md
- fleet-master-index.md
- forge-ideas-overhaul-conversation-starter.md

## Constraints

- Must remove `<placeholder>` drift before later command steps are run
- Must follow the documented command history and feature spec history patterns
- Must preserve the build plan as the authoritative execution record

## Dependencies

- FEAT-PO-001

## Suggested Context Files

- forge/docs/research/ideas/forge-build-plan.md
- forge/docs/research/ideas/fleet-master-index.md
- forge/docs/research/ideas/forge-ideas-overhaul-conversation-starter.md
