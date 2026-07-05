# TASK-JNB-102 Plan Audit — 2026-07-05

Compared: implementation_plan.md v1 vs actual diff (light intensity —
±50% variance thresholds).

## Files — planned vs actual

| Planned | Actual | Notes |
|---|---|---|
| src/forge/gating/wrappers.py | ✅ | field + helper + 3 sites, as planned |
| src/forge/cli/_serve_deps_gating.py | ✅ | callback bound in make_gate_check_deps |
| src/forge/pipeline/cli_steering.py | ✅ | + removed pre-existing unused `Mapping` import (AC-10: modified files must lint clean; F401 exists on HEAD) |
| src/forge/cli/runtime.py | ✅ | SqliteRowCancelledNotifier + default wiring |
| tests ×3 | ✅ | integration (9 tests) + steering seam (6) + notifier/runtime (5) |

## Extra files (justified)

- `src/forge/lifecycle/persistence.py` — the plan assumed
  `find_active_or_recent` could hydrate a row by build_id; in reality it
  takes a feature_id and returns only (build_id, status). Added the
  read-only `get_build_row(build_id) -> BuildRow | None` accessor
  (mirrors the existing `SELECT *` + `_row_to_build_row` read pattern).
  Discovered at implementation time; smaller than the alternatives
  (private-attr reach-through or raw SQL in the notifier).

## AC audit

- AC-1 reject branch: TestRejectEmitsCancelled — exactly one payload,
  cancelled_by=decided_by, reason=notes-or-constant, correlation_id —
  asserted on the WIRE (stronger than the spy the AC asked for).
- AC-2 max-wait: TestMaxWaitEmitsCancelled (gate_check branch) +
  TestDeferTimeoutEmitsCancelled (the :810 defer duplicate — exactly
  one emit proven across the recursion).
- AC-3 CLI: TestCancelledNotifierSeam (3 branches × exactly-once,
  cancelled_by=responder, defaults) + TestSqliteRowCancelledNotifier
  (correlation_id/feature_id enrichment on the wire envelope, canonical
  subject). Production default wiring proven (build_cli_runtime).
- AC-4 negative: approve/override zero emissions + TERMINAL no-op zero
  notifies.
- AC-5 DDR-007: raising callback swallowed WARNING-only with transition
  already recorded; transport failure swallowed by the emitter tier;
  raising CLI notifier swallowed after mark_cancelled.
- AC-6: full suite 5036 passed — only the HEAD-pre-existing infra
  failures (fleet-memory NAS, docker image, sidecar e2e, real-broker
  BDD); zero assertion changes to existing tests.
- AC-7: ruff check + format clean on all modified files.

## Notes

- The CLI transport question the task file glossed over is resolved
  honestly: `forge cancel` now emits FOR REAL via the established
  `forge queue` sync one-shot publish pattern (FORGE_NATS_URL),
  best-effort — not a spy-only seam.
- Theoretical future double-emit (CLI cancel of a PAUSED_AT_GATE build
  once a daemon-side synthetic-reject chain is live) documented in the
  notifier docstring; unreachable today (CLI runtime wires a no-op
  injector; the sidecar never surfaces terminal-cancel SSE).
- Review process: one independent code-reviewer pass + structured
  inline AC audit (the heavyweight multi-agent review was not re-run
  after the 2026-07-05 Fable usage limit). Reviewer verdict: all six
  focus areas (ordering, exception safety, double-emit, get_build_row,
  back-compat, test quality) structurally clean. Two findings, both
  fixed same session: (1) the notifier's emit-authority docstring
  omitted the controlling unreachability reason (the CLI runtime's
  no-op synthetic injector) — docstring now names both reasons and the
  future double-emit hazard; (2) the DDR-007 tests did not pin
  transition-BEFORE-publish ordering — added order-log assertions at
  the reject site, the max-wait site, and the CLI handler
  (test suite now 67 tests across the three JNB-102 files).

## Follow-ups recorded (reviewer findings, out of scope here)

- Pre-existing order inconsistency inside wrappers.py: the reject
  branch runs mark_cancelled → transition_to_cancelled while both
  max-wait branches run transition → mark. Both satisfy DDR-007
  (SQLite before publish either way); cosmetic cleanup candidate.
- Latent race once production gate adapters land: a CLI cancel of a
  PAUSED_AT_GATE build flips builds.status directly while a daemon-side
  gate_check still awaits a response; the later response/timeout will
  hit apply_transition's optimistic-concurrency guard and raise an
  uncaught RuntimeError inside gate_check before any cancelled emit.
  Unreachable today (no production gate adapters; no-op injector);
  must be addressed by the gate-activation follow-up task already
  listed in TASK-JNB-101's plan.

## Verdict

PASS with one recorded extra file (persistence read accessor, justified
above). LOC within light-intensity variance.
