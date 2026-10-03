---
id: TASK-FFEC-003
title: Create response schemas
task_type: declarative
parent_review: TASK-REV-FFEC
feature_id: FEAT-FFEC
wave: 2
implementation_mode: direct
complexity: 3
dependencies:
  - TASK-FFEC-001
---

Define the Pydantic schemas for the creation statistics response.

## The words of the request this task serves

> Get Users Created Per Day: returns the number of users created on each of the last 7 days (today and the six days before it), oldest first, counting soft-deleted users too.

## Files to Create

- `src/users/schemas.py`

## Files to Modify

- `src/users/schemas.py`

## Acceptance Criteria

- UserCreationStat schema with date and count fields
- Response schema as a list of UserCreationStat
- All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Use Pydantic v2
- Ensure date format is ISO 8601