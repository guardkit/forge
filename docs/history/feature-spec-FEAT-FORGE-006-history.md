/feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/models/DM-graphiti-entities.md \
  --context forge/docs/design/models/DM-calibration.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-004-graphiti-group-partitioning.md
I'll execute the /feature-spec command for FEAT-FORGE-006. Starting with Phase 1 context gathering.

Bash List project root and design docs directory
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/ && ls /home/richardwoollcott/Projects/appmilla_github/forge/forge/docs/design/ 2>/dev/null || echo "forge/docs/design/ not at that path"

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
forge/docs/design/ not at that path
Bash Find FEAT-FORGE-006 feature files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -name "FEAT-FORGE-006*" -type f 2>/dev/null | head -20

OUT
(Bash completed with no output)
Bash Find existing .feature files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -name "*.feature" -type f 2>/dev/null | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/nats-fleet-integration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/features/nats-fleet-integration/nats-fleet-integration.feature
Bash Examine docs directory structure
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/ && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/ 2>/dev/null

OUT
adr
architecture
design
history
product
research
reviews
state
contracts
decisions
diagrams
models
Bash Locate FEAT-FORGE-006 references
IN
grep -rn "FEAT-FORGE-006" /home/richardwoollcott/Projects/appmilla_github/forge/ --include="*.md" --include="*.yaml" --include="*.yml" -l 2>/dev/null | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/ADR-FLEET-001-trace-richness.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/ADR-FLEET-001-trace-richness.md
Bash List design subdirectories
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/ 2>/dev/null

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/:
API-cli.md
API-nats-agent-dispatch.md
API-nats-approval-protocol.md
API-nats-fleet-lifecycle.md
API-nats-pipeline-events.md
API-sqlite-schema.md
API-subagents.md
API-subprocess.md
API-tool-layer.md

/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/:
DDR-001-reply-subject-correlation.md
DDR-002-resume-value-rehydration-helper.md
DDR-003-sqlite-schema-layout-wal.md
DDR-004-graphiti-group-partitioning.md
DDR-005-cli-context-manifest-resolution.md
DDR-006-async-subagent-state-channel-contract.md

/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/:
DM-build-lifecycle.md
DM-calibration.md
DM-discovery.md
DM-gating.md
DM-graphiti-entities.md
Bash Find FEAT-FORGE-006 in forge-build-plan
IN
grep -n "FEAT-FORGE-006" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | head -20

OUT
131:| FEAT-FORGE-006 | Infrastructure Coordination | 001, 002 | 2-3 days | Graphiti seeding after each pipeline stage, Graphiti querying for cross-project context, test verification, git operations (clone/pull/branch/push/PR via `gh`) | Git operations, SQLite build history and stage log |
137:- SQLite build history schema — folded into FEAT-FORGE-006 (infrastructure coordination)
315:# FEAT-FORGE-006: Infrastructure Coordination
316:guardkit feature-spec FEAT-FORGE-006 \
376:# FEAT-FORGE-006: Infrastructure Coordination (depends on 001, 002)
377:guardkit feature-plan FEAT-FORGE-006 \
414:guardkit autobuild FEAT-FORGE-006
Read forge-build-plan.md (lines 101-180)
Read forge-build-plan.md (lines 301-420)
Now I have the FEAT-FORGE-006 description. Let me read the four context files.

Read DM-graphiti-entities.md
Read DM-calibration.md
Read API-subprocess.md
Read DDR-004-graphiti-group-partitioning.md
Let me also check existing feature specs for conventions and look for any SQLite schema context.

Read nats-fleet-integration.feature (lines 2-61)
Bash List prior feature artefacts
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/ && ls /home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/

OUT
nats-fleet-integration_assumptions.yaml
nats-fleet-integration.feature
nats-fleet-integration_summary.md
pipeline-state-machine-and-configuration_assumptions.yaml
pipeline-state-machine-and-configuration.feature
pipeline-state-machine-and-configuration_summary.md
Read API-sqlite-schema.md (lines 2-81)
Read nats-fleet-integration_summary.md
Context loaded: stack=python, 5 domain models + 6 DDRs + 9 API contracts found, 2 existing .feature files (FEAT-FORGE-001, -002), 31 ADRs referenced.

Proceeding to Phase 2.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Infrastructure Coordination (FEAT-FORGE-006)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@feat-forge-006 @infrastructure-coordination
Feature: Infrastructure Coordination
  As Rich, the sole operator of Forge
  I want Forge to persist every gate decision and session outcome to its long-term memory,
    ingest my Q&A history as retrievable priors, verify code via tests, and drive
    version control through safe, permission-constrained shell operations
  So that each new build benefits from the lessons of the last, every build produces
    a reviewable pull request, and no subprocess ever escapes its allowlisted sandbox

  Background:
    Given Forge is configured from the project configuration file
    And the long-term memory service is reachable
    And the durable build-history store is available
    And subprocess permissions are constitutionally enforced

━━ GROUP A: Key Examples (10 scenarios) ━━

  # Why: Core seeding path — every completed stage is durably remembered
  @key-example @smoke
  Scenario: A stage outcome is seeded into long-term memory after the stage completes
    Given a build is running
    And a stage produced a gate decision
    When the stage result is recorded
    Then the gate decision should appear in the pipeline-history memory group
    And its rationale, score, and criterion breakdown should be retrievable by future builds

  # Why: Capability lookup recorded before dispatch — supports write-before-send invariant
  @key-example
  Scenario: A capability resolution is recorded before the matching specialist is dispatched
    Given the reasoning model requested a tool by name
    When Forge resolves the capability to a specialist
    Then the resolution should be written to pipeline-history before the specialist is invoked
    And the competing candidates and the chosen agent should both be captured

  # Why: Overrides are the learning signal — they must be captured whenever Rich diverges from the gate
  @key-example
  Scenario: An operator override is recorded whenever the reviewer diverges from the gate
    Given Forge has recorded a gate decision
    When the operator's response differs from the gate's recommendation
    Then an override event should be stored in pipeline-history
    And it should be linked to the originating gate decision

  # Why: End-of-build summary is the substrate of cross-build pattern mining
  @key-example @smoke
  Scenario: A session outcome is written when the build reaches a terminal state
    Given a build has completed, failed, or been cancelled
    When the terminal state transition is recorded
    Then a session outcome should be written once
    And it should link to every gate decision and capability resolution produced during the build

  # Why: Priors retrieval — the whole point of writing history
  @key-example @smoke
  Scenario: At build start Forge retrieves priors from similar past builds
    Given previous builds exist for the same feature or project
    When a new build starts
    Then Forge should retrieve recent similar session outcomes
    And Forge should retrieve the operator's recent override behaviour for the capabilities in play
    And Forge should retrieve approved calibration adjustments relevant to the current capabilities
    And these priors should be available to the reasoning model as narrative context

  # Why: Q&A priors retrieval from the calibration group
  @key-example
  Scenario: At build start Forge retrieves Q&A priors for the expected pipeline stages
    Given the operator's past Q&A history has been ingested
    When a new build starts
    Then Forge should retrieve the top matching Q&A events for the build's expected stages
    And these Q&A priors should be available to the reasoning model as narrative context

  # Why: Incremental ingestion — unchanged files are skipped, changed files re-parse
  @key-example
  Scenario: On startup Forge ingests the operator's Q&A history files
    Given the operator's history files are listed in the configuration
    When Forge boots
    Then each history file should be parsed into calibration events
    And each event should be written to the calibration-history memory group
    And a snapshot of each file's content hash should be recorded

  # Why: Git clone + branch preparation is the first durable action of every build
  @key-example @smoke
  Scenario: A dedicated, ephemeral worktree is prepared for every build
    Given a build is about to enter PREPARING
    When the worktree is requested
    Then a fresh worktree should be created under the allowlisted builds directory
    And the worktree path should be tied to this specific build

  # Why: Tests verify the build before it is eligible for PR creation
  @key-example @smoke
  Scenario: The build's tests are executed before a pull request is proposed
    Given the autobuild step has produced changes in the worktree
    When Forge verifies the build
    Then the configured test command should be run inside the worktree
    And the reasoning model should receive the pass/fail summary and failing test details

  # Why: Every build that passes produces a reviewable PR
  @key-example
  Scenario: A pull request is opened when verification passes
    Given verification passed
    When Forge finalises the build
    Then the changes should be committed on the build's branch
    And the branch should be pushed to the remote
    And a pull request should be opened against the base branch
    And the session outcome should record the pull-request URL

━━ GROUP B: Boundary Conditions (5 scenarios) ━━

  # Why: Subprocess default timeout — at-limit and just-over pair
  @boundary
  Scenario Outline: A subprocess call is allowed to run up to the configured timeout
    Given the default subprocess timeout is configured
    When a subprocess runs for <duration>
    Then the outcome should be <result>

    Examples:
      | duration                         | result                                                                 |
      | just under the default timeout   | the subprocess should complete and return its output                   |
      | exactly at the default timeout   | the subprocess should complete and return its output                   |

  # Why: Just-outside boundary — subprocess exceeding timeout is terminated
  @boundary @negative
  Scenario: A subprocess that exceeds the configured timeout is terminated
    Given the default subprocess timeout is configured
    When a subprocess runs for longer than the configured timeout
    Then the subprocess should be terminated
    And the outcome should be recorded as a timeout, not a success

  # Why: Learning-loop threshold — proposals only fire above the minimum evidence count
  # [ASSUMPTION: confidence=high] Pattern detection requires at least five supporting overrides before a proposal is emitted
  @boundary
  Scenario Outline: An adjustment proposal is only emitted when the evidence count meets the minimum
    Given the learning loop is watching a capability
    When the recent override count is <count>
    Then the learning loop should <behaviour>

    Examples:
      | count                         | behaviour                                   |
      | one below the minimum         | wait for more evidence and not propose      |
      | exactly at the minimum        | propose a calibration adjustment            |
      | well above the minimum        | propose a calibration adjustment            |

  # Why: Incremental ingestion — sha match is the skip signal
  @boundary
  Scenario Outline: A history file is re-parsed only when its content hash has changed
    Given a history file has been ingested before
    When Forge refreshes the calibration corpus and the file's content hash has <state>
    Then the file should <action>

    Examples:
      | state                  | action                                                  |
      | not changed            | be skipped without re-parsing                           |
      | changed                | be re-parsed and new events added to the memory group   |

  # Why: Calibration adjustments expire — at-expiry and after-expiry pair
  # [ASSUMPTION: confidence=high] Adjustments with expires_at in the past are excluded at retrieval, not deleted
  @boundary
  Scenario Outline: Expired calibration adjustments are excluded from retrieval
    Given an approved calibration adjustment exists
    When priors are retrieved and the adjustment's expiry is <state>
    Then the adjustment should <result>

    Examples:
      | state                             | result                                                  |
      | still in the future               | be returned as an active prior                          |
      | exactly at the current time       | be excluded from priors                                 |
      | in the past                       | be excluded from priors but retained for audit          |

━━ GROUP C: Negative Cases (7 scenarios) ━━

  # Why: Long-term memory is enrichment, not a hard dependency — builds survive its outages
  @negative
  Scenario: A long-term memory write failure does not halt the build
    Given a stage outcome is being recorded
    When the long-term memory service is unreachable
    Then a structured failure should be logged
    And the authoritative build-history entry should still be committed
    And the build should continue to the next stage

  # Why: Pending calibration adjustments must never be returned as priors
  @negative
  Scenario: Unapproved calibration adjustments are excluded from priors
    Given a proposed calibration adjustment has not yet been approved
    When priors are retrieved at build start
    Then the proposed adjustment should not appear in the narrative context

  # Why: Unchanged Q&A re-ingestion must be idempotent
  @negative
  Scenario: Re-ingesting the same Q&A events produces no duplicates
    Given a batch of history events has already been ingested
    When the same source file is parsed again
    Then each previously-seen event should be recognised as a duplicate
    And no duplicate calibration events should appear in the memory group

  # Why: Partial parse failures must preserve what was parsed
  @negative
  Scenario: A malformed section in a history file is tolerated without losing earlier events
    Given a history file contains a malformed section
    When Forge ingests the file
    Then the events before the malformed section should still be written
    And the file snapshot should be marked as partial so that it will be re-tried on the next refresh

  # Why: Test failures must be observable, not silent
  @negative
  Scenario: Failing tests are reported to the reasoning model rather than crashing the build
    Given the autobuild step produced changes
    When the test run reports failures
    Then the reasoning model should receive the failure summary and the failing test identifiers
    And the build should remain in a state where the reasoning model can decide how to respond

  # Why: Pull-request creation is not always possible — missing credentials must be surfaced safely
  @negative
  Scenario: A pull request cannot be opened when GitHub credentials are missing
    Given the GitHub credentials are not available in the environment
    When Forge attempts to open a pull request
    Then the attempt should fail with a structured error
    And no pull-request URL should be recorded on the session outcome
    And the session outcome should still be written with the failure reason

  # Why: Constitutional permissions must refuse any command outside the allowlist
  @negative @security
  Scenario: A shell command that is not on the allowlist is refused
    Given the subprocess permissions list a fixed set of allowed binaries
    When the reasoning model attempts to invoke a binary that is not on the allowlist
    Then the invocation should be refused before any process is spawned
    And the refusal should be recorded with the attempted command and the operator's current permissions

━━ GROUP D: Edge Cases (6 scenarios) ━━

  # Why: Write ordering invariant — authoritative store commits before long-term memory mirror
  @edge-case
  Scenario: The authoritative build history is committed before the long-term memory mirror
    Given a stage result has arrived
    When Forge records the stage
    Then the durable build-history entry should be committed first
    And only then should the matching long-term memory entry be written

  # Why: Reconcile backfills anything that failed to land in long-term memory
  @edge-case
  Scenario: Entries missing from long-term memory are backfilled on the next build
    Given a previous build wrote build-history entries that never reached long-term memory
    When the next build starts
    Then Forge should detect the entries missing from long-term memory
    And Forge should backfill them into the pipeline-history memory group

  # Why: Session outcomes are never in-progress — only terminal
  @edge-case
  Scenario: A build interrupted mid-run never leaves an in-progress session outcome behind
    Given a build has been interrupted before a terminal state was reached
    When Forge inspects its long-term memory for the build
    Then no non-terminal session outcome should exist for that build
    And the interruption should be represented only by the individual stage entries already written

  # Why: Cross-group edges — pipeline-history references calibration-history
  @edge-case
  Scenario: A gate decision can reference the Q&A event that informed it
    Given a gate decision drew on a prior Q&A event
    When the gate decision is written to pipeline-history
    Then the decision should carry a link to the originating Q&A event in calibration-history

  # Why: Worktree cleanup is best-effort — failure must not hold up the state machine
  @edge-case
  Scenario: A worktree that cannot be deleted does not block the terminal state transition
    Given the build has reached a terminal state
    When the worktree cleanup fails
    Then the cleanup failure should be logged
    And the build should still be marked as terminal
    And the build's durable history should still be finalised

  # Why: Incremental ingestion on every boot and build finish
  @edge-case
  Scenario: Changed Q&A files are ingested again after every completed build
    Given a completed build has finished
    And one of the operator's history files has changed since it was last parsed
    When the post-build refresh runs
    Then the changed file should be re-ingested
    And any unchanged files should be skipped

━━ GROUP E: Security / Concurrency / Data Integrity / Integration (7 scenarios) ━━

  # Why: Subprocess working directory is confined to the ephemeral worktree
  @security
  Scenario: A subprocess cannot be launched with a working directory outside the worktree
    Given the working-directory allowlist permits only per-build worktrees
    When a subprocess is requested with a working directory outside the allowlist
    Then the invocation should be refused before any process is spawned

  # Why: GitHub credentials come only from the environment, never from forge.yaml
  @security
  Scenario: GitHub operations use credentials from the environment rather than configuration
    Given GitHub credentials are provided by the deployment environment
    When Forge creates a pull request
    Then the credentials should be read from the environment
    And no GitHub credentials should ever be read from the project configuration file

  # Why: Duplicate ingestion must be a true no-op — deterministic identity
  @data-integrity
  Scenario: A Q&A event has a deterministic identity so re-ingestion never creates duplicates
    Given the same history event is parsed twice
    When the event is written to the calibration-history memory group
    Then both writes should resolve to the same stored entity
    And the retrievable count of that event should remain one

  # Why: Concurrent gate writes within a build must not interleave or corrupt each other
  @concurrency
  Scenario: Gate decisions produced in close succession are recorded in order
    Given two stage results arrive for the same build within a short window
    When both are recorded
    Then both gate decisions should appear in the session outcome's linked entries
    And the session outcome should link to them in the order they were decided

  # Why: Reviewer approval round-trip flips the approved flag, making the adjustment visible to priors
  @integration
  Scenario: An approved calibration adjustment becomes visible to subsequent builds
    Given the learning loop proposed a calibration adjustment
    When the operator approves it via the approval round-trip
    Then the adjustment should be marked approved
    And the next build should retrieve the adjustment as a prior

  # Why: History-parser idempotency is provenance-safe — re-running on the same file yields the same counts
  @data-integrity
  Scenario: A second ingestion pass on an unchanged history file reports zero new events
    Given a history file has been fully ingested
    When the file is re-scanned without having changed
    Then the ingestion result should report zero new events
    And no write operations should be issued against the memory service

  # Why: Branches, commits, pushes, and PR titles flow end-to-end from a single build
  @integration
  Scenario: A successful end-to-end build produces a branch, a commit, a push, and a pull request
    Given a build has just produced verified changes
    When Forge finalises the build
    Then a build-specific branch should exist locally
    And the branch should carry a commit summarising the build
    And the branch should be pushed to the remote
    And a pull request should be opened
    And the session outcome should record the pull-request URL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 35 scenarios across 5 groups
Inferred assumptions: 8 (will be resolved in Phase 5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review each group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For each group, you may:

  [A] Accept  — include all scenarios in this group as-is
  [R] Reject  — exclude all scenarios in this group
  [M] Modify  — accept but specify changes (provide inline)
  [+] Add     — accept and add additional scenarios you describe
  [?] Defer   — mark group for future specification; excluded from output

Fast path: type "A A A A A" to accept all five groups at once.

GROUP A — Key Examples (10 scenarios): _
GROUP B — Boundary Conditions (5 scenarios): _
GROUP C — Negative Cases (7 scenarios): _
GROUP D — Edge Cases (6 scenarios): _
GROUP E — Security / Concurrency / Data Integrity / Integration (7 scenarios): _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Waiting for your curation response. You can reply per-group (e.g. A M A A A with inline change notes) or with the fast path (A A A A A). After curation I'll offer optional edge-case expansion (Phase 4) and then resolve the 8 inferred assumptions (Phase 5) before writing files to features/infrastructure-coordination/.

forge-build-plan.md
A A A A A
All five groups accepted. Moving to Phase 4.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
I've identified 8 additional scenarios covering security, concurrency, data integrity, and integration-boundary behaviour that Group E did not already cover. A sample:


  # Why: Supply-chain safety — subprocess arguments must not be taken from long-term memory content
  @edge-case @security
  Scenario: Retrieved priors are not used as subprocess arguments
    Given priors have been retrieved from long-term memory
    When Forge invokes a subprocess
    Then the subprocess arguments should be derived only from configuration or reasoning-model decisions
    And no retrieved prior text should be passed directly as a shell argument

  # Why: Split-brain safety — two Forge instances must not race the same build's writes
  @edge-case @concurrency
  Scenario: A second Forge instance cannot mirror a stage that has already been mirrored
    Given one Forge instance has already written a stage entry to long-term memory
    When a second Forge instance attempts to mirror the same stage
    Then the second write should be recognised as already present and skipped
Additional scenarios I'd propose if you include all 8:

Security: refusal to read/write outside the filesystem allowlist; redaction of secrets (e.g. tokens) from long-term memory rationale fields
Concurrency: ordering guarantee when a gate decision and an override event arrive within the same millisecond; rolling-window recency horizon for override counts
Data integrity: supersession chain for calibration adjustments is acyclic; session outcome is written exactly once even under retries
Integration boundaries: priors retrieval degrades to empty list (not failure) when long-term memory returns no matches; PR body assembly uses only worktree-local information

Include them? [Y] Yes (curate all 8)  [S] Show all 8 and curate  [N] No, skip to Phase 5

Your choice [Y/S/N]: _
Y
Here are the 8 additional scenarios, grouped. These will be appended to Groups E if accepted.


  # Why: Secrets in rationale fields should never leak into long-term memory
  @edge-case @security
  Scenario: Secrets appearing in rationale text are redacted before long-term memory is written
    Given a gate decision's rationale contains something that looks like a credential
    When the gate decision is written to long-term memory
    Then the credential-shaped content should be redacted from the stored rationale

  # Why: Filesystem isolation mirrors shell isolation
  @edge-case @security
  Scenario: A file read outside the allowlist is refused
    Given the filesystem read allowlist is configured
    When a tool attempts to read a file outside the allowlist
    Then the read should be refused before any bytes are returned

  # Why: Priors content is never trusted as an executable input
  @edge-case @security
  Scenario: Retrieved priors are not used as subprocess arguments
    Given priors have been retrieved from long-term memory
    When Forge invokes a subprocess
    Then the subprocess arguments should be derived only from configuration or reasoning-model decisions
    And no retrieved prior text should be passed directly as a shell argument

  # Why: Split-brain safety — two Forge instances must not race on the same build
  @edge-case @concurrency
  Scenario: A second Forge instance cannot mirror a stage that has already been mirrored
    Given one Forge instance has already written a stage entry to long-term memory
    When a second Forge instance attempts to mirror the same stage
    Then the second write should be recognised as already present and skipped

  # Why: Recency horizon — very old overrides should not skew pattern detection
  @edge-case @concurrency
  Scenario: Override counts used by pattern detection honour a bounded recency horizon
    Given operator overrides exist across a wide time range
    When the learning loop counts recent overrides for a capability
    Then only overrides inside the configured recency horizon should be counted
    And older overrides should be excluded from the count

  # Why: Supersession chains must terminate
  @edge-case @data-integrity
  Scenario: A calibration adjustment cannot supersede one that already supersedes it
    Given an approved calibration adjustment chain exists
    When a new adjustment is proposed
    Then the supersession chain should be walked before write
    And a proposal that would create a cycle should be rejected

  # Why: Session outcomes are written exactly once, even under retry
  @edge-case @data-integrity
  Scenario: A session outcome is written exactly once even when the terminal transition is retried
    Given the terminal transition handler has already written the session outcome
    When the transition handler is retried
    Then no additional session outcome should be written
    And the existing session outcome should be preserved as the single record

  # Why: Empty priors are not a failure mode
  @edge-case @integration
  Scenario: Priors retrieval returns an empty context when there is nothing to retrieve
    Given the memory groups contain no entries relevant to the current build
    When priors are retrieved at build start
    Then the retrieval should return an empty narrative context
    And the build should proceed without priors

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review the 8 expansion scenarios as one block
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [A] Accept  — include all 8 scenarios
  [R] Reject  — discard all 8 scenarios
  [M] Modify  — accept with inline changes
  [+] Add     — accept and add more
  [?] Defer   — mark for future specification

Your choice: _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A
Expansion accepted — 43 scenarios total. Moving to Phase 5.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (8 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter to accept,
or type the actual value. I've ordered them by confidence —
highs first, lows last (the lows are the ones most worth your time).

[1] Override recency horizon used by learning-loop pattern detection
    Proposed: 30 days
    Confidence: high
    Basis: DM-graphiti-entities §4 — "OverrideEvent on same target_identifier in last 30 days"
    Accept or enter value: _

[2] Default base branch for pull requests
    Proposed: main
    Confidence: high
    Basis: API-subprocess §4 — create_pr signature has base: str = "main"
    Accept or enter value: _

[3] Test verification command for Python builds
    Proposed: pytest (run from the worktree root)
    Confidence: medium
    Basis: Python stack convention; build plan says "test verification" without specifying command
    Accept or enter value: _

[4] Failed-test report shape delivered back to the reasoning model
    Proposed: pass/fail counts + list of failing test node-ids + tail of captured output
    Confidence: medium
    Basis: pytest convention; not specified in any provided context file
    Accept or enter value: _

[5] Priors empty-context representation when nothing relevant exists
    Proposed: empty narrative section in the system prompt (not an error, not omitted block)
    Confidence: medium
    Basis: ADR-ARCH-018 says priors are injected prose-style; empty-list shape is natural but unstated
    Accept or enter value: _

[6] Credential redaction in gate-decision rationale before it is written to long-term memory
    Proposed: pattern-based redaction for tokens/keys/secrets-shaped strings in the rationale field
    Confidence: low
    Basis: Inferred security hygiene; not stated in any provided context
    Accept or enter value: _

[7] Split-brain dedupe mechanism for a second Forge instance attempting to mirror the same stage
    Proposed: rely on entity_id uniqueness at the memory-service level; repeat writes resolve to the same entity
    Confidence: low
    Basis: Inferred from CalibrationEvent deterministic id pattern; GateDecision uses UUID so this assumes a separate "already written" check
    Accept or enter value: _

[8] Ordering of gate-decision links inside a session outcome
    Proposed: chronological by decided_at ascending
    Confidence: low
    Basis: Natural ordering; not specified in any provided context
    Accept or enter value: _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply with overrides for any numbered item (e.g. 3: pytest -q or 6: do not redact, rationale is stored verbatim) and I'll apply them. Reply accept all to take every default as-is.

accept defaults
All defaults accepted. Writing output files now.

Bash Create output directory and verify
IN
mkdir -p /home/richardwoollcott/Projects/appmilla_github/forge/features/infrastructure-coordination && ls /home/richardwoollcott/Projects/appmilla_github/forge/features/

OUT
confidence-gated-checkpoint-protocol
guardkit-command-invocation-engine
infrastructure-coordination
nats-fleet-integration
pipeline-state-machine-and-configuration
specialist-agent-delegation
Write infrastructure-coordination.feature
425 lines
# Generated by /feature-spec
# Feature: Infrastructure Coordination (FEAT-FORGE-006)
# Stack: python
# Assumptions: 8 (see infrastructure-coordination_assumptions.yaml)
# Generated: 2026-04-24T00:00:00Z

@feat-forge-006 @infrastructure-coordination
Feature: Infrastructure Coordination
  As Rich, the sole operator of Forge
  I want Forge to persist every gate decision and session outcome to its long-term memory,
    ingest my Q&A history as retrievable priors, verify code via tests, and drive
    version control through safe, permission-constrained shell operations
  So that each new build benefits from the lessons of the last, every build produces
    a reviewable pull request, and no subprocess ever escapes its allowlisted sandbox

  Background:
    Given Forge is configured from the project configuration file
    And the long-term memory service is reachable
    And the durable build-history store is available
    And subprocess permissions are constitutionally enforced

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core seeding path — every completed stage is durably remembered
  @key-example @smoke
  Scenario: A stage outcome is seeded into long-term memory after the stage completes
    Given a build is running
    And a stage produced a gate decision
    When the stage result is recorded
    Then the gate decision should appear in the pipeline-history memory group
    And its rationale, score, and criterion breakdown should be retrievable by future builds

  # Why: Capability lookup recorded before dispatch — supports write-before-send invariant
  @key-example
  Scenario: A capability resolution is recorded before the matching specialist is dispatched
    Given the reasoning model requested a tool by name
    When Forge resolves the capability to a specialist
    Then the resolution should be written to pipeline-history before the specialist is invoked
    And the competing candidates and the chosen agent should both be captured

  # Why: Overrides are the learning signal — they must be captured whenever Rich diverges from the gate
  @key-example
  Scenario: An operator override is recorded whenever the reviewer diverges from the gate
    Given Forge has recorded a gate decision
    When the operator's response differs from the gate's recommendation
    Then an override event should be stored in pipeline-history
    And it should be linked to the originating gate decision

  # Why: End-of-build summary is the substrate of cross-build pattern mining
  @key-example @smoke
  Scenario: A session outcome is written when the build reaches a terminal state
    Given a build has completed, failed, or been cancelled
    When the terminal state transition is recorded
    Then a session outcome should be written once
    And it should link to every gate decision and capability resolution produced during the build

  # Why: Priors retrieval — the whole point of writing history
  @key-example @smoke
  Scenario: At build start Forge retrieves priors from similar past builds
    Given previous builds exist for the same feature or project
    When a new build starts
    Then Forge should retrieve recent similar session outcomes
    And Forge should retrieve the operator's recent override behaviour for the capabilities in play
    And Forge should retrieve approved calibration adjustments relevant to the current capabilities
    And these priors should be available to the reasoning model as narrative context

  # Why: Q&A priors retrieval from the calibration group
  @key-example
  Scenario: At build start Forge retrieves Q&A priors for the expected pipeline stages
    Given the operator's past Q&A history has been ingested
    When a new build starts
    Then Forge should retrieve the top matching Q&A events for the build's expected stages
    And these Q&A priors should be available to the reasoning model as narrative context

  # Why: Incremental ingestion — unchanged files are skipped, changed files re-parse
  @key-example
  Scenario: On startup Forge ingests the operator's Q&A history files
    Given the operator's history files are listed in the configuration
    When Forge boots
    Then each history file should be parsed into calibration events
    And each event should be written to the calibration-history memory group
    And a snapshot of each file's content hash should be recorded

  # Why: Git clone + branch preparation is the first durable action of every build
  @key-example @smoke
  Scenario: A dedicated, ephemeral worktree is prepared for every build
    Given a build is about to enter PREPARING
    When the worktree is requested
    Then a fresh worktree should be created under the allowlisted builds directory
    And the worktree path should be tied to this specific build

  # Why: Tests verify the build before it is eligible for PR creation
  # [ASSUMPTION: confidence=medium] The configured test command is pytest run from the worktree root
  @key-example @smoke
  Scenario: The build's tests are executed before a pull request is proposed
    Given the autobuild step has produced changes in the worktree
    When Forge verifies the build
    Then the configured test command should be run inside the worktree
    And the reasoning model should receive the pass/fail summary and failing test details

  # Why: Every build that passes produces a reviewable PR
  # [ASSUMPTION: confidence=high] The default pull-request base branch is main
  @key-example
  Scenario: A pull request is opened when verification passes
    Given verification passed
    When Forge finalises the build
    Then the changes should be committed on the build's branch
    And the branch should be pushed to the remote
    And a pull request should be opened against the default base branch
    And the session outcome should record the pull-request URL

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Subprocess default timeout — at-limit and just-under pair
  @boundary
  Scenario Outline: A subprocess call is allowed to run up to the configured timeout
    Given the default subprocess timeout is configured
    When a subprocess runs for <duration>
    Then the outcome should be <result>

    Examples:
      | duration                         | result                                                |
      | just under the default timeout   | the subprocess should complete and return its output  |
      | exactly at the default timeout   | the subprocess should complete and return its output  |

  # Why: Just-outside boundary — subprocess exceeding timeout is terminated
  @boundary @negative
  Scenario: A subprocess that exceeds the configured timeout is terminated
    Given the default subprocess timeout is configured
    When a subprocess runs for longer than the configured timeout
    Then the subprocess should be terminated
    And the outcome should be recorded as a timeout, not a success

  # Why: Learning-loop threshold — proposals only fire above the minimum evidence count
  # [ASSUMPTION: confidence=high] Pattern detection requires at least five supporting overrides before a proposal is emitted
  @boundary
  Scenario Outline: An adjustment proposal is only emitted when the evidence count meets the minimum
    Given the learning loop is watching a capability
    When the recent override count is <count>
    Then the learning loop should <behaviour>

    Examples:
      | count                         | behaviour                                   |
      | one below the minimum         | wait for more evidence and not propose      |
      | exactly at the minimum        | propose a calibration adjustment            |
      | well above the minimum        | propose a calibration adjustment            |

  # Why: Incremental ingestion — sha match is the skip signal
  @boundary
  Scenario Outline: A history file is re-parsed only when its content hash has changed
    Given a history file has been ingested before
    When Forge refreshes the calibration corpus and the file's content hash has <state>
    Then the file should <action>

    Examples:
      | state          | action                                                  |
      | not changed    | be skipped without re-parsing                           |
      | changed        | be re-parsed and new events added to the memory group   |

  # Why: Calibration adjustments expire — at-expiry and after-expiry pair
  # [ASSUMPTION: confidence=high] Adjustments with expires_at in the past are excluded at retrieval, not deleted
  @boundary
  Scenario Outline: Expired calibration adjustments are excluded from retrieval
    Given an approved calibration adjustment exists
    When priors are retrieved and the adjustment's expiry is <state>
    Then the adjustment should <result>

    Examples:
      | state                          | result                                                  |
      | still in the future            | be returned as an active prior                          |
      | exactly at the current time    | be excluded from priors                                 |
      | in the past                    | be excluded from priors but retained for audit          |

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Long-term memory is enrichment, not a hard dependency — builds survive its outages
  @negative
  Scenario: A long-term memory write failure does not halt the build
    Given a stage outcome is being recorded
    When the long-term memory service is unreachable
    Then a structured failure should be logged
    And the authoritative build-history entry should still be committed
    And the build should continue to the next stage

  # Why: Pending calibration adjustments must never be returned as priors
  @negative
  Scenario: Unapproved calibration adjustments are excluded from priors
    Given a proposed calibration adjustment has not yet been approved
    When priors are retrieved at build start
    Then the proposed adjustment should not appear in the narrative context

  # Why: Unchanged Q&A re-ingestion must be idempotent
  @negative
  Scenario: Re-ingesting the same Q&A events produces no duplicates
    Given a batch of history events has already been ingested
    When the same source file is parsed again
    Then each previously-seen event should be recognised as a duplicate
    And no duplicate calibration events should appear in the memory group

  # Why: Partial parse failures must preserve what was parsed
  @negative
  Scenario: A malformed section in a history file is tolerated without losing earlier events
    Given a history file contains a malformed section
    When Forge ingests the file
    Then the events before the malformed section should still be written
    And the file snapshot should be marked as partial so that it will be re-tried on the next refresh

  # Why: Test failures must be observable, not silent
  # [ASSUMPTION: confidence=medium] The failed-test report delivered to the reasoning model carries pass/fail counts, failing test identifiers, and a tail of captured output
  @negative
  Scenario: Failing tests are reported to the reasoning model rather than crashing the build
    Given the autobuild step produced changes
    When the test run reports failures
    Then the reasoning model should receive the failure summary and the failing test identifiers
    And the build should remain in a state where the reasoning model can decide how to respond

  # Why: Pull-request creation is not always possible — missing credentials must be surfaced safely
  @negative
  Scenario: A pull request cannot be opened when GitHub credentials are missing
    Given the GitHub credentials are not available in the environment
    When Forge attempts to open a pull request
    Then the attempt should fail with a structured error
    And no pull-request URL should be recorded on the session outcome
    And the session outcome should still be written with the failure reason

  # Why: Constitutional permissions must refuse any command outside the allowlist
  @negative @security
  Scenario: A shell command that is not on the allowlist is refused
    Given the subprocess permissions list a fixed set of allowed binaries
    When the reasoning model attempts to invoke a binary that is not on the allowlist
    Then the invocation should be refused before any process is spawned
    And the refusal should be recorded with the attempted command and the operator's current permissions

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Write ordering invariant — authoritative store commits before long-term memory mirror
  @edge-case
  Scenario: The authoritative build history is committed before the long-term memory mirror
    Given a stage result has arrived
    When Forge records the stage
    Then the durable build-history entry should be committed first
    And only then should the matching long-term memory entry be written

  # Why: Reconcile backfills anything that failed to land in long-term memory
  @edge-case
  Scenario: Entries missing from long-term memory are backfilled on the next build
    Given a previous build wrote build-history entries that never reached long-term memory
    When the next build starts
    Then Forge should detect the entries missing from long-term memory
    And Forge should backfill them into the pipeline-history memory group

  # Why: Session outcomes are never in-progress — only terminal
  @edge-case
  Scenario: A build interrupted mid-run never leaves an in-progress session outcome behind
    Given a build has been interrupted before a terminal state was reached
    When Forge inspects its long-term memory for the build
    Then no non-terminal session outcome should exist for that build
    And the interruption should be represented only by the individual stage entries already written

  # Why: Cross-group edges — pipeline-history references calibration-history
  @edge-case
  Scenario: A gate decision can reference the Q&A event that informed it
    Given a gate decision drew on a prior Q&A event
    When the gate decision is written to pipeline-history
    Then the decision should carry a link to the originating Q&A event in calibration-history

  # Why: Worktree cleanup is best-effort — failure must not hold up the state machine
  @edge-case
  Scenario: A worktree that cannot be deleted does not block the terminal state transition
    Given the build has reached a terminal state
    When the worktree cleanup fails
    Then the cleanup failure should be logged
    And the build should still be marked as terminal
    And the build's durable history should still be finalised

  # Why: Incremental ingestion on every boot and build finish
  @edge-case
  Scenario: Changed Q&A files are ingested again after every completed build
    Given a completed build has finished
    And one of the operator's history files has changed since it was last parsed
    When the post-build refresh runs
    Then the changed file should be re-ingested
    And any unchanged files should be skipped

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security / Concurrency / Data Integrity / Integration
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Subprocess working directory is confined to the ephemeral worktree
  @security
  Scenario: A subprocess cannot be launched with a working directory outside the worktree
    Given the working-directory allowlist permits only per-build worktrees
    When a subprocess is requested with a working directory outside the allowlist
    Then the invocation should be refused before any process is spawned

  # Why: GitHub credentials come only from the environment, never from forge.yaml
  @security
  Scenario: GitHub operations use credentials from the environment rather than configuration
    Given GitHub credentials are provided by the deployment environment
    When Forge creates a pull request
    Then the credentials should be read from the environment
    And no GitHub credentials should ever be read from the project configuration file

  # Why: Duplicate ingestion must be a true no-op — deterministic identity
  @data-integrity
  Scenario: A Q&A event has a deterministic identity so re-ingestion never creates duplicates
    Given the same history event is parsed twice
    When the event is written to the calibration-history memory group
    Then both writes should resolve to the same stored entity
    And the retrievable count of that event should remain one

  # Why: Concurrent gate writes within a build must not interleave or corrupt each other
  # [ASSUMPTION: confidence=low] Gate-decision links inside a session outcome are ordered chronologically by decided_at ascending
  @concurrency
  Scenario: Gate decisions produced in close succession are recorded in order
    Given two stage results arrive for the same build within a short window
    When both are recorded
    Then both gate decisions should appear in the session outcome's linked entries
    And the session outcome should link to them in the order they were decided

  # Why: Reviewer approval round-trip flips the approved flag, making the adjustment visible to priors
  @integration
  Scenario: An approved calibration adjustment becomes visible to subsequent builds
    Given the learning loop proposed a calibration adjustment
    When the operator approves it via the approval round-trip
    Then the adjustment should be marked approved
    And the next build should retrieve the adjustment as a prior

  # Why: History-parser idempotency is provenance-safe — re-running on the same file yields the same counts
  @data-integrity
  Scenario: A second ingestion pass on an unchanged history file reports zero new events
    Given a history file has been fully ingested
    When the file is re-scanned without having changed
    Then the ingestion result should report zero new events
    And no write operations should be issued against the memory service

  # Why: Branches, commits, pushes, and PR titles flow end-to-end from a single build
  # [ASSUMPTION: confidence=high] The default pull-request base branch is main
  @integration
  Scenario: A successful end-to-end build produces a branch, a commit, a push, and a pull request
    Given a build has just produced verified changes
    When Forge finalises the build
    Then a build-specific branch should exist locally
    And the branch should carry a commit summarising the build
    And the branch should be pushed to the remote
    And a pull request should be opened against the default base branch
    And the session outcome should record the pull-request URL

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E (expansion) — additional Security / Concurrency / Data Integrity / Integration
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Secrets in rationale fields should never leak into long-term memory
  # [ASSUMPTION: confidence=low] Credential-shaped substrings in rationale text are redacted by a pattern-based filter before write
  @edge-case @security
  Scenario: Secrets appearing in rationale text are redacted before long-term memory is written
    Given a gate decision's rationale contains something that looks like a credential
    When the gate decision is written to long-term memory
    Then the credential-shaped content should be redacted from the stored rationale

  # Why: Filesystem isolation mirrors shell isolation
  @edge-case @security
  Scenario: A file read outside the allowlist is refused
    Given the filesystem read allowlist is configured
    When a tool attempts to read a file outside the allowlist
    Then the read should be refused before any bytes are returned

  # Why: Priors content is never trusted as an executable input
  @edge-case @security
  Scenario: Retrieved priors are not used as subprocess arguments
    Given priors have been retrieved from long-term memory
    When Forge invokes a subprocess
    Then the subprocess arguments should be derived only from configuration or reasoning-model decisions
    And no retrieved prior text should be passed directly as a shell argument

  # Why: Split-brain safety — two Forge instances must not race on the same build
  # [ASSUMPTION: confidence=low] Split-brain dedupe relies on entity_id uniqueness at the memory-service level; a pre-check skips an already-present mirror write
  @edge-case @concurrency
  Scenario: A second Forge instance cannot mirror a stage that has already been mirrored
    Given one Forge instance has already written a stage entry to long-term memory
    When a second Forge instance attempts to mirror the same stage
    Then the second write should be recognised as already present and skipped

  # Why: Recency horizon — very old overrides should not skew pattern detection
  # [ASSUMPTION: confidence=high] The recency horizon for counting overrides in pattern detection is thirty days
  @edge-case @concurrency
  Scenario: Override counts used by pattern detection honour a bounded recency horizon
    Given operator overrides exist across a wide time range
    When the learning loop counts recent overrides for a capability
    Then only overrides inside the configured recency horizon should be counted
    And older overrides should be excluded from the count

  # Why: Supersession chains must terminate
  @edge-case @data-integrity
  Scenario: A calibration adjustment cannot supersede one that already supersedes it
    Given an approved calibration adjustment chain exists
    When a new adjustment is proposed
    Then the supersession chain should be walked before write
    And a proposal that would create a cycle should be rejected

  # Why: Session outcomes are written exactly once, even under retry
  @edge-case @data-integrity
  Scenario: A session outcome is written exactly once even when the terminal transition is retried
    Given the terminal transition handler has already written the session outcome
    When the transition handler is retried
    Then no additional session outcome should be written
    And the existing session outcome should be preserved as the single record

  # Why: Empty priors are not a failure mode
  # [ASSUMPTION: confidence=medium] When no priors match, retrieval returns an empty narrative section rather than omitting the block or erroring
  @edge-case @integration
  Scenario: Priors retrieval returns an empty context when there is nothing to retrieve
    Given the memory groups contain no entries relevant to the current build
    When priors are retrieved at build start
    Then the retrieval should return an empty narrative context
    And the build should proceed without priors
Write infrastructure-coordination_assumptions.yaml
66 lines
# Assumptions manifest for infrastructure-coordination.feature
# Generated by /feature-spec

feature: "Infrastructure Coordination"
feature_id: "FEAT-FORGE-006"
generated: "2026-04-24T00:00:00Z"
stack: "python"
review_required: true  # 3 low-confidence assumptions accepted as defaults

assumptions:
  - id: "ASSUM-001"
    scenario: "Override counts used by pattern detection honour a bounded recency horizon"
    assumption: "The recency horizon used when counting overrides for pattern detection is 30 days"
    confidence: high
    basis: "DM-graphiti-entities.md §4 — 'OverrideEvent on same target_identifier in last 30 days'"
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "A pull request is opened when verification passes / A successful end-to-end build produces a branch, a commit, a push, and a pull request"
    assumption: "The default base branch for pull requests is main"
    confidence: high
    basis: "API-subprocess.md §4 — create_pr signature declares base: str = 'main'"
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "The build's tests are executed before a pull request is proposed"
    assumption: "The configured test command for Python builds is pytest, run from the worktree root"
    confidence: medium
    basis: "Python stack convention; the build plan references 'test verification' without naming a command"
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "Failing tests are reported to the reasoning model rather than crashing the build"
    assumption: "The failed-test report delivered to the reasoning model carries pass/fail counts, failing test identifiers, and a tail of captured output"
    confidence: medium
    basis: "pytest convention; the precise structure of the result dictionary is not specified in any provided context file"
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "Priors retrieval returns an empty context when there is nothing to retrieve"
    assumption: "When no priors match, retrieval returns an empty narrative section in the system prompt rather than omitting the block or raising"
    confidence: medium
    basis: "ADR-ARCH-018 says priors are injected prose-style; the empty-case representation is not explicitly specified"
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "Secrets appearing in rationale text are redacted before long-term memory is written"
    assumption: "Credential-shaped substrings (tokens, keys, secrets) in the rationale field are redacted by a pattern-based filter before the entity is written to long-term memory"
    confidence: low
    basis: "Inferred security hygiene; no provided context file specifies redaction rules for Graphiti writes"
    human_response: "confirmed (default accepted — REVIEW REQUIRED)"

  - id: "ASSUM-007"
    scenario: "A second Forge instance cannot mirror a stage that has already been mirrored"
    assumption: "Split-brain dedupe for mirror writes relies on entity_id uniqueness at the memory-service level; an already-present mirror write is recognised and skipped"
    confidence: low
    basis: "Inferred from the CalibrationEvent deterministic entity_id pattern; GateDecision uses a UUID so a separate 'already written' check is assumed for the split-brain case"
    human_response: "confirmed (default accepted — REVIEW REQUIRED)"

  - id: "ASSUM-008"
    scenario: "Gate decisions produced in close succession are recorded in order"
    assumption: "Gate-decision links inside a session outcome are ordered chronologically by decided_at ascending"
    confidence: low
    basis: "Natural ordering; no provided context file specifies the link-ordering semantics of SessionOutcome -[CONTAINS]-> GateDecision edges"
    human_response: "confirmed (default accepted — REVIEW REQUIRED)"
Write infrastructure-coordination_summary.md
99 lines
# Feature Spec Summary: Infrastructure Coordination

**Feature ID**: FEAT-FORGE-006
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 43 total (6 smoke, 0 regression)
**Assumptions**: 8 total (2 high / 3 medium / 3 low confidence)
**Review required**: Yes — 3 low-confidence assumptions accepted as defaults

## Scope

Specifies Forge's cross-build memory and infrastructure plumbing: seeding
`GateDecision`, `CapabilityResolution`, `OverrideEvent`, `CalibrationAdjustment`,
and `SessionOutcome` entities into the `forge_pipeline_history` Graphiti group
after each pipeline stage; ingesting the operator's Q&A history files into the
`forge_calibration_history` group with content-hash-based incremental refresh;
retrieving priors (similar past builds, recent override behaviour, approved
adjustments, Q&A priors) at build start for injection into the reasoning model's
system prompt; verifying autobuild changes via the configured test command
inside the build's ephemeral worktree; and driving git / `gh` operations
(branch, commit, push, pull-request creation) through DeepAgents' `execute`
tool under the constitutional subprocess-permissions constraint. Write ordering,
reconciliation, idempotency, cross-group edges, and learning-loop evidence
thresholds are all described in domain terms; storage primitives (FalkorDB,
SQLite DDL, shell invocation shapes) appear only as capability observations.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 10 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 7 |
| Edge cases (@edge-case) | 14 |
| Smoke (@smoke) | 6 |
| Regression (@regression) | 0 |
| Security (@security) | 6 |
| Concurrency (@concurrency) | 3 |
| Data integrity (@data-integrity) | 4 |
| Integration (@integration) | 3 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + security). Group totals do not sum to 43.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — stage seeding, capability resolution, overrides, session outcome, priors retrieval (runtime + Q&A), history ingestion, worktree preparation, test verification, PR creation | 10 |
| B | Boundary Conditions — subprocess timeout, learning-loop evidence minimum, history-file hash change, adjustment expiry | 5 |
| C | Negative Cases — memory write failure tolerated, unapproved adjustments filtered, re-ingestion idempotency, partial parse tolerance, failing tests reported, missing credentials, disallowed binary refused | 7 |
| D | Edge Cases — write ordering, reconcile backfill, no in-progress session outcome, cross-group edges, worktree cleanup best-effort, post-build ingestion refresh | 6 |
| E | Security / Concurrency / Data Integrity / Integration — working-directory allowlist, env-only credentials, deterministic Q&A identity, in-order gate links, approved-adjustment visibility, re-scan zero-writes, end-to-end branch+commit+push+PR | 7 |
| E (expansion) | Additional scenarios — rationale-field secret redaction, filesystem read allowlist, priors-as-argument refusal, split-brain mirror dedupe, recency-horizon bound, supersession-cycle rejection, session-outcome retry idempotency, empty priors representation | 8 |

## Deferred Items

None.

## Open Assumptions (low confidence — human verification recommended)

- **ASSUM-006** — Credential-shape redaction in rationale fields before long-term memory write. Not stated in any provided context file; accepted as default pattern-based filter.
- **ASSUM-007** — Split-brain dedupe mechanism for mirror writes. Inferred from `CalibrationEvent.entity_id` pattern; `GateDecision` uses a UUID, so a separate pre-check is assumed.
- **ASSUM-008** — Ordering of `GateDecision` links inside a `SessionOutcome`. Inferred as chronological by `decided_at` ascending; not specified.

## Assumptions Summary

| ID | Confidence | Response |
|----|------------|----------|
| ASSUM-001 | high | confirmed |
| ASSUM-002 | high | confirmed |
| ASSUM-003 | medium | confirmed |
| ASSUM-004 | medium | confirmed |
| ASSUM-005 | medium | confirmed |
| ASSUM-006 | low | confirmed (default — REVIEW REQUIRED) |
| ASSUM-007 | low | confirmed (default — REVIEW REQUIRED) |
| ASSUM-008 | low | confirmed (default — REVIEW REQUIRED) |

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. This feature writes
  `SessionOutcome` on terminal state transitions defined by FEAT-FORGE-001, and
  mirrors every `stage_log` row committed by FEAT-FORGE-001's SQLite adapter into
  the `forge_pipeline_history` Graphiti group.
- **FEAT-FORGE-002** — NATS Fleet Integration. This feature records
  `CapabilityResolution` entries produced by FEAT-FORGE-002's discovery cache, and
  records `OverrideEvent` entities generated by reviewer responses arriving via
  the NATS approval round-trip.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Infrastructure Coordination" \
      --context features/infrastructure-coordination/infrastructure-coordination_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
Bash Verify output files
IN
ls -la /home/richardwoollcott/Projects/appmilla_github/forge/features/infrastructure-coordination/

OUT
total 48
drwxrwxr-x 2 richardwoollcott richardwoollcott  4096 Apr 24 20:04 .
drwxrwxr-x 8 richardwoollcott richardwoollcott  4096 Apr 24 20:01 ..
-rw-rw-r-- 1 richardwoollcott richardwoollcott  3827 Apr 24 20:03 infrastructure-coordination_assumptions.yaml
-rw-rw-r-- 1 richardwoollcott richardwoollcott 25164 Apr 24 20:03 infrastructure-coordination.feature
-rw-rw-r-- 1 richardwoollcott richardwoollcott  5305 Apr 24 20:04 infrastructure-coordination_summary.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: Infrastructure Coordination (FEAT-FORGE-006)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Files written:

features/infrastructure-coordination/infrastructure-coordination.feature
features/infrastructure-coordination/infrastructure-coordination_assumptions.yaml
features/infrastructure-coordination/infrastructure-coordination_summary.md
Scenarios: 43 total

@key-example: 10 · @boundary: 5 · @negative: 7 · @edge-case: 14
@smoke: 6 · @regression: 0
@security: 6 · @concurrency: 3 · @data-integrity: 4 · @integration: 3
Assumptions: 8 total

high: 2 (auto-proceed) · medium: 3 (Coach review recommended) · low: 3 (human review required)
REVIEW REQUIRED: 3 low-confidence assumptions accepted as defaults

ASSUM-006 — rationale-field credential redaction pattern
ASSUM-007 — split-brain mirror dedupe mechanism
ASSUM-008 — GateDecision link ordering inside SessionOutcome
See infrastructure-coordination_assumptions.yaml for full rationale.

Deferred: none.

Next step:


/feature-plan "Infrastructure Coordination" \
  --context features/infrastructure-coordination/infrastructure-coordination_summary.md



