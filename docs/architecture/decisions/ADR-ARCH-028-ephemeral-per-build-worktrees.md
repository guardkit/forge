# ADR-ARCH-028: Ephemeral per-build working trees at `/var/forge/builds/{build_id}/`

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 6

## Context

Forge clones and mutates target repos during builds. Two bad options:
- **Shared working tree** per repo — concurrent or retry-from-scratch scenarios leave partial state between builds. ADR-SP-012 says sequential, but crash recovery retries from scratch (ADR-ARCH-009), so in-place mutation risks inconsistent starting state.
- **Per-build isolation but persistent** — consumes disk indefinitely; old trees clutter diagnosis.

ADR-ARCH-023 (permissions) already requires filesystem allowlists. The blast-radius story argues for isolation + cleanup.

## Decision

Each build operates in `/var/forge/builds/{build_id}/` — a dedicated directory created during PREPARING state and deleted on terminal state (COMPLETE | FAILED | CANCELLED | SKIPPED).

Lifecycle:
1. **PREPARING** → `mkdir -p /var/forge/builds/{build_id}/` → `git clone {repo} /var/forge/builds/{build_id}/` → `git checkout -b feature/{feature_id}`.
2. **RUNNING / PAUSED / FINALISING** → all git + GuardKit + test invocations scope to this directory.
3. **Terminal state** → logs and metadata already captured to SQLite + Graphiti → `rm -rf /var/forge/builds/{build_id}/`.

On crash recovery (INTERRUPTED): `rm -rf` any `/var/forge/builds/{interrupted_build_id}/` the crashed process left behind (SQLite knows the id) → fresh clone on retry.

Permissions (ADR-ARCH-023) include `/var/forge/builds/*` in both `allow_read` and `allow_write` — scoped, no parent-dir access.

## Consequences

- **+** Blast-radius is enumerable: Forge can only write under `/var/forge/builds/{build_id}/`, `~/.forge/*`, `/tmp/forge-*`.
- **+** Secrets accidentally written to a working tree during a build (by GuardKit, by an injected specialist output) don't leak into the next build's tree.
- **+** Failed-build diagnosis uses SQLite `stage_log` + Graphiti + logs — richer than rummaging in a half-mutated git tree.
- **+** Crash recovery's retry-from-scratch policy (ADR-SP-013) is clean — no stale state from the interrupted attempt.
- **−** Re-cloning for every build is network + time overhead. `git clone --depth 1 --filter=blob:none` keeps it cheap; for local-network repos on GB10 (if mirrored), it's seconds.
- **−** If Rich wants to inspect a failed build's working tree, it's gone. Mitigation: `forge history --feature FEAT-XXX` shows the full stage log; LangSmith trace (if enabled) shows the tool-call timeline; the PR branch on GitHub (if pushed before failure) preserves the code. If that's insufficient, a config flag `preserve_failed_worktrees: true` could be added V2.
