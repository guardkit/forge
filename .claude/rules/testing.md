---
paths: "**/*.test.*, **/tests/**, **/*_test.*, **/*Test.*, **/*Spec.*"
---

# Testing Guide

## Testing Frameworks

pytest

## Test Structure

- Unit tests: Test individual functions/methods
- Integration tests: Test component interactions
- E2E tests: Test full user workflows

## Coverage Requirements

- Minimum line coverage: 80%
- Minimum branch coverage: 75%
- All public APIs must have tests

## Test Naming

- test_<method_name>_<scenario>_<expected_result>
- Example: test_get_user_with_valid_id_returns_user
- Use descriptive names that explain the test

## Best Practices

- Keep tests focused and isolated
- Use descriptive test names
- Follow AAA pattern (Arrange, Act, Assert)
- Mock external dependencies