# Forge — System Architecture

> **Version:** 1.0
> **Generated:** 2026-04-18 via `/system-arch`
> **Status:** Architecture Design — ready for `/system-design`
> **Supersedes:** none (first-pass architecture for the Forge repo)
> **Related anchors:**
> - [forge-pipeline-architecture.md](../research/forge-pipeline-architecture.md) v2.2 (16 Apr 2026) — pipeline anchor
> - [forge-pipeline-orchestrator-refresh.md](../research/ideas/forge-pipeline-orchestrator-refresh.md) v3 (11 Apr 2026) — checkpoint protocol
> - [fleet-master-index.md](../research/ideas/fleet-master-index.md) — fleet context

---

## 1. What Forge Is

Forge is the NATS-native pipeline orchestrator for the Software Factory. It is a long-running DeepAgents agent harness that consumes `pipeline.build-queued.{feature_id}` messages from JetStream and drives features through specification → architecture → planning → implementation → PR, applying confidence-gated reasoning at each stage.

**The human moves from operator to approver.** The reasoning model makes decisions the human was making (module decomposition, context assembly, stage ordering) using calibration priors learned from Rich's past sessions; Rich reviews when confidence is low.

**Forge is not a specialist.** It is a coordinator. Domain judgment is delegated to fleet specialist agents via runtime-discovered capabilities. Forge uses a strong reasoning model (Gemini 3.1 Pro primary) for orchestration decisions — not fine-tuned domain knowledge.

---

## 2. Structural Pattern

**Hexagonal modules inside a DeepAgents two-model orchestrator.**

- The `create_deep_agent(...)` compiled state graph is the shell — reasoning loop, built-in tools (`write_todos`, filesystem, `execute`, `task`, `interrupt`), sub-agent dispatch.
- Inside: pure domain modules (gating, state machine, notifications, learning, calibration, discovery) with no I/O imports.
- Thin adapters at the edges: NATS (via `nats-core`), SQLite, Graphiti, subprocess. Forge-specific `@tool` functions wrap adapters at the DeepAgents tool-layer boundary.
- No transport abstraction (ADR-ARCH-003) — NATS is the transport, not a replaceable plugin.

See [system-context.md](system-context.md) for C4 Level 1 and [container.md](container.md) for C4 Level 2.

---

## 3. Module Map (15 modules in 5 groups)

### A. DeepAgents Shell
- `forge.agent` — wires `create_deep_agent()` → `CompiledStateGraph`; exported via `langgraph.json`
- `forge.prompts` — system prompt templates with `{date}`, `{domain_prompt}`, `{available_capabilities}`, `{calibration_priors}`, `{project_context}` placeholders injected at build start
- `forge.subagents` — 2 pre-declared: `build_plan_composer`, `autobuild_runner`. Everything else spawned via `task()` on demand (ADR-ARCH-020)

### B. Domain Core (pure, no I/O imports)
- `forge.gating` — reasoning-driven gate decisions informed by retrieved priors (ADR-ARCH-019)
- `forge.state_machine` — build-lifecycle states (QUEUED→…→COMPLETE/FAILED); PAUSED via LangGraph `interrupt()` (ADR-ARCH-021)
- `forge.notifications` — decides what to emit based on retrieved Rich-ack-behaviour priors
- `forge.learning` — detects override patterns, proposes `CalibrationAdjustment` entities to Graphiti (never to YAML)
- `forge.calibration` — parses history files into `CalibrationEvent` stream (ADR-ARCH-006)
- `forge.discovery` — runtime fleet capability resolution via `NATSKVManifestRegistry` with live watch (ADR-ARCH-015, ADR-ARCH-017)
- `forge.history_labels` — trivial helper writing reasoning-model's stage labels to SQLite `stage_log`

### C. Tool Layer (`@tool(parse_docstring=True)` functions — Forge-specific only)
- `dispatch_by_capability` — single generic tool for all fleet dispatch; replaces role-specific tools
- `approval_tools` — build `ApprovalRequestPayload`s for `interrupt()` return values
- `notification_tools` — emit `NotificationPayload` via NATS
- `graphiti_tools` — `record_override`, `write_gate_decision`, `read_override_history`, `write_session_outcome`
- `guardkit_*` — thin wrappers over DeepAgents `execute` for the full GuardKit CLI (11 commands — ADR-ARCH-004)
- `history_tools` — SQLite schema'd writes (`BuildRow`, `StageRow`)

**Not needed:** `git_tools`, `pr_tools`, `file_tools`, `queue_tools` — subsumed by DeepAgents built-ins (`execute`, `read_file`/`write_file`, etc.) per ADR-ARCH-020.

### D. Adapters (I/O edges)
- `forge.adapters.nats` — JetStream pull consumer (`max_ack_pending=1` — ADR-ARCH-014), publish, KV read, fleet watch, approval round-trip publisher
- `forge.adapters.sqlite` — `~/.forge/forge.db` (WAL); `builds` + `stage_log`; crash-recovery reconciliation
- `forge.adapters.guardkit` — subcommand composition + progress-stream parser on top of `execute`
- `forge.adapters.graphiti` — read/write against `forge_pipeline_history` + `forge_calibration_history` groups
- `forge.adapters.history_parser` — tokenises markdown history files → `CalibrationEvent` stream

### E. Cross-cutting
- `forge.config` — `AgentConfig` + `forge.yaml` loader; infrastructure + models + constitutional rules + learning meta-config only (ADR-ARCH-019, ADR-ARCH-023)
- `forge.cli` — Click CLI (`queue | status | history | cancel | skip`); reads SQLite direct, writes via NATS (ADR-ARCH-013)
- `forge.fleet` — Forge's own `fleet.register` publication + heartbeat lifecycle

---

## 4. Technology Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Agent framework | LangChain DeepAgents 0.5.3+ (2026-04-15) |
| Graph runtime | LangGraph — `langgraph.json` / `langgraph dev` / `CompiledStateGraph` |
| Model client | `init_chat_model("provider:model")` — provider-neutral (ADR-ARCH-010) |
| Reasoning model (primary) | `google_genai:gemini-3.1-pro` |
| Implementation model (primary) | `google_genai:gemini-2.5-flash` |
| Fallback providers | Anthropic Opus 4.7/Sonnet 4.6, OpenAI GPT-5, local vLLM |
| Schemas | Pydantic 2 + pydantic-settings (via `nats-core.AgentConfig`) |
| CLI | Click |
| Async I/O | `asyncio` |
| Testing | pytest, pytest-asyncio, `unittest.mock` (per `pytest-agent-testing-specialist` rule) |
| Lint / type | ruff, mypy `--strict` |
| Internal library | `nats-core` (pip-installed from sibling repo) |

---

## 5. Data Stores

| Store | Purpose | ADR |
|---|---|---|
| SQLite `~/.forge/forge.db` (WAL) | Authoritative build history — `builds` + `stage_log` | ADR-SP-013 |
| JetStream `PIPELINE` (7-day) | Build queue; `max_ack_pending=1` enforces sequential execution | ADR-SP-011, ADR-ARCH-014 |
| JetStream `AGENTS` (7-day) | Specialist commands + results transport | ADR-SP-017 |
| JetStream `FLEET` | Self-registration + heartbeats | fleet-master-index |
| JetStream `JARVIS` + `NOTIFICATIONS` | Outbound notifications routed via Jarvis | anchor §3 |
| NATS KV `agent-registry` | Live fleet discovery table | nats-core ADR-004 |
| FalkorDB via Graphiti (`whitestocks:6379`) | `forge_pipeline_history` + `forge_calibration_history` | ADR-ARCH-005, ADR-ARCH-006 |
| LangGraph Memory Store | Per-thread recall within a running build | ADR-ARCH-022 |
| Per-build worktrees `/var/forge/builds/{build_id}/` | Ephemeral — created on PREPARING, deleted on terminal state | ADR-ARCH-028 |

**NOT used:** LangGraph checkpointer (omitted per ADR-ARCH-009 — JetStream+SQLite sufficient).

---

## 6. Multi-Consumer API Strategy

| Consumer | Protocol | Notes |
|---|---|---|
| Rich | Click CLI | Reads SQLite direct; writes via NATS (ADR-ARCH-013) |
| Jarvis | NATS JetStream publish → `pipeline.build-queued.*` | ADR-SP-014 Pattern A |
| Specialist Agents | NATS request/reply — `agents.command.*` / `agents.result.*.{correlation_id}` | LES1 parity rule on reply-subject correlation |
| Dashboard / notification adapters (future) | NATS pub/sub → `pipeline.*` + `jarvis.notification.*` | Read-only viewports |
| Other fleet agents (future) | NATS send → `agents.command.forge` | Fleet control plane, not build path |
| GuardKit / git / gh | subprocess via DeepAgents `execute` | Outbound callees — not consumers |

**Not used:** MCP (ADR-ARCH-012 — pipeline traffic would overflow context), HTTP/REST, gRPC.

---

## 7. Pipeline as Reasoning, Not Sequence

Forge has **no pre-coded stage catalogue** (ADR-ARCH-016). The reasoning model:

1. Receives a build and retrieves priors (calibration + project context + live fleet capabilities) into the system prompt.
2. Uses DeepAgents' built-in `write_todos` to plan the build's todo list — these todos ARE the emergent stage labels Rich reads in `forge history`.
3. Invokes local `@tool`s (GuardKit commands, etc.) and fleet specialists (via `dispatch_by_capability`) in whatever order makes sense for this build, guided by:
   - The capability descriptions in each registered agent's `AgentManifest`
   - The typical factory shape (spec → arch → plan → build → PR) as prose guidance in the system prompt
   - Retrieved priors from similar prior builds
   - Policy guardrails in `forge.yaml`
4. Evaluates each Coach-scored result and decides auto-approve / flag-for-review / hard-stop in context — no static thresholds (ADR-ARCH-019).
5. Writes every decision + outcome to Graphiti `forge_pipeline_history` — compounds into priors for future builds.

**Training mode is emergent, not a flag.** Few priors → natural reasoning conservatism → frequent flags → Rich approves → priors grow → conservatism relaxes organically.

---

## 8. Confidence Gates

Four gate modes:
- **🟢 AUTO_APPROVE** — reasoning model decides based on Coach score + priors + build risk
- **🟡 FLAG_FOR_REVIEW** — `interrupt()` pauses the graph; NATS publishes `ApprovalRequestPayload`; resume on Rich's reply
- **🔴 HARD_STOP** — terminal failure; build fails; JetStream message acked
- **🛑 MANDATORY_HUMAN_APPROVAL** — early-build-plan override that bypasses Coach score; relaxes to confidence-gated via Graphiti-written `CalibrationAdjustment` once Rich has approved N consecutive plans

**PR review is always human** (constitutional rule, ADR-ARCH-026 belt+braces: prompt AND executor assertion).

---

## 9. Learning Loop

Three Graphiti groups, two write paths:

| Group | Written by | Read by |
|---|---|---|
| `forge_pipeline_history` | `forge.adapters.graphiti` per dispatch (gate decisions, scores, overrides, outcomes) | System prompt retrieval at next build start |
| `forge_calibration_history` | `forge.adapters.history_parser` via batch ingestion of Rich's history files + incremental as new files appear | System prompt retrieval — similar-context priors |

`forge.learning` detects patterns (e.g. "Rich overrode 6/10 recent flag-for-reviews on `review_architecture`") and proposes `CalibrationAdjustment` entities. Rich confirms via CLI approval round-trip; entity lands in Graphiti; future builds retrieve it. **No YAML edits** (ADR-ARCH-019).

---

## 10. Cross-Cutting

| Concern | Approach | ADR |
|---|---|---|
| Auth | NATS account-based (APPMILLA); env-only LLM keys; DeepAgents permissions (fs/shell/network allowlists) | ADR-ARCH-023 |
| Observability | NATS `pipeline.*` event stream + Graphiti audit + SQLite `stage_log` + optional LangSmith; no Prometheus V1 | ADR-ARCH-024 |
| Error handling | Tools return structured error strings, never raise; adapter exceptions converted at tool boundary; degraded mode is reasoning-input | ADR-ARCH-025 |
| Validation | Pydantic at every boundary (NATS, tools, CLI, SQLite, Graphiti, YAML) | D22 fleet-wide |
| Secrets | env only; `structlog` redact-processor; never in `AgentManifest`/logs/Graphiti | agent-manifest-contract |
| Prompt-injection defence | Constitutional rules belt+braces (prompt AND executor assertion) | ADR-ARCH-026 |

---

## 11. Constraints

- **Single instance, no horizontal scaling** (ADR-ARCH-027). Fleet growth = multiple Forge instances (one per operator), never one scaled out.
- **Sequential builds only** (ADR-SP-012 + `max_ack_pending=1`).
- **Best-effort availability**, no SLA (ADR-ARCH-029). Bounded by GB10 + Tailscale + LLM provider uptime.
- **Budget ~£500/month LLM ceiling** (ADR-ARCH-030) — drives Gemini-primary choice and learning-loop cost reduction.
- **Local-first Docker deployment on GB10** alongside NATS and dual-role specialist-agent containers (ADR-ARCH-011).

---

## 12. Relationship to Anchor v2.2

This architecture is a **refinement** of [forge-pipeline-architecture.md](../research/forge-pipeline-architecture.md) v2.2, not a replacement. It:

- Preserves: NATS-native orchestration (ADR-SP-011), sequential builds (ADR-SP-012), JetStream queue + SQLite history (ADR-SP-013), Jarvis as upstream trigger (ADR-SP-014), dual-role specialist deployment (ADR-SP-015), singular topic convention (ADR-SP-016), stream retention reconciliation (ADR-SP-017)
- Extends: the 5-stage pipeline description (anchor §4) becomes prose guidance in the reasoning model's system prompt rather than a hardcoded state machine
- Clarifies: implementation substrate as DeepAgents 0.5.3 built-ins (planning via `write_todos`, filesystem + `execute`, `task` delegation, `interrupt`-based pause, Memory Store, auto-summarisation, permissions system)

---

## 13. Decision Index

30 ADRs captured across the 6 categories. See [decisions/](decisions/) for the full set:

| # | Title | Category |
|---|---|---|
| ADR-ARCH-001 | Clean/Hexagonal modules within DeepAgents two-model orchestrator | Structural pattern |
| ADR-ARCH-002 | Two-model separation — reasoning drives graph, implementation executes within tools/sub-agents | Structural pattern |
| ADR-ARCH-003 | NATS-native orchestration via `nats-core` adapters, no transport ABC | Structural pattern |
| ADR-ARCH-004 | Full GuardKit CLI as tool surface — one `@tool` per subcommand | Tool layer |
| ADR-ARCH-005 | Graphiti-fed learning loop — per-capability priors retrieved + outcomes written back | Learning |
| ADR-ARCH-006 | Calibration corpus — ingest history files as CalibrationEvent stream | Learning |
| ADR-ARCH-007 | Build Plan as explicit gated artefact with relaxation criteria | Gating |
| ADR-ARCH-008 | Forge produces its own history files in Pattern-2/Pattern-3 format | Pattern propagation |
| ADR-ARCH-009 | Omit LangGraph checkpointer — JetStream+SQLite sufficient | Data stores |
| ADR-ARCH-010 | Provider-neutral two-model configuration via `init_chat_model` | Models |
| ADR-ARCH-011 | Local-first Docker deployment on GB10 | Deployment |
| ADR-ARCH-012 | No MCP interface for Forge | API strategy |
| ADR-ARCH-013 | CLI read path bypasses NATS | API strategy |
| ADR-ARCH-014 | Single JetStream consumer (`max_ack_pending=1`) enforces sequential builds | API strategy |
| ADR-ARCH-015 | Capability-driven specialist dispatch — no `agent_id` hardcoding | Fleet integration |
| ADR-ARCH-016 | Fleet is the catalogue — no pre-coded stage kinds | Fleet integration |
| ADR-ARCH-017 | Live fleet-registry watching for hot-swap capability detection | Fleet integration |
| ADR-ARCH-018 | Calibration priors as retrievable system-prompt input | Learning |
| ADR-ARCH-019 | No static behavioural config — gates/thresholds/notifications/training-mode are all reasoning outputs | Agent autonomy |
| ADR-ARCH-020 | Adopt DeepAgents 0.5.3 built-ins (`write_todos`, filesystem, `execute`, `task`, `interrupt`, Memory Store, permissions, auto-summarisation) | Implementation substrate |
| ADR-ARCH-021 | PAUSED state realised as LangGraph `interrupt()` | State machine |
| ADR-ARCH-022 | Dual agent memory — Memory Store + Graphiti | Data stores |
| ADR-ARCH-023 | Permissions as constitutional safety config — NOT reasoning-adjustable | Security |
| ADR-ARCH-024 | Observability = NATS event stream + Graphiti + SQLite stage_log; no Prometheus V1 | Observability |
| ADR-ARCH-025 | Tool error handling — return structured error strings, never raise | Error handling |
| ADR-ARCH-026 | Constitutional rules enforced belt+braces — prompt AND executor assertion | Security |
| ADR-ARCH-027 | No horizontal scaling — single instance, single process | Scalability |
| ADR-ARCH-028 | Ephemeral per-build working trees `/var/forge/builds/{build_id}/` | Security |
| ADR-ARCH-029 | Best-effort availability — no SLA | Availability |
| ADR-ARCH-030 | Budget ceiling ≈ £500/month LLM | Cost |

---

*"The Forge doesn't mine the ore or design the blueprint — it does the making. And it knows when to ask for help."*
