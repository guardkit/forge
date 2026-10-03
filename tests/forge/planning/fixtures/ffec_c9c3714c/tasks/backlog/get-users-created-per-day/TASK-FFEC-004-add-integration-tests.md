---
id: TASK-FFEC-004
title: Add integration tests
task_type: testing
parent_review: TASK-REV-FFEC
feature_id: FEAT-FFEC
wave: 3
implementation_mode: task-work
complexity: 4
dependencies:
  - TASK-FFEC-002
  - TASK-FFEC-003
---

Add integration tests for the creation statistics endpoint.

## The words of the request this task serves

> Get Users Created Per Day: returns the number of users created on each of the last 7 days (today and the six days before it), oldest first, counting soft-deleted users too.

## Files to Create

- `tests/users/test_created_per_day.py`

## Files to Modify

- `tests/conftest.py` (if needed for test setup)

## Acceptance Criteria

- Test returns exactly 7 entries
- Test verifies correct date range
- Test verifies ordering
- Test verifies soft-deleted users included
- All modified files pass project-configured lint/format checks with zero errors

## Implementation Notes

- Use pytest with httpx for endpoint testing
- Seed database with users across different days
- Include a soft-deleted user in test data