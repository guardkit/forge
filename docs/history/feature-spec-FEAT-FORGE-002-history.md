/feature-spec FEAT-FORGE-002 \
  --context forge/docs/design/contracts/API-nats-pipeline-events.md \
  --context forge/docs/design/contracts/API-nats-fleet-lifecycle.md \
  --context forge/docs/design/models/DM-discovery.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md
I'll execute the /feature-spec command for FEAT-FORGE-002. Starting Phase 1: Context Gathering.

Bash List forge project root
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/

OUT
AGENTS.md
command_history.md
docs
features
langgraph.json
LICENSE
mcp.json
migrations
orchestrator-config.yaml
place-documents.sh
pyproject.toml
README.md
spikes
src
tasks
tests
Globpattern: "**/FEAT-FORGE-002*"
No files found
Globpattern: "**/*.feature"
Found 1 file
Bash List docs directory
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/docs/

OUT
adr
architecture
design
history
product
research
reviews
state
Globpattern: "**/FEAT-FORGE*"
No files found
Grep "FEAT-FORGE-002" (in /Users/richardwoollcott/Projects/appmilla_github/forge)
2 lines of output
Globpattern: "**/features/**/*.md"
Found 1 file
Grep "FEAT-FORGE-002" (in /Users/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md)
111 lines of output
Now let me read the context files specified in the command.

Bash List contract and model files
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ /Users/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/ 2>&1 | head -50

OUT
/Users/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/:
API-cli.md
API-nats-agent-dispatch.md
API-nats-approval-protocol.md
API-nats-fleet-lifecycle.md
API-nats-pipeline-events.md
API-sqlite-schema.md
API-subagents.md
API-subprocess.md
API-tool-layer.md

/Users/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/:
DM-build-lifecycle.md
DM-calibration.md
DM-discovery.md
DM-gating.md
DM-graphiti-entities.md
Bash Check nats-core docs structure
IN
ls /Users/richardwoollcott/Projects/appmilla_github/nats-core/docs/design/ 2>&1 | head -20

OUT
contracts
decisions
diagrams
mcp-tools.json
models
specs
Read API-nats-pipeline-events.md
Read API-nats-fleet-lifecycle.md
Read DM-discovery.md
Read agent-manifest-contract.md
Read nats-core-system-spec.md
Read pipeline-state-machine-and-configuration.feature
Read pipeline-state-machine-and-configuration_summary.md
Let me get a bit more context on the degraded mode expectation and relevant ADRs.

Grep "degraded mode|FLAG FOR REVIEW|specialist unavailable" (in /Users/richardwoollcott/Projects/appmilla_github/forge/docs/design)
41 lines of output
Grep "heartbeat_interval|heartbeat_timeout|stale_heartbeat|fleet.yaml" (in /Users/richardwoollcott/Projects/appmilla_github/forge/docs)
23 lines of output
Context loaded: stack=python, 4 context files read, 1 existing .feature file (FEAT-FORGE-001), ADRs on fleet discovery/dispatch/degraded-mode indexed.

Now presenting Phase 2: the full grouped proposal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: NATS Fleet Integration (FEAT-FORGE-002)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@feat-forge-002 @nats-fleet-integration
Feature: NATS Fleet Integration
  As Rich, the sole operator of Forge
  I want Forge to announce itself on the fleet, track which specialist agents are alive,
    and broadcast every pipeline lifecycle event on the shared bus
  So that Jarvis, dashboards, and downstream notifiers have a reliable live view of Forge,
    and Forge can route work only to specialists that are actually available

  Background:
    Given Forge is configured from the project configuration file
    And the fleet message bus is reachable
━━ GROUP A: Key Examples (7 scenarios) ━━


  # Why: Core registration — Forge announces itself so other fleet members can discover it
  @key-example @smoke
  Scenario: On startup Forge registers itself with the fleet
    When Forge starts
    Then Forge should publish its own capability manifest to the fleet registration channel
    And the fleet registry should list Forge as a ready agent
    And the manifest should include Forge's tools, intents, and trust tier

  # Why: Core heartbeat — liveness signal keeps Forge discoverable
  @key-example @smoke
  Scenario: Forge publishes a heartbeat at the configured interval
    Given Forge has been registered with the fleet
    When the configured heartbeat interval elapses
    Then Forge should publish a heartbeat carrying its current status and workload
    And the heartbeat should reflect whether a build is currently in flight

  # Why: Discovery — keep a live 30-second-TTL view of which specialists exist
  @key-example
  Scenario: Forge maintains a live cache of fleet agents
    Given two specialist agents are registered with the fleet
    When Forge is asked to resolve a capability by tool name
    Then Forge should consult its live fleet cache
    And the resolution should identify the matching specialist without re-reading the registry

  # Why: Discovery update — a newly-joined agent becomes visible without restart
  @key-example
  Scenario: A newly-registered specialist becomes available for resolution
    Given Forge is watching fleet lifecycle events
    When a new specialist agent publishes its registration
    Then the new agent should be present in Forge's fleet view
    And subsequent resolutions may select the new agent

  # Why: Lifecycle publication — each stage completion is observable on the bus
  @key-example @smoke
  Scenario: Forge publishes a stage-complete event after each gated stage
    Given a build is running
    When a pipeline stage completes and is evaluated
    Then Forge should publish a stage-complete event
    And the event should carry the build's correlation identifier
    And the event should describe the stage outcome, the gate decision, and the coach score

  # Why: Lifecycle terminal — a successful build announces completion to downstream listeners
  @key-example
  Scenario: Forge publishes a build-complete event when the pipeline finishes successfully
    Given a build has progressed to the finalising stage and produced a pull request
    When the build transitions to complete
    Then Forge should publish a build-complete event carrying the pull request details
    And the event should share the build's originating correlation identifier

  # Why: Graceful shutdown — Forge should remove itself so routing skips it immediately
  @key-example
  Scenario: On graceful shutdown Forge deregisters from the fleet
    Given Forge has been registered with the fleet
    When Forge receives a graceful shutdown signal
    Then Forge should publish a deregistration event before exiting
    And the fleet registry should no longer list Forge as an available agent
━━ GROUP B: Boundary Conditions (5 scenarios) ━━


  # Why: Just-inside boundary — heartbeat age equal to the stale threshold is still fresh enough
  @boundary
  Scenario Outline: An agent whose last heartbeat is within the stale threshold remains selectable
    Given a specialist agent is registered with the fleet
    And the specialist's most recent heartbeat arrived <seconds_ago> seconds ago
    When Forge is asked to resolve the specialist's tool
    Then the specialist should be considered available

    Examples:
      | seconds_ago |
      | 30          |
      | 60          |
      | 90          |

  # Why: Just-outside boundary — once the threshold is crossed, the agent is marked degraded
  @boundary @negative
  Scenario: An agent whose last heartbeat is past the stale threshold is excluded from primary resolution
    Given a specialist agent is registered with the fleet
    And the specialist has not sent a heartbeat for longer than the stale-heartbeat threshold
    When Forge is asked to resolve the specialist's tool
    Then the specialist should be marked as degraded in the fleet view
    And the specialist should be excluded from the primary resolution result

  # Why: Just-inside boundary — progress events are emitted at or under the configured interval
  @boundary
  Scenario: A long-running stage still publishes progress within the configured interval
    Given a build is running a stage that will take several minutes
    When the configured progress interval elapses without a stage transition
    Then Forge should publish a progress event carrying wave and task details
    And the progress event should share the build's correlation identifier

  # Why: Just-inside boundary — intent confidence at the minimum threshold is accepted
  @boundary
  Scenario: Intent-based fallback selects an agent whose confidence is exactly at the minimum threshold
    Given no agent advertises a tool named "draft-plan"
    And one agent advertises an intent whose confidence equals the minimum acceptable confidence
    When Forge is asked to resolve the capability by intent pattern
    Then the agent should be selected as the match

  # Why: Just-outside boundary — intent confidence below threshold is rejected
  @boundary @negative
  Scenario: Intent-based fallback rejects an agent whose confidence is below the minimum threshold
    Given no agent advertises a tool named "draft-plan"
    And the only candidate agent advertises an intent with confidence below the minimum acceptable confidence
    When Forge is asked to resolve the capability by intent pattern
    Then the resolution should return no match
━━ GROUP C: Negative Cases (6 scenarios) ━━


  # Why: Degraded mode — when no specialist exists, the stage must flag for review, never auto-approve
  @negative
  Scenario: Resolving a tool that no fleet agent advertises forces the stage to flag for review
    Given no registered agent advertises the requested tool
    And no registered agent advertises a matching intent
    When the pipeline asks Forge to resolve the capability for a stage
    Then the resolution outcome should be recorded as unresolved
    And the stage should be flagged for human review
    And the stage should not be allowed to auto-approve

  # Why: Degraded mode — an agent marked degraded is not a valid primary match
  @negative
  Scenario: A tool advertised only by a degraded agent does not produce a primary match
    Given the only agent advertising the requested tool is currently marked degraded
    When the pipeline asks Forge to resolve the capability
    Then the primary resolution should return no match
    And the stage should be flagged for human review

  # Why: Malformed fleet event — a bad registration must not corrupt the cache
  @negative
  Scenario: An invalid registration event is ignored without affecting the live fleet view
    Given Forge is watching fleet registration events
    When a registration event arrives with a payload that fails schema validation
    Then the invalid event should be rejected and logged
    And Forge's fleet view should remain unchanged
    And subsequent valid events should continue to be processed

  # Why: Duplicate registration — re-announcing supersedes, never duplicates
  @negative
  Scenario: A repeated registration of the same agent supersedes the prior manifest
    Given an agent is already registered with a known manifest version
    When the same agent re-registers with a newer manifest version
    Then the fleet view should reflect only the newer manifest
    And no duplicate entry should appear for that agent

  # Why: Invalid build trigger — a malformed build-queued payload produces a terminal failure event
  @negative
  Scenario: A malformed build-queued payload is acknowledged and reported as failed
    Given Forge is subscribed to the build-queue channel
    When a build-queued message arrives whose payload fails schema validation
    Then the message should be acknowledged so the queue advances
    And Forge should publish a build-failed event explaining the payload was malformed

  # Why: Permission guard — builds whose path is outside the allowlist must not start
  @negative
  Scenario: A build whose feature path falls outside the configured allowlist is refused
    Given the configuration specifies an allowlist of permitted paths
    When a build-queued message arrives with a feature path outside that allowlist
    Then the build should not start
    And Forge should publish a build-failed event explaining the path is not permitted
━━ GROUP D: Edge Cases (7 scenarios) ━━


  # Why: Cache freshness — stale cache triggers a live registry re-read before resolution
  @edge-case
  Scenario: A resolution after the cache has gone stale re-reads the fleet registry
    Given Forge has a cached fleet view older than the cache freshness window
    When Forge is asked to resolve a capability
    Then the fleet registry should be re-read before the resolution is produced
    And the resolution should be made against the refreshed view

  # Why: Two equally-qualified agents — tie-break preserves a deterministic ordering
  @edge-case
  Scenario: When two agents advertise the same tool, the higher trust tier is preferred
    Given two registered agents both advertise the requested tool
    And one agent's trust tier ranks above the other
    When Forge is asked to resolve the tool
    Then the higher-ranked agent should be selected
    And the other agent should be recorded as a competing candidate

  # Why: Two equally-qualified agents at the same tier — queue depth tie-breaks
  @edge-case
  Scenario: When two agents at the same trust tier advertise the same tool, the one with the shallower queue is preferred
    Given two registered agents at the same trust tier both advertise the requested tool
    And the most recent heartbeats report different queue depths
    When Forge is asked to resolve the tool
    Then the agent with the shallower queue should be selected

  # Why: Lifecycle event for a paused stage — downstream listeners get informed immediately
  @edge-case
  Scenario: Flagging a stage for human review publishes a build-paused event
    Given a build is running and a gate decision flags the current stage for review
    When the pipeline prepares to wait for a decision
    Then Forge should publish a build-paused event before entering the paused state
    And the paused event should carry the rationale and the reply subject for a decision

  # Why: Resume after approval — the lifecycle event carries the decision that unblocked the build
  @edge-case
  Scenario: Resuming a paused build publishes a build-resumed event
    Given a build is paused waiting for a decision
    When an approval response rehydrates the build
    Then Forge should publish a build-resumed event
    And the event should carry the decision and the responder

  # Why: Crash recovery — a paused build in SQLite is re-announced so listeners don't miss it
  @edge-case
  Scenario: Restarting Forge with a paused build in the history re-emits the paused event
    Given Forge's history records a paused build awaiting a decision
    When Forge restarts and reconciles its state
    Then Forge should re-publish a build-paused event for that build
    And the event should carry the original build's correlation identifier

  # Why: Correlation threading — every lifecycle event for a build carries the original correlation id
  @edge-case
  Scenario: All lifecycle events for one build share the originating correlation identifier
    Given a build has been queued with a known correlation identifier
    When Forge publishes started, progress, stage-complete, and complete events for that build
    Then every published event should carry the same originating correlation identifier
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 25 scenarios across 4 groups
Inferred assumptions: 5 (will be resolved in Phase 5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review each group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For each group, respond with one of:
[A] Accept — include all scenarios as-is
[R] Reject — exclude all scenarios in this group
[M] Modify — accept but describe changes inline
[+] Add — accept and describe additional scenarios to add
[?] Defer — mark group for future specification

Fast path: type A A A A to accept all four groups at once.

GROUP A — Key Examples (7 scenarios): _
GROUP B — Boundary Conditions (5 scenarios): _
GROUP C — Negative Cases (6 scenarios): _
GROUP D — Edge Cases (7 scenarios): _

Awaiting your curation input before proceeding to Phase 4 (edge-case expansion) and Phase 5 (assumption resolution).

command-history.md
A A A A
All four groups accepted. Now presenting Phase 4: Edge Case Expansion.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I've identified 8 additional scenarios covering security, concurrency, data integrity, and integration boundaries. Here's the sample (option S) followed by the full titles so you can decide:

Sample preview (2 of 8):


  # Why: Secrets must never leak onto the bus via manifests
  @edge-case @security
  Scenario: Forge's registration manifest excludes runtime secrets
    When Forge publishes its capability manifest to the fleet
    Then the manifest should not contain any API keys, credentials, or connection strings
    And the manifest should only describe capabilities, identity, and permissions

  # Why: Crash mid-publish — SQLite state must remain the source of truth if the bus refuses the event
  @edge-case @data-integrity
  Scenario: A lifecycle publish failure does not roll back the build's recorded progress
    Given a build has just transitioned to the running state and been recorded in history
    When publishing the build-started event fails at the transport layer
    Then the build should remain recorded as running in the local history
    And the failure should be logged for later reconciliation
    And the build should continue without aborting
Full list of the 8 proposed scenarios:

@security — Forge's registration manifest excludes runtime secrets
@security — Build-queued messages from disallowed originating adapters are refused
@concurrency — Simultaneous fleet-change events update the cache without loss
@concurrency — A build-queued message is not redelivered after terminal acknowledgement
@data-integrity — A lifecycle publish failure does not roll back the build's recorded progress
@data-integrity — A redelivered build-queued message for a completed build is acknowledged idempotently
@integration — Heartbeats continue to publish even when the fleet registry is temporarily unreachable
@integration — A build-queued message is held unacknowledged while paused, so the queue position is preserved on restart
Include them?


[Y] Yes — include all 8 and move to curation
[S] Show the remaining 6 in full before deciding
[N] No — skip and proceed to Phase 5
Your choice [Y/S/N]: _

Y
Including all 8. Here are the full scenarios for curation.

━━ GROUP E: Edge Case Expansion (8 scenarios) ━━


  # Why: Secrets must never leak onto the bus via manifests
  @edge-case @security
  Scenario: Forge's registration manifest excludes runtime secrets
    When Forge publishes its capability manifest to the fleet
    Then the manifest should not contain any API keys, credentials, or connection strings
    And the manifest should only describe capabilities, identity, and permissions

  # Why: Trigger authenticity — builds may only be initiated by approved originators
  @edge-case @security
  Scenario: A build-queued message from an unrecognised originating adapter is refused
    Given Forge is subscribed to the build-queue channel
    When a build-queued message arrives declaring an originating adapter that is not on the approved list
    Then the build should not start
    And Forge should publish a build-failed event explaining the originator was not recognised

  # Why: Concurrent fleet updates — the cache must remain consistent under racing events
  @edge-case @concurrency
  Scenario: Simultaneous fleet-change events update the cache without loss
    Given two agents publish registration and deregistration events at effectively the same moment
    When Forge processes both events
    Then the final fleet view should reflect each event exactly once
    And neither update should overwrite the other's effect

  # Why: Acknowledgement is terminal — a completed build must not be re-picked from the queue
  @edge-case @concurrency
  Scenario: A build-queued message is not redelivered after terminal acknowledgement
    Given a build has reached a terminal outcome and has been acknowledged on the queue
    When the build-queue is inspected
    Then no redelivery of that build should occur
    And the queue should advance to the next pending item

  # Why: History is the source of truth — transport failures don't rewrite recorded progress
  @edge-case @data-integrity
  Scenario: A lifecycle publish failure does not roll back the build's recorded progress
    Given a build has just transitioned to the running state and been recorded in history
    When publishing the build-started event fails at the transport layer
    Then the build should remain recorded as running in the local history
    And the failure should be logged for later reconciliation
    And the build should continue without aborting

  # Why: At-least-once delivery — redelivery must not restart a completed build
  @edge-case @data-integrity
  Scenario: A redelivered build-queued message for a completed build is acknowledged idempotently
    Given a build is already recorded as complete in the local history
    When the build-queue redelivers the original build-queued message after a restart
    Then Forge should acknowledge the redelivery without starting a new build
    And no duplicate build record should be created

  # Why: Liveness must survive transient registry problems — Forge's own heartbeats are independent
  @edge-case @integration
  Scenario: Heartbeats continue to publish even when the fleet registry is temporarily unreachable
    Given Forge has been registered with the fleet
    When the fleet registry becomes temporarily unreachable while the bus remains up
    Then Forge should continue publishing heartbeats at the configured interval
    And Forge should attempt to refresh its fleet view when the registry becomes reachable again

  # Why: Paused builds hold their place in the queue — the message stays unacked until terminal
  @edge-case @integration
  Scenario: A paused build holds its queue position until a terminal decision is reached
    Given a build is paused waiting for a decision
    When Forge is asked whether the underlying queue message has been acknowledged
    Then the message should remain unacknowledged
    And the queue should not advance to the next pending item until the paused build reaches a terminal state
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Curate Group E:


[A] Accept  [R] Reject  [M] Modify  [+] Add  [?] Defer
GROUP E — Edge Case Expansion (8 scenarios): _
A
Group E accepted. Now Phase 5: Assumption Resolution.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (5 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Press Enter to accept the proposed default, or type a replacement value.

[1] Heartbeat interval
Proposed: 30 seconds
Confidence: high
Basis: API-nats-fleet-lifecycle §2.3 — forge.yaml.fleet.heartbeat_interval_seconds default 30, consistent with AgentConfig.heartbeat_interval_seconds in nats-core
Accept or enter value: _

[2] Stale-heartbeat threshold (degraded-mode trigger)
Proposed: 90 seconds
Confidence: high
Basis: API-nats-fleet-lifecycle §3.2 — forge.yaml.fleet.stale_heartbeat_seconds default 90; ADR-ARCH-017 confirms
Accept or enter value: _

[3] Discovery cache TTL
Proposed: 30 seconds
Confidence: high
Basis: DM-discovery §1 — "30-second TTL cache of agent-registry"; API-nats-fleet-lifecycle §3.2 confirms
Accept or enter value: _

[4] Intent-fallback minimum confidence
Proposed: 0.7
Confidence: high
Basis: DM-discovery §3 — min_confidence: float = 0.7 default; API-nats-fleet-lifecycle §4 step 2 confirms
Accept or enter value: _

[5] Build-progress publish cadence
Proposed: at least every 60 seconds during RUNNING
Confidence: high
Basis: API-nats-pipeline-events §3.1 — "At least every 60s during RUNNING; also on wave boundaries"
Accept or enter value: _

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All five assumptions are grounded in the supplied context files — high confidence across the board, so no REVIEW REQUIRED flag is expected. Awaiting your responses.

accept defaults
All defaults accepted. Executing Phase 6: writing output files.

Write nats-fleet-integration.feature
318 lines
# Generated by /feature-spec
# Feature: NATS Fleet Integration (FEAT-FORGE-002)
# Stack: python
# Assumptions: 5 (see nats-fleet-integration_assumptions.yaml)
# Generated: 2026-04-24T00:00:00Z

@feat-forge-002 @nats-fleet-integration
Feature: NATS Fleet Integration
  As Rich, the sole operator of Forge
  I want Forge to announce itself on the fleet, track which specialist agents are alive,
    and broadcast every pipeline lifecycle event on the shared bus
  So that Jarvis, dashboards, and downstream notifiers have a reliable live view of Forge,
    and Forge can route work only to specialists that are actually available

  Background:
    Given Forge is configured from the project configuration file
    And the fleet message bus is reachable

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core registration — Forge announces itself so other fleet members can discover it
  @key-example @smoke
  Scenario: On startup Forge registers itself with the fleet
    When Forge starts
    Then Forge should publish its own capability manifest to the fleet registration channel
    And the fleet registry should list Forge as a ready agent
    And the manifest should include Forge's tools, intents, and trust tier

  # Why: Core heartbeat — liveness signal keeps Forge discoverable
  # [ASSUMPTION: confidence=high] Heartbeat interval is 30 seconds by default (forge.yaml.fleet.heartbeat_interval_seconds)
  @key-example @smoke
  Scenario: Forge publishes a heartbeat at the configured interval
    Given Forge has been registered with the fleet
    When the configured heartbeat interval elapses
    Then Forge should publish a heartbeat carrying its current status and workload
    And the heartbeat should reflect whether a build is currently in flight

  # Why: Discovery — keep a live view of which specialists exist
  # [ASSUMPTION: confidence=high] Discovery cache TTL is 30 seconds
  @key-example
  Scenario: Forge maintains a live cache of fleet agents
    Given two specialist agents are registered with the fleet
    When Forge is asked to resolve a capability by tool name
    Then Forge should consult its live fleet cache
    And the resolution should identify the matching specialist without re-reading the registry

  # Why: Discovery update — a newly-joined agent becomes visible without restart
  @key-example
  Scenario: A newly-registered specialist becomes available for resolution
    Given Forge is watching fleet lifecycle events
    When a new specialist agent publishes its registration
    Then the new agent should be present in Forge's fleet view
    And subsequent resolutions may select the new agent

  # Why: Lifecycle publication — each stage completion is observable on the bus
  @key-example @smoke
  Scenario: Forge publishes a stage-complete event after each gated stage
    Given a build is running
    When a pipeline stage completes and is evaluated
    Then Forge should publish a stage-complete event
    And the event should carry the build's correlation identifier
    And the event should describe the stage outcome, the gate decision, and the coach score

  # Why: Lifecycle terminal — a successful build announces completion to downstream listeners
  @key-example
  Scenario: Forge publishes a build-complete event when the pipeline finishes successfully
    Given a build has progressed to the finalising stage and produced a pull request
    When the build transitions to complete
    Then Forge should publish a build-complete event carrying the pull request details
    And the event should share the build's originating correlation identifier

  # Why: Graceful shutdown — Forge should remove itself so routing skips it immediately
  @key-example
  Scenario: On graceful shutdown Forge deregisters from the fleet
    Given Forge has been registered with the fleet
    When Forge receives a graceful shutdown signal
    Then Forge should publish a deregistration event before exiting
    And the fleet registry should no longer list Forge as an available agent

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Just-inside boundary — heartbeat age up to the stale threshold is still fresh enough
  # [ASSUMPTION: confidence=high] Stale-heartbeat threshold is 90 seconds (forge.yaml.fleet.stale_heartbeat_seconds)
  @boundary
  Scenario Outline: An agent whose last heartbeat is within the stale threshold remains selectable
    Given a specialist agent is registered with the fleet
    And the specialist's most recent heartbeat arrived <seconds_ago> seconds ago
    When Forge is asked to resolve the specialist's tool
    Then the specialist should be considered available

    Examples:
      | seconds_ago |
      | 30          |
      | 60          |
      | 90          |

  # Why: Just-outside boundary — once the stale threshold is crossed, the agent is marked degraded
  @boundary @negative
  Scenario: An agent whose last heartbeat is past the stale threshold is excluded from primary resolution
    Given a specialist agent is registered with the fleet
    And the specialist has not sent a heartbeat for longer than the stale-heartbeat threshold
    When Forge is asked to resolve the specialist's tool
    Then the specialist should be marked as degraded in the fleet view
    And the specialist should be excluded from the primary resolution result

  # Why: Just-inside boundary — progress events are emitted at or under the configured interval
  # [ASSUMPTION: confidence=high] Progress cadence is at least every 60 seconds during RUNNING
  @boundary
  Scenario: A long-running stage still publishes progress within the configured interval
    Given a build is running a stage that will take several minutes
    When the configured progress interval elapses without a stage transition
    Then Forge should publish a progress event carrying wave and task details
    And the progress event should share the build's correlation identifier

  # Why: Just-inside boundary — intent confidence at the minimum threshold is accepted
  # [ASSUMPTION: confidence=high] Intent-fallback minimum confidence is 0.7
  @boundary
  Scenario: Intent-based fallback selects an agent whose confidence is exactly at the minimum threshold
    Given no agent advertises a tool named "draft-plan"
    And one agent advertises an intent whose confidence equals the minimum acceptable confidence
    When Forge is asked to resolve the capability by intent pattern
    Then the agent should be selected as the match

  # Why: Just-outside boundary — intent confidence below the minimum threshold is rejected
  @boundary @negative
  Scenario: Intent-based fallback rejects an agent whose confidence is below the minimum threshold
    Given no agent advertises a tool named "draft-plan"
    And the only candidate agent advertises an intent with confidence below the minimum acceptable confidence
    When Forge is asked to resolve the capability by intent pattern
    Then the resolution should return no match

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Degraded mode — when no specialist exists, the stage must flag for review, never auto-approve
  @negative
  Scenario: Resolving a tool that no fleet agent advertises forces the stage to flag for review
    Given no registered agent advertises the requested tool
    And no registered agent advertises a matching intent
    When the pipeline asks Forge to resolve the capability for a stage
    Then the resolution outcome should be recorded as unresolved
    And the stage should be flagged for human review
    And the stage should not be allowed to auto-approve

  # Why: Degraded mode — an agent marked degraded is not a valid primary match
  @negative
  Scenario: A tool advertised only by a degraded agent does not produce a primary match
    Given the only agent advertising the requested tool is currently marked degraded
    When the pipeline asks Forge to resolve the capability
    Then the primary resolution should return no match
    And the stage should be flagged for human review

  # Why: Malformed fleet event — a bad registration must not corrupt the cache
  @negative
  Scenario: An invalid registration event is ignored without affecting the live fleet view
    Given Forge is watching fleet registration events
    When a registration event arrives with a payload that fails schema validation
    Then the invalid event should be rejected and logged
    And Forge's fleet view should remain unchanged
    And subsequent valid events should continue to be processed

  # Why: Duplicate registration — re-announcing supersedes, never duplicates
  @negative
  Scenario: A repeated registration of the same agent supersedes the prior manifest
    Given an agent is already registered with a known manifest version
    When the same agent re-registers with a newer manifest version
    Then the fleet view should reflect only the newer manifest
    And no duplicate entry should appear for that agent

  # Why: Invalid build trigger — a malformed build-queued payload produces a terminal failure event
  @negative
  Scenario: A malformed build-queued payload is acknowledged and reported as failed
    Given Forge is subscribed to the build-queue channel
    When a build-queued message arrives whose payload fails schema validation
    Then the message should be acknowledged so the queue advances
    And Forge should publish a build-failed event explaining the payload was malformed

  # Why: Permission guard — builds whose path is outside the allowlist must not start
  @negative
  Scenario: A build whose feature path falls outside the configured allowlist is refused
    Given the configuration specifies an allowlist of permitted paths
    When a build-queued message arrives with a feature path outside that allowlist
    Then the build should not start
    And Forge should publish a build-failed event explaining the path is not permitted

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Cache freshness — stale cache triggers a live registry re-read before resolution
  @edge-case
  Scenario: A resolution after the cache has gone stale re-reads the fleet registry
    Given Forge has a cached fleet view older than the cache freshness window
    When Forge is asked to resolve a capability
    Then the fleet registry should be re-read before the resolution is produced
    And the resolution should be made against the refreshed view

  # Why: Two equally-qualified agents — tie-break preserves a deterministic ordering
  @edge-case
  Scenario: When two agents advertise the same tool, the higher trust tier is preferred
    Given two registered agents both advertise the requested tool
    And one agent's trust tier ranks above the other
    When Forge is asked to resolve the tool
    Then the higher-ranked agent should be selected
    And the other agent should be recorded as a competing candidate

  # Why: Two equally-qualified agents at the same tier — queue depth tie-breaks
  @edge-case
  Scenario: When two agents at the same trust tier advertise the same tool, the one with the shallower queue is preferred
    Given two registered agents at the same trust tier both advertise the requested tool
    And the most recent heartbeats report different queue depths
    When Forge is asked to resolve the tool
    Then the agent with the shallower queue should be selected

  # Why: Lifecycle event for a paused stage — downstream listeners get informed immediately
  @edge-case
  Scenario: Flagging a stage for human review publishes a build-paused event
    Given a build is running and a gate decision flags the current stage for review
    When the pipeline prepares to wait for a decision
    Then Forge should publish a build-paused event before entering the paused state
    And the paused event should carry the rationale and the reply subject for a decision

  # Why: Resume after approval — the lifecycle event carries the decision that unblocked the build
  @edge-case
  Scenario: Resuming a paused build publishes a build-resumed event
    Given a build is paused waiting for a decision
    When an approval response rehydrates the build
    Then Forge should publish a build-resumed event
    And the event should carry the decision and the responder

  # Why: Crash recovery — a paused build in history is re-announced so listeners don't miss it
  @edge-case
  Scenario: Restarting Forge with a paused build in the history re-emits the paused event
    Given Forge's history records a paused build awaiting a decision
    When Forge restarts and reconciles its state
    Then Forge should re-publish a build-paused event for that build
    And the event should carry the original build's correlation identifier

  # Why: Correlation threading — every lifecycle event for a build carries the original correlation id
  @edge-case
  Scenario: All lifecycle events for one build share the originating correlation identifier
    Given a build has been queued with a known correlation identifier
    When Forge publishes started, progress, stage-complete, and complete events for that build
    Then every published event should carry the same originating correlation identifier

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security, Concurrency, Data Integrity, Integration Boundaries
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Secrets must never leak onto the bus via manifests
  @edge-case @security
  Scenario: Forge's registration manifest excludes runtime secrets
    When Forge publishes its capability manifest to the fleet
    Then the manifest should not contain any API keys, credentials, or connection strings
    And the manifest should only describe capabilities, identity, and permissions

  # Why: Trigger authenticity — builds may only be initiated by approved originators
  @edge-case @security
  Scenario: A build-queued message from an unrecognised originating adapter is refused
    Given Forge is subscribed to the build-queue channel
    When a build-queued message arrives declaring an originating adapter that is not on the approved list
    Then the build should not start
    And Forge should publish a build-failed event explaining the originator was not recognised

  # Why: Concurrent fleet updates — the cache must remain consistent under racing events
  @edge-case @concurrency
  Scenario: Simultaneous fleet-change events update the cache without loss
    Given two agents publish registration and deregistration events at effectively the same moment
    When Forge processes both events
    Then the final fleet view should reflect each event exactly once
    And neither update should overwrite the other's effect

  # Why: Acknowledgement is terminal — a completed build must not be re-picked from the queue
  @edge-case @concurrency
  Scenario: A build-queued message is not redelivered after terminal acknowledgement
    Given a build has reached a terminal outcome and has been acknowledged on the queue
    When the build-queue is inspected
    Then no redelivery of that build should occur
    And the queue should advance to the next pending item

  # Why: History is the source of truth — transport failures don't rewrite recorded progress
  @edge-case @data-integrity
  Scenario: A lifecycle publish failure does not roll back the build's recorded progress
    Given a build has just transitioned to the running state and been recorded in history
    When publishing the build-started event fails at the transport layer
    Then the build should remain recorded as running in the local history
    And the failure should be logged for later reconciliation
    And the build should continue without aborting

  # Why: At-least-once delivery — redelivery must not restart a completed build
  @edge-case @data-integrity
  Scenario: A redelivered build-queued message for a completed build is acknowledged idempotently
    Given a build is already recorded as complete in the local history
    When the build-queue redelivers the original build-queued message after a restart
    Then Forge should acknowledge the redelivery without starting a new build
    And no duplicate build record should be created

  # Why: Liveness must survive transient registry problems — heartbeats are independent
  @edge-case @integration
  Scenario: Heartbeats continue to publish even when the fleet registry is temporarily unreachable
    Given Forge has been registered with the fleet
    When the fleet registry becomes temporarily unreachable while the bus remains up
    Then Forge should continue publishing heartbeats at the configured interval
    And Forge should attempt to refresh its fleet view when the registry becomes reachable again

  # Why: Paused builds hold their place in the queue — the message stays unacked until terminal
  @edge-case @integration
  Scenario: A paused build holds its queue position until a terminal decision is reached
    Given a build is paused waiting for a decision
    When Forge is asked whether the underlying queue message has been acknowledged
    Then the message should remain unacknowledged
    And the queue should not advance to the next pending item until the paused build reaches a terminal state
Write nats-fleet-integration_assumptions.yaml
45 lines
# Assumptions manifest for nats-fleet-integration.feature
# Generated by /feature-spec

feature: "NATS Fleet Integration"
feature_id: "FEAT-FORGE-002"
generated: "2026-04-24T00:00:00Z"
stack: "python"
review_required: false

assumptions:
  - id: "ASSUM-001"
    scenario: "Forge publishes a heartbeat at the configured interval"
    assumption: "Heartbeat interval is 30 seconds by default"
    confidence: high
    basis: "API-nats-fleet-lifecycle §2.3 — forge.yaml.fleet.heartbeat_interval_seconds default 30; consistent with AgentConfig.heartbeat_interval_seconds in nats-core agent-manifest-contract"
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "An agent whose last heartbeat is past the stale threshold is excluded from primary resolution"
    assumption: "Stale-heartbeat threshold is 90 seconds"
    confidence: high
    basis: "API-nats-fleet-lifecycle §3.2 — forge.yaml.fleet.stale_heartbeat_seconds default 90; reinforced by ADR-ARCH-017 live-fleet-watching"
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "Forge maintains a live cache of fleet agents"
    assumption: "Discovery cache TTL is 30 seconds"
    confidence: high
    basis: "DM-discovery §1 — '30-second TTL cache of agent-registry KV'; API-nats-fleet-lifecycle §3.2 confirms cache invalidation contract"
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "Intent-based fallback selects an agent whose confidence is exactly at the minimum threshold"
    assumption: "Intent-fallback minimum confidence is 0.7"
    confidence: high
    basis: "DM-discovery §3 — resolve() parameter min_confidence: float = 0.7; API-nats-fleet-lifecycle §4 step 2 uses the same default"
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "A long-running stage still publishes progress within the configured interval"
    assumption: "Build-progress publish cadence is at least every 60 seconds during RUNNING"
    confidence: high
    basis: "API-nats-pipeline-events §3.1 — 'At least every 60s during RUNNING; also on wave boundaries from autobuild_runner'"
    human_response: "confirmed"
Write nats-fleet-integration_summary.md
80 lines
# Feature Spec Summary: NATS Fleet Integration

**Feature ID**: FEAT-FORGE-002
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 33 total (3 smoke, 0 regression)
**Assumptions**: 5 total (5 high / 0 medium / 0 low confidence)
**Review required**: No — all assumptions grounded in supplied context files

## Scope

Specifies Forge's role on the shared NATS fleet: self-registration on startup,
periodic heartbeats, graceful deregistration, live watching of fleet lifecycle
events, and capability resolution via the 30-second-TTL discovery cache with
degraded-mode fallback when specialists are absent or unresponsive. Also covers
the outbound pipeline-event stream (started / progress / stage-complete / paused
/ resumed / complete / failed / cancelled) and the inbound build-queue
subscription with terminal-only acknowledgement, duplicate detection, and
path-allowlist refusal. Behaviour is described in domain terms; transport
primitives (JetStream consumer config, KV buckets, PubAck semantics) appear only
as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 7 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 8 |
| Edge cases (@edge-case) | 15 |
| Smoke (@smoke) | 3 |
| Regression (@regression) | 0 |
| Security (@security) | 2 |
| Concurrency (@concurrency) | 2 |
| Data integrity (@data-integrity) | 2 |
| Integration (@integration) | 2 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + security). Group totals do not sum to 33.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — registration, heartbeat, live cache, stage-complete, build-complete, deregister | 7 |
| B | Boundary Conditions — stale-heartbeat threshold, progress interval, intent-confidence minimum | 5 |
| C | Negative Cases — unresolved capability, degraded-only match, malformed events, duplicate register, malformed queue payload, path-allowlist | 6 |
| D | Edge Cases — stale-cache re-read, tie-break by tier and queue depth, paused/resumed events, crash-recovery re-announce, correlation threading | 7 |
| E | Security / Concurrency / Data Integrity / Integration — secret-free manifest, originator allowlist, racing cache updates, terminal-ack idempotency, publish-failure does not regress history, redelivery idempotency, registry-outage heartbeats, paused-build queue hold | 8 |

## Deferred Items

None.

## Assumptions (all high confidence, all confirmed)

- **ASSUM-001** — heartbeat interval 30s (forge.yaml.fleet.heartbeat_interval_seconds)
- **ASSUM-002** — stale-heartbeat threshold 90s (forge.yaml.fleet.stale_heartbeat_seconds)
- **ASSUM-003** — discovery cache TTL 30s (DM-discovery §1)
- **ASSUM-004** — intent-fallback minimum confidence 0.7 (DM-discovery §3)
- **ASSUM-005** — build-progress publish cadence at least every 60s during RUNNING (API-nats-pipeline-events §3.1)

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. FEAT-FORGE-002 extends the
  state machine with live bus publishing and subscribes to the inbound build-queue subject;
  the SQLite history and the state-machine transitions described in FEAT-FORGE-001 are
  referenced here only as the durable substrate for lifecycle events and crash-recovery
  re-announcement.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "NATS Fleet Integration" \
      --context features/nats-fleet-integration/nats-fleet-integration_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: NATS Fleet Integration (FEAT-FORGE-002)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:

features/nats-fleet-integration/nats-fleet-integration.feature
features/nats-fleet-integration/nats-fleet-integration_assumptions.yaml
features/nats-fleet-integration/nats-fleet-integration_summary.md
Scenarios: 33 total

@key-example: 7 @boundary: 5
@negative: 8 @edge-case: 15
@smoke: 3 @regression: 0
@security: 2 @concurrency: 2 @data-integrity: 2 @integration: 2
Assumptions: 5 total — 5 high / 0 medium / 0 low (no REVIEW REQUIRED flag)

Deferred: None

Next step:


/feature-plan "NATS Fleet Integration" \
  --context features/nats-fleet-integration/nats-fleet-integration_summary.md