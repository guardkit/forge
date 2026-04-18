# ADR-ARCH-029: Best-effort availability — no SLA

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 6

## Context

Forge is a local-first single-operator system running on GB10 at Rich's home network. No paying customer, no commercial SLA commitment. Practical availability is bounded by home-network infrastructure (power, internet, Tailscale) and LLM provider uptime. A formal SLA would be misleading.

However, *degraded behaviour* should be well-defined so Rich knows what to expect when dependencies fail.

## Decision

**No availability SLA.** "Best effort" is the service level.

**Degraded behaviour per dependency failure** (reasoning-driven, not config-coded — ADR-ARCH-019):

| Dependency down | Behaviour |
|---|---|
| **NATS** | Forge cannot accept new builds; in-flight build stalls until NATS returns; reconnect via nats-py; JetStream redelivers on AckWait expiry. |
| **Graphiti** | Degraded: no calibration priors, no pipeline-history retrieval, no learning write-back. Reasoning still runs (naturally conservative without priors per ADR-ARCH-019). |
| **Any specialist agent** | Reasoning decides: fall back to local GuardKit tool, pause for Rich, or skip the work. Force flag-for-review on whatever replacement is used. |
| **LLM primary (Gemini)** | Manual env-var flip to fallback provider (ADR-ARCH-010). V2: automatic failover. |
| **GitHub API** | Stage 5 PR creation stalls; build waits until reachable. SQLite marks build as FINALISING; JetStream message stays unacked; retry works naturally. |
| **Graphiti + all specialists down** | Forge still runs GuardKit commands directly; every gate forces flag-for-review; heavy human load. Functional but painful. |

**Recovery:**
- Forge process crash: JetStream redelivers, SQLite reconciles (RUNNING → INTERRUPTED), PAUSED rows re-emit ApprovalRequestPayload (ADR-ARCH-021 + ADR-SP-013).
- GB10 reboot: same mechanism on boot-up.
- Tailscale outage (MacBook-side): Forge on GB10 runs locally; Rich's CLI fails until restored; builds queue continues if triggered.

**Uptime aspiration:** months between unexpected Forge crashes once V1 stabilises. Not a commitment.

## Consequences

- **+** Honest positioning — no false promises.
- **+** Degraded-mode behaviours are explicit and reasoning-driven — each dependency failure has a known effect on build-time judgment.
- **+** Multi-provider LLM config (ADR-ARCH-010) caps the worst-case vendor outage.
- **+** Retry-from-scratch + JetStream redelivery (ADR-SP-013) make transient infrastructure failures self-healing after restore.
- **−** Rich is the sole operator; if he's away, a stuck pipeline stays stuck. V2: dashboard + notification adapters for push alerts.
- **−** No formal incident SLO/SLA means no forcing function for reliability engineering; operational stability is a judgement call.
