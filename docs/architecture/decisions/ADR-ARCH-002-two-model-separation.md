# ADR-ARCH-002: Two-model separation — reasoning drives the graph, implementation executes within tools/sub-agents

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 1

## Context

CLAUDE.md prescribes DeepAgents' two-model pattern: "reasoning model drives decisions, evaluates output quality; implementation model executes tasks, generates artifacts." Fleet decision D5 also forbids a single model evaluating its own output (prevents self-confirmation bias — validated by Block + Anthropic research cited in `pipeline-orchestrator-motivation.md`).

For Forge specifically, "implementation" is both (a) subprocess invocation of GuardKit/git/gh which don't need an LLM at all, and (b) content-generating sub-agents (`build_plan_composer`, `architecture_dispatcher`) which are genuine LLM synthesis.

## Decision

- **Reasoning model** (configured via `AGENT_MODELS__REASONING_MODEL`) drives the outer DeepAgents graph: decides which tool to call next, evaluates Coach scores from specialist agents, constructs `write_todos` plans, writes stage labels.
- **Implementation model** (configured via `AGENT_MODELS__IMPLEMENTATION_MODEL`) is used inside *content-generating* sub-agents (`build_plan_composer` synthesises `buildplan.md`; `architecture_dispatcher` formulates commands to the architect). Subprocess tools (GuardKit, git, gh) don't invoke this model at all — they delegate to GuardKit's own configured models.
- Two-model separation applies even when both models are from the same vendor family (e.g. Gemini Pro for reasoning + Gemini Flash for implementation) — tier difference is sufficient.

## Consequences

- **+** Follows DeepAgents' documented pattern; aligns with D5 fleet-wide.
- **+** Cost-optimises: implementation model can be cheaper/faster (Flash tier) for content generation while reasoning stays on Pro for judgment.
- **+** Supports cross-family configuration (e.g. Gemini Pro reasoning + Claude Sonnet implementation) for maximum separation when Rich wants it.
- **+** Aligns with specialist-agent LES1 factory-level model resolution rule — both models resolve via `AgentConfig` + `init_chat_model` at one factory layer.
- **−** Increases config surface (two `{provider}:{model}` strings instead of one); mitigated by sensible defaults in `forge.yaml`.
- **−** Implementation model may go unused for builds that never invoke `build_plan_composer` (e.g. Mode B feature-addition with a small feature skipping the build-plan stage); acceptable cost.
