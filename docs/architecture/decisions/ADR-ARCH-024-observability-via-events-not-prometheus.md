# ADR-ARCH-024: Observability = NATS event stream + Graphiti + SQLite stage_log — no Prometheus V1

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 5

## Context

Forge emits lifecycle events (`pipeline.build-started`, `pipeline.build-progress`, `pipeline.stage-complete`, `pipeline.build-paused`, `pipeline.build-complete`, `pipeline.build-failed`) on NATS by design (anchor §7–8). These are *already* an observability stream — typed, structured, subscribable. Adding Prometheus/Grafana on top would duplicate the signal without adding new information. The fleet is local-first; no cloud metrics stack.

## Decision

**Four complementary observability channels, no duplication:**

1. **Structured JSON logs** — `structlog` or stdlib `logging` with JSON formatter. Every line carries `build_id`, `feature_id`, `correlation_id`, `todo_label`, plus `capability` / `target_agent_id` / `coach_score` when relevant. Consumed via `docker logs forge` or a future log-aggregator container.

2. **NATS `pipeline.*` event stream** — every state transition + gate decision is published as a typed payload. Dashboards, Slack adapters, future telemetry consumers subscribe to `pipeline.>`. **This is Forge's primary observability channel.**

3. **SQLite `stage_log`** — one row per reasoning-model dispatch: `build_id`, `todo_label` (emergent stage name from ADR-ARCH-016), `target` (local tool or fleet agent_id), `duration_secs`, `coach_score`, `gate_decision`, `rationale_summary`. Queryable via `forge history` — the narrative Rich reads as prose.

4. **LangSmith tracing** (optional) — opt-in via `AGENT_LANGSMITH_PROJECT=forge` env var. If enabled, full DeepAgents graph invocation trace (messages, tool calls, interrupts) captured for post-build inspection by Rich.

5. **Graphiti `forge_pipeline_history`** — every gate decision + override + outcome also lands here as a structured entity. Not just logs; queryable for analytics and retrievable as priors for future builds.

6. **Fleet heartbeat** — `fleet.heartbeat.forge` every 30s carries uptime, queue_depth, active_tasks, last_completed_at, health flags (`graphiti_healthy`, `specialist_X_healthy`). Jarvis + future dashboard observe.

**Not included in V1:** Prometheus, Grafana, InfluxDB, OpenTelemetry collectors. V2 could add a `nats → prometheus` bridge as a notification-adapter-style subscriber without Forge changes.

**Auto-summarisation** (DeepAgents built-in, ADR-ARCH-020) compresses *in-conversation context* only — it never truncates what lands in SQLite or Graphiti. Those are structured + complete by design.

## Consequences

- **+** Zero metrics-stack operational overhead in V1.
- **+** Every observability signal is already part of the architecture — no parallel instrumentation.
- **+** NATS pub/sub makes adding new observability consumers free (subscribe to `pipeline.>`, done).
- **+** Graphiti doubles as long-term analytics store — cross-build trends queryable.
- **−** No real-time metrics dashboard in V1. Operators who want Grafana-style views build the adapter themselves.
- **−** Log-level alerting not set up in V1 — Rich sees events in `forge history` or via Jarvis notifications, not via PagerDuty.
