# Activation-bundle rehearsal (G-09 · Lane B / Phase E1 B4-prep)

Hermetic rehearsal of `scripts/activate_planning.py` — the ONE receipted,
reversible operator step that turns **live planning** on for the B4 window
(`planning.enabled` + `planning.target_terminal.enabled` ON, member-id approver +
default target repo ensured) and can turn it back off (`--rollback`).

Rehearsed **against a config copy only** (`fixture-forge.resting.yaml`) — never
forge-prod, never the live NATS bus, never a container (rule 8). The script
mutates a config file and writes a receipt; the forge-prod recreate that makes
the flip take effect is the coordinator's attended step at the window.

## Receipts (`receipts/`)

The rehearsal drove the full sequence against a resting-state copy:

| # | invocation | mode | dry-run | changed |
|---|---|---|---|---|
| 1 | `--dry-run` | apply | yes | yes (computed, nothing written) |
| 2 | (default) | apply | no | yes (config written) |
| 3 | (default) again | apply | no | **no** (idempotent — left as-is) |
| 4 | `--rollback --dry-run` | rollback | yes | yes (computed) |
| 5 | `--rollback` | rollback | no | yes (restored resting) |

Each receipt is a JSON before/after state dump + the preflight checklist. The
config copy ends back in the **resting state** (both flags OFF).

## Running it for real at B4

```bash
# On the GB10, against the live config (default ~/forge-state/forge.yaml):
python scripts/activate_planning.py --dry-run          # preview
python scripts/activate_planning.py                    # flip ON (writes config + receipt)
#   → then recreate forge-prod (attended: Ack-Pending-0 + worker-free, rollback tag first)
# Kill switch:
python scripts/activate_planning.py --rollback         # flip OFF (resting state)
```

The three `confirm` checklist items (broker notification ACL, Slack app
scopes/invite, fleet-watcher `nats_url`) are the J04 / MP-010 out-of-band
prerequisites — established live in MP-010 and confirmed by the coordinator at
the window, not set by this script.
