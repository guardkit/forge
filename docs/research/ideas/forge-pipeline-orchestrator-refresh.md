# GuardKit Factory — Pipeline Orchestrator Agent Refresh
## Conversation Starter for Strategic Re-Alignment
## Date: 11 April 2026 (updated: 11 April 2026 — v3)
## Status: Exploration — captures how the ecosystem has evolved since March 2026. Aligned with anchor v2.2.
## Canonical architecture: [forge-pipeline-architecture.md](../forge-pipeline-architecture.md) v2.2 — this document is a supporting design artefact for the checkpoint protocol and specialist-agent delegation model.
## Repo: `guardkit/forge`
## Related: `specialist-agent` unified harness (Phase 1B), `nats-core` (98% coverage, implemented), `nats-infrastructure` (configured, ready to run), fleet master index

---

## Purpose of this document

The pipeline orchestrator conversation starter (`pipeline-orchestrator-conversation-starter.md`) was written in March 2026. Since then, significant architectural decisions and implementations have changed the landscape. This document captures what's different and how the orchestrator should evolve to take advantage of the specialist agent fleet rather than doing everything itself.

**This is not a replacement for the original conversation starter.** It's an addendum that should be read alongside it. The original captures the correct motivation, modes, and checkpoint protocol. This document captures the new inter-agent collaboration patterns and the confidence-gated checkpoint model.

---

## What's Changed Since March 2026

### 1. The Specialist Agent Harness Exists

The architect-agent codebase (being renamed to `specialist-agent`) has been proven in Phase 0:
- Player-Coach adversarial loop: 0.75 score, 93 seconds, 1006 tests green
- Weighted evaluation with 6 criteria and 4 detection patterns
- Configurable LLM provider (Claude, GPT, Gemini, local vLLM)

Phase 1B (in progress) refactors it into a unified harness with role-based configuration:
- `--role architect` → conversation starters with C4 diagrams, ADRs
- `--role product-owner` → product documents with user stories, acceptance criteria
- Same generation loop, scoring engine, session lifecycle
- Different prompts, criteria, output types per role
- Each role defines its own `fleet:` section in `agent-role.yaml`, from which `AgentManifest` is generated — this means new roles are automatically discoverable by the Forge without any Forge code changes

**Implication for the orchestrator:** The orchestrator doesn't need to embed architectural reasoning or product documentation logic. It delegates to specialist agents and consumes their output. The role-config-driven manifest generation means the Forge's `discover_agents` capability isn't reading static manifests — it's reading manifests that are derived from role configs. Add a role, the fleet grows, Forge adapts. This is the "zero refactoring" promise of the AgentManifest pattern.

### 2. The Product Owner Agent Is No Longer Separate

The `guardkit/product-owner-agent` repo has been deleted. The product owner is now a role within the specialist-agent harness. The orchestrator calls the specialist-agent via NATS `agents.command.product-owner-agent` rather than a separate service.

### 3. NATS Core Library Is Implemented (97% Test Coverage)

The `nats-core` library is no longer a design contract — it's working code at 98% test coverage across 17 test files. The full implementation includes:

- **`NATSClient`** — async client wrapping nats-py with typed `MessageEnvelope` construction, project-scoped topic prefixing, and safe JSON deserialisation. Fleet convenience methods: `register_agent()`, `deregister_agent()`, `heartbeat()`, `get_fleet_registry()`, `watch_fleet()`, `call_agent_tool()`.
- **`NATSKVManifestRegistry`** — JetStream KV-backed implementation of `ManifestRegistry` ABC. `find_by_intent()` and `find_by_tool()` for dynamic agent discovery. `InMemoryManifestRegistry` for testing.
- **`MessageEnvelope`** — canonical wire format with typed `EventType` enum covering 19 event types across four domains (Pipeline, Agent, Jarvis, Fleet). Each event type has a registered payload class.
- **`Topics`** — immutable namespace registry with `resolve()` for template substitution (`agents.{agent_id}.tools.{tool_name}`) and `for_project()` for multi-tenancy scoping.
- **`AgentConfig`** — pydantic-settings model with `ModelConfig`, `GraphitiConfig`, `NATSConfig`. Environment variable override via `AGENT_` prefix and `__` nested delimiter.
- **Event payloads** — full lifecycle coverage:
  - Fleet: `AgentHeartbeatPayload`, `AgentDeregistrationPayload` (registration uses `AgentManifest` directly per DDR-002)
  - Agent: `AgentStatusPayload`, `ApprovalRequestPayload`/`ApprovalResponsePayload` (the checkpoint protocol), `CommandPayload`/`ResultPayload`
  - Pipeline: ~~`FeaturePlannedPayload`~~, ~~`FeatureReadyForBuildPayload`~~ *(RETIRED — see correction 12 below; replaced by `StageCompletePayload` and `BuildQueuedPayload`)*, `BuildStartedPayload`/`BuildProgressPayload`/`BuildCompletePayload`/`BuildFailedPayload`
  - Jarvis: `IntentClassifiedPayload`, `DispatchPayload`, `AgentResultPayload`, `NotificationPayload`

**What remains:** Integration tests against a live NATS server. The unit tests mock the NATS connection; the integration tests (`test_client_integration.py`) need a running NATS instance to execute. This is the weekend task.

### 4. NATS Infrastructure Is Configured and Ready to Run

The `nats-infrastructure` repo has:
- `config/nats-server.conf` — server named `ships-computer`, binds 0.0.0.0:4222, JetStream enabled with 1GB memory / 10GB file storage, HTTP monitoring on :8222
- `config/accounts/accounts.conf.template` — multi-tenancy: APPMILLA account (Rich + James, full access `>`), FINPROXY account (Mark, scoped to `finproxy.>`), SYS account (admin)
- `scripts/docker-entrypoint.sh` — envsubst-based password injection at container startup, validates all four password env vars are set
- `scripts/verify-nats.sh` — startup verification
- `.env.example` — template for the four required passwords
- ADR-001 (standalone infra repo) and ADR-002 (account multi-tenancy) documented
- Claude Code agents for all NATS patterns (FastStream broker, handler-service separation, Pydantic schemas, pytest-asyncio testing)
- AutoBuild has executed against TASK-NATS-001 through TASK-NATS-004; tasks in `design_approved` state

**To run:** `cp .env.example .env`, set real passwords, `docker compose up -d`. Then run nats-core integration tests against it.

### 5. Fine-Tuned Models Are Per-Role

Phase F of the specialist-agent defines per-role fine-tuning:
- `architect-gemma4-31b` — fine-tuned on architecture books (Ousterhout, Ford, Farley)
- `product-owner-gemma4-31b` — fine-tuned on product management books (Cagan, Patton)
- Deployed to AWS Bedrock CMI (serverless, pay-per-use)

**Implication:** The orchestrator doesn't need to be fine-tuned itself — it's a coordinator, not a specialist. It uses a strong reasoning model (Claude, Gemini) for orchestration decisions and delegates domain judgment to fine-tuned specialists.

### 6. The `/feature-spec` Command Uses Propose-Review Methodology

The `/feature-spec` v2 command (FEAT-008 in the guardkit repo) uses Specification by Example — the AI proposes Gherkin scenarios, the human curates. This is a fundamentally different interaction model from the original conversation starter's assumption that slash commands are simple tool invocations.

**Implication:** Some pipeline stages produce output that may need human review. But the Coach scoring mechanism already determines output quality — we can use it to decide *whether* a human needs to see the output at all. See the Confidence-Gated Checkpoint Protocol below.

---

## The Agent Name Question

The pipeline orchestrator needs a short, distinctive name for fleet identity and daily use. "Pipeline Orchestrator" is descriptive but verbose.

### Candidates

| Name | Rationale | Feel |
|------|-----------|------|
| **Forge** | Where raw materials become finished products. Ties to DDD talk "software factory" theme. Short, strong, distinctive | Industrial, purposeful |
| **Shipwright** | The builder of the ship. Ties to "Ship's Computer" metaphor. A shipwright builds and maintains the vessel | Nautical, craftsmanlike |
| **Quartermaster** | The officer who manages supplies and logistics. Coordinates resources without being any single specialist | Military, systematic |
| **Conductor** | Orchestrates the orchestra. Each musician (agent) is a specialist; the conductor coordinates timing and dynamics | Musical, elegant |
| **Anvil** | Where things are hammered into shape. The forge's working surface | Industrial, minimal |
| **Foundry** | Where raw metal is cast into components. Factory metaphor | Industrial, solid |

**Recommendation: Forge.** It's short (5 letters, one syllable), ties directly to the "software factory" DDD talk narrative, and the metaphor is precise — raw materials (product docs, conversation starters) go in, finished products (deployed code) come out. The forge doesn't mine the ore (that's the product owner) or design the blueprint (that's the architect) — it does the making.

`forge build finproxy` reads better than `pipeline-orchestrator build finproxy`.

Fleet identity: `agent_id: "forge"`, NATS topic: `agents.command.forge`.

---

## Confidence-Gated Checkpoint Protocol

### The Problem with Hard Checkpoints

The March 2026 design placed a hard human checkpoint after every major pipeline stage. But the Phase 0 FinProxy run scored 0.75 — meaning the Coach already knows when output is good enough. If the Coach is confident, why interrupt the pipeline?

Hard checkpoints at every stage create unnecessary friction. The "93% defaults accepted" stat from the motivation doc tells us that most of the time, the output is fine and the human just says "approved." We're blocking the pipeline for a rubber stamp.

### The Solution: Confidence-Gated Checkpoints

Every specialist agent stage already runs through the Coach (Player-Coach adversarial loop). The Coach produces a weighted score with specific criterion breakdowns and detection pattern findings. The Forge uses this score to decide the checkpoint mode:

**🟢 AUTO-APPROVE (score ≥ auto_threshold)**
Pipeline continues automatically. Human gets a notification (not a gate) with the score, criterion breakdown, and a link to the output. The notification is informational — the human can review at leisure but the pipeline doesn't wait.

**🟡 FLAG FOR REVIEW (min_threshold ≤ score < auto_threshold)**
Pipeline pauses. Human gets the output plus the Coach's specific concerns — not just "I'm not sure" but *which criteria scored low* and *which detection patterns fired*. The human can approve, request revision, or reject. This is where the Coach earns its keep — it provides targeted feedback, not a generic "please check."

**🔴 HARD STOP (score < min_threshold, or critical detection fired)**
Pipeline blocks until human resolves. This fires when a critical detection pattern triggers (PHANTOM architecture, UNGROUNDED claims, contradicts existing ADRs) regardless of overall score. Some things are too risky to auto-approve even with a high aggregate score.

### Threshold Configuration

Thresholds are configurable **per stage** and **per project**:

```yaml
# forge-pipeline-config.yaml (per project)
project: finproxy

checkpoints:
  product_docs:
    auto_threshold: 0.80
    min_threshold: 0.50
    critical_detections: [VAGUE_REQUIREMENT, UNTESTABLE_ACCEPTANCE]
    reviewer: james          # who gets notified/flagged
    escalation_channel: "jarvis.notification.slack"

  architecture:
    auto_threshold: 0.80
    min_threshold: 0.50
    critical_detections: [PHANTOM, UNGROUNDED, SCOPE_CREEP]
    reviewer: rich
    escalation_channel: "jarvis.notification.slack"

  feature_spec:
    auto_threshold: 0.75     # slightly lower — specs are easier to fix downstream
    min_threshold: 0.45
    critical_detections: [VAGUE_REQUIREMENT, UNTESTABLE_ACCEPTANCE, MISSING_TRADEOFF]
    reviewer: rich
    escalation_channel: "jarvis.notification.slack"

  build_verification:
    auto_threshold: 1.0      # tests either pass or they don't
    min_threshold: 0.0       # any test failure → hard stop
    critical_detections: []
    reviewer: rich
    escalation_channel: "jarvis.notification.slack"

  pr_review:
    auto_threshold: null     # always human — this is the final gate
    min_threshold: 0.0
    reviewer: rich
```

### NATS Integration

The checkpoint protocol maps directly to the nats-core event payloads:

- **Auto-approve** → publish `NotificationPayload` to `jarvis.notification.{adapter}` with score summary
- **Flag for review** → publish `ApprovalRequestPayload` to `agents.approval.{agent_id}.{task_id}` with `risk_level` derived from score band, Coach criterion breakdown in `details`
- **Human responds** → `ApprovalResponsePayload` on `agents.approval.{agent_id}.{task_id}.response` with `decision: approve | reject | defer | override`
- **Hard stop** → `ApprovalRequestPayload` with `risk_level: "high"`, critical detections in `details`

All of these payload types already exist in nats-core and are tested. The Forge doesn't need to invent a checkpoint wire format — it's already built.

### Project Maturity Adjustment

Early in a project (greenfield FinProxy), you probably want lower auto_threshold values — more human eyes on everything while the architecture is being established. Once the architecture is stable and you're in feature mode (Mode B), the Coach has enough context and the thresholds can rise.

The Forge could even learn this automatically: track the rate at which humans override auto-approved outputs. If the override rate is above 10%, lower the auto_threshold. If it's consistently 0%, raise it. This is a future optimisation — start with manual threshold configuration.

### Detection Pattern Override

Some detection patterns are critical regardless of overall score. If the Coach detects PHANTOM (architecture references that don't exist), the stage goes to 🔴 HARD STOP even if the aggregate score is 0.85. This is configured via `critical_detections` per stage.

### What This Means for the Pipeline

Most pipeline runs will flow end-to-end with zero human interruption. The human gets a stream of notifications: "Product docs scored 0.83 — auto-approved ✅", "Architecture scored 0.78 — auto-approved ✅", "Feature spec #3 scored 0.52, SCOPE_CREEP detected — flagged for review 🟡". The human only engages when the Coach has specific concerns.

The final PR review remains a hard human checkpoint — this is the one gate that should never auto-approve. Everything else is confidence-gated.

### DDD Southwest Narrative

"The factory doesn't just build — it knows when to ask for help. The judge model reviews every output and only interrupts a human when it has specific concerns. In practice, 93% of the time the pipeline runs end to end without stopping. When it does stop, it tells you *exactly why* — which criteria scored low, which patterns it detected, what it's worried about."

---

## Revised Architecture: Forge as NATS-Native Pipeline Orchestrator

### Core Framing

The Forge is the NATS-native pipeline orchestrator (see [anchor v2.2 §2](../forge-pipeline-architecture.md)). Confidence-gated checkpoints are how it decides when to involve Rich — they are the mechanism by which the orchestrator manages human engagement, not the Forge's identity.

Its core competency is: orchestrating the pipeline from feature spec through to PR, managing the state machine between stages, knowing which agent to call, what context to pass, evaluating Coach scores against configurable thresholds, routing to humans when confidence is low, and recovering from failures. The checkpoint protocol (described in detail in this document) is the Forge's *quality gate mechanism*, not its *purpose*.

With confidence-gated checkpoints, the distinction between "autonomous stage" and "human stage" dissolves. Every stage is evaluated by the Coach. The Coach score determines whether a human needs to engage. The Forge manages this flow.

### The Old Model (March 2026)

The orchestrator invokes GuardKit slash commands as tools, doing everything itself:

```
Forge Agent
  ├── tool: system_arch        (invoke /system-arch directly)
  ├── tool: system_design      (invoke /system-design directly)
  ├── tool: feature_spec       (invoke /feature-spec directly)
  ├── tool: feature_plan       (invoke /feature-plan directly)
  ├── tool: autobuild          (invoke autobuild directly)
  ├── tool: graphiti_seed      (invoke graphiti add-context)
  └── tool: verify             (run tests)
```

### The New Model (April 2026)

The Forge delegates domain judgment to specialist agents via NATS, uses GuardKit slash commands for implementation, and applies confidence-gated checkpoints at every stage:

```
Forge Agent
  │
  ├── DELEGATE to specialist agents (via NATS)
  │   ├── product-owner-agent: "Generate product documentation from these raw inputs"
  │   │   → returns ProductDocument + Coach score + criterion breakdown
  │   ├── architect-agent: "Generate architecture from these product docs"
  │   │   → returns ConversationStarter + Coach score + criterion breakdown
  │   ├── architect-agent: "Is this feature feasible given our architecture?"
  │   │   → returns FeasibilityAssessment
  │   └── (future) ux-designer-agent: "Design the user flows for this feature"
  │       → returns UX spec + Coach score
  │
  ├── EVALUATE (confidence-gated checkpoints)
  │   ├── Check Coach score against per-stage thresholds
  │   ├── Check for critical detection patterns
  │   ├── 🟢 Auto-approve → NotificationPayload to jarvis.notification.*
  │   ├── 🟡 Flag → ApprovalRequestPayload to agents.approval.*
  │   └── 🔴 Hard stop → ApprovalRequestPayload (risk_level: high)
  │
  ├── INVOKE GuardKit commands (for implementation stages)
  │   ├── /system-arch      (consumes architect-agent output as conversation starter)
  │   ├── /system-design    (consumes /system-arch output)
  │   ├── /feature-spec     (consumes product-owner output + architecture)
  │   ├── /feature-plan     (consumes /feature-spec output)
  │   ├── autobuild         (implements tasks from /feature-plan)
  │   └── /task-review      (reviews issues, creates fix tasks)
  │
  ├── COORDINATE
  │   ├── graphiti_seed     (seed outputs into knowledge graph after each stage)
  │   ├── verify            (run tests, integration checks)
  │   ├── git operations    (branch, commit, PR)
  │   └── ci_trigger        (GitHub Actions)
  │
  └── HARD CHECKPOINT (always human, never auto-approved)
      └── PR review — final gate before merge
```

### What This Changes

1. **The Forge doesn't need architectural judgment.** It delegates to the architect-agent. The architect-agent has fine-tuned domain knowledge (Phase F) — the Forge's reasoning model doesn't need it.

2. **The Forge doesn't need product management judgment.** It delegates to the product-owner-agent. James's review is confidence-gated — he only reviews when the Coach flags concerns, not every pipeline run.

3. **The Forge's checkpoint protocol is its quality gate mechanism.** Its core competency is: orchestrating the pipeline end-to-end, evaluating Coach scores, applying threshold logic, routing to humans when confidence is low, and managing the state machine between stages. This is orchestration reasoning, not domain reasoning.

4. **The Forge discovers agents dynamically.** Via `NATSKVManifestRegistry.find_by_intent()` and `find_by_tool()`, the Forge finds which agents are available and routes work accordingly. The `AgentManifest` generated from each role's `agent-role.yaml` means new specialist roles are automatically discoverable.

5. **Most stages flow without human intervention.** The confidence-gated protocol means the pipeline only pauses when the Coach has specific concerns. The "93% defaults accepted" stat becomes architectural — it's built into the checkpoint logic, not dependent on human response time.

---

## Revised Pipeline Flow

### Mode A: Greenfield (with confidence-gated checkpoints)

```
Raw input (idea, brief, product concept)
    │
    ▼
Forge dispatches to product-owner-agent (via NATS call_agent_tool)
    → ProductDocument (user stories, acceptance criteria, backlog)
    → Coach score: 0.83, no critical detections
    │
    ▼
Confidence gate: score 0.83 ≥ auto_threshold 0.80
    🟢 AUTO-APPROVE → NotificationPayload to James, pipeline continues
    │
    ▼
Forge dispatches to architect-agent (via NATS call_agent_tool)
    → ConversationStarter (C4, ADRs, open questions, constraints)
    → Coach score: 0.78, no critical detections
    │
    ▼
Confidence gate: score 0.78 ≥ auto_threshold 0.80? NO (0.78 < 0.80)
    🟡 FLAG FOR REVIEW → ApprovalRequestPayload to Rich with criterion breakdown
    Rich reviews → ApprovalResponsePayload (decision: approve)
    │
    ▼
Forge invokes /system-arch (consumes ConversationStarter as input)
    → ARCHITECTURE.md, ADRs, C4 diagrams (full resolution)
    │
    ▼
Forge invokes /system-design
    → DESIGN.md, API contracts, data models
    │
    ▼
Forge seeds outputs to Graphiti
    │
    ▼
Forge invokes /feature-spec × N
    → BDD feature files, assumptions manifests
    → Coach score per feature spec
    │
    ▼
Confidence gate per feature spec:
    🟢 Spec #1 scored 0.82 → auto-approve
    🟢 Spec #2 scored 0.79 → auto-approve (threshold 0.75 for specs)
    🟡 Spec #3 scored 0.52, SCOPE_CREEP detected → flag for review
    │
    ▼
Forge invokes /feature-plan × N (for approved specs)
    → Task breakdowns per feature
    │
    ▼
Forge invokes autobuild × N (sequential on GB10, parallel on Bedrock)
    → Implemented features
    │
    ▼
Forge invokes verify (tests, integration checks)
    → All tests pass? Continue. Any failure? 🔴 HARD STOP
    │
    ▼
Forge triggers CI/CD (git push, PR, GitHub Actions)
    │
    ▼
🔴 HARD CHECKPOINT: Human reviews PR (always human, never auto-approved)
```

### Mapping to Anchor v2.2 Pipeline Stages

The greenfield flow above uses finer-grained steps than anchor v2.2 §4's five stages. This mapping shows the correspondence:

| This doc's flow block | Anchor v2.2 §4 Stage | Notes |
|-----------------------|-----------------------|-------|
| product-owner-agent delegation | Stage 1 — Specification Review | Anchor verifies spec completeness; this doc generates product docs from raw input |
| architect-agent delegation | Stage 2 — Architecture Review | Specialist evaluates feasibility, produces ADRs |
| /system-arch + /system-design | (between Stage 2 and Stage 3) | Tactical resolution of architect's strategic output — not a separate anchor stage |
| /feature-spec × N | Stage 3 — Feature Planning (first half) | Per-spec confidence gating |
| /feature-plan × N | Stage 3 — Feature Planning (second half) | Task decomposition from approved specs |
| autobuild × N | Stage 4 — AutoBuild Execution | Per-task Coach validation |
| verify + git/PR | Stage 5 — PR Creation | Quality gates + PR creation |

The anchor's Stage 1 ("Specification Review") is broader than just product-owner delegation — it also verifies that `/feature-spec` outputs exist and assumptions are resolved. The mapping is approximate; the anchor is authoritative for stage definitions.

### Key Differences from Previous Versions

1. **Confidence-gated checkpoints replace hard checkpoints.** The Coach score determines whether a human needs to engage, not a fixed pipeline stage. Most stages auto-approve.

2. **The first two stages are agent-to-agent delegations via NATS**, using `NATSClient.call_agent_tool()` which already supports request-reply with timeout. The specialist agents bring fine-tuned domain judgment. The Forge brings pipeline coordination.

3. **Per-spec gating.** Feature specs are evaluated individually. Spec #1 might auto-approve while spec #3 gets flagged. The pipeline doesn't block on *all* specs — just the ones the Coach is worried about.

4. **PR review is the only unconditional hard checkpoint.** Everything else is conditional on Coach confidence.

5. **Specialist agents return Coach scores as part of their result payload.** The Forge doesn't re-evaluate — it reads the score the specialist's own Coach produced. This means the specialist's domain-specific criteria (not the Forge's generic reasoning) determine confidence.

---

## Forge Tool Inventory (Revised)

### Fleet Agent Tools (via NATS)

**Reply-subject convention (LES1 parity rule).** `call_agent_tool()` publishes
commands to `agents.command.<agent_id>` and subscribes to
`agents.result.<agent_id>.<correlation_id>` (or the convention nats-core's
`NATSClient.call_agent_tool()` sets) for the real reply. The JetStream AGENTS
stream intercepts `agents.>` and returns `PubAck` within ~3ms — **do not treat
PubAck as success**. Per specialist-agent LES1 §2 (walkthrough §retest-smoke):
`nats request` against `agents.>` returned PubAck and exited ~3ms later; the real
reply arrived separately on `agents.result.<role>`. Operators read PubAck as
success and the round-trip contract was silently broken for iterations.
Document publishers/subscribers per role in one table before go-live.

| Tool | Target Agent | NATS Method | Input | Output |
|------|-------------|-------------|-------|--------|
| `delegate_product_docs` | product-owner-agent | `call_agent_tool()` | Raw input, product brief | ProductDocument + CoachScore |
| `delegate_architecture` | architect-agent | `call_agent_tool()` | ProductDocument, scope | ConversationStarter + CoachScore |
| `check_feasibility` | architect-agent | `call_agent_tool()` | Feature summary, constraints | FeasibilityAssessment |
| `discover_agents` | fleet registry | `NATSKVManifestRegistry.list_all()` | — | Available AgentManifests |
| `find_agent_for_intent` | fleet registry | `NATSKVManifestRegistry.find_by_intent()` | Intent string | Matching AgentManifests |

### GuardKit Command Tools (direct invocation)

| Tool | Input | Output |
|------|-------|--------|
| `system_arch` | ConversationStarter path | ARCHITECTURE.md, ADRs |
| `system_design` | ARCHITECTURE.md path | DESIGN.md, contracts |
| `feature_spec` | Architecture + feature description | BDD feature file + CoachScore |
| `feature_plan` | Feature spec path | Task breakdown |
| `autobuild` | Feature ID | Build result |
| `task_review` | Subject path | Review report |

### Context Manifest Convention

The Forge reads `.guardkit/context-manifest.yaml` from each target repo to discover
cross-repo dependencies and assemble `--context` flags automatically. This eliminates
the manual context flag selection that previously required a Claude Desktop conversation
to identify the right docs for each command invocation.

Manifest format:
```yaml
repo: forge
dependencies:
  nats-core:
    path: ../nats-core
    relationship: "Why this repo depends on nats-core"
    key_docs:
      - path: docs/design/specs/nats-core-system-spec.md
        category: specs          # specs | contracts | decisions | source | product | architecture
        description: "What this doc provides"
internal_docs:
  always_include:               # Included in every GuardKit command for this repo
    - docs/architecture/ARCHITECTURE.md
    - docs/design/DESIGN.md
```

Category filtering by command type:
- `/system-arch` → architecture + decisions
- `/system-design` → specs + decisions + contracts
- `/feature-spec` → specs + contracts + source
- `autobuild` → contracts + source
- `internal_docs.always_include` → every command

This convention replaces the manual `--context` flag assembly that was previously done
in Claude Desktop sessions and captured in build plans. The build plans remain as
the authoritative record of what was actually run, but the Forge uses manifests for
automated invocations.

### Checkpoint Tools (using nats-core event payloads)

| Tool | NATS Payload | Topic Pattern | Purpose |
|------|-------------|---------------|---------|
| `auto_approve_notify` | `NotificationPayload` | `jarvis.notification.{adapter}` | Inform human of auto-approved stage |
| `request_approval` | `ApprovalRequestPayload` | `agents.approval.{agent_id}.{task_id}` | Flag or hard-stop for human review |
| `await_approval` | subscribe `ApprovalResponsePayload` | `agents.approval.{agent_id}.{task_id}.response` | Wait for human decision |
| `record_override` | custom payload | `forge.telemetry.overrides` | Track human override rate for threshold learning |

### Infrastructure Tools

| Tool | Input | Output |
|------|-------|--------|
| `graphiti_seed` | Document paths, type | Seed confirmation |
| `graphiti_query` | Query string | Retrieved context |
| `verify` | Project path, test command | Pass/fail |
| `git_operations` | Branch/commit/PR params | Git result |
| `publish_pipeline_event` | Pipeline event payload | Delivery confirmation |

### Pipeline Event Publishing

The Forge publishes pipeline lifecycle events using the nats-core payloads that already exist:

| Pipeline Stage | Event Published | Payload |
|---------------|----------------|---------|
| Build queued | `pipeline.build-queued.{feature_id}` | `BuildQueuedPayload` (feature_id, repo, triggered_by, correlation_id — see anchor v2.2 §7) |
| Build starts | `pipeline.build-started.{feature_id}` | `BuildStartedPayload` (build_id, wave_total) |
| Build progress | `pipeline.build-progress.{feature_id}` | `BuildProgressPayload` (wave, overall_progress_pct, elapsed_seconds) |
| Stage complete | `pipeline.stage-complete.{feature_id}` | `StageCompletePayload` (stage, status, coach_score) |
| Build paused | `pipeline.build-paused.{feature_id}` | `BuildPausedPayload` (stage, coach_score, gate_mode) |
| Build completes | `pipeline.build-complete.{feature_id}` | `BuildCompletePayload` (tasks_completed, tasks_failed, pr_url, summary) |
| Build fails | `pipeline.build-failed.{feature_id}` | `BuildFailedPayload` (failure_reason, recoverable, failed_task_id) |

> **Decision (TASK-FVD3, correction 12):** `FeaturePlannedPayload` and `FeatureReadyForBuildPayload` are **retired** from this document. Anchor v2.2 §7 does not include them, and their function is covered by `StageCompletePayload` (for Stage 3 completion) and `BuildQueuedPayload` (for build readiness). The payloads still exist in `nats-core` and should be marked `@deprecated` there — coordinated via TASK-NCFA-001 in the nats-core repo.

### Degraded Mode

If a specialist agent is unavailable (not registered in `NATSKVManifestRegistry`), the Forge falls back:
- No product-owner-agent → Forge uses `/feature-spec` directly with raw input (lower quality product docs, no Coach score — defaults to 🟡 FLAG FOR REVIEW)
- No architect-agent → Forge uses `/system-arch` directly with a basic conversation starter (no fine-tuned architectural judgment, no Coach score — defaults to 🟡 FLAG FOR REVIEW)
- Both available → Full pipeline with specialist delegation and confidence-gated checkpoints (best quality)

This degradation is automatic — the Forge queries the registry at pipeline start and adapts its tool selection. In degraded mode, the absence of a Coach score means the Forge cannot auto-approve — it always flags for human review. This is the correct behaviour: less confidence in the output → more human oversight.

---

## Forge Agent Identity

```yaml
agent_id: forge
name: Forge
description: "Pipeline orchestrator and checkpoint manager — coordinates specialist agents, applies confidence-gated quality gates, and produces verified deployable code from raw ideas"
template: langchain-deepagents-weighted-evaluation  # or future adversarial template

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
  # Per specialist-agent LES1 §4 (TASK-MDF-POLR): any tool call whose handler
  # may exceed 30s MUST be fire-and-forget — return session_id/pipeline_id
  # immediately, expose _status and _cancel companions. Forge pipeline runs up
  # to 60 min (`max_task_timeout_seconds: 3600`), far beyond the Claude Desktop
  # 240s MCP timeout. Tool descriptions are a contract; they must match the
  # async implementation, not a hope of "I'll tidy this up later."
  - name: forge_greenfield
    description: "Start a full greenfield pipeline run from raw input. Returns `pipeline_id` immediately (fire-and-forget per LES1 §4 POLR). Poll `forge_status(pipeline_id)` for progress; cancel with `forge_cancel(pipeline_id)`."
    risk_level: mutating
    async_mode: true
  - name: forge_feature
    description: "Start adding a feature to an existing project. Returns `pipeline_id` immediately (fire-and-forget per LES1 §4 POLR). Poll `forge_status(pipeline_id)`; cancel with `forge_cancel(pipeline_id)`."
    risk_level: mutating
    async_mode: true
  - name: forge_review_fix
    description: "Start a review-and-fix cycle on existing code. Returns `pipeline_id` immediately (fire-and-forget per LES1 §4 POLR). Poll `forge_status(pipeline_id)`; cancel with `forge_cancel(pipeline_id)`."
    risk_level: mutating
    async_mode: true
  - name: forge_status
    description: "Get current pipeline status for a project (or specific pipeline_id)"
    risk_level: read_only
    async_mode: false
  - name: forge_cancel
    description: "Cancel an in-flight pipeline run by pipeline_id. Companion to the three mutating fire-and-forget tools per LES1 §4 POLR."
    risk_level: mutating
    async_mode: false

max_concurrent: 1  # sequential builds per ADR-SP-012
trust_tier: core    # infrastructure-level agent, not specialist
nats_topic: agents.command.forge
```

### AgentConfig (using nats-core schema)

**Provider-resolution rule (LES1 parity).** `reasoning_model` must be resolved
from `AGENT_MODELS__REASONING_MODEL` (via `AgentConfig`, with the `AGENT_` env
prefix and `__` nested delimiter) at the lowest layer that instantiates the chat
model — **not** in each handler, and **not** via hard-coded defaults at the call
site. Per specialist-agent LES1 §3 (TASK-MDF-PMEV + `command_router.py.
_default_player_model()` CRMV parity patch in commit `8b9d584`): the same bug
was fixed twice because handlers re-computed defaults from their own view of the
environment. Every transport forge adds (NATS, future MCP, CLI) must route
through the same factory — the factory, not the handler, owns resolution.

```yaml
# forge/agent-config.yaml.example
models:
  reasoning_model: "claude-sonnet-4-20250514"  # orchestration reasoning
  reasoning_endpoint: ""
  implementation_model: null                    # Forge doesn't implement code
  embedding_model: null                         # Forge doesn't embed directly

graphiti:
  endpoint: "bolt://promaxgb10-41b1:7687"
  default_group_ids:
    - "appmilla-fleet"
    - "forge_pipeline_history"

nats:
  url: "nats://promaxgb10-41b1:4222"

langsmith_project: "forge"
heartbeat_interval_seconds: 30
max_task_timeout_seconds: 3600  # 60 min — full pipeline runs are long
```

---

## Pipeline State Machine

The Forge's state machine is formally defined in [anchor v2.2 §6](../forge-pipeline-architecture.md). The states are:

- **IDLE** — no pending JetStream messages; waiting for next `pipeline.build-queued`
- **PREPARING** — git clone/pull, validate feature YAML, create branch, write SQLite
- **RUNNING** — pipeline stages 1-5 executing with confidence gating and NATS commands to specialists
- **PAUSED** — awaiting human review after a 🟡 flag-for-review gate; JetStream message remains unacknowledged
- **FINALISING** — push branch, create PR, publish `pipeline.build-complete`, ACK JetStream message
- **COMPLETE** — PR created, SQLite updated, JetStream message acknowledged
- **FAILED** — hard stop or rejection; SQLite updated, JetStream message acknowledged
- **INTERRUPTED** — crash recovery state; JetStream redelivers, Forge restarts from PREPARING

The PAUSED state is the runtime manifestation of the confidence-gated checkpoint protocol described in this document. When a stage scores 🟡 (between `min_threshold` and `auto_threshold`), the Forge publishes `pipeline.build-paused` and enters PAUSED until the human responds.

See anchor v2.2 §6 for the full state transition diagram and crash recovery logic.

---

## What Stays the Same from March

These aspects of the original conversation starter are unchanged and should be carried forward:

1. **Three orchestration modes** — Greenfield (Mode A), Feature (Mode B), Review-Fix (Mode C)
2. **Multi-project parallel execution** — NATS topic prefix isolation per project (using `Topics.for_project()`)
3. **Execution environment abstraction** — worktrees (Phase 1) → devcontainers (Phase 2) → sandboxes (Phase 3)
4. **CI/CD integration** — self-healing loop on CI failures
5. **Dashboard UX** — orchestrator card per active project
6. **Provider-agnostic execution** — cloud or local, configurable
7. **Pipeline state persistence** — resume after crash (NATS KV for state)
8. **The motivation** — 93% defaults accepted, 3 high-impact decisions, 4:1 leverage ratio

---

## What's New or Changed

| Topic | March 2026 | April 2026 v3 |
|-------|-----------|--------------|
| Upstream stages | Forge invokes slash commands for everything | Forge delegates to specialist agents via NATS `call_agent_tool()` |
| Checkpoint protocol | Hard human checkpoint after every major stage | **Confidence-gated checkpoints** — Coach score determines auto-approve / flag / hard stop |
| Checkpoint wire format | Not defined | **nats-core payloads** — `ApprovalRequestPayload`, `ApprovalResponsePayload`, `NotificationPayload` (already implemented) |
| Agent discovery | Static tool inventory | Dynamic via `NATSKVManifestRegistry.find_by_intent()` / `find_by_tool()` |
| Pipeline events | Not defined | **nats-core payloads** — `BuildQueuedPayload` through `BuildCompletePayload` per anchor v2.2 §7 (`FeaturePlannedPayload` and `FeatureReadyForBuildPayload` retired — see correction 12) |
| Product documentation | Not addressed — assumed human provides | Product-owner-agent generates from raw input |
| Architectural judgment | Forge does it via `/system-arch` | Architect-agent provides conversation starter, Forge feeds it to `/system-arch` |
| Feasibility checks | Not addressed | Architect-agent provides via `call_agent_tool()` |
| Fine-tuned models | Not applicable (Forge uses API models) | Specialist agents use fine-tuned models; Forge uses reasoning API |
| Degraded mode | Not addressed | Automatic fallback if specialist agents unavailable (with forced FLAG FOR REVIEW) |
| Agent name | "Pipeline Orchestrator" | **Forge** |
| Core identity | Pipeline orchestrator | **NATS-native pipeline orchestrator** with confidence-gated checkpoints (see anchor v2.2 §2) |
| NATS messaging layer | "Needed" | **Implemented** — nats-core at 98% coverage, nats-infrastructure configured |
| `/feature-spec` interaction | Simple tool invocation | Confidence-gated — auto-approves high-scoring specs, flags low-scoring ones |
| Specialist agent repo | Separate repos (architect-agent, product-owner-agent) | Single repo (`specialist-agent`) with role configs that generate `AgentManifest` |
| Config schema | Ad-hoc | **`AgentConfig` from nats-core** — shared schema across fleet |

---

## Open Questions

1. **Should the Forge itself be a role in the specialist-agent harness?** No — the Forge is a coordinator, not a specialist. It doesn't have weighted evaluation criteria for its own output. It doesn't need a Player-Coach loop. It's a different kind of agent (orchestrator vs evaluator). Keep it in its own repo.

2. **Does the Forge need its own Graphiti context?** Yes — the Forge needs pipeline history (what was built, what failed, what was approved, what override rates look like) across sessions. This is different from the architect's architectural context or the product owner's product context. The Forge's Graphiti group_id is `forge_pipeline_history`.

3. **When does the Forge call the architect vs invoke `/system-arch` directly?** The architect-agent produces a conversation starter (strategic input). `/system-arch` resolves it into full architecture (tactical output). Both are needed in the greenfield pipeline. The Forge calls the architect first, then feeds the conversation starter to `/system-arch`.

4. **What happens when a specialist agent disagrees with the Forge?** Example: the architect-agent scores a conversation starter at 0.55 (below threshold, won't accept). The Forge can either: (a) pass the feedback to the human, (b) provide additional context to the architect for retry, (c) skip the specialist and use `/system-arch` directly. Option (b) first, then (a) if retries exhausted.

5. **Should the Forge learn threshold adjustments automatically?** Track override rates: if humans consistently approve auto-flagged outputs (override rate > 90% for a stage), raise the auto_threshold. If humans consistently reject auto-approved outputs (override rate > 10%), lower it. Start with manual thresholds, add learning later.

6. **How does the Forge return Coach scores to the pipeline?** The specialist agent's `ResultPayload` has a generic `result: dict[str, Any]` field. Convention: include `coach_score`, `criterion_breakdown`, and `detection_findings` keys in the result dict. This is a convention, not a new payload type — keeps it simple.

---

## Suggested Research Topics

- **Anthropic Agent SDK evaluation** — their March 2026 article uses the Claude Agent SDK. Worth comparing to LangChain DeepAgents for orchestrator-level coordination (compaction, context resets, sprint contracts)
- **AsyncSubAgent for non-blocking specialist calls** — DeepAgents 0.5.0a2 has `AsyncSubAgent` which allows the Forge to launch specialist agent calls without blocking the orchestration loop
- **Sprint contract pattern** — the Anthropic article's "generator and evaluator negotiate what done looks like" maps to the Forge negotiating stage completion criteria with each specialist
- **Degraded mode testing** — how does pipeline quality change when specialist agents aren't available vs when they are? This quantifies the value of the specialist fleet
- **Threshold calibration** — what are the right starting thresholds per stage? Run 10 pipelines with hard checkpoints, record human decisions, use the data to set initial auto_threshold values
- **Coach score as pipeline telemetry** — aggregate Coach scores across runs to track quality trends. Are outputs getting better over time (fine-tuning improving)? Are certain stages consistently flagged (need attention)?

---

## Build Sequence

The Forge build depends on:

1. ✅ Phase 0 — GuardKit CLI, AutoBuild, Graphiti (all working)
2. ✅ nats-core — message contracts, manifest registry, topic registry, client (98% coverage, implemented)
3. ✅ nats-infrastructure — NATS server config, account multi-tenancy, Docker entrypoint (configured, ready to run)
4. ◻ **Weekend task: spin up NATS on GB10, run nats-core integration tests** — validates the messaging backbone end-to-end
5. ▶ Specialist-agent Phase 1 — output quality
6. ◻ Specialist-agent Phase 1B — unified harness (architect + product owner roles)
7. ◻ Specialist-agent Phase 3 — NATS fleet integration (import nats-core, `client.register_agent(manifest)`, handle `agents.command.*`)
8. ◻ **Forge** — NATS fleet orchestrator with confidence-gated checkpoints

Steps 4 is the immediate action. Once the NATS server is running and nats-core integration tests pass, the messaging backbone is proven. Specialist-agent Phase 3 then becomes a straightforward integration: import nats-core, register the manifest on startup, subscribe to command topics, return results. The Forge builds on top of all of this.

The Forge is the capstone. It's the last major agent to build because it coordinates everything else. But it's also the highest-leverage: once it works, the "software factory" is real.

---

## Jarvis as Upstream Build Trigger

This document describes the Forge's *runtime behaviour* once a build is in flight — the checkpoint protocol, specialist delegation, and state machine. The *trigger path* that puts builds into JetStream is defined in [anchor v2.2 §5.0 "Build Request Sources"](../forge-pipeline-architecture.md) and ADR-SP-014.

In summary: builds enter JetStream as `pipeline.build-queued.{feature_id}` messages from three possible sources — CLI (`forge queue`), Jarvis (voice/Telegram/dashboard/CLI-wrapper), or future notification adapters. The Forge consumes from JetStream without distinguishing sources at the consumer level; the `BuildQueuedPayload` carries `triggered_by`, `originating_adapter`, and `correlation_id` for history and progress routing. Forge also registers on `fleet.register` for Jarvis CAN-bus discovery.

The checkpoint protocol described in this document applies identically regardless of trigger source. A Jarvis-triggered build goes through the same confidence gates as a CLI-triggered one.

---

## Do-Not-Reopen Decisions

1. **Forge is a coordinator, not a specialist.** No Player-Coach loop for Forge itself.
2. **Forge uses `AgentConfig` from nats-core.** Shared schema across fleet.
3. **Confidence-gated checkpoints, not hard checkpoints.** Coach score determines human engagement.
4. **PR review is always human.** The final gate before merge never auto-approves.
5. **NATS-native from day one.** No subprocess fallback — nats-core and nats-infrastructure are implemented and ready to run. Skip v0.
6. **Degraded mode forces FLAG FOR REVIEW.** No Coach score → no auto-approve.
7. **nats-core event payloads for checkpoint wire format.** `ApprovalRequestPayload`, `ApprovalResponsePayload`, `NotificationPayload` — no new types needed.
8. **Pipeline event publishing uses existing nats-core payloads.** `BuildQueuedPayload` through `BuildCompletePayload` per anchor v2.2 §7. `FeaturePlannedPayload` and `FeatureReadyForBuildPayload` retired (see correction 12 decision above).

---

*Conversation starter refresh v3: 11 April 2026*
*"The Forge doesn't mine the ore or design the blueprint — it does the making. And it knows when to ask for help."*
