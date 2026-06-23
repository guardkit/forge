# /task-work — TASK-RBX-008 — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /task-work TASK-RBX-008 — 2026-06-22T13:19:28Z

### Prompt

```
/task-work TASK-RBX-008
```

### Assistant response

I'll start by loading the task context for TASK-RBX-008. Let me find the task file.

I found the task in backlog. Let me read it along with the feature context.

Let me examine the relevant source files to understand the current contract.

Now let me read the persistence layer.

Now let me look at the models and the registry's StepOutcome.

Now let me look at the migration schema and existing tests to understand the test patterns and seam markers.

Let me look at the shell-script step handlers to see what handlers actually put in their result dicts, and the existing executor seam/integration test.

Let me look at the executor unit test to understand the fake repo and existing patterns.

Let me check the seam marker definition and an example seam test for the project's convention.

I now have a complete understanding. Let me check for other consumers of `StepResult` / `captured_output` before deciding the model change.

Decision: **Option (b)** — give `StepResult` a typed `payload: Mapping | None` field (default `None`, so all existing constructions stay valid), round-trip it through encode/decode, and have the executor put the handler dict there instead of JSON-stuffing `captured_output`. This preserves the executor-recorded timing/exit_code metadata while making the structured result first-class.

Let me check the pytest `addopts` to know how seam tests are gated.

The plan is clear. Let me implement. Starting with the model — add the `payload` field to `StepResult`.

Now the encode/decode round-trip in the repository:

Now the executor's `_build_step_result` — replace the JSON stopgap with the first-class payload, and drop the now-unused `json` import:

Now update the existing executor test that asserted the old captured-output stopgap:

Now the dedicated real-repo seam test. Let me create it:

Now let me run the affected tests: the executor suite, the persistence runbook suite, and the new seam test.

All pass. Let me run the broader runbook-related suite plus the seam-marked subset to confirm nothing else regressed, and run lint/format checks.

Now lint/format checks (AC-4). Let me check which tools the project uses and run them on the modified files.

zsh doesn't word-split unquoted variables. Let me pass files explicitly.

Formatting applied. Let me run the full affected suite once more to confirm green, then verify nothing else in the repo imports/breaks on the `StepResult` change.

All green (167 passed, 1 skipped). Now let me update the task file — mark acceptance criteria complete and transition to `in_review`.

The task file's status frontmatter now reflects `in_review` (kept in its feature folder `runbook-executor/` to preserve FEAT-RBX grouping, matching how the other RBX tasks are organized).

## ✅ Task Work Complete — TASK-RBX-008

**Harden executor↔persistence result contract**

### What changed

**Model reconciliation (chose option b):** Rather than keep the `captured_output`-as-JSON stopgap, `StepResult` now carries a typed `payload: Mapping[str, Any] | None` field. The handler's structured result is a first-class persisted value, not a JSON blob smuggled inside `captured_output`. This keeps the executor-recorded `started_at`/`completed_at` timing and the status-derived `exit_code` as proper metadata.

- [runbook_models.py](src/forge/persistence/repositories/runbook_models.py#L99-L106) — added `payload` field (default `None`, so every existing `StepResult(...)` construction stays valid)
- [runbook.py](src/forge/persistence/repositories/runbook.py#L142-L175) — `_encode_result`/`_decode_result` round-trip `payload`; legacy rows without the key decode to `None`
- [executor.py](src/forge/executor/executor.py#L396-L418) — `_build_step_result` writes the dict to `payload` with `captured_output=""`; removed the `json.dumps` stopgap and the now-unused `import json`

**Real-repo seam test (new):** [test_executor_seam.py](tests/forge/executor/test_executor_seam.py) — marked `@pytest.mark.seam`, drives `RunbookExecutor.run` against a real `RunbookRepository` + tmp SQLite (no fakes). Asserts status + payload round-trip for passed/failed/multi-step runs, and that the step is observably `running` mid-handler — which only happens if the real `try_claim_step_for_execution` fired. It fails if `update_step_status(result=…)` were dropped (payload → `None`) or if the claim method were missing (`AttributeError`).

Also updated the existing AC-002 assertion in [test_executor.py](tests/forge/executor/test_executor.py#L209-L214) to check `payload` instead of the old captured-output stopgap.

### Quality gates

| Gate | Result |
|------|--------|
| Tests | ✅ 167 passed, 1 skipped (executor + persistence + runbook BDD/CLI/fold) |
| Seam subset (`-m seam`) | ✅ 4 passed |
| `ruff check` | ✅ clean on all 5 files |
| `ruff format --check` | ✅ clean |

All four acceptance criteria are met. Changes are staged in the working tree (not committed) — let me know if you'd like me to commit them.
