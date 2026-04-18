# Review Report: TASK-REV-D90D

## Executive Summary

**Verdict: Docs are substantially aligned with anchor v2.2 — but 8 actionable findings remain, 3 of which should be fixed before `/system-arch` runs against these docs as context.**

The TASK-REV-A1F2 alignment review (15 April) drove a major correction pass that produced anchor v2.2 (16 April). Most of the high-severity findings from that review have been incorporated: Jarvis integration (§5.0), specialist-agent dual-role deployment (§3.1), singular topic naming (ADR-SP-016), stream retention reconciliation (ADR-SP-017), and four new ADRs (SP-014..017). The build plan and fleet-master-index were also updated inline (per TASK-FVD3, TASK-FVD4).

**What remains is cleanup, not architecture.** The stale concepts listed in the task (ready-for-dev, PM Adapter webhooks, kanban triggers) are already absent from the ideas docs. The remaining findings are:
- Retired payloads still listed as active in one table (refresh doc line 53)
- D38 decision summary references a now-retired event name
- nats-core coverage figure inconsistency (97% vs 98%)
- Two task files (TASK-update-build-plan-da15, TASK-update-fleet-index-d22) that are stale coordination artifacts

## Review Details
- **Mode**: Architectural Review
- **Depth**: Standard
- **Focus**: All aspects (stale references, NATS consistency, implementation readiness, cross-doc consistency)
- **Trade-off Priority**: Quality/reliability
- **Duration**: ~45 minutes
- **Documents Reviewed**: 12 primary + secondary documents

---

## Findings

### FINDING-1: Retired payloads still listed as active in refresh doc event table
- **File**: `docs/research/ideas/forge-pipeline-orchestrator-refresh.md`
- **Line**: 53
- **Severity**: MEDIUM
- **Issue**: Line 53 lists `FeaturePlannedPayload` and `FeatureReadyForBuildPayload` in the "nats-core event payloads" bullet as if they are active. Line 479 adds a retirement decision (TASK-FVD3, correction 12), and line 682 correctly states they are retired. But a reader scanning the "What's Changed" section sees them listed as current payloads.
- **Fix**: Add `(RETIRED — see correction 12 below)` annotation to line 53, or move them to a separate "Retired" sub-list.

### FINDING-2: Refresh doc pipeline event comparison table still lists retired payloads
- **File**: `docs/research/ideas/forge-pipeline-orchestrator-refresh.md`
- **Line**: 602
- **Severity**: LOW
- **Issue**: The "Before/After" comparison table at line 602 says `FeaturePlannedPayload through BuildCompletePayload (already implemented)`. This range includes retired payloads. Should say `BuildStartedPayload through BuildCompletePayload` or explicitly note the retired ones.
- **Fix**: Update table cell to exclude retired payloads or annotate them.

### FINDING-3: D38 decision title references retired `feature_ready_for_build` event
- **File**: `docs/research/ideas/fleet-master-index.md`
- **Line**: 645
- **Severity**: MEDIUM
- **Issue**: D38's title reads "`feature_ready_for_build` replaces kanban-triggered events" — but `feature_ready_for_build` has itself been retired (acknowledged at line 158: "retired — its function is covered by `StageCompletePayload`"). The decision summary still frames `feature_ready_for_build` as the current mechanism, which is misleading. The *intent* of D38 (no kanban triggers, pipeline events replace PM tool events) remains correct, but the title and wording need updating.
- **Fix**: Retitle D38 to something like "Pipeline events replace kanban-triggered events" and update the description to reference `StageCompletePayload` and `BuildQueuedPayload` as the current mechanisms. Note that `feature_ready_for_build` was an intermediate design that was itself superseded.

### FINDING-4: nats-core coverage figure inconsistency (97% vs 98%)
- **File**: Multiple — fleet-master-index.md (lines 170, 401, 562, 682), forge-pipeline-orchestrator-refresh.md (lines 7, 43, 610, 649), conversation-starter-gap-analysis.md (lines 98, 175, 225, 267, 298), big-picture-vision-and-durability.md (line 96), forge-ideas-overhaul-conversation-starter.md (lines 52, 85, 247)
- **Severity**: LOW
- **Issue**: All ideas docs consistently say "97% test coverage." The TASK-REV-A1F2 alignment review found "98% coverage" (755 passed, 698 stmts, 16 missed) from actual pytest execution. The build plan itself says "97% test coverage" in the prerequisites. This is a minor discrepancy but should be consistent.
- **Fix**: Bulk update all references to "~98%" or simply "98%". Alternatively, accept "97%" as the documented figure and note that actual coverage may vary slightly with test additions.

### FINDING-5: Stale coordination task files should be marked COMPLETED or removed
- **File**: `docs/research/ideas/TASK-update-build-plan-da15.md`
- **Severity**: LOW
- **Issue**: This task describes adding DA15 (AgentConfig) to `architect-agent-finproxy-build-plan.md` — a document that is already marked SUPERSEDED. The task is now moot. It was never executed and serves no current purpose.
- **Fix**: Delete the file or add a SUPERSEDED header noting that the target document is itself superseded.

- **File**: `docs/research/ideas/TASK-update-fleet-index-d22.md`
- **Severity**: INFO
- **Issue**: This task is marked COMPLETED (executed inline as part of TASK-FVD4). The file should be moved to a completed tasks folder or deleted to reduce clutter.
- **Fix**: Move to `tasks/completed/` or delete.

### FINDING-6: Fleet-master-index PM Adapter phrasing could be tightened
- **File**: `docs/research/ideas/fleet-master-index.md`
- **Line**: 142
- **Severity**: LOW
- **Issue**: "The PM Adapter layer (Linear, GitHub Projects) remains in the architecture but is repositioned as an optional visibility adapter." The phrase "remains in the architecture" could confuse a reader into thinking it currently exists. The anchor v2.2 §11 explicitly lists PM Adapter as "removed from scope."
- **Fix**: Rephrase to "The PM Adapter concept (Linear, GitHub Projects) is positioned as a future optional visibility adapter — not part of the current architecture."

### FINDING-7: Build plan nats-core prerequisite checkbox overstates readiness
- **File**: `docs/research/ideas/forge-build-plan.md`
- **Line**: 33
- **Severity**: MEDIUM (already noted in alignment review, partially addressed)
- **Issue**: The nats-core prerequisite reads "[~] nats-core library implemented — shipping at 98% coverage, but v2.2-critical payloads... must be added in Phase 2." The `[~]` marker is correct (partially complete), and the description now correctly identifies missing payloads. However, the alignment review found this prerequisite needs to be more prominently flagged as BLOCKING until `BuildQueuedPayload`, `BuildPausedPayload`, `BuildResumedPayload`, `StageCompletePayload`, and `StageGatedPayload` are added.
- **Status**: Partially addressed by TASK-FVD3 corrections. The current wording is acceptable but could be clearer.

### FINDING-8: Alignment review (TASK-REV-A1F2) has outstanding items not yet tracked
- **File**: `docs/research/forge-build-plan-alignment-review.md`
- **Severity**: MEDIUM
- **Issue**: The alignment review identified several items that were addressed in v2.2, but some remain open:
  - `pipeline-state` NATS KV bucket fate (tracked as TASK-PSKV-001 in nats-infrastructure) — decision still pending
  - nats-core payload additions (BuildQueuedPayload, etc.) — still missing in nats-core code
  - specialist-agent dual-role deployment bugs — still blocking (--role flag ignored, manifest hardcoded)
  - `FeaturePlannedPayload` / `FeatureReadyForBuildPayload` deprecation in nats-core (TASK-NCFA-001) — still pending
- **Fix**: No action needed in forge docs. These are tracked in their respective repos. However, the build plan's prerequisite checklist accurately reflects these as incomplete.

---

## Stale Concepts Check (from task Known Issues)

| Concept | Status | Evidence |
|---------|--------|----------|
| `ready-for-dev` events | **CLEAN** — not found in any ideas doc | Removed before v2.2 alignment pass |
| PM Adapter / Linear webhooks | **CLEAN** — no active references | Only historical context references remain (anchor §11 "removed from scope") |
| Kanban board triggers | **CLEAN** — no active references | Only philosophical references ("no kanban boards") remain, which are correct |
| `ReadyForDevPayload` | **CLEAN** — not found in ideas docs | Exists only in nats-core code (pending removal) |
| `FeaturePlannedPayload` | **PARTIALLY STALE** — listed in refresh doc line 53 | Retirement decision at line 479 exists but line 53 still lists as active |
| `feature-planned` / `ticket-updated` events | **CLEAN** — not referenced as active | Only in historical/retirement context |
| `feature_ready_for_build` | **STALE IN D38** — D38 title references it as current | Line 158 correctly says retired; D38 title contradicts |

---

## NATS JetStream Consistency

| Check | Status |
|-------|--------|
| Topic naming (singular `agents.command.*`) | **CONSISTENT** — anchor v2.2 aligned with nats-core |
| Stream definitions (6 streams) | **CONSISTENT** — anchor v2.2 matches nats-infrastructure |
| Stream retention (PIPELINE 7d, SYSTEM 1h) | **CONSISTENT** — reconciled via ADR-SP-017 |
| JetStream described as current (not future) | **CONSISTENT** — all docs treat NATS JetStream as available |
| Payload schemas | **PARTIALLY CONSISTENT** — anchor v2.2 §7 defines the canonical set, but nats-core code still has stale payloads and is missing new ones |

---

## Implementation Readiness Assessment

| Document | Ready for `/system-arch`? | Notes |
|----------|--------------------------|-------|
| `forge-build-plan.md` | **YES** (after minor fixes) | Prerequisites accurate, features well-defined, command sequence complete |
| `fleet-master-index.md` | **YES** (after D38 fix) | Agent inventory current, repo map accurate, decisions comprehensive |
| `forge-pipeline-architecture.md` (anchor v2.2) | **YES** | Canonical, internally consistent, ADRs complete |
| `forge-pipeline-orchestrator-refresh.md` | **YES** (after line 53 fix) | Correctly scoped as supporting design artefact |
| `forge-build-plan-alignment-review.md` | **N/A** — historical review | Findings largely incorporated into v2.2; remaining items tracked |
| `docs/product/roadmap.md` | **YES** | Correctly complements core build plan, no contradictions |

---

## Cross-Document Consistency

| Check | Status |
|-------|--------|
| ADR references (SP-010..017) | **CONSISTENT** across anchor, build plan, fleet index |
| Decision log (D1–D39) | **CONSISTENT** — D38 title needs update but content is correct |
| Agent names/repos | **CONSISTENT** — all docs use `specialist-agent`, archived repos marked |
| Version numbers and dates | **CONSISTENT** — all primary docs dated April 2026 |
| Forge identity (orchestrator vs checkpoint manager) | **CONSISTENT** — anchor frames as orchestrator with gates; refresh adds checkpoint manager emphasis; both compatible |

---

## Recommendations (Priority Ordered)

### Priority 1: Fix before `/system-arch` (3 items)

1. **[FINDING-3] Update D38 in fleet-master-index.md** — Retitle from `feature_ready_for_build` reference to pipeline event framing. This goes into `/system-arch` context.

2. **[FINDING-1] Annotate retired payloads in refresh doc line 53** — Add retirement note so `/system-arch` doesn't pick up stale payload names.

3. **[FINDING-2] Fix refresh doc pipeline event table (line 602)** — Same reason as above.

### Priority 2: Cleanup (3 items)

4. **[FINDING-6] Tighten PM Adapter phrasing in fleet-master-index line 142** — Minor wording improvement.

5. **[FINDING-4] Reconcile nats-core coverage figure** — Bulk update 97% → 98% across docs (or accept 97% as documented).

6. **[FINDING-5] Remove/mark stale coordination task files** — TASK-update-build-plan-da15.md is moot; TASK-update-fleet-index-d22.md is completed.

### Priority 3: Track (2 items, no forge doc changes needed)

7. **[FINDING-7] Build plan prerequisite wording** — Current `[~]` marker is acceptable; no change needed.

8. **[FINDING-8] Alignment review outstanding items** — Tracked in respective repos (nats-core, specialist-agent, nats-infrastructure).

---

## Architecture Score: 82/100

| Criterion | Score | Notes |
|-----------|-------|-------|
| Stale concept removal | 9/10 | `ready-for-dev`, PM Adapter, kanban triggers all clean. Only `feature_ready_for_build` in D38 remains. |
| NATS JetStream consistency | 8/10 | Topic naming, streams, retention all reconciled. Retired payloads still referenced in one table. |
| Implementation readiness | 8/10 | Build plan prerequisites accurate. Missing nats-core payloads correctly identified as blocking. |
| Cross-document consistency | 9/10 | ADRs, decisions, agent names, versions all consistent. |
| Gap identification | 7/10 | No gaps beyond what alignment review already found. Roadmap complements core plan well. |

---

*Review completed: 16 April 2026*
*Reviewer: /task-review (architectural mode, standard depth)*
