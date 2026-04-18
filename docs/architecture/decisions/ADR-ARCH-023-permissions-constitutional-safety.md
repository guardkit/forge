# ADR-ARCH-023: Permissions as constitutional safety config — NOT reasoning-adjustable

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 5

## Context

ADR-ARCH-019 removed behavioural config (gates, thresholds, notifications, training-mode) from `forge.yaml` — these are reasoning outputs or retrievable priors. But DeepAgents' permissions system (filesystem paths, shell binaries, network hosts) is a different beast: *safety boundaries* are not behaviour the reasoning model should negotiate. An agent that can rewrite its own permissions can escape its sandbox.

## Decision

DeepAgents permissions live in `forge.yaml` as **constitutional static config**. They are not reasoning-adjustable, not retrievable from Graphiti, not modifiable via CLI approval. Changing them requires editing `forge.yaml` and restarting the Forge container.

```yaml
# forge.yaml — safety boundaries
permissions:
  filesystem:
    allow_read:  ["/var/forge/builds/*", "/etc/forge/*", "~/.forge/*"]
    allow_write: ["/var/forge/builds/*", "~/.forge/*", "/tmp/forge-*"]
  shell:
    allow_binaries: [git, gh, guardkit, python, pytest]
    deny_default: true
  network:
    allow_hosts:
      - "promaxgb10-41b1:4222"          # NATS
      - "whitestocks:6379"               # Graphiti FalkorDB
      - "api.github.com"                 # gh CLI
      - "*.googleapis.com"               # Gemini (primary)
      - "api.anthropic.com"              # fallback
      - "api.openai.com"                 # fallback
```

The reasoning model can *request* a capability outside the allowlist; the DeepAgents runtime will refuse and return an error to the model. The model can then reason about alternatives (use a permitted tool, pause for Rich, fail the build). It cannot expand the allowlist from within the graph.

## Consequences

- **+** Safety invariants are physically enforceable — a reasoning model going off-rails (or prompt-injected) cannot execute arbitrary binaries, touch arbitrary paths, or reach arbitrary hosts.
- **+** Forge's blast radius is small and enumerable — audit by reading the allowlists.
- **+** Consistent with fleet D18 ("every tool declares read_only / mutating / destructive") — permissions operationalise this at runtime.
- **+** Adding a new binary/host (e.g. because a new GuardKit subcommand needs `jq`) is a conscious policy change, not an implicit agent capability.
- **−** If Rich adds a new LLM provider or a new specialist agent on a new host, the allowlist must be updated and Forge restarted. Operational tax; acceptable for safety.
- **−** Tension with ADR-ARCH-019's "agent decides" principle; deliberately drawn: *behaviour* is reasoned; *safety* is static. The distinction is load-bearing.
