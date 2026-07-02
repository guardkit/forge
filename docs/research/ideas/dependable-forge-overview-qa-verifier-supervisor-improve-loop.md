# Dependable Forge — Overview: the three loops, the stub oracle, and the repo delimiters

## Shared high-level context · 2 July 2026 · Claude Desktop

---

## Purpose

This is the **shared context** for making Forge + AutoBuild a dependable resource. Read it first. Two repo-scoped conversation-starters sit under it — one for `guardkit`, one for `forge` — and this document defines the strategic frame they share **and the seam between them**, so a build session rooted in either repo cannot interfere with the other. It exists specifically to answer: *if this work touches two repos, where are the clear delimiters?*

**"Dependable" has a precise meaning here:** a GREEN means *it works*, not *it type-checks*; and the loop keeps running whether or not frontier is available. Everything below serves those two properties.

---

## The three loops (Rich's model, from `conversation-capture-2026-06-14-forge-meta-harness.md`)

| Loop | Cadence / substrate | What it is |
|---|---|---|
| **plan** | attended · frontier-class | ideation → `/feature-spec` → `/feature-plan` (DF-003, unchanged) |
| **build** | unattended · local | the Unattended Build Service (UBS) — the factory's night shift |
| **improve** | periodic · local | the meta-harness — a local proposer rewrites the build harness from execution traces |

The three components in play each map to one loop, and that mapping is the whole architecture:

- **QA Verifier** → the *oracle inside the build loop* (makes a GREEN trustworthy) **and** the *dial the UBS ratchets autonomy against* ("autonomy follows verification quality").
- **Unattended Build Service (UBS)** → the *build loop itself*. Already scoped (`unattended-build-service-scope.md`, Phase UBS, NOT STARTED); keystone FEAT-UBS-001.
- **Meta-harness improve loop** → the *improve loop*. Already captured (14 June); pre-ADR; proposer eval + trace corpus in progress.

`DECISION-DF-006` governs substrate across all three: frontier is a revocable teacher, not a critical-path worker. Build and improve run local; frontier is confined to attended planning and to a one-time eval/calibration yardstick. A frontier outage is a non-event for the factory floor.

---

## The dependability gap: silent stubs / false approvals

The concrete thing standing between "runs" and "dependable" is that **a GREEN can currently mean "it type-checks," not "it works."** AutoBuild's oracle is (spec/Gherkin) + (tests) + (Coach code review), and a stub returning plausibly-shaped data passes all three — it satisfies the Gherkin at the type level, passes co-generated unit tests (the model that wrote the stub wrote tests it satisfies, or tests that mock the dependency it never really calls), and survives code review because a stub is structurally indistinguishable from a lean real implementation in a diff. Player and Coach share that oracle, so the Coach cannot be a safety net for what the tests miss.

**This is not hypothetical — it is already caught in the wild.** The meta-harness backlog's governance case (`fs-01-coach-false-approval-partial-run`, from FEAT-MEM-04) records a green Coach — "All tasks completed cleanly," 7/7 SUCCESS — hiding a real `app.py` lifespan/DI regression (`DeterministicWriter(store=store)` missing `settings`, breaking `test_app_lifespan`), because the smoke gate missed wave-4 changes and per-task verification scoped tests too narrowly. It was caught only by an independent full-suite run.

**The fix is an oracle the Player did not author, exercising real behaviour end-to-end:** an anti-stub AST scan, a coverage/reachability gate, and — the real one — a behavioural round-trip against the live dependency (for fleet-memory, exactly what FEAT-MEM-05 already is). These are deterministic Python that need no fine-tune; the fine-tuned Coach reads and generalises over that evidence afterwards. This is the QA Verifier's Phase 0.

The loop closes on itself neatly: **the QA Verifier's behavioural-evidence gates are the fix for `fs-01`, and the meta-harness eval corpus is how you *measure* that they work** (the `fs-*` category pass rate). Dependability becomes a number the improve loop tracks, not a hope.

---

## How the pieces fit

```
improve loop  (meta-harness, LOCAL proposer)     — rewrites the harness from traces; measures fs-* pass rate
   └── build loop  (UBS night shift, LOCAL)        — drives autobuild, Mode C fix/resume, escalates to Rich's phone
         └── guardkit autobuild  (engine)           — Player-Coach per feature/wave  [Rich's moat — not replaced]
               └── QA Verifier  (the Coach)          — LLM judge + deterministic behavioural evidence
                     └── behavioural oracle           — independent round-trip (FEAT-MEM-05 etc.) — the stub catcher
```

Nothing here replaces anything else. The GuardKit Player-Coach stays the engine; the UBS is the local driver that was previously Claude-Code-by-hand; the QA Verifier is the oracle that makes the driver *safe* to run unattended; the improve loop is what makes all of it get better over time from its own exhaust.

---

## The repo delimiters (the answer to "where are the clear delimiters?")

Cross-repo work from a single in-repo session is a dodgy process precisely because two sessions can edit each other's ground and diverge. The rule that removes the problem:

**Each build session owns exactly one repo. The only coupling is a declared, versioned contract that neither session edits unilaterally.**

| | `guardkit` owns | `forge` owns |
|---|---|---|
| Components | the Coach; the QA Verifier evidence gates + fine-tune; everything behind `--coach-model` (the verdict + the evidence bundle) | the UBS daemon; the `autobuild_runner` node bodies; budget guards; notifications; the meta-harness improve loop; the trace store |
| A session here… | works **only** in `guardkit`; never edits `forge` | works **only** in `forge`; never edits `guardkit` |

**The seam** is the `guardkit autobuild` CLI (invoked by `forge/adapters/guardkit/run.py`) plus the `--coach-model` verdict/evidence-bundle schema. `forge` consumes `guardkit` as an **installed dependency** across that seam — it reads the verdict and exit status as a black box and never reaches into the Coach internals. `guardkit` never touches the UBS.

**The contract at the seam is versioned in this document** (below), and any change to it is a coordinated decision — an ADR — not something a single session makes on its own. That is the delimiter: separate working directories, one declared contract, changes to the contract escalated out of the session.

### The seam contract (v1 — change only via ADR)

- **Invocation:** `forge` invokes `guardkit autobuild <feature>` per task/wave via `adapters/guardkit/run.py`. Graph shape and `AutobuildState` schema are **frozen** (the bridge translator depends on them).
- **Verdict surface:** the Coach's decision is consumed via `--coach-model` output — `{decision, score, issues, criteria_met, quality_assessment}` plus a `behavioural_evidence` block (the QA Verifier's addition). `forge` reads these; it does not compute them.
- **Autonomy dial:** the UBS's `max_review_cycles` / threshold config (FEAT-UBS-002) keys off `last_coach_score` / `aggregate_coach_score`. The *meaning* of those scores is owned by `guardkit`; the *policy* over them is owned by `forge`.
- **Substrate:** both sides local (DF-006). Frontier appears on neither.

---

## Sequencing

1. **`DECISION-DF-006`** filed (done) — the substrate constraint both specs inherit.
2. **QA Verifier Phase 0** (`guardkit`) — deterministic gates (anti-stub AST, coverage/reachability, FEAT-MEM-05 as the behavioural oracle). Cheap, no fine-tune, closes the `fs-01` class. **This is the highest-leverage first move** and it de-risks everything downstream.
3. **FEAT-UBS-001** (`forge`) — wire the `autobuild_runner` placeholders to the guardkit adapter (the keystone that makes the night shift real). Gated by QA Verifier Phase 0 for unattended safety.
4. Then **QA Verifier Phase 1** (fine-tune) + **UBS-002/003/004**; the improve loop after.

**Honest priority note.** Rich's own June sequencing (findings D15; the 14 June capture's 20 June status update) places the **LPA HSBC demo (9 July)** and the **output-side deploy/verify loop** *ahead* of this build-side improve work. If the demo is still live, QA Verifier Phase 0 is the only item here cheap enough to run in parallel without stealing focus; the UBS keystone and the improve loop are post-demo. This is a flag, not a blocker — but sequence with it in view rather than letting build-side dependability quietly displace the demo.

---

## Which doc to open for each build

- **`guardkit` session** → `guardkit/docs/research/ideas/qa-verifier-behavioural-evidence-gates-conversation-starter.md`
- **`forge` session** → the existing `forge/docs/research/ideas/unattended-build-service-scope.md` + its DF-006/coupling addendum (`unattended-build-service-df006-and-supervisor-addendum.md`)

---

## Related documents

- `DECISION-DF-006` — frontier is a revocable teacher (substrate constraint).
- `unattended-build-service-scope.md` + `unattended-build-service-build-plan.md` — the build loop (UBS).
- `conversation-capture-2026-06-14-forge-meta-harness.md` — the improve loop (proposer, trace corpus, `fs-01`).
- `factory-scaling-and-output-bottleneck-findings.md` — the June strategic anchor (output-side bottleneck; "autonomy bought per step").
- `proposer-eval-build-plan.md` · `ADR-FLEET-001-trace-richness.md` — the improve loop's eval slate and trace schema.

---

*Prepared 2 July 2026 · shared context for the QA Verifier (guardkit) and UBS/Supervisor (forge) work.*
*Read before either repo-scoped starter. Owns the seam contract between the two.*
