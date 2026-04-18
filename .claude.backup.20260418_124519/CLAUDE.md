# GuardKit - Default Template

## When to Use This Template

The **default** template is a **language-agnostic** starting point for projects that don't fit GuardKit's specialized templates.

### Use Default Template For:
- Languages not yet supported (Go, Rust, Ruby, PHP, Kotlin, Swift, etc.)
- Custom tech stacks with unique technology combinations
- Learning/prototyping GuardKit workflows
- Template development using `/template-create`

### Use Specialized Templates Instead:
- **React/TypeScript** → `react-typescript` template
- **Python/FastAPI** → `fastapi-python` template
- **Next.js Full-Stack** → `nextjs-fullstack` template
- **React + FastAPI Monorepo** → `react-fastapi-monorepo` template
- **LangChain/DeepAgents** → `langchain-deepagents` template

**Why?** Specialized templates provide stack-specific quality gates, testing frameworks, pre-configured agents, and optimized workflows.

## Project Structure

```
.claude/                    # Configuration
├── agents/                # (Empty - add your own)
├── commands/              # (Symlinked from global)
├── rules/                 # Pattern documentation
│   ├── code-style.md      # Language-agnostic conventions
│   ├── workflow.md        # GuardKit workflow phases
│   └── quality-gates.md   # Quality standards
└── settings.json          # Documentation level config

tasks/                      # Task management
├── backlog/
├── in_progress/
├── in_review/
├── blocked/
└── completed/

docs/                       # Documentation
├── guides/                # (Created as needed)
└── state/                 # Implementation plans & metrics
```

## Core Workflow

All core commands available via global symlinks:

```bash
# Create and work on tasks
/task-create "Feature description"
/task-work TASK-XXX
/task-complete TASK-XXX
/task-status

# Development modes
/task-work TASK-XXX --mode=tdd
/task-work TASK-XXX --micro
```

## Documentation Level System

The template includes `settings.json` with documentation level configuration:

| Level | Complexity | Duration | Output |
|-------|-----------|----------|--------|
| **Minimal** | 1-3 | 8-12 min | Quick summaries, embedded results |
| **Standard** | 4-10 | 12-18 min | Full reports, comprehensive coverage |
| **Comprehensive** | 7-10 or triggers | 36+ min | Standalone documents, enhanced analysis |

Override with `--docs=minimal|standard|comprehensive`

## Getting Started

### 1. Initialize
```bash
guardkit init default
```

### 2. Customize for Your Stack

**Add testing configuration** (if needed):
```json
// .claude/settings.json
{
  "stack": {
    "language": "go",
    "testing": {
      "command": "go test ./...",
      "coverage_command": "go test -cover ./..."
    }
  }
}
```

**Add stack-specific agents** (optional):
- Place in `.claude/agents/`
- Or use `/template-create` for systematic generation

### 3. Start Working
```bash
/task-create "Your first task"
/task-work TASK-001
```

## Customization Paths

### Quick Customization
Extend `.claude/settings.json` with minimal stack configuration.

### Full Template Creation
For reusable templates, use `/template-create`:

```bash
/template-create

# Creates complete template with:
# - Agents
# - Commands
# - File templates
# - Configuration
# - Testing infrastructure
```

See [Creating Local Templates](../../../docs/guides/creating-local-templates.md)

## Rules Documentation

Detailed patterns and best practices in `.claude/rules/`:

- **code-style.md**: Language-agnostic naming, organization, commenting
- **workflow.md**: GuardKit phases, modes, quality gates
- **quality-gates.md**: Testing philosophy, enforcement, standards

Load rules as needed:
```bash
cat .claude/rules/code-style.md
cat .claude/rules/workflow.md
```

## Migration to Specialized Template

When your project matures, migrate to a specialized template:

1. Identify your stack (React, Python, .NET, etc.)
2. Generate new template or use existing
3. Preserve state: Copy `.claude/state/`, `tasks/`, `docs/`
4. Reinitialize: `guardkit init <specialized-template>`

See [Template Migration Guide](../../../docs/guides/template-migration.md)

## Philosophy

**"Start simple, scale as needed"**

The default template provides core workflow without imposing stack-specific constraints, allowing you to:
- Explore GuardKit with any language
- Build custom templates for specialized stacks
- Prototype quickly before committing to structure
- Develop in languages not yet supported by built-in templates

When ready for more structure, migrate to a specialized template or create your own.

## Support

- **User Guide**: See root `CLAUDE.md` for complete documentation
- **Template Guide**: [Creating Local Templates](../../../docs/guides/creating-local-templates.md)
- **Migration Guide**: [Template Migration Guide](../../../docs/guides/template-migration.md)
