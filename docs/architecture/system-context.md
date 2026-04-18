# Forge — C4 Level 1 (System Context)

> **Generated:** 2026-04-18 via `/system-arch`
> **Approved by:** Rich (diagram review gate, this session)

```mermaid
C4Context
    title Forge — System Context (v1)

    Person(rich, "Rich", "Operator: queues builds, reviews 🟡 flagged stages, reviews PRs")
    Person(reviewers, "James, Mark", "PR reviewers — interact with GitHub, not Forge")

    System(forge, "Forge", "NATS-native pipeline orchestrator. DeepAgents agent harness. Drives features → PR with reasoning-driven confidence gates and Graphiti-fed learning.")

    System_Ext(jarvis, "Jarvis", "Fleet intent router — publishes build triggers on Rich's behalf from voice/Telegram/dashboard/CLI-wrapper (ADR-SP-014)")
    System_Ext(specialists, "Specialist Agents (Fleet)", "PO + Architect today; QA, UX, Ideation as they ship. Capability-discovered at runtime via AgentManifest — no role hardcoding.")
    System_Ext(nats, "NATS + JetStream", "Transport backbone. Streams: PIPELINE, AGENTS, FLEET, JARVIS, NOTIFICATIONS. KV: agent-registry.")
    System_Ext(guardkit, "GuardKit CLI", "Slash commands: /system-arch, /system-design, /system-plan, /feature-spec, /feature-plan, /task-review, /task-work, /task-complete, autobuild, graphiti seed/query")
    System_Ext(graphiti, "Graphiti (FalkorDB)", "Fleet knowledge graph. Groups: forge_pipeline_history (outcomes + overrides), forge_calibration_history (ingested history files).")
    System_Ext(llm, "LLM Providers", "Gemini 3.1 Pro (primary) / Anthropic / OpenAI / local vLLM — provider-neutral via init_chat_model. Single env var to switch.")
    System_Ext(github, "GitHub", "git push + PR creation via gh CLI")

    Rel(rich, forge, "forge queue | status | history | cancel | skip", "Click CLI")
    Rel(jarvis, nats, "publish BuildQueuedPayload", "pipeline.build-queued.*")
    BiRel(forge, nats, "pull-consume triggers; publish lifecycle events; fleet register/heartbeat; approval round-trips", "pipeline.* / agents.* / fleet.*")
    BiRel(forge, specialists, "dispatch_by_capability (resolved at runtime); receive Coach-scored results", "agents.command.* / result.*")
    Rel(forge, guardkit, "subprocess via DeepAgents execute with --nats", "shell")
    BiRel(forge, graphiti, "retrieve priors (calibration + pipeline history); write outcomes + override events", "bolt://")
    Rel(forge, llm, "reasoning + implementation model invocations", "HTTPS")
    Rel(forge, github, "git push branch + gh pr create", "HTTPS")
    Rel(reviewers, github, "review + merge PRs", "web UI")
    Rel(nats, jarvis, "notifications routed back to originating adapter", "jarvis.notification.*")
```

## What to look for

Forge is bidirectionally-connected to NATS, specialists, and Graphiti — these are the three load-bearing external dependencies. Jarvis interacts only via NATS (not directly with Forge — ADR-SP-014 Pattern A). LLM is a one-way call-out (requests; responses implicit). GitHub is one-way via git/gh. James & Mark interact only with GitHub — deliberate (they don't touch pipeline internals). GuardKit is subprocess-only, no NATS round-trip from Forge's side.

Node count: 10 / 30 threshold.

## Trust boundaries

- **Forge + NATS + specialists** run inside the APPMILLA NATS account (D13) — fleet-boundary trust.
- **Graphiti (FalkorDB on Synology)** reachable via Tailscale mesh from GB10.
- **LLM providers + GitHub** — public internet, HTTPS, token-authenticated.
- **Jarvis** — fellow APPMILLA fleet agent; trust inherited from fleet boundary.

## Related diagrams

- [container.md](container.md) — C4 Level 2 (Forge's internal containers)
- [forge-pipeline-architecture.md §6](../research/forge-pipeline-architecture.md#6-forge-state-machine) — state-machine diagram (anchor)
