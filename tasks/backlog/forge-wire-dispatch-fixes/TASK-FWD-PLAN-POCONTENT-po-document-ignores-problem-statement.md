---
id: TASK-FWD-PLAN-POCONTENT
title: "Mode P PO document ignores the problem_statement: generic hallucinated epics with fabricated source_documents"
status: backlog
created: 2026-07-11T19:30:00Z
priority: high
task_type: bug
found_by: Mode-P execution-contract lane Sfinal live validation (2026-07-11) — surfaced once the full dispatch round-trip worked
feature_ref: FEAT-SPL-002
tags: [mode-p, planning, product-owner, content-quality, specialist-side, found-2026-07-11]
complexity: 2
---

# Mode P PO greenfield document is off-topic — problem_statement never shapes the output

## Problem (observed live twice, 2026-07-11)

With the full execution contract fixed (TASK-FWD-PLAN-DISPATCHFMT), two live greenfield
runs produced structurally-valid PO documents whose CONTENT ignored the request:

- Request (both runs): *"A small CLI tool that summarises a git repository's last week
  of commits into a Slack-ready weekly report…"*
- Run 1 (correlation `5998f96a…`, 76-min session): epics about a **multi-protocol event
  ingestion platform** (REST/gRPC/WebSocket, TLS 1.3, GDPR), citing fabricated
  `source_documents` `product-brief.md` / `api-spec.md`.
- Run 2 (correlation `cc879942…`, 8-min session): epics about a **generic project/task
  manager** (Create Project, Archive Project, Create Task, Assign Task), citing a
  fabricated `problem-statement.md` (verified: no such file exists in the container).
- Both: `project_name: ""`.

## What is already ruled OUT (verified read-only in the deployed container)

- Forge sends `args={"problem_statement": <real text>}` (S2 fix; harness-driven with
  forge's real publish path; the run record + handoff doc carry the verbatim request).
- The deployed `_handle_po_greenfield` passes `problem_statement=args["problem_statement"]`
  into `run_product_session` (command_router.py:704).
- `run_product_session` appends a `## Problem Statement` section when the value is
  truthy (deployed orchestrator/session.py:1343).

## Remaining hypotheses (in likelihood order)

1. The player model is the non-fine-tuned `qwen36-workhorse` (the `product-owner-agent`
   alias is an INTERIM mapping — no PO fine-tune exists on the box) and the deployed
   session's prompt template dominates: the model regurgitates template/example
   material and fabricates source-document citations.
2. A deployed-session bug drops/overrides the problem-statement section between
   :935 and the actual player prompt (e.g. a docs-gathering step replacing sections).

## Acceptance criteria

- Capture the exact `agents.command.product-owner-agent` bytes AND the first player
  prompt of a live run (or add value-free prompt-section logging) to pin hypothesis
  1 vs 2.
- A greenfield run for the git-summariser request produces a document whose epics are
  ABOUT the request; no fabricated `source_documents`.
- Decide the model question explicitly: PO fine-tune (train/pull) vs prompt-hardening
  on the workhorse (relates to the M9 INTERIM alias decision).

## Notes

- Specialist-agent repo currently carries the FEAT-DF12 Phase A claim — coordinate any
  specialist-side change behind it (this file lives forge-side because the finding was
  produced by the forge-lane validation; the fix is likely specialist/model-side).
- The 76-min vs 8-min session variance (same verb, same model) is worth a look in the
  same pass — run 1 logged 37 `graphiti-core is not installed` fleet-scope failures
  (a retry pathology?); run 2 only a handful.
