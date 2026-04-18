# ADR-ARCH-006: Calibration corpus — ingest history files as `CalibrationEvent` stream

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 2 Revision 3

## Context

Across `specialist-agent`, `nats-core`, and `nats-infrastructure` Rich has produced ~35 history files capturing every GuardKit session's Q&A turns — `command_history.md`, `feature-spec-FEAT-00N-history.md`, `feature-plan-…-history.md`, `system-arch-history.md`, `system-design-history.md`, `system-plan-history.md`. These encode response signatures (e.g. `A A A A` fast-path acceptance, `accept defaults`, rare inline edits with corrections) and validate fleet-master-index Pattern 3's "~95% defaults accepted" observation.

This is *training data*. Forge should learn from it — not by hand-copying thresholds into YAML, but by parsing it into structured evidence the reasoning model retrieves at build start.

## Decision

Create two modules:

- **`forge.calibration`** (domain core, pure): defines `CalibrationEvent` schema; classifies response patterns (`ACCEPT_ALL`, `ACCEPT_WITH_EDIT`, `REJECT`, `DEFER`, `CUSTOM`); computes per-command acceptance rates.
- **`forge.adapters.history_parser`**: tokenises markdown history files, identifies Q&A turns by the `━━━` section markers + proposed-default + response pattern, emits `CalibrationEvent` stream.

Ingestion:
- **Batch** at project setup — `forge calibrate <path>` CLI subcommand scans a directory (or set of repos) for history files and seeds them into Graphiti `forge_calibration_history`.
- **Incremental** — re-runnable as new history files appear (V1 manual invocation; V2 optional file-watcher).

Retrieval:
- At build start, `forge.prompts` retrieves similar-context `CalibrationEvent`s and injects them into the `{calibration_priors}` placeholder of the system prompt. Similarity is context-driven — same command, same project type, comparable scope.

## Consequences

- **+** Forge inherits Rich's historical decision signatures on day 1; not a cold start.
- **+** The more Rich uses the factory, the smarter Forge gets — new history files land, next build retrieves better priors.
- **+** Enables the emergent "training mode" curve (ADR-ARCH-019) — small corpus → conservative behaviour → grows with sample size.
- **−** History-file format is not strictly standardised; parser must tolerate variation across the ~35 files; some files may yield noisy events.
- **−** Ingestion requires Gemini 2.5 Pro (Graphiti's ingestion LLM — already configured in `.guardkit/graphiti.yaml`) to process each episode; one-time cost per history file.
- **−** Rich's response wording is terse ("A A A A"); normalising into `CalibrationEvent.response_normalised` is heuristic; future fine-tuning could improve accuracy.
