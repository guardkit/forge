# /task-work — TASK-MP-014 — history

Captured automatically by `.claude/hooks/capture_slash_command.py`.


---

## /task-work TASK-MP-014 — 2026-07-06T21:10:33Z

### Prompt

```
/task-work TASK-MP-014
```

### Assistant response

The composition's `_spawn_drive` already provides per-cid dedup via `live_drives`. Let me check how the previous task-work run (MP-013) was executed in this repo to match conventions, then plan.

No existing composition test pins the `_spawn_drive` dedup. Let me look at how `test_serve_planning.py` composes with fakes to see where the no-double-dispatch test fits.

Now implementing. First the spawner extraction in `_serve_planning.py`:

Now the edits. First, extract the spawner factory as a module-level function:

Now the consumer change — kick the driver on non-terminal duplicates, plus the nak backoff:

Now the core change — kick the driver on non-terminal duplicates. I'll extract a shared helper and use it in both the fresh and duplicate paths:

The flow is correct. Now the docstring updates:

Phase 4 — Testing. Running the affected test suites:

All 63 pass. Let me confirm the new tests actually ran:

All new tests collected and green. Now the full suite to catch regressions:

The BDD failure pre-exists on a clean tree — not caused by this change. Running the rest of the suite past it:

The new spawner (lines 118–168) is fully covered; the 69% module figure reflects pre-existing untested composition paths. `planning_consumer.py` is at 90%.

Phase 5 — quick review: lint + type check:

Remaining lint hits are all pre-existing. Re-running the affected suites after the format/import fix:
