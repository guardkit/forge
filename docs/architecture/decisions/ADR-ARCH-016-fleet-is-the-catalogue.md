# ADR-ARCH-016: Fleet is the catalogue — no pre-coded stage kinds; stages are emergent labels

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 4 Revisions 5 → 6 → 7

## Context

Initial revisions tried to pre-declare "stage kinds" in Forge's code (a `STAGE_KINDS` dictionary mapping `specification_review` / `architecture_review` / `quality_review` → preferred tools). Rich pushed back repeatedly: even a commented-out block of future stages is Forge pre-declaring behaviour for capabilities the fleet hasn't surfaced yet. The agent harness's value is *composing what's available into what the build needs*, not replaying fixed sequences.

Parallel push: `forge.yaml` had an ordered `pipeline.stages: [...]` list. Same problem — that's a state machine in YAML, not an agent reasoning about work. If that's the architecture, we don't need DeepAgents.

## Decision

Forge has **no pre-coded stage catalogue, anywhere**:

- No `STAGE_KINDS` dictionary in `forge.stages` (module dramatically reduced to a tiny helper for SQLite writes).
- No `pipeline.stages: [...]` list in `forge.yaml`.
- No `preferred_tool` / `fallback_intent` mappings anywhere.

**The catalogue is the live fleet plus local tools:**

- `forge.discovery.list_capabilities()` returns every registered agent's `AgentManifest` with full `ToolCapability.description` + `IntentCapability.description` text. This is injected into the system prompt as `{available_capabilities}`.
- DeepAgents registers all Forge-specific `@tool` functions (GuardKit subcommands, git, gh via `execute`, dispatch_by_capability, approval, notification, graphiti, history) — the reasoning model sees them with their docstrings.
- The system prompt describes the factory's *shape* in prose ("a feature goes through specification, architecture, planning, implementation, PR — what each means for *this* build depends on what the fleet can do").

**Stages are emergent labels:** when the reasoning model dispatches, it names what it's doing ("Specification Review" / "Exploring alternate data model" / "Accessibility Audit") and the label is written **retrospectively** to SQLite `stage_log`. `forge history` renders the labels as build narrative. Labels are reasoning outputs, not a controlled vocabulary consulted prospectively.

## Consequences

- **+** Adding a QA/UX/Ideation agent requires zero Forge changes — agent ships, registers its manifest, reasoning model reads its description at next build, decides whether to use it based on build needs + calibration priors.
- **+** Rich's long-standing concern (*"if we hard code the pipeline stages what's the point in having an agent harness?"*) resolved.
- **+** Matches how an agent with tool access actually works — the LLM reads tool docstrings and composes.
- **+** Observability preserved: `forge history` still reads as a narrative, because the reasoning model labels its own dispatches.
- **−** Requires high-quality `AgentManifest` descriptions from specialist agents — garbage in, garbage out. Mitigated: contract in `agent-manifest-contract.md` already requires this.
- **−** Unfamiliar pattern for developers used to explicit state machines. Docs and examples matter.
