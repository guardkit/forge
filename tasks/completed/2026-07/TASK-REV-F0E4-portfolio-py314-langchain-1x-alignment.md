---
id: TASK-REV-F0E4
title: Verify Python 3.14 + langchain-1.x portfolio alignment (per Jarvis FEAT-J004-702C precedent)
status: review_complete
task_type: review
review_mode: diagnostic
review_depth: standard
created: 2026-04-28T00:00:00Z
updated: 2026-04-29T11:55:00Z
previous_state: backlog
state_transition_reason: "Diagnostic review complete; [I]mplement decided; FEAT-F0EP feature folder + 3 deferred-promoted siblings filed"
priority: high
tags: [portfolio-alignment, langchain-1x, python-pinning, FA04-followup, ddd-southwest-demo]
complexity: 0
test_results:
  status: passed
  coverage: null
  last_run: 2026-04-29T11:27:00Z  # subset run (full suite blocked by pre-existing nats_core; see report §5.1)
related_external_reviews:
  - "guardkit/.claude/reviews/TASK-REV-FA04-report.md"  # langchain trapdoor diagnosis (closed)
  - "jarvis/docs/architecture/decisions/ADR-ARCH-010-python-312-and-deepagents-pin.md"  # rev2 pin recipe
  - "guardkit/docs/guides/portfolio-python-pinning.md"  # portfolio policy
  - "study-tutor/.claude/reviews/TASK-REV-57BD-report.md"  # parallel portfolio rollout (template for this review's [I]mplement structure)
review_results:
  mode: diagnostic
  depth: standard
  score: 88  # qualitative — 6/6 versions match, 0 langchain-runtime failures, but 218 pre-existing out-of-scope failures
  findings_count: 7  # F1-F7 in report §9
  recommendations_count: 5  # R1-R5 in report §10
  decision: implement  # user selected [I]mplement at Phase 5 checkpoint
  decided_at: 2026-04-29T11:40:00Z
  report_path: .claude/reviews/TASK-REV-F0E4-report.md
  adr_drafted: docs/architecture/decisions/ADR-ARCH-032-langchain-1x-portfolio-alignment.md
  adr_status: proposed  # flipped to accepted by TASK-LCP-001
  implementation_feature:
    feature_id: FEAT-F0EP
    feature_slug: langchain-1x-pin-alignment
    folder: tasks/backlog/langchain-1x-pin-alignment/
    subtasks: [TASK-LCP-001]
    waves: 1
    deferred_promoted: [TASK-FIX-F0E6, TASK-FIX-F0E7, TASK-FIX-F0E8]  # orthogonal demo-blockers uncovered by the review; flat siblings, not in feature folder
  empirical_evidence:
    python_version: "3.14.2"
    install_outcome: "success (uv pip install -e \".[providers]\"; .venv/bin/python -c \"import forge\" OK)"
    pytest_outcome: "1522 passed, 162 failed (all 162 pre-existing async-infra failures, root cause: missing pytest-asyncio)"
    pytest_log_path: docs/history/portfolio-py314-rebaseline-pytest.txt
    langchain_runtime_failures: 0
    trapdoor_active_today: false
    resolver_picks_jarvis_verified_set: true  # 6-of-6 match against ADR-ARCH-010-rev2 verified set
    resolved_versions:
      langchain: "1.2.15"
      langchain-core: "1.3.2"
      langgraph: "1.1.10"
      langchain-anthropic: "1.4.2"  # Jarvis: 1.4.1 — one patch ahead
      langchain-openai: "1.2.1"
      langchain-google-genai: "4.2.2"
      langchain-community: "0.4.1"  # forge-only — not in Jarvis pin set
      deepagents: "0.5.4"
  out_of_scope_blockers_filed_separately:
    - "nats-core 0.2.0 PyPI wheel publishes under nats/ not nats_core/ → TASK-FIX-F0E6"
    - "[dependency-groups].dev missing pytest-asyncio (162 async-test failures) → TASK-FIX-F0E7"
    - "tests/unit/test_git_operations.py references missing forge.build module → TASK-FIX-F0E8"
    - "task script `pip install -e \".[dev,providers]\"` silently no-ops on dev (PEP 735 vs extras) — bundled into TASK-FIX-F0E7"
---

# Verify Python 3.14 + langchain-1.x portfolio alignment (per Jarvis FEAT-J004-702C precedent)

## Context

Jarvis hit a 33-min autobuild stall on FEAT-J004-702C run 1 (2026-04-27) caused by a stale Python pin (`>=3.12,<3.13`) excluding the user's Mac default Python 3.14, compounded by langchain ecosystem 0.x→1.x version skew when the resolver was given open-floor `>=0.3` pins. The investigation lives at [`guardkit/.claude/reviews/TASK-REV-FA04-report.md`](../../../guardkit/.claude/reviews/TASK-REV-FA04-report.md). The remediation is captured in [`jarvis/docs/architecture/decisions/ADR-ARCH-010-python-312-and-deepagents-pin.md`](../../../jarvis/docs/architecture/decisions/ADR-ARCH-010-python-312-and-deepagents-pin.md) Revision 2:

- `requires-python = ">=3.11"` (open upper bound)
- langchain ecosystem pinned to coherent 1.x with `<2` caps:
  - `langchain-core>=1.3,<2`
  - `langchain>=1.2,<2` (added explicitly; was implicit transitively)
  - `langgraph>=1.1,<2`
  - `langchain-openai>=1.2,<2`
  - `langchain-anthropic>=1.4,<2` (in providers)
  - `langchain-google-genai>=4.2,<5` (in providers)
- Empirical run on Python 3.14 with this pin set: 25 test failures → 1 pre-existing docstring drift. Validated end-to-end via Jarvis FEAT-J004-702C run 2 (12 tasks completed cleanly across Waves 1-4 before an unrelated complexity/timeout failure on Wave 5; see [TASK-REV-9D13](../../../guardkit/tasks/backlog/TASK-REV-9D13-diagnose-J004-013-timeout-budget-exhausted.md)).

The portfolio rollout was paused while the orchestrator-side Wave 5 issues were resolved (CEIL/WALL/FRSH/FLOR family in `guardkit/tasks/backlog/autobuild-stall-resilience/`, all completed 2026-04-28). With Jarvis now stable end-to-end, this review picks up the rollout.

**Forge is DDD South West demo-critical** (per the autobuild-stall-resilience README: "Autobuild builds jarvis/study-tutor/forge for the demo"). This review is therefore **high priority** — any latent forge-side breakage caused by the same langchain/langgraph version-skew pattern needs to surface and be fixed before the demo.

## Current pin state (read directly from `pyproject.toml` — pre-review snapshot)

```toml
requires-python = ">=3.11"

dependencies = [
    "deepagents>=0.5.3,<0.6",
    "langchain>=1.2.11",          # 1.x ✓
    "langchain-core>=1.2.18",     # 1.x ✓
    "langgraph>=0.2",             # 0.x ← MISMATCH (Jarvis-precedent risk pattern)
    "langchain-community>=0.3",   # likely 0.x band
    "langchain-anthropic>=0.2",   # 0.x — should be >=1.4,<2
    "langchain-openai>=0.2",      # 0.x — should be >=1.2,<2
    "langchain-google-genai>=2.0", # 2.x — should be >=4.2,<5
    ...
]
```

**This is the same shape as Jarvis was when it broke**: langchain/langchain-core on 1.x but langgraph and the providers still on 0.x. With the resolver given those open floors, on a fresh install a newer langchain-core 1.x pulls in deps that expect a paired langchain 1.x while langgraph 0.x's middleware still imports the 0.x compat helpers. The exact `block_translators.langchain_v0` ModuleNotFoundError that broke Jarvis can recur here.

This pattern is currently latent — if forge happens to have a stale `uv.lock`/venv with mutually compatible 0.x pairs, runtime works today. The *next* fresh install on a clean machine (or after a `uv sync --upgrade`) is when it surfaces. The DDD demo's clean-machine setup risk is non-trivial.

## Goal

Apply the FA04 recipe to forge: empirically confirm that pinning the langchain ecosystem to coherent 1.x with `<2` caps (matching Jarvis's ADR-ARCH-010-rev2 set) works on Python 3.14, then capture the rebaseline as a forge-side ADR (next available number) referencing ADR-ARCH-010-rev2 as the upstream precedent. **No GuardKit changes; no Jarvis changes — fixes live in this repo.**

## Source artefacts

- This repo: `pyproject.toml`, `uv.lock`, `tests/`, `docs/architecture/decisions/`
- Empirical Jarvis run-2 evidence: `jarvis/docs/history/autobuild-FEAT-J004-702C-run-2-history.md` (Waves 1-4 are the validated baseline)
- Jarvis ADR rev2: `jarvis/docs/architecture/decisions/ADR-ARCH-010-python-312-and-deepagents-pin.md`
- Portfolio guide: `guardkit/docs/guides/portfolio-python-pinning.md`
- GuardKit `template-validate` rule (informational): `guardkit/installer/core/lib/template_validation/sections/section_01_manifest.py::_validate_python_pin`

## Investigation scope

1. **Empirical 3.14 install + test run**:
   ```bash
   cd /Users/richardwoollcott/Projects/appmilla_github/forge
   mv .python-version .python-version.bak 2>/dev/null
   rm -rf .venv
   uv venv --python 3.14 .venv
   uv pip install --upgrade --python .venv/bin/python -e ".[dev,providers]"
   .venv/bin/python -m pytest --tb=no -q | tee /tmp/forge-3.14-pytest.log
   mv .python-version.bak .python-version 2>/dev/null
   ```
   Capture: resolved versions of langchain-* / langgraph / deepagents, pytest pass/fail counts, error categorisation.

2. **Failure categorisation** (per the FA04 playbook):
   - **Pin guard tests** (if any) — by-design, update lockstep
   - **langchain runtime errors** — `block_translators.langchain_v0` or similar — confirms the mismatch hypothesis
   - **API-level breakages** — langchain 1.x removed/renamed APIs that forge code uses
   - **Pre-existing flakes** — unrelated to this work

3. **Pin recommendation**:
   - Match Jarvis's set verbatim where applicable, OR
   - Tighten only what's broken, leaving stable-on-3.14 deps untouched.
   - Rationale: the goal is portfolio coherence, not maximum churn.

4. **forge-specific dependency pattern check**: forge has `langchain-community>=0.3` which Jarvis doesn't. Verify whether `langchain-community` has a 1.x or whether 0.x is still current — that influences whether this dep needs pinning.

5. **CI / lockfile check**: forge has a `uv.lock`. Re-locking on 3.14 with the new pins should produce a coherent lockfile. Verify before committing.

6. **ADR**: file the rebaseline as `ADR-ARCH-XXX` (next number after `ADR-ARCH-031`) referencing ADR-ARCH-010-rev2 as the cross-repo precedent. Include the verified-versions table and the rationale.

## Acceptance criteria

- [ ] Empirical 3.14 + `uv pip install --upgrade -e ".[dev,providers]"` succeeds and `.venv/bin/python -c "import forge"` works.
- [ ] Full pytest run captured to `docs/history/portfolio-py314-rebaseline-pytest.log` (or similar).
- [ ] Resolved versions for `langchain-core / langchain / langgraph / langchain-openai / langchain-anthropic / langchain-google-genai / langchain-community / deepagents` documented.
- [ ] Failure categorisation: each failing test sorted into one of (pin guard / langchain runtime / API-level break / pre-existing).
- [ ] Pin update recommendation: explicit diff against current `pyproject.toml` showing only the necessary changes, with `<2` caps applied per Jarvis's pattern.
- [ ] New ADR (`ADR-ARCH-XXX-langchain-1x-portfolio-alignment.md`) drafted with rationale, verified-versions table, and cross-repo precedent reference.
- [ ] Recommendation on whether forge needs an `[project.optional-dependencies].providers` rebaseline (Jarvis did).
- [ ] Recommendation on whether forge needs a portfolio-pinning guide reference in its `CLAUDE.md` (consistent with the GuardKit-side template treatment).
- [ ] No proposed changes to GuardKit or Jarvis — fixes live in this repo.
- [ ] Report saved to `.claude/reviews/TASK-REV-F0E4-report.md` (or per `/task-review` convention).

## Out of scope

- Implementing the pin updates — that becomes a follow-up `/task-create` + `/task-work` after the review's [I]mplement option.
- Re-investigating the langchain trapdoor itself (closed by FA04).
- Re-investigating the orchestrator complexity/timeout family (closed by 9D13 + CEIL/WALL/FRSH/MAXT/FLOR).
- Other portfolio repos — each has its own review task in its own `tasks/backlog/`.

## Suggested workflow

```bash
/task-review TASK-REV-F0E4 --mode=diagnostic
# Run the empirical 3.14 install + pytest.
# Categorise failures.
# Compare resolved versions against Jarvis's verified set (langchain-core 1.3.0, langchain 1.2.15,
#   langgraph 1.1.9, langchain-anthropic 1.4.1, langchain-openai 1.2.0, langchain-google-genai 4.2.2).
# Draft the pin diff and the ADR.
# Surface the [A]ccept / [I]mplement / [R]evise checkpoint.
```

## References

- Cross-repo (read-only): `guardkit/.claude/reviews/TASK-REV-FA04-report.md`
- Cross-repo (read-only): `jarvis/docs/architecture/decisions/ADR-ARCH-010-python-312-and-deepagents-pin.md` Revision 2
- Cross-repo (read-only): `guardkit/docs/guides/portfolio-python-pinning.md`
- Cross-repo (read-only): `guardkit/tasks/backlog/autobuild-stall-resilience/IMPLEMENTATION-GUIDE.md`
- This repo: `pyproject.toml`, `uv.lock`, `tests/`
