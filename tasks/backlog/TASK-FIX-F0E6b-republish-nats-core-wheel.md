---
id: TASK-FIX-F0E6b
title: Republish a corrected `nats-core` wheel so PyPI installs work standalone (option d follow-up)
status: backlog
created: 2026-04-29T12:30:00Z
updated: 2026-04-29T12:30:00Z
priority: medium
tags: [fix, nats-core, packaging, pypi, follow-up, F0E6-followup]
complexity: 5
task_type: fix
parent_task: TASK-FIX-F0E6
scoping_source: tasks/in_progress/TASK-FIX-F0E6-nats-core-import-namespace.md (Decision)
estimated_effort: "2-4 hours (cross-repo: build, test, republish)"
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Republish a corrected `nats-core` wheel so PyPI installs work standalone (option d follow-up)

## Description

Follow-up to TASK-FIX-F0E6's option (d) — the long-term fix that was
deferred so the demo unblock could ship as option (b) (sibling-source).

The published `nats-core==0.2.0` wheel on PyPI is malformed:

- Filename: `nats_core-0.2.0-py3-none-any.whl` (released 2026-04-14)
- dist-info directory: `nats_core-0.2.0.dist-info/` (correct name)
- RECORD contents: only `nats/client/{__init__,connection,errors,
  message,subscription}.py` and `nats/client/protocol/*.py` — **9 files
  total**.
- Missing: the entire `nats_core/` namespace that the source repo's
  `pyproject.toml` declares (`[tool.hatch.build.targets.wheel] packages =
  ["src/nats_core"]`). All of `nats_core.events`, `nats_core.envelope`,
  `nats_core.manifest`, `nats_core.topics`, `nats_core.client`, etc. are
  absent from the wheel.

Both forge and jarvis currently work around this with
`[tool.uv.sources] nats-core = { path = "../nats-core", editable = true }`.
That works for in-repo development but means **standalone PyPI installs
are broken** for any consumer that doesn't have the sibling working tree
checked out. This blocks the cross-team demo story where someone clones
just one of the consuming repos.

This task fixes the upstream wheel build so:
1. `pip install nats-core==0.3.0` (or whatever the next version is)
   from PyPI gives consumers the full `nats_core/` namespace.
2. Forge and jarvis can drop their `[tool.uv.sources]` overrides if
   they choose to (independent decision per repo).

The bug appears to be in the `nats-core` repo's wheel-build configuration
or release pipeline — the source has the right `packages = ["src/nats_core"]`
declaration but somehow the published artefact doesn't include those
files. Diagnose in the `nats-core` repo (`appmilla_github/nats-core/`).

## Acceptance Criteria

- [ ] **Root cause identified** in the `nats-core` repo: explain why the
      published 0.2.0 wheel only contains `nats/client/...` and not
      `nats_core/...`. Likely candidates: a mistaken
      `[tool.hatch.build.targets.wheel] packages` value, a release script
      that built from the wrong directory, or a `.gitignore` /
      `.hatch-ignore` excluding `src/nats_core/`.
- [ ] **Local wheel build verified**: `cd ~/Projects/appmilla_github/nats-core && hatch build` (or `python -m build`)
      produces a wheel whose RECORD contains the expected
      `nats_core/{events,envelope,manifest,topics,client,…}` files.
- [ ] **Local install verified**: `pip install dist/nats_core-*.whl` into
      a fresh venv on a separate machine (or in a chroot/container),
      then `python -c "from nats_core.events import ApprovalRequestPayload"`
      succeeds.
- [ ] **Republished to PyPI** (or to the team's internal index, whichever
      is canonical) with a bumped version (`>=0.3.0`).
- [ ] **Forge and jarvis tested against the new wheel**: confirm both
      repos can resolve `nats-core>=0.3` purely from the index without
      a `[tool.uv.sources]` override.
- [ ] **`[tool.uv.sources]` removal is OPTIONAL per consuming repo** —
      jarvis and forge can choose to keep the sibling override for
      developer-iteration ergonomics. This task only requires that
      removing it is now *possible*, not that it actually happens.

## Out of scope

- Forge's `[tool.uv.sources]` override stays in place even after this
  task lands. Removing it (if desired) is a separate cleanup.
- Jarvis's `[tool.uv.sources]` override likewise. (See
  `~/Projects/appmilla_github/jarvis/pyproject.toml` — its comment
  already says "when the team is ready to consume `nats-core` purely
  from PyPI, this `[tool.uv.sources]` entry can be deleted".)
- API changes to `nats-core` — this is a packaging fix only.

## Source Material

- **Parent task**: [TASK-FIX-F0E6](../in_progress/TASK-FIX-F0E6-nats-core-import-namespace.md) (or completed/ once F0E6 lands) — full investigation and the
  empirical evidence that the published wheel is malformed.
- **Forge fix that ships in F0E6**: `pyproject.toml` `[tool.uv.sources]`
  block (sibling-source workaround).
- **Jarvis reference**: `~/Projects/appmilla_github/jarvis/pyproject.toml`
  has the same workaround with extensive comments.
- **The malformed wheel**: `nats_core-0.2.0-py3-none-any.whl` on PyPI
  (`https://files.pythonhosted.org/packages/.../nats_core-0.2.0-py3-none-any.whl`).
- **Source repo (where the fix lives)**: `~/Projects/appmilla_github/nats-core/`
  at git HEAD `2a39596` (as of 2026-04-29). Source has the correct
  `src/nats_core/` layout; the bug is somewhere in the build/release
  step that produces and uploads the wheel.

## Notes

This task is filed in the **forge** repo for traceability with TASK-FIX-F0E6,
but the actual work happens in the `nats-core` repo. Consider mirroring it
into `nats-core/tasks/backlog/` once started, or treating the forge copy as
the index entry and doing the work directly in `nats-core` without a parallel
task there.
