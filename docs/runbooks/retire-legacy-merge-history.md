# Retire the inspected pre-routing merge history

This is an attended one-off operation for the owner-approved 35 historical
approvals and six retained publication holders. It is not a general cleanup
command. Do not run it until the reviewed release containing permanent
retirement enforcement is available and all old producers are stopped.

The quiescence JSON is an audit input. It records observations made by the
operator; neither the JSON nor SQLite grants permission or proves process
absence. Discover the current deployment and container names during the
window. Do not reuse a historical name.

Set private paths and the reviewed source identity:

```bash
FORGE_CHECKOUT=/path/to/reviewed/forge
FORGE_DB=/path/to/live/forge.db
REVIEWED_COMMIT=<40-character-reviewed-commit>
PRIVATE_DIR=/path/to/private/retirement-audit
PYTHON=/path/to/reviewed/forge-venv/bin/python
SCRIPT="$FORGE_CHECKOUT/scripts/retire_legacy_merge_history.py"
test "$(git -C "$FORGE_CHECKOUT" rev-parse HEAD)" = "$REVIEWED_COMMIT"
install -d -m 700 "$PRIVATE_DIR"
```

Before any migration or mutation, exclude intake and new CLI starts, stop the
resolved old coordinator and related producers under the applicable authority,
and observe callbacks, standalone CLIs, remote/runner/model tasks, deployment
work, and paused old tasks. Save the exact commands and evidence locations.
Take and verify a private pre-migration SQLite backup using the estate's
reviewed backup procedure. A migration must run in its own one-off process with
no recovery or consumers; never start the coordinator merely to migrate.

If the ledger is below v18, apply the repository's reviewed migrations in that
stopped window. Verify the only expected change, schema version 18, and zero
`feature_routing_seeds`. Then repeat all observations and census checks.

Capture the complete current inventory. This command reads every current
approved decision, historical merge-deploy stage on those builds, and retained
holder; it refuses any
set other than the exact 35 distinct approvals, 13 executor-PASSED, 18
executor-FAILED, and four merge-GATED latest later outcomes, and the six-holder
subset. Every historical merge-deploy stage on those builds is inventoried,
hashed, validated and preserved; the command never chooses a subset just to
meet a count. A merge-GATED outcome is recorded unfinished history and is not
treated as a successful or terminal executor report.

```bash
"$PYTHON" "$SCRIPT" \
  --database "$FORGE_DB" \
  --capture-inventory "$PRIVATE_DIR/inventory.json" \
  --history-before <inspected-UTC-cutoff> \
  --audit-sha256 <64-lowercase-hex-audit-attribution>
```

Inspect the private inventory and independently compare its IDs, hashes,
stage-log high-water mark, holder CAS identities, and cutoff to the final
attended observations. Capture changed or new work as a new inspection; do not
edit the file to make it pass. The script creates it mode 0600.

Run the ordinary read-only verification:

```bash
"$PYTHON" "$SCRIPT" \
  --database "$FORGE_DB" \
  --inventory "$PRIVATE_DIR/inventory.json"
```

It must print exactly a dry-run object with 35 approved builds and six retained
holders. Create mode-0600 `quiescence.json` with exact keys below. Every boolean
must reflect a fresh attended observation, `paused_old_tasks` must be zero,
`deployment_observed` must name the deployment actually resolved during this
window, and `observation_provenance` must list the commands/evidence used.

```json
{
  "format_version": 1,
  "observed_at": "<UTC timestamp>",
  "producers_stopped": true,
  "coordinator_callbacks_absent": true,
  "standalone_clis_absent": true,
  "remote_tasks_absent": true,
  "runner_tasks_absent": true,
  "model_tasks_absent": true,
  "deployment_work_absent": true,
  "paused_old_tasks": 0,
  "deployment_observed": "<resolved deployment/container identity>",
  "observation_provenance": ["<command and private evidence reference>"]
}
```

Apply once. Both backup and audit destinations must not exist. The script
atomically creates them mode 0600, makes a SQLite backup, verifies
`integrity_check`, writes and fsyncs a pending audit record, then performs the
35 receipt inserts and six holder CAS releases in one `BEGIN IMMEDIATE`
transaction. A failed later CAS rolls back every earlier change.

```bash
"$PYTHON" "$SCRIPT" \
  --database "$FORGE_DB" \
  --inventory "$PRIVATE_DIR/inventory.json" \
  --apply \
  --backup "$PRIVATE_DIR/forge-v18-before-retirement.db" \
  --audit-output "$PRIVATE_DIR/retirement-audit.json" \
  --quiescence "$PRIVATE_DIR/quiescence.json" \
  --reviewed-source-commit "$REVIEWED_COMMIT"
```

Require `mode=applied`, 35 retirement receipts, six released holders, and audit
status `committed`. Re-run the maintenance guards with the reviewed Forge and
router readers. Verify all original merge-deploy stage bytes and the 13/18/4
latest outcomes are unchanged, including every earlier executor report; only 35
administrative stage rows and the six holders' lease holder, lease expiry, and
update timestamp may differ.

Start the reviewed new coordinator/CLI release before reopening intake. Old
binaries do not enforce retirement receipts. Rolling back to old code while
retaining the receipts is unsupported and requires another stopped-producer
window plus an explicit reviewed treatment. There is no clear-now/enforce-later
interval.
