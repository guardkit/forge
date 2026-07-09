# WS3-S5 — Adversarial merge gate (attended v1) — 2026-07-09

**Session:** L18 / WS3-S5 (Opus 4.8, forge lane). **Q2 = ATTENDED-V1** (Rich,
2026-07-09, WS3 §7 dated note) — the gate ships now with Rich as the checkpoint;
reviewer-seat SLM localisation stays WS4's.

## What shipped

Formalizes the practiced **N-reviewers / ≥2-refuters / refuted-by-default /
executed-reproduction** workflow (LPA-14/15; the FEAT-DD4F post-merge review as
the exemplar record — 16/16 confirmed, 0/32 refutations) as a forge merge-gate
STAGE for agent-built features. v1 = an **attended checkpoint**.

New package `src/forge/review_gate/`:

- **`models.py`** — the F14 emission targets (`ReviewFindingsRecord`, `Finding`,
  `Refuter`, `ReviewStats`, `ReviewSubject`) + the raw fan-out inputs
  (`RawFinding`, `RefuterVote`) + loud review-input parsers (`raw_finding_from_dict`
  rejects a reviewer that asserts its own `status` — the verdict is the gate's).
- **`assembler.py`** — the refuted-by-default core. `assemble_review_findings`
  derives each finding's `confirmed`/`refuted` status; it is **never trusted from
  the input**. A finding without a non-empty `executed_reproduction` is
  structurally unable to reach `confirmed` (LPA-15). A critical/high finding is
  confirmed only if it carries ≥`min_refuters` (≥2) refuters AND a majority
  return `not_refuted` (survives refutation); a crit/high with too few refuters
  is a fan-out contract violation and **raises** (never emit an unchallenged
  serious finding).
- **`record.py`** — emits the F14 `review-findings` YAML **natively** (guardkit
  consumed as a subprocess black box, seam v1 frozen, DF-001 — the guardkit model
  is never imported) and validates it across the frozen CLI seam via
  `guardkit qa validate review-findings`. An absent validator **raises**, never a
  silent pass.
- **`reviewer.py`** — the fan-out seat protocol `ReviewerInvoker`. The PRODUCTION
  DEFAULT `UnconfiguredReviewerInvoker` **raises loudly** (reviewer-seat SLMs are
  WS4's scope, DF-001) — an unconfigured seat is never a silent empty review.
- **`stage.py`** — `MergeReviewGateRunner`: build packet → reviewer fan-out (one
  reviewer per dimension, then the runner OWNS refuter dispatch: exactly
  `min_refuters` independent refuters per crit/high proposal) → refuted-by-default
  assembly → F14 emission → [optional] guardkit validation → disposition. A
  confirmed critical/high finding ⇒ **BLOCKED**, pausing for the checkpoint's
  disposition. The gate never auto-approves a merge over a confirmed crit/high
  finding (DF-009 posture).

CLI `forge review-gate` (`src/forge/cli/review_gate.py`) — the attended entry:
adjudicates a pre-collected reviewer fan-out (the honest v1 shape — an operator
dispatches the reviewers per the DD4F practice and feeds their proposals +
refuter votes in as JSON), emits the F14 record, validates it, and reports the
disposition. Exit 0 = clean, 4 = BLOCKED (disposition is the checkpoint's), 1 =
hard error.

Config `ReviewGateConfig` (`review_gate` on `ForgeConfig`), **default OFF**
(`enabled=False`) — same rollout pattern as `deploy.enabled`. Flag OFF is a
byte-for-byte no-op: nothing dispatches a review and the attended CLI refuses to
run.

## Attended-v1 posture — the gate mechanics reused, not re-implemented

Per DF-001, on the unattended critical path the reviewer seats must be local;
those local SLM seats are WS4's. v1 therefore:

- **Reviewer fan-out backend** = an injected seam; the production default raises
  loudly (no silent empty review). The gate is proven end-to-end by injecting a
  fixture reviewer that replays the DD4F findings.
- **Disposition pause** reuses the EXISTING approval-gate machinery (Gate
  G1-proven): the runner takes an optional `escalate` callback (the boot-scoped
  approval publisher). When injected, a BLOCKED review invokes it; otherwise the
  runner surfaces `disposition_required=True` and the attended operator
  dispositions the written record directly. **Honest scope:** the live wiring of
  `escalate` into the daemon's approval publisher is NOT done this session (the
  seam is real and unit-tested; the production wire is a documented carry — same
  posture as the deploy stage's `awaiting_approval` routing). The gate is INERT
  in production (`review_gate.enabled=False`) until reviewer-seat SLMs land.

## GATE

- **End-to-end on ONE real agent-built feature** — the FEAT-DD4F / FEAT-SPL-002
  post-merge review replayed as the fixture
  (`tests/forge/review_gate/fixtures/dd4f_review_input.json`, faithful to the
  16/16-confirmed / 0-refutations record). The runner drives the full machinery;
  all 7 findings-with-repro confirm, the record BLOCKS on the confirmed
  critical/high findings.
- **The emitted F14 record validates via `guardkit qa validate`** — run LIVE
  against guardkit's installed F14 schema (`review-findings` kind); not skipped
  (guardkit-py on PATH). Belt-and-braces: forge's native emission is checked
  against the frozen guardkit boundary.
- **Refuted-by-default demonstrably enforced** — a repro-less finding is
  structurally unable to reach `confirmed`, tested directly (`resolve_status`
  across every severity) AND through the full assembly AND on the poisoned DD4F
  fixture (strip the CRITICAL finding's repro ⇒ it drops to `refuted`, one fewer
  confirmed).
- **Full forge suite failing set == the pre-existing infra baseline** —
  **8 failed + 2 errors**, all live-broker/postgres/docker (cancel-paused +
  fleet-memory ×5 + production-image + serve-orchestrator + 2 real-broker errors);
  confirmed identical on pristine HEAD. +67 new review_gate tests pass, 5472
  passed total. The two config round-trip tests were updated additively for the
  new `review_gate` section (the `deploy`/`planning` precedent).
- **Merge-review sweep on my own diff (the DD4F rule, recursively)** — see below.

## Merge review (DD4F rule, applied to this diff)

3 independent adversarial reviewers (correctness · F14 schema conformance ·
refuted-by-default integrity), each refuted-by-default, each requiring an
executed reproduction. **The correctness reviewer surfaced no surviving
defects** (the majority-refute boundary, the repro-less-cannot-confirm core, and
stats tallies all held under executed reproduction). Three findings survived
refutation on the other two dimensions — **all fixed + pinned before commit**:

- **[MEDIUM · conformance] `ReviewSubject` was unvalidated** — a `subject.kind`
  outside the F14 Literal (`workingtree`) flowed through and forge emitted a
  schema-invalid record; silent on the `--no-validate` / runner `validate=False`
  paths. Reproduction: `forge review-gate … --no-validate` exited 0/CLEAN while
  the written record failed `guardkit qa validate` (`subject.kind Input should be
  'tree', 'commit' or 'merge'`). **Fixed:** `ReviewSubject.__post_init__`
  validates `kind ∈ {tree,commit,merge}` and non-empty `ref` on EVERY path (CLI,
  runner, direct), raising `ReviewInputError`. Pinned: `test_subject.py` (5) +
  `test_cli_review_gate.py::test_bad_subject_kind_is_loud_even_without_validate`.
- **[MEDIUM · overclaim, the DD4F sin] "reuses the existing approval-gate
  machinery" was stated as wired fact** — but nothing is wired: no approval
  import in the package, the runner is never instantiated in production, the
  `escalate` hook is only bound to test lambdas. Exactly the "claimed-as-wired,
  actually unwired" pattern the DD4F review kills. **Fixed:** softened the
  docstrings (`__init__.py`, `config/models.py`, `stage.py` module + arg +
  inline comment) to state the escalate seam is **present but UNWIRED in v1**;
  the attended operator dispositions the record directly. (This §, and the
  Attended-v1 posture above, already carried the honest hedge.)
- **[LOW · overclaim] "≥2 *independent* refuters" did not enforce distinct
  refuters** — two votes from the same `who` met the quorum. Direction was
  conservative (over-block), but "independent" was not enforced. Reproduction:
  a critical finding with `[{who:r1},{who:r1}]` confirmed + BLOCKED. **Fixed:**
  the assembler now rejects duplicate `who` within a finding and requires
  ≥`min_refuters` DISTINCT refuters for the crit/high quorum. Pinned:
  `test_assembler.py::test_duplicate_who_is_not_independent` +
  `::test_serious_needs_distinct_refuters_for_quorum`.

Post-fix: full review_gate + config suites **123 passed**; ruff + black clean;
the attended CLI re-run on the DD4F fixture validates live via guardkit and
BLOCKS (exit 4) on the 4 confirmed critical/high findings.

## Guardrails honoured

- guardkit consumed as a subprocess black box (seam v1 frozen) — the guardkit
  F14 model is never imported; validation is the CLI boundary only.
- `planning.enabled` / `deploy.enabled` defaults untouched (both inert);
  `review_gate.enabled` defaults OFF.
- No nats-core changes (no review-domain wire payload in v1 — the F14 record is
  the durable output).
- Live broker READ-ONLY (no broker touched — the gate is filesystem + subprocess).

## Carried (honest scope, named)

1. **Live escalation wiring** — `escalate` into the daemon's boot-scoped approval
   publisher (surface the BLOCKED disposition to the phone loop). The seam is
   built + unit-tested; the production wire lands when the gate is promoted off
   default-OFF (with WS4's local reviewer seats).
2. **Local reviewer-seat SLM backend** (`ReviewerInvoker` production impl) — WS4
   (DF-001). v1 ships the loud-raising unconfigured default + the attended
   file-fed CLI.
3. **Pipeline wiring** — the gate is not dispatched by the Mode A/B/C reasoning
   loop; v1 is the standalone attended CLI, same as the deploy stage's standalone
   runner. Automatic post-review dispatch is a promotion step.
