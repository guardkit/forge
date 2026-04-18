# ADR-ARCH-004: Full GuardKit CLI as tool surface — one `@tool` per subcommand

- **Status:** Accepted
- **Date:** 2026-04-18
- **Session:** `/system-arch` Category 2 Revision 3

## Context

The refresh doc (v3) lists Forge as invoking `/system-arch`, `/system-design`, `/feature-spec`, `/feature-plan`, `autobuild`, `/task-review` among its tools. Initial Category 2 framing scoped the GuardKit adapter to AutoBuild only. Rich's Revision 3 correction: Forge should be able to invoke **every** GuardKit slash command, because Mode A (Greenfield), Mode B (Feature), and Mode C (Review-Fix) exercise different subsets of the CLI. The history-file corpus across specialist-agent, nats-core, and nats-infrastructure confirms the command set in active use.

## Decision

Expose the full GuardKit CLI as individual DeepAgents tools:

| `@tool` | Command |
|---|---|
| `guardkit_system_arch` | `/system-arch` |
| `guardkit_system_design` | `/system-design` |
| `guardkit_system_plan` | `/system-plan` |
| `guardkit_feature_spec` | `/feature-spec` |
| `guardkit_feature_plan` | `/feature-plan` |
| `guardkit_task_review` | `/task-review` |
| `guardkit_task_work` | `/task-work` |
| `guardkit_task_complete` | `/task-complete` |
| `guardkit_autobuild` | `guardkit autobuild feature … --nats` |
| `guardkit_graphiti_seed` | `guardkit graphiti add-context` |
| `guardkit_graphiti_query` | `guardkit graphiti query` |

Each tool is a thin `@tool(parse_docstring=True)` wrapper around `forge.adapters.guardkit`, which composes the subcommand + args + progress-stream handling on top of DeepAgents' built-in `execute` (ADR-ARCH-020).

**Forge also produces its own history files** using the same Pattern-2/Pattern-3 format from `fleet-master-index.md` — `command_history.md`, `feature-spec-{NAME}-history.md`, per-stage logs — feeding the next project's calibration corpus.

## Consequences

- **+** Reasoning model can compose Mode A/B/C workflows by choosing from a flat tool inventory — natural DeepAgents pattern.
- **+** Each command's args + expected outputs documented in the tool's docstring (per `langchain-tool-decorator-specialist` rule); reasoning model reads docstrings as capability descriptions.
- **+** Mode C (review-fix) is first-class supported via `guardkit_task_review` + `guardkit_task_work` + `guardkit_task_complete` — not retrofitted.
- **+** History files Forge produces feed the next project's Forge calibration — the factory teaches itself.
- **−** 11 tool descriptors in the DeepAgents system prompt — context cost; mitigated by DeepAgents' auto-summarisation (ADR-ARCH-020).
- **−** Forge's adapter must parse GuardKit's stdout consistently across 11 commands; contract-level work to handle inconsistencies (e.g. `/feature-spec` output format vs `autobuild` output format).
