---
id: TASK-FFEC-001
title: Implement creation statistics query
task_type: feature
parent_review: TASK-REV-FFEC
feature_id: FEAT-FFEC
wave: 1
implementation_mode: task-work
complexity: 4
dependencies: []
---

Implement the database query that calculates user creation counts for the last 7 days.

## The words of the request this task serves

> Get Users Created Per Day: returns the number of users created on each of the last 7 days (today and the six days before it), oldest first, counting soft-deleted users too. Use the existing users table and its created_at column — do not change the user model or add a migration.

## Files to Create

- `src/users/stats.py`

## Files to Modify

- `src/users/crud.py`

## Acceptance Criteria

- Query returns exactly 7 entries
- Entries are ordered oldest to newest
- Includes today and the 6 preceding days
- Includes soft-deleted users in counts
- Does not modify user model or add migrations

## Implementation Notes

- Use SQLAlchemy 2.0 select/execute patterns
- Ensure query is compatible with asyncpg
- Verify query handles days with zero creations correctly