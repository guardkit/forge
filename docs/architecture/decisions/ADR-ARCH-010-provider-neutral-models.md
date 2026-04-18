# ADR-ARCH-010: Provider-neutral two-model configuration via `init_chat_model`

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 3 Revision 4

## Context

D2/D3 fleet-wide decisions require provider-agnostic model selection. Anthropic's 2026 uptime history, Gemini 3.1 Pro's pricing advantage, and the fleet's already-mixed use of Gemini (Graphiti ingestion) and Claude (Coach) make locking to a single vendor a structural risk and cost penalty. The `pipeline-orchestrator-conversation-starter.md` D2 explicitly calls out *"Gemini 3.1 Pro API (primary) or Claude API — configurable."*

Initial Category 3 framing anchored on Anthropic (`ANTHROPIC_API_KEY`, Opus 4.7 + Sonnet 4.6). Rich's Revision 4 correction: keep provider-neutral, lead with Gemini 3.1 Pro as primary per cost + reliability analysis.

## Decision

All model instantiation flows through **`init_chat_model("provider:model")`** (LangChain's provider-neutral factory), resolved at the lowest factory layer from `AgentConfig.models.{reasoning,implementation}_model` (env vars `AGENT_MODELS__REASONING_MODEL` / `AGENT_MODELS__IMPLEMENTATION_MODEL`) per the specialist-agent LES1 parity rule — the factory owns resolution, not handlers.

**Primary configuration**:
```yaml
models:
  reasoning_model:      "google_genai:gemini-3.1-pro"
  implementation_model: "google_genai:gemini-2.5-flash"
```

**Alternates** installed and supported (switch via env var, no code change):
- `anthropic:claude-opus-4-7` / `claude-sonnet-4-6`
- `openai:gpt-5` / `gpt-5-mini`
- `vllm:qwen3-coder-next` (GB10 local, zero-marginal-cost)
- `ollama:qwen2.5:14b` (MacBook local)

LangChain integrations installed via `pyproject.toml [project.optional-dependencies.providers]` block per LCOI rule (CLAUDE.md `.[providers]` pattern):
- `langchain-anthropic` (in base dependencies)
- `langchain-google-genai`
- `langchain-openai`

Failover automation is **V2-deferred**. V1 requires manual env-var flip during provider outages.

## Consequences

- **+** Survives any single-vendor outage via config flip — no code release required.
- **+** Cost-optimises: Gemini 3.1 Pro substantially cheaper than Opus at sustained volume.
- **+** Consistent with fleet-wide Graphiti Gemini move (`.guardkit/graphiti.yaml`, 14 April 2026).
- **+** Two-model separation (D5) preserved via Gemini Pro + Flash tier difference, or cross-family (Gemini Pro + Claude Sonnet) per Rich's preference.
- **+** LES1 parity rule respected: single factory resolution point, handlers don't re-compute.
- **−** Four LangChain integrations to keep current; mitigated by LCOI's single optional-deps block.
- **−** V1 manual failover requires operator intervention on outage; acceptable for single-operator local-first system.
