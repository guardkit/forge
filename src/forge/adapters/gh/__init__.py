"""gh adapter package — boundary code for ``gh`` subprocess calls.

Currently exposes :func:`create_pr` (TASK-GCI-007), the thin wrapper around
``gh pr create`` that returns a :class:`forge.adapters.git.models.PRResult`
and converts a missing ``GH_TOKEN`` env var into a structured failure
rather than letting ``gh`` exit with a confusing prompt.

The result DTO (:class:`PRResult`) lives in :mod:`forge.adapters.git.models`
because gh and git share the same adapter return contract; see
``docs/design/contracts/API-subprocess.md`` §4.
"""

from forge.adapters.gh.operations import create_pr

__all__ = ["create_pr"]
