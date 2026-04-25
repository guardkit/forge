# Forge Build Plan — Pipeline Orchestrator & Checkpoint Manager

## Status: `/system-arch` ✅ done · `/system-design` ✅ done · `/feature-spec` 🟡 **5 / 8 complete** (FEAT-FORGE-001..005 ✅ · 3 remaining; later steps blocked on specialist-agent Phase 3 + NATS infra running)
## Repo: `guardkit/forge`
## Agent ID: `forge`
## Target: Post specialist-agent Phase 3 completion
## Depends On: nats-core (✅ v0.2.0 inc. TASK-NCFA-003), nats-infrastructure (✅ configured, ◻ running), specialist-agent Phase 3 (◻)

### Progress log

| Step | Command | Status | Date | Commit | Artefacts |
|---|---|---|---|---|---|
| 1 | `/system-arch` | ✅ complete | 2026-04-18 | `9f41e22` (seeded by later refinements) | `docs/architecture/ARCHITECTURE.md`, `system-context.md`, `container.md`, `domain-model.md`, `assumptions.yaml`, **31 ADRs** (`ADR-ARCH-001`..`031`) |
| 2 | `/system-design` | ✅ complete | 2026-04-23 | `b40365c` | `docs/design/` — **9 API contracts + 5 data models + 6 DDRs + 2 C4 L3 diagrams**; 20 artefacts seeded into Graphiti (`project_design` + `architecture_decisions`) |
| 3 | `/feature-spec × 8` | 🟡 in progress (5 / 8) | 2026-04-24 (FEAT-FORGE-001..005) | — | FEAT-FORGE-001..004 ✅ (see Step 3 for per-feature details); FEAT-FORGE-005 ✅ → `features/guardkit-command-invocation-engine/` (32 scenarios, 7 assumptions resolved — 4 high, 3 medium, 0 low) |
| 4 | `/feature-plan × 8` | ◻ pending | — | — | — |
| 5 | `autobuild × 8` (Waves 1–6) | ◻ pending | — | — | — |
| 6 | Validation | ◻ pending | — | — | — |
| 7 | FinProxy first real run | ◻ pending | — | — | — |

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
      NATS-callable.
- [ ] **At least one specialist agent NATS-callable** — verified end-to-end: Forge can
      call `call_agent_tool()` on architect-agent, receive `ResultPayload` with
      `coach_score`, `criterion_breakdown`, `detection_findings` in the result dict

- [ ] **Specialist-agent dual-role deployment** — `--role` flag wired to manifest builder; `get_product_owner_manifest()` exists; `agent_id` derived from role or overridable via `SPECIALIST_AGENT_ID`; PO + Architect can run concurrently on the same NATS without fleet registration collision. See ADR-SP-015 in anchor v2.2 §9.

### Soft Prerequisites (valuable but not blocking)

- [ ] **specialist-agent Phase 1B complete** — unified harness with `--role` flag. If
      not ready, Forge can delegate to architect role only (single-role degraded mode)
- [ ] **specialist-agent Phase G complete** — Graphiti runtime. If not ready, Forge
      skips `graphiti_seed` steps (knowledge doesn't compound but pipeline still works)
- [ ] **specialist-agent Phase F complete** — fine-tuned models on Bedrock. If not
      ready, specialist agents use base models (lower quality but pipeline still works)

### Context Manifests (Cross-Repo Dependency Maps)

The Forge reads `.guardkit/context-manifest.yaml` from each target repo to discover
cross-repo dependencies and their key docs. These manifests are the data source for
`src/forge/commands/context.py` — the module that constructs `--context` flags for
GuardKit command invocations.

Manifests must exist in target repos before the Forge can assemble context-aware
command invocations:

- [x] **forge** — `.guardkit/context-manifest.yaml` (4 dependencies: nats-core,
      specialist-agent, nats-infrastructure, guardkit)
- [ ] **lpa-platform** — `.guardkit/context-manifest.yaml` (3 dependencies: nats-core,
      finproxy-docs, dotnet-functional-fastendpoints-exemplar)
- [ ] **specialist-agent** — `.guardkit/context-manifest.yaml` (3 dependencies:
      nats-core, nats-infrastructure, agentic-dataset-factory — phase-tagged)

Manifests are YAML files listing each dependency's path, relationship, and key docs
with categories (specs, contracts, decisions, source, product, architecture). The Forge
uses categories to filter context by command type: `/feature-spec` pulls specs +
contracts; `/system-arch` pulls architecture + decisions.

See `forge-context-manifest.yaml`, `lpa-platform-context-manifest.yaml`, and
`specialist-agent-context-manifest.yaml` for the current manifests.

### Context Documents Available

These documents will be used as `--context` inputs during the build:

| Document | Path | Used In |
|----------|------|---------|
| **Forge pipeline architecture v2.2** | `forge/docs/research/forge-pipeline-architecture.md` | **Primary context for /system-arch** |
| Forge build-plan alignment review | `forge/docs/research/forge-build-plan-alignment-review.md` | /system-arch (drift history, supporting context) |
| Pipeline orchestrator refresh v3 | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | /system-arch (supporting — see TASK-FVD3 for corrections) |
| Original pipeline motivation | `forge/docs/research/pipeline-orchestrator-motivation.md` | /system-arch |
| Original conversation starter | `forge/docs/research/pipeline-orchestrator-conversation-starter.md` | /system-arch |
| Fleet master index v2 | `forge/docs/research/ideas/fleet-master-index.md` | /system-arch (see TASK-FVD4 for corrections) |
| Specialist agent vision | `specialist-agent/docs/research/ideas/architect-agent-vision.md` | /system-arch |
| nats-core system spec | `nats-core/docs/design/specs/nats-core-system-spec.md` | /system-arch, /system-design |
| Agent manifest contract | `nats-core/docs/design/contracts/agent-manifest-contract.md` | /system-design |
| Forge pipeline config example | (to be produced by /system-arch) | /system-design, /feature-spec |

---

## Feature Summary

| # | Feature | Depends On | Est. Duration | Description | Anchor §10 Phase 4 Coverage |
|---|---------|-----------|---------------|-------------|----------------------------|
| FEAT-FORGE-001 | Pipeline State Machine & Configuration | — | 2-3 days | Core state machine (IDLE→PREPARING→RUNNING→FINALISING→COMPLETE/FAILED per anchor §6), project config loading (`forge.yaml`), crash recovery, sequential build queue | JetStream pull consumer, state machine, `forge.yaml` config, crash recovery |
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
- [`API-sqlite-schema.md`](../../design/contracts/API-sqlite-schema.md) — `builds` + `stage_log` DDL + WAL
- [`API-tool-layer.md`](../../design/contracts/API-tool-layer.md) — all `@tool` functions + 11 GuardKit wrappers
- [`API-subagents.md`](../../design/contracts/API-subagents.md) — sync `build_plan_composer` + async `autobuild_runner`
- [`API-subprocess.md`](../../design/contracts/API-subprocess.md) — GuardKit/git/gh via DeepAgents `execute`

Five data models ([`forge/docs/design/models/`](../../design/models/)):
- [`DM-build-lifecycle.md`](../../design/models/DM-build-lifecycle.md) — `Build` + `StageLogEntry` + state machine transitions
- [`DM-gating.md`](../../design/models/DM-gating.md) — `GateDecision`, `GateMode`, `PriorReference`, `DetectionFinding`
- [`DM-calibration.md`](../../design/models/DM-calibration.md) — `CalibrationEvent`/`Adjustment` + ingestion + learning pipeline
- [`DM-discovery.md`](../../design/models/DM-discovery.md) — `CapabilityResolution` + live cache invalidation
- [`DM-graphiti-entities.md`](../../design/models/DM-graphiti-entities.md) — entity + edge shapes in `forge_pipeline_history` + `forge_calibration_history`

Six DDRs ([`forge/docs/design/decisions/`](../../design/decisions/)):
- [`DDR-001`](../../design/decisions/DDR-001-reply-subject-correlation.md) — reply-subject correlation (Convention B)
- [`DDR-002`](../../design/decisions/DDR-002-resume-value-rehydration-helper.md) — `resume_value_as` helper in `forge.adapters.langgraph`
- [`DDR-003`](../../design/decisions/DDR-003-sqlite-schema-layout-wal.md) — SQLite WAL + STRICT tables
- [`DDR-004`](../../design/decisions/DDR-004-graphiti-group-partitioning.md) — two Graphiti groups
- [`DDR-005`](../../design/decisions/DDR-005-cli-context-manifest-resolution.md) — context-manifest resolver placement + category table
- [`DDR-006`](../../design/decisions/DDR-006-async-subagent-state-channel-contract.md) — `AutobuildState` schema for `async_tasks` channel

Two C4 L3 diagrams ([`forge/docs/design/diagrams/`](../../design/diagrams/)):
- [`agent-runtime.md`](../../design/diagrams/agent-runtime.md) — Agent Runtime components (16 nodes, approved)
- [`domain-core.md`](../../design/diagrams/domain-core.md) — Domain Core components (15 nodes, approved)

**Deliberately NOT produced (per ADRs):**
- ❌ `openapi.yaml` — Forge has no HTTP/REST surface (ADR-ARCH-012)
- ❌ `mcp-tools.json` — no MCP interface (ADR-ARCH-012)
- ❌ `a2a-schemas.yaml` — fleet uses NATS request/reply, not A2A protocol (ADR-ARCH-003)
- ❌ `DESIGN.md` monolith — replaced by the per-container contract/model set above (richer, more localisable)
- ❌ `forge-system-spec.md` (BDD acceptance criteria) — belongs to `/feature-spec` (Step 3), not `/system-design`

**Validation (what held vs what the plan expected):**
- ✅ Pipeline config schema fully specified in [`forge-build-plan.md#Pipeline-Configuration-Schema`](./forge-build-plan.md#pipeline-configuration-schema) — no changes needed from the anchor-v2.2 shape
- ✅ Checkpoint protocol contract specifies exact NATS topic patterns — see [`API-nats-approval-protocol.md`](../../design/contracts/API-nats-approval-protocol.md) §2 + §3.2
- ✅ GuardKit command invocation contract specifies subprocess interface + env + output discovery — [`API-subprocess.md`](../../design/contracts/API-subprocess.md) §3 + [`API-tool-layer.md`](../../design/contracts/API-tool-layer.md) §6
- ✅ State machine transitions formally defined — [`DM-build-lifecycle.md §2.1`](../../design/models/DM-build-lifecycle.md#21-valid-transitions)
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
- ✅ **FEAT-FORGE-004** — Confidence-Gated Checkpoint Protocol (2026-04-24)
  - Artefacts: `features/confidence-gated-checkpoint-protocol/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 32 (8 @key-example · 5 @boundary · 7 @negative · 10 @edge-case · 4 @smoke · 4 @regression · 2 @security · 2 @concurrency · 2 @data-integrity · 1 @integration)
  - Assumptions: 7 resolved — 5 high, 2 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - ASSUM-003 (behaviour at max-wait ceiling) kept at medium — API-nats-approval-protocol §7 describes refresh up to ~3600s but does not describe the terminal action; specific fallback (cancel / escalate / fail-open) deferred to `forge-pipeline-config`.
    - ASSUM-007 (expected-approver allowlist) kept at medium — API §4.1 shows `responder` as free-form string (`rich` / Jarvis adapter id); allowlist semantics implied by the constitutional framing but not explicitly enumerated.
- ✅ **FEAT-FORGE-005** — GuardKit Command Invocation Engine (2026-04-24)
  - Artefacts: `features/guardkit-command-invocation-engine/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 32 (7 @key-example · 6 @boundary · 10 @negative · 14 @edge-case · 3 @smoke) — Groups A–D plus security / concurrency / integration-boundary expansion accepted.
  - Assumptions: 7 resolved — 4 high, 3 medium, 0 low, 0 open. Ready for `/feature-plan`.
    - 4 high-confidence values drawn from explicit context: default subprocess timeout (600s, API-subprocess.md §2), dependency-traversal depth cap (2 levels, DDR-005), stdout-tail size (4 KB, API-subprocess.md §3.4), and feature-spec manifest-category filter (specs / contracts / source / decisions, DDR-005).
    - ASSUM-005 (retry-with-additional-context) kept at medium — build plan §130 mandates "error handling and retry" but API-tool-layer.md §6 leaves retry shape to the reasoning loop; minimal inferred shape confirmed (fresh subprocess, explicit + manifest paths merged).
    - ASSUM-006 (parallel `guardkit_*` within a single build) kept at medium — not prohibited by any contract but no explicit concurrency statement in API-tool-layer.md §6.
    - ASSUM-007 (no cached state across concurrent builds) kept at medium — DDR-005 describes the resolver as a stateless function but does not explicitly forbid caching.
- ✅ **FEAT-FORGE-006** — Infrastructure Coordination (2026-04-24)
  - Artefacts: `features/infrastructure-coordination/{slug}.feature` · `{slug}_assumptions.yaml` · `{slug}_summary.md`
  - Scenarios: 43 (10 @key-example · 5 @boundary · 7 @negative · 14 @edge-case · 6 @smoke · 6 @security · 3 @concurrency · 4 @data-integrity · 3 @integration) — Groups A–E plus security / concurrency / data-integrity / integration expansion accepted.
  - Assumptions: 8 resolved — 2 high, 3 medium, 3 low, **3 open (REVIEW REQUIRED)**. Ready for `/feature-plan` subject to low-confidence review.
    - 2 high-confidence values drawn from explicit context: override recency horizon (30 days, DM-graphiti-entities.md §4) and default PR base branch (`main`, API-subprocess.md §4).
    - ASSUM-003 (test verification command = `pytest` from worktree root) kept at medium — build plan §131 references "test verification" without naming a runner; Python stack convention.
    - ASSUM-004 (failed-test report shape: pass/fail counts + failing node-ids + tail) kept at medium — pytest convention; not specified in any contract.
    - ASSUM-005 (priors empty-context shape = empty narrative section rather than omitted block or error) kept at medium — ADR-ARCH-018 specifies prose-style injection but not the empty case.
    - ⚠️ ASSUM-006 (credential-shape redaction in rationale before Graphiti write) open at **low** — not stated in any provided context; inferred security hygiene only.
    - ⚠️ ASSUM-007 (split-brain mirror dedupe via entity_id uniqueness + pre-check) open at **low** — inferred from CalibrationEvent deterministic id pattern; GateDecision uses UUID so a separate "already written" check is assumed.
    - ⚠️ ASSUM-008 (GateDecision links inside SessionOutcome ordered by decided_at ascending) open at **low** — no ordering semantics specified for `SessionOutcome -[CONTAINS]-> GateDecision` edges in DM-graphiti-entities.md.

> **Context-flag resolution (post-`/system-design`):** placeholders from the original
> build plan resolve as follows. `DESIGN.md` / `forge-system-spec.md` were not produced
> as monolithic files (see Step 2 deviation note); instead each `/feature-spec` pulls
> the relevant per-container contract + data model. The GuardKit context-manifest
> resolver (DDR-005) can drive most of this automatically once
> `.guardkit/context-manifest.yaml` is populated.

```bash
# FEAT-FORGE-001: Pipeline State Machine & Configuration  ✅ COMPLETE (2026-04-24)
# Output: forge/features/pipeline-state-machine-and-configuration/
guardkit feature-spec FEAT-FORGE-001 \
  --context forge/docs/design/models/DM-build-lifecycle.md \
  --context forge/docs/design/contracts/API-sqlite-schema.md \
  --context forge/docs/design/contracts/API-cli.md \
  --context forge/docs/design/decisions/DDR-003-sqlite-schema-layout-wal.md

# FEAT-FORGE-002: NATS Fleet Integration  ✅ COMPLETE (2026-04-24)
# Output: forge/features/nats-fleet-integration/
guardkit feature-spec FEAT-FORGE-002 \
  --context forge/docs/design/contracts/API-nats-pipeline-events.md \
  --context forge/docs/design/contracts/API-nats-fleet-lifecycle.md \
  --context forge/docs/design/models/DM-discovery.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md \
  --context nats-core/docs/design/contracts/agent-manifest-contract.md

# FEAT-FORGE-003: Specialist Agent Delegation  ✅ COMPLETE (2026-04-24)
# Output: forge/features/specialist-agent-delegation/
guardkit feature-spec FEAT-FORGE-003 \
  --context forge/docs/design/contracts/API-nats-agent-dispatch.md \
  --context forge/docs/design/models/DM-discovery.md \
  --context forge/docs/design/decisions/DDR-001-reply-subject-correlation.md \
  --context nats-core/docs/design/specs/nats-core-system-spec.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol  ✅ COMPLETE (2026-04-24)
# Output: forge/features/confidence-gated-checkpoint-protocol/
guardkit feature-spec FEAT-FORGE-004 \
  --context forge/docs/design/contracts/API-nats-approval-protocol.md \
  --context forge/docs/design/models/DM-gating.md \
  --context forge/docs/design/decisions/DDR-002-resume-value-rehydration-helper.md

# FEAT-FORGE-005: GuardKit Command Invocation Engine  ✅ COMPLETE (2026-04-24)
# Output: forge/features/guardkit-command-invocation-engine/
guardkit feature-spec FEAT-FORGE-005 \
  --context forge/docs/design/contracts/API-tool-layer.md \
  --context forge/docs/design/contracts/API-subprocess.md \
  --context forge/docs/design/decisions/DDR-005-cli-context-manifest-resolution.md

# FEAT-FORGE-006: Infrastructure Coordination  ✅ COMPLETE (2026-04-24)
# Output: forge/features/infrastructure-coordination/
guardkit feature-spec FEAT-FORGE-006 \
  --context forge/docs/design/models/DM-graphiti-entities.md \
  --context forge/docs/design/models/DM-calibration.md \
  --context forge/docs/design/contracts/API-subprocess.md \
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

# FEAT-FORGE-003: Specialist Agent Delegation (depends on 002)
guardkit feature-plan "Specialist Agent Delegation" \
  --context forge/features/specialist-agent-delegation/specialist-agent-delegation_summary.md

# FEAT-FORGE-004: Confidence-Gated Checkpoint Protocol (depends on 003)
guardkit feature-plan "Confidence-Gated Checkpoint Protocol" \
  --context forge/features/confidence-gated-checkpoint-protocol/confidence-gated-checkpoint-protocol_summary.md

# FEAT-FORGE-006: Infrastructure Coordination (depends on 001, 002)
guardkit feature-plan "Infrastructure Coordination" \
  --context forge/features/infrastructure-coordination/infrastructure-coordination_summary.md

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

# Integration test: queue a test feature (canonical CLI surface per anchor §5)
forge queue FEAT-TEST-001 --repo guardkit/test-project --branch main
forge status
forge history --feature FEAT-TEST-001

# Verify pipeline events published to NATS
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
on.

1. **CMDW gate — production-image subscription round-trip.** Build the forge
   production container, run `forge serve` inside it, publish one real
   `pipeline.build-queued` message from outside the container, verify the
   subscribed JetStream pull consumer delivers it to an actual pipeline run. A
   stale container build that silently fails to subscribe is the exact
   specialist-agent CMDW failure mode applied to forge. (TASK-MDF-CMDW)

2. **PORT gate — `(specialist_role, forge_stage)` dispatch matrix.** For every
   `(role ∈ {product-owner, architect}, stage)` pair used in Mode A (per
   refresh doc §"Revised Pipeline Flow"), execute one end-to-end round-trip via
   NATS on the production specialist-agent image. Any red pair is a hard stop
   before declaring forge canonical. This is the PORT lesson applied to the
   consumer side: specialist-agent's PORT bug meant the PO handlers were never
   registered, and forge wouldn't detect this until integration time.
   (TASK-MDF-PORT)

3. **ARFS gate — per-tool handler-completeness matrix.** For each tool in the
   forge AgentManifest (`forge_greenfield`, `forge_feature`, `forge_review_fix`,
   `forge_status`, `forge_cancel`), walk the full chain
   `tool-schema → NATS adapter handler → core API → orchestrator method` and
   execute one smoke-test round-trip. Any hop with a `TODO`/`NotImplementedError`
   is a blocker: ARFS proved that unit tests don't catch missing methods when
   schema + adapter are wired but the orchestrator is not. (TASK-MDF-ARFS)

4. **Canonical-freeze live-verification gate.** Before this build plan is
   declared canonical, every shell block in this document MUST have been
   executed verbatim on a clean MacBook + GB10 in a single walkthrough session
   and logged in `command-history.md`. Annotate any block that required
   workarounds with `[as of commit <sha>]`. Per LES1 §8: guide copy-paste blocks
   are code; a CI-passing guide can still fail on a clean machine (cd /tmp
   workarounds, wrong Python pins, omitted provisioning — all found in the
   specialist-agent canonical guide at retest time).

> **Note:** Mode-based wrappers (`forge greenfield`, `forge feature`, `forge review-fix`) are optional higher-level wrappers around `forge queue` and may be added later if they earn their place. The canonical CLI surface is `forge queue`.

### Step 7: First Real Run — FinProxy

Once validation passes, run the Forge on FinProxy as the first real pipeline:

```bash
forge queue FEAT-FINPROXY-001 --repo guardkit/finproxy --branch main
forge status
forge history --feature FEAT-FINPROXY-001
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
| `src/forge/commands/context.py` | 005 | Create — --context flag construction from pipeline state + context manifests (reads `.guardkit/context-manifest.yaml` from target repo, resolves cross-repo paths, filters by command category) |
| `.guardkit/context-manifest.yaml` | 005 | Create — Forge's own cross-repo dependency manifest (nats-core, specialist-agent, nats-infrastructure, guardkit) |
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
| `pyproject.toml` | 001 | Create — core deps: nats-core, pydantic, pydantic-settings. **`[providers]` extra must list every LangChain integration named anywhere in `src/`** (e.g. langchain-anthropic, langchain-openai, langchain-google-genai). Per specialist-agent LES1 §3 (LCOI retest finding): transitive pulls by deepagents do **not** cover every declared provider — each must be explicit. |
| `tests/` | all | Create — test files per feature |
| `docs/architecture/ARCHITECTURE.md` | /system-arch | Create |
| `docs/design/DESIGN.md` | /system-design | Create |
| `docs/design/specs/forge-system-spec.md` | /system-design | Create |
| `command-history.md` | all | Create — running log of all commands run |
| `.env.example` | 001 | Create — template with placeholder, non-real values. **Never ship a real-looking provider key anywhere in committed `.env*` files.** Per specialist-agent LES1 §3 (retest-env): a placeholder like `OPENAI_API_KEY=not_needed` in `.env` silently overrode the operator's shell-env real key via Compose `${VAR}` interpolation, producing HTTP 401 `"Incorrect API key provided: not_needed"`. Pre-merge gate: CI check scanning tracked `.env*` for `[A-Z_]+_API_KEY=[a-zA-Z0-9-]{20,}` fails the build. |
| `Dockerfile` | (deferred, FEAT-FORGE-009+) | Create when forge containerizes. **When added, `pip install .[providers]` (not `pip install .`) — literal-match to the documented venv install.** Per specialist-agent LES1 §3 (DKRX, commit `8b9d584`): Dockerfile extras ≡ guide extras; any drift is a latent provider-missing bug. Grep check: `pip install .[…]` in Dockerfile must include the same extras the guide prescribes. |

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
max_concurrent: 1       # ADR-SP-012 — sequential builds only

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

## Jarvis Integration

The Forge supports multiple build trigger sources via ADR-SP-014 (Pattern A — accepted in anchor v2.2 §9). Jarvis publishes `BuildQueuedPayload` to `pipeline.build-queued.{feature_id}` — the same JetStream topic that `forge queue` CLI publishes to. Forge consumes without distinguishing sources at the consumer level; the payload's `triggered_by`, `originating_adapter`, and `correlation_id` fields carry source metadata for history, diagnostics, and routing progress events back to the originator.

The build plan does **not** require Jarvis to function. The CLI (`forge queue`) is the default and simplest path. Jarvis adds the voice (Reachy Mini), Telegram, dashboard, and CLI-wrapper entry points. For the full `BuildQueuedPayload` design including Jarvis-aware fields, correlation flow, and example payloads, see [forge-build-plan-alignment-review.md Appendix C](../forge-build-plan-alignment-review.md#appendix-c--buildqueuedpayload-full-design-jarvis-aware).

Forge also registers on `fleet.register` as an agent (`agent_id=forge`, intents: `build.*`, `pipeline.*`, `max_concurrent=1`) so that Jarvis's CAN-bus routing can discover it. Registration is for discovery; triggering remains a JetStream publish.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Subprocess orchestration complexity | GuardKit commands invoked as subprocesses may have environment/path issues | FEAT-FORGE-005 builds a robust invoker with env setup, working dir management, and output discovery |
| NATS async coordination | Request-reply with `call_agent_tool()` may timeout under load | Configurable timeout per delegation, retry with backoff, degraded mode fallback |
| Specialist agent not available | Pipeline blocks if required agent is down | Degraded mode: fall back to direct GuardKit commands, force FLAG FOR REVIEW |
| State machine complexity | 3 modes × multiple stages × checkpoint states | State machine formally defined in /system-design, tested independently in FEAT-FORGE-001 |
| First-time integration | Everything connects for the first time in FEAT-FORGE-007 | Use small test corpus first, not FinProxy. Debug integration issues before the real run. |
| Orphan containers from parallel waves | Deleted Conductor worktrees leave Docker containers labelled against a path that no longer exists; subsequent waves hit port conflicts and `docker compose down` becomes a silent no-op | Always tear down with `docker compose down --remove-orphans`. Document the label-inspection flow (`docker ps --filter label=com.docker.compose.project=<name>`) in each wave's cleanup step. Per specialist-agent LES1 §7 (TASK-MDF-ORPH): this pattern cost a walkthrough iteration to diagnose. |
| CLI credential leakage | `forge status`, `forge history`, `forge queue` may print NATS URLs with embedded credentials, or log KV-registry values containing secrets | CLI outputs must default to redaction. Any value matching `nats://[^@]+@.*` or `*_PASSWORD=.*` renders as `***`; `--verbose` opts in to plaintext with a displayed warning. Per specialist-agent LES1 §2/§7: `nats account info` leaked `RICH_NATS_PASSWORD` into the walkthrough log — the same shape applies to `forge status`. |

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
| **Forge pipeline architecture v2.2** | `forge/docs/research/forge-pipeline-architecture.md` | **Primary anchor** — pipeline stages, state machine, NATS topics, payloads, ADRs SP-010..017 |
| Forge build-plan alignment review | `forge/docs/research/forge-build-plan-alignment-review.md` | Drift analysis, correction list, Appendix C (BuildQueuedPayload), Appendix D (specialist-agent refactor) |
| Pipeline orchestrator refresh v3 | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` | Scope: Forge identity, checkpoint protocol, tool inventory, NATS integration, degraded mode |
| Original motivation | `forge/docs/research/pipeline-orchestrator-motivation.md` | Why: 93% defaults, 3 decisions, 4:1 leverage |
| Original conversation starter | `forge/docs/research/pipeline-orchestrator-conversation-starter.md` | Three modes, multi-project, execution environments |
| Fleet master index v2 | `forge/docs/research/ideas/fleet-master-index.md` | Fleet context, decisions D1-D38, repo map, build sequence |
| Specialist agent vision | `specialist-agent/docs/research/ideas/architect-agent-vision.md` | Three roles, three-layer architecture, delegation targets |
| nats-core system spec | `nats-core/docs/design/specs/nats-core-system-spec.md` | Payload schemas, topic registry, client API |
| Agent manifest contract | `nats-core/docs/design/contracts/agent-manifest-contract.md` | AgentManifest, ToolCapability, IntentCapability |
| This build plan | `forge/docs/research/ideas/forge-build-plan.md` | Command sequence, feature summary, prerequisites |
| Forge context manifest | `.guardkit/context-manifest.yaml` | Cross-repo dependency map (nats-core, specialist-agent, nats-infrastructure, guardkit) |
| LPA platform context manifest | `lpa-platform/.guardkit/context-manifest.yaml` | Cross-repo dependency map (nats-core, finproxy-docs, exemplar) |
| Specialist agent context manifest | `specialist-agent/.guardkit/context-manifest.yaml` | Cross-repo dependency map (nats-core, nats-infrastructure, dataset factory) |

---

*Build plan created: 12 April 2026*
*Updated: 23 April 2026 — Steps 1 & 2 complete; nats-core v0.2.0 shipped (TASK-NCFA-003)*
*Status: Ready for `/feature-spec × 8` (Step 3) once specialist-agent Phase 3 + NATS infra are running*
*"The Forge is the capstone. It's the last major agent to build because it coordinates everything else. But it's also the highest-leverage: once it works, the Software Factory is real."*
