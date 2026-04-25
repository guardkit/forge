/feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md


 I'll execute the /feature-spec command for FEAT-FORGE-004. Starting with Phase 1: Context Gathering.

Bash Check top-level and docs structure
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/ && ls /home/richardwoollcott/Projects/appmilla_github/forge/forge/docs/design/ 2>/dev/null || echo "no forge/docs/design"

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
no forge/docs/design
Bash Find FEAT-FORGE-004 related files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -type f -name "*.md" -path "*FEAT-FORGE-004*" 2>/dev/null | head -20

OUT
(Bash completed with no output)
Bash List design directories
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/contracts/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/models/ /home/richardwoollcott/Projects/appmilla_github/forge/docs/design/decisions/ 2>/dev/null

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
The paths need adjusting — the user's paths have an extra forge/ prefix. Let me read the actual context files and find FEAT-FORGE-004 description.

Bash Find FEAT directories and files
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -type d -name "FEAT*" 2>/dev/null | head -20 && echo "---" && find /home/richardwoollcott/Projects/appmilla_github/forge -type f -name "*FEAT*" 2>/dev/null | head -20

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/autobuild/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.git/worktrees/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/.guardkit/worktrees/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/.guardkit/autobuild/FEAT-FORGE-002
---
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/features/FEAT-FORGE-002.yaml
/home/richardwoollcott/Projects/appmilla_github/forge/docs/history/feature-spec-FEAT-FORGE-002-history.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/history/feature-spec-FEAT-FORGE-001.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-002.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-001.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-003.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-005.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-006.md
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/FEAT-PO-004.md
/home/richardwoollcott/Projects/appmilla_github/forge/.git/refs/heads/autobuild/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.git/logs/refs/heads/autobuild/FEAT-FORGE-002
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/history/feature-spec-FEAT-FORGE-002-history.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/history/feature-spec-FEAT-FORGE-001.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/.guardkit/features/FEAT-FORGE-002.yaml
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-002.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-001.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-003.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-005.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-006.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/product/feature_spec_inputs/FEAT-PO-004.md
Read API-nats-approval-protocol.md
Read DM-gating.md
Read DDR-002-resume-value-rehydration-helper.md
Bash Find files mentioning FEAT-FORGE-004
IN
find /home/richardwoollcott/Projects/appmilla_github/forge -type f \( -name "*.md" -o -name "*.yaml" \) 2>/dev/null | xargs grep -l "FEAT-FORGE-004" 2>/dev/null | head -10

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md
/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/worktrees/FEAT-FORGE-002/docs/research/ideas/forge-build-plan.md
Bash List feature input sources
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/ /home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/features/ 2>/dev/null

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/docs/product/feature_spec_inputs/:
FEAT-PO-001.md
FEAT-PO-002.md
FEAT-PO-003.md
FEAT-PO-004.md
FEAT-PO-005.md
FEAT-PO-006.md

/home/richardwoollcott/Projects/appmilla_github/forge/.guardkit/features/:
FEAT-FORGE-002.yaml
Bash Check existing features and tasks
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/ /home/richardwoollcott/Projects/appmilla_github/forge/tasks/ 2>/dev/null

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/features/:
nats-fleet-integration
pipeline-state-machine-and-configuration

/home/richardwoollcott/Projects/appmilla_github/forge/tasks/:
backlog
completed
in_review
Bash Extract FEAT-FORGE-004 description
IN
cat /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | grep -A 50 "FEAT-FORGE-004" | head -80

OUT
| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve, 🟡 flag for review (PAUSED state), 🔴 hard stop. Configurable thresholds per anchor §4 | Confidence-gated checkpoints with configurable thresholds |
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
--
# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine
guardkit feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/contracts/API-tool-layer.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md

# FEAT-FORGE-006: Infrastructure Coordination
guardkit feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/models/DM-graphiti-entities.md \
  --context forge/docs/design/models/DM-calibration.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-004-graphiti-group-partitioning.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  # plus all previous feature spec files (forge/features/FEAT-FORGE-001..006/*)

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix
Bash Existing feature spec examples
IN
ls /home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/ /home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/ 2>/dev/null

OUT
/home/richardwoollcott/Projects/appmilla_github/forge/features/nats-fleet-integration/:
nats-fleet-integration_assumptions.yaml
nats-fleet-integration.feature
nats-fleet-integration_summary.md

/home/richardwoollcott/Projects/appmilla_github/forge/features/pipeline-state-machine-and-configuration/:
pipeline-state-machine-and-configuration_assumptions.yaml
pipeline-state-machine-and-configuration.feature
pipeline-state-machine-and-configuration_summary.md
Now I have the feature description. Let me also check for the broader anchor context and look at existing feature specs for convention.

Bash Get more context on FEAT-FORGE-004
IN
grep -B 2 -A 30 "FEAT-FORGE-004\|Confidence-Gated Checkpoint" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | head -80

OUT
| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads | Publish pipeline events, `fleet.register` (ADR-SP-014) |
| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles (ADR-SP-015), result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure | NATS command invocation of specialist agents (Stage 2) |
| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve, 🟡 flag for review (PAUSED state), 🔴 hard stop. Configurable thresholds per anchor §4 | Confidence-gated checkpoints with configurable thresholds |
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
--
  --context nats-core/docs/design/specs/nats-core-system-spec.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine
guardkit feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/contracts/API-tool-layer.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md

# FEAT-FORGE-006: Infrastructure Coordination
guardkit feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/models/DM-graphiti-entities.md \
  --context forge/docs/design/models/DM-calibration.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-004-graphiti-group-partitioning.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  # plus all previous feature spec files (forge/features/FEAT-FORGE-001..006/*)

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix
guardkit feature-spec FEAT-FORGE-008 \
  --context forge/features/FEAT-FORGE-007/feature-spec.md \
  --context forge/docs/design/contracts/API-cli.md
```
--
  --context forge/features/<feat-forge-003-slug>/<feat-forge-003-slug>_summary.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol (depends on 003)
guardkit feature-plan FEAT-FORGE-004 \
  --context forge/features/<feat-forge-004-slug>/<feat-forge-004-slug>_summary.md

# FEAT-FORGE-006: Infrastructure Coordination (depends on 001, 002)
guardkit feature-plan FEAT-FORGE-006 \
  --context forge/features/<feat-forge-006-slug>/<feat-forge-006-slug>_summary.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End (depends on 003, 004, 005, 006)
Read pipeline-state-machine-and-configuration.feature
Read nats-fleet-integration_summary.md
Let me check the anchor document for FEAT-FORGE-004 details and examine existing feature file conventions.

Bash Look for checkpoint context in anchor
IN
grep -B 2 -A 30 "Confidence-Gated\|confidence-gat\|check.*point\|checkpoint" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | head -200

OUT
This build plan captures the full GuardKit command sequence to build the Forge — the
pipeline orchestrator and checkpoint manager that coordinates the specialist agent fleet.
The Forge is the capstone of the Software Factory: once it works, the pipeline from raw
idea to deployed code runs end-to-end with confidence-gated human engagement.

**Scope document:** `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md`
(v3, 11 April 2026) — defines the Forge's architecture, tool inventory, checkpoint
protocol, and NATS integration. Read that document first.

**Fleet context:** `forge/docs/research/ideas/fleet-master-index.md` (v2, 12 April
2026) — decisions D33–D38 govern the Forge's coordination model.

---

## Prerequisites

All prerequisites must be met before starting Step 1 (`/system-arch`).

### Hard Prerequisites (blocking)

- [x] **nats-core library implemented** — v0.2.0 shipped 2026-04-23 with **TASK-NCFA-003**: reconciled `StageCompletePayload`, `BuildPausedPayload`, `BuildResumedPayload` and added `BuildCancelledPayload` (all ISO-8601 `str` timestamps); `BuildQueuedPayload` already present. All topics registered. 761/761 tests passing, 98% coverage. Forge pins `nats-core>=0.2.0,<0.3` in `pyproject.toml`. Interim payload-carrier module was retired before creation (see DDR-001).
- [ ] **nats-infrastructure running on GB10** — NATS server up, JetStream enabled,
      accounts configured, `docker compose up -d` executed **and provisioning
      scripts run**: `provision-streams.sh` (creates AGENTS + PIPELINE + SYSTEM
      streams per anchor v2.2) and `provision-kv.sh` (creates `agent-registry`,
      `pipeline-state`, and other KV buckets). `verify-nats.sh` is read-only and
      does **not** self-heal a fresh volume. Per TASK-MDF-PRVS / TASK-NI-PSBUG
      (specialist-agent LES1 §7): a fresh-volume NATS without explicit
      provisioning will accept publishes (PubAck) but not retain or deliver them
      — exactly the MacBook failure mode. Scripts may require a `set +u`
      workaround on unset-var-strict shells until the ttl_opts bug is patched
      upstream.
- [ ] **nats-core integration tests passing** — tests against live NATS server on GB10,
      validates MessageEnvelope round-trip, KV registry operations, pub/sub lifecycle
- [ ] **specialist-agent Phase 3 complete** — NATS fleet integration: agents register
      via `client.register_agent(manifest)`, respond to `agents.command.*`, return
      `ResultPayload` with Coach scores. At minimum, the architect role must be
--
| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads | Publish pipeline events, `fleet.register` (ADR-SP-014) |
| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles (ADR-SP-015), result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure | NATS command invocation of specialist agents (Stage 2) |
| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve, 🟡 flag for review (PAUSED state), 🔴 hard stop. Configurable thresholds per anchor §4 | Confidence-gated checkpoints with configurable thresholds |
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
--
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
- [`API-sqlite-schema.md`](../../design/contracts/API-sqlite-schema.md) — `builds` + `stage_log` DDL + WAL
- [`API-tool-layer.md`](../../design/contracts/API-tool-layer.md) — all `@tool` functions + 11 GuardKit wrappers
- [`API-subagents.md`](../../design/contracts/API-subagents.md) — sync `build_plan_composer` + async `autobuild_runner`
- [`API-subprocess.md`](../../design/contracts/API-subprocess.md) — GuardKit/git/gh via DeepAgents `execute`
--
  --context nats-core/docs/design/specs/nats-core-system-spec.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine
guardkit feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/contracts/API-tool-layer.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md

# FEAT-FORGE-006: Infrastructure Coordination
guardkit feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/models/DM-graphiti-entities.md \
  --context forge/docs/design/models/DM-calibration.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-004-graphiti-group-partitioning.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/contracts/API-subagents.md \
  --context forge/docs/design/diagrams/agent-runtime.md \
  --context forge/docs/design/diagrams/domain-core.md \
  --context forge/docs/design/decisions/DDR-006-async-subagent-state-channel-contract.md \
  # plus all previous feature spec files (forge/features/FEAT-FORGE-001..006/*)

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix
guardkit feature-spec FEAT-FORGE-008 \
  --context forge/features/FEAT-FORGE-007/feature-spec.md \
  --context forge/docs/design/contracts/API-cli.md
--
  --context forge/features/<feat-forge-003-slug>/<feat-forge-003-slug>_summary.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol (depends on 003)
guardkit feature-plan FEAT-FORGE-004 \
  --context forge/features/<feat-forge-004-slug>/<feat-forge-004-slug>_summary.md

# FEAT-FORGE-006: Infrastructure Coordination (depends on 001, 002)
guardkit feature-plan FEAT-FORGE-006 \
  --context forge/features/<feat-forge-006-slug>/<feat-forge-006-slug>_summary.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End (depends on 003, 004, 005, 006)
guardkit feature-plan FEAT-FORGE-007 \
  --context forge/features/<feat-forge-007-slug>/<feat-forge-007-slug>_summary.md

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix (depends on 007)
guardkit feature-plan FEAT-FORGE-008 \
  --context forge/features/<feat-forge-008-slug>/<feat-forge-008-slug>_summary.md
```

> **Note on slugs:** `/feature-spec` writes output under a kebab-cased slug of the
> feature **name**, not the `FEAT-FORGE-NNN` ID. The FEAT-FORGE-001 slug resolved
> to `pipeline-state-machine-and-configuration`. The placeholders above resolve
> as each `/feature-spec` completes — replace them with the real slugs at that
> point.

**Validation:**
- Task wave structure respects feature dependencies
- Each task has clear inputs, outputs, and acceptance criteria
- Integration tasks (FEAT-FORGE-007) are in later waves

### Step 5: Build (autobuild × 8)

Build features in dependency order. Run sequentially on GB10 (or Bedrock when available).
--
# (subscribe to pipeline.> on GB10 and observe)

# Verify checkpoint protocol
# (set low auto_approve threshold in forge.yaml to force FLAG FOR REVIEW,
#  verify pipeline.build-paused arrives)

# Verify degraded mode
# (stop specialist agents, run pipeline, verify forced FLAG FOR REVIEW)
```

#### Specialist-agent LES1 Parity Gates (pre-merge required)

Derived from TASK-REV-B8E4 cross-agent lessons (series LES1) and recorded by
TASK-REV-C3E7. Each gate has a cited evidence pointer back to a specialist-agent
TASK-MDF-* id. **All four gates must be green on the production image before the
forge build is declared canonical** — unit-test-only passes are what CMDW failed
Bash Get more anchor context
IN
grep -B 2 -A 20 "§4\|section 4\|PAUSED\|auto-approve\|AUTO_APPROVE" /home/richardwoollcott/Projects/appmilla_github/forge/docs/research/ideas/forge-build-plan.md | head -120

OUT
| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads | Publish pipeline events, `fleet.register` (ADR-SP-014) |
| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles (ADR-SP-015), result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure | NATS command invocation of specialist agents (Stage 2) |
| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve, 🟡 flag for review (PAUSED state), 🔴 hard stop. Configurable thresholds per anchor §4 | Confidence-gated checkpoints with configurable thresholds |
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
--

**Expected outcome:** The pipeline delegates to specialist agents, evaluates Coach
scores, auto-approves or flags as appropriate, invokes GuardKit commands, produces a PR.

---

## Files That Will Change

| File | Feature | Change Type |
|------|---------|-------------|
| `src/forge/__init__.py` | 001 | Create |
| `src/forge/cli/main.py` | 001 | Create — CLI entrypoint (greenfield, feature, review-fix, status) |
| `src/forge/pipeline/state_machine.py` | 001 | Create — pipeline states, transitions, persistence |
| `src/forge/pipeline/config.py` | 001 | Create — forge-pipeline-config.yaml loading + validation |
| `src/forge/pipeline/session.py` | 001 | Create — session lifecycle, crash recovery via NATS KV |
| `src/forge/fleet/registration.py` | 002 | Create — Forge AgentManifest, fleet registration, heartbeat |
| `src/forge/fleet/discovery.py` | 002 | Create — NATSKVManifestRegistry queries, degraded mode |
| `src/forge/fleet/events.py` | 002 | Create — pipeline event publishing (nats-core payloads) |
| `src/forge/delegation/agent_caller.py` | 003 | Create — call_agent_tool wrapper, result parsing |
| `src/forge/delegation/result_parser.py` | 003 | Create — extract Coach score, criterion breakdown |
| `src/forge/checkpoints/evaluator.py` | 004 | Create — score vs threshold, critical detection check |
| `src/forge/checkpoints/protocol.py` | 004 | Create — approval request/response, notification |
| `src/forge/checkpoints/config.py` | 004 | Create — per-stage threshold configuration |
--
## Pipeline Configuration Schema

> **Note:** The schema below is richer than anchor v2.2 §4's `forge.yaml` example (which uses `confidence_thresholds` + `build_config` + `degraded_mode`). The additional fields below (`reviewer`, `critical_detections`, `escalation_channel`) are operationally useful and are pending promotion to the anchor as a v2.3 amendment (TASK-FVD5). Both schemas are shown here; the anchor's shape is the current contract.

For the `forge-pipeline-config.yaml` that FEAT-FORGE-001 loads:

```yaml
# forge-pipeline-config.yaml (per project)
project: finproxy

checkpoints:
  product_docs:
    auto_threshold: 0.80
    min_threshold: 0.50
    critical_detections: [VAGUE_REQUIREMENT, UNTESTABLE_ACCEPTANCE]
    reviewer: james
    escalation_channel: "jarvis.notification.slack"

  architecture:
    auto_threshold: 0.80
    min_threshold: 0.50
    critical_detections: [PHANTOM, UNGROUNDED, SCOPE_CREEP]
    reviewer: rich
--

  pr_review:
    auto_threshold: null     # always human — never auto-approved (D37)
    min_threshold: 0.0
    reviewer: rich
```

---

## Jarvis Integration

The Forge supports multiple build trigger sources via ADR-SP-014 (Pattern A — accepted in anchor v2.2 §9). Jarvis publishes `BuildQueuedPayload` to `pipeline.build-queued.{feature_id}` — the same JetStream topic that `forge queue` CLI publishes to. Forge consumes without distinguishing sources at the consumer level; the payload's `triggered_by`, `originating_adapter`, and `correlation_id` fields carry source metadata for history, diagnostics, and routing progress events back to the originator.

The build plan does **not** require Jarvis to function. The CLI (`forge queue`) is the default and simplest path. Jarvis adds the voice (Reachy Mini), Telegram, dashboard, and CLI-wrapper entry points. For the full `BuildQueuedPayload` design including Jarvis-aware fields, correlation flow, and example payloads, see [forge-build-plan-alignment-review.md Appendix C](../forge-build-plan-alignment-review.md#appendix-c--buildqueuedpayload-full-design-jarvis-aware).

Forge also registers on `fleet.register` as an agent (`agent_id=forge`, intents: `build.*`, `pipeline.*`, `max_concurrent=1`) so that Jarvis's CAN-bus routing can discover it. Registration is for discovery; triggering remains a JetStream publish.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
--
1. **Forge is a coordinator, not a specialist** — no Player-Coach loop, no fine-tuning.
2. **Confidence-gated checkpoints, not hard checkpoints** — Coach score determines human engagement.
3. **PR review is always human** — final gate never auto-approves.
4. **NATS-native from day one** — no subprocess fallback for agent communication.
5. **Degraded mode forces FLAG FOR REVIEW** — no Coach score → no auto-approve.
6. **nats-core event payloads for all wire formats** — no new payload types.
7. **Context-first delivery** — no kanban integration, no ticket creation. PM adapter
   is optional visibility layer (FEAT-FORGE-009+, not in initial build).

---

## Post-Architecture Update Instructions

After `/system-arch` (Step 1) and `/system-design` (Step 2) produce their outputs,
this build plan must be updated with the exact file paths. Follow this procedure:

### After Step 1: /system-arch completes

1. **List the files produced:**
   ```bash
   ls forge/docs/architecture/
   ls forge/docs/decisions/
   ```
Read nats-fleet-integration.feature
Phase 1 complete. Now generating the Phase 2 proposal.


Context loaded: stack=python, FEAT-FORGE-004 (Confidence-Gated Checkpoint Protocol),
3 context files (API-nats-approval-protocol, DM-gating, DDR-002-resume-value-rehydration-helper),
2 existing sibling feature specs (FEAT-FORGE-001 state-machine, FEAT-FORGE-002 fleet),
depends on FEAT-FORGE-003 (specialist delegation), anchor v2.2 §4 + ADR-ARCH-019 + ADR-ARCH-021 Rev 10 + ADR-ARCH-026 referenced
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FEATURE SPEC PROPOSAL: Confidence-Gated Checkpoint Protocol (FEAT-FORGE-004)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@feat-forge-004 @confidence-gated-checkpoint-protocol
Feature: Confidence-Gated Checkpoint Protocol
  As Rich, the sole operator of Forge
  I want each gated pipeline stage to be evaluated against the evidence from past builds,
    and for any stage Forge cannot confidently approve on its own to pause and ask me first
  So that safe work continues automatically, uncertain work waits for my review,
    unsafe work halts loudly, and every gating decision is traceable back to the priors and findings that produced it

  Background:
    Given Forge is configured from the project configuration file
    And Forge is connected to the fleet message bus
    And the specialist-agent delegation layer is able to return Coach scores and detection findings for gated stages
━━ GROUP A: Key Examples (8 scenarios) ━━


  # Why: Core happy path — when the evidence confidently supports the work, the pipeline continues without human engagement.
  @key-example @smoke
  Scenario: A confidently-supported stage is auto-approved and the build continues
    Given a gated stage has just completed with a strong Coach score, a clean set of detection findings, and priors that concur
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as auto-approve
    And the rationale, the priors consulted, and the findings considered should all be recorded on the decision
    And the build should continue to the next stage without pausing

  # Why: Core uncertain path — when the evidence is ambiguous, the build pauses and asks Rich to decide.
  @key-example @smoke
  Scenario: A stage with ambiguous evidence pauses and requests human review
    Given a gated stage has just completed with evidence that is neither clearly positive nor clearly negative
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as flag-for-review
    And the build should enter the paused state
    And an approval request should be published for human attention on the build-specific approval channel
    And the request should carry the stage label, the Coach score, the criterion breakdown, the detection findings, the rationale, the evidence priors, and the resume options available to Rich

  # Why: Core unsafe path — clearly bad work halts the build rather than burning more turns.
  @key-example @smoke
  Scenario: A stage with strongly negative evidence halts the build
    Given a gated stage has just completed with a poor Coach score or strongly negative detection findings
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as hard-stop
    And the build should transition to a failed outcome
    And the reasons for halting should be recorded on the decision for later review

  # Why: Resume on approval — Rich confirms the paused work and the build continues where it left off.
  @key-example
  Scenario: Approval from Rich resumes the paused build
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with an approval decision
    Then the build should resume from the point at which it paused
    And the decision and responder should be recorded against the gate

  # Why: Reject on decision — Rich refuses the paused work; the build ends cleanly.
  @key-example
  Scenario: Rejection from Rich ends the paused build
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with a rejection decision
    Then the build should transition to a cancelled outcome
    And the reason Rich gave for rejecting should be recorded against the gate

  # Why: Override/skip — Rich waves through the specific stage without endorsing the general criteria.
  @key-example
  Scenario: Override from Rich skips the stage and the build continues
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with an override decision
    Then the specific gated stage should be marked as overridden
    And the build should continue to the next stage
    And the override and its reason should be recorded against the gate

  # Why: Constitutional rule — PR review is always human, no matter how strong the evidence.
  @key-example @smoke
  Scenario: A pull-request-review stage always requires human approval regardless of evidence
    Given a pull-request-review stage has just completed with evidence that would otherwise warrant auto-approval
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as mandatory human approval
    And the decision should be marked as a constitutional override
    And the build should pause and request Rich's decision before any pull request is created or merged

  # Why: Evidence-driven gating — each decision carries a written trail back to the priors and findings that shaped it.
  @key-example
  Scenario: Every gate decision records its rationale, priors, and findings
    Given a gated stage has completed and Forge has evaluated the gate
    When I inspect the recorded gate decision for that stage
    Then the decision should identify the stage, the target being gated, and the mode chosen
    And the decision should include the reasoning-model rationale in plain language
    And the decision should list the priors that informed it and the detection findings considered
    And the decision should record when it was made
━━ GROUP B: Boundary Conditions (5 scenarios) ━━


  # Why: Degraded mode — no Coach score means auto-approval is off the table (invariant from DM-gating §6).
  @boundary @negative
  Scenario: A gated stage with no Coach score cannot be auto-approved
    Given a gated stage has completed but no Coach score is available for it
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as degraded
    And the decision mode must not be auto-approve
    And the decision mode should be one of flag-for-review, hard-stop, or mandatory human approval

  # Why: Just-inside boundary for the initial approval wait.
  # [ASSUMPTION: confidence=high] Default approval request timeout is 300 seconds (API-nats-approval-protocol §3.1).
  @boundary
  Scenario: An approval request is published with the default wait time when none is specified
    Given Forge has evaluated a stage as flag-for-review
    When Forge publishes the approval request for that stage
    Then the request should carry the default wait time of 300 seconds
    And the build should remain paused until Rich responds or the wait time elapses

  # Why: Just-outside boundary — wait exceeds the per-request default but stays within the configured maximum.
  # [ASSUMPTION: confidence=high] Maximum total wait is approximately 3600 seconds via forge.yaml.approval.max_wait_seconds (API-nats-approval-protocol §7).
  @boundary
  Scenario: An approval that is not answered within the default wait time is refreshed without cancelling the pause
    Given a build has been paused and the default wait time has elapsed without a response
    When Forge checks the pending request
    Then Forge should publish a fresh approval request carrying an incremented attempt count
    And the build should remain paused and continue waiting for Rich
    And the prior request identifier should remain valid for deduplication

  # Why: Criterion-breakdown boundary (invariant from DM-gating §6).
  @boundary
  Scenario Outline: Criterion-breakdown values at the permitted extremes are accepted
    Given a gated stage completes with a criterion score of <value> for one criterion
    When Forge evaluates the gate
    Then the gate decision should be recorded without any validation error against that criterion

    Examples:
      | value |
      | 0.0   |
      | 1.0   |

  # Why: Criterion-breakdown outside the permitted range is a malformed input and must be refused.
  @boundary @negative
  Scenario: Criterion-breakdown values outside the permitted range are refused
    Given a gated stage completes with a criterion score outside the range zero to one
    When Forge attempts to evaluate the gate
    Then the input should be refused as invalid
    And no gate decision should be recorded
━━ GROUP C: Negative Cases (5 scenarios) ━━


  # Why: Detection findings must be able to escalate even when the headline score is strong.
  @negative
  Scenario: A critical detection finding escalates a stage away from auto-approval regardless of a strong score
    Given a gated stage has completed with a strong Coach score
    And at least one detection finding on that stage is marked as critical severity
    When Forge evaluates the gate
    Then the decision mode must not be auto-approve
    And the critical finding should be listed on the decision as part of the rationale

  # Why: Constitutional guardrail — executor-layer belt-and-braces for PR creation, not only PR review.
  @negative
  Scenario: Creating a pull request after review is treated with the same constitutional rule as reviewing one
    Given a stage that creates a pull request after review has completed with evidence that would otherwise warrant auto-approval
    When Forge evaluates the gate
    Then the gate decision should be recorded as mandatory human approval
    And the decision should be marked as a constitutional override

  # Why: Invariant — when the mode is mandatory human approval, auto-approve override must be recorded or no threshold applied.
  @negative
  Scenario: A mandatory-human-approval decision must not masquerade as a threshold-based approval
    Given Forge is recording a gate decision whose mode is mandatory human approval
    When the decision is persisted
    Then the decision should either be marked as a constitutional override with no threshold applied
    Or the decision should record no threshold value at all

  # Why: Only Rich-approved calibration adjustments shape gate decisions; unapproved proposals are invisible to the evaluator.
  @negative
  Scenario: An unapproved calibration adjustment does not influence gate decisions
    Given a calibration adjustment has been proposed but not yet approved by Rich
    When Forge retrieves calibration adjustments for a gate evaluation
    Then the unapproved adjustment should not be included in the retrieved set
    And the gate decision should not cite the unapproved adjustment as evidence

  # Why: Response payloads arriving with an unrecognised decision value must be rejected rather than silently coerced.
  @negative
  Scenario: An approval response with an unrecognised decision value is refused
    Given a build is paused awaiting Rich's decision
    When a response arrives carrying a decision value that is not one of the accepted options
    Then the response should be refused as invalid
    And the build should remain paused
    And the rejection should be surfaced so that a correctly-formed response can still be sent
━━ GROUP D: Edge Cases (8 scenarios) ━━


  # Why: Idempotency on the responder side — duplicates must not accidentally resume a build twice.
  # [ASSUMPTION: confidence=high] Responders deduplicate on request identifier; first response wins (API-nats-approval-protocol §6).
  @edge-case
  Scenario: Duplicate approval responses for the same request are ignored after the first
    Given a build has been paused and a response carrying a specific request identifier has already resumed it
    When a second response carrying the same request identifier arrives
    Then the second response should be ignored
    And the build should continue under the decision taken by the first response

  # Why: Crash recovery — if Forge restarts while paused, the request is re-emitted so Rich never sees a silent pipeline.
  @edge-case @regression
  Scenario: A build that was paused before a crash re-emits its approval request on restart
    Given a build was paused awaiting approval and Forge then stopped before a response arrived
    When Forge restarts and reloads the build from durable state
    Then the build should re-enter the paused state for that stage
    And the approval request for that stage should be re-published
    And responders should still treat the original request identifier as the idempotency key

  # Why: CLI steering — Rich can cancel a paused build from the command line; this is indistinguishable from a normal rejection to the graph.
  @edge-case
  Scenario: Cancelling a paused build from the command line behaves as a rejection
    Given a build is paused awaiting Rich's decision
    When Rich runs the cancel command for that build from the command line
    Then a synthetic rejection response should be injected for that paused stage
    And the cancellation reason should be recorded as coming from the command line
    And the build should transition to a cancelled outcome

  # Why: CLI steering — Rich can skip a single paused stage from the command line without approving the whole run.
  @edge-case
  Scenario: Skipping a paused build from the command line overrides the current stage only
    Given a build is paused awaiting Rich's decision
    When Rich runs the skip command for that build from the command line
    Then a synthetic override response should be injected for the paused stage
    And only that stage should be marked as overridden
    And the build should continue to the next stage

  # Why: The rehydration contract (DDR-002) must be invisible to callers — the same code works whether the response arrives typed or as a bare mapping.
  @edge-case
  Scenario Outline: Approval responses are handled identically whether they arrive typed or as a bare mapping
    Given a build is paused awaiting Rich's decision
    When a response for that build arrives as a <shape>
    Then the build should observe the response as a typed approval payload
    And the decision and responder fields should be safely readable before the build resumes

    Examples:
      | shape                          |
      | typed approval payload         |
      | bare mapping of equivalent content |

  # Why: Each build has its own approval channel; responses for one build must never be applied to another.
  @edge-case
  Scenario: Responses are routed only to the build whose identifier they match
    Given two builds are paused at the same time on different stages
    When a response arrives addressed to one build's approval channel
    Then only the matching build should resume
    And the other paused build should continue waiting for its own response

  # Why: Degraded mode is observable — if specialist scoring is unavailable, evaluations cannot silently land as auto-approve.
  @edge-case @regression
  Scenario: Degraded mode is recorded on every gate decision that lacks a Coach score
    Given a gated stage completes while specialist scoring is unavailable
    When Forge evaluates the gate for that stage
    Then the gate decision should be marked as degraded
    And the degraded-mode marker should appear on the stored decision for later review

  # Why: Timeout refreshes have a ceiling — once the total configured wait is reached, the pause cannot be extended indefinitely.
  # [ASSUMPTION: confidence=medium] Total wait is bounded by forge.yaml.approval.max_wait_seconds (implied by API-nats-approval-protocol §7 "refresh up to ≈ 3600"); behaviour at the ceiling is not explicitly described.
  @edge-case
  Scenario: A pause that reaches the configured maximum wait ends rather than refreshing forever
    Given a build has been paused awaiting Rich's decision for the full configured maximum wait
    When the wait ceiling is reached without a response
    Then the pause should end according to the configured fallback behaviour
    And the reason for ending should be recorded against the gate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 26 scenarios across 4 groups
Inferred assumptions: 4 surfaced inline (all trace back to the three context files; 3 high-confidence, 1 medium-confidence)
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

GROUP A — Key Examples (8 scenarios): _
GROUP B — Boundary Conditions (5 scenarios): _
GROUP C — Negative Cases (5 scenarios): _
GROUP D — Edge Cases (8 scenarios): _



Waiting for your curation input before Phase 4 (edge case expansion offer) and Phase 5 (assumption resolution).
forge-build-plan.md
A A A A
All four groups accepted. Moving to Phase 4.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGE CASE EXPANSION (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I've identified 6 additional scenarios covering security, concurrency, data integrity, and integration boundaries around the checkpoint protocol:

Security (2):

A response from an unexpected responder identity is not treated as authoritative
The constitutional PR-review rule is wired independently in both the system prompt and the gate evaluator, so disabling one does not bypass it
Concurrency (2):

Two responses arriving simultaneously for the same paused build are serialised so exactly one decision resumes the build
The outbound approval-request publish and the local pause transition are consistent — a pause is never left without a published request, and a published request never outlives a build that moved on
Data integrity (1):

A gate decision is durably recorded against the build even if the downstream notification publish fails, so history is never lost because a notifier was down
Integration boundaries (1):

The published approval request carries enough rendering context (stage label, rationale, findings, resume options) for a notification adapter to render the decision to Rich without needing to query Forge for more detail
Include them?


[Y] Yes — show and curate them (same A/R/M/+/? actions)
[S] Sample — show 2 full examples, then decide
[N] No — skip and proceed to Phase 5

Your choice [Y/S/N]: _
Y
━━ GROUP E: Security / Concurrency / Data Integrity / Integration (6 scenarios) ━━


  # Why: Responder identity matters — a response that does not come from a recognised responder must not silently resume a production build.
  @security @edge-case
  Scenario: A response from an unrecognised responder is not treated as authoritative
    Given a build is paused awaiting Rich's decision
    When a response arrives whose responder is not the expected approver for this deployment
    Then the response should not resume the build on its own
    And the anomaly should be surfaced for review
    And the build should continue to wait for a response from the expected approver

  # Why: Belt-and-braces (ADR-ARCH-026) — the PR-review rule lives in two places so that losing one does not silently re-enable auto-approval.
  @security @regression
  Scenario: The constitutional pull-request-review rule is enforced independently at two layers
    Given the system prompt carries the safety constitution that declares pull-request review is always human
    And the gate evaluator carries a hardcoded rule for pull-request targets
    When either of those two layers is disabled in isolation
    Then a pull-request-review stage should still be recorded as mandatory human approval
    And the loss of the other layer should be surfaced as a constitutional regression

  # Why: Concurrent responses must resolve to exactly one outcome — the first to land wins, the rest are observed duplicates.
  @concurrency @edge-case
  Scenario: Two responses arriving for the same paused build at the same time resolve to exactly one decision
    Given a build is paused awaiting Rich's decision
    When two responses for that build are accepted by the bus at effectively the same moment
    Then the build should resume under exactly one of the two decisions
    And the other response should be recorded as a duplicate without further effect

  # Why: The pause-and-publish pair must be consistent — a build must never be paused without a published request, and a published request must never outlive a build that moved on.
  @concurrency @data-integrity
  Scenario: Pausing and publishing the approval request are observed as a single consistent transition
    Given Forge is about to transition a build into the paused state for a gated stage
    When Forge performs the transition
    Then an approval request for that build should be observable on the bus before any status query reports the build as paused-without-request
    And when the build leaves the paused state, no further approval requests for that build's paused stage should be published

  # Why: Graphiti history is the traceability backbone — a transient publish failure must not erase the record of the decision that was made.
  @data-integrity @regression
  Scenario: A gate decision is recorded durably even if the notification publish fails
    Given Forge has evaluated a gated stage and produced a gate decision
    And the downstream notification publish is temporarily unavailable
    When Forge attempts to publish the notification and the publish fails
    Then the gate decision should still be recorded durably against the build
    And the failure of the notification publish should be surfaced for retry or investigation

  # Why: The approval request is the contract between Forge and any notification adapter — it must carry enough context to render without a secondary query.
  @integration
  Scenario: The approval request carries enough context for a notification adapter to render the decision unaided
    Given Forge has evaluated a gated stage as flag-for-review
    When Forge publishes the approval request for that stage
    Then the request should carry the build identifier, the feature identifier, and the stage label
    And the request should carry the Coach score, the criterion breakdown, and the detection findings that shaped the decision
    And the request should carry the reasoning-model rationale in plain language and the priors that informed it
    And the request should carry the resume options available to Rich and pointers to the artefacts under review
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATION: Group E (6 scenarios)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


[A] Accept  [R] Reject  [M] Modify  [+] Add  [?] Defer

GROUP E — Security / Concurrency / Data Integrity / Integration (6 scenarios): _
A
Group E accepted. Moving to Phase 5.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSUMPTION RESOLUTION (7 items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each assumption has a proposed default. Press Enter to accept, or type the actual value.


[1] Default initial approval-request wait time
    Proposed: 300 seconds
    Confidence: high
    Basis: API-nats-approval-protocol §3.1 — ApprovalRequestPayload.timeout_seconds default = 300
    Accept or enter value: _

[2] Maximum total approval wait before the pause cannot be refreshed any further
    Proposed: ~3600 seconds (forge.yaml.approval.max_wait_seconds)
    Confidence: high
    Basis: API-nats-approval-protocol §7 — "refresh up to forge.yaml.approval.max_wait_seconds ≈ 3600"
    Accept or enter value: _

[3] Behaviour when the total max-wait ceiling is reached without a response
    Proposed: The pause ends according to a configured fallback behaviour, with the reason recorded on the gate
    Confidence: medium
    Basis: API-nats-approval-protocol §7 states refresh behaviour up to ~3600s but does not describe the terminal action; inferred that a bounded-wait must have a terminal behaviour, with the specific resolution left to forge-pipeline-config
    Accept or enter value (options: "cancel build / treat as rejection", "escalate to mandatory human approval", "fail-open / auto-approve", other): _

[4] Constitutional-override target identifiers for the PR-review rule
    Proposed: "review_pr" and "create_pr_after_review"
    Confidence: high
    Basis: DM-gating §3 and API-nats-approval-protocol §8 — hardcoded branch for both identifiers
    Accept or enter value: _

[5] Synthetic CLI decisions injected when Rich cancels or skips a paused build
    Proposed: cancel → reject ("cli cancel"); skip → override ("cli skip")
    Confidence: high
    Basis: API-nats-approval-protocol §7 explicitly describes both mappings
    Accept or enter value: _

[6] Responder idempotency — first response wins, duplicates discarded
    Proposed: Deduplicate on request_id; short-TTL processed_request_ids set in responder memory
    Confidence: high
    Basis: API-nats-approval-protocol §6 — "Responders MUST be idempotent on request_id"
    Accept or enter value: _

[7] "Expected approver" for production builds (used by the unrecognised-responder scenario)
    Proposed: Rich, via the configured notification adapter responder identity (e.g. "rich" / jarvis adapter id)
    Confidence: medium
    Basis: API-nats-approval-protocol §4.1 — ApprovalResponsePayload.responder is free-form string ("rich" / jarvis adapter id); the allowlist semantics of "expected approver" are implied by constitutional framing, not explicitly enumerated
    Accept or enter value: _
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
accept defaults
All defaults accepted. Executing Phase 6 — writing output files.

Bash Create output directory
IN
mkdir -p /home/richardwoollcott/Projects/appmilla_github/forge/features/confidence-gated-checkpoint-protocol && ls /home/richardwoollcott/Projects/appmilla_github/forge/features/

OUT
confidence-gated-checkpoint-protocol
nats-fleet-integration
pipeline-state-machine-and-configuration
specialist-agent-delegation
Write confidence-gated-checkpoint-protocol.feature
330 lines
# Generated by /feature-spec
# Feature: Confidence-Gated Checkpoint Protocol (FEAT-FORGE-004)
# Stack: python
# Assumptions: 7 (see confidence-gated-checkpoint-protocol_assumptions.yaml)
# Generated: 2026-04-24T00:00:00Z

@feat-forge-004 @confidence-gated-checkpoint-protocol
Feature: Confidence-Gated Checkpoint Protocol
  As Rich, the sole operator of Forge
  I want each gated pipeline stage to be evaluated against the evidence from past builds,
    and for any stage Forge cannot confidently approve on its own to pause and ask me first
  So that safe work continues automatically, uncertain work waits for my review,
    unsafe work halts loudly, and every gating decision is traceable back to the priors
    and findings that produced it

  Background:
    Given Forge is configured from the project configuration file
    And Forge is connected to the fleet message bus
    And the specialist-agent delegation layer is able to return Coach scores and detection findings for gated stages

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP A — Key Examples
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Core happy path — when the evidence confidently supports the work, the pipeline continues without human engagement.
  @key-example @smoke
  Scenario: A confidently-supported stage is auto-approved and the build continues
    Given a gated stage has just completed with a strong Coach score, a clean set of detection findings, and priors that concur
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as auto-approve
    And the rationale, the priors consulted, and the findings considered should all be recorded on the decision
    And the build should continue to the next stage without pausing

  # Why: Core uncertain path — when the evidence is ambiguous, the build pauses and asks Rich to decide.
  @key-example @smoke
  Scenario: A stage with ambiguous evidence pauses and requests human review
    Given a gated stage has just completed with evidence that is neither clearly positive nor clearly negative
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as flag-for-review
    And the build should enter the paused state
    And an approval request should be published for human attention on the build-specific approval channel
    And the request should carry the stage label, the Coach score, the criterion breakdown, the detection findings, the rationale, the evidence priors, and the resume options available to Rich

  # Why: Core unsafe path — clearly bad work halts the build rather than burning more turns.
  @key-example @smoke
  Scenario: A stage with strongly negative evidence halts the build
    Given a gated stage has just completed with a poor Coach score or strongly negative detection findings
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as hard-stop
    And the build should transition to a failed outcome
    And the reasons for halting should be recorded on the decision for later review

  # Why: Resume on approval — Rich confirms the paused work and the build continues where it left off.
  @key-example
  Scenario: Approval from Rich resumes the paused build
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with an approval decision
    Then the build should resume from the point at which it paused
    And the decision and responder should be recorded against the gate

  # Why: Reject on decision — Rich refuses the paused work; the build ends cleanly.
  @key-example
  Scenario: Rejection from Rich ends the paused build
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with a rejection decision
    Then the build should transition to a cancelled outcome
    And the reason Rich gave for rejecting should be recorded against the gate

  # Why: Override/skip — Rich waves through the specific stage without endorsing the general criteria.
  @key-example
  Scenario: Override from Rich skips the stage and the build continues
    Given a build is paused on a gated stage awaiting Rich's decision
    When Rich responds to the pending approval request with an override decision
    Then the specific gated stage should be marked as overridden
    And the build should continue to the next stage
    And the override and its reason should be recorded against the gate

  # Why: Constitutional rule — PR review is always human, no matter how strong the evidence.
  @key-example @smoke
  Scenario: A pull-request-review stage always requires human approval regardless of evidence
    Given a pull-request-review stage has just completed with evidence that would otherwise warrant auto-approval
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as mandatory human approval
    And the decision should be marked as a constitutional override
    And the build should pause and request Rich's decision before any pull request is created or merged

  # Why: Evidence-driven gating — each decision carries a written trail back to the priors and findings that shaped it.
  @key-example
  Scenario: Every gate decision records its rationale, priors, and findings
    Given a gated stage has completed and Forge has evaluated the gate
    When I inspect the recorded gate decision for that stage
    Then the decision should identify the stage, the target being gated, and the mode chosen
    And the decision should include the reasoning-model rationale in plain language
    And the decision should list the priors that informed it and the detection findings considered
    And the decision should record when it was made

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP B — Boundary Conditions
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Degraded mode — no Coach score means auto-approval is off the table (invariant from DM-gating §6).
  @boundary @negative
  Scenario: A gated stage with no Coach score cannot be auto-approved
    Given a gated stage has completed but no Coach score is available for it
    When Forge evaluates the gate for that stage
    Then the gate decision should be recorded as degraded
    And the decision mode must not be auto-approve
    And the decision mode should be one of flag-for-review, hard-stop, or mandatory human approval

  # Why: Just-inside boundary for the initial approval wait.
  # [ASSUMPTION: confidence=high] Default approval request timeout is 300 seconds (API-nats-approval-protocol §3.1).
  @boundary
  Scenario: An approval request is published with the default wait time when none is specified
    Given Forge has evaluated a stage as flag-for-review
    When Forge publishes the approval request for that stage
    Then the request should carry the default wait time of 300 seconds
    And the build should remain paused until Rich responds or the wait time elapses

  # Why: Just-outside boundary — wait exceeds the per-request default but stays within the configured maximum.
  # [ASSUMPTION: confidence=high] Maximum total wait is approximately 3600 seconds via forge.yaml.approval.max_wait_seconds (API-nats-approval-protocol §7).
  @boundary
  Scenario: An approval that is not answered within the default wait time is refreshed without cancelling the pause
    Given a build has been paused and the default wait time has elapsed without a response
    When Forge checks the pending request
    Then Forge should publish a fresh approval request carrying an incremented attempt count
    And the build should remain paused and continue waiting for Rich
    And the prior request identifier should remain valid for deduplication

  # Why: Criterion-breakdown boundary (invariant from DM-gating §6).
  @boundary
  Scenario Outline: Criterion-breakdown values at the permitted extremes are accepted
    Given a gated stage completes with a criterion score of <value> for one criterion
    When Forge evaluates the gate
    Then the gate decision should be recorded without any validation error against that criterion

    Examples:
      | value |
      | 0.0   |
      | 1.0   |

  # Why: Criterion-breakdown outside the permitted range is a malformed input and must be refused.
  @boundary @negative
  Scenario: Criterion-breakdown values outside the permitted range are refused
    Given a gated stage completes with a criterion score outside the range zero to one
    When Forge attempts to evaluate the gate
    Then the input should be refused as invalid
    And no gate decision should be recorded

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP C — Negative Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Detection findings must be able to escalate even when the headline score is strong.
  @negative
  Scenario: A critical detection finding escalates a stage away from auto-approval regardless of a strong score
    Given a gated stage has completed with a strong Coach score
    And at least one detection finding on that stage is marked as critical severity
    When Forge evaluates the gate
    Then the decision mode must not be auto-approve
    And the critical finding should be listed on the decision as part of the rationale

  # Why: Constitutional guardrail — executor-layer belt-and-braces for PR creation, not only PR review.
  @negative
  Scenario: Creating a pull request after review is treated with the same constitutional rule as reviewing one
    Given a stage that creates a pull request after review has completed with evidence that would otherwise warrant auto-approval
    When Forge evaluates the gate
    Then the gate decision should be recorded as mandatory human approval
    And the decision should be marked as a constitutional override

  # Why: Invariant — when the mode is mandatory human approval, auto-approve override must be recorded or no threshold applied.
  @negative
  Scenario: A mandatory-human-approval decision must not masquerade as a threshold-based approval
    Given Forge is recording a gate decision whose mode is mandatory human approval
    When the decision is persisted
    Then the decision should either be marked as a constitutional override with no threshold applied
    Or the decision should record no threshold value at all

  # Why: Only Rich-approved calibration adjustments shape gate decisions; unapproved proposals are invisible to the evaluator.
  @negative
  Scenario: An unapproved calibration adjustment does not influence gate decisions
    Given a calibration adjustment has been proposed but not yet approved by Rich
    When Forge retrieves calibration adjustments for a gate evaluation
    Then the unapproved adjustment should not be included in the retrieved set
    And the gate decision should not cite the unapproved adjustment as evidence

  # Why: Response payloads arriving with an unrecognised decision value must be rejected rather than silently coerced.
  @negative
  Scenario: An approval response with an unrecognised decision value is refused
    Given a build is paused awaiting Rich's decision
    When a response arrives carrying a decision value that is not one of the accepted options
    Then the response should be refused as invalid
    And the build should remain paused
    And the rejection should be surfaced so that a correctly-formed response can still be sent

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP D — Edge Cases
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Idempotency on the responder side — duplicates must not accidentally resume a build twice.
  # [ASSUMPTION: confidence=high] Responders deduplicate on request identifier; first response wins (API-nats-approval-protocol §6).
  @edge-case
  Scenario: Duplicate approval responses for the same request are ignored after the first
    Given a build has been paused and a response carrying a specific request identifier has already resumed it
    When a second response carrying the same request identifier arrives
    Then the second response should be ignored
    And the build should continue under the decision taken by the first response

  # Why: Crash recovery — if Forge restarts while paused, the request is re-emitted so Rich never sees a silent pipeline.
  @edge-case @regression
  Scenario: A build that was paused before a crash re-emits its approval request on restart
    Given a build was paused awaiting approval and Forge then stopped before a response arrived
    When Forge restarts and reloads the build from durable state
    Then the build should re-enter the paused state for that stage
    And the approval request for that stage should be re-published
    And responders should still treat the original request identifier as the idempotency key

  # Why: CLI steering — Rich can cancel a paused build from the command line; this is indistinguishable from a normal rejection to the graph.
  @edge-case
  Scenario: Cancelling a paused build from the command line behaves as a rejection
    Given a build is paused awaiting Rich's decision
    When Rich runs the cancel command for that build from the command line
    Then a synthetic rejection response should be injected for that paused stage
    And the cancellation reason should be recorded as coming from the command line
    And the build should transition to a cancelled outcome

  # Why: CLI steering — Rich can skip a single paused stage from the command line without approving the whole run.
  @edge-case
  Scenario: Skipping a paused build from the command line overrides the current stage only
    Given a build is paused awaiting Rich's decision
    When Rich runs the skip command for that build from the command line
    Then a synthetic override response should be injected for the paused stage
    And only that stage should be marked as overridden
    And the build should continue to the next stage

  # Why: The rehydration contract (DDR-002) must be invisible to callers — the same code works whether the response arrives typed or as a bare mapping.
  @edge-case
  Scenario Outline: Approval responses are handled identically whether they arrive typed or as a bare mapping
    Given a build is paused awaiting Rich's decision
    When a response for that build arrives as a <shape>
    Then the build should observe the response as a typed approval payload
    And the decision and responder fields should be safely readable before the build resumes

    Examples:
      | shape                              |
      | typed approval payload             |
      | bare mapping of equivalent content |

  # Why: Each build has its own approval channel; responses for one build must never be applied to another.
  @edge-case
  Scenario: Responses are routed only to the build whose identifier they match
    Given two builds are paused at the same time on different stages
    When a response arrives addressed to one build's approval channel
    Then only the matching build should resume
    And the other paused build should continue waiting for its own response

  # Why: Degraded mode is observable — if specialist scoring is unavailable, evaluations cannot silently land as auto-approve.
  @edge-case @regression
  Scenario: Degraded mode is recorded on every gate decision that lacks a Coach score
    Given a gated stage completes while specialist scoring is unavailable
    When Forge evaluates the gate for that stage
    Then the gate decision should be marked as degraded
    And the degraded-mode marker should appear on the stored decision for later review

  # Why: Timeout refreshes have a ceiling — once the total configured wait is reached, the pause cannot be extended indefinitely.
  # [ASSUMPTION: confidence=medium] Total wait is bounded by forge.yaml.approval.max_wait_seconds (implied by API-nats-approval-protocol §7 "refresh up to ≈ 3600"); behaviour at the ceiling is not explicitly described.
  @edge-case
  Scenario: A pause that reaches the configured maximum wait ends rather than refreshing forever
    Given a build has been paused awaiting Rich's decision for the full configured maximum wait
    When the wait ceiling is reached without a response
    Then the pause should end according to the configured fallback behaviour
    And the reason for ending should be recorded against the gate

  # ─────────────────────────────────────────────────────────────────────────
  # GROUP E — Security / Concurrency / Data Integrity / Integration
  # ─────────────────────────────────────────────────────────────────────────

  # Why: Responder identity matters — a response that does not come from a recognised responder must not silently resume a production build.
  # [ASSUMPTION: confidence=medium] The "expected approver" allowlist is implied by the constitutional framing; the specific responder value is configured per deployment (e.g. "rich" or the Jarvis adapter identity).
  @security @edge-case
  Scenario: A response from an unrecognised responder is not treated as authoritative
    Given a build is paused awaiting Rich's decision
    When a response arrives whose responder is not the expected approver for this deployment
    Then the response should not resume the build on its own
    And the anomaly should be surfaced for review
    And the build should continue to wait for a response from the expected approver

  # Why: Belt-and-braces (ADR-ARCH-026) — the PR-review rule lives in two places so that losing one does not silently re-enable auto-approval.
  @security @regression
  Scenario: The constitutional pull-request-review rule is enforced independently at two layers
    Given the system prompt carries the safety constitution that declares pull-request review is always human
    And the gate evaluator carries a hardcoded rule for pull-request targets
    When either of those two layers is disabled in isolation
    Then a pull-request-review stage should still be recorded as mandatory human approval
    And the loss of the other layer should be surfaced as a constitutional regression

  # Why: Concurrent responses must resolve to exactly one outcome — the first to land wins, the rest are observed duplicates.
  @concurrency @edge-case
  Scenario: Two responses arriving for the same paused build at the same time resolve to exactly one decision
    Given a build is paused awaiting Rich's decision
    When two responses for that build are accepted by the bus at effectively the same moment
    Then the build should resume under exactly one of the two decisions
    And the other response should be recorded as a duplicate without further effect

  # Why: The pause-and-publish pair must be consistent — a build must never be paused without a published request, and a published request must never outlive a build that moved on.
  @concurrency @data-integrity
  Scenario: Pausing and publishing the approval request are observed as a single consistent transition
    Given Forge is about to transition a build into the paused state for a gated stage
    When Forge performs the transition
    Then an approval request for that build should be observable on the bus before any status query reports the build as paused-without-request
    And when the build leaves the paused state, no further approval requests for that build's paused stage should be published

  # Why: Graphiti history is the traceability backbone — a transient publish failure must not erase the record of the decision that was made.
  @data-integrity @regression
  Scenario: A gate decision is recorded durably even if the notification publish fails
    Given Forge has evaluated a gated stage and produced a gate decision
    And the downstream notification publish is temporarily unavailable
    When Forge attempts to publish the notification and the publish fails
    Then the gate decision should still be recorded durably against the build
    And the failure of the notification publish should be surfaced for retry or investigation

  # Why: The approval request is the contract between Forge and any notification adapter — it must carry enough context to render without a secondary query.
  @integration
  Scenario: The approval request carries enough context for a notification adapter to render the decision unaided
    Given Forge has evaluated a gated stage as flag-for-review
    When Forge publishes the approval request for that stage
    Then the request should carry the build identifier, the feature identifier, and the stage label
    And the request should carry the Coach score, the criterion breakdown, and the detection findings that shaped the decision
    And the request should carry the reasoning-model rationale in plain language and the priors that informed it
    And the request should carry the resume options available to Rich and pointers to the artefacts under review
Write confidence-gated-checkpoint-protocol_assumptions.yaml
59 lines
# Assumptions manifest for confidence-gated-checkpoint-protocol.feature
# Generated by /feature-spec

feature: "Confidence-Gated Checkpoint Protocol"
feature_id: "FEAT-FORGE-004"
generated: "2026-04-24T00:00:00Z"
stack: "python"
review_required: false  # 5 high-confidence + 2 medium-confidence assumptions, all grounded in supplied context files

assumptions:
  - id: "ASSUM-001"
    scenario: "An approval request is published with the default wait time when none is specified"
    assumption: "The default initial approval-request wait time is 300 seconds"
    confidence: high
    basis: "API-nats-approval-protocol §3.1 — ApprovalRequestPayload.timeout_seconds default = 300"
    human_response: "confirmed"

  - id: "ASSUM-002"
    scenario: "An approval that is not answered within the default wait time is refreshed without cancelling the pause"
    assumption: "The maximum total approval wait before the pause cannot be refreshed any further is approximately 3600 seconds (forge.yaml.approval.max_wait_seconds)"
    confidence: high
    basis: "API-nats-approval-protocol §7 — 'refresh up to forge.yaml.approval.max_wait_seconds ≈ 3600'"
    human_response: "confirmed"

  - id: "ASSUM-003"
    scenario: "A pause that reaches the configured maximum wait ends rather than refreshing forever"
    assumption: "When the total max-wait ceiling is reached without a response, the pause ends according to a configured fallback behaviour, with the reason recorded on the gate. The specific fallback (cancel / escalate / fail-open) is a configuration choice deferred to forge-pipeline-config."
    confidence: medium
    basis: "API-nats-approval-protocol §7 describes refresh behaviour up to ~3600s but does not describe the terminal action; inferred that a bounded-wait must have a terminal behaviour."
    human_response: "confirmed"

  - id: "ASSUM-004"
    scenario: "A pull-request-review stage always requires human approval regardless of evidence; Creating a pull request after review is treated with the same constitutional rule"
    assumption: "The constitutional-override target identifiers for the PR-review rule are exactly 'review_pr' and 'create_pr_after_review'"
    confidence: high
    basis: "DM-gating §3 and API-nats-approval-protocol §8 — hardcoded branch for both identifiers"
    human_response: "confirmed"

  - id: "ASSUM-005"
    scenario: "Cancelling a paused build from the command line behaves as a rejection; Skipping a paused build from the command line overrides the current stage only"
    assumption: "Synthetic CLI decisions injected when Rich cancels or skips a paused build map as cancel → reject (reason 'cli cancel'); skip → override (reason 'cli skip')"
    confidence: high
    basis: "API-nats-approval-protocol §7 explicitly describes both mappings"
    human_response: "confirmed"

  - id: "ASSUM-006"
    scenario: "Duplicate approval responses for the same request are ignored after the first; Two responses arriving for the same paused build at the same time resolve to exactly one decision"
    assumption: "Responder idempotency is enforced on the request identifier; the first response wins and duplicates are discarded via a short-TTL processed_request_ids set in responder memory"
    confidence: high
    basis: "API-nats-approval-protocol §6 — 'Responders MUST be idempotent on request_id. Rich's client / Jarvis adapter enforces this via a short-TTL processed_request_ids set in memory.'"
    human_response: "confirmed"

  - id: "ASSUM-007"
    scenario: "A response from an unrecognised responder is not treated as authoritative"
    assumption: "The 'expected approver' for production builds is Rich, identified via the configured notification adapter responder identity (e.g. 'rich' or the Jarvis adapter id); responses from other responders are surfaced as anomalies and do not resume the build on their own"
    confidence: medium
    basis: "API-nats-approval-protocol §4.1 — ApprovalResponsePayload.responder is a free-form string with examples 'rich' / 'jarvis adapter id'. The allowlist semantics of 'expected approver' are implied by the constitutional framing but not explicitly enumerated."
    human_response: "confirmed"
Write confidence-gated-checkpoint-protocol_summary.md
93 lines
# Feature Spec Summary: Confidence-Gated Checkpoint Protocol

**Feature ID**: FEAT-FORGE-004
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 32 total (4 smoke, 4 regression)
**Assumptions**: 7 total (5 high / 2 medium / 0 low confidence)
**Review required**: No — all assumptions traceable to supplied context files

## Scope

Specifies Forge's confidence-gated checkpoint protocol: how each gated pipeline stage is
evaluated against Coach scores, detection findings, retrieved priors, and Rich-approved
calibration adjustments to produce one of four gate modes — auto-approve, flag-for-review
(paused state awaiting Rich), hard-stop, or mandatory human approval. Covers the
approval request/response round-trip across the build-specific approval channel
(idempotent on request identifier, bounded-wait with refresh, consistent with the
paused-state transition), the constitutional belt-and-braces rule that forces PR-review
and PR-create stages to mandatory human approval regardless of score, degraded-mode
behaviour when specialist scoring is unavailable, the resume-value rehydration contract
that hides direct-invoke vs. server-mode serde differences from callers, and CLI steering
(`forge cancel` → synthetic reject, `forge skip` → synthetic override). Decisions are
written durably even when downstream notification publishes fail, and each decision
records the rationale, priors consulted, and findings considered. Behaviour is described
in domain terms — the reasoning-model-driven evaluation and NATS transport surface as
capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 7 |
| Edge cases (@edge-case) | 10 |
| Smoke (@smoke) | 4 |
| Regression (@regression) | 4 |
| Security (@security) | 2 |
| Concurrency (@concurrency) | 2 |
| Data integrity (@data-integrity) | 2 |
| Integration (@integration) | 1 |

Note: several scenarios carry multiple tags (e.g. boundary + negative, edge-case +
regression, security + edge-case). Group totals do not sum to 32.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — auto-approve, flag-for-review, hard-stop, approve/reject/override resume paths, constitutional PR-review rule, decision traceability | 8 |
| B | Boundary Conditions — degraded mode, default wait time, refresh within max wait, criterion-score extremes accepted and rejected | 5 |
| C | Negative Cases — critical finding escalation, PR-create constitutional branch, mandatory-approval threshold invariant, unapproved calibration adjustment filtered, unknown decision value refused | 5 |
| D | Edge Cases — idempotent duplicate response, crash-recovery re-emit, CLI cancel/skip synthetic decisions, typed-vs-dict rehydration, per-build response routing, degraded-mode marker, max-wait ceiling | 8 |
| E | Security / Concurrency / Data Integrity / Integration — unrecognised responder, two-layer constitutional guard, simultaneous responses, pause-and-publish consistency, durable decision on publish failure, adapter-renderable approval request | 6 |

## Deferred Items

None.

## Assumptions

- **ASSUM-001** (high) — default initial approval-request wait time 300 seconds (API-nats-approval-protocol §3.1)
- **ASSUM-002** (high) — maximum total approval wait ≈ 3600 seconds via `forge.yaml.approval.max_wait_seconds` (API-nats-approval-protocol §7)
- **ASSUM-003** (medium) — behaviour at the max-wait ceiling ends the pause under a configured fallback; specific fallback deferred to `forge-pipeline-config`
- **ASSUM-004** (high) — constitutional-override target identifiers are `review_pr` and `create_pr_after_review` (DM-gating §3, API-nats-approval-protocol §8)
- **ASSUM-005** (high) — CLI steering mapping: cancel → reject (`cli cancel`), skip → override (`cli skip`) (API-nats-approval-protocol §7)
- **ASSUM-006** (high) — responder idempotency on request identifier, first response wins, short-TTL dedup set (API-nats-approval-protocol §6)
- **ASSUM-007** (medium) — "expected approver" identity configured per deployment (e.g. `rich` / Jarvis adapter id); allowlist semantics implied by constitutional framing (API-nats-approval-protocol §4.1)

## Upstream Dependencies

- **FEAT-FORGE-003** — Specialist Agent Delegation. This feature consumes Coach scores,
  criterion breakdowns, and detection findings produced by the specialist delegation
  layer; gating logic here presupposes those results are available (or explicitly absent
  in degraded mode).
- **FEAT-FORGE-002** — NATS Fleet Integration. The build-specific approval channel
  (`agents.approval.forge.{build_id}` and its `.response` mirror) rides on the fleet
  message bus established by FEAT-FORGE-002.
- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The paused-state
  transition, crash-recovery re-emission, and durable recording of each gate decision
  against the build all ride on the state machine and SQLite substrate from
  FEAT-FORGE-001.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Confidence-Gated Checkpoint Protocol" \
      --context features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
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
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 