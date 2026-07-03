---
id: TASK-ABW-OPS
title: Operator handoff — GB10 allowlist + sidecar restart workflow
task_type: operator_handoff
parent_feature: autobuild-runner-wireup
feature_id: FEAT-ABW1
wave: 2
implementation_mode: direct
complexity: 2
dependencies:
  - TASK-ABW-001
estimated_minutes: 30
status: pending
---

# TASK-ABW-OPS — Operator handoff: GB10 allowlist + sidecar restart

## Context

The wireup work in TASK-ABW-001 is a code change. It does not, and
cannot, modify host-level operational state on the GB10 where
forge-prod runs. This task captures the per-host manual steps an
operator must perform to bring the wired runner into a validated,
unattended-ready state.

**2026-07-02 update.** The original DDDSW demo (2026-05-16) has passed; this
task is no longer demo-blocking. It is now the **operational-validation gate for
the UBS keystone** — the step that turns "code-complete" (TASK-ABW-001) into
"proven on a real run". It also feeds **ADR-ARCH-033**: AC-OPS-05 must now
*archive the autobuild transcript* so the coach-score parser (a FEAT-UBS-002
prerequisite) can be built against real `guardkit autobuild --verbose` output
rather than an assumed format.

This task is `task_type: operator_handoff` — AutoBuild will not attempt
it. The operator must verify the runtime acceptance criteria below
manually, then mark the task complete via `/task-complete`.

Source plan: [`docs/research/ideas/autobuild-runner-wireup-plan.md`](../../../docs/research/ideas/autobuild-runner-wireup-plan.md) §Scope items 5 and 7.

## Required operator follow-up

### 1. Update forge.yaml allowlist on the GB10

The pipeline consumer's `_path_inside_allowlist` check rejects any
`feature_yaml_path` that does not resolve inside
`forge_config.permissions.filesystem.allowlist`. The new resolver in
TASK-ABW-001 applies the same check to the resolved repo cwd. The
allowlist on the GB10 must therefore include the local checkout of the
demo repo.

- [ ] **AC-OPS-01**: On the GB10 (`promaxgb10-*`), edit
  `~/forge-state/forge.yaml` and add the absolute path to
  `~/Projects/appmilla_github/api_test` to
  `permissions.filesystem.allowlist`. Tildes are not expanded by the
  forge config loader — write the fully expanded path.

### 2. Restart forge-prod to pick up the new allowlist

The forge config is loaded once at process start; an allowlist edit
without a restart has no effect.

- [ ] **AC-OPS-02**: Restart forge-prod and confirm via
  `forge status` (or the equivalent runbook command) that the new
  allowlist entry is present in the loaded config.

### 3. Restart the langgraph-runner sidecar (post-merge of TASK-ABW-001)

`langgraph dev` is launched with `--no-reload` per the multi-specialist
runbook §2.0. Edits to `autobuild_runner.py` are not picked up by a
running sidecar.

**Note (2026-07-02):** the `coach-ft-v3` Coach routing added on 2026-06-21
(`autobuild_runner.py` argv) is *also* dormant until this restart — any host
still on an older sidecar is running the **pre-fine-tune Coach**, so the
deployed behaviour differs from the code on disk. Confirm the restarted sidecar
is on the `coach-ft-v3` argv before treating any run as representative.

- [ ] **AC-OPS-03**: After TASK-ABW-001 merges and the new
  `forge:latest` image (if rebuilt) is available, restart the
  langgraph-runner sidecar with:

  ```bash
  pkill -f "langgraph dev"
  rm -rf .langgraph_api/
  # re-launch via the standard sidecar command
  ```

  Confirm the runner is healthy and re-listing `autobuild_runner` as a
  registered subagent.

### 4. Update the companion runbook

The runbook
`jarvis/docs/runbooks/RUNBOOK-jarvis-forge-autobuild-version-endpoint-demo.md`
currently documents §4.1 prompts against the stubbed runner. Update it
post-merge to reflect that real `guardkit autobuild` execution now
runs against the resolved repo cwd.

- [ ] **AC-OPS-04**: Edit the runbook to remove any "this is a stub —
  no code is written" caveats and add a note pointing to TASK-ABW-001
  as the change that closed the stub.

### 5. End-to-end rehearsal against FEAT-9E59

- [ ] **AC-OPS-05**: From a jarvis chat REPL, paste the prompt from
  `RUNBOOK-jarvis-forge-autobuild-version-endpoint-demo.md` §4.1 and
  observe ~33 min wall-clock. Confirm on the wire:
  - `pipeline.build-queued` + `build-started` + (≥1 `stage-complete`)
    + `build-complete` envelopes.
  - `autobuild/FEAT-9E59` branch in api_test with Player-Coach commits.
  - `src/version/router.py` exists with the `VersionResponse` schema
    and `GET /version` handler.
  - `pytest` passes when invoked from the worktree.

- [ ] **AC-OPS-06 (ADR-ARCH-033):** Capture and archive the full
  `guardkit autobuild --verbose` transcript from this run — both the runner's
  captured stdout and, if `--nats` streaming is on, the `pipeline.stage-complete.*`
  event payloads. Save under `docs/reviews/` (or the harvest corpus). This
  transcript is the input for building the coach-score parser that populates
  `last_coach_score` / `aggregate_coach_score` — the FEAT-UBS-002 prerequisite.
  Specifically note **whether the output contains a machine-parseable Coach score**
  (e.g. a `coach_score:` line or a JSON verdict block) and in what exact shape.

## Out of scope

- Any code changes. This is a manual, per-host task. Code changes
  belong in TASK-ABW-001 or a follow-up.
- Multi-host rollout. Only the demo GB10 is in scope for the
  2026-05-16 deadline.
