---
paths: "**/agent.py, **/domains/**, **/prompts/**"
---

# Domain Prompt Injection

## Overview

Runtime domain context injection via `domains/{domain}/DOMAIN.md` files. The orchestrator system prompt contains a `{domain_prompt}` placeholder that is filled at startup with domain-specific instructions. This keeps the orchestrator domain-agnostic while allowing per-domain customisation without code changes.

## Implementation

### Loading domain context

```python
def _load_domain_prompt(project_root: Path, domain: str) -> str:
    """Read domain-specific guidelines from domains/{domain}/DOMAIN.md."""
    domain_path = project_root / "domains" / domain / "DOMAIN.md"
    try:
        return domain_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Domain file not found at %s — using default prompt.", domain_path)
        return _DEFAULT_DOMAIN_PROMPT
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read domain file %s (%s) — using default prompt.", domain_path, exc)
        return _DEFAULT_DOMAIN_PROMPT
```

### Injecting into the system prompt

```python
# Prompt template with placeholder
ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the **Orchestrator** — the top-level reasoning agent.
...
## Domain-Specific Instructions

{domain_prompt}
"""

# Injection at runtime
system_prompt = ORCHESTRATOR_SYSTEM_PROMPT.format(
    date=today,
    domain_prompt=domain_prompt,
)
```

### Domain selection via CLI

```python
_arg_parser.add_argument(
    "--domain",
    default=DEFAULT_DOMAIN,
    help="Domain name to load from domains/{domain}/DOMAIN.md",
)
```

### Directory structure

```
domains/
├── example-domain/
│   └── DOMAIN.md        # Domain-specific instructions
├── data-pipeline/
│   └── DOMAIN.md        # Data pipeline domain
└── web-app/
    └── DOMAIN.md        # Web application domain
```

## Defensive Fallback Chain

The loading function implements a three-layer fallback:

1. **File found and readable** → return content
2. **FileNotFoundError** → log warning, return default prompt
3. **OSError / UnicodeDecodeError** → log warning, return default prompt

The default prompt ensures the orchestrator always starts:

```python
_DEFAULT_DOMAIN_PROMPT = (
    "No domain-specific guidelines loaded. "
    "Follow general software engineering best practices."
)
```

## When to Use

- Making agent behaviour configurable per domain without code changes
- Supporting multiple use cases from a single orchestrator deployment
- Allowing non-developers to customise agent behaviour via Markdown files

## Best Practices

- Keep DOMAIN.md files focused on instructions, not implementation details
- Always provide a meaningful default prompt for the no-domain case
- Never let domain loading failures prevent the orchestrator from starting
- Use `str.format()` with named placeholders, not f-strings, for prompt templates
- Separate domain content from prompt structure (DOMAIN.md vs prompt constants)
