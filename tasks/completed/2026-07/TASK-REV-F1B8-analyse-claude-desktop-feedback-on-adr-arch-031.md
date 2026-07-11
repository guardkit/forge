---
id: TASK-REV-F1B8
title: Analyse Claude Desktop feedback proposing ADR-ARCH-031 (AsyncSubAgent vs sync task() amendment to ADR-ARCH-020)
status: review_complete
created: 2026-04-19T00:00:00Z
updated: 2026-04-19T00:00:00Z
priority: high
tags: [architecture-review, decision-point, adr, deepagents, async, subagent-composition, amendment]
complexity: 4
task_type: review
decision_required: true
review_mode: architectural
review_depth: standard
source_feedback:
  origin: claude-desktop
  received: 2026-04-19
  verbatim: |
    A small update to Forge. Add a new short ADR — call it ADR-ARCH-031 —
    that says: "Long-running subagents use AsyncSubAgent; short-lived
    delegations use sync task(). autobuild_runner is async;
    build_plan_composer is sync." This is a light-touch amendment to
    ADR-ARCH-020, not a rewrite. It gives you cancellation, mid-flight
    steering, and meaningful forge status narrative without disturbing
    the 2-pre-declared subagents shape.
  supporting_context:
    - path: docs/research/ideas/conversation-capture-2026-04-19-fleet-v3-framing.md
      role: claude-desktop-session-reasoning
      key_lines: [50, 56, 61, 69, 243, 260, 310]
      summary: |
        Session-level conversation capture containing the "why" behind
        the amendment. Line 69 records the async-vs-sync split rationale
        (autobuild_runner async because long-running + non-blocking
        supervisor; build_plan_composer sync because bounded + output
        gates next stage). Line 243 originates the ADR name. Line 260
        flags a follow-up to reference ADR-ARCH-031 in ARCHITECTURE.md
        Decision Index §13 (out of scope for this review).
review_results:
  score: 78
  findings_count: 8
  recommendations_count: 5
  decision: implement
  report_path: .claude/reviews/TASK-REV-F1B8-review-report.md
  completed_at: 2026-04-19T00:00:00Z
  artefacts_applied:
    - "ADR-ARCH-020: amendment annotation appended pointing to ADR-ARCH-031"
    - "ADR-ARCH-031: 'Interaction with ADR-ARCH-021 (interrupt())' subsection added before Crash recovery"
    - "ADR-ARCH-031: References section extended with ADR-ARCH-002/-007/-008/-021 cross-references"
  follow_up_tasks:
    - TASK-DOC-B2A4  # Add ADR-ARCH-031 to ARCHITECTURE.md Decision Index §13
  artefacts_deferred:
    - "Length pruning of ADR-ARCH-031 (127 -> 60-80 lines) — optional; left as-is pending explicit trim request"
test_results:
  status: not_applicable
  coverage: null
  last_run: null
---

# Task: Analyse Claude Desktop feedback proposing ADR-ARCH-031 (AsyncSubAgent vs sync task() amendment to ADR-ARCH-020)

## Description

Claude Desktop proposed a light-touch amendment to the DeepAgents built-ins
decision (ADR-ARCH-020) by way of a new, short ADR-ARCH-031. The proposed
rule assigns the two pre-declared sub-agents to different execution shapes:

- `autobuild_runner` → `AsyncSubAgent` (long-running)
- `build_plan_composer` → sync `task()` (short-lived delegation)

Claimed benefits: cancellation, mid-flight steering, and a richer
`forge_status` narrative. Claimed cost: none — the 2-pre-declared-subagents
shape is preserved.

This is an **analysis-then-decide** review. The outputs are (a) a decision
checkpoint (Accept / Implement / Revise / Cancel), and (b) if **Implement**,
a new ADR-ARCH-031 plus a minimal annotation on ADR-ARCH-020 cross-linking
the amendment. The task is **not** about writing Forge code — it is about
making sure the amendment is correct, consistent with the rest of the
architecture, and carries its own justification so future readers don't
have to reconstruct it.

## Source Material (inputs — read first)

- **Feedback (canonical)**: front-matter `source_feedback.verbatim`
  block above — this is the Claude Desktop proposal verbatim.
- **Claude Desktop reasoning (supporting context — load alongside the
  verbatim block)**:
  `docs/research/ideas/conversation-capture-2026-04-19-fleet-v3-framing.md`
  — the session-level conversation capture that contains the "why"
  behind the amendment. Specifically:
  - Line 50 — Rich's prompt asking which DeepAgents 0.5.3 features
    (async subagents, middleware) affect the architecture.
  - Lines 56, 61 — SDK surface area: `AsyncSubAgent`,
    `AsyncSubAgentMiddleware`, and the five supervisor tools
    (`start_async_task`, `check_async_task`, `update_async_task`,
    `cancel_async_task`, `list_async_tasks`).
  - Line 69 — the async-vs-sync split rationale in its original form:
    `autobuild_runner` async (long-running; supervisor benefits from
    not blocking), `build_plan_composer` sync (bounded; output gates
    the next stage).
  - Line 243 — originating action-item naming the ADR as
    `ADR-ARCH-031-async-subagents-for-long-running-work.md` amending
    ADR-ARCH-020.
  - Line 260 — related commitment to add an ADR-ARCH-031 reference
    in Forge `ARCHITECTURE.md` Decision Index §13 (flag as a
    follow-up; **out of scope for this review** — see Scope below).
  - Line 310 — positioning of sync `task()` for bounded gates and
    `interrupt()` for approval pauses, which this review must keep
    consistent with ADR-ARCH-021.

  Treat this doc as **supporting context, not authority**. Where it
  differs from the `source_feedback.verbatim` block or from the
  pinned-SDK reality at `deepagents >= 0.5.3, < 0.6`, prefer the
  verbatim block and the SDK; record the divergence as a finding.
- **ADR being amended**:
  `docs/architecture/decisions/ADR-ARCH-020-adopt-deepagents-builtins.md`
  — the `task` row in the Decision table explicitly names
  `build_plan_composer` and `autobuild_runner` as the 2 pre-declared
  sub-agents.
- **ADRs that reference the pre-declared sub-agents**:
  - `docs/architecture/decisions/ADR-ARCH-002-two-model-separation.md`
    — names `build_plan_composer` as a content-generating sub-agent that
    uses the implementation model.
  - `docs/architecture/decisions/ADR-ARCH-007-build-plan-as-gated-artefact.md`
    — defines `build_plan_composer` as the synthesiser of `buildplan.md`
    in canonical Pattern-1 structure.
  - `docs/architecture/decisions/ADR-ARCH-021-paused-via-langgraph-interrupt.md`
    — PAUSED state via `interrupt()`; may interact with cancellation.
  - `docs/architecture/decisions/ADR-ARCH-008-forge-produces-own-history.md`
    — `buildplan.md` authorship trail, relevant to build_plan_composer
    being sync and short-lived.
- **Template pattern documentation**:
  - `.claude/rules/patterns/subagent-composition.md` — documents both
    `SubAgent` and `AsyncSubAgent` TypedDict factory patterns.
  - `.claude/agents/subagent-composition-specialist.md` and
    `.claude/agents/deepagents-orchestrator-specialist.md` — extended
    guidance on how these specs are consumed by `create_deep_agent`.
- **DeepAgents SDK (external, version pinned by ADR-ARCH-020)**:
  `deepagents >= 0.5.3, < 0.6`. Verify `AsyncSubAgent` and the sync
  `task()` tool are both present in this version; verify cancellation
  semantics; verify the interaction with `interrupt()` (ADR-ARCH-021).

## Scope

### In scope

- Technical validation of the proposal against DeepAgents 0.5.3.
- Consistency check against ADR-ARCH-002, -007, -008, -020, -021.
- Drafting ADR-ARCH-031 in the existing ADR style if decision is
  **Implement**.
- Minimal annotation on ADR-ARCH-020 pointing to the amendment
  (additive, non-structural, per the TASK-REV-C3E7 precedent of
  append-only ADR corrections).
- Capturing the rationale for the split (cancellation, mid-flight
  steering, forge_status narrative) in the ADR's Consequences section.

### Out of scope

- **No Forge code changes.** This review ends at ADR text.
- No new runtime behaviours beyond what the ADR articulates.
- Do **not** change the number of pre-declared sub-agents (must remain 2).
- Do **not** modify the body of ADR-ARCH-020; only append/annotate per
  existing convention.
- No changes to Forge-specific `@tool`s, Graphiti, NATS adapter, or
  history_tools — those are downstream of the shape decision.
- No changes under `docs/research/ideas/` unless a doc there directly
  contradicts the amendment; in that case flag as a finding rather than
  editing in this task.
- **ARCHITECTURE.md Decision Index §13 update** (per conversation
  capture line 260) — out of scope for this task; capture as a
  follow-up task at decision time if **[I]mplement** is chosen.

## Acceptance Criteria

- [ ] **SDK validation**: confirm `AsyncSubAgent` TypedDict and the sync
      `task()` tool both exist in `deepagents >= 0.5.3, < 0.6` (per
      ADR-ARCH-020 pin). Cite the source (template pattern file, SDK
      docs, or code) in the report. If either does not exist at the
      pinned version, mark the proposal as **blocked** and recommend
      **Revise**.
- [ ] **Pre-declared count invariant**: report explicitly states that
      the amendment keeps the pre-declared sub-agent count at 2
      (`build_plan_composer`, `autobuild_runner`) and does not
      introduce a third. Any deviation is a blocker.
- [ ] **ADR cross-reference audit**: each of ADR-ARCH-002, -007, -008,
      -020, -021 has an explicit disposition: **unaffected**
      (cite the line), **affected — amendment consistent** (cite the
      line and the reason), or **affected — conflict** (cite and
      propose resolution).
- [ ] **Cancellation / steering semantics**: report confirms that the
      claimed benefits (cancellation, mid-flight steering,
      forge_status narrative) are realisable with `AsyncSubAgent` in
      DeepAgents 0.5.3. Cite which mechanism provides each benefit.
      If any claim is unverifiable at the pinned SDK version, flag
      it and recommend **Revise** rather than silently accepting.
- [ ] **interrupt() interaction**: report verifies the amendment does
      not conflict with ADR-ARCH-021 (PAUSED via `interrupt()`). If
      the async shape changes how interrupt surfaces (or how the
      NATS adapter resumes), capture that as a finding.
- [ ] **ADR-ARCH-031 draft (if Implement)**: follows the existing ADR
      header format (Status / Date / Session), uses the same
      Context / Decision / Consequences structure as neighbouring
      ADRs, and is short — target ≤ 60 lines. Explicitly labels
      itself as an amendment to ADR-ARCH-020.
- [ ] **ADR-ARCH-020 annotation (if Implement)**: a single appended
      line or short appendix at the bottom of ADR-ARCH-020 pointing
      to ADR-ARCH-031. No edits to the Context or Decision sections
      of ADR-ARCH-020 (preserve the "append-only corrections"
      convention set by TASK-REV-C3E7).
- [ ] **Review report** written to
      `.claude/reviews/TASK-REV-F1B8-review-report.md` with:
      executive summary, proposal validation table, ADR
      cross-reference table, benefits-verification table, decision
      checkpoint (Accept / Implement / Revise / Cancel), and — if
      **Implement** — the drafted ADR-ARCH-031 body (for review
      before applying) and the exact ADR-ARCH-020 annotation text.
- [ ] Front-matter `review_results` block populated on completion
      (score, findings_count, recommendations_count, decision,
      completed_at).

## Review Method

1. **Load the proposal and the amended ADR** — Read the
   `source_feedback.verbatim` block end-to-end. Read ADR-ARCH-020
   in full, paying attention to the `task` row and the
   "Dynamic sub-agent spawning; 2 pre-declared sub-agents" phrasing.
   Then load `docs/research/ideas/conversation-capture-2026-04-19-
   fleet-v3-framing.md` (lines 50, 56, 61, 69, 243, 260, 310) as
   supporting context — especially the line 69 async-vs-sync split
   rationale — and note any divergences between it, the verbatim
   block, and the pinned SDK surface for later dispositioning.

2. **SDK validation pass** — Confirm `AsyncSubAgent` is a public
   TypedDict in DeepAgents 0.5.3 and that sync `task()` is the
   built-in delegation tool named in ADR-ARCH-020. Use
   `.claude/rules/patterns/subagent-composition.md`,
   `.claude/agents/subagent-composition-specialist.md`, and
   (if needed) the DeepAgents repo/docs at the pinned version.

3. **ADR cross-reference pass** — For each of ADR-ARCH-002, -007,
   -008, -020, -021, record disposition per the acceptance
   criterion. Use the ADR cross-reference table template from
   TASK-REV-C3E7 as a model.

4. **Benefits-verification pass** — Map each claimed benefit
   (cancellation, mid-flight steering, forge_status narrative) to
   a specific DeepAgents / LangGraph mechanism. Cancellation
   typically comes from LangGraph's async cancellation of the
   in-flight node; mid-flight steering comes from state updates
   between async turns; forge_status narrative comes from the
   ability of `forge_status` to observe a sub-agent's `write_todos`
   progress while it is still running. Confirm each mapping.

5. **Interrupt-interaction pass** — Trace how an approval gate
   fires when inside the async sub-agent vs the sync sub-agent.
   Verify ADR-ARCH-021's resume path still holds.

6. **Decision checkpoint** — Present findings and sub-decisions
   (SDK validated Y/N, invariant preserved Y/N, cross-refs clean
   Y/N, benefits verified Y/N, interrupt clean Y/N) and block on
   user decision:
   - **[A]ccept** — Record findings only; no new ADR.
   - **[I]mplement** — Write ADR-ARCH-031 and annotate ADR-ARCH-020.
   - **[R]evise** — Deeper pass (e.g. if SDK check fails or
     interrupt conflict surfaces).
   - **[C]ancel** — Discard; capture reason.

7. **Apply edits (if Implement)** — Create
   `docs/architecture/decisions/ADR-ARCH-031-async-vs-sync-subagents.md`
   (naming to match the neighbouring ADR filenames — kebab-case
   slug after the id). Append the single cross-reference line to
   ADR-ARCH-020. No other edits.

## Known Risks / Watch-outs

- **Pinned SDK drift**: ADR-ARCH-020 pins DeepAgents at `>=0.5.3, <0.6`.
  If `AsyncSubAgent` semantics change in a future 0.5.x point release,
  the amendment may need its own `**Decision facts as of commit:**` line
  (LES1 §8 anchor-v2.2 convention enforced by TASK-REV-C3E7 edit #2).
- **build_plan_composer duration**: ADR-ARCH-007 frames
  `build_plan_composer` as the synthesiser of a full `buildplan.md`.
  A non-trivial plan synthesis could itself be long-running —
  verify "short-lived" is the right framing in practice, or adjust
  the rule. The conversation-capture line 69 framing is
  "bounded, output gates next stage" — prefer that phrasing over
  "short-lived" in the drafted ADR if the bounded/gating property
  is what actually justifies the sync shape.
- **forge_status narrative scope creep**: the "meaningful narrative"
  benefit could tempt scope expansion (e.g. custom progress-reporting
  tools). Keep the ADR to the shape decision only; any narrative
  tooling is a downstream task.
- **interrupt() inside async sub-agent**: if `AsyncSubAgent` changes
  how `interrupt()` returns control, ADR-ARCH-021's resume semantics
  need re-verification, not re-assertion.
- **Amendment vs rewrite**: the feedback is explicit that this is
  a light-touch amendment. Keep ADR-ARCH-031 short (target ≤ 60
  lines). If the draft grows beyond that, it is probably trying to
  re-litigate ADR-ARCH-020 and should be pruned.
- **Numbering collision**: verify no other in-flight review is
  claiming `ADR-ARCH-031`. Current highest on disk is ADR-ARCH-030;
  the next slot is free as of 2026-04-19.

## Test Requirements

Doc-only task; no code or test execution required. Verification is
by self-review against the Acceptance Criteria checklist and by
re-reading ADR-ARCH-031 (if drafted) and the ADR-ARCH-020
annotation for internal consistency.

## Implementation Notes

Populated by `/task-review TASK-REV-F1B8` on 2026-04-19.

**Discovery at review time:** `docs/architecture/decisions/ADR-ARCH-031-async-subagents-for-long-running-work.md` already existed on disk as an untracked file when this review began — a prior session had pre-executed the **[I]mplement** outcome. The review therefore evaluated the existing draft against the acceptance criteria rather than drafting from scratch. Full rationale and findings in `.claude/reviews/TASK-REV-F1B8-review-report.md`.

**Artefacts applied at decision time (Implement):**

1. **ADR-ARCH-020** — appended a single-paragraph "Amendment — 2026-04-19"
   annotation at the bottom of the file pointing to ADR-ARCH-031. The Context
   and Decision sections above the annotation are unchanged, preserving the
   TASK-REV-C3E7 append-only-corrections convention.
2. **ADR-ARCH-031** — inserted a new "Interaction with ADR-ARCH-021
   (`interrupt()`)" subsection immediately before the existing "Crash
   recovery" subsection. Content: `interrupt()` continues to be the PAUSED
   mechanism; it halts the async subgraph, not the supervisor; the NATS
   `ApprovalResponsePayload` subscriber resumes the specific subgraph that
   interrupted; external approval protocol + SQLite + JetStream crash
   recovery path unchanged; `async_tasks` state channel supplements rather
   than replaces those.
3. **ADR-ARCH-031** — extended the References section with one-line
   cross-references to ADR-ARCH-002 (orthogonal — shape-not-model),
   ADR-ARCH-007 (`build_plan_composer` stays sync because its output gates
   the next stage), ADR-ARCH-008 (autobuild `command_history.md` writes
   from inside the async subagent), and ADR-ARCH-021 (interaction above).

**Follow-up task created in backlog:** `TASK-DOC-B2A4` — append ADR-ARCH-031
row to `docs/architecture/ARCHITECTURE.md` §13 Decision Index and update the
prose count ("30 ADRs" → "31 ADRs"). Corresponds to conversation capture
line 260 commitment which was explicitly out of scope for this review.

**Artefacts deferred:**

- Optional length pruning of ADR-ARCH-031 (127 → 60–80 lines). The ADR is
  structurally sound but carries more design-note-level material than the
  ≤60-line target. Left as-is pending an explicit trim request; the excess
  material (langgraph.json shape, full forge history narrative example) is
  illustrative and not load-bearing, so delay is safe.

## Test Execution Log

_(Not applicable — review task.)_
