# Forge Build Plan — Pipeline Orchestrator & Checkpoint Manager

## Status: Ready for `/system-arch` (after specialist-agent Phase 3 + NATS infrastructure tested)
## Repo: `guardkit/forge`
## Agent ID: `forge`
## Target: Post specialist-agent Phase 3 completion
## Depends On: nats-core (✅), nats-infrastructure (✅ configured, ◻ running), specialist-agent Phase 3 (◻)

---

## Purpose

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

- [x] **nats-core library implemented** — 97% test coverage, all event payloads,
      NATSClient, NATSKVManifestRegistry, Topics, AgentConfig, AgentManifest
- [ ] **nats-infrastructure running on GB10** — NATS server up, JetStream enabled,
      accounts configured, `docker compose up -d` executed and verified
- [ ] **nats-core integration tests passing** — tests against live NATS server on GB10,
      validates MessageEnvelope round-trip, KV registry operations, pub/sub lifecycle
- [ ] **specialist-agent Phase 3 complete** — NATS fleet integration: agents register
      via `client.register_agent(manifest)`, respond to `agents.command.*`, return
      `ResultPayload` with Coach scores. At minimum, the architect role must be
      NATS-callable.
- [ ] **At least one specialist agent NATS-callable** — verified end-to-end: Forge can
      call `call_agent_tool()` on architect-agent, receive `ResultPayload` with
      `coach_score`, `criterion_breakdown`, `detection_findings` in the result dict

### Soft Prerequisites (valuable but not blocking)

- [ ] **specialist-agent Phase 1B complete** — unified harness with `--role` flag. If
      not ready, Forge can delegate to architect role only (single-role degraded mode)
- [ ] **specialist-agent Phase G complete** — Graphiti runtime. If not ready, Forge
      skips `graphiti_seed` steps (knowledge doesn't compound but pipeline still works)
- [ ] **specialist-agent Phase F complete** — fine-tuned models on Bedrock. If not
      ready, specialist agents use base models (lower quality but pipeline still works)

### Context Documents Available

These documents will be used as `--context` inputs during the build:

| Document | Path | Used In |
|----------|------|---------|
| Pipeline orchestrator refresh v3 | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | /system-arch |
| Original pipeline motivation | `forge/docs/research/pipeline-orchestrator-motivation.md` | /system-arch |
| Original conversation starter | `forge/docs/research/pipeline-orchestrator-conversation-starter.md` | /system-arch |
| Fleet master index v2 | `forge/docs/research/ideas/fleet-master-index.md` | /system-arch |
| Specialist agent vision | `specialist-agent/docs/research/ideas/architect-agent-vision.md` | /system-arch |
| nats-core system spec | `nats-core/docs/design/specs/nats-core-system-spec.md` | /system-arch, /system-design |
| Dev pipeline architecture | Project knowledge: `dev-pipeline-architecture.md` | /system-arch |
| Dev pipeline system spec | Project knowledge: `dev-pipeline-system-spec.md` | /system-arch |
| Agent manifest contract | `nats-core/docs/design/contracts/agent-manifest-contract.md` | /system-design |
| Forge pipeline config example | (to be produced by /system-arch) | /system-design, /feature-spec |

---

## Feature Summary

| # | Feature | Depends On | Est. Duration | Description |
|---|---------|-----------|---------------|-------------|
| FEAT-FORGE-001 | Pipeline State Machine & Configuration | — | 2-3 days | Core state machine (IDLE→PLANNING→DELEGATING→BUILDING→VERIFYING→COMPLETE), project config loading (`forge-pipeline-config.yaml`), session persistence via NATS KV, crash recovery, three mode dispatch (greenfield/feature/review-fix) |
| FEAT-FORGE-002 | NATS Fleet Integration | 001 | 2-3 days | Fleet registration (`AgentManifest` for Forge), heartbeat publishing, agent discovery via `NATSKVManifestRegistry`, degraded mode detection (specialist unavailable → forced FLAG FOR REVIEW), pipeline event publishing using nats-core payloads |
| FEAT-FORGE-003 | Specialist Agent Delegation | 002 | 2-3 days | `call_agent_tool()` for product-owner and architect roles, result parsing (Coach score + criterion breakdown + detection findings from `ResultPayload.result` dict), timeout handling, retry with additional context on failure |
| FEAT-FORGE-004 | Confidence-Gated Checkpoint Protocol | 003 | 2-3 days | Score evaluation against per-stage thresholds, critical detection pattern override, 🟢 auto-approve (`NotificationPayload`), 🟡 flag for review (`ApprovalRequestPayload`), 🔴 hard stop, await human response (`ApprovalResponsePayload`), per-stage + per-project threshold configuration |
| FEAT-FORGE-005 | GuardKit Command Invocation Engine | 001 | 2-3 days | Subprocess calls to `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review`. Context flag construction from pipeline state. Output capture and artifact path tracking. Error handling and retry. |
| FEAT-FORGE-006 | Infrastructure Coordination | 001, 002 | 2-3 days | Graphiti seeding after each pipeline stage (`graphiti_seed`), Graphiti querying for cross-project context, test verification (`verify`), git operations (branch creation, commit, push, PR via `gh`), CI trigger |
| FEAT-FORGE-007 | Mode A Greenfield End-to-End | 003, 004, 005, 006 | 3-5 days | Full integration: raw input → delegate to PO agent → checkpoint → delegate to architect → checkpoint → /system-arch → /system-design → /feature-spec × N → /feature-plan × N → autobuild × N → verify → git/PR → hard checkpoint (PR review). The primary pipeline mode. |
| FEAT-FORGE-008 | Mode B Feature & Mode C Review-Fix | 007 | 2-3 days | Mode B: add feature to existing project (skip PO/architect delegation, start from /feature-spec). Mode C: review and fix issues (/task-review → /task-work cycle). Both use checkpoint protocol. |

**Estimated total: 4-6 weeks** (includes iteration time, integration testing, and the
inevitable debugging of subprocess orchestration + async NATS patterns)

---

## GuardKit Command Sequence

### Step 1: /system-arch

Produces the Forge's system architecture — ARCHITECTURE.md, ADRs, C4 diagrams,
component boundaries.

```bash
cd ~/Projects/appmilla_github/forge

guardkit system-arch \
  --context forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md \
  --context forge/docs/research/pipeline-orchestrator-conversation-starter.md \
  --context forge/docs/research/pipeline-orchestrator-motivation.md \
  --context forge/docs/research/ideas/fleet-master-index.md \
  --context specialist-agent/docs/research/ideas/architect-agent-vision.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md
```

**Expected outputs:**
- `forge/docs/architecture/ARCHITECTURE.md`
- `forge/docs/architecture/c4-*.svg` (context, container, component diagrams)
- `forge/docs/decisions/ADR-FORGE-001-*.md` (likely: checkpoint protocol, delegation model, state persistence)

**Validation:**
- Architecture captures all three modes (greenfield, feature, review-fix)
- Confidence-gated checkpoint protocol is a first-class architectural component
- Specialist agent delegation via NATS `call_agent_tool()` is clearly bounded
- GuardKit command invocation is subprocess-based, not in-process
- Degraded mode is documented as a structural capability
- Pipeline event publishing uses nats-core payloads (no new types)
- State persistence uses NATS KV (not filesystem)

**⚠️ After this step:** Update `--context` flags in Steps 2-5 below to reference the
actual files produced. See "Post-Architecture Update Instructions" at the end of this
document.

### Step 2: /system-design

Produces detailed design — component interactions, data models, API contracts,
sequence diagrams.

```bash
guardkit system-design \
  --context forge/docs/architecture/ARCHITECTURE.md \
  --context <ADR files produced by Step 1> \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md \
  --context forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md
```

**Expected outputs:**
- `forge/docs/design/DESIGN.md`
- `forge/docs/design/specs/forge-system-spec.md` (features, BDD acceptance criteria)
- `forge/docs/design/contracts/` (pipeline config schema, checkpoint protocol contract)

**Validation:**
- System spec contains BDD acceptance criteria for all 8 features
- Pipeline config schema (`forge-pipeline-config.yaml`) is fully specified with
  per-stage thresholds, critical detections, reviewer assignment, escalation channels
- Checkpoint protocol contract specifies exact NATS topic patterns and payload mappings
- Sequence diagrams for Mode A greenfield show the full delegation → checkpoint →
  invoke → verify → PR flow
- GuardKit command invocation contract specifies subprocess interface, environment
  variables, output file discovery
- State machine transitions are formally defined with valid/invalid transitions

**⚠️ After this step:** Update `--context` flags in Steps 3-5 below. See "Post-
Architecture Update Instructions."

### Step 3: /feature-spec × 8

Produces BDD feature specifications for each feature. Run sequentially — later features
reference earlier ones.

```bash
# FEAT-FORGE-001: Pipeline State Machine & Configuration
guardkit feature-spec FEAT-FORGE-001 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context <pipeline config schema from Step 2>

# FEAT-FORGE-002: NATS Fleet Integration
guardkit feature-spec FEAT-FORGE-002 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md

# FEAT-FORGE-003: Specialist Agent Delegation
guardkit feature-spec FEAT-FORGE-003 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context <checkpoint protocol contract from Step 2>

# FEAT-FORGE-005: GuardKit Command Invocation Engine
guardkit feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md

# FEAT-FORGE-006: Infrastructure Coordination
guardkit feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md

# FEAT-FORGE-007: Mode A Greenfield End-to-End
guardkit feature-spec FEAT-FORGE-007 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context <all previous feature spec files>

# FEAT-FORGE-008: Mode B Feature & Mode C Review-Fix
guardkit feature-spec FEAT-FORGE-008 \
  --context forge/docs/design/DESIGN.md \
  --context forge/docs/design/specs/forge-system-spec.md \
  --context <FEAT-FORGE-007 feature spec>
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
respected.

```bash
# Run in dependency order:
guardkit feature-plan FEAT-FORGE-001  # no deps
guardkit feature-plan FEAT-FORGE-002  # depends on 001
guardkit feature-plan FEAT-FORGE-005  # depends on 001 (can parallel with 002)
guardkit feature-plan FEAT-FORGE-003  # depends on 002
guardkit feature-plan FEAT-FORGE-004  # depends on 003
guardkit feature-plan FEAT-FORGE-006  # depends on 001, 002
guardkit feature-plan FEAT-FORGE-007  # depends on 003, 004, 005, 006
guardkit feature-plan FEAT-FORGE-008  # depends on 007
```

**Validation:**
- Task wave structure respects feature dependencies
- Each task has clear inputs, outputs, and acceptance criteria
- Integration tasks (FEAT-FORGE-007) are in later waves

### Step 5: Build (autobuild × 8)

Build features in dependency order. Run sequentially on GB10 (or Bedrock when available).

```bash
# Wave 1: Foundation (can parallel)
guardkit autobuild FEAT-FORGE-001
guardkit autobuild FEAT-FORGE-005

# Wave 2: NATS integration (depends on Wave 1)
guardkit autobuild FEAT-FORGE-002

# Wave 3: Delegation & coordination (depends on Wave 2)
guardkit autobuild FEAT-FORGE-003
guardkit autobuild FEAT-FORGE-006

# Wave 4: Checkpoint protocol (depends on Wave 3)
guardkit autobuild FEAT-FORGE-004

# Wave 5: End-to-end integration (depends on all above)
guardkit autobuild FEAT-FORGE-007

# Wave 6: Additional modes (depends on Wave 5)
guardkit autobuild FEAT-FORGE-008
```

### Step 6: Validation

After all features are built:

```bash
# Run full test suite
cd ~/Projects/appmilla_github/forge
pytest

# Integration test: Mode A greenfield on a test project
# (use a small test corpus, not FinProxy — save FinProxy for the real run)
python -m forge.cli greenfield \
  --project test-project \
  --docs ./test-docs \
  --config ./forge-pipeline-config.yaml

# Verify pipeline events published to NATS
# (subscribe to pipeline.> on GB10 and observe)

# Verify checkpoint protocol
# (set low auto_threshold to force FLAG FOR REVIEW, verify ApprovalRequestPayload arrives)

# Verify degraded mode
# (stop specialist agents, run pipeline, verify forced FLAG FOR REVIEW)
```

### Step 7: First Real Run — FinProxy

Once validation passes, run the Forge on FinProxy as the first real pipeline:

```bash
python -m forge.cli greenfield \
  --project finproxy \
  --docs ~/Projects/appmilla_github/finproxy-docs \
  --config ./configs/finproxy-pipeline-config.yaml \
  --scope "Phase 1 — MoneyHub Open Banking integration"
```

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
| `src/forge/commands/invoker.py` | 005 | Create — subprocess GuardKit command execution |
| `src/forge/commands/context.py` | 005 | Create — --context flag construction from pipeline state |
| `src/forge/commands/artifacts.py` | 005 | Create — output file discovery and tracking |
| `src/forge/coordination/graphiti.py` | 006 | Create — seed outputs into knowledge graph |
| `src/forge/coordination/git.py` | 006 | Create — branch, commit, push, PR |
| `src/forge/coordination/verify.py` | 006 | Create — test runner, integration checks |
| `src/forge/modes/greenfield.py` | 007 | Create — Mode A full pipeline orchestration |
| `src/forge/modes/feature.py` | 008 | Create — Mode B add feature |
| `src/forge/modes/review_fix.py` | 008 | Create — Mode C review and fix |
| `src/forge/manifest.py` | 002 | Create — Forge AgentManifest (imports from nats-core) |
| `forge-pipeline-config.yaml.example` | 001 | Create — example config with FinProxy thresholds |
| `configs/finproxy-pipeline-config.yaml` | 007 | Create — FinProxy-specific pipeline config |
| `pyproject.toml` | 001 | Create — dependencies: nats-core, pydantic, pydantic-settings |
| `tests/` | all | Create — test files per feature |
| `docs/architecture/ARCHITECTURE.md` | /system-arch | Create |
| `docs/design/DESIGN.md` | /system-design | Create |
| `docs/design/specs/forge-system-spec.md` | /system-design | Create |
| `command-history.md` | all | Create — running log of all commands run |

---

## Expected Timeline

| Phase | Duration | What |
|-------|----------|------|
| /system-arch | 1 session | Architecture, C4 diagrams, ADRs |
| /system-design | 1 session | Detailed design, system spec, contracts |
| /feature-spec × 8 | 2-3 sessions | BDD specs for all features |
| /feature-plan × 8 | 1-2 sessions | Task breakdowns |
| Build Waves 1-2 (001, 002, 005) | 1 week | Foundation + NATS integration |
| Build Waves 3-4 (003, 004, 006) | 1 week | Delegation + checkpoints + coordination |
| Build Waves 5-6 (007, 008) | 1-2 weeks | End-to-end integration + additional modes |
| Validation + FinProxy run | 1 week | Testing, debugging, first real pipeline |
| **Total** | **4-6 weeks** | From /system-arch to first FinProxy pipeline run |

**Note:** The Forge build cannot start until specialist-agent Phase 3 is complete and
NATS infrastructure is tested. Plan accordingly — if Phase 3 completes mid-May, the
Forge build runs into June. This is fine; the DDD Southwest demo (16 May) uses the
specialist-agent directly, not the Forge.

---

## Forge Agent Manifest

For fleet registration (FEAT-FORGE-002):

```yaml
agent_id: forge
name: Forge
description: "Pipeline orchestrator and checkpoint manager — coordinates specialist
  agents, applies confidence-gated quality gates, and produces verified deployable
  code from raw ideas"
trust_tier: core
nats_topic: agents.command.forge
max_concurrent: 3

intents:
  - pattern: "build.*"
    signals: [build, develop, implement, create, make, ship]
    confidence: 0.90
  - pattern: "pipeline.*"
    signals: [pipeline, stages, progress, status, deploy]
    confidence: 0.85
  - pattern: "feature.*"
    signals: [feature, add feature, new capability, requirement]
    confidence: 0.80

tools:
  - name: forge_greenfield
    description: "Run full greenfield pipeline from raw input to deployed code"
    risk_level: mutating
    async_mode: true
  - name: forge_feature
    description: "Add a feature to an existing project"
    risk_level: mutating
    async_mode: true
  - name: forge_review_fix
    description: "Review and fix issues in existing code"
    risk_level: mutating
    async_mode: true
  - name: forge_status
    description: "Get current pipeline status for a project"
    risk_level: read_only
    async_mode: false
```

---

## Pipeline Configuration Schema

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
    escalation_channel: "jarvis.notification.slack"

  feature_spec:
    auto_threshold: 0.75
    min_threshold: 0.45
    critical_detections: [VAGUE_REQUIREMENT, UNTESTABLE_ACCEPTANCE, MISSING_TRADEOFF]
    reviewer: rich
    escalation_channel: "jarvis.notification.slack"

  build_verification:
    auto_threshold: 1.0      # tests either pass or they don't
    min_threshold: 0.0
    critical_detections: []
    reviewer: rich
    escalation_channel: "jarvis.notification.slack"

  pr_review:
    auto_threshold: null     # always human — never auto-approved (D37)
    min_threshold: 0.0
    reviewer: rich
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Subprocess orchestration complexity | GuardKit commands invoked as subprocesses may have environment/path issues | FEAT-FORGE-005 builds a robust invoker with env setup, working dir management, and output discovery |
| NATS async coordination | Request-reply with `call_agent_tool()` may timeout under load | Configurable timeout per delegation, retry with backoff, degraded mode fallback |
| Specialist agent not available | Pipeline blocks if required agent is down | Degraded mode: fall back to direct GuardKit commands, force FLAG FOR REVIEW |
| State machine complexity | 3 modes × multiple stages × checkpoint states | State machine formally defined in /system-design, tested independently in FEAT-FORGE-001 |
| First-time integration | Everything connects for the first time in FEAT-FORGE-007 | Use small test corpus first, not FinProxy. Debug integration issues before the real run. |

---

## Do-Not-Reopen Decisions (Forge-Specific)

Captured in fleet-master-index D33–D38 and forge-pipeline-orchestrator-refresh v3
do-not-reopen list. Key ones for the build:

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

2. **Update Step 2 `--context` flags** in this document:
   - Replace `<ADR files produced by Step 1>` with actual ADR filenames
   - Example: `--context forge/docs/decisions/ADR-FORGE-001-checkpoint-protocol.md`

3. **Record in command-history.md:**
   ```markdown
   ## /system-arch — [date]
   ### Command
   [exact command run]
   ### Files Produced
   - forge/docs/architecture/ARCHITECTURE.md
   - forge/docs/decisions/ADR-FORGE-001-*.md
   - [etc.]
   ### Decisions Made During Session
   - [any decisions surfaced during /system-arch]
   ```

### After Step 2: /system-design completes

1. **List the files produced:**
   ```bash
   ls forge/docs/design/
   ls forge/docs/design/specs/
   ls forge/docs/design/contracts/
   ```

2. **Update Step 3 `--context` flags** in this document:
   - Replace `<pipeline config schema from Step 2>` with actual path
   - Replace `<checkpoint protocol contract from Step 2>` with actual path
   - Example: `--context forge/docs/design/contracts/checkpoint-protocol-contract.md`

3. **Update Step 4 and Step 5** if the system spec restructures features or changes
   the dependency graph.

4. **Update "Files That Will Change" table** if /system-design produces a different
   package structure than the one sketched above.

5. **Record in command-history.md** (same pattern as Step 1).

### After Step 3: /feature-spec sessions

1. **For each feature spec session**, create a history file:
   ```bash
   # Example:
   touch forge/feature-spec-FEAT-FORGE-001-history.md
   ```
   Record Rich's responses to acceptance groups, assumptions resolved, and any
   deviations from defaults (following Pattern 3 from fleet-master-index).

2. **Update Step 3 `--context` flags for later features:**
   - Replace `<all previous feature spec files>` in FEAT-FORGE-007 with actual paths
   - Replace `<FEAT-FORGE-007 feature spec>` in FEAT-FORGE-008 with actual path

### Template for --context flag updates

When updating, use this pattern:
```bash
# Before (placeholder):
guardkit feature-spec FEAT-FORGE-004 \
  --context <checkpoint protocol contract from Step 2>

# After (actual path):
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/checkpoint-protocol-contract.md
```

### Verification checklist after all updates

- [ ] All `<placeholder>` strings removed from this document
- [ ] All `--context` flags reference files that exist on disk
- [ ] command-history.md has entries for /system-arch and /system-design
- [ ] feature-spec-FEAT-FORGE-XXX-history.md exists for each feature spec run
- [ ] "Files That Will Change" table matches actual package structure from /system-design

---

## Source Documents

| Document | Path | What It Provides |
|----------|------|-----------------|
| Pipeline orchestrator refresh v3 | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | Scope: Forge identity, checkpoint protocol, tool inventory, NATS integration, degraded mode |
| Original motivation | `forge/docs/research/pipeline-orchestrator-motivation.md` | Why: 93% defaults, 3 decisions, 4:1 leverage |
| Original conversation starter | `forge/docs/research/pipeline-orchestrator-conversation-starter.md` | Three modes, multi-project, execution environments |
| Fleet master index v2 | `forge/docs/research/ideas/fleet-master-index.md` | Fleet context, decisions D1-D38, repo map, build sequence |
| Specialist agent vision | `specialist-agent/docs/research/ideas/architect-agent-vision.md` | Three roles, three-layer architecture, delegation targets |
| nats-core system spec | `nats-core/docs/design/specs/nats-core-system-spec.md` | Payload schemas, topic registry, client API |
| Agent manifest contract | `nats-core/docs/design/contracts/agent-manifest-contract.md` | AgentManifest, ToolCapability, IntentCapability |
| Dev pipeline architecture | Project knowledge | Build Agent lifecycle, topic taxonomy, PM adapter |
| Dev pipeline system spec | Project knowledge | Component specs, actor model, system boundary |
| This build plan | `forge/docs/research/ideas/forge-build-plan.md` | Command sequence, feature summary, prerequisites |

---

*Build plan created: 12 April 2026*
*Status: Ready for /system-arch when prerequisites met*
*"The Forge is the capstone. It's the last major agent to build because it coordinates everything else. But it's also the highest-leverage: once it works, the Software Factory is real."*
