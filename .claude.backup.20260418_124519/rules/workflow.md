---
paths: "tasks/**/*"
---

# GuardKit Workflow

## Phase Execution

GuardKit executes tasks through a structured workflow with built-in quality gates:

```
Phase 1: Requirements Analysis (skipped in GuardKit - use RequireKit for formal requirements)
Phase 2: Implementation Planning
Phase 2.5: Architectural Review (SOLID/DRY/YAGNI scoring)
Phase 2.7: Complexity Evaluation (auto-proceed routing)
Phase 2.8: Human Checkpoint (complexity-based)
Phase 3: Implementation
Phase 4: Testing (compilation + test execution + coverage)
Phase 4.5: Test Enforcement Loop (auto-fix, max 3 attempts)
Phase 5: Code Review
Phase 5.5: Plan Audit (verify implementation matches plan)
```

## Quality Gates

| Gate | Threshold | Action if Failed |
|------|-----------|------------------|
| **Compilation** | 100% | Task → BLOCKED |
| **Tests Pass** | 100% | Auto-fix (3 attempts) then BLOCKED |
| **Line Coverage** | ≥80% | Request more tests |
| **Branch Coverage** | ≥75% | Request more tests |
| **Architecture** | ≥60/100 | Human checkpoint |
| **Plan Compliance** | 0 violations | Variance review |

## Development Modes

### Standard Mode (Default)
```bash
/task-work TASK-XXX
```
- Implementation and tests together
- Fastest for straightforward features
- All phases execute in sequence

### TDD Mode
```bash
/task-work TASK-XXX --mode=tdd
```
- RED: Write failing tests first
- GREEN: Implement minimal code to pass
- REFACTOR: Improve code quality
- Best for complex business logic

### BDD Mode (Requires RequireKit)
```bash
/task-work TASK-XXX --mode=bdd
```
- Requires Gherkin scenarios from RequireKit
- Generates step definitions automatically
- Implements to pass scenarios
- Best for formal behavior specifications

## Workflow Commands

### Create Task
```bash
/task-create "Task title" [priority:high|medium|low]
```

### Work on Task
```bash
/task-work TASK-XXX [flags]
```

**Common flags**:
- `--mode=tdd|standard|bdd` - Development mode
- `--design-only` - Stop at design approval
- `--implement-only` - Use approved design
- `--micro` - Streamlined for trivial tasks
- `--no-questions` - Skip clarification phase

### Complete Task
```bash
/task-complete TASK-XXX
```

### Check Status
```bash
/task-status [TASK-XXX]
```

## Task States

```
BACKLOG → IN_PROGRESS → IN_REVIEW → COMPLETED
              ↓              ↓
           BLOCKED        BLOCKED
```

**State Descriptions**:
- **BACKLOG**: New task, not started
- **IN_PROGRESS**: Active development
- **IN_REVIEW**: Passed quality gates, awaiting approval
- **BLOCKED**: Quality gates failed, needs intervention
- **COMPLETED**: Finished and archived

## Complexity Routing

Tasks are automatically routed based on complexity score (1-10):

| Score | Level | Checkpoint |
|-------|-------|----------|
| 1-3 | Simple | AUTO_PROCEED (no review needed) |
| 4-6 | Medium | QUICK_OPTIONAL (10s timeout, optional review) |
| 7-10 | Complex | FULL_REQUIRED (mandatory human review) |

**Force triggers** (always FULL_REQUIRED):
- Security keywords (auth, password, encryption)
- Schema changes (migration, database)
- Breaking changes (public API modifications)
- User flag (`--review`)

## Micro-Task Mode

For trivial tasks (complexity 1, single file, <1 hour):

```bash
/task-work TASK-XXX --micro
```

**Skipped phases**:
- Phase 2 (Planning)
- Phase 2.5 (Architectural Review)
- Phase 5.5 (Plan Audit)

**Benefits**: 3-5 minutes vs 15+ minutes

## Design-First Workflow

For complex tasks requiring upfront design approval:

```bash
# Design phase
/task-work TASK-XXX --design-only

# [Review and approve plan]

# Implementation phase
/task-work TASK-XXX --implement-only
```

## Clarifying Questions

Phase 1.5 asks targeted questions before planning (complexity ≥3):

**Control flags**:
- `--no-questions` - Skip clarification
- `--with-questions` - Force clarification
- `--defaults` - Use defaults without prompting
- `--answers="1:Y 2:N"` - Inline answers for automation

## Documentation Levels

System automatically adjusts documentation detail based on complexity:

| Level | Complexity | Duration | Use Case |
|-------|-----------|----------|----------|
| **Minimal** | 1-3 | 8-12 min | Simple tasks, fast iteration |
| **Standard** | 4-10 | 12-18 min | Normal development |
| **Comprehensive** | 7-10 or triggers | 36+ min | Security, compliance, complex |

Override with `--docs=minimal|standard|comprehensive`

## Best Practices

1. **Use /task-work for all implementation** - Don't manually code
2. **Trust quality gates** - They catch real issues early
3. **Choose appropriate mode** - TDD for logic, Standard for features
4. **Keep tasks focused** - One feature per task
5. **Review blocked tasks promptly** - Fix and re-run quickly
