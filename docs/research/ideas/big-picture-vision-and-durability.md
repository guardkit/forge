# Ship's Computer — Big Picture: What We're Building and Why

## The Strategic Vision Behind the Jarvis Agent Fleet

**Date:** April 2026 (updated: 12 April 2026)
**Author:** Rich (Appmilla)

---

## What This Document Is

This captures the strategic thinking behind the entire Ship's Computer / Jarvis project.
Not the technical specs (those live in each repo's vision docs and the fleet master index)
but the *why* — the goals, the learning journey, the durability analysis, and the honest
assessment of what's at risk and what's permanent.

Read this first. Then read `fleet-master-index.md` for the technical inventory.

---

## The Big Picture

We're building a Software Factory — a fleet of specialist agents orchestrated through
NATS JetStream that collapses the entire software delivery system into one outcome-driven
pipeline. The fleet handles the full lifecycle from ideation through to deployed code:

```
Product idea / raw information
        ↓
Ideation Agent (score and rank ideas)
        ↓
Product Owner Agent (structured product docs)
        ↓  ← calls Architect Agent via NATS: "Is this feasible?"
Architect Agent (conversation starter with C4, ADRs)
        ↓
Forge (checkpoint manager — confidence-gated quality gates)
        ↓
GuardKit Commands (/system-arch → /system-design → /feature-spec → build)
        ↓
Outcome Gate (did the thing work as intended?)
        ↓
Deployed software
```

All three specialist agents (Ideation, Product Owner, Architect) are deployments of the
same binary — `specialist-agent serve --role X` — differentiated by role configuration,
fine-tuned model weights, and per-role knowledge graph (the unified harness pattern).

Plus a General Purpose Agent for everything else, a YouTube Planner for content creation,
and a GCSE multi-subject Tutor for Rich's daughter.

The system is accessible via multiple adapters: Reachy Mini (voice), Telegram, Slack,
Dashboard, and CLI. Jarvis is the intent router that dispatches requests to the right
agent.

But the system is not the point. **The system is a vehicle for learning.** The real goals
are deeper.

---

## The Three Goals

### 1. Learn AI-Augmented Development by Doing It

Every software engineer is trying to figure out how to work effectively with AI. Most
are reading blog posts and watching demos. We're building real systems, hitting real
problems, and documenting real solutions.

The war stories are the asset:
- **180+ review reports** across AutoBuild runs, revealing patterns in how AI agents fail
- **FB01–FB28 cascading fix series** — a single root cause that propagated through 28
  fixes, teaching us about the stochastic development problem
- **The NemoClaw saga** — where NVIDIA's marketing promised one thing and the community
  forums revealed another, teaching us about due diligence with new platforms
- **The C4 validation discovery** — that forcing C4 diagramming changes root cause
  analysis approximately 9/10 times vs verbal analysis alone
- **The self-evaluation failure** — that AI agents confidently fill specification gaps
  rather than acknowledging uncertainty, and that adversarial evaluation (Coach) catches
  what self-evaluation misses
- **Phase 0 FinProxy: 0.75 score in 93 seconds** — the first real specialist agent run
  on real product docs, proving the Player-Coach adversarial loop works end-to-end.
  Three iterations to converge. 1,006 tests green. The Coach detected planted flaws
  4 out of 4 times.
- **Phase 1 FinProxy: 0.93 score in 162 seconds** — quality improvement from ADR format
  changes, narrative flow, and failure mode detection. Fewer iterations (2 vs 3) means
  the Coach's confidence is rising as the system calibrates.
- **First fine-tune: loss 2.45 → 0.50** — Gemma 4 31B on 1,736 GCSE English tutor
  examples using Unsloth + TRL SFTTrainer on the DGX Spark GB10. Two hours five minutes.
  End-to-end pipeline proven: data generation (agentic dataset factory) → training
  (Unsloth) → LoRA adapter → merged model → GGUF quantisation. All outputs persisted
  to host.
- **Domain fidelity regressions** — Phase 1C identified 5 regression patterns
  (SOURCE_COLLAPSE, DOMAIN_DILUTION) where the model collapsed distinct domain
  boundaries or diluted source-specific knowledge. The lesson: quality gates must
  check for what the model *removes* as well as what it *adds*.
- **nats-core at 97% test coverage** — 17 test files, 6 features, implemented in full.
  The messaging backbone is working code with typed payloads, not a design contract.
  Proves that the GuardKit command sequence (/system-arch → /system-design →
  /feature-spec × 6 → build) produces production-quality library code.
- **Feature spec defaults accepted ~95%** — across 7 feature specs in specialist-agent,
  Rich almost always accepts GuardKit's proposed defaults. Validates prompt calibration
  and provides training signal for future checkpoint automation.

These insights don't become obsolete when tools change. They're transferable knowledge
about how to work with AI systems, period.

### 2. Learn Fine-Tuning, RAG, and Agentic AI Hands-On

The technology stack we're working with is genuinely at the frontier:

- **Fine-tuning small language models** — Unsloth QLoRA on Gemma 4 31B Dense, learning
  that fine-tuning teaches behaviour while RAG teaches facts (two independently
  updatable layers). First fine-tune complete; multi-domain expansion planned.
- **Knowledge graphs for agent memory** — Graphiti (FalkorDB-backed) providing persistent
  per-role and per-project knowledge, with three-scope compounding (project → role →
  fleet). Validated limitation: Graphiti stores relationships and decisions, not
  documents.
- **Agent harness design** — LangChain DeepAgents SDK, understanding the unified harness
  pattern where one codebase serves multiple specialist roles via configuration.
  The three-layer architecture (behaviour + knowledge + context) as independently
  updatable layers.
- **Adversarial cooperation** — Player-Coach pattern, weighted evaluation with
  detection patterns and penalties, the insight that two-model separation prevents
  self-confirmation bias. Proven across two FinProxy runs.
- **Local inference** — vLLM on DGX Spark GB10, three simultaneous models (Graphiti
  LLM, embedding, AutoBuild), the reality of running AI locally vs the marketing
  promises. Fine-tuned models served locally for development, deployed to AWS Bedrock
  CMI for production.
- **Training data generation** — the agentic dataset factory: Player-Coach adversarial
  loop generates high-quality training examples at 94.8% acceptance rate. ~2,500
  examples in a production run. The pipeline that trains the pipeline.
- **PDF ingestion** — Docling standard mode for digital PDFs, VLM mode for scanned
  paperbacks (via HP OfficeJet). Both validated on GB10 with Mr Bruff GCSE guides.

Each of these is a skill that compounds. The combination — someone who can fine-tune
models AND build agentic systems AND design knowledge graph architectures AND has 25
years of systems engineering — is genuinely rare.

### 3. Build Content from the Journey

The YouTube channel isn't a side project — it's the documentation layer for goals 1 and 2.
Every problem solved is a potential video. Every honest failure is content that resonates
with the target audience: mid-career engineers navigating the AI transition.

The Rory Sutherland insight applies: **sell how you think, not what you do.** A video about
"how to use AutoBuild" has a shelf life. A video about "what I learned from 180 review
reports about why AI agents fail" is evergreen. The tool might change; the insight is permanent.

The one-of-one test: could 100 other creators make this video? If the content is grounded
in specific, lived experience (the FB28 fix series, the NemoClaw forums, the moment James
reviewed 14 FinProxy docs and had almost no feedback, the Phase 0 FinProxy run scoring
0.75 on the first real attempt), the answer is no.

Content strategy validated: 70% browse (story-driven) / 30% search (tutorials). YouTube
Planner agent in the fleet. Three tooling systems designed (Research & Intelligence,
Video Planning Pipeline, Transcript Map).

---

## The Software Factory — Why Coordination Is the Real Problem

### The Solow Paradox Revisited

"You can see the computer age everywhere except in the productivity statistics." Robert
Solow said that in 1987 about IT. The same observation applies to AI coding tools in
2026. Individual developers feel faster. Team and organisational throughput isn't scaling
proportionally.

**Accelerating coding without fixing coordination simply exposes the coordination
bottleneck.** This mirrors the IT productivity paradox exactly — the gains only arrived
when organisations redesigned their processes around the technology, not when they
bolted new technology onto existing processes.

### The Steam Engine Trap

When electric motors replaced steam engines in factories, the first generation of
adopters simply swapped one power source for another — same layout, same belts, same
pulleys. The productivity gains only arrived when engineers asked: *if we are not
constrained by a central power source, how should a factory actually be organised?*

Bolting AI onto existing PM tools — even Linear's agent approach — is electrifying the
steam engine. The Software Factory skips the intermediate step entirely.

### Context-First Delivery

Traditional software delivery maintains three separate systems in synchronisation:
a planning system (Jira/Linear), a knowledge system (Confluence/Google Docs), and a
build system (GitHub/CI). Every handoff between them is a translation step. Every
translation step is an opportunity for drift, loss of context, and wasted hours.

The Software Factory collapses all three into one pipeline. The structured documents
produced by the Product Owner Agent *are* the project management. The git history and
agent logs *are* the audit trail. The Coach scores *are* the quality reporting. A query
to the system answers "where are we?" There is no separate reporting layer.

The coordination overhead does not get automated. It ceases to exist as a distinct
concern.

### Outcome Gates Replace Progress Tracking

Traditional delivery measures activity: sprints, velocity, burn-down. Outcome-based
delivery asks: did the thing work as intended? These are gates, not stages. A gate is
either passed or it isn't. An agent monitors for it. **Outcome gates replace progress
tracking. Decisions replace status updates. The pipeline signals rather than reports.**

### The FinProxy Evidence

The FinProxy collaboration made this concrete: Rich produced fourteen structured
documents over a weekend. The comparable time spent on weekly reporting spreadsheets,
timesheets, and status updates was pure friction layered on top of something that was
already working. The docs *were* the work. The PM overhead was coordination tax.

---

## Durability Analysis

Not everything we're building has the same shelf life. Understanding which layers are
durable and which are replaceable is critical for making good investment decisions.

### Layer 1: Methodology & Domain Knowledge (Permanent)

These survive any tool or framework change:

| Asset | Why It's Durable |
|-------|-----------------|
| C4 validation as root cause analysis | Architectural pattern, tool-independent |
| Adversarial evaluation beats self-evaluation | Fundamental AI insight, validated by Anthropic independently |
| Fine-tuning teaches behaviour, RAG teaches facts | Model-agnostic principle |
| Weighted criteria for subjective quality grading | Evaluation methodology, domain-independent |
| Review-before-fix workflow | Engineering discipline |
| Specification by Example (BDD/Gherkin) | Decades-old, tool-independent methodology |
| "Stay at altitude" architectural practice | Engineering wisdom |
| Autonomy bias detection in frontier models | Structural AI insight |
| The 180+ review reports and lessons learned | Historical evidence, permanently valuable |
| Template seeding into knowledge graphs | Context engineering pattern |
| Context-first delivery collapses coordination | Organisational design principle, not tooling |
| Solow Paradox / steam engine trap framing | Economic insight, applies across eras |
| Two-model separation prevents self-confirmation | Evaluation methodology |
| Three-layer architecture (behaviour + knowledge + context) | Model-agnostic principle |
| Detection patterns with penalties (PHANTOM, UNGROUNDED, etc.) | Quality methodology |
| Feature spec defaults acceptance as calibration signal | Training data methodology |

**Risk of obsolescence: Near zero.** These are ways of thinking, not tools.

### Layer 2: Architectural Patterns (2-3+ Years)

These are durable patterns that may shift in specific implementation but the concepts persist:

| Asset | Why It's Durable | What Might Change |
|-------|-----------------|-------------------|
| NATS event bus for multi-agent orchestration | Event-driven architecture is standard | Specific message broker might change |
| Intent router dispatching to specialist agents | The industry is converging on this pattern | Router implementation might simplify |
| Unified harness — one codebase, many roles | Standard configuration-driven design | Harness framework might change |
| Adapter pattern (MCP, NATS, CLI as interface layers) | Classic integration pattern | Adapters may become unnecessary if platforms standardise |
| Two-model separation (reasoning ≠ implementation) | Prevents self-confirmation bias | May become unnecessary as models improve |
| Confidence-gated checkpoints | Essential for trust in autonomous systems | Checkpoint frequency may decrease over time |
| Provider-agnostic execution (cloud/local/Bedrock) | Avoids vendor lock-in | The "local" option may become less relevant if cloud costs drop |
| Multi-tenancy via topic prefix isolation | Standard multi-tenant pattern | Account model may evolve |
| Graphiti for per-role knowledge compounding | Knowledge graphs for agent context is growing | Specific KG technology may change |
| CAN bus dynamic registration pattern | Self-announcing agents is becoming standard | Protocol details may simplify |
| AgentManifest as single source of truth | Capability announcement is becoming standard | Schema may evolve |
| Context-first delivery (no tickets, no kanban) | Outcome-driven coordination | PM tools may evolve to match |
| Confidence-gated checkpoints via Coach scores | Quality-driven pipeline flow | Threshold calibration will evolve |

**Risk of obsolescence: Low-medium.** The patterns are sound. Specific technologies
(NATS, Graphiti, DeepAgents SDK) may be superseded, but migration is bounded — swap
the implementation, keep the interface.

### Layer 3: Templates & Agent Implementations (12-18 Months)

These are the most replaceable — and that's by design:

| Asset | What Might Replace It | Mitigation |
|-------|----------------------|-----------|
| `langchain-deepagents` templates | Better agent frameworks, native SDK templates | Templates are extractable patterns — the methodology transfers |
| AutoBuild Player-Coach loop | Better AI coding agents (Claude native, Cursor improvements) | Tool interface stability (D7) — swap implementation, keep interface |
| Specific agent implementations | End-to-end platforms that do it all | Heterogeneous fleet means adopt best-of-breed per agent |
| vLLM local inference setup | NVIDIA stack improvements, NemoClaw maturity | Provider-agnostic config — switch providers via YAML |
| DeepAgents SDK | Framework consolidation, new entrants | Orchestrator template captures patterns above the framework |
| AWS Bedrock CMI for fine-tuned models | Better serverless inference options | Provider-agnostic — Bedrock is a hosting choice, not architecture |

**Risk of obsolescence: Medium-high.** But this is the *designed-to-be-replaceable* layer.
The architecture explicitly supports swapping these components:

- Tool interface stability (D7): signatures identical across implementations
- Provider independence: `agent-config.yaml` switches cloud/local/Bedrock
- NATS backbone: agents are independent — replace one without touching others
- Template harvest pattern: build → prove → extract template → rebuild from better template
- AgentManifest pattern: zero refactoring when transport changes (MCP → NATS → future)

### The "Good Enough Kills Great" Risk

The biggest strategic risk isn't that individual tools get replaced. It's that a big
player ships an integrated end-to-end platform that's *good enough* across the whole
pipeline — ideation through deployment — making the overhead of maintaining a custom
multi-agent fleet not worth it.

**Why we're protected against this:**

1. **Heterogeneous fleet** — If Cursor ships a great coding agent, we swap our
   implementation model. If Google ships a great ideation tool, we swap our ideation
   agent. The intent router doesn't care what's behind each dispatch.

2. **Domain-specific knowledge** — No generic platform will encode Rich's architectural
   patterns, Appmilla's project knowledge, or the specific evaluation criteria for
   FinProxy regulatory compliance. The domain layer is ours. Fine-tuned models trained
   on curated architecture books carry judgment that API models don't have.

3. **Graphiti compounds** — Every project the system processes adds to the knowledge
   graph across three scopes (project → role → fleet). After 10 projects, the system
   knows patterns a generic tool can't. This is a moat that grows with use.

4. **The learning was the point** — Even if every tool is replaced, the skills and
   insights gained from building the system are permanent personal assets. The repos
   are the evidence. The war stories are the content. The journey is the product.

5. **No public project combines all five** — Fine-tuned domain models + adversarial
   quality evaluation + per-role knowledge compounding + NATS fleet coordination +
   serverless Bedrock deployment. See `specialist-agent/docs/research/ideas/
   landscape-conversation-starter.md` for the full positioning against Devin, MetaGPT,
   ChatDev, CrewAI, and LangGraph.

---

## What We're NOT Building

Clarity on scope prevents scope creep:

- **Not a product for others** (yet) — This is a personal/Appmilla system. Open-sourcing
  the data training pipeline and methodology (not the data itself) is legally clean and
  strategically more valuable than releasing weights or data.
- **Not a replacement for Claude Desktop** — The agents jumpstart and structure work.
  Deep back-and-forth ideation still happens in Claude Desktop. The Ideation Agent does
  the first 30 minutes of warm-up, not the whole session.
- **Not a competitor to Cursor/Windsurf/etc.** — Those are coding tools. This is a
  full-lifecycle system from ideation through deployment. Different scope.
- **Not an enterprise platform** — Single developer (Rich) + small team (James, Mark).
  Designed for personal productivity, not multi-team enterprise deployment.
- **Not a better Jira** — The Software Factory eliminates the need for a coordination
  layer, not improves one. No kanban boards, no tickets, no status reports.

---

## The Compounding Flywheel

The system is designed to compound across multiple dimensions:

```
Build agents with GuardKit Factory
    → Agents produce YouTube content ideas
    → Content documents the building process
    → Content attracts community
    → Community feedback improves methodology
    → Methodology improves agent templates
    → Better templates build better agents
    → Agents produce better content...
```

**Graphiti compounds:** Every project adds knowledge across three scopes. The tenth
project benefits from lessons in the first nine. Per-role compounding means the
architect agent gets better at architecture across all projects, while per-project
compounding means FinProxy-specific decisions stay isolated.

**Templates compound:** Every bug found updates the template. The TRF-12 review found
84% of bugs were template-preventable — those fixes now benefit every future project.

**Content compounds:** Every failure story is a video. Every honest assessment builds
trust. The channel grows while the system improves.

**Skills compound:** Fine-tuning + RAG + knowledge graphs + agent harnesses + systems
engineering + content creation. Each skill makes the others more valuable.

**Training data compounds:** The agentic dataset factory generates training data using
the same adversarial quality pattern as the agents it trains. More domains, more
examples, better models, better agents, better training data.

---

## DDD Southwest — 16 May 2026

**"2026: The Year of the Software Factory"** — Engine Shed, Bristol

Narrative arc: Solow Paradox → coordination bottleneck → steam engine trap →
context-first delivery → outcome gates → the factory runs end-to-end → human only
engaged when the Coach has specific concerns.

Demo sequence:
1. Architect agent producing architecture from product docs (Phase 0/1 FinProxy)
2. Same harness configured as product owner (Phase 1B second role)
3. Two agents calling each other via NATS (Phase 3 agent-to-agent)
4. Different fine-tuned models, different books, different criteria — same harness code
5. Training data generated by yet another agent using the same adversarial pattern
6. System learns from every session — the 10th project is easier than the 1st

**Key differentiator:** No public project combines fine-tuned domain models +
adversarial quality evaluation + per-role knowledge compounding + NATS fleet
coordination + serverless Bedrock deployment.

---

## Containerisation Strategy: Phase 2, Driven by Concurrency

### The Question

Should agents run in containers (Docker/Podman) for safety, isolation, and portability?
Google's SCION project (April 2026) takes this approach — wrapping coding agents (Gemini CLI,
Claude Code, Codex) in OCI containers with isolated credentials, workspaces, and lifecycle
management. NemoClaw uses OpenShell sandboxes with kernel-level isolation. Is this something
we need?

### The Assessment: Yes, and Sooner Than Originally Planned

The original decision deferred containers to Phase 10+. Two architectural insights
changed this:

1. **CAN bus registration pattern** — With dynamic agent discovery, containers become
   the natural lifecycle management unit. `docker compose up` brings agents online,
   they auto-register with Jarvis, `docker compose down` triggers graceful deregistration.
   The container IS the agent lifecycle.

2. **Concurrency requires isolation** — Running multiple agent instances in parallel
   (e.g., two Forge pipelines for concurrent project builds) requires process
   isolation. Without containers, you're managing multiple Python processes manually —
   venvs, port conflicts, mixed log files, no clean start/stop per agent. With
   containers, `docker compose up --scale forge=2` gives you parallel builds immediately.

**Containers move to Phase 2** — alongside NATS infrastructure. The `nats-asyncio-service`
template already produces a Dockerfile. NATS infrastructure is already Docker Compose.
Adding agent containers to the same compose network is natural, not additional overhead.

**Why containers matter for agents:**
- **Blast radius** — An agent with filesystem and network access can damage the host if it
  goes wrong. A container limits the damage to the container.
- **Credential isolation** — Each agent gets its own API keys, preventing one compromised
  agent from accessing another's credentials.
- **Reproducibility** — Same Python version, same dependencies, every time. No "works on
  my machine" across GB10 and MacBook.
- **Multi-tenant trust** — When Mark runs agents against FinProxy, a container boundary
  means his agent can't accidentally access Appmilla-internal files.
- **Always-on safety** — Agents running autonomously overnight need a stronger safety net
  than agents you're actively watching.

**Risks to manage (not blockers):**
- **GPU passthrough complexity** — Agents that need local vLLM inference require GPU access
  inside the container. On DGX Spark with ARM64, this needs testing. However, most agents
  call vLLM via HTTP API, not direct GPU access, so this only affects the vLLM service
  itself (already containerised).
- **Image rebuild cycle** — Templates and agent configurations are still changing. Pin to
  `docker compose build` + volume mounts for config, not baked-in images, during development.
- **Debugging overhead** — Mitigated by `docker compose logs -f {agent}` and NATS monitoring
  on port 8222.

### Decision

**D14: Containerisation** — Phase 2. Agents run in Docker containers for lifecycle
management (CAN bus registration pattern), concurrency (parallel builds via `--scale`),
and operational sanity (clean start/stop/logs per agent). The `nats-asyncio-service`
template produces Dockerfiles. Fleet compose in `nats-infrastructure` repo.

---

## How to Read the Rest

| Document | What It Covers | Where |
|----------|---------------|-------|
| **Fleet Master Index** | Technical inventory: all repos, agents, templates, build sequence, decisions D1-D38 | `forge/docs/research/ideas/fleet-master-index.md` |
| **Forge Pipeline Orchestrator Refresh** | Forge identity, confidence-gated checkpoints, tool inventory, NATS integration | `forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md` |
| **Specialist Agent Vision** | Three roles, three-layer architecture, phase sequence, unified harness, DDD demo | `specialist-agent/docs/research/ideas/architect-agent-vision.md` |
| **Jarvis Vision** | Intent router architecture, CAN bus registration, agent fleet details | `jarvis/docs/research/ideas/jarvis-vision.md` |
| **Landscape Research** | Devin, MetaGPT, ChatDev, CrewAI positioning, five differentiators | `specialist-agent/docs/research/ideas/landscape-conversation-starter.md` |
| **nats-core System Spec** | Message schemas, topic registry, fleet registration, BDD criteria | `nats-core/docs/design/specs/nats-core-system-spec.md` |
| **nats-infrastructure System Spec** | Server deployment, accounts, streams, fleet compose | `nats-infrastructure/docs/design/specs/nats-infrastructure-system-spec.md` |

---

## The Honest Bottom Line

We're building a system that might look different in 18 months as tools evolve. But the
methodology, the architectural patterns, the domain knowledge, and the skills gained from
building it are permanent. The repos are the evidence. The war stories are the content.
The journey is the product.

The worst case isn't that the tools change — it's that we stop learning. As long as we're
building, failing honestly, documenting what happened, and sharing the stories, every hour
invested produces durable value regardless of what the technology landscape does next.

---

*Updated: 12 April 2026*
*Previous version: April 2026 (pre-Phase 0/1 validation, pre-fine-tuning, pre-context-first delivery)*
