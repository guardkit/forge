# Review Report: TASK-REV-C3E7

## Executive Summary

**Verdict: 7/8 D90D findings resolved (1 auto-resolved in this review); specialist-agent lessons surface 14 actionable gaps in forge ideas docs, 6 of which are BLOCKERS for `/system-arch` and should be applied before forge starts shipping code against the build plan.**

The TASK-REV-D90D follow-through check found the April 16 alignment pass (FEAT-FVDA v2.2 commit) resolved 7 of 8 findings. Only **F4** (nats-core coverage 97% vs 98% inconsistency) was still open; this review auto-applied the bulk 97%→98% update across both primary and secondary ideas docs as a low-risk edit per user preference.

Layering the specialist-agent cross-agent lessons (TASK-REV-B8E4, series LES1) on top of the now-clean D90D baseline surfaces **14 parity gaps** in the three primary forge docs. These gaps cluster into six categories corresponding to the lessons doc's six parity surfaces. Of these:

- **6 BLOCKERS for `/system-arch`** — prerequisite additions and pre-merge smoke-test gates that must exist in the build plan before downstream commands consume it as context. Missing them would re-run specialist-agent's failure modes on forge.
- **5 MEDIUM-impact parity additions** — new decision-log entries, result-subject documentation, and fire-and-forget/status-companion tool annotations.
- **3 LOW-impact hygiene items** — .env hygiene rule, CLI redaction rule, "as of commit X" ADR annotation convention.

The forge docs are **structurally sound** — the identity, NATS-native stance, AgentManifest, and confidence-gated checkpoint protocol are all aligned with the lessons. The gaps are all **additive** (new pre-merge gates, new prerequisite bullets, new annotations) — no restructuring or ADR rewrites are needed.

**Anchor v2.2 integrity:** All proposed edits are purely additive annotations or prerequisite expansions. No cross-doc anchor references are invalidated.

---

## Review Details

- **Mode:** Architectural
- **Depth:** Standard
- **Focus:** All aspects (Transport, Provider, Packaging, Handler, Tooling, Ops)
- **Trade-off Priority:** Quality/reliability (pre-merge gate completeness)
- **Clarification preferences:** Report + auto-apply low-risk edits; full D90D verification first
- **Duration:** ~60 minutes
- **Documents Reviewed:** 3 primary + 3 secondary + 2 source inputs (lessons doc + D90D report)
- **Source lessons:** TASK-REV-B8E4, canonical round 1, series LES1

---

## Phase 1a — TASK-REV-D90D Action Verification

Disposition for each of the 8 findings in `.claude/reviews/TASK-REV-D90D-review-report.md`:

| # | Finding | Target | Disposition | Evidence |
|---|---------|--------|-------------|----------|
| F1 | Retired payloads listed as active at refresh line 53 | `forge-pipeline-orchestrator-refresh.md:53` | **RESOLVED** | Line 53 now shows `~~FeaturePlannedPayload~~`, `~~FeatureReadyForBuildPayload~~` with `(RETIRED — see correction 12 below)` inline annotation |
| F2 | Refresh-doc event-comparison table at line 602 includes retired range | `forge-pipeline-orchestrator-refresh.md:602` | **RESOLVED** | Line 602 now reads `BuildQueuedPayload through BuildCompletePayload per anchor v2.2 §7 (FeaturePlannedPayload and FeatureReadyForBuildPayload retired — see correction 12)` |
| F3 | D38 title references retired `feature_ready_for_build` | `fleet-master-index.md:645` | **RESOLVED** | D38 retitled to "Pipeline events replace kanban-triggered events"; body describes `pipeline.build-queued`, `StageCompletePayload`, `BuildQueuedPayload` as current mechanisms; explicitly notes `feature_ready_for_build` was itself retired |
| F4 | nats-core coverage figure inconsistency (97% vs 98%) | fleet-master-index.md (4 locs), refresh (4 locs), + 3 secondary docs | **RESOLVED (by this review)** | Build plan already at 98%; this review auto-applied 98% bulk update across remaining 16 citations in fleet-master-index.md, forge-pipeline-orchestrator-refresh.md, big-picture-vision-and-durability.md, conversation-starter-gap-analysis.md, forge-ideas-overhaul-conversation-starter.md. Verified zero remaining 97% refs under `docs/research/ideas/` |
| F5 | Stale coordination task files | `docs/research/ideas/TASK-update-build-plan-da15.md`, `TASK-update-fleet-index-d22.md` | **RESOLVED** | Both files deleted per `git status` (staged `D` entries) |
| F6 | PM Adapter phrasing at fleet-master-index line 142 | `fleet-master-index.md:140–146` | **RESOLVED** | Section retitled "PM Tools as Optional Visibility Adapters"; body now leads with "A future PM Adapter layer … is envisioned as" and explicitly states "it is not part of the current architecture" |
| F7 | Build plan `[~]` prerequisite wording | `forge-build-plan.md:33` | **RESOLVED** | Line 33 correctly shows `[~]` marker with "98% coverage" + explicit list of v2.2-critical payloads missing (`BuildQueuedPayload`, `BuildPausedPayload`, `BuildResumedPayload`, `StageCompletePayload`, `StageGatedPayload`) + anchor §7 reference. D90D said wording was "acceptable, no change needed" |
| F8 | Alignment-review outstanding items (cross-repo) | — | **RESOLVED (by declaration)** | No forge-doc action required; items tracked in nats-core (TASK-NCFA-001), specialist-agent, nats-infrastructure. D90D itself stated no forge changes needed |

**Blocker check:** The three D90D "Priority 1: Fix before `/system-arch`" items (F3, F1, F2) are all resolved. **No D90D blockers remain for `/system-arch`.**

Proceeding to lessons-layering passes.

---

## Phase 2 — Six Parity-Surface Pass

Each row walks the lessons doc's `§1`–`§7` rules against the three primary forge docs (`forge-build-plan.md`, `fleet-master-index.md`, `forge-pipeline-orchestrator-refresh.md`). The "forge" column of the lessons doc's parity-matrix at lines 231–238 of the lessons doc is the spine.

| # | Surface | Lessons rule (specialist-agent failure mode) | Forge doc disposition | Severity |
|---|---------|---------------------------------------------|------------------------|----------|
| S1 | **Transport — MCP stdio** | Banner on stderr; CWD absolute in wrapper (MCPB) | **N/A (for now)** — Forge is NATS-native per Do-Not-Reopen #5 (refresh line 679). No MCP surface at launch. Keep rule on-deck if forge later exposes `--transport stdio`. | N/A |
| S2 | **Transport — NATS fleet + JetStream** | Live subscription in prod lifecycle (CMDW); reply subjects distinct + documented per role; role-aware dispatch (PORT) | **PARTIAL — gaps G1, G2, G3** — Forge's own `pipeline.build-queued` consumer is described (fleet-master-index line 540–549, anchor §5.0). But: (a) no explicit pre-merge smoke-test gate that the consumer is subscribed in the **production image**, not just integration tests; (b) result subject convention (`agents.result.<id>`) is not documented in the refresh doc's Fleet Agent Tools table (line 390–397); (c) forge's dispatch to multi-role specialists is not framed as a `(role, command)` matrix that must be all-green. | HIGH |
| S3 | **Provider resolution + packaging** | Env-resolved at factory (PMEV/CRMV); `[providers]` extra lists every LangChain integration (LCOI); Dockerfile `pip install .[providers]` literal-match (DKRX); no real-looking keys in `.env` | **GAP — G4, G5, G6, G7** — Refresh line 539 pins `reasoning_model: "claude-sonnet-4-20250514"` and line 79 states forge "uses a strong reasoning model (Claude, Gemini)" (multi-provider-capable). But: (a) `[providers]` extras convention is not declared in the build plan's `pyproject.toml` row (line 380); (b) the "resolve at factory" rule is not stated; (c) no `.env` hygiene bullet; (d) no Dockerfile row in "Files That Will Change" — deferred but no pre-merge gate for when it arrives. | HIGH |
| S4 | **Handler completeness** | Every listed mode/command has a method at every layer: tool-schema → adapter → core API → orchestrator (ARFS) | **GAP — G8** — FEAT-FORGE-007 covers end-to-end integration, but the build plan has no pre-merge gate that each of the four AgentManifest tools (`forge_greenfield`, `forge_feature`, `forge_review_fix`, `forge_status`) has a runnable handler at every layer. ARFS's specific failure mode — a tool listed in the manifest but missing in the orchestrator — is a latent risk until an explicit matrix gate is added. | MEDIUM |
| S5 | **Tooling — long-running tools** | Fire-and-forget + poll for any tool >30s (POLR); tool description must match implementation; separate fast sync paths from gen-loop paths | **PARTIAL — G9, G10** — Manifest correctly marks `forge_greenfield/feature/review_fix` as `async_mode: true` and `forge_status` as `async_mode: false, read_only` (build plan line 436–451). Good start. But: (a) the descriptions ("Run full greenfield pipeline from raw input to deployed code") don't say "returns session_id immediately"; (b) no `forge_cancel` companion tool listed in the manifest (CLI has `forge cancel` per line 115, but agent manifest doesn't); (c) no explicit cite of POLR pattern. Forge pipeline runs up to 60 min (`max_task_timeout_seconds: 3600`, refresh line 555) — far beyond the 240s MCP timeout and the 30s threshold. | MEDIUM |
| S6 | **Ops hygiene** | `docker compose down --remove-orphans` documented (ORPH); fresh-volume provisioning required not optional (PRVS/NI-PSBUG); envsubst restart documented; CLI redaction for secrets | **GAP — G11, G12, G13, G14** — Build plan line 34–36 hard-prereq is "nats-infrastructure running on GB10 — `docker compose up -d` executed and verified" which is **insufficient** under PRVS lesson (fresh volume → streams/KV not auto-provisioned; this was exactly the failure mode behind a MacBook retest iteration). Also: no orphan-container callout for Conductor worktree flows; no `.env` hygiene rule; no CLI-output redaction rule. | HIGH |

**Surface summary:** 0/6 surfaces are fully covered; 2/6 are N/A or PARTIAL-OK; 4/6 need pre-merge additions. The gaps cluster in Transport (S2), Provider/Packaging (S3), and Ops (S6) — exactly the surfaces where specialist-agent lost 4+ iterations.

---

## Phase 3 — Evidence Pointer Disposition

Each cited evidence id gets an explicit disposition for forge. Maps to the 14 gaps enumerated in Phase 4.

| # | Evidence id | Rule | Forge disposition | Maps to |
|---|-------------|------|-------------------|---------|
| 1 | **TASK-MDF-CMDW** | NATS commands need live subscription in production | **GAP** — forge's own `pipeline.build-queued` consumer needs an explicit pre-merge smoke test on the production image | G1 |
| 2 | **TASK-MDF-PMEV** | MCP handlers derive player_model from `AGENT_MODELS__REASONING_MODEL` | **GAP** — same principle applies to forge's orchestration reasoning model factory (resolve from env, not hard-code) | G5 |
| 3 | **TASK-MDF-ARFS** | Every listed mode/command has a method at every layer | **GAP** — forge's four agent tools need end-to-end hop tests | G8 |
| 4 | **TASK-MDF-MCPB** | MCP stdio banner → stderr | **N/A** — forge is NATS-only at launch. Re-evaluate if MCP surface is added later (`specialist-agent` pattern recommended: gate splits banner by transport mode) | (S1, N/A) |
| 5 | **TASK-MDF-PORT** | Role-aware dispatch tables in multi-role agents | **GAP (modified)** — forge is single-role but **consumes** multi-role specialists (architect, product-owner, ideation); needs a `(specialist_role, stage)` dispatch matrix test | G3 |
| 6 | **TASK-MDF-POLR** | Fire-and-forget + poll for tool calls >30s | **GAP** — forge's async tools need session_id / `_status` / `_cancel` companions documented in manifest | G9 |
| 7 | **TASK-MDF-ORPH** | `docker compose down --remove-orphans` | **GAP** — forge uses Conductor worktree flows (implied by `.claude/settings.json` conventions); orphan pitfall applies when worktrees are torn down | G12 |
| 8 | **TASK-MDF-PRVS** | Fresh-volume NATS needs explicit stream + KV provisioning | **GAP** — forge's prereq list line 34–36 assumes `docker compose up -d` is sufficient; it isn't. `provision-streams.sh` + `provision-kv.sh` must be required, not implicit | G11 |
| 9 | **TASK-NI-PSBUG** | `ttl_opts[@]: unbound variable` under `set -u` in provision-streams.sh | **N/A (for forge edits)** — script bug lives in nats-infrastructure repo. Forge's prereq should reference "provisioning successfully completed" as the gate, and should note that provisioning scripts may need patching on unset-var-strict shells | (G11 addendum) |
| — | **CRMV** (retest commit `8b9d584`) | `command_router.py._default_player_model()` — PMEV parity on NATS path | Mapped into G5 (PMEV applies to all transports) | G5 |
| — | **LCOI** (retest commit `8b9d584`) | `langchain-openai` added to `[providers]` extra | Maps to G4 (`[providers]` extras convention) | G4 |
| — | **DKRX** (retest commit `8b9d584`) | Dockerfile installs `.[providers]` not `.` | Maps to G7 (Dockerfile extras gate for when forge gets a Dockerfile) | G7 |

**Evidence coverage:** 9/9 cited ids have explicit dispositions (7 GAP, 2 N/A). Zero unmapped.

---

## Phase 4 — Per-Agent Checklist Pass (forge column)

Walking the 22-row per-agent checklist at lessons doc lines 256–279 against the three primary forge docs. "forge" column ticks are the target; this table records current-state disposition.

| # | Item | Forge tick in lessons | Current state | Gap? |
|--:|------|:---:|---------------|:---:|
| 1 | MCP `serve --transport stdio` banner on stderr + stream-split test | ✅ | N/A at launch (NATS-only); gate on-deck for future MCP surface | —*¹ |
| 2 | Bash MCP wrapper `cd`s to absolute path before `exec` | ✅ | N/A at launch; same as #1 | —*¹ |
| 3 | `serve-nats` subscribes in production lifecycle (not tests) | ✅ | Build plan prereq line 42–44 covers "at least one specialist agent NATS-callable" but no pre-merge gate for forge's **own** consumer in the prod image | **G1** |
| 4 | `(role, command)` dispatch matrix green for every declared role | ✅ | Forge is single-role; but dispatch to multi-role specialists is not framed as a required green matrix | **G3** |
| 5 | Reply subject `agents.result.<id>` documented; smoke test subscribes before publishing | ✅ | Not documented in refresh doc Fleet Agent Tools table (line 390–397) | **G2** |
| 6 | `AGENT_MODELS__REASONING_MODEL` resolved at model factory in every transport | ✅ | Refresh line 49 mentions `AGENT_` prefix but "resolve at factory" rule not stated | **G5** |
| 7 | `[providers]` extra lists every LangChain integration | ✅ | Build plan `pyproject.toml` row (line 380) lists only `nats-core, pydantic, pydantic-settings` — no `[providers]` pattern | **G4** |
| 8 | Dockerfile `pip install .[providers]` literal-match to guide | ✅ | Forge has no Dockerfile row in "Files That Will Change"; deferred with no gate | **G7** |
| 9 | No real-looking provider keys in `.env`; `.env.example` only | ✅ | No `.env.example` row in build plan files table | **G13** |
| 10 | Tools >30s return `session_id` immediately + expose `_status`/`_cancel` | ✅ (forge footnote ¹ — confirm 30s threshold) | Manifest tools are `async_mode: true` (correct) but no `_status`/`_cancel` companions listed; descriptions don't say "returns session_id" | **G9** |
| 11 | Every listed mode/command has method at every layer | ✅ | FEAT-FORGE-007 integration test exists; no explicit per-tool hop-matrix gate | **G8** |
| 12 | `docker compose down --remove-orphans` documented; orphan pitfall called out | ✅ | Not mentioned in build plan or refresh | **G12** |
| 13 | NATS stream + KV provisioning required (not optional) in guide §2 | ✅ | Build plan prereq line 34–36 says only "docker compose up -d executed" — insufficient | **G11** |
| 14 | Guide copy-paste blocks live-verified on a clean machine before canonical freeze | ✅ | No pre-canonical-freeze verification gate in build plan | **G6** |
| 15 | ADRs annotated with "as of commit X"; re-verified at each walkthrough | ✅ | Convention not called out in forge docs (anchor v2.2 ADRs are at SP-* ids, no commit annotations) | **G10** |
| 16 | envsubst restart documented | ✅ | nats-infrastructure's concern; forge inherits but should cite provisioning dependency | (G11) |
| 17 | CLI introspection tools support redaction or `--quiet` | ✅ | Forge's `forge queue/status/history/cancel` CLI has no redaction rule documented | **G14** |
| 18 | Tool description matches implementation | ✅ | Manifest descriptions are "Run full greenfield pipeline…" — no mention of fire-and-forget / session_id | **G9** (dup) |
| 19 | Latency classification done | ✅ | async_mode split exists but POLR pattern not cited as applying | (S5 partial) |
| 20 | `notifications/cancelled` handled server-side | ✅ | MCP-specific; N/A at launch for NATS-only forge | —*¹ |
| 21 | Accumulated-latency surfaces treated as long-running | — (forge footnote ¹) | Forge pipeline IS long-running; covered by async_mode:true | — (OK) |
| 22 | Single-transport stream-split test | — | Specific to study-tutor; N/A for forge | — (N/A) |

**Footnote ¹:** Lessons doc notes "**forge** — confirm the 30s threshold still applies for your role's workloads; if forge's PO-equivalent generation loops are shorter or different, re-scope rows 10, 19, 21 accordingly. Keep the rule; tune the threshold." Forge's 60-min `max_task_timeout_seconds` confirms the rule applies with high margin.

**Checklist tally:** Of the 22 items, 14 are relevant to forge at launch. Of those 14: 0 fully met in docs, 11 flagged as gaps (G1–G14 consolidated), 2 partially met (S5), 1 implicit (item 21).

---

## Consolidated Findings — 14 Gaps

Full list of gaps, each mapped to target doc + line + severity + proposed fix.

### BLOCKERS for `/system-arch` (6 items — HIGH severity)

### GAP-1 · Forge `pipeline.build-queued` consumer needs prod-image smoke-test gate

- **Target:** `forge-build-plan.md` Step 6 (line 308–331)
- **Lesson source:** TASK-MDF-CMDW ("Integration tests pass" ≠ "works in production")
- **Gap:** Validation steps test that forge can `forge queue FEAT-TEST-001`, but don't explicitly verify the JetStream pull consumer is subscribed inside the production image (as opposed to the dev venv). CMDW was exactly this failure mode on specialist-agent.
- **Proposed fix:** Add a Step 6 validation bullet: "**Production-image CMDW gate:** Build the production container, run `forge serve` inside it, publish one `pipeline.build-queued` message from outside the container, verify the subscribed consumer delivers it to a real run. Stale container builds will silently fail to subscribe — this is the specialist-agent CMDW lesson applied to forge."
- **Severity:** HIGH (blocks `/system-arch` consuming the build plan as canonical context)

### GAP-2 · Result-subject convention undocumented in Fleet Agent Tools table

- **Target:** `forge-pipeline-orchestrator-refresh.md` §"Fleet Agent Tools (via NATS)" table at line 390–397
- **Lesson source:** Walkthrough §retest-smoke — `agents.result.<role>` is where real replies land; confusion with `agents.>` JetStream intercept caused operator misreading of PubAck as success
- **Gap:** The Fleet Agent Tools table lists tool → target agent → NATS method → input → output but doesn't call out the reply-subject pattern (`agents.result.<agent_id>`) that `call_agent_tool()` uses internally. A reader of the refresh doc cannot tell from the table where real replies arrive vs where PubAck-only acks arrive.
- **Proposed fix:** Add a paragraph above the table: "Reply subject convention: `call_agent_tool()` publishes to `agents.command.<agent_id>` and subscribes to `agents.result.<agent_id>.<correlation_id>` for the real reply. The JetStream AGENTS stream intercepts `agents.>` and returns PubAck within ~3ms — **do not treat PubAck as success**. See nats-core `NATSClient.call_agent_tool()` semantics."
- **Severity:** HIGH (future `/system-arch` context would bake in the misreading if not annotated)

### GAP-3 · Specialist-dispatch `(role, stage)` matrix is not a required green gate

- **Target:** `forge-build-plan.md` Step 6 validation (line 308–331) + `forge-pipeline-orchestrator-refresh.md` §"Fleet Agent Tools" table
- **Lesson source:** TASK-MDF-PORT (multi-role dispatch must be tested per-pair) — modified for forge (single-role agent consuming multi-role specialists)
- **Gap:** Forge calls specialist-agent with three distinct roles (architect, product-owner, ideation). Per the refresh doc Mode A flow (line 296–355), the pipeline exercises `delegate_product_docs` → `delegate_architecture` → `check_feasibility`. But there's no pre-merge gate that each `(specialist_role, forge_stage)` pair has been smoke-tested against the production specialist-agent image. This is the PORT lesson applied at the consumer side: specialist-agent's PORT bug meant the PO role's handlers were never registered; forge wouldn't detect this until integration time.
- **Proposed fix:** Add to Step 6 validation: "**Specialist dispatch matrix gate:** For each `(role ∈ {product-owner, architect}, stage)` pair used in Mode A, execute one end-to-end round-trip via NATS on the production specialist-agent image. A failure on any pair is a hard stop before `/system-arch` — this is the PORT lesson applied to the consumer side."
- **Severity:** HIGH

### GAP-11 · Fresh-volume provisioning prerequisite is insufficient

- **Target:** `forge-build-plan.md` Prerequisites line 34–36
- **Lesson source:** TASK-MDF-PRVS + TASK-NI-PSBUG (fresh-volume NATS is not self-healing; `verify-nats.sh` is read-only)
- **Gap:** Current prereq: "nats-infrastructure running on GB10 — NATS server up, JetStream enabled, accounts configured, `docker compose up -d` executed and verified". This is **insufficient** — a fresh volume under `docker compose up -d` has no streams and no KV buckets. `pipeline.build-queued` publishing would succeed with a PubAck but no stream retention; fleet registry lookups via `NATSKVManifestRegistry` would fail.
- **Proposed fix:** Replace line 35–36 with: "- [ ] **nats-infrastructure running on GB10** — NATS server up, JetStream enabled, accounts configured, `docker compose up -d` executed **and provisioning scripts run**: `provision-streams.sh` (creates AGENTS + PIPELINE + SYSTEM streams per anchor v2.2) and `provision-kv.sh` (creates `agent-registry`, `pipeline-state`, and other KV buckets). `verify-nats.sh` is read-only and does not self-heal. Per TASK-MDF-PRVS/NI-PSBUG: scripts may require `set +u` in specific shells until ttl_opts unset-var bug is patched upstream."
- **Severity:** HIGH (forge's first real run will silently misbehave without provisioning — exactly the specialist-agent MacBook failure mode)

### GAP-12 · Orphan-container pitfall uncalled in Conductor worktree context

- **Target:** `forge-build-plan.md` Risks & Mitigations table (line 513–521) or Step 6 validation
- **Lesson source:** TASK-MDF-ORPH (`docker compose down` is silent no-op when labels point at deleted worktree)
- **Gap:** Forge uses parallel wave execution (build plan line 284–305 describes 6 waves). If waves are run through Conductor worktrees (as is convention for parallel-capable tasks), `docker compose down` in a deleted worktree leaves orphan containers that confuse subsequent waves.
- **Proposed fix:** Add to Risks & Mitigations: "| Orphan containers from parallel wave execution | Deleted Conductor worktrees leave Docker containers behind, causing subsequent-wave port conflicts | Always use `docker compose down --remove-orphans`; document in each wave's cleanup. This is TASK-MDF-ORPH applied to parallel build flows. |"
- **Severity:** HIGH (only reveals itself on second real run; would have been caught during specialist-agent MacBook walkthrough if orphan-aware flow had been documented)

### GAP-6 · Guide copy-paste blocks must be live-verified before canonical freeze

- **Target:** `forge-build-plan.md` new "Canonical freeze gate" section (post-Step 7)
- **Lesson source:** "Guide copy-paste blocks are code; CI-test them or treat them as such" (lessons doc §8 ¶1)
- **Gap:** The build plan contains dozens of shell blocks (guardkit invocations, docker commands, pytest). No pre-canonical-freeze gate that these have been run on a clean machine. Specialist-agent MacBook walkthrough found multiple `cd /tmp` workarounds, wrong Python version pins, and omitted provisioning steps — all in a CI-passing guide.
- **Proposed fix:** Add a short section to the build plan: "**Before canonical-freeze (after Step 7):** All shell blocks in this document MUST have been executed verbatim on a clean MacBook + GB10 in a single walkthrough session and logged in `command-history.md`. Annotate any block that required workarounds with `[as of commit X]`. This is the lessons doc §8 rule applied."
- **Severity:** HIGH (prevents repeating the specialist-agent canonical-guide-breaks-on-clean-machine failure)

### MEDIUM additions (5 items)

### GAP-4 · `[providers]` extras convention not declared in pyproject entry

- **Target:** `forge-build-plan.md` "Files That Will Change" line 380 + a prereq-adjacent rule
- **Lesson source:** LCOI + DKRX retest commit `8b9d584`
- **Gap:** Build plan pyproject row lists `nats-core, pydantic, pydantic-settings` but not an `[providers]` extra even though refresh line 79 anticipates multi-provider reasoning model.
- **Proposed fix:** Update line 380 to: "`pyproject.toml` | 001 | Create — core deps: nats-core, pydantic, pydantic-settings. **`[providers]` extra must list every LangChain integration named anywhere in src/** (langchain-anthropic, langchain-openai if used). Per LCOI lesson: transitive pulls by deepagents do **not** cover every declared provider — each must be explicit."
- **Severity:** MEDIUM

### GAP-5 · "Resolve provider at factory" rule not stated

- **Target:** `forge-pipeline-orchestrator-refresh.md` §"AgentConfig" around line 535–557
- **Lesson source:** TASK-MDF-PMEV + `command_router.py._default_player_model()` (CRMV parity)
- **Gap:** Refresh line 49 says `AgentConfig` uses `AGENT_` env prefix. But the rule — **resolve model from env at the factory that instantiates the chat model, not at the handler** — is not stated. This is the single most expensive specialist-agent bug (same fix in two places), and forge inherits the same shape.
- **Proposed fix:** Add a bullet at the top of the AgentConfig section: "**Provider-resolution rule:** `reasoning_model` must be resolved from `AGENT_MODELS__REASONING_MODEL` (via `AgentConfig`) at the lowest layer that instantiates the chat model — not in each handler. See TASK-MDF-PMEV / CRMV parity patch in `specialist-agent` commit `8b9d584`: the same bug was fixed twice because handlers were re-computing defaults. Forge's `call_agent_tool()` results pass through its own factory; factory must not hard-code the default."
- **Severity:** MEDIUM

### GAP-8 · Handler completeness matrix per manifest tool

- **Target:** `forge-build-plan.md` Step 6 validation
- **Lesson source:** TASK-MDF-ARFS (tool listed → adapter present → core method missing)
- **Gap:** Manifest declares four tools (`forge_greenfield`, `forge_feature`, `forge_review_fix`, `forge_status`) but no pre-merge gate that each traces end-to-end through tool-schema → NATS adapter → core API → orchestrator method. ARFS proved that unit tests don't catch this — the schema and adapter are wired, the core method is missing, and the dispatcher silently rejects.
- **Proposed fix:** Add to Step 6 validation: "**Handler completeness gate:** For each tool in the forge AgentManifest, walk the full `tool-schema → NATS adapter handler → core API → orchestrator method` chain and execute one smoke-test round-trip. Mark any hop with a TODO/NotImplementedError as a blocker before merge."
- **Severity:** MEDIUM

### GAP-9 · Fire-and-forget companion tools missing from manifest

- **Target:** `forge-pipeline-orchestrator-refresh.md` manifest at line 511–528 + `forge-build-plan.md` manifest at line 424–451
- **Lesson source:** TASK-MDF-POLR (sync `await` on long loops; fire-and-forget + `_status`/`_cancel` companions required for tool calls >30s)
- **Gap:** `forge_greenfield/feature/review_fix` are correctly marked `async_mode: true`, but: (a) descriptions don't say "returns session_id / pipeline_id immediately"; (b) no `forge_cancel` companion tool in manifest (only `forge_status`); (c) POLR pattern not cited. Forge pipelines run ~60 min — far beyond the Claude Desktop 240s MCP timeout and the 30s threshold. Jarvis or any future MCP wrapper will timeout unless fire-and-forget is explicit.
- **Proposed fix:** Update manifest entries:
  - `forge_greenfield` description: "Start a greenfield pipeline run. **Returns `pipeline_id` immediately** — poll `forge_status(pipeline_id)` for progress; cancel with `forge_cancel(pipeline_id)`. Per TASK-MDF-POLR: tools exceeding 30s must use fire-and-forget."
  - Add new tool entry `forge_cancel` with `risk_level: mutating`, `async_mode: false`.
  - Apply same description pattern to `forge_feature` and `forge_review_fix`.
- **Severity:** MEDIUM

### GAP-10 · "As of commit X" annotation convention for forge ADRs

- **Target:** `forge-build-plan.md` new bullet in Step 1 /system-arch validation (line 150–157)
- **Lesson source:** "Decision records must be revalidated at each major milestone" (lessons doc §8 ¶2); TASK-DEPG-A3F2 Decision #2 overstated parity claim
- **Gap:** Anchor v2.2 ADRs (SP-010..017) don't carry "as of commit X" annotations. Specialist-agent's Decision #2 ("near-full parity" claim) was overstated when written and surfaced as overstated only at live retest. Forge's first /system-arch run will produce ADR-FORGE-001+ — baking the annotation convention into Step 1 validation avoids the same trap.
- **Proposed fix:** Add to Step 1 Validation (line 150–157): "Each ADR produced carries a trailer: `**Decision facts as of commit:** <sha>` — to be re-verified at each subsequent walkthrough (per lessons doc §8)."
- **Severity:** MEDIUM (convention additions cost ~1 line per ADR, prevent overstated-parity recurrence)

### LOW hygiene additions (3 items)

### GAP-7 · Dockerfile extras gate for when forge gets a Dockerfile

- **Target:** `forge-build-plan.md` "Files That Will Change" table — new row
- **Lesson source:** DKRX (Dockerfile `pip install .` must literal-match `pip install .[providers]` from the guide)
- **Gap:** Forge has no Dockerfile planned in Files That Will Change. Forge will eventually be containerized (it's a fleet member, convention is containerization). When that row is added, the DKRX lesson says the `pip install` line must include the same extras as the documented venv install.
- **Proposed fix:** Add a deferred row: "`Dockerfile` | (deferred, FEAT-FORGE-009+) | Create — **when added, must use `pip install .[providers]` matching the documented venv install per DKRX lesson**. Dockerfile extras ≡ guide extras: literal-match required."
- **Severity:** LOW (deferred; gate when created)

### GAP-13 · `.env` hygiene rule

- **Target:** `forge-build-plan.md` "Files That Will Change" table — new row + rule
- **Lesson source:** Retest §env — `OPENAI_API_KEY=not_needed` placeholder in `.env` silently overrode operator's real shell-env key
- **Gap:** Forge will ship a `.env.example` for local dev. No rule in build plan that `.env` never contains real-looking keys.
- **Proposed fix:** Add row: "`.env.example` | 001 | Create — **never ship real-looking provider keys**. Per retest-env lesson: placeholder values like `not_needed` can silently override shell-env real values through Compose `${VAR}` interpolation. Pre-merge gate: CI check scanning `.env*` files for pattern `[A-Z_]+_API_KEY=[a-zA-Z0-9-]{20,}` fails the build."
- **Severity:** LOW (prophylactic; cost of miss is latent 401 errors in production)

### GAP-14 · CLI output redaction rule

- **Target:** `forge-build-plan.md` Risks & Mitigations table
- **Lesson source:** Lessons doc §2 ¶4 + §7 ¶4 (CLI introspection tools can print credentials in plaintext; `nats account info` leaked `RICH_NATS_PASSWORD`)
- **Gap:** Forge CLI commands (`forge queue`, `forge status`, `forge history`, `forge cancel`, `forge skip`) may render NATS URLs or registry state containing embedded credentials. No redaction rule documented.
- **Proposed fix:** Add to Risks & Mitigations: "| CLI credential leakage | `forge status`/`history` could print NATS URLs with credentials or log NATS KV entries containing secrets | Per lessons doc §7: CLI outputs must support redaction or `--quiet`. Default: redact any value matching `nats://[^@]+@.*` or `*_PASSWORD=.*` to `***`; `--verbose` opts in to plaintext with a warning. |"
- **Severity:** LOW

---

## Per-Doc Edit List (Decision-Gated)

What would change in each primary doc if `[I]mplement` is chosen. Edits applied by this review (auto-apply branch) are noted separately below.

### `forge-build-plan.md`

| Gap | Location | Edit type |
|-----|----------|-----------|
| G1 | Step 6 validation (line 308–331) | **ADD** CMDW prod-image smoke-test gate |
| G3 | Step 6 validation | **ADD** `(role, stage)` specialist-dispatch matrix gate |
| G4 | pyproject row (line 380) | **ANNOTATE** with `[providers]` extras rule |
| G6 | Post-Step-7 section | **ADD** canonical-freeze live-verification gate |
| G7 | Files That Will Change | **ADD** deferred Dockerfile row with DKRX literal-match rule |
| G8 | Step 6 validation | **ADD** handler-completeness matrix gate |
| G10 | Step 1 validation (line 150–157) | **ADD** "as of commit X" ADR annotation convention |
| G11 | Prerequisites line 34–36 | **REPLACE** `docker compose up -d` bullet with provisioning-required wording |
| G12 | Risks & Mitigations (line 513–521) | **ADD** orphan-container row |
| G13 | Files That Will Change | **ADD** `.env.example` row with hygiene rule |
| G14 | Risks & Mitigations | **ADD** CLI redaction row |

**Total edits: 11 (7 adds, 2 annotates, 1 replace, 1 additional deferred row)**

### `forge-pipeline-orchestrator-refresh.md`

| Gap | Location | Edit type |
|-----|----------|-----------|
| G2 | §"Fleet Agent Tools (via NATS)" above line 390 | **ADD** result-subject convention paragraph |
| G5 | §"AgentConfig (using nats-core schema)" line 535 | **ADD** provider-resolution-at-factory bullet |
| G9 | §"Forge Agent Identity" manifest (line 511–528) | **ANNOTATE** three mutating tool descriptions; **ADD** `forge_cancel` tool |

**Total edits: 3 (2 adds, 1 annotate-block)**

### `fleet-master-index.md`

No new edits proposed beyond the F4 bulk-update already applied. The fleet-master-index is currently well-aligned. Optionally, if Implement is chosen, one cross-reference bullet can be added to the D33–D38 block pointing readers to `forge-build-plan.md` for the lessons-layered pre-merge gates. Marked **optional** — not a blocker.

### Secondary docs (scan-only scope)

- `big-picture-vision-and-durability.md` — F4 coverage fix applied (1 edit). No other gaps surfaced.
- `conversation-starter-gap-analysis.md` — F4 coverage fix applied (5 edits). No other gaps surfaced.
- `forge-ideas-overhaul-conversation-starter.md` — F4 coverage fix applied (3 edits). No other gaps surfaced.
- `architect-agent-finproxy-build-plan.md` — SUPERSEDED; not touched.

---

## Edits Applied by This Review (Low-Risk Auto-Apply)

Per user preference "Report + auto-apply low-risk edits" (typo-level, citation additions, anchor preservation), the following edits have been applied in-place. Anchor v2.2 structure preserved — no ADR content, decision-log entries, or cross-doc anchor references modified.

| # | File | Line(s) | Change | Basis |
|---|------|---------|--------|-------|
| 1 | `fleet-master-index.md` | 170 | `97% test coverage` → `98% test coverage` (repo-map status bullet) | D90D F4 |
| 2 | `fleet-master-index.md` | 401 | `✅ 97% coverage` → `✅ 98% coverage` (Infrastructure Components table) | D90D F4 |
| 3 | `fleet-master-index.md` | 562 | `97% test coverage, 17 test files, 6 features implemented` → `98% test coverage, …` (Proof Points table) | D90D F4 |
| 4 | `fleet-master-index.md` | 682 | `✅ Implemented — 97% test coverage` → `✅ Implemented — 98% test coverage` (Build Sequence block) | D90D F4 |
| 5 | `forge-pipeline-orchestrator-refresh.md` | 7 | `nats-core (97% coverage, implemented)` → `98% coverage` (header) | D90D F4 |
| 6 | `forge-pipeline-orchestrator-refresh.md` | 43 | `working code at 97% test coverage across 17 test files` → `98% test coverage` | D90D F4 |
| 7 | `forge-pipeline-orchestrator-refresh.md` | 610 | `nats-core at 97% coverage, nats-infrastructure configured` → `98% coverage` | D90D F4 |
| 8 | `forge-pipeline-orchestrator-refresh.md` | 649 | `nats-core … (97% coverage, implemented)` → `98% coverage` | D90D F4 |
| 9 | `big-picture-vision-and-durability.md` | 96 | `nats-core at 97% test coverage` → `98% test coverage` | D90D F4 |
| 10 | `conversation-starter-gap-analysis.md` | 98 | `nats-core: 97% test coverage` → `98% test coverage` | D90D F4 |
| 11 | `conversation-starter-gap-analysis.md` | 175, 225, 267, 298 | `97% coverage` → `98% coverage` (4 occurrences) | D90D F4 |
| 12 | `forge-ideas-overhaul-conversation-starter.md` | 52 | `97% test coverage, 6 features` → `98% test coverage, 6 features` | D90D F4 |
| 13 | `forge-ideas-overhaul-conversation-starter.md` | 85, 247 | `(97% coverage)` → `(98% coverage)` (2 occurrences) | D90D F4 |

**Total low-risk edits applied: 17 citation corrections across 5 files, resolving D90D F4.**

**Verification:** `Grep -r '97% (test )?coverage' docs/research/ideas` returns zero matches after edits.

**Scope discipline:** No anchor v2.2 cross-doc references touched. No ADR content modified. No new content added — purely citation corrections of a single figure already validated by TASK-REV-A1F2's live pytest execution.

---

## Architecture Score: 75/100

| Criterion | Score | Notes |
|-----------|-------|-------|
| D90D follow-through | 10/10 | 7/8 resolved by prior commits; F4 resolved by this review |
| Transport parity (S1 + S2) | 6/10 | Forge NATS-native correctly stated; S1 N/A; S2 has 3 gaps (G1/G2/G3) — all HIGH severity |
| Provider & packaging parity (S3) | 5/10 | Multi-provider anticipated but no extras/factory/env-hygiene rules documented (G4/G5/G7/G13) |
| Handler completeness (S4) | 7/10 | FEAT-FORGE-007 integration exists; per-tool hop gate missing (G8) |
| Tooling parity (S5) | 7/10 | async_mode split correctly done; fire-and-forget companions missing (G9/G18) |
| Ops hygiene (S6) | 4/10 | Provisioning prereq wrong (G11); orphan/env/CLI-redaction unstated (G12/G13/G14) |
| Pre-canonical-freeze discipline | 5/10 | No live-verification gate (G6) or "as of commit X" convention (G10) |
| Anchor v2.2 integrity | 10/10 | No existing anchors broken; all proposed edits are additive |
| Cross-doc consistency | 9/10 | F4 coverage reconciled; D38 + retirement annotations all aligned |

Score rationale: docs are **structurally aligned** with the lessons (identity, NATS-native stance, confidence-gated protocol, manifest shape) — but the **pre-merge gate inventory is thin**, which is where specialist-agent lost 4+ walkthrough iterations. Closing the 6 BLOCKERS lifts the score to ~88/100; closing all 14 to ~94/100.

---

## Decision Checkpoint

**Review produced:** 1 Phase 1a verification table (8/8 D90D dispositions), 1 parity-surface table (6/6 dispositions), 1 evidence-pointer table (9/9 + 3 retest-commit mappings), 1 per-agent checklist table (22/22 dispositions), 14 consolidated gap findings with proposed fixes, per-doc edit lists, and 17 low-risk edits already applied in-place.

**Options:**

- **[A]ccept** — Approve findings as-is. Low-risk F4 edits remain applied; the 14 gap recommendations are recorded in this report but not applied. Task moves to `review_complete` / `completed`. Use this if you want to triage fixes downstream (e.g. via `/task-create` follow-ups) rather than inline.

- **[R]evise** — Request deeper analysis. Options: (i) go comprehensive on any single surface (e.g. full Transport audit); (ii) walk walkthrough log for additional evidence on specific gaps; (iii) audit secondary docs more thoroughly; (iv) run a second pass after external input. Tell me which surface(s) to deepen.

- **[I]mplement** — Apply the 14 proposed edits in-place in forge ideas docs. This turns recommendations into doc edits, preserving anchor v2.2 structure. Estimated 11 edits to `forge-build-plan.md`, 3 edits to `forge-pipeline-orchestrator-refresh.md`, 0 required edits to `fleet-master-index.md` (1 optional cross-reference). Zero restructuring; all additive annotations/bullets.

- **[C]ancel** — Discard review. Low-risk F4 edits already committed to the working tree would remain (they reify D90D F4, which was open). If you want F4 reverted too, tell me.

**Recommendation:** **[I]mplement**. The 6 BLOCKERS materially affect what `/system-arch` will consume as context. Deferring them risks baking the specialist-agent MacBook failure modes into forge's architecture output — exactly the failure class this task was created to prevent. The edits are small (~14 surgical annotations), purely additive, and each has a cited evidence trail back to a specialist-agent TASK-MDF-* id.

---

*Review completed: 18 April 2026*
*Reviewer: /task-review (architectural mode, standard depth)*
*Lessons source: specialist-agent TASK-REV-B8E4, series LES1*
*Prior review verified: TASK-REV-D90D (7/8 resolved + F4 auto-applied)*
