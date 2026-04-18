# ADR-ARCH-030: Budget ceiling ≈ £500/month LLM — drives Gemini-primary + learning cost reduction

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 6

## Context

Forge is a research/personal-productivity project pre-commercialisation. LLM cost is the only substantial operational cost (GB10 is sunk hardware; NATS + Graphiti are self-hosted; GitHub is existing). A build with 200–500 reasoning-model turns + implementation-model calls + retrieval overhead can run £2–10 at Gemini 3.1 Pro pricing. Monthly usage of 50–200 builds (Rich's realistic max) lands around £100–2000/month. A ceiling prevents runaway.

## Decision

**Target LLM budget: ~£500/month** across all builds.

This is a design constraint, not a contract. Architectural consequences:

- **Gemini 3.1 Pro primary** (ADR-ARCH-010) — cheapest advanced-reasoning tier at sustained volume. Alternate providers (Anthropic, OpenAI) are more expensive per turn and therefore used only during Gemini outage.
- **Sequential builds** (ADR-SP-012 + ADR-ARCH-014) physically cap concurrent LLM spend — one build at a time, one set of turns consuming quota.
- **Memory Store caches retrievals within a build** (ADR-ARCH-022) — repeated references to the same prior don't re-query Graphiti (which has its own LLM-backed entity extraction).
- **Auto-summarisation** (DeepAgents built-in, ADR-ARCH-020) caps prompt growth on long builds — caps per-turn cost.
- **Calibration corpus ingested once** (ADR-ARCH-006) — ongoing Gemini cost is build-time only, not re-ingestion.
- **Learning-loop efficacy** (ADR-ARCH-005, ADR-ARCH-019) — compounding priors reduce forced human round-trips over time → fewer pauses waiting for Rich → faster builds → lower LLM cost per completed PR. The budget *should* trend down per-PR as calibration grows.
- **Local vLLM as zero-marginal-cost fallback** — available for implementation model (Qwen3-Coder-Next on port 8002) when Rich wants to run on-prem.

## Consequences

- **+** Explicit budget shape makes spend predictable — per-build estimate × monthly cadence = monthly forecast.
- **+** Gemini-primary is both cost-optimal AND the fleet's de facto choice (Graphiti uses Gemini 2.5 Pro per `.guardkit/graphiti.yaml`).
- **+** Learning loop's ROI becomes visible — a metric worth tracking: "£ per completed PR" should trend downward.
- **+** Budget overrun = learning-signal that priors or prompts need tuning, not a demand for "more budget."
- **−** If Rich needs to rush multiple greenfield builds (high per-build cost due to no priors), monthly spend can spike above ceiling. Operational event — not structural.
- **−** Provider lock-in to Gemini *for cost reasons* — even with multi-provider support, switching to Anthropic full-time would break the budget. Accepted as pragmatic.
