/feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-build-lifecycle.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md \
  --context forge/features/nats-fleet-integration/nats-fleet-integration.feature \
  --context forge/features/nats-fleet-integration/nats-fleet-integration_summary.md \
  --context forge/features/specialist-agent-delegation/specialist-agent-delegation.feature \
  --context forge/features/specialist-agent-delegation/specialist-agent-delegation_summary.md \
  --context forge/features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature \
  --context forge/features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine_summary.md \
  --context forge/features/infrastructure-coordination/infrastructure-coordination.feature \
  --context forge/features/infrastructure-coordination/infrastructure-coordination_summary.md

  I'll execute /feature-spec for FEAT-FORGE-007 through the six-phase Propose-Review methodology. Starting with Phase 1: Context Gathering.

Bash Find FEAT-FORGE-007 feature file
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/features/ 2>/dev/null && find /home/richardwoollcott/Projects/appmilla_github/forge -maxdepth 4 -name "FEAT-FORGE-007*" 2>/dev/null

OUT
FEAT-FORGE-002.yaml
Bash List repo root
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/

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
Bash Search for FEAT-FORGE-007 references
IN
grep -r "FEAT-FORGE-007" /home/richardwoollcott/Projects/appmilla_github/forge --include="*.md" --include="*.yaml" -l 2>/dev/null | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/.claude/reviews/TASK-REV-C3E7-review-report.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/fleet-architecture-v3-coherence-via-flywheel.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md
/home/richardwoollcott/Projects/appmilla_github/forge/.claude/reviews/TASK-REV-C3E7-review-report.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/fleet-architecture-v3-coherence-via-flywheel.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/history/feature-spec-FEAT-FORGE-004-history.md
Bash List features and research folders
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/ 2>/dev/null

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/:
ADR-FLEET-001-trace-richness.md
architect-agent-finproxy-build-plan.md
big-picture-vision-and-durability.md
conversation-capture-2026-04-19-fleet-v3-framing.md
conversation-starter-gap-analysis.md
deepagents-053-verification.md
fleet-architecture-v3-coherence-via-flywheel.md
fleet-master-index.md
forge-build-plan.md
forge-ideas-overhaul-conversation-starter.md
forge-pipeline-orchestrator-refresh.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/:
confidence-gated-checkpoint-protocol
guardkit-command-invocation-engine
infrastructure-coordination
nats-fleet-integration
pipeline-state-machine-and-configuration
specialist-agent-delegation
Bash Find FEAT-FORGE-007 description in build plan
IN
grep -A 30 "FEAT-FORGE-007" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md 2>/dev/null | head -80

OUT
| FEAT-FORGE-007 | Mode A Greenfield End-to-End | 003, 004, 005, 006 | 3-5 days | Full integration: raw input → delegate to PO agent → checkpoint → delegate to architect → checkpoint → /system-arch → /system-design → /feature-spec × N → /feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint (PR review). The primary pipeline mode. | Full end-to-end pipeline validation |
| FEAT-FORGE-008 | Mode B Feature & Mode C Review-Fix | 007 | 2-3 days | Mode B: add feature to existing project (skip PO/architect delegation, start from /feature-spec). Mode C: review and fix issues (/task-review → /task-work cycle). Both use checkpoint protocol. | *(optional modes — not in anchor §10 Phase 4)* |

**Anchor §10 Phase 4 bullets not yet covered by a feature:**
- CLI commands: `forge queue`, `forge status`, `forge history`, `forge cancel`, `forge skip` — folded into FEAT-FORGE-001 (CLI entrypoint)
- SQLite build history schema — folded into FEAT-FORGE-006 (infrastructure coordination)

**Estimated total: 4-6 weeks** (includes iteration time, integration testing, and the
inevitable debugging of subprocess orchestration + async NATS patterns)

---

## GuardKit Command Sequence

### Step 1: /system-arch ✅ COMPLETE (2026-04-18)

Produced the Forge's system architecture — ARCHITECTURE.md, ADRs, C4 diagrams,
component boundaries.

```bash
cd ~/Projects/appmilla_github/forge

guardkit system-arch \
  --context forge/docs/research/forge-pipeline-architecture.md \
  --context forge/docs/research/forge-build-plan-alignment-review.md \
  --context forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md \
  --context forge/docs/research/pipeline-orchestrator-conversation-starter.md \
  --context forge/docs/research/pipeline-orchestrator-motivation.md \
  --context forge/docs/research/ideas/fleet-master-index.md \
  --context specialist-agent/docs/research/ideas/architect-agent-vision.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
--
- ◻ **Sequence diagrams for Mode A greenfield** — not produced. Content is implicit across the contract set (pipeline events + approval protocol + dispatch + subagents) but a single end-to-end sequence diagram would be useful. Consider an optional follow-up artefact before `/feature-spec FEAT-FORGE-007`.
- ◻ **BDD acceptance criteria for all 8 features** — **deferred to `/feature-spec`** (Step 3 below). Template expected this in /system-design; Forge's NOT-DDD structure means acceptance criteria belong with per-feature spec sessions, not with the interface-design pass.

**Coupled changes shipped in the same commit (`b40365c`):**
- `pyproject.toml` — pinned `nats-core>=0.2.0,<0.3`
- `docs/architecture/assumptions.yaml` — ASSUM-004 updated to reflect nats-core v0.2.0 / TASK-NCFA-003

**Graphiti seeding:** 20/20 artefacts seeded (`project_design` + `architecture_decisions` groups). Two initial vLLM flakes + one post-seed edit recovered with `--force` re-seed.

**Sibling task created in `nats-core`:** [`TASK-NCFA-003`](../../../../nats-core/tasks/backlog/forge-v2-alignment/TASK-NCFA-003-add-forge-system-design-pipeline-payloads.md) — shipped same-day as `nats-core 0.2.0`.

### Step 3: /feature-spec × 8 — 🟡 IN PROGRESS (6 / 8)

Produces BDD feature specifications for each feature. Run sequentially — later features
reference earlier ones.

**Completed:**
- ✅ **FEAT-FORGE-001** — Pipeline State Machine & Configuration (2026-04-24; revised 2026-04-24)
  - Artefacts: `features/pipeline-state-machine-and-configuration/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 34 (6 @key-example · 6 @boundary · 11 @negative · 16 @edge-case · 3 @smoke)
  - Assumptions: 5 resolved — 5 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - Post-review revisions: ASSUM-002 (arbitrary turn-budget ceiling) retired; ASSUM-005 (cancel-operator audit) promoted low → medium on schema grounds.
- ✅ **FEAT-FORGE-002** — NATS Fleet Integration (2026-04-24)
  - Artefacts: `features/nats-fleet-integration/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 33 (7 @key-example · 5 @boundary · 8 @negative · 15 @edge-case · 3 @smoke · 2 @security · 2 @concurrency · 2 @data-integrity · 2 @integration)
  - Assumptions: 5 resolved — 5 high, 0 medium, 0 low, 0 open. Ready for `/feature-plan` (plan already run — commit `6a29ed3`).
- ✅ **FEAT-FORGE-003** — Specialist Agent Delegation (2026-04-24)
  - Artefacts: `features/specialist-agent-delegation/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 33 (5 @key-example · 6 @boundary · 9 @negative · 15 @edge-case · 2 @smoke · 3 @security · 3 @concurrency · 1 @data-integrity · 2 @integration)
  - Assumptions: 6 resolved — 5 high, 1 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - ASSUM-005 (retry policy on soft-failure dispatch) kept at medium — build plan §128 mandates retry but leaves the count to the reasoning loop per ADR-ARCH-015.
--
# FEAT-FORGE-007: Mode A Greenfield End-to-End
# Design context: async-subagent stack (subagents API + both C4 L3 diagrams + DDR-006)
# plus the state machine (DM-build-lifecycle) and checkpoint protocol
# (API-nats-approval-protocol) that the mode threads through every stage.
# Prior-feature context: `.feature` + `_summary.md` for FEAT-FORGE-001..006
# (assumptions.yaml omitted — already echoed inside each summary).
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-build-lifecycle.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md \
  --context forge/features/nats-fleet-integration/nats-fleet-integration.feature \
Bash List feature spec history files
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/history/ 2>/dev/null

OUT
command-history.md
feature-plan-nats-integration-history.md
feature-spec-FEAT-FORGE-001.md
feature-spec-FEAT-FORGE-002-history.md
feature-spec-FEAT-FORGE-003-history.md
feature-spec-FEAT-FORGE-004-history.md
feature-spec-FEAT-FORGE-005-history.md
feature-spec-FEAT-FORGE-006-history.md
system-arch-history.md
system-design-history.md
Bash Find FEAT-FORGE-007 detailed description
IN
grep -B 2 -A 80 "^## FEAT-FORGE-007\|FEAT-FORGE-007.*Mode A.*End-to-End" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md 2>/dev/null | head -150

OUT
| FEAT-FORGE-005 | GuardKit Command Invocation Engine | 001 | 2-3 days | Subprocess calls to `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`. Context flag construction from pipeline state + `.guardkit/context-manifest.yaml`. Output capture and artifact path tracking. Error handling and retry. | Subprocess invocation of GuardKit AutoBuild with `--nats` flag (Stage 4) |
| FEAT-FORGE-006 | Infrastructure Coordination | 001, 002 | 2-3 days | Graphiti seeding after each pipeline stage, Graphiti querying for cross-project context, test verification, git operations (clone/pull/branch/push/PR via `gh`) | Git operations, SQLite build history and stage log |
| FEAT-FORGE-007 | Mode A Greenfield End-to-End | 003, 004, 005, 006 | 3-5 days | Full integration: raw input → delegate to PO agent → checkpoint → delegate to architect → checkpoint → /system-arch → /system-design → /feature-spec × N → /feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint (PR review). The primary pipeline mode. | Full end-to-end pipeline validation |
| FEAT-FORGE-008 | Mode B Feature & Mode C Review-Fix | 007 | 2-3 days | Mode B: add feature to existing project (skip PO/architect delegation, start from /feature-spec). Mode C: review and fix issues (/task-review → /task-work cycle). Both use checkpoint protocol. | *(optional modes — not in anchor §10 Phase 4)* |

**Anchor §10 Phase 4 bullets not yet covered by a feature:**
- CLI commands: `forge queue`, `forge status`, `forge history`, `forge cancel`, `forge skip` — folded into FEAT-FORGE-001 (CLI entrypoint)
- SQLite build history schema — folded into FEAT-FORGE-006 (infrastructure coordination)

**Estimated total: 4-6 weeks** (includes iteration time, integration testing, and the
inevitable debugging of subprocess orchestration + async NATS patterns)

---

## GuardKit Command Sequence

### Step 1: /system-arch ✅ COMPLETE (2026-04-18)

Produced the Forge's system architecture — ARCHITECTURE.md, ADRs, C4 diagrams,
component boundaries.

```bash
cd ~/Projects/appmilla_github/forge

guardkit system-arch \
  --context forge/docs/research/forge-pipeline-architecture.md \
  --context forge/docs/research/forge-build-plan-alignment-review.md \
  --context forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md \
  --context forge/docs/research/pipeline-orchestrator-conversation-starter.md \
  --context forge/docs/research/pipeline-orchestrator-motivation.md \
  --context forge/docs/research/ideas/fleet-master-index.md \
  --context specialist-agent/docs/research/ideas/architect-agent-vision.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md
```

**Actual outputs (on disk):**
- [`forge/docs/architecture/ARCHITECTURE.md`](../../architecture/ARCHITECTURE.md) — module map, stack, gates, relationship to anchor v2.2, decision index (31 ADRs)
- [`forge/docs/architecture/system-context.md`](../../architecture/system-context.md) — C4 Level 1
- [`forge/docs/architecture/container.md`](../../architecture/container.md) — C4 Level 2 (approved)
- [`forge/docs/architecture/domain-model.md`](../../architecture/domain-model.md) — core concepts, lifecycle, ownership
- [`forge/docs/architecture/assumptions.yaml`](../../architecture/assumptions.yaml) — tracked assumptions (updated 2026-04-23 for nats-core v0.2.0)
- [`forge/docs/architecture/decisions/ADR-ARCH-001`..`031`](../../architecture/decisions/) — 31 ADRs across structural / tool layer / learning / gating / state / API / fleet / deployment / security / cost / implementation

**Key revisions since initial session:**
- ADR-ARCH-021 Revision 10 (2026-04-20) — server-mode rehydration contract (Option C hybrid) recorded after TASK-SPIKE-D2F7
- ADR-ARCH-031 (2026-04-19) — async subagents for `autobuild_runner`
- Commits: `9f41e22` (initial `/system-arch`) + `7bcc7da` (review + refinements) + `0a40b25` (ADR-021 Rev 10) + `79589c5` (TASK-ADR-REVISE completion)

**Validation (all green):**
- ✅ Architecture captures all three modes (greenfield, feature, review-fix)
- ✅ Confidence-gated checkpoint protocol is a first-class architectural component (see §8 + domain model `GateDecision`)
- ✅ Specialist agent delegation via NATS `call_agent_tool()` clearly bounded — ADR-ARCH-015
- ✅ GuardKit command invocation is subprocess-based — ADR-ARCH-004, ADR-ARCH-020
- ✅ Degraded mode is documented structural capability — ARCHITECTURE.md §7 + refresh-doc §"Degraded Mode"
- ✅ Pipeline event publishing uses nats-core payloads only (no new types invented here; v0.2.0 ships them)
- ✅ State persistence uses **SQLite + JetStream** (not NATS KV per the earlier draft — ADR-ARCH-009 explicitly omits the LangGraph checkpointer; ADR-SP-013 makes SQLite authoritative; builds KV is intentionally unused as a state store)
- ◻ **ADR trailer** (`**Decision facts as of commit:** <sha>`) — **not yet applied** to the 31 ADRs. Low-priority backfill task; consider a follow-up commit after /feature-spec.

**Noted deviation from the expected shape:**
- Build plan predicted ADRs under `forge/docs/decisions/ADR-FORGE-001-*`. Actual location is `forge/docs/architecture/decisions/ADR-ARCH-*`. References updated throughout this document accordingly.

### Step 2: /system-design ✅ COMPLETE (2026-04-23)

Produced detailed design — API contracts per container, data models, DDRs, C4 L3 diagrams.

```bash
guardkit system-design \
  --context forge/docs/architecture/ARCHITECTURE.md \
  --context forge/docs/architecture/decisions/ADR-ARCH-001..031.md  # all 31 ADRs loaded
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md \
  --context forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md
```

**Actual outputs (on disk — commit `b40365c`):**

Nine API contracts ([`forge/docs/design/contracts/`](../../design/contracts/)):
- [`API-nats-pipeline-events.md`](../../design/contracts/API-nats-pipeline-events.md) — inbound `pipeline.build-queued.>` pull consumer + 8 outbound lifecycle subjects
- [`API-nats-agent-dispatch.md`](../../design/contracts/API-nats-agent-dispatch.md) — specialist dispatch with LES1 per-correlation reply subject
- [`API-nats-approval-protocol.md`](../../design/contracts/API-nats-approval-protocol.md) — `interrupt()` round-trip with rehydration contract
- [`API-nats-fleet-lifecycle.md`](../../design/contracts/API-nats-fleet-lifecycle.md) — Forge self-registration + KV watch
- [`API-cli.md`](../../design/contracts/API-cli.md) — `forge queue/status/history/cancel/skip`
--
  --context forge/docs/design/decisions/DDR-004-graphiti-group-partitioning.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End
# Design context: async-subagent stack (subagents API + both C4 L3 diagrams + DDR-006)
# plus the state machine (DM-build-lifecycle) and checkpoint protocol
# (API-nats-approval-protocol) that the mode threads through every stage.
# Prior-feature context: `.feature` + `_summary.md` for FEAT-FORGE-001..006
# (assumptions.yaml omitted — already echoed inside each summary).
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-build-lifecycle.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration.feature \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md \
  --context forge/features/nats-fleet-integration/nats-fleet-integration.feature \
  --context forge/features/nats-fleet-integration/nats-fleet-integration_summary.md \
  --context forge/features/specialist-agent-delegation/specialist-agent-delegation.feature \
  --context forge/features/specialist-agent-delegation/specialist-agent-delegation_summary.md \
  --context forge/features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol.feature \
  --context forge/features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine.feature \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine_summary.md \
  --context forge/features/infrastructure-coordination/infrastructure-coordination.feature \
  --context forge/features/infrastructure-coordination/infrastructure-coordination_summary.md

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix
guardkit feature-spec FEAT-FORGE-008 \
  --context forge/features/FEAT-FORGE-007/feature-spec.md \
  --context forge/docs/design/contracts/API-cli.md
```

**Validation per feature spec:**
- BDD scenarios cover happy path, error cases, and edge cases
- Acceptance groups are reviewable (Rich will likely accept defaults ~95% based on
  observed pattern, but the Forge is the capstone — expect more manual review here)
- Each feature spec references the nats-core payloads it uses (no invented types)
- FEAT-FORGE-007 integration spec covers the full greenfield flow end-to-end

**Record Rich's responses:** Create `feature-spec-FEAT-FORGE-XXX-history.md` for each
spec session (following Pattern 3 from the fleet-master-index).

### Step 4: /feature-plan × 8

Produces task breakdowns for each feature. Run sequentially — dependencies must be
respected. Each invocation takes the matching `/feature-spec` summary as `--context`
so the plan is grounded in the curated scenarios and resolved assumptions (see
`installer/core/commands/feature-plan.md` §"Step 11: Link BDD scenarios to tasks").

```bash
# Run in dependency order:

# FEAT-FORGE-001: Pipeline State Machine & Configuration (no deps)
guardkit feature-plan "Pipeline State Machine and Configuration" \
  --context forge/features/pipeline-state-machine-and-configuration/pipeline-state-machine-and-configuration_summary.md

# FEAT-FORGE-002: NATS Fleet Integration (depends on 001)  ✅ COMPLETE (commit 6a29ed3)
guardkit feature-plan "NATS Fleet Integration" \
  --context forge/features/nats-fleet-integration/nats-fleet-integration_summary.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine (depends on 001 — can parallel with 002)
guardkit feature-plan FEAT-FORGE-005 \
  --context forge/features/guardkit-command-invocation-engine/guardkit-command-invocation-engine_summary.md
Bash Find feature locations in build plan
IN
grep -n "FEAT-FORGE-00[1-7]" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | head -50

OUT
3:## Status: `/system-arch` ✅ done · `/system-design` ✅ done · `/feature-spec` 🟡 **5 / 8 complete** (FEAT-FORGE-001..005 ✅ · 3 remaining; later steps blocked on specialist-agent Phase 3 + NATS infra running)
15:| 3 | `/feature-spec × 8` | 🟡 in progress (5 / 8) | 2026-04-24 (FEAT-FORGE-001..005) | — | FEAT-FORGE-001..004 ✅ (see Step 3 for per-feature details); FEAT-FORGE-005 ✅ → `features/guardkit-command-invocation-engine/` (32 scenarios, 7 assumptions resolved — 4 high, 3 medium, 0 low) |
126:| FEAT-FORGE-001 | Pipeline State Machine & Configuration | — | 2-3 days | Core state machine (IDLE→PREPARING→RUNNING→FINALISING→COMPLETE/FAILED per anchor §6), project config loading (`forge.yaml`), crash recovery, sequential build queue | JetStream pull consumer, state machine, `forge.yaml` config, crash recovery |
127:| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads | Publish pipeline events, `fleet.register` (ADR-SP-014) |
128:| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles (ADR-SP-015), result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure | NATS command invocation of specialist agents (Stage 2) |
129:| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve, 🟡 flag for review (PAUSED state), 🔴 hard stop. Configurable thresholds per anchor §4 | Confidence-gated checkpoints with configurable thresholds |
130:| FEAT-FORGE-005 | GuardKit Command Invocation Engine | 001 | 2-3 days | Subprocess calls to `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`. Context flag construction from pipeline state + `.guardkit/context-manifest.yaml`. Output capture and artifact path tracking. Error handling and retry. | Subprocess invocation of GuardKit AutoBuild with `--nats` flag (Stage 4) |
131:| FEAT-FORGE-006 | Infrastructure Coordination | 001, 002 | 2-3 days | Graphiti seeding after each pipeline stage, Graphiti querying for cross-project context, test verification, git operations (clone/pull/branch/push/PR via `gh`) | Git operations, SQLite build history and stage log |
132:| FEAT-FORGE-007 | Mode A Greenfield End-to-End | 003, 004, 005, 006 | 3-5 days | Full integration: raw input → delegate to PO agent → checkpoint → delegate to architect → checkpoint → /system-arch → /system-design → /feature-spec × N → /feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint (PR review). The primary pipeline mode. | Full end-to-end pipeline validation |
136:- CLI commands: `forge queue`, `forge status`, `forge history`, `forge cancel`, `forge skip` — folded into FEAT-FORGE-001 (CLI entrypoint)
137:- SQLite build history schema — folded into FEAT-FORGE-006 (infrastructure coordination)
249:- ◻ **Sequence diagrams for Mode A greenfield** — not produced. Content is implicit across the contract set (pipeline events + approval protocol + dispatch + subagents) but a single end-to-end sequence diagram would be useful. Consider an optional follow-up artefact before `/feature-spec FEAT-FORGE-007`.
266:- ✅ **FEAT-FORGE-001** — Pipeline State Machine & Configuration (2026-04-24; revised 2026-04-24)
271:- ✅ **FEAT-FORGE-002** — NATS Fleet Integration (2026-04-24)
275:- ✅ **FEAT-FORGE-003** — Specialist Agent Delegation (2026-04-24)
280:- ✅ **FEAT-FORGE-004** — Confidence-Gated Checkpoint Protocol (2026-04-24)
286:- ✅ **FEAT-FORGE-005** — GuardKit Command Invocation Engine (2026-04-24)
294:- ✅ **FEAT-FORGE-006** — Infrastructure Coordination (2026-04-24)
314:# FEAT-FORGE-001: Pipeline State Machine & Configuration  ✅ COMPLETE (2026-04-24)
316:guardkit feature-spec FEAT-FORGE-001 \
322:# FEAT-FORGE-002: NATS Fleet Integration  ✅ COMPLETE (2026-04-24)
324:guardkit feature-spec FEAT-FORGE-002 \
331:# FEAT-FORGE-003: Specialist Agent Delegation  ✅ COMPLETE (2026-04-24)
333:guardkit feature-spec FEAT-FORGE-003 \
339:# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol  ✅ COMPLETE (2026-04-24)
341:guardkit feature-spec FEAT-FORGE-004 \
346:# FEAT-FORGE-005: GuardKit Command Invocation Engine  ✅ COMPLETE (2026-04-24)
348:guardkit feature-spec FEAT-FORGE-005 \
353:# FEAT-FORGE-006: Infrastructure Coordination  ✅ COMPLETE (2026-04-24)
355:guardkit feature-spec FEAT-FORGE-006 \
361:# FEAT-FORGE-007: Mode A Greenfield End-to-End
365:# Prior-feature context: `.feature` + `_summary.md` for FEAT-FORGE-001..006
367:guardkit feature-spec FEAT-FORGE-007 \
389:  --context forge/features/FEAT-FORGE-007/feature-spec.md \
398:- FEAT-FORGE-007 integration spec covers the full greenfield flow end-to-end
413:# FEAT-FORGE-001: Pipeline State Machine & Configuration (no deps)
417:# FEAT-FORGE-002: NATS Fleet Integration (depends on 001)  ✅ COMPLETE (commit 6a29ed3)
421:# FEAT-FORGE-005: GuardKit Command Invocation Engine (depends on 001 — can parallel with 002)
422:guardkit feature-plan FEAT-FORGE-005 \
425:# FEAT-FORGE-003: Specialist Agent Delegation (depends on 002)
429:# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol (depends on 003)
433:# FEAT-FORGE-006: Infrastructure Coordination (depends on 001, 002)
437:# FEAT-FORGE-007: Mode A Greenfield End-to-End (depends on 003, 004, 005, 006)
438:guardkit feature-plan FEAT-FORGE-007 \
447:> feature **name**, not the `FEAT-FORGE-NNN` ID. The FEAT-FORGE-001 slug resolved
455:- Integration tasks (FEAT-FORGE-007) are in later waves
463:guardkit autobuild FEAT-FORGE-001
464:guardkit autobuild FEAT-FORGE-005
467:guardkit autobuild FEAT-FORGE-002
470:guardkit autobuild FEAT-FORGE-003
Now I have the FEAT-FORGE-007 description: "Mode A Greenfield End-to-End" — full integration of raw input → PO delegation → checkpoint → architect delegation → checkpoint → /system-arch → /system-design → /feature-spec × N → /feature-plan × N → autobuild × N → verify → git/PR. Let me read the context files.

Bash List design context files
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/diagrams/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/ 2>/dev/null

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

/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/diagrams/:
agent-runtime.md
domain-core.md

/home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/:
DM-build-lifecycle.md
DM-calibration.md
DM-discovery.md
DM-gating.md
DM-graphiti-entities.md
Bash List feature subdirectory contents
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/*/

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/features/confidence-gated-checkpoint-protocol/:
confidence-gated-checkpoint-protocol_assumptions.yaml
confidence-gated-checkpoint-protocol.feature
confidence-gated-checkpoint-protocol_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/guardkit-command-invocation-engine/:
guardkit-command-invocation-engine_assumptions.yaml
guardkit-command-invocation-engine.feature
guardkit-command-invocation-engine_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/infrastructure-coordination/:
infrastructure-coordination_assumptions.yaml
infrastructure-coordination.feature
infrastructure-coordination_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/:
nats-fleet-integration_assumptions.yaml
nats-fleet-integration.feature
nats-fleet-integration_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/:
pipeline-state-machine-and-configuration_assumptions.yaml
pipeline-state-machine-and-configuration.feature
pipeline-state-machine-and-configuration_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/specialist-agent-delegation/:
specialist-agent-delegation_assumptions.yaml
specialist-agent-delegation.feature
specialist-agent-delegation_summary.md
Update Todos

Phase 1: Gather context (read all 18 --context files)

Phase 2: Generate initial proposal (grouped scenarios)

Phase 3: Curate (await user A/R/M/+/? per group)

Phase 4: Edge case expansion (offer Y/S/N)

Phase 5: Resolve assumptions with user

Phase 6: Write .feature, _assumptions.yaml, _summary.md
Read API-subagents.md
Read API-nats-approval-protocol.md
Read DM-build-lifecycle.md
Read agent-runtime.md
Read domain-core.md
Read DDR-006-async-subagent-state-channel-contract.md
Read pipeline-state-machine-and-configuration_summary.md
Read specialist-agent-delegation_summary.md
Read confidence-gated-checkpoint-protocol_summary.md
Read guardkit-command-invocation-engine_summary.md
Read infrastructure-coordination_summary.md
Read nats-fleet-integration_summary.md
Context is loaded. Let me also peek at one prior history to understand Rich's curation style, then move to Phase 2.

Bash Check prior history for review style
IN
head -100 /home/richardwoollcott/Projects/appmilla_github/forge/docs/history/feature-spec-FEAT-FORGE-006-history.md 2>/dev/null

OUT
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
Context loaded: stack=python, 16 design artefacts read (3 contracts, 1 model, 2 diagrams, 1 DDR, 6 prior summaries + 6 prior .feature implied), 0 existing .feature for FEAT-FORGE-007.

Now generating Phase 2 proposal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Mode A Greenfield End-to-End
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@feat-forge-007 @mode-a-greenfield
Feature: Mode A Greenfield End-to-End
  As Rich, the operator triggering a greenfield build
  I want Forge to drive a raw product idea through delegation, architecture,
    per-feature specification, planning, autobuild, and pull-request creation
    in a single supervised run
  So that I can land a reviewed pull request from a one-line brief without
    hand-stitching the intermediate stages

  Background:
    Given Forge is registered on the fleet
    And the live discovery cache lists a healthy product-owner specialist and a healthy architect specialist
    And the operator's calibration history has been ingested
    And a project repository is configured with a writable worktree allowlist
    And a build has been queued for a feature identifier in greenfield mode
━━ GROUP A: Key Examples (8 scenarios) ━━


  # Why: The capstone happy path — one greenfield brief produces one reviewed pull request
  @key-example @smoke
  Scenario: A greenfield brief drives a full pipeline run to a pull request awaiting review
    Given the build is picked up from the queue
    When Forge delegates to the product-owner specialist and the result is auto-approved
    And Forge delegates to the architect specialist and the result is auto-approved
    And Forge invokes architecture, design, per-feature specification, per-feature planning, and autobuild in order
    And every gated stage along the way is auto-approved
    And the pull request is created on the working branch
    Then the build should pause at pull-request review for mandatory human approval
    And the build status should remain non-terminal until a human responds
    And the recorded stage history should contain the full chain in order
    # [ASSUMPTION: confidence=high] Eight stage classes drive Mode A: product-owner → architect → /system-arch → /system-design → /feature-spec → /feature-plan → autobuild → pull-request review

  # Why: Stage outputs must feed forward — the product-owner's charter is consumed by the architect
  @key-example
  Scenario: The product-owner output is supplied as input to the architect delegation
    Given the product-owner specialist has returned an approved charter
    When Forge delegates to the architect specialist
    Then the architect dispatch context should include the approved charter
    And the architect should not be invoked before the product-owner stage is recorded as approved

  # Why: Architecture artefacts must feed /system-design — directional dependency between subprocess stages
  @key-example
  Scenario: Architecture outputs are supplied as context for system design
    Given the architecture stage has produced architecture artefacts
    When Forge invokes the system-design subcommand
    Then the system-design invocation should be supplied with the architecture artefact paths as context
    And system-design should not be invoked before the architecture stage is recorded as approved

  # Why: System design partitions the work into one or more features that drive the spec/plan/autobuild loop
  @key-example
  Scenario: System design produces a feature catalogue that drives per-feature iterations
    Given the system-design stage has produced a feature catalogue
    When Forge enters the per-feature loop
    Then one /feature-spec dispatch should be issued for each feature in the catalogue
    And one /feature-plan dispatch should be issued for each feature after its spec is approved
    And one autobuild dispatch should be issued for each feature after its plan is approved

  # Why: Long-running per-feature autobuild runs through an async subagent so the supervisor stays responsive
  @key-example @smoke
  Scenario: Autobuild runs as an asynchronous subagent so the supervisor remains responsive during long runs
    Given a feature has an approved build plan
    When Forge dispatches autobuild for that feature
    Then the dispatch should be a long-running asynchronous task with its own task identifier
    And the live status view should report wave and task progress for the running autobuild
    And the supervisor should remain available to answer status queries while the autobuild is in flight
    # [ASSUMPTION: confidence=high] Autobuild dispatch uses the AsyncSubAgent pattern with the async_tasks state channel exposing wave and task indices

  # Why: A pause at any gated stage suspends the build without losing per-stage outputs
  @key-example
  Scenario: A flagged-for-review checkpoint pauses the build and the next stage waits on the response
    Given Forge is in the per-feature loop and a stage has been flagged for review
    When the operator responds with approve
    Then the build should resume from the next stage in the chain
    And the prior approved stage outputs should still be available as context
    And no later stage should have been started before the response was received

  # Why: PR creation must always defer to a human — constitutional rule (belt-and-braces)
  @key-example @regression
  Scenario: The pull-request review stage is mandatory human approval regardless of upstream scores
    Given every preceding stage of the build has been auto-approved with high Coach scores
    When Forge reaches the pull-request review stage
    Then the build should pause for mandatory human approval
    And the pause should not be eligible to be auto-approved
    # [ASSUMPTION: confidence=high] Pull-request review and pull-request creation tool names are constitutionally pinned to mandatory human approval

  # Why: Successful build records a session outcome with the full chain of gate decisions
  @key-example
  Scenario: Completing the pull-request review writes a session outcome that links every gate decision in order
    Given a build is paused at pull-request review
    When the operator approves the pull request
    Then the build should reach a complete terminal state
    And the recorded session outcome should reference every gate decision from product-owner through pull-request review
    And the recorded gate decisions should be linked in chronological order
━━ GROUP B: Boundary Conditions (6 scenarios) ━━


  # Why: A single-feature catalogue is the smallest valid scope — exercises one full inner loop
  @boundary @smoke
  Scenario: A greenfield build with a single feature in the catalogue completes one inner loop
    Given the system-design stage has produced exactly one feature
    When the build progresses through the per-feature loop
    Then exactly one /feature-spec dispatch and one /feature-plan dispatch and one autobuild dispatch should be recorded
    And the chain should culminate in a single pull-request review pause

  # Why: Multiple-feature catalogues exercise the iteration boundary — each feature must run its own inner loop
  @boundary
  Scenario Outline: Multi-feature catalogues run one inner loop per feature
    Given the system-design stage has produced <count> features in the catalogue
    When the build progresses through the per-feature loop
    Then <count> /feature-spec dispatches should be recorded
    And <count> /feature-plan dispatches should be recorded
    And <count> autobuild dispatches should be recorded
    Examples:
      | count |
      | 2     |
      | 5     |

  # Why: Just-outside boundary — a system-design stage that produces no features cannot proceed
  @boundary @negative
  Scenario: A system-design stage that produces zero features cannot enter the per-feature loop
    Given the system-design stage has produced no features
    When the per-feature loop is reached
    Then the build should not issue any /feature-spec dispatches
    And the build should be flagged for review with the missing-catalogue rationale recorded

  # Why: Stage-ordering invariant — no stage dispatches before its prerequisite chain is approved
  @boundary
  Scenario Outline: A downstream stage is not dispatched before its prerequisite has reached the approved state
    Given the prerequisite stage has not yet been approved
    When the build's reasoning loop considers the next dispatch
    Then no dispatch should be issued for <stage>
    Examples:
      | stage             | prerequisite                 |
      | architect         | product-owner                |
      | system-arch       | architect                    |
      | system-design     | system-arch                  |
      | feature-spec      | system-design                |
      | feature-plan      | feature-spec for that feature|
      | autobuild         | feature-plan for that feature|
      | pull-request      | autobuild for every feature  |

  # Why: Per-feature autobuild boundary — minimal one-wave plan exercises the smallest valid plan shape
  @boundary
  Scenario: Autobuild executing a single-wave plan completes one wave before reporting complete
    Given a feature's approved build plan contains exactly one wave with one task
    When autobuild is dispatched for that feature
    Then the live status view should report progress for one wave and one task
    And on success the autobuild lifecycle should reach the completed state with one task completed

  # Why: Async-subagent ETA boundary — supervisor can poll status without blocking
  @boundary
  Scenario: While autobuild is running, the supervisor can answer concurrent status queries
    Given autobuild is dispatched and reports the running-wave lifecycle
    When the operator requests live status during the wave
    Then the supervisor should respond with the current wave and task indices
    And the autobuild itself should remain in the running-wave lifecycle
━━ GROUP C: Negative Cases (8 scenarios) ━━


  # Why: A hard-stop at the product-owner stage terminates the build — no later dispatches should occur
  @negative
  Scenario: A hard-stop at the product-owner stage prevents any later stage from being dispatched
    Given the product-owner specialist returns a result that causes a hard-stop gate
    When the build evaluates the product-owner result
    Then the build should reach a failed terminal state
    And no architect, architecture, design, specification, planning, or autobuild dispatch should have been recorded

  # Why: Specialist degraded mode forces flag-for-review even when scoring is unavailable
  @negative
  Scenario: When no product-owner specialist is reachable the build is flagged for review at that stage
    Given the live discovery cache reports no healthy product-owner specialist
    When the build reaches the product-owner stage
    Then the build should be paused for review
    And the pause rationale should record degraded specialist availability
    And no later stage should have been dispatched

  # Why: A failed inner-loop /feature-spec dispatch must not cascade into a planning attempt
  @negative
  Scenario: A failed /feature-spec dispatch for a feature halts that feature's inner loop
    Given a feature is being processed in the per-feature loop
    When the /feature-spec dispatch for that feature returns a failed result
    Then the build should not issue a /feature-plan dispatch for that feature
    And the build should not issue an autobuild dispatch for that feature
    And the failed-spec rationale should be recorded against the build

  # Why: Constitutional override — pull-request review can never be auto-approved by upstream score alone
  @negative @regression
  Scenario: Auto-approval is refused at the pull-request review stage even when the upstream Coach score is at the maximum
    Given the upstream stages have all returned the maximum Coach score
    When the build reaches the pull-request review stage
    Then the build should pause for mandatory human approval
    And the pause should not be eligible to resolve without a human decision

  # Why: Skip steering must not bypass constitutional gates
  @negative @regression
  Scenario: A skip directive issued against the pull-request review stage is refused
    Given the build is paused at pull-request review
    When the operator issues a skip directive for that stage
    Then the build should remain paused for mandatory human approval
    And the skip should be recorded as refused with a constitutional rationale

  # Why: An autobuild internal hard-stop must not be silently absorbed — it surfaces to the build
  @negative
  Scenario: An autobuild internal hard-stop is propagated to the build and prevents pull-request creation
    Given autobuild is in flight for a feature
    When an internal task hits a hard-stop gate
    Then the autobuild lifecycle should reach a failed terminal state for that feature
    And no pull-request creation dispatch should be issued for that feature
    And the build's stage history should record the autobuild failure with the hard-stop rationale

  # Why: Pull-request creation failure must mark the build failed even if every prior stage was approved
  @negative
  Scenario: A pull-request creation failure marks the build failed with no pull-request URL
    Given every prior stage has been approved
    When the pull-request creation stage fails
    Then the build should reach a failed terminal state
    And no pull-request URL should be recorded against the build

  # Why: Reject decision at any pause is a terminal failure path, not a retry path
  @negative
  Scenario: A reject decision at a flagged-for-review checkpoint terminates the build
    Given the build is paused at any flagged-for-review checkpoint before pull-request review
    When the operator responds with reject
    Then the build should reach a failed terminal state
    And no later stage should be dispatched
━━ GROUP D: Edge Cases (10 scenarios) ━━


  # Why: Crash mid-stage triggers anchor §5 retry-from-scratch — the build re-enters preparing
  @edge-case
  Scenario Outline: A crash during any non-terminal stage is recovered by retry-from-scratch
    Given the build is in the <stage> stage
    When the runtime restarts after an unexpected interruption
    Then the build should re-enter the preparing state
    And the prior in-flight stage should be reattempted from the start
    Examples:
      | stage                  |
      | product-owner          |
      | architect              |
      | architecture           |
      | system-design          |
      | feature-spec           |
      | feature-plan           |
      | autobuild              |

  # Why: Async subagent state channel is advisory on recovery — SQLite is authoritative
  @edge-case
  Scenario: After a crash mid-autobuild the build's authoritative status comes from durable history not the live state channel
    Given autobuild was running and the runtime crashes
    When the runtime restarts
    Then the build's authoritative status should be read from the durable history
    And any live state channel data should be treated as advisory
    # [ASSUMPTION: confidence=high] On crash recovery, durable history (SQLite) is authoritative; live async-subagent state channel is advisory

  # Why: Mid-flight steering — the operator can inject a directive to a running autobuild
  @edge-case
  Scenario: A directive injected into a running autobuild is delivered as a pending instruction without halting the wave
    Given autobuild is in the running-wave lifecycle for a feature
    When the operator injects a steering directive for that autobuild
    Then the directive should appear as pending against that autobuild's live state
    And the autobuild should remain in the running-wave lifecycle until it next checks for directives

  # Why: Cancel during a paused stage produces a synthetic reject and terminates the build
  @edge-case
  Scenario: A cancel directive while paused at a checkpoint terminates the build with a synthetic reject
    Given the build is paused at a flagged-for-review checkpoint
    When the operator issues a cancel directive
    Then the pause should resolve as a synthetic reject with a cancel rationale
    And the build should reach a cancelled terminal state

  # Why: Cancel during autobuild reaches into the async subagent
  @edge-case
  Scenario: A cancel directive during a running autobuild halts the asynchronous task and terminates the build
    Given autobuild is in flight for a feature
    When the operator issues a cancel directive for the build
    Then the autobuild's live state should reach the cancelled lifecycle
    And the build should reach a cancelled terminal state with no pull-request creation attempted

  # Why: Skip on a non-constitutional stage records the skip and continues the chain
  @edge-case
  Scenario: A skip directive on a non-constitutional stage marks that stage skipped and resumes the chain
    Given the build is paused at a flagged-for-review checkpoint that is not pull-request review
    When the operator issues a skip directive
    Then the stage should be recorded as skipped on the build's stage history
    And the build should resume at the next stage in the chain

  # Why: Approval responses are routed by build identifier — concurrent paused builds remain isolated
  @edge-case
  Scenario: An approval response is routed to the build whose identifier matches the response channel
    Given two builds are simultaneously paused at flagged-for-review checkpoints
    When an approval response is received that matches one build's identifier
    Then only that build should resume
    And the other paused build should remain awaiting its own approval

  # Why: Async subagent's interrupt does not block the supervisor — pauses are visible via status only
  @edge-case
  Scenario: An internal autobuild pause is observable through the supervisor without blocking other supervisor work
    Given an autobuild internal task fires a flagged-for-review pause
    When the operator queries live status
    Then the autobuild's live state should report awaiting-approval with the stage label
    And the supervisor should remain free to perform other work for other builds

  # Why: Per-feature parallelism boundary — supervisor sequences inner loops to avoid resource clash
  @edge-case
  Scenario: Per-feature inner loops are sequenced so each feature's autobuild completes before the next feature's autobuild begins
    Given the system-design stage has produced two or more features
    When the build progresses through the per-feature loop
    Then no second autobuild dispatch should begin while a first autobuild is still in a non-terminal lifecycle
    # [ASSUMPTION: confidence=medium] Per-feature autobuild runs are sequenced within a single build to avoid worktree contention; inter-feature parallelism is out of scope for Mode A v1

  # Why: A repeat approval for the same request is idempotent — first response wins
  @edge-case
  Scenario: A duplicate approval response for the same paused stage is ignored
    Given the build has resumed after an approval response was honoured
    When a duplicate response with the same request identifier arrives
    Then the build should not re-resume
    And no additional stage transition should be recorded for the duplicate
━━ GROUP E: Security (3 scenarios) ━━


  # Why: Constitutional pull-request rule is enforced at both the prompt layer and the executor layer
  @security @regression
  Scenario: Pull-request review enforcement holds even if a misconfigured prompt would have allowed auto-approve
    Given the system prompt is configured incorrectly so that pull-request review appears auto-approvable
    When the build reaches the pull-request review stage
    Then the executor layer should still enforce mandatory human approval
    # [ASSUMPTION: confidence=high] Constitutional pull-request review is enforced both in the prompt and by an executor-side hardcoded branch (belt-and-braces)

  # Why: Specialist results cannot escalate beyond their delegated authority
  @security
  Scenario: A specialist result that asserts an override of constitutional rules is ignored at gating
    Given a specialist returns a result claiming to override the pull-request review rule
    When the build reaches the pull-request review stage
    Then the override claim should be ignored
    And the build should pause for mandatory human approval

  # Why: Each build runs only inside its ephemeral worktree allowlist — no cross-build leakage
  @security
  Scenario: Subprocess stages run only against the build's ephemeral worktree path
    Given the build has a configured worktree path
    When any subprocess stage is dispatched
    Then the working directory used by the subprocess should fall under the build's worktree allowlist
    And no path outside that allowlist should be writable by the dispatched subprocess
━━ GROUP F: Concurrency (2 scenarios) ━━


  # Why: Two builds run concurrently with isolated approval channels and isolated autobuild task identifiers
  @concurrency
  Scenario: Two concurrent greenfield builds use separate approval channels and separate autobuild task identifiers
    Given two distinct builds are dispatched at approximately the same time
    When both builds reach autobuild and both later reach a flagged-for-review pause
    Then each build should have a distinct autobuild task identifier
    And each build's approval pause should resolve only on a response matching its own build identifier

  # Why: A long-running autobuild does not block the supervisor's reasoning loop for unrelated builds
  @concurrency
  Scenario: The supervisor can dispatch a different build's stage while another build's autobuild is mid-flight
    Given a first build's autobuild is in the running-wave lifecycle
    When a second build is queued and picked up
    Then the second build's product-owner stage should be dispatched without waiting for the first build's autobuild to complete
━━ GROUP G: Data Integrity (3 scenarios) ━━


  # Why: Stage-history ordering invariant — the chain reflects the actual order of approvals
  @data-integrity
  Scenario: The recorded stage history of a successful build follows the canonical Mode A ordering
    Given a build has reached the complete terminal state
    When the operator inspects the build's stage history
    Then the stage entries should appear in the order product-owner, architect, architecture, design, then per-feature spec, plan, and autobuild, then pull-request review

  # Why: Each per-feature loop's artefacts are attributed to the feature that produced them
  @data-integrity
  Scenario: Per-feature artefact paths are recorded against the feature that produced them
    Given the system-design stage has produced two or more features
    When per-feature stages complete for each feature
    Then each per-feature stage entry should record the artefact paths produced for its feature only
    And no artefact path should be attributed to more than one feature

  # Why: Even when a stage's outbound notification publish fails, the build's durable transition still holds
  @data-integrity
  Scenario: A notification publish failure does not regress the build's recorded stage progress
    Given a stage has been approved
    When the outbound notification publish for that approval fails
    Then the stage should still be recorded as approved on the build's history
    And the next stage's prerequisite should still evaluate as satisfied
━━ GROUP H: Integration Boundaries (3 scenarios) ━━


  # Why: End-to-end smoke — minimal greenfield input produces a paused-for-review pull request
  @integration @smoke
  Scenario: A minimal greenfield brief drives a single-feature run to a pull request awaiting human review
    Given the operator queues a build with a one-line product brief
    And the system-design stage will produce a single-feature catalogue
    When the build proceeds through every Mode A stage with auto-approval at every flagged-for-review checkpoint
    Then the terminal state should be paused at pull-request review awaiting human approval
    And a pull-request URL should be recorded against the build

  # Why: Degraded mode (no specialist available) at any specialist stage forces flag-for-review there
  @integration
  Scenario: A greenfield build with no available specialists is flagged for review at every specialist stage
    Given no product-owner or architect specialist is reachable on the fleet
    When a greenfield build is queued and picked up
    Then the build should be flagged for review at the product-owner stage with a degraded rationale
    And no architect dispatch should occur until the operator responds

  # Why: A multi-feature catalogue produces a series of completed inner loops culminating in pull-request review
  @integration
  Scenario: A multi-feature greenfield run produces one autobuild and one pull-request review pause per feature
    Given the system-design stage will produce two or more features
    When the build proceeds through every Mode A stage with auto-approval at every flagged-for-review checkpoint
    Then for each feature there should be one /feature-spec, one /feature-plan, and one autobuild stage entry
    # [ASSUMPTION: confidence=medium] One pull request per feature in the catalogue (not a single combined pull request); pull-request review pauses occur per feature
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 43 scenarios across 8 groups
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

Fast path: type A A A A A A A A to accept all eight groups at once.

GROUP A — Key Examples (8 scenarios): _
GROUP B — Boundary Conditions (6 scenarios): _
GROUP C — Negative Cases (8 scenarios): _
GROUP D — Edge Cases (10 scenarios): _
GROUP E — Security (3 scenarios): _
GROUP F — Concurrency (2 scenarios): _
GROUP G — Data Integrity (3 scenarios): _
GROUP H — Integration Boundaries (3 scenarios): _
Awaiting your decisions per group.

command-history.md
A A A A A A A A
All eight groups accepted.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I've identified 4 additional scenarios specific to the integration / capstone level (most security/concurrency concerns are already established in FEAT-FORGE-001..006, so these are tightly scoped to behaviour that only matters end-to-end):

Concurrency (1):

Two operators race on the same paused build — first response wins, second is idempotent
Data integrity (2):

Build correlation identifier threads through every published lifecycle event from queue to terminal
The calibration priors snapshot captured at build start remains stable for the whole run, even if the operator's history changes mid-run
Integration (1):

Long-term-memory seeding failure for one stage does not regress that stage's approved status
Include them?

[Y] Yes — show and curate them
[S] Sample — show 2 examples then decide
[N] No — skip and proceed to Phase 5
Your choice [Y/S/N]:

Y
━━ EXPANSION GROUP I: Additional integration-level scenarios (4 scenarios) ━━


  # Why: First-write-wins discipline survives at the integration level even when two operators respond simultaneously
  @concurrency @edge-case
  Scenario: Two simultaneous approval responses for the same paused build resolve as first-wins
    Given the build is paused at a flagged-for-review checkpoint
    When two approval responses arrive simultaneously for the same paused stage with different decisions
    Then the build should resolve under exactly one of those decisions
    And no second resume should be applied for the duplicate response

  # Why: Correlation threading is the audit anchor — every lifecycle event for one build must carry the same correlation identifier
  @data-integrity
  Scenario: Every published lifecycle event for a build threads the same correlation identifier from queue to terminal
    Given a build has been queued with a correlation identifier
    When the build progresses through every Mode A stage to a terminal state
    Then every lifecycle event published for that build should carry that same correlation identifier

  # Why: Calibration snapshot stability — a long-running build sees a consistent priors view, not a moving target
  @data-integrity
  Scenario: Calibration priors captured at build start remain the snapshot used by every stage of that build
    Given a build is picked up and a calibration-priors snapshot is captured at start
    When the operator's calibration history is updated while the build is mid-run
    Then later stages of the in-flight build should still use the priors snapshot captured at start
    # [ASSUMPTION: confidence=medium] Calibration priors are snapshotted at build start and remain stable for the duration of that build; mid-run history changes apply only to subsequent builds

  # Why: Long-term memory seeding is best-effort and must never regress an authoritative approval
  @integration @data-integrity
  Scenario: A long-term-memory seeding failure for one stage does not regress that stage's recorded approval
    Given a stage has been approved by gating
    When the long-term-memory seeding for that stage's gate decision fails
    Then the stage should still be recorded as approved on the build's history
    And the next stage's prerequisite should still evaluate as satisfied
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Expansion group
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP I — Expansion (4 scenarios): _
[A]ccept / [R]eject / [M]odify / [+]Add / [?]Defer

A
Expansion accepted. Moving to Phase 5.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (8 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter (or reply "accept") to take the default, or type the actual value / override.

[1] Stage chain composition for Mode A

Proposed: Eight stage classes — product-owner → architect → /system-arch → /system-design → /feature-spec → /feature-plan → autobuild → pull-request review
Confidence: high
Basis: forge-build-plan.md FEAT-FORGE-007 description verbatim
Accept or override: _
[2] Autobuild dispatch pattern

Proposed: Long-running asynchronous subagent with own task identifier and live wave/task progress in the async_tasks state channel
Confidence: high
Basis: API-subagents.md §3 + DDR-006
Accept or override: _
[3] Pull-request stage constitutional pin

Proposed: The "pull-request review" and "pull-request creation" tool names are constitutionally pinned to mandatory human approval regardless of upstream Coach score
Confidence: high
Basis: API-nats-approval-protocol.md §8 + ADR-ARCH-026
Accept or override: _
[4] Crash-recovery authority

Proposed: On restart, durable history is authoritative for build status; live async-subagent state channel is advisory only
Confidence: high
Basis: DDR-006 "Crash-recovery linkage" + DM-build-lifecycle §5 invariants
Accept or override: _
[5] Constitutional belt-and-braces enforcement layers

Proposed: Pull-request review enforcement is wired both at the system-prompt layer and at the executor (gating) layer; loss of either is a constitutional regression
Confidence: high
Basis: API-nats-approval-protocol.md §8 explicit
Accept or override: _
[6] Per-feature autobuild sequencing within a single build

Proposed: Within a single build, per-feature autobuild runs are sequenced (one autobuild for one feature must reach a terminal lifecycle before the next feature's autobuild begins) — inter-feature parallelism is out of scope for Mode A v1
Confidence: medium
Basis: Build has a single worktree path (DM-build-lifecycle Build.worktree_path); inter-feature parallelism inside one worktree would cause branch contention. Not explicitly stated as a sequencing rule in any context file.
Accept or override: _
[7] Calibration-priors snapshot stability across a build

Proposed: Calibration priors retrieved at build start are the snapshot used by every stage of that build; mid-run changes to the operator's history apply only to subsequent builds
Confidence: medium
Basis: agent-runtime.md describes prompts as "Assembled at build start"; snapshot-stability is implied but not stated as an invariant.
Accept or override: _
[8] Pull-request granularity for multi-feature catalogues

Proposed: Each feature in the catalogue produces its own pull request; the pull-request review pause occurs per feature (not a single combined pull request for the build)
Confidence: medium
Basis: Build plan §10 reads "/feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint" — the "× N" attaches to autobuild but the git/PR step's granularity is not pinned. Per-feature PR is the most natural interpretation given per-feature artefacts and per-feature gate pauses.
Accept or override: _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Awaiting your responses (you can answer all at once, e.g. "accept all" / "1 accept, 8 override: one PR per build").

accept all
All 8 accepted at defaults. Writing Phase 6 output files.

Update Todos

Phase 1: Gather context (read all 18 --context files)

Phase 2: Generate initial proposal (grouped scenarios)

Phase 3: Curate (await user A/R/M/+/? per group)

Phase 4: Edge case expansion (offer Y/S/N)

Phase 5: Resolve assumptions with user

Phase 6: Write .feature, _assumptions.yaml, _summary.md
Write mode-a-greenfield-end-to-end.feature
474 lines
# Generated by /feature-spec
# Feature: Mode A Greenfield End-to-End (FEAT-FORGE-007)
# Stack: python
# Assumptions: 8 (see mode-a-greenfield-end-to-end_assumptions.yaml)
# Generated: 2026-04-25T00:00:00Z

@feat-forge-007 @mode-a-greenfield
Feature: Mode A Greenfield End-to-End
  As Rich, the operator triggering a greenfield build
  I want Forge to drive a raw product idea through delegation, architecture,
    per-feature specification, planning, autobuild, and pull-request creation
    in a single supervised run
  So that I can land a reviewed pull request from a one-line brief without
    hand-stitching the intermediate stages

  Background:
    Given Forge is registered on the fleet
    And the live discovery cache lists a healthy product-owner specialist and a healthy architect specialist
    And the operator's calibration history has been ingested
    And a project repository is configured with a writable worktree allowlist
    And a build has been queued for a feature identifier in greenfield mode

  # ===========================================================================
  # GROUP A — Key Examples
  # ===========================================================================

  # Why: The capstone happy path — one greenfield brief produces one reviewed pull request
  # [ASSUMPTION: confidence=high] Eight stage classes drive Mode A: product-owner → architect → /system-arch → /system-design → /feature-spec → /feature-plan → autobuild → pull-request review
  @key-example @smoke
  Scenario: A greenfield brief drives a full pipeline run to a pull request awaiting review
    Given the build is picked up from the queue
    When Forge delegates to the product-owner specialist and the result is auto-approved
    And Forge delegates to the architect specialist and the result is auto-approved
    And Forge invokes architecture, design, per-feature specification, per-feature planning, and autobuild in order
    And every gated stage along the way is auto-approved
    And the pull request is created on the working branch
    Then the build should pause at pull-request review for mandatory human approval
    And the build status should remain non-terminal until a human responds
    And the recorded stage history should contain the full chain in order

  # Why: Stage outputs must feed forward — the product-owner's charter is consumed by the architect
  @key-example
  Scenario: The product-owner output is supplied as input to the architect delegation
    Given the product-owner specialist has returned an approved charter
    When Forge delegates to the architect specialist
    Then the architect dispatch context should include the approved charter
    And the architect should not be invoked before the product-owner stage is recorded as approved

  # Why: Architecture artefacts must feed /system-design — directional dependency between subprocess stages
  @key-example
  Scenario: Architecture outputs are supplied as context for system design
    Given the architecture stage has produced architecture artefacts
    When Forge invokes the system-design subcommand
    Then the system-design invocation should be supplied with the architecture artefact paths as context
    And system-design should not be invoked before the architecture stage is recorded as approved

  # Why: System design partitions the work into one or more features that drive the spec/plan/autobuild loop
  @key-example
  Scenario: System design produces a feature catalogue that drives per-feature iterations
    Given the system-design stage has produced a feature catalogue
    When Forge enters the per-feature loop
    Then one /feature-spec dispatch should be issued for each feature in the catalogue
    And one /feature-plan dispatch should be issued for each feature after its spec is approved
    And one autobuild dispatch should be issued for each feature after its plan is approved

  # Why: Long-running per-feature autobuild runs through an async subagent so the supervisor stays responsive
  # [ASSUMPTION: confidence=high] Autobuild dispatch uses the AsyncSubAgent pattern with the async_tasks state channel exposing wave and task indices
  @key-example @smoke
  Scenario: Autobuild runs as an asynchronous subagent so the supervisor remains responsive during long runs
    Given a feature has an approved build plan
    When Forge dispatches autobuild for that feature
    Then the dispatch should be a long-running asynchronous task with its own task identifier
    And the live status view should report wave and task progress for the running autobuild
    And the supervisor should remain available to answer status queries while the autobuild is in flight

  # Why: A pause at any gated stage suspends the build without losing per-stage outputs
  @key-example
  Scenario: A flagged-for-review checkpoint pauses the build and the next stage waits on the response
    Given Forge is in the per-feature loop and a stage has been flagged for review
    When the operator responds with approve
    Then the build should resume from the next stage in the chain
    And the prior approved stage outputs should still be available as context
    And no later stage should have been started before the response was received

  # Why: PR creation must always defer to a human — constitutional rule (belt-and-braces)
  # [ASSUMPTION: confidence=high] Pull-request review and pull-request creation tool names are constitutionally pinned to mandatory human approval
  @key-example @regression
  Scenario: The pull-request review stage is mandatory human approval regardless of upstream scores
    Given every preceding stage of the build has been auto-approved with high Coach scores
    When Forge reaches the pull-request review stage
    Then the build should pause for mandatory human approval
    And the pause should not be eligible to be auto-approved

  # Why: Successful build records a session outcome with the full chain of gate decisions
  @key-example
  Scenario: Completing the pull-request review writes a session outcome that links every gate decision in order
    Given a build is paused at pull-request review
    When the operator approves the pull request
    Then the build should reach a complete terminal state
    And the recorded session outcome should reference every gate decision from product-owner through pull-request review
    And the recorded gate decisions should be linked in chronological order

  # ===========================================================================
  # GROUP B — Boundary Conditions
  # ===========================================================================

  # Why: A single-feature catalogue is the smallest valid scope — exercises one full inner loop
  @boundary @smoke
  Scenario: A greenfield build with a single feature in the catalogue completes one inner loop
    Given the system-design stage has produced exactly one feature
    When the build progresses through the per-feature loop
    Then exactly one /feature-spec dispatch and one /feature-plan dispatch and one autobuild dispatch should be recorded
    And the chain should culminate in a single pull-request review pause

  # Why: Multiple-feature catalogues exercise the iteration boundary — each feature must run its own inner loop
  @boundary
  Scenario Outline: Multi-feature catalogues run one inner loop per feature
    Given the system-design stage has produced <count> features in the catalogue
    When the build progresses through the per-feature loop
    Then <count> /feature-spec dispatches should be recorded
    And <count> /feature-plan dispatches should be recorded
    And <count> autobuild dispatches should be recorded

    Examples:
      | count |
      | 2     |
      | 5     |

  # Why: Just-outside boundary — a system-design stage that produces no features cannot proceed
  @boundary @negative
  Scenario: A system-design stage that produces zero features cannot enter the per-feature loop
    Given the system-design stage has produced no features
    When the per-feature loop is reached
    Then the build should not issue any /feature-spec dispatches
    And the build should be flagged for review with the missing-catalogue rationale recorded

  # Why: Stage-ordering invariant — no stage dispatches before its prerequisite chain is approved
  @boundary
  Scenario Outline: A downstream stage is not dispatched before its prerequisite has reached the approved state
    Given the prerequisite "<prerequisite>" has not yet been approved
    When the build's reasoning loop considers the next dispatch
    Then no dispatch should be issued for "<stage>"

    Examples:
      | stage         | prerequisite                  |
      | architect     | product-owner                 |
      | system-arch   | architect                     |
      | system-design | system-arch                   |
      | feature-spec  | system-design                 |
      | feature-plan  | feature-spec for that feature |
      | autobuild     | feature-plan for that feature |
      | pull-request  | autobuild for every feature   |

  # Why: Per-feature autobuild boundary — minimal one-wave plan exercises the smallest valid plan shape
  @boundary
  Scenario: Autobuild executing a single-wave plan completes one wave before reporting complete
    Given a feature's approved build plan contains exactly one wave with one task
    When autobuild is dispatched for that feature
    Then the live status view should report progress for one wave and one task
    And on success the autobuild lifecycle should reach the completed state with one task completed

  # Why: Async-subagent ETA boundary — supervisor can poll status without blocking
  @boundary
  Scenario: While autobuild is running, the supervisor can answer concurrent status queries
    Given autobuild is dispatched and reports the running-wave lifecycle
    When the operator requests live status during the wave
    Then the supervisor should respond with the current wave and task indices
    And the autobuild itself should remain in the running-wave lifecycle

  # ===========================================================================
  # GROUP C — Negative Cases
  # ===========================================================================

  # Why: A hard-stop at the product-owner stage terminates the build — no later dispatches should occur
  @negative
  Scenario: A hard-stop at the product-owner stage prevents any later stage from being dispatched
    Given the product-owner specialist returns a result that causes a hard-stop gate
    When the build evaluates the product-owner result
    Then the build should reach a failed terminal state
    And no architect, architecture, design, specification, planning, or autobuild dispatch should have been recorded

  # Why: Specialist degraded mode forces flag-for-review even when scoring is unavailable
  @negative
  Scenario: When no product-owner specialist is reachable the build is flagged for review at that stage
    Given the live discovery cache reports no healthy product-owner specialist
    When the build reaches the product-owner stage
    Then the build should be paused for review
    And the pause rationale should record degraded specialist availability
    And no later stage should have been dispatched

  # Why: A failed inner-loop /feature-spec dispatch must not cascade into a planning attempt
  @negative
  Scenario: A failed /feature-spec dispatch for a feature halts that feature's inner loop
    Given a feature is being processed in the per-feature loop
    When the /feature-spec dispatch for that feature returns a failed result
    Then the build should not issue a /feature-plan dispatch for that feature
    And the build should not issue an autobuild dispatch for that feature
    And the failed-spec rationale should be recorded against the build

  # Why: Constitutional override — pull-request review can never be auto-approved by upstream score alone
  @negative @regression
  Scenario: Auto-approval is refused at the pull-request review stage even when the upstream Coach score is at the maximum
    Given the upstream stages have all returned the maximum Coach score
    When the build reaches the pull-request review stage
    Then the build should pause for mandatory human approval
    And the pause should not be eligible to resolve without a human decision

  # Why: Skip steering must not bypass constitutional gates
  @negative @regression
  Scenario: A skip directive issued against the pull-request review stage is refused
    Given the build is paused at pull-request review
    When the operator issues a skip directive for that stage
    Then the build should remain paused for mandatory human approval
    And the skip should be recorded as refused with a constitutional rationale

  # Why: An autobuild internal hard-stop must not be silently absorbed — it surfaces to the build
  @negative
  Scenario: An autobuild internal hard-stop is propagated to the build and prevents pull-request creation
    Given autobuild is in flight for a feature
    When an internal task hits a hard-stop gate
    Then the autobuild lifecycle should reach a failed terminal state for that feature
    And no pull-request creation dispatch should be issued for that feature
    And the build's stage history should record the autobuild failure with the hard-stop rationale

  # Why: Pull-request creation failure must mark the build failed even if every prior stage was approved
  @negative
  Scenario: A pull-request creation failure marks the build failed with no pull-request URL
    Given every prior stage has been approved
    When the pull-request creation stage fails
    Then the build should reach a failed terminal state
    And no pull-request URL should be recorded against the build

  # Why: Reject decision at any pause is a terminal failure path, not a retry path
  @negative
  Scenario: A reject decision at a flagged-for-review checkpoint terminates the build
    Given the build is paused at any flagged-for-review checkpoint before pull-request review
    When the operator responds with reject
    Then the build should reach a failed terminal state
    And no later stage should be dispatched

  # ===========================================================================
  # GROUP D — Edge Cases
  # ===========================================================================

  # Why: Crash mid-stage triggers anchor §5 retry-from-scratch — the build re-enters preparing
  @edge-case
  Scenario Outline: A crash during any non-terminal stage is recovered by retry-from-scratch
    Given the build is in the "<stage>" stage
    When the runtime restarts after an unexpected interruption
    Then the build should re-enter the preparing state
    And the prior in-flight stage should be reattempted from the start

    Examples:
      | stage         |
      | product-owner |
      | architect     |
      | architecture  |
      | system-design |
      | feature-spec  |
      | feature-plan  |
      | autobuild     |

  # Why: Async subagent state channel is advisory on recovery — durable history is authoritative
  # [ASSUMPTION: confidence=high] On crash recovery, durable history is authoritative; live async-subagent state channel is advisory
  @edge-case
  Scenario: After a crash mid-autobuild the build's authoritative status comes from durable history not the live state channel
    Given autobuild was running and the runtime crashes
    When the runtime restarts
    Then the build's authoritative status should be read from the durable history
    And any live state channel data should be treated as advisory

  # Why: Mid-flight steering — the operator can inject a directive to a running autobuild
  @edge-case
  Scenario: A directive injected into a running autobuild is delivered as a pending instruction without halting the wave
    Given autobuild is in the running-wave lifecycle for a feature
    When the operator injects a steering directive for that autobuild
    Then the directive should appear as pending against that autobuild's live state
    And the autobuild should remain in the running-wave lifecycle until it next checks for directives

  # Why: Cancel during a paused stage produces a synthetic reject and terminates the build
  @edge-case
  Scenario: A cancel directive while paused at a checkpoint terminates the build with a synthetic reject
    Given the build is paused at a flagged-for-review checkpoint
    When the operator issues a cancel directive
    Then the pause should resolve as a synthetic reject with a cancel rationale
    And the build should reach a cancelled terminal state

  # Why: Cancel during autobuild reaches into the async subagent
  @edge-case
  Scenario: A cancel directive during a running autobuild halts the asynchronous task and terminates the build
    Given autobuild is in flight for a feature
    When the operator issues a cancel directive for the build
    Then the autobuild's live state should reach the cancelled lifecycle
    And the build should reach a cancelled terminal state with no pull-request creation attempted

  # Why: Skip on a non-constitutional stage records the skip and continues the chain
  @edge-case
  Scenario: A skip directive on a non-constitutional stage marks that stage skipped and resumes the chain
    Given the build is paused at a flagged-for-review checkpoint that is not pull-request review
    When the operator issues a skip directive
    Then the stage should be recorded as skipped on the build's stage history
    And the build should resume at the next stage in the chain

  # Why: Approval responses are routed by build identifier — concurrent paused builds remain isolated
  @edge-case
  Scenario: An approval response is routed to the build whose identifier matches the response channel
    Given two builds are simultaneously paused at flagged-for-review checkpoints
    When an approval response is received that matches one build's identifier
    Then only that build should resume
    And the other paused build should remain awaiting its own approval

  # Why: Async subagent's interrupt does not block the supervisor — pauses are visible via status only
  @edge-case
  Scenario: An internal autobuild pause is observable through the supervisor without blocking other supervisor work
    Given an autobuild internal task fires a flagged-for-review pause
    When the operator queries live status
    Then the autobuild's live state should report awaiting-approval with the stage label
    And the supervisor should remain free to perform other work for other builds

  # Why: Per-feature parallelism boundary — supervisor sequences inner loops to avoid resource clash
  # [ASSUMPTION: confidence=medium] Per-feature autobuild runs are sequenced within a single build to avoid worktree contention; inter-feature parallelism is out of scope for Mode A v1
  @edge-case
  Scenario: Per-feature inner loops are sequenced so each feature's autobuild completes before the next feature's autobuild begins
    Given the system-design stage has produced two or more features
    When the build progresses through the per-feature loop
    Then no second autobuild dispatch should begin while a first autobuild is still in a non-terminal lifecycle

  # Why: A repeat approval for the same request is idempotent — first response wins
  @edge-case
  Scenario: A duplicate approval response for the same paused stage is ignored
    Given the build has resumed after an approval response was honoured
    When a duplicate response with the same request identifier arrives
    Then the build should not re-resume
    And no additional stage transition should be recorded for the duplicate

  # ===========================================================================
  # GROUP E — Security
  # ===========================================================================

  # Why: Constitutional pull-request rule is enforced at both the prompt layer and the executor layer
  # [ASSUMPTION: confidence=high] Constitutional pull-request review is enforced both in the prompt and by an executor-side hardcoded branch (belt-and-braces)
  @security @regression
  Scenario: Pull-request review enforcement holds even if a misconfigured prompt would have allowed auto-approve
    Given the system prompt is configured incorrectly so that pull-request review appears auto-approvable
    When the build reaches the pull-request review stage
    Then the executor layer should still enforce mandatory human approval

  # Why: Specialist results cannot escalate beyond their delegated authority
  @security
  Scenario: A specialist result that asserts an override of constitutional rules is ignored at gating
    Given a specialist returns a result claiming to override the pull-request review rule
    When the build reaches the pull-request review stage
    Then the override claim should be ignored
    And the build should pause for mandatory human approval

  # Why: Each build runs only inside its ephemeral worktree allowlist — no cross-build leakage
  @security
  Scenario: Subprocess stages run only against the build's ephemeral worktree path
    Given the build has a configured worktree path
    When any subprocess stage is dispatched
    Then the working directory used by the subprocess should fall under the build's worktree allowlist
    And no path outside that allowlist should be writable by the dispatched subprocess

  # ===========================================================================
  # GROUP F — Concurrency
  # ===========================================================================

  # Why: Two builds run concurrently with isolated approval channels and isolated autobuild task identifiers
  @concurrency
  Scenario: Two concurrent greenfield builds use separate approval channels and separate autobuild task identifiers
    Given two distinct builds are dispatched at approximately the same time
    When both builds reach autobuild and both later reach a flagged-for-review pause
    Then each build should have a distinct autobuild task identifier
    And each build's approval pause should resolve only on a response matching its own build identifier

  # Why: A long-running autobuild does not block the supervisor's reasoning loop for unrelated builds
  @concurrency
  Scenario: The supervisor can dispatch a different build's stage while another build's autobuild is mid-flight
    Given a first build's autobuild is in the running-wave lifecycle
    When a second build is queued and picked up
    Then the second build's product-owner stage should be dispatched without waiting for the first build's autobuild to complete

  # ===========================================================================
  # GROUP G — Data Integrity
  # ===========================================================================

  # Why: Stage-history ordering invariant — the chain reflects the actual order of approvals
  @data-integrity
  Scenario: The recorded stage history of a successful build follows the canonical Mode A ordering
    Given a build has reached the complete terminal state
    When the operator inspects the build's stage history
    Then the stage entries should appear in the order product-owner, architect, architecture, design, then per-feature spec, plan, and autobuild, then pull-request review

  # Why: Each per-feature loop's artefacts are attributed to the feature that produced them
  @data-integrity
  Scenario: Per-feature artefact paths are recorded against the feature that produced them
    Given the system-design stage has produced two or more features
    When per-feature stages complete for each feature
    Then each per-feature stage entry should record the artefact paths produced for its feature only
    And no artefact path should be attributed to more than one feature

  # Why: Even when a stage's outbound notification publish fails, the build's durable transition still holds
  @data-integrity
  Scenario: A notification publish failure does not regress the build's recorded stage progress
    Given a stage has been approved
    When the outbound notification publish for that approval fails
    Then the stage should still be recorded as approved on the build's history
    And the next stage's prerequisite should still evaluate as satisfied

  # ===========================================================================
  # GROUP H — Integration Boundaries
  # ===========================================================================

  # Why: End-to-end smoke — minimal greenfield input produces a paused-for-review pull request
  @integration @smoke
  Scenario: A minimal greenfield brief drives a single-feature run to a pull request awaiting human review
    Given the operator queues a build with a one-line product brief
    And the system-design stage will produce a single-feature catalogue
    When the build proceeds through every Mode A stage with auto-approval at every flagged-for-review checkpoint
    Then the terminal state should be paused at pull-request review awaiting human approval
    And a pull-request URL should be recorded against the build

  # Why: Degraded mode (no specialist available) at any specialist stage forces flag-for-review there
  @integration
  Scenario: A greenfield build with no available specialists is flagged for review at every specialist stage
    Given no product-owner or architect specialist is reachable on the fleet
    When a greenfield build is queued and picked up
    Then the build should be flagged for review at the product-owner stage with a degraded rationale
    And no architect dispatch should occur until the operator responds

  # Why: A multi-feature catalogue produces a series of completed inner loops culminating in pull-request review
  # [ASSUMPTION: confidence=medium] One pull request per feature in the catalogue (not a single combined pull request); pull-request review pauses occur per feature
  @integration
  Scenario: A multi-feature greenfield run produces one autobuild and one pull-request review pause per feature
    Given the system-design stage will produce two or more features
    When the build proceeds through every Mode A stage with auto-approval at every flagged-for-review checkpoint
    Then for each feature there should be one /feature-spec, one /feature-plan, and one autobuild stage entry
    And there should be one pull-request review pause per feature in the catalogue

  # ===========================================================================
  # GROUP I — Expansion (additional integration-level scenarios)
  # ===========================================================================

  # Why: First-write-wins discipline survives at the integration level even when two operators respond simultaneously
  @concurrency @edge-case
  Scenario: Two simultaneous approval responses for the same paused build resolve as first-wins
    Given the build is paused at a flagged-for-review checkpoint
    When two approval responses arrive simultaneously for the same paused stage with different decisions
    Then the build should resolve under exactly one of those decisions
    And no second resume should be applied for the duplicate response

  # Why: Correlation threading is the audit anchor — every lifecycle event for one build must carry the same correlation identifier
  @data-integrity
  Scenario: Every published lifecycle event for a build threads the same correlation identifier from queue to terminal
    Given a build has been queued with a correlation identifier
    When the build progresses through every Mode A stage to a terminal state
    Then every lifecycle event published for that build should carry that same correlation identifier

  # Why: Calibration snapshot stability — a long-running build sees a consistent priors view, not a moving target
  # [ASSUMPTION: confidence=medium] Calibration priors are snapshotted at build start and remain stable for the duration of that build; mid-run history changes apply only to subsequent builds
  @data-integrity
  Scenario: Calibration priors captured at build start remain the snapshot used by every stage of that build
    Given a build is picked up and a calibration-priors snapshot is captured at start
    When the operator's calibration history is updated while the build is mid-run
    Then later stages of the in-flight build should still use the priors snapshot captured at start

  # Why: Long-term memory seeding is best-effort and must never regress an authoritative approval
  @integration @data-integrity
  Scenario: A long-term-memory seeding failure for one stage does not regress that stage's recorded approval
    Given a stage has been approved by gating
    When the long-term-memory seeding for that stage's gate decision fails
    Then the stage should still be recorded as approved on the build's history
    And the next stage's prerequisite should still evaluate as satisfied
Write mode-a-greenfield-end-to-end_assumptions.yaml
123 lines
# Assumptions manifest for mode-a-greenfield-end-to-end.feature
# Generated by /feature-spec

feature: "Mode A Greenfield End-to-End"
feature_id: "FEAT-FORGE-007"
generated: "2026-04-25T00:00:00Z"
stack: "python"
review_required: false

assumptions:
  - id: "ASSUM-001"
    scenario: "A greenfield brief drives a full pipeline run to a pull request awaiting review"
    assumption: >-
      Eight stage classes drive Mode A: product-owner → architect → /system-arch
      → /system-design → /feature-spec → /feature-plan → autobuild →
      pull-request review.
    confidence: high
    basis: >-
      Verbatim from forge/docs/research/ideas/forge-build-plan.md FEAT-FORGE-007
      description: "raw input → delegate to PO agent → checkpoint → delegate to
      architect → checkpoint → /system-arch → /system-design → /feature-spec ×
      N → /feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint
      (PR review)".
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "Autobuild runs as an asynchronous subagent so the supervisor remains responsive during long runs"
    assumption: >-
      Autobuild dispatch uses the AsyncSubAgent pattern with the async_tasks
      state channel exposing wave and task indices and lifecycle.
    confidence: high
    basis: >-
      forge/docs/design/contracts/API-subagents.md §3 (autobuild_runner is an
      AsyncSubAgent launched via start_async_task) +
      forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md
      (AutobuildState shape: wave_index, task_index, lifecycle, etc.).
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "The pull-request review stage is mandatory human approval regardless of upstream scores"
    assumption: >-
      Pull-request review and pull-request creation tool names are
      constitutionally pinned to mandatory human approval regardless of upstream
      Coach score.
    confidence: high
    basis: >-
      forge/docs/design/contracts/API-nats-approval-protocol.md §8 (constitutional
      rule — PR review): tool_name in {"review_pr", "create_pr_after_review"} →
      GateMode.MANDATORY_HUMAN_APPROVAL regardless of score or priors. ADR-ARCH-026
      belt+braces enforcement.
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "After a crash mid-autobuild the build's authoritative status comes from durable history not the live state channel"
    assumption: >-
      On crash recovery, durable history is authoritative for build status; live
      async-subagent state channel data is advisory only.
    confidence: high
    basis: >-
      forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md
      "Crash-recovery linkage": SQLite is the authoritative lifecycle state; the
      state-channel data is advisory for reasoning. Reinforced by
      forge/docs/design/models/DM-build-lifecycle.md §5 invariants and
      §"Defence in depth with SQLite".
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "Pull-request review enforcement holds even if a misconfigured prompt would have allowed auto-approve"
    assumption: >-
      Constitutional pull-request review is enforced both at the prompt layer
      and at the executor (gating) layer; loss of either is a constitutional
      regression.
    confidence: high
    basis: >-
      forge/docs/design/contracts/API-nats-approval-protocol.md §8 verbatim:
      "Both must be wired independently. Loss of either is a constitutional
      regression."
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "Per-feature inner loops are sequenced so each feature's autobuild completes before the next feature's autobuild begins"
    assumption: >-
      Within a single build, per-feature autobuild runs are sequenced (one
      autobuild for one feature must reach a terminal lifecycle before the next
      feature's autobuild begins). Inter-feature parallelism is out of scope for
      Mode A v1.
    confidence: medium
    basis: >-
      Each Build has a single worktree_path (DM-build-lifecycle.Build); running
      two autobuilds concurrently in the same worktree would create branch
      contention. Sequencing is implied by the worktree-per-build model but is
      not explicitly stated as a sequencing rule in any provided context file.
    human_response: "confirmed"

  - id: "ASSUM-007"
    scenario: "Calibration priors captured at build start remain the snapshot used by every stage of that build"
    assumption: >-
      Calibration priors retrieved at build start are the snapshot used by every
      stage of that build; mid-run changes to the operator's calibration
      history apply only to subsequent builds.
    confidence: medium
    basis: >-
      forge/docs/design/diagrams/agent-runtime.md describes prompts (including
      {calibration_priors} placeholder) as "Assembled at build start";
      snapshot-stability across the build is implied but not stated as an
      explicit invariant.
    human_response: "confirmed"

  - id: "ASSUM-008"
    scenario: "A multi-feature greenfield run produces one autobuild and one pull-request review pause per feature"
    assumption: >-
      Each feature in the catalogue produces its own pull request; the
      pull-request review pause occurs per feature, not as a single combined
      pull request for the whole build.
    confidence: medium
    basis: >-
      forge/docs/research/ideas/forge-build-plan.md §10 reads
      "/feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint".
      The "× N" attaches to autobuild but the git/PR step's granularity is not
      pinned. Per-feature PR is the most natural interpretation given
      per-feature artefacts and per-feature gate pauses.
    human_response: "confirmed"
Write mode-a-greenfield-end-to-end_summary.md
120 lines
# Feature Spec Summary: Mode A Greenfield End-to-End

**Feature ID**: FEAT-FORGE-007
**Stack**: python
**Generated**: 2026-04-25T00:00:00Z
**Scenarios**: 47 total (4 smoke, 4 regression)
**Assumptions**: 8 total (5 high / 3 medium / 0 low confidence)
**Review required**: No — all assumptions traceable to supplied context files

## Scope

Specifies Forge's capstone Mode A greenfield orchestration: how a single one-line
product brief is driven through the eight-stage chain (product-owner delegation →
architect delegation → architecture → system design → per-feature specification →
per-feature planning → autobuild → pull-request review) under one supervised
build. Covers stage-ordering invariants (no downstream dispatch before
prerequisite approval), forward propagation of stage outputs into the next stage's
context, the asynchronous-subagent dispatch pattern for long-running autobuild
runs (with live wave/task progress and supervisor responsiveness during the
run), the constitutional belt-and-braces rule that pins pull-request review to
mandatory human approval at both the prompt and executor layers, crash recovery
as retry-from-scratch with durable history as the authoritative source against
the advisory live state channel, CLI steering (cancel → synthetic reject; skip
honoured on non-constitutional stages, refused on pull-request review), pause
isolation across simultaneously paused builds, idempotent first-write-wins
discipline on duplicate or simultaneous responses, and end-to-end integrity
invariants — correlation-identifier threading, calibration-priors snapshot
stability for the duration of one build, and per-feature artefact attribution.
Behaviour is described in domain terms; the AsyncSubAgent state channel,
LangGraph interrupt round-trip, NATS approval channel, and SQLite history surface
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 9 |
| Edge cases (@edge-case) | 11 |
| Smoke (@smoke) | 4 |
| Regression (@regression) | 4 |
| Security (@security) | 3 |
| Concurrency (@concurrency) | 3 |
| Data integrity (@data-integrity) | 6 |
| Integration (@integration) | 4 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + concurrency, integration + data-integrity). Group totals do not
sum to 47.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — full chain to PR-awaiting-review, forward propagation between stages, async-subagent dispatch, flag/resume cycle, constitutional PR-review pin, session-outcome chain | 8 |
| B | Boundary Conditions — single-feature catalogue, multi-feature outline (count=2,5), zero-feature catalogue, stage-ordering invariant outline (7 prerequisites), single-wave plan, concurrent status query during running wave | 6 |
| C | Negative Cases — PO hard-stop, no PO specialist (degraded), failed feature-spec halts inner loop, max-score does not auto-approve PR review, skip refused at PR review, autobuild internal hard-stop blocks PR creation, PR-creation failure marks build failed, reject decision is terminal | 8 |
| D | Edge Cases — crash recovery outline (7 stages), durable-history authority on recovery, mid-flight steering as pending directive, cancel during pause, cancel during autobuild, skip on non-constitutional stage, approval routed by build identifier, internal autobuild pause observable via supervisor, per-feature sequencing within a build, duplicate response idempotent | 10 |
| E | Security — belt-and-braces holds against misconfigured prompt, specialist override claim ignored at gating, subprocess worktree-allowlist confinement | 3 |
| F | Concurrency — two concurrent builds with isolated channels and task IDs, supervisor dispatches second build's stage during first build's autobuild | 2 |
| G | Data Integrity — canonical Mode A stage-history ordering, per-feature artefact attribution, notification publish failure does not regress approval | 3 |
| H | Integration Boundaries — minimal greenfield E2E smoke, no-specialists degraded E2E, multi-feature E2E with one PR per feature | 3 |
| I | Expansion — first-wins on simultaneous approvals, correlation threading queue→terminal, calibration-priors snapshot stability, memory-seeding failure does not regress approval | 4 |

## Deferred Items

None.

## Assumptions Summary

| ID | Confidence | Subject | Response |
|----|------------|---------|----------|
| ASSUM-001 | high | Eight Mode A stage classes | confirmed |
| ASSUM-002 | high | Autobuild via AsyncSubAgent + state channel | confirmed |
| ASSUM-003 | high | PR review pinned to mandatory human approval | confirmed |
| ASSUM-004 | high | Durable history authoritative on crash recovery | confirmed |
| ASSUM-005 | high | Constitutional belt-and-braces (prompt + executor) | confirmed |
| ASSUM-006 | medium | Per-feature autobuild sequenced within a build | confirmed |
| ASSUM-007 | medium | Calibration priors snapshotted at build start | confirmed |
| ASSUM-008 | medium | One pull request per feature in catalogue | confirmed |

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The build queue,
  state-machine transitions, durable history, crash recovery (retry-from-scratch),
  and CLI steering surface are referenced here as the substrate every Mode A
  stage rides on. FEAT-FORGE-007 adds no new transitions; it composes them.
- **FEAT-FORGE-002** — NATS Fleet Integration. The live discovery cache,
  fleet-lifecycle subscription, pipeline-event publishing (correlation
  threading), and approval channel are inherited; FEAT-FORGE-007 specifies how
  the supervisor sequences dispatches over them.
- **FEAT-FORGE-003** — Specialist Agent Delegation. The product-owner and
  architect specialist dispatches plus the soft/hard timeout, retry-with-context,
  and degraded-mode behaviour are inherited; FEAT-FORGE-007 specifies their
  ordering and forward propagation.
- **FEAT-FORGE-004** — Confidence-Gated Checkpoint Protocol. The auto-approve /
  flag-for-review / hard-stop / mandatory-human-approval gate modes, the
  build-keyed approval round-trip, idempotent first-wins, max-wait refresh,
  CLI cancel/skip mapping, and the constitutional PR-review rule are inherited;
  FEAT-FORGE-007 specifies how those gates compose across the eight-stage chain.
- **FEAT-FORGE-005** — GuardKit Command Invocation Engine. The subprocess
  contract for /system-arch, /system-design, /feature-spec, /feature-plan,
  autobuild, and /task-* — including context-flag construction and worktree
  confinement — is inherited; FEAT-FORGE-007 specifies the order and inputs.
- **FEAT-FORGE-006** — Infrastructure Coordination. Long-term-memory seeding,
  priors retrieval at build start, test verification, and git/gh PR creation are
  inherited; FEAT-FORGE-007 specifies how their failure modes interact with the
  build's authoritative recorded progress.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Mode A Greenfield End-to-End" \
      --context features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
Update Todos

Phase 1: Gather context (read all 18 --context files)

Phase 2: Generate initial proposal (grouped scenarios)

Phase 3: Curate (await user A/R/M/+/? per group)

Phase 4: Edge case expansion (offer Y/S/N)

Phase 5: Resolve assumptions with user

Phase 6: Write .feature, _assumptions.yaml, _summary.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC COMPLETE: Mode A Greenfield End-to-End (FEAT-FORGE-007)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files written:

features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end.feature
features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_assumptions.yaml
features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_summary.md
Scenarios: 47 total

@key-example: 8 @boundary: 6
@negative: 9 @edge-case: 11
@smoke: 4 @regression: 4
@security: 3 @concurrency: 3
@data-integrity: 6 @integration: 4
Assumptions: 8 total

high: 5 (auto-proceed)
medium: 3 (Coach review recommended)
low: 0
Deferred: None.

Next steps:


/feature-plan "Mode A Greenfield End-to-End" \
  --context features/mode-a-greenfield-end-to-end/mode-a-greenfield-end-to-end_summary.md
Per the build plan, FEAT-FORGE-007 depends on FEAT-FORGE-003, 004, 005, and 006 (all spec'd; 002 already planned & merged). Step 11 of /feature-plan will tag each scenario above with @task:<TASK-ID> for the task-level BDD runner.