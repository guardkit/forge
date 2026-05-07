---
id: TASK-FRR-PEB-FM-003
title: Fix Files-to-Modify path typos in PEB-006 and PEB-012, scrub PEB-006 autobuild_state
status: completed
created: 2026-05-07T00:00:00Z
updated: 2026-05-07T00:00:00Z
completed: 2026-05-07T00:00:00Z
completed_location: tasks/completed/TASK-FRR-PEB-FM-003/
previous_state: in_review
state_transition_reason: "All 8 ACs verified; pure markdown edits, no test gates"
priority: high
priority_band: P0
task_type: feature
parent_run: autobuild-FEAT-PEBR-fail-run-4
parent_run_log: docs/history/autobuild-FEAT-PEBR-fail-run-4.md
parent_feature: FEAT-PEBR
feature_id: FEAT-PEBR
related_tasks:
  - TASK-FRR-PEB-006
  - TASK-FRR-PEB-012
  - TASK-FRR-PEB-FM-002
  - TASK-GK-PA-002
implementation_mode: direct
wave: 0
complexity: 1
estimated_minutes: 15
dependencies: []
tags:
  - forge-task-frontmatter
  - autobuild
  - feat-pebr
  - unblock
  - P0
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Fix Files-to-Modify path typos in PEB-006 and PEB-012, scrub PEB-006 autobuild_state

## Description

FEAT-PEBR autobuild **run-4** (2026-05-07) approved 4 more tasks
(PEB-005, -007, -011, -014) but failed on PEB-006 with plan-audit
flagging `missing_files: [src/forge/cli/_approval_subscriber.py,
tests/forge/test_approval_subscriber.py]` for 4 consecutive turns,
eventually exiting via `timeout_budget_exhausted`.

The Player implementation was correct — `coach_turn_4.json` records
all 6 ACs verified, 6/6 criteria met. The Player modified the
**real** files at `src/forge/adapters/nats/approval_subscriber.py`
and the test file at `tests/forge/adapters/test_approval_subscriber.py`.

**The bug is task-side**: `## Files to Modify` in the task body
declares paths that don't exist on disk. TASK-GK-PA-002 (rev-2 fix)
correctly treats `## Files to Modify` as authoritative and flags the
typo'd paths as missing. The fix is to correct the declared paths to
match disk reality.

A parallel typo audit of all remaining FRR-PEB tasks turned up one
more: PEB-012 declares `tests/forge/cli/test_status.py` (real path:
`tests/forge/test_cli_status.py`). Fix both in a single forge commit
to unblock waves 5-8.

## Verified file existence

| Task    | Declared (current — wrong)                          | Real path (verified via `find`)                              |
|---------|-----------------------------------------------------|--------------------------------------------------------------|
| PEB-006 | `src/forge/cli/_approval_subscriber.py`             | `src/forge/adapters/nats/approval_subscriber.py`             |
| PEB-006 | `tests/forge/test_approval_subscriber.py`           | `tests/forge/adapters/test_approval_subscriber.py`           |
| PEB-012 | `tests/forge/cli/test_status.py`                    | `tests/forge/test_cli_status.py`                             |

PEB-008, -009, -013 declare files under `## Files to Create` that
don't exist on disk yet — that is **correct** (the Player will
create them) and out of scope for this task.

## Acceptance Criteria

- [ ] **AC-1 — PEB-006 `## Files to Modify` corrected.** In
  `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-pause-resume-canonicalisation.md`:
  - Replace `src/forge/cli/_approval_subscriber.py` (line 157) with
    `src/forge/adapters/nats/approval_subscriber.py`.
  - Replace `tests/forge/test_approval_subscriber.py` (line 158) with
    `tests/forge/adapters/test_approval_subscriber.py`.
- [ ] **AC-2 — PEB-006 prose / AC text aligned.** Search PEB-006's
  body for any other occurrence of the typo strings
  (`src/forge/cli/_approval_subscriber` or
  `tests/forge/test_approval_subscriber`) and either correct them to
  the real path or remove the bullet if it was informational. Verify
  with: `grep -n "_approval_subscriber\|test_approval_subscriber" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-*.md` — every match must reference the correct path.
- [ ] **AC-3 — PEB-006 `autobuild_state.turns[*]` scrubbed.** The
  PEB-006 frontmatter contains a stale `autobuild_state.turns` block
  with 4 persisted Coach feedback strings, each containing the typo'd
  paths. Either remove the `autobuild_state` key entirely or set to
  `autobuild_state: {}`. Reset `status:` to `backlog` (currently
  `blocked`).
- [ ] **AC-4 — PEB-012 `## Files to Modify` corrected.** In
  `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-012-forge-status-in-flight-surface.md`:
  - Replace `tests/forge/cli/test_status.py` (line 82) with
    `tests/forge/test_cli_status.py`.
- [ ] **AC-5 — PEB-012 Coach validation commands updated.** Line 95
  of PEB-012 has `PYTHONPATH=src python -m pytest tests/forge/cli/test_status.py -x -v -k in_flight` — update to
  `tests/forge/test_cli_status.py`.
- [ ] **AC-6 — PEB-012 prose / AC text aligned.** Search PEB-012's
  body for any other occurrence of `tests/forge/cli/test_status` and
  correct or remove. Verify with
  `grep -n "tests/forge/cli/test_status" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-012-*.md`
  → expected: 0 hits.
- [ ] **AC-7 — Both task files parse as valid YAML in their
  frontmatter** post-edit. Verify with
  `python3 -c "import yaml; yaml.safe_load(open('PATH').read().split('---')[1])"`.
- [ ] **AC-8 — Cross-task audit clean.** A repeat of the audit run on
  2026-05-07 (see review report) reports zero `[Modify]` violations
  for all 14 FRR-PEB tasks (Create-axis violations are expected for
  not-yet-started tasks). The audit script is in the description
  below for reproducibility.

## Out of Scope

- Modifying any FRR-PEB task other than -006 and -012.
- Touching `## Files to Create` lists in PEB-008, -009, -013 — those
  files don't exist on disk yet but that is correct (Player will
  create them).
- Re-running autobuild. That is a separate operator step after this
  PR lands.
- Touching the worktree at `.guardkit/worktrees/FEAT-PEBR/` — preserve
  as-is.
- Adding any code or test files. Pure task-frontmatter / task-body
  edits.

## Files to Modify

- `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-pause-resume-canonicalisation.md`
- `tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-012-forge-status-in-flight-surface.md`

## Implementation notes

`implementation_mode: direct` — no Player/Coach loop needed.

Suggested approach (per file):

1. **PEB-006**: Three `Edit` operations:
   - Replace the two typo paths in `## Files to Modify` (lines 157-158).
   - Replace the same typo paths anywhere else in the body (likely
     the `## Implementation notes` block at lines 162-165).
   - Set `status: blocked` → `status: backlog` and remove the
     `autobuild_state:` block from frontmatter (lines 30-93 or
     wherever the block starts/ends).
2. **PEB-012**: Two `Edit` operations:
   - Replace `tests/forge/cli/test_status.py` in `## Files to Modify`
     (line 82).
   - Replace `tests/forge/cli/test_status.py` in the `## Coach
     validation commands` section (line 95).

After all edits, run the audit script below to confirm zero residual
typos in the FRR-PEB family.

### Audit script (reproducibility)

```python
import re, pathlib

repo = pathlib.Path('.')
task_dir = repo / 'tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge'
section_re = re.compile(r'^## Files to (Create|Modify)\s*\n(.*?)(?=\n##|\Z)', re.DOTALL | re.MULTILINE)

for task_file in sorted(task_dir.glob('TASK-FRR-PEB-0*.md')):
    body = task_file.read_text().split('---', 2)[2]
    for m in section_re.finditer(body):
        kind, section = m.group(1), m.group(2)
        for line in section.splitlines():
            line = line.strip()
            if not line.startswith(('-', '*')):
                continue
            bm = re.match(r'^[-*]\s*`([^`]+)`', line) or re.match(r'^[-*]\s*(\S+)', line)
            if not bm:
                continue
            path = bm.group(1)
            if not path.endswith(('.py', '.toml', '.yaml', '.yml', '.json', '.jsonl')):
                continue
            target = repo / path
            wt = repo / '.guardkit/worktrees/FEAT-PEBR' / path
            if kind == 'Modify' and not (target.exists() or wt.exists()):
                print(f'{task_file.name}: [{kind}] missing → {path}')
```

Expected post-fix: zero output. (Create-axis violations are filtered.)

## Test requirements

No automated tests. Manual verification per ACs.

## Coach validation commands

```bash
# Verify typos are gone
grep -nc "src/forge/cli/_approval_subscriber\|tests/forge/test_approval_subscriber\|tests/forge/cli/test_status" \
  tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-{006,012}-*.md
# Expected: 0 across both files

# Verify autobuild_state scrubbed in PEB-006
grep -nc "autobuild_state:" \
  tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-*.md
# Expected: 0 (or 1 if you set "autobuild_state: {}")

# Verify status reset
grep -n "^status:" tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-*.md
# Expected: status: backlog

# YAML validity
python3 -c "
import yaml
for p in [
    'tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-006-pause-resume-canonicalisation.md',
    'tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/TASK-FRR-PEB-012-forge-status-in-flight-surface.md',
]:
    yaml.safe_load(open(p).read().split('---')[1])
    print(f'{p}: yaml ok')
"
```
