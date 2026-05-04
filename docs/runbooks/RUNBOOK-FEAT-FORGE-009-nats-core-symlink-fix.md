# Runbook: FEAT-FORGE-009 — `nats-core` Symlink Fix for AutoBuild Bootstrap

**Status:** **Retired (2026-05-04)** by upstream guardkit fixes **TASK-FIX-AB60** (autonomous `uv venv` arrangement) and **TASK-FIX-AB61** (autonomous `[tool.uv.sources]` symlink pre-creation). Guardkit now creates the bridging symlink autonomously when copying `pyproject.toml` into a worktree — no per-machine setup required. Runbook kept for historical context; the manual `ln -s` step described below is no longer needed.

**Supersedes:** `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md` (TASK-FIX-F09A1 / `.guardkit/preflight.sh`). The preflight-script approach was incomplete on macOS — see *Why preflight.sh was insufficient* below. The preflight script itself was retired by **TASK-CLN-FB61** at the same time as this runbook.

**Applies to:** MacBook (verified 2026-05-01), GB10 (`promaxgb10-41b1`), and any future workstation cloning forge in the canonical sibling layout.

---

## TL;DR — One command per machine, one time

```bash
cd /path/to/appmilla_github/forge
ln -s ../../../nats-core .guardkit/worktrees/nats-core
ls -la .guardkit/worktrees/nats-core/pyproject.toml   # must resolve, not error
```

After this symlink exists, `guardkit autobuild feature FEAT-FORGE-XXX --verbose --fresh` bootstraps cleanly. No preflight script, no per-run dance.

---

## The problem

Forge's `pyproject.toml` declares:

```toml
[tool.uv.sources]
nats-core = { path = "../nats-core", editable = true }
```

This override exists because the `nats-core` wheel currently published to PyPI is malformed — the dist-info is named `nats_core-0.x.x.dist-info` but the `nats_core/` namespace forge imports is **absent from the wheel**. See the `TASK-FIX-F0E6` comment block in `pyproject.toml` for the full forensic. Until **TASK-FIX-F0E6b** republishes a working wheel, forge resolves `nats-core` from the sibling working tree at `appmilla_github/nats-core/`.

The override works fine for normal development:
- `pyproject.toml` lives at `forge/pyproject.toml`
- uv resolves `../nats-core` relative to that file → `appmilla_github/nats-core/` ✓

It breaks under `guardkit autobuild`:
- GuardKit creates a worktree at `forge/.guardkit/worktrees/FEAT-FORGE-009/`
- `pyproject.toml` is copied to `forge/.guardkit/worktrees/FEAT-FORGE-009/pyproject.toml`
- uv resolves `../nats-core` relative to **that** file → `forge/.guardkit/worktrees/nats-core/` ✗
- That path doesn't exist → `error: Distribution not found at: file:///.../forge/.guardkit/worktrees/nats-core` → bootstrap hard-fail before any task runs.

This is forge-specific. Sibling repos (e.g. `study-tutor`) that don't carry a `[tool.uv.sources]` override autobuild with no setup at all.

---

## The solution

A single relative symlink at `.guardkit/worktrees/nats-core` that points uv at the real sibling repo:

```
forge/.guardkit/worktrees/nats-core → ../../../nats-core
```

The symlink lives **alongside** the per-feature worktree dirs (`FEAT-FORGE-002/`, `FEAT-FORGE-009/`, …), not inside any of them. It survives wiping individual worktrees with `git worktree remove` and is unaffected by `--fresh` autobuild runs.

### Why three `..`

Symlink targets are resolved relative to **the directory containing the symlink**, not the project root. The symlink lives at:

```
forge/.guardkit/worktrees/nats-core    ← symlink itself
forge/.guardkit/worktrees/             ← containing dir
```

To reach `appmilla_github/`:
- `..` → `forge/.guardkit/`
- `../..` → `forge/`
- `../../..` → `appmilla_github/` ✓

So `../../../nats-core` resolves to `appmilla_github/nats-core/`, the canonical sibling.

> **Trap:** an earlier attempt used `ln -s ../../nats-core …` which silently produced a dangling symlink (`forge/nats-core/`, which doesn't exist). `ls` on the symlink will succeed, but `ls -la <symlink>/pyproject.toml` will error with `No such file or directory`. **Always verify the target with the `ls` line in the TL;DR.**

---

## Application steps

### MacBook (one-time)

```bash
cd ~/Projects/appmilla_github/forge
ln -s ../../../nats-core .guardkit/worktrees/nats-core
ls -la .guardkit/worktrees/nats-core/pyproject.toml
# Expected: /Users/.../forge/.guardkit/worktrees/nats-core/pyproject.toml
```

Then run autobuild as normal:

```bash
guardkit autobuild feature FEAT-FORGE-009 --verbose --fresh
```

### GB10 (`promaxgb10-41b1`)

Same command. The path inside `appmilla_github/` is identical — the relative target works regardless of the absolute filesystem prefix:

```bash
cd ~/Projects/appmilla_github/forge   # or wherever forge is cloned on GB10
ln -s ../../../nats-core .guardkit/worktrees/nats-core
ls -la .guardkit/worktrees/nats-core/pyproject.toml
```

If `nats-core` is **not** a sibling of `forge` on the target machine, override the symlink target with an absolute path or a different relative path that reaches the actual `nats-core` clone.

### Verification

A correct setup looks like this:

```
$ ls -la .guardkit/worktrees/
drwxr-xr-x   FEAT-FORGE-002
drwxr-xr-x   FEAT-FORGE-009          # only if a worktree currently exists
lrwxr-xr-x   nats-core -> ../../../nats-core
```

`ls .guardkit/worktrees/nats-core/pyproject.toml` must print a path, not error.

---

## Recovery: stale worktree + branch from a previous failed run

If a prior `--fresh` autobuild attempt failed mid-bootstrap, it can leave behind:
- a registered git worktree at `.guardkit/worktrees/FEAT-FORGE-009/`
- an `autobuild/FEAT-FORGE-009` branch pointing at the same SHA as `main`

A subsequent `--fresh` will refuse to recreate them (`fatal: a branch named 'autobuild/...' already exists`) and guardkit's auto-cleanup will fail too (`cannot delete branch ... used by worktree`). The error message itself prints the recovery commands; for completeness:

```bash
git worktree remove .guardkit/worktrees/FEAT-FORGE-009 --force
git branch -D autobuild/FEAT-FORGE-009
```

These are safe in this context — both are guardkit-managed scratch with no user commits. Verify with `git worktree list` (only `main` should remain) and `git branch -a | grep autobuild` (no output).

---

## Why `preflight.sh` was insufficient

`TASK-FIX-F09A1` shipped `.guardkit/preflight.sh` as the original Layer-1 fix. It had two design choices that don't survive the way GuardKit actually invokes installs on macOS:

1. **Symlink at the wrong nesting level.** The script creates the symlink at `<worktree>/.guardkit/worktrees/nats-core` — *inside* the per-feature worktree, one level deeper than where uv looks. The runbook rationale (lines 67–72 of `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md`) correctly identifies that uv resolves `../nats-core` to `forge/.guardkit/worktrees/nats-core`, but the script then puts the symlink one level too deep. uv never consults it.
2. **Wrong venv seeded.** The script seeds `<worktree>/.guardkit/venv/` via plain `pip install -e <abs-path-to-nats-core>`. But GuardKit's `environment_bootstrap` runs `uv pip install -e .` against the **project-level** `forge/.venv` (visible in any failing log: `Using Python 3.x.x environment at: /Users/.../forge/.venv`). The seeded worktree-local venv is never consulted. Even if it were, uv honours `[tool.uv.sources]` and tries to resolve the source path regardless of whether the package is already installed.

The symlink-at-the-correct-level approach removes both failure modes by giving uv a path it can actually follow, with no venv-seeding required.

`preflight.sh` and the GB10-era validation that "passed" probably succeeded because GB10 was running guardkit through a code path that used plain `pip` (which ignores `[tool.uv.sources]` and short-circuits when the package is in the venv). On the Mac with a current guardkit using `uv pip install`, that codepath is dead.

---

## Retiring this runbook

This symlink can be removed when **either** of the following lands:

- **TASK-FIX-F0E6b** — republish a working `nats-core` wheel to PyPI. Once forge can drop `[tool.uv.sources]`, the path-resolution problem evaporates.
- ~~**TASK-FIX-F09A2** — guardkit-side fix that rewrites `[tool.uv.sources]` paths when copying `pyproject.toml` into a worktree, or that resolves them relative to the original repo root.~~ **Landed as TASK-FIX-AB61 (2026-05-04).** Guardkit now parses `[tool.uv.sources]` and pre-creates the bridging symlink autonomously, retiring this runbook's manual setup. Paired with **TASK-FIX-AB60** (autonomous `uv venv` arrangement on the no-venv stderr sentinel).

---

## See also

- `pyproject.toml` — the `TASK-FIX-F0E6` comment block at `[tool.uv.sources]` documents the original PyPI wheel breakage.
- `docs/history/RUNBOOK-FEAT-FORGE-009-bootstrap-prep.md` — superseded preflight-script runbook (kept for historical context).
- `tasks/backlog/TASK-FIX-F09A2-guardkit-uv-sources-detection.md` — durable cross-repo fix (Layer 2).
- `.claude/reviews/TASK-REV-F09A-review-report.md` — original layered-fix review.
