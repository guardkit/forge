---
id: TASK-UBS-002-integration
title: Wire the budget-guard skeleton into the supervisor + plumb the profile
task_type: feature_work
parent_feature: unattended-build-service
feature_id: FEAT-UBS-002
wave: 2
implementation_mode: pipeline
complexity: 6
dependencies:
  - FEAT-UBS-002-skeleton  # config models + budget_guard module + CLI flag (landed)
  - TASK-ABW-OPS           # produces the autobuild transcript → coach-score gap
status: in_review
updated: 2026-07-09T00:00:00Z
---

> **✅ ENFORCEMENT SEAM DONE 2026-07-09 (WS3-S6) — §1 built + demonstrably
> enforcing on a fixture run; §2/§3 dispositioned below.**
>
> **§1 supervisor enforcement seam (done):** `Supervisor` gained DI fields
> `budget_guards` / `budget_profile_name` / `budget_wall_clock` /
> `budget_started_at_reader` / `budget_pause`, and a new
> `TurnOutcome.PAUSED_BUDGET`. At the follow-up `TASK_REVIEW` cyclic step
> `_next_turn_mode_c` calls `_enforce_mode_c_budget`: computes
> `BuildBudgetMetrics` (review_cycles via `count_review_cycles`, elapsed via
> `_budget_elapsed_seconds`, tokens/coach-score `None`), runs
> `evaluate_budget`, and on a breach refuses the dispatch, builds the
> `risk_level="high"` `build_budget_breach_approval_payload`
> (`details.reason == "budget_guard_breach"`), invokes the injected
> `budget_pause` (publish + pause), and returns `PAUSED_BUDGET`. **Strict
> no-op for attended / caps-off / unwired profiles (ASSUM-010 intact).**
> GATE met: `TestModeCBudgetEnforcement` (5 tests) — breach→pause+risk-high,
> under-cap→dispatch, attended→no-op, no-guards→no-op, and (merge-review
> hardening) already-PAUSED→WAITING (never re-escalate with the same
> deterministic request_id, never dispatch a paused build) — green; full
> supervisor suite green.
>
> **§2 profile carriage (DEFERRED, per the task's own "until then
> default_profile"):** the `builds.profile` column + `record_pending_build`
> / `_row_to_build_row` / `BuildRow` carriage is NOT built this session —
> the daemon would resolve `config.budget.resolve(default_profile)` (=
> `attended` = caps off = safe no-op) until it lands. Remaining production
> hookup: wire `budget_guards` + a `budget_pause` callback (SQLite PAUSED +
> `emit_paused_then_interrupt`, ADR-ARCH-021 order) into `build_supervisor`
> — currently blocked because `build_supervisor` does not yet wire
> `mode_c_planner` at all (Mode C is dormant in the production supervisor
> builder), so the enforcement is fixture-demonstrable but dormant in prod
> until Mode C is live. This matches the task's own note: "cannot be
> end-to-end validated without a real Mode C run."
>
> **✅ §2(a) PROFILE CARRIAGE DONE 2026-07-26 (AC-04) — forge-only, additive.**
> The `--profile` selection now travels to the daemon on the build row (the
> nats-core seam stays frozen; option §2(b) barred). Shipped:
> - `src/forge/lifecycle/schema_v5.sql` — additive `ALTER TABLE builds ADD
>   COLUMN profile TEXT` (NULL-able, no default → `resolve(None)` =
>   `default_profile` = attended/caps-off = backward-compatible for historical
>   rows); registered as migration 5 in `migrations.py:40-47`
>   (`_SCHEMA_VERSION=5`, `_MIGRATIONS += (5, "schema_v5.sql")`).
> - `persistence.py`: `BuildRow.profile: str | None = None` (L218);
>   `_row_to_build_row` reads it (defensive keys L303, construction L338);
>   `record_pending_build(..., profile=)` sniffs `payload.profile` then INSERTs
>   the column (L723); `queue_build(..., profile=)` forwards it (L794).
> - `cli/queue.py:798` — `queue_build(payload, mode=..., profile=profile_name)`;
>   the stale "not yet plumbed" NOTE is replaced by an enforcement-pending NOTE
>   (fires only for a capped profile) since the profile now reaches the row.
> Tests: `tests/unit/lifecycle/test_schema_v5_migration.py` (fresh→v5, additive,
> existing-v4 upgrade reads old rows NULL, idempotent);
> `tests/forge/test_profile_carriage.py` (unattended lands + daemon
> `resolve(row.profile)` yields unattended caps; NULL row → default);
> `test_cli_profile_flag.py` updated to the new truth (carriage asserted).
> **Still deferred (out of this task's scope):** the daemon-side ENFORCEMENT
> wiring into `serve.py build_supervisor` — Mode C stays dormant in prod, so
> caps travel + resolve but do not yet pause a live build. §3 coach-score floor
> unchanged.
>
> **§3 coach-score floor (SUPERSEDED — see the 2026-07-26 closure below).**
> ~~`last_coach_score` stays `None` (ADR-ARCH-033); the `min_coach_score`
> branch is inert and activates automatically when the runner feeds a real
> score. No `budget_guard` change needed.~~
>
> **✅ §3 COACH-SCORE FLOOR WIRE DONE 2026-07-26 (AC-05) — DI seam + fixture
> reader; production reader deferred to the Mode-C-production lane.**
> The supervisor no longer hardcodes `last_coach_score=None`. Shipped
> (`src/forge/pipeline/supervisor.py`, no `budget_guard.py` change):
> - New DI field `budget_coach_score_reader: (build_id) -> float | None`
>   (default `None`), mirroring `budget_started_at_reader`. Default `None`
>   → `last_coach_score=None` → floor inert → byte-identical to the pre-reader
>   path (AC-03 preserved).
> - New helper `_budget_last_coach_score(build_id)`: returns the reader's value
>   when wired; a reader failure degrades to `None` with a loud `logger.error`
>   (mirrors the `build_mode_reader` safe-default shape) so enforcement never
>   crashes and the OTHER caps still enforce.
> - `_enforce_mode_c_budget` metrics assembly now feeds
>   `last_coach_score=self._budget_last_coach_score(build_id)`; the evaluator's
>   `min_coach_score` branch activates automatically on a non-None score.
> - Tests (`TestModeCBudgetEnforcement`, +4 → 9 total): below-floor → PAUSED
>   `budget_guard_breach`/`min_coach_score`; at-floor → dispatches; reader
>   None/unwired → dispatch (floor inert); reader raises → loud-log + None +
>   the review-cycle cap still fires.
>
> **HONEST FINDING — the production reader is NOT wired this session, by
> design.** Investigation of the UBS1C population (`autobuild_runner.py`
> ~1900-2032): the decision-derived `last_coach_score` (1.0 success / 0.0
> feedback) lands on the in-memory `AutobuildRunnerState` snapshot and flows
> outward only through the lifecycle emitter / nats payloads
> (`translation.py` `StageCompletePayload.coach_score`, `autobuild_runner.py`
> L620) — the streaming path. The Mode C **Supervisor** runs a *different*
> execution path (ADR-ARCH-033 two-path split) and has no store it can query
> **by build_id** for that specific streaming value. (The forge SQLite
> `stage_log.coach_score` column *is* queryable via `read_stages(build_id)`,
> but it carries the **gating/specialist** coach scores, not the UBS1C
> streaming aggregate.) Per the task's scope, this session therefore ships the
> **DI seam + a documented fixture reader in tests** only. **The production
> reader lands with the Mode-C-production lane** (the same lane that wires
> `serve.py build_supervisor`, explicitly out of this task's scope) — that lane
> chooses and injects the real `budget_coach_score_reader` alongside the live
> Mode C planner.

# TASK-UBS-002-integration — connect the budget guard to the live build loop

## Context

The FEAT-UBS-002 **skeleton** has landed (2026-07-02):

- `forge.config.models.BudgetGuards` / `BudgetConfig` — named profiles;
  `attended` reserved as caps-off (ASSUM-010); `unattended` conservative caps.
- `forge.pipeline.budget_guard` — pure `evaluate_budget`, the escalation
  details dict, and the `risk_level="high"` `ApprovalRequestPayload` builder.
- `forge queue --profile` — validates the name + echoes the caps.

Three integration pieces were **deliberately deferred** because they touch the
live supervisor loop and/or the frozen `nats-core` seam and cannot be
end-to-end validated without a real Mode C run. This task closes them.

## Scope

### 1. Supervisor enforcement seam
Wire `evaluate_budget` into `ForgeSupervisor._next_turn_mode_c`
(`src/forge/pipeline/supervisor.py`, planner call at ~L1269):

- Inject the resolved `BudgetGuards` for the build (see §2) + a wall-clock
  source into the supervisor's constructor (dependency injection, mirroring
  `mode_c_planner` / `mode_c_history_reader`).
- After the planner returns a **follow-up** `TASK_REVIEW` (the cyclic step),
  compute `BuildBudgetMetrics`:
  - `review_cycles` via `budget_guard.count_review_cycles(history,
    is_review=lambda e: e.stage_class == StageClass.TASK_REVIEW)`;
  - `elapsed_wallclock_seconds` from the build's `started_at`;
  - `tokens_used` if available (else `None`);
  - `last_coach_score` from the build (see §3 — `None` until the gap closes).
- On a breach: do **not** dispatch. Add a `TurnOutcome.PAUSED_BUDGET`, emit the
  `build_budget_breach_approval_payload(...)` via the approval publisher, then
  pause via `PipelineLifecycleEmitter.emit_paused_then_interrupt` (ADR-ARCH-021
  ordering: publish `build-paused` before `interrupt()`). Never a silent stop,
  never a silent continue (scope §4).
- The guard is a no-op for attended profiles (`caps_enabled is False`) — do not
  touch the ModeCCyclePlanner (ASSUM-010 stays intact).

### 2. Carry the profile across the queue→daemon boundary
`--profile` is validated at queue time but not yet delivered to the daemon —
`BuildQueuedPayload` (nats-core) has no profile field and the `builds` table has
no profile column. Pick one:
- **(a)** Add `builds.profile TEXT` (schema.sql migration + `record_pending_build`
  INSERT + `_row_to_build_row` + `BuildRow`); the daemon reads it and resolves
  caps via `config.budget.resolve(row.profile)`. Forge-only; preferred.
- **(b)** Add `BuildQueuedPayload.profile` in nats-core — a **seam change**, so
  it goes through an ADR + coordinated release, not a forge session.
Until then the daemon applies `config.budget.default_profile`.

### 3. Activate the coach-score floor
The `min_coach_score` branch in `evaluate_budget` is inert while
`last_coach_score` is `None` (ADR-ARCH-033). This unblocks once the coach-score
gap closes (the runner populates `AutobuildState.last_coach_score` from the
transcript captured by TASK-ABW-OPS AC-OPS-06). No change to `budget_guard` is
needed — just feed a real score into `BuildBudgetMetrics`.

## Acceptance criteria
- [ ] AC-01: A Mode C build under an unattended profile that reaches
  `max_review_cycles` pauses and emits a `risk_level="high"` approval request
  whose `details.reason == "budget_guard_breach"`; SQLite shows PAUSED.
- [ ] AC-02: A wall-clock breach on a stalled stage pauses + notifies.
- [ ] AC-03: An attended build (caps off) is never paused by the guard —
  ModeCCyclePlanner behaviour is byte-for-byte unchanged.
- [ ] AC-04: `forge queue --profile unattended` delivers the profile to the
  daemon (via §2), verified by the daemon resolving the unattended caps.
- [x] AC-05 (2026-07-26): With a real `last_coach_score` present (supplied by
  the `budget_coach_score_reader` DI seam), a score below the floor pauses;
  at/above proceeds. Proven via the fixture reader in `TestModeCBudgetEnforcement`;
  the production reader lands with the Mode-C-production lane (see the §3 banner
  note — the streaming score is not Supervisor-queryable by build_id).

## Out of scope
- The QA-Verifier fine-tune and the coach-score *parser* itself (guardkit /
  ADR-ARCH-033 — this task only *consumes* a populated score).
- Editing `nats-core` unless option §2(b) is explicitly chosen via ADR.
