# TASK-JNB-101 Plan Audit (Hubbard step 6) — 2026-07-05

Compared: implementation_plan.md v2 (post-arch-review) vs the actual diff.

## Files — planned vs actual

| Planned (v2) | Actual | Notes |
|---|---|---|
| src/forge/config/models.py | ✅ | as planned |
| src/forge/adapters/nats/approval_subscriber.py (decision gate) | ✅ | as planned |
| src/forge/cli/_serve_deps_gating.py (NEW) | ✅ | + subscriber_clock/dedup_ttl pass-throughs (test injectability, discovered at test time) |
| src/forge/cli/_serve_production.py (registry field) | ✅ | as planned (C3) |
| src/forge/cli/serve.py (~8 lines) | ✅ (~30 lines) | grew by the DDR-007 soft-fail guard around gate-parts construction (review-driven) |
| tests/test_approval_config.py | ✅ | as planned |
| tests/cli/test_serve_deps_gating.py (NEW) | ✅ | + TestPublishRefreshClosure, TestServeComposeSeam (review-driven) |
| tests/integration/test_jnb101_production_wiring.py (NEW) | ✅ | + TestOverrideResumes, correlation assertion, 5s budgets (review-driven) |
| tests/forge/adapters/test_approval_subscriber.py (edit if needed) | not needed | no existing test pinned the unconditional emit (full suite green without edits) |
| docs contract + runbook | ✅ | as planned |

## Extra files (not in plan v2) — all justified

- `src/forge/gating/wrappers.py` — additive correlation threading
  (GateCheckDeps.correlation_id + envelope param + 3 stamp sites).
  Review-driven: without it the four-step chain's correlation step is
  inert against real jarvis traffic. Recorded as Deviation 2 in the
  task file.
- `tests/test_forge_config.py` — second round-trip test needed the new
  key (missed in planning; mechanical fallout).
- `tests/forge/test_contract_and_seam.py` — clock-hygiene allowlist
  entry for the pre-existing `resumed_at` timestamp violation
  (**failing on HEAD before this task**; verified via git stash).

## Metrics

- Estimated LOC: ~170 module + ~33 src edits + ~550 tests.
- Actual LOC (git diff --stat): src +~560 (docstring-dense module +
  review fixes), tests +~1080 (two new files + edits). Variance driven
  by the review round (7 confirmed findings fixed with tests) and by
  module docstrings carrying the AC-3 deviation record. No scope creep
  beyond the recorded deviations; JNB-102 territory
  (pipeline_publisher.py, cancelled emits) untouched — verified by the
  acs-scope review lens and by grep.

## Gates at audit time

- Full suite: 5017 passed / 8 skipped; 8 failed + 2 errors are the
  HEAD-pre-existing infra set (fleet-memory NAS, docker image builds,
  sidecar e2e, real-broker BDD) — verified identical with the diff
  stashed. Clock-hygiene contract test now PASSES (failed on HEAD).
- ruff check + ruff format: clean on all touched files (repo's
  configured toolchain; no mypy config exists in forge).

## Verdict

PASS with recorded deviations (task file "Implementation Deviations"
section). Severity: medium variance (extra wrappers.py file + LOC
overrun), fully explained by the two review rounds; no unexplained
extras.
