# FEAT-PAC — the phantom-ack wedge cure (scope + buildplan)
## 2026-07-27 night · BINDING SPEC for the orchestrated build · plan-of-record NEXT #2(a)

> Grounded in the two sources of truth (ai-transition mission + plan). Lane claim:
> ai-transition exec-plan §7. Measurable: **M0** — the 25h wedge class required
> frontier-coordinator diagnosis plus manual broker surgery on the routine path; this
> lane makes the chain heal itself at boot and alarm loudly at runtime, removing that
> attended-frontier intervention class. No frontier assumed anywhere.

## The wedge (facts, live-pinned 2026-07-27 — treat as ground truth)

- The daemon's pull consumer (stream `PIPELINE`, durable `forge-serve` by default —
  `_serve_config.py:120`, env `FORGE_DURABLE_NAME`) is bound at
  `_serve_daemon._attach_consumer` (`src/forge/cli/_serve_daemon.py:234`) with
  `MAX_ACK_PENDING = 1` (:105/:258) — deliberate strict serialization.
- The ack is DEFERRED to the terminal publish (`_process_message` :282 never acks on
  success; a `BuildAckHandle` registered via the lifecycle bridge fires it on the
  terminal SSE — `adapters/nats/pipeline_consumer.py:373/552`). A daemon death in that
  window strands the single slot; pull consumers redeliver only on pulls, so all
  dispatch jams silently (the 25h live receipt).
- When the stranded message is PURGED, the phantom is invisible to BOTH boot
  reconciles: `recovery.reconcile_on_boot` (`lifecycle/recovery.py:368`) reads SQLite
  only; the JetStream twin (`pipeline_consumer.py:912`) has `fetch_redeliveries`
  stubbed to `[]` in production (`_serve_production.py:535-539`, the single-consumer
  10100 rule). Proven by two restarts fixing nothing.
- The proven MANUAL cure: delete the consumer; the daemon recreates it at attach —
  nats-py `pull_subscribe` is bind-or-create (`nats/js/client.py:583-608`).
- Live healthy idle shape (coordinator read 07-27): `num_pending 0 · ack_pending 0 ·
  num_waiting 1` (a parked pull is idle-good). The stream shows large `Deleted
  Messages` counts as NORMAL workqueue behavior (acked = removed) — deletion counts
  prove nothing; only the pending message's own sequence matters.

## The design (design-pass verdict; API pinned against installed nats-py 2.15.0)

### The discriminator (the load-bearing idea)

With `max_ack_pending=1` the ack-pending set is a singleton — the single outstanding
message is exactly the LAST-DELIVERED one, so `pending_seq = delivered.stream_seq`
(`ConsumerInfo.delivered` is `SequenceInfo{stream_seq,...}`, `nats/js/api.py:650/668`;
Optional — guard None. NOT `ack_floor+1`: on the multi-subject PIPELINE stream the
sequences between the floor and the delivered watermark belong to other subjects'
consumed messages — live-proven 2026-07-27 when a gate-paused build held seq 653 while
`ack_floor+1`=649 was a consumed foreign message already gone; the +1 formula would
have cured a legitimate hold). Then:

- `js.get_msg('PIPELINE', seq=pending_seq)` **succeeds** → the held message still
  exists → a LEGITIMATE long-held ack (an in-flight or redeliverable build). NEVER
  cure this: deleting the durable would drop its position and, with
  `DeliverPolicy.ALL`, replay history.
- raises `nats.js.errors.NotFoundError` → the message is GONE (purge or delete_msg
  hole) → **PHANTOM**: no ack can ever release the slot. Cure.

The idle signature alone (`ack_pending>0 + waiting>0 + no deliveries for N min`) is
IDENTICAL for a legitimate hours-long build and the phantom — it may alarm, it must
NEVER auto-cure. `get_msg` is the only honest discriminator (and is robust where the
`first_seq` floor comparison misses single `delete_msg` holes).

### 1. Detection/cure module — `src/forge/adapters/nats/consumer_health.py` (new)

- `@dataclass AckSlotReport`: `status: Literal["healthy","held","phantom","unknown"]`,
  `pending_seq: int | None`, `num_ack_pending/num_waiting/num_pending: int`,
  `detail: str` (plain-language, operator-readable).
- `async def inspect_ack_slot(js, stream, durable) -> AckSlotReport`:
  `consumer_info` → `num_ack_pending in (0, None)` ⇒ `healthy`; else derive
  `pending_seq` (ack_floor None ⇒ `unknown`, logged); `get_msg` exists ⇒ `held`;
  `NotFoundError` ⇒ `phantom`; ANY other exception ⇒ `unknown` + WARNING —
  absence-of-failure: an API error never claims phantom, and `unknown` never cures.
- `async def cure_phantom(js, stream, durable) -> bool`: `delete_consumer` only.
  NO recreate here — recreation is the daemon's own `_attach_consumer` bind-or-create.
  Returns False + WARNING on error (never raises).
- Pure module: no SQLite, no ledger writes, no envelope publishes — broker-state
  surgery only, so there is NO ledger-lie surface by construction.

### 2. Boot cure — the new step in `serve.py:_run_serve`

Insert between `consumer_reconcile_on_boot` (≈:1161) and `compose_dispatch_chain` —
before the daemon task exists, so there is no live subscription and no race:

1. `report = inspect_ack_slot(...)` and log it at INFO (the boot health line).
2. `phantom` ⇒ ERROR log naming the seq + `cure_phantom` + **re-inspect with the SAME
   check** — the re-inspect must now report `healthy` (fix-and-re-verify in
   miniature); log the cure receipt at WARNING (an operator should see it happened).
3. `held` ⇒ INFO only (message still present — the normal redelivery path owns it).
4. `unknown` ⇒ WARNING, no action.
5. The WHOLE step is exception-guarded: a health-check bug must never block boot.

### 3. Runtime watchdog — alarm-only in v1 (the honest scope line)

A periodic asyncio task beside the daemon (interval `FORGE_ACK_WATCHDOG_SECONDS`,
default 300; `0` disables) running the SAME `inspect_ack_slot`:

- `phantom` ⇒ `logger.error` with the named wedge signature + set the shared state
  flag; **NO auto-cure mid-run in v1** — deleting the durable under the daemon's live
  `PullSubscription` invalidates it mid-fetch; the honest v1 is loud alarm + healthz
  visibility; an operator restart then triggers the boot cure. Said in code and here.
- The healthz endpoint gains an `ack_slot` field (`"healthy" | "held" | "phantom" |
  "unknown"`, last watchdog/boot reading) WITHOUT changing any existing key — the
  standing `curl :8088/healthz` check becomes wedge-visible. Builder pins the healthz
  seam file:line in code comments.

### 4. Tests (mock the JetStream API — NO broker anywhere, per the hardened playbook)

- inspect: healthy / held / phantom / ack_floor-None → unknown / API-error → unknown;
  the pending_seq arithmetic incl. Optional guards.
- cure: delete called with the right names; error → False, no raise.
- boot step: phantom → cure + re-inspect receipt; held → NO delete call ever;
  exception-guard proven (inspect raising cannot block boot).
- watchdog: interval firing, flag set on phantom, NO delete call on any status;
  disabled at 0.
- healthz: field present, existing keys byte-unchanged.

## Fences (binding on every builder and coach)

- Venue: **forge ONLY**. nats-core untouched. No consumer-config changes
  (`MAX_ACK_PENDING` stays 1; ack semantics/deferred-ack design untouched).
- **BROKER ISOLATION (standing playbook block)**: no NATS connections, no nats CLI, no
  port 4222 from builders/tests — mocks only. Coaches grep for live-broker access.
- No SQLite schema changes, no ledger writes from the new code, `.guardkit/**` and
  `uv.lock` untouched, no service restarts/deploys — local path-limited commits only.
- The forge full suite has ~12 known environment-coupled failures — prove deltas
  against a baseline worktree, never chase them.
- DEPLOY IS NOT THE BUILDERS': going live requires the attended forge-prod
  container-recreation recipe (rollback tag + DB backup + `/proc`-style verify) — the
  coordinator's step, after review, per the verify laws.

## Measurables (honest statement)

- **M0**: removes an attended-frontier surgery class from the routine path (wedge
  self-heals at boot; runtime alarm makes the failure loud instead of silent-for-25h).
  Supports the unattended-profile precondition.
- M1–M5: not moved by this lane; said plainly.
