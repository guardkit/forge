---
complexity: 7
created: 2026-05-06 00:00:00+00:00
dependencies:
- TASK-FRR-PEB-002
documentation_level: standard
estimated_minutes: 120
feature_id: FEAT-PEBR
id: TASK-FRR-PEB-003
implementation_mode: task-work
parent_review: TASK-REV-F010M
parent_task: TASK-FORGE-FRR-F010M
priority: high
status: design_approved
tags:
- forge-serve
- autobuild-runner
- pipeline-lifecycle-emitter
- sse-translation
- producer-stream-event-schema
task_type: feature
test_results:
  coverage: null
  last_run: null
  status: pending
title: SSE → typed pipeline envelope translation layer (Option C primary; Option E
  fallback)
updated: 2026-05-06 00:00:00+00:00
wave: 2
---

# Task: SSE → typed pipeline envelope translation layer

## TL;DR

Implement the SSE-to-typed-envelope translation layer that maps
`langgraph_sdk` `StreamPart` events from `client.runs.join_stream(...)`
into typed `pipeline.*` envelopes (`BuildStartedPayload`,
`StageCompletePayload`, `BuildCompletePayload`, `BuildFailedPayload`).
This is the **dominant Option C risk surface** per the scoping doc — the
contract test below is the primary mitigation.

**Option E (Hybrid) fallback note**: if the SSE event shape proves
insufficient to construct typed envelopes cleanly during implementation
(e.g. silent schema drift across `langgraph-api` minor versions), the
task may be reshaped to consume D-NATS per-stage events instead. **Decide
this no later than the smoke-gate failure of Wave 2** — do not pivot
mid-implementation; re-plan the wave.

This task is the **producer side** of the §4 Integration Contract for
`STREAM_EVENT_SCHEMA` (consumed by T4).

## Locks BDD scenarios (primary)

- @smoke `An autobuild that runs to completion in the sidecar produces the
  full lifecycle envelope sequence on the wire` (with T4)
- @smoke @regression `An autobuild that fails asynchronously inside the
  sidecar produces build-failed on the wire` (with T4)
- @key-example @regression `Every envelope published for a sidecar
  autobuild threads the inbound correlation identifier` (with T4)
- @boundary `A single-stage autobuild produces a build-started, exactly
  one stage-complete, and a terminal envelope`

## Acceptance criteria

- AC-1: `src/forge/lifecycle_bridge/translation.py` exposes a
  `StreamEventTranslator` class with method
  `translate(stream_part: StreamPart, context: BuildContext) -> PipelineEvent | None`.
- AC-2: The translator handles every documented `StreamPart.event` value
  the langgraph-runner sidecar emits during an autobuild run; unknown
  events return `None` and are logged at DEBUG (not WARNING — unknown
  events are routine during langgraph-api minor bumps).
- AC-3: Each typed payload constructed by the translator carries
  `correlation_id` from `BuildContext.correlation_id` (no fallback;
  raises if missing).
- AC-4: A **contract test** round-trips a known `AutobuildState`
  mutation sequence through a recorded SSE stream fixture and validates
  the emitted `pipeline.*` envelopes against the `nats_core.events`
  Pydantic schemas. Fixture lives at
  `tests/forge/lifecycle_bridge/fixtures/sse_stream_canonical.jsonl`
  (records both success and failure paths).
- AC-5: `pyproject.toml` is updated with explicit upper bounds on
  `langgraph-sdk` and `langgraph-api` (e.g. `~=0.3.13` for sdk; check
  current version and lock minor). Bumps require a new contract test
  fixture re-record.
- AC-6: F010C correlation-id AST guard fixture extended with the new
  emit sites the translator introduces (via downstream emitter calls
  in T4 — coordinate with T4 author on the call-site list).
- AC-7: All modified files pass project-configured lint/format checks
  with zero errors.

## §4 Integration Contract — STREAM_EVENT_SCHEMA (producer)

This task **produces** the `STREAM_EVENT_SCHEMA` artifact consumed by
TASK-FRR-PEB-004. See `IMPLEMENTATION-GUIDE.md` §4 for the full contract.
Summary:

- **Artifact**: typed `PipelineEvent` (one of `BuildStartedPayload`,
  `StageCompletePayload`, `BuildCompletePayload`, `BuildFailedPayload`,
  `BuildPausedPayload`, `BuildResumedPayload`, `BuildCancelledPayload`)
- **Format constraint**: Pydantic v1 model from `forge.pipeline.payloads`
  with `correlation_id: str` field always populated, never `None`.
- **Validation method**: T4's seam test imports the translator, feeds a
  recorded `StreamPart`, and asserts the returned `PipelineEvent` is a
  valid Pydantic model with non-empty `correlation_id`.

## Test requirements

- Translation contract test (round-trip success + failure path) per
  AC-4.
- Unknown-event smoke test: translator returns `None`, logs at DEBUG,
  does not raise.
- Correlation-id-missing test: translator raises
  `MissingCorrelationIdError` rather than emitting an envelope without
  the field.
- Property test: every `StreamPart` in the canonical fixture produces
  exactly one envelope or `None` (no double-emits).

## Files to Create

- `src/forge/lifecycle_bridge/translation.py`
- `tests/forge/lifecycle_bridge/test_translation.py`
- `tests/forge/lifecycle_bridge/test_translation_contract.py`
- `tests/forge/lifecycle_bridge/fixtures/__init__.py`
- `tests/forge/lifecycle_bridge/fixtures/sse_stream_canonical.jsonl`

## Files to Modify

- `pyproject.toml`
- `tests/forge/test_pipeline_consumer_correlation_id.py`

## Implementation notes

- Touchpoints: `src/forge/lifecycle_bridge/translation.py` (primary);
  `tests/forge/lifecycle_bridge/fixtures/` (new fixtures);
  `pyproject.toml` (version bounds).
- Reference: `src/forge/dispatch/autobuild_async.py`'s existing
  `LifecycleEmitterAdapter` does the analogous in-process mapping
  (lifecycle string → emit method); this task replicates that shape
  out-of-process on raw `StreamPart` events.
- The `stream_mode="values"` mode carries full `AutobuildState` channel
  snapshots; the translator detects state transitions by comparing
  consecutive snapshots. Reuse `AutobuildState` types from
  `forge.pipeline.autobuild_runner`.
- **Risk gate**: if AC-1's `StreamPart` shape varies across
  `langgraph-api` versions in ways that defeat the translator (verified
  via fixture replay), surface the issue in this task's review and
  trigger the Wave-2-end pivot decision to Option E.

## Coach validation commands

```bash
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_translation.py -x -v
PYTHONPATH=src python -m pytest tests/forge/lifecycle_bridge/test_translation_contract.py -x -v
PYTHONPATH=src python -m pytest tests/forge/test_pipeline_consumer_correlation_id.py -x -v
ruff check src/forge/lifecycle_bridge/translation.py
```