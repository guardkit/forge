---
paths: "tests/**/*, **/test_*.*, **/*.test.*, .claude/settings.json"
---

# Quality Standards

## Code Quality

### Test Coverage
- **Recommended**: ≥80% line coverage, ≥75% branch coverage
- **Enforcement**: Language-dependent (some languages have better tooling)
- **Flexibility**: Can be adjusted per project in `.claude/settings.json`

**Why not enforced**:
The default template is language-agnostic. Coverage tools and standards vary significantly:
- Go: `go test -cover` (built-in)
- Python: `pytest --cov` (requires plugin)
- Ruby: SimpleCov
- Rust: Tarpaulin or kcov
- PHP: PHPUnit with xdebug
- Some languages lack good coverage tools

Projects using specialized templates (React, Python, .NET) have enforced coverage thresholds.

### Quality Gate Enforcement

All phases include active quality gates:

| Phase | Gate | Enforcement |
|-------|------|-------------|
| **2.5** | Architectural review | Score ≥60/100 or human review |
| **4** | Tests execute | 100% (tests must run) |
| **4.5** | Tests pass | 100% (zero failures) |
| **4.5** | Build/compile | 100% (if applicable) |
| **5** | Code review | Manual approval |
| **5.5** | Plan compliance | Variance review |

### Test Execution (Phase 4)

**Required**: All tests MUST execute without errors
- Tests can fail (that's caught in Phase 4.5)
- But test framework must run successfully
- No missing dependencies or configuration issues

**Stack-specific commands**:
Configure in `.claude/settings.json` if needed:
```json
{
  "testing": {
    "command": "your-test-command",
    "coverage_command": "your-coverage-command"
  }
}
```

## Documentation

### Architecture Decision Records (ADRs)

**When to create ADRs**:
- Architectural pattern choices
- Technology selection decisions
- Breaking changes
- Complex trade-off decisions

**Location**: `docs/architecture/`

**Format**: Lightweight markdown
```markdown
# ADR-001: Use Repository Pattern

## Context
Need consistent data access layer...

## Decision
Implement Repository pattern...

## Consequences
- ✅ Testable data access
- ✅ Consistent interface
- ❌ Additional abstraction layer
```

### Task Tracking

All tasks tracked in markdown:
- **Location**: `tasks/{state}/TASK-XXX.md`
- **Format**: YAML frontmatter + markdown content
- **States**: backlog, in_progress, in_review, blocked, completed

### Implementation Plans

Generated during Phase 2:
- **Location**: `docs/state/{task_id}/implementation_plan.md`
- **Format**: Human-readable markdown
- **Used by**: Phase 2.8 (checkpoint), Phase 5.5 (audit)

## Testing Philosophy

**"Implementation and testing are inseparable"**

Every implementation should include tests. The specific testing approach depends on:
- **Language**: Test frameworks vary by stack
- **Complexity**: Simple tasks may have fewer tests
- **Type**: Unit, integration, or end-to-end
- **Project standards**: Team-specific requirements

## Phase 4.5: Test Enforcement

**Zero tolerance policy** for test failures:

1. **Initial run**: Execute all tests
2. **If failures**: Auto-invoke fix agent (max 3 attempts)
3. **Each attempt**: Modify code, re-run tests
4. **Success**: All tests passing → Proceed to Phase 5
5. **Blocked**: Max attempts exhausted → Move to BLOCKED state

**What gets fixed**:
- ✅ Compilation errors
- ✅ Test assertion failures
- ✅ Logic bugs causing failures

**What doesn't get modified**:
- ❌ Test code (only fixed if provably incorrect)
- ❌ Test specifications
- ❌ Coverage thresholds

## Phase 5.5: Plan Audit

**Verify implementation matches approved plan**:

1. **Load saved plan** from Phase 2
2. **Compare actual vs planned**:
   - Files created/modified
   - External dependencies added
   - Lines of code (LOC) variance
   - Implementation duration
3. **Calculate severity**: low/medium/high based on variances
4. **Prompt for decision**: Approve, Revise, Escalate, Cancel

**Variance thresholds**:
- **Low**: <10% variance, 0 extra files
- **Medium**: 10-30% variance, 1-2 extra files
- **High**: >30% variance, 3+ extra files, major deviations

**Benefits**:
- Catches scope creep automatically
- Validates complexity estimates
- Ensures AI follows plan
- Creates feedback loop for future planning

## Customizing Quality Standards

### Per-Project Configuration

Adjust thresholds in `.claude/settings.json`:

```json
{
  "defaults": {
    "testCoverage": 80,
    "maxComplexity": 10
  },
  "plan_review": {
    "thresholds": {
      "default": {
        "auto_approve": 80,
        "approve_with_recommendations": 60,
        "reject": 0
      }
    }
  }
}
```

### Stack-Specific Settings

For specialized stacks, consider using `/template-create` to generate a custom template with:
- Stack-specific quality gates
- Appropriate test frameworks
- Technology-aware agents
- Pre-configured thresholds

## Quality Metrics Tracking

GuardKit tracks quality metrics for continuous improvement:
- **Architectural review scores**: Trend over time
- **Test pass rates**: Phase 4.5 success rates
- **Plan compliance**: Variance patterns
- **Complexity estimates**: Accuracy feedback

**Location**: `docs/state/metrics/`

These metrics help refine estimation and catch recurring issues.
