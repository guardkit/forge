"""Git adapter package — boundary code for ``git`` subprocess calls.

Currently exposes the declarative result DTOs returned by the adapter
operations defined in TASK-GCI-006:

- :class:`GitOpResult` — per-operation result for ``prepare_worktree``,
  ``commit_all``, ``push``, and ``cleanup_worktree``.
- :class:`PRResult` — result of the ``gh pr create`` call (lives here
  because gh and git share the same adapter return contract; see
  ``docs/design/contracts/API-subprocess.md`` §4).

The re-export pattern matches :mod:`forge.config` so callers can write
``from forge.adapters.git import GitOpResult`` without depending on the
internal module layout.
"""

from forge.adapters.git.models import GitOpResult, PRResult

__all__ = [
    "GitOpResult",
    "PRResult",
]
