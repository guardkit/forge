/feature-spec FEAT-FORGE-003 \
  --context forge/docs/design/contracts/API-nats-agent-dispatch.md \
  --context forge/docs/design/models/DM-discovery.md \
  --context forge/docs/design/decisions/DDR-001-reply-subject-correlation.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md

 I'll execute the /feature-spec command for FEAT-FORGE-003. Let me start with Phase 1: Context Gathering.

Bash List project structure
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/ 2>&1 | head -30 && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/ 2>&1 | head -20

OUT
AGENTS.md
command_history.md
docs
features
langgraph.json
LICENSE
mcp.json
orchestrator-config.yaml
place-documents.sh
pyproject.toml
README.md
spikes
src
tasks
---
agentic-dataset-factory
api_test
docling-dgx-spark-scripts
forge
graphiti
guardkit
logiops
nats-core
nats-infrastructure
specialist-agent
vllm-profiling
Bash Detect stack
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 3 -type f \( -name "pyproject.toml" -o -name "setup.py" -o -name "requirements.txt" -o -name "go.mod" -o -name "Cargo.toml" -o -name "*.csproj" -o -name "*.sln" -o -name "package.json" \) 2>&1 | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/pyproject.toml
Bash Inspect features, tasks, docs
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/ 2>&1 | head -30 && echo "---TASKS---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/tasks/ 2>&1 | head -40 && echo "---DOCS---" && find /home/richardwoollcott/Projects/appmilla_github/forge/docs -maxdepth 3 -type d 2>&1 | head -30

OUT
nats-fleet-integration
pipeline-state-machine-and-configuration
---TASKS---
backlog
completed
in_review
---DOCS---
/home/richardwoollcott/Projects/appmilla_github/forge/docs
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product
/home/richardwoollcott/Projects/appmilla_github/forge/docs/state
/home/richardwoollcott/Projects/appmilla_github/forge/docs/adr
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design
/home/richardwoollcott/Projects/appmilla_github/forge/docs/reviews
/home/richardwoollcott/Projects/appmilla_github/forge/docs/history
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/diagrams
/home/richardwoollcott/Projects/appmilla_github/forge/docs/reviews/architecture
Bash Find FEAT-FORGE-003 files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 5 -type f -name "FEAT-FORGE-003*" 2>&1 | head -20 && echo "---" && find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 5 -type d -name "FEAT-FORGE-003*" 2>&1 | head -10

OUT
---
Bash Look for FORGE-003 and spec inputs
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -type f -iname "*FORGE-003*" 2>&1 | head -20 && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/product/ 2>&1 && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/ 2>&1

OUT
---
feature_spec_inputs
roadmap.md
---
FEAT-PO-001.md
FEAT-PO-002.md
FEAT-PO-003.md
FEAT-PO-004.md
FEAT-PO-005.md
FEAT-PO-006.md
Read roadmap.md
Bash Inspect existing feature specs
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/ 2>&1 && echo "---PIPELINE---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/ 2>&1

OUT
nats-fleet-integration_assumptions.yaml
nats-fleet-integration.feature
nats-fleet-integration_summary.md
---PIPELINE---
pipeline-state-machine-and-configuration_assumptions.yaml
pipeline-state-machine-and-configuration.feature
pipeline-state-machine-and-configuration_summary.md
Bash Search for FEAT-FORGE-003 references
IN
grep -r "FEAT-FORGE-003" /home/richardwoollcott/Projects/appmilla_github/forge/ --include="*.md" -l 2>&1 | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/forge-build-plan-alignment-review.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/forge-build-plan-alignment-review.md
Bash Inspect design docs
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ 2>&1 && echo "---MODELS---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/ 2>&1 && echo "---DECISIONS---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/ 2>&1

OUT
API-cli.md
API-nats-agent-dispatch.md
API-nats-approval-protocol.md
API-nats-fleet-lifecycle.md
API-nats-pipeline-events.md
API-sqlite-schema.md
API-subagents.md
API-subprocess.md
API-tool-layer.md
---MODELS---
DM-build-lifecycle.md
DM-calibration.md
DM-discovery.md
DM-gating.md
DM-graphiti-entities.md
---DECISIONS---
DDR-001-reply-subject-correlation.md
DDR-002-resume-value-rehydration-helper.md
DDR-003-sqlite-schema-layout-wal.md
DDR-004-graphiti-group-partitioning.md
DDR-005-cli-context-manifest-resolution.md
DDR-006-async-subagent-state-channel-contract.md
Bash Find FEAT-FORGE-003 definition
IN
grep -l "FEAT-FORGE-003" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md 2>&1 && echo "---" && grep -n "FEAT-FORGE-003" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md 2>&1 | head -30

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md
---
128:| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles (ADR-SP-015), result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure | NATS command invocation of specialist agents (Stage 2) |
296:# FEAT-FORGE-003: Specialist Agent Delegation
297:guardkit feature-spec FEAT-FORGE-003 \
368:# FEAT-FORGE-003: Specialist Agent Delegation (depends on 002)
369:guardkit feature-plan FEAT-FORGE-003 \
413:guardkit autobuild FEAT-FORGE-003
Read forge-build-plan.md (lines 281-360)
Read forge-build-plan.md (lines 111-280)
Now I understand FEAT-FORGE-003. Let me read the context files in parallel.

Read API-nats-agent-dispatch.md
Read DM-discovery.md
Read DDR-001-reply-subject-correlation.md
Bash Check nats-core spec file
IN
ls /home/richardwoollcott/Projects/appmilla_github/nats-core/docs/design/specs/ 2>&1 && echo "---" && wc -l /home/richardwoollcott/Projects/appmilla_github/nats-core/docs/design/specs/nats-core-system-spec.md 2>&1

OUT
nats-core-system-spec.md
---
719 /home/richardwoollcott/Projects/appmilla_github/nats-core/docs/design/specs/nats-core-system-spec.md
Read nats-core-system-spec.md (lines 2-201)
Read nats-core-system-spec.md (lines 201-500)
Read nats-fleet-integration.feature (lines 2-81)
Read nats-fleet-integration_summary.md
Bash Find ADR-ARCH-015
IN
grep -l "dispatch_by_capability\|ADR-ARCH-015" /home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ 2>&1 | head -5 && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-015* 2>&1

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-012-no-mcp-interface.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-017-live-fleet-watching.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-016-fleet-is-the-catalogue.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-020-adopt-deepagents-builtins.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-015-capability-driven-dispatch.md
---
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-015-capability-driven-dispatch.md
Read ADR-ARCH-015-capability-driven-dispatch.md
Phase 1: Context Gathering
Context loaded: stack=python, 2 existing .feature files (nats-fleet-integration, pipeline-state-machine-and-configuration), 4 context files read (API-nats-agent-dispatch, DM-discovery, DDR-001, nats-core-system-spec), 1 parent ADR (ADR-ARCH-015 capability-driven dispatch).

FEAT-FORGE-003 scope (from forge-build-plan.md §128): Specialist Agent Delegation — dispatch_by_capability() tool for product-owner and architect roles per ADR-ARCH-015, LES1-parity reply-subject correlation (DDR-001), Coach score + criterion breakdown + detection findings parsing from ResultPayload.result, timeout handling, retry with additional context on failure, capability resolution via forge.discovery with intent-pattern fallback and tie-breaks.

Phase 2: Initial Proposal

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Specialist Agent Delegation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@feat-forge-003 @specialist-agent-delegation
Feature: Specialist Agent Delegation
  As Rich, the sole operator of Forge
  I want Forge to delegate domain judgement to fleet specialist agents via a single
    capability-driven dispatch path, with an unambiguous reply contract and a clear
    degraded-mode fallback when no specialist is available
  So that new specialist roles are reachable without code changes, silent round-trip
    failures from the LES1 class are prevented, and every dispatched decision feeds
    the gating layer with a Coach score, criterion breakdown, and detection findings

  Background:
    Given Forge is registered with the fleet and is watching fleet lifecycle events
    And the live capability cache is fresh
━━ GROUP A: Key Examples (5 scenarios) ━━


  # Why: Core happy-path — exact tool match, round-trip reply, gating input formed
  @key-example @smoke
  Scenario: Forge delegates a stage to a specialist advertising the exact tool
    Given a specialist agent advertises a tool matching the stage's requested capability
    When Forge dispatches the stage to that capability
    Then Forge should subscribe to a correlation-keyed reply channel before publishing the command
    And the specialist should publish its result on that correlation-keyed channel
    And Forge should feed the coach score, criterion breakdown, and detection findings into the gating layer

  # Why: Intent-pattern fallback — resolution still succeeds when no exact tool match exists
  @key-example
  Scenario: Forge falls back to intent-pattern matching when no tool match exists
    Given no specialist advertises the requested tool by exact name
    And a specialist advertises an intent pattern that covers the request at sufficient confidence
    When Forge resolves the capability
    Then Forge should select the intent-matching specialist
    And the resulting resolution record should mark the match source as an intent-pattern match

  # Why: Result parsing — Coach output may live at the top level or nested under result
  @key-example @smoke
  Scenario: Forge reads Coach output preferring top-level fields over nested result fields
    Given a specialist returns a result carrying both top-level Coach fields and nested Coach fields
    When Forge parses the reply
    Then Forge should use the top-level coach score, criterion breakdown, and detection findings
    And the nested fields should be retained only as fallback evidence

  # Why: Retry path — reasoning loop can re-dispatch with additional context after a soft failure
  @key-example
  Scenario: Forge retries a failed dispatch with additional context on the second attempt
    Given a first dispatch to a specialist returns an error result
    When Forge decides to retry the stage with additional context
    Then Forge should issue a new dispatch with a fresh correlation
    And the additional context should be carried in the retry command
    And the retry attempt should be recorded alongside the original attempt

  # Why: Outcome correlation — discovery records are linked back to their downstream outcome
  @key-example
  Scenario: Forge links each capability resolution to its downstream outcome
    Given Forge has dispatched to a resolved specialist
    When the specialist's reply is received and the gate decision is produced
    Then the resolution record should be linked to that gate decision as its outcome
    And the resolution record should be marked as having its outcome correlated
━━ GROUP B: Boundary Conditions (6 scenarios) ━━


  # Why: Just-inside boundary — intent match at exactly the minimum confidence is accepted
  # [ASSUMPTION: confidence=high] Intent-fallback minimum confidence is 0.7 (DM-discovery §3)
  @boundary
  Scenario: An intent match at exactly the minimum confidence is accepted
    Given no specialist advertises the requested tool by exact name
    And a single specialist advertises the intent at exactly the minimum confidence threshold
    When Forge resolves the capability
    Then the specialist should be selected
    And the resolution should record the match source as an intent-pattern match

  # Why: Just-outside boundary — intent match below threshold is rejected
  @boundary @negative
  Scenario: An intent match below the minimum confidence is treated as unresolved
    Given no specialist advertises the requested tool by exact name
    And the only intent-pattern match is just below the minimum confidence threshold
    When Forge resolves the capability
    Then no specialist should be selected
    And the resolution should record the match source as unresolved

  # Why: Just-inside boundary — reply arriving inside the local timeout is accepted
  # [ASSUMPTION: confidence=high] Forge-side dispatch timeout default is 900 seconds (API-nats-agent-dispatch §5)
  @boundary
  Scenario: A reply received just before the local timeout succeeds
    Given Forge has dispatched a command with the default local timeout
    When the specialist's reply arrives just before the local timeout would have fired
    Then the reply should be accepted as the authoritative outcome
    And no timeout handling should run

  # Why: Just-outside boundary — reply arriving after the local timeout is not honoured
  @boundary @negative
  Scenario: A reply received after the local timeout is not used
    Given Forge has dispatched a command with the default local timeout
    When no reply arrives before the local timeout fires
    Then Forge should synthesise a timeout result for the reasoning loop
    And any reply that arrives afterwards should be ignored

  # Why: Tie-break by trust tier — core beats specialist beats extension at equal confidence
  @boundary
  Scenario Outline: Trust-tier ranking decides between otherwise-equivalent specialists
    Given two specialists advertise the same capability at the same confidence and queue depth
    And one is in the <higher_tier> trust tier and the other is in the <lower_tier> trust tier
    When Forge resolves the capability
    Then Forge should select the <higher_tier> specialist
    And the non-selected specialist should appear in the competing agents list

    Examples:
      | higher_tier | lower_tier |
      | core        | specialist |
      | specialist  | extension  |

  # Why: Tie-break by queue depth — lowest queue wins at equal tier and confidence
  @boundary
  Scenario: Lowest queue depth wins when trust tier and confidence are equal
    Given two specialists advertise the same capability at the same trust tier and confidence
    And one specialist has a lighter queue than the other
    When Forge resolves the capability
    Then Forge should select the specialist with the lighter queue
    And the non-selected specialist should appear in the competing agents list
━━ GROUP C: Negative Cases (7 scenarios) ━━


  # Why: Unresolved capability — no specialist matches and reasoning must decide next move
  @negative
  Scenario: No matching specialist triggers the degraded reasoning path
    Given no specialist advertises the requested tool or an intent-pattern match
    When Forge attempts to dispatch the stage
    Then Forge should synthesise a degraded result carrying the unresolved capability description
    And the reasoning loop should receive the degraded result as the outcome of the stage

  # Why: Degraded agents are excluded — a specialist that reports degraded status is not selected
  @negative
  Scenario: A specialist reporting degraded status is excluded from resolution
    Given only one specialist advertises the requested capability
    And that specialist's latest heartbeat reports a degraded status
    When Forge resolves the capability
    Then the specialist should not be selected
    And the resolution should record the match source as unresolved

  # Why: Specialist error — the specialist answered, but the outcome is an error
  @negative
  Scenario: A specialist error result is surfaced to the reasoning loop
    Given Forge has dispatched a command to a specialist
    When the specialist replies with an error result carrying an explanation
    Then the error explanation should be visible to the reasoning loop
    And Forge should not auto-approve the stage on the error result

  # Why: PubAck is not success — the audit stream may ack quickly and Forge must still wait for the reply
  @negative
  Scenario: A fast publish acknowledgement is not treated as the specialist's answer
    Given Forge has subscribed to the correlation-keyed reply channel before publishing
    When the fleet bus acknowledges the publish quickly but no specialist reply has arrived
    Then Forge should continue waiting for the specialist's reply on the correlation-keyed channel
    And the publish acknowledgement should not be recorded as a successful dispatch

  # Why: Wrong correlation — a stray reply for a different request must not be consumed
  @negative
  Scenario: A reply addressed to a different correlation is not consumed
    Given Forge is waiting on a correlation-keyed reply channel for a specific request
    When a reply for a different request is published on a different correlation-keyed channel
    Then Forge should not consume that reply
    And Forge should continue waiting for the reply matching its own correlation

  # Why: Missing Coach output — without a Coach score the gate cannot auto-approve
  @negative
  Scenario: A reply that omits the Coach score cannot auto-approve the stage
    Given a specialist replies with a successful result that carries no Coach score
    When Forge evaluates the stage's gate
    Then the gate should default to flagging the stage for human review
    And the reason for the flag should cite the missing Coach score

  # Why: Malformed reply — a reply that fails schema validation is treated as an error, not success
  @negative
  Scenario: A malformed reply envelope is treated as an error outcome
    Given a reply arrives on the correlation-keyed channel but fails envelope validation
    When Forge observes the reply
    Then Forge should treat the dispatch as failed with a schema-validation reason
    And Forge should not feed the malformed payload into the gating layer
━━ GROUP D: Edge Cases (7 scenarios) ━━


  # Why: Write-before-send invariant — the resolution record is persisted before the command is published
  @edge-case
  Scenario: The capability resolution is persisted before the command is published
    Given Forge is about to dispatch to a resolved specialist
    When Forge publishes the dispatch command
    Then the capability resolution record should already be persisted
    And the persisted resolution should reference the agent that the command was sent to

  # Why: Subscribe-before-publish invariant — per LES1, the reply channel must exist before the command goes out
  @edge-case
  Scenario: The reply subscription is established before the command is published
    Given Forge is about to dispatch to a resolved specialist
    When Forge publishes the dispatch command
    Then the subscription to the correlation-keyed reply channel should already be active
    And it should remain active until either the reply arrives or the local timeout fires

  # Why: Unsubscribe on timeout — the one-shot subscription is released to prevent leaks
  @edge-case
  Scenario: The correlation-keyed subscription is released after a timeout
    Given Forge has dispatched a command and begun waiting for a reply
    When the local timeout fires before any reply arrives
    Then Forge should release the correlation-keyed subscription
    And any subsequent late reply on that channel should not be delivered to Forge

  # Why: Cache freshness — a specialist joining mid-build becomes resolvable without restart
  @edge-case
  Scenario: A specialist that joins during a build becomes resolvable on the next resolution
    Given Forge has already resolved an earlier stage against the current fleet
    When a new specialist registers that advertises a capability Forge will need
    And Forge resolves the next stage
    Then the newly-registered specialist should be considered during resolution

  # Why: Cache invalidation — a specialist that leaves must not be selected afterwards
  @edge-case
  Scenario: A specialist that deregisters after an earlier stage is not selected for the next one
    Given the specialist that served an earlier stage has deregistered
    When Forge resolves the next stage against the same capability
    Then the deregistered specialist should not be selected
    And resolution should fall back to another matching specialist or to the unresolved path

  # Why: Async-mode dispatch — long-running work returns a run identifier to be polled
  @edge-case
  Scenario: An async-mode dispatch returns a run identifier for later polling
    Given the resolved specialist advertises the capability as long-running
    When Forge dispatches the command
    Then the specialist's first reply should carry a run identifier rather than a final result
    And Forge should poll for the final result using the capability's status tool

  # Why: Concurrent dispatches — two simultaneous dispatches must not cross wires
  @edge-case @concurrency
  Scenario: Two concurrent dispatches to the same specialist keep their replies separate
    Given Forge has two stages ready to dispatch at the same time
    And both stages resolve to the same specialist
    When Forge publishes both commands
    Then each dispatch should subscribe to its own correlation-keyed reply channel
    And each reply should be delivered to the dispatch that issued its correlation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 25 scenarios across 4 groups
Inferred assumptions: 6 (will be resolved in Phase 5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Phase 3: Human Curation

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review each group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For each group, you may:

  [A] Accept  — include all scenarios in this group as-is
  [R] Reject  — exclude all scenarios in this group
  [M] Modify  — accept but specify changes (provide inline)
  [+] Add     — accept and add additional scenarios you describe
  [?] Defer   — mark group for future specification; excluded from output

Fast path: type "A A A A" to accept all four groups at once.

GROUP A — Key Examples (5 scenarios): _
GROUP B — Boundary Conditions (6 scenarios): _
GROUP C — Negative Cases (7 scenarios): _
GROUP D — Edge Cases (7 scenarios): _
Please respond with your curation decisions for each group (e.g. A A A A to fast-accept all four, or per-group A/R/M/+/? with inline notes for modifications or additions). After curation I'll offer Phase 4 (edge case expansion — security, concurrency, integration boundaries) and then Phase 5 (assumption resolution) before writing the output files.

forge-build-plan.md
A A A A
All four groups accepted. Proceeding to Phase 4.

Phase 4: Edge Case Expansion

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
I've identified 8 additional scenarios covering security, concurrency, data integrity, and integration boundaries specific to specialist agent dispatch.

Security (3):

A reply from a source other than the resolved specialist is not trusted — only the registered agent's reply satisfies the dispatch
Dispatch parameters containing secrets are not written to the capability resolution record or Graphiti history
An agent whose trust tier is extension cannot shadow a core specialist for the same exact tool name
Concurrency (2):

A fleet event invalidates the cache while a resolution is in flight — the in-flight dispatch keeps the manifest snapshot it already read, the next resolution sees the update
Two simultaneous resolutions observe a tied candidate set consistently — tie-break ordering is deterministic across concurrent callers
Data integrity (1):

A specialist replies twice on the same correlation-keyed channel — only the first reply counts; any second reply is ignored
Integration boundaries (2):

The fleet bus disconnects while Forge is waiting for a reply — the dispatch fails cleanly into the reasoning loop as a timeout-equivalent, not as a silent hang
The registry read itself fails during cache refresh — Forge falls back to the last-known manifest snapshot while the stale warning is surfaced to the reasoning loop

[Y] Yes — show and curate them (same A/R/M/+/? actions)
[S] Sample — show 2 examples, then decide
[N] No — skip and proceed to Phase 5

Your choice [Y/S/N]:
Y
Here are the full GROUP E scenarios for curation.

━━ GROUP E: Security / Concurrency / Data Integrity / Integration (8 scenarios) ━━


  # Why: Reply authenticity — only the resolved specialist's reply completes the round-trip
  @edge-case @security
  Scenario: A reply from a source other than the resolved specialist is not trusted
    Given Forge has dispatched a command to a specific resolved specialist
    When a reply arrives on the correlation-keyed channel whose source identifier is a different agent
    Then Forge should not accept the reply as the specialist's answer
    And Forge should continue to wait for the resolved specialist's reply or for the local timeout

  # Why: Secret hygiene — dispatched parameters may carry sensitive details that must not be persisted
  @edge-case @security
  Scenario: Dispatch parameters flagged as sensitive are not persisted in the resolution record
    Given a stage dispatches parameters that include fields marked as sensitive
    When Forge records the capability resolution
    Then the persisted resolution should not contain the sensitive parameter values
    And the pipeline history view of this dispatch should describe only non-sensitive fields

  # Why: Trust-tier is authoritative — a lower-trust extension cannot override a core specialist for the same tool
  @edge-case @security
  Scenario: An extension-tier specialist cannot shadow a core-tier specialist for the same tool
    Given a core-tier specialist and an extension-tier specialist both advertise the same tool
    When Forge resolves the capability
    Then the core-tier specialist should be selected
    And the extension-tier specialist should appear only in the competing agents list

  # Why: Snapshot stability — an in-flight resolution must not see a cache swap mid-call
  @edge-case @concurrency
  Scenario: A fleet event that invalidates the cache does not disturb an in-flight resolution
    Given Forge is in the middle of resolving a capability against the current fleet snapshot
    When a fleet event arrives that invalidates the cache
    Then the in-flight resolution should complete against the snapshot it already read
    And the next resolution after the in-flight one should use the refreshed snapshot

  # Why: Determinism — concurrent resolutions with the same inputs must produce the same choice
  @edge-case @concurrency
  Scenario: Two concurrent resolutions for the same capability produce the same choice
    Given two stages resolve the same capability at the same time against the same fleet snapshot
    When both resolutions complete
    Then both should select the same specialist
    And the competing-agents list should be consistent across the two resolutions

  # Why: Exactly-once delivery — a duplicate reply on the same correlation is ignored
  @edge-case @data-integrity
  Scenario: A duplicate reply on the same correlation-keyed channel is ignored
    Given Forge has already accepted a reply on a correlation-keyed channel
    When a second reply for the same correlation is published afterwards
    Then Forge should not reprocess the duplicate reply
    And the gate decision derived from the first reply should stand

  # Why: Bus outage — a disconnect while waiting must not hang the pipeline silently
  @edge-case @integration
  Scenario: The fleet bus disconnects while Forge is waiting for a reply
    Given Forge has dispatched a command and is waiting for the specialist's reply
    When the fleet bus becomes unreachable before the reply arrives
    Then Forge should treat the dispatch as failed and surface the outcome to the reasoning loop
    And the pipeline should not wait indefinitely for a reply that cannot arrive

  # Why: Registry outage — Forge keeps serving from the last known snapshot rather than stalling
  @edge-case @integration
  Scenario: A registry read failure during cache refresh falls back to the last-known snapshot
    Given the live fleet registry is temporarily unreadable
    When Forge needs to resolve a capability and its cache has just expired
    Then Forge should resolve against the last-known fleet snapshot
    And the reasoning loop should be informed that the fleet view may be stale

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Group E
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP E — Security / Concurrency / Data Integrity / Integration (8 scenarios): _
Accept (A), reject (R), modify (M + notes), add (+ + notes), or defer (? + notes)?

A
Group E accepted. Proceeding to Phase 5.

Phase 5: Assumption Resolution

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (6 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter to accept,
or type the actual value.
[1] Intent-fallback minimum confidence

Proposed: 0.7
Confidence: high
Basis: forge.discovery.resolve(..., min_confidence=0.7) in DM-discovery §3 and API-nats-agent-dispatch §2
Accept or enter value: _
[2] CommandPayload advisory timeout (specialist-side)

Proposed: 600 seconds (10 min)
Confidence: high
Basis: CommandPayload.timeout_seconds = 600 default in API-nats-agent-dispatch §3.2
Accept or enter value: _
[3] Forge-side hard dispatch timeout (local subscribe cut-off)

Proposed: 900 seconds (15 min; 50% headroom over CommandPayload default)
Confidence: high
Basis: forge.yaml.dispatch.default_timeout_seconds = 900 in API-nats-agent-dispatch §5
Accept or enter value: _
[4] Discovery cache TTL

Proposed: 30 seconds
Confidence: high
Basis: DM-discovery §1 and API-nats-agent-dispatch §2
Accept or enter value: _
[5] Retry policy on soft-failure dispatch

Proposed: Reasoning-model-driven — retry decision and additional-context construction are the reasoning model's responsibility; no fixed max-retry count is enforced at the dispatch layer
Confidence: medium
Basis: Build plan §128 says "retry with additional context on failure" without specifying a count; ADR-ARCH-015 places the decision in the reasoning loop
Accept or enter value: _
[6] Gate fallback when Coach score is absent

Proposed: FLAG_FOR_REVIEW (no auto-approve, no hard stop)
Confidence: high
Basis: API-nats-agent-dispatch §6 "no Coach score → no auto-approve"; pipeline orchestrator refresh §"Degraded Mode"
Accept or enter value: _

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Please respond per-item (e.g. 1: accept, 2: accept, ...) or accept all to take every proposed default. You can also override any individual value inline (e.g. 3: 1200).

accept defaults
All defaults accepted. Writing output files now.

Bash Create output directory
IN
mkdir -p /home/richardwoollcott/Projects/appmilla_github/forge/features/specialist-agent-delegation && ls /home/richardwoollcott/Projects/appmilla_github/forge/features/specialist-agent-delegation 2>&1

OUT
(Bash completed with no output)
Write specialist-agent-delegation.feature
324 lines
# Generated by /feature-spec
# Feature: Specialist Agent Delegation (FEAT-FORGE-003)
# Stack: python
# Assumptions: 6 (see specialist-agent-delegation_assumptions.yaml)
# Generated: 2026-04-24T00:00:00Z

@feat-forge-003 @specialist-agent-delegation
Feature: Specialist Agent Delegation
  As Rich, the sole operator of Forge
  I want Forge to delegate domain judgement to fleet specialist agents via a single
    capability-driven dispatch path, with an unambiguous reply contract and a clear
    degraded-mode fallback when no specialist is available
  So that new specialist roles are reachable without code changes, silent round-trip
    failures from the LES1 class are prevented, and every dispatched decision feeds
    the gating layer with a Coach score, criterion breakdown, and detection findings

  Background:
    Given Forge is registered with the fleet and is watching fleet lifecycle events
    And the live capability cache is fresh

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core happy-path — exact tool match, round-trip reply, gating input formed
  @key-example @smoke
  Scenario: Forge delegates a stage to a specialist advertising the exact tool
    Given a specialist agent advertises a tool matching the stage's requested capability
    When Forge dispatches the stage to that capability
    Then Forge should subscribe to a correlation-keyed reply channel before publishing the command
    And the specialist should publish its result on that correlation-keyed channel
    And Forge should feed the coach score, criterion breakdown, and detection findings into the gating layer

  # Why: Intent-pattern fallback — resolution still succeeds when no exact tool match exists
  @key-example
  Scenario: Forge falls back to intent-pattern matching when no tool match exists
    Given no specialist advertises the requested tool by exact name
    And a specialist advertises an intent pattern that covers the request at sufficient confidence
    When Forge resolves the capability
    Then Forge should select the intent-matching specialist
    And the resulting resolution record should mark the match source as an intent-pattern match

  # Why: Result parsing — Coach output may live at the top level or nested under result
  @key-example @smoke
  Scenario: Forge reads Coach output preferring top-level fields over nested result fields
    Given a specialist returns a result carrying both top-level Coach fields and nested Coach fields
    When Forge parses the reply
    Then Forge should use the top-level coach score, criterion breakdown, and detection findings
    And the nested fields should be retained only as fallback evidence

  # Why: Retry path — reasoning loop can re-dispatch with additional context after a soft failure
  # [ASSUMPTION: confidence=medium] Retry is reasoning-model-driven; no fixed max-retry count is enforced at the dispatch layer
  @key-example
  Scenario: Forge retries a failed dispatch with additional context on the second attempt
    Given a first dispatch to a specialist returns an error result
    When Forge decides to retry the stage with additional context
    Then Forge should issue a new dispatch with a fresh correlation
    And the additional context should be carried in the retry command
    And the retry attempt should be recorded alongside the original attempt

  # Why: Outcome correlation — discovery records are linked back to their downstream outcome
  @key-example
  Scenario: Forge links each capability resolution to its downstream outcome
    Given Forge has dispatched to a resolved specialist
    When the specialist's reply is received and the gate decision is produced
    Then the resolution record should be linked to that gate decision as its outcome
    And the resolution record should be marked as having its outcome correlated

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Just-inside boundary — intent match at exactly the minimum confidence is accepted
  # [ASSUMPTION: confidence=high] Intent-fallback minimum confidence is 0.7 (DM-discovery §3)
  @boundary
  Scenario: An intent match at exactly the minimum confidence is accepted
    Given no specialist advertises the requested tool by exact name
    And a single specialist advertises the intent at exactly the minimum confidence threshold
    When Forge resolves the capability
    Then the specialist should be selected
    And the resolution should record the match source as an intent-pattern match

  # Why: Just-outside boundary — intent match below threshold is rejected
  @boundary @negative
  Scenario: An intent match below the minimum confidence is treated as unresolved
    Given no specialist advertises the requested tool by exact name
    And the only intent-pattern match is just below the minimum confidence threshold
    When Forge resolves the capability
    Then no specialist should be selected
    And the resolution should record the match source as unresolved

  # Why: Just-inside boundary — reply arriving inside the local timeout is accepted
  # [ASSUMPTION: confidence=high] Forge-side hard dispatch timeout default is 900 seconds (API-nats-agent-dispatch §5)
  @boundary
  Scenario: A reply received just before the local timeout succeeds
    Given Forge has dispatched a command with the default local timeout
    When the specialist's reply arrives just before the local timeout would have fired
    Then the reply should be accepted as the authoritative outcome
    And no timeout handling should run

  # Why: Just-outside boundary — reply arriving after the local timeout is not honoured
  @boundary @negative
  Scenario: A reply received after the local timeout is not used
    Given Forge has dispatched a command with the default local timeout
    When no reply arrives before the local timeout fires
    Then Forge should synthesise a timeout result for the reasoning loop
    And any reply that arrives afterwards should be ignored

  # Why: Tie-break by trust tier — core beats specialist beats extension at equal confidence
  @boundary
  Scenario Outline: Trust-tier ranking decides between otherwise-equivalent specialists
    Given two specialists advertise the same capability at the same confidence and queue depth
    And one is in the <higher_tier> trust tier and the other is in the <lower_tier> trust tier
    When Forge resolves the capability
    Then Forge should select the <higher_tier> specialist
    And the non-selected specialist should appear in the competing agents list

    Examples:
      | higher_tier | lower_tier |
      | core        | specialist |
      | specialist  | extension  |

  # Why: Tie-break by queue depth — lowest queue wins at equal tier and confidence
  @boundary
  Scenario: Lowest queue depth wins when trust tier and confidence are equal
    Given two specialists advertise the same capability at the same trust tier and confidence
    And one specialist has a lighter queue than the other
    When Forge resolves the capability
    Then Forge should select the specialist with the lighter queue
    And the non-selected specialist should appear in the competing agents list

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Unresolved capability — no specialist matches and reasoning must decide next move
  @negative
  Scenario: No matching specialist triggers the degraded reasoning path
    Given no specialist advertises the requested tool or an intent-pattern match
    When Forge attempts to dispatch the stage
    Then Forge should synthesise a degraded result carrying the unresolved capability description
    And the reasoning loop should receive the degraded result as the outcome of the stage

  # Why: Degraded agents are excluded — a specialist that reports degraded status is not selected
  @negative
  Scenario: A specialist reporting degraded status is excluded from resolution
    Given only one specialist advertises the requested capability
    And that specialist's latest heartbeat reports a degraded status
    When Forge resolves the capability
    Then the specialist should not be selected
    And the resolution should record the match source as unresolved

  # Why: Specialist error — the specialist answered, but the outcome is an error
  @negative
  Scenario: A specialist error result is surfaced to the reasoning loop
    Given Forge has dispatched a command to a specialist
    When the specialist replies with an error result carrying an explanation
    Then the error explanation should be visible to the reasoning loop
    And Forge should not auto-approve the stage on the error result

  # Why: PubAck is not success — the audit stream may ack quickly and Forge must still wait for the reply
  @negative
  Scenario: A fast publish acknowledgement is not treated as the specialist's answer
    Given Forge has subscribed to the correlation-keyed reply channel before publishing
    When the fleet bus acknowledges the publish quickly but no specialist reply has arrived
    Then Forge should continue waiting for the specialist's reply on the correlation-keyed channel
    And the publish acknowledgement should not be recorded as a successful dispatch

  # Why: Wrong correlation — a stray reply for a different request must not be consumed
  @negative
  Scenario: A reply addressed to a different correlation is not consumed
    Given Forge is waiting on a correlation-keyed reply channel for a specific request
    When a reply for a different request is published on a different correlation-keyed channel
    Then Forge should not consume that reply
    And Forge should continue waiting for the reply matching its own correlation

  # Why: Missing Coach output — without a Coach score the gate cannot auto-approve
  # [ASSUMPTION: confidence=high] Gate fallback when Coach score is absent is FLAG_FOR_REVIEW (API-nats-agent-dispatch §6)
  @negative
  Scenario: A reply that omits the Coach score cannot auto-approve the stage
    Given a specialist replies with a successful result that carries no Coach score
    When Forge evaluates the stage's gate
    Then the gate should default to flagging the stage for human review
    And the reason for the flag should cite the missing Coach score

  # Why: Malformed reply — a reply that fails schema validation is treated as an error, not success
  @negative
  Scenario: A malformed reply envelope is treated as an error outcome
    Given a reply arrives on the correlation-keyed channel but fails envelope validation
    When Forge observes the reply
    Then Forge should treat the dispatch as failed with a schema-validation reason
    And Forge should not feed the malformed payload into the gating layer

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Write-before-send invariant — the resolution record is persisted before the command is published
  @edge-case
  Scenario: The capability resolution is persisted before the command is published
    Given Forge is about to dispatch to a resolved specialist
    When Forge publishes the dispatch command
    Then the capability resolution record should already be persisted
    And the persisted resolution should reference the agent that the command was sent to

  # Why: Subscribe-before-publish invariant — per LES1, the reply channel must exist before the command goes out
  @edge-case
  Scenario: The reply subscription is established before the command is published
    Given Forge is about to dispatch to a resolved specialist
    When Forge publishes the dispatch command
    Then the subscription to the correlation-keyed reply channel should already be active
    And it should remain active until either the reply arrives or the local timeout fires

  # Why: Unsubscribe on timeout — the one-shot subscription is released to prevent leaks
  @edge-case
  Scenario: The correlation-keyed subscription is released after a timeout
    Given Forge has dispatched a command and begun waiting for a reply
    When the local timeout fires before any reply arrives
    Then Forge should release the correlation-keyed subscription
    And any subsequent late reply on that channel should not be delivered to Forge

  # Why: Cache freshness — a specialist joining mid-build becomes resolvable without restart
  # [ASSUMPTION: confidence=high] Discovery cache TTL is 30 seconds (DM-discovery §1)
  @edge-case
  Scenario: A specialist that joins during a build becomes resolvable on the next resolution
    Given Forge has already resolved an earlier stage against the current fleet
    When a new specialist registers that advertises a capability Forge will need
    And Forge resolves the next stage
    Then the newly-registered specialist should be considered during resolution

  # Why: Cache invalidation — a specialist that leaves must not be selected afterwards
  @edge-case
  Scenario: A specialist that deregisters after an earlier stage is not selected for the next one
    Given the specialist that served an earlier stage has deregistered
    When Forge resolves the next stage against the same capability
    Then the deregistered specialist should not be selected
    And resolution should fall back to another matching specialist or to the unresolved path

  # Why: Async-mode dispatch — long-running work returns a run identifier to be polled
  # [ASSUMPTION: confidence=high] Advisory specialist-side command timeout default is 600 seconds (API-nats-agent-dispatch §3.2)
  @edge-case
  Scenario: An async-mode dispatch returns a run identifier for later polling
    Given the resolved specialist advertises the capability as long-running
    When Forge dispatches the command
    Then the specialist's first reply should carry a run identifier rather than a final result
    And Forge should poll for the final result using the capability's status tool

  # Why: Concurrent dispatches — two simultaneous dispatches must not cross wires
  @edge-case @concurrency
  Scenario: Two concurrent dispatches to the same specialist keep their replies separate
    Given Forge has two stages ready to dispatch at the same time
    And both stages resolve to the same specialist
    When Forge publishes both commands
    Then each dispatch should subscribe to its own correlation-keyed reply channel
    And each reply should be delivered to the dispatch that issued its correlation

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security / Concurrency / Data Integrity / Integration
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Reply authenticity — only the resolved specialist's reply completes the round-trip
  @edge-case @security
  Scenario: A reply from a source other than the resolved specialist is not trusted
    Given Forge has dispatched a command to a specific resolved specialist
    When a reply arrives on the correlation-keyed channel whose source identifier is a different agent
    Then Forge should not accept the reply as the specialist's answer
    And Forge should continue to wait for the resolved specialist's reply or for the local timeout

  # Why: Secret hygiene — dispatched parameters may carry sensitive details that must not be persisted
  @edge-case @security
  Scenario: Dispatch parameters flagged as sensitive are not persisted in the resolution record
    Given a stage dispatches parameters that include fields marked as sensitive
    When Forge records the capability resolution
    Then the persisted resolution should not contain the sensitive parameter values
    And the pipeline history view of this dispatch should describe only non-sensitive fields

  # Why: Trust-tier is authoritative — a lower-trust extension cannot override a core specialist for the same tool
  @edge-case @security
  Scenario: An extension-tier specialist cannot shadow a core-tier specialist for the same tool
    Given a core-tier specialist and an extension-tier specialist both advertise the same tool
    When Forge resolves the capability
    Then the core-tier specialist should be selected
    And the extension-tier specialist should appear only in the competing agents list

  # Why: Snapshot stability — an in-flight resolution must not see a cache swap mid-call
  @edge-case @concurrency
  Scenario: A fleet event that invalidates the cache does not disturb an in-flight resolution
    Given Forge is in the middle of resolving a capability against the current fleet snapshot
    When a fleet event arrives that invalidates the cache
    Then the in-flight resolution should complete against the snapshot it already read
    And the next resolution after the in-flight one should use the refreshed snapshot

  # Why: Determinism — concurrent resolutions with the same inputs must produce the same choice
  @edge-case @concurrency
  Scenario: Two concurrent resolutions for the same capability produce the same choice
    Given two stages resolve the same capability at the same time against the same fleet snapshot
    When both resolutions complete
    Then both should select the same specialist
    And the competing-agents list should be consistent across the two resolutions

  # Why: Exactly-once delivery — a duplicate reply on the same correlation is ignored
  @edge-case @data-integrity
  Scenario: A duplicate reply on the same correlation-keyed channel is ignored
    Given Forge has already accepted a reply on a correlation-keyed channel
    When a second reply for the same correlation is published afterwards
    Then Forge should not reprocess the duplicate reply
    And the gate decision derived from the first reply should stand

  # Why: Bus outage — a disconnect while waiting must not hang the pipeline silently
  @edge-case @integration
  Scenario: The fleet bus disconnects while Forge is waiting for a reply
    Given Forge has dispatched a command and is waiting for the specialist's reply
    When the fleet bus becomes unreachable before the reply arrives
    Then Forge should treat the dispatch as failed and surface the outcome to the reasoning loop
    And the pipeline should not wait indefinitely for a reply that cannot arrive

  # Why: Registry outage — Forge keeps serving from the last known snapshot rather than stalling
  @edge-case @integration
  Scenario: A registry read failure during cache refresh falls back to the last-known snapshot
    Given the live fleet registry is temporarily unreadable
    When Forge needs to resolve a capability and its cache has just expired
    Then Forge should resolve against the last-known fleet snapshot
    And the reasoning loop should be informed that the fleet view may be stale
Write specialist-agent-delegation_assumptions.yaml
52 lines
# Assumptions manifest for specialist-agent-delegation.feature
# Generated by /feature-spec

feature: "Specialist Agent Delegation"
feature_id: "FEAT-FORGE-003"
generated: "2026-04-24T00:00:00Z"
stack: "python"
review_required: false

assumptions:
  - id: "ASSUM-001"
    scenario: "An intent match at exactly the minimum confidence is accepted"
    assumption: "Intent-fallback minimum confidence threshold is 0.7"
    confidence: high
    basis: "forge.discovery.resolve(..., min_confidence=0.7) default in DM-discovery §3 and API-nats-agent-dispatch §2"
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "An async-mode dispatch returns a run identifier for later polling"
    assumption: "Advisory specialist-side command timeout default is 600 seconds (10 min)"
    confidence: high
    basis: "CommandPayload.timeout_seconds = 600 default in API-nats-agent-dispatch §3.2; advisory only, not enforced remotely"
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "A reply received just before the local timeout succeeds"
    assumption: "Forge-side hard dispatch timeout default is 900 seconds (15 min; 50% headroom over specialist advisory)"
    confidence: high
    basis: "forge.yaml.dispatch.default_timeout_seconds = 900 in API-nats-agent-dispatch §5"
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "A specialist that joins during a build becomes resolvable on the next resolution"
    assumption: "Discovery cache TTL is 30 seconds"
    confidence: high
    basis: "DM-discovery §1 and API-nats-agent-dispatch §2 — cache invalidated live by fleet.register/deregister/heartbeat"
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "Forge retries a failed dispatch with additional context on the second attempt"
    assumption: "Retry on soft-failure dispatch is reasoning-model-driven; no fixed max-retry count is enforced at the dispatch layer"
    confidence: medium
    basis: "Build plan §128 states 'retry with additional context on failure' without specifying a count; ADR-ARCH-015 places the decision in the reasoning loop"
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "A reply that omits the Coach score cannot auto-approve the stage"
    assumption: "Gate fallback when Coach score is absent is FLAG_FOR_REVIEW (not auto-approve, not hard-stop)"
    confidence: high
    basis: "API-nats-agent-dispatch §6 'no Coach score → no auto-approve'; pipeline orchestrator refresh §Degraded Mode"
    human_response: "confirmed"
Write specialist-agent-delegation_summary.md
89 lines
# Feature Spec Summary: Specialist Agent Delegation

**Feature ID**: FEAT-FORGE-003
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 33 total (2 smoke, 0 regression)
**Assumptions**: 6 total (5 high / 1 medium / 0 low confidence)
**Review required**: No — all assumptions grounded in supplied context files and ADRs

## Scope

Specifies Forge's specialist delegation path: a single capability-driven dispatch
tool that resolves the target agent via the live discovery cache (exact-tool match,
then intent-pattern fallback at minimum confidence, then tie-break by trust tier,
confidence, and queue depth), publishes the command on the fleet bus, and correlates
the reply on a correlation-keyed channel established before publish. Covers the
LES1 parity rule (PubAck is not success), the local hard-timeout cut-off, result
parsing (Coach score, criterion breakdown, detection findings — top-level preferred,
nested fallback), the degraded path when no specialist is resolvable, the
reasoning-model-driven retry with additional context on soft failure, outcome
correlation back onto the resolution record, async-mode run-identifier polling, and
invariants around snapshot stability, authenticity, and exactly-once reply handling.
Behaviour is described in domain terms; transport primitives (JetStream audit
interception, subscribe-then-publish ordering, per-correlation subject names) appear
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 9 |
| Edge cases (@edge-case) | 15 |
| Smoke (@smoke) | 2 |
| Regression (@regression) | 0 |
| Security (@security) | 3 |
| Concurrency (@concurrency) | 3 |
| Data integrity (@data-integrity) | 1 |
| Integration (@integration) | 2 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + security). Group totals do not sum to 33.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — exact-tool dispatch, intent-pattern fallback, Coach-output parsing, retry-with-context, outcome correlation | 5 |
| B | Boundary Conditions — minimum confidence (just-inside / just-outside), local timeout (just-inside / just-outside), trust-tier tie-break, queue-depth tie-break | 6 |
| C | Negative Cases — unresolved capability, degraded-status exclusion, specialist error, PubAck-not-success, wrong-correlation reply, missing Coach score, malformed envelope | 7 |
| D | Edge Cases — write-before-send invariant, subscribe-before-publish invariant, unsubscribe-on-timeout, cache freshness on join, cache invalidation on deregister, async-mode polling, concurrent dispatches to same agent | 7 |
| E | Security / Concurrency / Data Integrity / Integration — reply-source authenticity, sensitive-parameter hygiene, trust-tier supremacy, in-flight snapshot stability, concurrent-resolution determinism, duplicate-reply idempotency, bus disconnect, registry outage fallback | 8 |

## Deferred Items

None.

## Assumptions (all confirmed)

- **ASSUM-001** — intent-fallback minimum confidence 0.7 (DM-discovery §3) — high
- **ASSUM-002** — advisory specialist-side command timeout 600s (API-nats-agent-dispatch §3.2) — high
- **ASSUM-003** — Forge-side hard dispatch timeout 900s (API-nats-agent-dispatch §5) — high
- **ASSUM-004** — discovery cache TTL 30s (DM-discovery §1) — high
- **ASSUM-005** — retry is reasoning-model-driven; no fixed max-retry count at dispatch layer — medium
- **ASSUM-006** — gate fallback when Coach score absent is FLAG_FOR_REVIEW (API-nats-agent-dispatch §6) — high

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The state machine and
  SQLite history are referenced here only as the durable substrate on which
  `CapabilityResolution` records and dispatch outcomes are persisted.
- **FEAT-FORGE-002** — NATS Fleet Integration. Provides the live fleet cache,
  fleet-lifecycle subscription, pipeline-event publishing, and heartbeat view that
  FEAT-FORGE-003's resolution layer reads from. This spec assumes the fleet cache
  is populated and fresh; degraded-cache behaviour is inherited from FEAT-FORGE-002
  and referenced only where dispatch-time snapshot semantics matter (Group E
  integration scenarios).

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Specialist Agent Delegation" \
      --context features/specialist-agent-delegation/specialist-agent-delegation_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
Bash Verify output files
IN
ls -la /home/richardwoollcott/Projects/appmilla_github/forge/features/specialist-agent-delegation/ && echo "---" && grep -c "^  Scenario" /home/richardwoollcott/Projects/appmilla_github/forge/features/specialist-agent-delegation/specialist-agent-delegation.feature

OUT
total 44
drwxrwxr-x 2 richardwoollcott richardwoollcott  4096 Apr 24 20:02 .
drwxrwxr-x 8 richardwoollcott richardwoollcott  4096 Apr 24 20:01 ..
-rw-rw-r-- 1 richardwoollcott richardwoollcott  2540 Apr 24 20:01 specialist-agent-delegation_assumptions.yaml
-rw-rw-r-- 1 richardwoollcott richardwoollcott 20706 Apr 24 20:01 specialist-agent-delegation.feature
-rw-rw-r-- 1 richardwoollcott richardwoollcott  4794 Apr 24 20:02 specialist-agent-delegation_summary.md
---
33
All three files written. 33 scenarios total.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: Specialist Agent Delegation (FEAT-FORGE-003)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Files written:

features/specialist-agent-delegation/specialist-agent-delegation.feature
features/specialist-agent-delegation/specialist-agent-delegation_assumptions.yaml
features/specialist-agent-delegation/specialist-agent-delegation_summary.md
Scenarios: 33 total

@key-example: 5 · @boundary: 6
@negative: 9 · @edge-case: 15
@smoke: 2 · @regression: 0
@security: 3 · @concurrency: 3 · @data-integrity: 1 · @integration: 2
Assumptions: 6 total

high: 5 (auto-proceed)
medium: 1 (Coach review recommended — ASSUM-005, retry policy)
low: 0
Review required: No — all assumptions grounded in supplied context files and ADRs.

Deferred: None.

Next step:


/feature-plan "Specialist Agent Delegation" \
  --context features/specialist-agent-delegation/specialist-agent-delegation_summary.md
/feature-plan Step 11 will insert @task:<TASK-ID> tags into the .feature file after tasks are created.

please update /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md with the status and ensure the feature-plan command presented in your last response is reflected in the build plan
Read forge-build-plan.md (lines 261-390)
Let me verify FEAT-FORGE-002's completion state and look at the feature-plan section more fully.

Bash Verify FEAT-FORGE-002 status and tasks
IN
cat /home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/nats-fleet-integration_summary.md | head -12 2>&1 && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/tasks/backlog/ 2>&1 | head -20

OUT
# Feature Spec Summary: NATS Fleet Integration

**Feature ID**: FEAT-FORGE-002
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 33 total (3 smoke, 0 regression)
**Assumptions**: 5 total (5 high / 0 medium / 0 low confidence)
**Review required**: No — all assumptions grounded in supplied context files

## Scope

Specifies Forge's role on the shared NATS fleet: self-registration on startup,
---
forge-v2-doc-alignment
nats-fleet-integration
TASK-REV-NF20-plan-nats-fleet-integration.md
Read forge-build-plan.md (lines 388-437)
Bash Check FEAT-FORGE-002 references in build plan
IN
grep -n "FEAT-FORGE-002" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md 2>&1 | head -20

OUT
127:| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads | Publish pipeline events, `fleet.register` (ADR-SP-014) |
288:# FEAT-FORGE-002: NATS Fleet Integration
289:guardkit feature-spec FEAT-FORGE-002 \
360:# FEAT-FORGE-002: NATS Fleet Integration (depends on 001)
361:guardkit feature-plan FEAT-FORGE-002 \
410:guardkit autobuild FEAT-FORGE-002
573:For fleet registration (FEAT-FORGE-002):
Making the edits now.

Edit forge-build-plan.md
Added 5 lines
Edit forge-build-plan.md
Added 1 line
Edit forge-build-plan.md
Modified
Bash Verify edits
IN
sed -n '260,275p;295,305p;365,373p' /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md

OUT
### Step 3: /feature-spec × 8 — 🟡 IN PROGRESS (2 / 8)

Produces BDD feature specifications for each feature. Run sequentially — later features
reference earlier ones.

**Completed:**
- ✅ **FEAT-FORGE-001** — Pipeline State Machine & Configuration (2026-04-24; revised 2026-04-24)
  - Artefacts: `features/pipeline-state-machine-and-configuration/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 34 (6 @key-example · 6 @boundary · 11 @negative · 16 @edge-case · 3 @smoke)
  - Assumptions: 5 resolved — 5 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - Post-review revisions: ASSUM-002 (arbitrary turn-budget ceiling) retired; ASSUM-005 (cancel-operator audit) promoted low → medium on schema grounds.
- ✅ **FEAT-FORGE-003** — Specialist Agent Delegation (2026-04-24)
  - Artefacts: `features/specialist-agent-delegation/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 33 (5 @key-example · 6 @boundary · 9 @negative · 15 @edge-case · 2 @smoke · 3 @security · 3 @concurrency · 1 @data-integrity · 2 @integration)
  - Assumptions: 6 resolved — 5 high, 1 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - ASSUM-005 (retry policy on soft-failure dispatch) kept at medium — build plan §128 mandates retry but leaves the count to the reasoning loop per ADR-ARCH-015.
  --context forge/docs/design/contracts/API-nats-pipeline-events.md \
  --context forge/docs/design/contracts/API-nats-fleet-lifecycle.md \
  --context forge/docs/design/models/DM-discovery.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md

# FEAT-FORGE-003: Specialist Agent Delegation  ✅ COMPLETE (2026-04-24)
# Output: forge/features/specialist-agent-delegation/
guardkit feature-spec FEAT-FORGE-003 \
  --context forge/docs/design/contracts/API-nats-agent-dispatch.md \
  --context forge/docs/design/models/DM-discovery.md \

# FEAT-FORGE-002: NATS Fleet Integration (depends on 001)
guardkit feature-plan FEAT-FORGE-002 \
  --context forge/features/<feat-forge-002-slug>/<feat-forge-002-slug>_summary.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine (depends on 001 — can parallel with 002)
guardkit feature-plan FEAT-FORGE-005 \
  --context forge/features/<feat-forge-005-slug>/<feat-forge-005-slug>_summary.md
Edits look correct. Three changes made to forge-build-plan.md:

Progress counter (line 260): 1 / 8 → 2 / 8
Completed list (after line 270): added FEAT-FORGE-003 entry with scenario/assumption counts
feature-spec bash block (line 296): marked # ✅ COMPLETE (2026-04-24) with output path
feature-plan bash block (line 369-370): resolved placeholder <feat-forge-003-slug> to specialist-agent-delegation and switched to quoted feature name (matches FEAT-FORGE-001's convention)
Heads-up — one inconsistency I noticed but did not touch: FEAT-FORGE-002's artefacts exist on disk (features/nats-fleet-integration/) and its /feature-plan has already run (commit 6a29ed3), but the build plan's Completed list and the <feat-forge-002-slug> placeholder on forge-build-plan.md:362 still reflect the earlier state. Want me to apply the same style of update for FEAT-FORGE-002? 








/feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md



━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: Confidence-Gated Checkpoint Protocol (FEAT-FORGE-004)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:

features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature
features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_assumptions.yaml
features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md
Scenarios: 32 total

@key-example: 8    @boundary: 5
@negative: 7    @edge-case: 10
@smoke: 4     @regression: 4
@security: 2   @concurrency: 2   @data-integrity: 2   @integration: 1
Assumptions: 7 total

high: 5 (auto-proceed)
medium: 2 (Coach review recommended) — ASSUM-003 (max-wait ceiling terminal behaviour), ASSUM-007 (expected-approver allowlist)
low: 0
Deferred: 0 groups

Next steps:

Review: features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature
Pass to feature-plan:

/feature-plan "Confidence-Gated Checkpoint Protocol" \
  --context features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md





