---
complexity: 3
dependencies:
- TASK-FFEC-001
feature_id: FEAT-FFEC
glue_intent: true
id: TASK-FFEC-002
implementation_mode: direct
parent_review: TASK-REV-FFEC
task_type: feature
title: Add endpoint to router
wave: 2
---

Expose the creation statistics endpoint in the users router.

## The words of the request this task serves

> Get Users Created Per Day: returns the number of users created on each of the last 7 days (today and the six days before it), oldest first, counting soft-deleted users too.

## Files to Create

- `src/users/router.py` (if not existing, otherwise modify)

## Files to Modify

- `src/users/router.py`

## Acceptance Criteria

- GET /users/created-per-day endpoint exists
- Returns 200 OK on success
- Uses the implementation from TASK-FFEC-001
- Response shape matches the schema from TASK-FFEC-003

## Implementation Notes

- Use async def for route handler
- Inject database session via dependency injection
- Ensure endpoint is documented in OpenAPI