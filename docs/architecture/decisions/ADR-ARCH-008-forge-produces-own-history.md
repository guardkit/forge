# ADR-ARCH-008: Forge produces its own history files in Pattern-2/Pattern-3 format

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 2 Revision 3

## Context

`fleet-master-index` formalises three patterns already used organically across specialist-agent, nats-core, and nats-infrastructure:

- Pattern 1: Build Plans (addressed in ADR-ARCH-007)
- Pattern 2: `command_history.md` — repo-level log of every GuardKit command run
- Pattern 3: `feature-spec-{NAME}-history.md` — per-feature Q&A session log

These are evidence, enable reproducibility, and **are the training data for future automation**. If Forge invokes GuardKit commands on Rich's behalf, those invocations should land in history files so the *next* project's Forge can learn from them.

## Decision

Forge writes history files at the repo it's operating on, mirroring the canonical patterns:

- **`command_history.md`** — appended to at every GuardKit command invocation with timestamp, command, args, exit code, summary of output. Forge is the writer instead of Rich, but the file format and location match Pattern 2.
- **`feature-spec-{NAME}-history.md`** / **`feature-plan-{NAME}-history.md`** / **`system-arch-history.md`** etc. — one per session, capturing the `--context` flags passed, the proposed defaults, the responses Forge gave (acting as the human proxy with calibration priors informing each response). Pattern 3.
- **`buildplan.md`** — produced by `build_plan_composer` per ADR-ARCH-007.

Forge attributes each decision in these files to its source: `[auto-approve: prior from similar build FEAT-XXX]`, `[flag-for-review: no priors, deferred to Rich]`, `[override: Rich rejected auto-approve]`. This trail is itself ingestable as CalibrationEvents (ADR-ARCH-006).

## Consequences

- **+** Pattern consistency across the fleet — human-authored and Forge-authored history files interchangeable as evidence.
- **+** The factory teaches itself: Forge's outputs become future Forge's priors.
- **+** Rich can audit Forge's decisions by reading the same markdown files he used to write himself.
- **+** Reproducibility: any build can be re-derived by reading the history file's command sequence + responses.
- **−** Requires write access to target repos — permissions allowlist (ADR-ARCH-023) must include each repo's root.
- **−** Increases per-build disk I/O and git diff size (history files commit alongside code); mitigated by placing in `docs/sessions/` or similar convention TBD in `/system-design`.
