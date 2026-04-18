# Forge — Domain Model

> **Generated:** 2026-04-18 via `/system-arch`
> **Related:** [ARCHITECTURE.md](ARCHITECTURE.md) §3 module map, [container.md](container.md) C4 Level 2

---

## Structural Pattern

**Hexagonal modules inside a DeepAgents two-model orchestrator** (ADR-ARCH-001).

The `create_deep_agent(...)` compiled state graph is the shell; pure domain modules form the core; thin adapters sit at the edges. `nats-core`, SQLite, Graphiti, subprocess (`execute`), LLM providers, and the filesystem are the external ports.

This is not DDD (no bounded contexts, no aggregates, no domain events). It's a single-responsibility orchestrator with clean module boundaries. The "domain" here is pipeline orchestration reasoning + evidence-based gate decisions.

---

## Core Concepts

### Build

A single feature-to-PR execution attempt. Identified by `build_id` (format: `build-{feature_id}-{YYYYMMDDHHMMSS}`). Owned by SQLite. Lifecycle-tracked via state machine; work-tracked via reasoning model's `write_todos` list.

```python
# conceptual — see ARCHITECTURE.md §5 data stores for SQL
class Build:
    build_id: str
    feature_id: str
    repo: str
    branch: str
    feature_yaml_path: str
    status: BuildStatus   # QUEUED | PREPARING | RUNNING | PAUSED | FINALISING |
                           # COMPLETE | FAILED | INTERRUPTED | CANCELLED | SKIPPED
    triggered_by: TriggerSource   # cli | jarvis | forge-internal | notification-adapter
    originating_adapter: str | None
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None
    pr_url: str | None
    error: str | None
```

### StageLogEntry

Every dispatch the reasoning model performs generates a row. **The stage name is emergent** — the reasoning model chooses a human-readable label (per ADR-ARCH-016: "Specification Review", "Exploring alternate data model", "Accessibility Audit", etc.) and it's written retrospectively to SQLite.

```python
class StageLogEntry:
    id: int
    build_id: str
    stage_label: str           # reasoning-model-chosen, e.g. "Architecture Review"
    target_kind: Literal["local_tool", "fleet_capability", "subagent"]
    target_identifier: str     # tool name / agent_id:tool_name / subagent name
    status: Literal["PASSED", "FAILED", "GATED", "SKIPPED"]
    coach_score: float | None
    gate_decision: GateDecision | None
    duration_secs: float
    started_at: datetime
    completed_at: datetime
    details_json: str          # rationale + breakdown + detection findings
```

### GateDecision

Output of the gating module's pure reasoning. Captures why a dispatch's Coach-scored result was auto-approved / flagged / hard-stopped.

```python
class GateDecision:
    mode: GateMode             # AUTO_APPROVE | FLAG_FOR_REVIEW | HARD_STOP |
                               # MANDATORY_HUMAN_APPROVAL
    rationale: str             # reasoning model's explanation (written to Graphiti)
    evidence: list[PriorReference]   # which retrieved priors informed the call
    coach_score: float | None
    detection_findings: list[DetectionFinding]
    threshold_applied: float | None   # may be absent when reasoning-driven, no static threshold
```

### CalibrationEvent

Parsed Q&A turn from Rich's history files (`command_history.md`, `feature-spec-{NAME}-history.md`, etc.). Ingested into `forge_calibration_history` Graphiti group.

```python
class CalibrationEvent:
    source_file: str
    command: str               # e.g. "/feature-spec", "/feature-plan"
    stage: str                 # "GROUP_A_CURATION" | "ASSUMPTION_RESOLUTION" | ...
    question: str              # system prompt shown to Rich
    default_proposed: str      # what the system suggested
    response_raw: str          # Rich's literal reply, e.g. "A A A A" | "accept defaults"
    response_normalised: ResponseKind   # ACCEPT_ALL | ACCEPT_WITH_EDIT | REJECT | DEFER | CUSTOM
    accepted_default: bool
    custom_content: str | None
    timestamp: datetime | None
```

### CalibrationAdjustment

Proposed bias-entity produced by `forge.learning` when override patterns cross thresholds (per ADR-ARCH-019). Rich confirms via CLI approval round-trip; entity written to `forge_pipeline_history`.

```python
class CalibrationAdjustment:
    target_capability: str          # e.g. "review_specification"
    project_scope: str | None       # None = fleet-wide
    observed_pattern: str           # "6 of 10 flag-for-reviews overridden at scores 0.78-0.82"
    proposed_bias: str              # human-readable adjustment
    approved_by_rich: bool
    approved_at: datetime | None
    expires_at: datetime | None     # optional — biases can be time-bounded
```

### AgentManifest (imported from nats-core)

The **only** source of truth for what a fleet specialist can do. Consumed by `forge.discovery` at runtime — never hardcoded in Forge (ADR-ARCH-015).

See [agent-manifest-contract.md](../../../nats-core/docs/design/contracts/agent-manifest-contract.md) for the full schema. Forge reads:
- `tools: list[ToolCapability]` — precise capability declarations for `dispatch_by_capability`
- `intents: list[IntentCapability]` — fallback matching for fuzzy reasoning dispatch
- `trust_tier: core | specialist | extension` — filters candidate set
- `status` — excludes `degraded` agents from primary resolution

### CapabilityResolution

Output of `forge.discovery.resolve(tool_name, intent_pattern=...)`. Records which fleet agent served a given capability request — written to `forge_pipeline_history` for future training ("when two agents advertised the same capability, prefer the one that passed last time").

```python
class CapabilityResolution:
    requested_tool: str
    requested_intent: str | None
    matched_agent_id: str | None    # None = unresolved → degraded mode
    match_source: Literal["tool_exact", "intent_pattern", "unresolved"]
    competing_agents: list[str]
    resolved_at: datetime
```

### TriggerSource / BuildQueuedPayload (imported from nats-core)

Per ADR-SP-014 Pattern A, builds enter Forge via one topic (`pipeline.build-queued.*`) regardless of source. Source metadata carried in payload:

```python
# Summary — canonical schema in nats-core
class BuildQueuedPayload:
    feature_id: str
    repo: str
    branch: str
    feature_yaml_path: str
    triggered_by: Literal["cli", "jarvis", "forge-internal", "notification-adapter"]
    originating_adapter: Literal[
        "terminal", "voice-reachy", "telegram", "slack", "dashboard", "cli-wrapper"
    ] | None
    originating_user: str | None
    correlation_id: str
    parent_request_id: str | None   # Jarvis dispatch ID for progress routing
    max_turns: int = 5
    sdk_timeout: int = 1800
    queued_at: datetime
```

---

## Relationships

```
Build (1) ─── (many) StageLogEntry
Build (1) ─── (1) BuildQueuedPayload (origin; in NATS)
Build (1) ─── (1..N) GateDecision  (one per scored dispatch)
Build (0..1) ─── (1) pull-consumed JetStream message (unacked until terminal)

CalibrationEvent (many) ─── (0..N) GateDecision
    # priors inform gate decisions, recorded as evidence references

CapabilityResolution (1) ─── (1) StageLogEntry
    # every fleet-specialist dispatch has a resolution record

CalibrationAdjustment (many) ─── (many) Build
    # biases retrieved into future builds' system prompts
```

---

## State Transitions (Build Lifecycle)

See [forge-pipeline-architecture.md §6](../research/forge-pipeline-architecture.md#6-forge-state-machine) for the canonical diagram. Summary:

```
                 JetStream delivers
   IDLE ─────────────────────────────► PREPARING ─────► RUNNING
    ▲                                                     │
    │                                          ┌──────────┴──────────┐
    │                                          ▼                     ▼
    │                                     FINALISING             PAUSED
    │                                          │         (interrupt(); awaits
    │                                          ▼           ApprovalResponse
    │                                     COMPLETE          via NATS)
    │                                          │                     │
    │                                          │  ◄──────────────────┘
    └──────────────────────────────────────────┘   (resume after approval)

  On crash:  JetStream redelivery + SQLite reconciliation
              RUNNING → INTERRUPTED (retry from scratch, fresh git checkout)
              PAUSED  → re-enters PAUSED (re-emit ApprovalRequestPayload)
```

**Key realisation changes (Revision 9):**
- RUNNING is a single lifecycle state; inside it, the DeepAgents graph operates freely — not modelled as sub-states
- PAUSED is implemented via LangGraph `interrupt()` (ADR-ARCH-021) — not custom pause wiring
- State-machine module operates on terminal conditions and external observability (JetStream acks, SQLite writes, event emission), not on pipeline-stage sequencing

---

## Module Ownership (data → owner)

| Entity | Owning module | Durability |
|---|---|---|
| `Build`, `StageLogEntry` | `forge.adapters.sqlite` | Durable — `~/.forge/forge.db` WAL |
| `GateDecision` | `forge.gating` (constructs), `forge.adapters.graphiti` (writes) + `forge.adapters.sqlite` (stage_log mirror) | Durable via both stores |
| `CalibrationEvent` | `forge.calibration` (parses), `forge.adapters.graphiti` (writes) | Durable in `forge_calibration_history` |
| `CalibrationAdjustment` | `forge.learning` (proposes), `forge.adapters.graphiti` (writes after Rich's approval) | Durable in `forge_pipeline_history` |
| `CapabilityResolution` | `forge.discovery` (constructs), `forge.adapters.graphiti` (writes) | Durable |
| `AgentManifest` | `nats-core` library (external) | Durable in NATS KV `agent-registry` — Forge reads only |
| `BuildQueuedPayload` | `nats-core` (external) | JetStream `PIPELINE` stream |
| Reply-subject correlation state | `forge.adapters.nats` | Per-call; cleared on reply or timeout |
| Pending approval state (during `interrupt()`) | LangGraph runtime + `forge.adapters.nats` | Until approval received or graph restarted |

---

## Not Modelled (deliberate exclusions)

- **"Stage kind" enum** — absent by design (ADR-ARCH-016). Stages are emergent labels from the reasoning model's `write_todos` output, not a controlled vocabulary.
- **Notification config entity** — there is no per-stage-kind or per-event-type notification configuration (ADR-ARCH-019). Notifications are reasoning outputs informed by retrieved Rich-ack-behaviour priors.
- **Training mode flag** — no such entity (ADR-ARCH-019). Training-like conservatism emerges from low calibration sample count; it is not toggled.
- **Threshold entity per tool** — no pre-declared threshold rows (ADR-ARCH-019). Priors in Graphiti bias reasoning; `forge.yaml.gate_defaults` does NOT exist.
- **PM tool entities (tickets, Kanban cards, Linear records)** — absent by architectural principle (anchor §11). Structured documents + PRs replace tickets (D33–D38).
- **Per-user ACL entities** — no per-user credentials in V1 (Cat 4, Q16). APPMILLA account is the trust boundary.

---

*Domain model last updated with the Category 2 revisions (Revs 3, 5, 6, 7, 8, 9) through Category 6 refinements.*
