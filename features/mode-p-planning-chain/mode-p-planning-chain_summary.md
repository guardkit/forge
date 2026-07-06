# Feature Spec Summary: Mode P Planning Chain

**Feature ID (fleet):** FEAT-SPL-002 (Sovereign Planning Loop, Session 3)
**Stack**: python
**Generated**: 2026-07-06T10:03:20Z (autonomous Fable session, `--auto`)
**Revised**: 2026-07-06 — TASK-REV-83E4 decision panel (+4 scenarios: RT-03/04/05/08;
7 assumptions amended in the manifest; report at `.claude/reviews/TASK-REV-83E4-review-report.md`)
**Scenarios**: 33 total (9 smoke, 0 regression)
**Assumptions**: 16 total (0 high / 0 medium / 16 low confidence; 7 panel-amended, all deferred)
**Review required**: Yes — REVIEW REQUIRED: all assumptions unconfirmed (--auto mode); Rich reviews the manifest

## Scope

Forge's Mode P planning chain: a pure-function planner (no reasoning model in the
chain) consumes `PlanningQueuedPayload` (nats-core 0.5.0, keyed by correlation id,
identity-pinned `originating_user` per DF-009), dispatches the `product_owner` stage
to the local PO specialist (Coach-scored) via the existing specialist dispatcher,
pauses at an identity-pinned `product_docs` approval checkpoint (never auto-approves;
escalates to the escalation approver on timeout or defer-cap), and on approval
executes the v1 `PLANNED-HANDOFF` terminal: commit approved
`feature_spec_inputs/<id>.md` to a planning branch in the target repo + publish a
notification carrying the exact attended `/feature-spec` command + mark the run row
`PLANNED-HANDOFF`. Includes the minimal planning approval-routing config (new
section — `ApprovalConfig` is a closed surface), the DF-004 `fallbacks:[]` boot
audit so planning model resolution can never silently escalate to cloud, and the
DF-006 config-gated frontier second-opinion (FLAG-only, compressed JSON brief,
degrade-to-human). The handoff step is registry-indirected so the FEAT-SPL-007/008
target terminal replaces it as configuration.

## Scenario Counts by Category

| Category | Count |
|----------|-------|
| Key examples (@key-example) | 8 |
| Boundary conditions (@boundary) | 5 |
| Negative cases (@negative) | 8 |
| Edge cases (@edge-case) | 12 |

## Deferred Items

None — no scenario groups were deferred. (Assumption-approval *dialogue* rendering
is out of scope by design: it is FEAT-SPL-003. The spec/plan specialist stages are
FEAT-SPL-007/008; this feature terminates at `PLANNED-HANDOFF`.)

## Open Assumptions (low confidence)

All 16 (ASSUM-001 … ASSUM-016) — `--auto` session; every `human_response` is
`deferred` for Rich's review. Load-bearing ones to review first:

- **ASSUM-001** — planning runs get their own durable run records keyed by
  correlation id (vs reusing builds rows, which mint ids from feature identity and
  allow only one pause).
- **ASSUM-002/003** — checkpoint composes with the TASK-GATE-D659 gate machinery
  via a planning stage label; expected approver = `originating_user` verbatim.
- **ASSUM-004/005** — escalation-instead-of-cancel at the first threshold; defer
  cap 3; escalated wait ceiling cancels (never auto-approves).
- **ASSUM-006/007/008** — handoff branch/file naming, notification-via-bus (jarvis
  renders Slack), PLANNED-HANDOFF as terminal state with registry-indirected step.
- **ASSUM-015** — ack-on-persist for planning intake (deliberate divergence from
  the build gate's held-slot invariant; avoids the ack-window redelivery wedge on
  human-latency pauses).

## Verified current-state inputs (2026-07-06, 7-agent re-verification)

- Wire contract FROZEN: nats-core 0.5.0 `PlanningQueuedPayload` + topic
  `pipeline.planning-queued.{correlation_id}` live in the forge venv; forge pins `<0.6`.
- Mode P absent everywhere in forge (mode enum, chains, planners, supervisor —
  swept clean); registration seam mapped incl. **schema_v3 migration** (builds.mode
  CHECK constraint) if the mode enum route is taken.
- **Production wiring gaps found**: serve.py's Supervisor construction passes no
  mode readers/planners (every live build falls back to Mode A logic), and the
  specialist dispatch orchestrator has no production composition. Mode P work must
  wire its path into serve composition or it will not run in the live daemon.
- Gate machinery (D659) is stage-agnostic and reusable; builds support one pause at
  a time; rearm owns all PAUSED re-emits; `ApprovalConfig` is closed (three fields,
  `extra="forbid"`, escalation fields explicitly forbidden).
- Guardkit seam `src/forge/adapters/guardkit/run.py` untouched rule confirmed.

## Integration with /feature-plan

This summary can be passed to `/feature-plan` as a context file:

    /feature-plan "Mode P Planning Chain" \
      --context features/mode-p-planning-chain/mode-p-planning-chain_summary.md
