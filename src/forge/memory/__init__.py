"""``forge.memory`` — credential redaction (the only surviving tier).

Retired 2026-08-03 (Rich's ruling: retire now, wire via factory later).
The Graphiti-backed memory tiers that used to live here — entity models,
the fire-and-forget writer, write-ordering, reconciler, Q&A ingestion,
priors retrieval, session-outcome and supersession — targeted GuardKit's
``graphiti`` CLI group, deleted from guardkit on 2026-07-02, and a
``graphiti_core`` package that is not installed. They were dead code
that misled auditing agents, so they are gone.

What remains, and why:

- :mod:`forge.memory.redaction` — live. Imported by
  ``forge.cli.runbook`` and ``forge.executor.shell_steps`` to scrub
  credentials from process output before it is logged or persisted.
- ``EmptyPriorsReader`` never lived here — it is defined in
  :mod:`forge.gating.degraded` against the
  :class:`forge.gating.wrappers.PriorsReader` protocol, and both are
  untouched by this retirement.

Successor: a factory-built, fleet-memory-backed ``PriorsReader``
(queued; do not hand-build it here).
"""

from __future__ import annotations

from .redaction import redact_credentials

__all__ = [
    "redact_credentials",
]
