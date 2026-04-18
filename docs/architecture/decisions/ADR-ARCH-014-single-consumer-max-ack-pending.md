# ADR-ARCH-014: Single JetStream consumer with `max_ack_pending=1` — sequential builds at transport layer

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4

## Context

ADR-SP-012 decides sequential builds (one at a time) for LLM rate limits, git safety, Graphiti contention, debuggability, and quality-over-speed. The decision can be enforced at three layers:

1. **Application logic** — Forge's reasoning model is told "one build at a time"; relies on the agent to comply.
2. **Database/state** — a lock in SQLite preventing concurrent transitions.
3. **Transport** — JetStream's pull consumer configured with `max_ack_pending=1`, physically making it impossible for a second message to be delivered while the first is unacked.

Application-layer enforcement is fragile (reasoning models can be coaxed). Database locks are complex across a distributed system. Transport enforcement is the physical invariant.

## Decision

The JetStream pull consumer Forge creates for `pipeline.build-queued.*` is configured with **`max_ack_pending=1`**:

```python
await js.pull_subscribe(
    subject="pipeline.build-queued.>",
    durable="forge-consumer",
    config=ConsumerConfig(
        max_ack_pending=1,
        ack_wait=timedelta(hours=1),  # covers longest expected build
        # ...
    ),
)
```

JetStream will not deliver a second message until the first is acknowledged. Forge acknowledges only on terminal states (COMPLETE | FAILED | CANCELLED | SKIPPED). PAUSED leaves the message unacked — paused builds hold the queue position until Rich resumes.

This **physically enforces** ADR-SP-012 at the NATS layer.

## Consequences

- **+** Sequential execution is a transport invariant, not a best-effort application guarantee — cannot be bypassed by reasoning-model error or prompt injection.
- **+** Queue depth visible in NATS monitoring; operational observability for free.
- **+** Paused builds naturally block the queue — Rich sees "build FEAT-001 paused, 3 queued behind it" via `forge status` + JetStream metadata.
- **−** If a build hangs indefinitely without paused status, `ack_wait` eventually redelivers — Rich must use `forge skip FEAT-XXX` to unblock the queue. Operational.
- **−** Cannot trivially run a "test build" in parallel to a production build (by design — ADR-SP-012).
