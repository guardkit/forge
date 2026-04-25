# Feature Spec Summary: Specialist Agent Delegation

**Feature ID**: FEAT-FORGE-003
**Stack**: python
**Generated**: 2026-04-24T00:00:00Z
**Scenarios**: 33 total (2 smoke, 0 regression)
**Assumptions**: 6 total (5 high / 1 medium / 0 low confidence)
**Review required**: No — all assumptions grounded in supplied context files and ADRs

## Scope

Specifies Forge's specialist delegation path: a single capability-driven dispatch
tool that resolves the target agent via the live discovery cache (exact-tool match,
then intent-pattern fallback at minimum confidence, then tie-break by trust tier,
confidence, and queue depth), publishes the command on the fleet bus, and correlates
the reply on a correlation-keyed channel established before publish. Covers the
LES1 parity rule (PubAck is not success), the local hard-timeout cut-off, result
parsing (Coach score, criterion breakdown, detection findings — top-level preferred,
nested fallback), the degraded path when no specialist is resolvable, the
reasoning-model-driven retry with additional context on soft failure, outcome
correlation back onto the resolution record, async-mode run-identifier polling, and
invariants around snapshot stability, authenticity, and exactly-once reply handling.
Behaviour is described in domain terms; transport primitives (JetStream audit
interception, subscribe-then-publish ordering, per-correlation subject names) appear
only as capability observations, not implementation steps.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 5 |
| Boundary conditions (@boundary) | 6 |
| Negative cases (@negative) | 9 |
| Edge cases (@edge-case) | 15 |
| Smoke (@smoke) | 2 |
| Regression (@regression) | 0 |
| Security (@security) | 3 |
| Concurrency (@concurrency) | 3 |
| Data integrity (@data-integrity) | 1 |
| Integration (@integration) | 2 |

Note: several scenarios carry multiple tags (e.g. boundary + negative,
edge-case + security). Group totals do not sum to 33.

## Group Layout

| Group | Theme | Scenarios |
|-------|-------|-----------|
| A | Key Examples — exact-tool dispatch, intent-pattern fallback, Coach-output parsing, retry-with-context, outcome correlation | 5 |
| B | Boundary Conditions — minimum confidence (just-inside / just-outside), local timeout (just-inside / just-outside), trust-tier tie-break, queue-depth tie-break | 6 |
| C | Negative Cases — unresolved capability, degraded-status exclusion, specialist error, PubAck-not-success, wrong-correlation reply, missing Coach score, malformed envelope | 7 |
| D | Edge Cases — write-before-send invariant, subscribe-before-publish invariant, unsubscribe-on-timeout, cache freshness on join, cache invalidation on deregister, async-mode polling, concurrent dispatches to same agent | 7 |
| E | Security / Concurrency / Data Integrity / Integration — reply-source authenticity, sensitive-parameter hygiene, trust-tier supremacy, in-flight snapshot stability, concurrent-resolution determinism, duplicate-reply idempotency, bus disconnect, registry outage fallback | 8 |

## Deferred Items

None.

## Assumptions (all confirmed)

- **ASSUM-001** — intent-fallback minimum confidence 0.7 (DM-discovery §3) — high
- **ASSUM-002** — advisory specialist-side command timeout 600s (API-nats-agent-dispatch §3.2) — high
- **ASSUM-003** — Forge-side hard dispatch timeout 900s (API-nats-agent-dispatch §5) — high
- **ASSUM-004** — discovery cache TTL 30s (DM-discovery §1) — high
- **ASSUM-005** — retry is reasoning-model-driven; no fixed max-retry count at dispatch layer — medium
- **ASSUM-006** — gate fallback when Coach score absent is FLAG_FOR_REVIEW (API-nats-agent-dispatch §6) — high

## Upstream Dependencies

- **FEAT-FORGE-001** — Pipeline State Machine & Configuration. The state machine and
  SQLite history are referenced here only as the durable substrate on which
  `CapabilityResolution` records and dispatch outcomes are persisted.
- **FEAT-FORGE-002** — NATS Fleet Integration. Provides the live fleet cache,
  fleet-lifecycle subscription, pipeline-event publishing, and heartbeat view that
  FEAT-FORGE-003's resolution layer reads from. This spec assumes the fleet cache
  is populated and fresh; degraded-cache behaviour is inherited from FEAT-FORGE-002
  and referenced only where dispatch-time snapshot semantics matter (Group E
  integration scenarios).

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Specialist Agent Delegation" \
      --context features/specialist-agent-delegation/specialist-agent-delegation_summary.md

`/feature-plan` Step 11 will link `@task:<TASK-ID>` tags back into the
`.feature` file after tasks are created.
