# ADR-ARCH-018: Calibration priors as retrievable system-prompt input

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revision 6

## Context

ADR-ARCH-006 parses Rich's history files into `CalibrationEvent` stream and seeds Graphiti `forge_calibration_history`. Question: *how* does the reasoning model use these at build time? Two options:

1. **Offline fine-tuning** — periodically train a Forge-specific model on the corpus. Wrong answer: Forge is a coordinator, not a specialist (fleet-master-index D36); fine-tuning is appropriate for specialists (Architect, PO), not orchestrators.
2. **Runtime retrieval** — at build start, retrieve similar-context calibration events + similar-context pipeline history and inject them into the system prompt. Reasoning model reads them as evidence.

## Decision

Runtime retrieval. Concretely:

- `forge.prompts` is a `system-prompt-template-specialist`-style module — domain-agnostic prompt template with placeholders (`{date}`, `{available_capabilities}`, `{calibration_priors}`, `{project_context}`, `{domain_prompt}`) injected at startup via `str.format()`.
- At build start (PREPARING state), `forge.agent`:
  1. Retrieves current-build context (feature_id, repo, feature scope) from the `BuildQueuedPayload`.
  2. Queries Graphiti `forge_calibration_history` for similar-context events (same command family, comparable scope) — budget: `learning_meta.retrieval_context_tokens` (default 2000 tokens).
  3. Queries Graphiti `forge_pipeline_history` for prior builds on this repo / similar features — outcomes, gate decisions, override events, `CapabilityResolution` records.
  4. Queries `forge.discovery.list_capabilities()` for the live fleet snapshot.
  5. Renders the system prompt with all placeholders filled and instantiates `create_deep_agent(system_prompt=..., tools=..., subagents=..., memory=...)`.

The reasoning model then reads the injected priors as evidence, not rules — used to bias its gate decisions, tool-call sequencing, and approval thresholds.

## Consequences

- **+** Consistent with DeepAgents pattern: one compiled graph per session, system prompt fully resolved at startup.
- **+** Priors are human-readable in logs and LangSmith traces — Rich can audit what evidence Forge was given.
- **+** Changes to the calibration corpus take effect on the *next* build, not mid-build — predictable behaviour.
- **+** Fine-tuning deferred — if evidence accumulates that retrieval is insufficient, Forge could graduate to a fine-tuned reasoning model in V2 without changing the runtime architecture.
- **−** Retrieval quality depends on Graphiti's similarity search + embedding model (nomic-embed-text-v1.5 per `.guardkit/graphiti.yaml`). Retrieval tuning may be needed after first real-world runs.
- **−** 2000-token retrieval is a cost centre per build (part of ADR-ARCH-030 budget envelope) — cap tightness vs quality is a V1 tuning concern.
