"""AC-4 alias for the F010F safety-net publish suite (TASK-FRR-PEB-005).

The F010F (TASK-FORGE-FRR-F010F) safety-net publish tests have always
lived in :mod:`tests.forge.test_pipeline_consumer_dispatch_failure_publish`.
TASK-FRR-PEB-005 AC-4 cites the path
``tests/forge/test_safety_net_publish.py`` — this module exists so the
AC-cited path resolves to a real, runnable test module.

The implementation is a plain ``from … import *`` re-export of the
F010F test classes and their module-level fixtures. pytest collects the
re-exported classes (``TestDispatchRaisePublishesBuildFailed``,
``TestPublishFailureStillAcks``, ``TestHappyPathDoesNotPublish``) under
this filename and reuses the inherited fixtures (``allowlist_root``,
``feature_yaml``, ``forge_config``) bound by name.

This alias is **deliberately stub-thin** — duplicating the F010F
assertions here would violate the no-F010F-production-touch contract
(AC-4) by creating a second source of truth for the safety-net publish
behaviour. Re-export keeps the canonical assertions in the original
file while letting AC-4's path-citation resolve.

Why not rename the original?
----------------------------

Renaming ``tests/forge/test_pipeline_consumer_dispatch_failure_publish.py``
would touch F010F's owning test file (still violating AC-4 in spirit)
and would also break any direct imports of those test classes from
linked CI machinery. Re-exporting under the AC-cited name is the
zero-risk operation.

References:
    * TASK-FORGE-FRR-F010F — sync-raise safety-net publish
      (canonical assertions live in the original file).
    * TASK-FRR-PEB-005 AC-4 — references this exact filename.
"""

# ruff: noqa: F401,F403
from __future__ import annotations

from tests.forge.test_pipeline_consumer_dispatch_failure_publish import (
    INBOUND_CORRELATION_ID,
    FEATURE_ID,
    TestDispatchRaisePublishesBuildFailed,
    TestHappyPathDoesNotPublish,
    TestPublishFailureStillAcks,
    allowlist_root,
    feature_yaml,
    forge_config,
)
