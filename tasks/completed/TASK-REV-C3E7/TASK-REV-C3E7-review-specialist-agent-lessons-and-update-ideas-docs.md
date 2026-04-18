---
id: TASK-REV-C3E7
title: Review specialist-agent lessons and update ideas docs
status: completed
created: 2026-04-18T00:00:00Z
updated: 2026-04-18T00:00:00Z
priority: high
tags: [documentation, review, architecture-review, cross-agent-lessons, specialist-agent, forge, implementation-readiness]
complexity: 6
task_type: review
decision_required: true
review_mode: architectural
review_depth: standard
review_results:
  score: 75
  findings_count: 14
  recommendations_count: 14
  decision: implement
  report_path: .claude/reviews/TASK-REV-C3E7-review-report.md
  completed_at: 2026-04-18T00:00:00Z
  low_risk_edits_applied: 17
  gap_edits_applied: 14
  d90d_dispositions:
    resolved: 8
    still_open: 0
  blockers_for_system_arch: 6
  edits_summary:
    forge-build-plan.md: 6 edit operations covering 11 gaps (G1/G3/G4/G6/G7/G8/G10/G11/G12/G13/G14)
    forge-pipeline-orchestrator-refresh.md: 3 edit operations covering 3 gaps (G2/G5/G9)
    fleet-master-index.md: 0 edits (structurally aligned; F4 auto-applied)
    secondary_docs: F4 coverage fix only
test_results:
  status: not_applicable
  coverage: null
  last_run: null
---

# Task: Review specialist-agent lessons and update ideas docs

## Description

Review the cross-agent lessons captured from the `specialist-agent` MacBook
build, deployment, and test walkthrough (TASK-REV-B8E4) and determine what
must change in the forge `docs/research/ideas/` documentation so that forge's
build plan, fleet index, and pipeline-orchestrator refresh explicitly
internalise those lessons *before* forge starts shipping code against
`/system-arch`.

The lessons span six parity surfaces (MCP stdio discipline, NATS/JetStream
fleet wiring, provider/adapter parity, tool-call timeouts, container
provisioning, and smoke-test discipline). Each surface has a documented
rule, an evidence pointer, and an "apply when…" note in the source
reference. Forge inherits the same transport, provisioning, and provider
substrate as specialist-agent, so every lesson is a candidate update target.

This is an **analysis-then-edit** task:
1. Read the lessons source and the walkthrough log.
2. Diff each lesson against the current state of the forge ideas docs.
3. For each gap, decide: add anchor, update ADR, update build step,
   annotate existing bullet, or defer with justification.
4. Produce a review report and apply any agreed doc updates.

## Source Material (inputs — read first)

- **Primary (canonical lessons doc)**:
  `/Users/richardwoollcott/Projects/appmilla_github/specialist-agent/docs/reference/cross-agent-lessons-from-specialist-agent.md`
  Audience line on the file explicitly includes forge. Status: canonical
  (round 1), series LES1, source review TASK-REV-B8E4.

- **Walkthrough log (evidence ledger)**:
  `/Users/richardwoollcott/Projects/appmilla_github/specialist-agent/.claude/reviews/TASK-REV-B8E4-walkthrough-log.md`
  Per-iteration evidence for every cited task id (MDF-CMDW, MDF-PMEV,
  MDF-ARFS, MDF-MCPB, MDF-PORT, MDF-POLR, MDF-ORPH, MDF-PRVS,
  NI-PSBUG, and the retest-commit trio CRMV/LCOI/DKRX).

- **Related cross-references** (listed in the lessons doc front-matter):
  - `tasks/backlog/macbook-deployment-fixes/README.md`
  - `docs/deployment/macbook-deployment-guide.md`
  - `tasks/backlog/TASK-DEPG-A3F2-specialist-agent-macbook-deployment-guide.md`
  (These live in the specialist-agent repo; read if a forge finding
  needs to point back at evidence.)

- **Prior forge review to verify action on**:
  `.claude/reviews/TASK-REV-D90D-review-report.md`
  The Apr 16 doc-alignment review logged 8 findings against the forge
  ideas docs (score 82/100). Several findings map to in-flight edits
  already visible in `git status` (e.g. the two stale coordination
  TASK-update-*.md files are deleted; `fleet-master-index.md` and
  `forge-pipeline-orchestrator-refresh.md` are modified). This review
  must confirm each of the 8 findings is resolved (or explicitly
  deferred) **before** layering the specialist-agent lessons on top.

## Scope

### Primary forge documents (must review for each lesson)

- `docs/research/ideas/forge-build-plan.md` — prerequisites, build steps,
  readiness gates for `/system-arch`. Main candidate for inserting
  "apply-before-first-merge" checklist items and prerequisite deltas.
- `docs/research/ideas/fleet-master-index.md` — fleet inventory, decision
  log (D-series), cross-repo references. Main candidate for new ADR
  pointers and any transport/provisioning parity rules that graduate into
  the decision log.
- `docs/research/ideas/forge-pipeline-orchestrator-refresh.md` — pipeline
  event model, stream retention, topic naming. Main candidate for
  NATS/JetStream parity lessons (subject discipline, PubAck ≠ success,
  result-subject naming).

### Secondary forge documents (scan only, update if cited)

- `docs/research/ideas/architect-agent-finproxy-build-plan.md` (SUPERSEDED
  — treat as read-only reference unless a lesson contradicts its
  superseded status).
- `docs/research/ideas/big-picture-vision-and-durability.md`
- `docs/research/ideas/conversation-starter-gap-analysis.md`
- `docs/research/ideas/forge-ideas-overhaul-conversation-starter.md`

### Explicitly out of scope

- No code changes.
- No changes under `docs/deployment/`, `docs/architecture/`, or
  `docs/product/` — this task is bounded to `docs/research/ideas/`.
- Do not modify the specialist-agent repo. The source docs there are
  canonical inputs, not editable targets.
- Do not retitle or restructure existing ADRs; only add, annotate, or
  append corrections at the bottom of the document (same pattern used
  by TASK-REV-A1F2 and TASK-REV-D90D).

## Acceptance Criteria

- [ ] **TASK-REV-D90D follow-through check**: each of the 8 findings
      (F1 retired payloads at refresh line 53; F2 refresh event table
      line 602; F3 D38 title `feature_ready_for_build`; F4 nats-core
      97% vs 98% coverage figure; F5 stale TASK-update-*.md files;
      F6 PM Adapter phrasing at fleet-master-index line 142; F7 build
      plan `[~]` prerequisite wording; F8 alignment-review outstanding
      items) has a disposition in the report: **resolved** (cite the
      current line/commit), **still open** (cite the fix needed), or
      **deferred** (cite the reason and tracking location).
- [ ] Every one of the six parity surfaces in the lessons doc has an
      explicit disposition recorded in the review report: **already
      covered** (cite the line), **gap — update needed** (cite the fix),
      or **deferred** (with justification).
- [ ] Each of the nine cited `TASK-MDF-*` / `TASK-NI-PSBUG` evidence
      pointers has been traced into a corresponding forge doc location
      (or explicitly marked N/A for forge).
- [ ] Per-agent checklist items from the lessons doc that apply to
      forge (the "forge" column / section, if present) are reflected in
      `forge-build-plan.md` as pre-merge gates or prerequisites.
- [ ] Any new anchors, ADR references, or build-step additions follow
      the existing anchor-versioning convention in the forge docs (do
      not break anchor v2.2 alignment established by TASK-REV-A1F2 /
      TASK-REV-D90D).
- [ ] Review report written to
      `.claude/reviews/TASK-REV-C3E7-review-report.md` with: executive
      summary, per-lesson findings table, per-doc edit list, and a
      decision checkpoint (Accept / Implement / Revise / Cancel).
- [ ] If the decision is **Implement**, the agreed doc edits are applied
      in-place in the forge ideas docs (no new docs created) and the
      diff is summarised in the report.
- [ ] Front-matter `review_results` block is populated on completion
      (score, findings_count, recommendations_count, decision,
      completed_at).

## Review Method

1. **Load inputs** — Read the lessons doc end-to-end, then skim the
   walkthrough log to confirm evidence pointers resolve. Note that the
   lessons doc is *prescriptive*, not exhaustive; treat its "apply
   when…" lines as the test for each forge doc location. Also load
   `.claude/reviews/TASK-REV-D90D-review-report.md` — read the 8
   findings table and the priority-ordered recommendation list.

1a. **TASK-REV-D90D action-verification pass** — Walk the 8 findings
    in order. For each, `git log`/`git diff` the target file and line
    (where line numbers are cited) and record **resolved** / **still
    open** / **deferred**. Flag any still-open P1 items (the three
    "fix before `/system-arch`" items) as blockers for the subsequent
    lessons-layering passes — fix or escalate before proceeding.

2. **Parity-surface pass** — For each of the six surfaces, walk the
   three primary forge docs and record: covered / gap / deferred. Use
   the lessons doc section headings as the checklist spine.

3. **Evidence-pointer pass** — For each cited TASK-MDF-* id, search the
   forge ideas docs for any existing reference. Absence is not
   automatically a finding — some evidence is specialist-agent-specific
   — but every id needs an explicit disposition.

4. **Per-agent checklist pass** — If the lessons doc contains a
   forge-specific checklist (it is addressed to forge among others),
   diff each item against the current `forge-build-plan.md`
   prerequisites and build-step gates.

5. **Decision checkpoint** — Present findings with severity
   (HIGH/MEDIUM/LOW), recommended fix, and target file/line. Block on
   user decision: Accept (record only), Implement (apply edits),
   Revise (deeper pass), Cancel.

6. **Apply edits (if Implement)** — Make minimal, surgical edits.
   Prefer annotation and appended corrections over restructuring.
   Preserve anchor v2.2 structure.

## Known Risks / Watch-outs

- **Anchor drift**: anchor v2.2 was established by the
  TASK-REV-A1F2 → v2.2 correction pass. Adding parity-surface content
  must not silently invalidate existing cross-doc anchor references.
- **Double-counting D90D fixes**: some D90D findings appear to have
  been actioned since the report was written (git status shows the
  two stale `TASK-update-*.md` files deleted and two primary ideas
  docs modified). Verify by reading current file state, not by
  trusting the report; a finding that looks "resolved" in git may
  only be partially addressed, and a finding that looks "still open"
  may have been subsumed by the FEAT-FVDA v2.2 alignment commit.
- **Retired-vs-active confusion**: prior review (TASK-REV-D90D) found
  retired payloads still listed as active. The same pattern could
  happen again if a lesson adds a new event/subject without retiring
  or annotating the superseded one.
- **Evidence provenance**: every new bullet that claims a rule should
  cite the TASK-MDF-* id (or the walkthrough section) so future
  reviewers can trace it back to specialist-agent evidence rather than
  to forge speculation.
- **Scope creep into code**: tempting to prototype a fix while
  editing the doc. Do not. This task produces docs; code is handled by
  downstream `/task-create` / `/task-work` once forge starts building.

## Test Requirements

Doc-only task; no code or test execution required. Verification is by
self-review against the Acceptance Criteria checklist and by
re-reading each edited doc section end-to-end for anchor integrity.

## Implementation Notes

Decision: **Implement** (applied 2026-04-18). See full report at
`.claude/reviews/TASK-REV-C3E7-review-report.md`.

### Phase 1a disposition (TASK-REV-D90D action verification)

7/8 findings already resolved by the FEAT-FVDA v2.2 alignment commit
(fea6d87) prior to this task. F4 (nats-core coverage 97% vs 98%) was still
open; auto-applied as low-risk edit during this review (17 citations fixed).
All 8 D90D dispositions captured in the report's Phase 1a table.

### Specialist-agent lessons (LES1) gap dispositions

14 parity gaps identified from the lessons doc at
`specialist-agent/docs/reference/cross-agent-lessons-from-specialist-agent.md`.
Severity: 6 HIGH (blockers for `/system-arch`), 5 MEDIUM, 3 LOW.

Per-surface disposition recorded in the report's parity-surface table; per
evidence-pointer recorded in the evidence-pointer table; per checklist-item
recorded in the per-agent checklist table.

### Edits applied in-place (diff summary)

**`docs/research/ideas/forge-build-plan.md`** — 6 edit operations covering 11
gaps:

1. Prerequisites line 34–36 — **replaced** nats-infrastructure prereq to
   require `provision-streams.sh` + `provision-kv.sh` execution beyond
   `docker compose up -d`, with TASK-MDF-PRVS / NI-PSBUG citation. (G11)
2. Step 1 /system-arch Validation — **added** bullet requiring each ADR to
   carry `**Decision facts as of commit:** <sha>` annotation, with LES1 §8
   citation. (G10)
3. Files That Will Change — `pyproject.toml` row **annotated** with
   `[providers]` extras requirement and LCOI citation. (G4)
4. Files That Will Change — **added** two rows: `.env.example` with no-real-
   keys hygiene rule (retest-env citation), and deferred `Dockerfile` row
   with DKRX literal-match rule. (G7, G13)
5. Step 6 Validation — **added** new "Specialist-agent LES1 Parity Gates"
   subsection with four pre-merge gates: CMDW prod-image round-trip, PORT
   `(role, stage)` matrix, ARFS handler-completeness matrix, canonical-
   freeze live-verification. (G1, G3, G6, G8)
6. Risks & Mitigations — **added** two rows: orphan-container (ORPH) with
   `docker compose down --remove-orphans` rule, and CLI credential leakage
   with redaction rule. (G12, G14)

**`docs/research/ideas/forge-pipeline-orchestrator-refresh.md`** — 3 edit
operations covering 3 gaps:

1. Fleet Agent Tools section — **added** reply-subject convention paragraph
   above the tools table, documenting `agents.result.<id>` vs JetStream
   `agents.>` PubAck trap (walkthrough §retest-smoke citation). (G2)
2. AgentConfig section — **added** provider-resolution-at-factory rule bullet
   with PMEV + CRMV citation (commit `8b9d584`). (G5)
3. Forge Agent Identity manifest — **annotated** three mutating tool
   descriptions (`forge_greenfield`, `forge_feature`, `forge_review_fix`) as
   fire-and-forget with `pipeline_id` return + `forge_status`/`forge_cancel`
   companion references, **added** new `forge_cancel` tool entry, and
   **added** POLR citation comment above the tools block. (G9)

**`docs/research/ideas/fleet-master-index.md`** — 4 low-risk F4 citation
edits (97% → 98%); no gap edits required (doc already structurally aligned).

**Secondary docs** (`big-picture-vision-and-durability.md`, `conversation-
starter-gap-analysis.md`, `forge-ideas-overhaul-conversation-starter.md`) —
F4 citation edits only (13 occurrences fixed).

### Anchor v2.2 integrity

All edits are additive annotations, prerequisite expansions, or table-row
additions. No ADR content modified; no cross-doc anchor references
invalidated; no existing anchors v2.2 §N structure broken. Pre/post state
verified by grep: zero remaining `97%` references under
`docs/research/ideas/`; all retired-payload annotations from D90D F1–F3
intact; D38 wording unchanged.

### Provenance

- **Source of lessons**: TASK-REV-B8E4 (specialist-agent MacBook walkthrough),
  canonical round 1, series LES1
- **Prior forge review verified**: TASK-REV-D90D
- **This review**: TASK-REV-C3E7 (17 low-risk + 14 gap edits applied)
- **Next downstream**: forge is now ready for `/system-arch` consumption of
  these docs as `--context` inputs. Step 6 LES1 Parity Gates become the
  pre-merge canonical-freeze checklist.

## Test Execution Log

[Not applicable — doc-only task]
