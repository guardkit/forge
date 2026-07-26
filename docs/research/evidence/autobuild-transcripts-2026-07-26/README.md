# Real `guardkit autobuild --verbose` transcripts — the ADR-ARCH-033 evidence gate, CLOSED
## 2026-07-26 · captured from three real factory builds on the GB10 · feeds the coach-score parser design

ADR-ARCH-033 gated the coach-score work on "capturing a real `guardkit autobuild --verbose`
transcript" (TASK-ABW-OPS AC-OPS-05 — never run). The 2026-07-25/26 coach-v4 session ran three
REAL feature builds; two full transcripts are archived here verbatim (gzip):

- `feat-cv4m-guardkit-autobuild-verbose.log.gz` — guardkit FEAT-CV4M (3 tasks, 2 waves,
  4 turns; includes ONE coach verdict-emission failure → synthetic feedback → turn-2 approval —
  the exact edge a parser must survive). Coach seat: gemma4-coach (legacy contract).
- `feat-sbho-guardkit-autobuild-verbose.log.gz` — guardkit FEAT-SBHO (2 tasks; timeout_budget
  walls; **judged by coach-ft-v4 under the v4 contract** — first production run of the tuned
  coach). Includes `--resume` output.
- (A third run, guardkit FEAT-8AD1 2026-07-25, lives in guardkit's own receipts.)

## The proven line grammar (what a parser may rely on)

Per-turn decision events (the `guardkit.orchestrator.progress` logger):

```
INFO:guardkit.orchestrator.progress:[<ISO8601>] Completed turn <N>: success - Coach approved - ready for human review
INFO:guardkit.orchestrator.progress:[<ISO8601>] Completed turn <N>: feedback - Feedback: <text...>
INFO:guardkit.orchestrator.autobuild:Coach approved on turn <N>
INFO:guardkit.orchestrator.autobuild:Orchestration complete: <TASK-ID>, decision=approved, turns=<N>
INFO:guardkit.orchestrator.parallel_strategy:Wave <N>: max_parallel=<K> (static) [source: feature-yaml]
[guardkit-checkpoint] Turn <N> complete (tests: pass)        <- already counted by _node_running_wave
```

Edge cases present in the archives:
- Verdict-emission failure: `WARNING:...Coach verdict-emission failed ... Emitting synthetic
  feedback decision` followed by a normal `Completed turn N: feedback - ...` line — parsers
  must treat it as a feedback turn, not a crash.
- Timeout walls: tasks ending `timeout` / `timeout_budget_exhausted` with NO final approved
  line — absence of a decision line is a real terminal shape.

## THE LOAD-BEARING NEGATIVE FINDING

**`guardkit autobuild feature` emits NO numeric coach score anywhere on stdout/stderr** (zero
`score`-bearing lines across both archives). `AutobuildState.last_coach_score` /
`aggregate_coach_score` therefore CANNOT be populated by scraping a score line — the assumed
format did not survive contact with the evidence. The honest, evidence-backed semantics
(adopted by FEAT-UBS1C):

- `last_coach_score` = 1.0 when the most recent completed turn's decision line is `success`,
  0.0 when `feedback` (decision-derived, not model-emitted).
- `aggregate_coach_score` = approved-turn ratio (`success` turns / decision-bearing turns)
  across the run so far.
- Richer per-verdict detail (the v4 contract's `coach_turn_N.json` with `contract`/`decision`/
  `findings`) lives in the TARGET repo's orchestrator-private dir
  (`<repo>/.guardkit/autobuild-private/<task>/`) — a legitimate FORGE read (the hold-out
  boundary excludes the Player, not the orchestrator), but a FOLLOW-UP: stdout grammar first,
  file reads only when a consumer needs findings, not scores.

UBS-002's budget guards should be specified against these ratio semantics.
