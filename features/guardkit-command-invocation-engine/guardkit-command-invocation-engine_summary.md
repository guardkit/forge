# Feature Spec Summary: GuardKit Command Invocation Engine (FEAT-FORGE-005)

**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 32 total (3 smoke, 0 regression)
**Assumptions**: 7 total (4 high / 3 medium / 0 low confidence)
**Review required**: No

## Scope

Covers how Forge drives GuardKit subcommands (`/system-arch`, `/system-design`,
`/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`, `/task-work`,
`/task-complete`, and the Graphiti subcommands) as subprocesses: auto-assembling
`--context` flags from `.guardkit/context-manifest.yaml`, streaming progress on
the NATS bus, capturing artefact paths, enforcing worktree and allowlist
confinement via DeepAgents permissions, and converting failures into structured
results that never raise past the tool-layer boundary. Includes the git/gh
adapter surface (worktree lifecycle, commit/push, pull-request creation) that
shares the same subprocess contract.

## Scenario Counts by Category

| Category                       | Count |
|--------------------------------|-------|
| Key examples (@key-example)    | 7     |
| Boundary conditions (@boundary)| 6     |
| Negative cases (@negative)     | 10    |
| Edge cases (@edge-case)        | 14    |
| Smoke (@smoke)                 | 3     |

_(Tags overlap — a scenario tagged `@boundary @negative` is counted once in each category.)_

## Deferred Items

None. All four core groups and the security/concurrency/integration expansion
were accepted without defer.

## Open Assumptions (low confidence)

None. All seven assumptions are high- or medium-confidence and were confirmed
at defaults.

## Grounding References

- API contract (tool layer): `forge/docs/design/contracts/API-tool-layer.md` §6 (GuardKit subcommand tools), §2 (universal error contract)
- API contract (subprocess): `forge/docs/design/contracts/API-subprocess.md` §2 (permissions), §3 (GuardKit adapter), §4 (git/gh), §5 (worktree lifecycle)
- Design decision: `forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md` (resolver placement, missing-manifest behaviour, category filter table, depth-2 cycle guard)
- Build plan: `forge/docs/research/ideas/forge-build-plan.md` FEAT-FORGE-005 scope

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan FEAT-FORGE-005 \
      --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine_summary.md

Per the build plan, FEAT-FORGE-005 depends on FEAT-FORGE-001 only and can be
built in parallel with FEAT-FORGE-002. Step 11 of `/feature-plan` will invoke
the `bdd-linker` subagent to tag each scenario above with the matching
`@task:<TASK-ID>` so the task-level BDD runner can pick them up as
Coach-blocking oracles during `/task-work`.
