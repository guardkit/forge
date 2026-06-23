# RUNBOOK: Pre-fix harness capture for the proposer-eval corpus

**For:** a Claude Code session running in the `guardkit` tree (it needs `git`).
**Goal:** give each corpus item its **pre-fix** harness so the candidate sees a real bug and Phase 3 patches a pre-fix checkout. Produces, per item: `harness-context.md` (pre-fix source — candidate INPUT), `gold-fix.patch` (the real committed fix diff — canonical gold), `base_commit.txt` (the pre-fix ref).

**Why this exists.** The weekend AutoBuild fixes are already merged into `guardkit`. If `harness-context.md` or the Phase 3 checkout use current source, the candidate is handed fixed code (nothing to diagnose) and its `fix_patch` won't apply. So each item is pinned to `base_commit = <fix-commit>^`.

**Never touch** `trace.log` or `GOLD.md` — human-authored. The script only writes the three generated files.

---

## Phase 0 — Pre-flight

```bash
cd ~/Projects/appmilla_github/guardkit
git rev-parse --is-inside-work-tree && git log --oneline -3
ls ~/Projects/appmilla_github/forge/docs/research/proposer-eval/corpus
```
**Pass:** guardkit is a git work tree; the five item dirs exist (`ff-01-*`, `ff-03-*`, `ff-05-*`, `ff-07-*`, `fs-01-*`).

## Phase 1 — Run the capture

```bash
bash ~/Projects/appmilla_github/forge/docs/research/proposer-eval/capture-prefix-harness.sh
```
For each item it prints the resolved `fix=<sha> base=<sha>` (or `STILL-LIVE` for FS-01). Read the output.

**Expected per item:**
| Item | mode | token | expectation |
|------|------|-------|-------------|
| ff-01-bootstrap-venv-py310 | fix | `_uv_python_request` | finds the TASK-AB-BOOTPY01 commit → base SHA |
| ff-03-plan-audit-ac-path-misparse | fix (AUTO) | `_scan_ac_for_missing_paths` | finds the commit **and the file** (was not in `ac_linter.py`; likely `validation/ac_validator.py`, `quality_gates/coach_evidence.py`, or `criteria_classifier.py`) |
| ff-05-bdd-gate-exit4-conftest | add | `conftest_bridge` | finds the ADD of `templates/conftest_bridge.py` → base SHA; `bdd_runner.py` shown at base |
| ff-07-stale-coach-venv-middep | fix | `changed_dependency_manifests` | finds the TASK-AB-COACHVENV01 commit → base SHA |
| fs-01-coach-false-approval-partial-run | live | `smoke_gate_wave_coverage` | **likely no fix** → `base_commit.txt = WORKING_TREE`, gold-fix.patch = TODO |

**If a token misses** (`NO commit found`): the message prints the `git log -S` to run by hand. Find the real token/file, update the `ITEMS` line in the script, re-run. (FF-03's symbol moved out of `ac_linter.py`, so AUTO-discovery is expected to do the work there.)

## Phase 2 — Trim harness-context to the symbol

The script writes the **whole** pre-fix file(s). Trim each `harness-context.md` to the symbol(s) named in its AUTOGEN header (kept exact — don't paraphrase), plus enough surrounding context to stand alone. `environment_bootstrap.py` especially is ~1k lines; keep only `_ensure_worktree_venv` / `_uv_python_request` / `get_requires_python` (FF-01) or the turn-loop region (FF-07).

**Pass:** each `harness-context.md` is the focused pre-fix code for that bug.

## Phase 3 — Leakage + sanity check (do not skip)

```bash
cd ~/Projects/appmilla_github/forge/docs/research/proposer-eval/corpus
for d in ff-* fs-*; do echo "== $d =="; cat "$d/base_commit.txt"; done
```
- **No gold in INPUT:** confirm no `harness-context.md` contains the fix (the `--python` pin for FF-01, `changed_dependency_manifests` for FF-07, the conftest bridge body for FF-05, etc.). The fix belongs only in `GOLD.md` / `gold-fix.patch`.
- **base_commit sane:** ff-01/03/05/07 hold a 40-char SHA; fs-01 holds `WORKING_TREE` (unless a smoke-gate-strengthening commit was found — then a SHA, and remove the TODO from its `gold-fix.patch`).
- **gold-fix.patch non-empty** for the four fixed items; FS-01's is the TODO marker.

**Pass:** the eval can now be run — Phase 3 of `RUNBOOK-proposer-eval.md` checks out `base_commit` of the **guardkit** repo per item before applying each candidate's `fix_patch`.

---

## Notes
- FF-05's bug is partly an **absence** (no `features/conftest.py`); its `harness-context` is `bdd_runner.py`'s exit-code handling at base, and the AUTOGEN header flags that the bridge did not exist at base.
- FS-01 is the governance item. If its gold is genuinely unimplemented, leaving `base_commit = WORKING_TREE` is correct — the candidate is asked to author the strengthening that does not yet exist, and its `verify.sh` asserts the strengthened gate goes red on the pre-fix regression.
- Re-running the script is safe; it only regenerates the three files.
