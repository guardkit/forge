# Forge Output-Side Loop — Runbook-Driven — Conversation Starter

## For: /system-arch + /system-design session · `forge` (new capability) + `fleet-memory` (first target) · June 2026

---

## Purpose of this document

Context brief for a session that produces **two architecture documents**:

1. **`/system-arch`** — system context, the runbook model + the step-type model
   as constraints, C4 diagrams, the exemplar-first plan, substrate and
   approval-gate decisions, ADRs, open questions.
2. **`/system-design`** — the Runbook/Step schema, the minimal executor, the
   step-type contracts (incl. the shared gate-step contract), the Forge↔Claude
   Code subprocess contract, deploy IaC and smoke-test design (fleet-memory, then
   LPA), NATS event schema, verdict schema, approval payload/audit, target tree.

Paste this at the start of that session, then generate the two documents
sequentially. The strategic anchor for *why* this is the immediate priority is
`factory-scaling-and-output-bottleneck-findings.md` (D11–D15).

---

## What is the output-side loop?

A **Forge capability** that takes a built artifact to a running, verified system
with minimal human attention — the back-half counterpart to AutoBuild.

The organising unit is a **runbook**. When Forge is asked to build a feature, it
**generates a runbook and executes it** — Forge is a *runbook executor that
dispatches by step type*. The runbook **begins where the build plan ends**: its
first unattended step runs AutoBuild on the feature, and the subsequent steps
deploy, verify-and-debug, and gate the irreversible edges. The runbook does three
kinds of thing, at three levels of autonomy (the step types):

- **Autonomous steps** (run AutoBuild, repeatable deploy) — Forge just runs them.
- **Supervised-loop steps** (verify-and-debug) — Forge invokes Claude Code as a
  subprocess against the live system; smoke tests are the verdict, the
  *environment* is the Coach. Logical bugs are fixed in-loop; spec-level issues
  are filed back to the front of the pipeline.
- **Approval-gate steps** (irreversible edges — prod credentials, first IAM,
  PR-merge) — the step sits in `awaiting-approval`, Forge pings, waits for a tap.

The target it changes: a deploy currently costs Rich a focused block of his day.
After this, Forge runs the runbook, pauses at a gate step, pings Rich ("LPA ready
for AWS staging — approve?"), proceeds on a tap, smoke-tests, reports green or
kicks Claude Code at the failures, and only pings again at the next gate or if
genuinely stuck. **Hours-present → seconds-async.**

---

## The bottleneck this solves

Development is fast now (AutoBuild done, local + Claude SDK). The constraint
moved to everything *either side* of development. The output side is **not
harder than development — it is higher blast radius with a different feedback
loop**:

| | AutoBuild (solved) | Output side (this) |
|---|---|---|
| Reversibility | Branch/PR — discard and re-run is free | Real side effects (AWS standup, credential injection, IAM) that do not cleanly undo |
| Verdict | Unit/seam tests as oracle | A live system behaving correctly in its real environment |
| Right approach | Full autonomy in a sandbox | Automate the repeatable middle; gate the irreversible edges |

That difference is the whole design. We do not make it fully autonomous; we make
the repeatable part autonomous and put a cheap async gate at the edges — and the
runbook is what expresses that split cleanly.

---

## The foundation: what already exists (treat as fixed)

- **AutoBuild is done** — Player-Coach loop working locally and via Claude SDK.
  The output-side loop is its back-half mirror, pointed at a running system
  instead of a branch.
- **The build-plan/runbook methodology is proven** — the whole agent factory
  (Jarvis / Forge / specialist-agent / study-tutor / AutoBuild) was built in ~8
  weeks via Claude Desktop ideation → build/scope docs with phased
  `/system-arch…/feature-plan` steps, **Claude Code generating and status-
  updating** as it goes, so "what's next" is always answerable. The runbook is
  that same proven pattern translated across the DF-003 attended/unattended line.
- **The subprocess pattern is established** — the Build Agent invokes GuardKit
  AutoBuild as a subprocess, not a NATS subscriber (ADR-SP-003). Supervised-loop
  steps use the same pattern: Forge invokes Claude Code as a subprocess against
  the live deploy.
- **NATS is the spine and the source of truth** (ADR-SP-002). Forge is a
  NATS-native orchestrator. The loop publishes lifecycle events; it does not
  introduce a new brain.
- **fleet-memory is built and needs standing up** — local, owned, reversible,
  zero external dependencies. The ideal first exemplar.
- **LPA platform is built** (`lpa-platform-poc`) — Keycloak login, form
  extraction, the Moneyhub feature, web app/platform. Not yet stood up because
  it needed manual operator handoff. The second target.

---

## The runbook as the unit of work (the heart of the design)

**Build plan vs runbook — same shape, different consumer.** The build plan is the
*attended* artifact: a human reads it and drives Claude Code through the planning
stages. The runbook is its *unattended* counterpart: Forge executes it through the
build-and-output stages. The boundary between them is exactly the DF-003
attended/unattended line, which makes the handoff natural — the last thing
attended planning produces (the feature plan/spec) is the input Forge turns into a
runbook. **The runbook begins where the build plan ends.**

**Step types express the three categories.** A runbook is an ordered list of
*typed* steps; the category an action belongs to is its step type — autonomous,
supervised-loop, or approval-gate. So Forge isn't "a thing with a deploy phase and
a verify phase and a gate"; it is a **runbook executor that dispatches by step
type**. Two things fall out for free: **resume-on-failure** is "re-enter at step
N", and a **gate** is just a step sitting in `awaiting-approval`.

**The runbook is the single source of execution state.** Status lives per step on
the runbook record — which is exactly what the **dashboard projects**. Rich's
"update status in the doc so I can always ask what's next" affordance survives; it
moves from a markdown file Rich reads to a runbook record the dashboard renders,
and now **James can ask "what's next" too**.

**The generation constraint — this is the load-bearing safety property.** Claude
Code **generates the runbook, fully and autonomously, exactly as it generates
build plans today** — nothing changes in that workflow. The one constraint:
Claude Code **composes the runbook from a vetted, typed step library** —
`deploy_compose`, `run_smoke_tests`, `await_approval`,
`invoke_claude_code_debug`, … — and **parameterises** those steps, rather than
inventing raw shell commands. Same authorship, same automation, **narrower
palette**. This matters because of the one property that separates the output side
from build-plan generation: a build plan's worst case is wasted time (every step
reversible), whereas a runbook's steps **are** the side effects (the deploy step
*is* the deploy). The typed palette is what carries AutoBuild's "tested before it
touches anything" guarantee across to the output side. The model's *latitude* then
lives **inside** the supervised-debug step — Claude Code fixing code and re-running
smoke tests, bounded and gated — **not** in the runbook structure.

**Harvest the step library; don't pre-build an engine.** A general "runbook
engine" is itself a template — building one before fleet-memory deploys is the
over-engineering the exemplar-before-template rule guards against. So split it:
adopt the runbook as the **data model + persisted artifact** from day one (the
dashboard and the gate mechanism depend on it), but keep the **executor minimal**
and let the **step library accrete** from real runbooks. fleet-memory's runbook
needs maybe two step types (`deploy_compose`, `run_smoke_tests`). LPA forces the
next ones into existence (`await_approval`, a credential step). The only thing
deliberate from day one is the **rule** that steps come from the library; the
library's *contents* grow by doing — the same "harvest, don't author" policy
applied one level down.

**Review the runbook before first execution.** Add a *review-the-runbook* gate
before any step fires — the same "review before fix" instinct already in the
pipeline — so the plan is seen before it touches anything.

**PR-merge is (at least initially) a gate.** AutoBuild produces a PR; merging to
main has consequences, and Rich curates PRs today. Make merge an `await_approval`
step in the runbook. This is where Rich's curation sits on the *unattended* path.

---

## Key architectural decisions (resolved — do not reopen)

| # | Decision | Resolution |
|---|----------|-----------|
| D1 | Orchestration source of truth | NATS event bus (inherit ADR-SP-002). The loop is producers/consumers on the existing bus. No framework-as-brain. |
| D2 | How the fix-agent is invoked | Forge invokes Claude Code as a **subprocess** against the live deploy. Same pattern as Build Agent→GuardKit (ADR-SP-003). Forge owns the lifecycle. |
| D3 | Autonomy model | **Three categories, expressed as step types.** Autonomous (deploy). Supervised-loop (verify-and-debug). Approval-gate (irreversible edge). Autonomy bought per *step* (DF-003 extended). |
| D4 | The verdict | Smoke tests are the oracle; the **environment is the Coach**. Verify-and-debug is AutoBuild's shape pointed at a running system. |
| D5 | Irreversible / external actions | **Never automated** — an `await_approval` step, gated by one async, mobile approval. Examples: prod credential injection, first IAM setup, PR-merge. |
| D6 | Moneyhub | A **waiting-on-someone-else** dependency, not an automation problem. It is **not a runbook step**; the loop must not block on it. |
| D7 | Deploy representation | **IaC + smoke tests**, expressed as runbook steps, built once as an exemplar and replayed by Forge. The exemplar is the deliverable; the loop generalises from it. |
| D8 | First exemplar | **fleet-memory** — local, owned, reversible, needed anyway. Build the loop *with fleet-memory as the test subject*: walk away with fleet-memory deployed and the reusable executor. |
| D9 | Second target | **LPA** = the **same executor** + `await_approval` steps for the AWS/credential edges. Prove the shape where a mistake costs nothing, then apply where blast radius is real. |
| D10 | Escalation | Claude Code fixes logical bugs in-loop; **spec-level issues are filed back to the front of the pipeline** (PO agent / `pipeline.feature-planned`) and the runbook pauses, not patched blindly. |
| D11 | Runbook is the unit of work | Forge generates a **runbook** and executes it, **dispatching by step type**. The runbook **begins where the build plan ends** (AutoBuild = first unattended step). Build plan (attended, human-read) and runbook (unattended, Forge-executed) are the same shape across the DF-003 line. |
| D12 | Typed/vetted steps, not freehand shell | Claude Code **generates** the runbook as it does today, but **composes from a vetted, typed step library** and parameterises steps — it does **not** invent raw commands. The palette is the safety property that carries AutoBuild's "tested before it touches anything" guarantee to the output side. Model latitude lives **inside** the supervised-debug step (bounded, gated). |
| D13 | Minimal executor; harvest the step library | Do **not** pre-build a general runbook engine. Adopt the runbook as **data model + persisted artifact** from day one (dashboard + gate depend on it); keep the **executor minimal** and let the **step library accrete** from real runbooks (fleet-memory ≈ 2 types; LPA forces the next). Only the *rule* (steps from the library) is deliberate up front. |
| D14 | Runbook = source of execution state | The runbook record is the single source of execution state — the **dashboard projects it**; "what's next" survives (now James too). Resume-on-failure = **re-enter at step N**; a gate is a step in `awaiting-approval`. |
| D15 | Curation gates on the unattended path | A **review-the-runbook** gate before first execution, and **PR-merge as an `await_approval` step** (at least initially). This is where human curation sits on the unattended path. |

---

## Warnings & constraints

- **Typed steps, not freehand shell, is the load-bearing safety constraint.** A
  hallucinated freehand step runs against AWS *before anyone sees it*. The palette
  **is** the safety property — it is precisely what separates this from build-plan
  generation, where every step is reversible. Runbook generation must never emit
  arbitrary shell.
- **Do not build a general runbook engine before fleet-memory.** The engine
  **emerges** from two or three real runbooks. Pre-building it is the speculative
  over-engineering the policy forbids. Adopt the *data model* now; harvest the
  *executor* and the *step library*.
- **Review the runbook before first execution** — see the plan before any step
  fires.
- **Irreversibility is the hazard.** Deploy steps must be idempotent /
  safe-to-re-run, and every irreversible action MUST be an `await_approval` step.
  No autonomous step may have an effect that cannot be cheaply undone.
- **The verdict is environmental, not unit tests.** Smoke tests exercise the live
  system in its real environment (for LPA: Keycloak login path, the
  form-extraction path, the Moneyhub integration *once creds exist*).
- **Moneyhub credentials are an external blocker** (client_id, RSA keypair, JWKS
  endpoint, redirect URI — still to be chased). The loop degrades gracefully: the
  FinProxy fundraising demo is GPU-free on AWS and **pre-seeds extracted data**
  (DEC-POC-006; ADR-SP-008 demoability exception). Do not let the loop block on
  Moneyhub.
- **LPA OPG PDFs are full-page raster** — Docling VLM is GPU-required (~8 min/doc).
  The AWS demo path assumes **no GPU on AWS**; the deploy/verify steps for the demo
  must not assume GPU there.
- **The approval gate is async and mobile** — same ergonomics as the Slack door
  for James, pointed at Rich. Approve from a phone.
- **Credential scoping** — the fix-agent (Claude Code) must never see prod
  secrets, even while debugging. Mirror LAP's credential-vault principle even
  though we are not adopting LAP.
- **Keep it a Forge capability, not a new service.** Subprocess invocation for the
  agent, producers/consumers on NATS for state. No competing brain.

---

## Step types (the three categories, as runbook steps)

```
Forge: build feature → generate runbook (Claude Code, from the typed step library)
                              │
                              ▼
                    [ review-runbook gate ]   ← human sees the plan before any step fires
                              │
   ┌──────────────────────────┴───────────────────────────────────────────┐
   │ runbook = ordered, typed steps · status per step · the source of state│
   └──────────────────────────┬───────────────────────────────────────────┘
                              ▼
  step type: AUTONOMOUS          e.g. run_autobuild, deploy_compose
      └─ Forge runs it, records status, advances

  step type: SUPERVISED-LOOP     e.g. invoke_claude_code_debug + run_smoke_tests
      └─ Forge → Claude Code (subprocess); smoke tests = verdict
         ├─ green ──────► advance
         ├─ logical bug ► fix in-loop, re-run smoke tests
         └─ spec-level ─► file task → front of pipeline, pause

  step type: APPROVAL-GATE       e.g. await_approval (PR-merge, prod creds, first IAM)
      └─ step sits in `awaiting-approval`; notify Rich; tap → advance
         (waiting-on-Moneyhub is NOT a step — it is out of the runbook entirely)

  resume-on-failure = re-enter at step N
```

Substrate per step follows DF-003: the autonomous deploy and the smoke-test
verdict are deterministic; the *judgment* lives only in the supervised-loop step,
which is the one piece that needs a capable agent.

---

## Exemplar-first plan

| Step | Target | Why | Output |
|------|--------|-----|--------|
| 1 (this weekend) | **fleet-memory** | Local, owned, reversible, zero external deps, needed anyway | fleet-memory deployed + the minimal runbook executor + first 2 step types |
| 2 | **LPA** | Real blast radius; FinProxy pressure | **Same executor**; runbook gains `await_approval` + a credential step; demo stood up |

The proof the framing is right: the jump from zero blast radius (fleet-memory) to
real blast radius (LPA) is a change in **runbook content** (which step types the
runbook uses), **not** in **executor code**. fleet-memory's runbook is trivial —
`deploy_compose`, then `run_smoke_tests`, no gates — because it is local and
reversible. The discipline: do not hand-crank fleet-memory's standup. Spend the
block building the executor *with fleet-memory as the test subject*, and let the
step library start accreting from what that runbook actually reaches for.

---

## Hardware topology

| Machine | Role |
|---|---|
| 2× NVIDIA DGX Spark GB10 (~256GB pooled, ConnectX-7) | Forge runs here; llama-swap on GB10:9000; NATS JetStream; **fleet-memory = local deploy target (same box)** |
| MacBook / Claude Desktop | Planning, this `/system-arch` + `/system-design` session |
| OpenCode / Claude Code on GB10 | Implementation, and the **fix-agent invoked in the supervised-loop step** |
| AWS eu-west-2 (ECS/Fargate CPU) | **LPA = remote deploy target.** Bedrock managed (Haiku) for the fundraising demo per ADR-SP-008; Tailscale reach for remote verify |

---

## Repo structure (target — `/system-design` finalises)

```
forge/
├── runbook/                   ← NEW: the unit of work
│   ├── model.py               ← Runbook + typed Step schema (status, gates-as-data)
│   ├── executor.py            ← MINIMAL: dispatch by step type, persist status, publish events
│   ├── generate.py            ← Claude Code generates a runbook FROM the step library
│   └── steps/                 ← the typed step library (accretes from real runbooks)
│       ├── deploy_compose.py        ← fleet-memory needs this
│       ├── run_smoke_tests.py       ← …and this
│       ├── await_approval.py        ← LPA forces this (shared gate-step contract)
│       └── invoke_claude_code_debug.py
└── ...                        ← existing NATS-native orchestrator

fleet-memory/
├── deploy/                    ← NEW: compose / IaC for standup (driven by deploy_compose)
└── tests/smoke/               ← NEW: smoke tests = the verdict of run_smoke_tests
    └── test_store_roundtrip.py
```

(The earlier `deploy/` + `verify/` + `gates/` split is folded in here: deploy and
verify are step types; the gate is the `await_approval` step type.)

---

## Open questions for /system-arch to resolve

1. **Runbook model + minimal executor** — the Runbook/Step schema, the
   dispatch-by-step-type executor, and the *starting* step vocabulary (kept
   minimal — only what fleet-memory needs). Confirm the executor is minimal and
   the library is **harvested, not designed up front**.
2. **Substrate for the fix-agent** — Claude Code on frontier vs the local
   workhorse (Qwen3.6-35B-A3B). The supervised-loop step is high-judgment but the
   runbook is unattended between gates — does invoking frontier here violate
   DF-001's "no cloud API on the critical path", or does the approval gate keep it
   attended-by-exception? **The load-bearing open question.**
3. **Approval-gate mechanism** — reuse the Slack adapter so there is **one**
   notification-and-approve mechanism for both James and Rich, or a Forge-native
   notification? (Strong lean: reuse Slack — see "single approval mechanism".)
4. **Review-runbook + PR-merge gates** — confirm a review-the-runbook gate before
   first execution, and whether PR-merge is an `await_approval` step from day one.
5. **Idempotency / teardown** — the safe-to-re-run contract per step, and whether
   there is a teardown path. fleet-memory (local) can be torn down freely; LPA
   (AWS) cannot.
6. **Local vs remote targets** — Forge runs the runbook from the GB10;
   fleet-memory is the same box, LPA is remote over Tailscale/AWS. What changes?
7. **Concurrency** — single runbook at a time vs per-project, tying into the open
   build-queue decision in `dev-pipeline-architecture.md`.

---

## Open questions for /system-design to resolve

1. **Runbook schema** — typed `Step` (type, params, status, result) and `Runbook`
   (ordered steps, current step, gates-as-data); the **persisted record the
   dashboard projects**.
2. **Step-type contracts** — `deploy_compose`, `run_smoke_tests`,
   `await_approval`, `invoke_claude_code_debug`: inputs/outputs/status
   transitions. Start with the two fleet-memory needs; the rest accrete.
3. **The gate-step (`await_approval`) contract is the first thing to nail** — it is
   the **shared approval mechanism** behind the deploy gate, the Slack
   approve-to-build, and the dashboard's one deliberate write. Four things depend
   on it.
4. **Forge↔Claude Code subprocess contract** — invocation, working dir,
   credential scoping (the fix-agent must not see prod secrets).
5. **fleet-memory deploy IaC + smoke set** — compose shape; smoke tests
   (Postgres+pgvector up, `AsyncPostgresStore` reachable, embed/store round-trip).
6. **LPA deploy IaC + gate placement** — AWS eu-west-2 ECS/Fargate CPU; Keycloak;
   pre-seeded extraction data; Mock-Bank for the fundraising demo; where the
   `await_approval` steps sit.
7. **NATS event schema** — `runbook-started / step-started / step-result /
   approval-requested / approval-granted / runbook-complete / escalated`,
   extending the existing envelope.
8. **Verdict schema** — reuse the Coach `{decision, score, issues, criteria_met,
   …}` shape, pointed at smoke-test output.
9. **Approval payload + audit** — what is recorded when Rich approves (who, when,
   what intent). The audit trail; never fabricated.

---

## What each command should produce

### /system-arch produces:
- System context (where the loop sits relative to AutoBuild, Forge, NATS, targets)
- The runbook model + the step-type model as binding constraints
- The minimal-executor / harvest-the-library decision (open question 1)
- C4 Level 1 and Level 2 diagrams
- The exemplar-first plan (fleet-memory → LPA; same executor, different content)
- Substrate decision (open question 2) and approval-gate decision (open question 3)
- Resolved arch open questions
- Out of scope for v1 (explicitly: no general runbook engine)
- ADRs (runbook-as-unit; typed-steps-not-freehand-shell; output-side decomposition;
  subprocess pattern reconfirmed; approval-gate)

### /system-design produces:
- Runbook/Step schema and the minimal executor
- Step-type contracts (the two fleet-memory needs + the `await_approval` gate-step
  contract — the shared approval mechanism)
- Forge↔Claude Code subprocess contract (with credential scoping)
- fleet-memory deploy IaC + smoke-test design
- LPA deploy IaC + `await_approval` placement
- NATS event schema for the runbook lifecycle
- Verdict schema (Coach-shaped)
- Approval payload + audit format
- Target file tree with all files specified

---

## Key insight to carry forward

**The runbook is the unit, and it makes the single approval mechanism concrete.**
Claude Code still generates everything — the only new constraint is **typed steps,
not freehand shell**, and that constraint earns its place *solely* because output
steps are irreversible, the one property separating this from the build-plan
generation already proven. Don't pre-build the engine — **harvest** it from
fleet-memory then LPA, where the jump in blast radius is a change in runbook
*content*, not executor *code*. The output-side Coach is the *environment*, not a
model. And the gate step (`await_approval`) is the same `awaiting-approval`-plus-
notification surface as the Slack approve-to-build, the deploy gate, and the
dashboard's one write — **one approval mechanism, one seat at the front for James
and one at the back for Rich**. When `/system-arch` runs, that gate-step contract
is the first thing to nail, because four things lean on it.

---

*Prepared: 19 June 2026 · Revised: 20 June 2026 (runbook-driven model folded in)*
*Use as context for /system-arch and /system-design. Companions: factory-scaling-and-output-bottleneck-findings.md, fleet-gateway-slack-jarvis-door-conversation-starter.md, factory-dashboard-conversation-starter.md*
