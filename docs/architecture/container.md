# Forge — C4 Level 2 (Container Diagram)

> **Generated:** 2026-04-18 via `/system-arch`
> **Approved by:** Rich (diagram review gate, this session)

```mermaid
C4Container
    title Forge — Container Diagram (DeepAgents 0.5.3 runtime)

    Person(rich, "Rich", "Operator")

    System_Boundary(forge, "Forge") {
        Container(agent, "Agent Runtime", "Python 3.12 / DeepAgents 0.5.3 / LangGraph", "CompiledStateGraph. Reasoning model (Gemini 3.1 Pro primary). write_todos drives stage narrative. Sub-agents: build_plan_composer, autobuild_runner. PAUSED = LangGraph interrupt(). Exported via langgraph.json.")
        Container(cli, "Forge CLI", "Python / Click", "Short-lived: forge queue/status/history/cancel/skip. Reads SQLite direct; writes via NATS.")
        Container(discovery, "Discovery + Learning + Calibration", "Python (pure domain core)", "Live fleet capability resolution (KV lookup + fleet.register watch). Override-rate learning. History-file CalibrationEvent ingestion. Injects priors into system prompt at build start.")
        ContainerDb(sqlite, "Build History", "SQLite WAL", "~/.forge/forge.db — builds + stage_log tables. Authoritative durable state per ADR-SP-013. CLI reads; agent writes.")
        Container(config, "Config + Permissions", "YAML / pydantic-settings", "forge.yaml — infrastructure, models, constitutional rules, DeepAgents permissions (fs/shell/network allowlists). Static safety config (ADR-ARCH-023).")
        Container(nats_adapter, "NATS Adapter", "nats-core / nats-py", "JetStream pull consumer (max_ack_pending=1). Publish. KV read. Fleet watch. Approval round-trip publisher.")
        Container(graphiti_adapter, "Graphiti Adapter", "Python / Graphiti client", "forge_pipeline_history + forge_calibration_history read/write. Priors retrieval at build start; outcome write-back continuously.")
        Container(subprocess_adapter, "Subprocess Adapter", "DeepAgents execute + thin wrappers", "guardkit_* / git / gh — path + binary allowlisted per permissions.")
        ContainerDb(worktrees, "Per-Build Worktrees", "Filesystem (ephemeral)", "/var/forge/builds/{build_id}/ — created on PREPARING, deleted on terminal state (ADR-ARCH-028). Blast-radius isolation.")
    }

    System_Ext(nats_server, "NATS + JetStream", "GB10 Docker — PIPELINE/AGENTS/FLEET streams + agent-registry KV")
    System_Ext(specialists, "Specialist Agents", "GB10 Docker — PO + Architect today; QA/UX/Ideation as they ship. Capability-discovered, never hardcoded (ADR-ARCH-015).")
    System_Ext(falkordb, "FalkorDB Graphiti", "Synology NAS @ whitestocks:6379 via Tailscale")
    System_Ext(llm, "LLM Provider", "Gemini 3.1 Pro primary / Anthropic / OpenAI / vLLM — provider-neutral via init_chat_model (ADR-ARCH-010)")
    System_Ext(guardkit_bin, "GuardKit CLI binary", "Container-installed — /usr/local/bin/guardkit")
    System_Ext(git_bin, "git + gh CLI binaries", "Container-installed")
    System_Ext(github, "GitHub API", "api.github.com")

    Rel(rich, cli, "forge …", "shell")
    Rel(cli, sqlite, "read status + history", "sqlite3")
    Rel(cli, nats_adapter, "publish queue/cancel/skip", "in-process")

    Rel(agent, config, "load at startup + permissions enforcement", "pydantic-settings")
    Rel(agent, discovery, "list_capabilities + retrieve priors for system prompt", "in-process")
    Rel(agent, sqlite, "write stage_log rows + lifecycle transitions", "sqlite3 WAL")
    BiRel(agent, nats_adapter, "pull trigger; publish events; interrupt ↔ ApprovalPayload", "in-process")
    BiRel(agent, graphiti_adapter, "priors in; outcomes out", "in-process")
    Rel(agent, subprocess_adapter, "invoke GuardKit / git / gh via @tools", "in-process")
    Rel(agent, worktrees, "DeepAgents filesystem tools (read/write/edit/ls/glob/grep)", "filesystem")
    Rel(agent, llm, "reasoning + implementation model invocations", "HTTPS")

    Rel(discovery, nats_adapter, "agent-registry KV lookup + fleet.register watch", "in-process")
    Rel(discovery, graphiti_adapter, "calibration corpus + pipeline history retrieval", "in-process")

    Rel(nats_adapter, nats_server, "NATS pub/sub + KV + JetStream pull", "NATS over TLS + Tailscale")
    BiRel(nats_server, specialists, "commands + results via agents.command.* / result.*", "NATS")
    Rel(graphiti_adapter, falkordb, "entity read/write", "bolt://")
    Rel(subprocess_adapter, guardkit_bin, "subcommand + args + progress parse", "execute")
    Rel(subprocess_adapter, git_bin, "git ops + gh pr create", "execute")
    Rel(subprocess_adapter, worktrees, "working-tree mutation", "filesystem")
    Rel(git_bin, github, "HTTPS auth via gh token", "HTTPS")
```

## What to look for

`agent` is the hub (expected — DeepAgents graph orchestrates everything); no adapters talk to each other directly (clean Hexagonal — all cross-adapter coordination goes through `agent` or `discovery`). `sqlite` has 2 accessors (`agent` write, `cli` read) — clean ownership. `worktrees` touched by both `agent` (via DeepAgents filesystem tools) and `subprocess_adapter` (via shell commands in `/var/forge/builds/{build_id}`) — deliberate, these are two legitimate access modes to the same filesystem. Externals cluster around their trust boundaries: NATS infrastructure on GB10, Graphiti on Synology, LLM + GitHub across the public internet.

Node count: 17 / 30 threshold.

## Async / inbound paths

- **Build trigger inbound:** Jarvis → nats_server → nats_adapter → agent (shown fully in [system-context.md](system-context.md); NATS aggregates into `nats_server` here)
- **Approval inbound:** nats_server → nats_adapter → agent `interrupt()` resume
- **Fleet capability changes:** `fleet.register` / `deregister` / `heartbeat` events → nats_adapter → discovery (live cache invalidation, ADR-ARCH-017)

## Module mapping

Each container maps to modules described in [ARCHITECTURE.md §3](ARCHITECTURE.md#3-module-map-15-modules-in-5-groups):

| Container | Modules |
|---|---|
| Agent Runtime | `forge.agent`, `forge.prompts`, `forge.subagents`, `forge.gating`, `forge.state_machine`, `forge.notifications`, `forge.history_labels`, `forge.tools.*` (all) |
| Discovery + Learning + Calibration | `forge.discovery`, `forge.learning`, `forge.calibration` |
| NATS Adapter | `forge.adapters.nats`, `forge.fleet` |
| Graphiti Adapter | `forge.adapters.graphiti`, `forge.adapters.history_parser` |
| Subprocess Adapter | `forge.adapters.guardkit` |
| Build History DB | `forge.adapters.sqlite` |
| Forge CLI | `forge.cli` |
| Config + Permissions | `forge.config` + `forge.yaml` file |

## Related

- [system-context.md](system-context.md) — C4 Level 1
- [ARCHITECTURE.md](ARCHITECTURE.md) — index, module map, decision list
- [forge-pipeline-architecture.md §6](../research/forge-pipeline-architecture.md#6-forge-state-machine) — state-machine diagram
