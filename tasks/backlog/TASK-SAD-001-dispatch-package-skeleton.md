---
id: TASK-SAD-001
title: "Dispatch package skeleton: forge.dispatch models and __init__"
task_type: declarative
status: backlog
priority: high
created: 2026-04-25T00:00:00Z
updated: 2026-04-25T00:00:00Z
parent_review: TASK-REV-SAD3
feature_id: FEAT-FORGE-003
wave: 1
implementation_mode: direct
complexity: 2
dependencies: []
tags: [dispatch, declarative, pydantic, scaffolding-domain]
test_results:
  status: pending
  coverage: null
  last_run: null
---

# Task: Dispatch package skeleton — forge.dispatch models and __init__

## Description

Create the `src/forge/dispatch/` Python package and its declarative models.
This task defines the data shapes consumed by every other task in the feature
(it is the producer for the `CapabilityResolution` extension and the
`DispatchAttempt` / `DispatchOutcome` schemas referenced in
`IMPLEMENTATION-GUIDE.md` §4).

Reuses `forge.discovery.models.CapabilityResolution` unchanged except for an
**append-only** `retry_of: Optional[str]` field, satisfying the open contract
in the existing FEAT-FORGE-002 model.

## Schema

```python
# src/forge/dispatch/models.py
from typing import Literal, Optional
from pydantic import BaseModel, Field

CorrelationKey = str  # 32 lowercase hex chars (validated at boundary, not type)

class DispatchAttempt(BaseModel):
    resolution_id: str
    correlation_key: CorrelationKey
    matched_agent_id: str
    attempt_no: int = 1
    retry_of: Optional[str] = None      # resolution_id of previous attempt

class SyncResult(BaseModel):
    kind: Literal["sync_result"] = "sync_result"
    resolution_id: str
    attempt_no: int
    coach_score: Optional[float] = None
    criterion_breakdown: dict = Field(default_factory=dict)
    detection_findings: list = Field(default_factory=list)

class AsyncPending(BaseModel):
    kind: Literal["async_pending"] = "async_pending"
    resolution_id: str
    attempt_no: int
    run_identifier: str

class Degraded(BaseModel):
    kind: Literal["degraded"] = "degraded"
    resolution_id: str
    attempt_no: int
    reason: str

class DispatchError(BaseModel):
    kind: Literal["error"] = "error"
    resolution_id: str
    attempt_no: int
    error_explanation: str

DispatchOutcome = SyncResult | AsyncPending | Degraded | DispatchError
```

## Schema extension to existing model

Append-only field on `forge.discovery.models.CapabilityResolution`:

```python
class CapabilityResolution(BaseModel):
    # ...existing fields preserved...
    retry_of: Optional[str] = None   # NEW — resolution_id of previous attempt
```

## Acceptance Criteria

- [ ] Package `src/forge/dispatch/` created with `__init__.py` re-exporting
      models.
- [ ] `DispatchAttempt`, `SyncResult`, `AsyncPending`, `Degraded`,
      `DispatchError` Pydantic models added to `src/forge/dispatch/models.py`.
- [ ] `DispatchOutcome` exported as a discriminated union over the four
      variants.
- [ ] `CapabilityResolution.retry_of: Optional[str] = None` added without
      breaking existing FEAT-FORGE-002 callers (verify by running existing
      tests in `tests/forge/discovery/test_discovery.py`).
- [ ] Round-trip test: each variant of `DispatchOutcome` survives
      `model_dump()` → `model_validate()` and the discriminator round-trips.
- [ ] All modified files pass project-configured lint/format checks with zero
      errors.

## Seam Note

This task is the **producer** for two §4 Integration Contracts:
- `CapabilityResolution` record schema → consumers TASK-SAD-002, TASK-SAD-006,
  TASK-SAD-007, TASK-SAD-009
- `DispatchOutcome` sum type → external consumer (FEAT-FORGE-004) — no internal
  consumer in this feature

## Implementation Notes

- Pydantic v2 `BaseModel`; use `Literal["..."]` discriminator on the four
  variants and `default_factory=dict` / `=list` for mutable defaults.
- Do NOT add `correlation_key` to `CapabilityResolution` — it is a transient
  per-attempt key, not a persistent property of the resolution. Keep it on
  `DispatchAttempt`.
- Validate `correlation_key` format (`re.fullmatch(r"[0-9a-f]{32}", k)`) at
  the boundary in TASK-SAD-003 (`CorrelationRegistry.bind`), not in the
  Pydantic model itself — the type is opaque by design.
