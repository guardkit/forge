---
complexity: 7
created: 2026-04-25 00:00:00+00:00
dependencies:
- TASK-SAD-001
feature_id: FEAT-FORGE-003
id: TASK-SAD-003
implementation_mode: task-work
parent_review: TASK-REV-SAD3
priority: high
status: design_approved
tags:
- dispatch
- correlation
- security
- exactly-once
- les1
- source-auth
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: 'CorrelationRegistry: subscribe-before-publish, exactly-once, source-auth'
updated: 2026-04-25 00:00:00+00:00
wave: 2
---

# Task: CorrelationRegistry — subscribe-before-publish, exactly-once, source-auth

## Description

Implement the **single most important domain primitive** in this feature: the
correlation registry. The registry is responsible for three invariants the
spec calls out by name and that the LES1 incident motivated:

1. **Subscribe-before-publish ordering** (D.subscribe-before-publish-invariant
   + the canonical LES1 lesson). The registry must expose a `bind()` method
   that establishes the subscription and only then returns a "ready" handle.
   The orchestrator MUST NOT publish without the handle.
2. **Exactly-once reply handling** (E.duplicate-reply-idempotency). A second
   reply on the same correlation key after the first has been accepted is
   silently dropped — it does not reprocess the gate or flip the outcome.
3. **Reply-source authenticity** (E.reply-source-authenticity). Each binding
   carries the `matched_agent_id` from the resolution; replies whose source
   identifier ≠ matched agent are rejected.

Also enforces:
- The PubAck-not-success rule (C.pubAck-not-success — PubAck on the audit
  stream does not flip the binding to "completed"; only a valid reply does).
- Wrong-correlation reply filtering (C.wrong-correlation-reply — replies on
  a different correlation channel are not consumed).
- Correlation key format invariant (32 lowercase hex; no embedded PII).

## Interface

```python
# src/forge/dispatch/correlation.py
import asyncio
import re
import secrets
from dataclasses import dataclass, field
from typing import Optional

CORRELATION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class CorrelationBinding:
    correlation_key: str
    matched_agent_id: str
    accepted: bool = False
    _future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future)


class CorrelationRegistry:
    """Per-dispatch correlation-keyed reply routing.

    Lifecycle for one dispatch:
      key = registry.fresh_correlation_key()
      binding = await registry.bind(key, matched_agent_id)
      # ↑ subscription is now active; only now is publish permitted
      # transport publishes the command...
      outcome = await registry.wait_for_reply(binding, timeout)
      registry.release(binding)
    """

    def __init__(self, transport: "ReplyChannel") -> None: ...

    def fresh_correlation_key(self) -> str:
        """Return a new 32-lowercase-hex correlation key.

        MUST NOT embed agent IDs, timestamps, or other PII.
        """
        return secrets.token_hex(16)

    async def bind(
        self, correlation_key: str, matched_agent_id: str
    ) -> CorrelationBinding:
        """Establish the per-correlation reply subscription.

        Returns ONLY after the subscription is active. The caller must not
        publish the dispatch command before `bind()` returns.
        """
        if not CORRELATION_KEY_RE.fullmatch(correlation_key):
            raise ValueError(f"invalid correlation key format: {correlation_key!r}")
        # ... subscribe via transport, register binding, return when active
        ...

    def deliver_reply(self, correlation_key: str, source_agent_id: str, payload: dict) -> None:
        """Internal entrypoint called by the transport when a reply arrives.

        - Drop replies whose correlation_key has no binding.
        - Drop replies whose source_agent_id != binding.matched_agent_id (E.reply-source-authenticity).
        - Drop subsequent replies after binding.accepted is True (E.duplicate-reply-idempotency).
        - Reject replies failing envelope validation (caller filters).
        """
        ...

    async def wait_for_reply(
        self, binding: CorrelationBinding, timeout_seconds: float
    ) -> Optional[dict]:
        """Wait for the authentic reply or until the timeout fires.

        Returns the payload on success, None on timeout. Does NOT release
        the binding — release() is the caller's responsibility (timeout
        coordinator handles unsubscribe-on-timeout via release()).
        """
        ...

    def release(self, binding: CorrelationBinding) -> None:
        """Release the subscription. Idempotent.

        After release, late replies on the same correlation key are
        silently dropped (D.unsubscribe-on-timeout).
        """
        ...
```

## Acceptance Criteria

- [ ] `src/forge/dispatch/correlation.py` defines `CorrelationRegistry` with
      `fresh_correlation_key`, `bind`, `wait_for_reply`, `deliver_reply`,
      `release`.
- [ ] `bind()` returns ONLY after the subscription is active; the binding
      handle exposes a way for the orchestrator to assert this in tests.
- [ ] `fresh_correlation_key()` returns 32 lowercase hex characters and
      contains no embedded agent ID, timestamp, or other PII.
- [ ] `deliver_reply()` drops replies whose `correlation_key` has no binding
      (covers C.wrong-correlation-reply).
- [ ] `deliver_reply()` drops replies whose `source_agent_id` differs from
      the binding's `matched_agent_id` (covers E.reply-source-authenticity).
- [ ] `deliver_reply()` drops a second authenticated reply after
      `binding.accepted is True` (covers E.duplicate-reply-idempotency); the
      first reply's outcome is preserved.
- [ ] `release()` is idempotent and prevents subsequent replies on the same
      correlation key from being delivered (covers D.unsubscribe-on-timeout).
- [ ] PubAck on the audit stream does NOT flip `accepted` (test against a
      fake transport that emits PubAck — covers C.pubAck-not-success).
- [ ] Concurrent `deliver_reply()` calls on the same correlation key are
      serialised (asyncio Lock) so exactly-once is robust under races.
- [ ] All modified files pass project-configured lint/format checks with
      zero errors.

## Seam Note

This task is a **producer** for the §4 `CorrelationKey` Integration Contract
(consumers: TASK-SAD-006 orchestrator, TASK-SAD-010 NATS adapter).

## Implementation Notes

- The transport interface is a thin protocol (`ReplyChannel`) declared here
  and implemented by the NATS adapter in TASK-SAD-010. Use a fake
  implementation in tests; do NOT import `nats` here.
- Use `asyncio.Future` per binding so timeout coordination (TASK-SAD-004)
  can race the future against an asyncio.timeout context.
- The registry MUST NOT log raw payloads (parameters may contain sensitive
  values until they reach `persist_resolution`).
- The "ready" semantics of `bind()` are critical. If you discover the
  underlying transport's subscribe is not synchronous, await an explicit
  acknowledgement or readiness signal before returning. Do NOT use
  `asyncio.sleep()`.