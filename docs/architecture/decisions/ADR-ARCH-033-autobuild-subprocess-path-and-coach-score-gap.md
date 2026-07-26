# ADR-ARCH-033: The autobuild subprocess path and the coach-score population gap

**Status:** Accepted (interim) — 2026-07-02. Ratifies the current two-path
state as a *consciously managed interim* with a convergence plan; it does **not**
bless the split as permanent architecture.
**Date:** 2026-07-02
**Deciders:** Rich + forge-only session (UBS state-map audit)
**Supersedes:** none
**Relates to:** ADR-ARCH-004 (full GuardKit CLI tool surface), ADR-ARCH-025
(tool error handling / never-raises), DDR-005 (context-flag resolution),
TASK-GCI-008 (`run.py` single-boundary), TASK-ABW-001 (runner wiring),
`unattended-build-service-scope.md` (FEAT-UBS-002 budget guards),
`dependable-forge-overview-...md` (the seam contract UBS-002 keys off).

## Context

A 2026-07-02 audit of the UBS keystone (FEAT-UBS-001) established that the
`autobuild_runner` node bodies are **not** placeholders — TASK-ABW-001 wired
`_node_running_wave` to invoke `guardkit autobuild` on 2026-05-14. That audit
surfaced two coupled facts that need a recorded decision.

**1. There are two independent subprocess pathways to the `guardkit` binary.**

- `adapters/guardkit/run.py::run()` is documented (TASK-GCI-008) as the *single
  boundary* for every GuardKit invocation. The tool-layer wrappers
  (`guardkit.py`, `graphiti.py`) go through it. It gives: DDR-005 context-flag
  resolution, `--nats` progress streaming (with the `progress_subscriber`
  binding `pipeline.stage-complete.*`), allowlist confinement, a structured
  `GuardKitResult` from `parser.py` (**including `coach_score` and
  `criterion_breakdown`**), a 600s *default-but-parameterisable* timeout, and a
  never-raises contract (ADR-ARCH-025).
- `subagents/autobuild_runner.py::_node_running_wave()` **bypasses `run.py`** and
  shells `guardkit autobuild feature <id> --fresh --verbose --coach-model
  coach-ft-v3` directly via `asyncio.create_subprocess_exec`. It uses a bespoke
  3600s timeout (`DEFAULT_AUTOBUILD_TIMEOUT_SECONDS`), merges stderr→stdout, and
  drains stdout **line-by-line** to count `[guardkit-checkpoint]` markers for the
  `stage_complete` fallback.

The runner's path is not gratuitous: `guardkit autobuild` is a *long-running*
(~33 min) multi-turn build that wants **live** progress, whereas `run.py`'s
`_execute_subprocess` buffers via `proc.communicate()` and returns only at the
end — unsuitable for a 33-minute `--verbose` stream as written. So the split has
a real cause; it is also real duplication (timeout/kill/confinement logic
reimplemented) and it makes the "single boundary" claim inaccurate for the one
invocation that matters most for the night shift.

**2. The runner does not populate Coach scores — and UBS-002 needs them.**

`AutobuildState.last_coach_score` / `aggregate_coach_score` are defined
(`autobuild_runner.py:202-203`), read by the bridge translator, and emitted on
the wire (`lifecycle_bridge/translation.py:591,625`). But **nothing writes them**:
`_node_running_wave` only sets `tasks_completed` (from the checkpoint count) and
maps the exit code; `lifecycle_bridge/wireup.py:1096` hardcodes
`running_snapshot["last_coach_score"] = None`. The pipe is laid end-to-end; no
source feeds it.

The seam contract (shared overview) states that FEAT-UBS-002's budget guards /
autonomy ratchet **key off `last_coach_score` / `aggregate_coach_score`**.
Therefore the coach-score gap is a hard prerequisite for UBS-002, not cosmetic
cleanup. `run.py`'s `parser.py` already extracts `coach_score` — but against the
*shorter* subcommands (feature-spec, system-arch). Whether
`guardkit autobuild --verbose` emits a `parser.py`-compatible `coach_score:` line
is **unverified and lives across the frozen seam** (the runner author greps only
`[guardkit-checkpoint]`, which is weak evidence the score line may not be present
in that shape). We must not add score-parsing on an assumed format.

## Decision

1. **Ratify the runner's dedicated long-running streaming subprocess as a
   deliberate interim.** Correct the "single boundary" claim: `run.py` is the
   single boundary for **one-shot tool invocations**; the long-running
   `autobuild` build deliberately uses a streaming subprocess in the runner
   *today*, for live progress + a 3600s budget + direct lifecycle mapping. Add a
   cross-reference comment in both `run.py` and `autobuild_runner.py` so the two
   paths are discoverable and neither reads as accidental drift.

2. **Record the coach-score population gap as UBS-002's first prerequisite.**
   Budget guards cannot ratchet against a field that is always `None`. Closing
   this gap is wave 1 of UBS-002, not a separate cleanup.

3. **Define the convergence path, gated on real output.** The target end-state is
   for `autobuild` to flow through a **streaming variant of `run.py`** (or a
   shared execution core) that returns a structured `GuardKitResult`, so the
   runner consumes `GuardKitResult.coach_score` instead of scraping stdout —
   collapsing the two paths and closing the gap in one move. This is **gated on
   capturing a real `guardkit autobuild --verbose` transcript** (the
   TASK-ABW-OPS FEAT-9E59 rehearsal, now an operator-handoff) so the parser is
   built and verified against actual output, not an assumed format. We do **not**
   refactor the invocation blind now — doing so immediately before an
   operator-handoff validation would make the rehearsal validate untested code
   instead of confirming the known-good demo path.

## Consequences

- **Positive.** The keystone stays working and demo-proven; the "single
  boundary" docs stop being misleading; UBS-002 gets an explicit, evidence-gated
  prerequisite instead of silently building on a `None` field; the convergence
  (fold `autobuild` into a streaming `run.py`) is captured, not forgotten.
- **Negative / accepted.** Duplicated subprocess logic persists in the interim.
  Live progress remains stdout-scraped rather than `--nats`-driven until
  convergence. UBS-002 cannot start its enforcement half until a real transcript
  confirms the score format.
- **Follow-ups.**
  - TASK-ABW-OPS AC-OPS-05 must **capture and archive** the autobuild stdout/NATS
    transcript (added in this session) — it is the input for the parser work.
  - A UBS-002 wave-1 task: populate `last_coach_score`/`aggregate_coach_score`
    from the verified transcript format (via a `run.py` streaming variant, or a
    minimal score extractor if convergence is deferred).
  - Revisit this ADR's "interim" status once the streaming-`run.py` convergence
    lands; at that point it moves to "Accepted (superseded by the converged
    path)".

## Non-goals

- This ADR does **not** change the frozen seam (`guardkit autobuild` CLI shape +
  `--coach-model` verdict schema). Reading a `coach_score` from the verdict is
  *consuming* the existing seam, not altering it.
- It does not touch `guardkit` (forge-only session; the Coach output format is
  owned across the seam).

## Closure (TASK-UBS1C-001 — 2026-07-26)

The coach-score population gap is **closed** for the interim streaming path.
`_node_running_wave` now parses `Completed turn <N>: success|feedback - ...`
decision lines from `guardkit autobuild --verbose` stdout and populates both
`last_coach_score` (1.0 for success, 0.0 for feedback) and
`aggregate_coach_score` (success ratio over decision-bearing turns) in the
snapshot. These scores flow through to `_node_completed` and are emitted by the
bridge translator.

Evidence backing: `docs/research/evidence/autobuild-transcripts-2026-07-26/README.md`
documents the negative finding (no numeric `coach_score:` line on stdout) and the
decision-derived semantics used for parsing.

The runner still bypasses `run.py` (the two-path split persists), but the
UBS-002 budget guards now have live coach-score data to ratchet against. The
convergence to a streaming `run.py` variant remains the target end-state.
