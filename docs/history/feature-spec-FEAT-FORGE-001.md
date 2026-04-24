/feature-spec FEAT-FORGE-001 \
  --context forge/docs/design/models/DM-build-lifecycle.md \
  --context forge/docs/design/contracts/API-sqlite-schema.md \
  --context forge/docs/design/contracts/API-cli.md \
  --context forge/docs/design/decisions/DDR-003-sqlite-schema-layout-wal.md

Bash Check design docs directory
IN
ls -la /Users/richardwoollcott/Projects/appmilla_github/forge/docs/design/ 2>/dev/null | head -30

OUT
total 0
drwxr-xr-x   6 richardwoollcott  staff  192 Apr 23 18:15 .
drwxr-xr-x  10 richardwoollcott  staff  320 Apr 23 18:15 ..
drwxr-xr-x  11 richardwoollcott  staff  352 Apr 23 20:49 contracts
drwxr-xr-x   8 richardwoollcott  staff  256 Apr 23 20:49 decisions
drwxr-xr-x   4 richardwoollcott  staff  128 Apr 23 18:35 diagrams
drwxr-xr-x   7 richardwoollcott  staff  224 Apr 23 18:28 models
Globpattern: "**/FEAT-FORGE-001*"
No files found
Globpattern: "features/**/*.feature"
No files found
Globpattern: "docs/design/**/*.md"
Found 22 files
Read DM-build-lifecycle.md
Read API-sqlite-schema.md
Read API-cli.md
Read DDR-003-sqlite-schema-layout-wal.md
Bash List root directory
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/ | head -40

OUT
AGENTS.md
command_history.md
docs
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
Globpattern: "**/FEAT-*.md"
Found 6 files
Globpattern: "**/features.yaml"
No files found
Bash List docs directory
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/docs/ 2>/dev/null

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
Globpattern: "docs/product/**/*.md"
Found 7 files
Bash List product docs
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/docs/product/

OUT
feature_spec_inputs
roadmap.md
Bash List feature spec inputs
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/

OUT
FEAT-PO-001.md
FEAT-PO-002.md
FEAT-PO-003.md
FEAT-PO-004.md
FEAT-PO-005.md
FEAT-PO-006.md
Grep "FEAT-FORGE-001"
7 lines of output
Grep "FEAT-FORGE-001" (in /Users/richardwoollcott/Projects/appmilla_github/forge/docs/product/roadmap.md)
37 lines of output
Grep "FEAT-FORGE-001" (in /Users/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md)
251 lines of output
Globpattern: "features/**/*"
No files found
Bash Check for features directory
IN
ls /Users/richardwoollcott/Projects/appmilla_github/forge/features 2>/dev/null || echo "no features dir"

OUT
no features dir
Read pyproject.toml
Context loaded: stack=python, 0 existing .feature files, context files cover build lifecycle state machine, SQLite schema/WAL, CLI contract, and DDR-003 schema rationale.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Pipeline State Machine & Configuration (FEAT-FORGE-001)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Feature: Pipeline State Machine and Configuration
  As Rich (the sole operator of Forge)
  I want to enqueue, observe, and steer pipeline builds through a durable state machine
  So that I always have a consistent, crash-safe view of what the pipeline is doing and have
  a reliable way to start, pause, cancel, or skip stages from the command line.

  Background:
    Given Forge is running with a loaded configuration
    And the build history store is empty
    And the pipeline is idle with no active build

━━ GROUP A: Key Examples (6 scenarios) ━━

  # Why: Core write path — enqueue a feature for build from the CLI
  @key-example @smoke
  Scenario: Queueing a new feature creates a pending build
    Given I have a feature description at a permitted repository path
    When I queue the feature for a build
    Then a new build should be recorded as pending pickup
    And the queue should report a correlation identifier for the request
    And the command should report success

  # Why: Full happy-path lifecycle — the build completes end to end
  @key-example @smoke
  Scenario: A queued build progresses through the lifecycle to completion
    Given a build has been queued for a feature
    When the pipeline picks up the build and all stages succeed
    Then the build should transition from pending pickup, to preparation, to running, to finalisation, to complete
    And the build should record the moment preparation began and the moment it completed
    And the build should expose the pull request it produced

  # Why: Core read path — `forge status` surfaces live activity without touching NATS
  @key-example @smoke
  Scenario: Viewing status shows all non-terminal builds and recent outcomes
    Given one build is currently running and one build completed yesterday
    When I ask for the current pipeline status
    Then the running build should be shown as active with its current stage
    And the recently completed build should be shown as a recent outcome
    And the status query should not require the pipeline agent to be reachable

  # Why: Core read path — `forge history` summarises prior builds for a feature
  @key-example
  Scenario: Viewing history for a specific feature lists all prior attempts
    Given three previous builds exist for the same feature, two complete and one failed
    When I ask for the history of that feature
    Then all three attempts should be listed from most recent to oldest
    And each attempt should display its final outcome and the stages it went through

  # Why: Configuration loading — defaults come from `forge.yaml`, overridable on the command line
  @key-example
  Scenario: Queueing a build applies configuration defaults when no overrides are given
    Given the configuration specifies a default reasoning-turn budget and a default stage timeout
    When I queue a feature without specifying those values
    Then the new build should adopt the configured defaults
    And when I override one of those values on the command line
    Then the new build should record the overridden value instead of the default

  # Why: Single-writer discipline — the read path is always available, even during writes
  @key-example
  Scenario: Status and history queries remain responsive while a build is actively writing
    Given a build is actively running and recording stage outcomes
    When I ask for status at the same moment
    Then the status query should return promptly without waiting for the writer

━━ GROUP B: Boundary Conditions (6 scenarios) ━━

  # Why: Just-inside and at-boundary values for the reasoning-turn budget
  @boundary
  Scenario Outline: Reasoning-turn budget overrides are accepted within the permitted range
    Given the default reasoning-turn budget is five
    When I queue a feature with a reasoning-turn budget of <turns>
    Then the build should be recorded with a reasoning-turn budget of <turns>

    Examples:
      | turns |
      | 1     |
      | 5     |
      | 20    |

  # Why: Just-outside boundary — zero or negative turn budgets are nonsensical
  @boundary @negative
  Scenario Outline: Reasoning-turn budgets outside the permitted range are rejected
    When I queue a feature with a reasoning-turn budget of <turns>
    Then the queue command should be rejected with a configuration error
    And no new build should be recorded

    Examples:
      | turns |
      | 0     |
      | -1    |

  # Why: History limit default and just-inside bound
  @boundary
  Scenario Outline: History returns at most the requested number of entries
    Given <available> prior builds exist
    When I ask for history with a limit of <limit>
    Then at most <expected> entries should be returned in newest-first order

    Examples:
      | available | limit | expected |
      | 10        | 50    | 10       |
      | 60        | 50    | 50       |
      | 60        | 1     | 1        |

  # Why: Just-inside default — `forge history` with no arguments returns the 50 most recent builds
  @boundary
  Scenario: History with no arguments returns the 50 most recent builds by default
    Given 75 prior builds exist
    When I ask for history without specifying a limit
    Then exactly 50 entries should be returned
    And they should be the 50 most recently queued

  # Why: Uniqueness invariant — a feature cannot be queued twice within the same correlation scope
  @boundary @negative
  Scenario: Queueing the same feature with the same correlation identifier twice is refused
    Given a build already exists for a feature under a specific correlation identifier
    When I queue the same feature again with the same correlation identifier
    Then the second queue attempt should be refused as a duplicate
    And only one build should remain recorded for that combination

  # Why: Status views bound the set of stages shown in the full detail view
  @boundary
  Scenario: Full status view includes the most recent stages per build
    Given a build has completed seventeen stages
    When I ask for status with full detail for that build
    Then at most the five most recent stages should be shown by default

━━ GROUP C: Negative Cases (7 scenarios) ━━

  # Why: Path allowlist is a safety boundary — queueing outside it must fail fast
  @negative
  Scenario: Queueing a build against a repository path outside the allowlist is refused
    Given a repository path that is not on the allowlist
    When I attempt to queue a feature for that repository
    Then the queue command should be refused with an unauthorised-path error
    And no new build should be recorded
    And no message should be published to the pipeline

  # Why: Active-duplicate guard — a feature already in flight cannot be re-queued
  @negative
  Scenario: Queueing a feature that already has an in-flight build is refused
    Given a build for a feature is currently pending pickup, preparing, running, or paused
    When I queue the same feature again
    Then the queue command should be refused as a duplicate in-flight build
    And the existing build should be unaffected

  # Why: Skip is only meaningful on a paused build awaiting review
  @negative
  Scenario: Skipping a build that is not paused is refused
    Given the pipeline is running a build that is not paused for review
    When I ask to skip the current stage of that build
    Then the skip command should be refused with an error
    And no skip decision should be sent to the pipeline

  # Why: Cancel resolves through the build history; missing builds should be reported, not crashed
  @negative
  Scenario: Cancelling a feature with no active or recent builds is refused
    Given there is no build on record for a given feature
    When I ask to cancel that feature
    Then the cancel command should be refused with a not-found error

  # Why: Crash during preparation should be surfaced as a failure, not a silent hang
  @negative
  Scenario: A feature description that fails validation causes preparation to fail
    Given a build has been picked up and its feature description is invalid
    When preparation attempts to validate the description
    Then the build should transition from preparation to failed
    And the build should record a structured reason describing the validation problem

  # Why: Hard-stop gate decision halts the build and is not retryable by the pipeline itself
  @negative
  Scenario: A hard-stop gate decision during running transitions the build to failed
    Given a running build has just produced a stage outcome that triggers a hard-stop decision
    When the gate resolves with no retry path
    Then the build should transition from running to failed
    And the completion time of the build should be recorded

  # Why: State machine invariant — transitions that are not in the allowed table are rejected
  @negative
  Scenario: Attempting to transition a build through an invalid path is refused
    Given a build is currently pending pickup
    When the pipeline attempts to transition that build directly to running without preparing first
    Then the transition should be refused as invalid
    And the build should remain pending pickup

━━ GROUP D: Edge Cases (9 scenarios) ━━

  # Why: Crash during preparation — redelivery restarts preparation from scratch
  @edge-case
  Scenario: A crash during preparation marks the build for redelivery
    Given a build had reached preparation
    When the pipeline process crashes before preparation completed
    And the pipeline process restarts and reconciles the build history
    Then the build should be marked as interrupted with a recoverable reason
    And the build should subsequently be picked up again and re-enter preparation

  # Why: Crash during running — retry-from-scratch policy
  @edge-case
  Scenario: A crash during running marks the build as interrupted and restarts it from scratch
    Given a build was in the running state when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then the build should be marked as interrupted
    And the build should be re-picked up and restart from preparation

  # Why: Crash during finalisation — the PR may have been created, so operator reconciles
  @edge-case
  Scenario: A crash during finalisation marks the build as interrupted with an operator warning
    Given a build was finalising when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then the build should be marked as interrupted
    And the recovery report should warn that the pull request may have been created and require manual reconciliation

  # Why: A paused build survives a crash — the approval request is re-issued on restart
  @edge-case
  Scenario: A paused build survives a pipeline crash and re-issues its approval request
    Given a build is paused awaiting a review decision
    When the pipeline crashes and restarts
    Then the build should remain paused after reconciliation
    And the pending approval request should be re-issued so reviewers can still respond

  # Why: Terminal states after a crash are a no-op — we must not restart completed builds
  @edge-case
  Scenario Outline: Builds in terminal states after a crash are not re-picked
    Given a build was already in the <terminal> state when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then no new work should be started for that build
    And any outstanding delivery for that build should be acknowledged

    Examples:
      | terminal   |
      | complete   |
      | failed     |
      | cancelled  |
      | skipped    |

  # Why: `forge cancel` during a paused state is modelled as a synthetic rejection
  @edge-case
  Scenario: Cancelling a paused build resolves its pending approval as a rejection
    Given a build is paused awaiting a review decision
    When I ask to cancel that build with a reason
    Then the cancel command should resolve the pending review as a rejection on my behalf
    And the build should transition from paused to cancelled
    And the reason I supplied should be recorded on the build

  # Why: `forge skip` during a flag-for-review pause continues the build without that stage
  @edge-case
  Scenario: Skipping a stage on a flagged-for-review pause resumes the build and marks the stage skipped
    Given a build is paused on a flag-for-review gate
    When I ask to skip the flagged stage with a reason
    Then the paused stage should be recorded as skipped with my reason
    And the build should resume from running
    And the overall build should still be allowed to complete successfully

  # Why: Sequential queue discipline — only one build runs at a time per project
  @edge-case
  Scenario: A second queued build waits until the first completes before starting
    Given one build is already running for a project
    When I queue a second feature for the same project
    Then the second build should be recorded as pending pickup
    And the second build should not begin preparation until the first build has reached a terminal state

  # Why: Watch mode streams state changes without blocking writes
  @edge-case
  Scenario: Watching status refreshes the view as the build progresses
    Given a build is running and moving through stages
    When I watch the status view
    Then the view should refresh to reflect each new stage as it begins
    And the view should stop refreshing once the build reaches a terminal state

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 28 scenarios across 4 groups
Inferred assumptions: 6 (will be resolved in Phase 5)
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

Fast path: type "A A A A" to accept all four groups at once.

GROUP A — Key Examples (6 scenarios): _
GROUP B — Boundary Conditions (6 scenarios): _
GROUP C — Negative Cases (7 scenarios): _
GROUP D — Edge Cases (9 scenarios): _
Awaiting your curation decisions for Groups A–D. After curation I'll offer optional edge-case expansion (security / concurrency / integration boundaries) in Phase 4, then resolve inferred assumptions (Phase 5) before writing the three output files.

forge-build-plan.md#142-151
A A A A
All four groups accepted. Proceeding to Phase 4.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I've identified 7 additional scenarios covering security, concurrency, data integrity,
and integration boundaries. Summary:

Security (2):
  - Queueing with a feature identifier containing path-traversal characters is refused
  - Cancelling a build triggered by another user still resolves cleanly
    (single-operator pipeline, but the identity is recorded for audit)

Concurrency (2):
  - Two CLI invocations queue different features at the same instant — both succeed,
    both appear in order, neither corrupts the build history
  - A second CLI reader during active writes sees a consistent snapshot
    (strengthens the @key-example "responsive while writing" scenario with a
     stronger concurrency guarantee)

Data integrity (2):
  - A terminal-state build always has a completion time recorded
    (cross-state invariant derived from the data model)
  - The pipeline publish step fails after the build row was written —
    the build should be visibly recoverable rather than silently stuck

Integration boundaries (1):
  - The pipeline is unreachable when the CLI tries to queue — the CLI reports
    a publish failure and does not leave a half-written build in history

Include them?

[Y] Yes — show all seven and curate them (same A/R/M/+/? actions per group)
[S] Sample — show 2 examples in full, then decide
[N] No — skip and proceed to Phase 5 (assumption resolution)

Your choice [Y/S/N]: _
Y

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION — Full Scenarios
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━ GROUP E: Security (2 scenarios) ━━

  # Why: Feature identifier is used in build identifier and on-disk paths — traversal must be refused
  @edge-case @negative
  Scenario: Queueing with a feature identifier that contains path-traversal characters is refused
    When I attempt to queue a feature whose identifier contains traversal characters such as "../"
    Then the queue command should be refused with a validation error
    And no new build should be recorded
    And no message should be published to the pipeline

  # Why: Audit trail — the identity of the operator who triggered the build is preserved through cancel
  @edge-case
  Scenario: Cancelling a build records the cancelling operator even when different from the originator
    Given a build is currently running and was originated by one operator
    When a different operator cancels the build with a reason
    Then the build should transition to cancelled
    And the originating operator should remain recorded on the build
    And the cancelling operator and their reason should be recorded on the resolution

━━ GROUP F: Concurrency (2 scenarios) ━━

  # Why: Two simultaneous queue commands must both be durable and ordered
  @edge-case
  Scenario: Two features queued at the same instant both appear in history
    When two different features are queued at effectively the same instant
    Then both builds should be recorded as pending pickup
    And both builds should be assigned distinct build identifiers
    And both builds should appear in history with their original queue-time ordering preserved

  # Why: WAL guarantee — readers see a consistent snapshot even during active writes
  @edge-case
  Scenario: A concurrent reader sees a consistent snapshot while the pipeline is writing
    Given the pipeline is in the middle of recording a new stage outcome
    When a second reader asks for history at the same instant
    Then the reader should see a consistent snapshot from the last committed write
    And the reader should never observe a partially written stage

━━ GROUP G: Data Integrity (2 scenarios) ━━

  # Why: Invariant — a terminal build always has a recorded completion time
  @edge-case
  Scenario Outline: Any build that reaches a terminal state has a recorded completion time
    When a build reaches the <terminal> state
    Then the build should have a completion time recorded
    And no further state transitions should be permitted for that build

    Examples:
      | terminal   |
      | complete   |
      | failed     |
      | cancelled  |
      | skipped    |

  # Why: Partial-failure between local write and pipeline publish must be recoverable, not silent
  @edge-case
  Scenario: A build row is written but the pipeline publish then fails
    Given queueing writes the build history row before publishing to the pipeline
    When the local write succeeds but the pipeline publish then fails
    Then the queue command should report a publish failure
    And the build should be visible as pending pickup so it can be reconciled or re-queued
    And the operator should be told the pipeline was not notified

━━ GROUP H: Integration Boundaries (1 scenario) ━━

  # Why: The CLI must fail cleanly when the pipeline messaging layer is unreachable
  @edge-case @negative
  Scenario: Queueing fails cleanly when the pipeline messaging layer is unreachable
    Given the pipeline messaging layer cannot be reached from the CLI
    When I attempt to queue a feature
    Then the queue command should fail with a messaging-layer error
    And the failure message should identify the messaging layer as the unreachable dependency
    And subsequent status queries should still work without the messaging layer

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Review each expansion group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP E — Security (2 scenarios): _
GROUP F — Concurrency (2 scenarios): _
GROUP G — Data Integrity (2 scenarios): _
GROUP H — Integration Boundaries (1 scenario): _

Fast path: "A A A A" accepts all four expansion groups.
A A A A

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (6 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter or reply "ok" to accept;
otherwise type the actual value.

[1] Minimum reasoning-turn budget
    Proposed: 1  Confidence: medium
    Basis: Domain convention — a zero-turn budget cannot perform any reasoning.
           Not stated explicitly in forge.yaml schema or CLI contract.
    Accept or enter value: _

[2] Maximum reasoning-turn budget (used in Group B boundary Examples)
    Proposed: 20  Confidence: low
    Basis: Chosen as an illustrative upper bound; no ceiling stated anywhere in
           the context files. Alternative: remove upper-bound boundary scenarios
           entirely and treat the value as unbounded.
    Accept, enter value, or reply "remove" to drop the upper bound: _

[3] Feature identifier validation rejects path-traversal sequences
    Proposed: yes — reject identifiers containing "../", "/", or "\"
    Confidence: medium
    Basis: feature_id is interpolated into build_id ("build-{feature_id}-{ts}") and
           into worktree paths ("/var/forge/builds/{build_id}/"). Traversal would
           escape the forge-managed filesystem root. Not stated explicitly, but
           implied by the path layout.
    Accept or enter value: _

[4] Sequential-queue scope
    Proposed: per-project (one active build at a time per project; different
              projects can run concurrently)
    Confidence: medium
    Basis: Roadmap says "sequential build queue". API-cli §9 says project
           scoping is inferred from repo path and NATS subjects are per-project.
           Per-project sequencing aligns with both, but "sequential" could also
           mean globally serialised.
    Accept, or reply "global" for globally-serialised: _

[5] Cancelling operator is recorded distinctly from the originating operator
    Proposed: yes — originating_user preserved unchanged; cancel reason+responder
              recorded on the resolution record for audit
    Confidence: low
    Basis: API-cli §6.2 describes cancellation as a "synthetic
           ApprovalResponsePayload(responder='rich', reason='cli cancel')" —
           responder is captured in details_json.approval.response. Whether
           originating_user is preserved separately is implied but not stated.
    Accept or enter value: _

[6] Queue-row visibility after a pipeline-publish failure
    Proposed: the pre-written QUEUED row remains visible in history so the
              operator can observe the failed-to-publish state and reconcile or
              re-queue manually
    Confidence: medium
    Basis: API-cli §3.3 step 4 is explicit that the row is written BEFORE publish
           (so `forge status` shows it). What happens if step 5 (publish) then
           fails is not stated. Alternatives: roll back the row; mark the row as
           "publish-failed"; keep the row as QUEUED.
    Accept, or reply with alternative ("rollback" / "mark-failed"): _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Awaiting responses 1–6 (numbered or in order).

accept defaults
Writing the three output files.

Write pipeline-state-machine-and-configuration.feature
380 lines
# Generated by /feature-spec
# Feature: Pipeline State Machine and Configuration (FEAT-FORGE-001)
# Stack: python
# Assumptions: 6 (see pipeline-state-machine-and-configuration_assumptions.yaml)
# Generated: 2026-04-23T20:00:00Z

@feat-forge-001 @pipeline-state-machine
Feature: Pipeline State Machine and Configuration
  As Rich, the sole operator of Forge
  I want to enqueue, observe, and steer pipeline builds through a durable state machine
  So that I always have a consistent, crash-safe view of what the pipeline is doing
  and have a reliable way to start, pause, cancel, or skip stages from the command line

  Background:
    Given Forge is configured from the project configuration file

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core write path — enqueue a feature for build from the CLI
  @key-example @smoke
  Scenario: Queueing a new feature creates a pending build
    Given I have a feature description at a permitted repository path
    When I queue the feature for a build
    Then a new build should be recorded as pending pickup
    And the queue should report a correlation identifier for the request
    And the command should report success

  # Why: Full happy-path lifecycle — the build completes end to end
  @key-example @smoke
  Scenario: A queued build progresses through the lifecycle to completion
    Given a build has been queued for a feature
    When the pipeline picks up the build and all stages succeed
    Then the build should transition from pending pickup, to preparation, to running, to finalisation, to complete
    And the build should record the moment preparation began and the moment it completed
    And the build should expose the pull request it produced

  # Why: Core read path — `forge status` surfaces live activity without touching the messaging layer
  @key-example @smoke
  Scenario: Viewing status shows all non-terminal builds and recent outcomes
    Given one build is currently running and one build completed yesterday
    When I ask for the current pipeline status
    Then the running build should be shown as active with its current stage
    And the recently completed build should be shown as a recent outcome
    And the status query should not require the pipeline agent to be reachable

  # Why: Core read path — `forge history` summarises prior builds for a feature
  @key-example
  Scenario: Viewing history for a specific feature lists all prior attempts
    Given three previous builds exist for the same feature, two complete and one failed
    When I ask for the history of that feature
    Then all three attempts should be listed from most recent to oldest
    And each attempt should display its final outcome and the stages it went through

  # Why: Configuration loading — defaults come from forge.yaml, overridable on the command line
  @key-example
  Scenario: Queueing a build applies configuration defaults when no overrides are given
    Given the configuration specifies a default reasoning-turn budget and a default stage timeout
    When I queue a feature without specifying those values
    Then the new build should adopt the configured defaults
    And when I override one of those values on the command line
    Then the new build should record the overridden value instead of the default

  # Why: Single-writer discipline — the read path remains available even while the agent is writing
  @key-example
  Scenario: Status and history queries remain responsive while a build is actively writing
    Given a build is actively running and recording stage outcomes
    When I ask for status at the same moment
    Then the status query should return promptly without waiting for the writer

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Just-inside and at-boundary values for the reasoning-turn budget
  # [ASSUMPTION: confidence=low] An upper bound of 20 reasoning turns is used for boundary examples;
  # no ceiling is stated in the CLI contract or forge.yaml schema.
  @boundary
  Scenario Outline: Reasoning-turn budget overrides are accepted within the permitted range
    Given the default reasoning-turn budget is five
    When I queue a feature with a reasoning-turn budget of <turns>
    Then the build should be recorded with a reasoning-turn budget of <turns>

    Examples:
      | turns |
      | 1     |
      | 5     |
      | 20    |

  # Why: Just-outside boundary — zero or negative turn budgets are nonsensical
  # [ASSUMPTION: confidence=medium] Reasoning-turn budget must be at least 1;
  # a zero-turn budget cannot perform any reasoning work.
  @boundary @negative
  Scenario Outline: Reasoning-turn budgets outside the permitted range are rejected
    When I queue a feature with a reasoning-turn budget of <turns>
    Then the queue command should be rejected with a configuration error
    And no new build should be recorded

    Examples:
      | turns |
      | 0     |
      | -1    |

  # Why: History limit default and just-inside bound
  @boundary
  Scenario Outline: History returns at most the requested number of entries
    Given <available> prior builds exist
    When I ask for history with a limit of <limit>
    Then at most <expected> entries should be returned in newest-first order

    Examples:
      | available | limit | expected |
      | 10        | 50    | 10       |
      | 60        | 50    | 50       |
      | 60        | 1     | 1        |

  # Why: Just-inside default — `forge history` with no arguments returns the 50 most recent builds
  @boundary
  Scenario: History with no arguments returns the 50 most recent builds by default
    Given 75 prior builds exist
    When I ask for history without specifying a limit
    Then exactly 50 entries should be returned
    And they should be the 50 most recently queued

  # Why: Uniqueness invariant — a feature cannot be queued twice within the same correlation scope
  @boundary @negative
  Scenario: Queueing the same feature with the same correlation identifier twice is refused
    Given a build already exists for a feature under a specific correlation identifier
    When I queue the same feature again with the same correlation identifier
    Then the second queue attempt should be refused as a duplicate
    And only one build should remain recorded for that combination

  # Why: Status views bound the set of stages shown in the full detail view
  @boundary
  Scenario: Full status view includes the most recent stages per build
    Given a build has completed seventeen stages
    When I ask for status with full detail for that build
    Then at most the five most recent stages should be shown by default

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Path allowlist is a safety boundary — queueing outside it must fail fast
  @negative
  Scenario: Queueing a build against a repository path outside the allowlist is refused
    Given a repository path that is not on the allowlist
    When I attempt to queue a feature for that repository
    Then the queue command should be refused with an unauthorised-path error
    And no new build should be recorded
    And no message should be published to the pipeline

  # Why: Active-duplicate guard — a feature already in flight cannot be re-queued
  @negative
  Scenario: Queueing a feature that already has an in-flight build is refused
    Given a build for a feature is currently pending pickup, preparing, running, or paused
    When I queue the same feature again
    Then the queue command should be refused as a duplicate in-flight build
    And the existing build should be unaffected

  # Why: Skip is only meaningful on a paused build awaiting review
  @negative
  Scenario: Skipping a build that is not paused is refused
    Given the pipeline is running a build that is not paused for review
    When I ask to skip the current stage of that build
    Then the skip command should be refused with an error
    And no skip decision should be sent to the pipeline

  # Why: Cancel resolves through the build history; missing builds should be reported, not crashed
  @negative
  Scenario: Cancelling a feature with no active or recent builds is refused
    Given there is no build on record for a given feature
    When I ask to cancel that feature
    Then the cancel command should be refused with a not-found error

  # Why: Crash during preparation should be surfaced as a failure, not a silent hang
  @negative
  Scenario: A feature description that fails validation causes preparation to fail
    Given a build has been picked up and its feature description is invalid
    When preparation attempts to validate the description
    Then the build should transition from preparation to failed
    And the build should record a structured reason describing the validation problem

  # Why: Hard-stop gate decision halts the build and is not retryable by the pipeline itself
  @negative
  Scenario: A hard-stop gate decision during running transitions the build to failed
    Given a running build has just produced a stage outcome that triggers a hard-stop decision
    When the gate resolves with no retry path
    Then the build should transition from running to failed
    And the completion time of the build should be recorded

  # Why: State machine invariant — transitions that are not in the allowed table are rejected
  @negative
  Scenario: Attempting to transition a build through an invalid path is refused
    Given a build is currently pending pickup
    When the pipeline attempts to transition that build directly to running without preparing first
    Then the transition should be refused as invalid
    And the build should remain pending pickup

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Crash during preparation — redelivery restarts preparation from scratch
  @edge-case
  Scenario: A crash during preparation marks the build for redelivery
    Given a build had reached preparation
    When the pipeline process crashes before preparation completed
    And the pipeline process restarts and reconciles the build history
    Then the build should be marked as interrupted with a recoverable reason
    And the build should subsequently be picked up again and re-enter preparation

  # Why: Crash during running — retry-from-scratch policy
  @edge-case
  Scenario: A crash during running marks the build as interrupted and restarts it from scratch
    Given a build was in the running state when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then the build should be marked as interrupted
    And the build should be re-picked up and restart from preparation

  # Why: Crash during finalisation — the PR may have been created, so operator reconciles
  @edge-case
  Scenario: A crash during finalisation marks the build as interrupted with an operator warning
    Given a build was finalising when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then the build should be marked as interrupted
    And the recovery report should warn that the pull request may have been created and require manual reconciliation

  # Why: A paused build survives a crash — the approval request is re-issued on restart
  @edge-case
  Scenario: A paused build survives a pipeline crash and re-issues its approval request
    Given a build is paused awaiting a review decision
    When the pipeline crashes and restarts
    Then the build should remain paused after reconciliation
    And the pending approval request should be re-issued so reviewers can still respond

  # Why: Terminal states after a crash are a no-op — we must not restart completed builds
  @edge-case
  Scenario Outline: Builds in terminal states after a crash are not re-picked
    Given a build was already in the <terminal> state when the pipeline crashed
    When the pipeline restarts and reconciles the build history
    Then no new work should be started for that build
    And any outstanding delivery for that build should be acknowledged

    Examples:
      | terminal  |
      | complete  |
      | failed    |
      | cancelled |
      | skipped   |

  # Why: `forge cancel` during a paused state is modelled as a synthetic rejection
  @edge-case
  Scenario: Cancelling a paused build resolves its pending approval as a rejection
    Given a build is paused awaiting a review decision
    When I ask to cancel that build with a reason
    Then the cancel command should resolve the pending review as a rejection on my behalf
    And the build should transition from paused to cancelled
    And the reason I supplied should be recorded on the build

  # Why: `forge skip` during a flag-for-review pause continues the build without that stage
  @edge-case
  Scenario: Skipping a stage on a flagged-for-review pause resumes the build and marks the stage skipped
    Given a build is paused on a flag-for-review gate
    When I ask to skip the flagged stage with a reason
    Then the paused stage should be recorded as skipped with my reason
    And the build should resume from running
    And the overall build should still be allowed to complete successfully

  # Why: Sequential queue discipline — only one build runs at a time per project
  # [ASSUMPTION: confidence=medium] Sequential-queue scope is per-project; different
  # projects can run concurrently. The roadmap says "sequential build queue" without
  # scoping it explicitly.
  @edge-case
  Scenario: A second queued build waits until the first completes before starting
    Given one build is already running for a project
    When I queue a second feature for the same project
    Then the second build should be recorded as pending pickup
    And the second build should not begin preparation until the first build has reached a terminal state

  # Why: Watch mode streams state changes without blocking writes
  @edge-case
  Scenario: Watching status refreshes the view as the build progresses
    Given a build is running and moving through stages
    When I watch the status view
    Then the view should refresh to reflect each new stage as it begins
    And the view should stop refreshing once the build reaches a terminal state

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Feature identifier is used in build identifier and on-disk paths — traversal must be refused
  # [ASSUMPTION: confidence=medium] Feature identifiers containing path-traversal
  # sequences (such as "../", "/", or "\") are rejected because feature_id is
  # interpolated into build_id and worktree paths.
  @edge-case @negative
  Scenario: Queueing with a feature identifier that contains path-traversal characters is refused
    When I attempt to queue a feature whose identifier contains traversal characters such as "../"
    Then the queue command should be refused with a validation error
    And no new build should be recorded
    And no message should be published to the pipeline

  # Why: Audit trail — the identity of the operator who triggered the build is preserved through cancel
  # [ASSUMPTION: confidence=low] The cancelling operator is recorded distinctly from
  # the originating operator for audit purposes; the originating operator is preserved
  # unchanged on the build record.
  @edge-case
  Scenario: Cancelling a build records the cancelling operator even when different from the originator
    Given a build is currently running and was originated by one operator
    When a different operator cancels the build with a reason
    Then the build should transition to cancelled
    And the originating operator should remain recorded on the build
    And the cancelling operator and their reason should be recorded on the resolution

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP F — Concurrency
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Two simultaneous queue commands must both be durable and ordered
  @edge-case
  Scenario: Two features queued at the same instant both appear in history
    When two different features are queued at effectively the same instant
    Then both builds should be recorded as pending pickup
    And both builds should be assigned distinct build identifiers
    And both builds should appear in history with their original queue-time ordering preserved

  # Why: WAL guarantee — readers see a consistent snapshot even during active writes
  @edge-case
  Scenario: A concurrent reader sees a consistent snapshot while the pipeline is writing
    Given the pipeline is in the middle of recording a new stage outcome
    When a second reader asks for history at the same instant
    Then the reader should see a consistent snapshot from the last committed write
    And the reader should never observe a partially written stage

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP G — Data Integrity
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Invariant — a terminal build always has a recorded completion time
  @edge-case
  Scenario Outline: Any build that reaches a terminal state has a recorded completion time
    When a build reaches the <terminal> state
    Then the build should have a completion time recorded
    And no further state transitions should be permitted for that build

    Examples:
      | terminal  |
      | complete  |
      | failed    |
      | cancelled |
      | skipped   |

  # Why: Partial-failure between local write and pipeline publish must be recoverable, not silent
  # [ASSUMPTION: confidence=medium] When the pre-publish local write has succeeded
  # but the pipeline publish then fails, the build row remains visible as pending
  # pickup so the operator can reconcile or re-queue it. The CLI contract specifies
  # the write-before-publish order but does not specify the failure-mode visibility.
  @edge-case
  Scenario: A build row is written but the pipeline publish then fails
    Given queueing writes the build history row before publishing to the pipeline
    When the local write succeeds but the pipeline publish then fails
    Then the queue command should report a publish failure
    And the build should be visible as pending pickup so it can be reconciled or re-queued
    And the operator should be told the pipeline was not notified

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP H — Integration Boundaries
  # ─────────────────────────────────────────────────────────────────────────

  # Why: The CLI must fail cleanly when the pipeline messaging layer is unreachable
  @edge-case @negative
  Scenario: Queueing fails cleanly when the pipeline messaging layer is unreachable
    Given the pipeline messaging layer cannot be reached from the CLI
    When I attempt to queue a feature
    Then the queue command should fail with a messaging-layer error
    And the failure message should identify the messaging layer as the unreachable dependency
    And subsequent status queries should still work without the messaging layer
Write pipeline-state-machine-and-configuration_assumptions.yaml
52 lines
# Assumptions manifest for pipeline-state-machine-and-configuration.feature
# Generated by /feature-spec

feature: "Pipeline State Machine and Configuration"
feature_id: "FEAT-FORGE-001"
generated: "2026-04-23T20:00:00Z"
stack: "python"
review_required: true

assumptions:
  - id: "ASSUM-001"
    scenario: "Reasoning-turn budgets outside the permitted range are rejected"
    assumption: "The minimum reasoning-turn budget is 1"
    confidence: medium
    basis: "Domain convention — a zero-turn budget cannot perform any reasoning. Not stated explicitly in the forge.yaml schema or CLI contract."
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "Reasoning-turn budget overrides are accepted within the permitted range"
    assumption: "The maximum reasoning-turn budget used for boundary examples is 20"
    confidence: low
    basis: "Chosen as an illustrative upper bound; no ceiling is stated anywhere in the context files."
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "Queueing with a feature identifier that contains path-traversal characters is refused"
    assumption: "Feature identifier validation rejects identifiers containing path-traversal sequences such as \"../\", \"/\", or \"\\\""
    confidence: medium
    basis: "feature_id is interpolated into build_id (\"build-{feature_id}-{timestamp}\") and into worktree paths (\"/var/forge/builds/{build_id}/\"). Traversal would escape the Forge-managed filesystem root. Not stated explicitly in the CLI contract."
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "A second queued build waits until the first completes before starting"
    assumption: "Sequential-queue discipline is scoped per-project; different projects can run concurrently"
    confidence: medium
    basis: "Roadmap says \"sequential build queue\" without scoping. API-cli §9 describes per-project topic scoping and per-project filtering, so per-project sequencing is consistent with both, but global serialisation is a plausible alternative reading."
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "Cancelling a build records the cancelling operator even when different from the originator"
    assumption: "The cancelling operator is recorded distinctly from the originating operator; originating_user is preserved unchanged; responder and reason are captured on the resolution record"
    confidence: low
    basis: "API-cli §6.2 describes cancellation as a synthetic ApprovalResponsePayload(responder=\"rich\", reason=\"cli cancel\"). The responder is captured in details_json.approval.response. Whether the originating_user field is preserved separately is implied but not stated explicitly."
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "A build row is written but the pipeline publish then fails"
    assumption: "When the pre-publish local write has succeeded but the pipeline publish fails, the build row remains visible as pending pickup so the operator can reconcile or re-queue"
    confidence: medium
    basis: "API-cli §3.3 step 4 is explicit that the row is written before publishing. What happens if step 5 (publish) then fails is not stated. Alternatives considered: roll back the row; mark the row as publish-failed; keep as QUEUED."
    human_response: "confirmed"
Write pipeline-state-machine-and-configuration_summary.md
83 lines
# Feature Spec Summary: Pipeline State Machine and Configuration

**Feature ID**: FEAT-FORGE-001
**Stack**: python
**Generated**: 2026-04-23T20:00:00Z
**Scenarios**: 35 total (3 smoke, 0 regression)
**Assumptions**: 6 total (0 high / 4 medium / 2 low confidence)
**Review required**: Yes (2 low-confidence assumptions)

## Scope

Specifies the durable build lifecycle — queue to terminal — including the state
machine transitions, SQLite-backed history, crash recovery, sequential-queue
discipline, and the CLI surface (`forge queue`, `forge status`, `forge history`,
`forge cancel`, `forge skip`). Behaviour is described in domain terms; underlying
mechanisms (WAL concurrency, NATS pipeline subjects, Pydantic validation) appear
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 6 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 11 |
| Edge cases (@edge-case) | 16 |
| Smoke (@smoke) | 3 |
| Regression (@regression) | 0 |

Note: several scenarios carry multiple tags — e.g. boundary + negative for the
"outside the permitted range" pair, edge-case + negative for the security and
integration-boundary refusals. Group totals do not sum to 35.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — queue, lifecycle, status, history, config, non-blocking reads | 6 |
| B | Boundary Conditions — turn budget, history limit, uniqueness, full-view slice | 6 |
| C | Negative Cases — allowlist, duplicate, skip-misuse, missing cancel, validation, hard-stop, invalid transition | 7 |
| D | Edge Cases — crash recovery across every lifecycle state, cancel-paused, skip-flagged, sequential queue, watch | 9 |
| E | Security — path-traversal rejection, cancel-operator audit | 2 |
| F | Concurrency — simultaneous queues, consistent-snapshot reads | 2 |
| G | Data Integrity — terminal-state completion-time invariant, write-then-publish partial failure | 2 |
| H | Integration Boundaries — pipeline messaging unreachable | 1 |

## Deferred Items

None. All four initial groups and all four expansion groups were accepted
without deferral.

## Open Assumptions (low confidence)

Two low-confidence assumptions that need human verification before this
specification is treated as authoritative:

- **ASSUM-002** — Maximum reasoning-turn budget of 20 used in boundary Examples.
  No ceiling is stated anywhere. Options: keep 20 as an illustrative upper
  bound, raise or lower it, or remove the upper-bound boundary and treat the
  value as unbounded.

- **ASSUM-005** — Cancelling operator recorded distinctly from originating
  operator. Implied by the "synthetic ApprovalResponsePayload(responder=…)"
  wording in API-cli §6.2 but not stated explicitly. If the intent is
  single-operator only, this scenario can be simplified or removed.

## Medium-Confidence Assumptions (Coach-review recommended)

- ASSUM-001 — minimum reasoning-turn budget is 1
- ASSUM-003 — feature_id rejects path-traversal sequences
- ASSUM-004 — sequential-queue scope is per-project
- ASSUM-006 — build row remains visible after publish failure

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Pipeline State Machine and Configuration" \
      --context features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md

`/feature-plan` Step 11 will link @task:<TASK-ID> tags back into the
`.feature` file after tasks are created.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: Pipeline State Machine and Configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:
  features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
  features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_assumptions.yaml
  features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md

Scenarios: 35 total
  @key-example: 6    @boundary: 6
  @negative: 11      @edge-case: 16
  @smoke: 3          @regression: 0

Assumptions: 6 total
  high: 0
  medium: 4 (Coach review recommended — ASSUM-001, 003, 004, 006)
  low: 2 (human review required — ASSUM-002 turn-budget ceiling, ASSUM-005 cancel-operator audit)

REVIEW REQUIRED: 2 low-confidence assumptions need verification
  See: features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_assumptions.yaml

Deferred: 0 groups

Next steps:
  Review: features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
  Pass to /feature-plan:
    /feature-plan "Pipeline State Machine and Configuration" \
      --context features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━