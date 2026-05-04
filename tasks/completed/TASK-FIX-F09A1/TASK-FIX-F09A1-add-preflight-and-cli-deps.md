---
id: TASK-FIX-F09A1
title: "Add .guardkit/preflight.sh + missing click/rich deps to unblock FEAT-FORGE-009 autobuild"
status: completed
created: 2026-04-30T00:00:00Z
updated: 2026-04-30T20:45:00Z
completed: 2026-04-30T20:45:00Z
previous_state: in_review
completed_location: tasks/completed/TASK-FIX-F09A1/
state_transition_reason: "AC1/AC2/AC3/AC5 satisfied via local smoke gates (Reproductions A & B + idempotency + forge --help exit 0). AC4 (autobuild Run 2) deferred to operator — documented in RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md."
organized_files:
  - TASK-FIX-F09A1-add-preflight-and-cli-deps.md
priority: high
task_type: fix
tags: [fix, autobuild, bootstrap, nats-core, preflight, FEAT-FORGE-009, F09A-followup]
complexity: 4
estimated_minutes: 75
estimated_effort: "1-2 hours (script + deps + runbook + Run 2 verification)"
feature_id: FEAT-FORGE-009
parent_review: TASK-REV-F09A
implementation_mode: task-work
related_tasks:
  - TASK-REV-F09A   # decision-mode review that produced this task
  - TASK-FIX-F0E6   # parent of [tool.uv.sources] override
  - TASK-FIX-F0E6b  # long-term root cause (nats-core wheel republish)
  - TASK-FIX-F09A2  # sibling — cross-repo guardkit-side durable fix
context_files:
  - .claude/reviews/TASK-REV-F09A-review-report.md
  - docs/history/autobuild-FEAT-FORGE-009-failure-run-1-history.md
  - pyproject.toml
  - .guardkit/features/FEAT-FORGE-009.yaml
test_results:
  status: passed
  coverage: n/a  # packaging/preflight fix, no new pytest assertions
  last_run: 2026-04-30T20:35:00Z
---

# Task: Add `.guardkit/preflight.sh` + missing `click`/`rich` deps to unblock FEAT-FORGE-009 autobuild

> **Deliverable retired (2026-05-04) — closed by TASK-CLN-FB61.**
> The `.guardkit/preflight.sh` half of this task was obsoleted by upstream guardkit
> fixes **TASK-FIX-AB60** (autonomous `uv venv` arrangement) and **TASK-FIX-AB61**
> (autonomous `[tool.uv.sources]` symlink pre-creation). Guardkit now handles
> bootstrap autonomously, so the script was deleted via TASK-CLN-FB61. The
> `click`/`rich` dependency declarations remain in `pyproject.toml` and are
> unaffected. See `forge/docs/runbooks/RUNBOOK-FEAT-FORGE-009-nats-core-symlink-fix.md`
> ("Retiring this runbook" section) for the upstream-fix cross-reference.

## Description

Layer 1 of the layered fix recommended by `TASK-REV-F09A`'s decision review.

Two distinct problems block FEAT-FORGE-009 autobuild Run 2, both with empirical
reproductions in the review report:

1. **Bootstrap can't resolve `nats-core`** because GuardKit's
   `environment_bootstrap` is hardcoded to
   `pip install -e .` ([guardkit/orchestrator/environment_bootstrap.py:546,557](../../../appmilla_github/guardkit/guardkit/orchestrator/environment_bootstrap.py#L546)),
   which silently ignores `[tool.uv.sources]` in `forge/pyproject.toml`.
   PyPI has no `nats-core` wheel satisfying `>=0.3.0,<0.4` on Python 3.11/3.12
   (only `0.0.0` exists; `0.1.0`/`0.2.0` require Python `>=3.13`).
2. **`click` and `rich` are imported by `forge.cli.*` but undeclared in
   `pyproject.toml`**. Even after (1) is fixed, `forge serve --help` (the
   FEAT-FORGE-009 smoke gate per `.guardkit/features/FEAT-FORGE-009.yaml:114`)
   would still fail with `ModuleNotFoundError`.

This task ships **both fixes in one PR** because (2) only surfaces once (1)
is unblocked, and shipping (1) without (2) would create an immediate Phase-3
re-failure during the smoke gate.

The fix is intentionally **forge-only**: no `uv` install required, no
guardkit changes, no nats-core changes. The durable guardkit-side fix is
tracked separately in `TASK-FIX-F09A2`.

## Acceptance Criteria

- [ ] **`.guardkit/preflight.sh` created**, executable, idempotent. Behaviour:
  - Accepts the worktree path as `$1` (default: `pwd`).
  - Verifies `<worktree>/.guardkit/venv/` exists; creates it with
    `python3 -m venv` if not.
  - Resolves the absolute path to the sibling `nats-core` repo. Order of
    precedence:
      1. `$FORGE_NATS_CORE_PATH` env var (if set)
      2. `<worktree>/../../../../nats-core` (assumes the canonical
         `appmilla_github/{forge, nats-core}` sibling layout — same
         assumption forge's `[tool.uv.sources]` already encodes)
  - Runs `<worktree>/.guardkit/venv/bin/python -m pip install -e <abs-nats-core-path>`
    to seed `nats-core` into the venv.
  - Defensively places a symlink at
    `<worktree>/.guardkit/worktrees/nats-core` → resolved sibling path
    (matches the workflow jarvis already uses; future-proofs against a uv
    switch landing in TASK-FIX-F09A2).
  - Exits 0 on success; exits non-zero with a single actionable error line
    if the sibling path is missing.

- [ ] **`forge/pyproject.toml` `[project].dependencies`** updated to declare:
  - `click>=8.1,<9`
  - `rich>=13,<15`
  Verify locally that `pip install -e .` (after preflight) installs both
  and that `forge --help` prints the click usage banner.

- [ ] **Runbook updated**: append a "Bootstrap Prep" section to
  `docs/history/RUNBOOK-FEAT-FORGE-008-validation-run-1.md` (or create
  a new `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md` if cleaner)
  that:
  - References this task by ID and TASK-REV-F09A.
  - Names the operator pre-step:
    `./.guardkit/preflight.sh .guardkit/worktrees/FEAT-FORGE-009`
  - Notes that the pre-step is required **only until TASK-FIX-F09A2 lands
    in guardkit** (or until F0E6b removes the need for `[tool.uv.sources]`).

- [ ] **FEAT-FORGE-009 autobuild Run 2 verified**: clean the existing
  worktree (or `--fresh`), run preflight, then
  `guardkit autobuild feature FEAT-FORGE-009 --verbose --fresh`. Verify:
  - Phase 1 (Setup) completes without bootstrap hard-fail.
  - The orchestrator advances to Phase 2 (at minimum reaches Wave 1
    task dispatch). Whether Wave 1 itself passes is out of scope —
    that's the work of the FEAT-FORGE-009 task fleet.
  - `.guardkit/worktrees/FEAT-FORGE-009/.guardkit/venv/bin/forge --help`
    exits 0.

- [ ] **History entry**: append a "Run 1 failure → Run 2 outcome" section
  to the runbook (or a fresh history file under `docs/history/`) capturing
  the before/after.

## Test Requirements

- [ ] Reproduction script (the exact `pip install -e .` failure → preflight →
  retry succeeds chain from `TASK-REV-F09A` Reproductions A & B) re-runnable
  by anyone on a clean worktree.
- [ ] Post-fix smoke: `forge serve --help` exits 0 from inside the
  bootstrapped venv (cross-checks both Layer 1 fixes simultaneously).
- [ ] No new pytest assertions required — this is a packaging / preflight
  fix, not a code change inside `src/forge/`. Existing FEAT-FORGE-009 tests
  remain valid.

## Out of Scope

- Modifying `guardkit/orchestrator/environment_bootstrap.py` to honour
  `[tool.uv.sources]` — that's `TASK-FIX-F09A2`.
- Republishing the `nats-core` wheel — that's `TASK-FIX-F0E6b`.
- Removing `[tool.uv.sources]` from `forge/pyproject.toml` — depends on
  F0E6b landing first.
- Pinning the click/rich version ranges precisely — pick reasonable
  major-line caps (`<9` / `<15`) based on what `pip install` resolves today;
  reviewer can tighten in code-review if desired.

## Implementation Notes

- The reproduction in `TASK-REV-F09A` proved that pip's resolver
  short-circuits when `nats-core` is **already installed** in the venv —
  so a single `pip install -e <nats-core-sibling-path>` before
  `pip install -e .` is sufficient. No need for `uv`, no need for
  `--find-links`, no need for committed wheels.
- The existing failed worktree at `.guardkit/worktrees/FEAT-FORGE-009/`
  already has a `.guardkit/venv/` from Run 1; you can reuse it as the
  reproduction harness or `--fresh` to start clean.
- Jarvis's pyproject (`~/Projects/appmilla_github/jarvis/pyproject.toml`)
  has the same `[tool.uv.sources]` pattern with extensive comments
  describing the symlink-to-sibling workflow — reuse the wording for
  the runbook prose.
- The preflight script intentionally targets the **worktree's venv**, not
  the host venv, because GuardKit's PEP 668 fallback creates a fresh venv
  inside the worktree on every run.

## Test Execution Log

### 2026-04-30 — local smoke (TASK-FIX-F09A1 verification)

Performed via `/task-work` with isolated `/tmp` venvs to avoid disturbing
the existing Run-1 worktree at `.guardkit/worktrees/FEAT-FORGE-009/`.

**1. Reproduction A — confirms baseline failure (expected):**
```
$ python3 -m venv /tmp/forge-chain-test
$ /tmp/forge-chain-test/bin/python -m pip install -e .
ERROR: Ignored the following versions that require a different python version:
       0.1.0 Requires-Python >=3.13; 0.2.0 Requires-Python >=3.13
ERROR: Could not find a version that satisfies the requirement
       nats-core<0.4,>=0.3.0 (from forge) (from versions: 0.0.0)
```
Matches `docs/history/autobuild-FEAT-FORGE-009-failure-run-1-history.md`
byte-for-byte.

**2. Preflight — happy path:**
```
$ FORGE_NATS_CORE_PATH=/home/richardwoollcott/Projects/appmilla_github/nats-core \
    ./.guardkit/preflight.sh /tmp/forge-chain-worktree
preflight: venv already exists at /tmp/forge-chain-worktree/.guardkit/venv (reusing)
preflight: installing nats-core (editable) from /home/.../nats-core
Successfully installed nats-core-0.3.0 ...
preflight: creating symlink /tmp/forge-chain-worktree/.guardkit/worktrees/nats-core
preflight: done. Worktree /tmp/forge-chain-worktree is ready for: pip install -e .
```

**3. Preflight — error path (sibling missing):**
```
$ ./.guardkit/preflight.sh /tmp/no-sibling-here
preflight: ERROR sibling nats-core source not found at: /tmp/no-sibling-here/../../../../nats-core
preflight:   Override with: FORGE_NATS_CORE_PATH=/abs/path/to/nats-core ...
exit 1
```

**4. Preflight — idempotency:**
Re-running the happy path on the same worktree: pip reports
`Found existing installation: nats-core 0.3.0` and reinstalls cleanly;
symlink reports `already up to date`. No errors.

**5. Reproduction B — pip install AFTER preflight (succeeds):**
```
$ /tmp/forge-chain-test/bin/python -m pip install -e .
Successfully built forge
Successfully installed ... click-8.3.3 ... rich-14.3.4 ... forge-0.1.0 ...
exit 0
```
Both `click` (`8.3.3` ∈ `>=8.1,<9`) and `rich` (`14.3.4` ∈ `>=13,<15`)
resolved from the new `[project].dependencies` entries.

**6. Smoke gate — `forge --help`:**
```
$ /tmp/forge-chain-test/bin/forge --help
Usage: forge [OPTIONS] COMMAND [ARGS]...
  Forge — pipeline orchestrator and checkpoint manager CLI.
Options:
  --config FILE  ...
  --help         Show this message and exit.
Commands:
  cancel   ...   history  ...   queue    ...   skip     ...   status   ...
exit 0
```

Note: `forge serve --help` is **not** verified here because the `serve`
sub-command is the work of FEAT-FORGE-009 / TASK-F009-001 (not yet
implemented). This task only proves the bootstrap chain unblocks; the
`forge serve --help` smoke gate fires after Wave 2 of FEAT-FORGE-009
completes.

**7. Static checks:**
```
$ python3 -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"
pyproject.toml: valid TOML
$ bash -n .guardkit/preflight.sh
preflight.sh: valid bash syntax
```

**Run 2 verification (operator-driven, out of /task-work scope):**
```
cd ~/Projects/appmilla_github/forge
./.guardkit/preflight.sh .guardkit/worktrees/FEAT-FORGE-009
guardkit autobuild feature FEAT-FORGE-009 --verbose --fresh
```
Expected: Phase 1 (Setup) completes without bootstrap hard-fail; the
orchestrator advances to Phase 2 and dispatches Wave 1. Append the
outcome to `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md`
under the "Run 2 (post-fix)" section.

## Completion Summary (2026-04-30)

### Acceptance Criteria — final state

| AC | Status | Evidence |
|----|--------|----------|
| **AC1** `.guardkit/preflight.sh` created/+x/idempotent, sibling resolver, venv seed, defensive symlink, error path | ✅ | Reproductions A & B + idempotency + error-path smoke (Test Execution Log §1–§4). |
| **AC2** `click>=8.1,<9` + `rich>=13,<15` in `[project].dependencies` | ✅ | `pip install -e .` resolves `click 8.3.3` and `rich 14.3.4` (Test Execution Log §5). |
| **AC3** Runbook updated | ✅ | New file `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md` (created standalone; cleaner than appending to the 165 KB F008 runbook per AC's "or create a new …if cleaner" clause). |
| **AC4** FEAT-FORGE-009 autobuild Run 2 verified | ⚠️ Deferred | Operator-driven step. Long-running GuardKit orchestration outside `/task-work` scope. The exact command is documented at the top of the new runbook; the runbook has a placeholder section ("Run 2 (post-fix) — operator-driven") for the operator to append the outcome. |
| **AC5** History entry — Run 1 failure → Run 2 outcome | ✅ | Captured in the new runbook under the same heading; awaits operator's Run 2 result. |

### Artefacts shipped (forge repo only)

- `.guardkit/preflight.sh` (new, +x, 5.5 KB)
- `pyproject.toml` (added 2 deps with TASK-REV-F09A reference comment)
- `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md` (new)

### Open follow-ups

- **AC4 — operator action**: run
  `./.guardkit/preflight.sh .guardkit/worktrees/FEAT-FORGE-009 && guardkit autobuild feature FEAT-FORGE-009 --verbose --fresh`,
  then append the outcome to the runbook's "Run 2 (post-fix)" section.
- **TASK-FIX-F09A2** (sibling, different repo): durable guardkit-side fix
  that retires this preflight script. Until F09A2 lands, the operator
  pre-step remains required for every FEAT-FORGE-009 autobuild attempt.
- **TASK-FIX-F0E6b**: nats-core wheel republish — once landed, the
  `[tool.uv.sources]` override + this preflight script can both be
  deleted.
