# ideas -- Product Roadmap

## Mode

evolve

## Change Summary

The existing build plan already covers the core Forge implementation roadmap with FEAT-FORGE-001 through FEAT-FORGE-008, so this evolve roadmap does not re-propose those items. Instead, it adds six next-step features around the edges of that plan: FEAT-PO-001 introduces a prerequisite readiness gate before `/system-arch`; FEAT-PO-002 operationalises placeholder cleanup and history-file discipline after architecture, design, and feature-spec sessions; FEAT-PO-003 adds safe validation and degraded fallback for cross-repo context manifests; FEAT-PO-004 makes Graphiti-optional operation visible rather than implicit; FEAT-PO-005 adds checkpoint override telemetry for threshold calibration; and FEAT-PO-006 turns the documented 'where are we?' promise into a queryable status capability. Priority has shifted toward execution safety, degraded-mode visibility, and calibration feedback because the biggest current gap is not missing core Forge features on paper, but the operational glue needed to start and run that plan with confidence.

## Epics

### EPIC-001: Forge foundation execution readiness

**Bounded Context:** Pipeline Orchestration

This epic closes the immediate gaps between the existing Forge build plan and the documented readiness requirements that still block execution. It focuses on prerequisite verification, architecture-session hygiene, and the operational scaffolding needed to start and sustain the Forge build with less manual drift.

**Features:**
  - FEAT-PO-001: Prerequisite readiness gate for Forge build start
  - FEAT-PO-002: Build plan placeholder resolution and command-history enforcement

### EPIC-002: Cross-repo context assembly and degraded delivery operations

**Bounded Context:** Context Resolution

This epic extends the existing command invocation and degraded-mode concepts into operationally complete capabilities that the current build plan references but does not yet cover fully. It strengthens how Forge assembles context from manifests and how it behaves when specialist or Graphiti capabilities are absent.

**Features:**
  - FEAT-PO-003: Context manifest validation and missing-manifest fallback policy
  - FEAT-PO-004: Graphiti-optional coordination with explicit knowledge-compounding status

### EPIC-003: Outcome visibility and calibration feedback

**Bounded Context:** Checkpoint Management

This epic adds the next layer of operational feedback needed once the initial Forge feature set exists on paper. It turns the confidence-gated model into an observable and calibratable system, grounded in the product docs' emphasis on outcome gates, override learning, and proactive signals.

**Features:**
  - FEAT-PO-005: Checkpoint override telemetry and threshold calibration reporting
  - FEAT-PO-006: Pipeline status query and stakeholder-facing outcome summary

## Priority Rationale

The current build plan already defines the core Forge implementation sequence through FEAT-FORGE-001 to FEAT-FORGE-008, so the next roadmap should not restate those features. The highest-value additions are the gaps that sit around that plan: proving readiness before the build starts, preventing placeholder drift after architecture and design sessions, making context-manifest automation safe when dependencies are incomplete, and improving visibility into degraded operation and checkpoint calibration. These features come next because they reduce execution risk for the existing plan and make the confidence-gated model operationally trustworthy once the main Forge build begins.

## Constraints and Dependencies

- The existing Forge feature set FEAT-FORGE-001 through FEAT-FORGE-008 remains the current core build plan and is treated as existing planned coverage.
- Hard prerequisites from the build plan still block execution: live NATS infrastructure, nats-core integration tests, specialist-agent Phase 3, and at least one NATS-callable specialist agent.
- Graphiti runtime remains optional but must be surfaced explicitly when absent.
- Context manifest automation depends on manifests existing in target repos and following the documented category convention.
- PR review remains always human and must not be auto-approved.

## Open Questions

- Should the readiness gate live as a standalone preflight command, part of `forge_status`, or an automatic first stage of every pipeline run?
- What exact machine-readable format should be used for placeholder-resolution checks and command-history enforcement?
- Should degraded context assembly stop the pipeline entirely for some command types, or always continue with a forced review?
- Which stakeholder-facing surface should be the canonical status output first: CLI, NATS notification stream, or dashboard adapter?

## Coverage Score

**100%** of document sections have at least one mapped feature.

Raw score: 1.0

## Feature Spec Inputs

### FEAT-PO-001: Prerequisite readiness gate for Forge build start

**Bounded Context:** Pipeline Orchestration

**Description:**
Forge should provide an explicit readiness gate that validates all hard prerequisites before `/system-arch` can begin, rather than relying on the build plan checklist being interpreted manually. The gate should verify NATS infrastructure availability, live nats-core integration status, specialist-agent Phase 3 completion, and at least one NATS-callable specialist role, then produce a machine-readable readiness report that explains which blocking conditions remain and why the build cannot start.

**Source Documents:** forge-build-plan.md, conversation-starter-gap-analysis.md, fleet-master-index.md

**Constraints:**
  - Must enforce the hard prerequisites listed before Step 1 in the existing build plan
  - Must distinguish blocking prerequisites from soft prerequisites
  - Must not start Forge build stages when specialist-agent Phase 3 or live NATS validation is missing

**Suggested Context Files:** forge/docs/research/ideas/forge-build-plan.md, forge/docs/research/ideas/fleet-master-index.md, forge/docs/research/ideas/conversation-starter-gap-analysis.md

### FEAT-PO-002: Build plan placeholder resolution and command-history enforcement

**Bounded Context:** Delivery Workflow Governance

**Description:**
After `/system-arch`, `/system-design`, and each `/feature-spec` session, Forge should detect unresolved placeholders in the build plan and require them to be replaced with concrete file paths before downstream steps continue. It should also enforce creation and updating of `command-history.md` and `feature-spec-FEAT-FORGE-XXX-history.md` so that the formalised history patterns documented in the fleet are captured as part of execution, not left as optional housekeeping.

**Source Documents:** forge-build-plan.md, fleet-master-index.md, forge-ideas-overhaul-conversation-starter.md

**Constraints:**
  - Must remove `<placeholder>` drift before later command steps are run
  - Must follow the documented command history and feature spec history patterns
  - Must preserve the build plan as the authoritative execution record

**Suggested Context Files:** forge/docs/research/ideas/forge-build-plan.md, forge/docs/research/ideas/fleet-master-index.md, forge/docs/research/ideas/forge-ideas-overhaul-conversation-starter.md

**Depends On:** FEAT-PO-001

### FEAT-PO-003: Context manifest validation and missing-manifest fallback policy

**Bounded Context:** Context Resolution

**Description:**
Forge should validate `.guardkit/context-manifest.yaml` availability and schema quality across target repos before assembling `--context` flags, because the current plan depends on manifests that are not yet complete in all repos. When a required manifest is missing or incomplete, Forge should fall back to a documented degraded policy that identifies which context categories cannot be assembled automatically, flags the pipeline for review, and records the missing dependency map as an operational issue rather than silently proceeding with partial context.

**Source Documents:** forge-build-plan.md, fleet-master-index.md, forge-pipeline-orchestrator-refresh.md

**Constraints:**
  - Must use the context manifest convention and category filtering described in the docs
  - Must account for lpa-platform and specialist-agent manifests still being incomplete
  - Must force review when automated context assembly is degraded

**Suggested Context Files:** forge/docs/research/ideas/forge-build-plan.md, forge/docs/research/ideas/fleet-master-index.md, forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md

**Depends On:** FEAT-PO-001

### FEAT-PO-004: Graphiti-optional coordination with explicit knowledge-compounding status

**Bounded Context:** Infrastructure Coordination

**Description:**
Forge should treat Graphiti coordination as an explicit optional capability rather than a silent best-effort integration. When specialist-agent Phase G or Graphiti runtime support is unavailable, Forge should continue the pipeline but publish and persist a clear status that knowledge seeding, cross-project querying, and compounding metrics were skipped, so operators understand that the pipeline ran in a non-compounding mode rather than assuming the knowledge layer was active.

**Source Documents:** forge-build-plan.md, conversation-starter-gap-analysis.md, big-picture-vision-and-durability.md, fleet-master-index.md

**Constraints:**
  - Must honour the build plan rule that Graphiti runtime is valuable but not blocking
  - Must not claim knowledge compounding when Phase G is not built
  - Must preserve pipeline continuity while increasing operator visibility

**Suggested Context Files:** forge/docs/research/ideas/forge-build-plan.md, forge/docs/research/ideas/conversation-starter-gap-analysis.md, forge/docs/research/ideas/big-picture-vision-and-durability.md, forge/docs/research/ideas/fleet-master-index.md

**Depends On:** FEAT-PO-001

### FEAT-PO-005: Checkpoint override telemetry and threshold calibration reporting

**Bounded Context:** Checkpoint Management

**Description:**
Forge should record how often humans approve flagged outputs, reject auto-approved outputs, and override stage decisions so that checkpoint thresholds can be adjusted from evidence instead of intuition. The reporting should expose stage-by-stage calibration signals, including override rate, critical detection frequency, and review burden, because the documentation explicitly frames confidence-gated checkpoints as something that can mature over time rather than a one-off static configuration.

**Source Documents:** forge-pipeline-orchestrator-refresh.md, fleet-master-index.md, big-picture-vision-and-durability.md

**Constraints:**
  - Must remain advisory rather than automatically changing thresholds initially
  - Must align with the confidence-gated checkpoint model already defined
  - Must support per-stage and per-project analysis

**Suggested Context Files:** forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md, forge/docs/research/ideas/fleet-master-index.md, forge/docs/research/ideas/big-picture-vision-and-durability.md

**Depends On:** FEAT-PO-001

### FEAT-PO-006: Pipeline status query and stakeholder-facing outcome summary

**Bounded Context:** Pipeline Orchestration

**Description:**
Forge should provide a queryable pipeline status view that answers the documented stakeholder question, 'where are we?', using stage state, checkpoint outcome, degraded-mode flags, and artifact links instead of traditional status reporting. The summary should show whether a project is blocked on prerequisites, awaiting approval, building features, running without Graphiti compounding, or ready for PR review, so the system delivers the proactive signals promised by the context-first delivery model.

**Source Documents:** fleet-master-index.md, conversation-starter-gap-analysis.md, big-picture-vision-and-durability.md, forge-pipeline-orchestrator-refresh.md

**Constraints:**
  - Must support context-first delivery and outcome-gate framing rather than ticket-based reporting
  - Must include degraded-mode and checkpoint status in the output
  - Must be compatible with the existing Forge identity and `forge_status` tool intent

**Suggested Context Files:** forge/docs/research/ideas/fleet-master-index.md, forge/docs/research/ideas/conversation-starter-gap-analysis.md, forge/docs/research/ideas/big-picture-vision-and-durability.md, forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md

**Depends On:** FEAT-PO-001, FEAT-PO-005

## Source Documents

| Document | Contribution |
| --- | --- |
| forge-build-plan.md | Provided the current planned Forge feature set, dependency order, prerequisites, command sequence, placeholder update process, and file-level implementation sketch. It established which capabilities are already planned versus which operational gaps remain. |
| fleet-master-index.md | Provided the authoritative fleet decisions, formalised patterns, context-first delivery model, confidence-gated checkpoint design, and current repo/build status. It was the primary source for existing coverage across the broader Software Factory. |
| forge-pipeline-orchestrator-refresh.md | Provided the evolved Forge identity as checkpoint manager, the confidence-gated operating model, degraded-mode behaviour, context manifest convention, and the open question about threshold calibration and status telemetry. |
| conversation-starter-gap-analysis.md | Highlighted unresolved gaps such as live integration testing, Graphiti runtime still pending, and the practical migration path to Forge as capstone. It also reinforced the stakeholder need for queryable context and proactive signals. |
| big-picture-vision-and-durability.md | Grounded the roadmap in the strategic context-first delivery model, outcome-gate philosophy, and durable operational patterns behind the Software Factory. |
| forge-ideas-overhaul-conversation-starter.md | Documented the formalised patterns for build plans, command history, and feature-spec history, which informed the proposed enforcement features around execution hygiene. |
| architect-agent-finproxy-build-plan.md | Used only as historical evidence for prior build and Graphiti planning patterns, consistent with its superseded warning. It was not treated as a current source of truth for Forge feature direction. |

## Assumptions

| # | Category | Assumption | Confidence | Impact if Wrong |
| --- | --- | --- | --- | --- |
| ASM-001 | constraints | The existing FEAT-FORGE-001 through FEAT-FORGE-008 roadmap in `forge-build-plan.md` represents the current planned implementation baseline and should count toward documentation coverage. | high | If those features are not considered active planned coverage, the coverage score would fall and this roadmap would need to restate or replace the core Forge features. |
| ASM-002 | integration | The Forge repository does not yet have completed operational features for readiness gating, manifest validation, and calibration telemetry beyond what is described in planning documents. | medium | If some of these capabilities already exist in code or another plan, parts of this roadmap may duplicate work and should be converted into enhancement or hardening features. |
| ASM-003 | technology | Graphiti query results were unavailable or empty for this planning pass, so historical grounding relied primarily on the provided documentation set. | high | If relevant Graphiti facts exist but were not retrieved, some dependency or decision nuances may be missing from the roadmap rationale. |
| ASM-004 | scope | A product documentation section is counted as covered if it is addressed either by the existing Forge build-plan features or by one of the newly proposed roadmap features. | high | Coverage scoring and gap analysis would need recalculation. |
