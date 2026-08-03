"""Forge LangChain ``@tool`` layer.

Per ``docs/design/contracts/API-tool-layer.md``, every external tool
exposed to the orchestrator graph is a ``@tool(parse_docstring=True)``
wrapper that returns a JSON string and never raises (ADR-ARCH-025).

The :mod:`forge.tools.guardkit` submodule (TASK-GCI-009) implements the
nine GuardKit subcommand wrappers; the version-control wrappers
(TASK-GCI-007) live in a sibling module. The Graphiti wrappers
(TASK-GCI-010) were retired 2026-08-03 — GuardKit deleted its
``graphiti`` CLI group on 2026-07-02, so they wrapped a command that no
longer exists. Successor = the factory-built fleet-memory PriorsReader
(queued).
"""
