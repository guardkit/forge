-- Forge SQLite schema v5 (TASK-UBS-002-integration §2 — profile carriage).
--
-- Additive migration that adds the ``profile`` column to the ``builds``
-- table (v1 schema.sql) so a ``forge queue --profile <name>`` selection
-- travels across the queue→daemon boundary via the build row rather than
-- the frozen ``nats-core`` ``BuildQueuedPayload`` seam (option §2(a),
-- forge-only). The daemon reads ``builds.profile`` and resolves caps via
-- ``config.budget.resolve(row.profile)``.
--
-- The column is NULL-able with no default: a NULL value means "no
-- per-build profile requested", which ``BudgetConfig.resolve(None)`` maps
-- to ``config.budget.default_profile`` (``attended`` = caps off, ASSUM-010).
-- Historical rows therefore read back as NULL and resolve to the attended
-- default with zero data movement — backward-compatible by construction.
--
-- This script is **delta-only**: schema.sql / schema_v2.sql / schema_v3.sql
-- / schema_v4.sql remain frozen. The migrations runner
-- (``forge.lifecycle.migrations.apply_at_boot``) executes v1–v5 in order for
-- fresh databases and applies only v5 to existing v4 databases.
--
-- SQLite-specific note: ``ALTER TABLE ... ADD COLUMN`` is not
-- IF-NOT-EXISTS-aware, so the runner relies on the schema_version ledger
-- (below) to ensure this script only runs once per database — the same
-- discipline schema_v2.sql uses for the ``mode`` column.

ALTER TABLE builds
    ADD COLUMN profile TEXT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (5, datetime('now'));
