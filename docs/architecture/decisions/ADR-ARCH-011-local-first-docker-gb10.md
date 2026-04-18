# ADR-ARCH-011: Local-first Docker deployment on GB10 alongside NATS + dual-role specialist-agents

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 3

## Context

Anchor v2.2 §10 states *"Local-first execution on GB10 + MacBook Pro via Tailscale. No cloud bills during development."* ADR-SP-012 mandates sequential builds (`max_concurrent: 1`); ADR-SP-015 puts dual-role specialist-agent containers on GB10. Forge is the next container in that topology.

## Decision

- **Packaging**: Docker container built from Forge repo's `Dockerfile`; image includes Python 3.12, `nats-core`, `deepagents`, GuardKit CLI binary, `git`, `gh`, and working-tree volume mount point at `/var/forge/builds/`.
- **Host**: GB10 (`promaxgb10-41b1`), added to the existing `nats-infrastructure` Docker Compose alongside:
  - `nats-server` (existing)
  - `specialist-agent-product-owner` (ADR-SP-015)
  - `specialist-agent-architect` (ADR-SP-015)
  - **`forge`** (this ADR)
  - Future: `jarvis` (when built)
- **Dev mode**: `langgraph dev` on MacBook Pro connecting to NATS on GB10 via Tailscale (matches specialist-agent dev workflow).
- **Volumes**:
  - `~/.forge/forge.db` mounted for SQLite persistence across container restarts.
  - `/var/forge/builds/` mounted for per-build working trees (ephemeral, ADR-ARCH-028).
  - `/etc/forge/forge.yaml` mounted read-only.
  - `~/.config/gh/hosts.yml` mounted read-only for gh auth.
- **Runtime mode**: long-running process; container restart policy `unless-stopped`; graph loaded once, consumes JetStream in a loop.
- **`max_concurrent: 1`** set in both `forge.yaml` and the published `AgentManifest` — constraint declared at both layers.
- **No horizontal scaling ever** — per ADR-ARCH-027, additional capacity comes from additional Forge instances (one per operator), never one scaled out.

## Consequences

- **+** Consistent topology with the rest of the fleet on GB10 — shared Tailscale, shared Docker, shared observability.
- **+** Zero cloud bills for Forge itself (LLM provider costs are separate — ADR-ARCH-030).
- **+** Recovery from GB10 reboot is automatic via Docker `unless-stopped` + JetStream redelivery + SQLite reconciliation.
- **−** Forge depends on GB10 uptime — Rich's home network is the availability ceiling (ADR-ARCH-029).
- **−** Compose file ownership sits in `nats-infrastructure`; Forge updates require a cross-repo change. Acceptable given fleet repo's central role.
