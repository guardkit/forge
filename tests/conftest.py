"""Root test conftest — THE BROKER ISOLATION FENCE (register 2a2, 2026-07-28).

Why this exists (the receipts): running ``pytest tests/`` on a host whose
shell profile exports ``FORGE_NATS_URL`` published REAL ``build_queued``
envelopes to the LIVE production broker THREE times across 2026-07-27/28
(junk builds FEAT-D70CDF / FEAT-3E094C / FEAT-089E6F / FEAT-812C4B — two
reached the production approval gate and the operator's Slack). The
wire-smoke test opts in by ENV PRESENCE, so an operator's convenience
export silently arms it.

The fence: unless ``FORGE_ALLOW_LIVE_BROKER=1`` is EXPLICITLY set, every
broker-address env var is overridden — before any test module imports —
to an unroutable address so accidental broker-reaching code fails fast
instead of touching production. Overriding (not deleting) also defuses
test code whose DEFAULT is ``nats://localhost:4222``.

This is module-level deliberately: pytest imports conftest.py before
collecting test modules, and the wire-smoke module reads its env at
import time.
"""

from __future__ import annotations

import os

#: Explicit, name-your-intent opt-in for tests that genuinely need a live
#: broker (the operator sets it for an attended integration run, never CI).
LIVE_BROKER_OPT_IN_ENV = "FORGE_ALLOW_LIVE_BROKER"

#: Unroutable fail-fast address (port 1 never carries NATS).
DEAD_BROKER_URL = "nats://127.0.0.1:1"

if os.environ.get(LIVE_BROKER_OPT_IN_ENV) != "1":
    os.environ["FORGE_NATS_URL"] = DEAD_BROKER_URL
    os.environ["NATS_URL"] = DEAD_BROKER_URL
