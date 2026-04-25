/feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/contracts/API-tool-layer.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md


  I'll execute the /feature-spec workflow for FEAT-FORGE-005. Starting with Phase 1 context gathering.

Bash Check project layout
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/ && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/forge/ 2>/dev/null || echo "no forge subdir"

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
no forge subdir
Bash Find existing feature files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -name "*.feature" -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null | head -30

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/nats-fleet-integration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/features/nats-fleet-integration/nats-fleet-integration.feature
Bash Detect stack signals
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 4 -name "pyproject.toml" -o -name "package.json" -o -name "go.mod" -o -name "Cargo.toml" 2>/dev/null | grep -v node_modules | grep -v .venv | head -10

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/pyproject.toml
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/pyproject.toml
Bash Locate context files
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ 2>/dev/null && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/ 2>/dev/null

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
---
DDR-001-reply-subject-correlation.md
DDR-002-resume-value-rehydration-helper.md
DDR-003-sqlite-schema-layout-wal.md
DDR-004-graphiti-group-partitioning.md
DDR-005-cli-context-manifest-resolution.md
DDR-006-async-subagent-state-channel-contract.md
Bash Check docs structure
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/ 2>/dev/null

OUT
adr
architecture
design
history
product
research
reviews
state
Bash Find FEAT-FORGE-005 references
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 5 -name "FEAT-FORGE-005*" -o -name "*FORGE-005*" 2>/dev/null | head -20

OUT
(Bash completed with no output)
Bash Search for FEAT-FORGE-005 mentions
IN
grep -rn "FEAT-FORGE-005" /home/richardwoollcott/Projects/appmilla_github/forge/docs/ /home/richardwoollcott/Projects/appmilla_github/forge/tasks/ /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/ 2>/dev/null | head -30

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:130:| FEAT-FORGE-005 | GuardKit Command Invocation Engine | 001 | 2-3 days | Subprocess calls to `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`. Context flag construction from pipeline state + `.guardkit/context-manifest.yaml`. Output capture and artifact path tracking. Error handling and retry. | Subprocess invocation of GuardKit AutoBuild with `--nats` flag (Stage 4) |
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:309:# FEAT-FORGE-005: GuardKit Command Invocation Engine
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:310:guardkit feature-spec FEAT-FORGE-005 \
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:364:# FEAT-FORGE-005: GuardKit Command Invocation Engine (depends on 001 — can parallel with 002)
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:365:guardkit feature-plan FEAT-FORGE-005 \
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:407:guardkit autobuild FEAT-FORGE-005
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md:678:| Subprocess orchestration complexity | GuardKit commands invoked as subprocesses may have environment/path issues | FEAT-FORGE-005 builds a robust invoker with env setup, working dir management, and output discovery |
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:130:| FEAT-FORGE-005 | GuardKit Command Invocation Engine | 001 | 2-3 days | Subprocess calls to `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`. Context flag construction from pipeline state + `.guardkit/context-manifest.yaml`. Output capture and artifact path tracking. Error handling and retry. | Subprocess invocation of GuardKit AutoBuild with `--nats` flag (Stage 4) |
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:309:# FEAT-FORGE-005: GuardKit Command Invocation Engine
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:310:guardkit feature-spec FEAT-FORGE-005 \
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:364:# FEAT-FORGE-005: GuardKit Command Invocation Engine (depends on 001 — can parallel with 002)
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:365:guardkit feature-plan FEAT-FORGE-005 \
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:407:guardkit autobuild FEAT-FORGE-005
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md:678:| Subprocess orchestration complexity | GuardKit commands invoked as subprocesses may have environment/path issues | FEAT-FORGE-005 builds a robust invoker with env setup, working dir management, and output discovery |
Bash Check product and architecture folders
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/product/ 2>/dev/null && echo "---" && ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/ 2>/dev/null

OUT
feature_spec_inputs
roadmap.md
---
ARCHITECTURE.md
assumptions.yaml
container.md
decisions
domain-model.md
system-context.md
Bash Check feature spec inputs
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/ 2>/dev/null

OUT
FEAT-PO-001.md
FEAT-PO-002.md
FEAT-PO-003.md
FEAT-PO-004.md
FEAT-PO-005.md
FEAT-PO-006.md
Read forge-build-plan.md (lines 301-420)
Read API-tool-layer.md
Read API-subprocess.md
Read DDR-005-cli-context-manifest-resolution.md
Read FEAT-PO-005.md
Read nats-fleet-integration.feature (lines 2-121)
Read forge-build-plan.md (lines 101-160)
Context loaded: stack=python, 2 existing .feature files (FEAT-FORGE-001 pipeline-state-machine, FEAT-FORGE-002 nats-fleet-integration), 3 context files ingested (API-tool-layer §6, API-subprocess §2–5, DDR-005 resolver rules), 1 product-spec input (FEAT-PO-005 — unrelated, telemetry feature).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: GuardKit Command Invocation Engine (FEAT-FORGE-005)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@feat-forge-005 @guardkit-invocation-engine
Feature: GuardKit Command Invocation Engine
  As Rich, the sole operator of Forge
  I want Forge to invoke GuardKit subcommands as subprocesses with context flags assembled automatically,
    stream progress while running, capture artefacts, and handle failures without crashing the build,
  So that the pipeline can drive `/system-arch`, `/feature-spec`, `/feature-plan`, `autobuild`, and related
    GuardKit commands end-to-end without hand-assembled flags or brittle output parsing.

  Background:
    Given Forge is running inside an ephemeral build worktree
    And a project configuration file defines the shell, filesystem, and network permissions
    And a context manifest is available at the repo root describing documents grouped by category
━━ GROUP A: Key Examples (7 scenarios) ━━


  # Why: Core invocation — a GuardKit subcommand runs and returns a parsed success result
  @key-example @smoke
  Scenario: A GuardKit subcommand completes successfully and its artefacts are captured
    Given the reasoning model invokes a GuardKit subcommand wrapper for the current build
    When the GuardKit process exits cleanly
    Then the invocation should report success
    And the returned result should list every artefact path emitted by GuardKit
    And the returned result should include the command's wall-clock duration
    And the returned result should include the Coach score when GuardKit produced one

  # Why: Auto-assembled context flags — the central value prop, zero hand-wiring
  # [ASSUMPTION: confidence=high] /feature-spec pulls manifest categories: specs, contracts, source, decisions
  @key-example @smoke
  Scenario: Context flags are assembled automatically from the manifest for the invoked subcommand
    Given the manifest lists documents tagged with multiple categories
    When Forge invokes the feature-spec wrapper
    Then the resolver should select only documents whose category is relevant to feature-spec
    And every always-included document should be prepended to the context list
    And the subprocess command line should carry one context flag per selected document

  # Why: Live telemetry — progress events surface on the bus while the subprocess still runs
  @key-example
  Scenario: GuardKit progress is streamed on the bus while the subprocess is still running
    Given the reasoning model invokes any GuardKit wrapper that supports streaming
    When the GuardKit process emits progress events during its run
    Then Forge should observe each progress event on the pipeline progress channel
    And the blocking invocation should still return only after the subprocess exits

  # Why: Error contract — the tool layer must never raise past its boundary
  @key-example @smoke
  Scenario: A failing GuardKit subprocess is reported as a structured error, not an exception
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits with a non-zero status
    Then the invocation should return a structured failure result
    And the failure result should carry the captured error output
    And the tool layer should not propagate an exception to the reasoning model

  # Why: Worktree confinement — every subprocess runs inside the build's ephemeral directory
  @key-example
  Scenario: Subprocesses are executed inside the current build's worktree
    Given a build's worktree has been prepared
    When any GuardKit, git, or shell subprocess is launched through the adapter
    Then the subprocess working directory should be the build's worktree
    And the subprocess should not be able to read or write outside the allowed worktree paths

  # Why: Version control adapter — PR creation is the terminal artefact of a build
  @key-example
  Scenario: Forge opens a pull request for the build through the version-control adapter
    Given a build has committed and pushed its work to a remote branch
    When Forge asks the version-control adapter to open a pull request
    Then the adapter should create the pull request against the configured base branch
    And the invocation should return the pull-request URL as a structured result

  # Why: Graphiti subcommands bypass manifest resolution — they don't take --context
  @key-example
  Scenario: Graphiti GuardKit subcommands skip context-manifest resolution entirely
    When Forge invokes a Graphiti GuardKit wrapper
    Then no context flags should be assembled
    And no manifest lookup should be performed for this invocation
━━ GROUP B: Boundary Conditions (6 scenarios) ━━


  # Why: Just-inside timeout — a subprocess that finishes just before the cap still succeeds
  # [ASSUMPTION: confidence=high] Default subprocess timeout is 600 seconds
  @boundary
  Scenario Outline: A subprocess that finishes within the timeout is reported as successful
    Given the subprocess timeout has been configured
    When a GuardKit wrapper completes after <seconds> seconds of runtime
    Then the invocation should report success
    And the returned duration should match the observed runtime

    Examples:
      | seconds |
      | 1       |
      | 300     |
      | 599     |

  # Why: Just-outside timeout — once the cap is breached, the result is "timeout", not "failed"
  @boundary @negative
  Scenario: A subprocess that exceeds the timeout is reported as timed-out
    Given the subprocess timeout has been configured
    When a GuardKit wrapper has been running longer than the configured timeout
    Then the invocation should return a timeout result
    And the reasoning model should be free to decide whether to retry or escalate

  # Why: Depth-2 dependency chase — the resolver should follow one hop into a sibling repo
  # [ASSUMPTION: confidence=high] Dependency traversal depth cap is 2
  @boundary
  Scenario: Context resolution follows dependency references up to the depth cap
    Given the manifest declares a dependency on a sibling repository
    And the sibling repository declares its own manifest with key documents
    When Forge invokes a subcommand that consumes both manifests' categories
    Then the resolver should include selected documents from both manifests
    And the final context list should preserve a stable order

  # Why: Just-outside depth — traversing a deeper chain should stop and warn, not explode
  @boundary @edge-case
  Scenario: Context resolution stops at the depth cap and warns instead of recursing further
    Given the manifest chain would require traversing beyond the resolver's depth cap
    When Forge resolves context flags for a subcommand
    Then the resolver should stop at the depth cap
    And the resolver should emit a structured cycle-detected warning
    And the invocation should still proceed with the documents resolved so far

  # Why: Just-inside stdout-tail — a compact stdout is preserved verbatim
  # [ASSUMPTION: confidence=high] stdout_tail captures the last 4 KB of subprocess output
  @boundary
  Scenario: A compact stdout is preserved verbatim in the returned result
    Given a GuardKit subprocess that prints fewer than four kilobytes to standard output
    When the invocation completes
    Then the returned result should include the full standard output

  # Why: Just-outside stdout-tail — larger output is truncated to the tail, not dropped
  @boundary
  Scenario: A large stdout is truncated to the most recent tail in the returned result
    Given a GuardKit subprocess that prints far more than the captured tail size
    When the invocation completes
    Then the returned result should include only the most recent slice of standard output
    And the slice size should match the configured tail limit
━━ GROUP C: Negative Cases (7 scenarios) ━━


  # Why: Missing manifest — proceed without --context rather than blocking adoption
  @negative
  Scenario: A missing context manifest degrades gracefully to no context flags
    Given the target repository has no context manifest
    When Forge invokes any GuardKit wrapper for that repository
    Then the invocation should proceed with no context flags
    And a structured warning should be recorded indicating the manifest was missing
    And the reasoning model should be able to see the warning in the returned result

  # Why: A binary that isn't on the allowlist must be refused before it runs
  @negative
  Scenario: A subprocess whose binary is not in the shell allowlist is refused
    Given the project configuration lists the permitted shell binaries
    When the reasoning model attempts to execute a binary that is not on the allowlist
    Then the subprocess should be refused before launch
    And the invocation should return a structured permissions error
    And no side effects should occur on the worktree

  # Why: A cwd outside the worktree allowlist must be refused — worktree confinement invariant
  @negative
  Scenario: A subprocess targeting a working directory outside the allowlist is refused
    Given the project configuration allows subprocesses only within the build worktrees path
    When a subprocess is requested with a working directory outside that path
    Then the subprocess should be refused before launch
    And the invocation should return a structured permissions error

  # Why: Non-zero exit with captured stderr — must reach the reasoning model intact
  @negative
  Scenario: A non-zero exit is reported as a failure with the subprocess error output
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits with a non-zero status and writes diagnostics to its error stream
    Then the invocation should return a structured failure result
    And the failure result should include the subprocess exit status and error output

  # Why: Tolerant parser — unknown output shape should NOT explode the whole call
  @negative @edge-case
  Scenario: An unknown GuardKit output shape degrades to success with no artefacts
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits cleanly but its output does not match the expected artefact shape
    Then the invocation should still report success
    And the returned artefact list should be empty
    And the reasoning model should be responsible for deciding whether the stage produced useful work

  # Why: Documents outside the filesystem read-allowlist must be omitted, not silently included
  @negative
  Scenario: Context documents that fall outside the read allowlist are omitted with a warning
    Given the manifest references a document whose path is outside the filesystem read allowlist
    When Forge resolves context flags for a subcommand
    Then the out-of-allowlist document should be omitted from the context list
    And a structured warning should be emitted identifying the omitted document

  # Why: Missing GitHub credentials — surface at the adapter boundary, never raise
  @negative
  Scenario: A pull-request creation without GitHub credentials returns a structured error
    Given the runtime has no GitHub access credentials available
    When Forge asks the version-control adapter to open a pull request
    Then the adapter should return a structured error explaining the credential is missing
    And no pull request should be created
━━ GROUP D: Edge Cases (6 scenarios) ━━


  # Why: The resolver must not loop on a circular dependency graph
  @edge-case
  Scenario: A circular dependency chain is detected and resolved safely
    Given two repositories' manifests reference one another as dependencies
    When Forge resolves context flags for a subcommand
    Then the resolver should detect the cycle before repeating a manifest
    And the resolver should stop at the depth cap
    And a structured cycle-detected warning should be recorded

  # Why: Worktree cleanup is best-effort — must not block terminal transitions
  @edge-case
  Scenario: A failed worktree cleanup is logged but does not prevent build completion
    Given a build has reached a terminal state
    When the adapter attempts to delete the build's worktree
    And the deletion fails
    Then the build should still be marked as terminal in history
    And a structured warning should be logged about the failed cleanup

  # Why: NATS progress stream is telemetry, not authoritative — outcome must still be returned
  @edge-case
  Scenario: The authoritative result still returns when progress streaming is unavailable
    Given the progress stream channel is unavailable during a GuardKit invocation
    When the subprocess exits cleanly
    Then the invocation should still return a parsed success result with artefact paths
    And the missing progress stream should not itself fail the call

  # Why: Retry with additional context — explicit build-plan requirement ("error handling and retry")
  # [ASSUMPTION: confidence=medium] A failed invocation can be retried with extra context flags chosen by the reasoning model
  @edge-case
  Scenario: A failed invocation can be retried with additional explicit context
    Given a GuardKit wrapper has returned a failure result
    When the reasoning model retries the same wrapper with additional explicit context paths
    Then the retry should merge the explicit paths with the manifest-derived ones
    And the retry should be a fresh subprocess launch, not a continuation

  # Why: Tool-layer exceptions must never escape — the universal error contract
  @edge-case
  Scenario: An unexpected error inside a wrapper is returned as a structured error, not raised
    Given any GuardKit wrapper is invoked
    When an unexpected internal error occurs inside the wrapper
    Then the wrapper should catch the error
    And the wrapper should return a structured error result describing the error type and message
    And no exception should propagate to the reasoning model

  # Why: Multiple parallel invocations within the same build — important for autobuild fan-out
  # [ASSUMPTION: confidence=medium] Multiple GuardKit wrappers may run in parallel within the same build worktree
  @edge-case
  Scenario: Parallel GuardKit invocations in the same build do not corrupt each other's results
    Given two GuardKit wrappers are invoked in parallel within the same build
    When both subprocesses complete
    Then each invocation should receive its own independent structured result
    And neither result should contain artefacts or progress events belonging to the other
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 26 scenarios across 4 groups
Inferred assumptions: 6 (1 medium, 5 high — resolved in Phase 5)
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

GROUP A — Key Examples (7 scenarios): _
GROUP B — Boundary Conditions (6 scenarios): _
GROUP C — Negative Cases (7 scenarios): _
GROUP D — Edge Cases (6 scenarios): _

forge-build-plan.md
A A A A
All four groups accepted. Proceeding to Phase 4.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I've identified 6 additional scenarios covering security, concurrency, and integration boundaries for the GuardKit Command Invocation Engine:

Security (2):

A context-manifest path that attempts to escape the repository via traversal is rejected
A subprocess command with shell metacharacters injected into its arguments is passed through as literal tokens, not expanded
Concurrency (2):

Two builds running against the same target repo resolve their manifests independently (no shared mutable state)
A cancelled build's in-flight subprocess is terminated and its partial artefacts are discarded
Integration boundaries (2):

GuardKit emits progress faster than Forge can consume from the bus — progress events should not be lost for forge status
A GuardKit subcommand that stalls mid-run (producing no stdout and no progress events) still honours the timeout
Include them?

[Y] Yes — show and curate them (same A/R/M/+/? actions)
[S] Sample — show 2 examples, then decide
[N] No — skip and proceed to Phase 5

Your choice [Y/S/N]: _

Y
Here are the 6 additional scenarios for review.

━━ EDGE CASE EXPANSION — 6 additional scenarios ━━


  # ─────────────────────────────────────────────────────────────────────────
  # Security
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Path traversal in a manifest entry must not escape the repository root
  @edge-case @negative
  Scenario: A context manifest entry that would escape the repository root is rejected
    Given the context manifest lists a document path that resolves outside the target repository
    When Forge resolves context flags for a subcommand
    Then the out-of-repository path should be omitted from the context list
    And a structured warning should be emitted identifying the rejected path
    And the invocation should proceed with the remaining valid documents

  # Why: Shell-metacharacter injection via arguments must stay literal, not expand
  @edge-case @negative
  Scenario: Shell metacharacters in subprocess arguments are passed as literal tokens
    Given the reasoning model builds subprocess arguments that contain shell metacharacters
    When the adapter launches the subprocess
    Then each argument should be delivered to the binary as a single literal token
    And no shell expansion or secondary command should be evaluated

  # ─────────────────────────────────────────────────────────────────────────
  # Concurrency
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Two concurrent builds targeting the same repo must resolve independently
  # [ASSUMPTION: confidence=medium] Context resolution has no cached state shared across concurrent builds
  @edge-case
  Scenario: Two concurrent builds against the same repository resolve context independently
    Given two builds are running at the same time against the same target repository
    When each build invokes a GuardKit wrapper requiring context resolution
    Then each build should receive its own resolved context flag list
    And one build's resolver warnings should not appear in the other build's result

  # Why: Cancellation must terminate in-flight subprocess and discard partial artefacts
  @edge-case
  Scenario: A cancelled build terminates its in-flight subprocess cleanly
    Given a GuardKit subprocess is currently running inside a build
    When the build is cancelled
    Then the subprocess should be terminated
    And any partial artefacts produced by the terminated subprocess should not be reported as completed work

  # ─────────────────────────────────────────────────────────────────────────
  # Integration boundaries
  # ─────────────────────────────────────────────────────────────────────────

  # Why: The live status view must not miss progress events because of slow consumption
  @edge-case
  Scenario: Progress events emitted faster than Forge consumes them are still observable for live status
    Given a GuardKit subprocess is emitting progress events at a high cadence
    When Forge is slower to consume than the producer is to emit
    Then the live status view should still reflect the most recent progress events
    And the authoritative completion result should remain unaffected

  # Why: A stalled subprocess with no output still hits the timeout, never hangs the build
  @edge-case
  Scenario: A silent stalled subprocess is terminated by the configured timeout
    Given a GuardKit subprocess has produced no standard output and no progress events
    When the subprocess has been running longer than the configured timeout
    Then the invocation should return a timeout result
    And the subprocess should be terminated before its process handle is released
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Edge-case expansion scenarios
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Actions: [A] Accept all / [R] Reject all / [M] Modify / [+] Add / [?] Defer — or per-scenario responses.

EDGE CASE EXPANSION (6 scenarios): _

A
All 6 edge-case scenarios accepted. Proceeding to Phase 5.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (7 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter to accept, or type the actual value.

Drawn from explicit context (confidence=high — auto-proceed):

[1] Default subprocess timeout
Proposed: 600 seconds  Confidence: high
Basis: API-subprocess.md §2 — permissions.shell.timeout_default_seconds: 600
Accept or enter value: _

[2] Manifest-dependency traversal depth cap
Proposed: 2 levels  Confidence: high
Basis: DDR-005 "Decision — Circular dependency guard" (depth-2 stop)
Accept or enter value: _

[3] Standard-output tail truncation size
Proposed: 4 KB  Confidence: high
Basis: API-subprocess.md §3.4 GuardKitResult.stdout_tail — "Last 4 KB for logging"
Accept or enter value: _

[4] Manifest categories selected for feature-spec invocations
Proposed: specs, contracts, source, decisions  Confidence: high
Basis: DDR-005 _COMMAND_CATEGORY_FILTER["feature-spec"]
Accept or enter value: _

Genuinely inferred (confidence=medium — Coach-reviewable):

[5] Retry-with-additional-context behaviour
Proposed: Reasoning model may retry a failed wrapper with extra explicit
context paths; retry is a fresh subprocess, and the extra paths
merge with manifest-derived ones for that retry only
Confidence: medium
Basis: Forge build-plan "error handling and retry" mentioned for FEAT-FORGE-005,
but no explicit retry contract in API-tool-layer or API-subprocess
Accept or enter value: _

[6] Concurrent GuardKit invocations within a build
Proposed: Multiple guardkit_* wrappers may run in parallel within the
same build worktree; each returns an independent result
Confidence: medium
Basis: Not prohibited by any context file; DeepAgents async execute
permits it; parallel dispatch fan-out is plausible for autobuild
Accept or enter value: _

[7] Concurrent-build isolation of context resolution
Proposed: The context resolver holds no mutable state shared across
concurrent builds; every invocation re-reads the manifest
Confidence: medium
Basis: DDR-005 describes the resolver as a stateless function of
(repo_path, subcommand); no caching layer is specified
Accept or enter value: _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

accept defaults
All defaults accepted. Writing output files.

Bash Create output directory
IN
mkdir -p /home/richardwoollcott/Projects/appmilla_github/forge/features/guardkit-command-invocation-engine && echo "ok"

OUT
ok
Write guardkit-command-invocation-engine.feature
319 lines
# Generated by /feature-spec
# Feature: GuardKit Command Invocation Engine (FEAT-FORGE-005)
# Stack: python
# Assumptions: 7 (see guardkit-command-invocation-engine_assumptions.yaml)
# Generated: 2026-04-24T00:00:00Z

@feat-forge-005 @guardkit-invocation-engine
Feature: GuardKit Command Invocation Engine
  As Rich, the sole operator of Forge
  I want Forge to invoke GuardKit subcommands as subprocesses with context flags assembled automatically,
    stream progress while running, capture artefacts, and handle failures without crashing the build,
  So that the pipeline can drive /system-arch, /feature-spec, /feature-plan, autobuild, and related
    GuardKit commands end-to-end without hand-assembled flags or brittle output parsing.

  Background:
    Given Forge is running inside an ephemeral build worktree
    And a project configuration file defines the shell, filesystem, and network permissions
    And a context manifest is available at the repo root describing documents grouped by category

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core invocation — a GuardKit subcommand runs and returns a parsed success result
  @key-example @smoke
  Scenario: A GuardKit subcommand completes successfully and its artefacts are captured
    Given the reasoning model invokes a GuardKit subcommand wrapper for the current build
    When the GuardKit process exits cleanly
    Then the invocation should report success
    And the returned result should list every artefact path emitted by GuardKit
    And the returned result should include the command's wall-clock duration
    And the returned result should include the Coach score when GuardKit produced one

  # Why: Auto-assembled context flags — the central value prop, zero hand-wiring
  # [ASSUMPTION: confidence=high] /feature-spec pulls manifest categories: specs, contracts, source, decisions
  @key-example @smoke
  Scenario: Context flags are assembled automatically from the manifest for the invoked subcommand
    Given the manifest lists documents tagged with multiple categories
    When Forge invokes the feature-spec wrapper
    Then the resolver should select only documents whose category is relevant to feature-spec
    And every always-included document should be prepended to the context list
    And the subprocess command line should carry one context flag per selected document

  # Why: Live telemetry — progress events surface on the bus while the subprocess still runs
  @key-example
  Scenario: GuardKit progress is streamed on the bus while the subprocess is still running
    Given the reasoning model invokes any GuardKit wrapper that supports streaming
    When the GuardKit process emits progress events during its run
    Then Forge should observe each progress event on the pipeline progress channel
    And the blocking invocation should still return only after the subprocess exits

  # Why: Error contract — the tool layer must never raise past its boundary
  @key-example @smoke
  Scenario: A failing GuardKit subprocess is reported as a structured error, not an exception
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits with a non-zero status
    Then the invocation should return a structured failure result
    And the failure result should carry the captured error output
    And the tool layer should not propagate an exception to the reasoning model

  # Why: Worktree confinement — every subprocess runs inside the build's ephemeral directory
  @key-example
  Scenario: Subprocesses are executed inside the current build's worktree
    Given a build's worktree has been prepared
    When any GuardKit, git, or shell subprocess is launched through the adapter
    Then the subprocess working directory should be the build's worktree
    And the subprocess should not be able to read or write outside the allowed worktree paths

  # Why: Version control adapter — PR creation is the terminal artefact of a build
  @key-example
  Scenario: Forge opens a pull request for the build through the version-control adapter
    Given a build has committed and pushed its work to a remote branch
    When Forge asks the version-control adapter to open a pull request
    Then the adapter should create the pull request against the configured base branch
    And the invocation should return the pull-request URL as a structured result

  # Why: Graphiti subcommands bypass manifest resolution — they don't take --context
  @key-example
  Scenario: Graphiti GuardKit subcommands skip context-manifest resolution entirely
    When Forge invokes a Graphiti GuardKit wrapper
    Then no context flags should be assembled
    And no manifest lookup should be performed for this invocation

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Just-inside timeout — a subprocess that finishes just before the cap still succeeds
  # [ASSUMPTION: confidence=high] Default subprocess timeout is 600 seconds
  @boundary
  Scenario Outline: A subprocess that finishes within the timeout is reported as successful
    Given the subprocess timeout has been configured
    When a GuardKit wrapper completes after <seconds> seconds of runtime
    Then the invocation should report success
    And the returned duration should match the observed runtime

    Examples:
      | seconds |
      | 1       |
      | 300     |
      | 599     |

  # Why: Just-outside timeout — once the cap is breached, the result is "timeout", not "failed"
  @boundary @negative
  Scenario: A subprocess that exceeds the timeout is reported as timed-out
    Given the subprocess timeout has been configured
    When a GuardKit wrapper has been running longer than the configured timeout
    Then the invocation should return a timeout result
    And the reasoning model should be free to decide whether to retry or escalate

  # Why: Depth-2 dependency chase — the resolver should follow one hop into a sibling repo
  # [ASSUMPTION: confidence=high] Dependency traversal depth cap is 2
  @boundary
  Scenario: Context resolution follows dependency references up to the depth cap
    Given the manifest declares a dependency on a sibling repository
    And the sibling repository declares its own manifest with key documents
    When Forge invokes a subcommand that consumes both manifests' categories
    Then the resolver should include selected documents from both manifests
    And the final context list should preserve a stable order

  # Why: Just-outside depth — traversing a deeper chain should stop and warn, not explode
  @boundary @edge-case
  Scenario: Context resolution stops at the depth cap and warns instead of recursing further
    Given the manifest chain would require traversing beyond the resolver's depth cap
    When Forge resolves context flags for a subcommand
    Then the resolver should stop at the depth cap
    And the resolver should emit a structured cycle-detected warning
    And the invocation should still proceed with the documents resolved so far

  # Why: Just-inside stdout-tail — a compact stdout is preserved verbatim
  # [ASSUMPTION: confidence=high] stdout_tail captures the last 4 KB of subprocess output
  @boundary
  Scenario: A compact stdout is preserved verbatim in the returned result
    Given a GuardKit subprocess that prints fewer than four kilobytes to standard output
    When the invocation completes
    Then the returned result should include the full standard output

  # Why: Just-outside stdout-tail — larger output is truncated to the tail, not dropped
  @boundary
  Scenario: A large stdout is truncated to the most recent tail in the returned result
    Given a GuardKit subprocess that prints far more than the captured tail size
    When the invocation completes
    Then the returned result should include only the most recent slice of standard output
    And the slice size should match the configured tail limit

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Missing manifest — proceed without --context rather than blocking adoption
  @negative
  Scenario: A missing context manifest degrades gracefully to no context flags
    Given the target repository has no context manifest
    When Forge invokes any GuardKit wrapper for that repository
    Then the invocation should proceed with no context flags
    And a structured warning should be recorded indicating the manifest was missing
    And the reasoning model should be able to see the warning in the returned result

  # Why: A binary that isn't on the allowlist must be refused before it runs
  @negative
  Scenario: A subprocess whose binary is not in the shell allowlist is refused
    Given the project configuration lists the permitted shell binaries
    When the reasoning model attempts to execute a binary that is not on the allowlist
    Then the subprocess should be refused before launch
    And the invocation should return a structured permissions error
    And no side effects should occur on the worktree

  # Why: A cwd outside the worktree allowlist must be refused — worktree confinement invariant
  @negative
  Scenario: A subprocess targeting a working directory outside the allowlist is refused
    Given the project configuration allows subprocesses only within the build worktrees path
    When a subprocess is requested with a working directory outside that path
    Then the subprocess should be refused before launch
    And the invocation should return a structured permissions error

  # Why: Non-zero exit with captured stderr — must reach the reasoning model intact
  @negative
  Scenario: A non-zero exit is reported as a failure with the subprocess error output
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits with a non-zero status and writes diagnostics to its error stream
    Then the invocation should return a structured failure result
    And the failure result should include the subprocess exit status and error output

  # Why: Tolerant parser — unknown output shape should NOT explode the whole call
  @negative @edge-case
  Scenario: An unknown GuardKit output shape degrades to success with no artefacts
    Given the reasoning model invokes a GuardKit wrapper
    When the subprocess exits cleanly but its output does not match the expected artefact shape
    Then the invocation should still report success
    And the returned artefact list should be empty
    And the reasoning model should be responsible for deciding whether the stage produced useful work

  # Why: Documents outside the filesystem read-allowlist must be omitted, not silently included
  @negative
  Scenario: Context documents that fall outside the read allowlist are omitted with a warning
    Given the manifest references a document whose path is outside the filesystem read allowlist
    When Forge resolves context flags for a subcommand
    Then the out-of-allowlist document should be omitted from the context list
    And a structured warning should be emitted identifying the omitted document

  # Why: Missing GitHub credentials — surface at the adapter boundary, never raise
  @negative
  Scenario: A pull-request creation without GitHub credentials returns a structured error
    Given the runtime has no GitHub access credentials available
    When Forge asks the version-control adapter to open a pull request
    Then the adapter should return a structured error explaining the credential is missing
    And no pull request should be created

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: The resolver must not loop on a circular dependency graph
  @edge-case
  Scenario: A circular dependency chain is detected and resolved safely
    Given two repositories' manifests reference one another as dependencies
    When Forge resolves context flags for a subcommand
    Then the resolver should detect the cycle before repeating a manifest
    And the resolver should stop at the depth cap
    And a structured cycle-detected warning should be recorded

  # Why: Worktree cleanup is best-effort — must not block terminal transitions
  @edge-case
  Scenario: A failed worktree cleanup is logged but does not prevent build completion
    Given a build has reached a terminal state
    When the adapter attempts to delete the build's worktree
    And the deletion fails
    Then the build should still be marked as terminal in history
    And a structured warning should be logged about the failed cleanup

  # Why: NATS progress stream is telemetry, not authoritative — outcome must still be returned
  @edge-case
  Scenario: The authoritative result still returns when progress streaming is unavailable
    Given the progress stream channel is unavailable during a GuardKit invocation
    When the subprocess exits cleanly
    Then the invocation should still return a parsed success result with artefact paths
    And the missing progress stream should not itself fail the call

  # Why: Retry with additional context — explicit build-plan requirement ("error handling and retry")
  # [ASSUMPTION: confidence=medium] A failed invocation can be retried with extra context flags chosen by the reasoning model
  @edge-case
  Scenario: A failed invocation can be retried with additional explicit context
    Given a GuardKit wrapper has returned a failure result
    When the reasoning model retries the same wrapper with additional explicit context paths
    Then the retry should merge the explicit paths with the manifest-derived ones
    And the retry should be a fresh subprocess launch, not a continuation

  # Why: Tool-layer exceptions must never escape — the universal error contract
  @edge-case
  Scenario: An unexpected error inside a wrapper is returned as a structured error, not raised
    Given any GuardKit wrapper is invoked
    When an unexpected internal error occurs inside the wrapper
    Then the wrapper should catch the error
    And the wrapper should return a structured error result describing the error type and message
    And no exception should propagate to the reasoning model

  # Why: Multiple parallel invocations within the same build — important for autobuild fan-out
  # [ASSUMPTION: confidence=medium] Multiple GuardKit wrappers may run in parallel within the same build worktree
  @edge-case
  Scenario: Parallel GuardKit invocations in the same build do not corrupt each other's results
    Given two GuardKit wrappers are invoked in parallel within the same build
    When both subprocesses complete
    Then each invocation should receive its own independent structured result
    And neither result should contain artefacts or progress events belonging to the other

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security, Concurrency, and Integration Boundaries
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Path traversal in a manifest entry must not escape the repository root
  @edge-case @negative
  Scenario: A context manifest entry that would escape the repository root is rejected
    Given the context manifest lists a document path that resolves outside the target repository
    When Forge resolves context flags for a subcommand
    Then the out-of-repository path should be omitted from the context list
    And a structured warning should be emitted identifying the rejected path
    And the invocation should proceed with the remaining valid documents

  # Why: Shell-metacharacter injection via arguments must stay literal, not expand
  @edge-case @negative
  Scenario: Shell metacharacters in subprocess arguments are passed as literal tokens
    Given the reasoning model builds subprocess arguments that contain shell metacharacters
    When the adapter launches the subprocess
    Then each argument should be delivered to the binary as a single literal token
    And no shell expansion or secondary command should be evaluated

  # Why: Two concurrent builds targeting the same repo must resolve independently
  # [ASSUMPTION: confidence=medium] Context resolution has no cached state shared across concurrent builds
  @edge-case
  Scenario: Two concurrent builds against the same repository resolve context independently
    Given two builds are running at the same time against the same target repository
    When each build invokes a GuardKit wrapper requiring context resolution
    Then each build should receive its own resolved context flag list
    And one build's resolver warnings should not appear in the other build's result

  # Why: Cancellation must terminate in-flight subprocess and discard partial artefacts
  @edge-case
  Scenario: A cancelled build terminates its in-flight subprocess cleanly
    Given a GuardKit subprocess is currently running inside a build
    When the build is cancelled
    Then the subprocess should be terminated
    And any partial artefacts produced by the terminated subprocess should not be reported as completed work

  # Why: The live status view must not miss progress events because of slow consumption
  @edge-case
  Scenario: Progress events emitted faster than Forge consumes them are still observable for live status
    Given a GuardKit subprocess is emitting progress events at a high cadence
    When Forge is slower to consume than the producer is to emit
    Then the live status view should still reflect the most recent progress events
    And the authoritative completion result should remain unaffected

  # Why: A stalled subprocess with no output still hits the timeout, never hangs the build
  @edge-case
  Scenario: A silent stalled subprocess is terminated by the configured timeout
    Given a GuardKit subprocess has produced no standard output and no progress events
    When the subprocess has been running longer than the configured timeout
    Then the invocation should return a timeout result
    And the subprocess should be terminated before its process handle is released
Write guardkit-command-invocation-engine_assumptions.yaml
86 lines
# Assumptions manifest for guardkit-command-invocation-engine.feature
# Generated by /feature-spec

feature: "GuardKit Command Invocation Engine"
feature_id: "FEAT-FORGE-005"
generated: "2026-04-24T00:00:00Z"
stack: "python"
review_required: false

assumptions:
  - id: "ASSUM-001"
    scenario: "A subprocess that finishes within the timeout is reported as successful"
    assumption: "Default subprocess timeout is 600 seconds"
    confidence: high
    basis: >-
      API-subprocess.md §2 — forge.yaml.permissions.shell.timeout_default_seconds is 600.
      Stated explicitly in the permissions schema.
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "Context resolution follows dependency references up to the depth cap"
    assumption: "Manifest-dependency traversal depth cap is 2 levels"
    confidence: high
    basis: >-
      DDR-005 "Decision — Circular dependency guard". Resolver stops at depth 2
      with a context_manifest_cycle_detected warning.
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "A compact stdout is preserved verbatim in the returned result"
    assumption: "GuardKitResult.stdout_tail captures the last 4 KB of subprocess output"
    confidence: high
    basis: >-
      API-subprocess.md §3.4 GuardKitResult schema — stdout_tail is explicitly the
      last 4 KB for logging.
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "Context flags are assembled automatically from the manifest for the invoked subcommand"
    assumption: >-
      The manifest categories selected for /feature-spec invocations are
      specs, contracts, source, and decisions
    confidence: high
    basis: >-
      DDR-005 _COMMAND_CATEGORY_FILTER["feature-spec"] = {specs, contracts, source, decisions}.
      Hardcoded in forge.adapters.guardkit.context_resolver.
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "A failed invocation can be retried with additional explicit context"
    assumption: >-
      The reasoning model may retry a failed wrapper with extra explicit context paths;
      the retry is a fresh subprocess launch and the extra paths merge with the
      manifest-derived ones for that retry only
    confidence: medium
    basis: >-
      forge-build-plan.md FEAT-FORGE-005 scope mentions "Error handling and retry",
      but neither API-tool-layer.md §6 nor API-subprocess.md specifies a retry
      contract — this is the minimal inferred shape that satisfies the build-plan
      requirement while keeping retry a reasoning-model concern.
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "Parallel GuardKit invocations in the same build do not corrupt each other's results"
    assumption: >-
      Multiple guardkit_* wrappers may run in parallel within the same build worktree;
      each returns an independent structured result
    confidence: medium
    basis: >-
      Not prohibited by any context file; DeepAgents async execute permits it;
      parallel dispatch fan-out is plausible for autobuild orchestration. No
      explicit concurrency contract is stated in API-tool-layer.md §6.
    human_response: "confirmed"

  - id: "ASSUM-007"
    scenario: "Two concurrent builds against the same repository resolve context independently"
    assumption: >-
      The context resolver holds no mutable state shared across concurrent builds;
      every invocation re-reads the manifest
    confidence: medium
    basis: >-
      DDR-005 describes the resolver as a stateless function of
      (repo_path, subcommand); no caching layer is specified. This assumption
      makes that statelessness explicit for the test surface.
    human_response: "confirmed"
Write guardkit-command-invocation-engine_summary.md
62 lines
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
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: GuardKit Command Invocation Engine (FEAT-FORGE-005)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:

guardkit-command-invocation-engine.feature
guardkit-command-invocation-engine_assumptions.yaml
guardkit-command-invocation-engine_summary.md
Scenarios: 32 total
@key-example: 7   @boundary: 6
@negative: 10     @edge-case: 14
@smoke: 3         @regression: 0

Assumptions: 7 total
high: 4 (auto-proceed — all drawn from API-subprocess.md / DDR-005 explicit text)
medium: 3 (Coach-reviewable — retry contract, in-build parallelism, cross-build isolation)
low: 0

Deferred: none.

Review required: No — no low-confidence assumptions.

Next steps:

Review: guardkit-command-invocation-engine.feature
Pass to feature-plan (FEAT-FORGE-005 depends on FEAT-FORGE-001, can parallel FEAT-FORGE-002):

guardkit feature-plan FEAT-FORGE-005 \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine_summary.md