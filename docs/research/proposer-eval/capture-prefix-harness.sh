#!/usr/bin/env bash
# =============================================================================
# capture-prefix-harness.sh
#
# Populate each proposer-eval corpus item with its PRE-FIX harness state from
# git, so the candidate sees a real bug (not the already-merged weekend fix)
# and Phase 3 patches a pre-fix checkout.
#
# WHY: the weekend AutoBuild fixes (FF-01 interpreter pin, FF-07 intra-wave
# refresh, FF-05 conftest bridge, FF-03 AC-path scanner) are already merged
# into guardkit. Feeding the candidate current source = showing fixed code,
# and Phase 3's `fix_patch` would not apply. Each item therefore needs a
# `base_commit` = the parent of its fix commit; harness-context and the Phase 3
# checkout both derive from it.
#
# Per item this writes THREE files and NOTHING else (GOLD.md, trace.log are
# human-authored and never touched):
#   harness-context.md  <- exact pre-fix source: `git show <base>:<file>`        (candidate INPUT)
#   gold-fix.patch      <- the real committed diff: `git show <fix> -- <file>`   (canonical gold)
#   base_commit.txt     <- <base> (= <fix>^)  |  WORKING_TREE  if no fix exists yet
#
# RUN: from a Claude Code session, anywhere. Requires git + the guardkit repo.
# Idempotent — re-running overwrites the three generated files per item.
#
#   bash capture-prefix-harness.sh            # all items
#   GUARDKIT=/path/to/guardkit bash capture-prefix-harness.sh
#
# AFTER RUNNING (manual/agentic follow-up — see CAPTURE-PREFIX.md):
#   1. TRIM each harness-context.md to the symbol(s) named in TRIM[] below.
#   2. CONFIRM no gold/fix code leaked into harness-context (it lives in GOLD.md / gold-fix.patch).
#   3. SANITY-CHECK base_commit.txt; FS-01 should read WORKING_TREE if its gold is unimplemented.
# =============================================================================
set -uo pipefail

GUARDKIT="${GUARDKIT:-$HOME/Projects/appmilla_github/guardkit}"
CORPUS="${CORPUS:-$HOME/Projects/appmilla_github/forge/docs/research/proposer-eval/corpus}"

git -C "$GUARDKIT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "FATAL: not a git work tree: $GUARDKIT (set GUARDKIT=...)"; exit 1; }
[ -d "$CORPUS" ] || { echo "FATAL: corpus dir missing: $CORPUS (set CORPUS=...)"; exit 1; }

# --- per-item config ---------------------------------------------------------
# slug | mode | token | files
#   mode=fix : a fix commit exists; locate via `git log -S <token> -- <files>`
#              (if <files>=AUTO, search repo-wide and derive files from the commit)
#   mode=add : the fix ADDED a file (the "bug" was an absence); locate the first
#              file's add commit via --diff-filter=A
#   mode=live: expected to have NO fix yet (still-live gap). base=WORKING_TREE,
#              harness-context = current source. If a fix IS found, it is captured as fix.
ITEMS="
ff-01-bootstrap-venv-py310|fix|_uv_python_request|guardkit/orchestrator/environment_bootstrap.py
ff-03-plan-audit-ac-path-misparse|fix|_scan_ac_for_missing_paths|AUTO
ff-05-bdd-gate-exit4-conftest|add|conftest_bridge|guardkit/templates/conftest_bridge.py guardkit/orchestrator/quality_gates/bdd_runner.py
ff-07-stale-coach-venv-middep|fix|changed_dependency_manifests|guardkit/orchestrator/environment_bootstrap.py
fs-01-coach-false-approval-partial-run|live|smoke_gate_wave_coverage|guardkit/orchestrator/smoke_gates.py guardkit/orchestrator/quality_gates/task_work_interface.py guardkit/orchestrator/quality_gates/stack_test_execution.py
"

# Symbol(s) to TRIM harness-context.md down to after capture (the script writes
# whole files; trimming is a human/agent follow-up so it stays exact).
declare -A TRIM=(
  [ff-01-bootstrap-venv-py310]="_ensure_worktree_venv + _uv_python_request + DetectedManifest.get_requires_python"
  [ff-03-plan-audit-ac-path-misparse]="_scan_ac_for_missing_paths"
  [ff-05-bdd-gate-exit4-conftest]="BDD gate exit-code handling in bdd_runner.py; NOTE conftest_bridge.py is ABSENT at base"
  [ff-07-stale-coach-venv-middep]="the orchestrator turn-loop region around the Coach test (pre-refresh) + bootstrap lifecycle"
  [fs-01-coach-false-approval-partial-run]="smoke-gate wave scheduling + per-task test scope"
)

emit_header () {  # $1=slug  $2=ref-description
  echo "# INPUT — harness-context.md · $1"
  echo "# AUTOGEN by capture-prefix-harness.sh — $2"
  echo "# Candidate INPUT: contains the bug, NO gold. TRIM to: ${TRIM[$1]}  (then delete this header)."
}

dump_files_at () {  # $1=ref  $2...=files  -> stdout fenced python blocks
  local ref="$1"; shift
  for f in "$@"; do
    echo; echo "## $f @ $ref"; echo '```python'
    git -C "$GUARDKIT" show "$ref:$f" 2>/dev/null || echo "(did not exist at $ref)"
    echo '```'
  done
}

printf '%s\n' "$ITEMS" | while IFS='|' read -r slug mode token files; do
  [ -z "${slug// }" ] && continue
  dir="$CORPUS/$slug"
  [ -d "$dir" ] || { echo "SKIP $slug — corpus dir missing"; continue; }
  echo "=== $slug ($mode) ==="

  # ---- live mode: capture current source unless a fix turns up -------------
  if [ "$mode" = "live" ]; then
    C=$(git -C "$GUARDKIT" log -S "$token" -1 --format=%H -- $files 2>/dev/null || true)
    if [ -z "$C" ]; then
      echo "  no fix commit for token '$token' — STILL-LIVE; harness-context = current source"
      echo "WORKING_TREE" > "$dir/base_commit.txt"
      { emit_header "$slug" "live (no fix yet): current source == pre-fix"
        dump_files_at "HEAD" $files
      } > "$dir/harness-context.md"
      echo "TODO: gold-fix not yet authored. FS-01 gold = the smoke-gate strengthening in GOLD.md (widen smoke to later waves; full-suite-in-worktree before complete; broaden per-task scope) — still to implement." > "$dir/gold-fix.patch"
      continue
    fi
    echo "  found a fix commit $C for a 'live' item — capturing as fix"
    mode=fix
  fi

  # ---- locate the fix commit ----------------------------------------------
  if [ "$mode" = "add" ]; then
    first=$(echo "$files" | awk '{print $1}')
    C=$(git -C "$GUARDKIT" log --diff-filter=A -1 --format=%H -- "$first" 2>/dev/null || true)
  else
    C=$(git -C "$GUARDKIT" log -S "$token" -1 --format=%H -- $files 2>/dev/null || true)
    [ -z "$C" ] && C=$(git -C "$GUARDKIT" log -S "$token" -1 --format=%H 2>/dev/null || true)
  fi
  if [ -z "$C" ]; then
    echo "  !! NO commit found for token '$token' (mode=$mode). Investigate by hand:"
    echo "     git -C $GUARDKIT log -S '$token' -- $files"
    continue
  fi
  base="$C^"

  # resolve AUTO files from the commit
  if [ "$files" = "AUTO" ]; then
    files=$(git -C "$GUARDKIT" show --name-only --format= "$C" | grep -E '\.py$' | tr '\n' ' ')
    echo "  AUTO files: $files"
  fi

  echo "$base" > "$dir/base_commit.txt"
  echo "  fix=$C  base=$base"

  # gold-fix.patch = real committed diff (scoped to the files of interest)
  git -C "$GUARDKIT" show "$C" -- $files > "$dir/gold-fix.patch"

  # harness-context = pre-fix source at base
  { emit_header "$slug" "pre-fix @ $base (parent of fix $C)"
    dump_files_at "$base" $files
  } > "$dir/harness-context.md"
done

echo
echo "DONE."
echo "Generated per item: harness-context.md (pre-fix source), gold-fix.patch (real diff), base_commit.txt."
echo "Follow-up (see CAPTURE-PREFIX.md): TRIM harness-context to the named symbol(s); confirm no gold leaked; sanity-check base_commit.txt."
