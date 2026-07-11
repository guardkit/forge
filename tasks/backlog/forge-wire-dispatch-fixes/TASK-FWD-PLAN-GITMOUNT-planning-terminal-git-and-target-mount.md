---
id: TASK-FWD-PLAN-GITMOUNT
title: "Mode P PLANNED-HANDOFF terminal: forge-prod image needs git + target-repo mount"
status: backlog
created: 2026-07-11T11:00:00Z
priority: high
task_type: deploy
found_by: Session A MP-010 live validation (2026-07-11)
feature_ref: FEAT-SPL-002
tags: [mode-p, deploy, gb10, planning-handoff, found-2026-07-11]
complexity: 3
---

# Mode P planning terminal cannot complete on the live forge-prod image

## Problem (verified live, Session A 2026-07-11)

The planning PLANNED-HANDOFF terminal runs `WorktreeGitRunner()` **in-process**
(`src/forge/cli/_serve_planning.py:719` → `forge.planning.handoff.prepare_branch_and_write` →
`git worktree add`). On the live GB10 forge-prod:

- the runtime image (Debian bookworm, `python:3.14-slim`) installs **only `curl`** — **git is
  absent** (`Dockerfile` runtime stage ~line 154);
- forge-prod bind-mounts only `~/forge-prod-state/.forge` and `~/forge-state` — the target repo
  working copy is **not mounted** (container view: `repo_path is not a directory`).

Live proof: MP-010 run `523adb76` reached the terminal after a real phone approval and failed
`GitRunner failed: repo_path is not a directory: …/api_test` → run FAILED (clean, never raised).
Contrast: autobuild BUILDS work because they delegate to the host `forge-langgraph-sidecar`
(which has git + the repos); planning handoff does NOT delegate — it shells out in forge-prod.

## Acceptance criteria

- Decide the fix shape: (a) add `git` to the forge runtime image + bind-mount the target
  working copy(ies) into forge-prod at the `target_repo_paths` path; OR (b) delegate the planning
  handoff to the host sidecar (same seam builds use). Record the decision.
- If (a): add `git` to the runtime `apt-get install` line AND update
  `tests/dockerfile/test_install_layer.py` + the FEAT-FORGE-008 install-layer equivalence
  contract (the runbook §0.4/§6.1 literal-match) so the equivalence claim still holds.
- Re-run MP-010 AC-4/AC-5 on the rebuilt deploy: a real planning run reaches PLANNED-HANDOFF,
  writes `planning/{cid}` + `feature_spec_inputs/{cid}.md` in the target repo, notification
  carries the exact `/feature-spec` command; kill-NATS-mid-pause recovery completes.

## Notes
- Gates the "live planning" half of Session A's unblock (J05 / live planning). Pairs with
  TASK-FWD-PLAN-FLEETWATCHER (degraded PO content) — both must land before Mode P is
  production-ready. Evidence: `docs/state/TASK-MP-010/deploy-verification-2026-07-11-session-a.md`.
