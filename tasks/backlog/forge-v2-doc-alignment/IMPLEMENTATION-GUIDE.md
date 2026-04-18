# Implementation Guide — Forge v2.2 Doc Alignment

## Wave structure

```
Wave 1 (foundation — must complete first)
  └── TASK-FVD1  Anchor v2.2 updates

Wave 2 (parallel after Wave 1)
  ├── TASK-FVD2  forge-build-plan.md
  ├── TASK-FVD3  forge-pipeline-orchestrator-refresh.md
  └── TASK-FVD4  fleet-master-index.md
```

## Reference materials

Every task in this feature references sections of the **alignment review** as the authoritative correction list:

- File-scoped punch list: [alignment review §5](../../../docs/research/forge-build-plan-alignment-review.md#5-correction-list-file-scoped-punch-list)
- Draft ADRs: [alignment review Appendix E](../../../docs/research/forge-build-plan-alignment-review.md#appendix-e--draft-adrs-for-anchor-v22)
- BuildQueuedPayload design: [Appendix C](../../../docs/research/forge-build-plan-alignment-review.md#appendix-c--buildqueuedpayload-full-design-jarvis-aware) — used by TASK-FVD1 when defining §7 payload changes and by TASK-FVD2 when discussing prerequisites

Do not re-derive corrections from the alignment review — copy them across verbatim where the review specifies "change X to Y" and cite the correction number in the commit message.

## Golden rules

1. **Anchor is the source of truth.** If TASK-FVD2/3/4 reveal a drift that is not covered by the alignment review, stop and fix the anchor first (or raise a new task) rather than introducing a new inconsistency.
2. **No code changes.** If a doc correction implies a code change, note it and link to the appropriate task in `nats-core/`, `specialist-agent/`, `nats-infrastructure/`, or `jarvis/`.
3. **ADR status transitions.** The four ADRs (SP-014..017) are currently **Proposed** in the anchor §9. TASK-FVD1 promotes them to **Accepted** *only* if Rich has signed off on the alignment review. If Rich asks for revisions during TASK-FVD1 execution, update the ADR Context/Decision/Consequences in-place before accepting.
4. **Cross-references stay valid.** After every file edit, grep for broken section references (`§5.0`, `§3.1`, etc.) and fix anywhere they are stale.
5. **`TASK-update-fleet-index-d22.md` is subsumed.** When TASK-FVD4 lands, mark `docs/research/ideas/TASK-update-fleet-index-d22.md` as completed (or delete it) with a note pointing at TASK-FVD4 as the executor.

## Validation

For each task, after completion:

- `git diff --stat` shows only the intended file(s) changed
- Grep for the removed concepts (`Kanban`, `PM Adapter`, `ready-for-dev`, `feature-planned`, `ticket-updated`, `PipelineTransport`, `v0 subprocess`) across the three dependent docs → zero hits outside the "Components Removed from Scope" and "What Changed" tables
- Grep for the singular vs plural topic form: `agents\.commands\.` should return zero hits in forge repo docs after TASK-FVD1 (except where explicitly called out as "was plural, now singular")
- Re-open the alignment review and verify each listed correction number is addressed
