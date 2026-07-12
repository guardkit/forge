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
## Diagnosis (2026-07-11, follow-ups session)

**Pinned cause: MIXED** — two distinct defects with different roots, plus one aggravating contributor.

1. **Fabricated `source_documents` = template-example-bleed (CONFIRMED).** The greenfield player prompt's own JSON exemplar hardcodes `source_documents[0].filename="problem-statement.md"` (and `feature_spec_inputs` `source_documents=["overview.md"]` / `suggested_context_files=["docs/design/relevant-doc.md"]`). `architect-agent` copied `problem-statement.md` verbatim into its roadmap in one **clean single shot** — reproducing Run 2's exact fabrication. Captured reasoning: *"I'll include `problem-statement.md` with a contribution summary."* Run 1's `product-brief.md`/`api-spec.md` are the same exemplar-copying class. Guards are inert in greenfield (`_audit_source_documents` session.py:281-283 early-returns on empty manifest; the Coach never gets a ground-truth manifest), so fabricated docs ship unchecked.

2. **Off-topic epics (event-ingestion / task-manager) = multi-turn session-loop artifact (NOT delivery, NOT the model single-shot).** The problem_statement is delivered intact and prominent to the player on iteration 1 (full static trace + Player is plain langchain `create_agent`, no summarisation middleware). All 4 resident models — **including the deployed CONTROL qwen36-workhorse via the product-owner-agent alias, temp 0.7, exact 2-section message** — produced **on-topic** git/commits/Slack output. The off-topic domains appear in NO prompt and NO KG (graphiti_query returns `[]` — graphiti-core not installed). The drift emerges only under the live loop (empty KG round-trips + Coach revision loop max=5 + qwen truncation seeding invalid JSON) progressively burying the 1-line statement. Degradation scales with thrash: Run 1 (76 min / 37 graphiti failures) drifted hardest; Run 2 (8 min) drifted mildly.

3. **Aggravating contributor = model choice.** qwen36 is a **reasoning** model; its `<think>` block (11.2k chars) ate the 3000-tok budget and truncated the CONTROL roadmap mid-JSON (`finish_reason=length`). That invalid JSON seeds the revision loop. `architect-agent` (non-reasoning) was the ONLY model to emit a complete valid on-topic roadmap in one shot (`finish_reason=stop`, 1488 tok).

`project_name:""` is a benign deterministic plumbing artifact (`_derive_project_name(docs_path='')`, session.py:533), not a dropped-statement symptom.

### A/B (exact deployed 2-section message; product-owner-agent alias = qwen36-workhorse)

| Model | On-topic | Complete valid roadmap (1-shot) | Fabricated citation | Note |
|---|---|---|---|---|
| **qwen36-workhorse** (CONTROL, deployed alias) | Yes | **No** — `finish_reason=length`, `<think>` ate 3000-tok budget, truncated at FEAT-PO-002 | weighed `problem-statement.md` | reasoning-budget problem, not drift |
| gemma4-coach | Yes (intent) | No — emitted `graphiti_query` then `stop` | — | followed Step 2, paused for tool |
| **architect-agent** | Yes | **Yes** — `finish_reason=stop`, 1488 tok | **Yes** — copied `problem-statement.md` verbatim | non-reasoning; only complete 1-shot; reproduced Run 2 fabrication clean |
| tutor-coach | Yes (intent) | No — `graphiti_query` then `stop`, 33 tok | — | tool-first, no drift |

**Verdict:** hypothesis 1 (model drifts) REFUTED; hypothesis 2 (session drops/overrides the statement) DISPROVEN. The content-ignoring is (a) a template-exemplar fabrication attractor + (b) a multi-turn loop degradation, not a delivery bug and not a bad single-shot model.

### Recommendation

- **llama-swap retarget: YES → `architect-agent`** (resident/cheap; NOT gpt-oss). Basis is NOT on-topic superiority (qwen was on-topic) but JSON completeness / no reasoning-truncation: architect emits a complete valid roadmap one-shot where qwen truncates and seeds the revision loop. It is a llama-swap config edit (fenced here) → queue for the alias owner. **Does not alone fix the drift.** Cheaper alternative if keeping qwen36: disable thinking (`enable_thinking=false` / `/no_think`) + raise `max_tokens`.
- **Specialist-side change: YES** (queue behind FEAT-DF12 Phase A):
  1. `roles/product-owner/prompts/player_greenfield.md` — strip hardcoded filenames from the Output-Format example (`source_documents`/`feature_spec_inputs` → `[]`). **Highest-confidence fix; kills the fabrication attractor.**
  2. Soften Step 2 / "Knowledge Graph Integration" to make `graphiti_query` optional (always empty in-container; wastes a loop iteration).
  3. Add a greenfield guard rejecting non-empty `source_documents` when `docs_path is None`.
  4. Per acceptance criteria: add value-free per-iteration player-message section logging on ONE live run to pin the drift driver (qwen-truncation-seeding vs pure multi-turn accumulation); single-shot replay can't reproduce the loop. Likely session fix: re-inject `## Problem Statement` into each revision-loop turn so it isn't buried after turn 1.
  5. Cosmetic: derive greenfield `project_name` from the problem statement / model value (session.py:533).

*All work READ-ONLY on container `specialist-agent-product-owner-agent-1`; one model call per curl to the resident `product-owner-agent` alias, no fleet eviction. Artifacts: `scratchpad/pocontent/` (sys_greenfield.md, user_msg.txt, run.py, resp_*.json, visible_*.txt, evidence_fabricated_citation.txt).*
## Applied (2026-07-11, follow-ups session)

- **llama-swap retarget EXECUTED**: `product-owner-agent` alias moved
  qwen36-workhorse → **architect-agent** (routing verified live; backup
  `config.yaml.bak-20260711-po-alias-retarget`). Rationale: architect-agent was
  the only resident model to emit a complete, valid, on-topic ProductRoadmap in
  one shot; qwen36's reasoning blocks consumed the whole completion budget
  (truncated JSON → Coach revision thrash → drift). Still INTERIM pending a PO
  fine-tune decision.
- **Specialist-side changes remain OPEN** (queue behind FEAT-DF12): strip the
  hardcoded exemplar filenames from `player_greenfield.md` (the fabrication
  attractor, reproduced clean single-shot); soften the mandatory `graphiti_query`
  step (graphiti-core absent → always empty); greenfield `project_name`
  derivation (deterministic '' today); make the anti-fabrication audits
  greenfield-aware (currently inert on empty manifest).
- Live re-validation of content quality rides the planning-activation run
  (jarvis env + planning.enabled:true), queued behind the JNB-009 claim.

## Correction (2026-07-11 late — retarget REVERTED)

The architect-agent retarget FAILED in the live session: the A/B replay was a raw
completion, but the session's tool-bound langchain path + architect-agent's
gemma4-thinking chat template produced unparseable output — `OutputParseError: No
valid JSON found in Player output` on turn 1, then ~10-min client-canceled retries
(run dfmt3, written off; it also exposed TASK-FWD-PLAN-M12). **Alias reverted to
qwen36-workhorse** (2nd backup `config.yaml.bak-20260711-po-alias-retarget` +
in-file comment), which completes the loop reliably (dfmt2 8m17s, dfmt4 18m30s —
both parsed, checkpoint + handoff green; dfmt4 approved from Rich's REAL phone).
Methodology lesson for the AC: any model A/B for this seam must replay the
TOOL-BOUND session path, not a raw completion. Content quality remains the
specialist-side fix list above (unchanged, queued behind FEAT-DF12); the dfmt4 doc
was again off-topic with fabricated citations, consistent with the diagnosis.

## Deploy attempt (2026-07-12) — fixes MERGED; live deploy ROLLED BACK on an unrelated regression

The six specialist-side fixes are BUILT, coach-verified, and MERGED (specialist-agent
`2c70379..96d04dd`, pushed). A live deploy was attempted (image `51ef89dc`, both
dual-role containers recreated; rollback tag `specialist-agent:rollback-pre-pocontent-20260712`).
Validation run `dfmt5268a4666728` FAILED differently: the generation loop ran 5
iterations with **coach score 0.00 every time** → `RuntimeError: … did not reach
acceptance threshold (final_score=0.00, verdict=REVISE)`. The player WAS generating
(large completions via the new client's /v1/responses path) — the SCORING leg is
broken on the repo-HEAD image. NOTE: the rebuilt image ships 13 days of merged work
(FEAT-96FC capture, FEAT-DF12 emitters, newer openai client 2.45/responses-API) beyond
these six changes — the 0.00-coach break is NOT attributed yet.

**State now: specialists ROLLED BACK to the known-good 13-day image** (the one that
served dfmt4 + the factory-1 run green). So production behavior is unchanged: docs
parse and hand off, with the known mild-drift/fabrication residual — the six fixes
sit merged, awaiting a validated specialist deploy.

**Next (a dedicated specialist-deploy lane, benched):** diagnose the coach-scoring
0.00 on the new image (first suspects: the coach model call path under openai 2.45 /
responses-API against llama-swap; the player-output parse feeding the coach; only then
these six changes), with a hermetic session harness BEFORE any recreate. Evidence:
session scratchpad `pocontent-deploy-fail-evidence.txt` + the dfmt5 logs.

---

## Dated addendum — 2026-07-12 (Factory-2 S1): the hermetic diagnosis is DONE — coach-0.00 ROOT-CAUSED, prime suspect REFUTED

The benched lane's hermetic-first step was executed by the Factory-2 pre-stage (run
`wf_afe2213a-e0d`, independently re-verified). Full report + reproducible drivers beside this
file: `factory-2-s1-hermetic-diagnosis-2026-07-12.md` (drivers also at `/tmp/f2-s1-out/`).

**Three blockers at specialist-agent HEAD (`96d04dd`), in dependency order:**
1. **Pre-LLM criteria-type crash** — `session.py:1981` assigns `load_criteria(...)` without
   `.criteria` (legacy path `:1135` has it); `run_generation_loop:777` does `len(criteria)` →
   `TypeError` in ~0.7s. One-line fix. This alone is why the headless tools were never driven.
2. **Responses API forced** — the deepagents openai provider profile sets
   `use_responses_api=True`; llama-swap/llama.cpp hangs on `POST /v1/responses` (~76s timeout)
   while `/v1/chat/completions` answers in ~2s. App-side `ProviderProfile(use_responses_api=False)`.
3. **THE coach-0.00 (root cause, load-bearing):** past 1+2, on real qwen36 output,
   **gemma4-coach returns 5 of the 6 required criterion scores** → `validate_criterion_scores`
   raises (`scoring.py:138`) → caught → `_EXTRACTION_FAILURE_SCORE=0.0`/REVISE every iteration
   (`generation_loop.py:901-910`) → `ModeLoopNotAcceptedError(best_score=0.00)` — the exact
   dfmt5268a4666728 bench shape. **Reproduced on openai 2.33.0 with the responses API bypassed —
   the "openai 2.45 / responses-API" prime suspect is REFUTED for the scoring break.** The break
   is coach-model criterion-count under-production, independent of client version and transport.

**Encouraging counterpoint:** the player's iteration-0 output on the Factory-1 gold input was
fully on-topic with ZERO fabricated citations — the six merged content fixes visibly work.
Generation quality is there; coach score-conformance is the sole substantive blocker (candidate
fixes: retry-on-miscount, prompt-force the 6th score, or accept-with-default — the lane's call).

**Also relevant to this lane from the same day's live runs:** (a) the deployed image's revision
loop ballooned a real PO session's context 92,359→128,802 tokens over an hour until M12's cutoff
fired (run `0a645e36` — the runtime face of the multi-turn drift these fixes cap); (b) forge's
soft_timeout does NOT propagate a cancel to the specialist (zombie session held the single-slot
seat; scoped `docker restart` was the cleanup); (c) the specialist treats a seat 429 as fatal
(no retry) — run `f9794a58` died in 3s on a transient slot collision.
