# ADR-ARCH-019: No static behavioural config — gates, thresholds, notifications, training-mode are reasoning outputs

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revisions 6 → 7 → 8

## Context

Across Revisions 6–8, successive attempts to anchor "sensible defaults" in `forge.yaml` kept falling to the same objection: *if the reasoning model reasons and learns, why is this in YAML?* Each iteration pre-declared behaviour for capabilities the system hadn't met yet — `gate_defaults` per stage kind, then per tool, then "universal default + per-tool overrides." All were static registries of imagined capabilities.

Rich's sharpest framing: *"how is this using an agent harness as I intended?"*

The resolution: anything the agent should reason about, retrieve evidence for, and learn from must not be declared statically. `forge.yaml` holds infrastructure, models, constitutional rules, and learning meta-config *only*.

## Decision

`forge.yaml` scope is strictly limited to:

1. **Infrastructure** — NATS URL, Graphiti endpoint, SQLite path.
2. **Models** — `reasoning_model` / `implementation_model` provider:model strings (ADR-ARCH-010).
3. **Build config** — `max_turns_per_task`, `sdk_timeout`, `max_concurrent: 1`.
4. **Constitutional rules** — `pr_review_always_human: true`, `sequential_builds_only: true`, `never_skip_precommit_hooks: true`. Not reasoning-adjustable.
5. **Permissions** — DeepAgents fs/shell/network allowlists (safety, not behaviour — ADR-ARCH-023).
6. **Learning meta-config** — `graphiti_groups`, `retrieval_context_tokens`, `min_samples_before_suggesting_adjustment`. Describes *how* learning works, not *what* to learn.

**Removed from `forge.yaml` (moved to reasoning + Graphiti):**
- Gate thresholds per stage or tool
- Notification on/off per event type
- `training_mode` flag
- Per-tool overrides
- Stage kind lists
- Per-capability priors or priorities

**How these concerns are handled instead:**

- **Gate decisions** — reasoning output per dispatch. Coach score + retrieved priors for this capability + build-risk context → decide auto-approve / flag / hard-stop. First use of a new capability (no priors) → reasoning naturally flags-for-review. First use itself is a calibration event.
- **Training mode** — *emergent*. Few priors → natural conservatism → frequent flags → Rich approves/overrides → samples grow → conservatism relaxes. No toggle.
- **Notifications** — reasoning decision per event. Model consults retrieved priors on Rich's past ack/read behaviour per event type, per project. Hard-stops always emit; flagged stages always block (that's `interrupt()`, not notification).
- **Per-capability bias** — `forge.learning` detects override patterns in Graphiti `forge_pipeline_history`, proposes a `CalibrationAdjustment` entity; Rich confirms via CLI approval round-trip; entity lands in Graphiti; future builds' prompt retrievals surface it. Never a YAML edit.

## Consequences

- **+** Resolves the agent-harness integrity question: reasoning and learning are first-class, not ornamental.
- **+** Adding any capability (current or future) requires zero Forge config. First-use priors come from reasoning + naturally conservative behaviour.
- **+** Rich's interventions compound: override in the moment → evidence for next build → `forge.learning` detects pattern → proposes adjustment → approved → biases future reasoning. Never a YAML edit loop.
- **+** Constitutional rules (the genuinely static invariants) are small and auditable.
- **−** First N builds run without priors — more human round-trips than a fully-calibrated system. Acceptable as the learning curve.
- **−** Requires Graphiti to be available for learning to progress. Degraded behaviour when unavailable (ADR-ARCH-029) — reasoning still runs, conservatively.
- **−** Harder to write rules like "always flag on Fridays" — such rules simply don't exist. The agent either learns the pattern or Rich overrides in the moment. Intentional.
