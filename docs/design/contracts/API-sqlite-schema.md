# API Contract — SQLite Schema (`~/.forge/forge.db`)

> **Type:** Internal data contract — authoritative build history
> **Engine:** SQLite 3, WAL mode
> **Path:** `~/.forge/forge.db` (configurable via `forge.yaml.sqlite.path`)
> **Related ADRs:** [ADR-ARCH-009](../../architecture/decisions/ADR-ARCH-009-omit-langgraph-checkpointer.md), [ADR-ARCH-013](../../architecture/decisions/ADR-ARCH-013-cli-read-bypasses-nats.md), [ADR-SP-013](../../research/forge-pipeline-architecture.md)

---

## 1. Purpose

SQLite is Forge's durable authoritative state store. Two tables:

- `builds` — one row per build attempt, lifecycle-tracked.
- `stage_log` — many rows per build, one per reasoning-model-chosen stage dispatch.

Concurrency model: **single writer (the agent runtime) + multiple readers (CLI)**, enforced by WAL mode. The CLI never writes; the agent never blocks reads.

LangGraph checkpointer is deliberately **not** used (ADR-ARCH-009) — SQLite + JetStream is authoritative for Forge's recovery story.

---

## 2. DDL

### 2.1 `builds` table

```sql
CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,                       -- build-{feature_id}-{YYYYMMDDHHMMSS}
    feature_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    feature_yaml_path TEXT NOT NULL,
    project TEXT,                                    -- NULL = fleet-wide; else "finproxy", "guardkit", etc.

    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'PREPARING', 'RUNNING', 'PAUSED', 'FINALISING',
        'COMPLETE', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'SKIPPED'
    )),

    triggered_by TEXT NOT NULL CHECK (triggered_by IN (
        'cli', 'jarvis', 'forge-internal', 'notification-adapter'
    )),
    originating_adapter TEXT,                        -- nullable
    originating_user TEXT,                           -- nullable
    correlation_id TEXT NOT NULL,
    parent_request_id TEXT,                          -- Jarvis dispatch ID

    queued_at TEXT NOT NULL,                         -- ISO 8601 UTC
    started_at TEXT,                                 -- PREPARING transition
    completed_at TEXT,                               -- terminal state transition

    worktree_path TEXT,                              -- /var/forge/builds/{build_id}/
    pr_url TEXT,
    error TEXT,                                      -- structured reason on FAILED/INTERRUPTED/CANCELLED

    max_turns INTEGER NOT NULL DEFAULT 5,
    sdk_timeout_seconds INTEGER NOT NULL DEFAULT 1800
) STRICT;

CREATE INDEX IF NOT EXISTS idx_builds_feature ON builds (feature_id, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_builds_status ON builds (status, queued_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_builds_feature_correlation ON builds (feature_id, correlation_id);
```

### 2.2 `stage_log` table

```sql
CREATE TABLE IF NOT EXISTS stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES builds(build_id),

    stage_label TEXT NOT NULL,                       -- Reasoning-model-chosen (ADR-ARCH-016)
    target_kind TEXT NOT NULL CHECK (target_kind IN (
        'local_tool', 'fleet_capability', 'subagent'
    )),
    target_identifier TEXT NOT NULL,                 -- tool name / agent_id:tool_name / subagent name

    status TEXT NOT NULL CHECK (status IN (
        'PASSED', 'FAILED', 'GATED', 'SKIPPED'
    )),
    gate_mode TEXT CHECK (gate_mode IN (
        'AUTO_APPROVE', 'FLAG_FOR_REVIEW', 'HARD_STOP', 'MANDATORY_HUMAN_APPROVAL'
    )),                                              -- NULL for PASSED stages that didn't gate

    coach_score REAL,                                -- 0.0–1.0; NULL if degraded / not applicable
    threshold_applied REAL,                          -- NULL when reasoning-driven (no static threshold)

    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_secs REAL NOT NULL,

    details_json TEXT NOT NULL                       -- JSON blob — rationale + breakdown + detections
) STRICT;

CREATE INDEX IF NOT EXISTS idx_stage_log_build ON stage_log (build_id, started_at);
CREATE INDEX IF NOT EXISTS idx_stage_log_gated ON stage_log (build_id, gate_mode)
    WHERE gate_mode IS NOT NULL;
```

### 2.3 `schema_version` table

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

-- Seed row on first boot
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, datetime('now'));
```

Migrations are explicit and sequential; see [DDR-003-sqlite-schema-layout-wal.md](../decisions/DDR-003-sqlite-schema-layout-wal.md) for rationale.

---

## 3. WAL + pragmas

Set on every connection in `forge.adapters.sqlite.connect()`:

```python
cx.execute("PRAGMA journal_mode = WAL;")
cx.execute("PRAGMA synchronous = NORMAL;")           # WAL is safe with NORMAL
cx.execute("PRAGMA foreign_keys = ON;")
cx.execute("PRAGMA busy_timeout = 5000;")            # 5s; CLI readers handle contention
```

**Write connection:** single, held by the agent runtime for the lifetime of the process. Serialised writes via `forge.adapters.sqlite._write_lock` (asyncio lock).

**Read connections:** per-CLI-invocation. `forge.adapters.sqlite.read_only_connect()` opens with `mode=ro` URI filename.

---

## 4. Write API (agent side)

```python
# forge.adapters.sqlite.writer
async def create_build(payload: BuildQueuedPayload) -> None: ...
async def transition_build(build_id: str, to_status: BuildStatus, **fields) -> None: ...
async def record_stage(entry: StageLogEntry) -> None: ...
async def mark_paused(build_id: str, approval_payload: ApprovalRequestPayload) -> None: ...
async def reconcile_on_boot() -> list[Build]: ...    # Returns builds needing recovery
```

All methods:

1. Accept typed Pydantic inputs (no raw dicts at this boundary).
2. Validate status transitions against the state machine (`forge.state_machine.valid_transitions`).
3. Emit a structured log entry (`structlog`) with `build_id`, `transition`, and duration.
4. Raise `ForgeSQLiteError` (subclass of `RuntimeError`) on constraint violations — caught at the tool boundary and converted to a structured error string per ADR-ARCH-025.

---

## 5. Read API (CLI side)

```python
# forge.adapters.sqlite.reader
def read_status(feature_id: str | None = None) -> list[BuildStatusView]: ...
def read_history(limit: int = 50, feature_id: str | None = None) -> list[BuildRow]: ...
def read_stages(build_id: str) -> list[StageLogEntry]: ...
def read_paused_builds() -> list[Build]: ...
```

Reads run synchronously (Click is sync); no coroutines involved.

---

## 6. Crash-Recovery Semantics

On boot, `reconcile_on_boot()` returns builds whose status is not terminal:

| SQLite status on boot | Recovery action |
|---|---|
| `QUEUED` | Wait for JetStream redelivery; no-op |
| `PREPARING` | Mark `INTERRUPTED`, publish `pipeline.build-failed` with `recoverable=True`, JetStream will redeliver → restart |
| `RUNNING` | Mark `INTERRUPTED` (retry-from-scratch policy per anchor §5) |
| `PAUSED` | Re-enter PAUSED; re-emit `ApprovalRequestPayload`; re-fire `interrupt()` when graph reruns |
| `FINALISING` | Mark `INTERRUPTED` with warning — PR creation may have succeeded; operator reconciles manually |
| `COMPLETE`/`FAILED`/`CANCELLED`/`SKIPPED` | Ack any residual JetStream message; no-op |

### 6.1 Boot-actor ownership (TASK-GATE-D659 §D4)

With the daemon-side pre-dispatch approval gate live, three **distinct** boot
actors run in a fixed order, each owning a disjoint slice of the recovery
matrix. The split closes the arch-review C1 boot-order tap-drop (a PAUSED
approval re-emitted before any response subscriber exists is silently dropped
on core NATS) and the C2 crash-mid-hop wedge.

| Boot actor | Runs at | Owns | PAUSED re-emit |
|---|---|---|---|
| `lifecycle.recovery.reconcile_on_boot` | boot step 2 (before compose) | `PREPARING`/`RUNNING`/`FINALISING` → `INTERRUPTED` + `build-failed` | **Suppressed** — passed a no-op `ApprovalRepublisher` (INFO log per suppressed row); recovery.py is otherwise unmodified |
| `adapters.nats.pipeline_consumer.reconcile_on_boot` | boot step 3 (before compose) | in-flight / `INTERRUPTED` redelivery redispatch (Branch-2) | **Suppressed** — empty `iter_paused_builds`; no-op re-emit fns |
| `cli._serve_gate_activation.rearm_paused_gates` | boot step 3.5 (`_compose`, live subscriber armed) | re-arming every `PAUSED` build's approval round-trip | **Owner of BOTH** — re-emits the `ApprovalRequestPayload` (verbatim persisted `request_id` + `correlation_id`) FIRST and `pipeline.build-paused.{feature_id}` SECOND, **arm-before-post** (the response subscription is provably live before either re-emit reaches the wire) |

So the `PAUSED` row in the table above resolves as: **status stays `PAUSED`; both
the approval request AND `pipeline.build-paused` are re-emitted verbatim (same
`request_id`, same `correlation_id`) by `rearm_paused_gates` only** — satisfying
`API-nats-pipeline-events §4` + `FEAT-FORGE-010:245-249` (build-paused
re-emitted) and this section's original intent (request re-emitted, verbatim
id). A crash inside the `QUEUED → … → PAUSED` synthetic-hop window leaves an
`INTERRUPTED` row that re-enters the lifecycle via the consumer twin seam
(Branch-2) or, on the first post-boot redelivery, `dispatch_build`'s three-arm
`INTERRUPTED → redispatch` (never skip-without-ack, which would wedge the
`max_ack_pending=1` consumer).

---

## 7. Backup + Retention

- WAL auto-checkpoints at 1000 frames (SQLite default).
- `~/.forge/forge.db` is backed up weekly via host cron to `~/.forge/backups/forge-YYYYMMDD.db.gz` (out of Forge scope; documented in `ops/backup.md`).
- Retention: indefinite. `builds` grows at ≤ 1 row/build (sequential execution caps this at ≤ a few hundred/year); `stage_log` grows at ~30 rows/build. `VACUUM` advisable quarterly; no automatic pruning.

---

## 8. Related

- Data model: [DM-build-lifecycle.md](../models/DM-build-lifecycle.md)
- CLI read path: [API-cli.md](API-cli.md)
- DDR: [DDR-003-sqlite-schema-layout-wal.md](../decisions/DDR-003-sqlite-schema-layout-wal.md)
