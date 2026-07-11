---
complexity: 6
dependencies: []
estimated_minutes: 90
feature_id: FEAT-FORGE-010
id: TASK-FW10-001
implementation_mode: task-work
parent_review: TASK-REV-FW10
priority: high
status: completed
tags:
- foundation
- seam-refactor
- jetstream
- recovery
task_type: feature
title: Refactor _serve_daemon seam to (_MsgLike) → None, set max_ack_pending=1, wire
  paired reconcile_on_boot
wave: 1
reconciled: "2026-07-11 WS3-S8 sweep — FW10 in completed/; moved by 9ef9138b (FEAT-DEA8 finalize)"
---

# TASK-FW10-001 — Refactor _serve_daemon seam, set `max_ack_pending=1`, wire paired `reconcile_on_boot`

## Why

This task is Wave 1 / the foundation. Every later task assumes the new
seam contract (`(_MsgLike) -> None`), the single shared NATS
connection, the `max_ack_pending=1` durable, and both
`reconcile_on_boot` routines firing before the first message is
fetched. Landing this in isolation means the rest of the feature can
parallelise. The seam-refactor design is reused verbatim from
[TASK-FORGE-FRR-001 implementation_plan.md](../../../docs/state/TASK-FORGE-FRR-001/implementation_plan.md);
the receipt-only `_default_dispatch` becomes unreachable from
production code paths after this task plus TASK-FW10-007.

## Files to modify

- `src/forge/cli/_serve_daemon.py`:
  - Change `DispatchFn` from `Callable[[bytes], Awaitable[None]]` to
    `Callable[[_MsgLike], Awaitable[None]]`. Re-export the new alias.
  - Replace `_process_message`: call `dispatch_payload(msg)` only.
    **Remove** the post-dispatch `await msg.ack()` from the success
    path. Keep the `except Exception` E3.1 path but make it ack the
    message itself before logging (since the dispatcher won't have
    when it raised).
  - In `_attach_consumer`'s `ConsumerConfig`, set `max_ack_pending=1`.
    Update the docstring's "does not gate on `max_ack_pending=1`"
    sentence.
  - Replace `_default_dispatch` with one that just logs + acks the
    message itself (preserves the test seam when monkey-patched).
    Update its docstring per the spec — the receipt-only language is
    gone.
- `src/forge/cli/serve.py`:
  - In `_run_serve`, open the NATS client **once** before constructing
    the dispatcher, the deps factory, the publisher, and the daemon
    loop, so all of them share the connection (ASSUM-011).
  - Run **both** `forge.lifecycle.recovery.reconcile_on_boot(...)` and
    `pipeline_consumer.reconcile_on_boot(...)` synchronously **before**
    the consumer is attached (ASSUM-009; F1 in the review report).
  - Refactor `run_daemon` (or its caller in `_serve_daemon`) to accept
    the injected client, instead of opening its own inside the
    reconnect loop.
- `src/forge/cli/_serve_state.py`:
  - Add a `chain_ready: bool` flag, default False; set True after
    composition completes in `_run_serve` (consumed by the healthz
    extension in this task).
- `src/forge/cli/_serve_healthz.py`:
  - Extend the healthy gate: healthy iff the NATS subscription is live
    AND `chain_ready` is True (ASSUM-012).
- `tests/cli/test_serve_daemon.py` (existing):
  - Migrate the monkey-patch sites to the new `(_MsgLike) -> None`
    seam signature.
  - Assert ack is **not** called by `_process_message` on the success
    path; ack **is** called on the E3.1 failure path.
  - Assert `max_ack_pending=1` is set on the `ConsumerConfig`
    `_attach_consumer` produces.
- `tests/cli/test_serve_healthz.py` (or equivalent):
  - Add tests for the three healthz states (chain_ready=False,
    chain_ready=True + subscription live, chain_ready=True +
    subscription dropped) — covers the Group E healthz Scenario
    Outline.

## Acceptance criteria

- [ ] `DispatchFn` is `Callable[[_MsgLike], Awaitable[None]]` and the
      `_serve_daemon` test seams use the new signature.
- [ ] `_process_message` does not call `msg.ack()` on the success path;
      the dispatcher (or the state machine via `handle_message`'s
      `ack_callback`) owns terminal-only ack.
- [ ] `_process_message`'s `except Exception` path acks the message
      itself before logging the error; the daemon stays running.
- [ ] `_attach_consumer`'s `ConsumerConfig` sets `max_ack_pending=1`;
      the docstring no longer claims otherwise.
- [ ] `_default_dispatch` logs + acks itself (preserves the test seam)
      and its docstring no longer describes a "receipt-only stub" as
      the production behaviour.
- [ ] `_run_serve` opens **one** NATS client and shares it with all
      downstream constructors. No second `nats.connect(...)` call
      anywhere in the daemon's startup path.
- [ ] Both `forge.lifecycle.recovery.reconcile_on_boot` and
      `pipeline_consumer.reconcile_on_boot` are awaited before the
      durable consumer is attached. (Step 5 in §5 of the
      IMPLEMENTATION-GUIDE.md must hold.)
- [ ] `_serve_state.chain_ready` flips True after composition; healthz
      reads it and reports unhealthy until then.
- [ ] Healthz reports unhealthy if the NATS subscription drops, even
      if `chain_ready` is True (Group E scenario row 3).
- [ ] All existing F009-003 daemon tests pass after the seam migration.
- [ ] All modified files pass project-configured lint/format checks
      with zero errors.

## Seam Tests

The following seam test validates the integration contract this task
**produces** (the new `DispatchFn` type alias). The consumer in
TASK-FW10-007 will write a stricter version of this test against its
own dispatcher closure; this stub asserts the producer-side invariant.

```python
"""Seam test: verify DispatchFn contract from TASK-FW10-001."""
import pytest


@pytest.mark.seam
@pytest.mark.integration_contract("DispatchFn")
async def test_dispatch_fn_signature_does_not_ack_on_success():
    """Verify _process_message does not call msg.ack() on the success path.

    Contract: DispatchFn(_MsgLike) -> Awaitable[None]; ack lifecycle is
    owned by the dispatcher (or the state machine via
    pipeline_consumer.handle_message's ack_callback). Producer:
    TASK-FW10-001.
    """
    from forge.cli import _serve_daemon

    ack_calls = 0

    class FakeMsg:
        data = b'{"feature_id":"F","correlation_id":"c"}'

        async def ack(self) -> None:
            nonlocal ack_calls
            ack_calls += 1

    async def fake_dispatch(msg) -> None:
        # Successful dispatch must NOT ack from inside _process_message.
        return None

    _serve_daemon.dispatch_payload = fake_dispatch
    await _serve_daemon._process_message(FakeMsg())

    assert ack_calls == 0, "ack must not fire on the success path"
```

## Implementation notes

- Order matters in `_run_serve`. Do NOT attach the durable consumer
  before both `reconcile_on_boot` routines complete. Attaching first
  risks a redelivered envelope landing on an unreconciled history view
  (F1 in the review).
- `chain_ready` is a simple bool on `_serve_state`. No async
  coordination needed — the daemon's startup is single-task synchronous
  until the first `await consumer.fetch(...)`.
- The dormant `pipeline_consumer.DURABLE_NAME = "forge-consumer"` is
  not touched by this task. Cleanup is a separate follow-up.
- **Operational rollout note** (capture in deploy docs): existing
  `forge-serve` durable in production must be `nats consumer rm`-ed
  before deploying the image with `max_ack_pending=1`. JetStream does
  not allow editing this field on a live consumer.

## Coach validation

- `pytest tests/cli -x` (smoke gate 1).
- `pytest tests/cli/test_serve_daemon.py tests/cli/test_serve_healthz.py -x`.
- Lint: project-configured ruff/format.
- Diff inspection: confirm no second `nats.connect(...)` in
  `_run_serve` or downstream factories.

## References

- [DDR-007](../../../docs/design/decisions/DDR-007-pipeline-lifecycle-emitter-wiring-path.md)
- [TASK-FORGE-FRR-001 implementation_plan.md](../../../docs/state/TASK-FORGE-FRR-001/implementation_plan.md) (load-bearing seam refactor design)
- [API-nats-pipeline-events.md §2.2](../../../docs/design/contracts/API-nats-pipeline-events.md)
- [ADR-ARCH-014](../../../docs/architecture/decisions/ADR-ARCH-014-single-consumer-max-ack-pending.md)
- IMPLEMENTATION-GUIDE.md §5 (boot order)