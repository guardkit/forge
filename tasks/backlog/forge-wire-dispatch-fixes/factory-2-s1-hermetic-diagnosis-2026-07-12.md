# S1 — Spec-seat hermetic validation (Factory-2)

**Date:** 2026-07-12 · **Executor:** Opus (Factory-2 S1) · **Repos touched:** specialist-agent = READ/EXECUTE ONLY (git status byte-identical, verified; zero writes, zero commits, zero container ops). All artifacts under `/tmp/f2-s1-out/`.

## VERDICT (headline)

Driving specialist-agent **HEAD**'s two headless tools (`po_feature_spec` / FEAT-SPL-007,
`architect_feature_plan` / FEAT-SPL-008) against the llama-swap seats via the documented
headless entry `core.api.run_specialist_mode(...)` hits **THREE distinct HEAD blockers**, in this
order: (1) a criteria-type crash pre-LLM, (2) the forced Responses API hanging on llama-swap, and
(3) — once both are bypassed — **the coach-0.00 signature reproduces**: gemma4-coach emits 5 of the
6 required criterion scores, so every iteration is scored 0.0/REVISE and the loop exhausts to
`ModeLoopNotAcceptedError(best_score=0.00)`.

- **Gate result:** the deferred "gold-trace live drive" is **NOT dischargeable at HEAD** — the
  headless tools do not produce contract-valid artifacts on real model output. **→ fallback ladder
  applies** (§2.4 handoff; rung B / Factory-2-minus for S3). Blockers 1+2 are trivial fixes;
  blocker 3 (coach criterion-count) is the substantive one and needs a real fix before rung A.
- **coach-0.00 (dfmt5268a4666728): REPRODUCES hermetically** — as a **criterion-count mismatch**
  (coach returns 5/6 scores → `validate_criterion_scores` raises → `_EXTRACTION_FAILURE_SCORE=0.0`
  → REVISE, ×max_iterations → not-accepted). **Crucially this is on openai 2.33.0, NOT 2.45, and
  with the Responses API bypassed** — so the bench's "openai 2.45 / responses-API" prime suspect is
  **REFUTED for the scoring break**. The scoring leg is broken because the **coach model output
  under-produces criterion scores**, independent of the openai client version and the responses API.

## The invocation path (discovered)

- Headless entry: **`specialist_agent.core.api.run_specialist_mode(role, mode, args, *, player_model, coach_model, output_path, max_iterations, ...)`** → returns a `SessionResult` whose `.artifacts` is `{relative_filename: content}`.
- It bootstraps role registration, resolves the mode via `modes.registry.get_mode`, validates
  required args, loads pinned templates, builds `SessionConfig`, then delegates to
  `Orchestrator.run_registered_mode(defn, args, templates)` (session.py:1924) → the generic runner
  → `run_generation_loop(...)`.
- `po_feature_spec` args: `{"from_input": <feature_spec_inputs md>, "stack": ..., "context": [...]}` (only `from_input` required).
- `architect_feature_plan` args: `{"feature_id", "spec_feature", "spec_summary", "target_repo_descriptor"}` all required; `spec_assumptions`/`revision_of`/`validate_feedback` optional. `target_repo_descriptor` is `{repo, test_roots(req), default_branch?, sibling_repos?, stack?}`. Passing a non-blank descriptor IS the "always pass scope" requirement — registered modes are structurally barred from the interactive ClarificationEngine, and a blank descriptor is refused pre-LLM.
- Model wiring: `player_model="openai:qwen36-workhorse"`, `coach_model="openai:gemma4-coach"`; env `OPENAI_BASE_URL=http://localhost:9000/v1` + dummy `OPENAI_API_KEY`. (Provider `local` also lands on `openai:{LOCAL_MODEL}` — same profile.)

## BLOCKER 1 — criteria type bug (crashes in ~0.7s, pre-LLM) — HEAD regression

**`run_registered_mode` passes the wrong type for `criteria`.**

- `session.py:1979-1981`:
  ```python
  mode_criteria = self._criteria                          # a list[CriterionDefinition] (correct)
  if defn.criteria_file is not None:
      mode_criteria = load_criteria(role_dir / defn.criteria_file)   # a CriteriaDefinitions OBJECT (BUG)
  ```
- The legacy path does it correctly: `session.py:1135` → `self._criteria = load_criteria(...).criteria` (note the **`.criteria`**).
- `run_generation_loop(criteria=...)` is typed `list[CriterionDefinition] | None` and does
  `expected_criterion_count = len(criteria)` at **generation_loop.py:777**.
- `CriteriaDefinitions` (criteria/loader.py:34) is a pydantic BaseModel with **no `__len__`** →
  `TypeError: object of type 'CriteriaDefinitions' has no len()`.
- **BOTH headless tools declare a `criteria_file`** (`criteria/feature_spec.yaml`,
  `criteria/feature_plan.yaml`), so both crash the moment they are invoked, **before any model
  call**. This is exactly why the tools are "🟡 BUILT, NEVER DRIVEN" — the default-role fallback
  (`self._criteria`, already a list) works, but any mode with its own criteria file does not.
- **One-line fix:** `session.py:1981` → `mode_criteria = load_criteria(role_dir / defn.criteria_file).criteria`.
- **Why the deployed image didn't hit it:** the deployed (13-day-old) image ran the **Mode-P
  greenfield** path (`run_product_owner`, which uses the corrected `.criteria` at :1135), not the
  headless `run_registered_mode`. So this bug has never been on the live path.
- Repro: `/tmp/f2-s1-out/po_head_unmodified/_error.json` (`TypeError ... has no len()`, elapsed 0.7s).

## BLOCKER 2 — the Responses API is forced; llama-swap doesn't serve it (hangs → APITimeoutError)

- The deepagents **openai provider profile** (`apply_provider_profile`) forces
  **`use_responses_api: True`** for every `openai:` spec (verified: both `openai:qwen36-workhorse`
  and `openai:gemma4-coach` → `{"use_responses_api": true, "max_tokens": 8192}`).
- Player/coach models are built via `agents/player.py::_build_player_model` →
  `langchain.chat_models.init_chat_model(spec, use_responses_api=True, max_tokens=8192)`.
- With `use_responses_api=True`, langchain-openai routes to
  `root_client.responses.with_raw_response.create(...)` → **`POST /v1/responses`**.
- **llama-swap / llama.cpp (build b9430) does NOT serve `/v1/responses`** — the request hangs.
  - Raw curl `POST /v1/responses`: **HTTP 000, 30s hang** (`/tmp/f2-s1-out/resp_probe_body.txt`).
  - langchain path (`/tmp/f2-s1-out/probe_langchain.py`): **`openai.APITimeoutError`** after ~76s
    (2 client retries × ~25s), traceback through `responses.with_raw_response.create`.
  - Control `POST /v1/chat/completions` on the SAME seat: **works in ~2s** (qwen36 replies "OK").
- Note: because line 802 (`player_result = await player.ainvoke(...)`) has **no try/except**, a
  responses-API timeout **propagates as a raised error** (it does NOT reach the coach). So this
  blocker, on its own, is NOT the coach-0.00 — it must be cleared first to even reach the coach.
- **Fix options:** register a harness/app-side openai `ProviderProfile(use_responses_api=False)`
  (chat/completions works), OR ensure the serving stack fronts a Responses-API-compatible endpoint.

## BLOCKER 3 — coach-0.00 REPRODUCES: gemma4-coach under-produces criterion scores (the real bug)

With blockers 1+2 bypassed, the loop runs on real qwen36 output and reaches the coach. On the
`po_feature_spec` gold input the **coach (gemma4-coach) returns only 5 criterion scores where 6 are
required** (feature_spec criteria = `domain_language, single_line_steps,
category_grouping_and_tags, boundary_pairs, manifest_completeness, no_elicitation`). Mechanism:

- `expected_criterion_count = len(criteria) = 6` (generation_loop.py:777).
- `_compute_iteration_score` → `validate_criterion_scores(scores, expected_count=6)` (line 349)
  raises `ValueError: Exactly 6 criterion scores required, got 5`.
- Caught at generation_loop.py:902 → `adjusted_score = _EXTRACTION_FAILURE_SCORE` (**0.0**),
  `verdict = REVISE`, feedback = "Coach output validation error: …".
- Observed **identically on iteration 1 AND iteration 2** (log: "Coach output validation failed …
  Exactly 6 criterion scores required, got 5") → deterministic, not a one-off.
- Result: every iteration scores 0.00 → `best_score` stays 0.00 → after `max_iterations` the runner
  raises `ModeLoopNotAcceptedError(best_score=0.00, verdict=REVISE)` — **the exact
  coach-scored-0.00-then-session-error shape of the bench dfmt5268a4666728.**

**This is the load-bearing diagnosis.** The bench blamed "openai 2.45 / responses-API"; this
hermetic run (openai **2.33.0**, responses API bypassed) reproduces the 0.00 anyway → the scoring
break is the **coach model failing to emit the full criterion-score set**, NOT the client/transport.
Likely real fixes to investigate (next lane): the coach prompt/criteria contract (does gemma4-coach
reliably enumerate all 6? is one criterion being merged/dropped?), or the coach model choice
(coach-ft-v3 / a non-reasoning coach), or loosening `validate_criterion_scores` to tolerate a
missing score as a per-criterion 0 rather than a whole-iteration extraction failure. The count
mismatch (5 vs 6), not a transport error, is the thing to chase.

## Env / deps (scratch)

- Used specialist-agent's **existing `.venv`** (editable install → runs HEAD src). **No `uv run` /
  `uv sync`** (fence honoured). No scratch venv needed — deps present.
- Pinned versions in that venv: **openai 2.33.0**, deepagents 0.5.6, langchain 1.2.17,
  langchain-core 1.3.2, langchain-openai 1.2.1, guardkit-py 0.1.0, gherkin-official 29.0.0,
  pydantic 2.13.3. **NB: openai is 2.33.0 here, NOT the 2.45 of the failed deploy image** — so the
  "2.45/responses-API" suspicion is only partially the story: the responses API is forced
  regardless of client version; the *symptom* differs by whether the endpoint is served.

## Harness-side workarounds (repo untouched) — used to drive past the blockers

Both are process-local; specialist-agent tree is never modified:
1. `register_provider_profile("openai", ProviderProfile(init_kwargs={"use_responses_api": False}))` — routes to `/v1/chat/completions`.
2. `CriteriaDefinitions.__len__ = lambda self: len(self.criteria)` — emulates the 1-line 1981 fix without disturbing the `.criteria` access the Orchestrator `__init__`/legacy path relies on.

## Reproducible harness commands (S3 re-uses verbatim)

```bash
# env
export OPENAI_BASE_URL=http://localhost:9000/v1
export OPENAI_API_KEY=dummy-not-needed
VENV=/home/richardwoollcott/Projects/appmilla_github/specialist-agent/.venv/bin/python

# po_feature_spec (007) — drivers carry the two harness patches inline
$VENV /tmp/f2-s1-out/drive_patched.py \
  --input /home/richardwoollcott/Projects/appmilla_github/api_test/feature_spec_inputs/41a2e3ef-a941-4d8a-9e39-7124f71bf43c.md \
  --out /tmp/f2-s1-out/po_patched \
  --player-model openai:qwen36-workhorse --coach-model openai:gemma4-coach \
  --max-iterations 5

# architect_feature_plan (008) — consumes 007's returned triple
$VENV /tmp/f2-s1-out/drive_plan.py \
  --po-out /tmp/f2-s1-out/po_patched \
  --out /tmp/f2-s1-out/plan_patched \
  --feature-id FEAT-UPTIME-001 \
  --player-model openai:qwen36-workhorse --coach-model openai:gemma4-coach
```
(For a REAL S3 pass the two harness patches should instead be the two source fixes:
session.py:1981 `.criteria`, and a `use_responses_api=False` app-side profile registration.)

## Timings / performance note

- Seat: qwen36-workhorse is a **reasoning** model; under the deepagents agent tool-loop
  (read_product_docs) + coach revision loop, a single 007 drive on the /uptime gold input ran
  **> 20 min** on a heavily-contended GPU (93% util, ECOSYS lane sharing the box). S3 should budget
  generously or cap `max_iterations`, and consider `enable_thinking=false` for the seat (the
  POCONTENT diagnosis's cheaper-alternative) to cut reasoning-token cost.

## po_feature_spec (007) drive outcome

- Ran on the gold input `api_test/feature_spec_inputs/41a2e3ef-….md` (the real Factory-1 /uptime
  handoff), player `openai:qwen36-workhorse`, coach `openai:gemma4-coach`, max_iterations 5.
- **Reached the coach every iteration** (blockers 1+2 bypassed) — so the player DID generate on
  real qwen36 output; the assembler/template/prompt/player legs all work end-to-end.
- **Never accepted:** iterations 1, 2, 3 ALL `coach validation failed: got 5 (need 6)` → score
  0.00 → REVISE. Each iteration took ~8 min on the contended GPU (93% util, ECOSYS lane sharing the
  box), so the harness `--wall-timeout 1500` (25 min) fired during iteration 4 before natural
  exhaustion — but 3/3 completed iterations scored **0.00**, so the outcome is determined: the loop
  would exhaust to `ModeLoopNotAcceptedError(best_score=0.00)`. **No artifacts are returned** (the
  runner raises on non-acceptance; artifacts only come back on success), so:
- **Gherkin normalizer / `guardkit feature validate`: NOT EXERCISED** — there is no accepted
  `.feature` / plan tree to validate, because the coach never accepts. (The normalizer is
  confirmed importable from the venv: `installer.core.commands.lib.feature_spec_normalize`;
  `guardkit feature validate FEAT-XXXX` is at `/home/richardwoollcott/.agentecflow/bin/guardkit`,
  exit 0 valid / 1 errors / 2 not-found — ready for S3 once a plan tree exists.)
- **Artifact quality (from the captured player output — session log
  `po_patched/session/-50e25da6.json`, iteration 0): EXCELLENT and encouraging.** The player
  (qwen36-workhorse) produced a 4.6 KB, fully **on-topic** spec: 29 "uptime" mentions, includes
  `started_at` + `uptime_seconds`, correct **3 demarcated FILE blocks**
  (`uptime-endpoint.feature` / `uptime-endpoint_assumptions.yaml` / `uptime-endpoint_summary.md`),
  clean Gherkin (`@key-example @smoke`, `@boundary`, `@negative`, low-confidence `[ASSUMPTION:…]`
  markers), and **ZERO fabricated citations** (none of `problem-statement.md` / `product-brief.md` /
  `api-spec.md` / `overview.md` / `uptime-api-spec.md` appear). The POCONTENT fixes at HEAD visibly
  work: no off-topic drift, no fabricated source_documents — a marked contrast to the pre-fix
  greenfield behaviour. **The generation quality is there; the ONLY thing preventing a valid
  artifact is the coach criterion-count (blocker 3).**

## architect_feature_plan (008) drive outcome

- **NOT independently driven to a pass.** 008 consumes 007's returned triple, which 007 never
  produced (coach-0.00). Driving 008 on a hand-built triple would (a) not be the gold trace and
  (b) almost certainly hit the SAME coach criterion-count fragility on the architect's own criteria
  (`criteria/feature_plan.yaml`). The `drive_plan.py` harness is written and ready
  (`/tmp/f2-s1-out/drive_plan.py`) — it always passes a non-blank `target_repo_descriptor` for
  api_test (`{repo: api_test, test_roots: [tests], default_branch: main, stack: python-fastapi}`),
  so the interactive ClarificationEngine is never entered — but it is blocked behind the shared
  coach-scoring fix.

## Path to rung A (what S3 needs before the headless tools can carry the pass)

1. **session.py:1981** → append `.criteria` (1-line; unblocks the tools past the pre-LLM crash).
2. **Responses API** → app-side `register_provider_profile("openai", ProviderProfile(init_kwargs={"use_responses_api": False}))`, OR front a Responses-API-capable endpoint. (Without this every `openai:` seat call hangs.)
3. **Coach criterion-count (the real work)** → make gemma4-coach reliably emit all 6 feature_spec
   criterion scores (and the architect's set), OR change coach model, OR relax
   `validate_criterion_scores` to score a missing criterion 0 instead of failing the whole
   iteration. Until this lands, the loop cannot accept and the tools emit nothing.

**Recommendation for S3 tonight:** take **rung B (Factory-2-minus)** — coordinator-run
`/feature-spec --auto` + `/feature-plan` non-interactively (frontier seat, named DF-001 exception,
recorded loudly). The headless target (rung A) is one small session-fix + the coach-scoring fix
away, and that is the specialist-deploy validation lane's job, not tonight's.

## Files (all under /tmp/f2-s1-out/)

- `S1-REPORT.md` (this file)
- `drive_feature_spec.py` — UNMODIFIED HEAD driver (proves blocker 1)
- `drive_patched.py` — 007 driver with the two harness patches (proves blockers 2+3)
- `drive_plan.py` — 008 driver (ready; blocked behind the coach fix)
- `probe_langchain.py` — minimal langchain responses-API probe (proves blocker 2 mechanism)
- `po_head_unmodified/_error.json` — blocker 1 evidence (TypeError len(CriteriaDefinitions), 0.7s)
- `resp_probe_body.txt` — raw `/v1/responses` HTTP 000 / 30s hang
- `po_patched/` — 007 patched run: `po_patched.log` (per-iteration coach 5/6 failures), `session/`
- `po_patched.log` — the coach-0.00 reproduction log

## Fence compliance

specialist-agent: **git status byte-identical before/after** (verified clean, HEAD `96d04dd`
unchanged); zero writes/commits/container ops; used the existing `.venv` (no `uv run`/`uv sync`);
no scratch venv needed; pinned DF-019 template bytes only read; no new models loaded; gpt-oss-120b
untouched. All outputs in `/tmp/f2-s1-out/`.
