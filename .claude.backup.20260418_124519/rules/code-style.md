---
paths: "**/*.py, **/*.ts, **/*.tsx, **/*.js, **/*.jsx"
---

# Code Style Guidelines

## Language-Agnostic Conventions

These guidelines apply regardless of programming language.

### Naming

- **Descriptive names**: Use clear, meaningful names that convey purpose
- **Avoid abbreviations**: Only use widely understood abbreviations (e.g., ID, URL, API)
- **Consistency**: Follow existing naming patterns within the project
- **Intent over mechanics**: Name based on what something does, not how it does it

**Examples**:
```
✅ getUsersByActiveStatus()
❌ getUsrs() or getUsersWithFlag()

✅ customerEmailAddress
❌ custEmlAddr or str1

✅ MAX_RETRY_ATTEMPTS
❌ MAX_RT or MAX
```

### File Organization

- **Single responsibility**: Each file focused on one concept or feature
- **Logical grouping**: Group related files in directories
- **Clear structure**: Use meaningful directory names
- **Flat when possible**: Avoid deep nesting (max 3-4 levels)

**Example structure**:
```
src/
├── features/          # Feature-based organization
│   ├── users/
│   ├── orders/
│   └── payments/
├── shared/           # Shared utilities
│   ├── utils/
│   └── types/
└── config/           # Configuration
```

### Comments

- **Why, not what**: Explain reasoning, not obvious functionality
- **Keep current**: Update comments when code changes
- **Doc comments**: Use for public APIs and interfaces
- **Remove dead code**: Don't comment out code - use version control

**Examples**:
```
✅ // Retry 3 times because external API is flaky
❌ // Call the API 3 times

✅ // HACK: Workaround for library bug #123 - remove after v2.0
❌ // This doesn't work right
```

## Code Organization

### Module Structure

- **Dependencies first**: Import/require statements at top
- **Constants next**: Define constants before usage
- **Main logic**: Core functionality in the middle
- **Exports last**: Export statements at bottom

### Function Size

- **Keep functions small**: Aim for <20 lines when possible
- **Single purpose**: Each function does one thing well
- **Extract complex logic**: Break down complicated operations
- **Early returns**: Reduce nesting with guard clauses

### Error Handling

- **Explicit errors**: Don't hide failures silently
- **Fail fast**: Validate inputs early
- **Meaningful messages**: Include context in error messages
- **Consistent patterns**: Use same error handling approach throughout

## Best Practices

### DRY Principle
- Don't repeat logic - extract to functions/modules
- Share common code across files
- Create utilities for repeated patterns

### YAGNI Principle
- Implement what's needed now
- Don't build for hypothetical future requirements
- Add complexity only when required

### KISS Principle
- Simple solutions over clever ones
- Readable code over compact code
- Obvious logic over optimized obscurity

## Testing Considerations

- **Testable code**: Write code that's easy to test
- **Pure functions**: Prefer functions without side effects
- **Dependency injection**: Make dependencies explicit
- **Avoid globals**: Minimize global state
